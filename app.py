import os
import io
import base64
import json
import re
import tempfile
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
from reportlab.graphics.shapes import Drawing, String
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.legends import Legend


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
# GOOGLE CLIENTS (Streamlit Secrets)
# ---------------------------
@st.cache_resource
def get_google_clients():
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except Exception as e:
        return None, None, None, f"Faltan librerías Google: {e}"

    sa_json_raw = st.secrets.get("GCP_SA_JSON", None)
    sheet_id = (st.secrets.get("SHEET_ID", "") or "").strip()

    if not sa_json_raw or not sheet_id:
        return None, None, None, "Faltan Secrets: GCP_SA_JSON o SHEET_ID"

    try:
        if isinstance(sa_json_raw, str):
            info = json.loads(sa_json_raw)
        elif isinstance(sa_json_raw, dict):
            info = sa_json_raw
        else:
            return None, None, None, "GCP_SA_JSON tiene formato inválido"
    except Exception as e:
        return None, None, None, f"JSON inválido en GCP_SA_JSON: {e}"

    scopes = [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets",
    ]
    try:
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        gc = gspread.authorize(creds)
        drive = build("drive", "v3", credentials=creds)
        return gc, drive, sheet_id, None
    except Exception as e:
        return None, None, None, f"No se pudo autenticar con Google: {e}"


def debug_google():
    st.sidebar.markdown("## 🔧 Diagnóstico Google")

    has_json = bool(st.secrets.get("GCP_SA_JSON", None))
    has_sheet = bool((st.secrets.get("SHEET_ID", "") or "").strip())

    st.sidebar.write("GCP_SA_JSON:", "✅" if has_json else "❌")
    st.sidebar.write("SHEET_ID:", "✅" if has_sheet else "❌")

    gc, drive, sheet_id, err = get_google_clients()
    if err:
        st.sidebar.error(err)
        st.sidebar.info("Revisa: Secrets + requirements.")
        return

    try:
        sh = gc.open_by_key(sheet_id)
        ws_names = [w.title for w in sh.worksheets()]
        st.sidebar.success("Conectado a Google ✅")
        st.sidebar.write("Hojas:", ws_names)

        if st.sidebar.button("✅ Probar escritura en 'reports'"):
            ws = sh.worksheet("reports")
            ws.append_row(
                ["TEST", "ok", datetime.now().isoformat(timespec="seconds")],
                value_input_option="USER_ENTERED",
            )
            st.sidebar.success("OK: se agregó una fila TEST en reports.")
    except Exception as e:
        st.sidebar.error("Error accediendo Sheet:")
        st.sidebar.code(str(e))


