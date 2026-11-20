"""💡 The Tips card's trade facts and tick store (read-only: nothing here places, modifies or cancels an order, and no
AI call is made).

  ticks      tips_ticks(hint_id, at, ltp, spot, bid, ask, dry): every quote the Tips poll (backend/tips/track.py) reads
             for a hint while it is armed / open on Kite (any mode but off; a dry run's rows carry dry = 1) and for 10
             minutes after the position closes. Never deleted: the archive job moves rows older than 180 days to
             data/archive.db (backend/database/archive.py).
  charges    charges_est(): Zerodha's F&O option rates (RATES below) on the entry and every exit order; an open position
             is priced as if sold at the last price. An estimate: the card says "≈ net".
  build      build(h, ticks, now): pure. The hint's `trade_facts`: entry / exit, who closed it, size, the best and worst
             move (₹, %, when), the time to T1 / the stop, slippage and spread at the entry, the index at the fill and
             at the exit, post → fill, the nearest the price came to the stop, the channel's follow-ups, the AI second
             opinions, the channel's own result, charges and net. Stored on tips_hints.facts by refresh() (its own
             UPDATE) and final once the position has closed.
  day        day_facts(): one row per trading day in tips_day_facts, written by the 3:35 PM Kite day check."""

import json
import re
from datetime import datetime, timedelta
from typing import Optional

from backend.config import IST
from backend.database.models import get_db
from backend.logging_config.logger import setup_logger

logger = setup_logger("tips")

AFTER_CLOSE_S = 600                    # ticks keep being stored 10 min after a Kite position closes
NEAR_STOP_PCT = 10.0                   # the stop came nearer than this: "never near" otherwise
WORKING = ("armed", "armed_watch", "armed_wait", "entered")

# Zerodha F&O option rates (per executed order; the estimate behind the card's "≈ net"). Brokerage is the lower of ₹20
# and 0.03 % of the order's turnover; STT 0.1 % on the sell side's premium; the exchange's transaction charge on the
# premium (NSE 0.03503 %, BSE 0.0325 %); SEBI ₹10 a crore (0.0001 %); stamp duty 0.003 % on the buy side; GST 18 % on
# brokerage + transaction charge + SEBI.
RATES = {"brokerage_max": 20.0, "brokerage_pct": 0.0003, "stt_sell": 0.001, "txn": {"NSE": 0.0003503, "BSE": 0.000325},
         "sebi": 0.000001, "stamp_buy": 0.00003, "gst": 0.18}


def _dt(iso) -> Optional[datetime]:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=IST)


def _num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def venue(exchange: Optional[str]) -> str:
    """The transaction-charge venue of an options exchange: BFO / BSE → BSE, everything else NSE."""
    return "BSE" if str(exchange or "").upper() in ("BFO", "BSE") else "NSE"


def charges_est(buys: list, sells: list, exchange: Optional[str] = "NFO") -> dict:
    """The charges of a set of executed option orders, each (price, qty). {brokerage, stt, txn, sebi, stamp, gst, total}."""
    buy_t = [float(p) * int(q) for p, q in buys if p is not None and q]
    sell_t = [float(p) * int(q) for p, q in sells if p is not None and q]
    brk = sum(min(RATES["brokerage_max"], RATES["brokerage_pct"] * t) for t in buy_t + sell_t)
    turnover = sum(buy_t) + sum(sell_t)
    stt = RATES["stt_sell"] * sum(sell_t)
    txn = RATES["txn"][venue(exchange)] * turnover
    sebi = RATES["sebi"] * turnover
    stamp = RATES["stamp_buy"] * sum(buy_t)
    gst = RATES["gst"] * (brk + txn + sebi)
    out = {"brokerage": brk, "stt": stt, "txn": txn, "sebi": sebi, "stamp": stamp, "gst": gst}
    out = {k: round(v, 2) for k, v in out.items()}
    out["total"] = round(brk + stt + txn + sebi + stamp + gst, 2)
    return out


