import os
import io
import base64
import re
from datetime import datetime, date, timedelta
from hashlib import pbkdf2_hmac
from typing import Dict, List, Tuple, Optional

import streamlit as st
from PIL import Image
from streamlit_drawable_canvas import st_canvas

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image as RLImage, PageBreak
)
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.utils import ImageReader

# ---------------------------
# CONFIG
# ---------------------------
APP_TITLE = "Checklist Equipos - Ferrosalt"
ASSETS_DIR = "assets"
LOGO_PATH = os.path.join(ASSETS_DIR, "logo.png")

SUPERVISOR_NOMBRE_DEFAULT = st.secrets.get("SUPERVISOR_NOMBRE", "Miguel Alarcón")
ADMIN_USER = st.secrets.get("ADMIN_USER", "Supervisor")
ADMIN_PASSWORD = st.secrets.get("ADMIN_PASSWORD", "1996")

STATUS_OPCIONES = ["OPERATIVO", "OPERATIVO CON FALLA", "INOPERATIVO"]

# ---------------------------
# GOOGLE SHEETS
# ---------------------------
@st.cache_resource
def get_google_client():
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except Exception as e:
        return None, None, f"Faltan librerías Google/gspread: {e}"

    sheet_id = (st.secrets.get("SHEET_ID", "") or "").strip()
    sa_info = st.secrets.get("gcp_service_account", None)

    if not sheet_id:
        return None, None, "Falta SECRET: SHEET_ID"
    if not sa_info or not isinstance(sa_info, dict):
        return None, None, "Falta SECRET: [gcp_service_account] (formato TOML recomendado)"

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",  # solo para abrir el sheet sin dramas
    ]
    try:
        creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
        gc = gspread.authorize(creds)
        return gc, sheet_id, None
    except Exception as e:
        return None, None, f"No se pudo autenticar con Google: {e}"

def debug_google():
    st.sidebar.markdown("## 🔧 Diagnóstico Google")

    has_sa = isinstance(st.secrets.get("gcp_service_account", None), dict)
    has_sheet = bool((st.secrets.get("SHEET_ID", "") or "").strip())

    st.sidebar.write("gcp_service_account:", "✅" if has_sa else "❌")
    st.sidebar.write("SHEET_ID:", "✅" if has_sheet else "❌")

    gc, sheet_id, err = get_google_client()
    if err:
        st.sidebar.error(err)
        st.sidebar.info("Revisa Secrets + compartir el Sheet con la service account.")
        return

    try:
        sh = gc.open_by_key(sheet_id)
        ws_names = [w.title for w in sh.worksheets()]
        st.sidebar.success("Conectado a Google Sheets ✅")
        st.sidebar.write("Hojas:", ws_names)
    except Exception as e:
        st.sidebar.error("Error accediendo al Sheet:")
        st.sidebar.code(str(e))

# ---------------------------
# SHEETS: SCHEMA + HELPERS
# ---------------------------
USERS_HEADERS = ["username", "full_name", "role", "active", "salt", "pw_hash", "created_at"]

REPORTS_HEADERS = [
    "report_id",
    "equipment_tipo",
    "equipment_codigo",
    "equipment_nombre",
    "horometro_inicial",
    "operador_user",
    "operador_nombre",
    "created_at",
    "created_date",
    "resultado_final",
    "estado_general",
    "observaciones_generales",
]

REPORT_ITEMS_HEADERS = ["report_id", "seccion", "item", "estado", "observacion", "tiene_foto"]

def _open_sheet():
    gc, sheet_id, err = get_google_client()
    if err or not gc:
        raise RuntimeError(err or "No hay cliente Google")
    return gc.open_by_key(sheet_id)

