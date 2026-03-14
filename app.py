import os
import uuid
import sqlite3
import csv
from datetime import datetime
from io import BytesIO

import dotenv
from openai import OpenAI
import pandas as pd
import streamlit as st

# สมมติว่าในไฟล์ prompt.py ของคุณมีตัวแปร PROMPT_WORKAW
from prompt import PROMPT_WORKAW

# =========================
# 0) STREAMLIT CONFIG (ต้องอยู่บนสุด)
# =========================
st.set_page_config(page_title="EDI Chatbot", page_icon="💬")

dotenv.load_dotenv(override=True)

# =========================
# AVATAR
# =========================
BOT_AVATAR = "🤖"
USER_AVATAR = "🧑"

# =========================
# 1) CONFIG OPENAI
# =========================
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
if not OPENROUTER_API_KEY:
    st.error("❌ ไม่พบ OPENROUTER_API_KEY (ตรวจ .env หรือ Environment Variables)")
    st.stop()

# สร้าง Client แบบไม่มี proxies (เพื่อแก้ Error TypeError: Client.__init__() got an unexpected keyword argument 'proxies')
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# =========================
# 2) SQLITE
# =========================
DB_PATH = "workaw_chat.db"

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,         -- 'user' | 'model'
                content TEXT NOT NULL,
                ts TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_ts ON messages(session_id, ts)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id, id)")
        conn.commit()

def db_add_message(session_id: str, role: str, content: str):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages (session_id, role, content, ts) VALUES (?, ?, ?, ?)",
            (session_id, role, content, datetime.utcnow().isoformat()),
        )
        conn.commit()

def db_load_messages(session_id: str, limit: int = 300):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT role, content, ts
            FROM messages
            WHERE session_id=?
            ORDER BY id ASC
            LIMIT ?
            """,
            (session_id, limit),
        )
        rows = cur.fetchall()
    return [{"role": r, "content": c, "ts": t} for r, c, t in rows]

def db_clear_session(session_id: str):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        conn.commit()

def db_list_sessions(limit: int = 50, user_turns_eq: int | None = None):
    where_clause = ""
    params = []
    if user_turns_eq is not None:
        where_clause = "WHERE s.user_turns = ?"
        params.append(user_turns_eq)

    params.append(limit)

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            WITH sessions AS (
                SELECT
                    session_id,
                    MIN(ts) AS first_ts,
                    MAX(ts) AS last_ts,
                    SUM(CASE WHEN role='user' THEN 1 ELSE 0 END) AS user_turns,
                    SUM(CASE WHEN role='model' THEN 1 ELSE 0 END) AS model_turns
                FROM messages
                GROUP BY session_id
            ),
            first_user_msg AS (
                SELECT m.session_id, m.content AS first_question
                FROM messages m
                JOIN (
                    SELECT session_id, MIN(id) AS first_user_id
                    FROM messages
                    WHERE role='user'
                    GROUP BY session_id
                ) x
                ON m.session_id = x.session_id AND m.id = x.first_user_id
            )
            SELECT
                s.session_id,
                s.first_ts,
                s.last_ts,
                s.user_turns,
                s.model_turns,
                COALESCE(f.first_question, '') AS first_question
            FROM sessions s
            LEFT JOIN first_user_msg f ON s.session_id = f.session_id
            {where_clause}
            ORDER BY s.last_ts DESC
            LIMIT ?
            """,
            tuple(params),
        )
        rows = cur.fetchall()
    return rows

def db_export_all_as_df():
    with db_conn() as conn:
        df = pd.read_sql_query(
            "SELECT id, session_id, role, content, ts FROM messages ORDER BY id ASC",
            conn,
        )
    return df

def db_export_session_as_df(session_id: str):
    with db_conn() as conn:
        df = pd.read_sql_query(
            "SELECT id, session_id, role, content, ts FROM messages WHERE session_id=? ORDER BY id ASC",
            conn,
            params=(session_id,),
        )
    return df

def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    df.to_csv(
        buf,
        index=False,
        encoding="utf-8-sig",
        quoting=csv.QUOTE_ALL,
        lineterminator="\n",
    )
    return buf.getvalue()

def shorten(text: str, n: int = 42) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[:n] + "…"

init_db()

# =========================
# 3) INITIALIZE APP STATE
# =========================
st.title("💬 น้องนวัตกรรม สวัสดีค่ะ")

if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
session_id = st.session_state.session_id

# =========================
# 4) LOAD KB (Excel)
# =========================
file_path = r"workaw_data.xlsx"
try:
    df_kb = pd.read_excel(file_path)
    file_content = df_kb.to_string(index=False)
except Exception as e:
    st.error(f"Error reading file: {e}")
    st.stop()

