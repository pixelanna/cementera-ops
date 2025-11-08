import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import math
import os, json, base64, requests

# ==== Helpers de integridad y reparación de DB ====
import time, shutil

def _sqlite_integrity_ok(conn):
    try:
        row = conn.execute("PRAGMA integrity_check;").fetchone()
        return bool(row) and str(row[0]).strip().lower() == "ok"
    except Exception:
        return False

def rebuild_empty_db(db_path, conn):
    """Cierra conexión, renombra DB dañada y crea una nueva vacía."""
    try:
        conn.close()
    except Exception:
        pass
    ts = time.strftime("%Y%m%d-%H%M%S")
    try:
        shutil.move(db_path, f"{db_path}.bad.{ts}")
    except Exception:
        pass
    new_conn = sqlite3.connect(db_path, check_same_thread=False)
    return new_conn

# (Opcional) borrar la DB del Gist para romper ciclos con un archivo corrupto.
def delete_db_in_gist():
    if not (GIST_ID and GITHUB_TOKEN):
        return False, "Faltan secrets (GIST_ID/GITHUB_TOKEN)"
    try:
        payload = {"files": {DB_FILE: None}}
        r = requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=_gh_headers(),
            data=json.dumps(payload),
            timeout=30
        )
        if r.status_code in (200, 201):
            return True, "Archivo eliminado del Gist"
        return False, f"{r.status_code}: {r.text}"
    except Exception as e:
        return False, f"Error al eliminar en Gist: {e}"

# === Secrets desde Streamlit Cloud ===
GIST_ID = st.secrets.get("GIST_ID")
GITHUB_TOKEN = st.secrets.get("GITHUB_TOKEN")
DB_FILE = st.secrets.get("DB_FILE", "cementera.db")

def _gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

def restore_db_from_gist():
    """
    Descarga cementera.db desde el Gist y lo escribe localmente.
    Guardamos el archivo en el Gist como BASE64 (texto). Si alguna vez
    quedó subido en binario crudo, también lo soporta (detecta y escribe).
    """
    if not (GIST_ID and GITHUB_TOKEN):
        return False, "Faltan secrets (GIST_ID/GITHUB_TOKEN)"
    try:
        g = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=_gh_headers(), timeout=20)
        g.raise_for_status()
        files = g.json().get("files", {})
        meta = files.get(DB_FILE)
        if not meta or not meta.get("raw_url"):
            return False, "Archivo aún no existe en el Gist (primer backup lo creará)"
        r = requests.get(meta["raw_url"], timeout=30)
        r.raise_for_status()
        raw = r.content  # bytes; puede ser base64 (texto) o binario
        try:
            blob = base64.b64decode(raw)  # si es base64, decodifica a binario
        except Exception:
            blob = raw  # ya es binario
        with open(DB_FILE, "wb") as f:
            f.write(blob)
        return True, "Restaurado desde Gist"
    except Exception as e:
        return False, f"Error al restaurar: {e}"

def backup_db_to_gist():
    """
    Sube/actualiza cementera.db al Gist como BASE64.
    Si hay error 409 (Conflict), elimina el archivo y reintenta.
    """
    if not (GIST_ID and GITHUB_TOKEN):
        return False, "Faltan secrets (GIST_ID/GITHUB_TOKEN)"
    try:
        with open(DB_FILE, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        payload = {"files": {DB_FILE: {"content": b64}}}

        # Primer intento
        r = requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=_gh_headers(),
            data=json.dumps(payload),
            timeout=30
        )

        # Si funcionó, genial
        if r.status_code == 200:
            return True, "Respaldado en Gist"

        # Si hay conflicto 409, borramos el archivo y subimos de nuevo
        if r.status_code == 409:
            del_payload = {"files": {DB_FILE: None}}
            requests.patch(
                f"https://api.github.com/gists/{GIST_ID}",
                headers=_gh_headers(),
                data=json.dumps(del_payload),
                timeout=30
            )
            r2 = requests.patch(
                f"https://api.github.com/gists/{GIST_ID}",
                headers=_gh_headers(),
                data=json.dumps(payload),
                timeout=30
            )
            if r2.status_code == 200:
                return True, "Respaldado en Gist tras resolver 409"
            else:
                return False, f"409 persistente ({r2.status_code})"

        # Cualquier otro error
        return False, f"{r.status_code}: {r.text}"

    except Exception as e:
        return False, f"Error al respaldar: {e}"

from datetime import datetime, timedelta
import pandas as pd

# --- Si no lo tienes: leer parámetro con default
def get_param(conn, name, default):
    try:
        val = pd.read_sql(
            "SELECT valor FROM parametros WHERE lower(nombre)=lower(?)", conn, params=(name,)
        ).iloc[0, 0]
        return float(str(val).replace(",", "."))
    except Exception:
        return float(default)

# --- Si no lo tienes: verificador de solapes (usa columnas hora_S, hora_T, hora_X)
def _dt(fecha_yyyy_mm_dd: str, hhmm: str) -> datetime:
    return datetime.strptime(f"{fecha_yyyy_mm_dd} {hhmm}", "%Y-%m-%d %H:%M")

def _overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
    return max(a0, b0) < min(a1, b1)

def check_conflicts(conn, fecha_str: str, mixer_id: int, dosif_codigo: str,
                    S: datetime, T: datetime, X: datetime, exclude_agenda_id=None):
    cur = conn.cursor()

    # Mixer: [S..X]
    if exclude_agenda_id is None:
        cur.execute("""SELECT id, proyecto, hora_S, hora_X
                       FROM agenda WHERE fecha=? AND mixer_id=?""",
                    (fecha_str, int(mixer_id)))
    else:
        cur.execute("""SELECT id, proyecto, hora_S, hora_X
                       FROM agenda WHERE fecha=? AND mixer_id=? AND id<>?""",
                    (fecha_str, int(mixer_id), int(exclude_agenda_id)))
    conf_mixer = []
    for aid, proy, s2, x2 in cur.fetchall():
        try:
            S2, X2 = _dt(fecha_str, s2), _dt(fecha_str, x2)
            if _overlap(S, X, S2, X2):
                conf_mixer.append(f"#{aid} {proy} [S:{s2}→X:{x2}]")
        except:
            pass

    # Dosif: [S..T]
    conf_dosif = []
    if dosif_codigo:
        if exclude_agenda_id is None:
            cur.execute("""SELECT id, proyecto, hora_S, hora_T
                           FROM agenda WHERE fecha=? AND dosif_codigo=?""",
                        (fecha_str, dosif_codigo))
        else:
            cur.execute("""SELECT id, proyecto, hora_S, hora_T
                           FROM agenda WHERE fecha=? AND dosif_codigo=? AND id<>?""",
                        (fecha_str, dosif_codigo, int(exclude_agenda_id)))
        for aid, proy, s2, t2 in cur.fetchall():
            try:
                S2, T2 = _dt(fecha_str, s2), _dt(fecha_str, t2)
                if _overlap(S, T, S2, T2):
                    conf_dosif.append(f"#{aid} {proy} [S:{s2}→T:{t2}]")
            except:
                pass

    return conf_mixer, conf_dosif

# --- Dosificadoras habilitadas
def dosif_habilitadas(conn):
    df = pd.read_sql("SELECT codigo FROM dosif WHERE habilitado=1 ORDER BY codigo", conn)
    return df["codigo"].tolist()

# --- SOLO mixers STD habilitados (excluye SANY) para el auto
def mixers_auto_std(conn):
    df = pd.read_sql("""SELECT id, unidad_id, placa, capacidad_m3, tipo, habilitado
                        FROM mixers WHERE habilitado=1 ORDER BY capacidad_m3 DESC, id""", conn)
    df = df[df["tipo"].astype(str).str.upper() != "SANY"]  # excluir SANY
    return [dict(row) for _, row in df.iterrows()]

