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

# ---------------------------------------------------
# Función de cálculo de tiempos
# ---------------------------------------------------
def calcular_tiempos(hora_Q_str, min_viaje_ida, volumen_m3,
                     tiempo_descarga_min, margen_lavado_min, tiempo_cambio_obra_min):
    # Toma Q (hora en obra) y calcula:
    # R (sale planta), S/T (carga), U (fin descarga), V (cambio en obra), W (regreso), X (fin total)

    hora_Q = datetime.strptime(hora_Q_str, "%H:%M")

    # Q → R (resta viaje ida)
    R = hora_Q - timedelta(minutes=int(min_viaje_ida))

    # Carga variable por volumen: 11 min cuando 8.5 m³; escalar y redondear hacia arriba
    tiempo_carga_base = 11  # base para 8.5 m³
    tiempo_carga_min = math.ceil(tiempo_carga_base * (float(volumen_m3) / 8.5))

    # S = inicio carga; T = fin carga (= R)
    S = R - timedelta(minutes=tiempo_carga_min)
    T = R

    # U = fin descarga desde Q
    U = hora_Q + timedelta(minutes=int(tiempo_descarga_min))
    # V = cambio en obra
    V = U + timedelta(minutes=int(tiempo_cambio_obra_min))
    # W = regreso (mismos min que ida)
    W = V + timedelta(minutes=int(min_viaje_ida))
    # X = fin total (lavado/margen)
    X = W + timedelta(minutes=int(margen_lavado_min))

    return R, S, T, U, V, W, X

# ---------------------------------------------------
# UI
# ---------------------------------------------------
tabs = st.tabs(["⚙️ Parámetros", "🚛 Mixers", "🏗️ Nuevo Proyecto", "📅 Calendario Día"])

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

    # --- Patch de esquema: agregar columna Unidad y un índice único para evitar duplicados por Unidad
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(mixers)")
    cols = [r[1].lower() for r in cur.fetchall()]
    if "unidad_id" not in cols:
        cur.execute("ALTER TABLE mixers ADD COLUMN unidad_id TEXT")
        conn.commit()
    # índice único sobre unidad_id (si hay nulls, no chocan en SQLite)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mixers_unidad ON mixers(unidad_id)")
    conn.commit()

    # --- Carga rápida desde Excel (pegar)
    with st.expander("📥 Carga rápida desde Excel (pegar aquí)"):
        st.caption("Formato por línea: **ID | Placa | Capacidad_m3 | Tipo** (ignora la columna Activo). Ejemplo: `218 25 | HAA1234 | 10 | SANY`")
        pegado = st.text_area("Pega tus filas (una por línea):", height=200, placeholder="218 25 | HAA1234 | 10 | SANY\nMX 25 | HAA3456 | 8.5 | STD\n...")
        coli, colj = st.columns([1,3])
        with coli:
            habilitar_todo = st.checkbox("Habilitar todos al cargar", value=True)
        if st.button("Cargar/Actualizar mixers"):
            if not pegado.strip():
                st.warning("No hay texto para procesar.")
            else:
                ok, err = 0, 0
                for line in pegado.splitlines():
                    if not line.strip():
                        continue
                    # admite separadores | ; , o tab
                    parts = [p.strip() for p in line.replace("\t", "|").replace(";", "|").replace(",", "|").split("|")]
                    if len(parts) < 4:
                        err += 1
                        continue
                    unidad_id, placa, cap_str, tipo = parts[0], parts[1], parts[2], parts[3]
                    try:
                        upsert_mixer_by_unidad(conn, unidad_id, placa, float(cap_str), tipo, 1 if habilitar_todo else 0)
                        ok += 1
                    except Exception:
                        err += 1
                st.success(f"Carga completada: {ok} OK, {err} con error.")
                st.rerun()

    # --- Leer datos y métricas (solo HABILITADO)
    dfm = pd.read_sql("SELECT id, unidad_id, placa, habilitado, capacidad_m3, tipo FROM mixers ORDER BY id", conn)

    if dfm.empty:
        total_disponibles = 0
        volumen_disponible = 0.0
    else:
        total_disponibles = int((dfm["habilitado"] == 1).sum())
        volumen_disponible = float(dfm.loc[dfm["habilitado"] == 1, "capacidad_m3"].sum())

    m1, m2, _ = st.columns([1, 1, 2])
    m1.metric("Mixers habilitados", total_disponibles)
    m2.metric("Volumen habilitado (m³)", f"{volumen_disponible:.1f}")

    # --- Vista amigable sin índice y sin columna 'activo'
    view = dfm.copy()
    view.rename(columns={
        "id": "MixerID",
        "unidad_id": "Unidad",
        "placa": "Placa",
        "capacidad_m3": "Capacidad_m3",
        "tipo": "Tipo",
        "habilitado": "Habilitado_flag"
    }, inplace=True)
    view["Habilitado (SI/NO)"] = view["Habilitado_flag"].apply(lambda x: "YES" if int(x) == 1 else "NO")
    view = view[["MixerID", "Unidad", "Placa", "Habilitado (SI/NO)", "Capacidad_m3", "Tipo"]]

    try:
        st.dataframe(view, use_container_width=True, hide_index=True)
    except TypeError:
        st.dataframe(view.style.hide(axis="index"), use_container_width=True)

    st.markdown("### 🔁 Alternar habilitado")
    if dfm.empty:
        st.info("No hay mixers cargados.")
    else:
        opciones = {
            f"ID {int(r.MixerID)} — {r.Unidad or 's/n'} — {r.Placa} ({r.Capacidad_m3} m³, {r.Tipo}) — "
            f"{'HABILITADO' if dfm.loc[dfm['id']==int(r.MixerID),'habilitado'].iloc[0]==1 else 'DESHABILITADO'}"
            : int(r.MixerID)
            for _, r in view.iterrows()
        }
        sel = st.selectbox("Selecciona un mixer", list(opciones.keys()))
        mixer_id = opciones[sel]

        cur.execute("SELECT habilitado FROM mixers WHERE id=?", (mixer_id,))
        row = cur.fetchone()
        if row is None:
            st.error("No se pudo leer el estado del mixer seleccionado.")
        else:
            estado = int(row[0])
            etiqueta = "DESHABILITAR" if estado == 1 else "HABILITAR"
            if st.button(etiqueta):
                nuevo = 0 if estado == 1 else 1
                cur.execute("UPDATE mixers SET habilitado=? WHERE id=?", (nuevo, mixer_id))
                conn.commit()
                st.success(f"Mixer {mixer_id} {'habilitado' if nuevo==1 else 'deshabilitado'}.")
                st.rerun()

    st.markdown("### ✏️ Editar Unidad y Placa (manual)")
    editable = st.data_editor(
        dfm[["id", "unidad_id", "placa"]],
        hide_index=True,
        use_container_width=True
    )
    if st.button("💾 Guardar cambios de Unidad/Placa"):
        for _, row in editable.iterrows():
            cur.execute(
                "UPDATE mixers SET unidad_id=?, placa=? WHERE id=?",
                (row["unidad_id"], row["placa"], int(row["id"]))
            )
        conn.commit()
        st.success("Cambios guardados.")
        st.rerun()

