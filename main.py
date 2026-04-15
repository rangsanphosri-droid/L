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

CHANNEL_SECRET  = os.getenv("aef87a8b12d0c955abcd8b5f1b599983", "")
ACCESS_TOKEN    = os.getenv("3NO//yPmzUfMweyrW/ev/FFwWT5q6+f4tjhFRoGhX1PG+cLDzj0AHmANMBw0mOGWyjmTKUiZxvv/ItYzT/QZ6cHAxkuC4sPuLtuEPRs6QUWp/BcVvD+8aHX8gm5i2t8+GUWNw71NZKtPjIREdWG/BAdB04t89/1O/w1cDnyilFU=", "")
GEMINI_API_KEY  = os.getenv("AIzaSyCxHXwI2h-ubqXky85YHn-WYWCKbth6_9k", "")

SYSTEM_PROMPT = "คุณคือ AI assistant ประจำโรงงาน ตอบคำถามเกี่ยวกับการผลิต ตอบภาษาไทย กระชับ ไม่เกิน 3-4 ประโยค"

BOT_KEYWORD = "bot"


def verify_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(
        CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


async def ask_gemini(user_message: str) -> str:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
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
        logger.info(f"reply_line status: {r.status_code} body: {r.text}")


@app.post("/callback")
async def callback(request: Request):
    body      = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        logger.warning("Signature verification failed")
        return Response(content="ok", status_code=200)

    data = json.loads(body)
    logger.info(f"Received events: {json.dumps(data, ensure_ascii=False)}")

    for event in data.get("events", []):
        event_type  = event.get("type")
        reply_token = event.get("replyToken", "")
        source_type = event.get("source", {}).get("type", "")

        logger.info(f"event_type={event_type} source_type={source_type} reply_token={bool(reply_token)}")

        if event_type == "join":
            await reply_line(reply_token, f"สวัสดีครับ! พิมพ์ {BOT_KEYWORD} นำหน้าเพื่อถามผมได้เลยครับ")
            continue

        if event_type != "message":
            logger.info(f"Skipped: event_type={event_type}")
            continue
        if event["message"]["type"] != "text":
            logger.info(f"Skipped: message type={event['message']['type']}")
            continue
        if not reply_token:
            logger.info("Skipped: no reply_token")
            continue

        user_text = event["message"]["text"].strip()
        logger.info(f"user_text='{user_text}' source_type={source_type}")

        if source_type == "group":
            if not user_text.lower().startswith(BOT_KEYWORD.lower()):
                logger.info(f"Skipped: group msg without keyword '{BOT_KEYWORD}'")
                continue
            user_text = user_text[len(BOT_KEYWORD):].strip()
            if not user_text:
                await reply_line(reply_token, f"มีอะไรให้ช่วยครับ? พิมพ์ {BOT_KEYWORD} ตามด้วยคำถาม")
                continue

        try:
            logger.info(f"Asking Gemini: '{user_text}'")
            answer = await ask_gemini(user_text)
            logger.info(f"Gemini answered: '{answer[:50]}...'")
        except Exception as e:
            logger.error(f"Gemini error: {e}")
            answer = f"ขออภัย เกิดข้อผิดพลาด: {str(e)}"

        await reply_line(reply_token, answer)

    return Response(content="ok", status_code=200)


@app.get("/")
async def root():
    return {"status": "running"}