# --- Buscar una combinación factible (mixer STD + dosif + Q) moviendo Q si hay choques
def asignar_viaje_factible(conn, fecha_sel: str, Q_str: str, volumen_m3: float,
                           min_ida: int, tiempo_descarga_min: int,
                           margen_lavado_min: int, tiempo_cambio_obra_min: int):
    intervalo_min = int(get_param(conn, "Intervalo_min", 15))
    mixers = mixers_auto_std(conn)
    dosifs = dosif_habilitadas(conn)
    if not mixers or not dosifs:
        return None

    Q_dt = datetime.strptime(f"{fecha_sel} {Q_str}", "%Y-%m-%d %H:%M")
    intentos = 0
    while intentos < 8*24:  # tope razonable
        for m in mixers:
            mid = int(m["id"])
            cap = float(m["capacidad_m3"])
            vol = min(float(volumen_m3), cap)

            # calcular_tiempos: ya la tienes (Q -> R,S,T,U,V,W,X)
            R, S, T, U, V, W, X = calcular_tiempos(
                Q_dt.strftime("%H:%M"), int(min_ida), float(vol),
                int(tiempo_descarga_min), int(margen_lavado_min), int(tiempo_cambio_obra_min)
            )
            S_dt = _dt(fecha_sel, S.strftime("%H:%M"))
            T_dt = _dt(fecha_sel, T.strftime("%H:%M"))
            X_dt = _dt(fecha_sel, X.strftime("%H:%M"))

            for dcode in dosifs:
                conf_mx, conf_df = check_conflicts(conn, fecha_sel, mid, dcode, S_dt, T_dt, X_dt, None)
                if not conf_mx and not conf_df:
                    return {
                        "mixer_id": mid, "dosif": dcode, "volumen_m3": vol,
                        "Q": Q_dt, "R": R, "S": S, "T": T, "U": U, "V": V, "W": W, "X": X
                    }

        Q_dt = Q_dt + timedelta(minutes=intervalo_min)
        intentos += 1

    return None

