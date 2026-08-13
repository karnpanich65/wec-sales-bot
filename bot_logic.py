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
    DISQUALIFY_KEYWORDS, WELCOME_MSG, FALLBACK_MSG, BRAND_NAME,
    MSG_SPLIT, RETURNING_MSG, DONE_MSG, STATUS_MSG,
    CONTACT_REFUSED_MSG, CASH_BUYER_MSG, CASH_INVITE_MSG, TIER2_GUARD_MSG,
    LOW_INCOME_BAHT, NO_COBORROWER_MSG, COBORROWER_INVITE_MSG,
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
# co_borrower ถามเฉพาะเคสรายได้ต่ำกว่า LOW_INCOME_BAHT (ดู _next_missing)
FIELD_ORDER = ["objective", "income", "co_borrower", "debt", "contact"]
FIELD_Q_INDEX = {"objective": 0, "income": 1, "debt": 2, "contact": 3,
                 "co_borrower": 4}

# ----------------------------------------------------------------------
# อ่านตัวเลขรายได้จากภาษาคนพิมพ์จริง
# ----------------------------------------------------------------------
_THAI_DIGIT_WORDS = {
    "หนึ่ง": 1, "สอง": 2, "สาม": 3, "สี่": 4, "ห้า": 5,
    "หก": 6, "เจ็ด": 7, "แปด": 8, "เก้า": 9, "สิบ": 10,
}
_UNIT_MULT = {"พัน": 1000, "หมื่น": 10000, "แสน": 100000, "ล้าน": 1000000,
              "k": 1000}
_NO_COB_HINTS = ("ไม่มี", "ไม่ได้", "ไม่มีใคร", "คนเดียว", "ยื่นเดี่ยว",
                 "ไม่อยากให้ใคร", "ไม่สะดวกหา", "no")


def _parse_income(msg: str) -> int | None:
    """คืนรายได้ต่อเดือนเป็นบาท — อ่านไม่ออกคืน None (ห้ามเดา)"""
    m = (msg or "").replace(",", "").replace(" ", "").lower()
    # "2หมื่น" "18k" "3แสน"
    mt = re.search(r"(\d+(?:\.\d+)?)(พัน|หมื่น|แสน|ล้าน|k)", m)
    if mt:
        return int(float(mt.group(1)) * _UNIT_MULT[mt.group(2)])
    # "สองหมื่น" "สามหมื่นกว่า"
    for w, v in _THAI_DIGIT_WORDS.items():
        for unit, mult in _UNIT_MULT.items():
            if unit != "k" and (w + unit) in m:
                return v * mult
    mt = re.search(r"\d{4,7}", m)
    if mt:
        return int(mt.group(0))
    return None

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⭐✅❌️]+"
)

# ----------------------------------------------------------------------
# Phase 5.3 — ตัวจับคำที่รู้จักคำปฏิเสธ
# ----------------------------------------------------------------------
# ภาษาไทยไม่เว้นวรรค การเช็ค "คำนี้อยู่ในข้อความไหม" แบบตรงๆ จึงพังเสมอ:
#   "ไม่กู้"    มีคำว่า "กู้"    -> ระบบนับว่าลูกค้าคุยเรื่องกู้ (+3)
#   "ไม่สะดวก" มีคำว่า "สะดวก"  -> ระบบนับว่าลูกค้ายอมนัดดูห้อง (+5)
# ทั้งสองเคสให้คะแนนบวกกับคนที่กำลังปฏิเสธเรา -> เกรด A ผิดตัว
# แก้ที่รากเดียว: หาคำเจอแล้วต้องมองย้อนหลังก่อนว่ามีคำปฏิเสธนำหน้าอยู่ไหม
_NEG_PREFIXES = (
    "ไม่", "ยังไม่", "ไม่ได้", "ไม่ต้อง", "ไม่ค่อย", "ไม่อยาก", "ไม่ขอ",
    "ไม่ใช่", "เลิก", "หยุด", "งด",
)
_NEG_LOOKBACK = 10          # ตัวอักษรที่มองย้อนหลังจากตำแหน่งที่เจอคำ


def _hit(msg: str, word: str) -> bool:
    """เจอคำนี้แบบ "ไม่ถูกปฏิเสธ" ไหม
    "อยากดูห้อง"     + "ดูห้อง" -> True
    "ไม่อยากดูห้อง"  + "ดูห้อง" -> False
    """
    if not word:
        return False
    start = 0
    while True:
        i = msg.find(word, start)
        if i < 0:
            return False
        pre = msg[max(0, i - _NEG_LOOKBACK):i]
        if not any(pre.endswith(n) for n in _NEG_PREFIXES):
            return True     # เจอแบบไม่มีคำปฏิเสธนำหน้า = ของจริง
        start = i + 1       # ตัวนี้ถูกปฏิเสธ ไปหาตัวถัดไป


def _hit_any(msg: str, words) -> bool:
    """เจอคำใดคำหนึ่งแบบไม่ถูกปฏิเสธ"""
    return any(_hit(msg, w) for w in words)


