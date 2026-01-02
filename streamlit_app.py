import os
import json
from datetime import date, datetime
import pandas as pd
import streamlit as st

# ============ LLM (OpenAI) ============
from openai import OpenAI

# ============ SQL Server ============
import pyodbc

st.set_page_config(page_title="Canave IA (Operación)", layout="wide")
st.title("Canave IA – Consultas Operativas (solo lectura)")

# -------------------------------
# 1) Secrets / configuración
# -------------------------------
def get_secret(key: str, default: str = "") -> str:
    # Primero intenta st.secrets (Streamlit Cloud / secrets.toml), si no, env vars
    if "secrets" in dir(st) and key in st.secrets:
        return str(st.secrets[key])
    return os.getenv(key, default)

OPENAI_API_KEY = get_secret("OPENAI_API_KEY")
OPENAI_MODEL = get_secret("OPENAI_MODEL", "gpt-4.1-mini")

SQL_SERVER = get_secret("SQL_SERVER")
SQL_DATABASE = get_secret("SQL_DATABASE")
SQL_USER = get_secret("SQL_USER")
SQL_PASSWORD = get_secret("SQL_PASSWORD")
SQL_DRIVER = get_secret("SQL_DRIVER", "ODBC Driver 18 for SQL Server")

if not OPENAI_API_KEY:
    st.warning("Falta OPENAI_API_KEY en secrets/env. Configúralo para habilitar el chat IA.")

# -------------------------------
# 2) Conexión SQL (solo lectura)
# -------------------------------
@st.cache_resource
def get_conn():
    if not all([SQL_SERVER, SQL_DATABASE, SQL_USER, SQL_PASSWORD]):
        raise RuntimeError("Faltan credenciales SQL en secrets/env (SQL_SERVER, SQL_DATABASE, SQL_USER, SQL_PASSWORD).")

    conn_str = (
        f"DRIVER={{{SQL_DRIVER}}};"
        f"SERVER={SQL_SERVER};"
        f"DATABASE={SQL_DATABASE};"
        f"UID={SQL_USER};"
        f"PWD={SQL_PASSWORD};"
        "Encrypt=yes;"
        "TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str, timeout=5)

def run_query(sql: str, params: list | None = None) -> pd.DataFrame:
    # Solo SELECT (guardrail básico)
    sql_strip = sql.strip().lower()
    if not sql_strip.startswith("select") and not sql_strip.startswith("exec"):
        raise ValueError("Solo se permiten consultas SELECT o EXEC de SPs controlados.")
    with get_conn() as conn:
        return pd.read_sql(sql, conn, params=params or [])

# -------------------------------
# 3) Herramientas permitidas (Tools)
#    Aquí NO hay SQL libre. Todo está controlado.
# -------------------------------
def ventas_ayer(sucursal: str) -> list[dict]:
    """
    Ajusta esta consulta a tu capa BI (ideal) o a tus tablas reales.
    Recomendación: usar una vista agregada por día/sucursal.
    """
    sql = """
    SELECT
        CAST(DATEADD(day, -1, GETDATE()) AS date) AS Fecha,
        ? AS Sucursal,
        0.0 AS VentaNeta,
        0 AS Tickets
    """
    # Placeholder seguro: devuelve 0 hasta que conectemos a la fuente real
    df = run_query(sql, [sucursal])
    return df.to_dict(orient="records")

def top_productos_mes(anio: int, mes: int, top_n: int, sucursal: str | None = None) -> list[dict]:
    """
    Ajusta esta consulta a tu capa BI o tablas reales.
    """
    # Placeholder: sin datos reales todavía
    df = pd.DataFrame([{
        "Anio": anio,
        "Mes": mes,
        "Sucursal": sucursal or "TODAS",
        "ArticuloId": "",
        "Articulo": "Pendiente de conectar a datos reales",
        "Unidades": 0,
        "VentaNeta": 0.0
    }]).head(top_n)
    return df.to_dict(orient="records")

# -------------------------------
# 4) Definición de Tools para el modelo
# -------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ventas_ayer",
            "description": "Obtiene ventas de ayer para una sucursal (solo lectura).",
            "parameters": {
                "type": "object",
                "properties": {"sucursal": {"type": "string"}},
                "required": ["sucursal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "top_productos_mes",
            "description": "Obtiene Top N productos del mes (todas las sucursales o una sucursal).",
            "parameters": {
                "type": "object",
                "properties": {
                    "anio": {"type": "integer"},
                    "mes": {"type": "integer"},
                    "top_n": {"type": "integer"},
                    "sucursal": {"type": ["string", "null"]},
                },
                "required": ["anio", "mes", "top_n"],
            },
        },
    },
]

def call_tool(name: str, args: dict):
    if name == "ventas_ayer":
        return ventas_ayer(**args)
    if name == "top_productos_mes":
        return top_productos_mes(**args)
    return {"error": f"Tool no permitida: {name}"}

# -------------------------------
# 5) Chat UI
# -------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "system",
            "content": (
                "Eres un asistente interno para consultas operativas. "
                "No inventes cifras. Si necesitas datos, usa tools. "
                "Si el usuario pide algo fuera de las tools, explica qué dato falta."
            )
        }
    ]

for m in st.session_state.messages:
    if m["role"] in ("user", "assistant"):
        with st.chat_message(m["role"]):
            st.write(m["content"])

prompt = st.chat_input("Ej: ¿Cuánto vendió ayer Tamazula 1? | Top 10 productos del mes 2026-01")
if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.write(prompt)

    if not OPENAI_API_KEY:
        st.session_state.messages.append({
            "role": "assistant",
            "content": "Falta configurar OPENAI_API_KEY. En cuanto lo configures, habilito las respuestas IA."
        })
        st.rerun()

    client = OpenAI(api_key=OPENAI_API_KEY)

    # Primera llamada: el modelo decide si llama tools
    r1 = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=st.session_state.messages,
        tools=TOOLS,
        tool_choice="auto",
    )
    msg = r1.choices[0].message

    # Si pidió tools, ejecutarlas y luego segunda llamada para redactar
    if msg.tool_calls:
        for tc in msg.tool_calls:
            tool_name = tc.function.name
            tool_args = json.loads(tc.function.arguments)
            try:
                data = call_tool(tool_name, tool_args)
            except Exception as e:
                data = {"error": str(e)}

            st.session_state.messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(data, ensure_ascii=False)
            })

        r2 = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=st.session_state.messages,
        )
        final_text = r2.choices[0].message.content or ""

        st.session_state.messages.append({"role": "assistant", "content": final_text})
        with st.chat_message("assistant"):
            st.write(final_text)

    else:
        # Respuesta sin tools
        final_text = msg.content or ""
        st.session_state.messages.append({"role": "assistant", "content": final_text})
        with st.chat_message("assistant"):
            st.write(final_text)

st.divider()
st.caption("Estado: Esta versión es un esqueleto seguro. Falta conectar consultas reales (vistas BI / SPs).")