# --- Planificador por volumen total (solo STD). SANY queda para edición manual.
def planificar_proyecto_auto(conn, cliente: str, proyecto: str, fecha_sel: str,
                             hora_Q_ini: str, volumen_total: float,
                             min_ida: int, tiempo_descarga_min: int,
                             requiere_bomba: str):
    margen_lavado_min      = int(get_param(conn, "Margen_lavado_min", 5))
    tiempo_cambio_obra_min = int(get_param(conn, "Tiempo_cambio_obra_min", 4))

    # Avisar si no hay STD
    if len(mixers_auto_std(conn)) == 0:
        raise ValueError("No hay mixers STD habilitados. Usa el editor para asignar SANY manualmente.")

    creado = []
    restante = float(volumen_total)
    Q_actual = datetime.strptime(f"{fecha_sel} {hora_Q_ini}", "%Y-%m-%d %H:%M")

    while restante > 0.001:
        asign = asignar_viaje_factible(
            conn, fecha_sel, Q_actual.strftime("%H:%M"),
            restante, min_ida, tiempo_descarga_min, margen_lavado_min, tiempo_cambio_obra_min
        )
        if asign is None:
            raise ValueError("No se encontró disponibilidad (mixers/dosif ocupados).")

        vol = float(asign["volumen_m3"])
        fecha_hora_q = f"{fecha_sel} {asign['Q'].strftime('%H:%M')}"
        ciclo_total_min   = int((asign["X"] - asign["S"]).total_seconds() // 60)
        min_viaje_regreso = int(min_ida)

        cur = conn.cursor()
        cur.execute("""
            INSERT INTO agenda (
                cliente, proyecto, fecha, hora_Q, min_viaje_ida, volumen_m3, estado,
                requiere_bomba, dosificadora, dosif_codigo, mixer_id,
                hora_R, hora_S, hora_T, hora_U, hora_V, hora_W, hora_X,
                fecha_hora_q, ciclo_total_min, min_viaje_regreso
            ) VALUES (?, ?, ?, ?, ?, ?, 'Programado', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            cliente, proyecto, fecha_sel, asign["Q"].strftime("%H:%M"), int(min_ida), vol,
            requiere_bomba, asign["dosif"], asign["dosif"], int(asign["mixer_id"]),
            asign["R"].strftime("%H:%M"), asign["S"].strftime("%H:%M"), asign["T"].strftime("%H:%M"),
            asign["U"].strftime("%H:%M"), asign["V"].strftime("%H:%M"), asign["W"].strftime("%H:%M"), asign["X"].strftime("%H:%M"),
            fecha_hora_q, ciclo_total_min, min_viaje_regreso
        ))
        conn.commit()
        try:
            ok, msg = backup_db_to_gist()
            try: st.toast("✅ Respaldo OK en GitHub" if ok else f"⚠️ "+msg)
            except: pass
        except: pass

        creado.append({
            "Mixer_ID": int(asign["mixer_id"]),
            "Dosif": asign["dosif"],
            "m3": vol,
            "Q_llega": asign["Q"].strftime("%H:%M"),
            "R_sale": asign["R"].strftime("%H:%M"),
            "S_ini_carga": asign["S"].strftime("%H:%M"),
            "T_fin_carga": asign["T"].strftime("%H:%M"),
            "U_fin_desc": asign["U"].strftime("%H:%M"),
            "X_fin_total": asign["X"].strftime("%H:%M"),
        })

        # siguiente llegada a obra espaciada por tiempo de descarga
        Q_actual = Q_actual + timedelta(minutes=int(tiempo_descarga_min))
        restante = max(0.0, restante - vol)

    return pd.DataFrame(creado)

st.set_page_config(page_title="Cementera OPS", layout="wide")
st.title("🚧 Constructora ETERNA | División CONETSA - Plantel Olímpico - v0.1")

# === Restaurar DB antes de abrir conexión ===
_ = restore_db_from_gist()

conn = sqlite3.connect(DB_FILE, check_same_thread=False)
c = conn.cursor()

# ---------------------------------------------------
# Conexión a SQLite (cacheada para Streamlit Cloud)
# ---------------------------------------------------
@st.cache_resource
def get_conn():
    # check_same_thread=False para permitir uso en Streamlit
    conn = sqlite3.connect("cementera.db", check_same_thread=False)
    return conn

conn = get_conn()
c = conn.cursor()

# ---------------------------------------------------
# Asegurar esquema (robusto ante conflictos previos)
# ---------------------------------------------------
def _object_kind(conn, name):
    row = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return row[0] if row else None  # "table" | "view" | "index" | None

def _table_cols(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]

def _has_col(conn, table, col):
    return col in _table_cols(conn, table)

def ensure_schema(conn):
    c = conn.cursor()

   # ---------------------------------------------------
# Asegurar esquema (robusto ante conflictos previos)
# ---------------------------------------------------
def _object_kind(conn, name: str):
    row = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return row[0] if row else None  # "table" | "view" | "index" | None

def _table_cols(conn, table: str):
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []

def _has_col(conn, table: str, col: str) -> bool:
    return col in _table_cols(conn, table)

def _safe_add_col(conn, table: str, coldef: str):
    colname = coldef.split()[0]
    if not _has_col(conn, table, colname):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")

def ensure_schema(conn):
    c = conn.cursor()

    # ---- 1) parametros ----
    kind = _object_kind(conn, "parametros")
    if kind is None:
        c.execute("""
        CREATE TABLE parametros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT UNIQUE,
            valor TEXT
        )""")
    elif kind == "table":
        cols = _table_cols(conn, "parametros")
        if "nombre" not in cols:
            c.execute("ALTER TABLE parametros RENAME TO parametros_old")
            c.execute("""
            CREATE TABLE parametros (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT UNIQUE,
                valor TEXT
            )""")
            old_cols = set(_table_cols(conn, "parametros_old"))
            common = [cname for cname in ["id", "nombre", "valor"] if cname in old_cols]
            if common:
                cols_csv = ",".join(common)
                c.execute(f"INSERT OR IGNORE INTO parametros ({cols_csv}) SELECT {cols_csv} FROM parametros_old")
            c.execute("DROP TABLE parametros_old")
        else:
            _safe_add_col(conn, "parametros", "valor TEXT")
    else:
        if kind == "view":
            c.execute("DROP VIEW parametros")
        elif kind == "index":
            c.execute("DROP INDEX parametros")
        c.execute("""
        CREATE TABLE IF NOT EXISTS parametros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT UNIQUE,
            valor TEXT
        )""")

    # ---- 2) mixers ----
    kind = _object_kind(conn, "mixers")
    if kind is None:
        c.execute("""
        CREATE TABLE mixers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            placa TEXT,
            activo INTEGER,
            habilitado INTEGER,
            capacidad_m3 REAL,
            tipo TEXT,
            unidad_id TEXT
        )""")
    elif kind == "table":
        _safe_add_col(conn, "mixers", "placa TEXT")
        _safe_add_col(conn, "mixers", "activo INTEGER")
        _safe_add_col(conn, "mixers", "habilitado INTEGER")
        _safe_add_col(conn, "mixers", "capacidad_m3 REAL")
        _safe_add_col(conn, "mixers", "tipo TEXT")
        _safe_add_col(conn, "mixers", "unidad_id TEXT")
    else:
        if kind == "view":
            c.execute("DROP VIEW mixers")
        elif kind == "index":
            c.execute("DROP INDEX mixers")
        c.execute("""
        CREATE TABLE IF NOT EXISTS mixers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            placa TEXT,
            activo INTEGER,
            habilitado INTEGER,
            capacidad_m3 REAL,
            tipo TEXT,
            unidad_id TEXT
        )""")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mixers_unidad ON mixers(unidad_id)")

    # ---- 3) dosif ----
    kind = _object_kind(conn, "dosif")
    if kind is None:
        c.execute("""
        CREATE TABLE dosif (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo TEXT,
            habilitado INTEGER
        )""")
    elif kind == "table":
        _safe_add_col(conn, "dosif", "codigo TEXT")
        _safe_add_col(conn, "dosif", "habilitado INTEGER")
        if not _has_col(conn, "dosif", "codigo"):
            c.execute("ALTER TABLE dosif RENAME TO dosif_old")
            c.execute("""
            CREATE TABLE dosif (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                codigo TEXT,
                habilitado INTEGER
            )""")
            old_cols = set(_table_cols(conn, "dosif_old"))
            common = [cname for cname in ["id", "codigo", "habilitado"] if cname in old_cols]
            if common:
                cols_csv = ",".join(common)
                c.execute(f"INSERT OR IGNORE INTO dosif ({cols_csv}) SELECT {cols_csv} FROM dosif_old")
            c.execute("DROP TABLE dosif_old")
    else:
        if kind == "view":
            c.execute("DROP VIEW dosif")
        elif kind == "index":
            c.execute("DROP INDEX dosif")
        c.execute("""
        CREATE TABLE IF NOT EXISTS dosif (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo TEXT,
            habilitado INTEGER
        )""")

    # ---- 4) agenda ----
    kind = _object_kind(conn, "agenda")
    if kind is None:
        c.execute("""
        CREATE TABLE agenda (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente TEXT,
            proyecto TEXT,
            fecha TEXT,
            hora_Q TEXT,
            min_viaje_ida INTEGER,
            volumen_m3 REAL,
            requiere_bomba TEXT,
            dosificadora TEXT,
            dosif_codigo TEXT,
            mixer_id INTEGER,
            hora_R TEXT, hora_S TEXT, hora_T TEXT, hora_U TEXT, hora_V TEXT, hora_W TEXT, hora_X TEXT,
            estado TEXT,
            fecha_hora_q TEXT,
            ciclo_total_min INTEGER,
            min_viaje_regreso INTEGER
        )""")
    elif kind == "table":
        for coldef in [
            "cliente TEXT",
            "proyecto TEXT",
            "fecha TEXT",
            "hora_Q TEXT",
            "min_viaje_ida INTEGER",
            "volumen_m3 REAL",
            "requiere_bomba TEXT",
            "dosificadora TEXT",
            "dosif_codigo TEXT",
            "mixer_id INTEGER",
            "hora_R TEXT", "hora_S TEXT", "hora_T TEXT", "hora_U TEXT", "hora_V TEXT", "hora_W TEXT", "hora_X TEXT",
            "estado TEXT",
            "fecha_hora_q TEXT",
            "ciclo_total_min INTEGER",
            "min_viaje_regreso INTEGER",
        ]:
            _safe_add_col(conn, "agenda", coldef)
    else:
        if kind == "view":
            c.execute("DROP VIEW agenda")
        elif kind == "index":
            c.execute("DROP INDEX agenda")
        c.execute("""
        CREATE TABLE IF NOT EXISTS agenda (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente TEXT,
            proyecto TEXT,
            fecha TEXT,
            hora_Q TEXT,
            min_viaje_ida INTEGER,
            volumen_m3 REAL,
            requiere_bomba TEXT,
            dosificadora TEXT,
            dosif_codigo TEXT,
            mixer_id INTEGER,
            hora_R TEXT, hora_S TEXT, hora_T TEXT, hora_U TEXT, hora_V TEXT, hora_W TEXT, hora_X TEXT,
            estado TEXT,
            fecha_hora_q TEXT,
            ciclo_total_min INTEGER,
            min_viaje_regreso INTEGER
        )""")

    conn.commit()
    ok, msg = backup_db_to_gist()
try:
    st.toast("✅ Respaldo OK en GitHub" if ok else f"⚠️ {msg}")
except Exception:
    pass

# 1) Probar integridad y asegurar esquema
try:
    # Si la DB se abrió pero está mal, integrity_check lo detecta
    if not _sqlite_integrity_ok(conn):
        # reconstruir limpia local
        conn = rebuild_empty_db(DB_FILE, conn)
    ensure_schema(conn)
except sqlite3.DatabaseError:
    # Si falló incluso al crear esquema, reconstruir y reintentar una vez
    conn = rebuild_empty_db(DB_FILE, conn)
    ensure_schema(conn)

def recalc_and_update_agenda(conn, agenda_id, fecha_str, hora_Q, min_viaje_ida, volumen_m3,
                             requiere_bomba, dosif_codigo, mixer_id):
    """Recalcula R..X y actualiza la fila en agenda."""
    # Lee parámetros
    tiempo_descarga_min    = float(get_param(conn, "Tiempo_descarga_min",    20))
    margen_lavado_min      = float(get_param(conn, "Margen_lavado_min",      10))
    tiempo_cambio_obra_min = float(get_param(conn, "Tiempo_cambio_obra_min", 5))

    # Recalcular
    R, S, T, U, V, W, X = calcular_tiempos(
        hora_Q,
        int(min_viaje_ida),
        float(volumen_m3),
        int(tiempo_descarga_min),
        int(margen_lavado_min),
        int(tiempo_cambio_obra_min),
    )
    fecha_hora_q = f"{fecha_str} {hora_Q}"
    ciclo_total_min = int((X - S).total_seconds() // 60)
    min_viaje_regreso = int(min_viaje_ida)

    cur = conn.cursor()
    cur.execute("""
        UPDATE agenda
        SET cliente = cliente,      -- no lo tocamos aquí (puedes ampliarlo si quieres)
            proyecto = proyecto,    -- idem
            fecha = ?, hora_Q = ?, min_viaje_ida = ?, volumen_m3 = ?, requiere_bomba = ?,
            dosificadora = ?, mixer_id = ?,
            hora_R = ?, hora_S = ?, hora_T = ?, hora_U = ?, hora_V = ?, hora_W = ?, hora_X = ?,
            estado = 'Programado', fecha_hora_q = ?, ciclo_total_min = ?, min_viaje_regreso = ?, dosif_codigo = ?
        WHERE id = ?
    """, (
        fecha_str, hora_Q, int(min_viaje_ida), float(volumen_m3), requiere_bomba,
        dosif_codigo, int(mixer_id),
        R.strftime("%H:%M"), S.strftime("%H:%M"), T.strftime("%H:%M"),
        U.strftime("%H:%M"), V.strftime("%H:%M"), W.strftime("%H:%M"), X.strftime("%H:%M"),
        fecha_hora_q, ciclo_total_min, min_viaje_regreso, dosif_codigo,
        int(agenda_id)
    ))
    conn.commit()

    ok, msg = backup_db_to_gist()
    try:
        st.toast("✅ Respaldo OK en GitHub" if ok else f"⚠️ {msg}")
    except Exception:
        pass

def get_param(conn, name: str, default=None):
    """
    Lee un parámetro por nombre (case-insensitive).
    Si no existe y se pasa default, lo crea y devuelve default.
    """
    cur = conn.cursor()
    cur.execute("SELECT valor FROM parametros WHERE lower(nombre)=lower(?)", (name,))
    row = cur.fetchone()
    if row is not None:
        return row[0]
    if default is not None:
        cur.execute("INSERT INTO parametros (nombre, valor) VALUES (?, ?)", (name, default))
        conn.commit()

        ok, msg = backup_db_to_gist()
        try:
            st.toast("✅ Respaldo OK en GitHub" if ok else f"⚠️ {msg}")
        except Exception:
            pass
        
        return default
    raise ValueError(f"Falta el parámetro '{name}'. Agrega este parámetro en la pestaña Parámetros.")

def ensure_required_params(conn):
    """Garantiza que existan los parámetros clave con defaults sensatos."""
    defaults = {
        "Tiempo_descarga_min": 20,
        "Margen_lavado_min": 10,
        "Tiempo_cambio_obra_min": 5,
        # Si quieres guardar explícito el base de carga:
        "Tiempo_carga_min": 11,           # base para 8.5 m³ (la carga real se escala)
        "Capacidad_mixer_m3": 8.5,        # referencial
        "Intervalo_min": 15
    }
    cur = conn.cursor()
    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO parametros (nombre, valor) VALUES (?, ?)", (k, v))
    conn.commit()

    ok, msg = backup_db_to_gist()
    try:
        st.toast("✅ Respaldo OK en GitHub" if ok else f"⚠️ {msg}")
    except Exception:
        pass

from datetime import time

def parse_hhmm(hhmm: str) -> time:
    return datetime.strptime(hhmm, "%H:%M").time()

def combine_date_time_str(date_str: str, hhmm: str) -> datetime:
    # date_str = "YYYY-MM-DD"
    return datetime.strptime(f"{date_str} {hhmm}", "%Y-%m-%d %H:%M")

def build_slots_15(date_str: str, start="00:00", end="23:59"):
    """Devuelve lista de datetimes cada 15 min del día."""
    start_dt = datetime.strptime(f"{date_str} {start}", "%Y-%m-%d %H:%M")
    end_dt   = datetime.strptime(f"{date_str} {end}",   "%Y-%m-%d %H:%M")
    out = []
    cur = start_dt
    while cur <= end_dt:
        out.append(cur)
        cur += timedelta(minutes=15)
    return out

def mark_busy(slots: list[datetime], busy_ranges: list[tuple[datetime, datetime]]):
    """Recibe slots 15' y una lista de (ini, fin) ocupados. Retorna lista de '■'/'·' por slot."""
    marks = []
    for s in slots:
        occupied = any(start <= s < end for (start, end) in busy_ranges)
        marks.append("■" if occupied else "·")
    return marks

