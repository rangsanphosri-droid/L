import os
import hashlib
import hmac
import base64
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request, Response
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Bangkok")

CHANNEL_SECRET    = os.getenv("LINE_CHANNEL_SECRET", "")
ACCESS_TOKEN      = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MAKE_WEBHOOK_URL  = os.getenv("MAKE_WEBHOOK_URL", "")

SYSTEM_PROMPT = (
    "คุณชื่อ Metro คือ AI assistant ประจำโรงงาน ตอบภาษาไทย "
    "ใช้คำลงท้ายว่า 'ครับ' เสมอ ตอบตรงประเด็น กระชับ ไม่เกิน 3 ประโยค "
    "ห้ามเพิ่มประโยคปิดท้ายเชิญชวนถามเพิ่ม เช่น 'ถ้ามีคำถามเพิ่มเติม...' "
    "หรือ 'ผมพร้อมตอบ...' โดยเด็ดขาด"
)
BOT_KEYWORD    = "บอท"
REMINDERS_FILE = Path("./reminders.json")

scheduler = AsyncIOScheduler(timezone=TZ)


# ══════════════════════════════════════════════════════════════
# Lifespan
# ══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_saved_reminders()
    scheduler.start()
    logger.info("Scheduler started")
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


# ══════════════════════════════════════════════════════════════
# Claude helper
# ══════════════════════════════════════════════════════════════

async def ask_claude(user_message: str, system: str = SYSTEM_PROMPT) -> str:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key":         ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    payload = {
        "model":      "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "system":     system,
        "messages":   [{"role": "user", "content": user_message}],
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]


# ══════════════════════════════════════════════════════════════
# Make.com helper
# ══════════════════════════════════════════════════════════════

async def send_to_make(task_title: str, user_id: str = ""):
    payload = {"title": task_title, "user_id": user_id}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(MAKE_WEBHOOK_URL, json=payload)
        resp.raise_for_status()


# ══════════════════════════════════════════════════════════════
# LINE helpers
# ══════════════════════════════════════════════════════════════

