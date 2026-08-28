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
import threading
from collections import deque

import requests
from flask import Flask, request, jsonify, Response
from dotenv import load_dotenv
from bot_logic import BotEngine, BOT_PAUSE_PAGES, OBSERVE_PAGES, page_observe
# r71 (Gift 23 ส.ค. 2026) — โหมดดับอารมณ์
from bot_logic import _lead_states, _conversations
from wec_calm import (detect_anger, calm_mode, detect_stop, stop_mode,
                      detect_info_ask, ZONE_REASON,
                      INFO_AGAIN_MSGS, INFO_AGAIN_TAIL, INFO_AGAIN_MAX,
                      detect_zone, detect_budget, zone_ack_line,
                      ZONE_NONE_MSG, zone_detail, build_zone_menu,
                      detect_size_ask, SIZE_ANSWER_GENERAL,
                      is_canned_ad_reply, ZONE_MENU_Q_PHONE,
                      build_kb_zone_block, LABEL_QUALIFIED, LABEL_ERR_TERMS,
                      detect_comment_interest, PUBLIC_COMMENT_THANKS,
                      PUBLIC_COMMENT_THANKS_F,
                      calm_stack_guard, MAX_BUBBLES)
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
# r70 (Gift 22 ส.ค. 2026) — เพจที่เปิดไว้แต่ "ยังไม่เปิดบอทตอบ"
# ------------------------------------------------------
# ประกาศในตัวเพจเอง ไม่ใช่สวิตช์ลับแยกต่างหาก:
#   "1234567890": {"brand": "อสังหาเงินล้านคอนโด ปล่อยเช่า",
#                  "tab": "Leads_MillionCondo", "reply": false}
# รับ "mode": "observe" ด้วย เผื่ออ่านง่ายกว่าในสายตาคนตั้งค่า
# ไม่ประกาศ = ตอบตามปกติ ไม่มีค่า default ที่ปิดเพจให้เอง
# ======================================================
def page_reply_off(page_id: str) -> bool:
    cfg = PAGES.get(str(page_id), {}) or {}
    if cfg.get("reply") is False:
        return True
    return str(cfg.get("mode", "")).strip().lower() in (
        "observe", "silent", "listen", "log")


OBSERVE_PAGES.update(str(_p) for _p in (PAGES or {}) if page_reply_off(_p))
if OBSERVE_PAGES:
    print("[OBSERVE] เพจโหมดเก็บข้อมูล (ไม่ตอบ แต่เก็บลีด/รีพอร์ตครบ): "
          + ", ".join(sorted(OBSERVE_PAGES)))


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


# ======================================================
# r55b — บอกให้ชัดว่า "ตอนนี้เครื่องกำลังรันโค้ดรอบไหน"
# คืน 20 ส.ค. 2026 เจอปัญหา: merge แล้วแต่ Railway ไม่หยิบไป deploy
# มองจากข้างนอกไม่มีทางรู้เลยว่าที่รันอยู่คือรอบเก่าหรือใหม่
# ดูได้ที่ log ตอนบูต หรือเปิด /health
# ======================================================
BOT_REVISION = "r93"
print(f"[VERSION] WEC bot รอบ {BOT_REVISION}")

FB_VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "wec_bot_verify_2569")
FB_APP_SECRET = os.environ.get("FB_APP_SECRET", "")  # เว้นว่าง = dev mode (ข้าม signature check)
GIFT_FB_PSID = os.environ.get("GIFT_FB_PSID", "")

GRAPH_API_URL = "https://graph.facebook.com/v19.0/me/messages"

# คีย์เปิดหน้า /review-log (ให้ผู้ตรวจ Meta / ทีมงานเท่านั้น)
REVIEW_LOG_KEY = os.environ.get("REVIEW_LOG_KEY", "wec-review-2026")
# กุญแจของ /import — ไม่ตั้งก็ใช้ตัวเดียวกับ REVIEW_LOG_KEY
IMPORT_KEY = os.environ.get("IMPORT_KEY", "").strip() or REVIEW_LOG_KEY

# ข้อมูลติดต่อที่โชว์ในหน้า privacy / data-deletion
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "karnpanich.phutrakul@gmail.com")
PAGE_URL = "https://m.me/108248514185091"

app = Flask(__name__)
# ======================================================
# r73 (Gift 23 ส.ค. 2026) — เก็บกวาดสรรพนามชายที่หลุดในเพจผู้หญิง
# ------------------------------------------------------
# เคสจริง: "ตรงนี้ผมต้องขอทราบรายได้ต่อเดือน...ค่ะ"  ← ผม + ค่ะ ประโยคเดียวกัน
# ต้นเหตุ: _FEMALE_PRONOUN ใน bot_logic เป็นลิสต์ตายตัวแค่ 7 แพทเทิร์น
#   (ผมรบกวน · เดี๋ยวผม · ผมช่วย · ผมส่ง · ผมขอ · ผมจะ · ผมคัด)
#   "ผมต้อง" ไม่อยู่ในลิสต์ -> หลุดออกไปหาลูกค้า
# ข้อความที่ Claude แต่งเองใช้สรรพนามอิสระ ลิสต์ตายตัวตามไม่ทันแน่นอน
#
# แก้: ครอบ to_female อีกชั้น แล้วกวาด "ผม" ที่ตามด้วยอักษรไทยทิ้งให้หมด
# ปลอดภัยเพราะ to_female ทำงานกับ "ข้อความบอท" เท่านั้น ไม่แตะข้อความลูกค้า
# และคำที่มี "ผม" แปลว่าเส้นผม (ทรงผม/เส้นผม/ผมร่วง) ไม่มีทางโผล่ในบอทคอนโด
# ======================================================
_MALE_PRON_RE = re.compile(r"ผม(?=[ก-๛])")
try:
    import bot_logic as _bl
    _ORIG_TO_FEMALE = _bl.to_female

    def _to_female_strict(text):
        try:
            t = _ORIG_TO_FEMALE(text)
        except Exception as _e:
            print(f"[FEMALE FIX] ของเดิมพัง ใช้ข้อความดิบ: {_e}")
            t = text
        try:
            t2 = _MALE_PRON_RE.sub("", t or "")
        except Exception as _e:
            print(f"[FEMALE FIX] กวาดไม่ได้ ส่งของเดิม: {_e}")
            return t
        if t2 != t:
            print(f"[FEMALE FIX] เก็บ 'ผม' ที่หลุด | {t[:60]!r}")
        return t2

    _bl.to_female = _to_female_strict
    print("[FEMALE FIX] เปิดตัวกวาดสรรพนามชายแล้ว")
except Exception as _e:
    print(f"[FEMALE FIX] ต่อไม่ติด: {_e}")


# ======================================================
# r76 (Gift เคาะ 24 ส.ค. 2569) — "ให้ก่อนขอ": ขยายปากทางของ _is_zone_ask
# ------------------------------------------------------
# ใช้ ZONE_MENU_MSG/ZONE_MENU_Q ชุดเดิมที่ผ่าน compliance แล้ว
# ไม่แต่งตัวเลขใหม่แม้แต่ตัวเดียว — แค่ให้ "ขอรายละเอียด / อยากดูคร่าวๆ /
# ส่งข้อมูลมาหน่อย" เข้าทางเดียวกับคำว่า "ทำเล"
# ======================================================
try:
    _ORIG_ZONE_ASK = _bl._is_zone_ask

    def _zone_or_info_ask(msg):
        # ⚠️ ตัวนี้ถูกเรียกจากใน bot_logic โดยตรง — โยน exception เมื่อไหร่
        # บอทเงียบใส่ลูกค้าทันที เลยต้องกันทุกชั้น (บทเรียน r73)
        try:
            if _ORIG_ZONE_ASK(msg):
                return True
        except Exception as _e:
            print(f"[INFO ASK] ของเดิมพัง: {_e}")
            return False
        # r79 — ข้อความสำเร็จรูปจากปุ่มตอบโฆษณาของ Meta ไม่ใช่คำถามราคา
        # (Gift 24 ส.ค.: ลูกค้ากดปุ่ม แล้วบอทเทราคาทั้งชุดใส่ เขาไม่อ่าน)
        try:
            if is_canned_ad_reply(msg):
                print(f"[CANNED AD] ข้อความสำเร็จรูปของ Meta "
                      f"— ไม่ใช่คำถามราคา ตอบสั้นแล้วขอเบอร์ "
                      f"| {(msg or '')[:40]!r}")
                return False
        except Exception:
            pass
        try:
            if detect_info_ask(msg):
                print(f"[INFO ASK] ลูกค้าขอข้อมูลกว้างๆ "
                      f"— ตอบย่าน+ช่วงราคาก่อน | {(msg or '')[:50]!r}")
                return True
        except Exception as _e:
            print(f"[INFO ASK] ตัวจับพัง ใช้ของเดิมแทน: {_e}")
        return False

    _bl._is_zone_ask = _zone_or_info_ask
    print("[INFO ASK] เปิดโหมดให้ก่อนขอแล้ว")
except Exception as _e:
    print(f"[INFO ASK] ต่อไม่ติด: {_e}")


# ======================================================
# r78 — เปลี่ยนตัวเลขในเมนูย่าน เป็นของจริงจากชีต SaleTeam
# ------------------------------------------------------
# `ZONE_MENU_MSG` เป็นตัวแปรระดับโมดูลใน bot_logic (import มาจาก faq_data)
# ฟังก์ชันที่ใช้มันอ่านค่าตอนถูกเรียก -> ทับจากตรงนี้ได้เลย
# ไม่ต้องแตะ bot_logic.py (448KB อัปขึ้น GitHub ไม่ได้) และไม่ต้องแตะ faq_data.py
# ⚠️ ทับเฉพาะ "ตัวเลขราคา/ขนาดห้อง" ชุดย่านยังเป็น 4 ย่านเดิมที่ Gift อนุมัติ
# ⚠️ ไม่มีชื่อโครงการแม้แต่คำเดียว — ดูตัวตรวจ _PROJECT_WORDS ข้างล่าง
# ======================================================
try:
    _OLD_ZONE_MENU = _bl.ZONE_MENU_MSG
    _NEW_ZONE_MENU = build_zone_menu()
    _bl.ZONE_MENU_MSG = _NEW_ZONE_MENU
    _bl.ZONE_MENU_Q = ZONE_MENU_Q_PHONE      # r79 — ขอเบอร์แทนถามงบ/โซน
    print(f"[ZONE PRICE] ใช้ราคาจริงจากชีตแล้ว ({len(_NEW_ZONE_MENU)} ตัวอักษร)")
    print("[LEAD FIRST] ต่อท้ายชุดย่านด้วยการขอเบอร์แล้ว")
    # r79 — FAQ ข้อ "ราคาเท่าไหร่" ยังใช้ช่วงเก่า 2-8 ล้าน ไม่ตรงกับชีต
    _OLD_RANGE = "เริ่มต้นประมาณ 2 ล้านบาท ไปถึงประมาณ 8 ล้านบาท"
    _NEW_RANGE = "เริ่มราว 2.1 ล้าน ถึง 11 ล้าน"
    try:
        import faq_data as _fq
        _fixed = 0
        for _item in getattr(_fq, "FAQ_DATABASE", []) or []:
            _a = _item.get("answer") if isinstance(_item, dict) else None
            if _a and _OLD_RANGE in _a:
                _item["answer"] = _a.replace(_OLD_RANGE, _NEW_RANGE)
                _fixed += 1
        print(f"[ZONE PRICE] แก้ช่วงราคาเก่าใน FAQ {_fixed} ข้อ")
    except Exception as _e:
        print(f"[ZONE PRICE] แก้ FAQ ไม่ได้: {_e}")

except Exception as _e:
    print(f"[ZONE PRICE] ทับเมนูไม่ได้ ใช้ของเดิม: {_e}")


# กันเหนียว: ชื่อโครงการห้ามหลุดออกไปหาลูกค้าเด็ดขาด (กฎข้อ 1)
# ถ้าวันหลังมีคนเผลอเอาชื่อโครงการมาใส่ ตัวนี้จะดักไว้ตอนบูต
_PROJECT_WORDS = (
    "seaside", "collect", "gravit", "tripple", "livin", "residence",
    "ressidence", "wutthakat", "ratchada 32", "chokchai 4", "i con",
    "c one", "thonglor", "pinklao", "rungsit", "ramintra", "phetkasem",
)
try:
    _low = (_bl.ZONE_MENU_MSG or "").lower()
    _hit = [w for w in _PROJECT_WORDS if w in _low]
    if _hit:
        print(f"[ZONE PRICE] 🔴 เจอชื่อโครงการในเมนู {_hit} — ถอยกลับใช้ของเดิม")
        _bl.ZONE_MENU_MSG = _OLD_ZONE_MENU
    else:
        print("[ZONE PRICE] ตรวจแล้ว ไม่มีชื่อโครงการในเมนู")
except Exception as _e:
    print(f"[ZONE PRICE] ตรวจชื่อโครงการไม่ได้: {_e}")
# r80 — บล็อกทำเลใน system prompt (ที่ AI เห็น) ยังเป็นราคาเก่าทั้งชุด
# r78 แก้แค่ข้อความตายตัว -> ในแชทเดียวกันได้ราคาคนละชุด ขัดกันเอง
# ตรงนี้ทับให้ตรงกับ ZONE_PRICES ชุดเดียวกับเมนู
# (Gift เคาะ 25 ส.ค.: ทองหล่อ "พูดช่วงราคาได้" — ของเดิมเขียนห้ามเดาตัวเลข)
_KB_START = "[กลุ่มเสนอก่อน — เอ่ยถึงกลุ่มนี้เป็นหลักเสมอ]"
_KB_END = 'ชลบุรี ห้องใหญ่ 50 ตร.ม. ประมาณ 4.3-6.9 ล้าน (มีของจริง แต่ "ไม่ใช่พัทยา/จอมเทียน")'
try:
    _sp = _bl.WEC_SYSTEM_PROMPT
    _i = _sp.find(_KB_START)
    _j = _sp.find(_KB_END)
    if _i < 0 or _j < 0:
        print("[KB SYNC] ⚠️ หาบล็อกทำเลใน system prompt ไม่เจอ — ปล่อยของเดิมไว้")
    else:
        _new_block = build_kb_zone_block()
        _low = _new_block.lower()
        _bad = [w for w in _PROJECT_WORDS if w in _low]
        if _bad:
            print(f"[KB SYNC] 🔴 เจอชื่อโครงการในบล็อกใหม่ {_bad} — ไม่ทับ")
        else:
            _sp2 = _sp[:_i] + _new_block + _sp[_j + len(_KB_END):]
            _bl.WEC_SYSTEM_PROMPT = _sp2
            print(f"[KB SYNC] ซิงก์ราคาใน system prompt แล้ว "
                  f"({len(_sp)} -> {len(_sp2)} ตัวอักษร)")
            if "ราคายังไม่ยืนยัน" in _sp2:
                print("[KB SYNC] ⚠️ ยังเหลือคำว่า 'ราคายังไม่ยืนยัน' อยู่ในพรอมต์")
except Exception as _e:
    print(f"[KB SYNC] ทับไม่ได้ ใช้ของเดิม: {_e}")



def _is_info_or_zone_ask(msg: str) -> bool:
    """ลูกค้ากำลังถามเรื่อง ข้อมูล/ทำเล/ราคา อยู่ไหม (ไม่โยน exception เด็ดขาด)"""
    try:
        if detect_info_ask(msg):
            return True
    except Exception:
        pass
    try:
        return bool(_bl._is_zone_ask(msg))
    except Exception:
        return False


# r77 — ลายเซ็นของ FALLBACK_MSG ไว้ตรวจว่าบอทกำลังจะถามโซน/งบซ้ำ
try:
    from faq_data import FALLBACK_MSG as _FALLBACK_RAW
except Exception:
    _FALLBACK_RAW = "ลูกค้าสนใจโซนไหน และงบประมาณคร่าวๆ"


# ======================================================
# r81 — ติดป้าย "ผ่านเกณฑ์รายได้" ให้แชท (Custom Labels API)
# ------------------------------------------------------
# ป้ายโผล่ในกล่องข้อความเพจ เซลกรองดูได้เลยว่าใครควรโทรก่อน
# ⚠️ กติกาเหล็ก: พังยังไงก็ห้ามกระทบการตอบลูกค้า — ยิงในเธรดแยก + กลืน error
# ======================================================
# ใช้เวอร์ชันเดียวกับที่ subscribe เพจ (v22.0) — main.py ไม่มี FB_GRAPH_URL
_LABEL_GRAPH = "https://graph.facebook.com/v22.0"

_LABEL_ID_CACHE: dict[str, str] = {}     # page_id -> label_id
_LABEL_BLOCKED: set[str] = set()         # เพจที่ยังไม่ได้กดยอมรับข้อกำหนด
_LABEL_DONE: set[str] = set()            # "page:psid" ที่ติดป้ายไปแล้ว


def _label_id_for_page(page_id: str, token: str) -> str:
    """หา label_id ของป้ายบนเพจนั้น ไม่มีก็สร้าง (คืน "" ถ้าทำไม่ได้)"""
    cached = _LABEL_ID_CACHE.get(page_id)
    if cached:
        return cached
    try:
        r = requests.get(f"{_LABEL_GRAPH}/me/custom_labels",
                         params={"fields": "name,page_label_name",
                                 "access_token": token}, timeout=6)
        j = r.json()
        if "error" in j:
            code = (j["error"] or {}).get("code")
            if code == LABEL_ERR_TERMS:
                _LABEL_BLOCKED.add(page_id)
                print(f"[LABEL] ⚠️ เพจ {page_id} ยังไม่ได้กดยอมรับ "
                      f"\"ข้อกำหนดการติดต่อของเพจ\" — ติดป้ายไม่ได้ "
                      f"(Gift ต้องกดเองใน Business Suite)")
            else:
                print(f"[LABEL] อ่านป้ายไม่ได้ page={page_id} "
                      f"{code}: {str((j['error'] or {}).get('message'))[:90]}")
            return ""
        for it in (j.get("data") or []):
            nm = it.get("page_label_name") or it.get("name") or ""
            if nm == LABEL_QUALIFIED and it.get("id"):
                _LABEL_ID_CACHE[page_id] = it["id"]
                print(f"[LABEL] เจอป้ายเดิมบนเพจ {page_id} แล้ว")
                return it["id"]
        # ไม่มี -> สร้างใหม่
        r2 = requests.post(f"{_LABEL_GRAPH}/me/custom_labels",
                           data={"page_label_name": LABEL_QUALIFIED,
                                 "access_token": token}, timeout=6)
        j2 = r2.json()
        if "error" in j2:
            code = (j2["error"] or {}).get("code")
            if code == LABEL_ERR_TERMS:
                _LABEL_BLOCKED.add(page_id)
            print(f"[LABEL] สร้างป้ายไม่ได้ page={page_id} "
                  f"{code}: {str((j2['error'] or {}).get('message'))[:90]}")
            return ""
        lid = j2.get("id") or ""
        if lid:
            _LABEL_ID_CACHE[page_id] = lid
            print(f"[LABEL] สร้างป้าย \"{LABEL_QUALIFIED}\" บนเพจ {page_id} แล้ว")
        return lid
    except Exception as e:
        print(f"[LABEL] หา/สร้างป้ายพลาด page={page_id}: {str(e)[:90]}")
        return ""


def _label_worker(psid: str, page_id: str):
    try:
        token = page_token(page_id)
        if not token:
            print(f"[LABEL] ไม่มี token ของเพจ {page_id} — ข้าม")
            return
        lid = _label_id_for_page(page_id, token)
        if not lid:
            return
        r = requests.post(f"{_LABEL_GRAPH}/{lid}/label",
                          data={"user": psid, "access_token": token}, timeout=6)
        j = r.json()
        if "error" in j:
            code = (j["error"] or {}).get("code")
            if code == LABEL_ERR_TERMS:
                _LABEL_BLOCKED.add(page_id)
            print(f"[LABEL] ติดป้ายไม่สำเร็จ {_mask(psid)} page={page_id} "
                  f"{code}: {str((j['error'] or {}).get('message'))[:90]}")
            return
        _LABEL_DONE.add(f"{page_id}:{psid}")
        print(f"[LABEL OK] ติดป้าย \"{LABEL_QUALIFIED}\" ให้ {_mask(psid)} "
              f"page={page_id} แล้ว")
    except Exception as e:
        print(f"[LABEL] เธรดติดป้ายพลาด: {str(e)[:90]}")


