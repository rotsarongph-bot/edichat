import os
import uuid
import sqlite3
from datetime import datetime
from io import BytesIO
import csv

import dotenv
from openai import OpenAI
import pandas as pd
import streamlit as st

dotenv.load_dotenv()

# =========================
# AVATAR & UI STYLING
# =========================
BOT_AVATAR = "🤖"
USER_AVATAR = "🧑"

st.set_page_config(page_title="EDI Chatbot", page_icon="💬", layout="wide")

# CSS สำหรับปรับความสวยงามและขยายขนาด Avatar
st.markdown("""
<style>
    /* ขยายขนาดกล่อง Avatar */
    [data-testid="stChatMessageAvatar"] {
        width: 3.5rem !important;
        height: 3.5rem !important;
        background-color: #f0f2f6;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
    }
    /* ขยายขนาด Icon/Emoji ข้างใน */
    [data-testid="stChatMessageAvatar"] div {
        font-size: 2.2rem !important;
    }
    /* ปรับแต่งกล่องข้อความให้สวยขึ้น */
    .stChatMessage {
        padding: 1rem;
        border-radius: 15px;
        margin-bottom: 10px;
    }
    /* ปรับสีพื้นหลังข้อความของ User */
    [data-testid="stChatMessage"]:nth-child(odd) {
        background-color: #f9f9f9;
    }
</style>
""", unsafe_allow_html=True)


# =========================
# 0) CONFIG OPENAI
# =========================
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
if not OPENROUTER_API_KEY:
    st.error("❌ ไม่พบ OPENROUTER_API_KEY (ตรวจ .env หรือ Environment Variables)")
    st.stop()

# สร้าง Client แบบไม่มี proxies
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# =========================
# 1) LOAD KB (Excel)
# =========================
file_path = r"workaw_data.xlsx"
try:
    df_kb = pd.read_excel(file_path)
    file_content = df_kb.to_string(index=False)
except Exception as e:
    st.error(f"❌ ไม่สามารถอ่านไฟล์ Excel ได้: {e}")
    st.stop()

# =========================
# 2) PROMPT ENGINEERING (Strict RAG & Concise)
# =========================
# รวม Prompt และเนื้อหา Excel เข้าไว้ใน System Instruction
SYSTEM_INSTRUCTION = f"""
คุณคือผู้ช่วย AI ชื่อ 'น้องนวัตกรรม' ให้บริการตอบคำถามเกี่ยวกับการรับสมัคร/ข้อมูลคณะศึกษาศาสตร์และนวัตกรรมการศึกษา
คำสั่งสำคัญที่สุดของคุณคือต้องปฏิบัติตามกฎด้านล่างอย่างเคร่งครัด:

1. ให้ตอบคำถามโดยอ้างอิงจาก [ข้อมูลอ้างอิง (Context)] ด้านล่างนี้เท่านั้น
2. ห้ามคิดคำตอบเอง ห้ามคาดเดา และห้ามใช้ความรู้ภายนอกเด็ดขาด
3. หากผู้ใช้ถามเรื่องที่ไม่มีใน [ข้อมูลอ้างอิง (Context)] ให้ตอบว่า "ขออภัยค่ะ ไม่มีข้อมูลนี้ในระบบค่ะ" 
4. หลังจากแจ้งว่าไม่มีข้อมูล ให้เสนอแนะหัวข้อหรือแนวทางคำถามที่มีใน Context เพื่อช่วยเหลือผู้ใช้ เช่น "คุณสามารถสอบถามเกี่ยวกับตำแหน่งที่เปิดรับ, คุณสมบัติ หรือสวัสดิการได้นะคะ"
5. ตอบกลับด้วยภาษาไทย สุภาพ เป็นทางการแต่น่ารัก และลงท้ายด้วย ค่ะ/คะ เสมอ
6. สำคัญมาก: ให้คำตอบ "สั้น กระชับ และตรงประเด็นที่สุด" ไม่ต้องอธิบายยืดเยื้อหรือเกริ่นนำยาว เพื่อความรวดเร็วและประหยัด Token

[ข้อมูลอ้างอิง (Context)]
{file_content}
"""

# ใช้งานผ่าน OpenRouter Chat Completions


# =========================
# 3) SQLITE (Database)
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

def db_get_latest_session_id():
    """ดึง session_id ล่าสุดที่มีการใช้งาน เพื่อให้แชทไม่หายเมื่อรีเฟรชเว็บ"""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT session_id FROM messages ORDER BY ts DESC LIMIT 1")
        row = cur.fetchone()
    return row[0] if row else None

