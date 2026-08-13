# bot_logic.py — WEC Sales Bot Phase 4.0
# Core engine: FAQ -> Qualification (Q1-Q4) -> Grading (A/B/C) -> Claude AI fallback
#
# Phase 3.x (เดิม) ดูประวัติท้ายไฟล์
#
# ======================================================================
# Phase 4.0 (2026-08-12) — "จำได้ ≠ ทำต่อ"
# ----------------------------------------------------------------------
# ปัญหาที่แก้ (จากหน้างานจริง):
#   A) state เก็บใน RAM -> เซิร์ฟเวอร์ restart/หลับ = ลูกค้าโดนถามใหม่ทั้งชุด
#   B) resume ดื้อๆ ก็ผิด -> ลูกค้าหายไป 3 วันแล้วกลับมาถามเรื่องใหม่
#      แต่บอทตอบคำถามค้างเมื่อ 3 วันก่อน = เหมือนไม่ฟัง
#   C) ส่งลีดเข้าชีตเฉพาะตอนจบ Q4 -> คนตอบ 3 ข้อแล้วหาย = ไม่มีใครรู้ว่ามีตัวตน
#   D) welcome + คำถาม อยู่บับเบิลเดียวกัน -> คำถามถูกสแกนผ่าน
#
# หลักการใหม่:
#   1. ข้อมูลที่ได้มาแล้ว (objective/income/debt/contact) จำถาวร ห้ามถามซ้ำ
#   2. ลำดับคำถามไม่ผูกกับของเก่า -> ตัดสินใจใหม่ทุกครั้งจาก "ข้อความล่าสุด"
#   3. ลูกค้าพูดอะไร ตอบอันนั้นก่อนเสมอ แล้วค่อยพ่วงคำถามที่ขาด 1 ข้อ
#   4. ปรับน้ำเสียงตามระยะที่หายไป (live / same_day / cold)
#   5. ส่งลีดเข้าชีตตั้งแต่คำตอบแรก แล้วอัปเดตแถวเดิม
#   6. ทุก turn เข้า Chat_Log เพื่อวัด funnel + fallback rate
#   7. คำถามแยกบับเบิลเสมอ (MSG_SPLIT)
#
# ความปลอดภัยในการ deploy:
#   ทุกอย่างที่คุยกับ Apps Script ตัวใหม่ ถูกกั้นด้วย env FEATURE_PERSIST=1
#   deploy โค้ดก่อน (flag ปิด) -> อัป Apps Script -> ค่อยเปิด flag
#   flag ปิด = พฤติกรรมเหมือน Phase 3.3 ทุกประการ ยกเว้นข้อ 1-4 ที่ทำงานบน RAM
# ======================================================================

import os
import re
import json
import time
import queue
import hashlib
import threading
import requests
from faq_data import (
    FAQ_DATABASE, WEC_SYSTEM_PROMPT, QUALIFY_QUESTIONS, QUALIFY_TRIGGERS,
    DISQUALIFY_MSG, DISQUALIFY_KEYWORDS, WELCOME_MSG, FALLBACK_MSG,
    MSG_SPLIT, RETURNING_MSG, DONE_MSG, STATUS_MSG,
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-3-5-haiku-20241022")
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "")
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
FB_GRAPH_URL = "https://graph.facebook.com/v19.0"

# เปิดฟีเจอร์จำถาวร/Chat_Log/ลีดบางส่วน (ต้องอัป Apps Script ก่อน)
FEATURE_PERSIST = os.environ.get("FEATURE_PERSIST", "0") == "1"
# salt สำหรับ hash PSID ก่อนลง Chat_Log (PDPA — ไม่เก็บ id ดิบ)
PSID_SALT = os.environ.get("PSID_SALT", "wec-2026")

# RAM cache (ยังใช้เป็นชั้นแรกเสมอ เพื่อความเร็ว)
_conversations: dict[str, list] = {}
_lead_states: dict[str, dict] = {}

# ระยะเวลาที่ถือว่า "ยังอยู่ในบทสนทนาเดียวกัน" / "วันเดียวกัน"
GAP_LIVE = 30 * 60          # 30 นาที
GAP_SAME_DAY = 24 * 60 * 60  # 1 วัน

# ลำดับข้อมูลที่ต้องเก็บ -> index ใน QUALIFY_QUESTIONS
FIELD_ORDER = ["objective", "income", "debt", "contact"]
FIELD_Q_INDEX = {"objective": 0, "income": 1, "debt": 2, "contact": 3}

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⭐✅❌️]+"
)

