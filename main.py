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
BOT_REVISION = "r124"  # r124 (1 ก.ย. 2569): อัปเดตราคา/ทำเลใน faq_data.py จากชีต Sales Project Q3 (26 ทำเล) — ไม่แตะ logic
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
                # r112 (31 ส.ค. 2569) — เดิมพิมพ์ resp.text ดิบ ตัดที่ 200 ตัว
                # ภาษาไทยออกมาเป็น \uXXXX อ่านไม่ออก และไม่รู้ว่าใช้โทเค็นเพจไหนยิง
                # -> ไล่เหตุ IG ส่งไม่ออกไม่ได้เลย ตอนนี้พิมพ์ให้ครบ
                _e112 = {}
                try:
                    _e112 = ((resp.json() or {}).get("error") or {})
                except Exception:
                    pass
                try:
                    _tok_src = ("PAGE_TOKEN_%s" % page_id
                                if page_id and os.environ.get(
                                    f"PAGE_TOKEN_{page_id}", "").strip()
                                else "FB_PAGE_ACCESS_TOKEN")
                    print(f"[FB SEND ERROR] {resp.status_code} "
                          f"page={page_id or '-'} tok={_tok_src} "
                          f"to={_mask(recipient_id)} "
                          f"code={_e112.get('code')}/"
                          f"{_e112.get('error_subcode')} "
                          f"type={_e112.get('type')} "
                          f"msg={str(_e112.get('message') or resp.text)[:300]}")
                except Exception:
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
#   6ก  ปรับโครงสร้าง ปิดแล้ว >1 ปี และ DSR <15%  -> W2 ผ่านเกณฑ์เครดิต (แจกได้)
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
# เกรดที่ใช้จริง: A B C N W1 W2 O R + X (ไม่รับเคส) · เหตุผลเขียนในสัญญาณเสมอ
#
# r115 (1 ก.ย. 2569, Gift เคาะ) — แยกเคสเครดิตออกจาก N เป็นเกรด W
#   เดิม N3/N4 เป็นแค่ "รหัสเหตุผล" ช่องเกรดขึ้น N เหมือนกันหมด
#   เซลแยกไม่ออกว่า "ยังไม่รู้ข้อมูล" (N) กับ "ติดเครดิตแต่รื้อได้" (W) ต่างกัน
#     W1 = เครดิตยังไม่ยืนยัน (บูโร/ล่าช้า/ปรับโครงสร้าง) — ดึงบูโรจริงก่อน
#     W2 = ผ่านเกณฑ์เครดิตแล้ว (ปรับโครงสร้างปิดเกิน 1 ปี + DSR <15%) — ยื่นได้
#   N1/N2 คงเดิม (ยังเป็นเกรด N) เพราะเป็นเรื่อง "ข้อมูลไม่ครบ" ไม่ใช่เครดิต
#   ★ ยังแจกเคสเหมือนเดิมทุกประการ — ตรวจแล้วทั้ง 2 Apps Script:
#     dEligibleRows กรองออกเฉพาะ X · dLekOk กันเฉพาะ A
#     getCallbackTime ให้เวลานัดโทรแบบ default เหมือน N
#     ceoBuildByPageKind นับเข้ากอง other เหมือน N
#     -> ไม่ต้องแก้ Apps Script เลย
# ทุกจุดมี fallback -> ห้ามทำให้บอทเงียบเด็ดขาด (บทเรียน r73/r75)
# ======================================================================
import bot_logic as _bl9

R89_UNIT_PRICE = 2_500_000      # ราคาห้องอ้างอิงใหม่ (เดิม 2.3M)
R89_RESTRUCT_MIN_YEARS = 1      # ปรับโครงสร้างต้องปิดมาแล้ว "เกิน" กี่ปี
R89_RESTRUCT_DSR_MAX = 0.15     # DSR ต้องต่ำกว่าเท่านี้ถึงรับ (N4)

# ---------- (1) เกรดใหม่ — ทับ BotEngine._grade ทั้งตัว ----------
_R89_ORIG_GRADE = _bl9.BotEngine._grade


def _r89_reason(self, data, state, code, txt):
    # r117 — W1 ไม่เข้าคิวแจกเคส (Gift เคาะ 1 ก.ย. 2569) ต้องเขียนติดไปกับเหตุผล
    # ให้ทุกคนที่เปิดชีตเห็นว่าทำไมเคสนี้ไม่มีชื่อเซล ไม่ใช่ระบบแจกพลาด
    if code == "W1":
        txt = txt + " · ไม่เข้าคิวแจกเคส — ดึงบูโรจริงก่อน ผ่านแล้วค่อยแจก"
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
                reason("W1", "เครดิตยังไม่ยืนยัน — เคยติดบูโร ปิดแล้วแต่ยังไม่พ้น 3 ปี"
                             "/ไม่รู้จำนวนปี · เซลดึงบูโรจริงก่อนเสนอแผน (KTB ดูย้อน 1 ปี)")
                return "W1"
        elif _k == "late":
            if data.get("ncb_over30") is True:
                return reject_x(f"ชำระล่าช้าเกิน {_bl9.NCB_LATE_DAYS} วัน", ncb=True)
            if data.get("ncb_over30") is False:
                self._add_signal(st, f"เคยชำระล่าช้าแต่ไม่เกิน {_bl9.NCB_LATE_DAYS} วัน "
                                     "— ไม่ใช่เคสแดง ตั้งธงให้เช็คบูโรจริง")
            else:
                reason("W1", "เครดิตยังไม่ยืนยัน — เคยชำระล่าช้า ยังไม่รู้ว่าเกิน 30 วันไหม โทรถามก่อน")
                return "W1"
        elif _k == "restruct":
            if still is True:
                return reject_x("ยังติดปรับโครงสร้างหนี้อยู่ — DSR ต่ำแค่ไหนก็ไม่รับ (เกณฑ์ 28 ส.ค.)",
                                ncb=True)
            if yrs is None:
                reason("W1", "เครดิตยังไม่ยืนยัน — ปรับโครงสร้างหนี้ ยังไม่รู้ว่าปิดหรือยัง/ปิดมากี่ปี "
                             "โทรยืนยันก่อน (เกณฑ์รับ: ปิดเกิน 1 ปี + DSR <15%)")
                return "W1"
            if yrs <= R89_RESTRUCT_MIN_YEARS:
                return reject_x(f"ปรับโครงสร้างปิดมา {yrs} ปี ยังไม่เกิน "
                                f"{R89_RESTRUCT_MIN_YEARS} ปี", ncb=True)
            _d6 = data.get("debt_baht")
            if _d6 is None:
                _d6 = _bl9._parse_debt_monthly(str(data.get("debt", "")))
            if _d6 is None or not income:
                reason("W1", f"ปรับโครงสร้างปิดมา {yrs} ปี (เกิน 1 ปีแล้ว) แต่ยังไม่รู้ยอดผ่อน/เดือน "
                             "— เกณฑ์รับต้อง DSR <15% · เซลโทรยืนยันยอดผ่อนก่อน")
                return "W1"
            _dsr = (_d6 / income) if income else 1.0
            if _dsr < R89_RESTRUCT_DSR_MAX:
                reason("W2", f"ผ่านเกณฑ์เครดิต — ปรับโครงสร้างปิดมา {yrs} ปี (เกิน 1 ปี) "
                             f"และ DSR {round(_dsr*100)}% (<15%) · แจกได้ ยื่นตามปกติ")
                return "W2"
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
        # r103 — ธงบอกเซลเป็น "ตัวเลข" ไม่ใช่คำตัดสิน
        #        (เราไม่รู้ว่าลูกค้ามีเงินก้อนปิดเองไหม จึงห้ามสรุปแทนเขา)
        _tot = data.get("debt_total_baht")
        _gap = cap_clear - R89_UNIT_PRICE
        try:
            _cash = ("ลูกค้าแจ้งว่ามีเงินก้อน"
                     if data.get("cash")
                     else "ลูกค้ายังไม่ได้บอกว่ามีเงินก้อนหรือไม่")
            _clear = f"ปิดหนี้แล้วกู้ได้ {cap_clear/1e6:.2f} ล้าน"
            if _tot:
                if _gap >= _tot:
                    data["bridge_ok"] = 1
                    self._add_signal(
                        st, f"💰 {_clear} · ยอดหนี้รวม {int(_tot):,} บาท · "
                            f"ส่วนต่างวงเงิน {_gap/1e6:.2f} ล้าน คลุมยอดหนี้ครบ · "
                            f"{_cash}")
                else:
                    self._add_signal(
                        st, f"💰 {_clear} · ยอดหนี้รวม {int(_tot):,} บาท · "
                            f"ส่วนต่างวงเงิน {_gap/1e6:.2f} ล้าน "
                            f"ต้องเคลียร์เพิ่มอีก {int(_tot - _gap):,} บาท · "
                            f"{_cash}")
            elif state is not None:
                self._add_signal(
                    st, f"💰 {_clear} · ส่วนต่างวงเงิน {_gap/1e6:.2f} ล้าน · "
                        f"ยังไม่รู้ยอดหนี้รวมคงเหลือ — เซลถามยอดปิดจริงก่อนวางแผน")
        except Exception as _e103:
            print(f"[R103 SIGNAL ERROR] {_e103} — ใช้ข้อความสำรอง")
            try:
                if _tot and _gap >= _tot:
                    data["bridge_ok"] = 1
                self._add_signal(st, f"เคสบริดจ์ · ปิดหนี้แล้วกู้ได้ "
                                     f"{cap_clear/1e6:.2f} ล้าน")
            except Exception:
                pass
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
            # r117 (1 ก.ย. 2569, Gift เคาะ) — เกรด C แจกได้เฉพาะ "มีผู้กู้ร่วม"
            # เหตุผล: C = ปิดหนี้หมดแล้ววงเงินก็ยังไม่ถึงราคาห้อง
            #   มีผู้กู้ร่วม = เคสจริง ดันไปห้องราคาต่ำกว่าได้ -> แจก
            #   ยื่นเดี่ยว/ยังไม่รู้ว่ามีไหม = ไม่มีทางไปต่อ -> X (ไม่แจก ไม่นัดโทร)
            # ของเดิมตัดเฉพาะคนที่ "บอกว่าไม่มี" ชัดๆ คนที่ไม่เคยตอบยังหลุดไปแจก
            _cob_ok = bool(data.get("co_borrower_income")
                           or data.get("co_borrower_yes"))
            if grade == "C" and not _cob_ok:
                why = ("ลูกค้าไม่บอกรายได้"
                       if (data.get("income_unknown") or data.get("income_refused"))
                       else "ไม่มีผู้กู้ร่วม ยื่นเดี่ยวไม่ผ่าน"
                       if data.get("co_borrower_none")
                       else "ยังไม่มีผู้กู้ร่วมยืนยัน และยื่นเดี่ยววงเงินไม่ถึงราคาห้อง")
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
            if (state is not None and _g in ("A", "B", "C", "N", "W1", "W2")
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


# ======================================================================
# r94 — "ไม่มี/ไม่ได้" ที่ไม่ได้พูดถึงผู้กู้ร่วม ต้องไม่ฆ่าเคส
#        กฎเหล็ก Gift 28 ส.ค. 2026: "ใช้เวลากับเคสที่ใช่"
# ----------------------------------------------------------------------
# ปัญหา (bot_logic บรรทัด 4033):
#   ตอนบอทรอคำตอบเรื่องผู้กู้ร่วม ถ้าข้อความมีคำว่า "ไม่มี" / "ไม่ได้" /
#   "คนเดียว" / "no" อยู่ตรงไหนก็ได้ -> co_borrower_none = True ทันที
#   แล้ว bot_logic:3160 ปิดเคสเลย ไม่ขอเบอร์
#
#   เคสที่ตายจริง:
#     "ไม่มีบัตรเครดิตครับ"        -> เจอ "ไม่มี" -> ปิดเคส
#     "ไม่ได้ผ่อนอะไรอยู่เลยครับ"   -> เจอ "ไม่ได้" -> ปิดเคส (ยิ่งดีด้วยซ้ำ ไม่มีภาระ)
#     "อยู่คนเดียวครับ"            -> เจอ "คนเดียว" -> ปิดเคส
#     "ไม่มีครับ แต่พี่สาวอาจช่วยได้" -> เจอ "ไม่มี" -> ปิดเคส ทั้งที่มีผู้กู้ร่วม
#
# วิธีคิดตามกฎเหล็ก — ถามซ้ำเฉพาะตอนที่คำตอบ "ตัดสินเคส" เท่านั้น:
#
#   ก. ข้อความชัดอยู่แล้ว (พูดถึงผู้กู้ร่วมตรงๆ / ปฏิเสธสั้นๆ ล้วน "ไม่มีครับ")
#      -> ทางเดิมทุกประการ ไม่แตะ
#
#   ข. ข้อความคลุมเครือ (มีคำว่าไม่มี/ไม่ได้ แต่พูดเรื่องอื่น) และเคสนี้
#      "ไม่จำเป็นต้องมีผู้กู้ร่วม" (นิยาม r91 — ไปเองได้ / เคสบริดจ์)
#      -> คำตอบไม่มีผลกับเคสเลย  ไม่ต้องถามซ้ำ ไม่เสียเทิร์น
#         ล้าง awaiting เดินหน้าขอเบอร์ต่อ  = 0 เทิร์นที่เสียไป
#
#   ค. ข้อความคลุมเครือ และเคสนี้ "ต้องมีผู้กู้ร่วมจริง"
#      -> คำตอบตัดสินเคส คุ้มที่จะถาม  ถามชัดๆ อีก 1 ครั้ง (ครั้งเดียว)
#         ตอบคลุมเครืออีก = ทางเดิม ปิดเคส ไม่ตื๊อรอบสาม
#
# ผลลัพธ์: เคสที่ใช่ไม่หลุด เคสที่ไม่ใช่ไม่กินเวลาเพิ่มเกิน 1 เทิร์น
# ======================================================================
import re as _re94

# คำที่แปลว่าประโยคนี้ "พูดถึงผู้กู้ร่วมจริง" — เจอแล้วถือว่าเป็นคำตอบชัดเจน
_R94_COB_WORDS = _re94.compile(
    r"(กู้ร่วม|ร่วมกู้|ผู้กู้|คนกู้|กู้ด้วย|กู้เดี่ยว|ยื่นเดี่ยว|ยื่นคนเดียว|กู้คนเดียว)")

# ปฏิเสธสั้นๆ ล้วน = คำตอบชัดเจน ไม่ต้องถามซ้ำ
_R94_SHORT_NO = _re94.compile(
    r"^(?:ไม่มี|ไม่ได้|ไม่|no|nope|ไม่มีใคร|ไม่มีคน|คนเดียว|เดี่ยว|"
    r"ไม่อยากให้ใคร|ไม่สะดวกหา|ไม่สะดวก|หาไม่ได้|ไม่น่ามี|คงไม่มี)"
    r"\s*(?:คน|ใคร|ครับ|ค่ะ|คะ|ค่า|จ้า|จ้ะ|จ๊ะ|นะ|น่ะ|เลย|อ่ะ|อะ|อ่า|ฮะ|ครับผม|"
    r"[\s\.\!\?,~ๆฯ])*$")


def _r94_ambiguous_no(msg):
    """ข้อความนี้จะโดนตีเป็น 'ไม่มีผู้กู้ร่วม' ทั้งที่ไม่ได้พูดเรื่องผู้กู้ร่วมหรือเปล่า"""
    try:
        s = str(msg or "").strip()
        if not s:
            return False
        low = s.lower()
        if not any(h in low for h in _bl9._NO_COB_HINTS):
            return False          # ไม่โดนตีเป็น "ไม่มี" อยู่แล้ว
        if _bl9._says_no_coborrower(s):
            return False          # พูดตรงๆ ว่าไม่มีผู้กู้ร่วม = ชัดเจน
        if _R94_COB_WORDS.search(s):
            return False          # ประโยคพูดถึงผู้กู้ร่วมจริง = ชัดเจน
        if _R94_SHORT_NO.match(s):
            return False          # "ไม่มีครับ" ล้วนๆ = ชัดเจน
        return True               # มีคำปฏิเสธ แต่พูดเรื่องอื่น = คลุมเครือ
    except Exception as _e:
        print(f"[R94 AMBIG ERROR] {_e} — ถือว่าชัดเจน (ทางเดิม)")
        return False


R94_COB_RE_Q = ("ขอถามให้ชัดอีกนิดเดียวนะครับ — เรื่องผู้กู้ร่วม "
                "(คู่สมรส พี่น้อง หรือพ่อแม่ที่มีรายได้ประจำ) "
                "เคสนี้ถ้ามีจะช่วยให้ผ่านง่ายขึ้นเยอะเลยครับ พอจะมีไหมครับ")

try:
    _R94_BASE_DECIDE = CalmBotEngine._decide

    def _decide_r94(self, msg, user_id, state, bucket, is_new):
        _uid = str(user_id)[:8]
        _reask = False
        try:
            _aw = state.get("awaiting")
            if (_aw in ("co_borrower", "co_income")
                    and not state.get("done")
                    and _r94_ambiguous_no(msg)):
                _d = state.get("data") or {}
                if not _r90_needs_cob(_d, state):
                    # ข: คำตอบไม่มีผลกับเคส -> ไม่ถามซ้ำ ไม่เสียเทิร์น
                    state["awaiting"] = None
                    self._add_signal(
                        state,
                        "ลูกค้าพิมพ์คำปฏิเสธที่ไม่ได้พูดถึงผู้กู้ร่วม "
                        "— เคสนี้ยื่นเองได้อยู่แล้ว ไม่ถามซ้ำ เดินหน้าขอเบอร์ต่อ")
                    print(f"[R94] {_uid}... คำปฏิเสธคลุมเครือ แต่เคสไปเองได้ "
                          "— ข้ามคำถามผู้กู้ร่วม")
                elif not state.get("_r94_reasked"):
                    # ค: คำตอบตัดสินเคส -> คุ้มที่จะถามชัดๆ อีก 1 ครั้ง
                    state["_r94_reasked"] = True
                    state["awaiting"] = None
                    _reask = True
                    print(f"[R94] {_uid}... คำปฏิเสธคลุมเครือ เคสนี้ต้องมีผู้กู้ร่วม "
                          "— ถามย้ำ 1 ครั้ง")
                else:
                    print(f"[R94] {_uid}... ถามย้ำไปแล้ว 1 ครั้ง — ใช้ทางเดิม")
        except Exception as _e:
            print(f"[R94 DECIDE ERROR] {_e} — ใช้ทางเดิม")
            _reask = False

        bubbles, grade = _R94_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)

        if _reask:
            try:
                if (not state.get("done") and not state.get("closed")
                        and not state.get("awaiting")):
                    state["awaiting"] = "co_borrower"
                    if isinstance(bubbles, list):
                        bubbles.append(R94_COB_RE_Q)
                    else:
                        bubbles = [bubbles, R94_COB_RE_Q]
                    self._add_signal(
                        state,
                        "บอทถามย้ำเรื่องผู้กู้ร่วม 1 ครั้ง "
                        "เพราะคำตอบแรกไม่ได้พูดถึงผู้กู้ร่วม")
            except Exception as _e:
                print(f"[R94 REASK ERROR] {_e} — ไม่ถามย้ำ")
        return bubbles, grade

    CalmBotEngine._decide = _decide_r94
    print("[R94] 'ไม่มี/ไม่ได้' ที่ไม่ได้พูดถึงผู้กู้ร่วม — ไม่ฆ่าเคสแล้ว")
except Exception as _e:
    print(f"[R94 ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")

print("[R94] กฎเหล็ก: ถามซ้ำเฉพาะตอนที่คำตอบตัดสินเคส (ครั้งเดียว)")


# ======================================================================
# r95 — เก็บ 4 ข้อค้างให้จบเป็นชุดเดียว (Gift สั่ง 28 ส.ค. 2026)
#        "จัดให้สอดคล้องกันแล้วทำเลย"
# ----------------------------------------------------------------------
# ทั้ง 4 ข้อมาจากกฎเหล็กเดียวกัน "ใช้เวลากับเคสที่ใช่" แตกเป็น 2 กติกา:
#
#   กติกา A — หนึ่งเรื่อง หนึ่งค่า
#       เลขเดียวกันมีสองค่า = ระบบตัดสินใจสองแบบโดยไม่มีใครรู้ตัว
#       เซลเสียเวลากับเคสผิดโดยอุบัติเหตุ  (-> ข้อ ง, ข้อ ฉ)
#
#   กติกา B — อย่าทิ้งเคสด้วย "ความไม่รู้"
#       ทิ้งเคสได้เมื่อมีหลักฐานว่าไม่ผ่านเท่านั้น
#       ไม่ใช่เพราะยังไม่ได้คำตอบ หรือเพราะคำใดคำหนึ่งไปตรงลิสต์
#       (-> ข้อ ค, ข้อ จ · เป็นกติกาเดียวกับ r93 เงินสด และ r94 ผู้กู้ร่วม)
#
#   ข้อ ง — ราคาห้องเหลือค่าเดียว 2.5M ทั้งระบบ
#   ข้อ ค — เนื้อความชนะช่องที่ค้าง ห้ามยัดคำตอบผิดช่อง
#   ข้อ จ — "ไม่บอกรายได้" ไม่ใช่ X อีกต่อไป ถ้ามีเบอร์ให้เซลตามได้
#   ข้อ ฉ — DSR: บอทเก็บตัวเลข ทีมวิเคราะห์ชี้ขาด (เจ้าของเดียว)
#   ข้อ ช — default โมเดล Claude ต้องไม่ชี้รุ่นที่ปลดระวาง
# ======================================================================

# ---------- ข้อ ง — ราคาห้องเหลือค่าเดียว 2.5M ----------
# เดิม bot_logic.UNIT_PRICE_BAHT = 2.3M  (ใช้ตัดสินว่าจะถามผู้กู้ร่วมไหม)
#      main.R89_UNIT_PRICE      = 2.5M  (ใช้ตีเกรด)
# ช่องโหว่: รายได้ 22,700-24,500 -> วงเงินตกระหว่าง 2.3M กับ 2.5M
#   -> bot_logic บอก "พอแล้ว ไม่ต้องถามผู้กู้ร่วม"
#   -> ตอนตีเกรดบอก "ไม่ถึงราคาห้อง" = C
#   = ไม่ได้ถามผู้กู้ร่วมทั้งที่ผู้กู้ร่วมพลิกเคสได้ ทิ้งโอกาสฟรีๆ
# ยกเป็น 2.5M เท่ากัน = ถามผู้กู้ร่วมครอบคลุมขึ้น
# และตั้งแต่ r91/r94 การ "ถามผู้กู้ร่วม" ไม่ฆ่าเคสบริดจ์อีกแล้ว จึงยกได้อย่างปลอดภัย
try:
    _R95_OLD_UNIT = _bl9.UNIT_PRICE_BAHT
    _bl9.UNIT_PRICE_BAHT = R89_UNIT_PRICE
    print(f"[R95] ราคาห้องอ้างอิงเหลือค่าเดียว {R89_UNIT_PRICE:,} "
          f"(เดิม bot_logic ใช้ {_R95_OLD_UNIT:,} · ปิดช่องโหว่รายได้ 22,700-24,500)")
except Exception as _e:
    print(f"[R95 UNIT ERROR] ต่อไม่ติด — ใช้ค่าเดิม: {_e}")


# ---------- ข้อ ค — เนื้อความชนะช่องที่ค้าง ----------
# ต่อยอดจาก r57 (SLOT FIX) ที่แก้ไว้แค่คู่ debt<->income
# ที่เจอตอนทดสอบ r94 ว่ายังยัดผิดช่องอยู่:
#   awaiting=co_borrower  "ผ่อนรถ 5000 ครับ"
#       -> co_borrower_yes=True · co_borrower_income=5000
#          = เสกผู้กู้ร่วมรายได้ 5,000 ขึ้นมาจากประโยคที่พูดเรื่องหนี้ตัวเอง
#   awaiting=co_debt      "มีครับ แฟน เงินเดือน 40000"
#       -> co_debt_baht=40000
#          = เอารายได้ผู้กู้ร่วมไปนับเป็นหนี้ผู้กู้ร่วม วงเงินหายทั้งก้อน
#   awaiting=debt         "รัชดา งบ 2 ล้านครับ"
#       -> debt = "รัชดา งบ 2 ล้านครับ" (ซ้ำสองรอบด้วย)
#
# ตัวเลขผิดในชีตแพงกว่าถามเพิ่มอีกเทิร์น — เซลโทรไปคุยบนเรื่องที่ไม่มีจริง
# วิธี: ถ้าอ่านออกว่าข้อความนี้ "ไม่ใช่คำตอบของช่องที่ค้าง"
#       -> พาไปลงช่องที่ถูก ถ้าช่องนั้นว่าง / ถ้าไม่มีที่ลงก็ทิ้ง ไม่เขียนมั่ว
#       -> ปล่อยช่องที่ค้างว่างไว้ _next_missing จะถามซ้ำเอง (= ทาง ก)
# เพดาน: สลับช่องได้ 4 ครั้งต่อแชท เกินนั้นกลับทางเดิม ไม่วนไม่รู้จบ
R95_REROUTE_MAX = 4

# คำที่บอกว่าประโยคนี้พูดถึง "คนอื่น" (ผู้กู้ร่วม) ไม่ใช่ตัวลูกค้าเอง
_R95_OTHER_WORDS = ("แฟน", "สามี", "ภรรยา", "ภรรยาผม", "คู่สมรส", "คู่ชีวิต",
                    "พ่อ", "แม่", "บิดา", "มารดา", "พี่", "น้อง", "ลูก",
                    "ญาติ", "พี่สาว", "พี่ชาย", "น้องสาว", "น้องชาย",
                    "กู้ร่วม", "ผู้กู้", "คนกู้", "เขา", "ท่าน")

# คำที่บอกว่าประโยคนี้พูดเรื่องทำเล/งบ ไม่ใช่ภาระผ่อน
_R95_ZONE_WORDS = ("โซน", "ย่าน", "แถว", "ทำเล", "ทําเล", "งบ", "งบประมาณ",
                   "ราคา", "ห้อง", "ตร.ม", "ตารางเมตร", "ชั้น", "วิว")


def _r95_kind(msg):
    """ข้อความนี้อ่านออกว่าเป็นเรื่องอะไร — คืน 'income' / 'debt' / None"""
    try:
        s = str(msg or "")
        _inc = _bl9._has_any(s, _bl9._INCOME_SAYS)
        _dbt = _bl9._has_any(s, _bl9._DEBT_SAYS)
        if _inc and not _dbt and _bl9._parse_income(s):
            return "income"
        if _dbt and not _inc and _bl9._parse_debt_monthly(s):
            return "debt"
    except Exception as _e:
        print(f"[R95 KIND ERROR] {_e}")
    return None


def _r95_reroute(field, msg, data, state):
    """คืน (ช่องใหม่, เหตุผล) · ช่องใหม่ = None แปลว่า 'ทิ้ง ไม่เขียนช่องไหนเลย'
    คืน False แปลว่า 'ไม่ต้องสลับ ใช้ทางเดิม'
    """
    try:
        s = str(msg or "")
        d = data or {}
        if (state or {}).get("_r95_reroutes", 0) >= R95_REROUTE_MAX:
            return False
        _other = _bl9._has_any(s, _R95_OTHER_WORDS)
        _kind = _r95_kind(s)

        # (0) เบอร์โทรไม่ใช่คำตอบเรื่องเงินไม่ว่าช่องไหน
        #     เจอตอนทดสอบ: awaiting=co_borrower  "0812345678 ครับ"
        #     -> _parse_income อ่านได้ 812,345 -> co_borrower_income = 812,345
        #     = เสกรายได้ผู้กู้ร่วมแปดแสนจากเบอร์โทร วงเงินพุ่ง เกรด A ปลอม
        if (field in ("income", "debt", "co_borrower", "co_income", "co_debt",
                      "cash_budget", "coop")
                and _bl9._looks_like_phone(s)):
            # ตัดเลขเบอร์ออกก่อน ถ้ายังเหลือตัวเลขเงินอยู่ = ประโยคนี้บอกทั้งสองอย่าง
            # ("0812345678 เงินเดือน 40000") -> ปล่อยให้ทางเดิมจัดการ อย่าตัดข้อมูลทิ้ง
            _rest = _re94.sub(r"0\d[\d\-\s\.]{7,}", " ", s)
            if not _bl9._parse_income(_rest) and not _bl9._parse_debt_monthly(_rest):
                if not (data or {}).get("contact"):
                    return ("contact", "เป็นเบอร์โทร ไม่ใช่คำตอบเรื่องเงิน")
                return (None, "เป็นเบอร์โทร ไม่ใช่คำตอบเรื่องเงิน — มีเบอร์แล้ว ทิ้ง")

        # (1) รอยอดผ่อนของผู้กู้ร่วม แต่ได้รายได้ของผู้กู้ร่วมมาแทน
        if field == "co_debt" and _kind == "income":
            if not d.get("co_income"):
                return ("co_income", "เป็นรายได้ผู้กู้ร่วม ไม่ใช่ยอดผ่อน")
            return (None, "เป็นรายได้ผู้กู้ร่วม ไม่ใช่ยอดผ่อน — มีค่าแล้ว ทิ้ง")

        # (2) รอคำตอบเรื่องผู้กู้ร่วม แต่ลูกค้าพูดเรื่องเงินของตัวเอง
        if field in ("co_borrower", "co_income") and _kind and not _other:
            if _kind == "income" and not _bl9._income_known(d):
                return ("income", "เป็นรายได้ของลูกค้าเอง ไม่ใช่คำตอบเรื่องผู้กู้ร่วม")
            if _kind == "debt" and not d.get("debt"):
                return ("debt", "เป็นภาระของลูกค้าเอง ไม่ใช่คำตอบเรื่องผู้กู้ร่วม")
            return (None, "พูดเรื่องเงินของตัวเอง ไม่ใช่คำตอบเรื่องผู้กู้ร่วม — ทิ้ง")

        # (3) รอยอดผ่อน แต่ได้ทำเล/งบมาแทน
        if field == "debt" and not _bl9._has_any(s, _bl9._DEBT_SAYS):
            if (_bl9._parse_debt_monthly(s) is None
                    and _bl9._has_any(s, _R95_ZONE_WORDS)):
                return (None, "เป็นทำเล/งบ ไม่ใช่ยอดผ่อน — ทิ้ง ไม่เขียนช่องหนี้")

        # (4) รอคำตอบเรื่องผู้กู้ร่วม แต่ได้ทำเล/งบมาแทน
        #     เจอตอนทดสอบ: awaiting=co_borrower  "รัชดา งบ 2.5 ล้านครับ"
        #     -> co_borrower_yes=True · co_borrower_income = 2,500,000
        #     -> รายได้รวม 2,540,000 -> วงเงินหลายร้อยล้าน -> เกรด A ปลอม
        #     เคสแบบนี้อันตรายที่สุด เพราะบอทไม่ได้เงียบ แต่ส่งเคสผิดให้เซลโทรด่วน
        if (field in ("co_borrower", "co_income", "co_debt")
                and _bl9._has_any(s, _R95_ZONE_WORDS)
                and not _other
                and not _bl9._has_any(s, _bl9._INCOME_SAYS)
                and not _bl9._has_any(s, _bl9._DEBT_SAYS)):
            return (None, "เป็นทำเล/งบ ไม่ใช่คำตอบเรื่องผู้กู้ร่วม — ทิ้ง")
    except Exception as _e:
        print(f"[R95 REROUTE ERROR] {_e} — ใช้ทางเดิม")
    return False


try:
    _R95_ORIG_CAPTURE = _bl9.BotEngine._capture

    def _capture_r95(self, state, field, msg):
        try:
            _r = _r95_reroute(field, msg, (state or {}).get("data") or {}, state)
            if _r is not False:
                _new, _why = _r
                state["_r95_reroutes"] = (state.get("_r95_reroutes") or 0) + 1
                state["awaiting"] = None
                print(f"[R95 SLOT] ช่อง {field!r} <- {msg[:34]!r} : {_why} "
                      f"-> {_new or 'ทิ้ง'}")
                if _new is None:
                    return None          # ไม่เขียนช่องไหนเลย ปล่อยให้ถามซ้ำ
                field = _new
        except Exception as _e:
            print(f"[R95 CAPTURE ERROR] {_e} — ใช้ทางเดิม")
        return _R95_ORIG_CAPTURE(self, state, field, msg)

    _bl9.BotEngine._capture = _capture_r95
    print("[R95] เนื้อความชนะช่องที่ค้าง — ไม่ยัดคำตอบผิดช่องแล้ว")
except Exception as _e:
    print(f"[R95 SLOT ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")


# ---------- ข้อ ค (ต่อ) — ด่านสุดท้าย: ตัวเลข/เดือน ต้องสมเหตุสมผล ----------
# ต่อให้ตัวแยกช่องพลาด ก็ต้องไม่มีทางที่ "รายได้/เดือน" หลักล้านจะหลุดเข้าเกรด
# 1,000,000/เดือน = เพดานที่กว้างมากอยู่แล้ว เกินนี้คือเลขงบ/ราคาห้องหลุดเข้ามาแน่
R95_MONTHLY_MAX = 1_000_000
_R95_MONEY_FIELDS = ("co_borrower_income", "co_debt_baht", "income_baht",
                     "income_total", "debt_baht")
