"""₿ Detections and alerts. Each newly CLOSED candle: the pivot levels in force, the 16 patterns (backend/crypto/
patterns.py), the filters, the state, one crypto_signals row per detection (never deleted), and for an alert the
Telegram line + the chart snapshot (kind "crypto").

States
  sent          an alert-state pattern inside the alert hours (sent_at = when Telegram took it; `why` says when it
                could not be delivered)
  quiet         the same outside the alert hours / while paused ("btc quiet 2h"): stored, marked muted on the chart,
                listed in the digest at alert_from
  mark          a mark-only pattern: on the chart and in the table, never on Telegram
  confirm_wait  a weak pattern waiting for the next candle to close in the direction (then sent / quiet or dropped)
  dropped       a filter failed (EMA, volume, prior trend, day), the cooldown, or no confirmation: `why`
Dedupe: one alert per (level, direction) per crypto.cooldown_candles. The [AI] line (backend/crypto/read.py) never
gates an alert: appended when ready within AI_WAIT_S, else sent as a second message. Every number in a line comes from
code. Times are 12-hour IST. ALERTS ONLY: nothing here places, sizes or books an order."""

import json
import threading
import time as _time
from datetime import datetime, time, timedelta
from html import escape
from pathlib import Path
from typing import Optional

from backend.config import IST, db_path
from backend.crypto import feed
from backend.crypto import patterns as pt
from backend.crypto import pivots as pv
from backend.database.models import get_db, get_setting, now_ist, set_setting
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm

logger = setup_logger("crypto")

KIND = "crypto"                       # the Telegram kind (never held by the AI quiet hours or the evening batch)
AI_WAIT_S = 3.0
QUIET_KEY = "crypto.quiet_until"
DIGEST_KEY = "crypto.digest_day"
WINDOW = 300                          # candles read for the EMA / volume average / snapshot
TF_LABEL = {"1m": "1 min", "5m": "5 min", "15m": "15 min", "1h": "1 h"}
NOTIFIER = None                       # the tests' Notifier (a fake Telegram); None = the configured one
_LOCK = threading.Lock()
LATE: dict = {}                       # signal id -> True: the alert went without its [AI] line (sent second when ready)


def snaps_dir() -> Path:
    """data/crypto/snaps next to the live DB (git-ignored with data/)."""
    return db_path().parent / "crypto" / "snaps"


def _now_ts() -> float:
    return _time.time()


def notifier(config):
    if NOTIFIER is not None:
        return NOTIFIER
    from backend.alerts.dispatch import Notifier
    return Notifier(config.alerts, ai=config.ai)


def px(v) -> str:
    """60010 -> '60,010'; 60010.5 -> '60,010.5'."""
    if v is None:
        return "—"
    return f"{float(v):,.2f}".rstrip("0").rstrip(".")


def ist(t: int) -> datetime:
    return datetime.fromtimestamp(int(t), IST)


# --- the store --------------------------------------------------------------------------------------------------------------

def _decode(r) -> dict:
    d = dict(r)
    for k in ("ai_json", "facts_json"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else None
        except (TypeError, ValueError):
            d[k] = None
    return d


def rows(where: str = "1", args: tuple = (), order: str = "t DESC, id DESC", limit: Optional[int] = None) -> list[dict]:
    with get_db() as conn:
        return [_decode(r) for r in conn.execute(f"SELECT * FROM crypto_signals WHERE {where} ORDER BY {order}" + (f" LIMIT {int(limit)}" if limit else ""), args)]


def load(sid: int) -> Optional[dict]:
    r = rows("id = ?", (sid,))
    return r[0] if r else None


def update(sid: int, **cols) -> None:
    for k in ("ai_json", "facts_json"):
        if k in cols and not isinstance(cols[k], (str, type(None))):
            cols[k] = json.dumps(cols[k])
    with get_db() as conn:
        conn.execute(f"UPDATE crypto_signals SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?", (*cols.values(), sid))


def insert(config, det: dict, state: str, why: Optional[str], lv: Optional[dict]) -> int:
    c = config.crypto
    facts = {"candles": det["candles"], "ema": det["ema"], "ema_len": det["ema_len"], "vol_x": det["vol_x"], "capped": det["capped"],
             "t0": det["t0"], "close": det["candles"][-1]["c"], "shape": det["facts"], "trend_ok": det["trend_ok"], "day_ok": det["day_ok"],
             "levels": (lv or {}).get("levels"), "period": (lv or {}).get("period"), "pivot_from": (lv or {}).get("from"),
             "pivot_type": c.pivot_type}
    with get_db() as conn:
        return conn.execute("INSERT INTO crypto_signals (symbol, tf, t, pattern, dir, level_name, level_px, entry, stop, r1, ema_ok, vol_ok, state, "
                            "why, facts_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (c.symbol, c.tf, det["t"], det["pattern"], det["dir"], det["level_name"], det["level_px"], det["entry"], det["stop"],
                             det["r1"], None if det["ema_ok"] is None else int(det["ema_ok"]), None if det["vol_ok"] is None else int(det["vol_ok"]),
                             state, why, json.dumps(facts), now_ist())).lastrowid


