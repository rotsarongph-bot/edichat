import os
import uuid
import sqlite3
from datetime import datetime
from io import BytesIO
import csv

import dotenv
import google.generativeai as genai
import pandas as pd
import streamlit as st
from google.generativeai.types import HarmCategory, HarmBlockThreshold

from prompt import PROMPT_WORKAW

dotenv.load_dotenv()

# =========================
# AVATAR (แก้ตรงนี้จุดเดียว)
# =========================
BOT_AVATAR = "🤖"
USER_AVATAR = "🧑"

# =========================
# 0) CONFIG
# =========================
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    st.error("❌ ไม่พบ GOOGLE_API_KEY (ตรวจ .env หรือ Environment Variables)")
    st.stop()

genai.configure(api_key=GOOGLE_API_KEY)

generation_config = {
    "temperature": 0.1,
    "top_p": 0.95,
    "top_k": 64,
    "max_output_tokens": 2048,
    "response_mime_type": "text/plain",
}

SAFETY_SETTINGS = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
}

model = genai.GenerativeModel(
    model_name="gemini-2.5-flash",
    safety_settings=SAFETY_SETTINGS,
    generation_config=generation_config,
    system_instruction=PROMPT_WORKAW,
)

# =========================
# 1) SQLITE: เก็บประวัติแชท
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
    """โหลดทั้ง user+model (ใช้ตอนส่งเข้าโมเดล หรือ export)"""
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


def db_load_user_messages(session_id: str, limit: int = 300):
    """โหลดเฉพาะ role='user' (ใช้แสดงในหน้าประวัติ)"""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT role, content, ts
            FROM messages
            WHERE session_id=? AND role='user'
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