def label_if_qualified(psid: str, page_id: str, state: dict):
    """ผ่านเกณฑ์รายได้ -> ติดป้ายให้แชท (ยิงเบื้องหลัง ไม่หน่วงการตอบ)

    Gift เคาะ 26 ส.ค.: ติดเฉพาะ "ผ่านเกณฑ์รายได้" ไม่ผูกกับเกรด
    """
    try:
        if not psid or not page_id or page_id in _LABEL_BLOCKED:
            return
        if f"{page_id}:{psid}" in _LABEL_DONE:
            return                      # ติดไปแล้ว ไม่ยิงซ้ำ
        data = (state or {}).get("data") or {}
        info = BotEngine._income_numbers(data)
        if info.get("qualified25k") != "1":
            return
        threading.Thread(target=_label_worker, args=(psid, page_id),
                         daemon=True).start()
    except Exception as e:
        print(f"[LABEL] เช็คเกณฑ์พลาด ข้ามไป: {str(e)[:90]}")


# ======================================================
# r82 — โชว์ "ตอนดึงชื่อ" ใน /review-log ให้ผู้ตรวจ Meta เห็น
# ------------------------------------------------------
# แอพเราเป็น server-to-server ไม่มีหน้าจอของตัวเอง
# วิธีที่เคยผ่าน App Review มาแล้ว (14 ส.ค.) คือถ่ายคู่กัน:
#   จอขวา = แอพ Messenger/IG จริง · จอซ้าย = /review-log ของเราเอง
#   แล้วจับคู่ timestamp + message_id ให้ผู้ตรวจเห็นว่าเป็นแอพเรายิงจริง
#
# แต่ /review-log ปัจจุบันบันทึกแค่ INBOUND / SEND_REQUEST / SEND_RESPONSE
# **ไม่มี event ตอนดึงชื่อเลย** ซึ่งเป็นหัวใจของ Business Asset User Profile Access
# -> ถ่ายวิดีโอไปก็ไม่มีอะไรให้ผู้ตรวจดูว่าเราใช้สิทธิ์นี้ยังไง
#
# ตรงนี้ครอบ _get_fb_name ให้ยิง event 2 ตัว: PROFILE_REQUEST / PROFILE_RESPONSE
# (ชื่อจริงถูก _scrub ใน log_event อยู่แล้ว — ไม่ใช่ฐานข้อมูลลูกค้า)
# ======================================================
try:
    _ORIG_GET_NAME = _bl.BotEngine._get_fb_name

    def _get_fb_name_logged(self, user_id, platform="facebook", page_id=""):
        _fields = "name,username" if platform == "instagram" else "first_name,last_name"
        try:
            log_event("PROFILE_REQUEST",
                      f"GET https://graph.facebook.com/{_mask(user_id)}"
                      f"?fields={_fields}",
                      {"endpoint": "https://graph.facebook.com/<PSID>",
                       "fields": _fields,
                       "platform": platform,
                       "page_id": page_id or "-",
                       "why": "Business Asset User Profile Access — read the "
                              "person's name so a human consultant can greet "
                              "them correctly on the call-back they asked for"})
        except Exception:
            pass
        nm = ""
        try:
            nm = _ORIG_GET_NAME(self, user_id, platform, page_id)
        finally:
            try:
                if nm:
                    log_event("PROFILE_RESPONSE",
                              f"HTTP 200 name = {nm}",
                              {"name": nm, "platform": platform,
                               "page_id": page_id or "-"})
                else:
                    log_event("PROFILE_ERROR",
                              "name not returned — permission is still "
                              "Standard Access (advanced access requested)",
                              {"platform": platform, "page_id": page_id or "-"})
            except Exception:
                pass
        return nm

    _bl.BotEngine._get_fb_name = _get_fb_name_logged
    print("[REVIEW LOG] เปิดบันทึกจังหวะดึงชื่อแล้ว (PROFILE_REQUEST/RESPONSE)")
except Exception as _e:
    print(f"[REVIEW LOG] ต่อตัวบันทึกชื่อไม่ติด: {_e}")


# ======================================================
# r85 — ไม่เก็บ state ของ "คีย์ชั่วคราวคอมเมนต์" ลง Postgres
# ------------------------------------------------------
# ของเดิม: บอทประมวลผลคอมเมนต์ด้วยคีย์ `c:{comment_id}` แล้ว save_turn
# เก็บลง Postgres ด้วย psid = "c:..." -> แถวขยะที่ส่งข้อความหาไม่ได้
# พอ rekey ไปคีย์จริง แถวเก่ายังค้าง -> ตัวกวาดทักกลับหยิบมายิง -> 400
# ตรงนี้ตัดตั้งแต่ต้นทาง ไม่ให้เกิดแถวขยะเพิ่ม
# (แถวเก่าที่ค้างอยู่แล้ว ชั้นกันที่ send_reply รับไว้)
# ======================================================
try:
    if _bl.pg_store is not None:
        _ORIG_SAVE_TURN = _bl.pg_store.save_turn

        def _save_turn_guarded(page_id, psid, state, *a, **k):
            try:
                if str(psid or "").startswith("c:"):
                    return None          # คีย์ชั่วคราว ไม่ต้องจำถาวร
            except Exception:
                pass
            return _ORIG_SAVE_TURN(page_id, psid, state, *a, **k)

        _bl.pg_store.save_turn = _save_turn_guarded
        print("[PG GUARD] ไม่เก็บคีย์ชั่วคราวของคอมเมนต์ลง Postgres แล้ว")
except Exception as _e:
    print(f"[PG GUARD] ต่อไม่ติด: {_e}")


def _norm_msg(s: str) -> str:
    """ตัดช่องว่าง/ตัวคั่นบับเบิลออก เพื่อเทียบว่าเป็นข้อความเดียวกันเป๊ะไหม"""
    return re.sub(r"\s+", "", (s or "")).replace("|", "")


# ⚠️ ห้ามใช้ FALLBACK_MSG เต็มประโยคมาเทียบ — เพจผู้หญิงจะถูกแปลง
# "ครับ" -> "ค่ะ" ทำให้เทียบไม่ติด (เจอจริงตอนเทสต์ r77)
# ใช้เฉพาะท่อนกลางที่ไม่มีคำลงท้ายบอกเพศ
_FALLBACK_KEY = _norm_msg("ลูกค้าสนใจโซนไหน และงบประมาณคร่าวๆ")
if _FALLBACK_KEY not in _norm_msg(_FALLBACK_RAW):
    print("[NO REASK ZONE] ⚠️ คีย์เทียบไม่ตรงกับ FALLBACK_MSG แล้ว — ตัวเลิกถามซ้ำจะไม่ทำงาน")
else:
    print("[NO REASK ZONE] เปิดตัวเลิกถามโซน/งบซ้ำแล้ว")


def _recent_bot_msgs(user_id: str, n: int = 6):
    """ข้อความล่าสุดที่บอทส่งไปในแชทนี้ (ไล่จากใหม่ไปเก่า)"""
    try:
        h = _conversations.get(user_id) or []
    except Exception:
        return []
    out = []
    for turn in reversed(h):
        if turn.get("role") == "assistant":
            out.append(turn.get("content") or "")
            if len(out) >= n:
                break
    return out


# ======================================================
# r71 (Gift 23 ส.ค. 2026) — โหมดดับอารมณ์ (ครอบ BotEngine ไว้ชั้นนอก)
# ------------------------------------------------------
# ทำไมครอบตรงนี้ แทนที่จะแก้ข้างใน bot_logic.py:
#   bot_logic.py 448KB อัปผ่านหน้าเว็บ GitHub ไม่ผ่านสักครั้ง (ลอง 4 รอบ
#   ค้างที่ "Uploading" ทุกรอบ) — ครอบชั้นนอกได้ผลเหมือนกันทุกประการ
#   และดีตรงที่ bot_logic.py ไม่ต้องแตะเลยแม้แต่บรรทัดเดียว
#
# กลไก 3 จังหวะ:
#   1) ก่อนเข้า process — จับอารมณ์ได้ -> ยัดธง handover ลง state ก่อน
#      ทำให้ process ตัวเดิมวิ่งเข้าทาง "เซลดูแลเอง": เก็บ log + ดูดข้อมูล
#      + แจกเคสครบ แต่ "ไม่รันสคริปต์ขาย" (ไม่ถามรายได้ ไม่เสนอโครงการ)
#   2) process ตัวเดิมคืน "" มา (เพราะโดนธง handover)
#   3) เอาข้อความดับอารมณ์ไปแทน + คืนธง "!" ให้ข้างล่างปลุกคน
#
# เรียก _resolve_state เองก่อน 1 ครั้งเพื่อให้มี state ให้ยัดธงตั้งแต่
# ข้อความแรกของแชทใหม่ — ปลอดภัยเพราะฟังก์ชันนี้ idempotent
# (คีย์ซ้ำ = คืนก้อนเดิม) และ process จะเรียกซ้ำอีกทีเองอยู่แล้ว
# ======================================================
class CalmBotEngine(BotEngine):
    def _zone_reply_in_context(self, st, reply, gender):
        """ประกอบคำตอบชุดทำเลใหม่ ให้ต่อจากที่ลูกค้าเคยบอกไว้ (Gift 24 ส.ค.)

        Gift: "ต่อเนื่องจากแชทเก่าๆ ที่เค้าคุยมา ไม่ใช่ไปถามโพล่งๆ"
        โครงคำตอบ 2 บับเบิล:
          1) ทวนสิ่งที่ลูกค้าเคยบอก (ถ้ามี) + ชุดย่าน+ช่วงราคาเดิม
          2) เหตุผลว่าทำไมเป็นช่วง + คำถามข้อที่ "ยังไม่ได้คำตอบ" เท่านั้น
        ใช้ _next_missing ตัวเดียวกับที่บอทใช้อยู่ จึงไม่มีทางถามข้อที่ตอบไปแล้ว

        พังเมื่อไหร่ = คืนคำตอบเดิมไปเลย ห้ามทำให้บอทเงียบเด็ดขาด
        (บทเรียน r73: ของที่ผมใส่เองทำบอทเงียบใส่ลูกค้าจริงมาแล้ว)
        """
        try:
            fem = (gender == "female")
            conv = (lambda t: _bl.to_female(t)) if fem else (lambda t: t)
            parts = [p.strip() for p in (reply or "").split(MSG_SPLIT) if p.strip()]
            if not parts:
                return reply
            menu = parts[0]
            data = st.get("data") or {}

            head = ""
            try:
                recap = (self._recap_short(data) or "").strip()
            except Exception:
                recap = ""
            if recap:
                head = conv(f"จากที่คุยไว้ก่อนหน้า — {recap} นะครับ") + "\n"

            # r79 — ให้ราคาแล้วรีบขอเบอร์ ไม่สวนคำถามคัดกรองยาวๆ ต่อ
            # (Gift: "เน้นขอเบอร์ lead เป็นหลัก · เค้าขี้เกียจอ่าน")
            if data.get("contact"):
                ask = ""            # ได้เบอร์แล้ว ไม่ต้องขอซ้ำ
                try:
                    _fld, ask = self._next_missing(data, st)
                except Exception:
                    ask = ""
                ask = conv(ask) if ask else ""
            else:
                ask = conv(ZONE_MENU_Q_PHONE)

            tail = conv(ZONE_REASON) + ("\n" + ask if ask else "")
            print(f"[ZONE CONTEXT] ต่อจากของเดิม recap={bool(recap)} "
                  f"ขอเบอร์={not bool(data.get('contact'))}")
            return head + menu + MSG_SPLIT + tail
        except Exception as _e:
            print(f"[ZONE CONTEXT ERROR] {_e} — ใช้คำตอบเดิม")
            return reply

    def _info_again_reply(self, st, reply, gender):
        """ลูกค้าถามเรื่องข้อมูล/ราคา ซ้ำอีกรอบ หลังส่งชุดย่านไปแล้ว (r76b)

        Gift 24 ส.ค.: "ไม่ใช่ไปถามโพล่งๆ"
        ของเดิมสวนคำถามคัดกรองเดิมเป๊ะกลับไปทุกครั้ง ลูกค้าอ่านว่าไม่ฟัง
        ตัวนี้เขียนใหม่เป็น: รับรู้ว่าเพิ่งส่งไปแล้ว + เหตุผล + ขอต่อ 1 ข้อ

        ⚠️ ห้ามคืนค่าว่างเด็ดขาด พังเมื่อไหร่ = คืนคำตอบเดิม (บทเรียน r73)
        """
        try:
            n = int(st.get("info_again_n") or 0)
            if n >= INFO_AGAIN_MAX:
                return reply
            fem = (gender == "female")
            conv = (lambda t: _bl.to_female(t)) if fem else (lambda t: t)

            q = ""
            try:
                _fld, q = self._next_missing(st.get("data") or {}, st)
            except Exception:
                q = ""
            tail = conv(q) if q else conv(INFO_AGAIN_TAIL)
            body = INFO_AGAIN_MSGS[n % len(INFO_AGAIN_MSGS)]
            new = conv(body) + MSG_SPLIT + tail
            if not new.strip():
                return reply

            st["info_again_n"] = n + 1
            print(f"[INFO AGAIN] ถามซ้ำเรื่องข้อมูล/ราคา ครั้งที่ {n + 1}"
                  f"/{INFO_AGAIN_MAX} — ตอบแบบรับรู้แทนถามซ้ำ")
            return new
        except Exception as _e:
            print(f"[INFO AGAIN ERROR] {_e} — ใช้คำตอบเดิม")
            return reply

    def _catch_zone_budget(self, st, user_message):
        """เก็บคำตอบ "โซน" กับ "งบ" ที่ลูกค้าเพิ่งบอก (r77)

        เก็บลง data["zone"] / data["budget"] ซึ่ง **อยู่นอก FIELD_ORDER**
        จึงไม่ไปแย่งคิวคำถามหลักของบอท (บทเรียน 18 ส.ค. เรื่องเบอร์โทรหาย)
        คืน (zone, budget, zone_none) ของ "รอบนี้" ไว้ให้คนเรียกใช้ต่อ
        """
        zone = budget = znone = ""
        try:
            zone, znone = detect_zone(user_message)
            budget = detect_budget(user_message)
            if not (zone or budget or znone):
                return "", "", ""
            data = st.setdefault("data", {})
            if zone and data.get("zone") != zone:
                data["zone"] = zone
                print(f"[CATCH ZONE] เก็บโซนที่ลูกค้าบอก -> {zone}")
            if budget and data.get("budget") != budget:
                data["budget"] = budget
                print(f"[CATCH BUDGET] เก็บงบที่ลูกค้าบอก -> {budget}")
            if znone:
                data["zone_none"] = znone
                print(f"[CATCH ZONE] ย่านที่ยังไม่มีของ -> {znone}")
        except Exception as _e:
            print(f"[CATCH ZONE ERROR] {_e}")
        return zone, budget, znone

    def _fix_repeat_zone_q(self, st, reply, gender, zone, budget, znone):
        """บอทกำลังจะถาม "สนใจโซนไหน และงบเท่าไหร่" ทั้งที่รู้คำตอบแล้ว (r77)

        เจอบั๊กจริง: ลูกค้าพิมพ์ "อยากได้แถวรัชดา" -> บอทตอบ FALLBACK_MSG
        ซึ่งถามเรื่องโซนซ้ำ เพราะ FIELD_ORDER ไม่มีช่องเก็บโซนเลย

        เขียนคำตอบใหม่เป็น: รับทราบสิ่งที่เขาบอก + ถามข้อที่ยังไม่ได้จริงๆ
        ⚠️ คืนค่าว่างไม่ได้เด็ดขาด พังเมื่อไหร่ = คืนคำตอบเดิม
        """
        try:
            data = st.get("data") or {}
            known_zone = zone or data.get("zone") or ""
            known_budget = budget or data.get("budget") or ""
            if not (known_zone or known_budget or znone):
                return reply
            if _FALLBACK_KEY not in _norm_msg(reply or ""):
                return reply          # ไม่ใช่คำถามตัวที่มีปัญหา ปล่อยผ่าน

            fem = (gender == "female")
            conv = (lambda t: _bl.to_female(t)) if fem else (lambda t: t)

            # พูด "รับทราบ ย่าน..." เฉพาะรอบที่ลูกค้าเพิ่งบอกมาจริงๆ
            # รอบถัดๆ ไปที่แค่ "จำได้" ให้เงียบเรื่องนี้ แล้วตัดคำถามซ้ำทิ้งพอ
            # (ไม่งั้นทวนย่านเดิมทุกเทิร์น น่ารำคาญพอกับถามซ้ำ)
            said_now = bool(zone or budget or znone)
            if znone:
                head = conv(ZONE_NONE_MSG.format(none=znone))
            elif said_now:
                head = conv(zone_ack_line(known_zone, known_budget))
                # r78 — ต่อด้วยช่วงราคา+ขนาดห้องจริงของย่านนั้น
                _d = zone_detail(known_zone) if known_zone else ""
                if _d:
                    head += "\n" + conv(_d)
            else:
                head = ""

            q = ""
            try:
                _fld, q = self._next_missing(data, st)
            except Exception:
                q = ""
            if not q:
                # ไม่มีข้อไหนขาดแล้ว -> ถามสิ่งที่ยังไม่รู้ระหว่างโซน/งบ
                if known_zone and not known_budget:
                    q = "งบคร่าวๆ ที่วางไว้ประมาณเท่าไหร่ครับ ตัวเลขกลมๆ ก็พอครับ"
                elif known_budget and not known_zone:
                    q = "แล้วสนใจย่านไหนเป็นพิเศษไหมครับ"
            # บับเบิลอื่นของคำตอบเดิม (ตัดเฉพาะตัวที่ถามโซน/งบซ้ำออก)
            _rest = [x.strip() for x in (reply or "").split(MSG_SPLIT)
                     if x.strip() and _FALLBACK_KEY not in _norm_msg(x)]
            parts = [p for p in ([head] if head.strip() else []) if p]
            if q:
                parts.append(conv(q))
            elif _rest:
                parts.append(_rest[0])
            elif not parts:
                return reply          # ไม่เหลืออะไรเลย -> ใช้ของเดิม ห้ามเงียบ
            out = MSG_SPLIT.join(parts)
            if not out.strip():
                return reply
            print(f"[NO REASK ZONE] รู้โซน/งบแล้ว เลิกถามซ้ำ "
                  f"| zone={known_zone!r} budget={known_budget!r} "
                  f"none={znone!r} ทวน={said_now}")
            return out
        except Exception as _e:
            print(f"[NO REASK ZONE ERROR] {_e} — ใช้คำตอบเดิม")
            return reply

    def _answer_size_ask(self, st, reply, gender):
        """ลูกค้าถามเรื่อง "ขนาดห้อง" ตรงๆ — ตอบได้แล้วตั้งแต่ r78

        ก่อนหน้านี้บอทไม่เคยตอบเรื่องขนาดห้องเลย เพราะ KB ไม่มีข้อมูล
        ตอนนี้มีของจริงจากชีต SaleTeam แล้ว (ทำเล + ช่วงราคา + ขนาดห้อง)
        รู้ย่าน -> บอกขนาดของย่านนั้น · ไม่รู้ย่าน -> บอกช่วงรวมแล้วชวนบอกย่าน

        ⚠️ ต่อหน้าคำตอบเดิม ไม่ทับ ไม่ทำให้เงียบ · จำกัด 2 ครั้งต่อแชท
        """
        try:
            n = int(st.get("size_ans_n") or 0)
            if n >= 2:
                return reply
            fem = (gender == "female")
            conv = (lambda t: _bl.to_female(t)) if fem else (lambda t: t)
            zone = (st.get("data") or {}).get("zone") or ""
            ans = zone_detail(zone) if zone else ""
            if not ans:
                ans = SIZE_ANSWER_GENERAL
            ans = conv(ans)
            if not ans.strip():
                return reply
            st["size_ans_n"] = n + 1
            print(f"[SIZE ASK] ตอบเรื่องขนาดห้อง ครั้งที่ {n + 1}/2 "
                  f"| zone={zone!r}")
            return ans + (MSG_SPLIT + reply if reply and reply.strip() else "")
        except Exception as _e:
            print(f"[SIZE ASK ERROR] {_e} — ใช้คำตอบเดิม")
            return reply

    def _canned_ad_reply(self, st, reply, gender):
        """ลูกค้ากดปุ่มตอบโฆษณาของ Meta — ตอบสั้น ไม่สวนคำถามห้วนๆ (r79)

        Gift 24 ส.ค.: "การตอบแบบนี้เค้าขี้เกียจอ่าน · เน้นขอเบอร์เป็นหลัก"
        ของเดิมตกมาที่ FALLBACK_MSG = "สนใจโซนไหน และงบประมาณเท่าไหร่"
        ซึ่งเป็นคำถามห้วนๆ ตั้งแต่ข้อความแรกที่ลูกค้ายังไม่ได้พูดอะไรเลย

        ตัดบับเบิลนั้นทิ้ง เหลือคำถามธรรมชาติของบอทเอง
        ⚠️ ห้ามคืนค่าว่าง พังเมื่อไหร่ = คืนคำตอบเดิม
        """
        try:
            parts = [x.strip() for x in (reply or "").split(MSG_SPLIT) if x.strip()]
            keep = [x for x in parts if _FALLBACK_KEY not in _norm_msg(x)]
            if not keep or keep == parts:
                return reply
            print(f"[CANNED AD] ตัดคำถามห้วนๆ ออก {len(parts)} -> {len(keep)} บับเบิล")
            return MSG_SPLIT.join(keep)
        except Exception as _e:
            print(f"[CANNED AD ERROR] {_e} — ใช้คำตอบเดิม")
            return reply

    # r86 — ด่านสุดท้ายของ _decide: กันบับเบิลซ้อนในเทิร์นเดียว
    # bot_logic._decide เรียก self._dedupe_exact(bubbles) เป็นตัวสุดท้าย
    # ก่อน return จึงเป็นจุดเดียวที่เห็นบับเบิลครบทั้งชุด
    # พังเมื่อไหร่ = คืนของเดิมทั้งชุด ห้ามทำให้บอทเงียบ (บทเรียน r73/r75)
    @staticmethod
    def _dedupe_exact(bubbles):
        try:
            out = BotEngine._dedupe_exact(bubbles)
        except Exception as _e:
            print(f"[STACK GUARD] _dedupe_exact เดิมพลาด: {_e} — ใช้ของดิบ")
            out = bubbles
        try:
            new = calm_stack_guard(out)
            if new:
                return new
            print("[STACK GUARD] ผลลัพธ์ว่าง — คืนของเดิมแทน")
        except Exception as _e:
            print(f"[STACK GUARD ERROR] {_e} — ใช้บับเบิลเดิม")
        return out

    def process(self, user_message, user_id, referral=None,
                platform="facebook", page_id="", brand="",
                sheet_tab="", gender=""):
        lvl = detect_anger(user_message)
        stop = detect_stop(user_message)          # r73 — ลูกค้าสั่งหยุด
        skey = f"{page_id}:{user_id}" if page_id else user_id

        # r75 — ล้างธงที่ตัวกันข้อความซ้ำของ r73 ทำค้างไว้
        # แชทที่โดนโยนเข้าโหมดรอคนตอนนั้น จะติดเงียบยาว 6 ชม. ถ้าไม่ล้าง
        # (ทำแบบเดียวกับตัวล้าง "(ปิดบอทรายเพจ)" ของ r68)
        _bad = _lead_states.get(skey)
        if _bad is not None and _bad.get("handover_by") == "(บอทเริ่มพูดวน — รอผู้จัดการ)":
            _bad["handover"] = False
            _bad["handover_by"] = ""
            _bad["handover_ended_at"] = int(time.time())
            _bad.pop("dup_blocked", None)
            print(f"[DUP UNSTICK] {_mask(user_id)} ล้างธงที่ r73 ทำค้าง — บอทกลับมาคุยได้")

        if lvl or stop:
            try:
                st, _ = self._resolve_state(user_id, platform,
                                            referral or {}, skey, page_id)
                _t = int(time.time())
                st["handover"] = True
                st["handover_at"] = _t
                st["handover_sale_at"] = _t
                st["handover_by"] = "(เคสร้องเรียน — รอผู้จัดการ)"
            except Exception as _e:
                print(f"[CALM PRELOCK] {_e}")

        _zone_before = bool((_lead_states.get(skey) or {}).get("zone_told"))
        try:
            _info_ask_now = _is_info_or_zone_ask(user_message)
        except Exception as _e:
            print(f"[INFO ASK] เช็คไม่ได้ ข้ามไป: {_e}")
            _info_ask_now = False

        # r77 — เก็บโซน/งบ "ก่อน" ให้บอทคิด จะได้ไม่ถามสิ่งที่เพิ่งตอบไป
        _z = _b = _zn = ""
        try:
            _size_ask = detect_size_ask(user_message)
        except Exception:
            _size_ask = False
        try:
            _canned = is_canned_ad_reply(user_message)
        except Exception:
            _canned = False
        try:
            _zst0, _ = self._resolve_state(user_id, platform,
                                           referral or {}, skey, page_id)
            _z, _b, _zn = self._catch_zone_budget(_zst0, user_message)
        except Exception as _e:
            print(f"[CATCH ZONE] ข้ามรอบนี้: {_e}")

        reply, grade = super().process(
            user_message, user_id, referral=referral, platform=platform,
            page_id=page_id, brand=brand, sheet_tab=sheet_tab, gender=gender)

        # r76 — ชุดทำเลเพิ่งยิงรอบนี้ -> ประกอบใหม่ให้ต่อจากที่คุยค้างไว้
        if reply:
            try:
                _zst = _lead_states.get(skey)
                if _zst is not None and _zst.get("zone_told"):
                    if not _zone_before:
                        _new = self._zone_reply_in_context(_zst, reply, gender)
                    elif _info_ask_now and not (lvl or stop):
                        # r76b — ส่งชุดย่านไปแล้ว แต่ลูกค้ายังถามเรื่องข้อมูล/ราคาอีก
                        _new = self._info_again_reply(_zst, reply, gender)
                    else:
                        _new = reply
                    # กันชั้นสุดท้าย: ของใหม่ต้องไม่ว่าง ไม่งั้นใช้ของเดิม
                    if _new and str(_new).strip():
                        reply = _new
                # r77 — เลิกถามโซน/งบ ที่ลูกค้าตอบไปแล้ว
                if _zst is not None and (_z or _b or _zn
                                         or (_zst.get("data") or {}).get("zone")
                                         or (_zst.get("data") or {}).get("budget")):
                    _new2 = self._fix_repeat_zone_q(_zst, reply, gender,
                                                    _z, _b, _zn)
                    if _new2 and str(_new2).strip():
                        reply = _new2
                # r78 — ลูกค้าถามขนาดห้องตรงๆ ตอบได้แล้ว
                if _zst is not None and _size_ask and not (lvl or stop):
                    _new3 = self._answer_size_ask(_zst, reply, gender)
                    if _new3 and str(_new3).strip():
                        reply = _new3
                # r79 — ข้อความสำเร็จรูปของ Meta ตอบสั้นๆ พอ
                if _canned and not (lvl or stop):
                    _new4 = self._canned_ad_reply(_zst, reply, gender)
                    if _new4 and str(_new4).strip():
                        reply = _new4
            except Exception as _e:
                print(f"[ZONE WRAP ERROR] {_e} — ใช้คำตอบเดิม")

        # r75 (24 ส.ค. 2569) — ⚠️ ตัวกันข้อความซ้ำของ r73 ถูกถอดออกแล้ว
        # ------------------------------------------------------------------
        # ของเดิม: ข้อความซ้ำกับ 6 บับเบิลหลัง = ไม่ส่ง + โยนเข้าโหมดรอคน
        # ผลจริงบน production 24 ส.ค. 09:51-11:00 น.: จับผิด 30+ ครั้งใน 1 ชม.
        # ไปโดนข้อความปกติล้วนๆ เช่นประโยคเปิด "เราช่วยดูว่าลงทุนคอนโด..."
        # และคำถามคัดกรองทั่วไป -> บอทเงียบใส่ลูกค้าจริง + แชทติดโหมดรอคน 6 ชม.
        #
        # สาเหตุที่คิดผิด: บอทตัวนี้ "ตั้งใจ" พูดประโยคเดิมซ้ำในหลายจังหวะ
        # (ทักกลับ · ถามซ้ำเมื่อลูกค้าตอบไม่ตรง · เปลี่ยนหัวข้อแล้ววนกลับ)
        # ข้อความซ้ำ != บอทพัง สำหรับสคริปต์แบบนี้
        #
        # ตอนนี้เหลือแค่ "บันทึกไว้ดู" ไม่บล็อก ไม่โยนเข้าโหมดรอคน
        # ถ้าจะกันจริง ต้องกันเฉพาะ STATUS_MSG/FALLBACK และนับ >= 3 ครั้งขึ้นไป
        if reply and reply.strip() and not (lvl or stop):
            _n = _norm_msg(reply)
            if _n and any(_norm_msg(p) == _n for p in _recent_bot_msgs(user_id)):
                print(f"[DUP SEEN] {_mask(user_id)} ข้อความซ้ำ (ส่งตามปกติ) "
                      f"| {reply[:50]!r}")

        if not (lvl or stop):
            return reply, grade

        st = _lead_states.get(skey)
        if st is None:
            print("[CALM] ไม่พบ state หลัง process — ตอบดับอารมณ์ + ปลุกคนอย่างเดียว")
            return CALM_FALLBACK, "!"

        if stop:
            calm, flag = stop_mode(self, user_id, st, user_message, stop)
        else:
            calm, flag = calm_mode(self, user_id, st, user_message, lvl)

        # Chat_Log เพิ่งบันทึกไปว่า "(เซลดูแลเอง — บอทไม่ตอบ)" ซึ่งไม่จริง
        # เพราะรอบนี้บอทตอบจริง -> เขียนอีกแถวให้ตรงกับสิ่งที่ส่งออกไป
        if calm:
            try:
                self._log(user_id, "", calm)
                self._persist(user_id, st, "", calm, "angry")
            except Exception as _e:
                print(f"[CALM LOG] {_e}")
        return calm, flag


