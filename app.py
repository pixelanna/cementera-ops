import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import math

st.set_page_config(page_title="Cementera OPS", layout="wide")
st.title("🚧 Cementera OPS - v0.1")

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
# Crear tablas si no existen
# ---------------------------------------------------
c.execute("""
CREATE TABLE IF NOT EXISTS parametros (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT UNIQUE,
    valor REAL
)""")

c.execute("""
CREATE TABLE IF NOT EXISTS mixers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    placa TEXT,
    activo INTEGER,
    habilitado INTEGER,
    capacidad_m3 REAL,
    tipo TEXT
)""")

c.execute("""
CREATE TABLE IF NOT EXISTS dosif (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo TEXT,
    habilitado INTEGER
)""")

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
    mixer_id INTEGER,
    hora_R TEXT,
    hora_S TEXT,
    hora_T TEXT,
    hora_U TEXT,
    hora_V TEXT,
    hora_W TEXT,
    hora_X TEXT
)""")
conn.commit()

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
    st.subheader("Parámetros del sistema")

    # --- Mostrar tabla con índice desde 1 ---
    dfp = pd.read_sql("SELECT nombre, valor FROM parametros ORDER BY nombre", conn)
    dfp_display = dfp.copy()
    dfp_display.index = range(1, len(dfp_display) + 1)

    edited = st.data_editor(
        dfp_display,
        key="param_editor",
        use_container_width=True,
        num_rows="fixed"  # evita agregar filas accidentalmente aquí
    )

    if st.button("💾 Guardar cambios de la tabla"):
        # Escribimos todo lo editado
        for _, row in edited.iterrows():
            nombre = str(row["nombre"]).strip()
            valor_raw = str(row["valor"]).strip()
            # intenta castear a float si se puede
            try:
                valor = float(valor_raw)
            except:
                valor = valor_raw  # deja texto (fechas, etc.)
            upsert_param(conn, nombre, valor)
        st.success("Cambios guardados.")

    st.markdown("---")

    # --- Agregar parámetro (+) ---
    st.markdown("### ➕ Agregar parámetro")
    colA, colB, colC = st.columns([2, 2, 1])
    with colA:
        nuevo_nombre = st.text_input("Nombre (único)", placeholder="p.ej. Tiempo_cambio_obra_min")
    with colB:
        nuevo_valor = st.text_input("Valor", placeholder="p.ej. 4 ó 2025-11-03")
    with colC:
        if st.button("Agregar"):
            if not nuevo_nombre:
                st.error("Escribe un nombre.")
            elif dfp["nombre"].str.lower().eq(nuevo_nombre.lower()).any():
                st.warning("Ese nombre ya existe. Usa la tabla para editarlo o borra primero.")
            else:
                try:
                    v = float(nuevo_valor)
                except:
                    v = nuevo_valor
                upsert_param(conn, nuevo_nombre.strip(), v)
                st.success(f"Parámetro '{nuevo_nombre}' agregado. Recarga para verlo en la tabla.")

    st.markdown("---")

    # --- Eliminar parámetro (🗑️) ---
    st.markdown("### 🗑️ Eliminar parámetro")
    if len(dfp) == 0:
        st.info("No hay parámetros para eliminar.")
    else:
        colD, colE = st.columns([3, 1])
        with colD:
            to_delete = st.selectbox("Selecciona el parámetro a eliminar", dfp["nombre"].tolist())
        with colE:
            if st.button("Eliminar", type="secondary"):
                cur = conn.cursor()
                cur.execute("DELETE FROM parametros WHERE nombre = ?", (to_delete,))
                conn.commit()
                st.success(f"Parámetro '{to_delete}' eliminado. Recarga para actualizar la tabla.")

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
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mixers_unidad ON mixers(unidad_id)")
    conn.commit()

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

    with st.expander("🛠 Ver fechas guardadas (últimos 50)"):
        df_chk = pd.read_sql("SELECT id, proyecto, fecha, hora_Q FROM agenda ORDER BY id DESC LIMIT 50", conn)
        st.dataframe(df_chk, use_container_width=True, hide_index=True)
        
        # --- Resumen por proyecto (Proyecto | Hora Q | Mixers)
        df_day = pd.read_sql("""
            SELECT proyecto, cliente, fecha, hora_Q, mixer_id
            FROM agenda
            WHERE fecha = ?
            ORDER BY hora_Q
        """, conn, params=(fecha_sel,))
    
        if df_day.empty:
            st.info("No hay viajes para la fecha seleccionada.")
        else:
            # Traer 'Unidad' y 'Placa' de mixers
            df_mix = pd.read_sql("SELECT id, unidad_id, placa FROM mixers", conn)
            id_to_label = {int(r["id"]): f"{r['unidad_id'] or 's/n'} ({r['placa']})" for _, r in df_mix.iterrows()}

        # Agrupar por proyecto y hora_Q, listando mixers
        df_day["Mixer"] = df_day["mixer_id"].map(id_to_label)
        resumen = (df_day
                   .groupby(["proyecto", "hora_Q"], as_index=False)
                   .agg({"Mixer": lambda s: ", ".join(sorted(set([x for x in s if pd.notna(x)])))})
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

    # Selector mixer (sin mostrar ID)
    df_mix_all = pd.read_sql("SELECT id, unidad_id, placa, habilitado FROM mixers ORDER BY id", conn)
    if df_mix_all.empty:
        st.info("No hay mixers en el sistema.")
    else:
        opciones_mx = {
            f"{(r['unidad_id'] or 's/n')} — {r['placa']} {'[HAB]' if r['habilitado']==1 else '[DESH]'}": int(r["id"])
            for _, r in df_mix_all.iterrows()
        }
        sel_mx_label = st.selectbox("Selecciona mixer", list(opciones_mx.keys()))
        sel_mx_id = opciones_mx[sel_mx_label]

        slots = build_slots_15(fecha_sel)  # 96 slots del día
        busy = mixer_busy_ranges_for_day(conn, sel_mx_id, fecha_sel)
        marks = mark_busy(slots, busy)

        # Render compacto: mostramos cada hora con sus 4 bloques de 15'
        # Construimos una tabla: Hora | 00 | 15 | 30 | 45
        rows = []
        for i, s in enumerate(slots):
            if s.minute == 0:
                # fila nueva
                hour = s.strftime("%H:00")
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

    df_dos = pd.read_sql("SELECT codigo FROM dosif WHERE habilitado=1", conn)
    if df_dos.empty:
        st.info("No hay dosificadoras habilitadas.")
    else:
        dos_opts = df_dos["codigo"].tolist()
        sel_dos = st.selectbox("Selecciona dosificadora", dos_opts, index=0)

        slots_d = build_slots_15(fecha_sel)
        busy_d = dosif_busy_ranges_for_day(conn, sel_dos, fecha_sel)
        marks_d = mark_busy(slots_d, busy_d)

        rows_d = []
        for i, s in enumerate(slots_d):
            if s.minute == 0:
                hour = s.strftime("%H:00")
                blocks = marks_d[i:i+4]
                if len(blocks) < 4:
                    blocks += [""] * (4 - len(blocks))
                rows_d.append([hour] + blocks)

        df_grid_d = pd.DataFrame(rows_d, columns=["Hora", ":00", ":15", ":30", ":45"])
        st.dataframe(df_grid_d, use_container_width=True, hide_index=True)
        st.caption("■ = ocupado | · = libre (según [S..T])")

    st.markdown("---")
st.markdown("## 📝 Editar / Eliminar viaje del día")

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
        etq = st.selectbox("Selecciona un viaje", list(opciones.keys()), key="edit_viaje_sel")
        agenda_id = opciones[etq]

    # Cargar fila elegida
    row = df_edit[df_edit["id"] == agenda_id].iloc[0]

    # Form para edición rápida
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
        if st.button("💾 Guardar cambios"):
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
        # Bloqueo borrar sin confirmar
        conf = st.checkbox("Confirmo eliminar este viaje permanentemente")
        if st.button("🗑️ Eliminar viaje", disabled=not conf):
            cur = conn.cursor()
            cur.execute("DELETE FROM agenda WHERE id=?", (int(agenda_id),))
            conn.commit()
            st.success("Viaje eliminado.")
            st.rerun()

# 5) Calendario del mes

    with tabs[4]:
        st.subheader("Calendario del mes — agrupado por proyectos")
    
        # Selecciona una fecha de referencia (usamos su mes)
        ref = st.date_input("Mes de referencia", datetime.now(), key="cal_mes_ref")
        y, m = ref.year, ref.month
        first = datetime(y, m, 1)
        if m == 12:
            last = datetime(y + 1, 1, 1) - timedelta(days=1)
        else:
            last = datetime(y, m + 1, 1) - timedelta(days=1)
    
        date_from = first.strftime("%Y-%m-%d")
        date_to = last.strftime("%Y-%m-%d")
    
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
                      .agg(
                          m3_total=("volumen_m3", "sum"),
                          mixers=("Mixer_SX", lambda s: ", ".join(s))
                      )
                   )
    
            agg.rename(columns={
                "fecha": "Fecha",
                "proyecto": "Proyecto",
                "m3_total": "Total m³",
                "mixers": "Mixers (S→X)"
            }, inplace=True)
    
            st.dataframe(agg, use_container_width=True, hide_index=True)
            st.caption("Cada fila = un proyecto en un día. Muestra total de m³ y mixers con sus ventanas S→X.")
