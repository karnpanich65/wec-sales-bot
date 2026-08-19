# main.py — WEC Sales Bot Phase 3.1
# Flask webhook server: Facebook Page Messenger + Instagram DM + Click-to-Messenger/IG Ads
#
# Phase 3 changes:
# 1. รองรับ event จาก Ads ครบทุกรูปแบบ:
#    - messaging_referrals  (ลูกค้าเดิมกดโฆษณา)
#    - postback + referral  (ลูกค้าใหม่กด Get Started จากโฆษณา)
#    - message.referral     (ข้อความแรกจาก Click-to-Messenger ad)
# 2. รองรับ entry.standby — ถ้าแอพไม่ได้เป็น Primary Receiver
#    จะ log เตือนให้เห็นใน Railway logs (สาเหตุหลักที่แชทจาก ads หาย)
# 3. Log ทุก event แบบละเอียด เพื่อ debug ง่าย
# 4. เก็บ ad_id + ref parameter ส่งต่อไป bot engine
#
# Phase 3.1 changes:
# 5. รองรับ Instagram DM (object = "instagram") — IG ที่ผูกกับเพจ
#    ใช้ flow เดียวกับ Facebook ทุกอย่าง + tag แหล่งที่มาใน Google Sheets

# Phase 3.3 changes (2026-07-28) — App Review / Live mode readiness:
# 6. หน้าเว็บจริงสำหรับ Meta: /            (อธิบายแอป)
#                            /privacy     (Privacy Policy — บังคับก่อนสลับ Live)
#                            /data-deletion (วิธีขอลบข้อมูล)
# 7. /review-log = App UI จริง (read-only live event viewer) ให้ผู้ตรวจ Meta
#    เห็น "การส่งข้อความจากแอปของเรา" = inbound webhook -> Send API request
#    -> response message_id  (ตอบข้อที่ผู้ตรวจสั่ง re-record)

import os
import hmac
import hashlib
import json
import re
import time
from collections import deque

import requests
from flask import Flask, request, jsonify, Response
from dotenv import load_dotenv
from bot_logic import BotEngine
from faq_data import MSG_SPLIT

load_dotenv()

# ======================================================
# Config — ใช้ Environment Variables ชุดเดิมทั้งหมด
# ======================================================
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")

# ======================================================
# Phase 5.0 — หลายเพจ
# ------------------------------------------------------
# WEC_PAGES  = JSON ไม่มีความลับ อ่าน/แก้ง่าย
#   {"108248514185091": {"brand": "Wealth Estate : อสังหาคุ้มค่า",
#                        "tab": "Facebook Leads"}, ...}
# โทเค็นแยกคนละตัวแปร (Railway มาสก์ให้) :  PAGE_TOKEN_<page_id>
# ถ้าไม่ตั้งอะไรเลย -> ใช้ FB_PAGE_ACCESS_TOKEN เดิม = พฤติกรรมเดิมทุกประการ
# ======================================================
# ชื่อแบรนด์มาจาก faq_data ที่เดียว (BRAND_NAME) ห้ามพิมพ์ซ้ำที่นี่
from faq_data import BRAND_NAME as DEFAULT_BRAND
DEFAULT_TAB = "Facebook Leads"
MAIN_PAGE_ID = os.environ.get("MAIN_PAGE_ID", "108248514185091")

def _load_pages() -> dict:
    raw = os.environ.get("WEC_PAGES", "").strip()
    if not raw:
        return {}
    try:
        cfg = json.loads(raw)
        if isinstance(cfg, dict):
            return cfg
        print("[PAGES] WEC_PAGES ต้องเป็น JSON object — ข้าม")
    except Exception as e:
        print(f"[PAGES ERROR] อ่าน WEC_PAGES ไม่ได้: {e} — ใช้ค่าเริ่มต้นแทน")
    return {}

PAGES = _load_pages()

# IG account id -> Facebook Page ID
# webhook ของ Instagram ส่ง entry.id เป็น "IG account id" ไม่ใช่ Page ID
# ตั้งใน WEC_PAGES ได้: {"<page_id>": {"ig_id": "<ig_account_id>", ...}}
IG_TO_PAGE = {}
for _pid, _cfg in (PAGES or {}).items():
    _ig = str((_cfg or {}).get("ig_id", "")).strip()
    if _ig:
        IG_TO_PAGE[_ig] = str(_pid)


def resolve_ig_page(ig_id: str) -> str:
    """แปลง IG account id -> Page ID ที่ผูกกัน
    ยังไม่ได้ตั้ง ig_id ใน WEC_PAGES -> ถือว่าเป็น IG ของเพจหลัก (ตอนนี้มี IG ตัวเดียว)
    """
    ig_id = str(ig_id or "")
    if ig_id in IG_TO_PAGE:
        return IG_TO_PAGE[ig_id]
    if ig_id and os.environ.get(f"PAGE_TOKEN_{ig_id}", "").strip():
        return ig_id          # ตั้งโทเค็นด้วย IG id ไว้ตรงๆ ก็ใช้ได้
    return MAIN_PAGE_ID


def page_token(page_id: str) -> str:
    """โทเค็นของเพจนั้น — ไม่มีก็ถอยไปใช้ตัวเดิม (กันบอทเงียบ)"""
    t = os.environ.get(f"PAGE_TOKEN_{page_id}", "").strip()
    return t or FB_PAGE_ACCESS_TOKEN


def page_brand(page_id: str) -> str:
    return (PAGES.get(str(page_id), {}) or {}).get("brand") or DEFAULT_BRAND


def page_tab(page_id: str) -> str:
    return (PAGES.get(str(page_id), {}) or {}).get("tab") or DEFAULT_TAB


def page_gender(page_id: str) -> str:
    """เพจที่แอดมินเป็นผู้หญิง ตั้ง "gender": "female" ใน WEC_PAGES
    ไม่ตั้ง = ชาย (ครับ) เหมือนเดิม -> เพจอสังหาคุ้มค่าไม่เปลี่ยนอะไรเลย
    """
    return (PAGES.get(str(page_id), {}) or {}).get("gender", "")


# ======================================================
# ตั้ง webhook fields ของแต่ละเพจให้ครบเอง (18 ส.ค. 2026)
# ----------------------------------------------------
# Meta ส่ง echo (ข้อความที่เพจส่งออก) ให้เฉพาะเพจที่ subscribe field
# `message_echoes` ไว้เท่านั้น — เปิดที่ระดับแอปอย่างเดียวไม่พอ
# ไม่มี field นี้ = โหมด "เซลรับช่วงเอง" ไม่ทำงานเลย เพราะบอทไม่รู้ว่าเซลพิมพ์
# เรียก Graph API ด้วยโทเค็นเพจที่ตั้งไว้ใน env อยู่แล้ว (ตัวเดียวกับที่ใช้ส่งข้อความ)
# รันตอนบูตครั้งเดียวใน thread — ล้มเหลวก็ไม่กระทบการตอบลูกค้า
# ======================================================
SUBSCRIBE_FIELDS = ("feed,messages,messaging_postbacks,messaging_referrals,"
                    "messaging_handovers,message_echoes,standby")