def ensure_sheet_exists(sheet_name: str, headers: list):
    try:
        sh = _open_sheet()
        try:
            ws = sh.worksheet(sheet_name)
        except Exception:
            ws = sh.add_worksheet(title=sheet_name, rows="2000", cols=str(max(10, len(headers) + 5)))
            ws.append_row(headers, value_input_option="RAW")
            return True, f"Hoja '{sheet_name}' creada."

        first_row = ws.row_values(1)
        if [h.strip() for h in first_row] != headers:
            return False, (
                f"⚠️ La hoja '{sheet_name}' existe pero los headers NO coinciden.\n"
                f"Esperado: {headers}\n"
                f"Actual:   {first_row}\n"
                f"Solución: reemplaza la fila 1 por los headers esperados."
            )
        return True, f"Hoja '{sheet_name}' OK."
    except Exception as e:
        return False, f"Error creando/verificando hoja '{sheet_name}': {e}"

def init_google_schema():
    checks = [
        ensure_sheet_exists("users", USERS_HEADERS),
        ensure_sheet_exists("reports", REPORTS_HEADERS),
        ensure_sheet_exists("report_items", REPORT_ITEMS_HEADERS),
    ]
    for ok, msg in checks:
        if not ok:
            st.warning(msg)

def append_row_sheet(sheet_name: str, row: list):
    sh = _open_sheet()
    ws = sh.worksheet(sheet_name)
    ws.append_row(row, value_input_option="USER_ENTERED")

def sheet_records(sheet_name: str) -> list:
    try:
        sh = _open_sheet()
        ws = sh.worksheet(sheet_name)
        return ws.get_all_records()
    except Exception:
        return []

def next_report_id() -> int:
    rows = sheet_records("reports")
    mx = 0
    for r in rows:
        try:
            mx = max(mx, int(r.get("report_id", 0)))
        except Exception:
            pass
    return mx + 1

# ---------------------------
# EQUIPOS + CHECKLISTS (NO TOCAR)
# ---------------------------
EQUIPOS = [
    {"tipo": "apilador", "codigo": "AP1", "nombre": "Apilador 1"},
    {"tipo": "apilador", "codigo": "AP3", "nombre": "Apilador 3"},
    {"tipo": "apilador", "codigo": "AP4", "nombre": "Apilador 4"},
    {"tipo": "transpaleta", "codigo": "TP3", "nombre": "Transpaleta 3"},
    {"tipo": "transpaleta", "codigo": "TP4", "nombre": "Transpaleta 4"},
    {"tipo": "electrico", "codigo": "ME7", "nombre": "Montacargas Eléctrico 7 (ME7)"},
    {"tipo": "electrico", "codigo": "ME8", "nombre": "Montacargas Eléctrico 8 (ME8)"},
    {"tipo": "electrico", "codigo": "ME11", "nombre": "Montacargas Eléctrico 11 (ME11)"},
    {"tipo": "combustion", "codigo": "MC5", "nombre": "Montacargas Combustión 5"},
]