def db_clear_session(session_id: str):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        conn.commit()

def db_list_sessions(limit: int = 50, min_user_turns: int = 1):
    # แก้ไขเงื่อนไขให้ดึงทุก session ที่มีการคุยของ user ตั้งแต่ 1 ครั้งขึ้นไป (ป้องกันประวัติหายเมื่อคุยยาวขึ้น)
    where_clause = "WHERE s.user_turns >= ?"
    params = [min_user_turns, limit]

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
        df = pd.read_sql_query("SELECT id, session_id, role, content, ts FROM messages ORDER BY id ASC", conn)
    return df

def db_export_session_as_df(session_id: str):
    with db_conn() as conn:
        df = pd.read_sql_query(
            "SELECT id, session_id, role, content, ts FROM messages WHERE session_id=? ORDER BY id ASC",
            conn, params=(session_id,)
        )
    return df

def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_ALL, lineterminator="\n")
    return buf.getvalue()

def shorten(text: str, n: int = 42) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[:n] + "…"

init_db()

# =========================
# 4) INITIALIZE SESSION
# =========================
st.title("💬 น้องนวัตกรรม สวัสดีค่ะ")
st.markdown("ยินดีต้อนรับ! สามารถสอบถามข้อมูลการรับสมัคร / ข้อมูลคณะศึกษาศาสตร์และนวัตกรรมการศึกษา ได้เลยค่ะ")

# ดึง Session ล่าสุดเสมอ เพื่อไม่ให้ประวัติหายเมื่อรีเฟรช
if "session_id" not in st.session_state:
    latest_sid = db_get_latest_session_id()
    if latest_sid:
        st.session_state.session_id = latest_sid
    else:
        st.session_state.session_id = str(uuid.uuid4())

session_id = st.session_state.session_id

# โหลดข้อความของ session ปัจจุบัน
if "messages" not in st.session_state:
    loaded_messages = db_load_messages(session_id)
    if loaded_messages:
        # มีประวัติอยู่แล้ว โหลดมาแสดง
        st.session_state["messages"] = [{"role": msg["role"], "content": msg["content"]} for msg in loaded_messages]
    else:
        # ไม่มีประวัติ เริ่มต้นคำทักทายใหม่
        initial_msg = "สวัสดีค่ะ น้องนวัตกรรมยินดีให้บริการ ต้องการสอบถามข้อมูลการรับสมัคร / ข้อมูลคณะศึกษาศาสตร์และนวัตกรรมการศึกษา ส่วนไหนคะ"
        st.session_state["messages"] = [{"role": "model", "content": initial_msg}]
        db_add_message(session_id, "model", initial_msg)