# 3) Nuevo Proyecto (viaje simple)
with tabs[2]:
    st.subheader("Nuevo Proyecto (viaje simple)")

    col1, col2, col3 = st.columns(3)
    with col1:
        cliente = st.text_input("Cliente")
        proyecto = st.text_input("Proyecto")
        fecha = st.date_input("Fecha", datetime.now())
    with col2:
        hora_Q = st.text_input("Hora en obra (HH:MM)", "08:00")
        min_viaje_ida = st.number_input("Minutos viaje ida", 0, 240, 30)
        volumen_m3 = st.number_input("Volumen (m³)", 1.0, 12.0, 8.5, step=0.5)
    with col3:
        requiere_bomba = st.selectbox("¿Requiere bomba?", ["NO", "YES"])
        dosificadora = st.selectbox("Dosificadora", ["DF-01", "DF-06"])
        mixer_id = st.number_input("Mixer ID (1-14)", 1, 14, 1)

    if st.button("Guardar viaje"):
        # Parámetros para cálculo
        for key in ["tiempo_descarga_min", "margen_lavado_min", "tiempo_cambio_obra_min"]:
            c.execute("SELECT valor FROM parametros WHERE nombre=?", (key,))
            val = c.fetchone()
            if val is None:
                st.error(f"Parámetro faltante: {key}")
                st.stop()

        c.execute("SELECT valor FROM parametros WHERE nombre='tiempo_descarga_min'")
        tiempo_descarga_min = c.fetchone()[0]
        c.execute("SELECT valor FROM parametros WHERE nombre='margen_lavado_min'")
        margen_lavado_min = c.fetchone()[0]
        c.execute("SELECT valor FROM parametros WHERE nombre='tiempo_cambio_obra_min'")
        tiempo_cambio_obra_min = c.fetchone()[0]

        # Verifica mixer
        c.execute("SELECT capacidad_m3 FROM mixers WHERE id=?", (int(mixer_id),))
        row = c.fetchone()
        if not row:
            st.error("Mixer no existe. Revisa el ID (1-14).")
            st.stop()
        capacidad_mixer = row[0]

        try:
            R, S, T, U, V, W, X = calcular_tiempos(
                hora_Q, min_viaje_ida, volumen_m3,
                tiempo_descarga_min, margen_lavado_min, tiempo_cambio_obra_min
            )
        except ValueError:
            st.error("Formato de hora inválido. Usa HH:MM (ej. 08:00).")
            st.stop()

        c.execute("""
            INSERT INTO agenda (
                cliente, proyecto, fecha, hora_Q, min_viaje_ida, volumen_m3, requiere_bomba,
                dosificadora, mixer_id, hora_R, hora_S, hora_T, hora_U, hora_V, hora_W, hora_X
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            cliente, proyecto, fecha.strftime("%Y-%m-%d"), hora_Q, int(min_viaje_ida), float(volumen_m3),
            requiere_bomba, dosificadora, int(mixer_id),
            R.strftime("%H:%M"), S.strftime("%H:%M"), T.strftime("%H:%M"),
            U.strftime("%H:%M"), V.strftime("%H:%M"), W.strftime("%H:%M"), X.strftime("%H:%M")
        ))
        conn.commit()
        st.success("✅ Viaje guardado correctamente")

# 4) Calendario del día
with tabs[3]:
    st.subheader("Agenda del día")
    hoy = datetime.now().strftime("%Y-%m-%d")
    df_agenda = pd.read_sql("SELECT * FROM agenda WHERE fecha = ?", conn, params=(hoy,))
    if df_agenda.empty:
        st.info("No hay viajes registrados para hoy.")
    else:
        st.dataframe(df_agenda, use_container_width=True)
