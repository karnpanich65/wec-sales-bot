# pg_store.py — ความจำของบอทบน PostgreSQL  (Phase 1 = เขียนคู่ขนานอย่างเดียว)
# ----------------------------------------------------------------------------
# Gift 19 ส.ค. 2026
# ปัญหาที่ตัวนี้มาแก้: _conversations กับ _lead_states อยู่ใน RAM อย่างเดียว
# Railway restart/deploy ทีเดียว ประวัติแชททุกคนหายหมด
# -> Anthropic API ได้ history ว่างเปล่า -> ตอบเหมือนเพิ่งเจอกันครั้งแรก
#
# กติกาเหล็กของไฟล์นี้ (บทเรียนจาก r26 ที่ทำบอทเงียบทั้งระบบ):
#   1. ห้ามทำให้ลูกค้ารอ  -> เขียนผ่านคิวเบื้องหลัง ไม่บล็อกเทิร์นการตอบ
#   2. ห้าม raise ออกไปข้างนอก -> พังยังไงก็กลืนไว้ในนี้
#   3. DB ล่ม = บอททำงานเหมือนไม่มี DB ไม่ใช่บอทหยุดตอบ
#
# เฟส 1 (ตอนนี้): เขียนอย่างเดียว ยังอ่านจาก RAM + ชีตเหมือนเดิม 100%
# เฟส 2 (ทีหลัง): เปิด PG_READ=1 แล้วค่อยให้บอทอ่านจากที่นี่
# ----------------------------------------------------------------------------

import os
import json
import queue
import threading
import time

DATABASE_URL   = os.environ.get("DATABASE_URL", "")
PG_DUAL_WRITE  = os.environ.get("PG_DUAL_WRITE", "0") == "1"
PG_READ        = os.environ.get("PG_READ", "0") == "1"      # เฟส 2 ค่อยเปิด
PG_QUEUE_MAX   = int(os.environ.get("PG_QUEUE_MAX", "2000"))
PG_COOLDOWN    = float(os.environ.get("PG_COOLDOWN", "60")) # พังแล้วพักกี่วินาที

try:
    import psycopg2
    import psycopg2.extras
    _HAS_DRIVER = True
except Exception as _e:                                     # ไม่มี driver = ปิดเงียบ
    psycopg2 = None
    _HAS_DRIVER = False
    print(f"[PG] psycopg2 ไม่พร้อม ({_e}) — ข้ามการเขียน Postgres")

ENABLED = bool(DATABASE_URL) and _HAS_DRIVER and PG_DUAL_WRITE

_q: "queue.Queue" = queue.Queue(maxsize=PG_QUEUE_MAX)
_conn = None
_conn_lock = threading.Lock()
_schema_ok = False
_cool_until = 0.0
_worker = None

STATS = {"queued": 0, "written": 0, "dropped": 0, "errors": 0, "last_error": ""}


DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    page_id    TEXT        NOT NULL,
    psid       TEXT        NOT NULL,
    state      JSONB       NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (page_id, psid)
);
CREATE TABLE IF NOT EXISTS messages (
    id         BIGSERIAL   PRIMARY KEY,
    page_id    TEXT        NOT NULL,
    psid       TEXT        NOT NULL,
    role       TEXT        NOT NULL,
    text       TEXT        NOT NULL,
    stage      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_lookup
    ON messages (page_id, psid, created_at DESC);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS source_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS messages_source_uniq ON messages (source_key);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS psid_hash TEXT;
CREATE INDEX IF NOT EXISTS messages_hash_lookup
    ON messages (psid_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS messages_psid_lookup
    ON messages (psid, created_at DESC);
"""


# ---------------------------------------------------------------- connection --
def _connect():
    """เปิด connection ใหม่ + สร้างตารางถ้ายังไม่มี — เรียกจาก worker thread เท่านั้น"""
    global _conn, _schema_ok
    c = psycopg2.connect(DATABASE_URL, connect_timeout=8)
    c.autocommit = True
    if not _schema_ok:
        with c.cursor() as cur:
            cur.execute(DDL)
        _schema_ok = True
        print("[PG] ตารางพร้อมแล้ว (sessions, messages)")
    _conn = c
    return c


def _get_conn():
    global _conn
    if _conn is not None:
        try:
            if _conn.closed == 0:
                return _conn
        except Exception:
            pass
    return _connect()


def _drop_conn():
    global _conn
    try:
        if _conn is not None:
            _conn.close()
    except Exception:
        pass
    _conn = None


# -------------------------------------------------------------------- worker --
def _run_job(job):
    kind = job[0]
    conn = _get_conn()
    if kind == "state":
        _, page_id, psid, state_json = job
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (page_id, psid, state, updated_at) "
                "VALUES (%s, %s, %s, now()) "
                "ON CONFLICT (page_id, psid) DO UPDATE "
                "SET state = EXCLUDED.state, updated_at = now()",
                (page_id, psid, state_json))
    elif kind == "msgs":
        _, page_id, psid, rows, phash = job
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO messages "
                "(page_id, psid, role, text, stage, psid_hash) VALUES %s",
                [(page_id, psid, r[0], r[1], r[2], phash or None) for r in rows])


def _worker_loop():
    global _cool_until
    while True:
        job = _q.get()
        try:
            if time.time() < _cool_until:
                STATS["dropped"] += 1          # อยู่ในช่วงพักหลังพัง — ทิ้งไปเลย
                continue
            _run_job(job)
            STATS["written"] += 1
        except Exception as e:
            STATS["errors"] += 1
            STATS["last_error"] = f"{type(e).__name__}: {e}"[:200]
            _drop_conn()
            _cool_until = time.time() + PG_COOLDOWN
            print(f"[PG ERROR] {STATS['last_error']} — พัก {PG_COOLDOWN:.0f} วิ")
        finally:
            _q.task_done()


def _ensure_worker():
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    _worker = threading.Thread(target=_worker_loop, name="pg-writer", daemon=True)
    _worker.start()
    print("[PG] เปิดคิวเขียนเบื้องหลังแล้ว")


def _submit(job):
    if not ENABLED:
        return
    _ensure_worker()
    try:
        _q.put_nowait(job)
        STATS["queued"] += 1
    except queue.Full:
        STATS["dropped"] += 1                  # คิวเต็ม = ทิ้ง ห้ามบล็อกลูกค้าเด็ดขาด


# ------------------------------------------------------------------ public --
def _clean_state(state: dict) -> dict:
    """ตัดของที่ serialize ไม่ได้ออก แล้วคืน dict ที่ปลอดภัยจะเก็บลง JSONB"""
    out = {}
    for k, v in (state or {}).items():
        try:
            json.dumps(v)
            out[k] = v
        except Exception:
            out[k] = str(v)[:500]
    return out


def save_turn(page_id: str, psid: str, state: dict,
              user_msg: str, reply: str, stage: str = "",
              psid_hash: str = "") -> None:
    """เรียกท้ายทุกเทิร์น — เขียน state + 2 ข้อความลงคิว (ไม่รอผล)"""
    if not ENABLED or not psid:
        return
    try:
        pid = str(page_id or "-")
        _submit(("state", pid, str(psid),
                 json.dumps(_clean_state(state), ensure_ascii=False)))
        rows = []
        if user_msg:
            rows.append(("user", str(user_msg)[:4000], stage or None))
        if reply:
            rows.append(("assistant", str(reply)[:4000], stage or None))
        if rows:
            _submit(("msgs", pid, str(psid), rows, str(psid_hash or "")))
    except Exception as e:
        STATS["errors"] += 1
        STATS["last_error"] = f"save_turn {type(e).__name__}: {e}"[:200]


def save_human(page_id: str, psid: str, text: str, who: str = "sale",
               psid_hash: str = "") -> None:
    """ข้อความที่เซลพิมพ์เองจากกล่องข้อความเพจ — บริบทที่ AI มองไม่เห็นมาตลอด"""
    if not ENABLED or not psid or not text:
        return
    try:
        _submit(("msgs", str(page_id or "-"), str(psid),
                 [("assistant", f"[{who}] {str(text)[:4000]}", "human")],
                 str(psid_hash or "")))
    except Exception:
        pass


# ------------------------------------------------------------------ อ่าน --
# เฟส 2 — บอทอ่าน state + ประวัติแชทจาก Postgres แทนที่จะรอ Apps Script
# อ่านต้องทำบนเธรดที่ลูกค้ารออยู่ (จะ async ไม่ได้) เลยคุมด้วย 2 อย่าง:
#   · connect_timeout 5 วิ + statement_timeout 4 วิ  -> แย่สุดคือ 9 วิ ยังดีกว่าชีต 20 วิ
#   · พังแล้วพัก READ_COOLDOWN -> ไม่ไปจิ้ม DB ที่ล่มอยู่ทุก request
# อ่านไม่ได้ = คืน None/[] แล้วปล่อยให้ทางเดิม (ชีต) ทำงานต่อ ห้ามทำบอทเงียบ
READ_ENABLED  = bool(DATABASE_URL) and _HAS_DRIVER and PG_READ
READ_COOLDOWN = float(os.environ.get("PG_READ_COOLDOWN", "30"))

_read_conn = None
_read_lock = threading.Lock()
_read_cool_until = 0.0

STATS["reads"] = 0
STATS["read_hits"] = 0
STATS["read_errors"] = 0


def _read_get():
    global _read_conn, _schema_ok
    if _read_conn is not None:
        try:
            if _read_conn.closed == 0:
                return _read_conn
        except Exception:
            pass
    c = psycopg2.connect(DATABASE_URL, connect_timeout=5,
                         options="-c statement_timeout=8000")
    c.autocommit = True
    # r38 — เดิม DDL รันจากฝั่งเขียนอย่างเดียว ถ้าหลัง deploy มีคนทักเข้ามา
    # ก่อนบอทได้เขียนอะไรสักครั้ง ฝั่งอ่านจะเจอ UndefinedColumn (psid_hash)
    # แล้วตัดตัวเองพัก 30 วิ — ลูกค้าคนนั้นเลยโดนถามซ้ำเหมือนเริ่มใหม่
    if not _schema_ok:
        try:
            with c.cursor() as cur:
                cur.execute(DDL)
            _schema_ok = True
            print("[PG] ตารางพร้อมแล้ว (ตรวจจากฝั่งอ่าน)")
        except Exception as _de:
            print(f"[PG] DDL ฝั่งอ่านไม่ผ่าน: {_de}")
    _read_conn = c
    return c


def _read_fail(e):
    global _read_conn, _read_cool_until
    STATS["read_errors"] += 1
    STATS["last_error"] = f"read {type(e).__name__}: {e}"[:200]
    try:
        if _read_conn is not None:
            _read_conn.close()
    except Exception:
        pass
    _read_conn = None
    _read_cool_until = time.time() + READ_COOLDOWN
    print(f"[PG READ ERROR] {STATS['last_error']} — พัก {READ_COOLDOWN:.0f} วิ")


def _can_read() -> bool:
    return READ_ENABLED and time.time() >= _read_cool_until


def load_state(page_id: str, psid: str):
    """คืน state ก้อนล่าสุดของลูกค้าคนนี้ หรือ None ถ้าไม่มี/อ่านไม่ได้"""
    if not _can_read() or not psid:
        return None
    try:
        STATS["reads"] += 1
        with _read_lock:
            conn = _read_get()
            with conn.cursor() as cur:
                cur.execute("SELECT state FROM sessions WHERE page_id = %s AND psid = %s",
                            (str(page_id or "-"), str(psid)))
                row = cur.fetchone()
        if row and isinstance(row[0], dict):
            STATS["read_hits"] += 1
            return row[0]
        return None
    except Exception as e:
        _read_fail(e)
        return None


def load_history(page_id: str, psid: str, limit: int = 10,
                 psid_hash: str = "") -> list:
    """คืนประวัติแชทรูปแบบที่ Anthropic API รับได้

    รวมท่อน role เดียวกันที่ติดกันเป็นก้อนเดียว และตัดหัวให้เริ่มด้วย user
    (ไม่งั้น API ตอบ 400) — ตรรกะเดียวกับ _resume_context ในบอท
    """
    if not _can_read() or not psid:
        return []
    try:
        with _read_lock:
            conn = _read_get()
            with conn.cursor() as cur:
                # ไม่กรองด้วย page_id — PSID ของ Facebook ผูกกับเพจอยู่แล้ว
                # และแถวเก่าจากชีตบางแถวไม่มีเพจติดมา ถ้ากรองจะหาไม่เจอ
                # จับคู่ทั้ง PSID จริง (แถวสด) และรหัสย่อ (แถวที่นำเข้าจากชีต)
                cur.execute(
                    "SELECT role, text FROM messages "
                    "WHERE psid = %s OR psid_hash = %s "
                    "ORDER BY created_at DESC, id DESC LIMIT %s",
                    (str(psid), str(psid_hash or psid), int(limit) * 3))
                rows = cur.fetchall()
    except Exception as e:
        _read_fail(e)
        return []
    rows = list(reversed(rows))
    msgs = []
    for role, text in rows:
        t = (text or "").strip()
        if not t:
            continue
        r = "user" if role == "user" else "assistant"
        if msgs and msgs[-1]["role"] == r:
            msgs[-1]["content"] += "\n" + t
        else:
            msgs.append({"role": r, "content": t})
    msgs = msgs[-limit:]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def list_handover(days: int = 3, limit: int = 300) -> list:
    """r50 — แชทที่เซลรับช่วงคุยเอง (ใช้ตอนดึงเคสย้อนหลังกลับมาแจก)"""
    if not _can_read():
        return []
    try:
        with _read_lock:
            conn = _read_get()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT page_id, psid, state FROM sessions "
                    "WHERE updated_at > now() - make_interval(days => %s) "
                    "AND state->>'handover' = 'true' "
                    "ORDER BY updated_at DESC LIMIT %s",
                    (int(days), int(limit)))
                rows = cur.fetchall()
        return [{"page_id": r[0], "psid": r[1], "state": r[2]}
                for r in rows if isinstance(r[2], dict)]
    except Exception as e:
        _read_fail(e)
        return []


def load_raw(psid: str, limit: int = 300) -> list:
    """r50 — ข้อความดิบเรียงเก่า->ใหม่ พร้อมบอกว่าอันไหนเซลพิมพ์ (stage='human')"""
    if not _can_read() or not psid:
        return []
    try:
        with _read_lock:
            conn = _read_get()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT role, text, stage FROM messages WHERE psid = %s "
                    "ORDER BY created_at ASC, id ASC LIMIT %s",
                    (str(psid), int(limit)))
                rows = cur.fetchall()
        return [{"role": r[0], "text": r[1] or "", "stage": r[2] or ""}
                for r in rows]
    except Exception as e:
        _read_fail(e)
        return []


# ------------------------------------------------------- นำเข้าของเก่า --
# ย้าย Chat_Log จากชีตเข้า messages — ยิงซ้ำกี่รอบก็ได้ข้อมูลชุดเดียว
# เพราะ source_key (ชื่อแท็บ + เลขแถว) มี unique index กันไว้
# ใช้เวลาจริงจากชีต ไม่ใช่ now() ไม่งั้นประวัติเก่าจะไปกองอยู่บนสุดหมด
def import_rows(rows: list) -> dict:
    if not (bool(DATABASE_URL) and _HAS_DRIVER):
        return {"ok": False, "inserted": 0, "reason": "no driver/url"}
    if not rows:
        return {"ok": True, "inserted": 0, "seen": 0}
    vals = []
    for r in rows:
        try:
            key = str(r.get("key") or "").strip()
            psid = str(r.get("psid") or "").strip()
            text = str(r.get("text") or "").strip()
            ts = str(r.get("ts") or "").strip()
            if not (key and psid and text and ts):
                continue
            role = "user" if str(r.get("role") or "").lower() in (
                "user", "customer", "cust", "ลูกค้า") else "assistant"
            vals.append((str(r.get("page_id") or "-"), psid, role, text[:4000],
                         (str(r.get("stage") or "") or None), ts, key,
                         str(r.get("psid_hash") or psid)))
        except Exception:
            continue
    if not vals:
        return {"ok": True, "inserted": 0, "seen": len(rows)}
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=8,
                                options="-c statement_timeout=25000")
        conn.autocommit = True
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO messages "
                "(page_id, psid, role, text, stage, created_at, "
                " source_key, psid_hash) "
                "VALUES %s ON CONFLICT (source_key) DO NOTHING",
                vals,
                template="(%s,%s,%s,%s,%s,%s::timestamptz,%s,%s)")
            n = cur.rowcount
        return {"ok": True, "inserted": int(n), "seen": len(rows), "valid": len(vals)}
    except Exception as e:
        return {"ok": False, "inserted": 0, "seen": len(rows),
                "error": f"{type(e).__name__}: {e}"[:250]}
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def fix_import_ts(hours: int = 14) -> dict:
    """เลื่อนเวลาแถวที่ import มาจากชีตให้ตรงความจริง — รันซ้ำได้ ไม่เลื่อนซ้ำ

    ที่มา: ตอน import ไฟล์ WEC CRM ตั้ง timezone เป็น America/Los_Angeles
    ค่าเวลาที่อ่านกลับมาจึงล้ำหน้าไป 14 ชม. (ข้อมูลทั้งหมดอยู่ในช่วง PDT)
    กันรันซ้ำด้วยคอลัมน์ ts_fixed — แถวที่แก้แล้วจะไม่ถูกแตะอีก
    """
    if not (bool(DATABASE_URL) and _HAS_DRIVER):
        return {"ok": False, "reason": "no driver/url"}
    try:
        hours = int(hours)
    except Exception:
        return {"ok": False, "reason": "hours ต้องเป็นตัวเลข"}
    if not (1 <= hours <= 24):
        return {"ok": False, "reason": "hours ต้องอยู่ระหว่าง 1-24"}
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=8,
                                options="-c statement_timeout=60000")
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS "
                        "ts_fixed SMALLINT")
            cur.execute("SELECT count(*) FROM messages "
                        "WHERE source_key IS NOT NULL AND ts_fixed IS NULL")
            todo = int(cur.fetchone()[0])
            cur.execute(
                "UPDATE messages "
                "SET created_at = created_at - make_interval(hours => %s), "
                "    ts_fixed = 1 "
                "WHERE source_key IS NOT NULL AND ts_fixed IS NULL", (hours,))
            moved = int(cur.rowcount)
            cur.execute("SELECT max(created_at)::text FROM messages "
                        "WHERE source_key IS NOT NULL")
            newest_imported = cur.fetchone()[0]
            cur.execute("SELECT max(created_at)::text FROM messages "
                        "WHERE source_key IS NULL")
            newest_live = cur.fetchone()[0]
        return {"ok": True, "pending_before": todo, "moved": moved,
                "hours": hours, "newest_imported": newest_imported,
                "newest_live": newest_live}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:250]}
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def counts() -> dict:
    """นับแถวจริงใน DB — ใช้ยืนยันว่านำเข้าครบไหม"""
    if not (bool(DATABASE_URL) and _HAS_DRIVER):
        return {"ok": False}
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=8,
                                options="-c statement_timeout=15000")
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM sessions")
            se = cur.fetchone()[0]
            cur.execute("SELECT count(*), count(source_key), "
                        "min(created_at)::text, max(created_at)::text FROM messages")
            m, imported, mn, mx = cur.fetchone()
        return {"ok": True, "sessions": int(se), "messages": int(m),
                "imported": int(imported), "oldest": mn, "newest": mx}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:250]}
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def stats() -> dict:
    d = dict(STATS)
    d["enabled"] = ENABLED
    d["read_mode"] = PG_READ
    d["queue"] = _q.qsize()
    d["cooling"] = max(0, int(_cool_until - time.time()))
    d["read_enabled"] = READ_ENABLED
    d["read_cooling"] = max(0, int(_read_cool_until - time.time()))
    return d