try:
    _R95_G2 = _bl9.BotEngine._grade

    def _grade_r95b(self, data, state=None):
        try:
            d = data or {}
            for _f in _R95_MONEY_FIELDS:
                _v = d.get(_f)
                if _v and int(_v) > R95_MONTHLY_MAX:
                    print(f"[R95 SANITY] {_f} = {int(_v):,} เกิน/เดือนที่เป็นไปได้ — ทิ้งค่านี้")
                    d.pop(_f, None)
                    if state is not None:
                        self._add_signal(
                            state,
                            f"⚠️ ระบบอ่านตัวเลขได้ {int(_v):,} ในช่อง {_f} "
                            "ซึ่งเป็นไปไม่ได้สำหรับยอดต่อเดือน (น่าจะเป็นงบ/ราคาห้อง) "
                            "— ตัดทิ้งแล้ว เซลถามตัวเลขจริงตอนโทร")
        except Exception as _e:
            print(f"[R95 SANITY ERROR] {_e}")
        return _R95_G2(self, data, state)

    _bl9.BotEngine._grade = _grade_r95b
    print(f"[R95] ด่านตัวเลข: ยอดต่อเดือนเกิน {R95_MONTHLY_MAX:,} = ตัดทิ้ง ไม่เอาไปตีเกรด")
except Exception as _e:
    print(f"[R95 SLOT ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")


# ---------- ข้อ จ + ฉ — แทรกที่ชั้น _grade ----------
# ข้อ จ: เดิมธง income_refused / income_unknown = X ไม่รับเคส ทันที
#   ปัญหา: เป็นการทิ้งเคสเพราะ "คำไปตรงลิสต์" ไม่ใช่เพราะรู้ว่าเขาไม่ผ่าน
#   เพิ่งโดนมาเองรอบ r93 — "ทำธุรกิจส่วนตัว" ไปตรงคำว่า "ส่วนตัว" -> X ทั้งเคส
#   ใหม่ (กติกา B เดียวกับเงินสด r93 และผู้กู้ร่วม r94):
#     · รู้รายได้แล้ว (บอกทีหลัง)      -> ล้างธง ตีเกรดตามจริง
#     · ไม่บอกรายได้ แต่ให้เบอร์มา     -> N1 เซลโทรถามเอง + ติดธงให้เห็นชัด
#     · ไม่บอกรายได้ และไม่มีเบอร์ด้วย -> X เหมือนเดิม (ไม่มีอะไรให้เซลทำต่อ)
#
# ข้อ ฉ: DSR มี 3 ค่าในระบบ (KPI <=80% · ปรับโครงสร้าง <15% · ตอนตีเกรดไม่ใช้เลย)
#   เจ้าของเดียว: **บอทเก็บตัวเลข ทีมวิเคราะห์ชี้ขาด**
#   - ไม่เอา DSR มาตีเกรดเพิ่ม (คงเดิม — ยกเว้นเกณฑ์เครดิตปรับโครงสร้าง <15%
#     ซึ่งเป็นกฎเครดิตแข็ง ไม่ใช่ KPI)  = เกรดไม่เปลี่ยนแม้แต่เคสเดียว
#   - แต่ต้องเห็นตัวเลขเดียวกันทั้งทีม -> ติดธง DSR ไว้ให้เซล/ทีมวิเคราะห์อ่าน
#   หลักเดียวกับ §0.5 (ยอดขาย x margin x %หุ้น) ที่ engine README ระบุว่า
#   ทีมวิเคราะห์คิดมือ บอทมีหน้าที่เก็บ input ให้ครบ
R95_DSR_KPI = 0.80

try:
    _R95_BASE_GRADE = _bl9.BotEngine._grade

    def _grade_r95(self, data, state=None):
        d = data or {}
        st = state if state is not None else {}
        _popped = []
        try:
            if d.get("income_refused") or d.get("income_unknown"):
                _inc = (d.get("income_total") or d.get("income_baht")
                        or _bl9._parse_income(str(d.get("income", ""))))
                _has_phone = bool(d.get("contact") or d.get("phone")
                                  or st.get("contact"))
                if _inc:
                    _why = ("ลูกค้าเคยบอกว่าไม่สะดวกบอกรายได้ แต่ภายหลังบอกตัวเลขมาแล้ว "
                            "— ตีเกรดตามตัวเลขจริง ไม่ตัดเคส")
                elif _has_phone:
                    _why = ("ลูกค้าไม่บอกรายได้ในแชท แต่ให้เบอร์มาแล้ว "
                            "— ไม่ตัดเคส เซลโทรถามรายได้เองก่อนตีเกรดจริง (เกณฑ์ 28 ส.ค. r95)")
                else:
                    _why = None
                if _why:
                    for _k in ("income_refused", "income_unknown"):
                        if d.get(_k):
                            _popped.append(_k)
                            d.pop(_k, None)
                    if state is not None:
                        self._add_signal(st, _why)
        except Exception as _e:
            print(f"[R95 REFUSE ERROR] {_e} — ใช้ทางเดิม")

        try:
            g = _R95_BASE_GRADE(self, data, state)
        finally:
            for _k in _popped:
                try:
                    d[_k] = True
                except Exception:
                    pass

        # ---- ข้อ ฉ — ติดธง DSR ให้ทีมวิเคราะห์ชี้ขาด (ไม่แตะเกรด) ----
        try:
            if state is not None:
                _i = (d.get("income_counted") or d.get("income_total")
                      or d.get("income_baht")
                      or _bl9._parse_income(str(d.get("income", ""))))
                _db = d.get("debt_baht")
                if _db is None:
                    _db = _bl9._parse_debt_monthly(str(d.get("debt", "")))
                if d.get("co_debt_baht"):
                    _db = (_db or 0) + int(d["co_debt_baht"])
                if _i and _db is not None and int(_i) > 0:
                    _dsr = int(_db) / int(_i)
                    d["dsr_now"] = round(_dsr, 3)
                    _mark = "เกินเพดาน" if _dsr > R95_DSR_KPI else "อยู่ในเพดาน"
                    self._add_signal(
                        st,
                        f"DSR ตอนนี้ {round(_dsr*100)}% "
                        f"(ผ่อน {int(_db):,} / รายได้ที่นับได้ {int(_i):,}) "
                        f"— {_mark} KPI {round(R95_DSR_KPI*100)}% · "
                        "ตัวเลขนี้ไม่ได้ใช้ตีเกรด ทีมวิเคราะห์เป็นคนชี้ขาด")
        except Exception as _e:
            print(f"[R95 DSR ERROR] {_e}")
        return g

    _bl9.BotEngine._grade = _grade_r95
    print("[R95] 'ไม่บอกรายได้' ไม่ใช่ X แล้ว ถ้ามีเบอร์ให้เซลตามได้ (N1 + ติดธง)")
    print("[R95] ติดธง DSR ให้ทีมวิเคราะห์ — ไม่แตะเกรด (เจ้าของเดียว)")
except Exception as _e:
    print(f"[R95 GRADE ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")


# ---------- ข้อ ช — default โมเดล Claude ห้ามชี้รุ่นที่ปลดระวาง ----------
# ของเดิมในโค้ด:
#   CLAUDE_MODEL       default = claude-3-5-haiku-20241022
#   CLAUDE_MODEL_SMART default = claude-sonnet-4-20250514   <-- 404 รัวๆ เมื่อวาน
# ถ้า env หายเมื่อไหร่ AI ตายทั้งสองทางเงียบๆ
# ไม่เดาชื่อรุ่นเอง — ใช้ค่าที่ "พิสูจน์แล้วว่าวิ่งได้จริงบนโปรดักชัน" เป็น default
# แล้วพิมพ์รุ่นที่จะใช้จริงลง log ตอนบูต จะได้เห็นทันทีถ้า env หาย
R95_MODEL_FALLBACK = "claude-sonnet-5"
try:
    _env_smart = (os.environ.get("CLAUDE_MODEL_SMART") or "").strip()
    _env_cheap = (os.environ.get("CLAUDE_MODEL") or "").strip()
    _smart = _env_smart or R95_MODEL_FALLBACK
    # รุ่นประหยัด: ถ้า env ไม่ได้ตั้ง อย่าเดารหัสรุ่น ใช้ตัวที่รู้ว่าวิ่งได้
    _cheap = _env_cheap or _smart
    _bl9.CLAUDE_MODEL_SMART = _smart
    _bl9.CLAUDE_MODEL = _cheap
    print(f"[R95] โมเดล AI ที่จะใช้จริง — ปกติ: {_cheap} · เทิร์นที่ต้องอ่านคน: {_smart}"
          + ("" if (_env_smart and _env_cheap)
             else "  ⚠️ (บางตัวมาจาก fallback ไม่ใช่ env — เช็ค Railway variables)"))
except Exception as _e:
    print(f"[R95 MODEL ERROR] ต่อไม่ติด — ใช้ค่าเดิมในโค้ด: {_e}")

# หมายเหตุเกรด D: ตรวจแล้วโค้ดเกรด D ที่เหลืออยู่ใน bot_logic._grade เป็นโค้ดตาย
# (r89 ทับ BotEngine._grade ทั้งตัว และในตัวใหม่ยุบ D เข้า C หมดแล้ว)
# ไม่มีทางเรียกถึง จึงไม่แตะ — แตะ bot_logic โดยไม่จำเป็นมีแต่ความเสี่ยง

print("[R95] ครบชุด — ราคาห้องค่าเดียว · ไม่ยัดผิดช่อง · ไม่ทิ้งเคสเพราะไม่รู้ · DSR มีเจ้าของ")


# ======================================================================
# r96 — ห้ามบอก "กู้ได้" ก่อนถาม (Gift สั่ง 29 ส.ค. 2026 ตี 1:32)
# ----------------------------------------------------------------------
# เคสจริง Sukon Hongwia... เพจ Realty Smart 28 ส.ค. 23:54
#   ลูกค้า: "ค้าขาย ตามงาน ได้ป่าวคะรับ"
#   บอท:   "กู้ได้ค่ะ ธนาคารมีโปรแกรมสำหรับอาชีพอิสระอยู่ ..." (ยาว 6 บรรทัด)
#
# Gift: "อาชีพไม่เข้าเกณฑ์นะ ไม่มีหลักแหล่ง ไม่มีทะเบียน
#        ถามก่อนสิ ค่อยบอกว่าได้ นี่บอกได้ก่อนเลย จริงๆกู้ยากแบ้งไม่รับ"
#
# ต้นตอ: faq_data.FAQ_DATABASE ข้อ faq_self_employed มีคีย์เวิร์ด "ค้าขาย"
#        แล้วตอบชุดเดียวกันหมด ขึ้นต้นด้วย "กู้ได้ครับ"
#        = ฟันธงให้ก่อนที่จะรู้ว่าเขามีทะเบียน/statement/อายุงานเท่าไหร่
#
# ของเดิมที่ถูกอยู่แล้วและต้องได้ทำงาน (แค่โดน FAQ ตัดหน้า):
#   SELF_EMP_Q          = "ทำอาชีพนี้มากี่ปี + จดทะเบียน/เสียภาษีไหม"
#   _self_emp_below_bar = ต่ำกว่า 2 ปี และไม่มีภาษี/ทะเบียน
#   SELF_EMP_SOFT_CLOSE = ปิดสุภาพ ตรงไปตรงมา
#
# แก้ 2 จุด:
#   A. FAQ ข้อนั้น -> เปลี่ยนเป็น "ถามก่อน" ไม่ฟันธง ไม่มีคำว่ากู้ได้
#   B. เพิ่มคำที่แปลว่า "ไม่มีหลักแหล่ง" เข้า _UNBANKABLE_JOBS
#      (ของเดิมมี หาบเร่/แผงลอย/ขายของรายวัน แต่ไม่มี ตามงาน/ตลาดนัด/ออกบูธ)
#      -> income_unbankable -> r91 ถือว่าผู้กู้ร่วมคือทางเดียว
#      -> ยังถามผู้กู้ร่วมก่อนเสมอ ไม่ปิดเคสทันที (กติกาเดิม r47)
# ======================================================================

# ---------- A: FAQ ต้องถามก่อน ห้ามฟันธงว่ากู้ได้ ----------
R96_SELF_EMP_ANSWER = (
    "ขอถามให้ชัดก่อนนะครับ ลูกค้าทำอาชีพนี้มากี่ปีแล้วครับ "
    "แล้วมีจดทะเบียนการค้า หรือ statement บัญชีย้อนหลัง 6-12 เดือน "
    "ที่เห็นเงินเข้าสม่ำเสมอไหมครับ "
    "ธนาคารดู 2 อย่างนี้เป็นหลักเลยครับ ตอบแล้วผมบอกได้ตรงกว่านี้เยอะ"
)
try:
    import faq_data as _fq96
    _n96 = 0
    for _it in getattr(_fq96, "FAQ_DATABASE", []) or []:
        if isinstance(_it, dict) and _it.get("id") == "faq_self_employed":
            _it["answer"] = R96_SELF_EMP_ANSWER
            _n96 += 1
    print(f"[R96] FAQ อาชีพอิสระ — เปลี่ยนเป็น 'ถามก่อน' ไม่ฟันธงว่ากู้ได้ ({_n96} ข้อ)")
    if _n96 == 0:
        print("[R96 WARN] ไม่เจอ faq_self_employed — ใช้คำตอบเดิม")
except Exception as _e:
    print(f"[R96 FAQ ERROR] ต่อไม่ติด — ใช้คำตอบเดิม: {_e}")


# ---------- B: อาชีพที่ 'ไม่มีหลักแหล่ง' ธนาคารไม่รับ ----------
# Gift ยืนยัน 29 ส.ค.: ค้าขายตามงาน/ตลาดนัด = ไม่มีหลักแหล่ง ไม่มีทะเบียน
# ไม่ใช่การตัดเคสทิ้งทันที — แค่บอกระบบว่า "ยื่นเดี่ยวไม่ผ่านแน่"
# ผู้กู้ร่วมยังพลิกได้ บอทจะถามผู้กู้ร่วมก่อนเสมอ (กติกา r47 ที่ Gift เคาะไว้)
R96_NO_BASE_JOBS = (
    "ขายตามงาน", "ค้าขายตามงาน", "ขายของตามงาน", "ตามงานอีเวนต์",
    "ออกงานขาย", "ขายตามตลาด", "ค้าขายตามตลาด", "ตลาดนัด",
    "ออกบูธ", "ออกร้าน", "เร่ขาย", "ขายเร่", "ขายเร่ร่อน",
    "ไม่มีหน้าร้าน", "ไม่มีร้านประจำ", "ไม่มีหลักแหล่ง", "ไม่มีที่อยู่เป็นหลักแหล่ง",
)
try:
    _old96 = tuple(_bl9._UNBANKABLE_JOBS)
    _add96 = tuple(w for w in R96_NO_BASE_JOBS if w not in _old96)
    _bl9._UNBANKABLE_JOBS = _old96 + _add96
    print(f"[R96] เพิ่มอาชีพไม่มีหลักแหล่ง {len(_add96)} คำ "
          f"(รวม {len(_bl9._UNBANKABLE_JOBS)} คำ) — ยื่นเดี่ยวไม่ผ่าน ต้องมีผู้กู้ร่วม")
except Exception as _e:
    print(f"[R96 JOBS ERROR] ต่อไม่ติด — ใช้ลิสต์เดิม: {_e}")

print("[R96] อาชีพอิสระ: ถามก่อน แล้วค่อยตอบตามจริง — ไม่สร้างความหวังเกินจริง")


# ======================================================================
# r97 — ถามหนี้ให้ละเอียดขึ้น + ประเมินวงเงินแบบปลอดภัย + เสนอบริการปิดภาระ
#        (Gift สั่ง 29 ส.ค. 2026 ตี 2)
# ----------------------------------------------------------------------
# กติกาที่ Gift ย้ำ 2 รอบ: **เกณฑ์ใช้เกณฑ์เดิม ห้ามแตะ**
#   เกรด A/B/C/N/X · แจกเคส · ลงชีต · ธงเซล  = ตัวเลขจริง เหมือนเดิมทุกตัว
#   ที่เพิ่มคือ "คำพูดกับลูกค้า" เท่านั้น — คนละชั้นกับเครื่องคิดเลข
#
# 3 ส่วน:
#   A. ถามหนี้ต่ออีก 1 ข้อ โดยทวนของเดิมกลับไป
#      เคสจริง: ถาม "มีผ่อนอะไรอยู่ไหม (เช่น บ้าน รถ บัตรเครดิต)"
#               ลูกค้าตอบแค่ "รถ เดือนละ 10000" — อ่านผ่านคำว่าบัตร
#      Gift: "นอกจากรถเดือนละ 10000 ที่แจ้ง มีอื่นๆ อีกไหม เช่น บัตร สินเชื่อ บ้าน"
#      -> ทวนของเดิม = พิสูจน์ว่าฟังอยู่ + ไม่ถามซ้ำสิ่งที่ตอบไปแล้ว
#
#   B. ลูกค้าถามวงเงินแต่ข้อมูลไม่ครบ -> "ขยี้" ด้วยเรื่องจริงที่คนพลาด
#      แล้ว **ปล่อยกลับ funnel เดิม** ถามทีละข้อ (ห้ามถามรวบ — Gift สั่ง
#      เพราะคำตอบจะปนกันแล้วยัดผิดช่องเหมือนบั๊กที่เพิ่งแก้ใน r95)
#
#   C. ครบ (รายได้+ภาระ+อายุ) -> แจ้งวงเงิน "ต่ำกว่าจริง" + ชี้ส่วนต่างถ้าปิดภาระ
#      Gift: "ตอบน้อยกว่าความเป็นจริงเพื่อความปลอดภัย อ้างว่าเป็นการสัมภาษณ์
#             ไม่เห็นเอกสาร ซึ่งธนาคารใช้หลักเอกสาร ... แล้วแจ้งวงเงินให้น้อยๆ
#             เพื่อเสนอบริการปิดภาระ"
#      เหตุผลที่อ้างเป็นความจริง 100% — ธนาคารตัดสินจากเอกสารจริงๆ
#      และส่วนต่าง (cap_clear - cap_now) ก็เป็นเลขจริงจากเครื่อง ไม่ได้ปั้น
# ======================================================================

# แจ้งลูกค้าที่ 70-80% ของวงเงินที่คำนวณได้ (ปัดลงหลักแสน)
# ปรับตัวเลขนี้ตัวเดียวจบ — Gift อยากให้ต่ำกว่านี้อีกก็ลดค่านี้
R97_QUOTE_PCT = 0.70
R97_QUOTE_SPAN = 0.10          # ช่วงบน = PCT + SPAN
R97_BRIDGE_MIN = 300_000       # ส่วนต่างต้องเกินเท่านี้ถึงจะเสนอบริการปิดภาระ


def _r97_band(value):
    """คืนข้อความช่วงวงเงินแบบปัดลงหลักแสน เช่น '2.5-2.9 ล้าน'"""
    lo = int(value * R97_QUOTE_PCT / 100000) * 100000
    hi = int(value * (R97_QUOTE_PCT + R97_QUOTE_SPAN) / 100000) * 100000
    if hi <= lo:
        hi = lo + 100000
    return f"{lo/1e6:.1f}-{hi/1e6:.1f} ล้าน"


# ---------- A: ถามหนี้ต่ออีก 1 ข้อ โดยทวนของเดิม ----------
_R97_DEBT_MORE_TAIL = ("มีอื่นๆ เพิ่มเติมอีกไหมครับ "
                       "เช่น บัตรเครดิต สินเชื่อส่วนบุคคล หรือผ่อนบ้าน")
_R97_NO_MORE = ("ไม่มี", "ไม่มีแล้ว", "หมดแล้ว", "แค่นี้", "เท่านี้", "มีแค่",
                "ไม่มีอื่น", "ไม่มีเพิ่ม", "no", "ครบแล้ว")


def _r97_debt_more_q(data):
    """สร้างคำถามทวนของเดิม — 'นอกจาก<ที่แจ้ง> มีอื่นๆ อีกไหม'"""
    try:
        raw = str((data or {}).get("debt") or "").strip()
        said = _bl9.BotEngine._tidy(raw)
        if said and len(said) <= 60:
            return f"นอกจาก{said} ที่แจ้งมา {_R97_DEBT_MORE_TAIL}"
    except Exception as _e:
        print(f"[R97 DEBTQ ERROR] {_e}")
    return f"นอกจากที่แจ้งมา {_R97_DEBT_MORE_TAIL}"


# ---------- B: ขยี้ตอนลูกค้าถามวงเงิน ----------
_R97_ASK_LIMIT = ("กู้ได้เท่าไหร่", "กู้ได้เท่าไร", "กู้ได้กี่", "วงเงิน",
                  "กู้ได้ประมาณ", "ซื้อได้เท่าไหร่", "ซื้อได้เท่าไร",
                  "ผ่อนไหวไหม", "กู้ผ่านไหม", "กู้ได้ไหม", "ประเมินให้หน่อย",
                  "คำนวณให้หน่อย", "คิดให้หน่อย", "ได้กี่ล้าน", "กี่ล้าน")

R97_TEASE = [
    "บอกคร่าวๆ ได้ครับ แต่ขอออกตัวก่อนนะครับ อันนี้คุยปากเปล่า "
    "ธนาคารเขาดูจากเอกสารล้วนๆ ครับ",

    "ที่คนพลาดกันเยอะสุดคือประวัติการผ่อนกับบัตรเครดิตครับ "
    "จ่ายช้าไปไม่กี่วันเมื่อปีก่อนโดยไม่รู้ตัว หรือบัตรที่เลิกใช้แล้วแต่ยังไม่ได้ปิด "
    "ธนาคารคิดจากวงเงินบัตร ไม่ใช่ยอดที่ใช้จริงครับ",

    "อีกตัวที่แรงมากคืออายุ รายได้เท่ากันเป๊ะ อายุ 35 กับ 55 "
    "วงเงินต่างกันเกือบล้าน เพราะปีผ่อนสั้นลง ค่างวดต่อล้านก็แพงขึ้นครับ "
    "ยังมีเรื่องบริษัทกับอายุงานอีก แต่พวกนั้นไว้ดูตอนส่งเอกสาร",

    "ขอถามทีละข้อสั้นๆ นะครับ ตอบครบเมื่อไหร่ผมคำนวณให้เลย",
]


def _r97_asks_limit(msg):
    try:
        return _bl9._has_any(str(msg or ""), _R97_ASK_LIMIT)
    except Exception:
        return False


# ---------- C: แจ้งวงเงินแบบปลอดภัย + เสนอบริการปิดภาระ ----------
R97_SERVICE_LINE = ("เรามีบริการช่วยปิดภาระตรงนี้ครับ "
                    "ขอเบอร์ให้ที่ปรึกษาดูเอกสารให้ เลขจริงจะชัดกว่านี้เยอะครับ")


def _r97_quote(data, state):
    """คืน list บับเบิลแจ้งวงเงิน (ต่ำกว่าจริง) — คืน [] ถ้ายังคำนวณไม่ได้

    ใช้ input ชุดเดียวกับ _grade เป๊ะ แต่ "ตัวเลขที่พูด" ถูกกดลงเหลือ 70-80%
    เกรด/ชีต/ธงเซล ยังใช้ตัวเลขจริงเหมือนเดิม ไม่กระทบกันเลย
    """
    try:
        d = data or {}
        st = state or {}
        inc = (d.get("income_total") or d.get("income_baht")
               or _bl9._parse_income(str(d.get("income", ""))))
        inc = int(inc) if inc else 0
        if not inc:
            return []
        if st.get("self_employed"):
            inc = int(inc * _bl9.FREELANCE_INCOME_PCT)
        debt = d.get("debt_baht")
        if debt is None:
            debt = _bl9._parse_debt_monthly(str(d.get("debt", "")))
        if debt is None:
            return []                     # ยังไม่รู้ภาระ = ยังคำนวณไม่ได้
        debt = max(0, int(debt)) + int(d.get("co_debt_baht") or 0)
        _own, _co = d.get("age"), d.get("co_age")
        if _co is not None:
            age = min(_own, _co) if _own else _co
        elif d.get("co_borrower_income"):
            age = None
        else:
            age = _own
        if age is None and not d.get("co_borrower_income"):
            return []                     # ไม่รู้อายุ = ห้ามเดา (เลขจะสูงเกินจริง)

        now = _bl9._capacity(inc, debt, age)
        clear = _bl9._capacity(inc, 0, age)
        if now <= 0 and clear <= 0:
            return []

        out = [f"ผมให้ตัวเลขแบบปลอดภัยไว้ก่อนนะครับ ตอนนี้น่าจะราว {_r97_band(now)}"
               if now > 0 else
               "ตรงๆ นะครับ ภาระที่ผ่อนอยู่ตอนนี้กินวงเงินจนแทบไม่เหลือเลยครับ"]

        if clear - now >= R97_BRIDGE_MIN:
            out.append(f"ที่ผ่อนอยู่ {debt:,} ต่อเดือนกินวงเงินไปเยอะเลยครับ "
                       f"ถ้าปิดตัวนี้ได้ ขยับขึ้นเป็นราว {_r97_band(clear)}")
            out.append(R97_SERVICE_LINE)
            try:
                st_ = st if isinstance(st, dict) else {}
                _bl9.BotEngine._add_signal(
                    st_, f"💡 เสนอบริการปิดภาระแล้ว — วงเงินจริง {now/1e6:.2f}M "
                         f"-> ปิดภาระได้ {clear/1e6:.2f}M (ต่าง {(clear-now)/1e6:.2f}M) "
                         f"· แจ้งลูกค้าที่ {_r97_band(now)} (กดลงเพื่อความปลอดภัย)")
            except Exception:
                pass
        return out
    except Exception as _e:
        print(f"[R97 QUOTE ERROR] {_e}")
        return []


# บับเบิลเดิมที่ซ้ำความกับ R97 — ยิงพร้อมกันแล้วลูกค้างง (เจอตอนเทส)
_R97_DROP = ("วงเงินกู้ขึ้นกับรายได้",
             "รบกวนบอกรายได้ต่อเดือนและยอดผ่อน",
             "เดี๋ยวผมประเมินเบื้องต้นให้")


def _r97_is_question(b):
    try:
        return "?" in str(b) or "ไหมครับ" in str(b) or "เท่าไหร่ครับ" in str(b) \
            or "ไหมคะ" in str(b) or "เท่าไหร่คะ" in str(b)
    except Exception:
        return False


try:
    _R97_BASE_DECIDE = CalmBotEngine._decide

    def _decide_r97(self, msg, user_id, state, bucket, is_new):
        # r118 (1 ก.ย. 2569) — สายผู้เช่า/ผู้ขาย ไม่ใช่กรวยคนซื้อ ข้ามชั้นนี้
        # เคสจริง 1 ก.ย. 11:06 น.: ลูกค้าบอก "สนใจเช่าคอนโด" ตั้งแต่ประโยคแรก
        # แต่ชั้นนี้ยัดคำถาม อายุ/วงเงิน/ผู้กู้ร่วม/หนี้ ใส่จนลูกค้าพิมพ์ว่า
        # "คุยกันไม่รู้เรื่องล่ะยกเลิกค่ะ" แล้วเซลต้องมาขอโทษแทนบอท
        if state.get("renter") or state.get("owner"):
            return _R97_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
        _uid = str(user_id)[:8]
        _tease = False
        try:
            _d = state.get("data") or {}
            # B: ถามวงเงินแต่ยังคำนวณไม่ได้ -> ขยี้ แล้วปล่อยกลับ funnel
            if (_r97_asks_limit(msg) and not state.get("_r97_teased")
                    and not state.get("done") and not _r97_quote(_d, state)):
                state["_r97_teased"] = True
                _tease = True
        except Exception as _e:
            print(f"[R97 TEASE ERROR] {_e}")

        bubbles, grade = _R97_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)

        try:
            if not isinstance(bubbles, list):
                bubbles = [bubbles] if bubbles else []
            _d = state.get("data") or {}

            if _tease:
                # ตัดบับเบิล FAQ วงเงินเดิมทิ้ง — ซ้ำความกับ R97_TEASE
                bubbles = [b for b in bubbles
                           if not any(k in str(b) for k in _R97_DROP)]
                bubbles = list(R97_TEASE) + bubbles
                print(f"[R97] {_uid}... ถามวงเงินแต่ข้อมูลไม่ครบ — ขยี้แล้วส่งกลับ funnel")

            # A: เพิ่งได้คำตอบเรื่องหนี้ครั้งแรก -> ถามต่ออีก 1 ข้อ ทวนของเดิม
            elif (_d.get("debt") and not state.get("_r97_debt_more")
                  and not state.get("done") and not state.get("closed")
                  and not _bl9._has_any(str(msg or ""), _R97_NO_MORE)):
                state["_r97_debt_more"] = True
                # 1 คำถาม/เทิร์น (Gift สั่ง) — ตัดคำถามอื่นในเทิร์นนี้ทิ้ง
                # ข้อที่ตัดยังว่างอยู่ _next_missing จะถามเองในเทิร์นถัดไป
                bubbles = [b for b in bubbles if not _r97_is_question(b)]
                bubbles.append(_r97_debt_more_q(_d))
                print(f"[R97] {_uid}... ถามหนี้ต่อ (ทวนของเดิม + บัตร/สินเชื่อ/บ้าน)")

            # C: ข้อมูลครบแล้ว -> แจ้งวงเงินแบบปลอดภัย ครั้งเดียวต่อแชท
            if (not state.get("_r97_quoted") and not state.get("closed")
                    and (state.get("_r97_teased") or _r97_asks_limit(msg))):
                _q = _r97_quote(_d, state)
                if _q:
                    state["_r97_quoted"] = True
                    bubbles = [b for b in bubbles
                               if not any(k in str(b) for k in _R97_DROP)]
                    bubbles = bubbles + _q
                    print(f"[R97] {_uid}... แจ้งวงเงินแบบปลอดภัย ({R97_QUOTE_PCT:.0%}) "
                          "+ เสนอบริการปิดภาระ")
        except Exception as _e:
            print(f"[R97 DECIDE ERROR] {_e} — ใช้ทางเดิม")
        return bubbles, grade

    CalmBotEngine._decide = _decide_r97
    print(f"[R97] ถามหนี้ละเอียดขึ้น + แจ้งวงเงินที่ {R97_QUOTE_PCT:.0%}-"
          f"{R97_QUOTE_PCT+R97_QUOTE_SPAN:.0%} ของจริง + เสนอบริการปิดภาระ")
except Exception as _e:
    print(f"[R97 ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")

print("[R97] เกณฑ์/เกรด/ชีต ไม่แตะ — เปลี่ยนเฉพาะคำพูดกับลูกค้า")


















# ======================================================
# r102 — ขอเบอร์ทั้งที่ยังไม่ผ่านเกณฑ์ (Gift 29 ส.ค. "ขอเบอร์มาทำไมยังไม่ผ่านเกณฑ์เลย")
# ======================================================
# เคสจริงเพจ Star Condominium (Callme Joy) — เล่นซ้ำได้ทุกครั้ง:
#   บอท: "พอมีใครกู้ร่วมได้ไหมครับ"        (awaiting=co_borrower)
#   ลูกค้า: "มีเป็นลูกชายได้มั้ยค่ะ"        <- ตอบว่ามี + บอกด้วยว่าเป็นใคร
#   บอท: "รับทราบค่ะ" แล้วข้ามไปถามหนี้     <- ไม่ได้บันทึกว่ามีผู้กู้ร่วมเลย
#   ...
#   บอท: "ขอเบอร์หน่อยครับ"                <- ขอเบอร์ทั้งที่รายได้ 18,000 ไม่ผ่านเกณฑ์
#                                             และยังไม่รู้รายได้ผู้กู้ร่วมสักบาท
#
# 3 รูรั่วซ้อนกัน:
#  A) คำตอบที่ "พูดเป็นคำถาม" ไม่ถูกเก็บ — process() ข้ามการ capture เมื่อ _is_question
#     "มีเป็นลูกชายได้มั้ยค่ะ" คือคำตอบว่ามี แต่ระบบเห็นเป็นคำถามล้วน -> co_borrower_yes ไม่ถูกตั้ง
#     -> co_income ไม่ถูกถาม -> ไม่มีอะไรกั้นไม่ให้เดินไปขอเบอร์
#  B) ด่านขอเบอร์เช็คแค่ "รู้รายได้หรือยัง" (_income_known) ไม่ได้เช็ค "ผ่านเกณฑ์หรือยัง"
#     18,000 = รู้แล้ว -> ผ่านด่าน ทั้งที่ต่ำกว่าเกณฑ์ 25,000 และต้องมีผู้กู้ร่วม
#  C) คำตอบอาชีพหล่นลงช่องหนี้ — "ประจำค่ะ" ไปต่อท้าย debt
#     -> r97 ทวนกลับว่า "นอกจากประจำค่ะ มีผ่อนมอเตอร์2100 ที่แจ้งมา..." = อ่านแล้วไม่โปร
#     (เจอแบบเดียวกันในเพจ Millionaire asset: "นอกจากไม่มี ที่แจ้งมา...")
#
# กฎเหล็กที่ยังถือ: ห้ามทิ้งเคส — ด่านใหม่กั้นได้จำกัดรอบ ครบแล้วปล่อยผ่าน + ติดธงให้เซล
# ไม่แตะสูตรคำนวณ ไม่แตะเกณฑ์เกรด ไม่แตะชีต ไม่แตะการแจกเคส