CHECKLISTS: Dict[str, List[Tuple[str, List[str]]]] = {
    "apilador": [
        ("INSPECCIÓN VISUAL Y SENSORIAL", [
            "Pintura", "Espejos", "Extintor", "Rueda central", "Rueda de carga", "Rueda de apoyo",
            "Seguro de uñas", "Cargador y conectores de la batería", "Estado de las horquillas", "Estado de las cadenas"
        ]),
        ("NIVELES DE LÍQUIDOS", ["Agua destilada de la batería", "Aceite hidráulico"]),
        ("CONSERVACIÓN DEL EQUIPO", ["Lubricación de los puntos de engrase", "Engrase del mástil", "Pulverizado y limpieza del equipo"]),
        ("INSPECCIÓN DE FUNCIONAL", ["Panel visualizador (Display)", "Palanca de mando (joystick)", "Estado de la cámara",
                                     "Pedal de control", "Luces delanteras", "Luces posteriores, circulina", "Claxon/alarma de retroceso"]),
        ("OPERACIÓN DEL MÁSTIL", ["Elevación al máximo", "Estado del vástago de pistón de elevación central",
                                  "Estado del vástago de pistones de elevación lateral", "Inclinación al máximo",
                                  "Estado del vástago de pistones de inclinación", "Desplazador lateral",
                                  "Estado del vástago pistón de desplazamiento", "Estado de las mangueras hidráulicas",
                                  "Estado del carro porta horquilla retráctil", "Estado del porta horquillas tipo pantógrafo"])
    ],
    "transpaleta": [
        ("COMPONENTE", [
            "Panel indicador", "Rueda central", "Rueda de apoyo", "Rueda de carga",
            "Lubricación de puntos de engrase", "Manubrio de control", "Brazo de seguridad",
            "Claxon", "Luces delanteras", "Nivel de agua destilada de la batería",
            "Cable de alimentación", "Otros"
        ])
    ],
    "electrico": [
        ("INSPECCIÓN VISUAL Y SENSORIAL (equipo apagado)", [
            "Pintura", "Cabina", "Extintor", "Asiento", "Cinturón de seguridad", "Espejos",
            "Seguro de uñas", "Llanta de tracción", "Llanta de dirección", "Cargador y conectores de la batería"
        ]),
        ("NIVELES (equipo apagado)", ["Agua destilada de la batería", "Líquido de freno", "Aceite hidráulico", "Aceite de corona"]),
        ("FUNCIONAL (equipo encendido)", ["Nivel de carga de la batería", "Indicador de horómetro", "Indicador de freno de estacionamiento"]),
        ("CONDUCCIÓN", [
            "Marcha adelante", "Marcha atrás", "Dirección ruedas traseras", "Tracción ruedas delanteras",
            "Pedal de control", "Luces delanteras", "Luces posteriores (freno, intermitente, direccionales)",
            "Claxon/alarma de retroceso", "Freno de servicio (pedal)", "Freno de estacionamiento (manual)"
        ]),
        ("OPERACIÓN DEL MÁSTIL (equipo encendido)", [
            "Mecanismo de pistón de elevación", "Mecanismo de pistón de inclinación", "Mecanismo de pistón de desplazamiento",
            "Estado del vástago de pistón de elevación central", "Estado del vástago de pistones de elevación lateral",
            "Estado del vástago de pistones de inclinación", "Estado del vástago de pistón de desplazamiento",
            "Estado de las mangueras hidráulicas", "Estado de las cadenas", "Estado de los rodamientos"
        ]),
        ("LIMPIEZA Y CONSERVACIÓN", ["Lubricación de puntos de engrase", "Engrase del mástil", "Pulverizado de partes internas", "Limpieza del equipo", "Otros"])
    ],
    "combustion": [
        ("INSPECCIÓN VISUAL Y SENSORIAL (equipo apagado)", [
            "Pintura", "Cabina", "Extintor", "Asiento", "Cinturón de seguridad", "Espejos",
            "Seguro de uñas", "Llanta de tracción", "Llanta de dirección"
        ]),
        ("NIVELES (equipo apagado)", [
            "Aceite de motor", "Agua de radiador", "Aceite de transmisión", "Líquido de freno",
            "Aceite hidráulico", "Aceite de caja y corona", "Cargador y conectores de la batería"
        ]),
        ("FUNCIONAL (equipo encendido)", ["Nivel de carga de la batería", "Indicador de horómetro", "Indicador de freno de estacionamiento"]),
        ("CONDUCCIÓN", [
            "Marcha adelante", "Marcha atrás", "Dirección ruedas traseras", "Tracción ruedas delanteras",
            "Pedal de control", "Luces delanteras", "Luces posteriores (freno, intermitente, direccionales)",
            "Claxon/alarma de retroceso", "Freno de servicio (pedal)", "Freno de estacionamiento (manual)"
        ]),
        ("OPERACIÓN DEL MÁSTIL (equipo encendido)", [
            "Mecanismo de pistón de elevación", "Mecanismo de pistón de inclinación", "Mecanismo de pistón de desplazamiento",
            "Estado del vástago de pistón de elevación central", "Estado del vástago de pistones de elevación lateral",
            "Estado del vástago de pistones de inclinación", "Estado del vástago de pistón de desplazamiento",
            "Estado de las mangueras hidráulicas", "Estado de las cadenas", "Estado de los rodamientos"
        ]),
        ("LIMPIEZA Y CONSERVACIÓN", ["Lubricación de puntos de engrase", "Engrase del mástil", "Pulverizado de partes internas", "Limpieza del equipo", "Otros"])
    ]
}