def db_list_sessions(limit: int = 50):
    """
    คืนค่าเป็นรายการ session พร้อม:
    - last_ts
    - user_turns / model_turns (คงไว้ใน DB แต่ใน UI จะโชว์ user อย่างเดียว)
    - first_question (คำถามแรกของ session)
    """
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
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
            ORDER BY s.last_ts DESC
            LIMIT ?
            """,
            (limit,),
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
    """
    แก้ปัญหา “CSV ดูไม่ครบ” ใน Excel/Sheets:
    - content มี , หรือขึ้นบรรทัดใหม่ -> ถ้าไม่ quote ทั้งหมด จะทำให้แถวแตก/เหมือนข้อมูลหาย
    - ใช้ QUOTE_ALL + utf-8-sig
    """
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
# 2) STREAMLIT UI
# =========================
st.set_page_config(page_title="EDI Chatbot", page_icon="💬")
st.title("💬 น้องนวัตกรรม สวัสดีค่ะ")

# สร้าง session_id (ผูกกับผู้ใช้ในเบราว์เซอร์นี้)
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
session_id = st.session_state.session_id

# =========================
# 3) LOAD KB (Excel) แบบเดิม
# =========================
file_path = r"workaw_data.xlsx"
try:
    df_kb = pd.read_excel(file_path)
    file_content = df_kb.to_string(index=False)
except Exception as e:
    st.error(f"Error reading file: {e}")
    st.stop()

# =========================
# Sidebar: ควบคุมและดูประวัติ
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

    # เลือก session เพื่อดูย้อนหลัง (โชว์คำถามแรกของแต่ละ session + โชว์ count เฉพาะ user)
    sessions = db_list_sessions(limit=50)
    chosen_sid = None
    if sessions:
        st.write("**Recent sessions**")

        # ✅ ปรับ label ให้โชว์ "user:{ut}" อย่างเดียว (ไม่โชว์ model)
        session_choices = [
            f"📝 {shorten(first_q)} | {last_ts} | user:{ut} | {sid[:8]}…"
            for sid, first_ts, last_ts, ut, mt, first_q in sessions
        ]
        chosen = st.selectbox("เลือก session เพื่อดูประวัติ", ["(current)"] + session_choices)

        if chosen != "(current)":
            sid_prefix = chosen.split("|")[-1].strip().replace("…", "")
            for sid, *_ in sessions:
                if sid.startswith(sid_prefix):
                    chosen_sid = sid
                    break

            if chosen_sid and st.button("👁️ เปิดดู session ที่เลือก"):
                st.session_state["view_session_id"] = chosen_sid
                st.rerun()
    else:
        st.info("ยังไม่มีประวัติในฐานข้อมูล")

    st.markdown("---")

    # ✅ Export CSV แบบเลือกได้ (ยัง export ทั้ง user+model ตาม DB เหมือนเดิม)
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

    else:  # Only selected session
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

    # Quick stats (จาก ALL)
    st.write("**Quick stats (All)**")
    all_df = db_export_all_as_df()
    st.write(f"- Total messages: {len(all_df)}")
    st.write(f"- Total sessions: {all_df['session_id'].nunique() if len(all_df) else 0}")
    if len(all_df):
        st.write(all_df["role"].value_counts())

# =========================
# 4) เลือกว่าจะ “แสดงแชท” จาก session ไหน
# =========================
view_session_id = st.session_state.get("view_session_id", session_id)

# ✅ โหลดสำหรับ "ประวัติ" ให้เป็น user-only
loaded_user_only = db_load_user_messages(view_session_id, limit=5000)

# สร้าง session_state['messages'] สำหรับ session ปัจจุบัน
if "messages" not in st.session_state:
    st.session_state["messages"] = [
        {"role": "model", "content": "สวัสดีค่ะ ต้องการสอบถามข้อมูลการรับสมัครส่วนไหนคะ"}
    ]
    db_add_message(session_id, "model", "สวัสดีค่ะ ต้องการสอบถามข้อมูลการรับสมัครส่วนไหนคะ")

# =========================
# 5) UI: โหมดดูประวัติ / โหมดคุย
# =========================
if view_session_id != session_id:
    st.info(f"กำลังดูประวัติ (แสดงเฉพาะข้อความผู้ใช้) session: {view_session_id}")

    if st.button("⬅️ กลับไปคุย (current session)"):
        st.session_state["view_session_id"] = session_id
        st.rerun()

    # ✅ แสดงเฉพาะ user
    if not loaded_user_only:
        st.warning("Session นี้ยังไม่มีข้อความจากผู้ใช้ (user)")
    else:
        for msg in loaded_user_only:
            st.chat_message("user", avatar=USER_AVATAR).write(msg["content"])

    st.stop()

# =========================
# 6) แสดงแชทของ current session (คุยจริง: แสดงทั้ง user+model)
# =========================
for msg in st.session_state["messages"]:
    if msg["role"] == "model":
        st.chat_message("model", avatar=BOT_AVATAR).write(msg["content"])
    else:
        st.chat_message("user", avatar=USER_AVATAR).write(msg["content"])

# =========================
# 7) ส่งข้อความ + บันทึกลง DB
# =========================
if prompt := st.chat_input():
    # เพิ่มลง session_state
    st.session_state["messages"].append({"role": "user", "content": prompt})
    st.chat_message("user", avatar=USER_AVATAR).write(prompt)

    # บันทึกลง SQLite (ยังเก็บเหมือนเดิม)
    db_add_message(session_id, "user", prompt)

    def generate_response():
        history = [
            {"role": msg["role"], "parts": [{"text": msg["content"]}]}
            for msg in st.session_state["messages"]
        ]

        if prompt.lower().startswith("add") or prompt.lower().endswith("add"):
            reply = "ขอบคุณสำหรับคำแนะนำค่ะ"
            st.chat_message("model", avatar=BOT_AVATAR).write(reply)
            st.session_state["messages"].append({"role": "model", "content": reply})
            db_add_message(session_id, "model", reply)
        else:
            # ✅ ใส่ไฟล์ฐานความรู้เหมือนเดิม (เวอร์ชันที่ตอบดีที่สุด)
            history.insert(1, {"role": "user", "parts": [{"text": file_content}]})

            chat_session = model.start_chat(history=history)
            response = chat_session.send_message(prompt)

            st.session_state["messages"].append({"role": "model", "content": response.text})
            st.chat_message("model", avatar=BOT_AVATAR).write(response.text)

            # บันทึกคำตอบลง SQLite (ยังเก็บเหมือนเดิม)
            db_add_message(session_id, "model", response.text)

    generate_response()