def _ensure_page_subscriptions():
    for _key in [k for k in os.environ if k.startswith("PAGE_TOKEN_")]:
        _tok = (os.environ.get(_key) or "").strip()
        if not _tok:
            continue
        _pid = _key[len("PAGE_TOKEN_"):]
        try:
            r = requests.post(
                f"https://graph.facebook.com/v22.0/{_pid}/subscribed_apps",
                params={"subscribed_fields": SUBSCRIBE_FIELDS,
                        "access_token": _tok},
                timeout=10,
            )
            print(f"[SUBSCRIBE] page={_pid} http={r.status_code} {r.text[:150]}")
        except Exception as _e:
            print(f"[SUBSCRIBE ERROR] page={_pid} {_e}")


try:
    import threading
    threading.Thread(target=_ensure_page_subscriptions, daemon=True).start()
except Exception as _e:
    print(f"[SUBSCRIBE BOOT ERROR] {_e}")


# ======================================================
# กัน Apps Script หลับ (Gift 19 ส.ค. 2026)
# ------------------------------------------------------
# ปัญหาจริง: ความจำของบอท (ประวัติแชท) เก็บที่ Google Sheet ผ่าน Apps Script
# ถ้าไม่มีใครเรียกสักพัก Google จะพักคอนเทนเนอร์ไว้ พอปลุกทีนึงกิน 5-10 วินาที
# เกินที่บอทรอไหว -> กู้ประวัติไม่ได้ -> เมื่อก่อนคือ "ทักทายใหม่ + ถามข้อ 1 ซ้ำ"
# (ตั้งแต่ r26 เปลี่ยนเป็นเงียบแล้วส่งให้เซล ซึ่งปลอดภัยกว่า แต่ก็ยังเสียโอกาส)
#
# หลักฐาน: ฝั่ง Apps Script ทุกคำสั่งเสร็จใน 0.6-4.3 วิ ไม่มีอันไหนช้า
#          แปลว่าเวลาที่หายไปคือ "ปลุกเครื่อง" ไม่ใช่ "ประมวลผล"
#
# วิธีแก้: ยิง GET เบาๆ ทุก 90 วินาที ให้มันตื่นอยู่ตลอด
# ⚠️ นี่คือการกันอาการ ไม่ใช่แก้ที่ราก — รากคือย้ายความจำไปฐานข้อมูลจริง
# ======================================================
KEEPWARM_SEC = int(os.environ.get("KEEPWARM_SEC", "90"))
# อ่านจาก env ตรงนี้เอง — ตัวแปรชื่อนี้อยู่ใน bot_logic ไม่ได้ถูก import เข้ามา
_APPS_URL = os.environ.get("APPS_SCRIPT_URL", "")


def _keep_apps_script_warm():
    if not _APPS_URL or KEEPWARM_SEC <= 0:
        print("[KEEPWARM] ปิดไว้ (ไม่มี APPS_SCRIPT_URL หรือ KEEPWARM_SEC<=0)")
        return
    _ok = _fail = 0
    while True:
        time.sleep(KEEPWARM_SEC)
        _t0 = time.time()
        try:
            r = requests.get(_APPS_URL, params={"ping": "1"}, timeout=20)
            _dt = time.time() - _t0
            if r.status_code == 200:
                _ok += 1
            else:
                _fail += 1
                print(f"[KEEPWARM] http={r.status_code} ใช้เวลา {_dt:.1f}s")
            # ช้าผิดปกติ = เพิ่งตื่น -> อยากรู้ว่าเกิดบ่อยแค่ไหน
            if _dt >= 5:
                print(f"[KEEPWARM SLOW] {_dt:.1f}s — Apps Script เพิ่งตื่น")
        except Exception as _e:
            _fail += 1
            print(f"[KEEPWARM ERROR] {_e}")
        # สรุปทุกๆ ~1 ชม. พอ ไม่ให้ log รก
        if (_ok + _fail) % 40 == 0:
            print(f"[KEEPWARM] สำเร็จ {_ok} / พลาด {_fail} (ทุก {KEEPWARM_SEC}s)")


try:
    import threading as _th_warm
    _th_warm.Thread(target=_keep_apps_script_warm, daemon=True).start()
    print(f"[KEEPWARM] เปิดแล้ว — ยิงทุก {KEEPWARM_SEC} วินาที")
except Exception as _e:
    print(f"[KEEPWARM BOOT ERROR] {_e}")


FB_VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "wec_bot_verify_2569")
FB_APP_SECRET = os.environ.get("FB_APP_SECRET", "")  # เว้นว่าง = dev mode (ข้าม signature check)
GIFT_FB_PSID = os.environ.get("GIFT_FB_PSID", "")

GRAPH_API_URL = "https://graph.facebook.com/v19.0/me/messages"

# คีย์เปิดหน้า /review-log (ให้ผู้ตรวจ Meta / ทีมงานเท่านั้น)
REVIEW_LOG_KEY = os.environ.get("REVIEW_LOG_KEY", "wec-review-2026")

# ข้อมูลติดต่อที่โชว์ในหน้า privacy / data-deletion
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "karnpanich.phutrakul@gmail.com")
PAGE_URL = "https://m.me/108248514185091"

app = Flask(__name__)
bot = BotEngine()

# ======================================================
# Event log สำหรับหน้า /review-log (in-memory, ไม่เก็บลงดิสก์)
# เก็บเฉพาะ 80 รายการล่าสุด + ปิดบัง PSID บางส่วน
# ======================================================
_EVENT_LOG: deque = deque(maxlen=80)
_BOOT_TS = time.time()


def _mask(psid: str) -> str:
    return (psid[:8] + "…") if psid else "-"


def _scrub(text: str) -> str:
    """กัน access token / คีย์ หลุดออกหน้า /review-log"""
    out = str(text)
    for secret in (FB_PAGE_ACCESS_TOKEN, os.environ.get("ANTHROPIC_API_KEY", ""),
                   FB_VERIFY_TOKEN, REVIEW_LOG_KEY):
        if secret and len(secret) > 6:
            out = out.replace(secret, "«redacted»")
    return re.sub(r"(access_token=)[^&\s'\")]+", r"\1«redacted»", out)


def log_event(kind: str, summary: str, detail: dict | None = None):
    """kind: INBOUND / SEND_REQUEST / SEND_RESPONSE / SEND_ERROR / SYSTEM"""
    clean_detail = {k: _scrub(v) if isinstance(v, str) else v
                    for k, v in (detail or {}).items()}
    _EVENT_LOG.appendleft({
        "ts": time.strftime("%H:%M:%S", time.localtime(time.time() + 7 * 3600)),
        "kind": kind,
        "summary": _scrub(summary),
        "detail": clean_detail,
    })


log_event("SYSTEM", "server started — WEC Messenger assistant v3.3")

# Cache ชั่วคราว: sender_id -> {"ad_id": ..., "ref": ..., "source": ...}
# เก็บข้อมูล ads referral ไว้จนกว่าข้อความแรกของลูกค้าจะมาถึง
# (in-memory — รีเซ็ตเมื่อ server restart; Phase 4 ค่อยย้ายไป Redis)
_pending_referrals: dict[str, dict] = {}