# ---------------------------
# AUTH (Users in Sheets)
# ---------------------------
def hash_password(password: str, salt: bytes) -> str:
    dk = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return base64.b64encode(dk).decode("utf-8")

def init_db_like():
    init_google_schema()

    users = sheet_records("users")
    exists = any(u.get("username") == ADMIN_USER for u in users)
    if not exists:
        salt = os.urandom(16)
        salt_b64 = base64.b64encode(salt).decode("utf-8")
        pw_hash = hash_password(ADMIN_PASSWORD, salt)
        append_row_sheet("users", [
            ADMIN_USER, "Supervisor", "supervisor", 1, salt_b64, pw_hash,
            datetime.now().isoformat(timespec="seconds")
        ])

def auth_user(username: str, password: str):
    username = username.strip()
    users = sheet_records("users")
    row = next((u for u in users if u.get("username") == username and int(u.get("active", 1)) == 1), None)
    if not row:
        return None
    salt = base64.b64decode(row["salt"])
    pw_hash = hash_password(password, salt)
    if pw_hash != row["pw_hash"]:
        return None
    return {"username": row["username"], "full_name": row["full_name"], "role": row["role"]}

def create_user(username: str, full_name: str, password: str, role: str, active: bool):
    username = username.strip()
    users = sheet_records("users")
    if any(u.get("username") == username for u in users):
        raise ValueError("Ese usuario ya existe.")

    salt = os.urandom(16)
    salt_b64 = base64.b64encode(salt).decode("utf-8")
    pw_hash = hash_password(password, salt)

    append_row_sheet("users", [
        username, full_name.strip(), role, 1 if active else 0,
        salt_b64, pw_hash, datetime.now().isoformat(timespec="seconds")
    ])

def fetch_users():
    users = sheet_records("users")
    users.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    out = []
    for u in users:
        out.append({
            "username": u.get("username", ""),
            "full_name": u.get("full_name", ""),
            "role": u.get("role", ""),
            "active": u.get("active", 1),
            "created_at": u.get("created_at", ""),
        })
    return out

# ---------------------------
# LOGIC
# ---------------------------
def compute_result(items_estado: List[str]) -> Tuple[str, str]:
    if any(s == "INOPERATIVO" for s in items_estado):
        return ("INOPERATIVO", "NO APTO")
    if any(s == "OPERATIVO CON FALLA" for s in items_estado):
        return ("FALLA", "RESTRICCIONES")
    return ("OPERATIVO", "APTO")

# ---------------------------
# PDF (NO SE GUARDA, SOLO DESCARGA)
# ---------------------------
NAVY = colors.HexColor("#0B2A5A")
styles = getSampleStyleSheet()

STYLE_TITLE = ParagraphStyle("t", parent=styles["Title"], alignment=TA_CENTER,
                             fontName="Helvetica-Bold", fontSize=14, textColor=NAVY)
STYLE_H2 = ParagraphStyle("h2", parent=styles["Heading2"],
                          fontName="Helvetica-Bold", textColor=NAVY, spaceBefore=6, spaceAfter=6)