# --- the levels ------------------------------------------------------------------------------------------------------------

def levels_for(config, t: int) -> Optional[dict]:
    """The pivot levels in force for the candle opening at `t` ({period, from, start, levels}) or None."""
    c = config.crypto
    period = pv.auto_period(c.tf, c.pivot_tf)
    if c.day_boundary == "utc":
        span = {"daily": 5, "weekly": 21, "monthly": 70}[period]
        bars = feed.load(c.symbol, "1d", int(t) - span * 86400, int(t))
        if not bars:
            bars = feed.load(c.symbol, c.tf, int(t) - span * 86400, int(t))
    else:
        span = {"daily": 3, "weekly": 15, "monthly": 63}[period]
        bars = feed.load(c.symbol, c.tf, int(t) - span * 86400, int(t))
    if not bars:
        return None
    return pv.levels_at(bars, t, period, c.day_boundary, c.pivot_type)


# --- the alert hours -------------------------------------------------------------------------------------------------------

def _hm(s: str) -> time:
    h, m = str(s).split(":")[:2]
    return time(int(h), int(m))


def quiet_until(now: Optional[datetime] = None) -> Optional[datetime]:
    with get_db() as conn:
        raw = get_setting(conn, QUIET_KEY)
    if not raw:
        return None
    try:
        q = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return q if q > (now or datetime.now(IST)) else None


def set_quiet(hours: Optional[float], now: Optional[datetime] = None) -> Optional[datetime]:
    """"btc quiet 2h" (hours) / "btc quiet off" (None): pause the alerts; detections are stored "quiet"."""
    now = now or datetime.now(IST)
    q = now + timedelta(hours=float(hours)) if hours else None
    with get_db() as conn:
        set_setting(conn, QUIET_KEY, q.isoformat(timespec="seconds") if q else "")
    return q


def in_hours(config, now: datetime) -> bool:
    """Inside alert_from–alert_until (IST; may run over midnight) on an alert day, and not paused."""
    c = config.crypto
    if pt.DAYS[now.weekday()] not in [str(d).lower()[:3] for d in c.alert_days]:
        return False
    if quiet_until(now):
        return False
    f, u, x = _hm(c.alert_from), _hm(c.alert_until), now.time()
    if f == u:
        return True
    return f <= x < u if f < u else (x >= f or x < u)


def hours_text(config, now: Optional[datetime] = None) -> str:
    """'alerts 8:00 AM – 11:00 PM' (+ ' · quiet until 4:10 PM')."""
    c = config.crypto
    q = quiet_until(now)
    return f"alerts {ampm(c.alert_from)} – {ampm(c.alert_until)}" + (f" · quiet until {ampm(q)}" if q else "")


# --- the words -----------------------------------------------------------------------------------------------------------------

def line(r: dict, confirmed_t: Optional[int] = None) -> str:
    """'₿ hammer on S1 60,010 · 5 min · 2:10 AM · long bias · stop under 59,980 · 1R 60,320 · EMA 20 above ✓ ·
    volume 1.3×' (every figure from the row)."""
    f = r.get("facts_json") or {}
    long_ = r["dir"] == "long"
    bits = [f"₿ {pt.LABEL[r['pattern']]} on {r['level_name']} {px(r['level_px'])}", TF_LABEL.get(r["tf"], r["tf"]), ampm(ist(r["t"])),
            f"{'long' if long_ else 'short'} bias", f"stop {'under' if long_ else 'over'} {px(r['stop'])}", f"1R {px(r['r1'])}"]
    if f.get("ema") is not None:
        side = "above" if f["close"] > f["ema"] else "below"
        bits.append(f"EMA {f.get('ema_len', 20)} {side} {'✓' if r.get('ema_ok') else '✗'}")
    if f.get("vol_x") is not None:
        bits.append(f"volume {f['vol_x']:g}×")
    if confirmed_t:
        bits.append(f"confirmed {ampm(ist(confirmed_t))}")
    return " · ".join(bits)