# ---------------------------
# DRIVE HELPERS
# ---------------------------
def extract_drive_file_id(url_or_id: str) -> str:
    if not url_or_id:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", url_or_id):
        return url_or_id
    m = re.search(r"/d/([A-Za-z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    return ""


def upload_file_to_drive(local_path: str, folder_id: str) -> str:
    if not local_path or not os.path.exists(local_path) or not folder_id:
        return ""
    try:
        from googleapiclient.http import MediaFileUpload
    except Exception:
        return ""

    _, drive, _, err = get_google_clients()
    if err or not drive:
        return ""

    metadata = {"name": os.path.basename(local_path), "parents": [folder_id]}
    media = MediaFileUpload(local_path, resumable=True)

    created = drive.files().create(
        body=metadata,
        media_body=media,
        fields="id",
    ).execute()

    fid = created.get("id", "")
    return f"https://drive.google.com/file/d/{fid}/view" if fid else ""


def download_drive_file(file_id_or_url: str, suffix: str = "") -> str:
    file_id = extract_drive_file_id(file_id_or_url)
    if not file_id:
        return ""

    try:
        from googleapiclient.http import MediaIoBaseDownload
    except Exception:
        return ""

    _, drive, _, err = get_google_clients()
    if err or not drive:
        return ""

    fd, out_path = tempfile.mkstemp(suffix=suffix or ".bin")
    os.close(fd)

    request = drive.files().get_media(fileId=file_id)
    with open(out_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    return out_path


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
    "operador_firma_path",
    "supervisor_user",
    "supervisor_nombre",
    "supervisor_firma_path",
    "approved_at",
    "estado",
    "pdf_path",
]

REPORT_ITEMS_HEADERS = ["report_id", "seccion", "item", "estado", "observacion", "foto_path"]


def ensure_sheet_exists(sheet_name: str, headers: list) -> Tuple[bool, str]:
    gc, _, sheet_id, err = get_google_clients()
    if err or not gc:
        return False, f"No hay conexión a Google: {err or 'desconocido'}"

    sh = gc.open_by_key(sheet_id)

    try:
        ws = sh.worksheet(sheet_name)
    except Exception:
        ws = sh.add_worksheet(title=sheet_name, rows="2000", cols=str(max(10, len(headers) + 5)))
        ws.append_row(headers, value_input_option="RAW")
        return True, f"Hoja '{sheet_name}' creada con headers."

    # OJO: aquí era donde se te caía. Lo hacemos tolerante.
    try:
        first_row = ws.row_values(1)
    except Exception as e:
        return False, (
            f"⚠️ No pude leer headers de '{sheet_name}'.\n"
            f"Error: {e}\n"
            f"(Tu app igual seguirá, pero revisa permisos/protecciones de esa hoja.)"
        )

    if [h.strip() for h in first_row] != headers:
        return False, (
            f"⚠️ La hoja '{sheet_name}' existe pero los headers NO coinciden.\n"
            f"Esperado: {headers}\n"
            f"Actual:   {first_row}\n"
            f"Solución: pega los headers esperados en la fila 1 (sin cambiar nombres)."
        )
    return True, f"Hoja '{sheet_name}' OK."


def init_google_schema():
    msgs = []
    for name, hdr in [
        ("users", USERS_HEADERS),
        ("reports", REPORTS_HEADERS),
        ("report_items", REPORT_ITEMS_HEADERS),
    ]:
        ok, msg = ensure_sheet_exists(name, hdr)
        msgs.append((ok, msg))

    for ok, msg in msgs:
        if not ok:
            st.warning(msg)


def append_row_sheet(sheet_name: str, row: list):
    gc, _, sheet_id, err = get_google_clients()
    if err or not gc:
        return
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(sheet_name)
    ws.append_row(row, value_input_option="USER_ENTERED")


def sheet_records(sheet_name: str) -> list:
    gc, _, sheet_id, err = get_google_clients()
    if err or not gc:
        return []
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(sheet_name)
    try:
        return ws.get_all_records()
    except Exception:
        # Si hay headers raros, no reventamos toda la app
        return []


def find_row_index_by_value(sheet_name: str, col_name: str, value: str) -> int:
    gc, _, sheet_id, err = get_google_clients()
    if err or not gc:
        return 0
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(sheet_name)
    headers = ws.row_values(1)
    if col_name not in headers:
        return 0
    col = headers.index(col_name) + 1
    cell = ws.find(str(value), in_column=col)
    return cell.row if cell else 0


def update_row_by_headers(sheet_name: str, row_idx: int, updates: dict):
    gc, _, sheet_id, err = get_google_clients()
    if err or not gc or not row_idx:
        return
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(sheet_name)
    headers = ws.row_values(1)
    for k, v in updates.items():
        if k in headers:
            ws.update_cell(row_idx, headers.index(k) + 1, v)


# ---------------------------
# EQUIPOS + CHECKLISTS
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
# LOCAL DIRS
# ---------------------------
def ensure_dirs():
    os.makedirs("data", exist_ok=True)
    os.makedirs(os.path.join("data", "photos"), exist_ok=True)
    os.makedirs(os.path.join("data", "signatures"), exist_ok=True)
    os.makedirs(os.path.join("data", "pdfs"), exist_ok=True)
    os.makedirs("assets", exist_ok=True)


# ---------------------------
# AUTH (Users in Sheets)
# ---------------------------
def hash_password(password: str, salt: bytes) -> str:
    dk = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return base64.b64encode(dk).decode("utf-8")


def init_db():
    ensure_dirs()

    # Schema
    init_google_schema()

    # Crear admin si no existe
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
# REPORTS LOGIC (Sheets)
# ---------------------------
def compute_result(items_estado: List[str]) -> Tuple[str, str]:
    if any(s == "INOPERATIVO" for s in items_estado):
        return ("INOPERATIVO", "NO APTO")
    if any(s == "OPERATIVO CON FALLA" for s in items_estado):
        return ("FALLA", "RESTRICCIONES")
    return ("OPERATIVO", "APTO")


def save_uploaded_image(uploaded_file, folder: str, prefix: str) -> str:
    if not uploaded_file:
        return ""
    ext = os.path.splitext(uploaded_file.name)[1].lower()
    if ext not in [".png", ".jpg", ".jpeg", ".webp"]:
        ext = ".png"
    filename = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{ext}"
    local_path = os.path.join("data", folder, filename)
    with open(local_path, "wb") as f:
        f.write(uploaded_file.getbuffer())

    folder_id = (st.secrets.get("DRIVE_PHOTOS_ID", "") or "").strip()
    drive_url = upload_file_to_drive(local_path, folder_id)
    return drive_url or local_path


def save_signature_from_canvas(canvas_result, folder: str, prefix: str) -> str:
    if canvas_result is None or canvas_result.image_data is None:
        return ""
    img = Image.fromarray(canvas_result.image_data.astype("uint8")).convert("RGBA")
    filename = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
    local_path = os.path.join("data", folder, filename)
    img.save(local_path)

    folder_id = (st.secrets.get("DRIVE_SIGNATURES_ID", "") or "").strip()
    drive_url = upload_file_to_drive(local_path, folder_id)
    return drive_url or local_path


def next_report_id() -> int:
    rows = sheet_records("reports")
    mx = 0
    for r in rows:
        try:
            mx = max(mx, int(r.get("report_id", 0)))
        except Exception:
            pass
    return mx + 1


def insert_report(payload: dict) -> int:
    report_id = next_report_id()

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
        payload.get("firma_operador_path", ""),
        "", "", "", "",
        "PENDIENTE",
        "",
    ])

    for it in payload["items"]:
        append_row_sheet("report_items", [
            report_id,
            it["seccion"],
            it["item"],
            it["estado"],
            it.get("observacion", ""),
            it.get("foto_path", ""),
        ])

    return report_id


