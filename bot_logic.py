# bot_logic.py — WEC Sales Bot Phase 3.1
# Core engine: FAQ -> Qualification (Q1-Q4) -> Grading (A/B/C) -> Claude AI fallback
#
# Phase 3 changes:
# 1. Claude API fallback ทำงานจริง (Phase 2 ประกาศ key ไว้แต่ไม่เคยเรียกใช้)
# 2. ชื่อ key ข้อมูลชัดเจน: objective / income / debt / contact
# 3. ตัด emoji ออกทั้งหมดตาม TONE_RULES.md ล่าสุด
# 4. รับ referral (ad_id / ref) จาก main.py และส่งต่อไป Google Sheets
#
# Phase 3.1 changes:
# 5. รองรับ Instagram DM — platform tag ("facebook" / "instagram")
#    ส่ง source เข้า Google Sheets + ดึงชื่อ IG (name/username)

import os
import re
import requests
from faq_data import (
    FAQ_DATABASE, WEC_SYSTEM_PROMPT, QUALIFY_QUESTIONS, QUALIFY_TRIGGERS,
    DISQUALIFY_MSG, DISQUALIFY_KEYWORDS, WELCOME_MSG, FALLBACK_MSG,
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-3-5-haiku-20241022")
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "")
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
FB_GRAPH_URL = "https://graph.facebook.com/v19.0"

# In-memory state (รีเซ็ตเมื่อ restart — Phase 4: Redis)
_conversations: dict[str, list] = {}
_lead_states: dict[str, dict] = {}

# Regex ลบ emoji / สัญลักษณ์ ออกจากคำตอบ Claude (กัน AI เผลอใส่)
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⭐✅❌️]+"
)


