import os
import hashlib
import hmac
import base64
import json
import logging
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import requests as req_lib
from fastapi import FastAPI, Request, Response
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Bangkok")

CHANNEL_SECRET    = os.getenv("LINE_CHANNEL_SECRET", "")
ACCESS_TOKEN      = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MAKE_WEBHOOK_URL  = os.getenv("MAKE_WEBHOOK_URL", "")
GOOGLE_SA_JSON    = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
CALENDAR_ID       = os.getenv("GOOGLE_CALENDAR_ID", "primary")
SHEET_ID          = "1fVgg3d1clzubsyWHSdMeDMCQLhxHSPMJ-f_KK_s1tIo"

SYSTEM_PROMPT = (
    "คุณชื่อ Metro คือ AI assistant ประจำโรงงาน ตอบภาษาไทย "
    "ใช้คำลงท้ายว่า 'ครับ' เสมอ ตอบตรงประเด็น กระชับ ไม่เกิน 3 ประโยค "
    "ห้ามเพิ่มประโยคปิดท้ายเชิญชวนถามเพิ่ม เช่น 'ถ้ามีคำถามเพิ่มเติม...' "
    "หรือ 'ผมพร้อมตอบ...' โดยเด็ดขาด"
)
BOT_KEYWORD = "บอท"

app = FastAPI()


# ══════════════════════════════════════════════════════════════
# Google Auth helper
# ══════════════════════════════════════════════════════════════

def _get_token(scopes: list[str]) -> str | None:
    if not GOOGLE_SA_JSON:
        return None
    try:
        info  = json.loads(GOOGLE_SA_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=scopes
        )
        creds.refresh(GRequest(session=req_lib.Session()))
        return creds.token
    except Exception as e:
        logger.error(f"Google token error: {e}")
        return None


async def get_token_async(scopes: list[str]) -> str | None:
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: _get_token(scopes)
    )


# ══════════════════════════════════════════════════════════════
# Google Sheets — เก็บ Reminder (ไม่หายแม้ Deploy ใหม่)
# ══════════════════════════════════════════════════════════════

SHEETS_SCOPE  = "https://www.googleapis.com/auth/spreadsheets"
SHEET_RANGE   = "Sheet1!A:D"
SHEET_BASE    = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}"


async def sheets_read() -> list[dict]:
    """อ่าน reminder ทั้งหมดจาก Google Sheet"""
    token = await get_token_async([SHEETS_SCOPE])
    if not token:
        logger.error("Sheets: ไม่มี token")
        return []
    url = f"{SHEET_BASE}/values/{SHEET_RANGE}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        logger.error(f"Sheets read error: {r.status_code} {r.text}")
        return []
    rows = r.json().get("values", [])
    reminders = []
    for row in rows:
        if len(row) >= 4 and row[0] != "job_id":   # ข้ามแถว header
            reminders.append({
                "job_id":    row[0],
                "target_id": row[1],
                "text":      row[2],
                "run_at":    row[3],
            })
    return reminders


async def sheets_append(reminder: dict):
    """เพิ่ม reminder แถวใหม่"""
    token = await get_token_async([SHEETS_SCOPE])
    if not token:
        return
    # สร้าง header ถ้ายังไม่มี
    existing = await sheets_read()
    if not existing:
        await _sheets_ensure_header(token)

    url = f"{SHEET_BASE}/values/{SHEET_RANGE}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
    row = [[reminder["job_id"], reminder["target_id"], reminder["text"], reminder["run_at"]]]
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"values": row})
    if r.status_code not in (200, 201):
        logger.error(f"Sheets append error: {r.status_code} {r.text}")


async def sheets_remove(job_id: str):
    """ลบ reminder ที่ยิงแล้ว แล้วเขียนกลับ"""
    all_r   = await sheets_read()
    kept    = [r for r in all_r if r["job_id"] != job_id]
    await sheets_rewrite(kept)


async def sheets_rewrite(reminders: list[dict]):
    """ลบทั้งหมดแล้วเขียนใหม่"""
    token = await get_token_async([SHEETS_SCOPE])
    if not token:
        return
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=15) as client:
        # Clear
        await client.post(f"{SHEET_BASE}/values/{SHEET_RANGE}:clear", headers=h)
        # Write header + data
        rows = [["job_id", "target_id", "text", "run_at"]]
        rows += [[r["job_id"], r["target_id"], r["text"], r["run_at"]] for r in reminders]
        await client.put(
            f"{SHEET_BASE}/values/Sheet1!A1?valueInputOption=RAW",
            headers=h, json={"values": rows})


async def _sheets_ensure_header(token: str):
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10) as client:
        await client.put(
            f"{SHEET_BASE}/values/Sheet1!A1?valueInputOption=RAW",
            headers=h,
            json={"values": [["job_id", "target_id", "text", "run_at"]]})


# ══════════════════════════════════════════════════════════════
# Google Calendar
# ══════════════════════════════════════════════════════════════

