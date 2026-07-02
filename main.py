# main.py — WEC Sales Bot Phase 3 (Rebuild)
# Flask webhook server for Facebook Page Messenger + Click-to-Messenger Ads
#
# สิ่งที่แก้จาก Phase 2:
# 1. รองรับ event จาก Ads ครบทุกรูปแบบ:
#    - messaging_referrals  (ลูกค้าเดิมกดโฆษณา)
#    - postback + referral  (ลูกค้าใหม่กด Get Started จากโฆษณา)
#    - message.referral     (ข้อความแรกจาก Click-to-Messenger ad)
# 2. รองรับ entry.standby — ถ้าแอพไม่ได้เป็น Primary Receiver
#    จะ log เตือนให้เห็นใน Railway logs (สาเหตุหลักที่แชทจาก ads หาย)
# 3. Log ทุก event แบบละเอียด เพื่อ debug ง่าย
# 4. เก็บ ad_id + ref parameter ส่งต่อไป bot engine

import os
import hmac
import hashlib
import json
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from bot_logic import BotEngine

load_dotenv()

# ======================================================
# Config — ใช้ Environment Variables ชุดเดิมทั้งหมด
# ======================================================
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
FB_VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "wec_bot_verify_2569")
FB_APP_SECRET = os.environ.get("FB_APP_SECRET", "")  # เว้นว่าง = dev mode (ข้าม signature check)
GIFT_FB_PSID = os.environ.get("GIFT_FB_PSID", "")

GRAPH_API_URL = "https://graph.facebook.com/v19.0/me/messages"

app = Flask(__name__)
bot = BotEngine()

# Cache ชั่วคราว: sender_id -> {"ad_id": ..., "ref": ..., "source": ...}
# เก็บข้อมูล ads referral ไว้จนกว่าข้อความแรกของลูกค้าจะมาถึง
# (in-memory — รีเซ็ตเมื่อ server restart; Phase 4 ค่อยย้ายไป Redis)
_pending_referrals: dict[str, dict] = {}