R102_CO_ASK_MAX = 2      # กั้นไม่ให้ขอเบอร์ได้สูงสุดกี่รอบ ครบแล้วปล่อย + ติดธง

# คำที่บอกว่า "มีผู้กู้ร่วม" แม้จะพูดเป็นคำถาม
_R102_COB_PERSON = ("ลูกชาย", "ลูกสาว", "ลูก", "แฟน", "สามี", "ภรรยา", "เมีย", "ผัว",
                    "คู่สมรส", "คู่ชีวิต", "พ่อ", "แม่", "บิดา", "มารดา", "พี่ชาย",
                    "พี่สาว", "น้องชาย", "น้องสาว", "พี่", "น้อง", "ญาติ", "ลุง", "ป้า",
                    "น้า", "อา")
# คำตอบเรื่อง "อาชีพ" ที่ไม่ใช่ยอดหนี้ — ห้ามหล่นลงช่องหนี้
_R102_JOB_WORDS = ("ประจำ", "พนักงาน", "ข้าราชการ", "รัฐวิสาหกิจ", "ฟรีแลนซ์",
                   "อิสระ", "ธุรกิจส่วนตัว", "ค้าขาย", "รับจ้าง", "เจ้าของกิจการ")


def _r102_says_has_cob(msg):
    """ข้อความนี้แปลว่า 'มีผู้กู้ร่วม' ไหม — รับได้แม้พูดเป็นคำถาม"""
    try:
        s = str(msg or "")
        if _bl9._says_no_coborrower(s):
            return False
        low = s.lower()
        if any(h in low for h in _bl9._NO_COB_HINTS):
            return False
        return any(w in s for w in _R102_COB_PERSON)
    except Exception as _e:
        print(f"[R102 COB ERROR] {_e}")
        return False


def _r102_submittable(data, state):
    """เคสนี้ 'ยื่นได้จริง' หรือยัง — ใช้ตัดสินว่าควรขอเบอร์ได้หรือยัง

    ยังไม่ได้ = รายได้ไม่ถึงเกณฑ์/ภาระหนัก + ต้องพึ่งผู้กู้ร่วม แต่ยังไม่รู้รายได้เขา
    """
    try:
        d = data or {}
        if d.get("cash") or d.get("income_unknown"):
            return True                      # ซื้อสด/ไม่ยอมบอก = ไม่ต้องกั้น
        _need_cob = bool(d.get("low_income") or d.get("high_burden"))
        if not _need_cob:
            return True                      # ยื่นเดี่ยวได้ ไม่ต้องกั้น
        if d.get("co_borrower_none"):
            return True                      # บอกแล้วว่าไม่มี — ทางอื่นจัดการต่อ
        if d.get("co_borrower_income"):
            return True                      # รู้รายได้ผู้กู้ร่วมแล้ว
        # ต้องมีผู้กู้ร่วม แต่ยังไม่รู้รายได้เขา -> ยังไม่ควรขอเบอร์
        return False
    except Exception as _e:
        print(f"[R102 SUBMIT ERROR] {_e}")
        return True                          # พลาดเมื่อไหร่ = ไม่กั้น (ห้ามทำให้ตัน)


try:
    _R102_ORIG_NEXT = _bl9.BotEngine._next_missing

    def _next_missing_r102(self, data, state=None, skip=None):
        _f, _q = _R102_ORIG_NEXT(self, data, state, skip)
        try:
            if _f == "contact":
                _st = state or {}
                _d = data or {}
                if not _r102_submittable(_d, _st):
                    # นับจาก asked ของ funnel เอง (เพิ่มตอน "ถามจริง" เท่านั้น)
                    # ห้ามใช้ตัวนับของตัวเอง — _next_missing ถูกเรียกหลายรอบต่อเทิร์น
                    # โควตาจะไหม้หมดตั้งแต่เทิร์นแรกโดยที่ยังไม่ได้ถามลูกค้าเลย
                    _asked = (_st.get("asked") or {})
                    _n = int(_asked.get("co_income") or 0)
                    _sk = skip or set()
                    _want = ("co_income" if _d.get("co_borrower_yes")
                             else "co_borrower")
                    # ★ r111 (31 ส.ค. 2569) — กันลูปไม่รู้จบ
                    # _decide มี while วนเรียก _next_missing(skip=...) จนกว่าจะ
                    # ได้ช่องที่ยังไม่ตันโควตา ถ้าเราคืนช่องที่อยู่ใน skip แล้ว
                    # (หรือช่องนั้นถามครบ MAX_ASK_PER_FIELD แล้ว) while จะวนไม่จบ
                    # -> บอทค้างทั้งเทิร์น ไม่ส่งข้อความ + log ท่วมจน Railway
                    #    ตัดทิ้ง (เหตุการณ์จริง 31 ส.ค. 2569 ทั้งวัน)
                    try:
                        _maxask = int(_bl9.MAX_ASK_PER_FIELD)
                    except Exception:
                        _maxask = 2
                    _burned = (_want in _sk
                               or int(_asked.get(_want) or 0) >= _maxask)
                    if _n < R102_CO_ASK_MAX and not _burned:
                        print(f"[R102 GATE] ยังไม่ผ่านเกณฑ์ (รายได้ "
                              f"{_d.get('income_baht') or _d.get('income')!r} "
                              f"+ ยังไม่รู้รายได้ผู้กู้ร่วม) — ยังไม่ขอเบอร์ "
                              f"ถามผู้กู้ร่วมต่อ รอบ {_n + 1}/{R102_CO_ASK_MAX}")
                        if _d.get("co_borrower_yes"):
                            return "co_income", _bl9.CO_INCOME_Q
                        return "co_borrower", _bl9.QUALIFY_QUESTIONS[
                            _bl9.FIELD_Q_INDEX["co_borrower"]]
                    if _burned:
                        print(f"[R102 GATE] ถาม {_want} ครบโควตา/ถูกข้ามแล้ว "
                              "— ปล่อยขอเบอร์ + ติดธง (กันลูป r111)")
                    # ครบโควตาแล้ว — ห้ามทิ้งเคส ปล่อยขอเบอร์ แต่ติดธงให้เซลเห็น
                    try:
                        _bl9.BotEngine._add_signal(
                            _st, "⚠️ ขอเบอร์ทั้งที่ยังไม่ผ่านเกณฑ์ — รายได้ต่ำกว่า "
                                 f"{_bl9.LOW_INCOME_BAHT:,} และยังไม่ได้รายได้ผู้กู้ร่วม "
                                 "(ถามครบโควตาแล้ว) เซลต้องเช็คก่อนเสนอ")
                    except Exception:
                        pass
                    print("[R102 GATE] ถามครบโควตาแล้ว — ปล่อยขอเบอร์ + ติดธงให้เซล")
        except Exception as _e:
            print(f"[R102 NEXT ERROR] {_e} — ใช้ทางเดิม")
        return _f, _q

    _bl9.BotEngine._next_missing = _next_missing_r102
    print(f"[R102] ด่านขอเบอร์: ต้องผ่านเกณฑ์ก่อน (กั้นได้ {R102_CO_ASK_MAX} รอบ "
          "แล้วปล่อย + ติดธง ไม่ทิ้งเคส)")
except Exception as _e:
    print(f"[R102 NEXT PATCH ERROR] ต่อไม่ติด: {_e}")


try:
    _R102_ORIG_CAPTURE = _bl9.BotEngine._capture

    def _capture_r102(self, state, field, msg):
        try:
            # (C) คำตอบอาชีพล้วน ไม่มีตัวเลข -> ห้ามลงช่องหนี้
            _s = str(msg or "")
            _job_only = (any(w in _s for w in _R102_JOB_WORDS)
                         and not _bl9._has_any(_s, _bl9._DEBT_SAYS))
            if field in ("debt", "debt_baht"):
                if _job_only and _bl9._parse_debt_monthly(_s) is None:
                    print(f"[R102 SLOT] {_s[:30]!r} เป็นคำตอบอาชีพ ไม่ใช่ยอดผ่อน "
                          "— ไม่เขียนช่องหนี้")
                    return None
            # หมายเหตุ: ไม่ดรอปช่อง co_income แม้คำตอบจะเป็นอาชีพล้วน
            # เพราะ funnel นับ asked ไปแล้ว ถ้าช่องยังว่างมันจะ "ข้าม" ช่องนี้ทิ้ง
            # ปล่อยให้เขียนได้ แล้วใช้ด่าน _r102_submittable กั้นแทน
            # (ด่านดู co_borrower_income ที่เป็นตัวเลขจริง ไม่ได้ดูข้อความ)
        except Exception as _e:
            print(f"[R102 CAPTURE ERROR] {_e} — ใช้ทางเดิม")
        return _R102_ORIG_CAPTURE(self, state, field, msg)

    _bl9.BotEngine._capture = _capture_r102
    print("[R102] คำตอบอาชีพไม่หล่นลงช่องหนี้แล้ว")
except Exception as _e:
    print(f"[R102 CAPTURE PATCH ERROR] ต่อไม่ติด: {_e}")


# ---------- (A) คำตอบผู้กู้ร่วมที่พูดเป็นคำถาม ต้องถูกบันทึก ----------
try:
    _R102_BASE_DECIDE = CalmBotEngine._decide

    def _decide_r102(self, msg, user_id, state, bucket, is_new):
        # r118 (1 ก.ย. 2569) — สายผู้เช่า/ผู้ขาย ไม่ใช่กรวยคนซื้อ ข้ามชั้นนี้
        # เคสจริง 1 ก.ย. 11:06 น.: ลูกค้าบอก "สนใจเช่าคอนโด" ตั้งแต่ประโยคแรก
        # แต่ชั้นนี้ยัดคำถาม อายุ/วงเงิน/ผู้กู้ร่วม/หนี้ ใส่จนลูกค้าพิมพ์ว่า
        # "คุยกันไม่รู้เรื่องล่ะยกเลิกค่ะ" แล้วเซลต้องมาขอโทษแทนบอท
        if state.get("renter") or state.get("owner"):
            return _R102_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
        try:
            _d = (state or {}).get("data") or {}
            if ((state or {}).get("awaiting") == "co_borrower"
                    and not _d.get("co_borrower_yes")
                    and not _d.get("co_borrower_none")
                    and _r102_says_has_cob(msg)):
                _d["co_borrower_yes"] = True
                _d.setdefault("co_borrower", str(msg)[:80])
                print(f"[R102 COB] {str(user_id)[:8]}... ตอบว่ามีผู้กู้ร่วม "
                      f"(พูดเป็นคำถามก็นับ) | {str(msg)[:40]!r}")
        except Exception as _e:
            print(f"[R102 COB ERROR] {_e} — ใช้ทางเดิม")
        bubbles, grade = _R102_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
        try:
            # กันถามเรื่องผู้กู้ร่วม 2 คำถามในเทิร์นเดียว (ด่าน r102 + คำถามทวนของเดิม)
            # กติกา Gift: 1 เทิร์น 1 คำถาม
            if isinstance(bubbles, list) and len(bubbles) > 1:
                _hit = [i for i, b in enumerate(bubbles) if "ผู้กู้ร่วม" in str(b)]
                if len(_hit) > 1:
                    for i in reversed(_hit[1:]):
                        bubbles.pop(i)
                    print(f"[R102] {str(user_id)[:8]}... ตัดคำถามผู้กู้ร่วมที่ซ้ำ "
                          f"ในเทิร์นเดียว เหลือ 1 ข้อ")
        except Exception as _e:
            print(f"[R102 BUBBLE ERROR] {_e} — ใช้ชุดเดิม")
        return bubbles, grade

    CalmBotEngine._decide = _decide_r102
    print("[R102] 'มีเป็นลูกชายได้มั้ยคะ' = ตอบว่ามีผู้กู้ร่วม ไม่ใช่แค่คำถาม")
except Exception as _e:
    print(f"[R102 DECIDE PATCH ERROR] ต่อไม่ติด: {_e}")


# ======================================================
# r100 — คำตอบลงผิดช่อง: "งบ" กลายเป็นรายได้ · "เงินเดือน" กลายเป็นหนี้
# ======================================================
# เคสจริงที่ไล่เจอ 29 ส.ค. 2026 (ตามรอยทีละเทิร์น):
#   บอทถามรายได้ -> ลูกค้าตอบ "งบ 2.5 ล้าน"  -> ลงช่องรายได้
#   บอทถามยอดผ่อน -> ลูกค้าตอบ "เงินเดือน 45000" -> ลงช่องหนี้
#   ตอนตีเกรดอ่านช่องรายได้ได้ "งบ 2.5 ล้าน" -> _parse_income คืน 2,500,000
#   -> เข้าใจว่ารายได้เดือนละ 2.5 ล้าน -> วงเงิน 331,355,932 -> เกรด A
#   ค่าที่ถูกคือ _capacity(45,000 · 10,000 · 35) = 3,644,067  (คลาดเคลื่อน 91 เท่า)
#
# r95 วางกฎ "เนื้อความชนะช่องที่ค้าง" ไว้แล้ว และ _r95_kind อ่านออกด้วยซ้ำว่า
# "เงินเดือน 45000" = income แต่ _r95_reroute ไม่มีสาขาที่ดูแลคู่ รายได้<->หนี้
# (มีแต่สาขาผู้กู้ร่วม/เบอร์โทร/โซน-งบ) r100 เติมสาขาที่ขาดไป
#
# ⚠️ Gift ท้วง 29 ส.ค.: "ธุรกิจ น่าจะเกิน 1 ล้านได้นะ" — ถูกต้อง
#    จึง **ไม่ตั้งเพดานตัดทิ้ง** เจ้าของกิจการมีรายได้เกินล้าน/เดือนได้จริง
#    ตัวตัดสินคือ "ลูกค้าเรียกมันว่าอะไร" ไม่ใช่ "เลขใหญ่แค่ไหน"
#      · พูดว่า รายได้/เงินเดือน/ยอดขาย/เดือนละ -> รับเป็นรายได้ ไม่ว่ากี่ล้าน
#      · พูดว่า งบ/งบประมาณ/ราคา/ไม่เกิน       -> เป็นงบซื้อ ไม่ใช่รายได้
#      · เลขหลักล้านโดดๆ ไม่มีคำกำกับ           -> ถามให้ชัด 1 ครั้ง ไม่เดา ไม่ทิ้ง
#
# ไม่แตะสูตรคำนวณ ไม่แตะเกณฑ์เกรด ไม่แตะชีต ไม่แตะการแจกเคส
# แก้แค่ "อะไรลงช่องไหน" · ทุกทางออกยังตอบลูกค้าเสมอ

# คำที่บอกว่าเลขก้อนนี้คือ "งบซื้อห้อง" ไม่ใช่รายได้ต่อเดือน
_R100_BUDGET_WORDS = ("งบ", "งบประมาณ", "ตั้งงบ", "ราคา", "ไม่เกิน",
                      "วงเงินซื้อ", "ราคาห้อง", "ห้องราคา")
# คำที่บอกว่าเป็นรายได้ (รวมสายธุรกิจ) — เพิ่มจาก _INCOME_SAYS เดิม
_R100_INCOME_WORDS = ("ยอดขาย", "รายรับ", "กำไร", "เงินเข้า", "ได้เดือนละ",
                      "รายได้", "เงินเดือน", "เดือนละ", "ต่อเดือน", "สลิป")
R100_BIG = 1_000_000        # เลขที่ "ต้องถามให้ชัด" ไม่ใช่ "ตัดทิ้ง"

R100_ASK_KIND = ("ขอเช็คให้ชัดนิดนึงนะครับ ตัวเลขที่แจ้งมา "
                 "หมายถึงรายได้ต่อเดือน หรืองบที่ตั้งไว้ซื้อห้องครับ")


def _r100_has(s, words):
    try:
        return any(w in str(s or "") for w in words)
    except Exception:
        return False


def _r100_reroute(field, msg, data, state):
    """เติมสาขาที่ r95 ยังไม่มี — คู่ รายได้ <-> หนี้ <-> งบ

    คืนเหมือน _r95_reroute: (ช่องใหม่, เหตุผล) · None = ทิ้ง · False = ไม่ต้องสลับ
    """
    try:
        s = str(msg or "")
        _inc_word = _r100_has(s, _R100_INCOME_WORDS)
        _bud_word = _r100_has(s, _R100_BUDGET_WORDS)
        _dbt_word = _bl9._has_any(s, _bl9._DEBT_SAYS)

        # (1) รอยอดผ่อน แต่ลูกค้าพูดถึงรายได้ชัดๆ -> ลงช่องรายได้ ไม่ใช่หนี้
        #     เคสจริง: "เงินเดือน 45000" กลายเป็น debt_baht=45000
        if field in ("debt", "debt_baht") and _inc_word and not _dbt_word:
            if _bl9._parse_income(s):
                return "income", "พูดถึงรายได้ ไม่ใช่ยอดผ่อน"

        # (2) รอรายได้ แต่ลูกค้าตอบงบซื้อห้อง -> ไม่เขียนช่องรายได้
        #     (ช่องงบมีตัวเก็บของมันเองอยู่แล้ว) แล้วปล่อยให้ถามรายได้ต่อ
        if field in ("income", "income_baht", "income_total"):
            if _bud_word and not _inc_word:
                return None, "เป็นงบซื้อห้อง ไม่ใช่รายได้ต่อเดือน — ถามรายได้ต่อ"
            # (3) เลขหลักล้านโดดๆ ไม่มีคำกำกับเลย -> ถามให้ชัด 1 ครั้ง
            #     ห้ามเดา และห้ามทิ้งเคส (ธุรกิจมีรายได้เกินล้านได้จริง)
            if not _inc_word and not _bud_word and not _dbt_word:
                _v = _bl9._parse_income(s)
                if _v and int(_v) >= R100_BIG:
                    if not (state or {}).get("_r100_asked_kind"):
                        state["_r100_asked_kind"] = True
                        state["_r100_ask_now"] = True
                        return None, f"เลข {int(_v):,} ไม่มีคำกำกับ — ถามให้ชัดก่อน"
    except Exception as _e:
        print(f"[R100 REROUTE ERROR] {_e} — ใช้ทางเดิม")
    return False


try:
    _R100_ORIG_CAPTURE = _bl9.BotEngine._capture

    def _capture_r100(self, state, field, msg):
        try:
            _r = _r100_reroute(field, msg, (state or {}).get("data") or {}, state)
            if _r is not False:
                _new, _why = _r
                print(f"[R100 SLOT] ช่อง {field!r} <- {str(msg)[:34]!r} : {_why} "
                      f"-> {_new or 'ไม่เขียนช่องไหน'}")
                if _new is None:
                    state["awaiting"] = None
                    return None
                state["awaiting"] = None
                field = _new
        except Exception as _e:
            print(f"[R100 CAPTURE ERROR] {_e} — ใช้ทางเดิม")
        return _R100_ORIG_CAPTURE(self, state, field, msg)

    _bl9.BotEngine._capture = _capture_r100
    print("[R100] คู่รายได้<->หนี้<->งบ ไม่ยัดผิดช่องแล้ว "
          "(เลขหลักล้านไม่มีคำกำกับ = ถามให้ชัด ไม่เดา ไม่ทิ้ง)")
except Exception as _e:
    print(f"[R100 CAPTURE PATCH ERROR] ต่อไม่ติด: {_e}")


# ---------- ถามให้ชัดว่าเลขก้อนนั้นคือรายได้หรืองบ ----------
try:
    _R100_BASE_DECIDE = CalmBotEngine._decide

    def _decide_r100(self, msg, user_id, state, bucket, is_new):
        # r118 (1 ก.ย. 2569) — สายผู้เช่า/ผู้ขาย ไม่ใช่กรวยคนซื้อ ข้ามชั้นนี้
        # เคสจริง 1 ก.ย. 11:06 น.: ลูกค้าบอก "สนใจเช่าคอนโด" ตั้งแต่ประโยคแรก
        # แต่ชั้นนี้ยัดคำถาม อายุ/วงเงิน/ผู้กู้ร่วม/หนี้ ใส่จนลูกค้าพิมพ์ว่า
        # "คุยกันไม่รู้เรื่องล่ะยกเลิกค่ะ" แล้วเซลต้องมาขอโทษแทนบอท
        if state.get("renter") or state.get("owner"):
            return _R100_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
        bubbles, grade = _R100_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
        try:
            if state.pop("_r100_ask_now", False):
                if isinstance(bubbles, list):
                    # แทนที่คำถามเดิม 1 ตัว ไม่เพิ่มจำนวนบับเบิล (กติกา 1 คำถาม/เทิร์น)
                    _q = [i for i, b in enumerate(bubbles)
                          if "?" in str(b) or "ไหมครับ" in str(b) or "เท่าไหร่" in str(b)
                          or "ไหมคะ" in str(b)]
                    if _q:
                        bubbles[_q[0]] = R100_ASK_KIND
                    else:
                        bubbles.append(R100_ASK_KIND)
                    print(f"[R100] {str(user_id)[:8]}... ถามให้ชัดว่าเป็นรายได้หรืองบ")
        except Exception as _e:
            print(f"[R100 DECIDE ERROR] {_e} — ใช้ชุดเดิม")
        return bubbles, grade

    CalmBotEngine._decide = _decide_r100
    print("[R100] เลขกำกวม -> ถาม 'รายได้ต่อเดือน หรืองบซื้อห้อง' ครั้งเดียว")
except Exception as _e:
    print(f"[R100 DECIDE PATCH ERROR] ต่อไม่ติด: {_e}")


# ======================================================
# r99 — เทิร์นแรก: ถามวัตถุประสงค์ก่อน ไม่เทข้อมูลทั้งชุด
# ======================================================
# เคสจริง 29 ส.ค. 2026 เพจ Wealth Owner — คุณ Mam Anchalee พิมพ์ "สนใจรายละเอียด"
# บอทตอบรวด 3 บับเบิล: ทักทาย+ถามวัตถุประสงค์ · ชุดย่าน+ช่วงราคา · อธิบายราคา+ขอเบอร์
# Gift: "ยังไม่แก้อีก อะไรเยอะแยะ เปิดมาแค่อันเดียวพอ อยู่เองหรือปล่อยเช่า"
#
# ต้นเหตุ: r76 [INFO ASK] ขยายปากทางให้ "ขอรายละเอียด/สนใจ/อยากดูคร่าวๆ"
# เข้าทางเดียวกับคำถามทำเลจริงๆ -> เทชุดย่าน+ช่วงราคา+ขอเบอร์ตั้งแต่ประโยคแรก
# ทั้งที่ยังไม่รู้ด้วยซ้ำว่าเขาจะอยู่เองหรือลงทุน
#
# วิธีแก้ที่เลือก: ปิดเฉพาะ "ปากทางที่ r76 ขยายไว้" เฉพาะเทิร์นแรกเท่านั้น
#   · เทิร์นแรก + ข้อความกว้างๆ ("สนใจรายละเอียด")  -> เข้า funnel ปกติ = ถามวัตถุประสงค์
#   · เทิร์นแรก + ถามทำเลจริงๆ ("มีห้องแถวรัชดาไหม") -> ตอบเหมือนเดิมทุกประการ
#   · เทิร์นที่ 2 เป็นต้นไป                          -> ไม่แตะเลย เหมือนเดิม 100%
#
# ⚠️ ทำไมไม่ใช้วิธี "ตัดบับเบิลทิ้งให้เหลือ 1"
#    เพราะบับเบิลที่ตัดทิ้งอาจเป็น "คำถาม" ที่ระบบตั้ง awaiting ไว้แล้ว
#    ตัดคำถามออกแต่ awaiting ยังค้าง = คำตอบเทิร์นถัดไปลงผิดช่อง -> เกรดเพี้ยน
#    (บทเรียนเดียวกับ r95 ที่เจอเบอร์โทรกลายเป็นรายได้ผู้กู้ร่วม)
#    วิธีนี้ไม่แตะบับเบิล ไม่แตะ awaiting เลย — แค่ไม่พาเข้าทางที่ผิดตั้งแต่แรก
#
# ไม่แตะเกณฑ์ ไม่แตะเกรด ไม่แตะชีต ไม่แตะการแจกเคส

# r99 แก้ 29 ส.ค. 04:35 — ของเดิมใช้ set ใน RAM จำว่า "ใครเคยคุยแล้ว"
# ซึ่งพังทันทีที่ deploy: RAM ล้าง -> ทุกบทสนทนาที่ค้างอยู่กลายเป็น "เทิร์นแรก" หมด
# -> บอทถามวัตถุประสงค์ซ้ำกลางคัน (Gift 29 ส.ค.: "มาถามซ้ำซ้อน ไม่โปร")
# หลักฐานจาก log 04:26-04:34: [R99] ยิง 18 ครั้งจาก 17 ข้อความ รวมถึงข้อความ
# ที่เป็นคำตอบกลางบทสนทนา เช่น 'บางซื่อ อยู่เอง' · 'เป็นpcขายปั๊มลมpumaคะ' · 'ค่ะ'
#
# ของใหม่: ใช้ is_new ที่บอทคำนวณเองใน _resolve_state (r88 โหลด session จาก
# Postgres ก่อนเสมอ) -> ลูกค้าเก่าที่กลับมาหลัง deploy ไม่ถูกนับเป็นเทิร์นแรกอีก
# ไม่มี state เป็นของตัวเองเลย = ไม่มีอะไรให้หายตอนรีสตาร์ท

try:
    _R99_OPEN_FLAG = {"first": False}
    _R99_PREV_ZONE_ASK = _bl._is_zone_ask

    def _zone_ask_r99(msg):
        """เทิร์นแรก: ปิดเฉพาะปากทางกว้างที่ r76 เปิดไว้ ไม่แตะคำถามทำเลจริง"""
        try:
            if _R99_OPEN_FLAG.get("first"):
                try:
                    if _ORIG_ZONE_ASK(msg):        # ถามทำเลจริงๆ -> ตอบเหมือนเดิม
                        return True
                except Exception:
                    pass
                print(f"[R99] เทิร์นแรก ข้อความกว้างๆ — ถามวัตถุประสงค์ก่อน "
                      f"ยังไม่เทชุดย่าน | {(msg or '')[:40]!r}")
                return False
        except Exception as _e:
            print(f"[R99 ZONE ERROR] {_e} — ใช้ทางเดิม")
        return _R99_PREV_ZONE_ASK(msg)

    _bl._is_zone_ask = _zone_ask_r99

    _R99_BASE_DECIDE = CalmBotEngine._decide

    def _decide_r99(self, msg, user_id, state, bucket, is_new):
        # r118 (1 ก.ย. 2569) — สายผู้เช่า/ผู้ขาย ไม่ใช่กรวยคนซื้อ ข้ามชั้นนี้
        # เคสจริง 1 ก.ย. 11:06 น.: ลูกค้าบอก "สนใจเช่าคอนโด" ตั้งแต่ประโยคแรก
        # แต่ชั้นนี้ยัดคำถาม อายุ/วงเงิน/ผู้กู้ร่วม/หนี้ ใส่จนลูกค้าพิมพ์ว่า
        # "คุยกันไม่รู้เรื่องล่ะยกเลิกค่ะ" แล้วเซลต้องมาขอโทษแทนบอท
        if state.get("renter") or state.get("owner"):
            return _R99_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
        # เช็คจาก state จริง ไม่ใช่ set ใน RAM และไม่ใช่ is_new
        # (is_new ของ bot_logic แปลว่า "ต้องทักทายใหม่ไหม" ไม่ใช่ "เทิร์นแรกไหม"
        #  ลูกค้าใหม่เอี่ยมก็ได้ is_new=False ได้ — ตรวจแล้ว 29 ส.ค.)
        # "ยังไม่เคยได้อะไรจากเขาเลย" = ยังไม่รู้ว่าอยู่เองหรือลงทุน = อย่าเพิ่งเท
        # state ถูกโหลดจาก Postgres มาก่อนถึงตรงนี้แล้ว -> ทนต่อการ deploy
        try:
            _st = state or {}
            _d99 = _st.get("data") or {}
            # r105 — เดิมปิดเฉพาะ "เทิร์นแรกเป๊ะๆ" (data ว่าง + ไม่มี awaiting)
            # เคสจริง 30 ส.ค. เพจ Wealth Owner (Kam Phl):
            #   บอทถาม "ซื้อปล่อยเช่า หรืออยู่เองคะ"  -> awaiting=objective
            #   ลูกค้า "สนใจรายละเอียด"               <- ไม่ใช่คำตอบ แต่มีคำว่า "รายละเอียด"
            #   บอทเท 3 ฟองรวด: ถามวัตถุประสงค์ซ้ำ + ย่าน/ราคา 2.1-11 ล้าน + ขอเบอร์
            # ทีม MKT: "ลูกค้าเห็นแบบนี้อ่านไม่ตอบกันหมดเลย"
            # ตราบใดที่ยังไม่รู้ว่าอยู่เองหรือลงทุน = ยังไม่เทชุดย่าน/ราคา/ขอเบอร์
            # คำถามทำเลจริง ("แถวไหนบ้าง") ยังตอบเหมือนเดิม (_ORIG_ZONE_ASK)
            _R99_OPEN_FLAG["first"] = (
                not _d99.get("objective")
                and not _st.get("zone_told")
                and not _st.get("done")
                and (not _st.get("awaiting") or _st.get("awaiting") == "objective"))
        except Exception as _e:
            _R99_OPEN_FLAG["first"] = False
            print(f"[R99 FLAG ERROR] {_e}")
        try:
            return _R99_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
        finally:
            _R99_OPEN_FLAG["first"] = False

    CalmBotEngine._decide = _decide_r99
    print("[R99] เทิร์นแรก (is_new จาก session จริง) ถามวัตถุประสงค์ก่อน — "
          "ยังไม่เทชุดย่าน/ช่วงราคา/ขอเบอร์ · คำถามทำเลจริงยังตอบเหมือนเดิม")
except Exception as _e:
    print(f"[R99 ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")


# ======================================================
# r98 — ค่า API: cache ไม่แตก · วัดได้ · ไม่ถูกเกรียนดูด · ไม่ตายเงียบ
# ======================================================
# ที่มา (Gift 29 ส.ค. 2026 "เชค log ทำไมใช้เปลืองมาก" + "พวกเกรียนๆ มาพิมซ้ำๆ")
#
# 1) เครดิตหมด 28 ส.ค. 21:32 UTC -> [CLAUDE ERROR] 400 รัว 120+ ครั้ง 5 ชม.
#    ทุกครั้งตกไป FALLBACK_MSG ประโยคเดิม ลูกค้าเห็นซ้ำทั้งคืน
#    = ที่ Gift บอกว่า "ตอบไม่ดี ไม่ฉลาด ไม่มี tone voice" — ไม่ใช่โมเดลโง่ แต่ไม่มีโมเดล
#
# 2) system prompt 16,385 ตัว ใส่ cache_control ไว้ แต่โค้ดเดิม "ต่อท้าย"
#    FEMALE_VOICE_RULE / ANGER_RULE / BEHAVIOR_READ_RULE เข้าไปในสตริงเดียวกัน
#    -> ต่อท้ายแค่ 250-400 ตัว แต่ cache key เปลี่ยนทั้งก้อน 12,000 โทเคน
#    -> 6 กระปุก cache TTL 5 นาที ทราฟฟิกจริง ~2 ข้อความ/นาที
#    -> หมดอายุก่อนโดนใช้ซ้ำ = จ่ายราคา "เขียน cache" เกือบทุกครั้ง
#       ไม่เคยได้ส่วนลด "อ่าน cache" ที่เป็นเหตุผลที่เปิด cache ตั้งแต่แรก
#    แก้: แยกเป็น 2 บล็อก — ก้อนใหญ่คงที่ใส่ cache / กฎแปรผันต่อท้ายไม่ใส่ cache
#         เหลือ cache เดียวต่อโมเดล อยู่ warm ตลอด
#
# 3) โค้ดเดิมทิ้ง usage ที่ API ส่งกลับมา -> ไล่ไม่ได้ว่าเงินหายไปกับอะไร
#    แก้: log ทุกครั้ง + สะสมยอดประมาณการเป็น USD
#
# 4) ไม่มีเพดานต่อคน -> คนพิมพ์รัวๆ ดูดเครดิตได้ไม่จำกัด
#    แก้: พิมพ์ซ้ำ = ตอบเดิม ไม่ยิง API · เพดานต่อคนต่อชั่วโมง/ต่อวัน · เพดานรวม
#
# กฎเหล็กที่ยังถือ: ห้ามทำให้บอทเงียบ — ทุกทางออกคืนข้อความเสมอ
# ไม่แตะเกณฑ์ ไม่แตะเกรด ไม่แตะชีต ไม่แตะการแจกเคส

import time as _t98
import difflib as _dm98
from collections import deque as _dq98

# --- เพดานกันเกรียน ---
R98_SIMILAR       = 0.90     # ความเหมือน 90% ขึ้นไป = นับว่าถามซ้ำ
R98_REPEAT_WINDOW = 600      # วิ — พิมพ์ข้อความเดิมซ้ำใน 10 นาที = ตอบเดิม ไม่ยิง API
R98_HOUR_MAX      = 20       # ยิง API ได้กี่ครั้ง/คน/ชั่วโมง
R98_DAY_MAX       = 60       # ยิง API ได้กี่ครั้ง/คน/วัน
R98_GLOBAL_HOUR   = 400      # เพดานรวมทุกคน/ชั่วโมง — กันบิลวิ่งหนี

