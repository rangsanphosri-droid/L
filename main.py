import os
import hashlib
import hmac
import base64
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request, Response
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Bangkok")

CHANNEL_SECRET    = os.getenv("LINE_CHANNEL_SECRET", "")
ACCESS_TOKEN      = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

SYSTEM_PROMPT = "คุณคือ AI assistant ประจำโรงงาน ตอบภาษาไทย กระชับ ไม่เกิน 3-4 ประโยค"
BOT_KEYWORD   = "บอท"
FILE_DIR      = Path("./files")
REMINDERS_FILE = Path("./reminders.json")

CONTENT_EXT = {
    "image": ".jpg",
    "video": ".mp4",
    "audio": ".m4a",
}

daily_log: dict[str, list[str]]  = defaultdict(list)
group_name_cache: dict[str, str] = {}
scheduler = AsyncIOScheduler(timezone=TZ)


# ─────────────────────── Lifespan ───────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # โหลด reminder ที่ค้างไว้ (กรณี restart)
    load_saved_reminders()

    # สรุปไฟล์รายวัน 17:00 จ-ส
    scheduler.add_job(
        send_daily_summary,
        CronTrigger(day_of_week="mon-sat", hour=17, minute=0, timezone=TZ),
        id="daily_summary",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started")
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


# ─────────────────────── Helpers ────────────────────────────

def verify_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(
        CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def safe_folder_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in (" ", "-", "_") else "_" for c in name).strip()


# ─────────────────────── Line API ───────────────────────────

async def get_group_name(group_id: str) -> str:
    if group_id in group_name_cache:
        return group_name_cache[group_id]
    url = f"https://api.line.me/v2/bot/group/{group_id}/summary"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=headers)
        name = r.json().get("groupName", group_id) if r.status_code == 200 else group_id
    group_name_cache[group_id] = name
    return name


async def reply_line(reply_token: str, text: str):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    body = {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, headers=headers, json=body)
        logger.info(f"reply_line status: {r.status_code}")


async def push_line(target_id: str, text: str):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    body = {"to": target_id, "messages": [{"type": "text", "text": text}]}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, headers=headers, json=body)
        logger.info(f"push_line status: {r.status_code}")


# ─────────────────────── Claude ─────────────────────────────

async def ask_claude(user_message: str, system: str = SYSTEM_PROMPT) -> str:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "system": system,
        "messages": [{"role": "user", "content": user_message}],
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]


# ─────────────────────── Reminder ───────────────────────────

def save_reminders_to_file(reminders: list[dict]):
    """บันทึก reminder ลงไฟล์ เพื่อกันหาย เมื่อ restart"""
    REMINDERS_FILE.write_text(json.dumps(reminders, ensure_ascii=False, indent=2))


def load_saved_reminders():
    """โหลด reminder ที่ยังไม่ถึงเวลาจากไฟล์ กลับเข้า scheduler"""
    if not REMINDERS_FILE.exists():
        return
    try:
        reminders = json.loads(REMINDERS_FILE.read_text())
        now = datetime.now(TZ)
        kept = []
        for r in reminders:
            run_at = datetime.fromisoformat(r["run_at"])
            if run_at > now:
                schedule_reminder_job(r["job_id"], r["target_id"], r["text"], run_at)
                kept.append(r)
        save_reminders_to_file(kept)
        logger.info(f"Loaded {len(kept)} pending reminders")
    except Exception as e:
        logger.error(f"load reminders error: {e}")


def schedule_reminder_job(job_id: str, target_id: str, text: str, run_at: datetime):
    scheduler.add_job(
        send_reminder,
        DateTrigger(run_date=run_at, timezone=TZ),
        args=[job_id, target_id, text],
        id=job_id,
        replace_existing=True,
    )