# ------------------------------------------------------
# กัน event ซ้ำ — Meta ยิง webhook ซ้ำถ้าเราตอบ 200 ช้าเกินไป
# ผลคือลูกค้าได้ข้อความเดิม 2 รอบ และชีตได้แถวซ้ำ
# เก็บ message id (mid) ที่ประมวลผลไปแล้ว 500 รายการล่าสุด
# ------------------------------------------------------
_SEEN_MIDS: deque = deque(maxlen=500)
_SEEN_SET: set = set()


def _already_handled(mid: str) -> bool:
    """True = เคยประมวลผล mid นี้แล้ว ให้ข้ามทิ้ง"""
    if not mid:
        return False
    if mid in _SEEN_SET:
        return True
    if len(_SEEN_MIDS) == _SEEN_MIDS.maxlen and _SEEN_MIDS:
        _SEEN_SET.discard(_SEEN_MIDS[0])
    _SEEN_MIDS.append(mid)
    _SEEN_SET.add(mid)
    return False


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


# ======================================================
# จังหวะพิมพ์ให้เหมือนคน (Gift 19 ส.ค. 2026 "พิมพ์ตอบเร็วไป")
# ------------------------------------------------------
# ตอบกลับใน 0.3 วิ = ลูกค้ารู้ทันทีว่าคุยกับบอท
# แอดมินจริงพิมพ์ประมาณ 5-15 วิ/ข้อความ แต่บอทรอนานขนาดนั้นไม่ได้
# (webhook ต้องตอบ 200 ให้ Meta ภายใน ~20 วิ ไม่งั้นโดนยิงซ้ำ)
# ทางสายกลาง: โชว์ "..." แล้วหน่วงตามความยาวข้อความ 1-3.5 วิ/บับเบิล
# รวมทั้งเทิร์นไม่เกิน TYPING_BUDGET_SEC — ปรับได้จาก env ไม่ต้องแก้โค้ด
# ปิดทั้งหมดด้วย TYPING_DELAY=0
# ======================================================
TYPING_ENABLED = os.environ.get("TYPING_DELAY", "1").strip() != "0"
TYPING_BASE_SEC = float(os.environ.get("TYPING_BASE_SEC", "1.0"))
TYPING_CPS = float(os.environ.get("TYPING_CPS", "18"))        # ตัวอักษร/วินาที
TYPING_MAX_SEC = float(os.environ.get("TYPING_MAX_SEC", "3.5"))
TYPING_BUDGET_SEC = float(os.environ.get("TYPING_BUDGET_SEC", "8"))


def send_sender_action(recipient_id: str, action: str, page_id: str = ""):
    """โชว์จุดไข่ปลา "กำลังพิมพ์..." — Meta ล้างให้เองเมื่อข้อความถูกส่ง"""
    token = page_token(page_id) if page_id else FB_PAGE_ACCESS_TOKEN
    if not token:
        return
    try:
        requests.post(
            GRAPH_API_URL,
            params={"access_token": token},
            json={"recipient": {"id": recipient_id}, "sender_action": action},
            timeout=5,
        )
    except Exception as e:
        print(f"[TYPING ERROR] {e}")


def _typing_pause(text: str) -> float:
    """หน่วงตามความยาวข้อความ — ข้อความยาวใช้เวลาพิมพ์นานกว่า"""
    return min(TYPING_MAX_SEC, TYPING_BASE_SEC + len(text) / TYPING_CPS)


def send_reply(recipient_id: str, text: str, page_id: str = ""):
    """ส่งคำตอบของบอท — แยกเป็นหลายบับเบิลถ้ามี MSG_SPLIT

    เหตุผล: คำถามที่อยู่รวมก้อนเดียวกับข้อความอื่นจะถูกลูกค้าสแกนผ่าน
    ส่งคำถามเป็นบับเบิลสุดท้ายเดี่ยวๆ ได้อัตราตอบกลับสูงกว่าชัดเจน
    """
    budget = TYPING_BUDGET_SEC
    for part in text.split(MSG_SPLIT):
        part = part.strip()
        if not part:
            continue
        if TYPING_ENABLED and budget > 0.2:
            pause = min(_typing_pause(part), budget)
            send_sender_action(recipient_id, "typing_on", page_id)
            time.sleep(pause)
            budget -= pause
        if not send_message(recipient_id, part, page_id):
            # ส่งถึงคนนี้ไม่ได้แล้ว (บล็อกเพจ / ปิดบัญชี / หลุด 24 ชม.)
            # บับเบิลที่เหลือก็ไม่ถึงเหมือนกัน ยิงต่อได้แค่ error ซ้ำ
            print(f"[SEND ABORT] {_mask(recipient_id)} ติดต่อไม่ได้ "
                  f"— ข้ามบับเบิลที่เหลือ")
            break


# Meta บอกว่า "ส่งถึงคนนี้ไม่ได้อีกแล้ว" — ยิงซ้ำก็ได้ error เดิม
# 551 = ลูกค้าบล็อกเพจ/ปิดบัญชี · 10 = หลุดหน้าต่าง 24 ชม. · 2018001 = id ใช้ไม่ได้
_UNREACHABLE_CODES = {551, 10}
_UNREACHABLE_SUBCODES = {1545041, 2018001, 2018108}


def _is_unreachable(resp) -> bool:
    try:
        err = (resp.json() or {}).get("error", {}) or {}
    except Exception:
        return False
    return (err.get("code") in _UNREACHABLE_CODES
            or err.get("error_subcode") in _UNREACHABLE_SUBCODES)


def send_message(recipient_id: str, text: str, page_id: str = "") -> bool:
    """ส่ง 1 บับเบิล — คืน False เมื่อส่งถึงลูกค้าคนนี้ไม่ได้อย่างถาวร
    (ให้ send_reply หยุดยิงบับเบิลที่เหลือ)"""
    token = page_token(page_id) if page_id else FB_PAGE_ACCESS_TOKEN
    if not token:
        print(f"[NO TOKEN] Would send to {recipient_id}: {text[:80]}")
        return True
    # Facebook จำกัด 2000 ตัวอักษร/ข้อความ
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)]
    for chunk in chunks:
        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": chunk},
            "messaging_type": "RESPONSE",
        }
        log_event(
            "SEND_REQUEST",
            f"POST {GRAPH_API_URL}  ->  recipient {_mask(recipient_id)}",
            {
                "method": "POST",
                "endpoint": GRAPH_API_URL,
                "messaging_type": "RESPONSE",
                "recipient.id": _mask(recipient_id),
                "message.text": chunk,
            },
        )
        try:
            resp = requests.post(
                GRAPH_API_URL,
                params={"access_token": token},
                json=payload,
                timeout=10,
            )
            if resp.status_code != 200:
                print(f"[FB SEND ERROR] {resp.status_code}: {resp.text[:200]}")
                log_event("SEND_ERROR", f"HTTP {resp.status_code}",
                          {"body": resp.text[:300]})
                if _is_unreachable(resp):
                    return False
            else:
                body = resp.json() if resp.content else {}
                print(f"[FB SEND OK] message_id={body.get('message_id', '-')}")
                log_event(
                    "SEND_RESPONSE",
                    f"HTTP 200  message_id = {body.get('message_id', '-')}",
                    {
                        "message_id": body.get("message_id", ""),
                        "recipient_id": _mask(body.get("recipient_id", "")),
                    },
                )
        except Exception as e:
            print(f"[FB SEND EXCEPTION] {e}")
            log_event("SEND_ERROR", f"exception: {e}")
    return True