# คำที่บอกว่าข้อความนี้ "เป็นคำถาม" ไม่ใช่คำตอบ
_QUESTION_HINTS = [
    "ไหม", "มั้ย", "หรือเปล่า", "รึเปล่า", "เท่าไหร่", "เท่าไร", "กี่",
    "อะไร", "ยังไง", "อย่างไร", "ทำไม", "ที่ไหน", "เมื่อไหร่", "ไหน",
    "ขอถาม", "สอบถาม", "?",
]


# ======================================================================
# คิวเขียนข้อมูลแบบไม่บล็อกการตอบ (เรียงลำดับด้วย worker เดียว)
# บอทต้องไม่ช้าลงเพราะการเก็บ log — และต้องไม่พังถ้า Apps Script ล่ม
# ======================================================================
_PERSIST_Q: "queue.Queue[dict]" = queue.Queue(maxsize=500)


def _persist_worker():
    while True:
        payload = _PERSIST_Q.get()
        try:
            if APPS_SCRIPT_URL:
                r = requests.post(APPS_SCRIPT_URL, json=payload, timeout=15)
                if r.status_code != 200:
                    print(f"[PERSIST] HTTP {r.status_code} {r.text[:120]}")
        except Exception as e:
            print(f"[PERSIST ERROR] {e}")
        finally:
            _PERSIST_Q.task_done()


threading.Thread(target=_persist_worker, daemon=True, name="wec-persist").start()


def _enqueue(payload: dict):
    """ยิงงานเขียนเข้าคิว — ถ้าคิวเต็มก็ทิ้ง ดีกว่าทำให้บอทค้าง"""
    if not (FEATURE_PERSIST and APPS_SCRIPT_URL):
        return
    try:
        _PERSIST_Q.put_nowait(payload)
    except queue.Full:
        print("[PERSIST] queue full — dropped")


def _hash_psid(psid: str) -> str:
    return hashlib.sha256((PSID_SALT + psid).encode()).hexdigest()[:12]


def _now() -> int:
    return int(time.time())