# --- the tick store ------------------------------------------------------------------------------------------------------

def wants_ticks(h: dict, mode: str, now: datetime) -> bool:
    """A hint's quote is stored as a tick: armed / open on Kite (any mode but off) or closed less than 10 min ago."""
    if mode == "off":
        return False
    k = h.get("kite") if isinstance(h.get("kite"), dict) else {}
    st = k.get("state")
    if st in WORKING:
        return True
    if st == "closed":
        c = _dt(k.get("closed_at"))
        return c is not None and (now - c).total_seconds() <= AFTER_CLOSE_S
    return False


def depth(q: dict) -> tuple[Optional[float], Optional[float]]:
    """(bid, ask) from a quote's market depth, when it carries one."""
    d = (q or {}).get("depth") or {}
    try:
        bid = _num(((d.get("buy") or [{}])[0] or {}).get("price")) or None
        ask = _num(((d.get("sell") or [{}])[0] or {}).get("price")) or None
    except (AttributeError, IndexError, TypeError):
        return None, None
    return bid, ask


def write_tick(conn, hid: int, at: str, ltp: Optional[float], spot: Optional[float] = None, bid: Optional[float] = None,
               ask: Optional[float] = None, dry: bool = False) -> None:
    conn.execute("INSERT OR IGNORE INTO tips_ticks (hint_id, at, ltp, spot, bid, ask, dry) VALUES (?,?,?,?,?,?,?)",
                 (hid, at, ltp, spot, bid, ask, int(bool(dry))))


def ticks(hid: int, conn=None) -> list[dict]:
    """The hint's stored ticks, oldest first; the Tips quotes (tips_quotes) stand in when no tick was stored."""
    if conn is None:
        with get_db() as c:
            return ticks(hid, c)
    rs = [dict(r) for r in conn.execute("SELECT at, ltp, spot, bid, ask, dry FROM tips_ticks WHERE hint_id = ? ORDER BY at", (hid,))]
    if rs:
        return rs
    return [{"at": r[0], "ltp": r[1], "spot": r[2], "bid": None, "ask": None, "dry": 0}
            for r in conn.execute("SELECT t, ltp, spot FROM tips_quotes WHERE hint_id = ? ORDER BY t", (hid,))]


# --- the facts -------------------------------------------------------------------------------------------------------------

def _sign(h: dict) -> int:
    return -1 if h.get("side") == "SELL" else 1


def _at_or_before(ts: list[dict], when: Optional[datetime], key: str = "ltp", slack_s: int = 30) -> Optional[dict]:
    """The last tick at or before `when` that has `key`, else the first one within `slack_s` after it."""
    if when is None:
        return None
    best = None
    for t in ts:
        d = _dt(t.get("at"))
        if d is None or t.get(key) is None:
            continue
        if d <= when:
            best = t
        elif best is None and (d - when).total_seconds() <= slack_s:
            return t
        else:
            break
    return best


def _stop_of(h: dict, k: dict) -> Optional[float]:
    if k.get("stop_px") is not None:
        return float(k["stop_px"])
    st = (h.get("plan") or {}).get("stop") or h.get("stop") or {}
    return float(st["value"]) if st.get("kind") == "premium" and st.get("value") is not None else None


def channel_result(h: dict) -> Optional[dict]:
    """The channel's own call (the record run): {outcome T1 / T2 / stop / time …, exit_px, at, closed}, or None."""
    run = h.get("run") or {}
    hits = run.get("hits") or []
    if hits:
        x = hits[-1]
        return {"outcome": f"T{x.get('k')}", "exit_px": x.get("px"), "at": x.get("t"), "closed": run.get("state") == "closed"}
    if run.get("state") == "closed" and run.get("entry_px") is not None:
        return {"outcome": str(run.get("end") or "closed"), "exit_px": run.get("exit_px"), "at": run.get("exit_t"), "closed": True}
    return None


_QUOTE = re.compile(r'^channel: "(.*?)"(?: · (.*))?$', re.S)