def caption(r: dict, config) -> str:
    """The snapshot's footer: 'BTCUSD · Delta · 5 min · IST · Tue 24 Nov 2:10 AM · daily pivots (Traditional)'."""
    f = r.get("facts_json") or {}
    d = ist(r["t"])
    return (f"{r['symbol']} · Delta · {TF_LABEL.get(r['tf'], r['tf'])} · IST · {d:%a} {d.day} {d:%b} {ampm(d)} · "
            f"{f.get('period') or 'daily'} pivots ({str(f.get('pivot_type') or config.crypto.pivot_type).title()})")


# --- processing ------------------------------------------------------------------------------------------------------------

def _cooldown_hit(config, det: dict) -> Optional[dict]:
    c = config.crypto
    n = int(c.cooldown_candles)
    if n <= 0:
        return None
    since = det["t"] - n * pv.TF_SECONDS[c.tf]
    r = rows("symbol = ? AND tf = ? AND level_name = ? AND dir = ? AND state IN ('sent', 'quiet', 'confirm_wait') AND t > ? AND t <= ?",
             (c.symbol, c.tf, det["level_name"], det["dir"], since, det["t"]), limit=1)
    return r[0] if r else None


def _needs_confirm(config, pattern: str) -> bool:
    return bool(getattr(config.crypto, f"confirm_{pattern}", False))


def process(config, t: int, now_ts: Optional[float] = None, candles: Optional[list[dict]] = None) -> list[int]:
    """The closed candle opening at `t`: resolve the waiting confirmations, then detect. Returns the new row ids.
    Idempotent: a (t, pattern) already stored is skipped."""
    c = config.crypto
    now_ts = _now_ts() if now_ts is None else now_ts
    cs = candles if candles is not None else feed.load(c.symbol, c.tf, end=t, limit=WINDOW)
    cs = [b for b in cs if b["t"] <= t]
    if not cs or cs[-1]["t"] != t:
        return []
    _resolve_waits(config, cs, now_ts)
    lv = levels_for(config, t)
    if not lv:
        return []
    dets = pt.detect(cs, pv.pattern_levels(lv["levels"], c.use_p), c)
    dets.sort(key=lambda d: 0 if d["state_set"] == "alert" else 1)
    ids = []
    for det in dets:
        with get_db() as conn:
            if conn.execute("SELECT 1 FROM crypto_signals WHERE symbol = ? AND tf = ? AND t = ? AND pattern = ?",
                            (c.symbol, c.tf, t, det["pattern"])).fetchone():
                continue
        if det["why"]:
            ids.append(insert(config, det, "dropped", det["why"], lv))
            continue
        if det["state_set"] == "mark":
            ids.append(insert(config, det, "mark", None, lv))
            continue
        hit = _cooldown_hit(config, det)
        if hit:
            ids.append(insert(config, det, "dropped", f"cooldown · {pt.LABEL[hit['pattern']]} on {hit['level_name']} at {ampm(ist(hit['t']))}", lv))
            continue
        if _needs_confirm(config, det["pattern"]):
            ids.append(insert(config, det, "confirm_wait", None, lv))
            continue
        sid = insert(config, det, "sent", None, lv)
        ids.append(sid)
        fire(config, sid, now_ts, cs)
    if _forming_get():
        forming_close(config, t)
    return ids


def scan_history(config, days: Optional[int] = None, now_ts: Optional[float] = None) -> dict:
    """Mark the patterns in the stored candles of the last `days` (crypto.scan_days): every detection is stored as "mark"
    with why "history scan" so the chart and the table show what the rules would have found. Nothing is sent, no
    confirmation waits, no AI read, no cooldown (the markers are the point). Idempotent per (t, pattern)."""
    c = config.crypto
    now_ts = _now_ts() if now_ts is None else now_ts
    days = int(c.scan_days if days is None else days)
    if days <= 0:
        return {"scanned": 0, "marked": 0}
    s = pv.TF_SECONDS.get(c.tf, 300)
    start = int(now_ts) - days * 86400
    cs = feed.load(c.symbol, c.tf, start - WINDOW * s, int(now_ts))
    done = [b for b in cs if b["t"] + s <= now_ts]
    marked, scanned = 0, 0
    lv_cache: dict = {}
    for i, b in enumerate(done):
        if b["t"] < start:
            continue
        scanned += 1
        win = done[max(0, i - WINDOW + 1):i + 1]
        day = b["t"] // 86400
        if day not in lv_cache:
            lv_cache[day] = levels_for(config, b["t"])
        lv = lv_cache[day]
        if not lv:
            continue
        for det in pt.detect(win, pv.pattern_levels(lv["levels"], c.use_p), c):
            with get_db() as conn:
                if conn.execute("SELECT 1 FROM crypto_signals WHERE symbol = ? AND tf = ? AND t = ? AND pattern = ?",
                                (c.symbol, c.tf, b["t"], det["pattern"])).fetchone():
                    continue
            if det["why"]:                                   # formed but filtered: a grey marker on the chart, like a live drop
                insert(config, det, "dropped", f"history scan · {det['why']}", lv)
                continue
            insert(config, det, "mark", "history scan", lv)
            marked += 1
    return {"scanned": scanned, "marked": marked, "days": days}