CALM_FALLBACK = ("รับเรื่องแล้วครับ ผมส่งต่อให้ผู้จัดการแล้วนะครับ "
                 "รบกวนขอชื่อ-นามสกุล เบอร์ติดต่อ และเวลาที่สะดวก "
                 "จะให้โทรกลับหาคุณโดยตรงครับ")

bot = CalmBotEngine()

# ======================================================
# r51 (Gift 20 ส.ค. 2026) — ทักกลับเคสที่เงียบไปแต่ข้อมูลยังไม่ครบ
# "ถ้าข้อมูลที่เค้าส่งมาล่าสุดยังไม่ครบและขาดการติดต่อไป ให้ทักหาลูกค้า"
#
# ทำไมยิงที่ 20 ชม. ไม่ใช่ 2 วันตามที่คิดไว้ตอนแรก:
#   Meta ปิดหน้าต่างส่งข้อความที่ 24 ชม. หลังลูกค้าพิมพ์ครั้งสุดท้าย
#   และเราส่งแบบ messaging_type=RESPONSE ไม่มี message tag
#   -> เลย 24 ชม. ยิงไปก็ได้ error 10 กลับมา ลูกค้าไม่เห็นอะไรเลย
#
# กันพัง 3 ชั้น:
#   1. FOLLOWUP_ENABLED=0 = ปิดสวิตช์ทันที ไม่ต้อง deploy
#   2. เพดานต่อรอบ (FOLLOWUP_MAX_PER_RUN) — บั๊กเดียวห้ามยิงเป็นร้อยคน
#   3. เช็คธงใน RAM ซ้ำก่อนส่งจริงทุกเคส (เซลอาจเพิ่งรับช่วงไปเมื่อกี้)
# ======================================================
FOLLOWUP_ENABLED = os.environ.get("FOLLOWUP_ENABLED", "1") != "0"
FOLLOWUP_SWEEP_SEC = int(os.environ.get("FOLLOWUP_SWEEP_SEC", "1800"))
# ตื่นมาแล้วรอสักพักก่อนกวาดรอบแรก — ให้ Postgres/ชีตพร้อมก่อน
FOLLOWUP_BOOT_DELAY = int(os.environ.get("FOLLOWUP_BOOT_DELAY", "300"))


def _followup_sweeper():
    if not FOLLOWUP_ENABLED or FOLLOWUP_SWEEP_SEC <= 0:
        print("[FOLLOWUP] ปิดไว้ (FOLLOWUP_ENABLED=0 หรือ FOLLOWUP_SWEEP_SEC<=0)")
        return
    time.sleep(FOLLOWUP_BOOT_DELAY)
    while True:
        try:
            res = bot.sweep_followups(send=send_reply, dry=False)
            _n = res.get("sent", 0)
            if _n or res.get("ready_count"):
                print(f"[FOLLOWUP] กวาด {res.get('scanned', 0)} เคส "
                      f"· เข้าเกณฑ์ {res.get('ready_count', 0)} "
                      f"· ทักไป {_n}")
            for it in (res.get("ready") or []):
                if it.get("ok") is False:
                    print(f"[FOLLOWUP SKIP] {str(it.get('psid'))[:8]}... "
                          f"{it.get('error')}")
        except Exception as e:
            print(f"[FOLLOWUP ERROR] {e}")
        time.sleep(FOLLOWUP_SWEEP_SEC)


try:
    threading.Thread(target=_followup_sweeper, daemon=True,
                     name="wec-followup").start()
    print(f"[FOLLOWUP] เปิดแล้ว — กวาดทุก {FOLLOWUP_SWEEP_SEC}s "
          f"(เริ่มอีก {FOLLOWUP_BOOT_DELAY}s)")
except Exception as _e:
    print(f"[FOLLOWUP BOOT ERROR] {_e}")

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
# r44 (Gift 20 ส.ค. 2026) — "อยากให้รู้สึกว่าพิมพ์ หยุด แล้วพิมพ์ต่อ นานกว่านี้หน่อย
# เขาจะได้รู้สึกรอบ้าง ลุ้นบ้าง" -> ช้าลง + มีจังหวะหยุดจริงระหว่างบับเบิล
# ทำได้เพราะย้ายการส่งไปเธรดเบื้องหลังแล้ว (ดู send_reply) webhook ตอบ 200 ทันที
# ไม่งั้นหน่วงนานๆ Meta จะยิง event ซ้ำ = ลูกค้าได้ข้อความซ้ำ
TYPING_ENABLED = os.environ.get("TYPING_DELAY", "1").strip() != "0"
# r55 (Gift 20 ส.ค. 2026) — "ไอ้ที่พิมพ์ๆ หยุดๆ มันนานไปไหม ขอแค่ 3-5 วิพอ"
# ของเดิมเทิร์นนึงกินเวลาได้ถึง 26 วิ (อ่าน 1.5 + พิมพ์ 7 + หยุด 1.8 + พิมพ์ 7 ...)
# เจตนาเดิมคือ "ให้เหมือนคนพิมพ์" แต่นานขนาดนั้นกลายเป็น "เหมือนบอทค้าง"
# ตัดเหลือรวมทั้งเทิร์นไม่เกิน 5 วิ — ยังเห็นจุดไข่ปลาขยับ แต่ไม่ต้องรอนาน
TYPING_BASE_SEC = float(os.environ.get("TYPING_BASE_SEC", "0.6"))
TYPING_CPS = float(os.environ.get("TYPING_CPS", "28"))        # ตัวอักษร/วินาที
TYPING_MAX_SEC = float(os.environ.get("TYPING_MAX_SEC", "2.2"))
TYPING_BUDGET_SEC = float(os.environ.get("TYPING_BUDGET_SEC", "5"))
TYPING_READ_SEC = float(os.environ.get("TYPING_READ_SEC", "0.8"))   # อ่านก่อนเริ่มพิมพ์
TYPING_GAP_SEC = float(os.environ.get("TYPING_GAP_SEC", "0.6"))     # หยุดระหว่างบับเบิล


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


# ล็อกรายคน — กันสองข้อความของคนเดียวกันพิมพ์ทับกันจนสลับลำดับ
_SEND_LOCKS: dict = {}
_SEND_LOCKS_GUARD = threading.Lock()


def _send_lock(recipient_id: str):
    with _SEND_LOCKS_GUARD:
        lk = _SEND_LOCKS.get(recipient_id)
        if lk is None:
            lk = threading.Lock()
            _SEND_LOCKS[recipient_id] = lk
            if len(_SEND_LOCKS) > 2000:
                for k in [k for k, v in list(_SEND_LOCKS.items())
                          if k != recipient_id and not v.locked()][:1000]:
                    _SEND_LOCKS.pop(k, None)
        return lk


def _send_reply_blocking(recipient_id: str, text: str, page_id: str = ""):
    """ส่งคำตอบของบอท — แยกเป็นหลายบับเบิลถ้ามี MSG_SPLIT

    เหตุผล: คำถามที่อยู่รวมก้อนเดียวกับข้อความอื่นจะถูกลูกค้าสแกนผ่าน
    ส่งคำถามเป็นบับเบิลสุดท้ายเดี่ยวๆ ได้อัตราตอบกลับสูงกว่าชัดเจน

    จังหวะ (r44): อ่านข้อความ -> พิมพ์ -> ส่ง -> หยุด -> พิมพ์ -> ส่ง
    ช่วง "หยุด" ต้องไม่มี typing_on ลูกค้าถึงจะเห็นจุดไข่ปลาหายไปจริงๆ
    """
    parts = [p.strip() for p in text.split(MSG_SPLIT) if p.strip()]
    if not parts:
        return
    budget = TYPING_BUDGET_SEC
    with _send_lock(recipient_id):
        if TYPING_ENABLED and budget > 0.2:
            # เห็นข้อความแล้ว แต่ยังไม่เริ่มพิมพ์ทันที = เหมือนคนกำลังอ่าน
            send_sender_action(recipient_id, "mark_seen", page_id)
            _read = min(TYPING_READ_SEC, budget)
            time.sleep(_read)
            budget -= _read
        for i, part in enumerate(parts):
            if TYPING_ENABLED and budget > 0.2:
                if i:
                    # หยุดพิมพ์ให้เห็นชัดก่อนขึ้นบับเบิลถัดไป
                    gap = min(TYPING_GAP_SEC, budget)
                    time.sleep(gap)
                    budget -= gap
                pause = min(_typing_pause(part), budget)
                if pause > 0.2:
                    send_sender_action(recipient_id, "typing_on", page_id)
                    time.sleep(pause)
                    budget -= pause
            if not send_message(recipient_id, part, page_id):
                # ส่งถึงคนนี้ไม่ได้แล้ว (บล็อกเพจ / ปิดบัญชี / หลุด 24 ชม.)
                # บับเบิลที่เหลือก็ไม่ถึงเหมือนกัน ยิงต่อได้แค่ error ซ้ำ
                print(f"[SEND ABORT] {_mask(recipient_id)} ติดต่อไม่ได้ "
                      f"— ข้ามบับเบิลที่เหลือ")
                break


_VALID_PSID = re.compile(r"^\d{5,}$")


def _is_sendable(recipient_id: str) -> bool:
    """ส่งข้อความหา id นี้ได้จริงไหม

    Meta รับเฉพาะ PSID/IGSID ที่เป็นตัวเลขล้วน
    คีย์ชั่วคราวของคอมเมนต์ (`c:{comment_id}`) ส่งไม่ได้ -> ยิงไปก็ได้
    `(#100) Param recipient[id] must be a valid ID string` กลับมาเปล่าๆ
    """
    return bool(_VALID_PSID.match(str(recipient_id or "").strip()))