class BotEngine:
    # ==================================================================
    # Entry point
    # ==================================================================
    def process(self, user_message: str, user_id: str,
                referral: dict | None = None,
                platform: str = "facebook") -> tuple[str, str | None]:
        """คืนค่า (ข้อความตอบ, grade หรือ None)

        ข้อความตอบอาจมี MSG_SPLIT คั่น -> main.py แยกส่งเป็นหลายบับเบิล
        """
        referral = referral or {}
        state, is_new = self._resolve_state(user_id, platform, referral)

        gap = _now() - state.get("last_seen", _now())
        bucket = self._gap_bucket(gap) if not is_new else "new"
        state["last_seen"] = _now()
        state["platform"] = platform
        if referral:
            state["referral"] = referral

        bubbles, grade = self._decide(user_message, user_id, state, bucket, is_new)

        reply = MSG_SPLIT.join([b for b in bubbles if b and b.strip()])
        self._log(user_id, user_message, reply)
        self._persist(user_id, state, user_message, reply, bucket)
        return reply, grade

    # ==================================================================
    # ตัดสินใจว่าจะตอบอะไร — หัวใจของ Phase 4
    # ==================================================================
    def _decide(self, msg: str, user_id: str, state: dict,
                bucket: str, is_new: bool) -> tuple[list[str], str | None]:
        data = state["data"]
        awaiting = state.get("awaiting")
        bubbles: list[str] = []
        grade = None

        # ---- 1) เปิดหัวข้อความตามระยะที่หายไป -------------------------
        if is_new:
            bubbles.append(WELCOME_MSG)
        elif bucket == "cold" and data:
            # หายไปเกิน 1 วัน + เคยให้ข้อมูลไว้ -> ทวนให้ฟังว่าเราจำได้
            recap = self._recap(data)
            if recap:
                bubbles.append(RETURNING_MSG.format(recap=recap))

        # ---- 2) ลูกค้าพูดอะไร ตอบอันนั้นก่อนเสมอ ----------------------
        # ถ้ากำลังรอคำตอบอยู่ แต่ลูกค้าดันถามคำถามกลับมา
        # ห้ามเอาคำถามของเขาไปนับเป็นคำตอบ — ตอบเขาก่อน แล้วค่อยถามใหม่
        consumed = False
        if awaiting and not self._is_question(msg) and self._is_valid_answer(awaiting, msg):
            self._capture(state, awaiting, msg)
            consumed = True
            state["awaiting"] = None
            # ตอบครบทุกข้อพอดี -> ปิดการขาย
            if awaiting == "contact":
                grade = self._finish(user_id, state, msg)
                bubbles.append(self._grade_reply(grade, msg))
                return bubbles, grade

        if not consumed:
            faq = self._check_faq(msg)
            if faq:
                bubbles.append(faq)
            elif awaiting:
                # กำลังรอคำตอบอยู่ แต่ข้อความไม่ใช่ทั้งคำตอบและไม่เข้า FAQ
                # -> ให้ Claude ตอบ แล้วค่อยถามซ้ำในบับเบิลถัดไป
                bubbles.append(self._ask_claude(msg, user_id))
            elif state.get("done"):
                # ลูกค้าให้ข้อมูลครบไปแล้ว — ห้ามขอเบอร์/ถามชุดเดิมซ้ำเด็ดขาด
                bubbles.append(STATUS_MSG if self._is_status_ask(msg)
                               else self._ask_claude(msg, user_id, done=True))
            elif not self._should_qualify(msg) and not is_new:
                bubbles.append(self._ask_claude(msg, user_id))

        # ---- 3) พ่วงคำถามที่ยังขาด 1 ข้อ (บับเบิลแยก) -----------------
        # เริ่มถามเมื่อ: ลูกค้าแสดงความสนใจ / เคยเข้าโหมดถามแล้ว / เพิ่งตอบไป 1 ข้อ
        if not state.get("done"):
            if self._should_qualify(msg) or state.get("qualifying") or consumed:
                state["qualifying"] = True
                field, question = self._next_missing(data)
                if field:
                    state["awaiting"] = field
                    bubbles.append(question)
                else:
                    # ข้อมูลครบแต่ยังไม่ได้ปิด (เช่นโหลด state เก่ามา)
                    grade = self._finish(user_id, state, data.get("contact", ""))
                    bubbles.append(DONE_MSG)

        # กันเคสไม่มีอะไรจะพูดเลย
        if not bubbles:
            bubbles.append(FALLBACK_MSG)
        return bubbles, grade

    # ==================================================================
    # State: RAM -> ชีต -> สร้างใหม่
    # ==================================================================
    def _blank_state(self, platform: str, referral: dict) -> dict:
        return {
            "data": {}, "awaiting": None, "qualifying": False, "done": False,
            "referral": referral or {}, "platform": platform,
            "last_seen": _now(), "lead_sent": False,
        }

    def _resolve_state(self, user_id: str, platform: str,
                       referral: dict) -> tuple[dict, bool]:
        if user_id in _lead_states:
            return _lead_states[user_id], False

        # RAM ไม่มี — เซิร์ฟเวอร์เพิ่ง restart หรือเป็นลูกค้าใหม่จริง
        loaded = self._load_session(user_id)
        if loaded:
            loaded.setdefault("data", {})
            loaded.setdefault("awaiting", None)
            loaded["psid"] = user_id
            _lead_states[user_id] = loaded
            _conversations.setdefault(user_id, [])
            print(f"[SESSION] restored {user_id[:8]}... "
                  f"fields={list(loaded.get('data', {}).keys())}")
            return loaded, False

        state = self._blank_state(platform, referral)
        state["psid"] = user_id
        # ลูกค้าใหม่ = มาจากเพจ/แอดด้วยเหตุผลบางอย่างเสมอ -> เข้าโหมดถามเลย
        # (ไม่ทิ้งคำถามลูกค้า และไม่ปล่อยให้จบแค่ "สวัสดีครับ")
        state["qualifying"] = True
        _lead_states[user_id] = state
        _conversations[user_id] = []
        return state, True

    def _load_session(self, user_id: str) -> dict | None:
        if not (FEATURE_PERSIST and APPS_SCRIPT_URL):
            return None
        try:
            r = requests.post(
                APPS_SCRIPT_URL,
                json={"action": "get_session", "psid": user_id},
                timeout=2.5,
            )
            if r.status_code != 200:
                return None
            j = r.json()
            st = j.get("state")
            return st if isinstance(st, dict) and st else None
        except Exception as e:
            print(f"[SESSION LOAD ERROR] {e}")
            return None

    def _persist(self, user_id: str, state: dict,
                 user_msg: str, reply: str, bucket: str):
        """เขียน session + Chat_Log ลงชีต (ไม่บล็อกการตอบ)"""
        h = _hash_psid(user_id)
        rows = [
            {"role": "user", "text": user_msg},
            {"role": "bot", "text": reply.replace(MSG_SPLIT, " | ")},
        ]
        _enqueue({
            "action": "turn",
            "psid": user_id,
            "psid_hash": h,
            "platform": state.get("platform", ""),
            "gap_bucket": bucket,
            "awaiting": state.get("awaiting") or "",
            "stage": self._stage_label(state),
            "ad_id": (state.get("referral") or {}).get("ad_id", ""),
            "state": {
                "data": state.get("data", {}),
                "awaiting": state.get("awaiting"),
                "qualifying": state.get("qualifying", False),
                "done": state.get("done", False),
                "referral": state.get("referral", {}),
                "platform": state.get("platform", ""),
                "last_seen": state.get("last_seen", _now()),
                "lead_sent": state.get("lead_sent", False),
            },
            "log": rows,
        })

    @staticmethod
    def _stage_label(state: dict) -> str:
        if state.get("done"):
            return "done"
        aw = state.get("awaiting")
        if aw:
            return f"await_{aw}"
        return "qualifying" if state.get("qualifying") else "open"

    @staticmethod
    def _gap_bucket(gap: int) -> str:
        if gap < GAP_LIVE:
            return "live"
        if gap < GAP_SAME_DAY:
            return "same_day"
        return "cold"

    # ==================================================================
    # เก็บคำตอบ / หาคำถามที่ยังขาด / ทวนความจำ
    # ==================================================================
    def _capture(self, state: dict, field: str, msg: str):
        data = state["data"]
        data[field] = msg
        if field == "income" and self._is_disqualified(msg):
            # รายได้ไม่ชัด -> ข้าม Q3 (บูโร) ไม่ต้องถาม ไม่ทิ้งลีด
            data["income_unknown"] = True
            data.setdefault("debt", "-")
        state["data"] = data
        # ได้ข้อมูลใหม่ -> ส่งเข้าชีตทันที ไม่รอครบ 4 ข้อ (กันลีดหลุด)
        self._upsert_lead(state)

    def _next_missing(self, data: dict) -> tuple[str | None, str | None]:
        for f in FIELD_ORDER:
            if f == "debt" and data.get("income_unknown"):
                continue
            if not data.get(f):
                return f, QUALIFY_QUESTIONS[FIELD_Q_INDEX[f]]
        return None, None

    @staticmethod
    def _tidy(v: str) -> str:
        """ตัดคำลงท้ายสุภาพออกก่อนเอาไปทวน — กัน 'ครับ' ซ้อนกันสองที"""
        v = (v or "").strip()
        for tail in ("ครับผม", "ครับ", "ค่ะ", "คะ", "ค่า", "จ้า", "นะ"):
            while v.endswith(tail):
                v = v[: -len(tail)].strip()
        return v.strip(" .,")

    def _recap(self, data: dict) -> str:
        parts = []
        obj = self._tidy(data.get("objective", ""))
        inc = self._tidy(data.get("income", ""))
        if obj:
            parts.append(f"ลูกค้าสนใจ{obj}")
        if inc and not data.get("income_unknown"):
            parts.append(inc)
        return " ".join(parts)

    # ==================================================================
    # ตรวจว่าข้อความเป็นคำถาม / เป็นคำตอบที่ใช้ได้
    # ==================================================================
    @staticmethod
    def _is_question(msg: str) -> bool:
        m = msg.lower()
        return any(k in m for k in _QUESTION_HINTS)

    @staticmethod
    def _is_status_ask(msg: str) -> bool:
        m = msg.lower()
        return any(k in m for k in ["ยังไม่โทร", "เมื่อไหร่จะโทร", "ติดต่อกลับ", "รอ"])

    @staticmethod
    def _is_valid_answer(field: str, msg: str) -> bool:
        """กันเก็บข้อความมั่วเป็นคำตอบ — โดยเฉพาะช่องเบอร์ติดต่อ"""
        m = msg.strip()
        if len(m) < 1:
            return False
        if field == "contact":
            digits = sum(c.isdigit() for c in m)
            return digits >= 6 or "line" in m.lower() or "ไลน์" in m
        return True

    # ==================================================================
    # FAQ / Trigger / Disqualify  (เหมือนเดิม)
    # ==================================================================
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

    # ==================================================================
    # ปิดเคส + เกรด
    # ==================================================================
    def _finish(self, user_id: str, state: dict, contact: str) -> str:
        data = state["data"]
        grade = "C" if data.get("income_unknown") else self._grade(data)
        state["done"] = True
        state["awaiting"] = None
        data["grade"] = grade
        fb_name = self._get_fb_name(user_id, state.get("platform", "facebook"))
        state["fb_name"] = fb_name
        # ส่งชุดเต็ม (แถวสมบูรณ์ + สร้างนัดในปฏิทิน) — schema เดิม ไม่แตะ
        self._send_to_sheets(user_id, data, grade, fb_name,
                             state.get("referral", {}),
                             state.get("platform", "facebook"))
        state["lead_sent"] = True
        return grade

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

    # ==================================================================
    # Claude AI Fallback  (เหมือนเดิม)
    # ==================================================================
    def _ask_claude(self, user_message: str, user_id: str,
                    done: bool = False) -> str:
        # done=True -> ลูกค้าให้เบอร์ไปแล้ว ห้ามตอบอะไรที่เป็นการขอเบอร์ซ้ำ
        if not ANTHROPIC_API_KEY:
            return STATUS_MSG if done else FALLBACK_MSG
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
            text = self._sanitize(resp.json()["content"][0]["text"].strip())
            if done and any(k in text for k in ["ขอเบอร์", "ID LINE", "เบอร์ติดต่อ"]):
                # กัน AI เผลอขอข้อมูลที่ลูกค้าให้ไปแล้ว
                return STATUS_MSG
            return text or (STATUS_MSG if done else FALLBACK_MSG)
        except Exception as e:
            print(f"[CLAUDE EXCEPTION] {e}")
            return STATUS_MSG if done else FALLBACK_MSG

    @staticmethod
    def _sanitize(text: str) -> str:
        text = _EMOJI_RE.sub("", text)
        text = text.replace("**", "").replace("###", "").replace("##", "")
        text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
        return text.strip()

    # ==================================================================
    # Facebook / Instagram / Google Sheets
    # ==================================================================
    def _get_fb_name(self, user_id: str, platform: str = "facebook") -> str:
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

    def _upsert_lead(self, state: dict):
        """ส่งลีด 'บางส่วน' เข้าชีตทันทีที่ได้ข้อมูลใหม่ — กันลีดหลุด
        ใช้ action แยก เพื่อไม่ให้ Apps Script ตัวเก่าเผลอสร้างนัดในปฏิทินซ้ำ
        """
        data = state["data"]
        _enqueue({
            "action": "lead_partial",
            "facebook_psid": state.get("psid", ""),
            "objective": data.get("objective", ""),
            "income": data.get("income", ""),
            "debt": data.get("debt", ""),
            "contact": data.get("contact", ""),
            "ad_id": (state.get("referral") or {}).get("ad_id", ""),
            "ref": (state.get("referral") or {}).get("ref", ""),
            "source": ("Instagram DM" if state.get("platform") == "instagram"
                       else "Facebook Messenger"),
        })

    def _send_to_sheets(self, user_id: str, data: dict, grade: str,
                        fb_name: str = "", referral: dict | None = None,
                        platform: str = "facebook"):
        """POST lead ชุดเต็มไป Apps Script -> Sheets + Calendar (schema เดิม)"""
        if not APPS_SCRIPT_URL:
            print("[SHEETS] APPS_SCRIPT_URL not set — skipped")
            return
        referral = referral or {}
        payload = {
            "facebook_psid": user_id,
            "fb_name":       fb_name,
            "objective":     data.get("objective", ""),
            "income":        data.get("income", ""),
            "debt":          data.get("debt", ""),
            "contact":       data.get("contact", ""),
            "grade":         grade,
            "ad_id":         referral.get("ad_id", ""),
            "ref":           referral.get("ref", ""),
            "source":        "Instagram DM" if platform == "instagram" else "Facebook Messenger",
        }
        try:
            resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=10)
            print(f"[SHEETS] {resp.status_code} {resp.text[:120]}")
        except Exception as e:
            print(f"[SHEETS ERROR] {e}")

    # ==================================================================
    def _log(self, user_id: str, user_msg: str, reply: str):
        h = _conversations.get(user_id, [])
        h.append({"role": "user", "content": user_msg})
        h.append({"role": "assistant", "content": reply.replace(MSG_SPLIT, "\n")})
        if len(h) > 20:
            h = h[-20:]
        _conversations[user_id] = h


# ======================================================================
# ประวัติเวอร์ชันก่อนหน้า
# Phase 3   : Claude fallback ใช้งานจริง / key ข้อมูลชัดเจน / ตัด emoji / รับ referral
# Phase 3.1 : รองรับ Instagram DM (platform tag)
# Phase 3.2 : ทักครั้งแรกไม่ทิ้งคำถามลูกค้า
# Phase 3.3 : รายได้ไม่ชัด ไม่ทิ้งลีด — ข้าม Q3 ไปขอ contact แล้วให้เกรด C
# ======================================================================