async def create_calendar_event(summary: str, run_at: datetime) -> bool:
    token = await get_token_async(["https://www.googleapis.com/auth/calendar"])
    if not token:
        return False
    end_at = run_at + timedelta(minutes=30)
    event  = {
        "summary": f"🔔 {summary}",
        "start":   {"dateTime": run_at.isoformat(), "timeZone": "Asia/Bangkok"},
        "end":     {"dateTime": end_at.isoformat(), "timeZone": "Asia/Bangkok"},
        "reminders": {"useDefault": False,
                      "overrides": [{"method": "popup", "minutes": 10}]},
    }
    url = f"https://www.googleapis.com/calendar/v3/calendars/{CALENDAR_ID}/events"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=event)
    if r.status_code in (200, 201):
        logger.info(f"Calendar event created: {summary}")
        return True
    logger.error(f"Calendar error {r.status_code}: {r.text}")
    return False


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
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(url, headers=headers,
            json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]})


async def push_line(target_id: str, text: str):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, headers=headers,
            json={"to": target_id, "messages": [{"type": "text", "text": text}]})
        logger.info(f"push_line: {r.status_code}")


# ══════════════════════════════════════════════════════════════
# Claude
# ══════════════════════════════════════════════════════════════

async def ask_claude(user_message: str, system: str = SYSTEM_PROMPT) -> str:
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
        resp = await client.post("https://api.anthropic.com/v1/messages",
            headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]


# ══════════════════════════════════════════════════════════════
# Reminder — ตั้ง / ดู / ยิง
# ══════════════════════════════════════════════════════════════