# =========================
# 5) SIDEBAR
# =========================
with st.sidebar:
    st.subheader("🧾 Chat History (SQLite)")
    st.caption(f"Current session: {session_id[:8]}…")

    if st.button("Clear History (this session)"):
        db_clear_session(session_id)
        st.session_state["messages"] = [
            {"role": "model", "content": "สวัสดีค่ะ ต้องการสอบถามข้อมูลการรับสมัครส่วนไหนคะ"}
        ]
        db_add_message(session_id, "model", "สวัสดีค่ะ ต้องการสอบถามข้อมูลการรับสมัครส่วนไหนคะ")
        st.session_state["view_session_id"] = session_id
        st.rerun()

    st.markdown("---")

    sessions_for_select = db_list_sessions(limit=200, user_turns_eq=1)
    chosen_sid = None
    
    if sessions_for_select:
        st.write("**Recent sessions**")
        session_choices = [
            f"📝 {shorten(first_q)} | {last_ts} | user:{ut} | {sid[:8]}…"
            for sid, first_ts, last_ts, ut, mt, first_q in sessions_for_select
        ]
        chosen = st.selectbox("เลือก session เพื่อดูประวัติ", ["(current)"] + session_choices)

        if chosen != "(current)":
            sid_prefix = chosen.split("|")[-1].strip().replace("…", "")
            for sid, *_ in sessions_for_select:
                if sid.startswith(sid_prefix):
                    chosen_sid = sid
                    break

            if chosen_sid and st.button("👁️ เปิดดู session ที่เลือก"):
                st.session_state["view_session_id"] = chosen_sid
                st.rerun()
    else:
        st.info("ยังไม่มี session ที่ user >= 1")

    st.markdown("---")

    st.write("**Export CSV**")
    export_mode = st.radio(
        "ต้องการ export อะไร?",
        ["All sessions", "Only current session", "Only selected session"],
        index=0,
    )

    if export_mode == "All sessions":
        export_df = db_export_all_as_df()
        st.download_button(
            "⬇️ Download CSV (All sessions)",
            data=df_to_csv_bytes(export_df),
            file_name="workaw_chat_history_ALL.csv",
            mime="text/csv",
        )
        st.caption(f"Rows: {len(export_df)} | Sessions: {export_df['session_id'].nunique() if len(export_df) else 0}")

    elif export_mode == "Only current session":
        export_df = db_export_session_as_df(session_id)
        st.download_button(
            "⬇️ Download CSV (Current session)",
            data=df_to_csv_bytes(export_df),
            file_name=f"workaw_chat_{session_id[:8]}_CURRENT.csv",
            mime="text/csv",
        )
        st.caption(f"Rows: {len(export_df)}")

    else:
        if chosen_sid:
            export_df = db_export_session_as_df(chosen_sid)
            st.download_button(
                "⬇️ Download CSV (Selected session)",
                data=df_to_csv_bytes(export_df),
                file_name=f"workaw_chat_{chosen_sid[:8]}_SELECTED.csv",
                mime="text/csv",
            )
            st.caption(f"Rows: {len(export_df)}")
        else:
            st.warning("ยังไม่ได้เลือก session (เลือกจาก Recent sessions ก่อน)")

    st.markdown("---")

    st.write("**Quick stats (All)**")
    all_df = db_export_all_as_df()
    st.write(f"- Total messages: {len(all_df)}")
    st.write(f"- Total sessions: {all_df['session_id'].nunique() if len(all_df) else 0}")
    if len(all_df):
        st.write(all_df["role"].value_counts())

# =========================
# 6) VIEW SESSION (history)
# =========================
view_session_id = st.session_state.get("view_session_id", session_id)
loaded = db_load_messages(view_session_id, limit=5000)

if "messages" not in st.session_state:
    st.session_state["messages"] = [
        {"role": "model", "content": "สวัสดีค่ะ ต้องการสอบถามข้อมูลการรับสมัครส่วนไหนคะ"}
    ]
    db_add_message(session_id, "model", "สวัสดีค่ะ ต้องการสอบถามข้อมูลการรับสมัครส่วนไหนคะ")

if view_session_id != session_id:
    st.info(f"กำลังดูประวัติ session: {view_session_id}")
    if st.button("⬅️ กลับไปคุย (current session)"):
        st.session_state["view_session_id"] = session_id
        st.rerun()

    for msg in loaded:
        if msg["role"] == "model":
            st.chat_message("model", avatar=BOT_AVATAR).write(msg["content"])
        else:
            st.chat_message("user", avatar=USER_AVATAR).write(msg["content"])
    st.stop()

# =========================
# 7) CHAT UI (Current session)
# =========================
for msg in st.session_state["messages"]:
    if msg["role"] == "model":
        st.chat_message("model", avatar=BOT_AVATAR).write(msg["content"])
    else:
        st.chat_message("user", avatar=USER_AVATAR).write(msg["content"])

# =========================
# 8) CHAT INPUT + GENERATE
# =========================
if prompt := st.chat_input():
    st.session_state["messages"].append({"role": "user", "content": prompt})
    st.chat_message("user", avatar=USER_AVATAR).write(prompt)
    db_add_message(session_id, "user", prompt)

    if prompt.lower().startswith("add") or prompt.lower().endswith("add"):
        reply_text = "ขอบคุณสำหรับคำแนะนำค่ะ"
    else:
        messages = [{"role": "system", "content": f"{PROMPT_WORKAW}\n\n[ข้อมูลอ้างอิง]\n{file_content}"}]
        
        for msg in st.session_state["messages"]:
            role = "assistant" if msg["role"] == "model" else "user"
            messages.append({"role": role, "content": msg["content"]})
        
        response = client.chat.completions.create(
            model="google/gemini-2.5-flash",
            messages=messages,
            temperature=0.1,
            top_p=0.95,
            max_tokens=2048,
        )
        reply_text = response.choices[0].message.content

    st.session_state["messages"].append({"role": "model", "content": reply_text})
    st.chat_message("model", avatar=BOT_AVATAR).write(reply_text)
    db_add_message(session_id, "model", reply_text)