def _resolve_waits(config, cs: list[dict], now_ts: float) -> None:
    c = config.crypto
    by_t = {b["t"]: i for i, b in enumerate(cs)}
    for r in rows("symbol = ? AND tf = ? AND state = 'confirm_wait'", (c.symbol, c.tf), order="t"):
        i = by_t.get(r["t"])
        if i is None or i + 1 >= len(cs):
            continue
        nxt = cs[i + 1]
        det = {"pattern": r["pattern"], "dir": r["dir"], "candles": (r["facts_json"] or {}).get("candles") or [cs[i]]}
        if pt.confirm(det, nxt):
            update(r["id"], state="sent")
            fire(config, r["id"], now_ts, cs[: i + 2], confirmed_t=nxt["t"])
        else:
            update(r["id"], state="dropped", why=f"not confirmed · the {ampm(ist(nxt['t']))} candle closed {'against' if (nxt['c'] < nxt['o']) == (r['dir'] == 'long') else 'flat'}")


def fire(config, sid: int, now_ts: float, cs: list[dict], confirmed_t: Optional[int] = None) -> dict:
    """An alert: quiet outside the hours (state quiet, digest later); inside: the snapshot, the [AI] line if ready
    within AI_WAIT_S, the Telegram line + PNG. {state, sent, ai}."""
    from backend.crypto import read
    c = config.crypto
    r = load(sid)
    now = datetime.fromtimestamp(now_ts, IST)
    if not in_hours(config, now):
        update(sid, state="quiet")
        r = load(sid)
        _snapshot(config, r, cs)
        read.start(config, sid, cs)
        return {"state": "quiet", "sent": False}
    t0 = _time.monotonic()
    job = read.start(config, sid, cs)
    png = _snapshot(config, r, cs)
    text = escape(line(r, confirmed_t))
    ai_line = None
    if job is not None:
        ai_line = read.wait_line(sid, max(0.0, AI_WAIT_S - (_time.monotonic() - t0)))
    with _LOCK:
        r = load(sid)
        ai_line = ai_line or ((r.get("ai_json") or {}).get("line"))
        if ai_line:
            text += "\n[AI] " + escape(ai_line)
            update(sid, ai_json={**(r.get("ai_json") or {}), "inline": True})
        elif job is not None:
            LATE[sid] = True
    out = {"state": "sent", "sent": False, "ai": bool(ai_line)}
    if not c.telegram:
        update(sid, why="Telegram off (crypto.telegram)")
        return out
    n = notifier(config)
    if not getattr(n, "ready", False):
        update(sid, why="not delivered · Telegram not set up")
        return out
    data = png.read_bytes() if (png and c.snapshot) else None
    ok = n.send(f"crypto|{sid}", KIND, text, png=data)
    if ok:
        update(sid, sent_at=now_ist(), why=None)
    else:
        update(sid, why="not delivered · Telegram refused or already sent")
    out["sent"] = ok
    return out


def send_ai_late(config, sid: int, text: str) -> bool:
    """The [AI] line that missed the AI_WAIT_S window: a second message under the alert."""
    with _LOCK:
        if not LATE.pop(sid, None):
            return False
    end = _time.monotonic() + 10                # after the alert itself
    r = load(sid)
    while r and not r.get("sent_at") and not r.get("why") and _time.monotonic() < end:
        _time.sleep(0.1)
        r = load(sid)
    if not r or r["state"] != "sent" or not r.get("sent_at") or not config.crypto.telegram:
        return False
    n = notifier(config)
    if not getattr(n, "ready", False):
        return False
    return n.send(f"crypto|ai|{sid}", KIND, f"₿ {escape(pt.LABEL[r['pattern']])} on {r['level_name']} · {ampm(ist(r['t']))}\n[AI] {escape(text)}")


def _snapshot(config, r: dict, cs: list[dict]) -> Optional[Path]:
    if not config.crypto.snapshot:
        return None
    from backend.crypto import snapshot
    try:
        p = snapshot.save(config, r, cs)
    except Exception:
        logger.exception("₿ snapshot failed")
        return None
    if p:
        update(r["id"], png_path=str(p))
    return p