class BotEngine:
    # --------------------------------------------------
    # Entry point
    # --------------------------------------------------
    def process(self, user_message: str, user_id: str,
                referral: dict | None = None,
                platform: str = "facebook") -> tuple[str, str | None]:
        """คืนค่า (ข้อความตอบ, grade หรือ None) — platform: facebook / instagram"""
        referral = referral or {}

        # ทักครั้งแรก
        if user_id not in _conversations:
            _conversations[user_id] = []
            _lead_states[user_id] = {"qualify_step": 0, "data": {},
                                     "referral": referral, "platform": platform}
            self._log(user_id, user_message, WELCOME_MSG)
            return WELCOME_MSG, None

        state = _lead_states.setdefault(
            user_id, {"qualify_step": 0, "data": {}, "referral": {}, "platform": platform})
        state["platform"] = platform
        # อัพเดต referral ถ้าเพิ่งได้มา (เช่น กดโฆษณาหลังคุยไปแล้ว)
        if referral:
            state["referral"] = referral

        # อยู่ในขั้นตอน Q1-Q4
        if state["qualify_step"] > 0:
            return self._handle_qualify(user_message, user_id)

        # FAQ ก่อน (ฟรี ตอบทันที)
        faq = self._check_faq(user_message)
        if faq:
            self._log(user_id, user_message, faq)
            return faq, None

        # เจอ keyword สนใจ -> เริ่ม Q1
        if self._should_qualify(user_message):
            state["qualify_step"] = 1
            _lead_states[user_id] = state
            q1 = QUALIFY_QUESTIONS[0]
            self._log(user_id, user_message, q1)
            return q1, None

        # ไม่เข้า FAQ / ไม่ trigger -> Claude AI fallback
        reply = self._ask_claude(user_message, user_id)
        self._log(user_id, user_message, reply)
        return reply, None

    # --------------------------------------------------
    # FAQ / Trigger / Disqualify
    # --------------------------------------------------
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

    def _is_disqualified(self, message: str) -> bool:
        msg = message.lower()
        return any(kw in msg for kw in DISQUALIFY_KEYWORDS)

    # --------------------------------------------------
    # Qualification Flow Q1-Q4
    # data keys: objective (Q1) / income (Q2) / debt (Q3) / contact (Q4)
    # --------------------------------------------------
    def _handle_qualify(self, user_message: str, user_id: str) -> tuple[str, str | None]:
        state = _lead_states[user_id]
        step = state["qualify_step"]
        data = state["data"]

        if step == 1:
            data["objective"] = user_message
            state["qualify_step"] = 2
            reply = QUALIFY_QUESTIONS[1]

        elif step == 2:
            # Q2 = อาชีพ + รายได้ — คัดออกถ้าไม่มีรายได้ประจำ
            if self._is_disqualified(user_message):
                state["qualify_step"] = 0
                state["data"] = {}
                _lead_states[user_id] = state
                self._log(user_id, user_message, DISQUALIFY_MSG)
                return DISQUALIFY_MSG, None
            data["income"] = user_message
            state["qualify_step"] = 3
            reply = QUALIFY_QUESTIONS[2]

        elif step == 3:
            data["debt"] = user_message
            state["qualify_step"] = 4
            reply = QUALIFY_QUESTIONS[3]

        elif step == 4:
            data["contact"] = user_message
            grade = self._grade(data)
            reply = self._grade_reply(grade, user_message)

            # ส่ง Lead เข้า Google Sheets + Calendar
            fb_name = self._get_fb_name(user_id, state.get("platform", "facebook"))
            self._send_to_sheets(user_id, data, grade, fb_name,
                                 state.get("referral", {}),
                                 state.get("platform", "facebook"))

            state["qualify_step"] = 0
            state["data"] = {}
            _lead_states[user_id] = state
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

    # --------------------------------------------------
    # Grading
    # --------------------------------------------------
    def _grade(self, data: dict) -> str:
        income_ans = data.get("income", "").lower()
        high_income = any(x in income_ans for x in
                          ["แสน", "100,", "150,", "200,", "100000", "150000", "200000"])
        med_income = any(x in income_ans for x in
                         ["3", "4", "5", "6", "7", "8", "9",
                          "สาม", "สี่", "ห้า", "หก", "เจ็ด", "แปด", "เก้า",
                          "30,", "40,", "50,", "60,", "70,", "80,", "90,"])
        if high_income:
            return "A"
        elif med_income:
            return "B"
        return "C"

    def _grade_reply(self, grade: str, contact: str = "") -> str:
        # ถ้าลูกค้าระบุเวลานัดเอง -> ยืนยันตามนั้น
        time_keywords = [
            "พรุ่งนี้", "มะรืน", "วันนี้", "เช้า", "บ่าย", "เย็น", "ค่ำ", "ตี",
            "โมง", "นาฬิกา", "ช่วง", "หลัง", "ก่อน", "สัปดาห์",
            "จันทร์", "อังคาร", "พุธ", "พฤหัส", "ศุกร์", "เสาร์", "อาทิตย์",
        ]
        if any(kw in contact for kw in time_keywords):
            return "ขอบคุณครับ ที่ปรึกษาจะโทรกลับตามเวลาที่นัดหมายครับ"
        if grade == "A":
            return "ขอบคุณครับ ที่ปรึกษาจะโทรกลับหาลูกค้าภายใน 30 นาทีครับ"
        elif grade == "B":
            return "ขอบคุณครับ ที่ปรึกษาจะโทรกลับภายใน 1-2 ชั่วโมง (09:00-18:00 น.) ครับ"
        return "ขอบคุณครับ ทีมงานจะติดต่อกลับหาลูกค้าในเร็วๆ นี้ครับ"

    # --------------------------------------------------
    # Claude AI Fallback
    # --------------------------------------------------
    def _ask_claude(self, user_message: str, user_id: str) -> str:
        """เรียก Claude API ตอบคำถามที่ไม่เข้า FAQ — ตามกฎ TONE_RULES"""
        if not ANTHROPIC_API_KEY:
            return FALLBACK_MSG

        history = _conversations.get(user_id, [])[-10:]
        messages = history + [{"role": "user", "content": user_message}]

        try:
            resp = requests.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 150,
                    "system": WEC_SYSTEM_PROMPT,
                    "messages": messages,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"[CLAUDE ERROR] {resp.status_code}: {resp.text[:200]}")
                return FALLBACK_MSG
            text = resp.json()["content"][0]["text"].strip()
            return self._sanitize(text) or FALLBACK_MSG
        except Exception as e:
            print(f"[CLAUDE EXCEPTION] {e}")
            return FALLBACK_MSG

    @staticmethod
    def _sanitize(text: str) -> str:
        """บังคับกฎ tone: ตัด emoji + markdown ออก"""
        text = _EMOJI_RE.sub("", text)
        text = text.replace("**", "").replace("###", "").replace("##", "")
        text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
        return text.strip()

    # --------------------------------------------------
    # Facebook / Instagram / Google Sheets
    # --------------------------------------------------
    def _get_fb_name(self, user_id: str, platform: str = "facebook") -> str:
        """ดึงชื่อจาก Graph API — Facebook PSID หรือ Instagram IGSID"""
        if not FB_PAGE_ACCESS_TOKEN:
            return ""
        fields = "name,username" if platform == "instagram" else "name"
        try:
            resp = requests.get(
                f"{FB_GRAPH_URL}/{user_id}",
                params={"fields": fields, "access_token": FB_PAGE_ACCESS_TOKEN},
                timeout=5,
            )
            j = resp.json()
            return j.get("name") or j.get("username") or ""
        except Exception as e:
            print(f"[NAME ERROR] ({platform}) {e}")
            return ""

    def _send_to_sheets(self, user_id: str, data: dict, grade: str,
                        fb_name: str = "", referral: dict | None = None,
                        platform: str = "facebook"):
        """POST lead ไป Google Apps Script -> Sheets + Calendar (schema เดิม)"""
        if not APPS_SCRIPT_URL:
            print("[SHEETS] APPS_SCRIPT_URL not set — skipped")
            return
        referral = referral or {}
        payload = {
            "facebook_psid": user_id,
            "fb_name":       fb_name,
            "objective":     data.get("objective", ""),  # Q1
            "income":        data.get("income", ""),     # Q2
            "debt":          data.get("debt", ""),       # Q3
            "contact":       data.get("contact", ""),    # Q4
            "grade":         grade,
            "ad_id":         referral.get("ad_id", ""),  # จากโฆษณา (ถ้ามี)
            "ref":           referral.get("ref", ""),
            # แหล่งที่มา — Apps Script v3 ใช้ field นี้ (v2 จะ ignore ไม่พัง)
            "source":        "Instagram DM" if platform == "instagram" else "Facebook Messenger",
        }
        try:
            resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=10)
            print(f"[SHEETS] {resp.status_code} {resp.text[:120]}")
        except Exception as e:
            print(f"[SHEETS ERROR] {e}")

    # --------------------------------------------------
    def _log(self, user_id: str, user_msg: str, reply: str):
        h = _conversations.get(user_id, [])
        h.append({"role": "user", "content": user_msg})
        h.append({"role": "assistant", "content": reply})
        if len(h) > 20:
            h = h[-20:]
        _conversations[user_id] = h