def followups(h: dict) -> list[dict]:
    """The channel's follow-ups linked to the hint: [{at, text[:80], action, did, delay_s}], oldest first."""
    from backend.tips import plan as pl
    posted = _dt(h.get("posted_at"))
    out = []
    for e in h.get("events") or []:
        if e.get("src") != "channel":
            continue
        txt = str(e.get("text") or "")
        m = _QUOTE.match(txt)
        if m:
            body, did = m.group(1), m.group(2) or ""
            act = pl.followup_action(body).get("action")
        elif txt.startswith("the channel edited the post"):
            body, did, act = txt, "", "edit"
        elif txt.startswith("the channel deleted the post"):
            body, did, act = txt, "", "deleted"
        else:
            body, did, act = txt, "", "note"
        at = _dt(e.get("t"))
        out.append({"at": e.get("t"), "text": body[:80], "action": act, "did": did,
                    "delay_s": int((at - posted).total_seconds()) if at and posted else None})
    return out


_AI_RE = re.compile(r'^\[AI\] (?:agrees · reads "(?P<q1>.*?)" as (?P<r1>.+)|#\d+ reads "(?P<q2>.*?)" as (?P<r2>.+?) · )')


def ai_opinions(k: dict) -> list[dict]:
    """The AI second opinions already read (no new call): [{at, line, reads, agrees}], from the hint's Kite log."""
    out = []
    for x in k.get("log") or []:
        t = str(x.get("text") or "")
        if not t.startswith("[AI]"):
            continue
        m = _AI_RE.match(t)
        if not m:
            continue
        agrees = m.group("r1") is not None
        reads = (m.group("r1") or m.group("r2") or "").split(" · ")[0].strip()
        out.append({"at": x.get("t"), "line": t[:160], "reads": reads, "agrees": agrees})
    return out


