"""
LINE Bot + Gemini AI
====================
วิธีรัน (local):  uvicorn main:app --reload --port 8000
วิธี deploy:      push ขึ้น GitHub → Render deploy อัตโนมัติ
"""

import os
import hashlib
import hmac
import base64
import json

import httpx
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

# ── ค่า config อ่านจาก Environment Variables ──────────────────────────────────
CHANNEL_SECRET  = os.getenv("aef87a8b12d0c955abcd8b5f1b599983", "")
ACCESS_TOKEN    = os.getenv("3NO//yPmzUfMweyrW/ev/FFwWT5q6+f4tjhFRoGhX1PG+cLDzj0AHmANMBw0mOGWyjmTKUiZxvv/ItYzT/QZ6cHAxkuC4sPuLtuEPRs6QUWp/BcVvD+8aHX8gm5i2t8+GUWNw71NZKtPjIREdWG/BAdB04t89/1O/w1cDnyilFU=", "")
GEMINI_API_KEY  = os.getenv("AIzaSyCxHXwI2h-ubqXky85YHn-WYWCKbth6_9k", "")

# ── System prompt: บอก AI ว่าตัวเองคือใคร ────────────────────────────────────
SYSTEM_PROMPT = """
คุณคือ AI assistant ประจำโรงงาน ตอบคำถามเกี่ยวกับการผลิต
ตอบภาษาไทย กระชับ ชัดเจน ไม่เกิน 3-4 ประโยค
"""


# ══════════════════════════════════════════════════════════════════════════════
# Helper functions
# ══════════════════════════════════════════════════════════════════════════════

def verify_signature(body: bytes, signature: str) -> bool:
    """ตรวจสอบว่า request มาจาก LINE จริง ไม่ใช่คนอื่นปลอม"""
    digest = hmac.new(
        CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


async def ask_gemini(user_message: str) -> str:
    """ส่งข้อความไปถาม Gemini แล้วรับคำตอบกลับมา"""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "system_instruction": {
            "parts": [{"text": SYSTEM_PROMPT}]
        },
        "contents": [
            {"parts": [{"text": user_message}]}
        ]
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        # ดึงข้อความตอบกลับออกมา
        return data["candidates"][0]["content"]["parts"][0]["text"]


async def reply_line(reply_token: str, text: str):
    """ส่งข้อความตอบกลับไปใน LINE chat"""
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
        await client.post(url, headers=headers, json=body)


# ══════════════════════════════════════════════════════════════════════════════
# Webhook endpoint
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/callback")
async def callback(request: Request):
    """
    LINE จะ POST ข้อมูลมาที่นี่ทุกครั้งที่ user ส่งข้อความ
    """
    body      = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    # ตรวจสอบ signature — ถ้าไม่ผ่านหยุดทันที
    if not verify_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    data = json.loads(body)

    for event in data.get("events", []):
        # รับเฉพาะ message event ประเภท text
        if event.get("type") != "message":
            continue
        if event["message"]["type"] != "text":
            continue

        user_text   = event["message"]["text"]
        reply_token = event["replyToken"]

        try:
            # ถามผ่าน Gemini AI
            answer = await ask_gemini(user_text)
        except Exception as e:
            answer = f"ขออภัย เกิดข้อผิดพลาด: {str(e)}"

        # ตอบกลับใน LINE
        await reply_line(reply_token, answer)

    # LINE ต้องการ 200 OK เสมอ
    return {"status": "ok"}


@app.get("/")
async def root():
    """Health check — ใช้ตรวจว่า server ยังทำงานอยู่"""
    return {"status": "running", "bot": "LINE Bot Factory"}