def send_reply(recipient_id: str, text: str, page_id: str = "",
               force: bool = False):
    """ยิงในเธรดเบื้องหลัง — webhook จะได้ตอบ 200 ให้ Meta ภายในไม่กี่ ms

    ถ้าหน่วงพิมพ์อยู่ใน request เลย Meta จะถือว่า timeout แล้วยิง event ซ้ำ
    ลูกค้าจะได้ข้อความซ้ำสองรอบ — เคยเจอมาแล้วตอน budget 8 วิ
    """
    # r70 — เพจโหมดเก็บข้อมูล: ด่านสุดท้ายก่อนยิงจริง
    # r71 — ยกเว้นข้อความดับอารมณ์ (force=True) Gift เคาะ 23 ส.ค. 2026:
    #       "คงโหมดเงียบไว้ แต่ให้ override ตอนโกรธ"
    # r85 — กัน SEND_ERROR (#100) ที่โผล่ใน /review-log
    # ต้นเหตุ: state ของคอมเมนต์ถูกเก็บลง Postgres ด้วย psid = "c:{comment_id}"
    # (`rekey`/`drop` ล้างแต่ใน RAM ไม่ได้ล้าง Postgres)
    # แล้วตัวกวาดทักกลับอ่านแถวนั้นมา -> ยิงหา "c:..." -> Meta ตอบ 400
    # ไม่ใช่ error ที่เกิดกับลูกค้าจริง แต่ทำให้ log ดูเหมือนระบบพัง
    # (สำคัญเป็นพิเศษตอนนี้ เพราะผู้ตรวจ Meta จะดู /review-log ในวิดีโอ)
    if not _is_sendable(recipient_id):
        print(f"[SEND SKIP] ข้ามการส่ง — id ไม่ใช่ PSID จริง "
              f"(คีย์ชั่วคราวของคอมเมนต์) | {str(recipient_id)[:40]!r}")
        return
    if page_observe(page_id) and not force:
        print(f"[OBSERVE] เพจ {page_id} ยังไม่เปิดบอทตอบ — ไม่ส่งข้อความ "
              f"| {text[:60]!r}")
        return
    if force and page_observe(page_id):
        print(f"[CALM OVERRIDE] เพจ {page_id} โหมดเก็บข้อมูล "
              f"แต่จับอารมณ์ได้ — ตอบ 1 ข้อความ | {text[:60]!r}")
    threading.Thread(target=_send_reply_blocking,
                     args=(recipient_id, text, page_id),
                     daemon=True).start()


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


# ---------------------------------------------------------------------------
# 19 ส.ค. 2026 — เคสจริง (เพจ Intake, ลูกค้า Aor Alisa):
# เซล "หลี" พิมพ์เองจากกล่องข้อความเพจ 3 ข้อความ แต่บอทไม่หยุด ตอบทับต่อ
# สาเหตุ: เดิมเช็คว่า "มี app_id ไหม" -> ถือว่าเป็นบอทส่งเอง
# แต่ Meta Business Suite / Page Inbox ก็แนบ app_id มาด้วยเหมือนกัน
# -> ข้อความที่คนพิมพ์ถูกนับเป็นของบอท -> ไม่เกิดการรับช่วง
# แก้: จำ message_id ที่บอทส่งเองไว้ แล้วเทียบตรงๆ ไม่เดาจาก app_id
# (เทียบ app_id เป็นตัวสำรองอีกชั้น เผื่อ mid หลุด)
FB_APP_ID = os.environ.get("FB_APP_ID", "9591150740963461").strip()
_SENT_MIDS: "deque" = deque(maxlen=2000)
_SENT_MID_SET: set = set()
_SENT_LOCK = threading.Lock()


def _remember_sent(mid: str):
    if not mid:
        return
    with _SENT_LOCK:
        if len(_SENT_MIDS) == _SENT_MIDS.maxlen:
            _SENT_MID_SET.discard(_SENT_MIDS[0])
        _SENT_MIDS.append(mid)
        _SENT_MID_SET.add(mid)


def _was_sent_by_bot(mid: str) -> bool:
    if not mid:
        return False
    with _SENT_LOCK:
        return mid in _SENT_MID_SET


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
                _remember_sent(str(body.get("message_id", "") or ""))
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
    if page_observe(page_id):          # r70 — เพจยังไม่เปิดบอทตอบ
        print(f"[OBSERVE] เพจ {page_id} ยังไม่เปิดบอทตอบ — ไม่ตอบคอมเมนต์")
        return False
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
    if page_observe(page_id):          # r70 — เพจยังไม่เปิดบอทตอบ
        print(f"[OBSERVE] เพจ {page_id} ยังไม่เปิดบอทตอบ — ไม่ private reply")
        return ""
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

    # r83 — Gift 26 ส.ค.: "เอาแค่ขอบคุณที่แสดงความคิดเห็น
    #        แต่ถ้าสนใจ ก็ตอบว่าส่งรายละเอียดให้ทางข้อความนะ
    #        ไม่ต้องไปตอบอะไรเหมือนใน messaging"
    # คนมาชมเฉยๆ ("เยี่ยมคะ" "สวยมาก") ไม่ควรโดนทักขาย = สแปม
    try:
        _interested = detect_comment_interest(text)
    except Exception as _e:
        print(f"[COMMENT] เช็คความสนใจไม่ได้ ถือว่าสนใจไว้ก่อน: {_e}")
        _interested = True
    if not _interested:
        print(f"[COMMENT SKIP] ไม่ใช่คนสนใจซื้อ — ขอบคุณอย่างเดียว "
              f"ไม่ทักแชท | {(text or '')[:40]!r}")
        if COMMENT_PUBLIC_REPLY:
            reply_to_comment(
                comment_id,
                PUBLIC_COMMENT_THANKS_F if gender == "female"
                else PUBLIC_COMMENT_THANKS,
                fb_page_id,
            )
        return

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
        # r84 🔴 บั๊กจริง (Gift ให้เช็ค 26 ส.ค. "เข้า lead ก็ต้องคุย lead")
        # `rekey` ย้าย _lead_states ด้วยคีย์ "{page}:{id}" ถูกต้องแล้ว
        # แต่ **บทสนทนา** ถูกเก็บคนละคีย์: `_log()` เขียนที่ `_conversations[user_id]`
        # (คีย์เปล่าๆ ไม่มี page นำหน้า) และ AI ก็อ่านจากคีย์นั้น
        # -> ของเดิมย้ายแต่คีย์ที่มี page นำหน้า ซึ่งแทบไม่มีข้อมูล
        # -> พอลูกค้าตอบในแชทต่อ บอทจำไม่ได้ว่าเพิ่งพูดอะไรไปในไพรเวทรีพลาย
        #    = ทักทายใหม่ / ถามซ้ำ ทั้งที่เพิ่งคุยกันไป
        try:
            _old_conv = _conversations.pop(tmp_key, None)
            if _old_conv:
                _conversations[psid] = _old_conv
                print(f"[COMMENT] ย้ายบทสนทนา {len(_old_conv)} ท่อน "
                      f"ไปคีย์จริงแล้ว — คุยต่อได้ ไม่ทักซ้ำ")
            else:
                print("[COMMENT] ไม่มีบทสนทนาให้ย้าย (ปกติถ้าตอบด้วยข้อความตายตัว)")
        except Exception as _e:
            print(f"[COMMENT] ย้ายบทสนทนาไม่สำเร็จ: {_e}")
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


def alert_complaint(sender_id: str, user_text: str, ad_id: str = "",
                    page_id: str = "", kind: str = "เคสร้องเรียน",
                    tail: str = "บอทหยุดขายและส่งต่อให้คนดูแลแล้ว — โทรกลับด่วนครับ"):
    """r71 (Gift 23 ส.ค. 2026) — ลูกค้าไม่พอใจ ต้องมีคนรู้ "เสมอ"

    ต่างจาก alert_lead อยู่จุดเดียว: ห้ามเงียบ
    เพจที่ยังไม่ได้ตั้ง alert_psid ให้ตกมาที่ Gift แทนการข้ามไปเฉยๆ
    (ลีดเกรด A หลุดยังตามเก็บจากชีตได้ เรื่องร้องเรียนหลุด = เสียลูกค้าถาวร)
    """
    target = page_alert_psid(page_id) or GIFT_FB_PSID
    if not target:
        print("[COMPLAINT] ไม่มีผู้รับแจ้งเตือนเลย "
              "— ดูในชีต ช่องสัญญาณจะขึ้น 🔴 เคสร้องเรียน")
        return
    alert = (
        f"🔴 {kind} — {page_brand(page_id)}\n"
        f"Sender: {sender_id}\n"
        f"ข้อความ: {user_text[:200]}\n"
        f"Ad ID: {ad_id or '-'}\n\n"
        + tail
    )
    ok = send_message(target, alert, page_id)
    # เพจรองส่งหา Gift ไม่ได้ถ้า Gift ไม่เคยทักเพจนั้น -> ถอยไปยิงจากเพจหลัก
    if not ok and page_id and str(page_id) != MAIN_PAGE_ID:
        send_message(target, alert, MAIN_PAGE_ID)
    print(f"[COMPLAINT] {kind} -> แจ้งเตือนที่ {_mask(target)} page={page_id or '-'}")


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
        _app_id = str(_msg.get("app_id") or "")
        _echo_mid = str(_msg.get("mid") or "")
        _from_app = _was_sent_by_bot(_echo_mid) or (
            bool(_app_id) and bool(FB_APP_ID) and _app_id == FB_APP_ID)
        if not _from_app and _app_id:
            print(f"[ECHO HUMAN] คนพิมพ์จากกล่องข้อความเพจ (app_id={_app_id}) "
                  f"— ไม่ใช่แอปของเรา ({FB_APP_ID})")
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
        send_reply(sender_id, reply_text, page_id,
                   force=(lead_grade in ("!", "STOP", "STOPSOFT")))  # r71/r73
    else:
        print(f"[SILENT] ({platform}) {_mask(sender_id)} ไม่ตอบ (เซลดูแลเอง)")

    # r81 — ผ่านเกณฑ์รายได้ -> ติดป้ายให้แชท (เช็คทุกเทิร์น ไม่ผูกกับเกรด)
    try:
        _lst = _lead_states.get(f"{page_id}:{sender_id}") or _lead_states.get(sender_id)
        if _lst is not None:
            label_if_qualified(sender_id, page_id, _lst)
    except Exception as _e:
        print(f"[LABEL] hook พลาด ข้ามไป: {str(_e)[:80]}")

    if lead_grade == "A":
        alert_lead(sender_id, user_text, lead_referral.get("ad_id", ""), page_id)
    elif lead_grade == "!":      # r71 — เคสร้องเรียน ปลุกคนทันที
        alert_complaint(sender_id, user_text,
                        lead_referral.get("ad_id", ""), page_id)
    elif lead_grade == "STOP":   # r73 — ลูกค้าสั่งหยุด: ปิดบอทถาวรแล้ว
        alert_complaint(sender_id, user_text,
                        lead_referral.get("ad_id", ""), page_id,
                        kind="ลูกค้าขอเลิกคุย",
                        tail=("ปิดบอทถาวรสำหรับแชทนี้แล้ว (ปลดด้วย #เปิดบอท)\n"
                              "ถ้าจะกู้เคส ต้องเป็นคนทักเองเท่านั้นครับ"))
    elif lead_grade == "STOPSOFT":   # r73 — ลูกค้าทักว่าบอทตอบไม่เข้าท่า
        alert_complaint(sender_id, user_text,
                        lead_referral.get("ad_id", ""), page_id,
                        kind="บอทตอบไม่ตรง ลูกค้าทักแล้ว",
                        tail=("บอทเงียบและส่งต่อให้คนแล้ว (ยังไม่ปิดถาวร)\n"
                              "รีบเข้าไปคุยต่อ ยังกู้เคสทันครับ"))
    elif lead_grade == "DUP":    # r73 — บอทเริ่มพูดวน กันไว้แล้ว
        alert_complaint(sender_id, user_text,
                        lead_referral.get("ad_id", ""), page_id,
                        kind="บอทพูดซ้ำ ถูกกันไว้",
                        tail=("บอทกำลังจะส่งข้อความเดิมซ้ำ ระบบกันไว้และเงียบแทน\n"
                              "แปลว่าสคริปต์ตันแล้ว — คนเข้าไปคุยต่อได้เลยครับ"))

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
    out = {"status": "ok", "revision": BOT_REVISION}
    # r68 — เช็คเองได้ว่ามีเพจไหนโดนสั่ง "หยุดฉุกเฉิน" ค้างอยู่ไหม
    # เปิด /health?pause=1 (หรือ ?mute=1 ก็ได้) ต้องได้ paused_pages: []
    # สวิตช์ "ปิดบอททั้งเพจ" (BOT_MUTED_PAGES) ถูกถอดทิ้งแล้วที่ r68
    # เหลือ PAUSE ระดับเพจ + ประโยคปิดรายแชทของเซล (ไม่โผล่ตรงนี้)
    if request.args.get("pause") or request.args.get("mute"):
        try:
            out["paused_pages"] = sorted(BOT_PAUSE_PAGES)
            out["observe_pages"] = sorted(OBSERVE_PAGES)
            out["page_mute_switch"] = "removed in r68"
        except Exception as e:
            out["paused_pages"] = {"error": str(e)[:200]}
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


@app.route("/import", methods=["POST"])
def import_chatlog():
    """นำเข้าประวัติแชทเก่าจาก Chat_Log (Apps Script ยิงมาเป็นชุดๆ)

    ต้องมี ?key=... ตรงกับ IMPORT_KEY (ค่าเริ่มต้นใช้ตัวเดียวกับ REVIEW_LOG_KEY)
    ยิงซ้ำได้ ข้อมูลไม่ซ้ำ เพราะกันด้วย source_key ฝั่ง Postgres
    """
    if request.args.get("key", "") != IMPORT_KEY:
        return jsonify({"ok": False, "error": "bad key"}), 403
    body = request.get_json(silent=True) or {}
    rows = body.get("rows")
    if not isinstance(rows, list):
        return jsonify({"ok": False, "error": "rows must be a list"}), 400
    try:
        from bot_logic import pg_store
        if pg_store is None:
            return jsonify({"ok": False, "error": "pg_store unavailable"}), 503
        return jsonify(pg_store.import_rows(rows[:1000]))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:300]}), 500


@app.route("/import/status", methods=["GET"])
def import_status():
    if request.args.get("key", "") != IMPORT_KEY:
        return jsonify({"ok": False, "error": "bad key"}), 403
    try:
        from bot_logic import pg_store
        if pg_store is None:
            return jsonify({"ok": False, "error": "pg_store unavailable"}), 503
        return jsonify(pg_store.counts())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:300]}), 500


@app.route("/import/fixts", methods=["POST", "GET"])
def import_fix_ts():
    """แก้เวลาแถวที่ import มาจากชีต (เลื่อนถอย 14 ชม.) — รันซ้ำได้ ไม่เลื่อนซ้ำ

    ที่มา: ไฟล์ WEC CRM เคยตั้ง timezone เป็น America/Los_Angeles
    เวลาที่อ่านออกมาตอน import จึงล้ำหน้าไป 14 ชม.
    """
    if request.args.get("key", "") != IMPORT_KEY:
        return jsonify({"ok": False, "error": "bad key"}), 403
    try:
        hours = int(request.args.get("hours", "14"))
    except Exception:
        return jsonify({"ok": False, "error": "hours ต้องเป็นตัวเลข"}), 400
    try:
        from bot_logic import pg_store
        if pg_store is None:
            return jsonify({"ok": False, "error": "pg_store unavailable"}), 503
        return jsonify(pg_store.fix_import_ts(hours))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:300]}), 500


@app.route("/recover/handover", methods=["GET", "POST"])
def recover_handover():
    """r50 — ดึงเคสที่เซลรับช่วงแล้วลีดตกหล่น (ก่อน r49) กลับมาแจกให้เซล

    ค่าเริ่มต้น dry=1 = ดูรายการอย่างเดียว ไม่เขียนอะไรทั้งนั้น
    ใส่ dry=0 ถึงจะแจกจริง · ไม่ว่าโหมดไหนก็ไม่ส่งข้อความหาลูกค้า
    """
    if request.args.get("key", "") != IMPORT_KEY:
        return jsonify({"ok": False, "error": "bad key"}), 403
    try:
        days = int(request.args.get("days", "3"))
    except Exception:
        days = 3
    dry = request.args.get("dry", "1") != "0"
    try:
        return jsonify(bot.recover_handover_leads(days=days, dry=dry))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:300]}), 500


@app.route("/followup/sweep", methods=["GET", "POST"])
def followup_sweep():
    """r51 — ดูว่ารอบนี้จะทักใครบ้าง (dry=1) หรือสั่งกวาดเองทันที (dry=0)

    เธรดเบื้องหลังกวาดให้อยู่แล้วทุก FOLLOWUP_SWEEP_SEC วินาที
    route นี้มีไว้ตรวจสอบด้วยตา + สั่งยิงนอกรอบตอนอยากทดสอบ
    """
    if request.args.get("key", "") != IMPORT_KEY:
        return jsonify({"ok": False, "error": "bad key"}), 403
    def _int(name, dflt):
        try:
            return int(request.args.get(name, str(dflt)))
        except Exception:
            return dflt
    dry = request.args.get("dry", "1") != "0"
    try:
        res = bot.sweep_followups(
            send=None if dry else send_reply,
            hours=_int("hours", 0), max_hours=_int("max_hours", 0),
            dry=dry, cap=_int("cap", 0))
        return jsonify(res)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:300]}), 500


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