def build(h: dict, ts: list[dict], now: datetime) -> Optional[dict]:
    """The hint's trade facts from its Kite state and its ticks (pure; see the module doc). None before a Kite fill."""
    k = h.get("kite") if isinstance(h.get("kite"), dict) else {}
    epx = _num(k.get("entry_px"))
    if epx is None or k.get("state") not in ("entered", "closed"):
        return None
    sign = _sign(h)
    lot = int(h.get("lot_size") or 0) or 1
    qty = int(k.get("qty") or 0)
    entry_at = _dt(k.get("entry_at"))
    closed = k.get("state") == "closed"
    exit_at = _dt(k.get("closed_at")) if closed else None
    end = exit_at or now
    inside = [t for t in ts if t.get("ltp") is not None and (_dt(t.get("at")) is not None)
              and (entry_at is None or _dt(t["at"]) >= entry_at) and _dt(t["at"]) <= end]
    last = inside[-1] if inside else None
    ltp_now = _num(k.get("exit_px")) if closed else (_num(last["ltp"]) if last else None)
    mfe = mae = None
    for t in inside:
        mv = (float(t["ltp"]) - epx) * sign
        if mfe is None or mv > mfe["pts"]:
            mfe = {"pts": mv, "at": t["at"]}
        if mae is None or mv < mae["pts"]:
            mae = {"pts": mv, "at": t["at"]}

    def ext(x):
        if x is None:
            return None
        return {"rs": round(x["pts"] * qty, 2), "pct": round(x["pts"] / epx * 100, 1) if epx else None, "at": x["at"]}

    tg = [float(x) for x in ((h.get("plan") or {}).get("targets") or h.get("targets") or []) if _num(x)]
    stop = _stop_of(h, k)

    def first_cross(level, toward_profit: bool):
        if level is None:
            return None
        for t in inside:
            v = float(t["ltp"])
            hit = (v - level) * sign >= 0 if toward_profit else (v - level) * sign <= 0
            if hit and entry_at is not None:
                return int((_dt(t["at"]) - entry_at).total_seconds())
        return None

    near = None
    if stop is not None:
        for t in inside:
            v = float(t["ltp"])
            d = (v - stop) * sign / v * 100 if v else None
            if d is not None and (near is None or d < near):
                near = d
    legs = k.get("legs") or {}
    e_leg = legs.get("entry") or {}
    placed = _dt(e_leg.get("at"))
    q0 = _at_or_before(ts, placed)
    slip = None
    if q0 is not None:
        d = (epx - float(q0["ltp"])) * sign
        slip = {"rs": round(d, 2), "pct": round(d / float(q0["ltp"]) * 100, 1) if q0["ltp"] else None, "quote": q0["ltp"]}
    qd = _at_or_before(ts, placed, "bid")
    spread = round(float(qd["ask"]) - float(qd["bid"]), 2) if qd and qd.get("ask") is not None and qd.get("bid") is not None else None
    s_fill = _at_or_before(ts, entry_at, "spot")
    s_end = None
    for t in ts:
        d = _dt(t.get("at"))
        if t.get("spot") is not None and d is not None and d <= end:
            s_end = t
    posted = _dt(h.get("posted_at"))
    realised = float(k.get("realised") or 0.0)
    q_open = int(k.get("qty_open") or 0)
    buys, sells = [], []
    entry_side_buy = sign > 0
    (buys if entry_side_buy else sells).append((epx, qty))
    booked = qty - q_open
    exit_avg = None
    if booked > 0:
        exit_avg = epx + realised / (booked * sign)
        (sells if entry_side_buy else buys).append((exit_avg, booked))
    if q_open > 0 and ltp_now is not None and not closed:
        (sells if entry_side_buy else buys).append((ltp_now, q_open))
    ch = charges_est(buys, sells, h.get("exchange"))
    gross = round(realised + ((ltp_now - epx) * q_open * sign if (ltp_now is not None and not closed) else 0.0), 2)
    return {
        "entry_px": epx, "entry_at": k.get("entry_at"), "exit_px": _num(k.get("exit_px")) if closed else None,
        "exit_at": k.get("closed_at") if closed else None, "exit_by": k.get("exit_by") if closed else None,
        "qty": qty, "lots": round(qty / lot, 2) if lot else None, "qty_open": q_open, "ltp": ltp_now,
        "mfe": ext(mfe), "mae": ext(mae),
        "time_to_t1_s": first_cross(tg[0] if tg else None, True), "time_to_stop_s": first_cross(stop, False),
        "slippage_entry": slip, "spread_entry": spread,
        "spot_at_fill": _num(s_fill["spot"]) if s_fill else None, "spot_at_exit": _num(s_end["spot"]) if s_end else None,
        "post_to_fill_s": int((entry_at - posted).total_seconds()) if entry_at and posted else None,
        "stop": stop, "stop_nearest_pct": round(near, 1) if near is not None else None,
        "followups": followups(h), "ai_opinions": ai_opinions(k), "channel_result": channel_result(h),
        "charges_est": ch["total"], "gross": gross, "net_est": round(gross - ch["total"], 2),
        "final": closed, "at": now.isoformat(timespec="seconds"),
    }


def summary_line(f: Optional[dict]) -> str:
    """One line for the model's order read (kite_check.ai_input): the best / worst move and the nearest the stop came."""
    if not f:
        return "Trade facts: none yet (no Kite fill)"

    def mv(x):
        return f"{x['pct']:+.1f} %" if x and x.get("pct") is not None else "-"
    bits = [f"best {mv(f.get('mfe'))}", f"worst {mv(f.get('mae'))}"]
    if f.get("stop_nearest_pct") is not None:
        bits.append(f"nearest the stop {f['stop_nearest_pct']:.1f} %")
    if f.get("post_to_fill_s") is not None:
        bits.append(f"post to fill {f['post_to_fill_s']} s")
    if f.get("exit_by"):
        bits.append(f"closed by {f['exit_by']}")
    return "Trade facts: " + " · ".join(bits)