STYLE_SMALL = ParagraphStyle("sm", parent=styles["Normal"], fontSize=9, leading=11)
STYLE_SMALL_B = ParagraphStyle("smb", parent=STYLE_SMALL, fontName="Helvetica-Bold")
STYLE_CENTER = ParagraphStyle("c", parent=STYLE_SMALL, alignment=TA_CENTER)
STYLE_CENTER_W = ParagraphStyle("cw", parent=STYLE_CENTER, textColor=colors.white, fontName="Helvetica-Bold")
STYLE_SMALL_B_W = ParagraphStyle("smbw", parent=STYLE_SMALL_B, textColor=colors.white, fontName="Helvetica-Bold")

def _rl_img_from_path(path: str, w_mm: float, h_mm: float):
    if not path or not os.path.exists(path):
        return None
    img = RLImage(path, width=w_mm * mm, height=h_mm * mm)
    img.hAlign = "LEFT"
    return img

def _rl_img_from_bytes(img_bytes: bytes, w_mm: float, h_mm: float):
    if not img_bytes:
        return None
    bio = io.BytesIO(img_bytes)
    return RLImage(ImageReader(bio), width=w_mm * mm, height=h_mm * mm)

def canvas_to_png_bytes(canvas_result) -> bytes:
    if canvas_result is None or canvas_result.image_data is None:
        return b""
    img = Image.fromarray(canvas_result.image_data.astype("uint8")).convert("RGBA")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()

def upload_to_png_bytes(uploaded_file) -> bytes:
    if not uploaded_file:
        return b""
    try:
        data = uploaded_file.getvalue()
        # normalizamos a PNG para que ReportLab no falle
        img = Image.open(io.BytesIO(data)).convert("RGB")
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return b""