# --- the digest, the forming heads-up, the hand quiet -----------------------------------------------------------------------

def digest_if_due(config, now_ts: Optional[float] = None) -> int:
    """At alert_from (and after a hand quiet ends): one message listing the detections stored "quiet" and not listed
    yet. Returns how many were listed."""
    c = config.crypto
    now_ts = _now_ts() if now_ts is None else now_ts
    now = datetime.fromtimestamp(now_ts, IST)
    if not c.digest or not in_hours(config, now):
        return 0
    rs = rows("symbol = ? AND state = 'quiet' AND digest_at IS NULL", (c.symbol,), order="t")
    if not rs:
        return 0
    head = f"₿ quiet hours · {len(rs)} detection{'s' if len(rs) != 1 else ''}"
    body = "\n".join("• " + escape(line(r)) for r in rs[:30])
    ok = True
    if c.telegram:
        n = notifier(config)
        ok = bool(getattr(n, "ready", False)) and n.send(f"crypto|digest|{now.isoformat(timespec='minutes')}", KIND, head + "\n" + body)
    if ok or not c.telegram:
        for r in rs:
            update(r["id"], digest_at=now.isoformat(timespec="seconds"))
        with get_db() as conn:
            set_setting(conn, DIGEST_KEY, now.date().isoformat())
    return len(rs) if ok else 0


FORMING_KEY = "crypto.forming"


def forming(config, t: int, now_ts: Optional[float] = None, candles: Optional[list[dict]] = None) -> list[str]:
    """The heads-up (crypto.forming): the candle opening at `t`, ≥ 80 % through, read as if closed, for the
    alert-state hammer / shooting star / engulfings at a level with the filters passing; one provisional line
    "forming · not confirmed" each (inside the hours). The close then confirms or cancels (forming_close)."""
    c = config.crypto
    now_ts = _now_ts() if now_ts is None else now_ts
    if not c.forming or not in_hours(config, datetime.fromtimestamp(now_ts, IST)):
        return []
    cs = candles if candles is not None else feed.load(c.symbol, c.tf, end=t, limit=WINDOW)
    cs = [b for b in cs if b["t"] <= t]
    if not cs or cs[-1]["t"] != t:
        return []
    lv = levels_for(config, t)
    if not lv:
        return []
    names = tuple(p for p in pt.FORMING if getattr(c, p) == "alert")
    sent = []
    held = _forming_get()
    for det in pt.detect(cs, pv.pattern_levels(lv["levels"], c.use_p), c, names=names):
        if det["why"]:
            continue
        key = f"{c.symbol}|{c.tf}|{t}|{det['pattern']}"
        if key in held:
            continue
        txt = f"₿ forming · {pt.LABEL[det['pattern']]} on {det['level_name']} {px(det['level_px'])} · {TF_LABEL.get(c.tf, c.tf)} · {ampm(ist(t))} · not confirmed"
        held[key] = {"level": det["level_name"], "at": int(now_ts)}
        if c.telegram:
            n = notifier(config)
            if getattr(n, "ready", False):
                n.send(f"crypto|forming|{key}", KIND, escape(txt))
        sent.append(txt)
    _forming_set(held)
    return sent


def _forming_get() -> dict:
    with get_db() as conn:
        raw = get_setting(conn, FORMING_KEY)
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _forming_set(d: dict) -> None:
    with get_db() as conn:
        set_setting(conn, FORMING_KEY, json.dumps(dict(list(d.items())[-50:])))


def forming_close(config, t: int) -> list[str]:
    """After the close of the candle opening at `t`: each provisional line gets its follow-up, "confirmed" when the
    closed candle stored the same pattern (any state but dropped), else "cancelled"."""
    c = config.crypto
    held = _forming_get()
    out = []
    for key, v in list(held.items()):
        sym, tf, tt, pat = key.split("|")
        if sym != c.symbol or tf != c.tf or int(tt) != int(t) or v.get("done"):
            continue
        got = rows("symbol = ? AND tf = ? AND t = ? AND pattern = ? AND state != 'dropped'", (sym, tf, int(tt), pat), limit=1)
        word = "confirmed at the close" if got else "cancelled · the close did not hold the shape"
        txt = f"₿ {pt.LABEL[pat]} on {v['level']} · {ampm(ist(int(tt)))} · {word}"
        if c.telegram:
            n = notifier(config)
            if getattr(n, "ready", False):
                n.send(f"crypto|formed|{key}", KIND, escape(txt))
        v["done"] = True
        out.append(txt)
    _forming_set(held)
    return out