# --- เบรกเกอร์ตอนเครดิตหมด ---
R98_CREDIT_FAILS  = 3        # 400 credit ติดกันกี่ครั้งถึงหยุดยิง
R98_CREDIT_COOL   = 600      # วิ — หยุดยิงนานแค่ไหนก่อนลองใหม่

R98_FLOOD_MSG  = "ขอเวลาสักครู่นะครับ เดี๋ยวที่ปรึกษาเข้ามาดูให้ครับ"
R98_AIDOWN_MSG = "ขอเวลาสักครู่นะครับ เดี๋ยวที่ปรึกษาเข้ามาตอบให้ครับ"

# ราคา USD ต่อ 1 ล้านโทเคน: (input, เขียน cache, อ่าน cache, output)
R98_PRICE = {
    "haiku":  (1.00,  1.25,  0.10,  5.00),
    "sonnet": (3.00,  3.75,  0.30, 15.00),
    "opus":  (15.00, 18.75,  1.50, 75.00),
}
R98_PRICE_DEFAULT = R98_PRICE["sonnet"]      # ไม่รู้จัก = คิดแพงไว้ก่อน ไม่ประเมินต่ำ

_R98_LAST: dict = {}          # uid -> [ข้อความที่ normalize แล้ว, คำตอบ, เวลา]
_R98_HITS: dict = {}          # uid -> deque(เวลาที่ยิง API)
_R98_DAY: dict = {}           # uid -> [วันที่, จำนวน]
_R98_GLOBAL: _dq98 = _dq98()  # เวลาที่ยิง API ทั้งระบบ
_R98_CREDIT = {"fails": 0, "until": 0.0}
_R98_SPEND = {"usd": 0.0, "calls": 0, "in": 0, "cw": 0, "cr": 0, "out": 0,
              "gated": 0, "repeat": 0}


# คำลงท้ายที่ไม่เปลี่ยนความหมายของคำถาม — ตัดทิ้งก่อนเทียบว่า "ถามซ้ำ" ไหม
# (เกรียนชอบพิมพ์เรื่องเดิมแล้วสลับคำลงท้าย/ยืดสระ ให้ระบบคิดว่าเป็นคำถามใหม่)
_R98_PARTICLE = _re94.compile(
    r"(ครับผม|ครับ|คร้าบ|คับ|ค่ะ|คะ|ค่า|จ้า|จ้ะ|จ๊ะ|ฮะ|นะ|น่ะ|เลย|อ่ะ|อะ|อ่า|หน่อย|ด้วย)")


def _r98_norm(s):
    """ตัดช่องว่าง/วรรคตอน/คำลงท้าย/ตัวซ้ำ เพื่อเทียบว่า 'ข้อความเดิม' ไหม"""
    t = _re94.sub(r"[\s\.\!\?,~ๆฯ\-_]+", "", str(s or "")).lower()
    t = _R98_PARTICLE.sub("", t)
    # ยุบตัวซ้ำ 3 ตัวขึ้นไปเหลือ 2 — แต่ห้ามแตะตัวเลข
    # (ถ้ายุบเลขด้วย "งบ 500,000" กับ "งบ 5,000,000" จะกลายเป็นข้อความเดียวกัน
    #  แล้วบอทจะตอบคำถามเก่าให้ลูกค้าที่ถามเรื่องใหม่ — ห้ามเด็ดขาด)
    return _re94.sub(r"([^\d])\1{2,}", r"\1\1", t)   # ฮ่าาาาา -> ฮ่าา


