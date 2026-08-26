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
                      PUBLIC_COMMENT_THANKS_F)
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
BOT_REVISION = "r83"
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


def send_reply(recipient_id: str, text: str, page_id: str = "",
               force: bool = False):
    """ยิงในเธรดเบื้องหลัง — webhook จะได้ตอบ 200 ให้ Meta ภายในไม่กี่ ms

    ถ้าหน่วงพิมพ์อยู่ใน request เลย Meta จะถือว่า timeout แล้วยิง event ซ้ำ
    ลูกค้าจะได้ข้อความซ้ำสองรอบ — เคยเจอมาแล้วตอน budget 8 วิ
    """
    # r70 — เพจโหมดเก็บข้อมูล: ด่านสุดท้ายก่อนยิงจริง
    # r71 — ยกเว้นข้อความดับอารมณ์ (force=True) Gift เคาะ 23 ส.ค. 2026:
    #       "คงโหมดเงียบไว้ แต่ให้ override ตอนโกรธ"
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