def mixer_busy_ranges_for_day(conn, mixer_id: int, date_str: str):
    """
    Construye rangos ocupados [S..X] de AGENDA para un mixer en ese día.
    S=hora_S, X=hora_X (ambas en HH:MM).
    """
    df = pd.read_sql("""
        SELECT fecha, hora_S, hora_X
        FROM agenda
        WHERE mixer_id = ? AND fecha = ?
    """, conn, params=(mixer_id, date_str))
    ranges = []
    for _, r in df.iterrows():
        s = combine_date_time_str(r["fecha"], r["hora_S"])
        x = combine_date_time_str(r["fecha"], r["hora_X"])
        ranges.append((s, x))
    return ranges

def dosif_busy_ranges_for_day(conn, dosif_codigo: str, date_str: str):
    """
    Rango ocupado de la dosificadora según ventanas de carga [S..T]
    """
    df = pd.read_sql("""
        SELECT fecha, hora_S, hora_T
        FROM agenda
        WHERE dosif_codigo = ? AND fecha = ?
    """, conn, params=(dosif_codigo, date_str))
    ranges = []
    for _, r in df.iterrows():
        s = combine_date_time_str(r["fecha"], r["hora_S"])
        t = combine_date_time_str(r["fecha"], r["hora_T"])
        ranges.append((s, t))
    return ranges

# --- PATCH: columnas extra para agenda (si no existen) ---
cur = conn.cursor()
cur.execute("PRAGMA table_info(agenda)")
agenda_cols = [r[1].lower() for r in cur.fetchall()]

def ensure_col(table, coldef):
    # coldef ejemplo: "estado TEXT"
    colname = coldef.split()[0]
    if colname.lower() not in agenda_cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
        conn.commit()

        ok, msg = backup_db_to_gist()
        try:
            st.toast("✅ Respaldo OK en GitHub" if ok else f"⚠️ {msg}")
        except Exception:
            pass

# Estado y trazas útiles (texto/num) — evita romper lo existente
ensure_col("agenda", "estado TEXT")
ensure_col("agenda", "fecha_hora_q TEXT")            # Fecha y hora Q combinadas
ensure_col("agenda", "ciclo_total_min INTEGER")      # (X - S) en minutos
ensure_col("agenda", "min_viaje_regreso INTEGER")    # igual a ida salvo que quieras cambiar
# Si quieres guardar código normalizado de dosif:
ensure_col("agenda", "dosif_codigo TEXT")            # ej. DF-01, DF-06