def fetch_pending_reports():
    rows = sheet_records("reports")
    pending = []
    for r in rows:
        if str(r.get("estado", "")).strip().upper() == "PENDIENTE":
            try:
                rid = int(r.get("report_id"))
            except Exception:
                continue
            pending.append({
                "id": rid,
                "created_at": r.get("created_at", ""),
                "equipment_codigo": r.get("equipment_codigo", ""),
                "equipment_nombre": r.get("equipment_nombre", ""),
                "operador_nombre": r.get("operador_nombre", ""),
                "resultado_final": r.get("resultado_final", ""),
                "estado_general": r.get("estado_general", ""),
            })
    pending.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return pending


def fetch_report_detail(report_id: int):
    reports = sheet_records("reports")
    rep = next((r for r in reports if str(r.get("report_id", "")) == str(report_id)), None)
    if not rep:
        return None, []
    items_all = sheet_records("report_items")
    items = [it for it in items_all if str(it.get("report_id", "")) == str(report_id)]
    return rep, items


def approve_report(report_id: int, supervisor_user: str, supervisor_nombre: str, firma_path: str, pdf_drive_url: str):
    row_idx = find_row_index_by_value("reports", "report_id", str(report_id))
    if not row_idx:
        return
    update_row_by_headers("reports", row_idx, {
        "supervisor_user": supervisor_user,
        "supervisor_nombre": supervisor_nombre,
        "supervisor_firma_path": firma_path,
        "approved_at": datetime.now().isoformat(timespec="seconds"),
        "estado": "APROBADO",
        "pdf_path": pdf_drive_url or "",
    })


# ---------------------------
# PDF STYLES
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


def _rl_img(path: str, w_mm: float, h_mm: float):
    if not path:
        return None
    if isinstance(path, str) and path.startswith("http"):
        tmp = download_drive_file(path, suffix=".png")
        if tmp and os.path.exists(tmp):
            path = tmp
    if not os.path.exists(path):
        return None
    img = RLImage(path, width=w_mm * mm, height=h_mm * mm)
    img.hAlign = "LEFT"
    return img