async def send_reminder(job_id: str, target_id: str, text: str):
    """ส่งข้อความเตือน และลบออกจากไฟล์"""
    await push_line(target_id, f"🔔 เตือนความจำ\n{text}")

    # ลบออกจากไฟล์
    if REMINDERS_FILE.exists():
        reminders = json.loads(REMINDERS_FILE.read_text())
        reminders = [r for r in reminders if r["job_id"] != job_id]
        save_reminders_to_file(reminders)


async def parse_and_set_reminder(user_text: str, target_id: str, reply_token: str):
    """
    ให้ Claude แปลงข้อความภาษาธรรมชาติ → วันเวลา + ข้อความเตือน
    แล้ว schedule job
    """
    now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

    parse_system = f"""คุณช่วยแปลงข้อความเตือนความจำภาษาไทยให้เป็น JSON
ตอนนี้คือวันที่และเวลา: {now_str} (Asia/Bangkok)
ตอบเฉพาะ JSON เท่านั้น ห้ามมีข้อความอื่น รูปแบบ:
{{
  "reminder_text": "ข้อความที่จะแสดงตอนเตือน",
  "datetime": "YYYY-MM-DD HH:MM"
}}
ถ้าไม่สามารถแยกเวลาได้ ให้ตอบ: {{"error": "ไม่เข้าใจเวลา"}}"""

    raw = await ask_claude(user_text, system=parse_system)

    # ดึง JSON ออกจากคำตอบ
    try:
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        data  = json.loads(raw[start:end])
    except Exception:
        await reply_line(reply_token, "❌ ไม่เข้าใจรูปแบบการเตือนครับ\nลองพิมพ์ใหม่ เช่น:\nบอท เตือน ประชุม พรุ่งนี้ 09:00")
        return

    if "error" in data:
        await reply_line(reply_token, f"❌ {data['error']}\nตัวอย่าง: บอท เตือน ส่งรายงาน วันศุกร์ 16:00")
        return

    reminder_text = data.get("reminder_text", user_text)
    dt_str        = data.get("datetime", "")

    try:
        run_at = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except Exception:
        await reply_line(reply_token, "❌ แปลงวันเวลาไม่ได้ครับ ลองระบุให้ชัดขึ้น เช่น 'พรุ่งนี้ 09:00' หรือ '20/04 14:30'")
        return

    if run_at <= datetime.now(TZ):
        await reply_line(reply_token, "❌ เวลาที่ระบุผ่านไปแล้วครับ กรุณาระบุเวลาในอนาคต")
        return

    # สร้าง job
    job_id = f"remind_{target_id}_{run_at.strftime('%Y%m%d%H%M%S')}"
    schedule_reminder_job(job_id, target_id, reminder_text, run_at)

    # บันทึกลงไฟล์กันหาย
    if REMINDERS_FILE.exists():
        existing = json.loads(REMINDERS_FILE.read_text())
    else:
        existing = []
    existing.append({
        "job_id":    job_id,
        "target_id": target_id,
        "text":      reminder_text,
        "run_at":    run_at.isoformat(),
    })
    save_reminders_to_file(existing)

    display_dt = run_at.strftime("%d/%m/%Y เวลา %H:%M น.")
    await reply_line(reply_token, f"✅ ตั้งเตือนแล้วครับ\n📌 {reminder_text}\n🕐 {display_dt}")
    logger.info(f"Reminder set: [{reminder_text}] at {run_at}")


# ─────────────────────── File Saving ────────────────────────