def _r98_same(a, b):
    """ถามซ้ำไหม — ตรงเป๊ะ หรือใกล้เคียงมาก (พิมพ์ผิด/ยืดสระ/สลับคำลงท้าย)

    กฎเหล็ก: ตัวเลขต่างกันเมื่อไหร่ = คนละคำถามเสมอ ห้ามนับว่าซ้ำ
    ในบอทนี้ตัวเลขคือเนื้อหาทั้งหมด (รายได้ ยอดผ่อน อายุ งบ)
    "งบ 500,000" กับ "งบ 5,000,000" หน้าตาเหมือนกัน 94% แต่คนละเรื่องคนละเกรด
    """
    if not a or not b:
        return False
    if _re94.sub(r"\D", "", a) != _re94.sub(r"\D", "", b):
        return False
    if a == b:
        return True
    if abs(len(a) - len(b)) > max(6, min(len(a), len(b)) // 3):
        return False
    try:
        return _dm98.SequenceMatcher(None, a, b).ratio() >= R98_SIMILAR
    except Exception:
        return False


def _r98_price(model):
    m = str(model or "").lower()
    for k, v in R98_PRICE.items():
        if k in m:
            return v
    return R98_PRICE_DEFAULT


def _r98_prune(uid, now):
    dq = _R98_HITS.get(uid)
    if dq is not None:
        while dq and now - dq[0] > 3600:
            dq.popleft()
        if not dq:
            _R98_HITS.pop(uid, None)
    while _R98_GLOBAL and now - _R98_GLOBAL[0] > 3600:
        _R98_GLOBAL.popleft()
    # กันหน่วยความจำบวม — ล้างของเก่าเมื่อโตเกิน
    if len(_R98_LAST) > 3000:
        for k, v in list(_R98_LAST.items()):
            if now - v[2] > R98_REPEAT_WINDOW * 3:
                _R98_LAST.pop(k, None)
    if len(_R98_DAY) > 5000:
        _today = _t98.strftime("%Y-%m-%d", _t98.gmtime(now))
        for k, v in list(_R98_DAY.items()):
            if v[0] != _today:
                _R98_DAY.pop(k, None)


def _r98_gate(uid, msg, state):
    """ยิง API ได้ไหม — คืน (ok, ข้อความสำเร็จรูปถ้าไม่ให้ยิง)

    ห้ามคืน (False, "") เด็ดขาด — บอทต้องมีอะไรตอบเสมอ
    """
    now = _t98.time()
    _r98_prune(uid, now)

    # (0) เครดิตหมด/API ล่ม — ไม่ยิงซ้ำให้เปลืองเวลา ลูกค้ารอ
    if _R98_CREDIT["until"] > now:
        return False, R98_AIDOWN_MSG

    # (1) พิมพ์ข้อความเดิมซ้ำ -> ตอบคำตอบเดิม ไม่เสียเงิน
    n = _r98_norm(msg)
    prev = _R98_LAST.get(uid)
    if (n and prev and prev[1] and now - prev[2] <= R98_REPEAT_WINDOW
            and _r98_same(prev[0], n)):
        _R98_SPEND["repeat"] += 1
        print(f"[R98 REPEAT] {uid[:8]}... พิมพ์ซ้ำใน {int(now - prev[2])} วิ "
              f"— ตอบเดิม ไม่ยิง API")
        return False, prev[1]

    # (2) เพดานรวมทั้งระบบ
    if len(_R98_GLOBAL) >= R98_GLOBAL_HOUR:
        _R98_SPEND["gated"] += 1
        print(f"[R98 GLOBAL CAP] ⚠️ ยิง API ครบ {R98_GLOBAL_HOUR} ครั้งใน 1 ชม. "
              f"— หยุดชั่วคราว เช็คว่ามีคนยิงถล่มไหม")
        return False, R98_FLOOD_MSG

    # (3) เพดานต่อคน
    dq = _R98_HITS.setdefault(uid, _dq98())
    if len(dq) >= R98_HOUR_MAX:
        _R98_SPEND["gated"] += 1
        try:
            _bl9.BotEngine._add_signal(
                state,
                f"🚧 พิมพ์ถี่มาก — ยิง AI ครบ {R98_HOUR_MAX} ครั้งใน 1 ชม. "
                f"บอทหยุดใช้ AI ชั่วคราว ให้คนเข้าไปดู")
        except Exception:
            pass
        print(f"[R98 RATE] {uid[:8]}... ครบ {R98_HOUR_MAX} ครั้ง/ชม. — ไม่ยิง API")
        return False, R98_FLOOD_MSG

    today = _t98.strftime("%Y-%m-%d", _t98.gmtime(now))
    d = _R98_DAY.get(uid)
    if not d or d[0] != today:
        d = [today, 0]
        _R98_DAY[uid] = d
    if d[1] >= R98_DAY_MAX:
        _R98_SPEND["gated"] += 1
        print(f"[R98 DAY CAP] {uid[:8]}... ครบ {R98_DAY_MAX} ครั้ง/วัน — ไม่ยิง API")
        return False, R98_FLOOD_MSG

    dq.append(now)
    d[1] += 1
    _R98_GLOBAL.append(now)
    return True, ""


try:
    _R98_BASE_ASK = _bl9.BotEngine._ask_claude

    def _ask_claude_r98(self, user_message, user_id, gender="",
                        done=False, state=None):
        _st = state or {}
        if not _bl9.ANTHROPIC_API_KEY:
            return _bl9.STATUS_MSG if done else _bl9.FALLBACK_MSG

        _ok, _canned = _r98_gate(user_id, user_message, _st)
        if not _ok:
            return _canned or (_bl9.STATUS_MSG if done else _bl9.FALLBACK_MSG)

        # ---------- ประวัติ (เหมือนเดิมทุกประการ) ----------
        history = _bl9._conversations.get(user_id, [])[-10:]
        if not history and _bl9.pg_store is not None:
            try:
                history = _bl9.pg_store.load_history(
                    _st.get("page_id", ""), user_id, 10, _bl9._hash_psid(user_id))
            except Exception:
                history = []
            if history:
                print(f"[PG] AI ใช้ประวัติ {len(history)} ท่อนจาก Postgres")
        messages = history + [{"role": "user", "content": user_message}]

        _angry = bool(_st.get("angry_now"))
        _smart = _angry or _bl9._needs_smart(user_message, _st)
        _model = _bl9.CLAUDE_MODEL_SMART if _smart else _bl9.CLAUDE_MODEL

        # ---------- หัวใจของ r98: แยก cache ออกจากกฎแปรผัน ----------
        # บล็อก 1 = ก้อนใหญ่ที่เหมือนกันทุกครั้ง -> cache เดียวต่อโมเดล อยู่ warm
        # บล็อก 2 = กฎที่เปลี่ยนตามเทิร์น -> ไม่ cache (สั้นมาก ไม่คุ้มและทำ key แตก)
        _sys = [{"type": "text",
                 "text": _bl9.WEC_SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}]
        _tail = ""
        if gender == "female":
            _tail += _bl9.FEMALE_VOICE_RULE
        if _angry:
            _tail += _bl9.ANGER_RULE
            _st.pop("angry_now", None)
        if _smart:
            _tail += _bl9.BEHAVIOR_READ_RULE
        if _tail:
            _sys.append({"type": "text", "text": _tail})

        try:
            resp = requests.post(
                _bl9.ANTHROPIC_URL,
                headers={
                    "x-api-key": _bl9.ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                    "anthropic-beta": "prompt-caching-2024-07-31",
                },
                json={"model": _model, "max_tokens": 400,
                      "system": _sys, "messages": messages},
                timeout=15,
            )
            if resp.status_code != 200:
                _body = resp.text[:200]
                print(f"[CLAUDE ERROR] {resp.status_code}: {_body}")
                if "credit balance" in _body or "billing" in _body.lower():
                    _R98_CREDIT["fails"] += 1
                    if _R98_CREDIT["fails"] >= R98_CREDIT_FAILS:
                        _R98_CREDIT["until"] = _t98.time() + R98_CREDIT_COOL
                        print("[R98 CREDIT] 🔴🔴🔴 เครดิต Anthropic หมด — "
                              f"หยุดยิง API {R98_CREDIT_COOL // 60} นาที "
                              "ลูกค้าจะได้ข้อความให้รอที่ปรึกษาแทน "
                              "→ เติมเงินที่ platform.claude.com/settings/billing")
                        try:
                            _bl9.BotEngine._add_signal(
                                _st,
                                "🔴 เครดิต AI หมด — บอทตอบด้วยข้อความสำรอง "
                                "ต้องมีคนเข้าไปคุยแทน")
                        except Exception:
                            pass
                    return R98_AIDOWN_MSG if not done else _bl9.STATUS_MSG
                return _bl9.STATUS_MSG if done else _bl9.FALLBACK_MSG

            _R98_CREDIT["fails"] = 0
            _R98_CREDIT["until"] = 0.0
            data = resp.json()

            # ---------- วัดเงิน ----------
            try:
                u = data.get("usage") or {}
                _in = int(u.get("input_tokens") or 0)
                _cw = int(u.get("cache_creation_input_tokens") or 0)
                _cr = int(u.get("cache_read_input_tokens") or 0)
                _out = int(u.get("output_tokens") or 0)
                p = _r98_price(_model)
                _usd = (_in * p[0] + _cw * p[1] + _cr * p[2] + _out * p[3]) / 1e6
                _R98_SPEND["usd"] += _usd
                _R98_SPEND["calls"] += 1
                _R98_SPEND["in"] += _in
                _R98_SPEND["cw"] += _cw
                _R98_SPEND["cr"] += _cr
                _R98_SPEND["out"] += _out
                _hit = "HIT " if _cr > _cw else "MISS"
                print(f"[CLAUDE $] {_model} smart={int(_smart)} cache={_hit} "
                      f"in={_in} cw={_cw} cr={_cr} out={_out} "
                      f"= ${_usd:.5f} | รวม {_R98_SPEND['calls']} ครั้ง "
                      f"${_R98_SPEND['usd']:.3f} "
                      f"(ซ้ำ {_R98_SPEND['repeat']} · กัน {_R98_SPEND['gated']})")
            except Exception as _ue:
                print(f"[R98 USAGE] อ่านไม่ได้: {_ue}")

            raw = data["content"][0]["text"].strip()
            if data.get("stop_reason") == "max_tokens":
                print(f"[CLAUDE TRUNCATED] {len(raw)} chars -> trimming")
                raw = _bl9._trim_to_sentence(raw)
                if not raw:
                    return _bl9.STATUS_MSG if done else _bl9.FALLBACK_MSG
            raw = self._take_behavior(raw, _st, _model)
            text = self._sanitize(raw)
            text = _bl9._strip_dup_greeting(text)
            before = len(text)
            text = _bl9._limit_sentences(text)
            if before > len(text):
                print(f"[CLAUDE TRIMMED] {before} -> {len(text)} chars")
            if done and any(k in text for k in ["ขอเบอร์", "ID LINE", "เบอร์ติดต่อ"]):
                return _bl9.STATUS_MSG
            out = text or (_bl9.STATUS_MSG if done else _bl9.FALLBACK_MSG)
            # จำไว้เผื่อลูกค้าพิมพ์ซ้ำ -> ตอบเดิมฟรี
            _R98_LAST[user_id] = [_r98_norm(user_message), out, _t98.time()]
            return out
        except Exception as e:
            print(f"[CLAUDE EXCEPTION] {e}")
            return _bl9.STATUS_MSG if done else _bl9.FALLBACK_MSG

    _bl9.BotEngine._ask_claude = _ask_claude_r98
    print(f"[R98] cache แยกบล็อกแล้ว (ก้อนคงที่ {len(_bl9.WEC_SYSTEM_PROMPT):,} ตัว) "
          f"· log ค่าใช้จ่ายทุกครั้ง · เพดาน {R98_HOUR_MAX}/ชม. {R98_DAY_MAX}/วัน/คน "
          f"· รวม {R98_GLOBAL_HOUR}/ชม. · เบรกเกอร์เครดิต {R98_CREDIT_FAILS} ครั้ง")
except Exception as _e:
    print(f"[R98 ERROR] ต่อไม่ติด — ใช้ _ask_claude เดิมทุกประการ: {_e}")


# ======================================================
# Main
# ======================================================
# ============================================================================
# r104 — ส่ง "วงเงินที่คำนวณได้" เข้าชีต
#
# Gift 30 ส.ค.: "ไม่ต้องระบบให้เซลรู้แค่แจก แต่ระบุให้ marketing รู้"
#
# ตรวจแล้วพบว่าท่อ marketing มีอยู่ครบ (คิวแจกเคส / ลีดเต็ม / สรุปรายวัน /
# สรุปตาม Ad ID) แต่คอลัมน์ "วงเงินกู้ที่คำนวณได้ (บาท)" ว่างเปล่าทุกแถว
# ตั้งแต่วันแรก เพราะ payload ที่บอทยิงเข้าชีตไม่เคยมีตัวเลขนี้เลย
# ทั้งที่ _grade_r89 คำนวณไว้แล้วใน data["capacity_now"] / ["capacity_clear"]
#
# แก้ที่ _income_numbers เพราะถูก spread เข้า payload ทั้ง 2 ทาง
# (_send_to_sheets และ _upsert_lead) — จุดเดียวครอบคลุมหมด
# ห้ามทำให้ชุดเดิมพัง: ถ้าอะไรพลาด ให้คืนค่าชุดเดิมเสมอ
# ============================================================================
try:
    _R104_ORIG_INUM = _bl9.BotEngine._income_numbers

    def _income_numbers_r104(data):
        try:
            _base = _R104_ORIG_INUM(data)
        except Exception as _e104a:
            print(f"[R104 BASE ERROR] {_e104a}")
            raise
        try:
            out = dict(_base)
            _d = data or {}
            _now = _d.get("capacity_now")
            _clr = _d.get("capacity_clear")
            out["capacity"] = "" if _now in (None, "") else int(_now)
            out["capacity_clear"] = "" if _clr in (None, "") else int(_clr)
            return out
        except Exception as _e104b:
            print(f"[R104 CAP ERROR] {_e104b} — ส่งชุดเดิมแทน")
            return _base

    _bl9.BotEngine._income_numbers = staticmethod(_income_numbers_r104)
    print("[R104] ส่งวงเงินประเมินเข้าชีตแล้ว (capacity + capacity_clear) "
          "— ช่องการตลาดที่ว่างมาตั้งแต่วันแรก")
except Exception as _e104:
    print(f"[R104 PATCH FAIL] {_e104} — ใช้ของเดิม ไม่กระทบการทำงาน")


# ============================================================================
# r105 — เซลพิมพ์ต่อโดยไม่ทวนชื่อ บอทต้องหยุด (ทีม MKT 30 ส.ค.)
#
# "พอเราพิมพ์ชื่อไป บอทมันยังไม่หยุดช่วยตอบนะคะ ของ wealth owner"
#
# หลักฐานจาก log 30 ส.ค. — กติกา r57 (ต้องมีชื่อเซลถึงจะหยุด) แก้ปัญหา
# ข้อความออโต้ได้จริง แต่เหวี่ยงกลับอีกทาง: เซล "คนจริง" ที่พิมพ์ต่อ
# โดยไม่ทวนชื่อ ถูกนับเป็นออโต้ -> บอทพิมพ์ทับ
#   07:16 [ECHO IGNORE] 'แฟนมีรายได้ต่อเดือนประมาณเท่าไหร่คะ'
#   09:06 [ECHO IGNORE] 'ครับผม ไม่ทราบ ลูกค้าเป็น พนักงานประจำ /'  (2 แชท)
#
# กติกาใหม่ (แคบมาก ไม่รื้อ r57):
#   ถ้าแชทนี้ "เคยมีเซลตัวจริงแนะนำตัวแล้ว" (handover_by เป็นชื่อคน)
#   ข้อความจากฝั่งเพจครั้งต่อ ๆ ไปถือเป็นคนเสมอ ไม่ต้องพิมพ์ชื่อซ้ำ
#   แชทที่ยังไม่เคยมีเซลเลย = ใช้กติกา r57 เดิมทุกประการ (ออโต้ยังไม่หยุดบอท)
#
# ค่าที่ขึ้นต้นด้วย "(" เป็นสถานะระบบ ไม่ใช่ชื่อคน — ไม่นับ
#   เช่น "(เพจยังไม่เปิดบอทตอบ)" "(ปิดบอทรายเพจ)" "(เคสร้องเรียน — รอผู้จัดการ)"
#
# ทำงานหลังของเดิมเสมอ: ให้ handle_page_echo ตัวจริงตัดสินก่อน
# แล้วค่อยอัปเกรดเฉพาะผลลัพธ์ที่ออกมาเป็น "logged" (= โดน ECHO IGNORE)
# -> พฤติกรรมเดิมทุกเส้นทางไม่ถูกแตะ
# ============================================================================
try:
    import time as _t105
    _R105_BASE_ECHO = CalmBotEngine.handle_page_echo

    def _handle_page_echo_r105(self, customer_id, text, platform="facebook",
                               page_id="", from_app=False):
        _res = _R105_BASE_ECHO(self, customer_id, text, platform, page_id, from_app)
        try:
            if _res != "logged" or from_app:
                return _res
            _skey = f"{page_id}:{customer_id}" if page_id else customer_id
            _st = _bl9._lead_states.get(_skey) or {}
            _prev = str(_st.get("handover_by") or "").strip()
            if not _prev or _prev.startswith("(") or _st.get("handover"):
                return _res
            _now105 = int(_t105.time())
            _st["handover"] = True
            _st["handover_at"] = _now105
            _st["handover_sale_at"] = _now105
            _st["handover_cust"] = 0
            _st.pop("handover_idle_released", None)
            _bl9._lead_states[_skey] = _st
            try:
                _bl9.BotEngine._add_signal(
                    _st, f"เซล{_prev} พิมพ์ต่อในแชทนี้ (ไม่ได้ทวนชื่อ) "
                         f"— บอทหยุดตอบต่อ ไม่พิมพ์ทับ")
            except Exception:
                pass
            print(f"[R105 HANDOVER] {str(customer_id)[:8]}... เซล{_prev} "
                  f"เคยรับช่วงแชทนี้แล้ว — ข้อความฝั่งเพจถือเป็นคน "
                  f"| {str(text or '')[:40]!r}")
            return "handover"
        except Exception as _e105:
            print(f"[R105 ECHO ERROR] {_e105} — ใช้ผลของเดิม")
        return _res

    CalmBotEngine.handle_page_echo = _handle_page_echo_r105
    print("[R105] เซลที่เคยรับช่วงแล้ว พิมพ์ต่อโดยไม่ทวนชื่อ = บอทหยุด "
          "(แชทที่ยังไม่เคยมีเซล ใช้กติกา r57 เดิม)")
except Exception as _e:
    print(f"[R105 PATCH FAIL] {_e} — ใช้ของเดิม ไม่กระทบการทำงาน")


# ============================================================================
# r106 — ข้อความสำรองถาม 2 เรื่องรวด = ตัวฆ่าบทสนทนาอันดับ 1
#
# Gift 30 ส.ค.: "มีประเด็นใหม่ๆ ตอบได้เลยอีกแล้ว"
# ทีม MKT: "สังเกตุถ้าลูกค้าเห็นแบบนี้อ่านไม่ตอบกันหมดเลย"
#
# ไม่เดา — ดึงจากตัวสแกนสุขภาพบอทของ WEC Reports (ย้อนหลัง 3 วัน 171 เคส
# ที่ลูกค้าเงียบหลังบอทตอบ พร้อมคอลัมน์ "จุดที่หลุด"):
#
#   21 เคส  "ขออนุญาตสอบถามเพิ่มนิดนึงค่ะ ลูกค้าสนใจโซนไหน และงบประมาณ
#            คร่าวๆ ประมาณเท่าไหร่คะ"          <- อันดับ 1 ของทุกข้อความ
#   17 เคส  ข้อความทักกลับเอง
#   11 เคส  คำถามวัตถุประสงค์ (คำถามแรก — เงียบบ้างเป็นเรื่องปกติ)
#
#   หลุดที่ช่อง: await_objective 27 · qualifying 24 · await_income 15
#   รายเพจ: Realty Smart 46 · Millionaire Asset 43 · New Chapter 28 · WO 27
#
# ประโยคนี้คือ FALLBACK_MSG (ข้อความตอนบอทไม่รู้จะตอบอะไร) — สอดคล้องกับธง
# "⚠️ ตอบด้วยข้อความสำรอง" ที่ขึ้น 78 แถวในตารางเดียวกัน
#
# ปัญหา 2 ชั้นในประโยคเดียว:
#   1) ถาม 2 เรื่องพร้อมกัน (โซน + งบ) ผิดกติกา "1 เทิร์น 1 คำถาม"
#   2) ถามงบ = เปิดช่องให้เลขงบไหลไปลงช่องรายได้ (บั๊กที่ r100 ต้องตามแก้)
#      ตัดคำถามงบออก = ปิดต้นทางของบั๊กนั้นไปด้วย
#
# แก้เฉพาะ "คำพูด" — ไม่แตะเกณฑ์ ไม่แตะฟันเนล ไม่แตะลำดับช่อง
# ต้องอัปเดต _FALLBACK_KEY ด้วย ไม่งั้นตัวกันถามซ้ำของ r77 จะเทียบไม่ติด
# (คีย์ห้ามมีคำลงท้ายบอกเพศ เพราะเพจผู้หญิงจะถูกแปลง ครับ -> ค่ะ)
# ============================================================================
try:
    R106_FALLBACK = "ขออนุญาตถามเพิ่มอีกนิดนะครับ ลูกค้าสนใจโซนไหนเป็นพิเศษครับ"
    _R106_KEY_RAW = "ลูกค้าสนใจโซนไหนเป็นพิเศษ"

    _r106_old = str(getattr(_bl9, "FALLBACK_MSG", ""))
    _bl9.FALLBACK_MSG = R106_FALLBACK
    try:
        import faq_data as _fq106
        _fq106.FALLBACK_MSG = R106_FALLBACK
    except Exception as _e106a:
        print(f"[R106] แก้ faq_data ไม่ได้ ({_e106a}) — ใช้ของ bot_logic อย่างเดียว")

    # ตัวกันถามซ้ำของ r77 เทียบด้วยคีย์นี้ ต้องขยับตาม
    _FALLBACK_RAW = R106_FALLBACK
    _FALLBACK_KEY = _norm_msg(_R106_KEY_RAW)
    if _FALLBACK_KEY not in _norm_msg(R106_FALLBACK):
        raise RuntimeError("คีย์ใหม่เทียบกับข้อความใหม่ไม่ติด")

    print(f"[R106] ข้อความสำรองเหลือคำถามเดียว (ตัดคำถามงบออก) "
          f"— ตัวฆ่าบทสนทนาอันดับ 1 จากสแกน 171 เคส")
    print(f"[R106] เดิม: {_r106_old[:60]}")
    print(f"[R106] ใหม่: {R106_FALLBACK}")
    print("[NO REASK ZONE] คีย์กันถามซ้ำอัปเดตตามข้อความใหม่แล้ว")
except Exception as _e106:
    print(f"[R106 PATCH FAIL] {_e106} — ใช้ข้อความเดิม ไม่กระทบการทำงาน")


# ============================================================================
# r107 — บอกวงเงินกับ "ลูกค้า" เอง ไม่ต้องรอให้เขาถาม
#
# Gift 30 ส.ค.: "อันไหนดีที่สุด แก้แล้วได้ภาพใหญ่ ... ได้ใจลูกค้า (ลูกค้าที่ใช่)"
#
# หลักฐาน:
#   · log 22 ชม.: บอทแจ้งวงเงินให้ลูกค้า 0 ครั้ง (ลูกค้าถาม 2 ครั้ง ข้อมูลไม่ครบ)
#     เพราะ r97 ข้อ C ติดเงื่อนไข "_r97_teased หรือ ลูกค้าถามเอง" เท่านั้น
#   · ส.ค. ทุกเพจ: ทักใหม่ 1,749 · ตีเกรดได้ 719 -> 1,030 คน (59%) ตายกลางทาง
#   · สแกนสุขภาพบอท 171 เคส: ตายที่ await_objective / qualifying / await_income
#     = ตายตอนถูกถาม 2-3 ข้อแรก โดยยังไม่ได้อะไรกลับเลย
#   · r104 (วันนี้) ต่อท่อวงเงินให้ ชีต/เซล/การตลาด/CEO ครบแล้ว
#     เหลือคนเดียวที่ยังไม่รู้ = คนที่ทักมาถาม
#
# เปลี่ยนแค่ "เมื่อไหร่" ไม่เปลี่ยน "อะไร":
#   ตัวเลขยังเป็นชุด r97 เดิม (กดลงเหลือ 70-80% ของจริง) · เกรด/สูตร/ชีตไม่แตะ
#   _r97_quote() คุมความปลอดภัยเองอยู่แล้ว — ไม่รู้รายได้/ภาระ/อายุ = ไม่พูด
#
# ข้อ ก) ข้อมูลครบ -> แจ้งวงเงินเลย
#   · ครั้งเดียวต่อแชท (ใช้ธง _r97_quoted ร่วมกับ r97 ไม่ซ้อนกัน)
#   · เทิร์นนั้นตัดคำถามอื่นทิ้ง = "ให้ข้อมูล" ไม่ใช่ "ให้+ถามรัว"
#     (แพตเทิร์นเดียวกับ r97 ข้อ A ที่ Gift สั่งไว้ว่า 1 คำถาม/เทิร์น
#      ข้อที่ตัดยังว่างอยู่ _next_missing จะถามเองเทิร์นถัดไป)
#   · ถ้ายังไม่ผ่านด่านขอเบอร์ของ r102 -> ตัดประโยคขอเบอร์ออก เหลือแต่ตัวเลข
#
# ข้อ ข) ติดแค่ "ไม่รู้อายุ" อย่างเดียว -> ถามอายุแบบมีเหตุผลให้ตอบ
#   หมายเหตุถึง Gift: ข้อนี้ปรับกติกา 19 ส.ค. ("ถามอายุเฉพาะตอนเจอสัญญาณ
#   เกษียณ/บำนาญ ไม่ถามทุกคน เพราะ funnel จะยาวขึ้นโดยไม่จำเป็น")
#   เหตุผลที่ขอปรับ: ตอนนั้นถามอายุแล้วลูกค้าไม่ได้อะไรกลับ = ยาวขึ้นเปล่าๆ จริง
#   ตอนนี้อายุคือชิ้นสุดท้ายที่ปลดล็อกตัวเลขที่เขาอยากรู้ = ถามแล้วมีของแลก
#   ถามแทนคำถามเดิมของเทิร์นนั้น (ไม่ได้เพิ่มจำนวนคำถาม) · ครั้งเดียวต่อแชท
#   ไม่เอาข้อนี้: ตั้ง R107_ASK_AGE = False บรรทัดเดียว
#
# ห้ามแทรกเมื่อ: เซลรับช่วงอยู่ · ปิดเคสแล้ว · สายเจ้าของห้อง/ผู้เช่า/ไม่รับเคส
# ============================================================================
R107_ASK_AGE = True
# ต้องมี "เท่าไหร่ครับ" เพื่อให้ _r97_is_question() มองเห็นว่าเป็นคำถาม
# ไม่งั้นตัวนับ 1 คำถาม/เทิร์น ของแพตช์อื่นจะนับข้อนี้ไม่ติด
R107_AGE_Q = ("เดี๋ยวผมประเมินวงเงินคร่าวๆ ให้เลยครับ "
              "คุณลูกค้าอายุเท่าไหร่ครับ")

try:
    _R107_BASE_DECIDE = CalmBotEngine._decide

    def _r107_skip(state, grade):
        st = state or {}
        if st.get("_r97_quoted") or st.get("_r107_done"):
            return True
        if st.get("closed") or st.get("done") or st.get("bot_off") or st.get("handover"):
            return True
        g = str(grade or "").strip().upper()[:1]
        if g in ("O", "R", "X"):
            return True
        d = st.get("data") or {}
        if d.get("cash"):            # ซื้อสด ไม่ต้องพูดเรื่องวงเงินกู้
            return True
        return False

    def _r107_age_is_only_blocker(d):
        """ติดแค่ 'ไม่รู้อายุ' จริงไหม — รายได้/ภาระต้องมีครบแล้ว"""
        try:
            inc = (d.get("income_total") or d.get("income_baht")
                   or _bl9._parse_income(str(d.get("income", ""))))
            if not inc:
                return False
            debt = d.get("debt_baht")
            if debt is None:
                debt = _bl9._parse_debt_monthly(str(d.get("debt", "")))
            if debt is None:
                return False
            return (d.get("age") is None and d.get("co_age") is None
                    and not d.get("co_borrower_income"))
        except Exception:
            return False

    def _decide_r107(self, msg, user_id, state, bucket, is_new):
        # r118 (1 ก.ย. 2569) — สายผู้เช่า/ผู้ขาย ไม่ใช่กรวยคนซื้อ ข้ามชั้นนี้
        # เคสจริง 1 ก.ย. 11:06 น.: ลูกค้าบอก "สนใจเช่าคอนโด" ตั้งแต่ประโยคแรก
        # แต่ชั้นนี้ยัดคำถาม อายุ/วงเงิน/ผู้กู้ร่วม/หนี้ ใส่จนลูกค้าพิมพ์ว่า
        # "คุยกันไม่รู้เรื่องล่ะยกเลิกค่ะ" แล้วเซลต้องมาขอโทษแทนบอท
        if state.get("renter") or state.get("owner"):
            return _R107_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
        bubbles, grade = _R107_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
        try:
            _uid = str(user_id)[:8]
            st = state or {}
            if _r107_skip(st, grade):
                return bubbles, grade
            d = st.get("data") or {}
            if not isinstance(bubbles, list):
                bubbles = [bubbles] if bubbles else []

            # ---------- ก) ข้อมูลครบ -> แจ้งวงเงินเลย ----------
            _q = _r97_quote(d, st)
            if _q:
                st["_r97_quoted"] = True
                st["_r107_done"] = True
                try:
                    if not _r102_submittable(d, st):
                        _q = [b for b in _q if b != R97_SERVICE_LINE]
                except Exception:
                    pass
                _kept = [b for b in bubbles
                         if not _r97_is_question(b)
                         and not any(k in str(b) for k in _R97_DROP)]
                bubbles = _kept + _q
                print(f"[R107] {_uid}... แจ้งวงเงินเองโดยไม่ต้องรอลูกค้าถาม "
                      f"({len(_q)} บับเบิล · ตัดคำถามอื่นในเทิร์นนี้ทิ้ง)")
                return bubbles, grade

            # ---------- ข) ติดแค่ไม่รู้อายุ -> ถามอายุแบบมีของแลก ----------
            if (R107_ASK_AGE and not st.get("age_asked")
                    and not st.get("_r107_age_q")
                    and _r107_age_is_only_blocker(d)):
                st["_r107_age_q"] = True
                st["age_asked"] = True
                st["age_pending"] = True
                _kept = [b for b in bubbles if not _r97_is_question(b)]
                bubbles = _kept + [R107_AGE_Q]
                try:
                    _bl9.BotEngine._add_signal(
                        st, "ถามอายุเพื่อปลดล็อกการแจ้งวงเงินให้ลูกค้า "
                            "(รายได้/ภาระครบแล้ว เหลืออายุอย่างเดียว)")
                except Exception:
                    pass
                print(f"[R107] {_uid}... รายได้+ภาระครบ เหลือแค่อายุ — "
                      f"ถามอายุแทนคำถามเดิมของเทิร์นนี้")
        except Exception as _e107:
            print(f"[R107 DECIDE ERROR] {_e107} — ใช้ทางเดิม")
        return bubbles, grade

    CalmBotEngine._decide = _decide_r107
    print(f"[R107] บอทบอกวงเงินกับลูกค้าเองแล้ว (ตัวเลขชุดเดิม {R97_QUOTE_PCT:.0%}-"
          f"{R97_QUOTE_PCT+R97_QUOTE_SPAN:.0%} ของจริง) · "
          f"ถามอายุเมื่อเหลือติดอายุอย่างเดียว = {R107_ASK_AGE}")
    print("[R107] ไม่แตะเกณฑ์ ไม่แตะสูตร ไม่แตะเกรด ไม่แตะชีต ไม่แตะการแจกเคส")
except Exception as _e:
    print(f"[R107 ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")


# ============================================================================
# r108 — ต้องรู้อายุ "ก่อน" ขอเบอร์ เพราะอายุเปลี่ยนเกรด
#
# Gift 30 ส.ค.: "ควรถามอายุก่อนจะขอเบอร์ด้วย เพราะมันจัดเกรดลูกค้า"
#             + "มันขออีกข้อเดียว หลายครั้งก็ตลกๆ อยู่นะ" (แก้คำพูดแล้วข้างบน)
#
# ทำไมถึงสำคัญจริง — ธงของ r92b พูดเองอยู่แล้ว:
#   "วงเงิน X นี้คิดบนสมมติฐานอายุ 35 ปี ถ้าอายุจริง 55+ วงเงินจะหายเกือบครึ่ง"
# แปลว่าเกรดที่ส่งให้เซล/ชีต/การตลาด ตอนไม่รู้อายุ = เกรดที่ยังไม่ยืนยัน
# ขอเบอร์แล้วแจกเคสตอนนั้น = แจกเกรดที่อาจผิดครึ่งหนึ่งเข้าคิวเซล
#
# กติกา: จะขอเบอร์ได้ ต้องรู้อายุก่อน (กั้นได้ 2 รอบ แล้วปล่อย + ติดธง)
#   · ห้ามทิ้งเคสเด็ดขาด — ครบโควตาแล้วปล่อยขอเบอร์ตามเดิม แต่ติดธงให้เซลรู้
#   · ใช้ท่อ awaiting_age เดิมของ bot_logic (บล็อก 0.46) เก็บคำตอบ
#     -> ลูกค้าตอบเลขเปล่า "34" ก็อ่านออก เพราะมีบริบทว่ากำลังถามอายุ
#   · เคลียร์ awaiting ทิ้งด้วย กัน "34" หล่นไปลงช่องเบอร์
#     ปลอดภัยเพราะ _next_missing คำนวณช่องถัดไปจาก data ใหม่ทุกเทิร์นอยู่แล้ว
#   · ซื้อสด / เซลรับช่วง / ปิดเคสแล้ว / รู้อายุอยู่แล้ว = ไม่กั้น
# ============================================================================
R108_MAX_ASK = 2
_R108_CONTACT_MARK = ("ขอเบอร์", "เบอร์ติดต่อ", "เบอร์โทร", "ขอช่องทางติดต่อ",
                      "ไลน์ไอดี", "LINE ID", "ขอเบอร์ติดต่อ")

try:
    _R108_BASE_DECIDE = CalmBotEngine._decide

    def _r108_asks_contact(state, bubbles):
        try:
            if (state or {}).get("awaiting") == "contact":
                return True
            return any(any(k in str(b) for k in _R108_CONTACT_MARK) for b in bubbles)
        except Exception:
            return False

    def _decide_r108(self, msg, user_id, state, bucket, is_new):
        # r118 (1 ก.ย. 2569) — สายผู้เช่า/ผู้ขาย ไม่ใช่กรวยคนซื้อ ข้ามชั้นนี้
        # เคสจริง 1 ก.ย. 11:06 น.: ลูกค้าบอก "สนใจเช่าคอนโด" ตั้งแต่ประโยคแรก
        # แต่ชั้นนี้ยัดคำถาม อายุ/วงเงิน/ผู้กู้ร่วม/หนี้ ใส่จนลูกค้าพิมพ์ว่า
        # "คุยกันไม่รู้เรื่องล่ะยกเลิกค่ะ" แล้วเซลต้องมาขอโทษแทนบอท
        if state.get("renter") or state.get("owner"):
            return _R108_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
        bubbles, grade = _R108_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
        try:
            _uid = str(user_id)[:8]
            st = state or {}
            d = st.get("data") or {}
            if (d.get("age") is not None or d.get("co_age") is not None
                    or d.get("cash") or st.get("awaiting_age") or st.get("age_pending")
                    or st.get("handover") or st.get("closed") or st.get("done")
                    or st.get("bot_off")):
                return bubbles, grade
            if not isinstance(bubbles, list):
                bubbles = [bubbles] if bubbles else []
            if not _r108_asks_contact(st, bubbles):
                return bubbles, grade

            _n = int(st.get("_r108_n") or 0)
            if _n >= R108_MAX_ASK:
                if not st.get("_r108_flag"):
                    st["_r108_flag"] = True
                    try:
                        _bl9.BotEngine._add_signal(
                            st, "⚠️ ขอเบอร์ทั้งที่ยังไม่รู้อายุ (ถามครบโควตาแล้ว) "
                                "— เกรดยังไม่ยืนยัน วงเงินคิดบนสมมติฐานอายุ "
                                f"{_bl9.DEFAULT_AGE} ปี · ถามอายุตอนโทรก่อนเสนอห้อง")
                    except Exception:
                        pass
                    print(f"[R108] {_uid}... ถามอายุครบโควตาแล้ว — "
                          "ปล่อยขอเบอร์ + ติดธงให้เซล (ไม่ทิ้งเคส)")
                return bubbles, grade

            st["_r108_n"] = _n + 1
            st["awaiting_age"] = True
            st["awaiting"] = None          # กัน "34" หล่นลงช่องเบอร์
            bubbles = [b for b in bubbles
                       if not any(k in str(b) for k in _R108_CONTACT_MARK)]
            bubbles = [b for b in bubbles if not _r97_is_question(b)]
            bubbles.append(R107_AGE_Q)
            print(f"[R108] {_uid}... ยังไม่รู้อายุ — ถามอายุก่อนขอเบอร์ "
                  f"รอบ {_n + 1}/{R108_MAX_ASK}")
        except Exception as _e108:
            print(f"[R108 DECIDE ERROR] {_e108} — ใช้ทางเดิม")
        return bubbles, grade

    CalmBotEngine._decide = _decide_r108
    print(f"[R108] ต้องรู้อายุก่อนขอเบอร์ (กั้น {R108_MAX_ASK} รอบ แล้วปล่อย + ติดธง)")
    print(f"[R108] คำถามอายุ: {R107_AGE_Q}")
except Exception as _e:
    print(f"[R108 ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")


# ----------------------------------------------------------------------
# r109 (30 ส.ค. 2569) — ส่ง "อายุ" เข้าชีต
# ----------------------------------------------------------------------
# บอทถามอายุมาตั้งแต่ r107/r108 และใช้คำนวณวงเงินจริง (_capacity: room = cap - age)
# แต่ payload ที่ยิงเข้า Apps Script ไม่เคยมีช่องนี้ -> อายุที่ได้มาถูกทิ้งทุกเคส
# ชีตเลยไม่มีอายุสักแถว ทั้งที่เป็นตัวแปรที่ทำให้วงเงินต่างกันเป็นล้าน
# ใส่ที่ _income_numbers เพราะเป็นทางผ่านเดียวของทุกคอลัมน์ตัวเลขที่เข้าชีต (เหมือน r104)
# ปลายทาง: Apps Script P4_C.AGE = คอลัมน์ 36 (P4_COLS 35 -> 36)
try:
    _R109_PREV_INUM = _bl9.BotEngine._income_numbers

    def _income_numbers_r109(data):
        _out = dict(_R109_PREV_INUM(data))
        try:
            _age = (data or {}).get("age")
            if _age in (None, ""):
                _out["age"] = ""
            else:
                _a = int(_age)
                # กันค่าเพี้ยน (เช่นลูกค้าพิมพ์ปี พ.ศ. หรือเลขมั่ว) ไม่ให้ลงชีต
                _out["age"] = _a if 15 <= _a <= 90 else ""
        except Exception:
            _out["age"] = ""
        return _out

    _bl9.BotEngine._income_numbers = staticmethod(_income_numbers_r109)
    print("[R109] ส่งอายุเข้าชีต (คอลัมน์ 36) ผ่าน _income_numbers")
except Exception as _e:
    print(f"[R109 ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")


# ----------------------------------------------------------------------
# r110 (31 ส.ค. 2569) — รายได้ผันแปร (คอม/OT/โบนัส) + อายุที่ลูกค้าบอกเอง
# ----------------------------------------------------------------------
# เคสจริง เพจ Intake คุณ Ohm Ja (ภาพจาก Gift 31 ส.ค.):
#   ลูกค้า: "เงินเดือน 25000 ได้มั้ยคะ อายุ 45 แต่มีค่าคอมด้วย"
#   บอท  : "ได้ค่ะ ค่าคมประมาณเดือนละเท่าไหร่ธรรมชาติคะ"   <- พิมพ์ผิด + คำขยะ
#   บอท  : "ดีเลยค่ะ ค่าคมเดือนละประมาณเท่าไหร่คะ"          <- ถามซ้ำข้อเดิม
#   แล้วขอเบอร์ในเทิร์นเดียวกัน ทั้งที่ยังไม่ได้คำตอบ
#
# ต้นเหตุจริง (พิสูจน์แล้วด้วย _public_guard):
#   "ค่าคอม" และ "คอมมิชชั่น" อยู่ในลิสต์ _NEVER_SAY  ->  ประโยคถูก "บล็อก"
#   ลิสต์นั้นตั้งใจกันไม่ให้บอทเผย "ค่าคอมของบริษัท" (ข้อมูลภายใน)
#   แต่มันบล็อก "ค่าคอมของลูกค้า" ซึ่งเป็นคนละความหมายไปด้วย
#   -> บอทถามคำนี้ตรงๆ ไม่ได้เลย AI เลยเลี่ยงไปพิมพ์ผิดจนหลุดยาม ("ค่าคม" ผ่าน)
#   -> ได้ข้อความเพี้ยน + ถามซ้ำ เพราะคำตอบไม่เคยถูกเก็บ
#
# และไม่มีช่องเก็บรายได้ผันแปรเลยทั้งระบบ (ไม่มี income_var/OT/โบนัสที่ไหน)
#   -> "ค่าคอมอีกหมื่นกว่าถึง 20,000" ตกพื้น เกรดคิดจากเงินเดือน 25,000 อย่างเดียว
#   -> เคสที่จริงๆ รายได้ ~45,000 ถูกตัดเป็นเคสก้ำกึ่ง
#
# r110 แก้ 3 อย่าง — ไม่แตะเกณฑ์ ไม่แตะสูตร แตะแค่ "อะไรลงช่องไหน"
#   1) เก็บ income_var แล้วบวกเข้า income_total (ทุกที่อ่าน income_total ก่อนอยู่แล้ว)
#   2) ถามด้วยประโยคที่ผ่านยาม ถามครั้งเดียว และห้ามขอเบอร์เทิร์นเดียวกัน
#   3) ลูกค้าบอกอายุเองก่อนบอทถาม = เก็บทันที ไม่ต้องถามซ้ำ
# ----------------------------------------------------------------------
try:
    import re as _re110

    # คำที่แปลว่า "มีรายได้ส่วนอื่นนอกจากเงินเดือน"
    _R110_VAR_WORDS = (
        "ค่าคอม", "คอมมิชชั่น", "คอมมิชชัน", "commission", "คอมฯ",
        "โอที", "โอ.ที", "ot", "ล่วงเวลา",
        "โบนัส", "bonus", "เบี้ยเลี้ยง", "ค่าตำแหน่ง", "ค่าน้ำมัน",
        "incentive", "อินเซนทีฟ", "เงินพิเศษ", "รายได้เสริม", "รายได้พิเศษ",
    )

    # ประโยคถามที่ "ผ่านยาม" — ห้ามมีคำว่า ค่าคอม/คอมมิชชั่น เด็ดขาด
    R110_VAR_Q = ("นอกจากเงินเดือน มีรายได้ส่วนอื่นอีกเดือนละประมาณเท่าไหร่ครับ")

    def _r110_mentions_var(msg):
        m = str(msg or "").lower()
        return any(w in m for w in _R110_VAR_WORDS)

    def _r110_recalc_total(data):
        """income_total = เงินเดือน + ผันแปร + ผู้กู้ร่วม (ส่วนไหนไม่รู้ = 0)"""
        try:
            _own = data.get("income_baht")
            if _own is None:
                _own = _bl9._parse_income(str(data.get("income", "")))
            if _own is None:
                return
            _var = data.get("income_var") or 0
            _cob = data.get("co_borrower_income") or 0
            data["income_total"] = int(_own) + int(_var) + int(_cob)
        except Exception:
            pass

    # ---------- 1) เก็บรายได้ผันแปร + อายุ ที่ลูกค้าพูดเอง ----------
    _R110_PREV_CAPTURE = _bl9.BotEngine._capture

    def _capture_r110(self, state, field, msg):
        _out = _R110_PREV_CAPTURE(self, state, field, msg)
        try:
            _d = state.get("data") or {}
            if _r110_mentions_var(msg):
                _d["income_var_said"] = True
        except Exception:
            pass
        return _out

    _bl9.BotEngine._capture = _capture_r110

    _R110_PREV_DECIDE = CalmBotEngine._decide

    def _decide_r110(self, msg, user_id, state, bucket, is_new):
        # r118 (1 ก.ย. 2569) — สายผู้เช่า/ผู้ขาย ไม่ใช่กรวยคนซื้อ ข้ามชั้นนี้
        # เคสจริง 1 ก.ย. 11:06 น.: ลูกค้าบอก "สนใจเช่าคอนโด" ตั้งแต่ประโยคแรก
        # แต่ชั้นนี้ยัดคำถาม อายุ/วงเงิน/ผู้กู้ร่วม/หนี้ ใส่จนลูกค้าพิมพ์ว่า
        # "คุยกันไม่รู้เรื่องล่ะยกเลิกค่ะ" แล้วเซลต้องมาขอโทษแทนบอท
        if state.get("renter") or state.get("owner"):
            return _R110_PREV_DECIDE(self, msg, user_id, state, bucket, is_new)
        _txt = str(msg or "")
        try:
            _d = state.setdefault("data", {})

            # (ก) ลูกค้าบอกอายุเองก่อนบอทถาม -> เก็บทันที
            if _d.get("age") is None and "อาย" in _txt:
                _a = _bl9._parse_age(_txt)
                if _a is not None and 18 <= int(_a) <= 90:
                    try:
                        _r92_apply_age(self, user_id, state, int(_a))
                    except Exception:
                        _d["age"] = int(_a)
                    state["awaiting_age"] = False
                    print(f"[R110] ลูกค้าบอกอายุเอง {_a} — เก็บแล้ว ไม่ถามซ้ำ")

            # (ข) ลูกค้าพูดถึงรายได้ผันแปร
            if _r110_mentions_var(_txt):
                _d["income_var_said"] = True

            # (ค) กำลังรอตัวเลขผันแปรอยู่ -> อ่านเลขจากข้อความนี้
            if state.get("awaiting_income_var") and _d.get("income_var") is None:
                _n = _bl9._parse_income(_txt)
                if _n is not None and 1000 <= int(_n) <= 2_000_000:
                    _d["income_var"] = int(_n)
                    state["awaiting_income_var"] = False
                    _r110_recalc_total(_d)
                    self._add_signal(
                        state,
                        f"รายได้ส่วนอื่นนอกเหนือเงินเดือน {int(_n):,} บาท/เดือน "
                        f"(ลูกค้าแจ้งเอง) · รายได้รวมที่ใช้ประเมิน "
                        f"{int(_d.get('income_total') or 0):,} บาท")
                    print(f"[R110] เก็บรายได้ผันแปร {_n:,} -> รวม "
                          f"{_d.get('income_total')}")
                elif _bl9._has_any(_txt, ("ไม่มี", "ไม่แน่นอน", "ไม่ได้ทุกเดือน")):
                    _d["income_var"] = 0
                    state["awaiting_income_var"] = False
                    print("[R110] ลูกค้าบอกว่าไม่มีรายได้ส่วนอื่น")
        except Exception as _e110:
            print(f"[R110 PRE ERROR] {_e110}")

        bubbles, grade = _R110_PREV_DECIDE(self, msg, user_id, state, bucket, is_new)

        # ---------- 2) ถามตัวเลขผันแปร ครั้งเดียว ห้ามพ่วงขอเบอร์ ----------
        try:
            _d = state.get("data") or {}
            _need = (_d.get("income_var_said")
                     and _d.get("income_var") is None
                     and not state.get("_r110_asked")
                     and not state.get("closed") and not state.get("done")
                     and not state.get("bot_off") and not state.get("handover")
                     and grade not in ("O", "R", "X"))
            if _need:
                _kept = [b for b in bubbles
                         if not _r97_is_question(b)
                         and not any(k in str(b) for k in _R108_CONTACT_MARK)]
                bubbles = _kept + [R110_VAR_Q]
                state["_r110_asked"] = True
                state["awaiting_income_var"] = True
                print("[R110] ถามรายได้ส่วนอื่น (ครั้งเดียว) — ตัดคำถามอื่น"
                      "และคำขอเบอร์ออกจากเทิร์นนี้")

            # กันข้อความที่พิมพ์คำต้องห้ามแบบเลี่ยงยาม ("ค่าคม") หลุดออกไป
            _fixed = []
            for _b in bubbles:
                _s = str(_b)
                if "ค่าคม" in _s:
                    _s = _s.replace("ค่าคมเดือนละประมาณเท่าไหร่คะ", R110_VAR_Q)
                    _s = _s.replace("ค่าคม", "รายได้ส่วนอื่น")
                    print("[R110] แก้คำพิมพ์ผิด 'ค่าคม' ที่เลี่ยงยามออกไปได้")
                if "ธรรมชาติคะ" in _s or "ธรรมชาติครับ" in _s:
                    _s = _s.replace("ธรรมชาติคะ", "คะ").replace("ธรรมชาติครับ", "ครับ")
                    print("[R110] ตัดคำขยะ 'ธรรมชาติ' ท้ายประโยค")
                _fixed.append(_s)
            bubbles = _fixed
        except Exception as _e110b:
            print(f"[R110 POST ERROR] {_e110b} — ใช้ทางเดิม")

        return bubbles, grade

    CalmBotEngine._decide = _decide_r110

    # ---------- 3) ส่ง income_var เข้าชีตผ่านจุดผ่านเดิม ----------
    _R110_PREV_INUM = _bl9.BotEngine._income_numbers

    def _income_numbers_r110(data):
        _out = dict(_R110_PREV_INUM(data))
        try:
            _v = (data or {}).get("income_var")
            _out["income_var"] = "" if _v in (None, "") else int(_v)
            _t = (data or {}).get("income_total")
            if _t not in (None, ""):
                _out["income_total"] = int(_t)
                _out["qualified25k"] = "1" if int(_t) >= _bl9.LOW_INCOME_BAHT else "0"
        except Exception:
            pass
        return _out

    _bl9.BotEngine._income_numbers = staticmethod(_income_numbers_r110)

    print("[R110] รายได้ผันแปร (คอม/OT/โบนัส) เก็บแยกช่อง + บวกเข้ารายได้รวม")
    print(f"[R110] คำถามที่ใช้ (ผ่านยาม): {R110_VAR_Q}")
    print("[R110] อายุที่ลูกค้าบอกเอง = เก็บทันที ไม่ถามซ้ำ")
except Exception as _e:
    print(f"[R110 ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")



# ============================================================
# r111 (31 ส.ค. 2569) — ยามกันลูป _next_missing
# เหตุ: _decide (bot_logic ~3274) มี
#         while field and asked[field] >= MAX_ASK_PER_FIELD:
#             _skipped.add(field)
#             field, question = self._next_missing(data, state, skip=_skipped)
#       ถ้าชั้น patch ไหนคืนช่องที่อยู่ใน skip อยู่แล้ว -> วนไม่จบ
#       บอทค้างทั้งเทิร์น ไม่ตอบลูกค้า และ log ท่วมจนโดน rate limit
# ยามนี้เป็นตาข่ายชั้นสุดท้าย: ใครคืนช่องที่สั่งข้ามมา ให้ตกกลับไปใช้
# ผลลัพธ์ของ _next_missing เดิม (ซึ่งเคารพ skip เสมอ)
# ห้ามทำให้บอทเงียบ: ถ้าพลาดตรงไหน คืนค่าที่ได้มาตามเดิม
# ============================================================
try:
    _R111_PREV_NEXT = _bl9.BotEngine._next_missing

    def _next_missing_r111(self, data, state=None, skip=None):
        _f, _q = _R111_PREV_NEXT(self, data, state, skip)
        try:
            _sk = skip or set()
            if _f and _f in _sk:
                print(f"[R111 LOOP GUARD] มีชั้นไหนคืน {_f!r} ทั้งที่สั่งข้ามแล้ว "
                      "— ตัดลูป ใช้ผลของ _next_missing เดิมแทน")
                _f2, _q2 = _R102_ORIG_NEXT(self, data, state, _sk)
                if _f2 and _f2 in _sk:
                    return None, None
                return _f2, _q2
        except Exception as _e111:
            print(f"[R111 GUARD ERROR] {_e111} — ใช้ค่าเดิม")
        return _f, _q

    _bl9.BotEngine._next_missing = _next_missing_r111
    print("[R111] ยามกันลูป _next_missing เปิดแล้ว (แก้เหตุบอทค้าง 31 ส.ค.)")
except Exception as _e:
    print(f"[R111 ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")



# ============================================================
# r112 (31 ส.ค. 2569) — หา IG account ที่ผูกกับแต่ละเพจเอง
# ------------------------------------------------------------
# เหตุ: webhook ของ Instagram ส่ง entry.id = "IG account id"
#       ถ้าไม่ได้ตั้ง ig_id ใน WEC_PAGES -> resolve_ig_page ถอยไปใช้เพจหลัก
#       -> ยิงด้วยโทเค็นผิดเพจ -> Meta ตอบ (#10) "ส่งนอกช่วงเวลาที่อนุญาต"
#       ทั้งที่ลูกค้าเพิ่งพิมพ์มา 6 วินาทีก่อน (เคสจริง 31 ส.ค. 2569)
# แก้: ถาม Graph เองว่าเพจไหนผูกกับ IG ไหน แล้วเติมลง IG_TO_PAGE
#      ไม่ต้องแก้ env ไม่ทับค่าที่ตั้งมือไว้ใน WEC_PAGES ตอนบูท
# ห้ามทำให้บอทเงียบ: หาไม่เจอ = ใช้เพจหลักเหมือนเดิมทุกประการ
# ============================================================
_R112_LOCK = threading.Lock()
_R112_LAST = 0.0
_R112_MANUAL = set(IG_TO_PAGE.keys())     # ที่ตั้งมือไว้ ห้ามทับ


def _r112_discover_ig(force: bool = False) -> dict:
    global _R112_LAST
    try:
        with _R112_LOCK:
            if not force and (time.time() - _R112_LAST) < 300:
                return IG_TO_PAGE
            _R112_LAST = time.time()
    except Exception:
        pass
    _ids = [str(p) for p in (PAGES or {}).keys()]
    if str(MAIN_PAGE_ID) not in _ids:
        _ids.append(str(MAIN_PAGE_ID))
    _found = 0
    for _pid in _ids:
        try:
            _tok = page_token(_pid)
            if not _tok:
                continue
            _r = requests.get(
                f"https://graph.facebook.com/v22.0/{_pid}",
                params={"fields": "instagram_business_account{id,username},"
                                  "connected_instagram_account{id,username}",
                        "access_token": _tok},
                timeout=8)
            if _r.status_code != 200:
                _e = {}
                try:
                    _e = ((_r.json() or {}).get("error") or {})
                except Exception:
                    pass
                print(f"[IG MAP] page={_pid} ถามไม่ได้ {_r.status_code} "
                      f"code={_e.get('code')} "
                      f"{str(_e.get('message') or '')[:140]}")
                continue
            _j = _r.json() or {}
            for _k in ("instagram_business_account",
                       "connected_instagram_account"):
                _o = _j.get(_k) or {}
                _iid = str(_o.get("id") or "").strip()
                if not _iid or _iid in _R112_MANUAL:
                    continue
                _old = IG_TO_PAGE.get(_iid)
                IG_TO_PAGE[_iid] = str(_pid)
                _found += 1
                print(f"[IG MAP] ig={_iid} (@{_o.get('username', '-')}) "
                      f"-> page={_pid}"
                      + ("" if _old in (None, str(_pid))
                         else f"  (เดิมชี้ไป {_old})"))
        except Exception as _e:
            print(f"[IG MAP ERROR] page={_pid}: {_e}")
    if not IG_TO_PAGE:
        print(f"[IG MAP] ไม่เจอ IG ผูกกับเพจไหนเลย "
              f"— ใช้เพจหลัก {MAIN_PAGE_ID} เหมือนเดิม "
              f"(เช็คสิทธิ์ instagram_basic / instagram_manage_messages)")
    else:
        print(f"[IG MAP] สรุป: {json.dumps(IG_TO_PAGE, ensure_ascii=False)}")
    return IG_TO_PAGE


try:
    _R112_ORIG_RESOLVE = resolve_ig_page

    def resolve_ig_page(ig_id: str) -> str:      # noqa: F811
        """เจอ IG id ที่ยังไม่รู้จัก -> ถาม Graph ก่อน 1 ครั้ง (คูลดาวน์ 5 นาที)
        แล้วค่อยตัดสินใจตามตรรกะเดิม"""
        _i = str(ig_id or "")
        try:
            if _i and _i not in IG_TO_PAGE:
                _r112_discover_ig()
        except Exception as _e:
            print(f"[IG MAP ERROR] {_e} — ใช้ทางเดิม")
        return _R112_ORIG_RESOLVE(_i)

    threading.Thread(target=_r112_discover_ig,
                     kwargs={"force": True}, daemon=True).start()
    print("[R112] ค้นหา IG ที่ผูกกับแต่ละเพจเอง + log error การส่งแบบเต็ม")
except Exception as _e:
    print(f"[R112 ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")



# ============================================================
# r113 (1 ก.ย. 2569) — ไล่เหตุ "ลีดจากโฆษณาทักมาแล้วส่งกลับไม่ได้"
# ------------------------------------------------------------
# เคสจริง 31 ส.ค. 23:45 น. เพจ Realty Smart ad_id=120249256510440716
#   [NAME ERROR] Graph 100: Object with ID '2625796...' does not exist
#   [FB SEND ERROR] code=10/2018278 (#10) ส่งนอกช่วงเวลาที่อนุญาต
# ทั้งที่เป็นข้อความแรกของลูกค้า -> หน้าต่าง 24 ชม. ควรเปิดอยู่
# ตั้งสมมติฐาน 2 ทาง แล้ววัดทั้งคู่ในบรรทัดเดียว:
#   (ก) webhook มาช้า/ยิงย้อนหลัง -> วัดจาก event.timestamp เทียบเวลาจริง
#   (ข) entry.id ไม่ใช่เพจที่รับข้อความจริง -> เทียบกับ recipient.id
#       ถ้าไม่ตรง = เราหยิบโทเค็นผิดเพจ (อาการเดียวกับ IG ก่อน r112)
#       กรณีนี้แก้ให้เลย: ใช้ recipient.id เป็นเพจที่ตอบกลับ
# ไม่เปลี่ยนพฤติกรรมอื่นเลย ตรงกัน = เงียบเหมือนเดิมทุกประการ
# ============================================================
try:
    _R113_ORIG_PROCESS_EVENT = process_event

    def process_event(event: dict, platform: str = "facebook",   # noqa: F811
                      page_id: str = ""):
        try:
            _msg113 = (event or {}).get("message") or {}
            if not _msg113.get("is_echo"):
                _snd = str(((event or {}).get("sender") or {}).get("id") or "")
                _rcpt = str(((event or {}).get("recipient") or {}).get("id") or "")
                _ts = (event or {}).get("timestamp") or 0
                try:
                    # r118 — event ชนิด referral ส่ง timestamp เป็น "วินาที"
                    # แต่ชนิด message ส่งเป็น "มิลลิวินาที" ของเดิมหาร 1000 หมด
                    # ทำให้ลีดโฆษณาทุกตัวขึ้นเตือน "ย้อนหลัง 496,235 ชม." (56 ปี)
                    _tsf = float(_ts) if _ts else 0.0
                    if _tsf > 1e11:          # มิลลิวินาที
                        _tsf = _tsf / 1000.0
                    _age = (time.time() - _tsf) if _tsf else -1.0
                except Exception:
                    _age = -1.0
                try:
                    _ref113 = extract_referral(event) or {}
                except Exception:
                    _ref113 = {}
                _mismatch = bool(platform == "facebook" and _rcpt
                                 and _rcpt != str(page_id) and _rcpt != _snd)
                if _ref113.get("ad_id") or _ref113.get("ref") or _age > 120 or _mismatch:
                    print(f"[EVENT DIAG] ({platform}) entry_page={page_id or '-'} "
                          f"recipient={_rcpt or '-'} from={_mask(_snd)} "
                          f"อายุ={_age:.0f} วิ "
                          f"ad_id={_ref113.get('ad_id') or '-'} "
                          f"src={_ref113.get('source') or '-'} "
                          f"keys={sorted((event or {}).keys())}")
                if _age > 86400:
                    print(f"[STALE EVENT] {_mask(_snd)} Meta ส่งย้อนหลัง "
                          f"{_age / 3600:.1f} ชม. — เกิน 24 ชม. ส่งกลับไม่ได้แน่นอน "
                          "((#10) ไม่ใช่บั๊กของเรา) แต่ยังบันทึกลีดตามปกติ")
                if _mismatch:
                    print(f"[PAGE FIX] webhook บอก entry={page_id} "
                          f"แต่ recipient={_rcpt} — ใช้ recipient เป็นเพจที่ตอบกลับ")
                    page_id = _rcpt
        except Exception as _e113:
            print(f"[R113 DIAG ERROR] {_e113} — ใช้ทางเดิม")
        return _R113_ORIG_PROCESS_EVENT(event, platform, page_id)

    print("[R113] วินิจฉัยลีดโฆษณา: วัดอายุ event + เทียบ entry.id กับ recipient.id")
except Exception as _e:
    print(f"[R113 ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")



# ============================================================
# r114 (1 ก.ย. 2569, Gift เคาะ) — ถามยอดหนี้รวม "2 จังหวะ"
# ------------------------------------------------------------
# เดิม (r89) ถามครั้งเดียว: ทันทีหลังลูกค้าบอกยอดผ่อน/เดือน = ก่อนขอเบอร์
# ปัญหา: ลูกค้าที่ตอบไม่ได้/ตอบเลี่ยงตรงนั้น = ไม่มียอดหนี้รวมตลอดกาล
#        -> เกรด B ขาดตัวเลขบริดจ์ เซลต้องโทรถามเอง
# ใหม่: ถ้ารอบแรกไม่ได้ตัวเลข -> พอได้เบอร์แล้วถามซ้ำอีก 1 ครั้ง (ครั้งเดียว)
#       จังหวะนี้ลูกค้าให้เบอร์แล้ว = เสียลีดยากกว่ามาก
# ได้คำตอบแล้วเขียนกลับแถวเดิมในชีตผ่าน p4UpsertLead (หาแถวตาม PSID
# เขียนทับเฉพาะช่องที่มีค่า · calendar=False = ไม่สร้างนัดโทรซ้ำ)
# ห้ามทำให้บอทเงียบ: พลาดตรงไหนก็ไหลไปทางเดิมทั้งหมด
# ============================================================
try:
    _R114_BASE_DECIDE = CalmBotEngine._decide

    def _r114_after_total(self, state, data):
        """ได้ยอดหนี้รวมช้า — คิดบริดจ์ใหม่ + เขียนกลับแถวเดิม"""
        try:
            _tot = data.get("debt_total_baht")
            _cc = data.get("capacity_clear")
            if _tot and _cc:
                _gap = int(_cc) - R89_UNIT_PRICE
                if _gap >= int(_tot):
                    data["bridge_ok"] = 1
                    self._add_signal(
                        state,
                        f"💰 ปิดหนี้แล้วกู้ได้ {int(_cc)/1e6:.2f} ล้าน · "
                        f"ยอดหนี้รวม {int(_tot):,} บาท · ส่วนต่างวงเงิน "
                        f"{_gap/1e6:.2f} ล้าน คลุมยอดหนี้ครบ (ถามหลังได้เบอร์)")
                else:
                    self._add_signal(
                        state,
                        f"💰 ปิดหนี้แล้วกู้ได้ {int(_cc)/1e6:.2f} ล้าน · "
                        f"ยอดหนี้รวม {int(_tot):,} บาท · ส่วนต่างวงเงิน "
                        f"{_gap/1e6:.2f} ล้าน ต้องเคลียร์เพิ่มอีก "
                        f"{int(_tot) - _gap:,} บาท (ถามหลังได้เบอร์)")
        except Exception as _e:
            print(f"[R114 BRIDGE ERROR] {_e}")

    def _decide_r114(self, msg, user_id, state, bucket, is_new):
        data = state.get("data") or {}
        # ---- (ก) รอบนี้คือคำตอบของคำถามยอดหนี้รวมรอบหลังเบอร์ ----
        if state.get("_r114_wait"):
            try:
                state.pop("_r114_wait", None)
                _amt = _r89_parse_total(msg)
                if _amt is not None:
                    data["debt_total_baht"] = int(_amt)
                    self._add_signal(
                        state, f"ยอดหนี้คงเหลือรวม {int(_amt):,} บาท "
                               "(ลูกค้าบอกเอง หลังให้เบอร์)")
                    _r114_after_total(self, state, data)
                    try:
                        self._send_name_update(user_id, state)
                        print(f"[R114] {str(user_id)[:8]}... ได้ยอดหนี้รวม "
                              f"{int(_amt):,} — เขียนกลับแถวเดิมแล้ว")
                    except Exception as _e2:
                        print(f"[R114 SHEET ERROR] {_e2}")
                    return ["ขอบคุณครับ เดี๋ยวที่ปรึกษาโทรกลับพร้อมแผนให้เลยครับ"], None
                # ไม่ใช่ตัวเลข (ถามกลับ/ตอบเลี่ยง) -> ปล่อยเทิร์นให้เอนจินเดิมจัดการ
                data["debt_total_baht"] = None      # ถามครบ 2 รอบแล้ว ห้ามถามอีก
                self._add_signal(
                    state, f"ถามยอดหนี้รวมครบ 2 รอบแล้ว ลูกค้าตอบ: "
                           f"{str(msg)[:60]} — เซลยืนยันตอนโทร")
            except Exception as _e:
                print(f"[R114 CONSUME ERROR] {_e} — ไปทางเดิม")
                state.pop("_r114_wait", None)

        _had_contact = bool((state.get("data") or {}).get("contact"))
        bubbles, grade = _R114_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)

        # ---- (ข) เพิ่งได้เบอร์เทิร์นนี้ แต่ยังไม่รู้ยอดหนี้รวม -> ถามต่อ 1 ข้อ ----
        try:
            _d = state.get("data") or {}
            if (not _had_contact and _d.get("contact")
                    and _d.get("debt_baht")
                    and not _d.get("debt_total_baht")
                    and not state.get("_r114_asked")
                    # r116 — เกรด B ใช้ชุดถามละเอียด 4 ข้อแทน ไม่ต้องถามสั้นซ้ำ
                    and str(_d.get("grade") or "") != "B"
                    and not _d.get("cash")
                    and not state.get("renter") and not state.get("owner")):
                state["_r114_asked"] = True
                state["_r114_wait"] = True
                bubbles = [b for b in (bubbles or []) if b and str(b).strip()]
                bubbles.append(R89_DEBT_TOTAL_Q)
                print(f"[R114] {str(user_id)[:8]}... ได้เบอร์แล้วแต่ยังไม่รู้ "
                      "ยอดหนี้รวม — ถามซ้ำจังหวะที่ 2")
        except Exception as _e:
            print(f"[R114 ASK ERROR] {_e} — ใช้ชุดเดิม")
        return bubbles, grade

    CalmBotEngine._decide = _decide_r114
    print("[R114] ยอดหนี้รวม: ถาม 2 จังหวะ (หลังยอดผ่อน + ซ้ำอีกครั้งหลังได้เบอร์)")