# ======================================================
# r86 — ล้างสถานะลูกค้าทีละคน (ไว้ใช้กับบัญชีทดสอบ)
# ------------------------------------------------------
# Gift 26 ส.ค. 2569: "ล้าง log คนนี้หน่อย เป็นผมเองเอาไว้เทส bot ทำ review"
# ก่อนหน้านี้ไม่มีทางล้างเลย ต้องรอ state หมดอายุไปเอง
# ทำ 3 อย่าง:
#   1) ลบแถว sessions + messages ของคู่ (page_id, psid) ใน Postgres
#   2) เขียน state เปล่ากลับเข้า sessions ทันที
#      เหตุผล: _resolve_state ไล่หา Postgres -> ชีต -> ว่าง
#      ถ้าลบเฉยๆ มันจะไปกู้ข้อมูลเก่าจาก "ชีต" กลับมาแทน
#      ใส่ก้อนเปล่าไว้ = ตัดจบตั้งแต่ชั้นแรก และไม่ต้องแตะชีตลีดเลย
#      (กติกา Gift: ห้ามยุ่งกับคอลัมน์แจกเคส/เจ้าของ/คิว)
#   3) ล้าง RAM: _lead_states ทุกรูปคีย์ · _conversations · _WELCOMED
# ใช้คีย์ตัวเดียวกับ /review-log
# ======================================================
@app.route("/reset-user", methods=["GET", "POST"])
def reset_user():
    if request.args.get("key", "") != REVIEW_LOG_KEY:
        return jsonify({"error": "invalid key"}), 401

    page_id = str(request.args.get("page", "") or "").strip()
    psid    = str(request.args.get("psid", "") or "").strip()
    prefix  = str(request.args.get("prefix", "") or "").strip()

    # --- หา psid จากเศษตัวเลขที่ log มาสก์ไว้ -------------------------
    # หาใน RAM ก่อน แล้วค่อยไปหาใน Postgres
    # (หลัง deploy ใหม่ RAM ว่างเสมอ ถ้าไม่ไล่ต่อจะหาไม่เจอทุกครั้ง)
    if not psid and prefix:
        cands = set()
        for k in list(_lead_states.keys()) + list(_conversations.keys()):
            k = str(k)
            tail = k.split(":")[-1]
            if tail.startswith(prefix) and tail.isdigit():
                cands.add(tail)
        if len(cands) != 1:
            try:
                from bot_logic import pg_store as _ps
                if _ps is not None and getattr(_ps, "DATABASE_URL", ""):
                    import psycopg2 as _pg2
                    _c = _pg2.connect(_ps.DATABASE_URL, connect_timeout=8)
                    try:
                        with _c.cursor() as _cur:
                            if page_id:
                                _cur.execute(
                                    "SELECT DISTINCT psid FROM sessions "
                                    "WHERE page_id=%s AND psid LIKE %s",
                                    (page_id, prefix + "%"))
                            else:
                                _cur.execute(
                                    "SELECT DISTINCT psid FROM sessions "
                                    "WHERE psid LIKE %s", (prefix + "%",))
                            for _r in _cur.fetchall():
                                if _r[0]:
                                    cands.add(str(_r[0]))
                    finally:
                        try:
                            _c.close()
                        except Exception:
                            pass
            except Exception as _e:
                print(f"[RESET USER] หา prefix ใน Postgres ไม่ได้: {_e}")
        if len(cands) != 1:
            return jsonify({"error": "prefix ไม่ชี้ชัด",
                            "prefix": prefix,
                            "matches": sorted(cands)[:20]}), 400
        psid = cands.pop()

    if not psid:
        return jsonify({"error": "ต้องส่ง psid หรือ prefix มาด้วย"}), 400

    out = {"psid_masked": _mask(psid), "page": page_id or "(ทุกเพจ)"}

    # --- 1+2) Postgres ------------------------------------------------
    pg_note = "ข้าม (ไม่ได้เปิด Postgres)"
    try:
        from bot_logic import pg_store
        if pg_store is not None and getattr(pg_store, "DATABASE_URL", ""):
            import psycopg2 as _pg
            import json as _json
            blank = {"data": {}, "awaiting": None, "qualifying": False,
                     "done": False, "referral": {}, "platform": "facebook",
                     "last_seen": time.time(), "lead_sent": False,
                     "asked": {}, "contact_refused": False,
                     "signals": [], "turns": 0, "price_asks": 0,
                     "psid": str(psid)}
            conn = _pg.connect(pg_store.DATABASE_URL, connect_timeout=8)
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    if page_id:
                        cur.execute("DELETE FROM messages WHERE page_id=%s AND psid=%s",
                                    (page_id, psid))
                        n_msg = cur.rowcount
                        cur.execute("DELETE FROM sessions WHERE page_id=%s AND psid=%s",
                                    (page_id, psid))
                        n_ses = cur.rowcount
                        cur.execute(
                            "INSERT INTO sessions (page_id, psid, state, updated_at) "
                            "VALUES (%s, %s, %s, now()) "
                            "ON CONFLICT (page_id, psid) DO UPDATE "
                            "SET state = EXCLUDED.state, updated_at = now()",
                            (page_id, psid,
                             _json.dumps(blank, ensure_ascii=False)))
                    else:
                        cur.execute("DELETE FROM messages WHERE psid=%s", (psid,))
                        n_msg = cur.rowcount
                        cur.execute("SELECT page_id FROM sessions WHERE psid=%s",
                                    (psid,))
                        pages = [r[0] for r in cur.fetchall()]
                        cur.execute("DELETE FROM sessions WHERE psid=%s", (psid,))
                        n_ses = cur.rowcount
                        for _p in pages:
                            cur.execute(
                                "INSERT INTO sessions (page_id, psid, state, updated_at) "
                                "VALUES (%s, %s, %s, now()) "
                                "ON CONFLICT (page_id, psid) DO UPDATE "
                                "SET state = EXCLUDED.state, updated_at = now()",
                                (_p, psid,
                                 _json.dumps(blank, ensure_ascii=False)))
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            pg_note = f"ลบ messages {n_msg} แถว · sessions {n_ses} แถว · ใส่ก้อนเปล่าคืนแล้ว"
    except Exception as e:
        pg_note = f"พลาด: {type(e).__name__}: {e}"[:200]
    out["postgres"] = pg_note

    # --- 3) RAM -------------------------------------------------------
    killed = []
    for k in [k for k in list(_lead_states.keys())
              if str(k) == psid or str(k).endswith(":" + psid)]:
        _lead_states.pop(k, None)
        killed.append(str(k))
    for k in [k for k in list(_conversations.keys())
              if str(k) == psid or str(k).endswith(":" + psid)]:
        _conversations.pop(k, None)
        killed.append("conv:" + str(k))
    try:
        _bl._WELCOMED.pop(psid, None)
    except Exception:
        pass
    out["ram"] = f"ล้าง {len(killed)} คีย์"
    out["ok"] = True
    print(f"[RESET USER] {_mask(psid)} page={page_id or '-'} | {pg_note} | RAM {len(killed)} คีย์")
    log_event("RESET_USER", f"cleared state for {_mask(psid)}",
              {"page": page_id or "-", "ram_keys": len(killed)})
    return jsonify(out)


# ======================================================
# r87 — ล้างกระดานบันทึกก่อนอัดวิดีโอ App Review
# ------------------------------------------------------
# Gift 26 ส.ค. 2569: "ล้าง log ชื่อผมออกหน่อย"
# ตอนอัดคลิปให้ผู้ตรวจดู กระดานควรมีเฉพาะ session ที่กำลังสาธิต
# ไม่ใช่ของลูกค้าจริงคนอื่นที่ค้างอยู่ 80 บรรทัด
# _EVENT_LOG เป็น deque ในหน่วยความจำ (maxlen=80) อยู่แล้ว
# -> ล้างได้ทันทีโดยไม่ต้อง restart และไม่กระทบบอทที่กำลังคุยกับลูกค้า
# ใช้คีย์ตัวเดียวกับ /review-log
# ======================================================
@app.route("/review-log/clear", methods=["GET", "POST"])
def review_log_clear():
    if request.args.get("key", "") != REVIEW_LOG_KEY:
        return jsonify({"error": "invalid key"}), 401
    n = len(_EVENT_LOG)
    _EVENT_LOG.clear()
    print(f"[REVIEW LOG] ล้างกระดาน {n} บรรทัด")
    log_event("SYSTEM",
              "operator log buffer reset — this console holds only the most "
              "recent events in memory")
    return jsonify({"ok": True, "cleared": n})


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
# r88 — Postgres อ่านได้ = ข้ามการรอชีต 15 วิ ตอน restart
# ------------------------------------------------------
# เคสจริง 27 ส.ค.: reads 87 · read_hits 13 -> 74 ครั้งที่ RAM+PG ไม่มี
# แล้วไปรอ _load_session (ชีต) ซึ่ง Apps Script ตันบ่อย = ลูกค้ารอ
# ถ้า Postgres อ่านได้ปกติ แปลว่า state ที่ไม่มีใน PG = ลูกค้าใหม่จริง
# ไม่ต้องไปถามชีตให้เสียเวลา · ต้องคืน None (ไม่ใช่ False!) เพราะ False
# = recovery_failed -> ยิงสัญญาณเตือนเซลทุกลูกค้าใหม่
# ======================================================
try:
    _ORIG_LOAD_SESSION = _bl.BotEngine._load_session

    def _load_session_pg_first(self, user_id, page_id=""):
        try:
            _ps = getattr(_bl, "pg_store", None)
            if _ps is not None and _ps._can_read():
                print(f"[SESSION SKIP] {str(user_id)[:8]}... Postgres อ่านได้ปกติ "
                      "— ข้ามชีต ไม่ต้องรอ 15 วิ")
                return None
        except Exception as _e:
            print(f"[SESSION SKIP] เช็ค Postgres ไม่ได้ ({_e}) — ใช้ทางเดิม")
        return _ORIG_LOAD_SESSION(self, user_id, page_id)

    _bl.BotEngine._load_session = _load_session_pg_first
    print("[R88] Postgres-first session load เปิดแล้ว")
except Exception as _e:
    print(f"[R88 ERROR] ต่อไม่ติด: {_e}")


# ======================================================================
# r89 — เกณฑ์เกรดใหม่ทั้งชุด (Gift เคาะ 28 ส.ค. 2026)
# ----------------------------------------------------------------------
# ตารางเกณฑ์ (ลำดับเดียวกับที่เคาะในแชท):
#   3   ไม่บอกรายได้ / ไม่มีผู้กู้ร่วม            -> X ไม่รับเคส (ไม่แจก ไม่นัดโทร)
#   4+5 ไม่รู้รายได้ (ตัดทางลัด A จากคะแนนทิ้ง)   -> N1 โทรถามรายได้
#   6ก  ปรับโครงสร้าง ปิดแล้ว >1 ปี และ DSR <15%  -> N4 ผ่านเกณฑ์เครดิต (แจกได้)
#   6ข  ยังติดปรับโครงสร้างอยู่                    -> X (DSR ต่ำแค่ไหนก็ไม่รับ)
#   6ค  ปิดแล้ว <=1 ปี หรือ DSR >=15%             -> X
#   6ง  บูโรค้าง / ล่าช้าเกิน 30 วัน               -> X
#   7   อาชีพอิสระ <2 ปี ไม่มีภาษี/ทะเบียน         -> X
#   8   วงเงินตอนนี้ >= 2.5M และยืนยันยอดผ่อนแล้ว  -> A
#   9   วงเงิน(สมมติไม่มีหนี้) >= 2.5M แต่ยังไม่ยืนยันยอดผ่อน -> N2 โทรถามยอดผ่อน
#   10  ปิดภาระแล้ววงเงิน >= 2.5M                  -> B (+เช็ค "บริดจ์แท้")
#   11  ผ่อน <= รายได้                             -> C
#   12  ผ่อน > รายได้ (เดิม D — ยุบทิ้ง)           -> C
# บริดจ์แท้ (นิยาม Gift): ส่วนต่างวงเงินหลังปิดหนี้ ต้องพอคืนยอดหนี้รวมที่ปิดให้
#   = (วงเงินปลอดภาระ - ราคาห้อง 2.5M) >= ยอดหนี้คงเหลือรวม -> แจกโม/เล็กได้
# เกรดที่ใช้จริง: A B C N O R + X (ไม่รับเคส) · เหตุผล N/X เขียนในสัญญาณเสมอ
# ทุกจุดมี fallback -> ห้ามทำให้บอทเงียบเด็ดขาด (บทเรียน r73/r75)
# ======================================================================
import bot_logic as _bl9

R89_UNIT_PRICE = 2_500_000      # ราคาห้องอ้างอิงใหม่ (เดิม 2.3M)
R89_RESTRUCT_MIN_YEARS = 1      # ปรับโครงสร้างต้องปิดมาแล้ว "เกิน" กี่ปี
R89_RESTRUCT_DSR_MAX = 0.15     # DSR ต้องต่ำกว่าเท่านี้ถึงรับ (N4)

# ---------- (1) เกรดใหม่ — ทับ BotEngine._grade ทั้งตัว ----------
_R89_ORIG_GRADE = _bl9.BotEngine._grade


def _r89_reason(self, data, state, code, txt):
    data["grade_reason"] = f"{code} {txt}"
    if state is not None:
        try:
            self._add_signal(state, f"[{code}] {txt}")
        except Exception:
            pass


def _grade_r89(self, data, state=None):
    st = state if state is not None else {}

    def reason(code, txt):
        _r89_reason(self, data, state, code, txt)

    def reject_x(why, ncb=False):
        if state is not None:
            st["soft_close"] = True
            if ncb:
                st["soft_close_msg"] = _bl9.NCB_SOFT_CLOSE
        reason("X", "ไม่รับเคส — " + why)
        return "X"

    # ---- รายได้ (คำนวณไว้ก่อน — ตัดสินทีหลังบล็อกเครดิต) ----
    income = (data.get("income_total") or data.get("income_baht")
              or _bl9._parse_income(str(data.get("income", ""))))
    income = int(income) if income else 0

    # ---- ข้อ 6 : ประวัติเครดิต — ต้องมาก่อนเช็ครายได้
    #      (คนที่ยังติดบูโร/ติดปรับโครงสร้าง ต้องเป็น X แม้ยังไม่รู้รายได้) ----
    _k = st.get("ncb_kind") if state is not None else None
    if _k and state is not None:
        yrs = data.get("ncb_years")
        still = data.get("ncb_still")
        if _k == "blacklist":
            if still is True:
                return reject_x("ยังติดบูโร/ค้างชำระอยู่ ณ ตอนนี้", ncb=True)
            if yrs is not None and yrs < 1:
                return reject_x("เพิ่งปิดบัญชีที่ค้าง ยังไม่พ้น 1 ปี", ncb=True)
            if yrs is not None and yrs >= _bl9.NCB_CLEAR_YEARS:
                self._add_signal(
                    st,
                    f"เคยติดบูโร แต่ปิดมาแล้ว {yrs} ปี"
                    + (f" ({data.get('ncb_bank')})" if data.get("ncb_bank") else "")
                    + f" — พ้น {_bl9.NCB_CLEAR_YEARS} ปีแล้ว ยื่นได้ตามปกติ")
                # ผ่าน -> ไหลไปคิดวงเงินต่อ
            else:
                reason("N3", "เครดิตยังไม่ยืนยัน — เคยติดบูโร ปิดแล้วแต่ยังไม่พ้น 3 ปี"
                             "/ไม่รู้จำนวนปี · เซลดึงบูโรจริงก่อนเสนอแผน (KTB ดูย้อน 1 ปี)")
                return "N"
        elif _k == "late":
            if data.get("ncb_over30") is True:
                return reject_x(f"ชำระล่าช้าเกิน {_bl9.NCB_LATE_DAYS} วัน", ncb=True)
            if data.get("ncb_over30") is False:
                self._add_signal(st, f"เคยชำระล่าช้าแต่ไม่เกิน {_bl9.NCB_LATE_DAYS} วัน "
                                     "— ไม่ใช่เคสแดง ตั้งธงให้เช็คบูโรจริง")
            else:
                reason("N3", "เครดิตยังไม่ยืนยัน — เคยชำระล่าช้า ยังไม่รู้ว่าเกิน 30 วันไหม โทรถามก่อน")
                return "N"
        elif _k == "restruct":
            if still is True:
                return reject_x("ยังติดปรับโครงสร้างหนี้อยู่ — DSR ต่ำแค่ไหนก็ไม่รับ (เกณฑ์ 28 ส.ค.)",
                                ncb=True)
            if yrs is None:
                reason("N3", "เครดิตยังไม่ยืนยัน — ปรับโครงสร้างหนี้ ยังไม่รู้ว่าปิดหรือยัง/ปิดมากี่ปี "
                             "โทรยืนยันก่อน (เกณฑ์รับ: ปิดเกิน 1 ปี + DSR <15%)")
                return "N"
            if yrs <= R89_RESTRUCT_MIN_YEARS:
                return reject_x(f"ปรับโครงสร้างปิดมา {yrs} ปี ยังไม่เกิน "
                                f"{R89_RESTRUCT_MIN_YEARS} ปี", ncb=True)
            _d6 = data.get("debt_baht")
            if _d6 is None:
                _d6 = _bl9._parse_debt_monthly(str(data.get("debt", "")))
            if _d6 is None or not income:
                reason("N3", f"ปรับโครงสร้างปิดมา {yrs} ปี (เกิน 1 ปีแล้ว) แต่ยังไม่รู้ยอดผ่อน/เดือน "
                             "— เกณฑ์รับต้อง DSR <15% · เซลโทรยืนยันยอดผ่อนก่อน")
                return "N"
            _dsr = (_d6 / income) if income else 1.0
            if _dsr < R89_RESTRUCT_DSR_MAX:
                reason("N4", f"ผ่านเกณฑ์เครดิต — ปรับโครงสร้างปิดมา {yrs} ปี (เกิน 1 ปี) "
                             f"และ DSR {round(_dsr*100)}% (<15%) · แจกได้ ยื่นตามปกติ")
                return "N"
            return reject_x(f"ปรับโครงสร้างปิดเกิน 1 ปีแล้ว แต่ DSR {round(_dsr*100)}% "
                            f"เกินเพดาน {round(R89_RESTRUCT_DSR_MAX*100)}%", ncb=True)

    # ---- ข้อ 3/4/5 : รายได้ ----
    if data.get("income_unknown") or data.get("income_refused"):
        return reject_x("ลูกค้าไม่บอกรายได้ (เกณฑ์ 28 ส.ค.)")
    if not income:
        reason("N1", "ยังไม่รู้รายได้ — โทรถามรายได้ + ผู้กู้ร่วม แล้วระบบตีเกรดจริงตอนเซลกรอกกลับ")
        return "N"

    # ---- ข้อ 7 : อาชีพอิสระ/เจ้าของกิจการ ----
    if state is not None and st.get("self_employed"):
        _y7 = data.get("self_emp_years")
        _t7 = data.get("self_emp_tax")
        _r7 = data.get("biz_registered")
        if not _r7 and (_t7 is False or (_y7 is not None and _y7 < _bl9.FREELANCE_MIN_YEARS)):
            st["soft_close"] = True     # คำปิดใช้ SELF_EMP_SOFT_CLOSE ตามทางเดิม
            self._add_signal(
                st,
                f"อาชีพอิสระยังไม่เข้าเกณฑ์ธนาคาร (ทำมา {_y7 if _y7 is not None else '?'} ปี · "
                f"ภาษี/จดทะเบียน: {'ไม่มี' if _t7 is False else 'ยังไม่ยืนยัน'}) "
                "— เกณฑ์กลางคือ 2 ปี+ และมีภาษีย้อนหลัง")
            reason("X", "ไม่รับเคส — อาชีพอิสระไม่เข้าเกณฑ์ (ต่ำกว่า 2 ปี/ไม่มีภาษี-ทะเบียน)")
            return "X"
        if _r7:
            self._add_signal(
                st,
                "เจ้าของกิจการจดทะเบียน — รับเคสได้ · รายได้จริงต้องคิดจาก "
                "ยอดขาย×margin×%หุ้น (§0.5) + เกรดบริษัทจาก DBD ให้ทีมวิเคราะห์คำนวณ")
        income = int(income * _bl9.FREELANCE_INCOME_PCT)
        data["income_counted"] = income
        self._add_signal(
            st,
            f"อาชีพอิสระ/เจ้าของกิจการ — คิดรายได้ที่แบงก์นับ 50% "
            f"(เกณฑ์ TTB/UOB) = {income:,} · KBank นับ 100% · KTB 30% · ธอส ไม่รับ")

    # ---- ภาระผ่อน ----
    debt = data.get("debt_baht")
    if debt is None:
        debt = _bl9._parse_debt_monthly(str(data.get("debt", "")))
    debt_unverified = debt is None and not _bl9._says_no_debt(str(data.get("debt", "")))
    debt = 0 if debt is None else max(0, int(debt))
    if data.get("co_debt_baht"):
        debt += int(data["co_debt_baht"])

    # ---- อายุคุมปีกู้ (ยกจากเกณฑ์เดิมทั้งดุ้น) ----
    _own_age = data.get("age")
    _co_age = data.get("co_age")
    _has_cob = bool(data.get("co_borrower_income"))
    if _co_age is not None:
        _age_calc = min(_own_age, _co_age) if _own_age else _co_age
    elif _has_cob:
        _age_calc = None
    else:
        _age_calc = _own_age
    cap_now = _bl9._capacity(income, debt, _age_calc)
    cap_clear = _bl9._capacity(income, 0, _age_calc)
    if (state is not None and cap_now >= _bl9.BIG_CASE_BAHT
            and str(st.get("page_id", "")) in _bl9.BIG_CASE_PAGES):
        st["route_to"] = "Gift"
        self._add_signal(
            st,
            f"🔷 วงเงินประเมิน {cap_now/1e6:.1f} ล้าน (เกิน 5 ล้าน) "
            f"— เพจ Wealth Estate ส่ง Gift คนเดียว ไม่เข้าคิวแจกปกติ")
    if state is not None and _own_age is not None:
        _solo = _bl9._capacity(income, debt, _own_age)
        if _has_cob and _co_age is None:
            self._add_signal(
                st,
                f"⚠️ วงเงิน {cap_now/1e6:.1f}M คิดโดย 'สมมติผู้กู้ร่วมอายุ ~35' "
                f"เพราะยังไม่รู้อายุจริง · ถ้ายื่นด้วยอายุผู้กู้หลัก {_own_age} "
                f"จะเหลือ {_solo/1e6:.1f}M — โทรถามอายุผู้กู้ร่วมก่อนเสนอห้อง")
        elif not _has_cob:
            _young = _bl9._capacity(income, debt, _bl9.DEFAULT_AGE)
            if _young > cap_now * 1.2:
                self._add_signal(
                    st,
                    f"อายุ {_own_age} ยื่นเดี่ยวได้ {cap_now/1e6:.1f}M "
                    f"— ถ้ามีผู้กู้ร่วมอายุ ~35 ขึ้นเป็น ~{_young/1e6:.1f}M")
    data["capacity_now"] = cap_now
    data["capacity_clear"] = cap_clear

    # ---- ข้อ 8-12 : ตัดเกรดด้วยวงเงิน 2.5 ล้าน ----
    if debt_unverified:
        if cap_clear >= R89_UNIT_PRICE:
            # ข้อ 9 — วงเงิน "สมมติไม่มีหนี้" ถึงเกณฑ์ แต่ยอดผ่อนยังไม่ยืนยัน
            reason("N2", f"รู้รายได้แล้ว ({income:,}) แต่ยังไม่ยืนยันยอดผ่อน/เดือน — "
                         f"วงเงินแบบไม่มีภาระ {cap_clear/1e6:.1f}M ถึงเกณฑ์ "
                         "· โทรยืนยันยอดผ่อนแล้วตีเกรดจริง")
            return "N"
        # ต่อให้ไม่มีหนี้เลย วงเงินก็ไม่ถึง -> จบที่ C ได้เลย ไม่ต้องรอยอดผ่อน
        if state is not None:
            self._add_signal(st, f"วงเงินแบบไม่มีภาระ {cap_clear/1e6:.1f}M "
                                 f"ยังไม่ถึงราคาห้อง {R89_UNIT_PRICE/1e6:.1f}M")
        return "C"
    if cap_now >= R89_UNIT_PRICE:
        return "A"
    if cap_clear >= R89_UNIT_PRICE:
        # ข้อ 10 — เคสบริดจ์ · เช็ค "บริดจ์แท้" สำหรับคิวโม/เล็ก
        _tot = data.get("debt_total_baht")
        _gap = cap_clear - R89_UNIT_PRICE
        if _tot:
            if _gap >= _tot:
                data["bridge_ok"] = 1
                self._add_signal(
                    st, f"[บริดจ์แท้] ส่วนต่างวงเงิน {_gap/1e6:.2f}M ≥ "
                        f"ยอดหนี้รวม {_tot/1e6:.2f}M — เข้าเกณฑ์แจกโม/เล็ก")
            else:
                self._add_signal(
                    st, f"ส่วนต่างวงเงิน {_gap/1e6:.2f}M ไม่พอปิดยอดหนี้รวม "
                        f"{_tot/1e6:.2f}M — ไม่ใช่บริดจ์แท้ (ไม่เข้าคิวโม/เล็ก)")
        elif state is not None:
            self._add_signal(st, "ยังไม่รู้ยอดหนี้รวมคงเหลือ — เช็คบริดจ์แท้ไม่ได้ "
                                 "(ไม่เข้าคิวโม/เล็ก จนกว่าเซลยืนยันยอด)")
        return "B"
    # ข้อ 11 + 12 — ยุบ D เข้า C ทั้งหมด (เกณฑ์ 28 ส.ค.)
    if debt > income and state is not None:
        self._add_signal(st, "ภาระผ่อนเกินรายได้ (เกณฑ์เดิมคือ D) — เคสบริดจ์หนัก "
                             "ดูแผนปิดหนี้ก่อน · เกณฑ์ใหม่ยุบเป็น C")
    return "C"