# ----------------------------------------------------------------------
# Phase 4.3 — ชุดคำใบ้
# ----------------------------------------------------------------------
# ลูกค้าปฏิเสธการถูกโทร — ไม่ได้ปฏิเสธเรา
# ต้องยอมรับทันทีและห้ามขอช่องทางซ้ำ (เรามี PSID คุยต่อในแชทได้ตลอด)
# หมายเหตุ: ลิสต์นี้มีคำว่า "ไม่" อยู่ในตัวอยู่แล้ว -> ใช้ substring ตรงๆ
#          ห้ามใช้ _hit เด็ดขาด ไม่งั้นจะถูกตัวเองปฏิเสธทิ้ง
_REFUSE_HINTS = [
    "ไม่สะดวก", "ไม่อยากให้โทร", "ไม่ต้องโทร", "ไม่ขอให้โทร", "ยังไม่โทร",
    "ไม่ให้เบอร์", "ยังไม่ให้เบอร์", "ไม่อยากให้ติดต่อ", "ไม่สะดวกคุย",
]
# เอาออกจากลิสต์ปฏิเสธแล้ว (14 ส.ค. 2026):
#   "ขอข้อมูลก่อน" / "ขอรายละเอียดก่อน" / "ขอดูข้อมูลก่อน"
# เหตุผล: คำพวกนี้อยู่ใน QUALIFY_TRIGGERS ด้วย = คนสนใจ
# โค้ดเช็คลิสต์ปฏิเสธก่อน คนที่แค่อยากดูรายละเอียดก่อนตัดสินใจ
# (ซึ่งคือคนซื้อจริงส่วนใหญ่) เลยถูกตีตรา "ห้ามขอเบอร์ตลอดกาล"
# เขาไม่ได้ปฏิเสธการโทร เขาแค่ขอข้อมูล -> ให้ไหลเข้าโหมดถามปกติ

# ซื้อเงินสด = ไม่มีเหตุผลจะถามรายได้/บูโรต่อ
# ต้องหยุดถามแล้วชวนดูห้องจริง (คนถือเงินสดจริงไม่โอนโดยไม่เห็นของ)
_CASH_HINTS = [
    "เงินสด", "ซื้อสด", "จ่ายสด", "ไม่กู้", "ไม่ต้องกู้", "ไม่ใช้สินเชื่อ",
    "ไม่ได้กู้", "cash",
]

# รับ ID LINE ที่ลูกค้าพิมพ์มาเฉยๆ (ไม่มีคำว่า line ไม่มีตัวเลข)
# LINE ID จริงคือ a-z 0-9 . _ - ยาว 4-20 ตัว ขึ้นต้นด้วยตัวอักษร/ตัวเลข
_LINE_ID_RE = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9._\-]{2,29}$")
_CONTACT_HINTS = ("line", "ไลน์", "ไอดี", "แอดมา", "แอดไป", "id:", "id ")
# คำอังกฤษสั้นๆ ที่หน้าตาเหมือนไอดีแต่ไม่ใช่
_NOT_ID_WORDS = {
    "ok", "okay", "yes", "no", "yep", "nope", "sure", "hi", "hello", "hey",
    "thanks", "thank", "cash", "555", "condo", "sale", "test", "none",
}

# ข้อมูลชั้น 2 — สิ่งเดียวที่คู่แข่งอยากได้จริง
# กันไว้ที่บอท = คำโกหก "ซื้อเงินสด" ปลดล็อกอะไรไม่ได้เลย
_TIER2_HINTS = [
    "ส่วนลด", "ลดได้", "ลดเท่าไหร่", "ราคาสุทธิ", "ราคาต่ำสุด", "ถูกสุด",
    "ราคาปิด", "เหลือกี่ห้อง", "เหลือกี่ยูนิต", "ว่างกี่ห้อง", "ว่างกี่ยูนิต",
    "ห้องเหลือ", "คอมมิชชั่น", "commission",
]

# ศัพท์ฝั่งคนขาย — คนซื้ออยู่เองแทบไม่ใช้
# ห้ามใส่คำที่มีใน _TIER2_HINTS ซ้ำ ไม่งั้นข้อความเดียวโดนหักคะแนน 2 เด้ง (-6)
_SELLER_JARGON = [
    "ราคาต่อตาราง", "ต่อตร.ม", "ต่อตรม", "ต่อ ตร.ม", "yield", "occupancy",
    "gross yield", "net yield", "เรทเช่า", "สต็อก", "ยอดจอง",
]

# คำที่คนซื้อจริงเกือบทุกคนพูดถึง (กังวลเรื่องกู้)
_LOAN_WORDS = [
    "กู้", "ผ่อน", "ธนาคาร", "ดอกเบี้ย", "สินเชื่อ", "บูโร", "วงเงิน",
    "ดาวน์", "อนุมัติ",
]

# ตอบรับนัดดูห้อง
# ห้ามใส่ชื่อวันซ้ำกับ _DAY_WORDS — ไม่งั้นพูดชื่อวันคำเดียวจะได้ทั้ง
# "ยอมนัด" และ "ยืนยันวัน" พร้อมกันในรอบเดียว (+5) ทั้งที่ยังไม่ได้ตอบรับ
# "ว่าง" เปล่าๆ ก็เอาออก เพราะไปชนกับ "ว่างงาน" และ "ว่างกี่ห้อง"
_VIEWING_YES = [
    "ดูห้อง", "นัด", "สะดวก", "ไปดู", "เข้าไปดู", "ขอดูจริง", "ว่างวัน",
]

# คำที่บอก "วัน" — ใช้ยืนยันว่านัดดูห้องจริง ไม่ใช่แค่พูดว่าสนใจ
_DAY_WORDS = [
    "จันทร์", "อังคาร", "พุธ", "พฤหัส", "ศุกร์", "เสาร์", "อาทิตย์",
    "พรุ่งนี้", "มะรืน", "วันนี้", "สุดสัปดาห์", "ต้นเดือน", "สิ้นเดือน",
]