# ======================================================
# Phase 5.4 — คอมเมนต์ใต้โพสต์ (Facebook feed / Instagram comments)
# ------------------------------------------------------
# คอมเมนต์ "ไม่ใช่" ข้อความ — ไม่มี PSID ให้ส่งตรง และไม่เข้า webhook messages
# Meta ให้ตอบได้ 2 ทาง ต่อ 1 คอมเมนต์:
#   1. ตอบใต้คอมเมนต์ (สาธารณะ)  POST /{comment_id}/comments
#   2. Private Reply เข้าแชท     POST /{page_id}/messages
#      recipient = {"comment_id": ...}   ** ได้ครั้งเดียวต่อคอมเมนต์เท่านั้น **
#      หลังจากนั้นต้องรอลูกค้าตอบก่อนถึงจะส่งเพิ่มได้
# ======================================================
COMMENT_REPLY = os.environ.get("COMMENT_REPLY", "1") == "1"       # ปิดได้ทันที
COMMENT_PUBLIC_REPLY = os.environ.get("COMMENT_PUBLIC_REPLY", "1") == "1"

# ตอบใต้คอมเมนต์ — สั้น สุภาพ ห้ามมีราคา/ส่วนลด/จำนวนห้อง (ข้อมูลชั้น 2)
# คนอื่นที่ผ่านมาเห็นด้วย เลยต้องไม่มีอะไรที่คู่แข่งเอาไปใช้ได้
PUBLIC_COMMENT_REPLY = "ขอบคุณที่สนใจครับ ส่งรายละเอียดให้ทางข้อความแล้วนะครับ"
PUBLIC_COMMENT_REPLY_F = "ขอบคุณที่สนใจค่ะ ส่งรายละเอียดให้ทางข้อความแล้วนะคะ"

_SEEN_COMMENTS: deque = deque(maxlen=500)
_SEEN_COMMENT_SET: set = set()


def _comment_handled(cid: str) -> bool:
    """Meta ยิง webhook คอมเมนต์ซ้ำได้เหมือน messages — กันตอบซ้ำ"""
    if not cid:
        return False
    if cid in _SEEN_COMMENT_SET:
        return True
    if len(_SEEN_COMMENTS) == _SEEN_COMMENTS.maxlen and _SEEN_COMMENTS:
        _SEEN_COMMENT_SET.discard(_SEEN_COMMENTS[0])
    _SEEN_COMMENTS.append(cid)
    _SEEN_COMMENT_SET.add(cid)
    return False


def reply_to_comment(comment_id: str, text: str, page_id: str = "") -> bool:
    """ตอบใต้คอมเมนต์แบบสาธารณะ (ต้องมีสิทธิ์ pages_manage_engagement)"""
    token = page_token(page_id)
    if not token:
        return False
    url = f"https://graph.facebook.com/v19.0/{comment_id}/comments"
    try:
        resp = requests.post(url, params={"access_token": token},
                             json={"message": text}, timeout=10)
        if resp.status_code != 200:
            print(f"[COMMENT REPLY ERROR] {resp.status_code}: {resp.text[:200]}")
            return False
        print(f"[COMMENT REPLY OK] {comment_id[:16]}...")
        return True
    except Exception as e:
        print(f"[COMMENT REPLY EXCEPTION] {e}")
        return False


def fetch_comment_text(comment_id: str, page_id: str = "") -> str:
    """อ่านข้อความคอมเมนต์จาก Graph API เมื่อ webhook ไม่ส่งมาให้"""
    token = page_token(page_id)
    if not token or not comment_id:
        return ""
    try:
        resp = requests.get(
            f"https://graph.facebook.com/v19.0/{comment_id}",
            params={"fields": "message,from", "access_token": token},
            timeout=8,
        )
        if resp.status_code != 200:
            print(f"[COMMENT FETCH ERROR] {resp.status_code}: {resp.text[:300]}")
            return ""
        return str((resp.json() or {}).get("message", "") or "")
    except Exception as e:
        print(f"[COMMENT FETCH EXCEPTION] {e}")
        return ""


def _comment_id_variants(comment_id: str, post_id: str) -> list:
    """รูปแบบ ID ที่ Meta ยอมรับสำหรับ private reply ต่างกันไปตามชนิดโพสต์

    feed webhook ส่ง comment_id มาได้ 2 แบบ: "123456" หรือ "POSTID_123456"
    ตัวไหนใช้ได้ขึ้นกับว่าโพสต์เป็นโพสต์ปกติ / โพสต์โฆษณา
    ลองทีละแบบดีกว่าเดา (error 1893060 = ID ใช้ไม่ได้)
    """
    out = [comment_id]
    if "_" in comment_id:
        tail = comment_id.split("_")[-1]
        if tail and tail != comment_id:
            out.append(tail)
    elif post_id:
        out.append(f"{post_id}_{comment_id}")
    return out


def private_reply(page_id: str, comment_id: str, text: str,
                  post_id: str = "") -> str:
    """ส่งข้อความเข้าแชทหาคนที่คอมเมนต์ — คืน PSID ถ้าสำเร็จ, "" ถ้าไม่

    ข้อจำกัดของ Meta: ยิงได้ครั้งเดียวต่อคอมเมนต์
    -> ต้องรวมทุกอย่างที่อยากพูดไว้ในข้อความเดียว ห้ามแตกบับเบิล
    """
    token = page_token(page_id)
    if not token or not page_id:
        return ""
    url = f"https://graph.facebook.com/v19.0/{page_id}/messages"
    last = ""
    for cid in _comment_id_variants(comment_id, post_id):
        psid = _try_private_reply(url, token, cid, text)
        if psid:
            return psid
        last = cid
    print(f"[PRIVATE REPLY] ลองครบทุกรูปแบบ ID แล้วไม่ผ่าน (ล่าสุด {last[:24]})")
    return ""


def _try_private_reply(url: str, token: str, comment_id: str, text: str) -> str:
    payload = {
        "recipient": {"comment_id": comment_id},
        "message": {"text": text[:1900]},
    }
    log_event("SEND_REQUEST", f"private reply -> comment {comment_id[:14]}…",
              {"endpoint": url, "message.text": text[:300]})
    try:
        resp = requests.post(url, params={"access_token": token},
                             json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"[PRIVATE REPLY ERROR] id={comment_id[:28]} "
                  f"{resp.status_code}: {resp.text[:600]}")
            log_event("SEND_ERROR", f"private reply HTTP {resp.status_code}",
                      {"body": resp.text[:400]})
            return ""
        body = resp.json() if resp.content else {}
        psid = str(body.get("recipient_id", ""))
        print(f"[PRIVATE REPLY OK] psid={_mask(psid)} "
              f"message_id={body.get('message_id', '-')}")
        log_event("SEND_RESPONSE", "private reply HTTP 200",
                  {"recipient_id": _mask(psid),
                   "message_id": body.get("message_id", "")})
        return psid
    except Exception as e:
        print(f"[PRIVATE REPLY EXCEPTION] {e}")
        log_event("SEND_ERROR", f"private reply exception: {e}")
        return ""