def upsert_param(conn, nombre, valor):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO parametros (nombre, valor)
        VALUES (?, ?)
        ON CONFLICT(nombre) DO UPDATE SET valor=excluded.valor
    """, (nombre, valor))
    conn.commit()

    ok, msg = backup_db_to_gist()
    try:
        st.toast("✅ Respaldo OK en GitHub" if ok else f"⚠️ {msg}")
    except Exception:
        pass

def upsert_mixer_by_unidad(conn, unidad_id, placa, capacidad_m3, tipo, habilitado=1):
    cur = conn.cursor()
    # normalizar tipo (SANY → SANNY)
    tipo_norm = "SANNY" if str(tipo).strip().upper() in ["SANY", "SANNY"] else "STD"
    # ¿existe ese unidad_id?
    cur.execute("SELECT id FROM mixers WHERE unidad_id = ?", (unidad_id,))
    row = cur.fetchone()
    if row:
        cur.execute(
            "UPDATE mixers SET placa=?, capacidad_m3=?, tipo=?, habilitado=? WHERE id=?",
            (placa, float(capacidad_m3), tipo_norm, int(habilitado), row[0])
        )
    else:
        cur.execute(
            "INSERT INTO mixers (placa, activo, habilitado, capacidad_m3, tipo, unidad_id) VALUES (?, ?, ?, ?, ?, ?)",
            (placa, 1, int(habilitado), float(capacidad_m3), tipo_norm, unidad_id)
        )
    conn.commit()

    ok, msg = backup_db_to_gist()
    try:
        st.toast("✅ Respaldo OK en GitHub" if ok else f"⚠️ {msg}")
    except Exception:
        pass

# ---------------------------------------------------
# Seed de datos si faltan
# ---------------------------------------------------
def seed_data():
    # Mixers
    c.execute("SELECT COUNT(*) FROM mixers")
    if c.fetchone()[0] == 0:
        mixers = []
        # 2 SANNY 10 m3
        for i in range(1, 3):
            mixers.append((f"SANNY-{str(i).zfill(2)}", 1, 1, 10.0, "SANNY"))
        # 12 STD 8.5 m3
        for i in range(3, 15):
            mixers.append((f"STD-{str(i).zfill(2)}", 1, 1, 8.5, "STD"))
        c.executemany(
            "INSERT INTO mixers (placa, activo, habilitado, capacidad_m3, tipo) VALUES (?, ?, ?, ?, ?)",
            mixers
        )

    # Dosificadoras
    c.execute("SELECT COUNT(*) FROM dosif")
    if c.fetchone()[0] == 0:
        c.executemany(
            "INSERT INTO dosif (codigo, habilitado) VALUES (?, ?)",
            [("DF-01", 1), ("DF-06", 1)]
        )

    # Parámetros base (según tu Excel)
    # Nota: SQLite permite guardar texto en una columna REAL sin romperse (tipado dinámico),
    # así que dejamos la columna como está y guardamos la fecha como 'YYYY-MM-DD'.
    base_params = {
        "Fecha_inicio": "2025-11-03",          # tu 11/3/2025 interpretado como 3-Nov-2025
        "Dias_planificados": 7,
        "Intervalo_min": 15,
        "Capacidad_mixer_m3": 8.5,
        "Tiempo_carga_min": 11,                # base para 8.5 m³
        "Tiempo_descarga_min": 20,
        "Margen_lavado_min": 5,
        "Bombas_disponibles": 3,
        "Dosificadoras_en_planta": 2,
        "Tiempo_cambio_obra_min": 4,
        "Mixers_SANNY": 2,
        "Capacidad_SANNY_m3": 10,
    }
    for k, v in base_params.items():
        c.execute("INSERT OR IGNORE INTO parametros (nombre, valor) VALUES (?, ?)", (k, v))

    conn.commit()

    ok, msg = backup_db_to_gist()
    try:
        st.toast("✅ Respaldo OK en GitHub" if ok else f"⚠️ {msg}")
    except Exception:
        pass

seed_data()
ensure_required_params(conn)

# ---------------------------------------------------
# Función de cálculo de tiempos
# ---------------------------------------------------
def calcular_tiempos(hora_Q, min_viaje_ida, volumen_m3,
                     tiempo_descarga_min, margen_lavado_min, tiempo_cambio_obra_min):
    from datetime import datetime, timedelta
    import math

    # Q → R
    hora_Q_dt = datetime.strptime(hora_Q.strip(), "%H:%M")
    R = hora_Q_dt - timedelta(minutes=int(min_viaje_ida))

    # Carga variable por volumen: 11 min cuando 8.5 m³; escalar y redondear hacia arriba
    tiempo_carga_base = 11
    tiempo_carga_min = math.ceil(tiempo_carga_base * (float(volumen_m3) / 8.5))

    # S/T (carga)
    S = R - timedelta(minutes=tiempo_carga_min)
    T = R

    # U (fin descarga), V (cambio), W (regreso), X (lavado)
    U = hora_Q_dt + timedelta(minutes=int(tiempo_descarga_min))
    V = U + timedelta(minutes=int(tiempo_cambio_obra_min))
    W = V + timedelta(minutes=int(min_viaje_ida))
    X = W + timedelta(minutes=int(margen_lavado_min))

    return R, S, T, U, V, W, X

# ---------------------------------------------------
# UI
# ---------------------------------------------------
tabs = st.tabs(["⚙️ Parámetros", "🚛 Mixers", "🏗️ Nuevo Proyecto", "📅 Calendario Día", "🗓️ Calendario Mes"])

# 1) Parámetros
with tabs[0]:
    st.subheader("Parámetros (editar en línea)")

    # 1) Carga
    dfp = pd.read_sql("SELECT id, nombre, valor FROM parametros ORDER BY nombre", conn)

    # 2) Vista editable: SIN configurar la columna Valor (para que sea editable)
    view = dfp.copy()
    view.insert(0, "N°", range(1, len(view) + 1))
    view.rename(columns={"nombre": "Nombre", "valor": "Valor"}, inplace=True)
    view = view[["N°", "Nombre", "Valor"]]

    st.caption("Haz clic en la celda Valor para editar. Presiona Enter para confirmar el cambio en la celda.")
    edited = st.data_editor(
        view,
        hide_index=True,
        use_container_width=True,
        disabled=False,                # fuerza modo editable
        num_rows="fixed",              # no se agregan filas, solo editar
        column_config={
            "N°": st.column_config.NumberColumn(disabled=True),
            "Nombre": st.column_config.TextColumn(disabled=True),
            # OJO: NO configuramos 'Valor' para dejarlo editable por defecto
        },
        key="param_table_editor_v2",   # clave nueva para evitar cacheos
    )

    # 3) Guardar cambios
    NUMERIC_KEYS = {
        "Intervalo_min",
        "Capacidad_mixer_m3",
        "Tiempo_carga_min",
        "Tiempo_descarga_min",
        "Margen_lavado_min",
        "Tiempo_cambio_obra_min",
    }

    def _normalize_number(x):
        if x is None:
            return None
        s = str(x).strip().replace(",", ".")
        try:
            return str(float(s))
        except Exception:
            return s

    c1, c2 = st.columns([1,1])
    with c1:
        if st.button("💾 Guardar cambios de la tabla"):
            cur = conn.cursor()
            ok, err = 0, 0
            for _, row in edited.iterrows():
                name = str(row["Nombre"]).strip()
                val  = row["Valor"]

                if name in NUMERIC_KEYS:
                    val = _normalize_number(val)
                    try:
                        float(str(val))  # valida
                    except Exception:
                        err += 1
                        continue

                try:
                    cur.execute("UPDATE parametros SET valor=? WHERE lower(nombre)=lower(?)", (str(val), name))
                    ok += 1
                except Exception:
                    err += 1
            conn.commit()

            ok, msg = backup_db_to_gist()
            try:
                st.toast("✅ Respaldo OK en GitHub" if ok else f"⚠️ {msg}")
            except Exception:
                pass
            
            # (si usas respaldo a Gist, descomenta la siguiente línea)
            # backup_db_to_gist()
            st.success(f"✅ Guardado: {ok} parámetro(s). {'❗Errores: '+str(err) if err else ''}")

    with c2:
        if st.button("🔄 Recargar"):
            st.rerun()

with st.expander("🛠️ Respaldo GitHub (debug)"):
    exists = os.path.exists(DB_FILE)
    size = os.path.getsize(DB_FILE) if exists else 0
    st.write(f"DB local: `{DB_FILE}` existe = {exists}, tamaño = {size} bytes")
    st.write("Secrets cargados:", bool(GITHUB_TOKEN), bool(GIST_ID), DB_FILE)

    colA, colB, colC = st.columns(3)
    with colA:
        if st.button("🔎 Probar conexión a Gist"):
            try:
                r = requests.get(f"https://api.github.com/gists/{GIST_ID}",
                                 headers=_gh_headers(), timeout=15)
                st.write("GET /gists/{id} status:", r.status_code)
                if r.ok:
                    files = r.json().get("files", {})
                    st.write("Archivos actuales en el Gist:", list(files.keys()))
                else:
                    st.error(r.text)
            except Exception as e:
                st.error(f"Error probando conexión: {e}")

    with colB:
        if st.button("⬆️ Forzar respaldo ahora"):
            ok, msg = backup_db_to_gist()
            st.write("backup_db_to_gist():", ok, msg)

    with colC:
        if st.button("⬇️ Intentar restaurar ahora"):
            ok, msg = restore_db_from_gist()
            st.write("restore_db_from_gist():", ok, msg)

    with st.expander("🧹 Reparación avanzada (si ves errores de esquema)"):
    cA, cB = st.columns(2)
    with cA:
        if st.button("❌ Eliminar DB del Gist (solo si está corrupta)"):
            ok, msg = delete_db_in_gist()
            st.write("delete_db_in_gist():", ok, msg)
    with cB:
        if st.button("⬆️ Subir DB local (limpia) al Gist"):
            ok, msg = backup_db_to_gist()
            st.write("backup_db_to_gist():", ok, msg)

# 2) Mixers
with tabs[1]:
    st.subheader("Listado de Mixers")

    # --- Asegurar columna unidad_id e índice único por Unidad (si no existían; no muestra nada al usuario)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(mixers)")
    cols = [r[1].lower() for r in cur.fetchall()]
    if "unidad_id" not in cols:
        cur.execute("ALTER TABLE mixers ADD COLUMN unidad_id TEXT")
        conn.commit()

        ok, msg = backup_db_to_gist()
        try:
            st.toast("✅ Respaldo OK en GitHub" if ok else f"⚠️ {msg}")
        except Exception:
            pass

    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mixers_unidad ON mixers(unidad_id)")
    conn.commit()

    ok, msg = backup_db_to_gist()
    try:
        st.toast("✅ Respaldo OK en GitHub" if ok else f"⚠️ {msg}")
    except Exception:
        pass

    # --- Leer datos base (sin 'activo'; no lo usamos más)
    dfm = pd.read_sql("""
        SELECT id, unidad_id, placa, habilitado, capacidad_m3, tipo
        FROM mixers
        ORDER BY id
    """, conn)

    # Métricas con habilitado=1
    total_habilitados = int((dfm["habilitado"] == 1).sum()) if not dfm.empty else 0
    volumen_habilitado = float(dfm.loc[dfm["habilitado"] == 1, "capacidad_m3"].sum()) if not dfm.empty else 0.0

    c1, c2, _ = st.columns([1,1,2])
    c1.metric("Mixers habilitados", total_habilitados)
    c2.metric("Volumen habilitado (m³)", f"{volumen_habilitado:.1f}")

    # --- Vista amigable: ocultamos ID real y agregamos N° (1..n)
    view = dfm.copy()
    view.insert(0, "N°", range(1, len(view) + 1))  # numeración visual
    view["Habilitado (SI/NO)"] = view["habilitado"].apply(lambda x: "YES" if int(x) == 1 else "NO")
    view.rename(columns={
        "unidad_id": "Unidad",
        "placa": "Placa",
        "capacidad_m3": "Capacidad_m3",
        "tipo": "Tipo",
    }, inplace=True)

    # Columnas a mostrar (sin ID)
    view_show = view[["N°", "Unidad", "Placa", "Habilitado (SI/NO)", "Capacidad_m3", "Tipo"]]

    # Mostrar tabla sin índice (0..n-1) y sin columna ID
    try:
        st.dataframe(view_show, use_container_width=True, hide_index=True)
    except TypeError:
        st.dataframe(view_show.style.hide(axis="index"), use_container_width=True)

    # --- Acciones: alternar habilitado (sin mostrar ID)
    st.markdown("### 🔁 Alternar habilitado")

    if dfm.empty:
        st.info("No hay mixers cargados.")
    else:
        # Mapeo etiqueta → id (sin mostrar ID en la etiqueta)
        opciones = {
            f"{(row['unidad_id'] or 's/n')} — {row['placa']} ({row['capacidad_m3']} m³, {row['tipo']}) — "
            f"{'HABILITADO' if int(row['habilitado'])==1 else 'DESHABILITADO'}": int(row["id"])
            for _, row in dfm.iterrows()
        }
        etiqueta_sel = st.selectbox("Selecciona un mixer", list(opciones.keys()))
        mixer_id = opciones[etiqueta_sel]

        cur.execute("SELECT habilitado FROM mixers WHERE id=?", (mixer_id,))
        row = cur.fetchone()
        if row is None:
            st.error("No se pudo leer el estado del mixer seleccionado.")
        else:
            estado = int(row[0])
            etiqueta_btn = "DESHABILITAR" if estado == 1 else "HABILITAR"
            if st.button(etiqueta_btn):
                nuevo = 0 if estado == 1 else 1
                cur.execute("UPDATE mixers SET habilitado=? WHERE id=?", (nuevo, mixer_id))
                conn.commit()

                ok, msg = backup_db_to_gist()
                try:
                    st.toast("✅ Respaldo OK en GitHub" if ok else f"⚠️ {msg}")
                except Exception:
                    pass
                
                st.success(f"Mixer {'habilitado' if nuevo==1 else 'deshabilitado'}.")
                st.rerun()

    # --- Eliminar mixer (sin mostrar ID)
    st.markdown("### 🗑️ Eliminar mixer")

    if not dfm.empty:
        opciones_del = {
            f"{(row['unidad_id'] or 's/n')} — {row['placa']} ({row['capacidad_m3']} m³, {row['tipo']})": int(row["id"])
            for _, row in dfm.iterrows()
        }
        etiqueta_sel_del = st.selectbox("Mixer a eliminar", list(opciones_del.keys()), key="del_sel")
        mixer_id_del = opciones_del[etiqueta_sel_del]

        # Verificar viajes asociados
        cur.execute("SELECT COUNT(*) FROM agenda WHERE mixer_id = ?", (mixer_id_del,))
        cnt = cur.fetchone()[0]

        if cnt > 0:
            st.warning(f"No se puede eliminar: este mixer tiene {cnt} viaje(s) en agenda.")
        else:
            col_chk, col_btn = st.columns([2,1])
            with col_chk:
                conf = st.checkbox("Confirmo que deseo eliminar este mixer de forma permanente.")
            with col_btn:
                if st.button("Eliminar definitivamente", type="primary", disabled=not conf):
                    cur.execute("DELETE FROM mixers WHERE id=?", (mixer_id_del,))
                    conn.commit()

                    ok, msg = backup_db_to_gist()
                    try:
                        st.toast("✅ Respaldo OK en GitHub" if ok else f"⚠️ {msg}")
                    except Exception:
                        pass
                    
                    st.success("Mixer eliminado.")
                    st.rerun()
# 3) Nuevo Proyecto (viaje simple)
with tabs[2]:
    st.subheader("Nuevo Proyecto (viaje simple)")

    col1, col2, col3 = st.columns(3)
    with col1:
        cliente = st.text_input("Cliente")
        proyecto = st.text_input("Proyecto")
        fecha = st.date_input("Fecha", datetime.now(), key="np_fecha")
    with col2:
        hora_Q = st.text_input("Hora en obra (HH:MM)", "08:00")
        min_viaje_ida = st.number_input("Minutos viaje ida", 0, 240, 30)
        volumen_m3 = st.number_input("Volumen (m³)", 1.0, 12.0, 8.5, step=0.5)
    with col3:
        requiere_bomba = st.selectbox("¿Requiere bomba?", ["NO", "YES"])
        dosificadora = st.selectbox("Dosificadora", ["DF-01", "DF-06"])

st.markdown("## 🏗️ Nuevo Proyecto — Automático (solo STD)")
with st.form("form_proj_auto_std", clear_on_submit=False):
    cliente_a   = st.text_input("Cliente", value="")
    proyecto_a  = st.text_input("Proyecto", value="")
    fecha_a     = st.date_input("Fecha", value=datetime.now()).strftime("%Y-%m-%d")
    hora_Q_a    = st.text_input("Hora en obra (HH:MM)", value="08:00")
    vol_total_a = st.number_input("Volumen total (m³)", min_value=1.0, step=0.5, value=83.0)
    min_ida_a   = st.number_input("Minutos viaje ida", min_value=0, max_value=300, value=25)
    tdesc_a     = st.number_input("Tiempo de descarga (min)", min_value=5, max_value=120, value=20)
    req_bomba_a = st.selectbox("¿Requiere bomba?", ["NO", "YES"], index=0)
    submitted   = st.form_submit_button("🚀 Planificar y guardar (STD)")

if submitted:
    try:
        df_res = planificar_proyecto_auto(
            conn,
            cliente=cliente_a.strip(),
            proyecto=proyecto_a.strip(),
            fecha_sel=fecha_a,
            hora_Q_ini=hora_Q_a.strip(),
            volumen_total=float(vol_total_a),
            min_ida=int(min_ida_a),
            tiempo_descarga_min=int(tdesc_a),
            requiere_bomba=req_bomba_a
        )
        st.success(f"✅ Proyecto planificado: {len(df_res)} viaje(s) creados (solo STD).")
        try:
            st.dataframe(df_res, use_container_width=True, hide_index=True)
        except TypeError:
            st.dataframe(df_res.style.hide(axis="index"), use_container_width=True)
    except Exception as e:
        st.error(f"❌ No se pudo planificar: {e}")
        
        # --- Cargar Mixers habilitados desde BD ---
        df_mix = pd.read_sql("SELECT id, unidad_id, placa, tipo, capacidad_m3, habilitado FROM mixers ORDER BY id", conn)
        if df_mix.empty:
            st.warning("No hay mixers registrados.")
            mixer_id = None
        else:
            opciones_mixer = {
                f"{(r['unidad_id'] or 's/n')} — {r['placa']} ({r['capacidad_m3']} m³, {r['tipo']})": int(r["id"])
                for _, r in df_mix.iterrows()
                if int(r["habilitado"]) == 1
            }
            if not opciones_mixer:
                st.warning("No hay mixers habilitados.")
                mixer_id = None
            else:
                etiqueta_sel = st.selectbox("Mixer", list(opciones_mixer.keys()))
                mixer_id = opciones_mixer[etiqueta_sel]

    if st.button("Guardar viaje"):
        # --- Validaciones rápidas ---
        # Mixer existe
        c.execute("SELECT 1 FROM mixers WHERE id=?", (int(mixer_id),))
        row = c.fetchone()
        if not row:
            st.error("Mixer no existe. Revisa el ID interno.")
            st.stop()

        # Parámetros del sistema
        for key in ["Tiempo_descarga_min", "Margen_lavado_min", "Tiempo_cambio_obra_min"]:
            c.execute("SELECT valor FROM parametros WHERE nombre=?", (key,))
            v = c.fetchone()
            if v is None:
                st.error(f"Falta el parámetro '{key}'. Agrega ese parámetro en la pestaña Parámetros.")
                st.stop()

        tiempo_descarga_min   = float(get_param(conn, "Tiempo_descarga_min",   20))
        margen_lavado_min     = float(get_param(conn, "Margen_lavado_min",     10))
        tiempo_cambio_obra_min= float(get_param(conn, "Tiempo_cambio_obra_min",5))

        # --- Cálculo de tiempos (todo dentro del botón) ---
        try:
            R, S, T, U, V, W, X = calcular_tiempos(
                hora_Q,
                int(min_viaje_ida),
                float(volumen_m3),
                int(tiempo_descarga_min),
                int(margen_lavado_min),
                int(tiempo_cambio_obra_min),
            )
        except ValueError:
            st.error("Formato de hora inválido. Usa HH:MM (ej. 06:20).")
            st.stop()

        # --- Campos extra para agenda ---
        fecha_str = fecha.strftime("%Y-%m-%d")
        fecha_hora_q = f"{fecha_str} {hora_Q}"
        ciclo_total_min = int((X - S).total_seconds() // 60)   # duración del ciclo [S..X]
        min_viaje_regreso = int(min_viaje_ida)                 # por ahora igual que ida
        dosif_codigo = dosificadora                            # "DF-01" / "DF-06"

        # --- Guardar en agenda (con todas las horas clave) ---
        c.execute("""
            INSERT INTO agenda (
                cliente, proyecto, fecha, hora_Q, min_viaje_ida, volumen_m3, requiere_bomba,
                dosificadora, mixer_id, hora_R, hora_S, hora_T, hora_U, hora_V, hora_W, hora_X,
                estado, fecha_hora_q, ciclo_total_min, min_viaje_regreso, dosif_codigo
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            cliente, proyecto, fecha_str, hora_Q, int(min_viaje_ida), float(volumen_m3), requiere_bomba,
            dosificadora, int(mixer_id),
            R.strftime("%H:%M"), S.strftime("%H:%M"), T.strftime("%H:%M"),
            U.strftime("%H:%M"), V.strftime("%H:%M"), W.strftime("%H:%M"), X.strftime("%H:%M"),
            "Programado", fecha_hora_q, ciclo_total_min, min_viaje_regreso, dosif_codigo
        ))
        conn.commit()

        ok, msg = backup_db_to_gist()
        try:
            st.toast("✅ Respaldo OK en GitHub" if ok else f"⚠️ {msg}")
        except Exception:
            pass
        
        st.success("✅ Viaje guardado correctamente")