# กรอบเวลาที่อยากได้ของ — คนไม่ซื้อจริงตอบลอย
_TIMELINE_WORDS = [
    "ภายใน", "ไม่เกิน", "ต้องการภายใน", "อยากได้ใน", "ปีนี้", "เดือนหน้า",
    "รีบ", "ด่วน", "ก่อนสิ้นปี",
]

# ----------------------------------------------------------------------
# น้ำเสียงหญิง (เพจที่แอดมินเป็นผู้หญิง)
# ----------------------------------------------------------------------
# สรรพนาม "ผม" -> ตัดทิ้ง (ผู้หญิงไทยมักละสรรพนามในบทสนทนาขาย)
_FEMALE_PRONOUN = [
    ("ผมรบกวน", "รบกวน"), ("เดี๋ยวผม", "เดี๋ยว"), ("ผมช่วย", "ช่วย"),
    ("ผมส่ง", "ส่ง"), ("ผมขอ", "ขอ"), ("ผมจะ", "จะ"), ("ผมคัด", "คัด"),
]

# ตัดสินว่าเป็นคำถามจาก "คำท้ายประโยค" เท่านั้น
# (ดูแค่ว่ามีคำถามอยู่ในประโยคไม่พอ — "เช่นบ้าน รถยนต์ หรือบัตรเครดิต" ไม่ใช่คำถาม
#  และ "ไม่ต้องจ่ายอะไรเพิ่ม" ก็ไม่ใช่ ทั้งที่มีคำว่า หรือ / อะไร อยู่)
_Q_ENDINGS = [
    "ไหม", "มั้ย", "หรือเปล่า", "รึเปล่า", "เท่าไหร่", "เท่าไร",
    "ยังไง", "อย่างไร", "เมื่อไหร่", "ไหน", "บ้าง", "อะไร", "นะ", "?",
]

# ประโยคเลือก "A หรือ B" = คำถาม  แต่ "เช่น A, B หรือ C" = การยกตัวอย่าง
_LIST_MARKERS = ["เช่น", "อาทิ", "ได้แก่"]


def _is_question_clause(clause: str) -> bool:
    c = clause.rstrip(" .,\u200b")
    if any(c.endswith(k) for k in _Q_ENDINGS):
        return True
    if "หรือ" in c and not any(k in c for k in _LIST_MARKERS):
        # "A หรือ B?" = คำถาม (หรือ อยู่ท้ายๆ)
        # "ขอ A หรือ B ไว้ทำ C" = บอกเล่า (ยังมีเนื้อความยาวต่อท้าย)
        if len(c) - c.rfind("หรือ") <= 28:
            return True
    return False


# สั่ง AI ให้เขียนเสียงหญิงตั้งแต่ต้นทาง ดีกว่าไปแปลงทีหลัง
# (ข้อความที่ AI สร้างสดทุกครั้ง กฎแปลงเดาไม่ได้ 100%)
FEMALE_VOICE_RULE = (
    "\n\n[น้ำเสียง] เพจนี้แอดมินเป็นผู้หญิง "
    "ให้ตอบด้วยสรรพนามผู้หญิงเสมอ ห้ามใช้คำว่า 'ครับ' หรือ 'ผม' เด็ดขาด "
    "ประโยคบอกเล่าลงท้าย 'ค่ะ' ประโยคคำถามลงท้าย 'คะ' "
    "ถ้าต้องเรียกตัวเองให้ใช้ 'เรา' หรือละไว้"
)

# ข้อความที่ลูกค้าเห็นบ่อยที่สุด — เขียนตัวหญิงไว้ตายตัว ไม่ให้กฎเดา
# (กฎยังดูแลข้อความอื่นๆ รวมถึงคำตอบที่ AI สร้างสดทุกครั้ง)
_FEMALE_EXACT = {
    QUALIFY_QUESTIONS[0]:
        "ลูกค้าสนใจลงทุนคอนโดปล่อยเช่าหรือเพื่ออยู่อาศัยเองคะ",
    QUALIFY_QUESTIONS[1]:
        "รบกวนสอบถามข้อมูลเบื้องต้นค่ะลูกค้า เป็นพนักงานประจำหรือเปล่าคะ "
        "และมีรายได้ต่อเดือนอยู่ที่ประมาณเท่าไหร่คะ",
    QUALIFY_QUESTIONS[2]:
        "ปัจจุบันมีผ่อนชำระอะไรในระบบบูโรบ้างไหมคะ "
        "เช่นบ้าน รถยนต์ หรือบัตรเครดิตคะ",
    QUALIFY_QUESTIONS[3]:
        "ขอ ID LINE หรือเบอร์ไว้ส่งห้องที่ตรงงบ พร้อมตารางผ่อนให้ดูค่ะ "
        "คุยทางแชทก่อนได้เลย ไม่ต้องโทรก็ได้ค่ะ",
    QUALIFY_QUESTIONS[4]:
        "ถ้ามีผู้กู้ร่วมจะยื่นได้วงเงินสูงขึ้นมากค่ะ ลูกค้าพอจะมีผู้กู้ร่วมไหมคะ "
        "เช่น คู่สมรส พ่อแม่ หรือพี่น้องคะ",
    NO_COBORROWER_MSG:
        "ขอบคุณที่สนใจนะคะ ขอบอกตรงๆ เลยจะได้ไม่เสียเวลาลูกค้าค่ะ "
        "ยื่นเดี่ยวที่รายได้ระดับนี้ ธนาคารส่วนใหญ่ยังไม่อนุมัติค่ะ",
    COBORROWER_INVITE_MSG:
        "วันไหนที่มีผู้กู้ร่วมแล้ว เช่น คู่สมรส พ่อแม่ หรือพี่น้อง "
        "ทักกลับมาที่เพจนี้ได้เลยนะคะ "
        "เดี๋ยวจัดห้องที่ตรงงบพร้อมตารางผ่อนให้ดูทันที ยินดีเสมอค่ะ",
}