def for_hint(h: dict, now: Optional[datetime] = None) -> Optional[dict]:
    """The hint's facts: the stored final ones (with the channel's result and the follow-ups brought up to date), else
    built live from its ticks."""
    now = now or datetime.now(IST)
    st = h.get("facts") if isinstance(h.get("facts"), dict) else None
    if st and st.get("final"):
        k = h.get("kite") if isinstance(h.get("kite"), dict) else {}
        return {**st, "channel_result": channel_result(h), "followups": followups(h), "ai_opinions": ai_opinions(k)}
    k = h.get("kite") if isinstance(h.get("kite"), dict) else {}
    if k.get("state") not in ("entered", "closed") or k.get("entry_px") is None:
        return None
    return build(h, ticks(h["id"]), now)


def refresh(h: dict, now: datetime, conn=None) -> Optional[dict]:
    """Build and store the facts (tips_hints.facts, its own UPDATE); a stored final one is left as it is."""
    st = h.get("facts") if isinstance(h.get("facts"), dict) else None
    if st and st.get("final"):
        return st
    f = build(h, ticks(h["id"], conn), now)
    if f is None:
        return None
    if conn is None:
        with get_db() as c:
            c.execute("UPDATE tips_hints SET facts = ? WHERE id = ?", (json.dumps(f), h["id"]))
    else:
        conn.execute("UPDATE tips_hints SET facts = ? WHERE id = ?", (json.dumps(f), h["id"]))
    h["facts"] = f
    return f


# --- the day ---------------------------------------------------------------------------------------------------------------

def day_facts(config, now: datetime) -> dict:
    """tips_day_facts for `now`'s day: {calls, taken, won, lost, realised, net, cap_used, slowest_fill_s, reader_down_min,
    ai_disagreed, closed_by {auto, you, time_stop, target, stop}} (written by the 3:35 PM day check)."""
    from backend.tips import book as bk
    from backend.tips import kite_orders as ko
    from backend.tips import reader as rd
    day = now.date().isoformat()
    calls = bk.rows("posted_at >= ? AND posted_at < ? AND channel != 'rehearsal'", (day, (now.date() + timedelta(days=1)).isoformat()))
    kite = bk.rows("kite IS NOT NULL AND json_extract(kite, '$.day') = ?", (day,))
    taken = won = lost = disagreed = 0
    realised = net = 0.0
    slowest = None
    closed_by = {"auto": 0, "you": 0, "time_stop": 0, "target": 0, "stop": 0}
    for h in kite:
        k = ko.kstate(h)
        if k.get("entry_px") is None or k["state"] not in ("entered", "closed"):
            continue
        taken += 1
        f = build(h, ticks(h["id"]), now) or {}
        if k["state"] == "closed":
            r = float(k.get("realised") or 0.0)
            realised += r
            net += float(f.get("net_est") or r)
            won += r > 0
            lost += r < 0
            if k.get("exit_by") in closed_by:
                closed_by[k["exit_by"]] += 1
        if f.get("post_to_fill_s") is not None:
            slowest = max(slowest or 0, int(f["post_to_fill_s"]))
        disagreed += sum(1 for x in f.get("ai_opinions") or [] if not x.get("agrees"))
    st = rd.status()
    down = None
    since = _dt(st.get("since"))
    if st.get("state") not in ("live", None) and since is not None and since.date() == now.date():
        down = int((now - since).total_seconds() // 60)
    dry = ko.mode(config) == "dry"
    used = ko.realised_today(day, dry)
    out = {"day": day, "calls": len(calls), "taken": taken, "won": int(won), "lost": int(lost), "realised": round(realised, 2),
           "net": round(net, 2), "cap_used": round(abs(min(used, 0.0)), 2), "cap": float(config.tips.kite_loss_cap),
           "slowest_fill_s": slowest, "reader_down_min": down, "ai_disagreed": disagreed, "closed_by": closed_by, "dry": dry}
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO tips_day_facts (day, json) VALUES (?, ?)", (day, json.dumps(out)))
    return out