async def handle_media(event: dict):
    source      = event.get("source", {})
    source_type = source.get("type", "")
    if source_type != "group":
        return

    group_id = source.get("groupId", "")
    msg      = event["message"]
    msg_type = msg["type"]
    msg_id   = msg["id"]

    if msg_type == "file":
        original_name = msg.get("fileName", f"{msg_id}.bin")
        ext      = Path(original_name).suffix or ".bin"
        filename = original_name
    else:
        ext      = CONTENT_EXT.get(msg_type, ".bin")
        ts       = datetime.now(TZ).strftime("%H%M%S")
        filename = f"{ts}_{msg_id}{ext}"

    group_name = await get_group_name(group_id)
    date_str   = datetime.now(TZ).strftime("%Y-%m-%d")
    folder     = FILE_DIR / safe_folder_name(group_name) / date_str
    folder.mkdir(parents=True, exist_ok=True)

    dl_url  = f"https://api-data.line.me/v2/bot/message/{msg_id}/content"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(dl_url, headers=headers)
            r.raise_for_status()
        (folder / filename).write_bytes(r.content)
        daily_log[group_id].append(filename)
        logger.info(f"Saved: {folder / filename}")
    except Exception as e:
        logger.error(f"Download error [{msg_id}]: {e}")


# ─────────────────────── Daily Summary ──────────────────────

async def send_daily_summary():
    if not daily_log:
        return
    date_str = datetime.now(TZ).strftime("%d/%m/%Y")
    for group_id, files in list(daily_log.items()):
        if not files:
            continue
        group_name = await get_group_name(group_id)
        lines = [f"📁 สรุปไฟล์วันนี้ ({date_str})\nกลุ่ม: {group_name}"]
        for i, f in enumerate(files, 1):
            lines.append(f"  {i}. {f}")
        lines.append(f"\nรวม {len(files)} ไฟล์ — บันทึกเรียบร้อยแล้วครับ ✅")
        await push_line(group_id, "\n".join(lines))
    daily_log.clear()


# ─────────────────────── Webhook ────────────────────────────

@app.post("/callback")
async def callback(request: Request):
    body      = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        logger.warning("Signature verification failed")
        return Response(content="ok", status_code=200)

    data = json.loads(body)

    for event in data.get("events", []):
        event_type  = event.get("type")
        reply_token = event.get("replyToken", "")
        source      = event.get("source", {})
        source_type = source.get("type", "")
        msg_type    = event.get("message", {}).get("type", "") if event_type == "message" else ""

        logger.info(f"event={event_type} source={source_type} msg_type={msg_type}")

        # ── บอทเข้ากลุ่ม ──
        if event_type == "join":
            await reply_line(reply_token,
                f"สวัสดีครับ! พิมพ์ '{BOT_KEYWORD}' นำหน้าเพื่อถามผมได้เลยครับ\n"
                f"ตัวอย่าง:\n"
                f"• {BOT_KEYWORD} [คำถาม]\n"
                f"• {BOT_KEYWORD} เตือน [เรื่อง] [วัน/เวลา]")
            continue

        if event_type != "message":
            continue
        if not reply_token:
            continue

        # ── รูป / วิดีโอ / เสียง / ไฟล์ → บันทึก ──
        if msg_type in ("image", "video", "audio", "file"):
            await handle_media(event)
            continue

        if msg_type != "text":
            continue

        user_text = event["message"]["text"].strip()

        # ── กลุ่ม: ต้องขึ้นต้นด้วย "บอท" ──
        if source_type == "group":
            if not user_text.lower().startswith(BOT_KEYWORD.lower()):
                continue
            user_text = user_text[len(BOT_KEYWORD):].strip()
            if not user_text:
                await reply_line(reply_token,
                    f"มีอะไรให้ช่วยครับ?\n"
                    f"• {BOT_KEYWORD} [คำถาม]\n"
                    f"• {BOT_KEYWORD} เตือน [เรื่อง] [วัน/เวลา]")
                continue

        # ── คำสั่งเตือนความจำ ──
        if user_text.startswith("เตือน"):
            remind_text = user_text[len("เตือน"):].strip()
            target_id = source.get("groupId") or source.get("userId", "")
            await parse_and_set_reminder(remind_text, target_id, reply_token)
            continue

        # ── ถาม AI ──
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
    remind_count = sum(1 for j in pending if j.id.startswith("remind_"))
    return {
        "status": "running",
        "files_today": sum(len(v) for v in daily_log.values()),
        "pending_reminders": remind_count,
    }