try:
    _bl9.BotEngine._grade = _grade_r89
    print("[R89] เกณฑ์เกรดใหม่ (A/B/C/N1-4/X · 2.5M · ตัด D) เปิดแล้ว")
except Exception as _e:
    print(f"[R89 GRADE ERROR] ต่อไม่ติด — ใช้เกณฑ์เดิม: {_e}")


# ---------- (2) กันสัญญาณเก่า "ยังไม่ได้ตัวเลขรายได้" ทับเหตุผลใหม่ ----------
try:
    _R89_ORIG_ADD_SIGNAL = _bl9.BotEngine._add_signal

    def _add_signal_r89(state, tag):
        try:
            if (isinstance(tag, str) and tag.startswith("⚠️ ยังไม่ได้ตัวเลขรายได้")
                    and (state.get("data", {}).get("grade_reason") or "")):
                return    # เหตุผล N1-N4/X เขียนไว้แล้ว ไม่ต้องซ้ำด้วยข้อความเก่า
        except Exception:
            pass
        return _R89_ORIG_ADD_SIGNAL(state, tag)

    _bl9.BotEngine._add_signal = staticmethod(_add_signal_r89)
except Exception as _e:
    print(f"[R89 SIGNAL ERROR] {_e}")


# ---------- (3) แปลเกรดตอนเขียนชีต: ข้อ 3 -> X · X ไม่สร้างนัดโทร ----------
try:
    _R89_ORIG_SEND = _bl9.BotEngine._send_to_sheets

    def _send_to_sheets_r89(self, user_id, data, grade, fb_name="", referral=None,
                            platform="facebook", page_id="", sheet_tab="",
                            signals=None, contact_refused=False, calendar=True,
                            sale=""):
        try:
            if grade == "C" and (data.get("income_unknown") or data.get("income_refused")
                                 or data.get("co_borrower_none")):
                why = ("ลูกค้าไม่บอกรายได้"
                       if (data.get("income_unknown") or data.get("income_refused"))
                       else "ไม่มีผู้กู้ร่วม ยื่นเดี่ยวไม่ผ่าน")
                grade = "X"
                data["grade"] = "X"
                data["grade_reason"] = "X ไม่รับเคส — " + why
                signals = ([f"[X] ไม่รับเคส — {why} (เกณฑ์ 28 ส.ค.)"]
                           + list(signals or []))
            if grade == "X":
                calendar = False        # ไม่รับเคส = ไม่สร้างนัดโทรในปฏิทิน
        except Exception as _e:
            print(f"[R89 SHEET ERROR] {_e} — เขียนแบบเดิม")
        return _R89_ORIG_SEND(self, user_id, data, grade, fb_name, referral,
                              platform, page_id, sheet_tab, signals,
                              contact_refused, calendar, sale)

    _bl9.BotEngine._send_to_sheets = _send_to_sheets_r89
except Exception as _e:
    print(f"[R89 SEND ERROR] {_e}")


# ---------- (4) คำถามปรับโครงสร้างใหม่ + อ่านคำตอบ "ปิดแล้ว/ยังติด" ----------
try:
    _bl9.NCB_Q["restruct"] = (
        "ขออนุญาตถามครับ ตอนนี้ปิดยอดปรับโครงสร้างหมดแล้ว "
        "หรือยังผ่อนตามแผนอยู่ครับ ถ้าปิดแล้ว ปิดมากี่ปีแล้วครับ")

    _R89_RESTRUCT_STILL = ("ยังผ่อน", "ผ่อนตามแผน", "ผ่อนอยู่", "ยังจ่าย",
                           "ยังไม่หมด", "เหลืออีก", "ยังเหลือ")
    _R89_ORIG_CAPTURE = _bl9.BotEngine._capture

    def _capture_r89(self, state, field, msg):
        out = _R89_ORIG_CAPTURE(self, state, field, msg)
        try:
            if field == "ncb" and state.get("ncb_kind") == "restruct":
                data = state["data"]
                _st = _bl9._ncb_still_stuck(msg)
                if _st is None:
                    _m = (msg or "").replace(" ", "")
                    if any(w in _m for w in _R89_RESTRUCT_STILL):
                        _st = True
                if _st is not None:
                    data["ncb_still"] = _st
                elif data.get("ncb_years") is not None and "ncb_still" not in data:
                    data["ncb_still"] = False   # บอกจำนวนปี = ปิดไปแล้ว
        except Exception as _e:
            print(f"[R89 CAPTURE ERROR] {_e}")
        return out

    # คำตอบ "ยังผ่อนตามแผนอยู่" ต้องนับเป็นคำตอบที่ถูกต้องของคำถาม ncb ด้วย
    # (ตัวเดิมรับเฉพาะ จำนวนปี / ปิดแล้ว-ยังติด / เกิน 30 วัน)
    _R89_ORIG_VALID = _bl9.BotEngine._is_valid_answer   # staticmethod เดิม

    def _is_valid_answer_r89(field, m):
        try:
            if field == "ncb":
                _mm = (m or "").replace(" ", "")
                if any(w in _mm for w in _R89_RESTRUCT_STILL):
                    return True
        except Exception:
            pass
        return _R89_ORIG_VALID(field, m)

    _bl9.BotEngine._is_valid_answer = staticmethod(_is_valid_answer_r89)

    _bl9.BotEngine._capture = _capture_r89
    print("[R89] คำถามปรับโครงสร้าง (ปิดแล้ว/ยังติด + กี่ปี) เปิดแล้ว")
except Exception as _e:
    print(f"[R89 NCBQ ERROR] {_e}")


# ---------- (5) สาย O: "อยากมีคอนโดปล่อยเช่า" = คนอยากซื้อ ไม่ใช่เจ้าของห้อง ----------
# เคสจริง 27 ส.ค. เพจ Realty Smart: ลูกค้าจากแอดพิมพ์ "อยากมีคอนโดปล่อยเช่าค่ะ"
# คำว่า "มีคอนโด" ใน _OWN_HAVE_WORDS แมตช์เป็น "ครอบครองแล้ว" -> ตัดเข้าสาย
# เจ้าของฝากเช่า -> บอทขอเบอร์ทันทีแล้วจบ ข้ามการคัดกรองทั้ง funnel
# สถิติ 17-27 ส.ค.: สาย O 28 เคส ได้เบอร์ 0 เคส = ท่อตัน
# แก้: ตัดคำ "อยาก/สนใจ/คิดจะ + มีxxx" (ความอยากในอนาคต) ออกก่อนตีความ
try:
    _R89_ORIG_OWNER_FLAGS = _bl9._owner_flags
    _R89_DESIRE_PRE = ("สนใจอยากจะ", "สนใจอยาก", "กำลังอยาก", "ฝันอยาก",
                       "อยากจะ", "คิดจะ", "อยาก", "สนใจ", "ถ้า", "หาก")
    _R89_HAVE_WORDS = ("มีคอนโด", "มีห้อง", "มีทาวน์", "มีบ้าน",
                       "มีอยู่แล้ว", "มีห้องอยู่")

    def _owner_flags_r89(msg):
        try:
            m = (msg or "").replace(" ", "")
            cleaned = m
            for _pre in _R89_DESIRE_PRE:
                for _hv in _R89_HAVE_WORDS:
                    cleaned = cleaned.replace(_pre + _hv, "")
            if cleaned != m:
                f = _R89_ORIG_OWNER_FLAGS(cleaned)
                print(f"[R89 OWNER] ตัดคำอยากมีออกก่อนตีความ: {m[:40]!r} -> flags={f}")
                return f
        except Exception as _e:
            print(f"[R89 OWNER ERROR] {_e} — ใช้ทางเดิม")
        return _R89_ORIG_OWNER_FLAGS(msg)

    _bl9._owner_flags = _owner_flags_r89
    print("[R89] แพตช์สาย O (อยากมีคอนโด = นักลงทุน) เปิดแล้ว")
except Exception as _e:
    print(f"[R89 OWNER PATCH ERROR] {_e}")


# ---------- (6) คำถามใหม่: ยอดหนี้คงเหลือรวม (ไว้เช็คบริดจ์แท้) ----------
# ถามแทรก 1 คำถามหลังลูกค้าบอกยอดผ่อน/เดือน (เฉพาะคนมีหนี้ >0)
# แล้วส่งคำถามเดิมที่ค้างไว้ต่อทันที — funnel เดิมไม่เปลี่ยนลำดับ
R89_DEBT_TOTAL_Q = ("ขอบคุณครับ ขอถามเพิ่มอีกนิดเดียวครับ "
                    "ยอดหนี้คงเหลือรวมทุกก้อนตอนนี้ประมาณเท่าไหร่ครับ "
                    "(บอกคร่าวๆ ได้เลยครับ เช่น 3 แสน หรือ 1 ล้าน)")

_R89_TOTAL_UNITS = {"ล้าน": 1_000_000, "แสน": 100_000, "หมื่น": 10_000,
                    "พัน": 1_000, "k": 1_000, "m": 1_000_000}


def _r89_parse_total(msg):
    s = str(msg or "").replace(",", "").replace("-", "").lower()
    # เบอร์โทรหน้าตาเหมือนตัวเลขเงินก้อนใหญ่ — ตัดทิ้งก่อนเสมอ
    s = re.sub(r"0[0-9]{8,9}", " ", s)
    if not s.strip():
        return None
    best = None
    for _m in re.finditer(r"([0-9]+(?:\.[0-9]+)?)\s*(ล้าน|แสน|หมื่น|พัน|k|m)", s):
        v = round(float(_m.group(1)) * _R89_TOTAL_UNITS[_m.group(2)])
        if 10_000 <= v <= 100_000_000 and (best is None or v > best):
            best = v
    if best is not None:
        return best
    for _n in re.findall(r"[0-9]{5,9}", s):
        v = int(_n)
        if 10_000 <= v <= 100_000_000 and (best is None or v > best):
            best = v
    return best


def _r89_consume_total(self, msg, user_id, state, bucket, is_new):
    state.pop("_r89_wait_debt_total", None)
    resume = state.pop("_r89_resume", None) or []
    data = state["data"]
    # ลูกค้าส่ง "เบอร์โทร" แทนคำตอบ -> นี่คือช่องทางติดต่อ ห้ามกลืนทิ้ง
    # ส่งเทิร์นคืนเอนจินเดิมให้จับเบอร์ตามปกติ (มันทวนคำถามที่ค้างเอง)
    if re.search(r"0[0-9]{8,9}", str(msg or "").replace("-", "").replace(" ", "")):
        return _R89_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
    amt = _r89_parse_total(msg)
    if amt is None and self._is_question(msg):
        # ลูกค้าถามกลับ — ปล่อยเทิร์นให้เอนจินเดิมตอบ (มันทวนคำถามค้างเอง)
        return _R89_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
    if amt is not None:
        data["debt_total_baht"] = int(amt)
        self._add_signal(state, f"ยอดหนี้คงเหลือรวม {int(amt):,} บาท (ลูกค้าบอกเอง)")
    else:
        data["debt_total_baht"] = None    # ถามแล้ว ไม่ได้ตัวเลข — ห้ามถามซ้ำ
        self._add_signal(state, f"ถามยอดหนี้รวมแล้ว ลูกค้าตอบ: {str(msg)[:60]} "
                                "— เซลยืนยันตอนโทร")
    if resume and str(resume[0]).startswith(("ขอบคุณ", "รับทราบ")):
        return resume, None
    return (["รับทราบครับ"] + resume) if resume else ["รับทราบครับ"], None


try:
    _R89_BASE_DECIDE = _bl9.BotEngine._decide

    def _decide_r89(self, msg, user_id, state, bucket, is_new):
        data = state.get("data") or {}
        if state.get("_r89_wait_debt_total"):
            try:
                return _r89_consume_total(self, msg, user_id, state, bucket, is_new)
            except Exception as _e:
                print(f"[R89 TOTAL ERROR] {_e} — ไปทางเดิม")
                state.pop("_r89_wait_debt_total", None)
                state.pop("_r89_resume", None)
        _prev_wait = state.get("awaiting")
        bubbles, grade = _R89_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
        try:
            if (_prev_wait == "debt" and grade is None and not state.get("done")
                    and not data.get("cash") and not state.get("owner")
                    and not state.get("renter")
                    and data.get("debt_baht") and "debt_total_baht" not in data
                    and bubbles and state.get("awaiting")):
                state["_r89_resume"] = list(bubbles)
                state["_r89_wait_debt_total"] = True
                return [R89_DEBT_TOTAL_Q], None
        except Exception as _e:
            print(f"[R89 HOLD ERROR] {_e}")
        return bubbles, grade

    CalmBotEngine._decide = _decide_r89
    print("[R89] คำถามยอดหนี้รวม (เช็คบริดจ์แท้) เปิดแล้ว")
except Exception as _e:
    print(f"[R89 DECIDE ERROR] {_e}")

print("[R89] ครบชุด — เกณฑ์ 28 ส.ค. 2026 ทำงานแล้ว")


# ======================================================================
# r90 — "กู้คนเดียว" ต้องไม่ฆ่าเคสที่ยื่นเดี่ยวผ่านอยู่แล้ว (Gift เคาะ 28 ส.ค. 2026)
# ----------------------------------------------------------------------
# เคสที่เจอตอนเทส r89:
#   ลูกค้ารายได้ 120,000 ไม่มีหนี้ ให้เบอร์แล้ว พิมพ์ว่า "กู้คนเดียวครับ"
#   -> X ไม่รับเคส  ทั้งที่วงเงินยื่นเดี่ยว 16.3 ล้าน ผ่านสบาย
#
# ต้นตอจริงอยู่ที่ bot_logic._finish บรรทัด 4649 (มีมาตั้งแต่ r87):
#       elif data.get("income_unknown") or data.get("co_borrower_none"):
#           grade = "C"          <-- ลัดวงจร ไม่เรียก _grade เลย
# ใครก็ตามที่พูดคำว่า "กู้คนเดียว/ยื่นคนเดียว/ไม่มีผู้กู้ร่วม" จะถูกตัดเป็น C ทันที
# ต่อให้รายได้ 7 หลักก็ตาม   แล้ว r89 เอา C ตัวนั้นไปแปลงต่อเป็น X = ไม่มีใครโทรเลย
#
# กติกาใหม่ (Gift อนุมัติ): ข้อ 3 ตัดเฉพาะเคสที่ "จำเป็นต้องมีผู้กู้ร่วมจริง"
# ใช้เกณฑ์เดียวกับที่เอนจินใช้ตัดสินว่าจะถามเรื่องผู้กู้ร่วมหรือไม่:
#       low_income  หรือ  high_burden  หรือ  อาชีพอิสระยังไม่ถึงเกณฑ์ธนาคาร
# ไม่เข้า 3 ข้อนี้ = ยื่นเดี่ยวถึงเกณฑ์อยู่แล้ว -> ตีเกรดตามจริง (A/B/C) ไม่ตัดเป็น X
#
# วิธีทำ: ปิดธง co_borrower_none ชั่วคราวเฉพาะตอน _finish ทำงาน แล้วคืนค่าให้เหมือนเดิม
#   - _finish จะไหลไปเข้า self._grade(...) ตามปกติ = ได้เกรดจริง
#   - r89 ที่แปลง C -> X อ่านธงนี้เหมือนกัน จึงไม่ยิงตามไปด้วย (ต้องการแบบนั้น)
#   - _intent_score หักคะแนน -2 จากธงนี้ ก็จะไม่หัก ซึ่งถูกแล้ว
#     เพราะคนที่ยื่นเดี่ยวผ่าน ไม่ควรโดนหักคะแนนเพราะไม่มีผู้กู้ร่วม
#   - สัญญาณ "ลูกค้ายืนยันว่าไม่มีผู้กู้ร่วม" ยังติดไปกับลีดเหมือนเดิม เซลเห็นครบ
#
# _finish คือประตูปิดเคสทางเดียวของทั้งระบบ (ดู docstring ของมัน) จึงคุมได้ครบทุกทาง
# ถ้าโค้ดส่วนนี้พังต้องไหลกลับไปทางเดิมเสมอ — ห้ามทำให้บอทเงียบ
# ======================================================================
def _r90_needs_cob(data, state):
    """เคสนี้ 'จำเป็นต้องมีผู้กู้ร่วม' จริงหรือไม่

    เกณฑ์เดียวกับที่ bot_logic ใช้ตัดสินว่าจะถามคำถามผู้กู้ร่วมไหม
    (รายได้ไม่ถึง / ภาระกินวงเงินจนยื่นเดี่ยวไม่ถึงห้อง / อาชีพอิสระไม่ถึงเกณฑ์)
    ผิดพลาดเมื่อไหร่ให้ตอบ True = ตัดเหมือนเดิม ปลอดภัยไว้ก่อน
    """
    d = data or {}
    st = state or {}
    try:
        if d.get("low_income") or d.get("high_burden"):
            return True
        if _bl9._self_emp_below_bar(d, st):
            return True
        return False
    except Exception as _e:
        print(f"[R90 NEEDCOB ERROR] {_e} — ถือว่าจำเป็นต้องมีผู้กู้ร่วม (ทางเดิม)")
        return True