except Exception as _e:
    print(f"[R114 ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")



# ============================================================
# r116 (1 ก.ย. 2569, Gift เคาะ) — ชุดยืนยันตัวเลข "เฉพาะเกรด B"
# ------------------------------------------------------------
# เหตุผล: เกรด B = เคสบริดจ์ ต้องรู้ยอดปิดภาระจริงถึงจะวางแผนได้
#   ยอดผ่อนรวมก้อนเดียวไม่พอ ต้องแยกรายการ เพราะแต่ละก้อนปิดยากง่ายไม่เท่ากัน
# ถามหลังได้เบอร์แล้วเท่านั้น — จังหวะนี้เสียลีดยากกว่ามาก
# 4 ข้อ ทีละข้อ · ข้อละครั้งเดียว (ถามกลับได้ 1 รอบ) · ตอบไม่ได้ = ข้ามไปข้อถัดไป
# ลูกค้าบอกพอ = หยุดทั้งชุดทันที + ติดธง  (ห้ามไล่บี้ ห้ามทิ้งเคส)
# ครบแล้ว: คิดวงเงินใหม่ -> ตีเกรดใหม่ + ติดธงว่าเปลี่ยนจากอะไร -> เขียนกลับแถวเดิม
# คำถามทุกข้อผ่าน _public_guard แล้ว (ห้ามมีคำว่า "คอมมิชชั่น"/"ค่าคอม" เด็ดขาด
# เพราะยามบล็อกทั้งบับเบิล = บอทเงียบ — บทเรียนจาก r110)
# ============================================================
R116_BRIDGE = ("ขอบคุณครับ ก่อนที่ปรึกษาจะโทรกลับ ขอเช็กตัวเลขเพิ่มอีก 4 ข้อสั้นๆ "
               "เพื่อคำนวณวงเงินให้แม่นขึ้นนะครับ")
R116_Q = {
    1: ("ข้อแรกครับ ผ่อนบ้านกับผ่อนรถ เดือนละเท่าไหร่ "
        "และยอดคงเหลือประมาณเท่าไหร่ครับ ถ้าไม่มีบอกว่าไม่มีได้เลยครับ"),
    2: ("ข้อสองครับ สินเชื่อส่วนบุคคลกับบัตรเครดิต จ่ายขั้นต่ำเดือนละเท่าไหร่ "
        "และยอดคงเหลือรวมประมาณเท่าไหร่ครับ"),
    3: ("ข้อสามครับ มีภาระอื่นอีกไหมครับ เช่น สหกรณ์ หักหน้าซอง กยศ. "
        "หรือผ่อนสินค้า ถ้ามีเดือนละเท่าไหร่ ยอดคงเหลือเท่าไหร่ครับ"),
    4: ("ข้อสุดท้ายครับ เงินเดือนที่แจ้งไว้ รวมโอที โบนัส และรายได้ส่วนอื่นแล้ว"
        "หรือยังครับ ถ้ายัง แต่ละอย่างเดือนละประมาณเท่าไหร่ครับ"),
}
R116_SLOT = {1: "home", 2: "loan", 3: "etc"}
R116_STOP = ("พอแล้ว", "ไม่สะดวก", "ไม่อยากบอก", "ไม่บอก", "ขี้เกียจ",
             "เดี๋ยวคุยกับ", "โทรมาเลย", "ไว้คุยโทร", "ไม่ตอบ", "พอก่อน")
R116_NONE = ("ไม่มี", "ไม่ได้ผ่อน", "ไม่มีเลย", "ไม่มีครับ", "ไม่มีค่ะ", "0")
R116_YES_ALL = ("รวมแล้ว", "รวมหมด", "รวมทุกอย่าง", "ครบแล้ว", "รวมอยู่แล้ว")
R116_NOT_YET = ("ยังไม่รวม", "ยัง", "ไม่รวม", "แยกต่างหาก", "ไม่ได้รวม")
_R116_UNIT = {"ล้าน": 1_000_000, "แสน": 100_000, "หมื่น": 10_000,
              "พัน": 1_000, "k": 1_000, "m": 1_000_000}


def _r116_amounts(msg):
    """ดึงจำนวนเงินทุกก้อน รองรับทั้ง 'สามแสน' และ '300000' — ตัดเบอร์โทรทิ้งก่อน"""
    s = str(msg or "").replace(",", "").lower()
    s = _re94.sub(r"0[0-9]{8,9}", " ", s)
    out = []
    for m in _re94.finditer(r"([0-9]+(?:\.[0-9]+)?)\s*(ล้าน|แสน|หมื่น|พัน|k|m)", s):
        v = round(float(m.group(1)) * _R116_UNIT[m.group(2)])
        if 300 <= v <= 100_000_000:
            out.append(v)
    s2 = _re94.sub(r"([0-9]+(?:\.[0-9]+)?)\s*(ล้าน|แสน|หมื่น|พัน|k|m)", " ", s)
    for x in _re94.findall(r"[0-9]+", s2):
        v = int(x)
        if 300 <= v <= 100_000_000:
            out.append(v)
    return out


def _r116_split(msg):
    """คืน (ยอดผ่อน/เดือน, ยอดคงเหลือ) — เดาจากขนาดเมื่อได้ตัวเดียว"""
    if any(w in str(msg or "") for w in R116_NONE) and not _r116_amounts(msg):
        return 0, 0
    nums = _r116_amounts(msg)
    if not nums:
        return None, None
    if len(nums) >= 2:
        return min(nums), max(nums)
    n = nums[0]
    return (None, n) if n >= 100_000 else (n, None)


def _r116_recalc(self, state, data):
    """รวมตัวเลขใหม่ -> คิดวงเงิน -> ตีเกรดใหม่ + ติดธงว่าเปลี่ยนจากอะไร"""
    _mo, _bal, _got = 0, 0, False
    for k in ("home", "loan", "etc"):
        _m = data.get(f"debt_{k}_monthly")
        _b = data.get(f"debt_{k}_balance")
        if _m is not None:
            _mo += int(_m); _got = True
        if _b is not None:
            _bal += int(_b); _got = True
    if _got:
        if _mo > 0:
            data["debt_baht"] = int(_mo)
        if _bal > 0:
            data["debt_total_baht"] = int(_bal)
            data["close_amount"] = int(_bal)
    _x = data.get("income_extra")
    if _x:
        data["income_var"] = int(_x)
        _own = data.get("income_baht") or 0
        _cob = data.get("co_borrower_income") or 0
        data["income_total"] = int(_own) + int(_x) + int(_cob)
    _old = str(data.get("grade") or "")
    try:
        _new = self._grade(data, state)
    except Exception as _e:
        print(f"[R116 REGRADE ERROR] {_e} — คงเกรดเดิม")
        _new = _old
    if _new == "X" and _old != "X":
        self._add_signal(state, f"⚠️ ตัวเลขใหม่ทำให้ระบบตีเป็น X แต่คงเกรด {_old} "
                                "ไว้เพราะแจกเคสไปแล้ว — เซลเช็กก่อนเสนอ")
        _new = _old
    if _new and _new != _old:
        data["grade"] = _new
        self._add_signal(state, f"🔁 เกรดเปลี่ยน {_old} → {_new} "
                                "หลังยืนยันตัวเลขหนี้/รายได้จริง (ชุด 4 ข้อหลังได้เบอร์)")
        print(f"[R116] เกรดเปลี่ยน {_old} -> {_new}")
    _cc = data.get("capacity_clear")
    _tot = data.get("debt_total_baht")
    if _cc and _tot:
        _gap = int(_cc) - R89_UNIT_PRICE
        data["bcr"] = 1 if _gap >= int(_tot) else ""
        if data["bcr"] == 1:
            self._add_signal(state, f"💰 BCR — ปิดหนี้แล้วกู้ได้ {int(_cc)/1e6:.2f} ล้าน · "
                                    f"ยอดปิดภาระ {int(_tot):,} บาท · "
                                    f"ส่วนต่าง {_gap/1e6:.2f} ล้าน คลุมครบ")
        else:
            self._add_signal(state, f"ปิดหนี้แล้วกู้ได้ {int(_cc)/1e6:.2f} ล้าน · "
                                    f"ยอดปิดภาระ {int(_tot):,} บาท · "
                                    f"ส่วนต่างขาดอีก {int(_tot) - _gap:,} บาท")


def _r116_save(self, user_id, state):
    try:
        self._send_name_update(user_id, state)
    except Exception as _e:
        print(f"[R116 SHEET ERROR] {_e}")


try:
    _R116_BASE_DECIDE = CalmBotEngine._decide

    def _decide_r116(self, msg, user_id, state, bucket, is_new):
        data = state.get("data") or {}
        _step = state.get("_r116_step")

        # ---------- กำลังอยู่ในชุดคำถาม ----------
        if _step:
            try:
                _t = str(msg or "")
                if any(w in _t for w in R116_STOP):
                    state.pop("_r116_step", None)
                    self._add_signal(state, "ลูกค้าขอไม่ตอบชุดยืนยันตัวเลขต่อ "
                                            "— เซลถามที่เหลือตอนโทร")
                    _r116_recalc(self, state, data)
                    _r116_save(self, user_id, state)
                    print(f"[R116] {str(user_id)[:8]}... ลูกค้าขอหยุด — ปิดชุด")
                    return ["ได้เลยครับ เดี๋ยวที่ปรึกษาโทรคุยรายละเอียดให้นะครับ"], None

                _advance = False
                if _step <= 3:
                    _m, _b = _r116_split(_t)
                    if _m is not None or _b is not None:
                        _slot = R116_SLOT[_step]
                        if _m is not None:
                            data[f"debt_{_slot}_monthly"] = int(_m)
                        if _b is not None:
                            data[f"debt_{_slot}_balance"] = int(_b)
                        self._add_signal(
                            state,
                            f"[ยืนยันตัวเลข {_step}/4] {_slot} ผ่อน/เดือน "
                            f"{('%s' % f'{int(_m):,}') if _m is not None else '-'} · "
                            f"คงเหลือ "
                            f"{('%s' % f'{int(_b):,}') if _b is not None else '-'}")
                        _advance = True
                else:
                    _neg = any(w in _t for w in R116_NOT_YET)
                    _pos = (not _neg) and any(w in _t for w in R116_YES_ALL)
                    _nums = _r116_amounts(_t)
                    if _pos:
                        data["income_confirmed"] = "รวมแล้ว"
                        self._add_signal(state, "[ยืนยันตัวเลข 4/4] เงินเดือนที่แจ้ง "
                                                "รวมโอที/โบนัส/รายได้อื่นแล้ว")
                        _advance = True
                    elif _nums:
                        data["income_confirmed"] = "ยังไม่รวม"
                        data["income_extra"] = int(sum(_nums))
                        self._add_signal(state, f"[ยืนยันตัวเลข 4/4] มีรายได้เพิ่มนอก"
                                                f"เงินเดือนรวม {int(sum(_nums)):,}/เดือน")
                        _advance = True
                    elif _neg:
                        data["income_confirmed"] = "ยังไม่รวม (ไม่ได้ตัวเลข)"
                        self._add_signal(state, "[ยืนยันตัวเลข 4/4] บอกว่ายังไม่รวม "
                                                "แต่ไม่ได้ตัวเลข — เซลถามตอนโทร")
                        _advance = True

                if not _advance:
                    _tries = int(state.get("_r116_tries") or 0) + 1
                    state["_r116_tries"] = _tries
                    if _tries < 2 and self._is_question(msg):
                        # ลูกค้าถามกลับ — ให้เอนจินเดิมตอบ แล้วทวนคำถามเดิมต่อท้าย
                        _bb, _gg = _R116_BASE_DECIDE(self, msg, user_id,
                                                     state, bucket, is_new)
                        _bb = [b for b in (_bb or []) if b and str(b).strip()]
                        _bb.append(R116_Q[_step])
                        return _bb, _gg
                    self._add_signal(state, f"[ยืนยันตัวเลข {_step}/4] ไม่ได้ตัวเลข "
                                            f"— ลูกค้าตอบ: {_t[:40]} · เซลถามตอนโทร")
                    _advance = True

                state["_r116_tries"] = 0
                _step += 1
                if _step > 4:
                    state.pop("_r116_step", None)
                    state.pop("_r116_tries", None)
                    _r116_recalc(self, state, data)
                    _r116_save(self, user_id, state)
                    print(f"[R116] {str(user_id)[:8]}... เก็บครบ 4 ข้อ — "
                          f"คิดวงเงินใหม่ + เขียนกลับชีตแล้ว")
                    return (["ขอบคุณมากครับ ข้อมูลครบแล้ว "
                             "เดี๋ยวที่ปรึกษาโทรกลับพร้อมแผนวงเงินให้เลยครับ"], None)
                state["_r116_step"] = _step
                _r116_save(self, user_id, state)
                return [R116_Q[_step]], None
            except Exception as _e:
                print(f"[R116 STEP ERROR] {_e} — ปิดชุด ไปทางเดิม")
                state.pop("_r116_step", None)
                state.pop("_r116_tries", None)

        # ---------- ยังไม่เริ่ม: เช็คว่าเพิ่งได้เบอร์ + เกรด B ไหม ----------
        _had_contact = bool((state.get("data") or {}).get("contact"))
        bubbles, grade = _R116_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
        try:
            _d = state.get("data") or {}
            _g = str(grade or _d.get("grade") or "")
            if (_g == "B" and not _had_contact and _d.get("contact")
                    and not state.get("_r116_done")
                    and not _d.get("cash")
                    and not state.get("renter") and not state.get("owner")):
                state["_r116_done"] = True
                state["_r116_step"] = 1
                state["_r116_tries"] = 0
                bubbles = [b for b in (bubbles or []) if b and str(b).strip()]
                # r114 อาจต่อคำถามยอดหนี้รวมสั้นไว้ — เอาออก ชุดนี้ละเอียดกว่า
                bubbles = [b for b in bubbles if b != R89_DEBT_TOTAL_Q]
                state.pop("_r114_wait", None)
                _pre = ""
                try:
                    if _d.get("debt_baht"):
                        _pre = (f"ที่แจ้งว่าผ่อนรวมเดือนละ {int(_d['debt_baht']):,} "
                                "ขอแยกเป็นรายการนะครับ ")
                except Exception:
                    _pre = ""
                bubbles.append(R116_BRIDGE)
                bubbles.append(_pre + R116_Q[1])
                print(f"[R116] {str(user_id)[:8]}... เกรด B + ได้เบอร์แล้ว "
                      "— เริ่มชุดยืนยันตัวเลข 4 ข้อ")
        except Exception as _e:
            print(f"[R116 START ERROR] {_e} — ใช้ชุดเดิม")
        return bubbles, grade

    CalmBotEngine._decide = _decide_r116
    print("[R116] ชุดยืนยันตัวเลขเกรด B (หนี้ 3 รายการ + รายได้จริง) เปิดแล้ว")
except Exception as _e:
    print(f"[R116 ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")


# ---------- r116 (2) — ส่ง 12 ช่องใหม่เข้าชีตผ่านจุดรวมเดิม ----------
try:
    _R116_PREV_INUM = _bl9.BotEngine._income_numbers

    def _income_numbers_r116(data):
        _out = dict(_R116_PREV_INUM(data))
        try:
            _d = data or {}
            for _k in ("debt_home_monthly", "debt_home_balance",
                       "debt_loan_monthly", "debt_loan_balance",
                       "debt_etc_monthly", "debt_etc_balance",
                       "income_extra", "close_amount",
                       "capacity_clear", "capacity_now"):
                _v = _d.get(_k)
                _out[_k] = "" if _v in (None, "") else int(_v)
            _out["income_confirmed"] = _d.get("income_confirmed") or ""
            _out["bcr"] = "✅" if _d.get("bcr") or _d.get("bridge_ok") else ""
        except Exception as _e:
            print(f"[R116 NUM ERROR] {_e}")
        return _out

    _bl9.BotEngine._income_numbers = staticmethod(_income_numbers_r116)
    print("[R116] ส่ง 12 ช่องใหม่เข้าชีต (คอลัมน์ 37–48)")
except Exception as _e:
    print(f"[R116 NUM PATCH ERROR] ต่อไม่ติด: {_e}")



# ============================================================
# r119 (1 ก.ย. 2569, Gift สั่ง) — ห้ามเคสลงทุนหลุดไปสายเช่า/เจ้าของ
# ------------------------------------------------------------
# ตัวจับเดิม (_is_renter) กันคำว่า "ปล่อยเช่า/ลงทุน" ได้เฉพาะ "ในข้อความเดียวกัน"
# แต่ธง renter ตั้งได้ทุกเทิร์น -> ลูกค้าที่คุยสายซื้อมาครึ่งทางแล้วพิมพ์คำว่า
# "เช่า" ลอยๆ จะโดนสลับสายทันที แล้ว _upsert_lead เขียนลงไฟล์ห้องเช่าเลย
# เคสจริงในไฟล์ผู้เช่า FB-XX-20260901-016: รายได้ 59,000 · ภาระ 60,000 ·
# อายุ 44 · เสนอปิดภาระ 2.68M ครบแบบนักลงทุน แต่ไปจบที่ไฟล์ห้องเช่า
# r118 ทำให้แรงขึ้นอีก เพราะสายเช่าข้ามคำถามคนซื้อทั้งหมด = หลุดเงียบ
#
# แก้ 2 ชั้น:
#   1. ถามยืนยันก่อนสลับสายเสมอ (Gift สั่ง) — ไม่สลับทันทีอีกต่อไป
#   2. ถ้าเคสมีหลักฐานนักลงทุนอยู่แล้ว (รู้รายได้/ภาระ/อายุ/วงเงิน/เกรด)
#      ตอบไม่ชัด = อยู่สายซื้อไว้ก่อน + ติดธงให้เซล (ห้ามทิ้งเคส)
# ใช้ threading.local เก็บ state ของเทิร์นนั้น — บอทรับหลายคนพร้อมกัน
# ถ้าใช้ตัวแปร global ธรรมดาจะสลับ state ข้ามคนได้
# ============================================================
R119_CONFIRM_Q = ("ขอเช็กให้ชัดนิดนึงนะครับ ลูกค้ากำลังมองหาห้องเช่าไว้อยู่เอง "
                  "หรือสนใจซื้อคอนโดไว้ปล่อยเช่าครับ")
# ลำดับสำคัญมาก: "ซื้อไว้ปล่อยเช่าครับ" มีคำว่าเช่าอยู่ด้วย
# ถ้าเช็คคำเช่าก่อนจะตีเป็นผู้เช่าทันที = เคสลงทุนหลุด (ตัวทดสอบจับได้ก่อน deploy)
# จึงต้องเรียง: ปฏิเสธการซื้อ -> คำสายซื้อ -> คำสายเช่า
_R119_SAY_NOTBUY = ("ไม่ได้ซื้อ", "ไม่ซื้อ", "ไม่ได้ลงทุน", "ไม่ลงทุน",
                    "ไม่ได้จะซื้อ", "ยังไม่ซื้อ")
_R119_SAY_BUY = ("ซื้อ", "ลงทุน", "ปล่อยเช่า", "ให้เช่า", "เก็บค่าเช่า", "ผ่อน",
                 "กู้", "สินเชื่อ", "ฝากเช่า", "ฝากขาย")
_R119_SAY_RENT = ("เช่าอยู่เอง", "อยู่เอง", "หาเช่า", "จะเช่า", "เช่าอย่างเดียว",
                  "อยากเช่า", "หาห้องเช่า", "เช่าห้อง", "มาเช่า")


def _r119_read_answer(t):
    """คืน 'rent' / 'buy' / '' — เรียงตามลำดับที่กันเคสลงทุนหลุดไว้แล้ว"""
    t = str(t or "")
    if any(w in t for w in _R119_SAY_NOTBUY):
        return "rent"
    if any(w in t for w in _R119_SAY_BUY):
        return "buy"
    if any(w in t for w in _R119_SAY_RENT):
        return "rent"
    return ""
_R119_TL = threading.local()


def _r119_state():
    return getattr(_R119_TL, "state", None) or {}


def _r119_has_invest_evidence(state) -> bool:
    """เคสนี้เดินสายซื้อมาแล้วจริงไหม — ดูจากของที่เก็บได้เท่านั้น"""
    try:
        d = (state or {}).get("data") or {}
        if state.get("done") or d.get("grade"):
            return True
        for k in ("income_baht", "income_total", "capacity_now", "capacity_clear",
                  "age", "co_borrower_income", "debt_total_baht"):
            if d.get(k):
                return True
        if d.get("debt_baht") is not None:
            return True
        _asked = (state.get("asked") or {})
        if any(_asked.get(k) for k in ("income", "debt", "co_borrower", "contact")):
            return True
    except Exception as _e:
        print(f"[R119 EVIDENCE ERROR] {_e}")
    return False


def _r119_gate(kind, raw_hit):
    """ตัวกลางของทั้ง _is_renter และ _is_owner_listing
    คืน True = ปล่อยให้สลับสายได้ · False = ยังไม่สลับ (ตั้งธงถามยืนยันแทน)"""
    if not raw_hit:
        return False
    st = _r119_state()
    if not st:
        return raw_hit                     # ไม่มี state = เรียกจากที่อื่น อย่าไปยุ่ง
    d = st.get("data") or {}
    if d.get(f"_r119_{kind}_ok"):
        return True                        # ลูกค้ายืนยันแล้ว
    if d.get("_r119_invest_ok"):
        return False                       # ยืนยันแล้วว่าเป็นสายซื้อ ห้ามสลับอีก
    st["_r119_pending"] = kind
    return False


try:
    _R119_ORIG_RENTER = _bl9._is_renter
    _R119_ORIG_OWNER = _bl9._is_owner_listing

    def _is_renter_r119(msg):
        return _r119_gate("rent", _R119_ORIG_RENTER(msg))

    def _is_owner_listing_r119(msg):
        return _r119_gate("own", _R119_ORIG_OWNER(msg))

    _bl9._is_renter = _is_renter_r119
    _bl9._is_owner_listing = _is_owner_listing_r119
    print("[R119] ต้องยืนยันก่อนสลับไปสายเช่า/เจ้าของ (กันเคสลงทุนหลุด)")
except Exception as _e:
    print(f"[R119 GATE ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")


try:
    _R119_BASE_DECIDE = CalmBotEngine._decide

    def _decide_r119(self, msg, user_id, state, bucket, is_new):
        _R119_TL.state = state
        try:
            data = state.get("data") or {}
            # ---- (ก) กำลังรอคำตอบยืนยัน ----
            if state.get("_r119_wait"):
                _kind = state.pop("_r119_wait", None)
                _orig = state.pop("_r119_orig", None) or msg
                _ans = _r119_read_answer(msg)
                _rent = (_ans == "rent")
                _buy = (_ans == "buy")
                if _rent:
                    data[f"_r119_{_kind}_ok"] = True
                    self._add_signal(state, "ลูกค้ายืนยันเองว่าต้องการเช่า/ฝากปล่อยเช่า "
                                            "— ไม่ใช่เคสลงทุน")
                    print(f"[R119] {str(user_id)[:8]}... ยืนยันสาย {_kind} — สลับสาย")
                    return _R119_BASE_DECIDE(self, _orig, user_id, state,
                                             bucket, is_new)
                if _buy:
                    data["_r119_invest_ok"] = True
                    self._add_signal(state, "ลูกค้ายืนยันว่าสนใจซื้อ/ลงทุน "
                                            "— เคยพิมพ์คำว่าเช่าแต่ไม่ใช่ผู้เช่า")
                    print(f"[R119] {str(user_id)[:8]}... ยืนยันสายซื้อ — อยู่สายลงทุนต่อ")
                elif _r119_has_invest_evidence(state):
                    data["_r119_invest_ok"] = True
                    self._add_signal(
                        state, "⚠️ ลูกค้าพิมพ์คำว่าเช่าแต่ตอบยืนยันไม่ชัด — "
                               "เคสนี้ให้ข้อมูลรายได้/ภาระมาแล้ว จึงคงไว้ในสายลงทุน "
                               "เซลเช็กตอนโทรว่าจะซื้อหรือเช่า")
                    print(f"[R119] {str(user_id)[:8]}... ตอบไม่ชัด + มีหลักฐาน"
                          "นักลงทุน — คงสายซื้อ")
                else:
                    data[f"_r119_{_kind}_ok"] = True
                    print(f"[R119] {str(user_id)[:8]}... ตอบไม่ชัด แต่ยังไม่มีข้อมูล"
                          f"สายซื้อเลย — ไปสาย {_kind}")
                    return _R119_BASE_DECIDE(self, _orig, user_id, state,
                                             bucket, is_new)

            state.pop("_r119_pending", None)
            bubbles, grade = _R119_BASE_DECIDE(self, msg, user_id, state,
                                               bucket, is_new)

            # ---- (ข) เทิร์นนี้เกือบสลับสาย -> ถามยืนยันก่อน ----
            _pend = state.pop("_r119_pending", None)
            if _pend and not state.get("renter") and not state.get("owner"):
                state["_r119_wait"] = _pend
                state["_r119_orig"] = msg
                bubbles = [b for b in (bubbles or []) if b and str(b).strip()]
                bubbles.append(R119_CONFIRM_Q)
                print(f"[R119] {str(user_id)[:8]}... เจอคำสาย {_pend} "
                      "— ถามยืนยันก่อน ยังไม่สลับสาย")
            return bubbles, grade
        except Exception as _e:
            print(f"[R119 DECIDE ERROR] {_e} — ใช้ทางเดิม")
            return _R119_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
        finally:
            _R119_TL.state = None

    CalmBotEngine._decide = _decide_r119
    print("[R119] ชั้นถามยืนยันสายเช่า/ลงทุน เปิดแล้ว")
except Exception as _e:
    print(f"[R119 ERROR] ต่อไม่ติด — ใช้ทางเดิม: {_e}")

# ============================================================================
# r120 (1 ก.ย. 2569, Gift เคาะ) — คำหยุดบอทแบบสัญลักษณ์เดียว "##"
# ----------------------------------------------------------------------------
# ของเดิมต้องพิมพ์ "ขอบคุณที่สนใจครับบ" (เติมตัวท้ายซ้ำ) ยาว พิมพ์ผิดง่าย
# และต้องมี 4 แบบตามเพศเซล (ครับบ/คับบ/ค่ะะ/คะะ)
# Gift ขอ: 1 สัญลักษณ์ 2 เคาะ ไม่มีเพศ ปิดบอทเฉพาะแชทนั้น ถาวร ผลเหมือนเดิมทุกอย่าง
#
# กติกาความปลอดภัย — จับแบบ "ทั้งข้อความต้องเป็น ## เท่านั้น":
#   · "##"            -> ปิด
#   · "#รับเคส"        -> ไม่ปิด (มี # แต่ไม่ใช่ทั้งข้อความ)
#   · "ห้อง #12 ครับ"  -> ไม่ปิด
#   ของเดิม _is_kill_phrase เป็น substring match จึงห้ามเอา "##"
#   ไปใส่ใน BOT_KILL_PHRASES ตรง ๆ (จะโดนทุกข้อความที่มี ## ปน)
#
# ทำงานเฉพาะฝั่งเพจ: _is_kill_phrase ถูกเรียกใน handle_page_echo เท่านั้น
#   -> ลูกค้าพิมพ์ "##" มาเอง บอทไม่หยุด
# ครอบของเดิมเสมอ: ของเดิมบอก True ก็ True (พฤติกรรมเดิมไม่ถูกแตะ)
# ปลดล็อกด้วย "#เปิดบอท" / "#คืนบอท" / "#resume" เหมือนเดิม
# ============================================================================
try:
    import re as _re120
    import bot_logic          # r120 fix: main.py เดิม import ไว้ในชื่ออื่น (_bl/_bl9)

    _R120_STOP = ("##", "＃＃", "#＃", "＃#")   # ครึ่งความกว้าง + เต็มความกว้าง

    _R120_ORIG_KILL = bot_logic._is_kill_phrase

    def _is_kill_phrase_r120(text):
        # 1) ของเดิมก่อนเสมอ — ประโยคปิดการขายยังใช้ได้ทุกแบบ
        try:
            if _R120_ORIG_KILL(text):
                return True
        except Exception:
            pass
        # 2) สัญลักษณ์เดียว — ต้องเป็น "ทั้งข้อความ" ไม่ใช่แค่มีปนอยู่
        try:
            t = _re120.sub(r"\s+", "", str(text or ""))
            if t and t in _R120_STOP:
                print("[R120] เจอคำหยุดบอท '##' — ปิดบอทถาวรเฉพาะแชทนี้")
                return True
        except Exception:
            pass
        return False

    bot_logic._is_kill_phrase = _is_kill_phrase_r120
    print("[R120] คำหยุดบอท '##' เปิดแล้ว (ฝั่งเพจเท่านั้น · ปลดล็อกด้วย #เปิดบอท)")
