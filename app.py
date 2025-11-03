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

    # --- Patch de esquema: agrega unidad_id si no existe (para almacenar '218 25', 'MX 25', etc.)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(mixers)")
    cols = [r[1].lower() for r in cur.fetchall()]
    if "unidad_id" not in cols:
        cur.execute("ALTER TABLE mixers ADD COLUMN unidad_id TEXT")  # nullable
        conn.commit()

    # --- Cargar datos
    dfm = pd.read_sql("SELECT id, unidad_id, placa, activo, habilitado, capacidad_m3, tipo FROM mixers ORDER BY id", conn)

    # Métricas (disponibles = habilitado=1)
    total_disponibles = int((dfm["habilitado"] == 1).sum()) if not dfm.empty else 0
    volumen_disponible = float(dfm.loc[dfm["habilitado"] == 1, "capacidad_m3"].sum()) if not dfm.empty else 0.0

    cA, cB, cC = st.columns([1, 1, 2])
    cA.metric("Mixers disponibles (habilitados)", total_disponibles)
    cB.metric("Volumen disponible (m³)", f"{volumen_disponible:.1f}")
    with cC:
        st.caption("Disponibles = habilitado=1 (pueden asignarse). 'Activo' se mantiene como bandera operativa aparte.")

    # --- Vista amigable: columnas renombradas y estados YES/NO
    view = dfm.copy()

    # Donde quieras mostrar tu ID libre (Excel): si no lo tienes aún, puedes editar después con un update sencillo
    view.rename(columns={
        "unidad_id": "Unidad",
        "placa": "Placa",
        "activo": "Activo_flag",
        "habilitado": "Habilitado_flag",
        "capacidad_m3": "Capacidad_m3",
        "tipo": "Tipo",
        "id": "MixerID"
    }, inplace=True)

    # Columnas visibles y orden
    view["Activo (SI/NO)"] = view["Activo_flag"].apply(lambda x: "YES" if int(x) == 1 else "NO")
    view["Habilitado (SI/NO)"] = view["Habilitado_flag"].apply(lambda x: "YES" if int(x) == 1 else "NO")

    # Botón por fila para alternar habilitado
    # Creamos una columna 'Toggle' con el texto del botón según estado actual
    view["Toggle"] = view["Habilitado (SI/NO)"].apply(lambda s: "DESHABILITAR" if s == "YES" else "HABILITAR")

    # Selección de columnas a mostrar
    show_cols = ["MixerID", "Unidad", "Placa", "Activo (SI/NO)", "Habilitado (SI/NO)", "Capacidad_m3", "Tipo", "Toggle"]

    # Data editor con botón por fila (ButtonColumn)
    from streamlit import column_config
    # Índice oculto (sin 0..n-1)
    try:
        edited = st.data_editor(
            view[show_cols],
            key="mixers_editor",
            use_container_width=True,
            hide_index=True,
            column_config={
                "Toggle": column_config.ButtonColumn(
                    "Habilitar/Deshabilitar",
                    help="Alterna el estado 'Habilitado' de este mixer",
                    width="small"
                )
            }
        )
    except TypeError:
        # Fallback si tu versión no soporta hide_index
        edited = st.data_editor(
            view[show_cols].style.hide(axis="index"),
            key="mixers_editor",
            use_container_width=True,
            column_config={
                "Toggle": column_config.ButtonColumn(
                    "Habilitar/Deshabilitar",
                    help="Alterna el estado 'Habilitado' de este mixer",
                    width="small"
                )
            }
        )

    # Si se presionó algún botón en esta ejecución, 'edited' tendrá True en esa celda
    # Detectamos clic en ButtonColumn comparando con el dataframe original 'view'
    try:
        # Buscamos filas donde cambió 'Toggle' a True (convención de Streamlit para ButtonColumn)
        # Nota: En las versiones recientes, ButtonColumn devuelve True en la celda presionada en el ciclo actual
        # Intentamos localizar esa fila por índice
        if isinstance(edited, pd.DataFrame):
            # Cuando se hace click, la celda 'Toggle' en esa fila queda en True en este run
            clicked_mask = edited["Toggle"] == True
            if clicked_mask.any():
                clicked_rows = edited[clicked_mask]
                for _, row in clicked_rows.iterrows():
                    mixer_id = int(row["MixerID"])
                    # Leer estado actual real
                    cur.execute("SELECT habilitado FROM mixers WHERE id=?", (mixer_id,))
                    cur_state = cur.fetchone()
                    if cur_state is not None:
                        nuevo = 0 if int(cur_state[0]) == 1 else 1
                        cur.execute("UPDATE mixers SET habilitado=? WHERE id=?", (nuevo, mixer_id))
                        conn.commit()
                st.success("Estado actualizado.")
                st.rerun()
    except Exception:
        # En caso de variaciones de versión, hacemos un plan B con selector + botón
        st.info("Si el botón por fila no responde en tu versión de Streamlit, usa el control rápido de abajo.")
        # Control rápido alternativo
        opciones = {f"ID {int(r.MixerID)} — {r.Placa} ({r.Capacidad_m3} m³)": int(r.MixerID) for _, r in view.iterrows()}
        sel = st.selectbox("Mixer para alternar habilitado", list(opciones.keys()))
        mixer_id = opciones[sel]
        cur.execute("SELECT habilitado FROM mixers WHERE id=?", (mixer_id,))
        cur_state = cur.fetchone()
        if cur_state is not None:
            etiqueta = "DESHABILITAR" if int(cur_state[0]) == 1 else "HABILITAR"
            if st.button(etiqueta):
                nuevo = 0 if int(cur_state[0]) == 1 else 1
                cur.execute("UPDATE mixers SET habilitado=? WHERE id=?", (nuevo, mixer_id))
                conn.commit()
                st.success("Estado actualizado.")
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