try:
    _R90_ORIG_FINISH = _bl9.BotEngine._finish

    def _finish_r90(self, user_id, state, contact, calendar=True):
        _restore = False
        try:
            _d = state.get("data") or {}
            if (_d.get("co_borrower_none")
                    and not _d.get("income_unknown")
                    and not _d.get("income_refused")
                    and not _d.get("cash")
                    and not state.get("owner")
                    and not state.get("renter")
                    and not _r90_needs_cob(_d, state)):
                _d["_r90_solo_ok"] = True
                _d.pop("co_borrower_none", None)
                _restore = True
                self._add_signal(
                    state,
                    "ลูกค้าบอกว่ากู้คนเดียว แต่รายได้/ภาระยื่นเดี่ยวถึงเกณฑ์อยู่แล้ว "
                    "— ตีเกรดตามจริง ไม่ตัดเป็น X (เกณฑ์ 28 ส.ค. ข้อ 3 ฉบับปรับ)")
                print(f"[R90] {str(user_id)[:8]}... กู้คนเดียวแต่ยื่นเดี่ยวผ่าน "
                      "— ตีเกรดตามจริง")
        except Exception as _e:
            print(f"[R90 FINISH ERROR] {_e} — ใช้ทางเดิม")
            _restore = False
        try:
            return _R90_ORIG_FINISH(self, user_id, state, contact, calendar)
        finally:
            if _restore:
                try:
                    state["data"]["co_borrower_none"] = True
                except Exception as _e:
                    print(f"[R90 RESTORE ERROR] {_e}")

    _bl9.BotEngine._finish = _finish_r90
    print("[R90] 'กู้คนเดียว' ไม่ตัดเคสที่ยื่นเดี่ยวผ่านแล้ว เปิดแล้ว")
except Exception as _e:
    print(f"[R90 ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")



# ---------- r90b — ปิดรูรั่ว "ประตูที่สอง": เขียนชีตซ้ำหลังปิดเคส ----------
# _send_name_update (bot_logic 5832) เรียก _send_to_sheets อีกรอบด้วยเกรดที่เก็บไว้
# ตอนลูกค้าตอบชื่อทีหลัง  จังหวะนั้นธง co_borrower_none ถูกคืนค่าแล้ว (โดยตั้งใจ)
# -> ตัวแปลงของ r89 เห็น C + ธง = แปลงเป็น X + ปิดนัดโทร ทั้งที่ตอนปิดเคสเป็น C ถูกต้อง
#
# เคสจริงที่โดน: อายุเยอะทำให้ปีกู้สั้น วงเงินไม่ถึง 2.5M -> C ถูกต้องแล้ว
# แต่ไม่ใช่เคสที่ "จำเป็นต้องมีผู้กู้ร่วม" (low_income/high_burden ไม่ติด)
# ผลคือลีดที่โทรได้กลายเป็น X ไม่มีใครโทร เพียงเพราะลูกค้าพิมพ์ชื่อตามมาทีหลัง
# ไล่ทั้งช่วงรายได้ 25,000-120,000 x ผ่อน 0-60,000 x อายุ 25-60 เจอ 378 ชุดที่เข้าข่าย
#
# เงื่อนไขปลด: ใช้ธง _r90_solo_ok ที่ _finish ปั๊มไว้เท่านั้น
# (ไม่คิดใหม่ตรงนี้ เพราะ _finish คือประตูตัดสินทางเดียว ถ้ามันไม่ได้ปลดให้ = ไม่ปลด)
try:
    _R90_ORIG_SEND = _bl9.BotEngine._send_to_sheets

    def _send_to_sheets_r90(self, user_id, data, grade, *a, **k):
        _restore = False
        try:
            if data.get("_r90_solo_ok") and data.get("co_borrower_none"):
                data.pop("co_borrower_none", None)
                _restore = True
        except Exception as _e:
            print(f"[R90B ERROR] {_e} — ใช้ทางเดิม")
            _restore = False
        try:
            return _R90_ORIG_SEND(self, user_id, data, grade, *a, **k)
        finally:
            if _restore:
                try:
                    data["co_borrower_none"] = True
                except Exception as _e:
                    print(f"[R90B RESTORE ERROR] {_e}")

    _bl9.BotEngine._send_to_sheets = _send_to_sheets_r90
    print("[R90] กันเกรดพลิกเป็น X ตอนเขียนชีตซ้ำ เปิดแล้ว")
except Exception as _e:
    print(f"[R90B ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")

print("[R90] ครบชุด — ข้อ 3 ตัดเฉพาะเคสที่จำเป็นต้องมีผู้กู้ร่วมจริง")

# ======================================================================
# r91 — นิยาม "จำเป็นต้องมีผู้กู้ร่วม" ใหม่ (Gift ทัก 28 ส.ค. 2026 เช้า)
# ----------------------------------------------------------------------
# r90 ใช้ธง low_income / high_burden เป็นตัวชี้ว่า "จำเป็นต้องมีผู้กู้ร่วม"
# ซึ่งผิด เพราะ high_burden ถูกตั้งเมื่อ:
#       วงเงินตอนนี้ < ราคาห้อง  และ  วงเงินปลอดภาระ >= ราคาห้อง
# นั่นคือ "นิยามของเคสบริดจ์" เป๊ะๆ — ปิดหนี้แล้วไปได้
# แปลว่า r90 ตัดเคสบริดจ์ทิ้งทั้งหมดที่ลูกค้าบอกว่าไม่มีผู้กู้ร่วม
#
# เคสจริงที่ Gift ยกมา:
#   รายได้ 25,000 ผ่อน 10,000  -> เกรด B · วงเงินปลอดภาระ 2.54M ถึงเกณฑ์
#       = เคสบริดจ์ที่ปิดหนี้แล้วไปได้ ต้องเก็บ ต้องขอเบอร์ ต้องแจกเคส
#       แต่ r90 ตัดเป็น X เพราะ high_burden ติด
#   รายได้ 35,000 ผ่อน 10,000  -> B · ปลอดภาระ 4.15M
#   รายได้ 35,000 หนี้รวม 100,000 -> ปิดก้อนเดียวจบ ไม่ต้องมีผู้กู้ร่วมเลย
#
# นิยามใหม่ (ตรงกับที่ Gift สั่ง): จำเป็นต้องมีผู้กู้ร่วมจริง ก็ต่อเมื่อ
#       "ปิดภาระหมดแล้ววงเงินก็ยังไม่ถึงราคาห้อง"  = ผู้กู้ร่วมคือทางเดียวที่เหลือ
# ถ้าปิดภาระแล้วถึงเกณฑ์ = ไปเองได้ (ทางบริดจ์) -> เก็บเคส ขอเบอร์ แจกตามปกติ
# บวกอีก 2 ข้อที่ผู้กู้ร่วมคือทางเดียวจริงๆ ไม่เกี่ยวกับวงเงิน:
#       - อาชีพอิสระยังไม่ถึงเกณฑ์ธนาคาร (< 2 ปี ไม่มีภาษี/ทะเบียน)
#       - ไม่มีรายได้ของตัวเอง / อาชีพที่แบงก์ไม่นับเป็นรายได้
# ======================================================================
def _r91_cap_clear(data, state):
    """วงเงินถ้าปิดภาระหมด — คิดแบบเดียวกับใน _grade เป๊ะ (รวมอายุ + อาชีพอิสระ)"""
    d = data or {}
    st = state or {}
    inc = (d.get("income_total") or d.get("income_baht")
           or _bl9._parse_income(str(d.get("income", ""))))
    inc = int(inc) if inc else 0
    if not inc:
        return None
    if st.get("self_employed"):
        inc = int(inc * _bl9.FREELANCE_INCOME_PCT)
    _own = d.get("age")
    _co = d.get("co_age")
    if _co is not None:
        _age = min(_own, _co) if _own else _co
    elif d.get("co_borrower_income"):
        _age = None
    else:
        _age = _own
    return _bl9._capacity(inc, 0, _age)


def _r90_needs_cob(data, state):
    """เคสนี้ 'จำเป็นต้องมีผู้กู้ร่วม' จริงหรือไม่ (นิยาม r91)

    True = ผู้กู้ร่วมคือทางเดียวที่เหลือจริงๆ -> ไม่มี = ไม่รับเคส (X)
    False = ไปเองได้ ไม่ว่าจะตอนนี้หรือหลังปิดภาระ (เคสบริดจ์) -> เก็บเคส แจกตามปกติ
    ผิดพลาดเมื่อไหร่คืน True = ตัดเหมือนเดิม ปลอดภัยไว้ก่อน
    """
    d = data or {}
    st = state or {}
    try:
        if _bl9._self_emp_below_bar(d, st):
            return True          # ธนาคารไม่รับอยู่แล้ว วงเงินไม่ช่วย
        if d.get("no_own_income") or d.get("income_unbankable"):
            return True          # ไม่มีรายได้ของตัวเอง / แบงก์ไม่นับรายได้ก้อนนี้
        _cc = _r91_cap_clear(d, st)
        if _cc is None:
            return True          # ไม่รู้รายได้ = ฟันธงไม่ได้ ใช้ทางเดิม
        if _cc >= R89_UNIT_PRICE:
            return False         # ปิดภาระแล้วถึงเกณฑ์ = เคสบริดจ์ ต้องเก็บ
        return True              # ปิดภาระหมดแล้วยังไม่ถึง = ต้องมีผู้กู้ร่วมจริง
    except Exception as _e:
        print(f"[R91 NEEDCOB ERROR] {_e} — ถือว่าจำเป็นต้องมีผู้กู้ร่วม (ทางเดิม)")
        return True


print("[R91] นิยามใหม่: เคสบริดจ์ (ปิดหนี้แล้วไปได้) ไม่ถือว่าต้องมีผู้กู้ร่วม")

# ---------- r91b — เคสบริดจ์ที่ไม่มีผู้กู้ร่วม ห้ามปิดเคส ต้องขอเบอร์ต่อ ----------
# bot_logic บรรทัด 3160: ถ้ากำลังรอคำตอบเรื่องผู้กู้ร่วม แล้วลูกค้าบอกว่าไม่มี
#   -> contact_refused = True · below_threshold = True · _finish(calendar=False)
#      · _close_chat() · ตอบ NO_COBORROWER_CLOSE แล้วจบบทสนทนา
# = ไม่ขอเบอร์เลย  ซึ่งถูกสำหรับเคสที่ยื่นเดี่ยวไม่ผ่านจริง
# แต่ผิดสำหรับเคสบริดจ์ (ปิดภาระแล้ววงเงินถึงเกณฑ์) ที่ Gift สั่งว่า
#   "ต้องเอาเบอร์มานะ แล้วก็แจกเคสด้วย"
#
# เคสจริง: รายได้ 25,000 ผ่อน 10,000 -> วงเงินปลอดภาระ 2.54M = เกรด B เคสบริดจ์
#   แต่พอบอกว่าไม่มีผู้กู้ร่วม บอทปิดเคสทันที ไม่ขอเบอร์ = ลีดหายทั้งใบ
#
# วิธี: ก่อนส่งเทิร์นให้เอนจินเดิม ถ้าเจอสถานการณ์นี้และ "ไม่จำเป็นต้องมีผู้กู้ร่วม"
# ให้ล้าง awaiting ทิ้งก่อน -> เงื่อนไข awaiting in (co_borrower, co_income) ไม่ตรง
# -> ไม่เข้าทางปิดเคส -> funnel เดินต่อไปขอช่องทางติดต่อตามปกติ
# ธง co_borrower_none ยังถูกตั้งตามเดิม สัญญาณยังติดไปกับลีด เซลเห็นครบ
try:
    _R91_BASE_DECIDE = CalmBotEngine._decide

    def _decide_r91(self, msg, user_id, state, bucket, is_new):
        try:
            _aw = state.get("awaiting")
            if (_aw in ("co_borrower", "co_income")
                    and _bl9._says_no_coborrower(msg)
                    and not state.get("done")
                    and not _r90_needs_cob(state.get("data") or {}, state)):
                state["awaiting"] = None
                self._add_signal(
                    state,
                    "ลูกค้าไม่มีผู้กู้ร่วม แต่ปิดภาระแล้ววงเงินถึงราคาห้อง "
                    "— เคสบริดจ์ ไม่ปิดเคส ขอช่องทางติดต่อต่อตามปกติ (Gift สั่ง 28 ส.ค.)")
                print(f"[R91] {str(user_id)[:8]}... ไม่มีผู้กู้ร่วมแต่เป็นเคสบริดจ์ "
                      "— คุยต่อ ขอเบอร์")
        except Exception as _e:
            print(f"[R91 DECIDE ERROR] {_e} — ใช้ทางเดิม")
        return _R91_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)

    CalmBotEngine._decide = _decide_r91
    print("[R91] เคสบริดจ์ไม่มีผู้กู้ร่วม — ไม่ปิดเคส ขอเบอร์ต่อ เปิดแล้ว")
except Exception as _e:
    print(f"[R91B ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")

# ======================================================================
# r92 — จับอายุให้ได้ทุกแบบที่คนพูด + เตือนเมื่อระบบเดาอายุ (Gift เคาะ 28 ส.ค. 2026)
# ----------------------------------------------------------------------
# ปัญหาที่เจอตอนตรวจเกณฑ์:
#   _has_age_signal("อายุ 58 แล้วค่ะ") = False  <-- ไม่รู้จักคำว่า "อายุ 58"
#   รู้จักแค่ เกษียณ/บำนาญ/อายุมาก/สูงวัย เท่านั้น
#   พอไม่รู้อายุ -> DEFAULT_AGE = 35 -> ได้ปีกู้เต็ม -> วงเงินสูงเกินจริง
#
# ลูกค้าคนเดียวกัน รายได้ 30,000 อายุ 58 พูดคนละแบบ ได้ผลตรงข้ามกัน:
#   "อายุ 58 แล้วค่ะ"        -> ระบบไม่เก็บอายุ -> คิดที่ 35 -> วงเงิน 3.56M -> A โทรใน 2 ชม.
#   "ใกล้เกษียณแล้วครับ"      -> ระบบถามอายุ ได้ 58 -> วงเงิน 1.91M -> X
# (ข้อความ "อายุ 58 แล้วค่ะ" มีอยู่จริงใน Chat_Log ของเราแล้ว)
#
# แก้ 3 อย่าง:
#   1. อ่านอายุจากข้อความได้เอง ไม่ต้องรอให้บอทถาม — "อายุ 58" / "58 ปีแล้ว" / "เกิดปี 2510"
#   2. เก็บแล้วเดินเส้นทางเดียวกับตอนตอบคำถามอายุเป๊ะ (ปีกู้ · ธงปีกู้สั้น · เสนอผู้กู้ร่วม)
#      -> พูดแบบไหนก็ได้ผลเหมือนกัน ไม่มีทางลัดให้เกรดเพี้ยน
#   3. ถ้ายังไม่รู้อายุจริง ติดธงบอกเซลว่า "วงเงินนี้คิดบนสมมติฐานอายุ 35"
#
# ไม่ถามอายุทุกคนเหมือนเดิม (Gift เคาะ 19 ส.ค.) — แค่ "ได้ยินแล้วต้องเก็บ"
# ======================================================================
import time as _t92

# คำที่บอกว่าตัวเลขนั้นคือ "อายุงาน" ไม่ใช่อายุคน — เจอแล้วห้ามอ่านเป็นอายุ
_R92_JOB_WORDS = ("อายุงาน", "ทำงานมา", "ทำมา", "ประสบการณ์", "ผ่อนมา", "เปิดร้านมา",
                  "ขายมา", "ทำอาชีพนี้มา", "อยู่บริษัทนี้", "ทำงานที่นี่",
                  "จดทะเบียนมา", "เปิดบริษัทมา", "ปิดมา", "ผ่อนมาแล้ว")


def _r92_parse_age(msg):
    """อ่านอายุจากข้อความ เฉพาะตอนที่บริบทชัดว่าเป็น 'อายุคน'

    รับ: "อายุ 58" · "อายุ58ปี" · "58 ปีแล้วครับ" · "เกิดปี 2510" · "เกิด พ.ศ. 2510"
    ไม่รับ: "ทำงานมา 20 ปี" · "อายุงาน 3 ปี" · "ปิดมา 5 ปี" · "ห้อง 35 ตร.ม."
    อ่านไม่ออกคืน None (ห้ามเดา)
    """
    try:
        s = str(msg or "").replace(",", "")
        # ประโยคที่พูดเรื่องอายุงาน/ระยะเวลา — ห้ามเดาจากรูปแบบ "NN ปี" เด็ดขาด
        _job = any(w in s for w in _R92_JOB_WORDS)
        for w in _R92_JOB_WORDS:
            s = s.replace(w, " ")
        # 1) มีคำว่า "อายุ" นำหน้าตัวเลข = ชัดเจนที่สุด
        mt = re.search(r"อายุ\s*(?:ประมาณ|ราว|ราวๆ)?\s*(\d{1,2})", s)
        if mt:
            n = int(mt.group(1))
            if 18 <= n <= 90:
                return n
        # 2) ปีเกิด — รับทั้ง พ.ศ. และ ค.ศ.
        mt = re.search(r"เกิด[^0-9]{0,12}(\d{4})", s)
        if mt:
            y = int(mt.group(1))
            _ce = _t92.localtime().tm_year
            if 2300 <= y <= _ce + 543:          # พ.ศ.
                n = (_ce + 543) - y
            elif 1900 <= y <= _ce:              # ค.ศ.
                n = _ce - y
            else:
                n = None
            if n is not None and 18 <= n <= 90:
                return n
        # 3) "58 ปีแล้ว" / "58 ปีครับ" — เฉพาะตอนไม่มีคำเรื่องอายุงานปนอยู่
        mt = None if _job else re.search(r"(?<!\d)(\d{2})\s*ปี(?!\s*ที่แล้ว)", s)
        if mt:
            n = int(mt.group(1))
            if 18 <= n <= 90:
                return n
    except Exception as _e:
        print(f"[R92 PARSE ERROR] {_e}")
    return None


def _r92_apply_age(self, user_id, state, age):
    """เก็บอายุแล้วเดินเส้นทางเดียวกับตอนลูกค้าตอบคำถามอายุ (bot_logic §0.46)

    คืน list ของบับเบิล ถ้าต้องพูดเรื่องผู้กู้ร่วมต่อ · คืน None ถ้าไหลต่อตามปกติ
    """
    data = state["data"]
    data["age"] = age
    _term = max(0, _bl9.AGE_CAP_MAX - age)
    data["age_term"] = _term
    if _term <= _bl9.AGE_TERM_ALERT:
        data["age_short_term"] = True
        data["low_income"] = True
        self._add_signal(
            state,
            f"⚠️ อายุ {age} — เพดานผ่อนถึง {_bl9.AGE_CAP_MAX} เหลือกู้ได้ {_term} ปี "
            f"ค่างวด/ล้านสูงกว่าปกติเกือบเท่าตัว · แบงก์ที่ปิดที่ 65 "
            f"(TTB/SCB/BBL/KBANK/BAY) แทบใช้ไม่ได้ · ต้องหาผู้กู้ร่วมอายุน้อย "
            f"(ลูกค้าบอกอายุเอง ไม่ได้ถาม)")
        if not state.get("done") and not state.get("asked", {}).get("co_borrower"):
            state["awaiting"] = "co_borrower"
            state.setdefault("asked", {})["co_borrower"] = 1
            print(f"[R92] {str(user_id)[:8]}... ลูกค้าบอกอายุ {age} เอง เหลือกู้ {_term} ปี "
                  "— เสนอผู้กู้ร่วมอายุน้อย")
            return [_bl9.AGE_COBORROWER_MSG.format(years=_term), _bl9.AGE_COBORROWER_Q]
    else:
        self._add_signal(state, f"อายุ {age} — กู้ได้ถึง {_term} ปี ยังอยู่ในเกณฑ์ปกติ "
                                "(ลูกค้าบอกเอง ไม่ได้ถาม)")
    print(f"[R92] {str(user_id)[:8]}... เก็บอายุ {age} จากข้อความลูกค้า (ปีกู้ {_term})")
    return None