def generate_pdf_bytes(payload: dict) -> Tuple[bytes, str]:
    """
    Genera PDF en memoria (bytes). Incluye:
    - Tabla checklist
    - Observaciones
    - Firma operador
    - Fotos adjuntas (solo ítems con evidencia), desde bytes
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm
    )
    story = []

    logo = _rl_img_from_path(LOGO_PATH, 35, 10)
    header_tbl = Table([[logo if logo else "", Paragraph("CHECKLIST DE EQUIPO", STYLE_TITLE)]],
                       colWidths=[45 * mm, 135 * mm])
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
        ("ALIGN", (1, 0), (1, 0), "CENTER"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(header_tbl)

    info = [
        [Paragraph(f"<b>Equipo:</b> {payload['equipment_nombre']}", STYLE_SMALL),
         Paragraph(f"<b>Código:</b> {payload['equipment_codigo']}", STYLE_SMALL),
         Paragraph(f"<b>Tipo:</b> {payload['equipment_tipo']}", STYLE_SMALL)],
        [Paragraph(f"<b>Operador:</b> {payload['operador_nombre']}", STYLE_SMALL),
         Paragraph(f"<b>Horómetro:</b> {payload['horometro']}", STYLE_SMALL),
         Paragraph(f"<b>Fecha:</b> {payload['created_at']}", STYLE_SMALL)],
        [Paragraph(f"<b>Resultado:</b> {payload['resultado_final']}", STYLE_SMALL_B),
         Paragraph(f"<b>Estado:</b> {payload['estado_general']}", STYLE_SMALL_B),
         Paragraph("", STYLE_SMALL)],
    ]
    info_tbl = Table(info, colWidths=[70 * mm, 55 * mm, 55 * mm])
    info_tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(info_tbl)
    story.append(Spacer(1, 6 * mm))

    data = [[
        Paragraph("Sección", STYLE_CENTER_W),
        Paragraph("Ítem", STYLE_CENTER_W),
        Paragraph("Estado", STYLE_CENTER_W),
        Paragraph("Observación", STYLE_CENTER_W),
    ]]

    for it in payload["items"]:
        data.append([
            Paragraph(it["seccion"], STYLE_SMALL),
            Paragraph(it["item"], STYLE_SMALL),
            Paragraph(it["estado"], STYLE_SMALL),
            Paragraph((it.get("observacion") or "-"), STYLE_SMALL),
        ])

    tbl = Table(data, colWidths=[55 * mm, 65 * mm, 30 * mm, 35 * mm], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(tbl)

    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("<b>Observaciones generales:</b> " + (payload.get("obs_general") or "NINGUNA"), STYLE_SMALL))

    # Fotos (solo evidencia)
    fotos = [(it["item"], it["seccion"], it.get("foto_bytes") or b"") for it in payload["items"] if it.get("foto_bytes")]
    if fotos:
        story.append(PageBreak())
        story.append(header_tbl)
        story.append(Paragraph("Fotos adjuntas (solo ítems con evidencia)", STYLE_H2))
        story.append(Spacer(1, 2 * mm))

        grid = []
        row = []
        for (item_name, sec, bts) in fotos:
            cell_story = []
            cell_story.append(Paragraph(f"<b>{item_name}</b><br/>{sec}", STYLE_SMALL))
            img = _rl_img_from_bytes(bts, 80, 45)
            if img:
                cell_story.append(Spacer(1, 2 * mm))
                cell_story.append(img)
            row.append(cell_story)

            if len(row) == 2:
                grid.append(row)
                row = []

        if row:
            row.append("")
            grid.append(row)

        photo_tbl = Table(grid, colWidths=[90 * mm, 90 * mm])
        photo_tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(photo_tbl)

    # Firma operador (al final)
    story.append(PageBreak())
    story.append(Paragraph("Firma Operador", STYLE_H2))
    sig = _rl_img_from_bytes(payload.get("firma_operador_bytes", b""), 80, 28)
    if sig:
        story.append(sig)
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(payload["operador_nombre"], STYLE_SMALL))

    doc.build(story)
    pdf_bytes = buffer.getvalue()
    fname = f"CHECKLIST_{payload['equipment_codigo']}_{payload['created_date']}.pdf"
    return pdf_bytes, fname

# ---------------------------
# UI
# ---------------------------
def sidebar_user():
    name = st.session_state.get("full_name")
    role = st.session_state.get("role")
    if not name or not role:
        return

    st.sidebar.markdown(f"### 👤 {name}")
    st.sidebar.markdown(f"🔑 **Rol:** {role}")
    if st.sidebar.button("Cerrar sesión"):
        st.session_state.clear()
        st.rerun()

def login_ui():
    st.title("🔐 Ingreso")
    st.caption("Operadores y supervisores ingresan con usuario y clave.")

    with st.form("login_form", clear_on_submit=False):
        u = st.text_input("Usuario")
        p = st.text_input("Clave", type="password")
        ok = st.form_submit_button("Ingresar")

    if ok:
        user = auth_user(u, p)
        if not user:
            st.error("Usuario o clave incorrectos.")
            return
        st.session_state["user"] = user["username"]
        st.session_state["role"] = user["role"]
        st.session_state["full_name"] = user["full_name"]
        st.rerun()

def supervisor_panel():
    st.subheader(f"🧑‍💼 Supervisor: {st.session_state.get('full_name','')}")
    tabs = st.tabs(["Usuarios", "Reportes (Sheet)", "Panel de control"])

    with tabs[0]:
        st.markdown("## Crear usuario")
        with st.form("create_user"):
            username = st.text_input("Usuario (sin espacios)")
            full_name = st.text_input("Nombre completo")
            password = st.text_input("Clave", type="password")
            role = st.selectbox("Rol", ["operador", "supervisor"])
            active = st.checkbox("Activo", value=True)
            save = st.form_submit_button("Guardar usuario")

        if save:
            try:
                if not username or not full_name or not password:
                    st.error("Completa usuario, nombre y clave.")
                else:
                    create_user(username, full_name, password, role, active)
                    st.success("✅ Usuario creado correctamente")
            except Exception as e:
                st.error(str(e))

        st.markdown("## Lista de usuarios")
        st.dataframe(fetch_users(), use_container_width=True)

    with tabs[1]:
        st.markdown("## Reportes guardados en Sheets")
        reps = sheet_records("reports")
        if not reps:
            st.info("Aún no hay reportes.")
        else:
            st.dataframe(reps, use_container_width=True)

    with tabs[2]:
        st.markdown("## Panel de control (desde Sheets)")

        rango = st.selectbox("Rango", ["Diario", "Semanal", "Mensual"], index=0)
        today = date.today()
        if rango == "Diario":
            start = today
        elif rango == "Semanal":
            start = today - timedelta(days=7)
        else:
            start = today - timedelta(days=30)

        reps = sheet_records("reports")
        rows = []
        for r in reps:
            try:
                dte = (r.get("created_date") or "").strip()
                if dte and dte >= start.isoformat():
                    rows.append(r)
            except Exception:
                pass

        total = len(rows)
        operadores = len(set(r.get("operador_nombre","") for r in rows if r.get("operador_nombre"))) if rows else 0
        equipos_con_envio = len(set(r.get("equipment_codigo","") for r in rows if r.get("equipment_codigo"))) if rows else 0
        total_equipos = len(EQUIPOS)
        equipos_sin_envio = total_equipos - equipos_con_envio

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Informes", total)
        c2.metric("Operadores con envíos", operadores)
        c3.metric("Equipos con envíos", equipos_con_envio)
        c4.metric("Equipos sin envío", equipos_sin_envio)

        res_counts = {"APTO": 0, "RESTRICCIONES": 0, "NO APTO": 0}
        for r in rows:
            k = (r.get("resultado_final") or "").strip()
            if k in res_counts:
                res_counts[k] += 1

        st.markdown("### Resumen Resultados")
        st.write(f"✅ APTO: **{res_counts['APTO']}**  |  ⚠️ RESTRICCIONES: **{res_counts['RESTRICCIONES']}**  |  ⛔ NO APTO: **{res_counts['NO APTO']}**")

def _reset_operator_checklist_state():
    keys = list(st.session_state.keys())
    for k in keys:
        if (
            "::" in k
            or k.startswith("hor_")
            or k.startswith("sig_op_")
            or k.startswith("obsgen_")
            or k.startswith("send_")
        ):
            try:
                del st.session_state[k]
            except Exception:
                pass

def operator_panel():
    st.subheader(f"👷 Operador: {st.session_state.get('full_name','')}")
    st.info("Completa checklist → firma → enviar. Se guarda SOLO en Sheets y se genera PDF para descargar.")

    eq_label_map = {f"{e['nombre']}": e for e in EQUIPOS}

    def _on_equipo_change():
        _reset_operator_checklist_state()
        st.rerun()

    sel = st.selectbox("Equipo", list(eq_label_map.keys()), key="op_eq_select", on_change=_on_equipo_change)
    eq = eq_label_map[sel]

    horometro = st.number_input("Horómetro inicial", min_value=0, step=1, value=0, key=f"hor_{eq['codigo']}")

    st.markdown("## Lista de verificación")

    items_payload = []
    estados_all = []
    checklist = CHECKLISTS[eq["tipo"]]

    for seccion, items in checklist:
        st.markdown(f"### {seccion}")
        for item in items:
            c1, c2, c3 = st.columns([2.2, 1.2, 2.2])
            with c1:
                st.write(item)
            with c2:
                estado = st.selectbox("Estado", STATUS_OPCIONES, key=f"{eq['codigo']}::{seccion}::{item}::estado")
            with c3:
                obs = st.text_input("Observación (si aplica)", key=f"{eq['codigo']}::{seccion}::{item}::obs")

            foto_bytes = b""
            if estado in ("OPERATIVO CON FALLA", "INOPERATIVO"):
                up = st.file_uploader(
                    f"Foto (se incrusta en el PDF): {item}",
                    type=["png", "jpg", "jpeg", "webp"],
                    key=f"{eq['codigo']}::{seccion}::{item}::foto"
                )
                if up:
                    foto_bytes = upload_to_png_bytes(up)

            estados_all.append(estado)
            items_payload.append({
                "seccion": seccion,
                "item": item,
                "estado": estado,
                "observacion": (obs or "").strip(),
                "foto_bytes": foto_bytes
            })

    estado_general, resultado_final = compute_result(estados_all)

    st.markdown("## Firma operador")
    st.write(f"Resultado automático: **{resultado_final}**")

    sig = st_canvas(
        fill_color="rgba(255,255,255,0)",
        stroke_width=2,
        stroke_color="#000000",
        background_color="#FFFFFF",
        height=120,
        width=520,
        drawing_mode="freedraw",
        key=f"sig_op_{eq['codigo']}"
    )

    obs_general = st.text_area("Observaciones generales (opcional)", key=f"obsgen_{eq['codigo']}")

    if st.button("📨 Enviar y generar PDF (descarga)", key=f"send_{eq['codigo']}"):
        firma_bytes = canvas_to_png_bytes(sig)
        if not firma_bytes:
            st.error("La firma del operador es obligatoria.")
            return

        # si item es falla o inoperativo, exige foto (para el PDF)
        for it in items_payload:
            if it["estado"] in ("OPERATIVO CON FALLA", "INOPERATIVO") and not it.get("foto_bytes"):
                st.error(f"Falta foto para el PDF en: {it['item']}")
                return

        report_id = next_report_id()

        payload = {
            "report_id": report_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "created_date": date.today().isoformat(),
            "equipment_tipo": eq["tipo"],
            "equipment_codigo": eq["codigo"],
            "equipment_nombre": eq["nombre"],
            "horometro": int(horometro),
            "operador_user": st.session_state["user"],
            "operador_nombre": st.session_state.get("full_name", ""),
            "obs_general": (obs_general or "").strip(),
            "estado_general": estado_general,
            "resultado_final": resultado_final,
            "firma_operador_bytes": firma_bytes,
            "items": items_payload
        }

        # 1) Guardar SOLO datos en Sheets (sin fotos, sin firmas, sin PDFs)
        append_row_sheet("reports", [
            report_id,
            payload["equipment_tipo"],
            payload["equipment_codigo"],
            payload["equipment_nombre"],
            payload["horometro"],
            payload["operador_user"],
            payload["operador_nombre"],
            payload["created_at"],
            payload["created_date"],
            payload["resultado_final"],
            payload["estado_general"],
            payload.get("obs_general", ""),
        ])

        for it in payload["items"]:
            append_row_sheet("report_items", [
                report_id,
                it["seccion"],
                it["item"],
                it["estado"],
                it.get("observacion", ""),
                "SI" if bool(it.get("foto_bytes")) else "NO",
            ])

        # 2) Generar PDF en memoria para descargar
        pdf_bytes, pdf_name = generate_pdf_bytes(payload)

        st.success(f"✅ Reporte guardado en Sheets. ID: {report_id}")
        st.download_button(
            "⬇️ Descargar PDF",
            data=pdf_bytes,
            file_name=pdf_name,
            mime="application/pdf"
        )

def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")

    # Sidebar debug siempre visible
    debug_google()

    # Inicializa hojas y usuario admin
    init_db_like()

    if not st.session_state.get("user") or not st.session_state.get("role") or not st.session_state.get("full_name"):
        st.session_state.pop("user", None)
        st.session_state.pop("role", None)
        st.session_state.pop("full_name", None)
        login_ui()
        return

    sidebar_user()

    if st.session_state.get("role") == "supervisor":
        supervisor_panel()
    else:
        operator_panel()

if __name__ == "__main__":
    main()