# ======================================================
# Facebook helpers
# ======================================================
def verify_fb_signature(body: bytes, signature: str) -> bool:
    if not FB_APP_SECRET:
        return True  # Dev mode: ข้ามการตรวจ signature
    expected = "sha256=" + hmac.new(
        FB_APP_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def send_message(recipient_id: str, text: str):
    if not FB_PAGE_ACCESS_TOKEN:
        print(f"[NO TOKEN] Would send to {recipient_id}: {text[:80]}")
        return
    # Facebook จำกัด 2000 ตัวอักษร/ข้อความ
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)]
    for chunk in chunks:
        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": chunk},
            "messaging_type": "RESPONSE",
        }
        try:
            resp = requests.post(
                GRAPH_API_URL,
                params={"access_token": FB_PAGE_ACCESS_TOKEN},
                json=payload,
                timeout=10,
            )
            if resp.status_code != 200:
                print(f"[FB SEND ERROR] {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"[FB SEND EXCEPTION] {e}")


def alert_gift(sender_id: str, user_text: str, ad_id: str = ""):
    """แจ้งเตือน Gift เมื่อได้ Lead Grade A"""
    if not GIFT_FB_PSID:
        return
    alert = (
        "GRADE A LEAD ใหม่ (Facebook Page)\n"
        f"Sender: {sender_id}\n"
        f"ข้อความ: {user_text[:100]}\n"
        f"Ad ID: {ad_id or '-'}\n\n"
        "ติดต่อกลับใน Messenger ด่วนครับ"
    )
    send_message(GIFT_FB_PSID, alert)


def extract_referral(event: dict) -> dict:
    """
    ดึงข้อมูล referral จาก event ทุกรูปแบบที่ Facebook ส่งมา:
    1. event.referral                    -> messaging_referrals (ลูกค้าเดิมกดโฆษณา / m.me?ref=)
    2. event.postback.referral           -> ลูกค้าใหม่กด Get Started จากโฆษณา
    3. event.message.referral            -> ข้อความแรกจาก Click-to-Messenger ad
    คืน dict: {"ad_id": ..., "ref": ..., "source": ...} หรือ {} ถ้าไม่มี
    """
    ref = (
        event.get("referral")
        or event.get("postback", {}).get("referral")
        or event.get("message", {}).get("referral")
        or {}
    )
    if not ref:
        return {}
    return {
        "ad_id": ref.get("ad_id", ""),
        "ref": ref.get("ref", ""),
        "source": ref.get("source", ""),   # ADS / SHORTLINK / CUSTOMER_CHAT_PLUGIN
        "type": ref.get("type", ""),
    }


# ======================================================
# Event processing
# ======================================================
def process_event(event: dict):
    """ประมวลผล messaging event 1 รายการ"""
    sender_id = event.get("sender", {}).get("id", "")
    if not sender_id:
        return

    # ข้าม echo (ข้อความที่เพจส่งเอง)
    if event.get("message", {}).get("is_echo"):
        return

    # --- 1) เก็บ referral / ad_id ทุกรูปแบบ ---
    referral = extract_referral(event)
    if referral.get("ad_id") or referral.get("ref"):
        _pending_referrals[sender_id] = referral
        print(f"[REFERRAL] {sender_id[:10]}... ad_id={referral.get('ad_id') or '-'} "
              f"ref={referral.get('ref') or '-'} source={referral.get('source') or '-'}")

    # --- 2) postback (เช่น Get Started) — ทักทายลูกค้าทันที ---
    if event.get("postback") and not event.get("message"):
        reply_text, lead_grade = bot.process(
            "สวัสดี", sender_id, referral=_pending_referrals.get(sender_id, {})
        )
        send_message(sender_id, reply_text)
        print(f"[POSTBACK] {sender_id[:10]}... -> welcomed")
        return

    # --- 3) ข้อความ text ปกติ ---
    message = event.get("message", {})
    if not message or not message.get("text"):
        # referral-only event (messaging_referrals) — ไม่มีข้อความ ไม่ต้องตอบ
        return

    user_text = message["text"]
    lead_referral = referral or _pending_referrals.pop(sender_id, {})

    reply_text, lead_grade = bot.process(user_text, sender_id, referral=lead_referral)
    send_message(sender_id, reply_text)

    if lead_grade == "A":
        alert_gift(sender_id, user_text, lead_referral.get("ad_id", ""))

    print(f"[MSG] {sender_id[:10]}... Grade={lead_grade or '-'} "
          f"| Q={user_text[:40]!r} | ad_id={lead_referral.get('ad_id') or '-'}")


# ======================================================
# Routes
# ======================================================
@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "WEC Facebook Bot v3 — Running"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Facebook เรียกครั้งเดียวตอนตั้งค่า webhook"""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == FB_VERIFY_TOKEN:
        print("[WEBHOOK] Verified by Facebook")
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def receive_webhook():
    """รับ event จาก Facebook"""
    body = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not verify_fb_signature(body, signature):
        print("[WEBHOOK] Signature verification FAILED")
        return "Unauthorized", 401

    data = request.get_json(silent=True)
    if not data or data.get("object") != "page":
        return jsonify({"status": "ignored"})

    for entry in data.get("entry", []):
        # กรณีปกติ: แอพเป็น Primary Receiver
        for event in entry.get("messaging", []):
            try:
                process_event(event)
            except Exception as e:
                print(f"[EVENT ERROR] {e} | event={json.dumps(event)[:300]}")

        # กรณีแอพไม่ได้เป็น Primary Receiver — event จะมาอยู่ใน standby
        # นี่คือสาเหตุหลักที่ "แชทจาก ads ไม่เข้า bot"
        # ต้องไปตั้ง Primary Receiver ใน Facebook App (ดู WORKFLOW.md ข้อ 3.4)
        for event in entry.get("standby", []):
            sender = event.get("sender", {}).get("id", "")
            print(f"[STANDBY WARNING] Event in standby from {sender[:10]}... "
                  "— แอพไม่ได้เป็น Primary Receiver! ไปแก้ใน App Dashboard "
                  "(Messenger > Settings > App Roles) หรือปิด Automation ใน Meta Business Suite")

    return jsonify({"status": "ok"})


# ======================================================
# Main
# ======================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"WEC Bot v3 starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
