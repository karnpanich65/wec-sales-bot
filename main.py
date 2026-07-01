# main.py — WEC Sales Bot Phase 1
# Flask webhook server for Facebook Page Messenger

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
# Config
# ======================================================
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
FB_VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "wec_bot_verify_2569")
FB_APP_SECRET = os.environ.get("FB_APP_SECRET", "")
GIFT_FB_PSID = os.environ.get("GIFT_FB_PSID", "")

app = Flash(__name__)
bot = BotEngine()

GRAPH_API_URL = "https://graph.facebook.com/v19.0/me/messages"


# ======================================================
# Facebook helpers
# ======================================================
def verify_fb_signature(body: bytes, signature: str) -> bool:
    if not FB_APP_SECRET:
        return True  # Dev mode: skip verification
    expected = "sha256=" + hmac.new(
        FB_APP_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def send_message(recipient_id: str, text: str):
    if not FB_PAGE_ACCESS_TOKEN:
        print(f"[NO TOKEN] Would send to {recipient_id}: {text[:80]}")
        return
    # Split long messages (Facebook limit: 2000 chars)
    chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
    for chunk in chunks:
        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": chunk},
            "messaging_type": "RESPONSE",
        }
        resp = requests.post(
            GRAPH_API_URL,
            params={"access_token": FB_PAGE_ACCESS_TOKEN},
            json=payload,
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"FB Send error {resp.status_code}: {resp.text[:200]}")


def alert_gift(sender_id: str, user_text: str):
    if not GIFT_FB_PSID:
        return
    alert = (
        "🔴 GRADE A LEAD ใหม่! (Facebook Page)\n"
        f"Sender: {sender_id}\n"
        f"ข้อความ: {user_text[:100]}\n\n"
        "→ ติดต่อกลับใน Messenger ด่วนครับ 📞"
    )
    send_message(GIFT_FB_PSID, alert)


# ======================================================
# Routes
# ======================================================
@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "WEC Facebook Bot v1 — Running 🏠"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Facebook calls this once to verify the webhook URL"""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == FB_VERIFY_TOKEN:
        print("✅ Webhook verified by Facebook")
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def receive_webhook():
    """Receive messages from Facebook"""
    body = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not verify_fb_signature(body, signature):
        return "Unauthorized", 401

    data = request.get_json(silent=True)
    if not data or data.get("object") != "page":
        return jsonify({"status": "ignored"})

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            # Skip echo messages (sent by Page itself)
            if event.get("message", {}).get("is_echo"):
                continue

            message = event.get("message", {})
            if not message or not message.get("text"):
                continue

            sender_id = event.get("sender", {}).get("id", "")
            user_text = message["text"]

            # Process through bot
            reply_text, lead_grade = bot.process(user_text, sender_id)

            # Reply to user
            send_message(sender_id, reply_text)

            # Alert Gift if Grade A
            if lead_grade == "A":
                alert_gift(sender_id, user_text)

            print(f"[{sender_id[:8]}...] Grade={lead_grade or '-'} | Q={user_text[:40]!r}")

    return jsonify({"status": "ok"})


# ======================================================
# Main
# ======================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"🚀 WEC Bot starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
