# bot_logic.py — WEC Sales Bot Phase 1
# Core bot engine: FAQ + Qualification + Claude AI via direct HTTP

import os
import json
import requests
from faq_data import FAQ_DATABASE, WEC_SYSTEM_PROMPT, QUALIFY_QUESTIONS, QUALIFY_TRIGGERS

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

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

        # Claude AI fallback
        reply = self._claude(user_message, user_id)
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
            data["debt"] = user_message
            state["qualify_step"] = 0
            state["data"] = {}
            _lead_states[user_id] = state
            grade = self._grade(data)
            reply = self._grade_reply(grade)
            self._log(user_id, user_message, reply)
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

    def _claude(self, user_message: str, user_id: str) -> str:
        if not ANTHROPIC_API_KEY:
            return "ขอบคุณที่ถามครับ คำถามนี้ขอให้ที่ปรึกษาตอบตรงๆ จะดีกว่าครับ พิมพ์ 'ติดต่อ' เพื่อให้ทีมงานโทรกลับได้เลยครับ"

        history = _conversations.get(user_id, [])
        history.append({"role": "user", "content": user_message})
        if len(history) > 20:
            history = history[-20:]

        try:
            resp = requests.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5",
                    "max_tokens": 150,
                    "system": WEC_SYSTEM_PROMPT,
                    "messages": history,
                },
                timeout=15,
            )
            data = resp.json()
            reply = data["content"][0]["text"]
        except Exception as e:
            print(f"Claude API error: {e}")
            reply = "ขออภัยครับ ระบบมีปัญหาชั่วคราว กรุณาทักใหม่ หรือพิมพ์ 'ติดต่อ' ให้ทีมโทรกลับครับ"

        history.append({"role": "assistant", "content": reply})
        _conversations[user_id] = history
        return reply

    def _log(self, user_id: str, user_msg: str, reply: str):
        h = _conversations.get(user_id, [])
        h.append({"role": "user", "content": user_msg})
        h.append({"role": "assistant", "content": reply})
        if len(h) > 20:
            h = h[-20:]
        _conversations[user_id] = h
