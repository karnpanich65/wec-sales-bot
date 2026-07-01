# bot_logic.py — WEC Sales Bot Phase 1
# Core bot engine: FAQ + Qualification + Claude AI via direct HTTP

import os
import json
import requests
from faq_data import FAQ_DATABASE, WEC_SYSTEM_PROMPT, QUALIFY_QUESTIONS, QUALIFY_TRIGGERS

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "")

# In-memory state (resets on server restart — Phase 2: use Redis)
_conversations: dict[str, list] = {}
_lead_states: dict[str, dict] = {}


class BotEngine:
    def process(self, user_message: str, user_id: str) -> tuple[str, str | None]:
        """Returns (reply_text, lead_grade or None)"""
        # First contact
        if user_id not in _conversations:
            _conversations[user_id] = []
            _lead_states[user_id] = {"qualify_step": 0, "data": {}}
            return self._welcome(), None

        state = _lead_states.get(user_id, {"qualify_step": 0, "data": {}})

        # Active qualification flow
        if state["qualify_step"] > 0:
            return self._handle_qualify(user_message, user_id)

        # FAQ fast path (free, instant)
        faq = self._check_faq(user_message)
        if faq:
            self._log(user_id, user_message, faq)
            return faq, None

        # Trigger qualification if intent detected
        if self._should_qualify(user_message):
            state["qualify_step"] = 1
            _lead_states[user_id] = state
            intro = QUALIFY_QUESTIONS[0]
            self._log(user_id, user_message, intro)
            return intro, None

        # Fallback: ไม่ว่าจะพูดอะไร → ขอเบอร์ติดต่อทันที (ไม่ใช้ Claude AI)
        reply = "รบกวนขอเบอร์ติดต่อหรือ ID LINE เพื่อให้ที่ปรึกษาโทรกลับหาลูกค้าโดยตรงครับ"
        self._log(user_id, user_message, reply)
        return reply, None

    # --------------------------------------------------
    def _welcome(self) -> str:
        return "สวัสดีครับ Wealth Estate : อสังหาคุ้มค่า ยินดีให้คำปรึกษาครับ"

    def _check_faq(self, message: str) -> str | None:
        msg = message.lower().strip()
        for faq in FAQ_DATABASE:
            for kw in faq["keywords"]:
                if kw in msg:
                    return faq["answer"]
        return None

    def _should_qualify(self, message: str) -> bool:
        msg = message.lower()
        return any(t in msg for t in QUALIFY_TRIGGERS)

    def _handle_qualify(self, user_message: str, user_id: str) -> tuple[str, str | None]:
        state = _lead_states[user_id]
        step = state["qualify_step"]
        data = state["data"]

        if step == 1:
            data["objective"] = user_message
            state["qualify_step"] = 2
            reply = QUALIFY_QUESTIONS[1]
        elif step == 2:
            data["budget"] = user_message
            state["qualify_step"] = 3
            reply = QUALIFY_QUESTIONS[2]
        elif step == 3:
            data["income"] = user_message
            state["qualify_step"] = 4
            reply = QUALIFY_QUESTIONS[3]
        elif step == 4:
            data["debt"] = user_message  # Q4 = contact (phone / LINE ID)
            state["qualify_step"] = 0
            state["data"] = {}
            _lead_states[user_id] = state
            grade = self._grade(data)
            reply = self._grade_reply(grade)
            self._log(user_id, user_message, reply)
            # ส่งข้อมูล Lead ไป Google Sheets + สร้าง Calendar event
            self._send_to_sheets(user_id, data, grade)
            return reply, grade
        else:
            state["qualify_step"] = 0
            _lead_states[user_id] = state
            return "ขออภัยครับ มีปัญหาชั่วคราว ทักใหม่ได้เลยครับ", None

        state["data"] = data
        _lead_states[user_id] = state
        self._log(user_id, user_message, reply)
        return reply, None

    def _grade(self, data: dict) -> str:
        # Q2 answer (employment + income) is stored in 'budget' key
        income_ans = data.get("budget", "").lower()
        high_income = any(x in income_ans for x in ["แสน", "100,", "150,", "200,", "100000", "150000"])
        med_income = any(x in income_ans for x in ["3", "4", "5", "สาม", "สี่", "ห้า", "30,", "40,", "50,"])
        if high_income:
            return "A"
        elif med_income:
            return "B"
        return "C"

    def _grade_reply(self, grade: str) -> str:
        if grade == "A":
            return "ขอบคุณครับ ที่ปรึกษาจะโทรกลับหาลูกค้าภายใน 30 นาทีครับ"
        elif grade == "B":
            return "ขอบคุณครับ ที่ปรึกษาจะโทรกลับภายใน 1-2 ชั่วโมง (09:00-18:00 น.) ครับ"
        return "ขอบคุณครับ ทีมงานจะติดต่อกลับหาลูกค้าในเร็วๆ นี้ครับ"

    def _send_to_sheets(self, user_id: str, data: dict, grade: str):
        """POST lead data ไป Google Apps Script => Sheets + Calendar"""
        if not APPS_SCRIPT_URL:
            print("APPS_SCRIPT_URL not set — skipping Sheets/Calendar")
            return
        payload = {
            "facebook_psid": user_id,
            "objective": data.get("objective", ""),  # Q1: ลงทุนปล่อยเช่า / อยู่เอง
            "income":    data.get("budget", ""),     # Q2: อาชีพ + รายได้/เดือน
            "debt":      data.get("income", ""),     # Q3: ภาระหนี้ / บูโร
            "contact":   data.get("debt", ""),       # Q4: เบอร์โทร / LINE ID
            "grade":     grade,
        }
        try:
            resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=10)
            print(f"Sheets/Calendar: {resp.status_code} {resp.text[:120]}")
        except Exception as e:
            print(f"Sheets/Calendar error: {e}")

    def _log(self, user_id: str, user_msg: str, reply: str):
        h = _conversations.get(user_id, [])
        h.append({"role": "user", "content": user_msg})
        h.append({"role": "assistant", "content": reply})
        if len(h) > 20:
            h = h[-20:]
        _conversations[user_id] = h