# =========================
# Sidebar & History Management
# =========================
with st.sidebar:
    st.image("https://cdn-icons-png.flaticon.com/512/4712/4712010.png", width=100) # รูปประดับ Sidebar
    st.subheader("🧾 ประวัติการแชท (Chat History)")
    st.caption(f"Session ปัจจุบัน: {session_id[:8]}…")

    if st.button("🆕 เริ่มบทสนทนาใหม่ (New Session)", use_container_width=True):
        new_sid = str(uuid.uuid4())
        st.session_state.session_id = new_sid
        initial_msg = "สวัสดีค่ะ น้องนวัตกรรมยินดีให้บริการ ต้องการสอบถามข้อมูลการรับสมัคร / ข้อมูลคณะศึกษาศาสตร์และนวัตกรรมการศึกษา ส่วนไหนคะ"
        st.session_state["messages"] = [{"role": "model", "content": initial_msg}]
        db_add_message(new_sid, "model", initial_msg)
        st.session_state["view_session_id"] = new_sid
        st.rerun()

    if st.button("🗑️ ล้างประวัติ (Clear Current)", use_container_width=True):
        db_clear_session(session_id)
        st.session_state["messages"] = [{"role": "model", "content": "สวัสดีค่ะ ต้องการสอบถามข้อมูลการรับสมัคร / ข้อมูลคณะศึกษาศาสตร์และนวัตกรรมการศึกษา ส่วนไหนคะ"}]
        db_add_message(session_id, "model", "สวัสดีค่ะ ต้องการสอบถามข้อมูลการรับสมัคร  / ข้อมูลคณะศึกษาศาสตร์และนวัตกรรมการศึกษา ส่วนไหนคะ")
        st.session_state["view_session_id"] = session_id
        st.rerun()

    st.markdown("---")

    # เรียกใช้ฟังก์ชันที่แก้บัคแล้ว (ให้ดึงทุก Session ที่ user มีส่วนร่วม >= 1 ครั้ง)
    sessions_for_select = db_list_sessions(limit=200, min_user_turns=1)
    chosen_sid = None
    if sessions_for_select:
        st.write("**ค้นหาประวัติที่ผ่านมา**")
        session_choices = [f"📝 {shorten(first_q)} | {sid[:8]}…" for sid, first_ts, last_ts, ut, mt, first_q in sessions_for_select]
        chosen = st.selectbox("เลือก session เพื่อดูประวัติ", ["(ปัจจุบัน)"] + session_choices)

        if chosen != "(ปัจจุบัน)":
            sid_prefix = chosen.split("|")[-1].strip().replace("…", "")
            for sid, *_ in sessions_for_select:
                if sid.startswith(sid_prefix):
                    chosen_sid = sid
                    break

            if chosen_sid and st.button("👁️ เปิดดูประวัตินี้", use_container_width=True):
                st.session_state["view_session_id"] = chosen_sid
                st.rerun()
    else:
        st.info("ยังไม่มีประวัติแชทอื่น")

    st.markdown("---")

    st.write("**ดาวน์โหลดข้อมูล (Export CSV)**")
    export_mode = st.radio("เลือกข้อมูลที่ต้องการ", ["ทั้งหมด (All)", "เฉพาะแชทปัจจุบัน"], index=0)

    if export_mode == "ทั้งหมด (All)":
        export_df = db_export_all_as_df()
        st.download_button("⬇️ ดาวน์โหลด CSV", data=df_to_csv_bytes(export_df), file_name="workaw_chat_ALL.csv", mime="text/csv", use_container_width=True)
    elif export_mode == "เฉพาะแชทปัจจุบัน":
        export_df = db_export_session_as_df(session_id)
        st.download_button("⬇️ ดาวน์โหลด CSV", data=df_to_csv_bytes(export_df), file_name=f"workaw_{session_id[:8]}.csv", mime="text/csv", use_container_width=True)

# =========================
# 5) VIEW SESSION MODE
# =========================
view_session_id = st.session_state.get("view_session_id", session_id)

if view_session_id != session_id:
    st.warning(f"👁️ โหมดดูประวัติย้อนหลัง (Session: {view_session_id[:8]})")
    if st.button("⬅️ กลับไปแชทปัจจุบัน", type="primary"):
        st.session_state["view_session_id"] = session_id
        st.rerun()

    loaded_history = db_load_messages(view_session_id, limit=5000)
    for msg in loaded_history:
        avatar = BOT_AVATAR if msg["role"] == "model" else USER_AVATAR
        st.chat_message(msg["role"], avatar=avatar).write(msg["content"])
    st.stop()

# =========================
# 6) CURRENT CHAT UI & LOGIC
# =========================
# แสดงแชทปัจจุบัน
for msg in st.session_state["messages"]:
    avatar = BOT_AVATAR if msg["role"] == "model" else USER_AVATAR
    st.chat_message(msg["role"], avatar=avatar).write(msg["content"])

# รับข้อความใหม่
if prompt := st.chat_input("พิมพ์คำถามของคุณที่นี่..."):
    st.session_state["messages"].append({"role": "user", "content": prompt})
    st.chat_message("user", avatar=USER_AVATAR).write(prompt)
    db_add_message(session_id, "user", prompt)

    def generate_response():
        messages = [{"role": "system", "content": SYSTEM_INSTRUCTION}]
        
        for msg in st.session_state["messages"]:
            role = "assistant" if msg["role"] == "model" else "user"
            messages.append({"role": role, "content": msg["content"]})

        with st.spinner("น้องนวัตกรรมกำลังพิมพ์..."):
            try:
                response = client.chat.completions.create(
                    model="google/gemini-2.5-flash",
                    messages=messages,
                    temperature=0.0,
                    top_p=0.95,
                    max_tokens=800,
                )
                reply_text = response.choices[0].message.content
            except Exception as e:
                reply_text = f"ขออภัยค่ะ ระบบเกิดข้อผิดพลาด: {str(e)}"

        st.session_state["messages"].append({"role": "model", "content": reply_text})
        st.chat_message("model", avatar=BOT_AVATAR).write(reply_text)
        db_add_message(session_id, "model", reply_text)

    generate_response()