try:
    _R92_BASE_DECIDE = CalmBotEngine._decide

    def _decide_r92(self, msg, user_id, state, bucket, is_new):
        try:
            _d = state.get("data") or {}
            if (_d.get("age") is None and not state.get("awaiting_age")
                    and not state.get("closed") and not state.get("done")):
                _age = _r92_parse_age(msg)
                if _age is not None:
                    _bub = _r92_apply_age(self, user_id, state, _age)
                    state["age_asked"] = True     # รู้แล้ว ไม่ต้องถามซ้ำ
                    if _bub:
                        return _bub, None
        except Exception as _e:
            print(f"[R92 DECIDE ERROR] {_e} — ใช้ทางเดิม")
        return _R92_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)

    CalmBotEngine._decide = _decide_r92
    print("[R92] อ่านอายุจากข้อความลูกค้าได้เองแล้ว (อายุ 58 / 58 ปี / เกิดปี 2510)")
except Exception as _e:
    print(f"[R92 ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")


# ---------- r92b — ไม่รู้อายุ ต้องบอกเซลว่าวงเงินนี้เดามา ----------
# DEFAULT_AGE = 35 ทำให้วงเงินสูงกว่าความจริงได้ถึง 46% ถ้าลูกค้าอายุ 58
# เซลต้องรู้ว่าตัวเลขนี้ยังไม่ยืนยัน จะได้ถามอายุตอนโทรก่อนเสนอห้อง
try:
    _R92_ORIG_GRADE = _bl9.BotEngine._grade

    def _grade_r92(self, data, state=None):
        _g = _R92_ORIG_GRADE(self, data, state)
        try:
            if (state is not None and _g in ("A", "B", "C", "N")
                    and data.get("age") is None and data.get("co_age") is None
                    and not data.get("cash") and data.get("capacity_now") is not None):
                self._add_signal(
                    state,
                    f"⚠️ ยังไม่รู้อายุลูกค้า — วงเงิน {data['capacity_now']/1e6:.1f}M นี้ "
                    f"คิดบนสมมติฐานอายุ {_bl9.DEFAULT_AGE} ปี (ได้ปีกู้เต็ม) "
                    "ถ้าอายุจริง 55+ วงเงินจะหายเกือบครึ่ง · ถามอายุตอนโทรก่อนเสนอห้อง")
        except Exception as _e:
            print(f"[R92B ERROR] {_e}")
        return _g

    _bl9.BotEngine._grade = _grade_r92
    print("[R92] เตือนเซลเมื่อวงเงินคิดบนสมมติฐานอายุ 35 เปิดแล้ว")
except Exception as _e:
    print(f"[R92B ERROR] ต่อไม่ติด: {_e}")

print("[R92] ครบชุด — อายุ: พูดแบบไหนก็ได้ผลเหมือนกัน")

# ======================================================================
# r93 — สายซื้อสด + เคสธุรกิจ (Gift เคาะ 28 ส.ค. 2026)
# ======================================================================
# ส่วน A: สายซื้อสด
# --------------------------------------------------
# ข้อมูลจาก Gift: ทำบริษัทมา 10 ปี คนที่บอกว่า "ซื้อสด" ปิดจริงแค่ 0.1%
#   = 1,000 เคส ได้ซื้อสดจริง 1 เคส
# แปลว่าคำว่า "ซื้อสด" ไม่ใช่สัญญาณลูกค้าคุณภาพ และ 999/1,000 สุดท้าย
# คือลูกค้าสินเชื่อธรรมดา  แต่ของเดิมพอได้ยินคำนี้ บอทหยุดคัดกรองทั้งหมดทันที
# (ข้ามคำถาม รายได้ · ภาระ · ผู้กู้ร่วม · อาชีพอิสระ · สหกรณ์)
# ผลจริงในชีต: ลีดสายนี้ 8 ราย ได้เบอร์ 0 ราย · ครึ่งหนึ่งไม่มีเกรด = ไม่ถูกแจก
#
# แก้ 4 อย่าง (Gift อนุมัติ):
#   1. เบอร์โทรที่มาตอนบอทถามงบ = เก็บเป็นเบอร์ ไม่ใช่งบ
#   2. สายเงินสดต้องปิดเคสได้จริง (ไหลไป _finish เหมือนสายกู้)
#   3. อย่าหยุดคัดกรอง — ยังถามรายได้/ภาระ/ผู้กู้ร่วมต่อตามปกติ
#   4. ตีเกรดจากข้อมูลจริงเหมือนลูกค้าทั่วไป + ติดธง "แจ้งว่าซื้อสด" ให้เซลเห็น
#      ไม่ให้คิวพิเศษ (ถ้าให้ A = เอาคนไม่ซื้อ 999 คนไปเบียดคิวโทร 2 ชม.)
#
# ส่วน B: เคสธุรกิจ — ถามยอดขาย + ภ.พ.30
# --------------------------------------------------
# เคสจริง FB-WE-20260827-232: "ทำธุรกิจส่วนตัว" -> เกรด N ไม่มีตัวเลขอะไรเลย
# ของเดิมถามแค่ "ทำมากี่ปี + มีภาษี/จดบริษัทไหม" ซึ่งไม่พอคิดรายได้
# กฎที่มีอยู่แล้ว §0.5: รายได้เจ้าของธุรกิจ = ยอดขาย × margin × %หุ้น
#   (README_engine ระบุเองว่า "ยังไม่มีใน engine" -> ทีมวิเคราะห์คิดมือ)
# บอทเป็นด่านคัดหยาบ หน้าที่คือ "เก็บอินพุตให้ครบ" ไม่ใช่คิดแทน
# Gift เคาะ: ยอดขายก่อนหัก >= 500,000 ต่อเดือน · ต่ำกว่านั้นเก็บเคส แจกปกติ ติดธงไว้
#            ไม่ยื่น ภ.พ.30 = ไม่ตัดทิ้ง ให้ถามหาหลักฐานอื่นแทน
# ======================================================================
R93_BIZ_SALES_BAR = 500_000      # ยอดขายก่อนหักต่อเดือน ที่ถือว่าเข้าเกณฑ์

R93_BIZ_Q = ("ขอถามเรื่องกิจการอีกนิดเดียวครับ ยอดขายต่อเดือน "
             "ก่อนหักค่าใช้จ่ายประมาณเท่าไหร่ครับ แล้วได้ยื่น ภ.พ.30 ไหมครับ "
             "(บอกคร่าวๆ ได้เลยครับ)")

R93_NO_VAT_NOTE = ("ถ้ายังไม่ได้ยื่น ภ.พ.30 ไม่เป็นไรครับ ธนาคารดูหลักฐานอื่นแทนได้ "
                   "เช่น ทะเบียนพาณิชย์ หรือ statement บัญชีร้านย้อนหลัง "
                   "เดี๋ยวที่ปรึกษาช่วยดูให้ครับ")

_R93_VAT_YES = ("ยื่น", "ยื่นแล้ว", "มี", "จดแล้ว", "จดvat", "จด vat", "ภพ30",
                "ภ.พ.30", "ภ.พ. 30", "เสียvat", "มีvat")
_R93_VAT_NO = ("ไม่ยื่น", "ไม่ได้ยื่น", "ยังไม่ยื่น", "ไม่มี", "ไม่ได้จด",
               "ยังไม่จด", "ไม่ได้ทำ", "ไม่เคยยื่น")


def _r93_has_phone(msg):
    try:
        return bool(re.search(r"0[0-9]{8,9}",
                              str(msg or "").replace("-", "").replace(" ", "")))
    except Exception:
        return False


def _r93_parse_sales(msg):
    """อ่านยอดขายต่อเดือน — ตัดเบอร์โทรทิ้งก่อนเสมอ อ่านไม่ออกคืน None"""
    try:
        s = str(msg or "").replace(",", "").lower()
        s = re.sub(r"0[0-9]{8,9}", " ", s)
        best = None
        for _m in re.finditer(r"([0-9]+(?:\.[0-9]+)?)\s*(ล้าน|แสน|หมื่น|พัน|k|m)", s):
            v = round(float(_m.group(1)) * _R89_TOTAL_UNITS[_m.group(2)])
            if 10_000 <= v <= 1_000_000_000 and (best is None or v > best):
                best = v
        if best is not None:
            return best
        for _n in re.findall(r"[0-9]{5,10}", s):
            v = int(_n)
            if 10_000 <= v <= 1_000_000_000 and (best is None or v > best):
                best = v
        return best
    except Exception as _e:
        print(f"[R93 SALES PARSE ERROR] {_e}")
        return None


def _r93_parse_vat(msg):
    """ยื่น ภ.พ.30 หรือไม่ — ไม่ชัดคืน None (ห้ามเดา)"""
    try:
        m = str(msg or "").replace(" ", "").lower()
        if any(w.replace(" ", "") in m for w in _R93_VAT_NO):
            return False
        if any(w.replace(" ", "") in m for w in _R93_VAT_YES):
            return True
    except Exception:
        pass
    return None


# ---------- A3 — ไม่หยุดคัดกรองเพราะคำว่า "ซื้อสด" ----------
try:
    _R93_ORIG_NEXT = _bl9.BotEngine._next_missing

    def _next_missing_r93(self, data, state=None, skip=None):
        _restore = False
        try:
            if data.get("cash") and data.get("_r93_keep_qualifying"):
                data.pop("cash", None)
                _restore = True
        except Exception as _e:
            print(f"[R93 NEXT ERROR] {_e} — ใช้ทางเดิม")
            _restore = False
        try:
            return _R93_ORIG_NEXT(self, data, state, skip)
        finally:
            if _restore:
                try:
                    data["cash"] = True
                except Exception:
                    pass

    _bl9.BotEngine._next_missing = _next_missing_r93
    print("[R93] สายซื้อสด — ยังคัดกรองรายได้/ภาระต่อตามปกติ เปิดแล้ว")
except Exception as _e:
    print(f"[R93A ERROR] ต่อไม่ติด: {_e}")


# ---------- A1/A2/A4 + B — แทรกที่ชั้น _decide ----------
try:
    _R93_BASE_DECIDE = CalmBotEngine._decide

    def _decide_r93(self, msg, user_id, state, bucket, is_new):
        # ---- B: กำลังรอคำตอบยอดขาย/ภ.พ.30 อยู่ ----
        if state.get("_r93_wait_biz"):
            try:
                return _r93_consume_biz(self, msg, user_id, state, bucket, is_new)
            except Exception as _e:
                print(f"[R93 BIZ ERROR] {_e} — ไปทางเดิม")
                state.pop("_r93_wait_biz", None)
                state.pop("_r93_biz_resume", None)

        # ---- A1: เบอร์โทรมาตอนบอทถามงบเงินสด -> อย่ากลืนเป็นงบ ----
        try:
            if state.get("awaiting") == "cash_budget" and _r93_has_phone(msg):
                state["awaiting"] = "contact"
                print(f"[R93] {str(user_id)[:8]}... เบอร์มาตอนถามงบ — เก็บเป็นเบอร์ ไม่ใช่งบ")
        except Exception as _e:
            print(f"[R93 PHONE ERROR] {_e}")

        _prev_wait = state.get("awaiting")
        bubbles, grade = _R93_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)

        # ---- A2/A4: เพิ่งกลายเป็นสายเงินสด -> ล้างค่าหลอก + ติดธง ----
        try:
            _d = state.get("data") or {}
            if _d.get("cash") and not _d.get("_r93_cash_noted"):
                _d["_r93_cash_noted"] = True
                _d["_r93_keep_qualifying"] = True
                if str(_d.get("income", "")).strip() == "ซื้อเงินสด":
                    _d.pop("income", None)
                if str(_d.get("debt", "")).strip() == "-":
                    _d.pop("debt", None)
                self._add_signal(
                    state,
                    "ลูกค้าแจ้งว่าซื้อเงินสด — ยังคัดกรองรายได้/ภาระต่อตามปกติ "
                    "(สถิติ 10 ปี: สายนี้ปิดจริง 0.1%) ตีเกรดจากข้อมูลจริง ไม่ให้คิวพิเศษ")
                print(f"[R93] {str(user_id)[:8]}... สายเงินสด — คัดกรองต่อ ไม่หยุด")
            # งบเงินสดถูกยัดลงช่อง debt -> ย้ายออก ไม่งั้นบอทไม่ถามภาระจริง
            if str(_d.get("debt", "")).startswith("งบเงินสด"):
                _d["cash_budget_note"] = _d.pop("debt")
        except Exception as _e:
            print(f"[R93 CASH ERROR] {_e}")

        # ---- B: ถึงเวลาถามยอดขาย/ภ.พ.30 หรือยัง ----
        try:
            _d = state.get("data") or {}
            if (state.get("self_employed") and grade is None
                    and not state.get("done") and not _d.get("cash")
                    and "biz_sales" not in _d
                    and not state.get("_r93_biz_asked")
                    and (_d.get("self_emp_years") is not None
                         or _d.get("biz_registered") or _d.get("self_emp_tax") is not None)
                    # ต้องได้ตัวเลขรายได้ก่อน ไม่งั้นคำถามยอดขายจะไปกินคำตอบรายได้
                    and (_d.get("income_total") or _d.get("income_baht")
                         or _bl9._parse_income(str(_d.get("income", ""))))
                    and bubbles and state.get("awaiting")):
                state["_r93_biz_asked"] = True
                state["_r93_biz_resume"] = list(bubbles)
                state["_r93_wait_biz"] = True
                print(f"[R93] {str(user_id)[:8]}... เคสธุรกิจ — ถามยอดขาย + ภ.พ.30")
                return [R93_BIZ_Q], None
        except Exception as _e:
            print(f"[R93 BIZ ASK ERROR] {_e}")

        return bubbles, grade

    CalmBotEngine._decide = _decide_r93
    print("[R93] เคสธุรกิจ — ถามยอดขาย/เดือน + ภ.พ.30 เปิดแล้ว")
except Exception as _e:
    print(f"[R93B ERROR] ต่อไม่ติด: {_e}")


def _r93_consume_biz(self, msg, user_id, state, bucket, is_new):
    """กินคำตอบยอดขาย/ภ.พ.30 แล้วส่งคำถามที่พักไว้ต่อทันที"""
    state.pop("_r93_wait_biz", None)
    resume = state.pop("_r93_biz_resume", None) or []
    data = state["data"]
    # ลูกค้าส่งเบอร์แทนคำตอบ -> ห้ามกลืน คืนเทิร์นให้เอนจินเดิมจับเบอร์
    if _r93_has_phone(msg):
        return _R93_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
    _sales = _r93_parse_sales(msg)
    _vat = _r93_parse_vat(msg)
    if _sales is None and self._is_question(msg):
        state["_r93_wait_biz"] = True          # ยังไม่ได้คำตอบ ถามใหม่รอบหน้า
        state["_r93_biz_resume"] = resume
        return _R93_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)

    data["biz_sales"] = _sales                 # None = ถามแล้วไม่ได้ตัวเลข ห้ามถามซ้ำ
    if _vat is not None:
        data["biz_vat30"] = _vat
    if _sales is not None:
        if _sales >= R93_BIZ_SALES_BAR:
            self._add_signal(
                state,
                f"เจ้าของธุรกิจ ยอดขาย ~{_sales:,}/เดือน (ก่อนหัก) เข้าเกณฑ์ "
                f"(>= {R93_BIZ_SALES_BAR:,}) — รายได้จริงให้ทีมวิเคราะห์คิดจาก "
                "ยอดขาย x margin x %หุ้น (§0.5) + เกรดบริษัทจาก DBD")
        else:
            self._add_signal(
                state,
                f"⚠️ เจ้าของธุรกิจ ยอดขาย ~{_sales:,}/เดือน ต่ำกว่าเกณฑ์ "
                f"{R93_BIZ_SALES_BAR:,}/เดือน — เก็บเคสแจกปกติ เซลตรวจยอดจริงตอนโทร")
    else:
        self._add_signal(
            state,
            f"ถามยอดขายเจ้าของธุรกิจแล้ว ลูกค้าตอบ: {str(msg)[:60]} "
            "— เซลถามยอดขาย/เดือน ตอนโทร")
    if _vat is True:
        self._add_signal(state, "ยื่น ภ.พ.30 — ใช้เป็นหลักฐานรายได้ได้ ขอย้อนหลัง 6-12 เดือน")
    elif _vat is False:
        self._add_signal(state, "ยังไม่ยื่น ภ.พ.30 — ไม่ตัดเคส ให้เซลขอหลักฐานอื่นแทน "
                                "(ทะเบียนพาณิชย์ / statement บัญชีร้านย้อนหลัง)")
    _head = ["รับทราบครับ"]
    if _vat is False:
        _head = [R93_NO_VAT_NOTE]
    if resume and str(resume[0]).startswith(("ขอบคุณ", "รับทราบ")):
        return resume, None
    return _head + resume, None


# ---------- r93c — "ทำธุรกิจส่วนตัว" ไม่ใช่การปฏิเสธบอกรายได้ ----------
# _INCOME_REFUSE_WORDS มีคำว่า "ส่วนตัว" ลอยๆ (ตั้งใจจับ "เป็นเรื่องส่วนตัว")
# แต่มันไปแมตช์ "ทำธุรกิจส่วนตัว" / "กิจการส่วนตัว" ด้วย
# ผล: เจ้าของธุรกิจที่ตอบคำถามรายได้ว่า "ทำธุรกิจส่วนตัวครับ"
#     ถูกบันทึกว่า income_refused = True -> เกณฑ์ข้อ 3 -> X ไม่รับเคส ไม่แจก ไม่โทร
# ซ้ำร้าย คำเดียวกันนี้อยู่ใน _SELF_EMP_WORDS ด้วย = ข้อความเดียวถูกตีความขัดกันเอง
# เคสจริง FB-WE-20260827-232: "รายได้/อาชีพ (Q2): ทำธุรกิจส่วนตัว"
# แก้: ตัดวลีธุรกิจออกก่อนเช็คคำปฏิเสธ — "เป็นเรื่องส่วนตัว" ยังนับว่าปฏิเสธเหมือนเดิม
_R93_BIZ_PRIVATE = ("ธุรกิจส่วนตัว", "กิจการส่วนตัว", "งานส่วนตัว", "ร้านส่วนตัว",
                    "อาชีพส่วนตัว", "ค้าขายส่วนตัว", "บริษัทส่วนตัว", "กิจส่วนตัว")
try:
    _R93_ORIG_REFUSE = _bl9._refuses_income

    def _refuses_income_r93(msg):
        try:
            _s = str(msg or "")
            _cleaned = _s
            for _w in _R93_BIZ_PRIVATE:
                _cleaned = _cleaned.replace(_w, " ")
            if _cleaned != _s:
                _out = _R93_ORIG_REFUSE(_cleaned)
                if not _out:
                    print(f"[R93] {_s[:32]!r} = บอกอาชีพ ไม่ใช่ปฏิเสธบอกรายได้")
                return _out
        except Exception as _e:
            print(f"[R93 REFUSE ERROR] {_e} — ใช้ทางเดิม")
        return _R93_ORIG_REFUSE(msg)

    _bl9._refuses_income = _refuses_income_r93
    print("[R93] 'ทำธุรกิจส่วนตัว' ไม่ถูกนับเป็นปฏิเสธบอกรายได้แล้ว")
except Exception as _e:
    print(f"[R93C ERROR] ต่อไม่ติด: {_e}")

print("[R93] ครบชุด — ซื้อสดคัดกรองต่อ · เคสธุรกิจถามยอดขาย+ภ.พ.30")






# ======================================================
# Main
# ======================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"WEC Bot v3.3 starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