def verify_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(
        CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


async def reply_line(reply_token: str, text: str):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    body = {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(url, headers=headers, json=body)


async def push_line(target_id: str, text: str):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    body = {"to": target_id, "messages": [{"type": "text", "text": text}]}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, headers=headers, json=body)
        logger.info(f"push_line status: {r.status_code}")


# ══════════════════════════════════════════════════════════════
# Reminder — บันทึก / โหลด / ส่ง
# ══════════════════════════════════════════════════════════════

def _load_file() -> list[dict]:
    if not REMINDERS_FILE.exists():
        return []
    try:
        return json.loads(REMINDERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_file(reminders: list[dict]):
    REMINDERS_FILE.write_text(
        json.dumps(reminders, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_saved_reminders():
    """โหลด reminder ที่ยังไม่ถึงเวลาจากไฟล์ กลับเข้า scheduler"""
    now  = datetime.now(TZ)
    kept = []
    for r in _load_file():
        run_at = datetime.fromisoformat(r["run_at"])
        if run_at > now:
            _add_job(r["job_id"], r["target_id"], r["text"], run_at)
            kept.append(r)
    _save_file(kept)
    logger.info(f"Loaded {len(kept)} pending reminders")


def _add_job(job_id: str, target_id: str, text: str, run_at: datetime):
    scheduler.add_job(
        _fire_reminder,
        DateTrigger(run_date=run_at, timezone=TZ),
        args=[job_id, target_id, text],
        id=job_id,
        replace_existing=True,
    )


async def _fire_reminder(job_id: str, target_id: str, text: str):
    """ส่งข้อความเตือน และลบออกจากไฟล์"""
    await push_line(target_id, f"🔔 เตือนความจำ\n{text}")
    reminders = [r for r in _load_file() if r["job_id"] != job_id]
    _save_file(reminders)


async def set_reminder(remind_text: str, target_id: str, reply_token: str):
    """ให้ Claude แปลงประโยคภาษาธรรมชาติ → วันเวลา แล้ว schedule"""
    now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    parse_system = (
        f"ตอนนี้คือ {now_str} เวลาไทย (Asia/Bangkok)\n"
        "แปลงข้อความเตือนความจำเป็น JSON เท่านั้น ห้ามมีข้อความอื่น\n"
        "รูปแบบ: {\"reminder_text\": \"ข้อความเตือน\", \"datetime\": \"YYYY-MM-DD HH:MM\"}\n"
        "ถ้าแปลงไม่ได้: {\"error\": \"สาเหตุ\"}"
    )
    raw = await ask_claude(remind_text, system=parse_system)

    try:
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        data  = json.loads(raw[start:end])
    except Exception:
        await reply_line(reply_token,
            "❌ ไม่เข้าใจครับ ลองพิมพ์ใหม่\nเช่น: บอท เตือน ประชุม พรุ่งนี้ 09:00")
        return

    if "error" in data:
        await reply_line(reply_token,
            f"❌ {data['error']}\nเช่น: บอท เตือน ส่งรายงาน วันศุกร์ 16:00")
        return

    try:
        run_at = datetime.strptime(data["datetime"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except Exception:
        await reply_line(reply_token, "❌ แปลงวันเวลาไม่ได้ครับ ระบุให้ชัดขึ้น เช่น 'พรุ่งนี้ 09:00'")
        return

    if run_at <= datetime.now(TZ):
        await reply_line(reply_token, "❌ เวลาที่ระบุผ่านไปแล้วครับ กรุณาระบุเวลาในอนาคต")
        return

    reminder_text = data.get("reminder_text", remind_text)
    job_id        = f"remind_{target_id}_{run_at.strftime('%Y%m%d%H%M%S')}"
    _add_job(job_id, target_id, reminder_text, run_at)

    # บันทึกลงไฟล์
    existing = _load_file()
    existing.append({"job_id": job_id, "target_id": target_id,
                     "text": reminder_text, "run_at": run_at.isoformat()})
    _save_file(existing)

    display_dt = run_at.strftime("%d/%m/%Y เวลา %H:%M น.")
    await reply_line(reply_token,
        f"✅ ตั้งเตือนแล้วครับ\n📌 {reminder_text}\n🕐 {display_dt}")
    logger.info(f"Reminder set: [{reminder_text}] at {run_at}")


async def list_reminders(target_id: str, reply_token: str):
    """แสดงรายการ reminder ที่ยังรออยู่"""
    pending = [r for r in _load_file() if r["target_id"] == target_id]
    if not pending:
        await reply_line(reply_token, "ไม่มีการแจ้งเตือนที่รออยู่ครับ")
        return
    lines = ["📋 รายการแจ้งเตือนที่ตั้งไว้ครับ:"]
    for i, r in enumerate(pending, 1):
        dt = datetime.fromisoformat(r["run_at"]).strftime("%d/%m/%Y %H:%M")
        lines.append(f"{i}. {r['text']} — {dt}")
    await reply_line(reply_token, "\n".join(lines))


# ══════════════════════════════════════════════════════════════
# Webhook
# ══════════════════════════════════════════════════════════════

@app.post("/callback")
async def callback(request: Request):
    body      = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        return Response(content="ok", status_code=200)

    data = json.loads(body)

    for event in data.get("events", []):
        event_type  = event.get("type")
        reply_token = event.get("replyToken", "")
        source      = event.get("source", {})
        source_type = source.get("type", "")
        user_id     = source.get("userId", "")

        if event_type == "join":
            await reply_line(reply_token,
                "สวัสดีครับ ผม Metro AI assistant\n"
                "มีอะไรให้ช่วยครับ?\n\n"
                f"• {BOT_KEYWORD} [คำถาม]\n"
                f"• {BOT_KEYWORD} เตือน [เรื่อง] [วัน/เวลา]\n"
                f"• {BOT_KEYWORD} ดูเตือน — ดูรายการแจ้งเตือน")
            continue

        if event_type != "message":
            continue
        if event["message"]["type"] != "text":
            continue
        if not reply_token:
            continue

        user_text = event["message"]["text"].strip()
        target_id = source.get("groupId") or user_id

        if source_type == "group":
            if not user_text.lower().startswith(BOT_KEYWORD.lower()):
                continue
            user_text = user_text[len(BOT_KEYWORD):].strip()
            if not user_text:
                await reply_line(reply_token,
                    "มีอะไรให้ช่วยครับ?\n"
                    f"• {BOT_KEYWORD} [คำถาม]\n"
                    f"• {BOT_KEYWORD} เตือน [เรื่อง] [วัน/เวลา]\n"
                    f"• {BOT_KEYWORD} ดูเตือน")
                continue

        # ── ดูรายการแจ้งเตือน ────────────────────────────────
        REMIND_LIST_KEYWORDS = (
            "ดูเตือน", "รายการเตือน", "เตือนอะไรบ้าง",
            "มีแจ้งเตือนอะไรบ้าง", "แจ้งเตือนอะไรบ้าง",
            "ดูแจ้งเตือน", "มีเตือนอะไรบ้าง", "เตือนมีอะไรบ้าง",
            "มีอะไรเตือนบ้าง", "รายการแจ้งเตือน",
        )
        if any(user_text == kw or user_text.startswith(kw) for kw in REMIND_LIST_KEYWORDS):
            await list_reminders(target_id, reply_token)
            continue

        # ── ตั้งการแจ้งเตือน ──────────────────────────────────
        if user_text.startswith("เตือน"):
            remind_text = user_text[len("เตือน"):].strip()
            if not remind_text:
                await reply_line(reply_token,
                    "ระบุเรื่องที่ต้องการเตือนด้วยครับ\n"
                    "เช่น: บอท เตือน ประชุมทีม พรุ่งนี้ 09:00")
                continue
            await set_reminder(remind_text, target_id, reply_token)
            continue

        # ── บันทึก Outlook Tasks ──────────────────────────────
        if user_text.startswith("บันทึก "):
            task_title = user_text[len("บันทึก "):].strip()
            if not task_title:
                await reply_line(reply_token,
                    "กรุณาระบุชื่องานด้วยครับ\nเช่น: บันทึก ตรวจสอบสายการผลิต A")
                continue
            try:
                await send_to_make(task_title, user_id)
                await reply_line(reply_token,
                    f"บันทึกแล้วครับ ✓\n📋 {task_title}\nดูได้ใน Outlook Tasks")
            except Exception as e:
                logger.error(f"Make.com error: {e}")
                await reply_line(reply_token, "บันทึกไม่สำเร็จครับ ลองใหม่อีกครั้งนะครับ")
            continue

        # ── ถาม Claude AI ─────────────────────────────────────
        try:
            answer = await ask_claude(user_text)
        except Exception as e:
            logger.error(f"Claude error: {e}")
            answer = f"ขออภัย เกิดข้อผิดพลาด: {str(e)}"

        await reply_line(reply_token, answer)

    return Response(content="ok", status_code=200)


@app.get("/")
async def root():
    pending = scheduler.get_jobs()
    return {
        "status":            "running",
        "pending_reminders": sum(1 for j in pending if j.id.startswith("remind_")),
    }