async def set_reminder(remind_text: str, target_id: str, reply_token: str):
    now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    parse_system = (
        f"ตอนนี้คือ {now_str} เวลาไทย (Asia/Bangkok)\n"
        "แปลงข้อความเตือนความจำเป็น JSON เท่านั้น ห้ามมีข้อความอื่น\n"
        "รูปแบบ: {\"reminder_text\": \"ข้อความเตือน\", \"datetime\": \"YYYY-MM-DD HH:MM\"}\n"
        "ถ้าแปลงไม่ได้: {\"error\": \"สาเหตุ\"}"
    )
    raw = await ask_claude(remind_text, system=parse_system)
    try:
        data = json.loads(raw[raw.find("{"):raw.rfind("}")+1])
    except Exception:
        await reply_line(reply_token,
            "❌ ไม่เข้าใจครับ เช่น: บอท เตือน ประชุม พรุ่งนี้ 09:00")
        return

    if "error" in data:
        await reply_line(reply_token, f"❌ {data['error']}")
        return

    try:
        run_at = datetime.strptime(data["datetime"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except Exception:
        await reply_line(reply_token, "❌ แปลงวันเวลาไม่ได้ครับ")
        return

    if run_at <= datetime.now(TZ):
        await reply_line(reply_token, "❌ เวลาที่ระบุผ่านไปแล้วครับ")
        return

    reminder_text = data.get("reminder_text", remind_text)
    job_id        = f"remind_{target_id}_{run_at.strftime('%Y%m%d%H%M%S')}"

    # บันทึกลง Google Sheet (ไม่หายแม้ deploy ใหม่)
    await sheets_append({
        "job_id":    job_id,
        "target_id": target_id,
        "text":      reminder_text,
        "run_at":    run_at.isoformat(),
    })

    # บันทึกลง Google Calendar
    cal_ok = await create_calendar_event(reminder_text, run_at)

    display_dt = run_at.strftime("%d/%m/%Y เวลา %H:%M น.")
    cal_text   = "\n📅 บันทึกใน Google Calendar แล้วครับ" if cal_ok else ""
    await reply_line(reply_token,
        f"✅ ตั้งเตือนแล้วครับ\n📌 {reminder_text}\n🕐 {display_dt}{cal_text}")


async def list_reminders(target_id: str, reply_token: str):
    now     = datetime.now(TZ)
    today   = now.date()
    all_r   = await sheets_read()
    pending = sorted(
        [r for r in all_r if r["target_id"] == target_id],
        key=lambda r: r["run_at"]
    )
    if not pending:
        await reply_line(reply_token, "ไม่มีการแจ้งเตือนที่รออยู่ครับ")
        return

    groups: dict[str, list] = {}
    for r in pending:
        run_at = datetime.fromisoformat(r["run_at"])
        d = run_at.date()
        if d == today:
            label = "📅 วันนี้"
        elif (run_at - now).total_seconds() < 86400 and d > today:
            label = "📅 พรุ่งนี้"
        else:
            label = f"📅 {run_at.strftime('%d/%m/%Y')}"
        groups.setdefault(label, []).append((run_at, r["text"]))

    lines = ["🔔 รายการแจ้งเตือนที่ตั้งไว้ครับ:"]
    for label, items in groups.items():
        lines.append(f"\n{label}")
        for run_at, text in items:
            lines.append(f"  • {run_at.strftime('%H:%M')} น. — {text}")
    await reply_line(reply_token, "\n".join(lines))


async def check_and_fire_reminders():
    """เช็คทุก 1 นาที — ยิง reminder ที่ถึงเวลา"""
    now   = datetime.now(TZ)
    all_r = await sheets_read()
    fired = []
    kept  = []
    for r in all_r:
        run_at = datetime.fromisoformat(r["run_at"])
        if run_at <= now:
            await push_line(r["target_id"], f"🔔 เตือนความจำ\n{r['text']}")
            logger.info(f"Fired: {r['text']}")
            fired.append(r)
        else:
            kept.append(r)
    if fired:
        await sheets_rewrite(kept)


# ══════════════════════════════════════════════════════════════
# Make.com
# ══════════════════════════════════════════════════════════════

async def send_to_make(task_title: str, user_id: str = ""):
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(MAKE_WEBHOOK_URL,
            json={"title": task_title, "user_id": user_id})


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
        target_id   = source.get("groupId") or user_id

        if event_type == "join":
            await reply_line(reply_token,
                "สวัสดีครับ ผม Metro AI assistant\n"
                "มีอะไรให้ช่วยครับ?\n\n"
                f"• {BOT_KEYWORD} [คำถาม]\n"
                f"• {BOT_KEYWORD} เตือน [เรื่อง] [วัน/เวลา]\n"
                f"• {BOT_KEYWORD} ดูเตือน")
            continue

        if event_type != "message":
            continue
        if event["message"]["type"] != "text":
            continue
        if not reply_token:
            continue

        user_text = event["message"]["text"].strip()

        if source_type == "group":
            if not user_text.lower().startswith(BOT_KEYWORD.lower()):
                continue
            user_text = user_text[len(BOT_KEYWORD):].strip()
            if not user_text:
                await reply_line(reply_token,
                    f"มีอะไรให้ช่วยครับ?\n"
                    f"• {BOT_KEYWORD} [คำถาม]\n"
                    f"• {BOT_KEYWORD} เตือน [เรื่อง] [วัน/เวลา]\n"
                    f"• {BOT_KEYWORD} ดูเตือน")
                continue

        # ── ดูรายการแจ้งเตือน ─────────────────────────────────
        REMIND_LIST_KW = (
            "ดูเตือน", "รายการเตือน", "เตือนอะไรบ้าง",
            "มีแจ้งเตือน", "แจ้งเตือนอะไร", "ดูแจ้งเตือน",
            "มีเตือนอะไร", "มีนัดอะไร", "พรุ่งนี้มี",
            "วันนี้มีอะไร", "มีกำหนดการ", "เตือนพรุ่งนี้",
            "แจ้งเตือนพรุ่งนี้", "นัดพรุ่งนี้",
        )
        if any(kw in user_text for kw in REMIND_LIST_KW):
            await list_reminders(target_id, reply_token)
            continue

        # ── ตั้งการแจ้งเตือน ───────────────────────────────────
        if user_text.startswith("เตือน"):
            remind_text = user_text[len("เตือน"):].strip()
            if not remind_text:
                await reply_line(reply_token,
                    "ระบุเรื่องที่ต้องการเตือนด้วยครับ\n"
                    "เช่น: บอท เตือน ประชุมทีม พรุ่งนี้ 09:00")
                continue
            await set_reminder(remind_text, target_id, reply_token)
            continue

        # ── บันทึก Outlook Tasks ───────────────────────────────
        if user_text.startswith("บันทึก "):
            task_title = user_text[len("บันทึก "):].strip()
            if not task_title:
                await reply_line(reply_token, "กรุณาระบุชื่องานด้วยครับ")
                continue
            try:
                await send_to_make(task_title, user_id)
                await reply_line(reply_token,
                    f"บันทึกแล้วครับ ✓\n📋 {task_title}\nดูได้ใน Outlook Tasks")
            except Exception as e:
                logger.error(f"Make.com error: {e}")
                await reply_line(reply_token, "บันทึกไม่สำเร็จครับ ลองใหม่นะครับ")
            continue

        # ── ถาม Claude AI ──────────────────────────────────────
        try:
            answer = await ask_claude(user_text)
        except Exception as e:
            logger.error(f"Claude error: {e}")
            answer = f"ขออภัย เกิดข้อผิดพลาด: {str(e)}"
        await reply_line(reply_token, answer)

    return Response(content="ok", status_code=200)


# ══════════════════════════════════════════════════════════════
# Cron endpoint — cron-job.org ยิงทุก 1 นาที
# ══════════════════════════════════════════════════════════════

@app.get("/cron")
async def cron():
    await check_and_fire_reminders()
    pending = await sheets_read()
    logger.info(f"Cron tick — pending: {len(pending)}")
    return {"status": "ok", "pending": len(pending)}


@app.get("/")
async def root():
    return {"status": "running"}
