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

load_dotenv()

# ======================================================
# Config — ใช้ Environment Variables ชุดเดิมทั้งหมด
# ======================================================
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
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


def send_message(recipient_id: str, text: str):
    if not FB_PAGE_ACCESS_TOKEN:
        print(f"[NO TOKEN] Would send to {recipient_id}: {text[:80]}")
        return
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
                params={"access_token": FB_PAGE_ACCESS_TOKEN},
                json=payload,
                timeout=10,
            )
            if resp.status_code != 200:
                print(f"[FB SEND ERROR] {resp.status_code}: {resp.text[:200]}")
                log_event("SEND_ERROR", f"HTTP {resp.status_code}",
                          {"body": resp.text[:300]})
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


def alert_gift(sender_id: str, user_text: str, ad_id: str = ""):
    """แจ้งเตือน Gift เมื่อได้ Lead Grade A (ปิดได้โดยลบ GIFT_FB_PSID)"""
    if not GIFT_FB_PSID:
        return
    alert = (
        "GRADE A LEAD ใหม่ (Facebook Page)\n"
        f"Sender: {sender_id}\n"
        f"ข้อความ: {user_text[:100]}\n"
        f"Ad ID: {ad_id or '-'}\n\n"
        "ติดต่อกลับใน Messenger ด่วนครับ"
    )
    send_message(GIFT_FB_PSID, alert)


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
def process_event(event: dict, platform: str = "facebook"):
    """ประมวลผล messaging event 1 รายการ (platform: facebook / instagram)"""
    sender_id = event.get("sender", {}).get("id", "")
    if not sender_id:
        return

    # ข้าม echo (ข้อความที่เพจ/IG ส่งเอง)
    if event.get("message", {}).get("is_echo"):
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
        )
        send_message(sender_id, reply_text)
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
    )
    send_message(sender_id, reply_text)

    if lead_grade == "A":
        alert_gift(sender_id, user_text, lead_referral.get("ad_id", ""))

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
    return jsonify({"status": "ok"})


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
        return jsonify({"status": "ignored"})
    platform = "instagram" if obj == "instagram" else "facebook"

    for entry in data.get("entry", []):
        # กรณีปกติ: แอพเป็น Primary Receiver
        for event in entry.get("messaging", []):
            try:
                process_event(event, platform)
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