# 4) Calendario del día
with tabs[3]:
    st.subheader("Calendario del día (viajes y recursos)")

    # Selecciona fecha a visualizar
    from zoneinfo import ZoneInfo
    # Navegación por días: Ayer / Hoy / Mañana (en Tegucigalpa)
    local_today = datetime.now(ZoneInfo("America/Tegucigalpa")).date()
    d = st.session_state.get("cal_d", local_today)
    
    colp, colh, coln = st.columns([1, 2, 1])
    if colp.button("◀ Ayer"):
        d = d - timedelta(days=1)
    if colh.button("Hoy"):
        d = local_today
    if coln.button("Mañana ▶"):
        d = d + timedelta(days=1)
    
    st.session_state["cal_d"] = d
    st.write(f"📅 Fecha seleccionada: **{d.strftime('%Y-%m-%d')}**")
    fecha_sel = d.strftime("%Y-%m-%d")

        # --- Resumen por proyecto (Proyecto | Hora Q | Mixers)
    df_day = pd.read_sql("""
        SELECT proyecto, cliente, fecha, hora_Q, mixer_id
        FROM agenda
        WHERE fecha = ?
        ORDER BY hora_Q
    """, conn, params=(fecha_sel,))
    
    # Mapeo de mixers SIEMPRE definido (antes de usarlo)
    df_mix = pd.read_sql("SELECT id, unidad_id, placa FROM mixers", conn)
    id_to_label = {int(r["id"]): f"{r['unidad_id'] or 's/n'} ({r['placa']})" for _, r in df_mix.iterrows()}
    
    def mixer_label(mid):
        if pd.isna(mid):
            return ""
        try:
            mid_i = int(mid)
        except Exception:
            return str(mid)
        return id_to_label.get(mid_i, f"ID {mid_i}")
    
    if df_day.empty:
        st.info(f"No hay viajes para la fecha seleccionada ({fecha_sel}).")
    else:
        df_day["Mixer"] = df_day["mixer_id"].apply(mixer_label)
        resumen = (
            df_day
            .groupby(["proyecto", "hora_Q"], as_index=False)
            .agg({"Mixer": lambda s: ", ".join(sorted(set([x for x in s if pd.notna(x) and x])) )})
        )
        resumen.rename(columns={"proyecto": "Proyecto", "hora_Q": "Hora en obra (Q)"}, inplace=True)
    
        st.markdown("### 🧾 Resumen del día por proyecto")
        try:
            st.dataframe(resumen, use_container_width=True, hide_index=True)
        except TypeError:
            st.dataframe(resumen.style.hide(axis="index"), use_container_width=True)
        st.markdown("---")
    
        # --- Agenda por mixer (slots 15')