# ---------------------------
# PDF: CHECKLIST
# ---------------------------
def generate_checklist_pdf(report_id: int) -> str:
    rep, items = fetch_report_detail(report_id)
    if not rep:
        return ""

    pdf_name = f"CHECKLIST_{rep.get('equipment_codigo','')}_{rep.get('created_date','')}.pdf"
    pdf_path = os.path.join("data", "pdfs", pdf_name)

    doc = SimpleDocTemplate(
        pdf_path, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm
    )

    story = []

    logo = _rl_img(LOGO_PATH, 35, 10)
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
        [Paragraph(f"<b>Equipo:</b> {rep.get('equipment_nombre','')}", STYLE_SMALL),
         Paragraph(f"<b>Código:</b> {rep.get('equipment_codigo','')}", STYLE_SMALL),
         Paragraph(f"<b>Tipo:</b> {rep.get('equipment_tipo','')}", STYLE_SMALL)],
        [Paragraph(f"<b>Operador:</b> {rep.get('operador_nombre','')}", STYLE_SMALL),
         Paragraph(f"<b>Horómetro:</b> {rep.get('horometro_inicial','')}", STYLE_SMALL),
         Paragraph(f"<b>Fecha:</b> {rep.get('created_at','')}", STYLE_SMALL)],
        [Paragraph(f"<b>Resultado:</b> {rep.get('resultado_final','')}", STYLE_SMALL_B),
         Paragraph(f"<b>Estado:</b> {rep.get('estado_general','')}", STYLE_SMALL_B),
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

    for it in items:
        data.append([
            Paragraph(str(it.get("seccion","")), STYLE_SMALL),
            Paragraph(str(it.get("item","")), STYLE_SMALL),
            Paragraph(str(it.get("estado","")), STYLE_SMALL),
            Paragraph(str(it.get("observacion") or "-"), STYLE_SMALL),
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
    story.append(Paragraph("<b>Observaciones generales:</b> " + (rep.get("observaciones_generales") or "NINGUNA"), STYLE_SMALL))

    # Fotos (solo si hay)
    fotos = [(it.get("item",""), it.get("seccion",""), it.get("foto_path","")) for it in items if it.get("foto_path")]
    if fotos:
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph("Fotos adjuntas (solo ítems con evidencia)", STYLE_H2))
        grid = []
        row = []
        for (item_name, sec, pth) in fotos:
            cell_story = []
            cell_story.append(Paragraph(f"<b>{item_name}</b><br/>{sec}", STYLE_SMALL))
            img = _rl_img(str(pth), 80, 45)
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

    story.append(PageBreak())
    story.append(Paragraph("Firmas", STYLE_H2))

    op_sig = _rl_img(rep.get("operador_firma_path") or "", 80, 28)
    sup_sig = _rl_img(rep.get("supervisor_firma_path") or "", 80, 28)

    sig_table = Table([
        [Paragraph("Firma Operador", STYLE_SMALL_B_W), Paragraph("Firma Supervisor", STYLE_SMALL_B_W)],
        [op_sig if op_sig else Paragraph("—", STYLE_SMALL), sup_sig if sup_sig else Paragraph("—", STYLE_SMALL)],
        [Paragraph(rep.get("operador_nombre",""), STYLE_SMALL), Paragraph(rep.get("supervisor_nombre") or SUPERVISOR_NOMBRE_DEFAULT, STYLE_SMALL)]
    ], colWidths=[90 * mm, 90 * mm])

    sig_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(sig_table)

    doc.build(story)
    return pdf_path


# ---------------------------
# PDF: GERENCIA
# ---------------------------
def _chart_pie_resultados(counts: Dict[str, int], w=180 * mm, h=75 * mm):
    labels = list(counts.keys())
    values = [counts[k] for k in labels]

    d = Drawing(w, h)
    pie = Pie()
    pie.x = 20
    pie.y = 8
    pie.width = 85 * mm
    pie.height = 65 * mm
    pie.data = values
    pie.labels = [f"{lab} ({val})" for lab, val in zip(labels, values)]
    pie.sideLabels = True
    pie.simpleLabels = False
    pie.slices.strokeWidth = 0.5
    d.add(pie)

    leg = Legend()
    leg.x = 115 * mm
    leg.y = 12
    leg.fontName = 'Helvetica'
    leg.fontSize = 8
    leg.colorNamePairs = [(pie.slices[i].fillColor, pie.labels[i]) for i in range(len(labels))]
    d.add(leg)

    d.add(String(0, h - 10, "Resultados", fontName="Helvetica-Bold", fontSize=10, fillColor=NAVY))
    return d


def _chart_bar(title: str, pairs: List[Tuple[str, int]], w=180 * mm, h=75 * mm, rotate_labels=True):
    if not pairs:
        pairs = [("Sin datos", 0)]
    labels = [p[0] for p in pairs]
    data = [[p[1] for p in pairs]]

    d = Drawing(w, h)
    d.add(String(0, h - 10, title, fontName="Helvetica-Bold", fontSize=10, fillColor=NAVY))

    bc = VerticalBarChart()
    bc.x = 10
    bc.y = 10
    bc.height = 55 * mm
    bc.width = 165 * mm
    bc.data = data
    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = max(1, max(data[0]))
    bc.valueAxis.valueStep = max(1, int(bc.valueAxis.valueMax / 4))

    bc.categoryAxis.categoryNames = labels
    if rotate_labels:
        bc.categoryAxis.labels.boxAnchor = 'ne'
        bc.categoryAxis.labels.angle = 30
        bc.categoryAxis.labels.dy = -2
        bc.categoryAxis.labels.dx = -2

    d.add(bc)

    vmax = max(1, bc.valueAxis.valueMax)
    slots = len(data[0])
    slot_w = (165 * mm) / max(1, slots)
    for i, v in enumerate(data[0]):
        x = 10 + (i * slot_w) + slot_w * 0.38
        y = 10 + (55 * mm) * (v / vmax) + 2
        d.add(String(x, y, str(v), fontName="Helvetica-Bold", fontSize=8, fillColor=colors.black))

    return d


def generate_gerencia_pdf(start: date, end: date, supervisor_name: str) -> str:
    pdf_path = os.path.join("data", "pdfs", f"INFORME_GERENCIA_{start}_{end}.pdf")

    reports = sheet_records("reports")
    report_items = sheet_records("report_items")

    # Filtrar por rango
    rows = []
    for r in reports:
        dte = str(r.get("created_date", "")).strip()
        if not dte:
            continue
        try:
            d_obj = date.fromisoformat(dte)
        except Exception:
            continue
        if start <= d_obj <= end:
            rows.append(r)

    total_informes = len(rows)
    operadores = len(set(str(r.get("operador_nombre","")) for r in rows)) if rows else 0
    equipos_con_envio = len(set(str(r.get("equipment_codigo","")) for r in rows)) if rows else 0
    total_equipos = len(EQUIPOS)
    equipos_sin_envio = total_equipos - equipos_con_envio

    res_counts = {"APTO": 0, "RESTRICCIONES": 0, "NO APTO": 0}
    falla_count = 0
    for r in rows:
        res = str(r.get("resultado_final","")).strip()
        if res in res_counts:
            res_counts[res] += 1
        if str(r.get("estado_general","")) in ("FALLA", "INOPERATIVO"):
            falla_count += 1

    eq_counts = {}
    for r in rows:
        code = str(r.get("equipment_codigo",""))
        eq_counts[code] = eq_counts.get(code, 0) + 1
    top_eq = sorted(eq_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    op_counts = {}
    for r in rows:
        op = str(r.get("operador_nombre",""))
        op_counts[op] = op_counts.get(op, 0) + 1
    top_op = sorted(op_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    day_counts = {}
    for r in rows:
        dte = str(r.get("created_date",""))
        day_counts[dte] = day_counts.get(dte, 0) + 1
    top_days = sorted(day_counts.items(), key=lambda x: x[0])[-14:]

    # Fotos dentro del rango
    report_ids_in_range = set(str(r.get("report_id","")) for r in rows)
    photo_rows = []
    for it in report_items:
        rid = str(it.get("report_id",""))
        if rid in report_ids_in_range and str(it.get("foto_path","")).strip():
            # buscar datos de reporte
            rep = next((x for x in rows if str(x.get("report_id","")) == rid), None)
            if rep:
                photo_rows.append({
                    "created_date": rep.get("created_date",""),
                    "equipment_codigo": rep.get("equipment_codigo",""),
                    "equipment_nombre": rep.get("equipment_nombre",""),
                    "seccion": it.get("seccion",""),
                    "item": it.get("item",""),
                    "foto_path": it.get("foto_path",""),
                })

    doc = SimpleDocTemplate(
        pdf_path, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm
    )
    story = []

    logo = _rl_img(LOGO_PATH, 35, 10)
    header_tbl = Table([[logo if logo else "", Paragraph("INFORME GERENCIA - CHECKLIST EQUIPOS", STYLE_TITLE)]],
                       colWidths=[45 * mm, 135 * mm])
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
        ("ALIGN", (1, 0), (1, 0), "CENTER"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    story.append(header_tbl)
    story.append(Paragraph(f"Rango: <b>{start}</b> a <b>{end}</b>  |  Supervisor: <b>{supervisor_name}</b>", STYLE_SMALL))
    story.append(Spacer(1, 4 * mm))

    kpi_data = [
        ["Informes", "Operadores", "Equipos (Total)", "Equipos con envío", "Equipos sin envío", "Fallas"],
        [str(total_informes), str(operadores), str(total_equipos), str(equipos_con_envio), str(equipos_sin_envio), str(falla_count)]
    ]
    kpi_tbl = Table(kpi_data, colWidths=[30 * mm, 30 * mm, 32 * mm, 32 * mm, 32 * mm, 28 * mm])
    kpi_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTSIZE", (0, 1), (-1, 1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    kpi_tbl._cellvalues[0] = [
        Paragraph("Informes", STYLE_CENTER_W),
        Paragraph("Operadores", STYLE_CENTER_W),
        Paragraph("Equipos (Total)", STYLE_CENTER_W),
        Paragraph("Equipos con envío", STYLE_CENTER_W),
        Paragraph("Equipos sin envío", STYLE_CENTER_W),
        Paragraph("Fallas", STYLE_CENTER_W),
    ]
    story.append(kpi_tbl)
    story.append(Spacer(1, 6 * mm))

    story.append(_chart_pie_resultados(res_counts))
    story.append(Spacer(1, 4 * mm))
    story.append(_chart_bar("Top equipos (envíos)", [(k, v) for k, v in top_eq]))
    story.append(PageBreak())

    story.append(header_tbl)
    story.append(Paragraph("Dashboard adicional", STYLE_H2))
    story.append(_chart_bar("Top operadores (envíos)", [(k, v) for k, v in top_op]))
    story.append(Spacer(1, 4 * mm))
    story.append(_chart_bar("Envíos por día (últimos 14)", [(k, v) for k, v in top_days]))
    story.append(Spacer(1, 6 * mm))

    story.append(Paragraph("Detalle de registros (rango)", STYLE_H2))
    reg_data = [[
        Paragraph("Fecha", STYLE_CENTER_W),
        Paragraph("Operador", STYLE_CENTER_W),
        Paragraph("Equipo", STYLE_CENTER_W),
        Paragraph("Código", STYLE_CENTER_W),
        Paragraph("Estado", STYLE_CENTER_W),
        Paragraph("Resultado", STYLE_CENTER_W),
    ]]
    # ordenar por fecha desc si se puede
    def _safe_date(x):
        try:
            return date.fromisoformat(str(x.get("created_date","")))
        except Exception:
            return date(1970,1,1)

    rows_sorted = sorted(rows, key=_safe_date, reverse=True)
    for r in rows_sorted:
        reg_data.append([
            Paragraph(str(r.get("created_date","")), STYLE_SMALL),
            Paragraph(str(r.get("operador_nombre","")), STYLE_SMALL),
            Paragraph(str(r.get("equipment_nombre","")), STYLE_SMALL),
            Paragraph(str(r.get("equipment_codigo","")), STYLE_SMALL),
            Paragraph(str(r.get("estado_general","")), STYLE_SMALL),
            Paragraph(str(r.get("resultado_final","")), STYLE_SMALL),
        ])

    reg_tbl = Table(reg_data, colWidths=[20 * mm, 35 * mm, 60 * mm, 18 * mm, 25 * mm, 25 * mm], repeatRows=1)
    reg_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(reg_tbl)

    if photo_rows:
        story.append(PageBreak())
        story.append(header_tbl)
        story.append(Paragraph("Fotos de fallas (rango seleccionado)", STYLE_H2))
        story.append(Paragraph("Evidencia adjunta (útil para compras/repuestos).", STYLE_SMALL))
        story.append(Spacer(1, 4 * mm))

        grid = []
        row = []
        for pr in photo_rows:
            cell_story = []
            cell_story.append(Paragraph(
                f"<b>{pr['equipment_nombre']} ({pr['equipment_codigo']})</b><br/>"
                f"{pr['created_date']}<br/>"
                f"{pr['seccion']}<br/><b>{pr['item']}</b>",
                STYLE_SMALL
            ))
            img = _rl_img(str(pr["foto_path"]), 80, 45)
            if img:
                cell_story.append(Spacer(1, 2 * mm))
                cell_story.append(img)
            else:
                cell_story.append(Paragraph("Foto no disponible", STYLE_SMALL))
            row.append(cell_story)

            if len(row) == 2:
                grid.append(row)
                row = []

            if len(grid) == 6:
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
                story.append(PageBreak())
                story.append(header_tbl)
                story.append(Paragraph("Fotos de fallas (continuación)", STYLE_H2))
                story.append(Spacer(1, 4 * mm))
                grid = []

        if row:
            row.append("")
            grid.append(row)
        if grid:
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

    doc.build(story)
    return pdf_path


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


def _bar_list(title: str, pairs: List[Tuple[str, int]], max_items=10):
    pairs = pairs[:max_items]
    if not pairs:
        st.info("Sin datos.")
        return

    maxv = max(v for _, v in pairs) if pairs else 1

    st.markdown(f"### {title}")
    for label, value in pairs:
        pct = 0 if maxv == 0 else int((value / maxv) * 100)
        html = f"""
        <div style="border:1px solid #e5e7eb;border-radius:10px;padding:10px;margin-bottom:10px;">
          <div style="display:flex;justify-content:space-between;font-size:12px;color:#111;">
            <div style="max-width:75%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{label}</div>
            <div style="font-weight:700;">{value}</div>
          </div>
          <div style="background:#f3f4f6;border-radius:10px;height:10px;margin-top:8px;overflow:hidden;">
            <div style="width:{pct}%;height:10px;background:#1f77b4;"></div>
          </div>
        </div>
        """
        st.markdown(html, unsafe_allow_html=True)


def supervisor_panel():
    st.subheader(f"🧑‍💼 Supervisor: {st.session_state.get('full_name','')}")
    tabs = st.tabs(["Usuarios", "Pendientes", "Panel de control", "Informe Gerencia (PDF)"])

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
        st.markdown("## Reportes pendientes (requiere firma supervisor)")
        pending = fetch_pending_reports()
        if not pending:
            st.info("No hay reportes pendientes.")
        else:
            options = {f"{p['equipment_nombre']} ({p['equipment_codigo']}) | {p['created_at']} | {p['resultado_final']}": p["id"] for p in pending}
            choice = st.selectbox("Selecciona un informe", list(options.keys()))
            rep_id = options[choice]

            rep, items = fetch_report_detail(rep_id)

            col1, col2 = st.columns(2)
            with col1:
                st.write(f"**Equipo:** {rep.get('equipment_nombre','')}")
                st.write(f"**Código:** {rep.get('equipment_codigo','')}")
                st.write(f"**Tipo:** {rep.get('equipment_tipo','')}")
                st.write(f"**Horómetro:** {rep.get('horometro_inicial','')}")
                st.write(f"**Operador:** {rep.get('operador_nombre','')}")
            with col2:
                st.write(f"**Fecha:** {rep.get('created_at','')}")
                st.write(f"**Resultado:** {rep.get('resultado_final','')}")
                st.write(f"**Estado:** {rep.get('estado_general','')}")
                st.write(f"**Obs:** {rep.get('observaciones_generales') or 'NINGUNA'}")

            st.dataframe(items, use_container_width=True, hide_index=True)

            st.markdown("### Supervisor (firma obligatoria)")
            supervisor_nombre = st.text_input("Nombre Supervisor", value=SUPERVISOR_NOMBRE_DEFAULT, key=f"sup_name_{rep_id}")

            sig = st_canvas(
                fill_color="rgba(255,255,255,0)",
                stroke_width=2,
                stroke_color="#000000",
                background_color="#FFFFFF",
                height=120,
                width=520,
                drawing_mode="freedraw",
                key=f"sig_sup_{rep_id}"
            )

            if st.button("✅ Aprobar y generar PDF final", key=f"ap_{rep_id}"):
                firma_path = save_signature_from_canvas(sig, "signatures", f"SUP_{rep_id}")
                if not firma_path:
                    st.error("Firma supervisor obligatoria.")
                else:
                    pdf_path = generate_checklist_pdf(rep_id)
                    pdf_folder_id = (st.secrets.get("DRIVE_PDFS_ID", "") or "").strip()
                    pdf_drive_url = upload_file_to_drive(pdf_path, pdf_folder_id)

                    approve_report(rep_id, st.session_state["user"], supervisor_nombre, firma_path, pdf_drive_url)

                    st.success("Aprobado. PDF generado.")
                    if pdf_drive_url:
                        st.markdown(f"📄 **PDF en Drive:** {pdf_drive_url}")

                    try:
                        with open(pdf_path, "rb") as f:
                            st.download_button(
                                "⬇️ Descargar PDF",
                                data=f.read(),
                                file_name=os.path.basename(pdf_path),
                                mime="application/pdf"
                            )
                    except Exception:
                        pass

    with tabs[2]:
        st.markdown("## Panel de control (Profesional)")

        rango = st.selectbox("Rango", ["Diario", "Semanal", "Mensual"], index=0, key="dash_rango")
        today = date.today()
        if rango == "Diario":
            start = today
        elif rango == "Semanal":
            start = today - timedelta(days=7)
        else:
            start = today - timedelta(days=30)

        rows = []
        reports = sheet_records("reports")
        for r in reports:
            dte = str(r.get("created_date","")).strip()
            try:
                d_obj = date.fromisoformat(dte)
            except Exception:
                continue
            if d_obj >= start:
                rows.append(r)

        total = len(rows)
        operadores = len(set(str(r.get("operador_nombre","")) for r in rows)) if rows else 0
        equipos_con_envio = len(set(str(r.get("equipment_codigo","")) for r in rows)) if rows else 0
        total_equipos = len(EQUIPOS)
        equipos_sin_envio = total_equipos - equipos_con_envio

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Informes", total)
        k2.metric("Operadores con envíos", operadores)
        k3.metric("Equipos con envíos", equipos_con_envio)
        k4.metric("Equipos sin envío", equipos_sin_envio)

        op_counts = {}
        for r in rows:
            op = str(r.get("operador_nombre",""))
            op_counts[op] = op_counts.get(op, 0) + 1
        top_op = sorted(op_counts.items(), key=lambda x: x[1], reverse=True)

        eq_counts = {}
        for r in rows:
            k = f"{r.get('equipment_nombre','')} ({r.get('equipment_codigo','')})"
            eq_counts[k] = eq_counts.get(k, 0) + 1
        top_eq = sorted(eq_counts.items(), key=lambda x: x[1], reverse=True)

        falla_counts = {}
        for r in rows:
            if str(r.get("estado_general","")) in ("FALLA", "INOPERATIVO"):
                k = f"{r.get('equipment_nombre','')} ({r.get('equipment_codigo','')})"
                falla_counts[k] = falla_counts.get(k, 0) + 1
        top_f = sorted(falla_counts.items(), key=lambda x: x[1], reverse=True)

        colA, colB = st.columns(2)
        with colA:
            _bar_list("Envíos por Operador (Top 10)", top_op, 10)
        with colB:
            _bar_list("Envíos por Equipo (Top 10)", top_eq, 10)

        _bar_list("Fallas por Equipo (Top 10)", top_f, 10)

        res_counts = {"APTO": 0, "RESTRICCIONES": 0, "NO APTO": 0}
        for r in rows:
            res = str(r.get("resultado_final","")).strip()
            if res in res_counts:
                res_counts[res] += 1

        st.markdown("### Resumen Resultados")
        st.write(
            f"✅ APTO: **{res_counts['APTO']}**  |  ⚠️ RESTRICCIONES: **{res_counts['RESTRICCIONES']}**  |  ⛔ NO APTO: **{res_counts['NO APTO']}**"
        )

    with tabs[3]:
        st.markdown("## Informe para Gerencia (PDF)")
        rango = st.selectbox("Tipo reporte", ["Diario", "Semanal", "Mensual"], key="ger_rango")

        today = date.today()
        if rango == "Diario":
            start = today
        elif rango == "Semanal":
            start = today - timedelta(days=7)
        else:
            start = today - timedelta(days=30)

        supervisor_nombre = st.text_input("Supervisor (para el PDF)", value=SUPERVISOR_NOMBRE_DEFAULT, key="ger_sup")

        if st.button("📄 Generar Informe Gerencia (PDF)", key="gen_ger"):
            pdf_path = generate_gerencia_pdf(start, today, supervisor_nombre)

            pdf_folder_id = (st.secrets.get("DRIVE_PDFS_ID", "") or "").strip()
            pdf_drive_url = upload_file_to_drive(pdf_path, pdf_folder_id)

            st.success("Informe gerencia generado.")
            if pdf_drive_url:
                st.markdown(f"📄 **Informe en Drive:** {pdf_drive_url}")

            with open(pdf_path, "rb") as f:
                st.download_button(
                    "⬇️ Descargar Informe Gerencia (PDF)",
                    data=f.read(),
                    file_name=os.path.basename(pdf_path),
                    mime="application/pdf"
                )


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
    st.info("Selecciona equipo → completa checklist → firma → enviar (queda PENDIENTE hasta firma del supervisor).")

    eq_label_map = {f"{e['nombre']}": e for e in EQUIPOS}

    def _on_equipo_change():
        _reset_operator_checklist_state()
        st.session_state["op_prev_sel"] = st.session_state.get("op_eq_select")
        st.rerun()

    sel = st.selectbox(
        "Equipo",
        list(eq_label_map.keys()),
        key="op_eq_select",
        on_change=_on_equipo_change
    )
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
                estado = st.selectbox(
                    "Estado",
                    STATUS_OPCIONES,
                    key=f"{eq['codigo']}::{seccion}::{item}::estado"
                )
            with c3:
                obs = st.text_input(
                    "Observación (si aplica)",
                    key=f"{eq['codigo']}::{seccion}::{item}::obs"
                )

            foto_path = ""
            if estado in ("OPERATIVO CON FALLA", "INOPERATIVO"):
                up = st.file_uploader(
                    f"Foto obligatoria: {item}",
                    type=["png", "jpg", "jpeg", "webp"],
                    key=f"{eq['codigo']}::{seccion}::{item}::foto"
                )
                if up:
                    foto_path = save_uploaded_image(up, "photos", f"{eq['codigo']}_{item}".replace(" ", "_")[:40])

            estados_all.append(estado)
            items_payload.append({
                "seccion": seccion,
                "item": item,
                "estado": estado,
                "observacion": obs.strip(),
                "foto_path": foto_path
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

    if st.button("📨 Enviar reporte (queda PENDIENTE)", key=f"send_{eq['codigo']}"):
        firma_path = save_signature_from_canvas(sig, "signatures", f"OP_{eq['codigo']}")
        if not firma_path:
            st.error("La firma del operador es obligatoria.")
            return

        for it in items_payload:
            if it["estado"] in ("OPERATIVO CON FALLA", "INOPERATIVO") and not it.get("foto_path"):
                st.error(f"Falta foto obligatoria en: {it['item']}")
                return

        payload = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "created_date": date.today().isoformat(),
            "equipment_tipo": eq["tipo"],
            "equipment_codigo": eq["codigo"],
            "equipment_nombre": eq["nombre"],
            "horometro": int(horometro),
            "operador_user": st.session_state["user"],
            "operador_nombre": st.session_state.get("full_name", ""),
            "obs_general": obs_general.strip(),
            "estado_general": estado_general,
            "resultado_final": resultado_final,
            "firma_operador_path": firma_path,
            "items": items_payload
        }

        report_id = insert_report(payload)
        st.success(f"✅ Reporte enviado. ID: {report_id} (pendiente firma supervisor).")


def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")

    # Diagnóstico siempre visible
    debug_google()

    # No reventar la UI por schema
    try:
        init_db()
    except Exception as e:
        st.error("Error inicializando base (Sheets). La app seguirá, pero revisa esto:")
        st.code(str(e))

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
