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

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
ACCESS_TOKEN   = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

SYSTEM_PROMPT = "คุณคือ AI assistant ประจำโรงงาน ตอบคำถามเกี่ยวกับการผลิต ตอบภาษาไทย กระชับ ไม่เกิน 3-4 ประโยค"
BOT_KEYWORD = "บอท"


def verify_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")

    # Debug: แสดงค่าจริงที่คำนวณได้ vs ที่ LINE ส่งมา
    logger.info(f"SECRET length : {len(LINE_CHANNEL_SECRET)}")
    logger.info(f"SECRET preview: '{LINE_CHANNEL_SECRET[:6]}...{LINE_CHANNEL_SECRET[-4:]}'")
    logger.info(f"Expected sig  : {expected[:20]}...")
    logger.info(f"Received sig  : {signature[:20]}...")
    logger.info(f"Match         : {hmac.compare_digest(expected, signature)}")

    return hmac.compare_digest(expected, signature)


async def ask_gemini(user_message: str) -> str:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": user_message}]}]
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]


async def reply_line(reply_token: str, text: str):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, headers=headers, json=body)
        logger.info(f"reply_line status: {r.status_code}")


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
        source_type = event.get("source", {}).get("type", "")

        logger.info(f"event_type={event_type} source_type={source_type}")

        if event_type == "join":
            await reply_line(reply_token, f"สวัสดีครับ! พิมพ์ '{BOT_KEYWORD}' นำหน้าเพื่อถามผมได้เลยครับ")
            continue

        if event_type != "message":
            continue
        if event["message"]["type"] != "text":
            continue
        if not reply_token:
            continue

        user_text = event["message"]["text"].strip()
        logger.info(f"user_text='{user_text}'")

        if source_type == "group":
            if not user_text.lower().startswith(BOT_KEYWORD.lower()):
                logger.info(f"Skipped: no keyword")
                continue
            user_text = user_text[len(BOT_KEYWORD):].strip()
            if not user_text:
                await reply_line(reply_token, f"มีอะไรให้ช่วยครับ? พิมพ์ '{BOT_KEYWORD}' ตามด้วยคำถาม")
                continue

        try:
            answer = await ask_gemini(user_text)
        except Exception as e:
            logger.error(f"Gemini error: {e}")
            if "429" in str(e):
        answer = "ขออภัยครับ ระบบ AI ยุ่งอยู่ รอสักครู่แล้วลองใหม่นะครับ"
    else:
        answer = f"ขออภัย เกิดข้อผิดพลาด: {str(e)}"

        await reply_line(reply_token, answer)

    return Response(content="ok", status_code=200)


@app.get("/")
async def root():
    return {"status": "running"}