def to_female(text: str) -> str:
    """ครับ -> ค่ะ (บอกเล่า) / คะ (คำถาม) · ตัดสรรพนามชาย"""
    exact = _FEMALE_EXACT.get((text or "").strip())
    if exact:
        return exact
    if not text or ("ครับ" not in text and "ผม" not in text):
        return text
    for a, b in _FEMALE_PRONOUN:
        text = text.replace(a, b)
    lines = []
    for line in text.split("\n"):
        buf, start = "", 0
        while True:
            i = line.find("ครับ", start)
            if i < 0:
                buf += line[start:]
                break
            clause = line[start:i]
            buf += clause
            nxt = i + 4
            if line[nxt:nxt + 2] == "ผม":      # "ครับผม"
                nxt += 2
            buf += "คะ" if _is_question_clause(clause) else "ค่ะ"
            start = nxt
        lines.append(buf)
    return "\n".join(lines)


# ชื่อแบรนด์ที่ฝังอยู่ใน WELCOME_MSG — ใช้แทนที่เมื่อมาจากเพจอื่น
# ชื่อแบรนด์มาจาก faq_data ที่เดียว ห้ามพิมพ์ซ้ำ
DEFAULT_BRAND = BRAND_NAME

# ถามคำถามเดิมได้สูงสุดกี่ครั้ง (กันบอทวนถามจนลูกค้าหนี)
MAX_ASK_PER_FIELD = 2

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
                platform: str = "facebook",
                page_id: str = "", brand: str = "",
                sheet_tab: str = "", gender: str = "") -> tuple[str, str | None]:
        """คืนค่า (ข้อความตอบ, grade หรือ None)

        ข้อความตอบอาจมี MSG_SPLIT คั่น -> main.py แยกส่งเป็นหลายบับเบิล
        """
        referral = referral or {}
        skey = f"{page_id}:{user_id}" if page_id else user_id
        state, is_new = self._resolve_state(user_id, platform, referral, skey)

        gap = _now() - state.get("last_seen", _now())
        bucket = self._gap_bucket(gap) if not is_new else "new"
        state["last_seen"] = _now()
        state["platform"] = platform
        # บริบทเพจ — คนละเพจ = คนละแบรนด์ คนละแท็บชีต (PSID ของ FB ผูกกับเพจ)
        if page_id:
            state["page_id"] = page_id
        if brand:
            state["brand"] = brand
        if sheet_tab:
            state["sheet_tab"] = sheet_tab
        if gender:
            state["gender"] = gender
        if referral:
            state["referral"] = referral

        bubbles, grade = self._decide(user_message, user_id, state, bucket, is_new)

        parts = [b for b in bubbles if b and b.strip()]
        if state.get("gender") == "female":
            parts = [to_female(p) for p in parts]
        reply = MSG_SPLIT.join(parts)
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
            bubbles.append(self._welcome(state))
        elif bucket == "cold" and data:
            # หายไปเกิน 1 วัน + เคยให้ข้อมูลไว้ -> ทวนให้ฟังว่าเราจำได้
            recap = self._recap(data)
            if recap:
                bubbles.append(RETURNING_MSG.format(recap=recap))

        # ---- 2) ลูกค้าพูดอะไร ตอบอันนั้นก่อนเสมอ ----------------------
        # ถ้ากำลังรอคำตอบอยู่ แต่ลูกค้าดันถามคำถามกลับมา
        # ห้ามเอาคำถามของเขาไปนับเป็นคำตอบ — ตอบเขาก่อน แล้วค่อยถามใหม่
        # ---- 1.5) อ่านเจตนาพิเศษก่อนอย่างอื่น (Phase 4.3) --------------
        state["turns"] = state.get("turns", 0) + 1
        self._scan_signals(state, msg)

        # ก) ลูกค้าปฏิเสธการถูกโทร -> ยอมรับทันที ห้ามขอช่องทางซ้ำ
        #    เขาไม่ได้ปฏิเสธเรา เขาปฏิเสธ "การถูกโทร" ซึ่งเราพูดขึ้นมาเอง
        if not state.get("done") and self._is_refusal(msg, awaiting):
            state["contact_refused"] = True
            state["awaiting"] = None
            self._upsert_lead(state)
            bubbles.append(CONTACT_REFUSED_MSG)
            return bubbles, None

        # ข) ซื้อเงินสด -> หยุดถามเรื่องกู้ทั้งชุด แล้วชวนดูห้องจริง
        #    คนถือเงินสดจริงไม่มีทางโอนโดยไม่เห็นของ = ด่านที่ฟรีสำหรับคนจริง
        if not state.get("done") and self._is_cash(msg) and not data.get("cash"):
            data["cash"] = True
            data.setdefault("income", "ซื้อเงินสด")
            data.setdefault("debt", "-")
            state["awaiting"] = None
            state["cash_invited"] = True
            self._upsert_lead(state)
            bubbles.append(CASH_BUYER_MSG)
            bubbles.append(CASH_INVITE_MSG)
            return bubbles, None

        # ค) ถามข้อมูลชั้น 2 (ราคาสุทธิ/ส่วนลด/ห้องที่เหลือ) -> บอทไม่ตอบเอง
        #    กันไว้ตรงนี้ = ไม่ต้องจับผิดว่าใครเป็นคู่แข่งเลย
        if self._is_tier2(msg):
            state["price_asks"] = state.get("price_asks", 0) + 1
            bubbles.append(TIER2_GUARD_MSG)
            return bubbles, None

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
            # ไม่มีผู้กู้ร่วม -> จบบทสนทนาตรงนี้เลย ไม่ขอเบอร์ ไม่ถามบูโรต่อ
            # ยื่นเดี่ยวไม่ผ่านอยู่แล้ว เก็บเบอร์ไป = เสียเวลาทั้งสองฝ่าย
            # แต่ยังเก็บลีดไว้ในชีต + เปิดประตูให้กลับมาเมื่อหาผู้กู้ร่วมได้
            if awaiting == "co_borrower" and data.get("co_borrower_none"):
                state["contact_refused"] = True   # กันโค้ดส่วนอื่นขอช่องทาง
                grade = self._finish(user_id, state, "-", calendar=False)
                bubbles.append(NO_COBORROWER_MSG)
                bubbles.append(COBORROWER_INVITE_MSG)
                return bubbles, grade

        if not consumed:
            faq = self._check_faq(msg)
            if faq:
                bubbles.append(faq)
            elif awaiting:
                # กำลังรอคำตอบอยู่ แต่ข้อความไม่ใช่ทั้งคำตอบและไม่เข้า FAQ
                # -> ให้ Claude ตอบ แล้วค่อยถามซ้ำในบับเบิลถัดไป
                bubbles.append(self._ask_claude(msg, user_id, state.get("gender", "")))
            elif state.get("done"):
                # ลูกค้าให้ข้อมูลครบไปแล้ว — ห้ามขอเบอร์/ถามชุดเดิมซ้ำเด็ดขาด
                bubbles.append(STATUS_MSG if self._is_status_ask(msg)
                               else self._ask_claude(msg, user_id, state.get("gender", ""), done=True))
            elif not self._should_qualify(msg) and not is_new:
                bubbles.append(self._ask_claude(msg, user_id, state.get("gender", "")))

        # ---- 3) พ่วงคำถามที่ยังขาด 1 ข้อ (บับเบิลแยก) -----------------
        # เริ่มถามเมื่อ: ลูกค้าแสดงความสนใจ / เคยเข้าโหมดถามแล้ว / เพิ่งตอบไป 1 ข้อ
        if not state.get("done"):
            if self._should_qualify(msg) or state.get("qualifying") or consumed:
                state["qualifying"] = True
                field, question = self._next_missing(data, state)
                asked = state.setdefault("asked", {})
                if field and asked.get(field, 0) >= MAX_ASK_PER_FIELD:
                    # ถามไป 2 ครั้งแล้วยังไม่ได้ -> หยุด อย่าไล่บี้จนลูกค้าหนี
                    if field == "contact":
                        state["contact_refused"] = True
                        bubbles.append(CONTACT_REFUSED_MSG)
                    field = None
                elif field:
                    asked[field] = asked.get(field, 0) + 1
                    state["awaiting"] = field
                    bubbles.append(question)

                if not field and not state.get("done"):
                    if state.get("contact_refused") and not data.get("contact"):
                        # ตอบครบทุกข้อยกเว้นช่องทาง -> เก็บลีดไว้ แต่ห้ามพูดว่าจะโทร
                        grade = self._finish(user_id, state, "-")
                        bubbles.append("รับทราบครับ ผมส่งเรื่องให้ที่ปรึกษาดูแลแล้ว "
                                       "สอบถามเพิ่มทางแชทนี้ได้ตลอดเลยครับ")
                    elif data.get("objective") or data.get("income"):
                        grade = self._finish(user_id, state, data.get("contact", ""))
                        bubbles.append(DONE_MSG)

        # กันเคสไม่มีอะไรจะพูดเลย
        if not bubbles:
            bubbles.append(FALLBACK_MSG)

        # กันขอช่องทางติดต่อซ้อนกันในข้อความเดียว (เจอหน้างาน 13 ส.ค.)
        bubbles = self._dedupe_contact_ask(bubbles)
        return bubbles, grade

    @staticmethod
    def _dedupe_contact_ask(bubbles: list[str]) -> list[str]:
        """ถ้ามีบับเบิลที่ขอเบอร์/LINE มากกว่า 1 อัน ให้เหลืออันสุดท้ายอันเดียว
        ลูกค้าเห็นขอเบอร์ 2 รอบติดกัน = รู้สึกโดนบี้ แล้วหนี
        """
        def is_ask(x: str) -> bool:
            return ("ขอเบอร์" in x or "ขอ ID LINE" in x
                    or ("เบอร์" in x and "LINE" in x))
        idx = [i for i, b in enumerate(bubbles) if is_ask(b)]
        if len(idx) <= 1:
            return bubbles
        keep = idx[-1]
        return [b for i, b in enumerate(bubbles) if i not in idx or i == keep]

    # ==================================================================
    # State: RAM -> ชีต -> สร้างใหม่
    # ==================================================================
    def _blank_state(self, platform: str, referral: dict) -> dict:
        return {
            "data": {}, "awaiting": None, "qualifying": False, "done": False,
            "referral": referral or {}, "platform": platform,
            "last_seen": _now(), "lead_sent": False,
            "asked": {}, "contact_refused": False,
            "signals": [], "turns": 0, "price_asks": 0,
        }

    def _resolve_state(self, user_id: str, platform: str,
                       referral: dict,
                       skey: str = "") -> tuple[dict, bool]:
        skey = skey or user_id
        if skey in _lead_states:
            return _lead_states[skey], False

        # RAM ไม่มี — เซิร์ฟเวอร์เพิ่ง restart หรือเป็นลูกค้าใหม่จริง
        loaded = self._load_session(user_id, (referral or {}).get("_page_id", ""))
        if loaded:
            loaded.setdefault("data", {})
            loaded.setdefault("awaiting", None)
            loaded["psid"] = user_id
            _lead_states[skey] = loaded
            _conversations.setdefault(skey, [])
            print(f"[SESSION] restored {user_id[:8]}... "
                  f"fields={list(loaded.get('data', {}).keys())}")
            return loaded, False

        state = self._blank_state(platform, referral)
        state["psid"] = user_id
        # ลูกค้าใหม่ = มาจากเพจ/แอดด้วยเหตุผลบางอย่างเสมอ -> เข้าโหมดถามเลย
        # (ไม่ทิ้งคำถามลูกค้า และไม่ปล่อยให้จบแค่ "สวัสดีครับ")
        # ตั้งใจให้เป็น True เสมอ -> QUALIFY_TRIGGERS จึงไม่ได้คุมว่า "เริ่มถามเมื่อไหร่"
        # มันคุมแค่ว่า "ข้อความนี้ต้องส่งให้ AI ตอบไหม"
        # ใครจะแก้ QUALIFY_TRIGGERS ให้รู้ไว้ว่ามันไม่มีผลกับจังหวะเริ่มถาม
        state["qualifying"] = True
        _lead_states[skey] = state
        _conversations[skey] = []
        return state, True

    def _load_session(self, user_id: str, page_id: str = "") -> dict | None:
        if not (FEATURE_PERSIST and APPS_SCRIPT_URL):
            return None
        try:
            r = requests.post(
                APPS_SCRIPT_URL,
                json={"action": "get_session", "psid": user_id,
                      "page_id": page_id},
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
            "page_id": state.get("page_id", ""),
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
        if field == "income" and not data.get("income_unknown"):
            # รายได้ต่ำกว่าเกณฑ์ยื่นเดี่ยว -> ต้องหาผู้กู้ร่วมก่อน
            # ถามผิดลำดับ = เสียเวลาทั้งสองฝ่าย เพราะยื่นเดี่ยวไม่ผ่านอยู่แล้ว
            n = _parse_income(msg)
            if n is not None and 1000 <= n < LOW_INCOME_BAHT:
                data["income_baht"] = n
                data["low_income"] = True
        if field == "co_borrower":
            low = msg.lower()
            if any(h in low for h in _NO_COB_HINTS):
                data["co_borrower_none"] = True
                self._add_signal(state, "รายได้ต่ำ+ไม่มีผู้กู้ร่วม")
        state["data"] = data
        # ได้ข้อมูลใหม่ -> ส่งเข้าชีตทันที ไม่รอครบ 4 ข้อ (กันลีดหลุด)
        self._upsert_lead(state)

    def _next_missing(self, data: dict,
                      state: dict | None = None) -> tuple[str | None, str | None]:
        state = state or {}
        for f in FIELD_ORDER:
            if f == "debt" and (data.get("income_unknown") or data.get("cash")):
                continue
            if f == "income" and data.get("cash"):
                continue          # ซื้อสด = ไม่มีเหตุผลจะถามรายได้
            if f == "co_borrower" and not data.get("low_income"):
                continue          # รายได้ถึงเกณฑ์ = ยื่นเดี่ยวได้ ไม่ต้องถาม
            if f == "co_borrower" and data.get("cash"):
                continue          # ซื้อสด = ไม่ได้กู้
            if f == "contact" and state.get("contact_refused"):
                continue          # เขาบอกแล้วว่าไม่สะดวก ห้ามถามอีก
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
    def _is_refusal(msg: str, awaiting: str | None = None) -> bool:
        """ลูกค้าไม่สะดวกให้ติดต่อ — ต้องแยกจาก 'วันเสาร์ไม่สะดวก'"""
        m = msg.lower()
        if not any(k in m for k in _REFUSE_HINTS):
            return False
        ctx = any(k in m for k in
                  ["โทร", "เบอร์", "line", "ไลน์", "ติดต่อ", "รายละเอียด", "ข้อมูล"])
        return ctx or awaiting == "contact"

    @staticmethod
    def _is_cash(msg: str) -> bool:
        m = msg.lower()
        if "ไม่มีเงินสด" in m or "ไม่พร้อมเงินสด" in m:
            return False
        return any(k in m for k in _CASH_HINTS)

    @staticmethod
    def _is_tier2(msg: str) -> bool:
        m = msg.lower()
        return any(k in m for k in _TIER2_HINTS)

    @staticmethod
    def _add_signal(state: dict, tag: str):
        sig = state.setdefault("signals", [])
        if tag not in sig:
            sig.append(tag)

    def _scan_signals(self, state: dict, msg: str):
        """ติดธงสัญญาณไว้ให้เซลเห็นก่อนโทร — ไม่ใช้ปฏิเสธใครเด็ดขาด
        ต้นทุนพลาดไม่เท่ากัน: กันคู่แข่งพลาด = เสียข้อมูลที่หาทางอื่นได้อยู่แล้ว
                              กันลูกค้าจริงพลาด = เสียดีลหลักล้าน
        """
        m = msg.lower()
        sig = state.setdefault("signals", [])

        # ข้อความที่กำลังปฏิเสธอยู่ ห้ามให้คะแนนบวกใดๆ ในรอบนี้
        # "วันนี้ไม่สะดวกครับ" เคยได้ทั้ง viewing_ok + viewing_confirmed = +5
        refusing = self._is_refusal(msg) or any(k in m for k in _REFUSE_HINTS)

        if _hit_any(m, _SELLER_JARGON) and "ศัพท์ฝั่งคนขาย" not in sig:
            sig.append("ศัพท์ฝั่งคนขาย")

        # _hit = เจอคำแบบไม่มี "ไม่/ยังไม่/ไม่ได้" นำหน้า
        # กัน "ไม่กู้" ถูกนับเป็น "คุยเรื่องกู้" (+3) ซึ่งเป็นช่องโหว่ซื้อสดปลอม
        if not refusing and _hit_any(m, _LOAN_WORDS):
            state["loan_talk"] = True
        if not refusing and _hit_any(m, _VIEWING_YES):
            state["viewing_ok"] = True
        # ยืนยันนัดจริง = ต้องเคยตอบรับนัดมาก่อน แล้วรอบนี้ระบุ "วัน"
        # ต้องเป็นคนละรอบกัน ไม่ใช่รอบเดียวได้ทั้งคู่
        if not refusing and (state.get("cash_invited") or state.get("viewing_ok")):
            if _hit_any(m, _DAY_WORDS):
                state["viewing_confirmed"] = True
        # บอกงบเป็นตัวเลข (ต้องมีหลักแสนขึ้นไป หรือคำว่าล้าน)
        if (not refusing and _hit_any(m, ["ล้าน", "งบ"])
                and any(ch.isdigit() for ch in m)):
            state["budget_given"] = True
        if not refusing and _hit_any(m, _TIMELINE_WORDS):
            state["timeline_given"] = True

        if (state.get("data", {}).get("cash") and state.get("contact_refused")
                and "ซื้อสด+ไม่ให้ช่องทาง" not in sig):
            sig.append("ซื้อสด+ไม่ให้ช่องทาง")

        if (state.get("price_asks", 0) >= 2 and not state.get("loan_talk")
                and "ถามราคาแต่ไม่ถามกู้" not in sig):
            sig.append("ถามราคาแต่ไม่ถามกู้")

        if (state.get("cash_invited") and state.get("turns", 0) >= 4
                and not state.get("viewing_ok")
                and "เลี่ยงนัดดูห้อง" not in sig):
            sig.append("เลี่ยงนัดดูห้อง")

    @staticmethod
    def _intent_score(state: dict) -> int:
        """คะแนนความจริงจัง — คิดจากสิ่งที่ลูกค้า "ลงทุนไปแล้ว" ไม่ใช่สิ่งที่เขาพูด
        >= 6  = เรียกคนทันที
        3-5   = เข้าคิวปกติ
        <= 2  = บอทคุยต่อเอง ไม่ต้องรบกวนคน (ยังอยู่ในชีต ไม่หาย)
        """
        d = state.get("data", {})
        sc = 0
        # --- บวก: ปลอมแล้วเจ็บ ---
        if state.get("viewing_confirmed"):
            sc += 5        # ยอมนัดดูห้องจริง + ระบุวัน = แพงที่สุด
        if state.get("loan_talk"):
            sc += 3        # คนซื้อจริงเกือบ 100% ถามเรื่องกู้ คู่แข่งไม่ถาม
        if state.get("budget_given"):
            sc += 2
        if state.get("timeline_given"):
            sc += 2
        c = str(d.get("contact", ""))
        if c and c != "-":
            low = c.lower()
            sc += 2 if ("line" in low or "ไลน์" in low) else 1   # LINE > เบอร์
        if state.get("turns", 0) >= 6:
            sc += 1
        # --- ลบ: ท่าทางฝั่งคนสืบข้อมูล ---
        sig = state.get("signals", [])
        if "ศัพท์ฝั่งคนขาย" in sig:
            sc -= 3
        if state.get("price_asks", 0) >= 1 and not d.get("objective"):
            sc -= 3        # ถามส่วนลด/ห้องเหลือ ก่อนบอกว่าตัวเองอยากได้อะไร
        if "ซื้อสด+ไม่ให้ช่องทาง" in sig:
            sc -= 3
        if "เลี่ยงนัดดูห้อง" in sig:
            sc -= 2
        # คุยยาวแล้วยังไม่แตะเรื่องกู้เลย — เป็นสัญญาณลบ "เฉพาะตอนที่เขาไล่ถามราคา"
        # เดิมหักทุกเคสที่ turns>=5 ซึ่งชนบทสนทนาปกติที่ตอบครบ 4 ข้อพอดี
        # ผลคือทุกคนตกไป C หมด เกรด A/B แทบไม่เคยเกิด (เจอ 14 ส.ค. 2026)
        if (state.get("turns", 0) >= 5 and not state.get("loan_talk")
                and state.get("price_asks", 0) >= 1):
            sc -= 2
        # รายได้ที่ยื่นเดี่ยวผ่าน = สัญญาณจริง (ตัวเลขปลอมได้ แต่เซลเช็คได้ตอนโทร)
        n = _parse_income(str(d.get("income", "")))
        if n is not None and not d.get("income_unknown"):
            if n >= 50000:
                sc += 3
            elif n >= LOW_INCOME_BAHT:
                sc += 2
        if d.get("co_borrower_none"):
            sc -= 2
        return sc

    @staticmethod
    def _welcome(state: dict) -> str:
        """ข้อความทักทาย — เปลี่ยนเฉพาะชื่อแบรนด์ตามเพจ ข้อความอื่นเหมือนกันหมด"""
        brand = state.get("brand")
        if not brand or brand == DEFAULT_BRAND:
            return WELCOME_MSG
        return WELCOME_MSG.replace(DEFAULT_BRAND, brand)

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
            low = m.lower()
            # 1) เบอร์โทร
            if sum(c.isdigit() for c in m) >= 6:
                return True
            # 2) บอกมาตรงๆ ว่าเป็นไลน์ / ไอดี
            if any(h in low for h in _CONTACT_HINTS):
                return True
            # 3) ID LINE ล้วนๆ เช่น "Giftdd" "gift_88" "@wealth.estate"
            #    คนไทยส่วนใหญ่พิมพ์แค่ไอดีมาเฉยๆ ไม่มีคำว่า line และไม่มีตัวเลข
            #    ของเดิมตกเคสนี้ทั้งหมด -> บอทถามซ้ำ (เจอหน้างาน 14 ส.ค. 2026)
            if (" " not in m and _LINE_ID_RE.match(m)
                    and low.lstrip("@") not in _NOT_ID_WORDS):
                return True
            return False
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
        # _hit -> "ไม่สนใจแล้วครับ" ไม่ถูกนับว่าสนใจ
        return _hit_any(message.lower(), QUALIFY_TRIGGERS)

    def _is_disqualified(self, message: str) -> bool:
        msg = message.lower()
        return any(kw in msg for kw in DISQUALIFY_KEYWORDS)

    # ==================================================================
    # ปิดเคส + เกรด
    # ==================================================================
    def _finish(self, user_id: str, state: dict, contact: str,
                calendar: bool = True) -> str:
        """ปิดเคส -> คิดเกรด + เขียนแถวสมบูรณ์ลงชีต
        calendar=False -> ลงชีตอย่างเดียว ไม่สร้างนัดโทรกลับ
                          (เคสที่ไม่มีช่องทางติดต่อ เช่น ไม่มีผู้กู้ร่วม)
        ทางเข้าปิดเคส "ทุกทาง" ต้องผ่านฟังก์ชันนี้ ห้ามเขียนซ้ำที่อื่น
        ไม่งั้นจะตกอย่างใดอย่างหนึ่งเสมอ (เคยตก lead_sent + fb_name มาแล้ว)
        """
        data = state["data"]
        if data.get("income_unknown") or data.get("co_borrower_none"):
            grade = "C"           # ยื่นเดี่ยวไม่ผ่าน = ยังไม่ใช่คิวโทรด่วน
        else:
            grade = self._grade(data, state)
        state["score"] = self._intent_score(state)
        data["score"] = state["score"]
        state["done"] = True
        state["awaiting"] = None
        data["grade"] = grade
        fb_name = self._get_fb_name(user_id, state.get("platform", "facebook"))
        state["fb_name"] = fb_name
        # ส่งชุดเต็ม (แถวสมบูรณ์ + สร้างนัดในปฏิทิน) — schema เดิม ไม่แตะ
        self._send_to_sheets(user_id, data, grade, fb_name,
                             state.get("referral", {}),
                             state.get("platform", "facebook"),
                             state.get("page_id", ""),
                             state.get("sheet_tab", ""),
                             signals=state.get("signals", []),
                             contact_refused=state.get("contact_refused", False),
                             calendar=calendar)
        state["lead_sent"] = True
        return grade

    def _grade(self, data: dict, state: dict | None = None) -> str:
        # ซื้อเงินสด "อย่างเดียว" ไม่ใช่เกรด A — พิมพ์คำเดียว ต้นทุนศูนย์
        # ต้องพ่วงสัญญาณที่ปลอมแล้วเจ็บ (เช่นยอมนัดดูห้อง) ถึงจะขึ้น A
        if state is not None:
            sc = self._intent_score(state)
            if sc >= 6:
                return "A"
            if sc <= 2:
                return "C"
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
                    gender: str = "",
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
                    "system": (WEC_SYSTEM_PROMPT + FEMALE_VOICE_RULE)
                              if gender == "female" else WEC_SYSTEM_PROMPT,
                    "messages": messages,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"[CLAUDE ERROR] {resp.status_code}: {resp.text[:200]}")
                # AI ล่ม + ลูกค้าให้ข้อมูลครบแล้ว -> ห้ามถอยไปถามชุดเดิมซ้ำ
                return STATUS_MSG if done else FALLBACK_MSG
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
            "page_id": state.get("page_id", ""),
            "tab": state.get("sheet_tab", ""),
            "signals": " | ".join(state.get("signals", [])),
            "score": self._intent_score(state),
            "cash": "ใช่" if data.get("cash") else "",
            "no_call": "ไม่สะดวกให้โทร" if state.get("contact_refused") else "",
        })

    def _send_to_sheets(self, user_id: str, data: dict, grade: str,
                        fb_name: str = "", referral: dict | None = None,
                        platform: str = "facebook", page_id: str = "",
                        sheet_tab: str = "", signals=None,
                        contact_refused: bool = False, calendar: bool = True):
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
            "page_id":       page_id,
            "tab":           sheet_tab,
            # เดิมส่ง "" ทั้งสองช่อง -> การเขียนรอบสุดท้ายลบธงที่เก็บมาทั้งบทสนทนาทิ้ง
            "signals":       " | ".join(signals or []),
            "score":         data.get("score", ""),
            "cash":          "ใช่" if data.get("cash") else "",
            "no_call":       "ไม่สะดวกให้โทร" if contact_refused else "",
            "no_calendar":   "1" if not calendar else "",
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