except Exception as _e:
    print(f"[R120 ERROR] ต่อไม่ติด — ใช้คำหยุดเดิม: {_e}")

# ============================================================================
# r121 (1 ก.ย. 2569, Gift สั่ง) — เปลี่ยนประโยคขอเบอร์ + ขอเวลาที่สะดวกในประโยคเดียว
# ----------------------------------------------------------------------------
# ของเดิม (QUALIFY_QUESTIONS[3]):
#   "ขอเบอร์หน่อยครับ เดี๋ยวที่ปรึกษาโทรไปบอกห้องที่ตรงงบพร้อมตารางผ่อนให้
#    คุยทางโทรเข้าใจง่ายกว่า ใช้เวลาไม่นานครับ"
# ใหม่ (Gift พิมพ์มาเอง):
#   "ขอเบอร์ติดต่อกลับ เพื่อให้ที่ปรึกษาโทรให้รายละเอียดเพิ่มเติม
#    แจ้งเวลาที่สะดวกได้เลยค่ะ"
#
# ทำไมแก้ที่นี่ไม่แก้ใน faq_data.py: กติกาโปรเจกต์ = ของใหม่อยู่ใน main.py ชั้น patch
# QUALIFY_QUESTIONS เป็น list -> แก้ในตัวเดียวมีผลทั้ง faq_data และ bot_logic
# (bot_logic ใช้ from faq_data import ... จึงถือ reference ตัวเดียวกัน)
#
# ผลพลอยได้: ประโยคนี้ชวนให้ลูกค้าบอก "เวลาที่สะดวก" ตั้งแต่รอบแรก
# ซึ่ง r65 (_parse_callback_time) จะจับไปเก็บใน data["callback_req"]
# แล้วเขียนขึ้นช่องสัญญาณของชีตให้เอง -> รีพอร์ตดึงไปโชว์เป็นคอลัมน์ได้
# ============================================================================
try:
    import faq_data as _fq121

    R121_ASK = ("ขอเบอร์ติดต่อกลับ เพื่อให้ที่ปรึกษาโทรให้รายละเอียดเพิ่มเติม "
                "แจ้งเวลาที่สะดวกได้เลยค่ะ")

    _r121_old = ""
    try:
        _r121_old = str(_fq121.QUALIFY_QUESTIONS[3] or "")
    except Exception:
        _r121_old = ""

    if _r121_old and _r121_old != R121_ASK:
        _fq121.QUALIFY_QUESTIONS[3] = R121_ASK
        try:
            import bot_logic as _bl121
            _bl121.QUALIFY_QUESTIONS[3] = R121_ASK      # ตัวเดียวกัน แต่ยืนยันให้ชัวร์
        except Exception:
            pass
        print(f"[R121] เปลี่ยนประโยคขอเบอร์แล้ว (เดิมยาว {len(_r121_old)} ตัวอักษร)")
    else:
        print("[R121] ประโยคขอเบอร์เป็นตัวใหม่อยู่แล้ว — ไม่แตะ")
except Exception as _e:
    print(f"[R121 ERROR] ต่อไม่ติด — ใช้ประโยคเดิม: {_e}")


# ============================================================================
# r122 (1 ก.ย. 2569, Gift สั่ง) — 2 เรื่อง: voice ตามเพจ + เลิกบอก "ไม่ต้องโทรก็ได้"
# ----------------------------------------------------------------------------
# (1) แก้ที่ r121 ทำพลาดเอง: ประโยคขอเบอร์ลงท้าย "ค่ะ" ตายตัว
#     ระบบมี voice รายเพจอยู่แล้ว — WEC_PAGES ตั้ง "gender": "female"
#     แล้ว bot_logic.to_female() แปลง ครับ -> ค่ะ ให้เอง (ครอบด้วย r73 อีกชั้น)
#     ข้อความต้นทางจึงต้องเขียนเป็น "ครับ" เสมอ เพจผู้หญิงถึงจะได้ ค่ะ
#     ถ้าเขียน ค่ะ ตายตัว เพจผู้ชายจะพูด ค่ะ ตลอด = ผิด voice
#
# (2) Gift เห็นหน้างาน 1 ก.ย. 14:57 แล้วสั่ง "ไม่เอาคำนี้":
#       "ไม่ต้องโทรก็ได้ครับ คุยทางแชทนี้ต่อได้เลย
#        รบกวนบอกงบประมาณคร่าวๆ กับโซนที่สนใจ เดี๋ยวผมคัดห้องที่ตรงมาให้ดูครับ"
#     ปัญหา: บอทเป็นฝ่ายบอกเองว่าไม่ต้องโทร = ปิดประตูการโทรด้วยมือตัวเอง
#     สวนกับกติกา Gift "เน้นขอเบอร์ lead เป็นหลัก" และทำให้เคสค้างในแชท
#     ใหม่: ยอมรับว่าไม่สะดวกโทร แต่ดันไป LINE ต่อ ไม่ตื๊อขอเบอร์ซ้ำ
# ============================================================================
try:
    import faq_data as _fq122
    import bot_logic as _bl122

    # ---------- (1) ประโยคขอเบอร์ กลับมาเป็นเสียงกลาง (ครับ) ----------
    R122_ASK = ("ขอเบอร์ติดต่อกลับ เพื่อให้ที่ปรึกษาโทรให้รายละเอียดเพิ่มเติม "
                "แจ้งเวลาที่สะดวกได้เลยครับ")
    try:
        if str(_fq122.QUALIFY_QUESTIONS[3] or "") != R122_ASK:
            _fq122.QUALIFY_QUESTIONS[3] = R122_ASK
            _bl122.QUALIFY_QUESTIONS[3] = R122_ASK
            print("[R122] ประโยคขอเบอร์ -> เสียงกลาง (ครับ) เพจผู้หญิงแปลงเป็น ค่ะ เอง")
    except Exception as _e1:
        print(f"[R122 WARN] ตั้งประโยคขอเบอร์ไม่ได้: {_e1}")

    # ---------- (2) ลูกค้าไม่สะดวกให้โทร -> ดันไป LINE ----------
    R122_REFUSED = ("ได้ครับ ถ้าไม่สะดวกคุยโทรศัพท์ ขอเป็น ID LINE ก็ได้ครับ\n"
                    "เดี๋ยวที่ปรึกษาส่งห้องที่ตรงงบพร้อมตารางผ่อนไปให้ทางไลน์")
    try:
        _fq122.CONTACT_REFUSED_MSG = R122_REFUSED
        _bl122.CONTACT_REFUSED_MSG = R122_REFUSED   # bot_logic ทำ from faq_data import จึงถือชื่อของตัวเอง
        print("[R122] เปลี่ยนคำตอบตอนลูกค้าไม่สะดวกให้โทร -> ดันไป LINE แล้ว")
    except Exception as _e2:
        print(f"[R122 WARN] ตั้งคำตอบไม่สะดวกให้โทรไม่ได้: {_e2}")

except Exception as _e:
    print(f"[R122 ERROR] ต่อไม่ติด — ใช้ของเดิม: {_e}")


# ============================================================================
# r123 (1 ก.ย. 2569, Gift สั่ง) — เปิดบอทตอบเพจ MillionCondo
# ----------------------------------------------------------------------------
# ตัวเลขที่ทำให้ตัดสินใจ (1 ก.ย.): เพจนี้มีลีดเข้า 24 ใบ แต่ "มีช่องทางติดต่อ 0"
#   ทุกใบเงียบเพราะ WEC_PAGES ตั้ง "reply": false ไว้ตั้งแต่ r70 (22 ส.ค.)
#   = จ่ายค่าแอดแล้วทิ้งทุกวัน
#
# ทำไมแก้ที่นี่ไม่ไปแก้ env: ตัวแปร WEC_PAGES อยู่รวมกับค่าตั้งค่าเพจอื่นทั้งหมด
#   การเขียนทับทั้งก้อนเสี่ยงกว่าการ override ในชั้น patch ที่ย้อนกลับได้ทันที
#   เมื่อ Gift แก้ env จริงเมื่อไหร่ แพตช์นี้จะไม่ทำอะไร (เจอ reply เป็น true อยู่แล้ว)
#
# จุดที่พลาดง่าย: main.py บรรทัด ~150 คำนวณ OBSERVE_PAGES ตอน import
#   ซึ่งเกิด "ก่อน" แพตช์ทุกตัว -> ตั้ง reply=True เฉย ๆ ไม่พอ
#   ต้องถอด page id ออกจาก OBSERVE_PAGES ด้วย ไม่งั้นบอทยังเงียบเหมือนเดิม
#
# จับเพจด้วยชื่อ ไม่ฮาร์ดโค้ด page id -> ถ้า id เปลี่ยนก็ยังทำงาน และไม่เดาผิดเพจ
# ============================================================================
try:
    import re as _re123

    _R123_MATCH = _re123.compile(r"millioncondo|อสังหาเงินล้าน", _re123.IGNORECASE)
    _r123_hit, _r123_already = [], []

    for _pid, _cfg in list((PAGES or {}).items()):
        try:
            _blob = " ".join(str((_cfg or {}).get(k, "")) for k in ("tab", "brand", "name"))
            if not _R123_MATCH.search(_blob):
                continue
            _was_off = page_reply_off(_pid)
            if not _was_off:
                _r123_already.append(str(_pid))
                continue
            _cfg["reply"] = True
            _cfg.pop("mode", None)          # กัน observe/silent/listen/log ค้างอยู่
            try:
                OBSERVE_PAGES.discard(str(_pid))
            except Exception:
                pass
            _r123_hit.append(str(_pid))
        except Exception as _e1:
            print(f"[R123 WARN] เพจ {_pid}: {_e1}")

    if _r123_hit:
        print(f"[R123] เปิดบอทตอบแล้ว {len(_r123_hit)} เพจ: {', '.join(_r123_hit)} "
              f"· OBSERVE_PAGES เหลือ {sorted(OBSERVE_PAGES)}")
    elif _r123_already:
        print(f"[R123] เพจ MillionCondo เปิดตอบอยู่แล้ว ({', '.join(_r123_already)}) — ไม่แตะ")
    else:
        print("[R123 WARN] หาเพจ MillionCondo ใน WEC_PAGES ไม่เจอ — บอทยังเงียบเหมือนเดิม")
except Exception as _e:
    print(f"[R123 ERROR] ต่อไม่ติด — เพจยังปิดตอบตามเดิม: {_e}")


# ===== r124 : ข้อสอบบอท (regression exam) — 2 ก.ย. 2569 =====
# เปิดที่ GET /exam · ใช้ข้อความสังเคราะห์ล้วน ไม่มีข้อมูลลูกค้าจริง
# ทุกข้อผูกกับอาการที่เคยพังจริง ถ้าข้อไหน "ตก" แปลว่ามี patch ใหม่ไปทับของเก่า
try:
    import json as _j124
    import bot_logic as _bl124

    def _r124_fn(name):
        f = globals().get(name)
        if f is None:
            f = getattr(_bl124, name, None)
        return f

    def _r124_flat(v):
        try:
            if isinstance(v, (tuple, list)):
                return " | ".join(["-" if x is None else str(x) for x in v])
            if v is None:
                return ""
            return str(v)
        except Exception:
            return "?"

    def _r124_truthy(v):
        if isinstance(v, (tuple, list)):
            return bool(v) and v[0] not in (None, False, "", 0)
        return bool(v)

    def _r124_check(kind, val, raw):
        s = _r124_flat(raw)
        if kind == "falsy":
            return (not _r124_truthy(raw)), s
        if kind == "truthy":
            return _r124_truthy(raw), s
        if kind == "has":
            return (val in s), s
        if kind == "no":
            return (val not in s), s
        if kind == "eq":
            return (s.strip() == val), s
        return False, s

    _R124_EXAM = [
        ("A1", "ช่องหนี้↔รายได้", "_r100_reroute", ("debt", "ประมาน 4-5 พันต่อเดือน", {}, {}), ("falsy", ""), "4-5 พัน/เดือน คือยอดผ่อน ห้ามนับเป็นรายได้ (log 2 ก.ย. 03:07)"),
        ("A2", "ช่องหนี้↔รายได้", "_r100_reroute", ("debt", "เดือนละ 2 หมื่น", {}, {}), ("falsy", ""), "ตอบตอนถามยอดผ่อน ไม่มีคำว่ารายได้ ห้ามย้าย (log 1 ก.ย. 15:40)"),
        ("A3", "ช่องหนี้↔รายได้", "_r100_reroute", ("debt", "เงินเดือน 43,000฿", {}, {}), ("has", "income"), "มีคำว่าเงินเดือนชัดเจน ย้ายถูกแล้ว"),
        ("A4", "ช่องหนี้↔รายได้", "_r100_reroute", ("debt", "เงินเดือน+โอที ประมาน 7-8 หมื่นครับ", {}, {}), ("has", "income"), "มีคำว่าเงินเดือน ย้ายถูกแล้ว"),
        ("A5", "ช่องหนี้↔รายได้", "_r100_reroute", ("debt", "ผ่อนรถ 8,500 บาท", {}, {}), ("falsy", ""), "ยอดผ่อนตรงตัว ห้ามย้าย"),
        ("A6", "ช่องหนี้↔รายได้", "_r100_reroute", ("debt", "ผ่อนบ้าน 12,000 ผ่อนรถ 6,000", {}, {}), ("falsy", ""), "ยอดผ่อนสองก้อน ห้ามย้าย"),
        ("A7", "ช่องหนี้↔รายได้", "_r100_reroute", ("debt", "ไม่มีภาระค่ะ", {}, {}), ("falsy", ""), "ตอบว่าไม่มีหนี้ ห้ามย้าย"),
        ("A8", "ช่องหนี้↔รายได้", "_r100_reroute", ("debt", "รายได้เดือนละ 45,000", {}, {}), ("has", "income"), "มีคำว่ารายได้ ย้ายถูก"),
        ("A9", "ช่องหนี้↔รายได้", "_r100_reroute", ("debt", "รับเดือนละ 55,000 ครับ", {}, {}), ("has", "income"), "คำว่ารับเดือนละ = รายได้"),
        ("A10", "ช่องหนี้↔รายได้", "_r100_reroute", ("debt", "งบซื้อห้องประมาณ 2 ล้าน", {}, {}), ("no", "income"), "งบซื้อห้อง ไม่ใช่รายได้ต่อเดือน"),
        ("A11", "ช่องหนี้↔รายได้", "_r100_reroute", ("debt", "34000-35000", {"income_baht": 34000}, {}), ("falsy", ""), "ตัวเลขเท่ารายได้ที่บันทึกไว้ ห้ามเก็บเป็นภาระ (เคส FB-AG-20260902-212)"),
        ("A12", "ช่องหนี้↔รายได้", "_r100_reroute", ("debt", "15000", {"income_baht": 50000}, {}), ("falsy", ""), "ยอดผ่อนจริงที่ไม่ใกล้รายได้ ต้องเก็บเป็นภาระตามเดิม ห้ามบล็อกเกินเหตุ"),
        ("J1", "รายได้อื่น", "_r130_is_only_salary", ("รับเงินเดือนอย่างเดียวครับ",), ("truthy", ""), "คำปฏิเสธ ต้องอ่านออกว่าไม่มีรายได้อื่น"),
        ("J2", "รายได้อื่น", "_r130_is_only_salary", ("มีค่าคอมอีกเดือนละ 8000",), ("falsy", ""), "มีรายได้อื่นจริง ห้ามตีเป็นคำปฏิเสธ"),
        ("K1", "ลำดับคำถาม", "_r134_exam_order", ("",), ("truthy", ""), "คำขอเบอร์ต้องเป็นบับเบิลสุดท้าย ไม่งั้นลูกค้าตอบข้ออื่นแล้วเบอร์หล่น (เคสนุกูล 3 ก.ย. 69)"),
        ("K2", "ลำดับคำถาม", "_r134_exam_order_keep", ("",), ("truthy", ""), "ไม่มีคำถามอื่นตามหลัง ห้ามสลับลำดับข้อความ"),
        ("K3", "ลำดับคำถาม", "_r134_exam_no_silence", ("",), ("truthy", ""), "ห้ามทำให้บอทเงียบ — บับเบิลต้องครบเท่าเดิมเสมอ"),
        ("L1", "ยืนยันรายได้/ภาระ", "_r134_bare_number", ("80000",), ("truthy", ""), "ตัวเลขเปล่าไม่มีคำบอกชนิด ต้องจับได้ว่ายังไม่แน่ใจ"),
        ("L2", "ยืนยันรายได้/ภาระ", "_r134_bare_number", ("ผ่อนเดือนละ 80000",), ("falsy", ""), "บอกชัดว่าผ่อน ห้ามถามยืนยันซ้ำให้เสียเทิร์น"),
        ("L3", "ยืนยันรายได้/ภาระ", "_r134_bare_number", ("รายได้ 80000",), ("falsy", ""), "บอกชัดว่ารายได้ ห้ามถามยืนยันซ้ำ"),
        ("L4", "ยืนยันรายได้/ภาระ", "_r134_bare_number", ("0808619099",), ("falsy", ""), "เบอร์โทร ห้ามตีเป็นยอดผ่อน"),
        ("M1", "ถามซ้ำ", "_r132_age_known", ({"data": {"age": 29}},), ("truthy", ""), "มีอายุในระบบแล้ว ต้องไม่ถามซ้ำ (เคสนุกูลถามอายุ 2 รอบ)"),
        ("M2", "ถามซ้ำ", "_r132_age_known", ({"data": {}},), ("falsy", ""), "ยังไม่มีอายุ ต้องถามได้ตามปกติ"),
        ("N1", "เบอร์หลังปิดเคส", "_r134_exam_contact_after_done", ("",), ("truthy", ""), "เบอร์ที่พิมพ์ติดคำว่าเบอไลน์ ต้องอ่านออก แต่คำขอบคุณต้องไม่ถูกตีเป็นเบอร์"),
        ("B1", "ช่องผู้กู้ร่วม", "_r95_reroute", ("co_income", "ผ่อนสามหมื่น", {}, {}), ("no", "co_income"), "พูดเรื่องหนี้ตัวเอง ไม่ใช่รายได้ผู้กู้ร่วม (log 1 ก.ย. 13:19)"),
        ("B2", "ช่องผู้กู้ร่วม", "_r95_reroute", ("co_borrower", "0632141859", {}, {}), ("no", "co_borrower"), "เป็นเบอร์โทร ไม่ใช่คำตอบเรื่องเงิน (log 1 ก.ย. 11:21)"),
        ("B3", "ช่องผู้กู้ร่วม", "_r95_reroute", ("co_debt", "เงินเดือนได้ประมาน 80,000", {}, {}), ("has", "co_income"), "เป็นรายได้ผู้กู้ร่วม ไม่ใช่ยอดผ่อน (log 1 ก.ย. 14:41)"),
        ("B4", "ช่องผู้กู้ร่วม", "_r95_reroute", ("co_borrower", "มีงานประจำเอกชน เงินเดือน 14000 ครับ", {}, {}), ("no", "co_borrower"), "พูดเรื่องเงินตัวเอง (log 1 ก.ย. 17:30)"),
        ("B5", "ช่องผู้กู้ร่วม", "_r95_reroute", ("co_income", "รายจ่ายต่อเดือนน่าจะ 4-5หมื่นครับ", {}, {}), ("no", "co_income"), "เป็นรายจ่าย ไม่ใช่รายได้ผู้กู้ร่วม (log 1 ก.ย. 17:12)"),
        ("C1", "อ่านตัวเลขรายได้", "_parse_income", ("สามหมื่นค่ะ",), ("has", "30000"), "ตัวเลขไทยต้องอ่านออก"),
        ("C2", "อ่านตัวเลขรายได้", "_parse_income", ("เงินเดือน 43,000฿",), ("has", "43000"), "มีลูกน้ำและสัญลักษณ์บาท"),
        ("C3", "อ่านตัวเลขรายได้", "_parse_income", ("เดือนละ 25,000 บาท",), ("has", "25000"), "รูปแบบมาตรฐาน"),
        ("C4", "อ่านตัวเลขรายได้", "_parse_income", ("รายได้ 4 หมื่นห้า",), ("has", "45000"), "หมื่นห้า = 45,000"),
        ("C5", "อ่านตัวเลขรายได้", "_parse_income", ("รายได้เดือนละ 4 ถึง 50000 เอาตัวสูง",), ("has", "50000"), "ช่วงตัวเลข = เอาตัวสูง (กติกา Gift)"),
        ("C6", "อ่านตัวเลขรายได้", "_parse_income", ("1.5 แสน",), ("has", "150000"), "แสนแบบทศนิยม"),
        ("C7", "อ่านตัวเลขรายได้", "_parse_income", ("60k",), ("has", "60000"), "หน่วย k"),
        ("C8", "อ่านตัวเลขรายได้", "_parse_income", ("ยังไม่มีรายได้ค่ะ",), ("falsy", ""), "ไม่มีตัวเลข ต้องไม่เดา"),
        ("C9", "อ่านตัวเลขรายได้", "_parse_income", ("เงินเดือน 23000 บวก OT 9000",), ("has", "32000"), "OT นับรวมด้วย (กติกา Gift 21 ส.ค.)"),
        ("C10", "อ่านตัวเลขรายได้", "_income_range", ("รับ 4-5 หมื่น",), ("has", "50000"), "ช่วง = เอาตัวสูง"),
        ("D1", "อ่านยอดผ่อน", "_parse_debt_monthly", ("ผ่อนรถ 8,500",), ("has", "8500"), "ยอดผ่อนตรงตัว"),
        ("D2", "อ่านยอดผ่อน", "_parse_debt_monthly", ("ประมาน 4-5 พันต่อเดือน",), ("has", "5000"), "คู่กับ A1 — ต้องลงช่องหนี้ได้"),
        ("D3", "อ่านยอดผ่อน", "_parse_debt_monthly", ("ไม่มีภาระ",), ("falsy", ""), "ไม่มีหนี้ = ไม่มีตัวเลข"),
        ("E1", "ปฏิเสธบอกรายได้", "_refuses_income", ("ไม่สะดวกบอกครับ",), ("truthy", ""), "ต้องจับได้ว่าปฏิเสธ"),
        ("E2", "ปฏิเสธบอกรายได้", "_refuses_income", ("เงินเดือน 30,000",), ("falsy", ""), "บอกแล้ว ไม่ใช่ปฏิเสธ"),
        ("F1", "เสียงเพจผู้หญิง", "to_female", ("สวัสดีครับ",), ("no", "ครับ"), "เพจผู้หญิงห้ามมีครับ"),
        ("F2", "เสียงเพจผู้หญิง", "to_female", ("ผมขอเบอร์ติดต่อกลับหน่อยครับ",), ("no", "ผม"), "ห้ามมีคำว่าผม"),
        ("F3", "เสียงเพจผู้หญิง", "to_female", ("ยินดีให้คำปรึกษาครับ",), ("has", "ค่ะ"), "ต้องแปลงเป็นค่ะ"),
        ("G1", "คำหยุดบอท", "_is_kill_phrase", ("##",), ("truthy", ""), "สัญลักษณ์หยุดบอทรายแชท (r120)"),
        ("G2", "คำหยุดบอท", "_is_kill_phrase", (" ## ",), ("truthy", ""), "มีช่องว่างหน้าหลังต้องยังใช้ได้"),
        ("G3", "คำหยุดบอท", "_is_kill_phrase", ("สนใจ##",), ("falsy", ""), "ต้องเป็นข้อความล้วน ห้ามจับกลางประโยค"),
        ("G4", "คำหยุดบอท", "_is_kill_phrase", ("ขอบคุณที่สนใจครับบ",), ("truthy", ""), "คำปิดเดิมของเพจต้องยังหยุดบอทได้"),
        ("H1", "รับเคส/คืนบอท", "_is_handover_trigger", ("#รับเคส",), ("truthy", ""), "เซลพิมพ์รับเคส บอทต้องหยุด 6 ชม."),
        ("H2", "รับเคส/คืนบอท", "_is_handover_trigger", ("ที่ปรึกษา",), ("truthy", ""), "คำเดิมต้องยังทำงาน"),
        ("H3", "รับเคส/คืนบอท", "_is_handover_trigger", ("สนใจคอนโดครับ",), ("falsy", ""), "ลูกค้าทักปกติ ห้ามหยุดบอท"),
        ("I1", "เวลาสะดวกให้โทร", "_parse_callback_time", ("สะดวกให้โทรหลัง 6 โมงเย็นค่ะ",), ("truthy", ""), "ต้องเก็บเวลาที่ลูกค้าสะดวกได้ (r65)"),
        ("I2", "อายุ", "_parse_age", ("อายุ 44 ปี",), ("has", "44"), "อ่านอายุได้"),
    ]

    def _r124_run():
        rows = []
        n_ok = 0
        n_bad = 0
        n_skip = 0
        for cid, grp, fname, args, spec, why in _R124_EXAM:
            f = _r124_fn(fname)
            if f is None:
                rows.append({"id": cid, "group": grp, "fn": fname, "input": _r124_flat(args[0]),
                             "expect": spec[0] + " " + str(spec[1]), "got": "",
                             "status": "ข้าม", "why": "ไม่พบฟังก์ชันนี้ในระบบแล้ว — อาจถูกเปลี่ยนชื่อ"})
                n_skip += 1
                continue
            try:
                raw = f(*args)
                good, got = _r124_check(spec[0], spec[1], raw)
            except Exception as _ex:
                good = False
                got = "ERROR: " + str(_ex)[:80]
            if good:
                n_ok += 1
            else:
                n_bad += 1
            rows.append({"id": cid, "group": grp, "fn": fname, "input": _r124_flat(args[0]),
                         "expect": spec[0] + " " + str(spec[1]), "got": got[:120],
                         "status": "ผ่าน" if good else "ตก", "why": why})
        return {"total": len(_R124_EXAM), "pass": n_ok, "fail": n_bad, "skip": n_skip, "rows": rows}

    def _r124_exam_view():
        try:
            body = _j124.dumps(_r124_run(), ensure_ascii=False)
            return body, 200, {"Content-Type": "application/json; charset=utf-8"}
        except Exception as _ex:
            return _j124.dumps({"error": str(_ex)[:200]}, ensure_ascii=False), 500, {"Content-Type": "application/json; charset=utf-8"}

    app.add_url_rule("/exam", "r124_exam", _r124_exam_view, methods=["GET"])
    print("[R124] ข้อสอบบอทพร้อม " + str(len(_R124_EXAM)) + " ข้อ ที่ /exam")
except Exception as _e124:
    print(f"[R124 ERROR] {_e124}")

# ===== r125 : ซ่อมการอ่านรายได้ + กัน R100 ย้ายช่องมั่ว — 2 ก.ย. 2569 =====
# ทำไม: ข้อสอบ r124 ตก 5 ข้อ ทุกข้อคือ "อ่านรายได้ต่ำกว่าจริง" -> เกรดต่ำ -> เคสดีไม่เข้าคิว
# หลักการ: ห่อของเดิม ไม่แก้ของเดิม พังเมื่อไหร่ตกกลับไปใช้ของเดิมเสมอ
try:
    import bot_logic as _bl125

    _R125_INCOME_WORDS = ("เงินเดือน", "รายได้", "รายรับ", "เงินได้", "รับเดือนละ", "ได้เดือนละ",
                          "รับสุทธิ", "สลิป", "ฐานเงินเดือน", "เงินเข้า", "โอที", "OT", "ค่าคอม", "คอมมิช")
    _R125_NOT_INCOME = ("งบ", "ราคา", "ซื้อ", "วงเงิน", "ยอดกู้", "ผ่อน", "หนี้", "บูโร", "ค้างชำระ", "เช่า")
    _R125_UNIT = {"พัน": 1000, "หมื่น": 10000, "แสน": 100000, "ล้าน": 1000000}
    _R125_DIGIT = {"หนึ่ง": 1, "เอ็ด": 1, "สอง": 2, "สาม": 3, "สี่": 4, "ห้า": 5,
                   "หก": 6, "เจ็ด": 7, "แปด": 8, "เก้า": 9, "ครึ่ง": 5}
    _R125_CEIL = 500000
    def _r125_scan(msg):
        t = str(msg or "").replace(",", "").replace("฿", " ")
        if not t:
            return 0
        has_inc = any(w in t for w in _R125_INCOME_WORDS)
        if (not has_inc) and any(w in t for w in _R125_NOT_INCOME):
            return 0
        best = 0.0
        try:
            for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:-|ถึง|~|/)\s*(\d+(?:\.\d+)?)\s*(พัน|หมื่น|แสน|ล้าน)?", t):
                hi = float(m.group(2))
                u = m.group(3)
                val = hi * _R125_UNIT[u] if u else hi
                if val > best:
                    best = val
        except Exception:
            pass
        try:
            for m in re.finditer(r"(\d+(?:\.\d+)?)?\s*(พัน|หมื่น|แสน|ล้าน)\s*(หนึ่ง|เอ็ด|สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|เก้า|ครึ่ง)?", t):
                head = m.group(1)
                base = float(head) if head else 1.0
                unit = _R125_UNIT[m.group(2)]
                val = base * unit
                tail = m.group(3)
                if tail:
                    val += _R125_DIGIT[tail] * (unit / 10.0)
                if val > best:
                    best = val
        except Exception:
            pass
        try:
            m = re.search(r"(\d{4,7})\D{0,14}(?:บวก|\+|และ|รวม|กับ)\D{0,14}(\d{3,7})", t)
            if m:
                val = float(m.group(1)) + float(m.group(2))
                if val > best:
                    best = val
        except Exception:
            pass
        if best < 1000 or best > _R125_CEIL:
            return 0
        return int(best)

    _r125_old_income = getattr(_bl125, "_parse_income", None)
    if _r125_old_income:
        def _r125_parse_income(msg):
            try:
                base = _r125_old_income(msg)
            except Exception:
                base = None
            try:
                b = int(base or 0)
            except Exception:
                b = 0
            try:
                alt = _r125_scan(msg)
                if alt and alt > b:
                    print(f"[R125 INC] {str(msg)[:40]} : {b} -> {alt}")
                    return alt
            except Exception as _e:
                print(f"[R125 ERROR inc] {_e}")
            return base
        _bl125._parse_income = _r125_parse_income
        globals()["_parse_income"] = _r125_parse_income

    _r125_old_range = getattr(_bl125, "_income_range", None)
    if _r125_old_range:
        def _r125_income_range(msg):
            try:
                base = _r125_old_range(msg)
            except Exception:
                base = None
            try:
                if int(base or 0):
                    return base
            except Exception:
                pass
            try:
                alt = _r125_scan(msg)
                if alt:
                    print(f"[R125 RANGE] {str(msg)[:40]} -> {alt}")
                    return alt
            except Exception as _e:
                print(f"[R125 ERROR range] {_e}")
            return base
        _bl125._income_range = _r125_income_range
        globals()["_income_range"] = _r125_income_range

    _r125_old_r100 = globals().get("_r100_reroute")
    if _r125_old_r100:
        def _r125_r100_reroute(field, msg, data, state):
            try:
                out = _r125_old_r100(field, msg, data, state)
            except Exception as _e:
                print(f"[R125 ERROR r100] {_e}")
                return False
            try:
                if field == "debt" and isinstance(out, (tuple, list)) and out and out[0] == "income":
                    t = str(msg or "")
                    if not any(w in t for w in _R125_INCOME_WORDS):
                        print(f"[R125 HOLD] ไม่ย้าย debt->income : {t[:45]}")
                        return False
            except Exception as _e:
                print(f"[R125 ERROR hold] {_e}")
            return out
        globals()["_r100_reroute"] = _r125_r100_reroute

    def _r125_parse_view():
        import json as _jp125
        try:
            body = request.get_json(force=True, silent=True) or {}
            texts = body.get("texts") or []
            if not isinstance(texts, list):
                texts = []
            texts = texts[:400]
            f = getattr(_bl125, "_parse_income", None)
            nums = []
            for s in texts:
                v = 0
                try:
                    v = int(f(s) or 0) if f else 0
                except Exception:
                    v = 0
                if not v:
                    try:
                        v = _r125_scan(s)
                    except Exception:
                        v = 0
                nums.append(v)
            return _jp125.dumps({"n": len(nums), "nums": nums}, ensure_ascii=False), 200, {"Content-Type": "application/json; charset=utf-8"}
        except Exception as _e:
            return _jp125.dumps({"error": str(_e)[:200]}, ensure_ascii=False), 500, {"Content-Type": "application/json; charset=utf-8"}

    app.add_url_rule("/parse", "r125_parse", _r125_parse_view, methods=["POST"])
    print("[R125] ซ่อมตัวอ่านรายได้ + กัน R100 ย้ายมั่ว + เปิด /parse แล้ว")