def process_comment(page_id: str, platform: str, value: dict):
    """รับคอมเมนต์ 1 รายการ -> ตอบใต้โพสต์ + ทักเข้าแชท + เปิดเคสในบอท"""
    if not COMMENT_REPLY:
        return

    # --- อ่านค่าจาก payload (FB กับ IG คนละรูปแบบ) ---
    if platform == "instagram":
        comment_id = str(value.get("id", ""))
        text = str(value.get("text", "") or "")
        frm = value.get("from", {}) or {}
        verb = "add"
        post_id = str((value.get("media", {}) or {}).get("id", ""))
    else:
        if value.get("item") != "comment":
            return                      # like / share / โพสต์เอง — ไม่เกี่ยว
        comment_id = str(value.get("comment_id", ""))
        text = str(value.get("message", "") or "")
        frm = value.get("from", {}) or {}
        verb = str(value.get("verb", "add"))
        post_id = str(value.get("post_id", ""))

    # ดูของจริงที่ Meta ส่งมา — เจอ 14 ส.ค. ว่าข้อความคอมเมนต์มาเป็นค่าว่าง
    # และ private reply ตอบกลับ error 1893060 (comment id ใช้ไม่ได้)
    # ต้องเห็น payload ดิบถึงจะรู้ว่าฟิลด์ไหนหาย/ผิดรูป
    print(f"[COMMENT RAW] {json.dumps(value, ensure_ascii=False)[:600]}")

    if verb not in ("add", "edited"):
        return                          # ลบคอมเมนต์ — ไม่ต้องทำอะไร
    if not comment_id:
        return

    from_id = str(frm.get("id", ""))
    from_name = str(frm.get("name") or frm.get("username") or "")

    # คอมเมนต์ของเพจเอง (รวมถึงคำตอบที่บอทเพิ่งตอบไป) -> ห้ามตอบ ไม่งั้นวนไม่จบ
    if from_id and from_id in (str(page_id), str(MAIN_PAGE_ID)):
        return
    if _comment_handled(comment_id):
        print(f"[DUPLICATE COMMENT] ข้าม {comment_id[:16]}...")
        return

    fb_page_id = resolve_ig_page(page_id) if platform == "instagram" else page_id
    gender = page_gender(fb_page_id)

    # Meta ไม่ส่งข้อความคอมเมนต์มาในบาง payload -> ไปอ่านเอาเองจาก Graph API
    # ไม่ได้ก็ไม่เป็นไร ใช้ "สนใจ" เป็นตัวตั้งต้นแทน (ลูกค้าคอมเมนต์ = สนใจอยู่แล้ว)
    if not text:
        text = fetch_comment_text(comment_id, fb_page_id)

    print(f"[COMMENT] ({platform}) {from_name or '-'} : {text[:60]}")
    log_event("INBOUND", f"comment ({platform}) from {from_name or '-'}",
              {"platform": platform, "comment_id": comment_id[:20],
               "text": text})

    # --- เดินบทสนทนาด้วยคีย์ชั่วคราว (ยังไม่รู้ PSID) ---
    tmp_key = f"c:{comment_id}"
    reply_text, _ = bot.process(
        text or "สนใจ", tmp_key, platform=platform,
        page_id=fb_page_id, brand=page_brand(fb_page_id),
        sheet_tab=page_tab(fb_page_id), gender=gender,
    )
    # Private Reply ส่งได้ข้อความเดียว -> รวมทุกบับเบิลเป็นก้อนเดียว
    one_shot = "\n\n".join(p.strip() for p in reply_text.split(MSG_SPLIT)
                           if p.strip())

    # bot.process เก็บ state ด้วยคีย์ "{page_id}:{user_id}" -> ต้องประกอบให้ตรง
    tmp_skey = f"{fb_page_id}:{tmp_key}" if fb_page_id else tmp_key

    psid = private_reply(fb_page_id, comment_id, one_shot, post_id)
    if psid:
        # ย้าย state ไปคีย์จริง -> พอลูกค้าตอบในแชท บอทคุยต่อได้เลย ไม่ทักซ้ำ
        new_skey = f"{fb_page_id}:{psid}" if fb_page_id else psid
        bot.rekey(tmp_skey, new_skey, psid)
    else:
        bot.drop(tmp_skey)              # ส่งไม่ได้ = อย่าทิ้ง state ค้างไว้
        print("[COMMENT] ทักแชทไม่สำเร็จ — ข้ามการตอบใต้โพสต์ด้วย")
        return

    if COMMENT_PUBLIC_REPLY:
        reply_to_comment(
            comment_id,
            PUBLIC_COMMENT_REPLY_F if gender == "female" else PUBLIC_COMMENT_REPLY,
            fb_page_id,
        )


def page_alert_psid(page_id: str) -> str:
    """ใครควรได้รับแจ้งเตือนลีดของเพจนี้

    สำคัญ: เพจอื่น Gift เป็นหัวหน้า ไม่ใช่คนตามลีด
    ถ้าไม่ได้กำหนด alert_psid ของเพจไว้ = ไม่แจ้งเตือนใครเลย
    (ลีดยังเข้าชีตครบเหมือนเดิม ไม่หาย — แค่ไม่ไปรบกวนคนผิด)
    """
    cfg = PAGES.get(str(page_id), {}) or {}
    psid = str(cfg.get("alert_psid", "")).strip()
    if psid:
        return psid
    # เพจหลักเท่านั้นที่ถอยไปหา Gift
    if not PAGES or str(page_id) == MAIN_PAGE_ID or not page_id:
        return GIFT_FB_PSID
    return ""


