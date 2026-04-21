import os
import hashlib
import hmac
import base64
import json
import logging

import httpx
from fastapi import FastAPI, Request, Response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

CHANNEL_SECRET   = os.getenv("LINE_CHANNEL_SECRET", "")
ACCESS_TOKEN     = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MAKE_WEBHOOK_URL  = os.getenv("MAKE_WEBHOOK_URL", "")

SYSTEM_PROMPT = "คุณชื่อ Metro คือ AI assistant ประจำโรงงาน ตอบคำถามเกี่ยวกับการผลิต ตอบภาษาไทย ใช้คำลงท้ายว่า 'ครับ' เสมอ กระชับ ไม่เกิน 3-4 ประโยค เมื่อทักทายให้แนะนำตัวว่า 'สวัสดีครับ ผม Metro AI assistant มีอะไรให้ช่วยครับ'"
BOT_KEYWORD   = "บอท"


# ══════════════════════════════════════════════════════════════
# Claude (Anthropic) helper
# ══════════════════════════════════════════════════════════════

async def ask_claude(user_message: str) -> str:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key":         ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    payload = {
        "model":      "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "system":     SYSTEM_PROMPT,
        "messages":   [{"role": "user", "content": user_message}],
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]


# ══════════════════════════════════════════════════════════════
# Make.com helper
# ══════════════════════════════════════════════════════════════

async def send_to_make(task_title: str, user_id: str = ""):
    payload = {"title": task_title, "user_id": user_id}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(MAKE_WEBHOOK_URL, json=payload)
        resp.raise_for_status()
        logger.info(f"Make.com response: {resp.status_code}")


# ══════════════════════════════════════════════════════════════
# LINE helpers
# ══════════════════════════════════════════════════════════════

def verify_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(
        CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


async def reply_line(reply_token: str, text: str):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type":  "application/json",
    }
    body = {
        "replyToken": reply_token,
        "messages":   [{"type": "text", "text": text}],
    }
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(url, headers=headers, json=body)


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
        source_type = event.get("source", {}).get("type", "")
        user_id     = event.get("source", {}).get("userId", "")

        if event_type == "join":
            msg = (
                f"สวัสดีครับ ผม Metro AI assistant\n"
                f"มีอะไรให้ช่วยครับ?\n\n"
                f"พิมพ์ '{BOT_KEYWORD}' นำหน้าเพื่อถามผมได้เลยครับ\n"
                f"เช่น '{BOT_KEYWORD} สายการผลิตมีปัญหา ทำยังไงดี'"
            )
            await reply_line(reply_token, msg)
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
                await reply_line(reply_token, "มีอะไรให้ช่วยครับ?")
                continue

        # ── บันทึก Outlook Tasks ผ่าน Make.com ───────────────
        if user_text.startswith("บันทึก "):
            task_title = user_text.replace("บันทึก ", "").strip()
            if not task_title:
                await reply_line(reply_token, "กรุณาระบุชื่องานด้วยครับ\nเช่น 'บันทึก ตรวจสอบสายการผลิต A'")
                continue
            try:
                await send_to_make(task_title, user_id)
                await reply_line(
                    reply_token,
                    f"บันทึกแล้วครับ ✓\n📋 {task_title}\n\nดูได้ใน Outlook Tasks และ Microsoft To Do"
                )
            except Exception as e:
                logger.error(f"Make.com error: {e}")
                await reply_line(reply_token, "บันทึกไม่สำเร็จครับ ลองใหม่อีกครั้งนะครับ")
            continue

        # ── ถาม Claude AI ─────────────────────────────────────
        try:
            answer = await ask_claude(user_text)
        except Exception as e:
            logger.error(f"Claude error: {e}")
            if "429" in str(e):
                answer = "ขออภัยครับ ระบบ AI ยุ่งอยู่ รอสักครู่แล้วลองใหม่นะครับ"
            else:
                answer = f"ขออภัย เกิดข้อผิดพลาด: {str(e)}"

        await reply_line(reply_token, answer)

    return Response(content="ok", status_code=200)


@app.get("/")
async def root():
    return {"status": "running"}