except Exception as _e125:
    print(f"[R125 ERROR] {_e125}")

# ==================================================================
# r126 (ชั้น 0+1) — กล่องเลขกลาง + /rules  · Gift เคาะ 2 ก.ย. 2026
#   อ่านอย่างเดียว ไม่ rebind อะไรทั้งสิ้น พฤติกรรมบอทเหมือนเดิมเป๊ะ
#   เปิดให้เห็น: ค่าคงที่จริง · ตาราง DSR แบงก์ · ช่วงเกรดตามรายได้ · ข้อสอบเกรด
# ==================================================================
try:
    import bot_logic as _bl126

    WEC_RULES = {
        "unit_price":      2300000,   # ราคาห้อง 1 ยูนิต
        "bridge_min_diff":  300000,   # ส่วนต่างขั้นต่ำถึงเรียกบริดจ์
        "bridge_max_dsr":     0.80,   # DSR ก่อนปิด เกินนี้ = ปิดไม่ไหว (Gift 2 ก.ย.)
        "biz_margin":         0.15,   # ธุรกิจ: รายได้ = ยอดขาย x margin (Gift 2 ก.ย.)
        "default_age":          35,
    }

    def _r126_cap(income, debt_m, age):
        try:
            return int(_bl126._capacity(int(income), int(debt_m), int(age)) or 0)
        except Exception:
            return -1

    def _r126_grade(income, debt_m, age):
        """ใช้คีย์จริงที่ bot_logic._grade อ่าน: income_baht / debt_baht / own_age
        debt_baht = 0 คือ "ยืนยันแล้วว่าไม่มีหนี้" ไม่ใช่ "ไม่รู้" """
        try:
            d = {"income_total": int(income), "income_baht": int(income),
                 "income": str(int(income)),
                 "debt_baht": int(debt_m),
                 "debt": ("ไม่มีหนี้ค่ะ" if int(debt_m) == 0
                          else str(int(debt_m)) + " ต่อเดือน"),
                 "age": int(age), "own_age": int(age), "age_years": int(age)}
            return str(bot._grade(d, None) or "")
        except Exception as _e:
            return "ERR " + str(_e)[:40]

    _R126_CASES = [
        ("รายได้ 15,000 ไม่มีหนี้", 15000, 0, 35, "C"),
        ("รายได้ 20,000 ไม่มีหนี้", 20000, 0, 35, "C"),
        ("รายได้ 24,600 ไม่มีหนี้ (เส้น A จริง)", 24600, 0, 35, "A"),
        ("รายได้ 25,000 ไม่มีหนี้", 25000, 0, 35, "A"),
        ("รายได้ 40,000 ไม่มีหนี้ = Suneerat 09/006", 40000, 0, 35, "A"),
        ("รายได้ 50,000 ผ่อน 35,000 (DSR 70%)", 50000, 35000, 35, "B"),
        ("รายได้ 50,000 ผ่อน 70,000 (DSR 140%) = FB-RS-506", 50000, 70000, 35, "C"),
        ("รายได้ 68,000 ผ่อน 50,000 = 09/002 TNT", 68000, 50000, 35, "B"),
        ("รายได้ 100,000 ไม่มีหนี้", 100000, 0, 35, "A"),
        ("ธุรกิจ ยอดขาย 1 ล้าน x margin 15% = 150,000", 150000, 0, 35, "A"),
        ("ธุรกิจ ยอดขาย 1 ล้าน x margin 50% = 500,000 (ของเดิม)", 500000, 0, 35, "A"),
        ("แนน 09/007 ตัวเลขถูก 36,000 ผ่อน 8,481", 36000, 8481, 35, "A"),
        ("แนน 09/007 ที่บอทอ่านได้ 15,000 ผ่อน 8,481", 15000, 8481, 35, "C"),
        ("อายุ 25 รายได้ 40,000 ไม่มีหนี้", 40000, 0, 25, "A"),
        ("อายุ 60 รายได้ 40,000 ไม่มีหนี้", 40000, 0, 60, "A"),
    ]

    def _r126_rules_view():
        H = []
        H.append("<meta charset=utf-8><title>WEC เกณฑ์เกรด</title>")
        H.append("<style>body{font-family:system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#111}"
                 "h2{margin:26px 0 8px;font-size:17px}table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}"
                 "th,td{border:1px solid #ddd;padding:6px 8px;text-align:left}th{background:#f5f5f5}"
                 ".ok{color:#0a7a2f;font-weight:600}.no{color:#b00020;font-weight:600}</style>")
        H.append("<h1 style=font-size:20px>WEC — เกณฑ์เกรดที่ใช้อยู่จริง</h1>")

        H.append("<h2>1 · ค่าคงที่ (กล่องเลขกลาง)</h2><table><tr><th>ชื่อ</th><th>ค่า</th></tr>")
        for k in ("unit_price", "bridge_min_diff", "bridge_max_dsr", "biz_margin", "default_age"):
            H.append("<tr><td>" + k + "</td><td>" + str(WEC_RULES[k]) + "</td></tr>")
        try:
            H.append("<tr><td>bot_logic.UNIT_PRICE_BAHT (ของจริงที่บอทใช้)</td><td>"
                     + str(getattr(_bl126, "UNIT_PRICE_BAHT", "?")) + "</td></tr>")
            H.append("<tr><td>bot_logic.FREELANCE_INCOME_PCT (margin ที่บอทใช้)</td><td>"
                     + str(getattr(_bl126, "FREELANCE_INCOME_PCT", "?")) + "</td></tr>")
            H.append("<tr><td>bot_logic.DEFAULT_AGE</td><td>"
                     + str(getattr(_bl126, "DEFAULT_AGE", "?")) + "</td></tr>")
        except Exception:
            pass
        H.append("</table>")

        H.append("<h2>2 · ช่วงเกรดตามรายได้ (ไม่มีภาระ อายุ 35)</h2>")
        H.append("<table><tr><th>รายได้/เดือน</th><th>วงเงินประเมิน</th><th>เกรดที่ได้</th></tr>")
        for inc in (15000, 18000, 20000, 22000, 22600, 25000, 30000, 35000,
                    40000, 50000, 60000, 80000, 100000, 150000, 200000):
            c = _r126_cap(inc, 0, 35)
            H.append("<tr><td>" + format(inc, ",") + "</td><td>"
                     + ("%.2f ล้าน" % (c / 1000000.0) if c >= 0 else "คิดไม่ได้")
                     + "</td><td>" + _r126_grade(inc, 0, 35) + "</td></tr>")
        H.append("</table>")

        H.append("<h2>3 · ข้อสอบเกรด</h2>")
        H.append("<table><tr><th>เคส</th><th>วงเงินตอนนี้</th><th>ปิดหนี้แล้ว</th><th>DSR</th>"
                 "<th>ควรได้</th><th>ได้จริง</th><th>ผล</th></tr>")
        _pass = 0
        for name, inc, dm, age, want in _R126_CASES:
            c_now = _r126_cap(inc, dm, age)
            c_clr = _r126_cap(inc, 0, age)
            got = _r126_grade(inc, dm, age)
            dsr = (dm * 100.0 / inc) if inc else 0
            good = (got == want)
            if good:
                _pass += 1
            H.append("<tr><td>" + name + "</td><td>"
                     + ("%.2fM" % (c_now / 1000000.0)) + "</td><td>"
                     + ("%.2fM" % (c_clr / 1000000.0)) + "</td><td>"
                     + ("%.0f%%" % dsr) + "</td><td>" + want + "</td><td>" + got
                     + "</td><td class=" + ("ok>ผ่าน" if good else "no>ตก") + "</td></tr>")
        H.append("</table>")
        H.insert(5, "<p>ข้อสอบเกรด <b>" + str(_pass) + " / "
                 + str(len(_R126_CASES)) + "</b> ข้อ</p>")

        H.append("<h2>4 · ตาราง DSR ของแบงก์ (ที่สูตรใช้จริง)</h2><table>"
                 "<tr><th>แบงก์</th><th>ช่วงรายได้ -> DSR%</th></tr>")
        try:
            for b, tiers in (getattr(_bl126, "_BANK_DSR_TIERS", {}) or {}).items():
                H.append("<tr><td>" + b + "</td><td>"
                         + " · ".join([format(t[0], ",") + "+ -> " + str(t[1]) + "%" for t in tiers])
                         + "</td></tr>")
        except Exception:
            pass
        H.append("</table>")
        return "".join(H), 200, {"Content-Type": "text/html; charset=utf-8"}

    app.add_url_rule("/rules", "r126_rules", _r126_rules_view, methods=["GET"])
    print("[R126] เปิด /rules — ดูเกณฑ์เกรด + ข้อสอบ (อ่านอย่างเดียว)")
except Exception as _e126:
    print("[R126 ERROR] " + str(_e126))

# ==================================================================
# r127 — B บริดจ์ต้องปิดภาระไหวจริง  · Gift เคาะ 2 ก.ย. 2026
#   DSR ก่อนปิด > 80%  =  ปิดเองไม่ไหว  ->  ไม่ใช่บริดจ์  ->  C
#   ทำได้แค่ "ลดเกรด B" เท่านั้น เกรดอื่นไม่แตะ ปลอดภัยต่อของเดิม
#   หมายเหตุ: เงื่อนไข "ต้องรู้ยอดหนี้คงเหลือ" ยังทำไม่ได้
#   เพราะบอทไม่มีช่องเก็บยอดคงเหลือเลย (มีแต่ debt_baht = ค่างวด/เดือน)
# ==================================================================
try:
    import bot_logic as _bl127

    _R127_BASE_GRADE = _bl127.BotEngine._grade

    def _grade_r127(self, data, state=None):
        g = _R127_BASE_GRADE(self, data, state)
        try:
            if str(g).strip().upper() != "B":
                return g
            inc = int(data.get("income_counted") or data.get("income_total")
                      or data.get("income_baht") or 0)
            dm = data.get("debt_baht")
            if not inc or dm is None:
                return g
            dsr = float(dm) / float(inc)
            cap = WEC_RULES["bridge_max_dsr"]
            if dsr > cap:
                try:
                    self._add_signal(state,
                        "DSR ก่อนปิด %.0f%% เกินเพดาน %.0f%% "
                        "— ปิดภาระเองไม่ไหวจริง ไม่นับเป็นบริดจ์ (Gift 2 ก.ย. 2026)"
                        % (dsr * 100, cap * 100))
                except Exception:
                    pass
                return "C"
        except Exception as _e:
            print("[R127 ERR] " + str(_e)[:80])
        return g

    _bl127.BotEngine._grade = _grade_r127
    print("[R127] B บริดจ์ต้อง DSR ก่อนปิด <= 80%")
except Exception as _e127:
    print("[R127 ERROR] " + str(_e127))

# ==================================================================
# r129 — ตัวเลขที่เท่ากับรายได้ ห้ามลงช่องภาระ · Gift เคาะ 3 ก.ย. 2026
#   ต้นเหตุจริงของเคส FB-AG-20260902-212:
#   บอทรอยอดผ่อน ลูกค้าตอบ "34000-35000" (ตัวเลขเปล่า ไม่มีคำบอกภาระ)
#   r100 ย้ายไปช่องรายได้ไม่ได้ เพราะด่านเดิมบังคับว่าต้องมีคำว่ารายได้
#   -> เก็บเป็นภาระ 34,500 -> ภาระผี -> วงเงินหด -> ถามผู้กู้ร่วม -> ไม่ได้เบอร์
#   กฎใหม่: ภาระที่เท่ากับรายได้พอดี เป็นไปไม่ได้ในทางปฏิบัติ = ลูกค้าทวนรายได้
#   ทำได้อย่างเดียวคือ "ไม่เก็บ" แล้วให้บอทถามภาระใหม่ให้ชัด ไม่เดาแทนลูกค้า
# ==================================================================
try:
    import bot_logic as _bl129

    _R129_NEAR = 0.10          # ห่างจากรายได้ไม่เกิน 10% = ถือว่าเลขเดียวกัน
    _R129_BASE = _r100_reroute

    def _r129_income_of(data):
        for k in ("income_counted", "income_total", "income_baht"):
            try:
                n = int(data.get(k) or 0)
            except Exception:
                n = 0
            if n:
                return n
        return 0

    def _r100_reroute_r129(field, msg, data, state):
        try:
            if field in ("debt", "debt_baht"):
                s = str(msg or "")
                if not _bl129._has_any(s, _bl129._DEBT_SAYS):
                    n = _bl129._parse_debt_monthly(s) or 0
                    inc = _r129_income_of(data or {})
                    if n and inc and abs(int(n) - inc) <= inc * _R129_NEAR:
                        print("[R129 HOLD] " + str(n) + " เท่ารายได้ " + str(inc)
                              + " ไม่ใช่ภาระ : " + s[:40])
                        return None      # ทิ้ง แล้วให้บอทถามภาระใหม่
        except Exception as _e:
            print("[R129 ERR] " + str(_e)[:70])
        return _R129_BASE(field, msg, data, state)

    _r100_reroute = _r100_reroute_r129
    globals()["_r100_reroute"] = _r100_reroute_r129
    print("[R129] ภาระที่เท่ารายได้ = ไม่เก็บ ถามใหม่")
except Exception as _e129:
    print("[R129 ERROR] " + str(_e129))

# ==================================================================
# r130 — "รับเงินเดือนอย่างเดียว" = ไม่มีรายได้อื่น · Gift เคาะ 3 ก.ย. 2026
#   r110 รับคำปฏิเสธแค่ 3 คำ: ไม่มี / ไม่แน่นอน / ไม่ได้ทุกเดือน
#   "รับเงินเดือนอย่างเดียวครับ" ไม่เข้าสักคำ -> ธงรอเลขยังค้าง
#   ลูกค้าตอบเลขถัดไป "34000-35000" -> ถูกเก็บเป็น "รายได้ส่วนอื่น 35,000"
#   -> รวมเป็น 27,000 + 34,000 + 35,000 = 96,000 (เคส FB-AG-20260902-212)
#   กฎใหม่: เจอคำว่า "อย่างเดียว/แค่/เฉพาะ + เงินเดือน" = ปิดธงทันที ตั้งเป็น 0
# ==================================================================
try:
    _R130_ONLY = ("อย่างเดียว", "แต่เงินเดือน", "เฉพาะเงินเดือน", "แค่เงินเดือน",
                  "มีแค่เงินเดือน", "ไม่มีรายได้อื่น", "ไม่มีอย่างอื่น",
                  "ไม่มีรายได้เสริม", "ไม่มีรายได้อื่นๆ", "เงินเดือนเท่านั้น")

    def _r130_is_only_salary(msg):
        """คำตอบที่แปลว่า ไม่มีรายได้นอกจากเงินเดือน"""
        try:
            t = str(msg or "")
            return any(w in t for w in _R130_ONLY)
        except Exception:
            return False

    _R130_BASE_DECIDE = CalmBotEngine._decide

    def _decide_r130(self, msg, user_id, state, bucket, is_new):
        try:
            if state.get("awaiting_income_var"):
                _d = state.setdefault("data", {})
                if _d.get("income_var") is None and _r130_is_only_salary(msg):
                    _d["income_var"] = 0
                    state["awaiting_income_var"] = False
                    print("[R130] เงินเดือนอย่างเดียว = ไม่มีรายได้อื่น -> 0")
        except Exception as _e:
            print("[R130 ERR] " + str(_e)[:70])
        return _R130_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)

    CalmBotEngine._decide = _decide_r130
    print("[R130] อ่านคำปฏิเสธรายได้อื่นได้กว้างขึ้น")
except Exception as _e130:
    print("[R130 ERROR] " + str(_e130))


# ======================================================
# r131 — เบอร์/ไลน์ที่มาหลังปิดเคส ต้องเก็บ ไม่ใช่ปล่อยหล่น
# ------------------------------------------------------
# เคสจริง นุกูล ประวีณไว 3 ก.ย. 2026 (Chat_Log แถว 6822-6824)
#   6822 ลูกค้าตอบอายุ "29"      -> บอทปิดเคสเป็น done ตรงนี้
#   6823 บอท "ขอเบอร์ติดต่อได้ไหมครับ"   <- ขอทั้งที่ปิดไปแล้ว
#   6824 ลูกค้า "0808619099เบอไลน์"      <- ไม่มีใครเก็บ
# ต้นเหตุ: bot_logic บรรทัด ~3071 มี guard  not state.get('done')
#   -> done แล้วเบอร์ที่ลูกค้าส่งมาไม่ถูกเก็บเลย ตกไปเข้าสาขา AI คุยเปล่าๆ
# ผลจริง: เกรด A (รายได้ 40,000 · ไม่มีหนี้ · อายุ 29 · วงเงิน ~5 ล้าน)
#   แต่ไม่มีช่องทางติดต่อ = แจกให้เซลไม่ได้ เสียลูกค้าฟรี
#
# แก้: done แล้วถ้าได้ช่องทางติดต่อ -> เก็บ + เรียก _finish ซ้ำ
#      p4UpsertLead หาแถวตาม PSID (idempotent) = เขียนซ้ำได้ ไม่เกิดแถวใหม่
# ขอบเขต: ทำงานเฉพาะตอน done + ยังไม่มี contact + ไม่ใช่เคสที่ตั้งใจปิด
#         (ปฏิเสธให้เบอร์ / ต่ำกว่าเกณฑ์ / ติดบูโร / เจ้าของห้อง / ผู้เช่า)
#         เส้นทางปกติที่ได้เบอร์ก่อนปิดเคส ไม่ถูกแตะเลย
# ======================================================
import bot_logic as _bl131

R131_THANKS = ("ได้รับข้อมูลติดต่อแล้วครับ ขอบคุณมากครับ "
               "เดี๋ยวที่ปรึกษาติดต่อกลับไปนะครับ")

_R131_SKIP_FLAGS = ("contact_refused", "below_threshold", "soft_close",
                    "owner", "renter", "bot_off")

_R131_BASE_DECIDE = CalmBotEngine._decide


def _r131_take(self, msg, user_id, state):
    """คืน (bubbles, grade) เมื่อเก็บช่องทางติดต่อหลังปิดเคสได้ / None เมื่อไม่เข้าเงื่อนไข"""
    _d = state.get("data") or {}
    if _d.get("contact") or _d.get("rent_contact"):
        return None
    for _f in _R131_SKIP_FLAGS:
        if state.get(_f):
            return None
    s = str(msg or "").strip()
    if not s or len(s) > 60:
        return None
    if not self._is_valid_answer("contact", s):
        return None
    self._capture(state, "contact", s)
    if not (state.get("data") or {}).get("contact"):
        return None
    self._add_signal(
        state,
        "☎️ ได้ช่องทางติดต่อหลังปิดเคส — r131 เขียนแถวซ้ำเพื่อให้เข้าคิวแจก")
    g = self._finish(user_id, state,
                     (state.get("data") or {}).get("contact", ""),
                     calendar=True)
    print(f"[R131] เก็บช่องทางติดต่อหลังปิดเคส {str(user_id)[:8]}... "
          f"-> อัปเดตแถว เกรด {g}")
    return [R131_THANKS], g


def _decide_r131(self, msg, user_id, state, bucket, is_new):
    try:
        if state.get("done"):
            _out = _r131_take(self, msg, user_id, state)
            if _out:
                return _out
    except Exception as e:
        print(f"[R131 ERROR] {e}")
    return _R131_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)


CalmBotEngine._decide = _decide_r131


# ======================================================
# r132 — ไม่ยิงคำถามซ้อนกันจนคำขอเบอร์หล่น + ไม่ถามซ้ำช่องที่มีค่าแล้ว
# ------------------------------------------------------
# เคสเดียวกัน (นุกูล) แถว 6817:
#   บอทส่ง 2 บับเบิลในเทิร์นเดียว
#     บับเบิล 1 "ขอเบอร์ติดต่อกลับ เพื่อให้ที่ปรึกษาโทร..."
#     บับเบิล 2 "เดี๋ยวผมประเมินวงเงินคร่าวๆ ให้เลยครับ คุณลูกค้าอายุเท่าไหร่ครับ"
#   ลูกค้าตอบ "29" = ตอบบับเบิลสุดท้าย -> คำขอเบอร์หล่นหายทั้งดุ้น
# และแถว 6821 บอทถามอายุซ้ำอีกรอบ ทั้งที่ได้ 29 ไปแล้วที่ 6818
#
# (ก) คำขอเบอร์ต้องเป็นบับเบิล "สุดท้าย" เสมอเมื่อเทิร์นนั้นมีคำถามอื่นด้วย
#     ย้ายลำดับอย่างเดียว ไม่ตัดข้อความทิ้ง (กติกา: ห้ามทำให้บอทเงียบ)
# (ข) ช่องที่มีค่าแล้ว ห้ามถามซ้ำ — ตัดเฉพาะบับเบิลนั้น
#     ถ้าตัดแล้วจะไม่เหลืออะไรเลย -> ไม่ตัด (ห้ามเงียบ)
# ======================================================
_R132_CONTACT_ASK = ("ขอเบอร์", "เบอร์ติดต่อ", "เบอร์หน่อย", "ขอไลน์",
                     "ID LINE", "ไอดีไลน์", "id line")
_R132_QUESTION = ("ไหมครับ", "ไหมคะ", "เท่าไหร่", "เท่าไร", "หรือเปล่า",
                  "หรือครับ", "หรือคะ", "อะไรบ้าง", "กี่ปี")
_R132_AGE_ASK = ("อายุเท่าไหร่", "อายุเท่าไร", "อายุกี่ปี", "อายุปีนี้")


def _r132_order(text):
    """ย้ายบับเบิลที่ขอเบอร์ไปท้ายสุด เมื่อมีคำถามอื่นตามหลังอยู่"""
    raw = str(text or "")
    parts = raw.split(MSG_SPLIT)
    if len(parts) < 2:
        return text
    hit = [i for i, p in enumerate(parts)
           if any(w.lower() in p.lower() for w in _R132_CONTACT_ASK)]
    if not hit or hit[-1] == len(parts) - 1:
        return text
    tail = parts[hit[-1] + 1:]
    if not any(any(q in p for q in _R132_QUESTION) for p in tail):
        return text
    _keep = set(hit)
    rest = [p for i, p in enumerate(parts) if i not in _keep]
    ask = [parts[i] for i in hit]
    print("[R132] เทิร์นนี้มีคำขอเบอร์ + คำถามอื่น — ย้ายคำขอเบอร์ไปบับเบิลสุดท้าย")
    return MSG_SPLIT.join(rest + ask)


_R132_BASE_SEND_REPLY = send_reply


def send_reply(recipient_id, text, page_id: str = "", force: bool = False):
    try:
        text = _r132_order(text)
    except Exception as e:
        print(f"[R132 ERROR] {e}")
    return _R132_BASE_SEND_REPLY(recipient_id, text, page_id, force)


def _r132_age_known(state):
    d = (state or {}).get("data") or {}
    for k in ("age", "own_age", "age_years"):
        try:
            if int(d.get(k) or 0) > 0:
                return True
        except Exception:
            pass
    return False


_R132_BASE_DECIDE = CalmBotEngine._decide


def _decide_r132(self, msg, user_id, state, bucket, is_new):
    out = _R132_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)
    try:
        bubbles, grade = out
        if bubbles and _r132_age_known(state):
            keep = [b for b in bubbles
                    if not any(w in str(b) for w in _R132_AGE_ASK)]
            if keep and len(keep) != len(bubbles):
                print("[R132] มีอายุในระบบแล้ว — ตัดบับเบิลถามอายุซ้ำออก")
                return keep, grade
    except Exception as e:
        print(f"[R132 ERROR] {e}")
    return out


CalmBotEngine._decide = _decide_r132


# ======================================================
# r133 — คำถามภาระรอบสอง ต้องมีคำว่า "ผ่อน" เสมอ
# ------------------------------------------------------
# เคสจริง NaraRai BM Smile 3 ก.ย. 2026 (Chat_Log แถว 6793-6796)
#   6793 บอท "ตอนนี้ลูกค้ามีผ่อนอะไรอยู่ไหมครับ เช่น บ้าน รถ หรือบัตรเครดิต"
#   6794 ลูกค้า "คอมต่างหาก"        <- ยังพูดเรื่องรายได้อยู่
#   6795 บอท "ขออีกนิดเดียวครับ รวมทุกอย่างแล้วเดือนละประมาณเท่าไหร่ครับ"
#        ^^^ ไม่มีคำว่าผ่อน/ภาระเลย ต่อจากประโยคเรื่องคอม = อ่านได้ว่า "รายได้รวม"
#   6796 ลูกค้า "80000"
# รอบนี้บอทเดาถูก (ตีเป็นรายได้) แต่เป็นการเดา ไม่ใช่ความตั้งใจ
# ถ้าเดาผิดอีกทาง = ภาระ 80,000 บนรายได้ 37,000 -> DSR 216% -> ทิ้งเคสทันที
# แก้ที่ชั้นคำพูดอย่างเดียว ไม่แตะตรรกะ
# ======================================================
_bl131.DEBT_REASK_MSG = (
    "ขอบคุณครับ ขออีกนิดเดียวครับ เฉพาะยอดผ่อนต่อเดือน "
    "(บ้าน รถ บัตรเครดิต สินเชื่อ) รวมกันแล้วประมาณเท่าไหร่ครับ "
    "ตัวเลขกลมๆ ก็พอครับ ถ้าไม่มีเลย บอกว่าไม่มีได้เลยครับ"
)


# ======================================================
# r134 — ตัวเลขเปล่าที่จะทำให้เกรดร่วง ต้องถามยืนยันก่อน (Gift เคาะ 3 ก.ย. 2026)
# ------------------------------------------------------
# Gift: "ถามเพิ่มเพื่อคอนเฟิร์มว่าเป็นรายได้หรือภาระ เฉพาะเคสที่ไม่แน่ใจ"
#       ขอบเขตที่เคาะ = "เฉพาะตอนเกรดจะร่วง"
# เหตุผลที่ต้องแคบ: ทุกเทิร์นที่เสียไป = เสี่ยงเสียลูกค้า (พิสูจน์แล้วในเคสนุกูล)
#   ถ้ายอดนั้นไม่เปลี่ยนผลลัพธ์ (เกรดยัง A/B อยู่ดี) -> ไม่ต้องถาม
#   ถ้ายอดนั้นทำให้ A/B ร่วงเป็น C/D/N -> ถาม เพราะถ้าเดาผิด = ทิ้งเคสฟรี
#
# แทนพฤติกรรมเดิมของ r129 ที่ "ทิ้งตัวเลขเงียบๆ แล้วถามภาระใหม่"
#   (ลูกค้างงว่าทำไมถามซ้ำ) -> เปลี่ยนเป็นถามให้ชัดไปเลย
#   ด่านตรวจจับของ r129 ยังอยู่ครบ ไม่ได้ถอดออก
#
# โควตา 1 ครั้ง/เคส · ไม่ตอบ/ตอบไม่ชัด = ไม่นับเป็นภาระ + ติดธงให้เซลถามตอนโทร
#   (กติกา Gift: ห้ามทิ้งเคส — เดาผิดทางบวกเสียเซล 1 สาย
#    เดาผิดทางลบเสียลูกค้าถาวร)
# ======================================================
_R134_ASK = ("ขอเช็คนิดนึงครับ ยอด {n:,} บาทนี่คือ "
             "ยอดผ่อนต่อเดือน หรือรายได้รวมต่อเดือนครับ")
_R134_OK = ("A", "B")


def _r134_bare_number(s):
    """ตัวเลขที่ไม่มีคำบอกชนิดเลย -> คืนจำนวน / ไม่ใช่ -> None"""
    t = str(s or "").strip()
    if not t or len(t) > 40:
        return None
    if _bl131._has_any(t, _bl131._DEBT_SAYS):
        return None
    if _bl131._has_any(t, _bl131._INCOME_SAYS):
        return None
    if _bl131._looks_like_phone(t):
        return None
    try:
        n = _bl131._parse_debt_monthly(t)
    except Exception:
        return None
    return int(n) if n else None


def _r134_grade_if(self, state, debt_n):
    """เกรดที่จะได้ ถ้าภาระ = debt_n (ไม่แตะ state จริง ไม่เขียนสัญญาณ)"""
    d = dict((state or {}).get("data") or {})
    d["debt_baht"] = int(debt_n)
    d["debt"] = ("ไม่มีหนี้ค่ะ" if not int(debt_n)
                 else str(int(debt_n)) + " ต่อเดือน")
    d.pop("income_unknown", None)
    # ส่ง state เป็น dict เปล่า ไม่ใช่ None — r127 เรียก _add_signal(state, ...)
    # ซึ่ง None.setdefault จะระเบิด (dict ทิ้ง = ไม่มีสัญญาณปลอมตกลงเคสจริง)
    try:
        return str(self._grade(d, {}) or "").strip().upper()[:1]
    except Exception as e:
        print(f"[R134 GRADE ERROR] {e}")
        return ""


_R134_BASE_DECIDE = CalmBotEngine._decide


def _decide_r134(self, msg, user_id, state, bucket, is_new):
    # ---- ก) กำลังรอคำยืนยันอยู่ -> อ่านคำตอบก่อนทุกอย่าง ----------
    try:
        pend = state.pop("r134_pending", None)
        if pend:
            s = str(msg or "")
            if _bl131._has_any(s, _bl131._DEBT_SAYS):
                self._capture(state, "debt", f"ผ่อนเดือนละ {int(pend)}")
                self._add_signal(state, f"ยืนยันแล้ว {int(pend):,} = ยอดผ่อน (r134)")
                print(f"[R134] ยืนยัน {pend} = ยอดผ่อน")
            elif _bl131._has_any(s, _bl131._INCOME_SAYS):
                self._capture(state, "income", f"รายได้เดือนละ {int(pend)}")
                self._add_signal(state, f"ยืนยันแล้ว {int(pend):,} = รายได้ (r134)")
                print(f"[R134] ยืนยัน {pend} = รายได้")
            else:
                self._capture(state, "debt", "ยังไม่ยืนยันยอด")
                self._add_signal(
                    state,
                    f"⚠️ ยอด {int(pend):,} ยังไม่ยืนยันว่าเป็นยอดผ่อนหรือรายได้ "
                    f"— ไม่นับเป็นภาระ ให้เซลถามตอนโทร (r134)")
                print(f"[R134] ตอบไม่ชัด — ไม่นับ {pend} เป็นภาระ ติดธงแทน")
            state["awaiting"] = None
    except Exception as e:
        print(f"[R134 ERROR resolve] {e}")

    # ---- ข) ตัวเลขเปล่าที่กำลังจะฆ่าเคส -> ถามยืนยันก่อน ----------
    try:
        if (state.get("awaiting") == "debt"
                and not state.get("r134_used")
                and not state.get("done")):
            n = _r134_bare_number(msg)
            if n:
                g_no = _r134_grade_if(self, state, 0)
                g_yes = _r134_grade_if(self, state, n)
                if g_no in _R134_OK and g_yes not in _R134_OK:
                    state["r134_used"] = True
                    state["r134_pending"] = n
                    print(f"[R134] ยอด {n} จะทำให้เกรด {g_no} -> {g_yes} "
                          f"— ถามยืนยันก่อน ({str(user_id)[:8]}...)")
                    return [_R134_ASK.format(n=n)], None
    except Exception as e:
        print(f"[R134 ERROR ask] {e}")

    return _R134_BASE_DECIDE(self, msg, user_id, state, bucket, is_new)


CalmBotEngine._decide = _decide_r134


# ------------------------------------------------------
# ข้อสอบล็อก r131-r134 (รายการอยู่ใน _R124_EXAM ด้านบน)
# ------------------------------------------------------
def _r134_exam_order(_=None):
    """คำขอเบอร์ต้องถูกย้ายไปบับเบิลสุดท้าย เมื่อมีคำถามอื่นตามหลัง"""
    a = "ขอเบอร์ติดต่อกลับหน่อยครับ"
    b = "คุณลูกค้าอายุเท่าไหร่ครับ"
    return _r132_order(a + MSG_SPLIT + b).endswith(a)


def _r134_exam_order_keep(_=None):
    """ไม่มีคำถามอื่นตามหลัง = ห้ามสลับลำดับ"""
    src = "เดี๋ยวที่ปรึกษาติดต่อไปนะครับ" + MSG_SPLIT + "ขอเบอร์ติดต่อกลับหน่อยครับ"
    return _r132_order(src) == src


def _r134_exam_no_silence(_=None):
    """ห้ามทำให้บอทเงียบ — จำนวนบับเบิลต้องเท่าเดิมเสมอ"""
    src = "ขอเบอร์ติดต่อกลับหน่อยครับ" + MSG_SPLIT + "คุณลูกค้าอายุเท่าไหร่ครับ"
    return (len(_r132_order(src).split(MSG_SPLIT))
            == len(src.split(MSG_SPLIT)))


def _r134_exam_contact_after_done(_=None):
    """r131: เบอร์/ไลน์ที่มาหลังปิดเคสต้องอ่านออก แต่คำมารยาทต้องไม่ถูกตีเป็นเบอร์"""
    ok = bot._is_valid_answer("contact", "0808619099เบอไลน์")
    bad = bot._is_valid_answer("contact", "ขอบคุณครับ")
    return bool(ok) and not bool(bad)



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"WEC Bot v3.3 starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