def alert_lead(sender_id: str, user_text: str, ad_id: str = "",
               page_id: str = ""):
    """แจ้งเตือนลีดเกรด A ไปหา 'เจ้าของเพจนั้น' ไม่ใช่ Gift เสมอไป"""
    target = page_alert_psid(page_id)
    if not target:
        print(f"[ALERT] เพจ {page_id} ยังไม่ได้กำหนดผู้รับแจ้งเตือน — ข้าม (ลีดอยู่ในชีตครบ)")
        return
    alert = (
        f"GRADE A LEAD ใหม่ — {page_brand(page_id)}\n"
        f"Sender: {sender_id}\n"
        f"ข้อความ: {user_text[:100]}\n"
        f"Ad ID: {ad_id or '-'}\n\n"
        "ติดต่อกลับใน Messenger ด่วนครับ"
    )
    send_message(target, alert, page_id)


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
def process_event(event: dict, platform: str = "facebook", page_id: str = ""):
    """ประมวลผล messaging event 1 รายการ (platform: facebook / instagram)"""
    sender_id = event.get("sender", {}).get("id", "")
    if not sender_id:
        return

    # echo = ข้อความที่ "เพจ" ส่งออก
    # ถ้าไม่มี app_id แปลว่า "คนพิมพ์เองจากกล่องข้อความเพจ" (เซลเข้ามาคุย)
    # -> เช็คว่าเป็นการทักเพื่อรับช่วงเคสไหม แล้วเก็บ log ข้อความเซลไว้ด้วย
    if event.get("message", {}).get("is_echo"):
        _msg = event.get("message", {})
        _from_app = bool(_msg.get("app_id"))
        _customer = event.get("recipient", {}).get("id", "")
        _txt = _msg.get("text", "") or ""
        if _customer and _txt:
            try:
                _res = bot.handle_page_echo(
                    _customer, _txt, platform=platform,
                    page_id=page_id, from_app=_from_app)
                if _res != "skip":
                    print(f"[ECHO] ({platform}) {_mask(_customer)} -> {_res} | {_txt[:40]!r}")
            except Exception as _e:
                print(f"[ECHO ERROR] {_e}")
        return

    # ข้าม event ที่เคยประมวลผลแล้ว (Meta ส่งซ้ำเมื่อรอ 200 นานเกินไป)
    # ต้องเช็คก่อนทุกอย่าง ไม่งั้นลูกค้าได้ข้อความซ้ำ + ชีตได้แถวซ้ำ
    _mid = event.get("message", {}).get("mid", "") or event.get("postback", {}).get("mid", "")
    if _already_handled(_mid):
        print(f"[DUPLICATE] ข้าม event ซ้ำ mid={_mid[:20]}... from {_mask(sender_id)}")
        return

    _kind = "postback (Get Started)" if event.get("postback") else "message"
    log_event(
        "INBOUND",
        f"webhook {platform} {_kind} from {_mask(sender_id)}",
        {
            "platform": platform,
            "sender.id": _mask(sender_id),
            "type": _kind,
            "text": event.get("message", {}).get("text", ""),
        },
    )

    # --- 1) เก็บ referral / ad_id ทุกรูปแบบ ---
    referral = extract_referral(event)
    if referral.get("ad_id") or referral.get("ref"):
        _pending_referrals[sender_id] = referral
        print(f"[REFERRAL] ({platform}) {sender_id[:10]}... ad_id={referral.get('ad_id') or '-'} "
              f"ref={referral.get('ref') or '-'} source={referral.get('source') or '-'}")

    # --- 2) postback (เช่น Get Started) — ทักทายลูกค้าทันที ---
    if event.get("postback") and not event.get("message"):
        reply_text, lead_grade = bot.process(
            "สวัสดี", sender_id,
            referral=_pending_referrals.get(sender_id, {}), platform=platform,
            page_id=page_id, brand=page_brand(page_id), sheet_tab=page_tab(page_id),
            gender=page_gender(page_id),
        )
        send_reply(sender_id, reply_text, page_id)
        print(f"[POSTBACK] ({platform}) {sender_id[:10]}... -> welcomed")
        return

    # --- 3) ข้อความ text ปกติ ---
    message = event.get("message", {})
    if not message or not message.get("text"):
        # referral-only event (messaging_referrals) — ไม่มีข้อความ ไม่ต้องตอบ
        return

    user_text = message["text"]
    lead_referral = referral or _pending_referrals.pop(sender_id, {})

    reply_text, lead_grade = bot.process(
        user_text, sender_id, referral=lead_referral, platform=platform,
        page_id=page_id, brand=page_brand(page_id), sheet_tab=page_tab(page_id),
            gender=page_gender(page_id),
    )
    if reply_text and reply_text.strip():
        send_reply(sender_id, reply_text, page_id)
    else:
        print(f"[SILENT] ({platform}) {_mask(sender_id)} ไม่ตอบ (เซลดูแลเอง)")

    if lead_grade == "A":
        alert_lead(sender_id, user_text, lead_referral.get("ad_id", ""), page_id)

    print(f"[MSG] ({platform}) {sender_id[:10]}... Grade={lead_grade or '-'} "
          f"| Q={user_text[:40]!r} | ad_id={lead_referral.get('ad_id') or '-'}")


# ======================================================
# Routes
# ======================================================
_CSS = """
:root{--navy:#17203D;--gold:#C9A142;--ink:#1b1b1b;--muted:#5b6472;--line:#e6e8ee}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
color:var(--ink);background:#f7f8fa;line-height:1.65}
header{background:var(--navy);color:#fff;padding:26px 20px}
header .w,main{max-width:820px;margin:0 auto}
header h1{margin:0;font-size:20px;letter-spacing:.2px}
header p{margin:6px 0 0;font-size:13px;color:#c9cfdd}
main{padding:28px 20px 60px}
h2{font-size:17px;margin:28px 0 8px;color:var(--navy)}
h2:first-child{margin-top:0}
p,li{font-size:14px;color:#26303f}
a{color:#1b4fa0}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin:14px 0}
.tag{display:inline-block;background:var(--gold);color:#2a2000;font-size:11px;font-weight:700;
padding:3px 9px;border-radius:20px;letter-spacing:.4px}
table{width:100%;border-collapse:collapse;margin:10px 0;font-size:13px}
th,td{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}
th{background:#eef0f5;color:var(--navy)}
footer{border-top:1px solid var(--line);margin-top:34px;padding-top:14px;font-size:12px;color:var(--muted)}
code{background:#eef0f5;padding:1px 5px;border-radius:4px;font-size:12.5px}
"""


def _page(title: str, body_html: str, subtitle: str = "") -> Response:
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · Wealth Estate</title><style>{_CSS}</style></head><body>
<header><div class="w"><span class="tag">WEALTH ESTATE</span>
<h1>{title}</h1><p>{subtitle or 'Messenger assistant for the Facebook Page "Wealth Estate : อสังหาคุ้มค่า"'}</p></div></header>
<main>{body_html}
<footer>Wealth Estate (Karnpanich Phutrakul, sole proprietor), Thailand ·
Contact: <a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a><br>
<a href="/">Home</a> · <a href="/privacy">Privacy Policy</a> · <a href="/data-deletion">Data Deletion</a>
</footer></main></body></html>"""
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/", methods=["GET"])
def root():
    body = """
<div class="card">
<h2>What this service is</h2>
<p>This is the backend server for an automated Messenger assistant used on a single Facebook Page
that we own — <b>"Wealth Estate : อสังหาคุ้มค่า"</b> (Page ID 108248514185091), a real-estate
investment consultancy in Thailand.</p>
<p>When someone messages our Page, the assistant replies instantly in Thai, answers frequently asked
questions about condominium rental investment, asks four short qualifying questions, and confirms
that a human consultant will call back. It only ever replies inside conversations the customer
started, within the standard 24-hour messaging window. It never sends promotional broadcasts and
uses no message tags.</p>
</div>
<div class="card">
<h2>Talk to the assistant</h2>
<p><a href="https://m.me/108248514185091">m.me/108248514185091</a> — this is a server-to-server
integration: there is no end-user login, app store download, or dashboard. The Messenger thread
<i>is</i> the user interface.</p>
</div>
<div class="card">
<h2>Technical endpoints</h2>
<table>
<tr><th>Path</th><th>Purpose</th></tr>
<tr><td><code>/health</code></td><td>Uptime health check</td></tr>
<tr><td><code>/webhook</code></td><td>Meta webhook receiver (GET verify / POST events)</td></tr>
<tr><td><code>/review-log</code></td><td>Live event viewer — access key required</td></tr>
<tr><td><code>/privacy</code></td><td>Privacy Policy</td></tr>
<tr><td><code>/data-deletion</code></td><td>How to request deletion of your data</td></tr>
</table>
</div>"""
    return _page("Automated Messenger Assistant", body)


@app.route("/privacy", methods=["GET"])
def privacy():
    body = f"""