st.markdown("### 🚛 Agenda por Mixer (15 min)")

# 1) Cargar mixers
df_mix_all = pd.read_sql(
    "SELECT id, unidad_id, placa, habilitado FROM mixers ORDER BY id",
    conn
)

# 2) Guardas si no hay mixers
if df_mix_all.empty:
    st.info("No hay mixers en el sistema.")
else:
    # 3) Selector de mixer (labels bonitos)
    opciones_mx = {
        f"{(r['unidad_id'] or 's/n')} — {r['placa']} {'[HAB]' if int(r['habilitado'])==1 else '[DESH]'}": int(r["id"])
        for _, r in df_mix_all.iterrows()
    }
    sel_mx_label = st.selectbox(
        "Selecciona mixer",
        list(opciones_mx.keys()),
        key=f"sel_mx_{fecha_sel}"  # clave única por día
    )
    sel_mx_id = opciones_mx[sel_mx_label]

    # 4) Construir slots y marcas SOLO aquí dentro
    slots = build_slots_15(fecha_sel)  # 96 slots del día
    busy  = mixer_busy_ranges_for_day(conn, sel_mx_id, fecha_sel)  # rangos [S..X]
    marks = mark_busy(slots, busy)

    # 5) Render: tabla Hora | 00 | 15 | 30 | 45
    rows = []
    for i, s in enumerate(slots):
        if s.minute == 0:
            hour = s.strftime("%H:%M")
            blocks = marks[i:i+4]  # 00,15,30,45
            if len(blocks) < 4:
                blocks += [""] * (4 - len(blocks))
            rows.append([hour] + blocks)

    df_grid = pd.DataFrame(rows, columns=["Hora", ":00", ":15", ":30", ":45"])
    st.dataframe(df_grid, use_container_width=True, hide_index=True)
    st.caption("■ = ocupado | · = libre (según [S..X])")

st.markdown("---")

# --- Agenda por dosificadora (slots 15')
st.markdown("### 🏭 Agenda por Dosificadora (15 min)")

# 1) Cargar dosificadoras habilitadas
df_dos = pd.read_sql("SELECT codigo FROM dosif WHERE habilitado=1", conn)

# 2) Guardas si no hay dosificadoras
if df_dos.empty:
    st.info("No hay dosificadoras habilitadas.")