<div class="card"><p><b>Last updated:</b> 28 July 2026 · Applies to the automated Messenger
assistant on the Facebook Page "Wealth Estate : อสังหาคุ้มค่า" (Page ID 108248514185091).</p>
<p><b>Data controller:</b> Karnpanich Phutrakul, individual sole proprietor, Thailand.
Contact: <a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a></p></div>

<div class="card">
<h2>1. What we collect</h2>
<p>Only what you send us in the Messenger conversation, plus what Meta provides with it:</p>
<table>
<tr><th>Data</th><th>Why</th></tr>
<tr><td>Your Messenger-scoped ID (PSID) and public first/last name</td>
<td>To hold the conversation and identify you when a consultant calls back</td></tr>
<tr><td>The messages you send us</td><td>To answer your questions</td></tr>
<tr><td>Your answers to four qualifying questions (investment purpose, occupation and income
range, existing loan obligations)</td><td>To prepare a relevant consultation</td></tr>
<tr><td>The phone number or LINE ID you choose to give us</td><td>So a human consultant can call
you back at your preferred contact point</td></tr>
<tr><td>Referral/ad identifiers Meta attaches when you arrive from an ad</td>
<td>To know which campaign you came from</td></tr>
</table>
<p>We do not ask for, and do not want, national ID numbers, bank account numbers, financial
statements, or health information through Messenger.</p>
</div>

<div class="card">
<h2>2. Why we use it (lawful basis)</h2>
<p>To respond to an enquiry you initiated and to provide the consultation you asked for
(performance of a service at your request), and — where required by Thailand's Personal Data
Protection Act — on the basis of the consent you give by sending us your contact details.
We do not sell your data and we do not use it for automated decision-making that has a legal
effect on you.</p>
</div>

<div class="card">
<h2>3. Who processes it for us</h2>
<table>
<tr><th>Processor</th><th>Role</th><th>Location</th></tr>
<tr><td>Railway Corp.</td><td>Hosts the server that runs the assistant</td><td>United States</td></tr>
<tr><td>Google LLC</td><td>Google Sheets (our lead record) and Google Calendar (call-back
scheduling), via Apps Script</td><td>United States</td></tr>
<tr><td>Anthropic PBC</td><td>Claude API — generates the assistant's reply text. Message content is
sent for processing and is not used to train models.</td><td>United States</td></tr>
<tr><td>Meta Platforms</td><td>Delivers the Messenger conversation itself</td><td>United States</td></tr>
</table>
<p>These transfers leave Thailand. We rely on the processors' contractual data-protection terms.
We share your data with no one else — no advertisers, no data brokers, no other agencies.</p>
</div>

<div class="card">
<h2>4. How long we keep it</h2>
<p>Conversation records and lead details stay in our Google Sheet for up to <b>24 months</b> from
your last contact, then are deleted. The server keeps only a short in-memory log of recent events
(about the last 80, with identifiers partly masked) which is erased whenever the server restarts —
nothing is written to disk on the server.</p>
</div>

<div class="card">
<h2>5. Your rights</h2>
<p>Under Thailand's PDPA you may ask us to access, correct, export, or delete your data, withdraw
consent, or object to our use of it. Email
<a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a> or message the Page — we answer within 30 days.
See <a href="/data-deletion">Data Deletion</a> for the fastest route.</p>
</div>

<div class="card">
<h2>6. Children</h2>
<p>The service is for adults considering property investment. We do not knowingly collect data from
anyone under 18; if you tell us you are a minor we delete the conversation record.</p>
</div>

<div class="card">
<h2>7. Changes</h2>
<p>If this policy changes we update the date at the top of this page.</p>
</div>"""
    return _page("Privacy Policy", body)


@app.route("/data-deletion", methods=["GET"])
def data_deletion():
    body = f"""
<div class="card">
<h2>Ask us to delete your data</h2>
<p>Two ways — either works, no account or login needed:</p>
<ol>
<li><b>Email</b> <a href="mailto:{CONTACT_EMAIL}?subject=Delete%20my%20data">{CONTACT_EMAIL}</a>
with the subject "Delete my data", from any address, telling us the name you used on Messenger.</li>
<li><b>Message the Page</b> at <a href="{PAGE_URL}">m.me/108248514185091</a> and type
<code>ลบข้อมูลของฉัน</code> or <code>delete my data</code>. A human on our team picks this up.</li>
</ol>
<p>We confirm and complete deletion within <b>30 days</b>. We remove your row from our Google Sheet
lead record and any call-back entry from our calendar. The server holds no persistent copy.</p>
<p>Deleting your Facebook or Messenger conversation on your side removes your copy of the thread but
does not by itself reach our lead record — send us one of the two requests above as well.</p>
</div>
<div class="card">
<h2>What we cannot delete</h2>
<p>Records we must keep to meet Thai tax or accounting obligations (for example, if you went on to
sign a contract with us) are kept for the period the law requires. We will tell you if this applies
to you and delete everything else.</p>
</div>"""
    return _page("Data Deletion Request", body)


@app.route("/health", methods=["GET"])
def health():
    # เพิ่ม 19 ส.ค. 2026 — ใช้ดูว่า Postgres เฟส 1 เขียนเข้าจริงไหม
    # เปิด /health?pg=1 แล้วดู written เพิ่มขึ้นเรื่อยๆ errors ต้องเป็น 0
    out = {"status": "ok"}
    if request.args.get("pg"):
        try:
            from bot_logic import pg_store
            out["pg"] = pg_store.stats() if pg_store else {"enabled": False}
        except Exception as e:
            out["pg"] = {"error": str(e)[:200]}
    return jsonify(out)


# ------------------------------------------------------
# /review-log — App UI จริง (read-only) สำหรับผู้ตรวจ Meta + ทีมงาน
# ต้องมี ?key=... ตรงกับ REVIEW_LOG_KEY
# ------------------------------------------------------
_REVIEW_LOG_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live Event Log — Wealth Estate Messenger Assistant</title><style>
:root{--navy:#17203D;--gold:#C9A142}
*{box-sizing:border-box}
body{margin:0;background:#0e1424;color:#e8ecf5;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{background:var(--navy);padding:14px 18px;border-bottom:2px solid var(--gold)}
h1{margin:0;font-size:15px;font-family:-apple-system,Segoe UI,Roboto,sans-serif}
header p{margin:5px 0 0;font-size:12px;color:#a9b4cc;font-family:-apple-system,Segoe UI,Roboto,sans-serif}
#live{display:inline-block;width:8px;height:8px;border-radius:50%;background:#35d07f;margin-right:6px;
animation:b 1.4s infinite}@keyframes b{50%{opacity:.25}}
main{padding:14px 18px 40px;max-width:1000px}
.row{border-left:3px solid #3a4665;padding:8px 12px;margin:8px 0;background:#161e33;border-radius:0 6px 6px 0}
.row.INBOUND{border-left-color:#4da3ff}
.row.SEND_REQUEST{border-left-color:var(--gold)}
.row.SEND_RESPONSE{border-left-color:#35d07f}
.row.SEND_ERROR{border-left-color:#ff5f56}
.k{font-size:10.5px;letter-spacing:.6px;font-weight:700;padding:2px 7px;border-radius:3px;background:#2a3452;color:#cfd8ee}
.ts{color:#7f8bab;font-size:11.5px;margin-left:8px}
.sum{margin:6px 0 0;font-size:13px;word-break:break-all}
.det{margin:6px 0 0;font-size:11.5px;color:#9fb0d0;white-space:pre-wrap;word-break:break-all}
.empty{color:#7f8bab;font-size:13px;padding:18px 0}
.note{background:#161e33;border:1px solid #2a3452;border-radius:8px;padding:12px 14px;margin:0 0 14px;
font-size:12.5px;color:#b9c4dc;font-family:-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.6}
</style></head><body>
<header><h1><span id="live"></span>Live event log — Wealth Estate Messenger assistant</h1>
<p>Page 108248514185091 · times ICT (UTC+7) · newest first · auto-refresh 1.5s</p></header>
<main>
<div class="note"><b>For Meta App Review:</b> this page is our app's own operator interface. Every
reply our app sends to Messenger appears here as a <b>SEND_REQUEST</b> (the outbound
<code>POST /me/messages</code> call our server makes) immediately followed by the
<b>SEND_RESPONSE</b> carrying the <code>message_id</code> Meta returns. Match that
<code>message_id</code> against the message delivered in the native Messenger client to confirm the
message was sent by this app. Recipient identifiers are partly masked — this log is operational,
not a customer database.</div>
<div id="rows"><div class="empty">Waiting for events… send a message to the Page to see one appear.</div></div>
</main>
<script>
const KEY = new URLSearchParams(location.search).get('key') || '';
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function tick(){
  try{
    const r = await fetch('/review-log/data?key=' + encodeURIComponent(KEY));
    if(!r.ok) return;
    const d = await r.json();
    const box = document.getElementById('rows');
    if(!d.events.length) return;
    box.innerHTML = d.events.map(e => {
      const det = Object.keys(e.detail||{}).length
        ? '<div class="det">' + esc(JSON.stringify(e.detail, null, 2)) + '</div>' : '';
      return '<div class="row ' + e.kind + '"><span class="k">' + e.kind +
             '</span><span class="ts">' + esc(e.ts) + ' ICT</span>' +
             '<div class="sum">' + esc(e.summary) + '</div>' + det + '</div>';
    }).join('');
  }catch(err){}
}
tick(); setInterval(tick, 1500);
</script></body></html>"""