else:
    dos_opts = df_dos["codigo"].tolist()
    sel_dos = st.selectbox(
        "Selecciona dosificadora",
        dos_opts,
        index=0,
        key=f"sel_dos_{fecha_sel}"  # clave única por día
    )

    # 3) Slots y marcas [S..T]
    slots_d = build_slots_15(fecha_sel)
    busy_d  = dosif_busy_ranges_for_day(conn, sel_dos, fecha_sel)
    marks_d = mark_busy(slots_d, busy_d)

    # 4) Render tabla
    rows_d = []
    for i, s in enumerate(slots_d):
        if s.minute == 0:
            hour = s.strftime("%H:%M")
            blocks = marks_d[i:i+4]
            if len(blocks) < 4:
                blocks += [""] * (4 - len(blocks))
            rows_d.append([hour] + blocks)

    df_grid_d = pd.DataFrame(rows_d, columns=["Hora", ":00", ":15", ":30", ":45"])
    st.dataframe(df_grid_d, use_container_width=True, hide_index=True)
    st.caption("■ = ocupado | · = libre (según [S..T])")

st.markdown("---")
with tabs[3]:
    st.markdown("## 📝 Editar / Eliminar viaje del día")

    with st.expander("Abrir editor"):
        # Cargamos viajes del día con más info para editar
        df_edit = pd.read_sql("""
            SELECT a.id, a.cliente, a.proyecto, a.fecha, a.hora_Q, a.min_viaje_ida, a.volumen_m3,
                   a.requiere_bomba, a.dosif_codigo, a.mixer_id,
                   a.hora_S, a.hora_T, a.hora_X
            FROM agenda a
            WHERE a.fecha = ?
            ORDER BY a.hora_Q, a.proyecto, a.mixer_id
        """, conn, params=(fecha_sel,))

        if df_edit.empty:
            st.info("No hay viajes para editar/eliminar en esta fecha.")
        else:
            # Para etiquetas legibles de mixer
            df_mix_lbl = pd.read_sql("SELECT id, unidad_id, placa FROM mixers", conn)
            id2mixer = {int(r["id"]): f"{r['unidad_id'] or 's/n'} ({r['placa']})" for _, r in df_mix_lbl.iterrows()}

            df_edit["Mixer_label"] = df_edit["mixer_id"].map(id2mixer)
            opciones = {
                f"[{r['hora_Q']}] {r['proyecto']} — {r['Mixer_label']} (S:{r['hora_S']} → X:{r['hora_X']})": int(r["id"])
                for _, r in df_edit.iterrows()
            }

            colsel, colact = st.columns([2,1])
            with colsel:
                etq = st.selectbox("Selecciona un viaje", list(opciones.keys()), key=f"edit_viaje_sel_{fecha_sel}")
                agenda_id = opciones[etq]

            # Cargar fila elegida
            row = df_edit[df_edit["id"] == agenda_id].iloc[0]

            # Form para edición rápida (todo DENTRO del else)
            c1, c2, c3 = st.columns(3)
            with c1:
                hora_Q_new = st.text_input(
                    "Hora en obra (HH:MM)",
                    value=row["hora_Q"],
                    key=f"edit_hora_{agenda_id}"
                )
                min_ida_new = st.number_input(
                    "Min viaje ida",
                    min_value=0, max_value=240, value=int(row["min_viaje_ida"]),
                    key=f"edit_minida_{agenda_id}"
                )
                vol_new = st.number_input(
                    "Volumen (m³)",
                    min_value=1.0, max_value=12.0, value=float(row["volumen_m3"]), step=0.5,
                    key=f"edit_vol_{agenda_id}"
                )

            with c2:
                req_bomba_new = st.selectbox(
                    "¿Requiere bomba?",
                    ["NO", "YES"],
                    index=0 if (row["requiere_bomba"] or "NO") == "NO" else 1,
                    key=f"edit_bomba_{agenda_id}"
                )
                dosif_new = st.selectbox(
                    "Dosificadora",
                    ["DF-01", "DF-06"],
                    index=0 if (row["dosif_codigo"] or "DF-01") == "DF-01" else 1,
                    key=f"edit_dosif_{agenda_id}"
                )
                # Mixers habilitados
                df_mix_hab = pd.read_sql(
                    "SELECT id, unidad_id, placa, habilitado, capacidad_m3, tipo FROM mixers ORDER BY id", conn
                )
                mix_opts = {
                    f"{(r['unidad_id'] or 's/n')} — {r['placa']} ({r['capacidad_m3']} m³, {r['tipo']})": int(r["id"])
                    for _, r in df_mix_hab.iterrows() if int(r["habilitado"]) == 1
                }
                mix_labels = list(mix_opts.keys())
                mix_values = list(mix_opts.values())
                try:
                    idx_default = mix_values.index(int(row["mixer_id"]))
                except ValueError:
                    idx_default = 0 if mix_values else 0
                mixer_lbl = st.selectbox(
                    "Mixer",
                    mix_labels if mix_labels else ["(sin mixers habilitados)"],
                    index=idx_default if mix_labels else 0,
                    key=f"edit_mixer_{agenda_id}"
                )
                mixer_new = mix_opts[mixer_lbl] if mix_labels else int(row["mixer_id"])

            with c3:
                fecha_new = st.date_input(
                    "Fecha del viaje",
                    datetime.strptime(row["fecha"], "%Y-%m-%d"),
                    key=f"edit_fecha_{agenda_id}"
                )

            b1, b2 = st.columns([1,1])
            with b1:
                if st.button("💾 Guardar cambios", key=f"btn_save_{agenda_id}"):
                    try:
                        recalc_and_update_agenda(
                            conn, agenda_id,
                            fecha_new.strftime("%Y-%m-%d"),
                            hora_Q_new.strip(),
                            int(min_ida_new),
                            float(vol_new),
                            req_bomba_new,
                            dosif_new,
                            int(mixer_new)
                        )
                        st.success("Viaje actualizado.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"No se pudo actualizar: {e}")

            with b2:
                conf = st.checkbox("Confirmo eliminar este viaje permanentemente", key=f"chk_del_{agenda_id}")
                if st.button("🗑️ Eliminar viaje", disabled=not conf, key=f"btn_del_{agenda_id}"):
                    cur = conn.cursor()
                    cur.execute("DELETE FROM agenda WHERE id=?", (int(agenda_id),))
                    conn.commit()

                    ok, msg = backup_db_to_gist()
                    try:
                        st.toast("✅ Respaldo OK en GitHub" if ok else f"⚠️ {msg}")
                    except Exception:
                        pass
                    
                    st.success("Viaje eliminado.")
                    st.rerun()

# 5) Calendario del mes
with tabs[4]:
    st.subheader("Calendario del mes — agrupado por proyectos")

    ref = st.date_input("Mes de referencia", datetime.now(), key="cal_mes_ref")
    y, m = ref.year, ref.month
    first = datetime(y, m, 1)
    last = (datetime(y + 1, 1, 1) - timedelta(days=1)) if m == 12 else (datetime(y, m + 1, 1) - timedelta(days=1))

    date_from = first.strftime("%Y-%m-%d")
    date_to   = last.strftime("%Y-%m-%d")

    dfm = pd.read_sql("""
        SELECT a.proyecto, a.fecha, a.volumen_m3, a.mixer_id,
               a.hora_S, a.hora_X
        FROM agenda a
        WHERE a.fecha BETWEEN ? AND ?
        ORDER BY a.fecha, a.hora_S
    """, conn, params=(date_from, date_to))

    if dfm.empty:
        st.info("No hay viajes registrados para este mes.")
    else:
        dmx = pd.read_sql("SELECT id, unidad_id, placa FROM mixers", conn)
        id2lbl = {int(r["id"]): f"{r['unidad_id'] or 's/n'} ({r['placa']})" for _, r in dmx.iterrows()}
        dfm["Mixer"] = dfm["mixer_id"].map(id2lbl)
        dfm["Mixer_SX"] = dfm.apply(lambda r: f"{r['Mixer']} [S:{r['hora_S']}→X:{r['hora_X']}]", axis=1)

        agg = (dfm.groupby(["fecha", "proyecto"], as_index=False)
                  .agg(m3_total=("volumen_m3", "sum"),
                       mixers=("Mixer_SX", lambda s: ", ".join(s))))
        agg.rename(columns={"fecha":"Fecha","proyecto":"Proyecto","m3_total":"Total m³","mixers":"Mixers (S→X)"}, inplace=True)

        st.dataframe(agg, use_container_width=True, hide_index=True)
        st.caption("Cada fila = un proyecto en un día. Muestra total de m³ y mixers con sus ventanas S→X.")