@app.route("/review-log", methods=["GET"])
def review_log():
    if request.args.get("key", "") != REVIEW_LOG_KEY:
        return _page(
            "Access key required",
            '<div class="card"><p>This page shows a live operational log and needs an access key.'
            ' Append <code>?key=…</code> to the URL. Meta App Review: the key is in the'
            ' “instructions for accessing the app” field of our submission.</p></div>',
        ), 401
    return Response(_REVIEW_LOG_HTML, mimetype="text/html; charset=utf-8")


@app.route("/review-log/data", methods=["GET"])
def review_log_data():
    if request.args.get("key", "") != REVIEW_LOG_KEY:
        return jsonify({"error": "invalid key"}), 401
    return jsonify({"events": list(_EVENT_LOG)})


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Facebook เรียกครั้งเดียวตอนตั้งค่า webhook"""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == FB_VERIFY_TOKEN:
        print("[WEBHOOK] Verified by Facebook")
        log_event("SYSTEM", "webhook verified by Meta")
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def receive_webhook():
    """รับ event จาก Facebook Page + Instagram"""
    body = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not verify_fb_signature(body, signature):
        print("[WEBHOOK] Signature verification FAILED")
        return "Unauthorized", 401

    data = request.get_json(silent=True)
    obj = (data or {}).get("object", "")
    if obj not in ("page", "instagram"):
        print(f"[WEBHOOK IGNORED] object={obj!r} — ไม่ใช่ page/instagram")
        return jsonify({"status": "ignored"})
    platform = "instagram" if obj == "instagram" else "facebook"

    for entry in data.get("entry", []):
        # entry.id = เพจที่ลูกค้าทักเข้ามา -> ใช้เลือกโทเค็น/แบรนด์/แท็บชีต
        # หมายเหตุ: ถ้า object = "instagram" entry.id คือ IG account id (คนละตัวกับ Page ID)
        raw_entry_id = str(entry.get("id", ""))
        # 19 ส.ค. 2026 — log 1 บรรทัดต่อ entry เพื่อจับว่า event หายตรงไหน
        # เคสจริง: Facebook ยิง POST /webhook เข้ามา 200 OK ใน 2 ms แต่ไม่มี log เลย
        # แปลว่ามีทางออกเงียบๆ อยู่ ต้องรู้ให้ได้ว่า entry มีอะไรมาบ้าง
        # (log แค่จำนวนกับธง ไม่เก็บเนื้อข้อความ — PDPA)
        _msgs = entry.get("messaging", []) or []
        _m0 = (_msgs[0].get("message") or {}) if _msgs else {}
        _chgs = entry.get("changes", []) or []
        # ชื่อ field ของ changes — 19 ส.ค. 2026 เจอว่าเว็บฮุกส่วนใหญ่เป็น changes
        # ที่โค้ดทิ้งทั้งหมด (รับแค่ feed/comments) ต้องรู้ว่ามันคืออะไร
        # เผื่อเป็นลีดที่เราทิ้งอยู่ทุกวันโดยไม่รู้ตัว
        _fields = ",".join(sorted({str(c.get("field", "?")) for c in _chgs})) or "-"
        print(f"[WEBHOOK IN] entry={raw_entry_id} msg={len(_msgs)} "
              f"chg={len(_chgs)}({_fields}) "
              f"sby={len(entry.get('standby', []) or [])} "
              f"echo={bool(_m0.get('is_echo'))} app={bool(_m0.get('app_id'))} "
              f"keys={sorted(entry.keys())}")
        page_id = raw_entry_id
        if platform == "instagram":
            print(f"[IG ENTRY] ig_account_id={page_id}")
            page_id = resolve_ig_page(page_id)

        # คอมเมนต์ใต้โพสต์ มาคนละช่องกับข้อความ (changes ไม่ใช่ messaging)
        # FB = field "feed" | IG = field "comments"
        for change in entry.get("changes", []):
            field = str(change.get("field", ""))
            if field not in ("feed", "comments"):
                continue
            try:
                process_comment(raw_entry_id, platform,
                                change.get("value", {}) or {})
            except Exception as e:
                print(f"[COMMENT ERROR] {e} | "
                      f"change={json.dumps(change, ensure_ascii=False)[:300]}")

        # กรณีปกติ: แอพเป็น Primary Receiver
        for event in entry.get("messaging", []):
            try:
                process_event(event, platform, page_id)
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
    print(f"WEC Bot v3.3 starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
