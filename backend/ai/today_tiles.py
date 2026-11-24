"""✨ Home › Today: the phase-aware tiles (evening · pre-market · market hours). NO model call, no quote call, no broker
call: every number comes from code over stored rows (the real book's state and order log, the Tips hints, the AI
jobs table, the stored VIX quote, the stored candles and candle-model reads) and the in-memory one-point-a-minute
P&L series Home already keeps. The only model text is text the model already wrote and the app stored (the 9:00 AM
brief, tonight's notes, the newest Tips review's "one thing", the market backdrop line), each row tagged `ai`.
Nothing here sends, skips, holds, waits on or changes an order.

  panel_phase(now, config, trading)  evening (after the 3:40 PM summary until 6:00 AM; weekends / holidays all day) ·
                                     premarket (6:00 AM → the 9:16 AM send) · market (the send → 3:40 PM)
  panel(config, now, trading)        {phase, chip, step, one, tiles: [3 × {key, title, meta, last, rows, empty}],
                                      buttons, footer}

A row: {b, n, icon, v: [{t, c}], tag: {t, c}, s, s_ai, ai, dim} · a step: {kind: "step", st, t, at} · a sparkline:
{kind: "spark", pts, c}. A tile whose live list is empty shows the last session's rows instead (`last: true`, greyed,
the date in the header); only with no last session either does it keep its empty line (a fresh install)."""

import json
import os
from datetime import date, datetime, time, timedelta
from typing import Callable, Optional

from backend.config import IST, AppConfig
from backend.database.models import get_db
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm

logger = setup_logger("ai")

PANEL_PHASES = ("evening", "premarket", "market")
PREMARKET_FROM = time(6, 0)
NEAR_PCT = 3.0                       # a held name within this % of its stop is on the watch tile (red)
SLIP_OK_PCT = 0.5                    # a fill this far from the plan's price or nearer: ok; further: warn
AI_TONIGHT = 2                       # [AI] lines under tonight's steps at most
EMPTY_ORDERS = "no orders now · the next send is 9:16 AM on the next trading day"
EMPTY_WATCH = "nothing to watch right now"
EMPTY_BOOKS = "no book figures yet"
VARIANT = {"E": "E+"}


# --- small helpers ----------------------------------------------------------------------------------------------------

def _safe(fn: Callable, default, *a):
    try:
        return fn(*a)
    except Exception:
        logger.exception("today tiles: %s failed", getattr(fn, "__name__", fn))
        return default


def _hm(s: str) -> time:
    h, m = str(s).split(":")[:2]
    return time(int(h), int(m))


def _inr(v, sign: bool = False) -> str:
    from backend.alerts.messages import inr
    return inr(None if v is None else (0.0 if abs(v) < 0.5 else v), 0, sign)


def _px(v) -> str:
    """A price as stored, up to 2 decimals, thousands grouped: 1,257.2 · 805 · 829.4; '—' for none."""
    if v is None:
        return "—"
    return f"{float(v):,.2f}".rstrip("0").rstrip(".")


def _pct(v: Optional[float], sign: bool = True) -> str:
    if v is None:
        return "—"
    s = ("+" if v > 0 else "−" if v < 0 else "") if sign else ("−" if v < 0 else "")
    return f"{s}{abs(v):.1f} %"


def _tone(v) -> str:
    return "up" if (v or 0) > 0 else "down" if (v or 0) < 0 else ""


def _day_label(iso: str) -> str:
    d = date.fromisoformat(str(iso)[:10])
    return f"{d:%a} {d.day} {d:%b}"


def _t12(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return ""
    return ampm(dt.astimezone(IST) if dt.tzinfo else dt)


def _trading(d: date) -> bool:
    from backend.ingest.holidays import is_trading_day
    return is_trading_day(d)


def prev_session(day: str) -> str:
    d = date.fromisoformat(day)
    for _ in range(14):
        d -= timedelta(days=1)
        if _trading(d):
            return d.isoformat()
    return d.isoformat()


def next_session(day: str) -> str:
    from backend.engine import real
    return real.next_session(day)


def row(n: str = "", v=None, s: Optional[str] = None, *, b: Optional[str] = None, icon: Optional[str] = None, tag: Optional[tuple] = None,
        s_ai: Optional[str] = None, ai: bool = False, dim: bool = False) -> dict:
    """One tile row (see the module doc). `v` is a string or a list of (text, tone) pairs."""
    segs = [] if v is None else [{"t": v, "c": ""}] if isinstance(v, str) else [{"t": t, "c": c} for t, c in v if t]
    out = {"n": n, "v": segs}
    for k, x in (("b", b), ("icon", icon), ("s", s), ("s_ai", s_ai)):
        if x:
            out[k] = x
    if tag:
        out["tag"] = {"t": tag[0], "c": tag[1] if len(tag) > 1 else ""}
    if ai:
        out["ai"] = True
    if dim:
        out["dim"] = True
    return out


def tile(key: str, title: str, meta: str, rows: list, empty: str, last_rows: Optional[Callable] = None) -> dict:
    """A tile; an empty live list falls back to the last session's rows (greyed, its date in the header)."""
    t = {"key": key, "title": title, "meta": meta, "last": False, "rows": rows, "empty": empty}
    if not rows and last_rows is not None:
        got = _safe(last_rows, None)
        if got and got[1]:
            t.update(last=True, meta=f"last session · {_day_label(got[0])}", rows=got[1])
    return t


# --- the phase ----------------------------------------------------------------------------------------------------------

def panel_phase(now: datetime, config: AppConfig, trading: Optional[bool] = None) -> str:
    if trading is None:
        trading = _trading(now.date())
    if not trading:
        return "evening"
    t = now.time()
    if t < PREMARKET_FROM:
        return "evening"
    if t < _hm(config.real.send_time):
        return "premarket"
    if t < _hm(getattr(config.alerts, "day_summary_time", "15:40") or "15:40"):
        return "market"
    return "evening"


def _step(phase: str, now: datetime, config: AppConfig, trading: bool) -> int:
    """The lit timeline dot: 0 brief · 1 orders · 2 close · 3 summary · 4 night batch (the ones before are done)."""
    t = now.time()
    if phase == "premarket":
        return 0 if t < _hm(config.ai.brief_time) else 1
    if phase == "market":
        return 1 if t < time(15, 30) else 2
    if not trading or t < PREMARKET_FROM:
        return 4
    return 4 if t >= _hm(config.alerts.job_time) else 3


# --- the real book ------------------------------------------------------------------------------------------------------

def _st() -> dict:
    from backend.engine import real
    return real._state() or {}


def _variant(o: dict, st: dict, config: AppConfig) -> str:
    v = o.get("variant") or st.get("variant") or config.real.variant
    return VARIANT.get(v, v or "")


def _queued(config: AppConfig, now: datetime) -> Optional[dict]:
    """The pre-send check of the plan still to go (the next send after `now`), or None when no plan waits."""
    from backend.engine import real
    st = _st()
    if not st.get("orders") or not st.get("as_of"):
        return None
    send_day = next_session(st["as_of"])
    today = now.date().isoformat()
    if send_day < today or (send_day == today and now.time() >= _hm(config.real.send_time)):
        return None
    return real.presend(config, now)


def _order_name(o: dict, st: dict, config: AppConfig) -> tuple[str, str]:
    act = "buy" if o["action"] == "BUY" else "exit"
    return o["symbol"], f" · {act}" + (f" · {_variant(o, st, config)}" if o["action"] == "BUY" else "")


def _queued_rows(q: dict, st: dict, config: AppConfig, tags: bool) -> list:
    out = []
    for o in q["orders"]:
        b, n = _order_name(o, st, config)
        if o["action"] == "BUY":
            v = [(f"{_inr(o['value'])} · {o['qty']} sh" if o.get("value") else f"{o['qty']} sh", "")]
            sub = " · ".join(x for x in (f"stop {_px(o['stop'])}" if o.get("stop") else "", o.get("why") or "") if x)
        else:
            v = [(f"stop {_px(o['stop'])}" if o.get("stop") else f"{o['qty']} sh", "")]
            sub = " · ".join(x for x in (o.get("why") or "", f"{o['qty']} sh" if o.get("stop") else "") if x)
        if o["tag"] == "held":
            sub = " · ".join(x for x in (o.get("reason"), sub) if x)
        out.append(row(n, v, sub, b=b, tag=((o["tag"], "ok" if o["tag"] == "will send" else "w") if tags else None)))
    return out


def _order_log(day: str) -> list[dict]:
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT symbol, action, qty, status, created_at, price, reason FROM real_orders WHERE day = ? "
                                              "AND action IN ('BUY', 'EXIT') ORDER BY id", (day,))]


def slip_pct(action: str, price: Optional[float], ref: Optional[float]) -> Optional[float]:
    """The fill against the plan's price, signed so + is worse for you (a BUY paid more, an EXIT got less)."""
    if not price or not ref:
        return None
    return round(((price / ref) - 1) * 100 if action == "BUY" else ((ref / price) - 1) * 100, 1)


def slip_tag(pct: Optional[float]) -> Optional[tuple]:
    if pct is None:
        return None
    return (_pct(pct) if pct else "0 %", "ok" if abs(pct) <= SLIP_OK_PCT else "w")


def _sent_rows(day: str, st: dict, config: AppConfig) -> list:
    """The day's order log: sent (with the price and the slippage tag), failed, skipped."""
    from backend.engine import real
    plan = {(o["symbol"], o["action"]): o for o in st.get("orders") or []}
    pos = st.get("positions") or {}
    out = []
    for r in _order_log(day):
        sym, act = r["symbol"], r["action"]
        o = plan.get((sym, act)) or {}
        n = " · buy" if act == "BUY" else " · exit"
        if r["status"] == "sent":
            p = pos.get(sym)
            ref = o.get("close") if act == "BUY" else None
            filled = act == "EXIT" or (p is not None and real.confirmed(p))
            word = "filled" if filled else "sent"
            pct = slip_pct(act, r["price"], ref)
            stop = (p or {}).get("stop") or o.get("stop")
            sub = " · ".join(x for x in (f"at {_t12(r['created_at'])}", f"plan {_px(ref)}" if ref else "",
                                         f"stop {_px(stop)} watched" if act == "BUY" and stop else "",
                                         "fill not confirmed yet" if act == "BUY" and p is not None and not real.confirmed(p) else "",
                                         r.get("reason") or "" if act == "EXIT" else "") if x)
            out.append(row(n, f"{word} {_px(r['price'])}" if r["price"] else word, sub, b=sym,
                           tag=slip_tag(pct) if pct is not None else (("done", "ok") if act == "EXIT" else None)))
        else:
            out.append(row(n, [("not sent", "dim")], " · ".join(x for x in (f"at {_t12(r['created_at'])}", r.get("reason") or "") if x), b=sym,
                           tag=(r["status"], "w")))
    return out


def _last_orders(day: str, config: AppConfig) -> Optional[tuple]:
    with get_db() as conn:
        d = conn.execute("SELECT MAX(day) FROM real_orders WHERE day < ?", (day,)).fetchone()[0]
    if not d:
        return None
    return d, [dict(r, dim=True) for r in _sent_rows(d, _st(), config)]


def _positions(st: dict, live: bool) -> list[dict]:
    """[{symbol, price, stop, target, stop_pct, target_pct}] of the open real positions (the live price when there is
    one and `live`, else the last close); stop_pct = how far the stop is under the price."""
    out = []
    for sym, p in (st.get("positions") or {}).items():
        px = (p.get("live") if live else None) or p.get("last")
        if not px:
            continue
        stop, tgt = p.get("stop"), p.get("target")
        out.append({"symbol": sym, "price": px, "stop": stop, "target": tgt, "qty": p.get("qty"),
                    "stop_pct": round((px - stop) / px * 100, 1) if stop else None,
                    "target_pct": round((tgt - px) / px * 100, 1) if tgt else None})
    return out


def open_risk(st: dict) -> Optional[float]:
    """₹ lost if every open real position went out at its stop from the last price."""
    ps = _positions(st, live=False)
    if not ps:
        return None
    return round(sum(max(0.0, (p["price"] - p["stop"]) * (p["qty"] or 0)) for p in ps if p["stop"]), 2)


# --- the candle model on the held names (stored reads; shadow tag) ----------------------------------------------------------

def held_odds(config: AppConfig, now: datetime) -> Optional[dict]:
    """{mode, items: ['DEF stop-first 52 %', 'GHI up 66 %', …], at} from the stored daily reads; None when off."""
    from backend.market import reads as mr
    from backend.market import real_reads as rr
    if not mr.on(config):
        return None
    got = {}
    for sym, pos in rr.open_positions().items():
        nb = rr.numbers(sym, pos, now.date().isoformat())
        if nb["at"]:
            got[sym] = nb
    mode = rr.gate(config)["mode"]
    if not got:
        return {"mode": mode, "items": [], "at": None}
    items = []
    sf = {s: x["stop_first"] for s, x in got.items() if x["stop_first"] is not None}
    for s in sorted(sf, key=lambda s: (-sf[s], s))[:1]:
        items.append((s, f"{s} stop-first {sf[s]} %"))
    ups = {s: x["up"] for s, x in got.items() if x["up"] is not None and s not in dict(items)}
    for s in sorted(ups, key=lambda s: (-ups[s], s)):
        items.append((s, f"{s} up {ups[s]} %"))
    at = max((x["at"] for x in got.values() if x["at"]), default=None)
    return {"mode": mode, "items": [t for _, t in items], "by": {s: x for s, x in got.items()}, "at": at}


def _odds_row(o: Optional[dict], n: int, title: str = "candle model · held names") -> Optional[dict]:
    if o is None:
        return None
    sub = " · ".join(o["items"][:n]) + (f" · read {_t12(o['at'])}" if o["at"] else "") if o["items"] else "no read yet"
    return row(title, None, sub, tag=(o["mode"], "w"), dim=True)


# --- Tips -----------------------------------------------------------------------------------------------------------------

def _tips_on(config: AppConfig) -> bool:
    from backend.tips import kite_orders as ko
    return bool(config.tips.enabled) or ko.mode(config) != "off"


def _reader_word() -> str:
    from backend.tips import reader as rd
    s = rd.status().get("state")
    return {"live": "reader live", "preview": "reader on the preview page", "down": "reader down", "logged_out": "reader logged out"}.get(s, "reader —")


def tips_channel_row(config: AppConfig, now: datetime, last_call: bool = False) -> Optional[dict]:
    from backend.tips import kite_orders as ko
    from backend.tips import track as tr
    if not _tips_on(config):
        return None
    first = tr.first_call(30, now)
    lite = ko.lite(config, now)
    c, sz = lite["caps"], lite["sizing"]
    caps = f"caps {c['entries']} of {c['max_entries']} entries · {c['max_lots']} lots max · risk {_inr(sz['risk_per_trade'])}"
    if last_call:
        lc = _last_call(now)
        sub = " · ".join(x for x in (lc, f"first call usually {first}" if first else "") if x) or caps
        return row("Tips channel", [(_reader_word(), "dim")], sub)
    return row("Tips channel", [(f"first call usually {first}" if first else "no call in 30 days", "dim")], f"{_reader_word()} · {caps}")


def _last_call(now: datetime) -> Optional[str]:
    from backend.tips import book as bk
    hs = bk.rows("posted_at < ? AND channel != 'rehearsal'", (now.date().isoformat(),), order="posted_at DESC", limit=1)
    if not hs:
        return None
    h = hs[0]
    d = h["posted_at"][:10]
    taken = len(bk.rows("posted_at >= ? AND posted_at < ? AND channel != 'rehearsal' AND kite IS NOT NULL "
                        "AND json_extract(kite, '$.entry_px') IS NOT NULL", (d, d + "T99")))
    when = "yesterday" if d == prev_session(now.date().isoformat()) else _day_label(d)
    return f"last call {when} {_t12(h['posted_at'])} · {taken} taken"


def _tips_day(config: AppConfig, day: str) -> Optional[dict]:
    """{pnl, n, parts: ['#14 +270', …], one} of the Kite hints of `day` (closed ones by their booked ₹)."""
    from backend.tips import ai4
    from backend.tips import book as bk
    from backend.tips import kite_orders as ko
    hs = bk.rows("kite IS NOT NULL AND json_extract(kite, '$.day') = ?", (day,), order="id")
    hs = [h for h in hs if ko.kstate(h).get("entry_px") is not None]
    if not hs:
        return None
    parts, tot = [], 0.0
    for h in hs:
        k = ko.kstate(h)
        r = float(k.get("realised") or 0.0)
        if k["state"] == "entered":
            from backend.tips import home as th
            r = th.open_pnl(h) or 0.0
        tot += r
        parts.append(f"#{h['id']} {_inr(r, True).replace('₹', '')}")
    one = None
    for r in reversed(ai4.rows("review", day=day)):
        if r.get("ok"):
            x = next((ln["text"] for ln in ((r.get("lines_json") or {}).get("lines") or []) if ln.get("label") == "one thing"), None)
            if x:
                one = f"#{r['hint_id']}: {x}" if r.get("hint_id") else x
                break
    return {"pnl": round(tot, 2), "n": len(hs), "parts": parts, "one": one}


# --- the night batch (the AI jobs table + the evening job's row) -----------------------------------------------------------

STEP_ST = {"done": "done", "running": "run", "queued": "queued", "skipped": "skipped", "failed": "failed"}


def _job(kind: str, key_like: str) -> list[dict]:
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT status, created, started, finished, error, note FROM ai_jobs WHERE kind = ? AND key LIKE ? "
                                              "ORDER BY id", (kind, key_like))]


def _agg(jobs: list[dict]) -> tuple[str, str]:
    """(state, right text) of a group of jobs: running if any runs, queued if any waits, done (the last finish) if any
    finished, else skipped / failed."""
    if not jobs:
        return "wait", ""
    sts = [j["status"] for j in jobs]
    if "running" in sts:
        return "run", "running"
    if "queued" in sts:
        return "queued", "queued"
    if "done" in sts:
        return "done", _t12(max((j["finished"] or "") for j in jobs if j["status"] == "done"))
    return STEP_ST.get(sts[-1], "skipped"), sts[-1]


def night_steps(config: AppConfig, night: str) -> dict:
    """{ran, rows, span}: the night batch of `night` (the evening's date) step by step from the stored rows."""
    from backend.market import reads as mr
    after = ampm(config.alerts.job_time)
    with get_db() as conn:
        eod = conn.execute("SELECT status, started_at, finished_at, message FROM job_runs WHERE job = 'eod' AND run_date = ? ORDER BY id DESC LIMIT 1",
                           (night,)).fetchone()
        scan = conn.execute("SELECT as_of, scanned, ran_at FROM scan_runs WHERE as_of <= ? ORDER BY id DESC LIMIT 1", (night,)).fetchone()
        notes = conn.execute("SELECT symbol FROM ai_notes WHERE day = ? AND kind IN ('filing', 'deal') ORDER BY id", (night,)).fetchall()
        rated = conn.execute("SELECT COUNT(*) FROM ai_views WHERE day = ? AND status = 'ok'", (night,)).fetchone()[0]
        tried = conn.execute("SELECT COUNT(*) FROM ai_views WHERE day = ?", (night,)).fetchone()[0]
        sugg = conn.execute("SELECT COUNT(*) FROM ai_suggestions WHERE day = ?", (night,)).fetchone()[0]
        score_run = conn.execute("SELECT status, finished_at FROM job_runs WHERE job = ? AND run_date = ? ORDER BY id DESC LIMIT 1",
                                 (mr.SCORE_JOB, night)).fetchone()
    rows, ran = [], False
    eod_ok = bool(eod and eod["status"] in ("ok", "partial"))
    if eod:
        ran = True
    e_st = "done" if eod_ok else "run" if eod and eod["status"] in ("running", "waiting", "retry") else "failed" if eod else "wait"
    e_at = _t12(eod["finished_at"]) if eod_ok else (eod["status"] if eod else f"after {after}")
    as_of = scan["as_of"] if scan and eod_ok else None
    rows.append({"kind": "step", "st": e_st, "t": f"daily candles to {_day_label(as_of)}" if as_of else "daily candles", "at": e_at})
    rows.append({"kind": "step", "st": e_st, "t": f"evening scan · {scan['scanned']} names" if as_of else "evening scan", "at": e_at})
    syms = [r[0] for r in notes if r[0]]
    cnt = {s: syms.count(s) for s in dict.fromkeys(syms)}
    names = ", ".join(f"{s} ×{n}" if n > 1 else s for s, n in list(cnt.items())[:3])
    st, at = _agg(_job("night", "note:%"))
    if st == "wait" and eod_ok:
        st, at = "skipped", "nothing new"
    rows.append({"kind": "step", "st": st, "t": f"notes · {len(notes)} filing{'s' if len(notes) != 1 else ''}" + (f" ({names})" if names else ""), "at": at or f"after {after}"})
    if config.ai.why:
        st, at = _agg(_job("night", f"why:{night}"))
        rows.append({"kind": "step", "st": st, "t": "why · the lines under today's exits", "at": at or f"after {after}"})
    jobs = _job("night", f"view:{night}") + _job("night", f"suggest:{night}")
    st, at = _agg(jobs)
    rows.append({"kind": "step", "st": st, "t": f"ratings · {rated} of {tried} · suggestions {sugg}", "at": at or f"after {after}"})
    st, at = _agg(_job("market", f"market:score:{night}"))
    if st == "wait" and score_run:
        st, at = ("done", _t12(score_run["finished_at"])) if score_run["status"] == "ok" else ("failed", score_run["status"])
    if mr.on(config):
        rows.append({"kind": "step", "st": st, "t": "candle model scorecard read", "at": at or ampm(mr.SCORE_TIME)})
    ran = ran or any(r["st"] not in ("wait",) for r in rows)
    ends = [r["at"] for r in rows if r["st"] == "done" and r["at"]]
    return {"ran": ran, "rows": rows, "end": ends[-1] if ends else None, "done": bool(rows) and all(r["st"] in ("done", "skipped") for r in rows)}


def tonight_ai(night: str, n: int = AI_TONIGHT) -> list[dict]:
    """Up to n [AI] lines the model already wrote tonight (the newest filing / deal notes, then the rating changes)."""
    out = []
    with get_db() as conn:
        for r in conn.execute("SELECT symbol, title, text FROM ai_notes WHERE day = ? AND kind IN ('filing', 'deal') AND model IS NOT NULL "
                              "ORDER BY id DESC LIMIT ?", (night, n)):
            t = (r["text"] or r["title"] or "").strip().split("\n")[0]
            if t:
                out.append(row(f"{r['symbol']}: {t}" if r["symbol"] and not t.startswith(r["symbol"]) else t, None, ai=True))
        if len(out) < n:
            for r in conn.execute("SELECT symbol, change FROM ai_views WHERE day = ? AND status = 'ok' AND change IS NOT NULL AND change != '' "
                                  "ORDER BY id DESC LIMIT ?", (night, n - len(out))):
                out.append(row(f"{r['symbol']}: {r['change']}", None, ai=True))
    return out[:n]


# --- the books ------------------------------------------------------------------------------------------------------------

def _curve() -> list:
    from backend.engine import overview
    return [list(x) for x in overview._today_curve]


def _hilo(curve: list, day: str) -> Optional[dict]:
    pts = [(t, v) for t, v in curve if str(t).startswith(day)]
    if not pts:
        return None
    hi, lo = max(pts, key=lambda p: p[1]), min(pts, key=lambda p: p[1])
    return {"hi": hi[1], "hi_at": _t12(hi[0]), "lo": lo[1], "lo_at": _t12(lo[0])}


def spark(curve: list, day: str) -> Optional[dict]:
    pts = [v for t, v in curve if str(t).startswith(day)]
    if len(pts) < 2:
        return None
    return {"kind": "spark", "pts": pts[-120:], "c": "up" if pts[-1] >= 0 else "down"}


def _day_moves(st: dict, day: str) -> list[tuple]:
    """[(symbol, % move on `day`)] of the held names from the stored daily candles (the last two closes up to `day`)."""
    from backend.market import real_reads as rr
    out = []
    for s, x in (st.get("positions") or {}).items():
        tok = rr.token_of(s, x)
        bars = rr.daily_bars(tok, 2, until=day) if tok is not None else []
        if len(bars) == 2 and bars[0].get("close"):
            out.append((s, (bars[1]["close"] / bars[0]["close"] - 1) * 100))
    return out


def _real_line(config: AppConfig, day: str, st: dict) -> Optional[dict]:
    """The Real book's day: {value, today, worst, best, slots, unconfirmed}; None without an account."""
    from backend.alerts import daysum
    from backend.engine import real
    p = daysum.real_part(config, day)
    if not p or not st:
        return None
    ch = _day_moves(st, day)
    worst = min(ch, key=lambda c: c[1]) if ch else None
    best = max(ch, key=lambda c: c[1]) if ch else None
    return {"value": p.get("value"), "today": p.get("today"), "worst": worst, "best": best,
            "slots": f"{len(st.get('positions') or {})} of {st.get('cap') or 0} slots", "unconfirmed": len(real.unconfirmed(st))}


def _real_row(config: AppConfig, day: str, st: dict, value_first: bool = False) -> Optional[dict]:
    x = _real_line(config, day, st)
    if not x:
        return None
    t = x["today"]
    pnl = (_inr(t, True) if t is not None else "not marked yet", _tone(t))
    val = (_inr(x["value"]), "")
    subs = [f"worst {x['worst'][0]} {_pct(x['worst'][1])}" if x["worst"] else "", f"best {x['best'][0]} {_pct(x['best'][1])}" if x["best"] else "",
            x["slots"], f"{x['unconfirmed']} fill{'s' if x['unconfirmed'] != 1 else ''} unconfirmed" if x["unconfirmed"] else ""]
    return row(" · E+", [val, pnl] if value_first else [pnl, val], " · ".join(s for s in subs if s), b="Real", icon="💰")


def _paper_rows(config: AppConfig, day: str, value_first: bool = False) -> list:
    from backend.alerts import daysum
    out = []
    for p in daysum.paper_parts(config, day):
        t = p.get("today")
        pnl = (_inr(t, True) if t is not None else f"marks at {ampm(p.get('marks_at') or config.alerts.job_time)}", _tone(t) if t is not None else "dim")
        val = (_inr(p.get("value")), "")
        name = p.get("name") or "E+"
        out.append(row(f" · {name}" if name else "", [val, pnl] if value_first else [pnl, val],
                       f"{p['stops']} stop{'s' if p['stops'] != 1 else ''} today" if p.get("stops") else None, b="Paper", icon="📝"))
    return out


def _options_today(config: AppConfig, now: datetime) -> Optional[dict]:
    """{pnl, trades, text} of the options book today (its own stored state; no chain or quote call)."""
    from backend.options import book as obook
    st = obook._state()
    if not st:
        return None
    day = now.date().isoformat()
    d = st.get("day") or {}
    if d.get("date") != day:
        with get_db() as conn:
            r = conn.execute("SELECT payload FROM options_days WHERE day = ?", (day,)).fetchone()
        if r:
            x = json.loads(r[0])
            return {"pnl": x.get("pnl") or 0.0, "trades": x.get("trades") or 0, "text": x.get("result") if x.get("trades") else x.get("why")}
        return {"pnl": None, "trades": 0, "text": None}
    o = st.get("open")
    pnl = round(float(d.get("realised") or 0.0) + (obook._open_pnl(o) if o else 0.0), 2)
    return {"pnl": pnl, "trades": d.get("trades") or 0, "open": bool(o), "text": d.get("why") or d.get("status")}


def _options_row(config: AppConfig, now: datetime, live: bool) -> Optional[dict]:
    x = _options_today(config, now)
    if x is None:
        return None
    c = config.options
    if x["trades"] or x.get("open"):
        v = [(_inr(x["pnl"], True), _tone(x["pnl"])), (f"{x['trades']} trade{'s' if x['trades'] != 1 else ''}", "")]
    else:
        v = [("no trade today" if live else "₹0 · no trade", "dim" if live else "")]
    sub = x.get("text") or ""
    if live and not x["trades"] and getattr(c, "afternoon_sell", False):
        sub = " · ".join(s for s in (sub, f"afternoon look {ampm(c.afternoon_time)}") if s)
    return row(" · paper", v, sub or None, b="Options", icon="🧩")


def _tips_book_row(config: AppConfig, day: str, live: bool, now: Optional[datetime] = None) -> Optional[dict]:
    if not _tips_on(config):
        return None
    x = _tips_day(config, day)
    if not x:
        return row(" · Kite", [("no trade", "dim")], None, b="Tips", icon="💡")
    n_open = None
    if live:
        from backend.engine.today_pnl import kite_part
        k = kite_part(config, now or datetime.now(IST))
        n_open = (k or {}).get("n_open")
    v = [(_inr(x["pnl"], True), _tone(x["pnl"])), (f"{n_open} open" if n_open else f"{x['n']} trade{'s' if x['n'] != 1 else ''}", "")]
    return row(" · Kite", v, " · ".join(x["parts"][:5]), b="Tips", icon="💡", s_ai=None if live else x["one"])


def _all_books_row(config: AppConfig, now: datetime, total: Optional[float]) -> Optional[dict]:
    day = now.date().isoformat()
    h = _hilo(_curve(), day)
    if total is None and not h:
        return None
    sub = f"high {_inr(h['hi'], True)} at {h['hi_at']} · low {_inr(h['lo'], True)} at {h['lo_at']}" if h else "high / low from the open: no sample yet"
    return row("all books", [(_inr(total, True) if total is not None else "—", _tone(total))], sub, dim=True)


def _today_total(config: AppConfig, now: datetime) -> Optional[float]:
    from backend.engine.today_pnl import today_pnl
    t = today_pnl(config, now)
    return t["total"] if any(p.get("pnl") or p.get("traded") for p in t["parts"]) else None


# --- the tiles per phase ----------------------------------------------------------------------------------------------------

def _last_on_record(sql: str, day: str) -> str:
    """The latest day a table has before `day` (the last session on record), else the previous trading day."""
    with get_db() as conn:
        d = conn.execute(sql, (day,)).fetchone()[0]
    return d or prev_session(day)


def _evening(config: AppConfig, now: datetime, trading: bool) -> dict:
    day = now.date().isoformat()
    early = trading and now.time() < PREMARKET_FROM          # after midnight, before 6:00 AM on a trading day
    st = _safe(_st, {})
    q = _safe(_queued, None, config, now)
    orders = _safe(_queued_rows, [], q, st, config, False) if q else []
    send_day = (q or {}).get("day") or (day if early else next_session(day))
    send = ampm(config.real.send_time)
    head = "This morning" if send_day == day else "Tomorrow" if send_day == (now.date() + timedelta(days=1)).isoformat() else \
        f"{date.fromisoformat(send_day):%a}"
    kite = "Kite logged in" if _safe(kite_login, {}, now).get("in") else "Kite not logged in"
    t1 = tile("tomorrow", f"{head} · {send}", f"{len(orders)} queued · {kite}" if orders else f"the plan comes after {ampm(config.alerts.job_time)}",
              orders, EMPTY_ORDERS, lambda: _last_orders(next_session(day) if trading and not early else day, config))
    t1["rows"] = t1["rows"] + [r for r in (_safe(tips_channel_row, None, config, now), _odds_row(_safe(held_odds, None, config, now), 2)) if r]
    # tonight: the night batch of the latest session (a weekend / holiday shows the last session's night, greyed)
    night = day if trading and not early else _last_on_record("SELECT MAX(run_date) FROM job_runs WHERE job = 'eod' AND run_date < ?", day)
    ns = _safe(night_steps, {"ran": False, "rows": [], "end": None, "done": False}, config, night)
    job = ampm(config.alerts.job_time)
    if not ns["ran"] and night != day and not early:          # a holiday with no night batch on record (a fresh install)
        t2 = {"key": "tonight", "title": "Tonight", "meta": f"from {job}", "last": False, "rows": [], "empty": _tonight_text(config)}
    elif not ns["ran"]:
        t2 = {"key": "tonight", "title": "Tonight", "meta": f"from {job} · queued", "last": False,
              "rows": [row(_tonight_text(config), None, dim=True)] + [dict(r, st="queued" if r["st"] == "wait" else r["st"]) for r in ns["rows"]],
              "empty": _tonight_text(config)}
    else:
        meta = f"{job} → {ns['end']} · {'done' if ns['done'] else 'running'}" if ns["end"] else f"from {job}"
        t2 = {"key": "tonight", "title": "Tonight", "meta": meta, "last": False, "rows": ns["rows"] + _safe(tonight_ai, [], night),
              "empty": _tonight_text(config)}
        if night != day and not early:
            t2.update(last=True, meta=f"last session · {_day_label(night)}")
    # the books of the latest session
    bday = day if trading and not early else _last_on_record("SELECT MAX(day) FROM real_equity WHERE day < ?", day)
    t3 = _books_tile(config, now, bday, "Books · today" if bday == day else "Books · last session", live=False)
    return {"tiles": [t1, t2, t3], "one": _evening_one(config, ns, q, trading, now),
            "buttons": [], "footer": f"at {ampm(config.ai.brief_time)} the brief, at {send} the orders · nothing here acts on its own"}


def _tonight_text(config: AppConfig) -> str:
    parts = ["notes"] + (["why"] if config.ai.why else []) + ["suggestions"] + (["ratings"] if config.ai.view_enabled else [])
    return f"tonight: {', '.join(parts)} after {ampm(config.alerts.job_time)}"


def _evening_one(config: AppConfig, ns: dict, q: Optional[dict], trading: bool, now: datetime) -> str:
    n = len([o for o in (q or {}).get("orders") or [] if o["tag"] == "will send"])
    nxt = "tomorrow" if trading and now.time() >= PREMARKET_FROM else "this morning" if trading else "next session"
    plan = f"{nxt}: {n} order{'s' if n != 1 else ''} queued for {ampm(config.real.send_time)}" if q else f"{nxt}: the plan comes with the night batch"
    if not trading:
        return f"holiday · the next session is {date.fromisoformat(next_session(now.date().isoformat())):%a} · {plan.split(': ', 1)[1]}"
    head = _tonight_text(config).split(" after ")[0] if not ns.get("done") else "tonight's batch is done"
    return f"{head} · {plan}"


def _books_tile(config: AppConfig, now: datetime, bday: str, title: str, live: bool) -> dict:
    st = _safe(_st, {})
    rows = [r for r in (_safe(_real_row, None, config, bday, st),) if r]
    rows += _safe(_paper_rows, [], config, bday)
    rows += [r for r in (_safe(_tips_book_row, None, config, bday, live, now), _safe(_options_row, None, config, now if bday == now.date().isoformat() else
                                                                                  datetime.fromisoformat(bday + "T15:40:00+05:30"), live)) if r]
    if bday == now.date().isoformat():
        a = _safe(_all_books_row, None, config, now, _safe(_today_total, None, config, now))
        if a:
            rows.append(a)
    traded = _safe(_traded_on, False, bday)
    same = bday == now.date().isoformat()
    meta = ("live · " + ampm(now)) if live else "today" if traded and same else "last session" + ("" if same else f" · {_day_label(bday)}")
    return {"key": "books", "title": title, "meta": meta, "last": False, "rows": rows, "empty": EMPTY_BOOKS}


def _traded_on(day: str) -> bool:
    with get_db() as conn:
        a = conn.execute("SELECT COUNT(*) FROM real_orders WHERE day = ? AND status = 'sent'", (day,)).fetchone()[0]
        b = conn.execute("SELECT COUNT(*) FROM tips_orders WHERE day = ? AND leg != 'test' AND status NOT IN ('voided', 'failed', 'rejected')",
                         (day,)).fetchone()[0]
    return bool(a or b)


def kite_login(now: datetime) -> dict:
    """{in, till}: a stored Kite token still fresh at `now` (the 6:00 AM rule), from the DB only."""
    from backend.kite_api import auth
    with get_db() as conn:
        tok, issued = auth.load_token(conn)
    ok = bool(tok) and auth.is_fresh(issued, now)
    return {"in": ok, "till": ampm(auth.expiry_for(issued)) if ok and issued else None}


def _kite_row(now: datetime) -> dict:
    k = kite_login(now)
    hook = "webhook token present" if os.getenv("TRADETRON_TOKEN", "").strip() else "webhook token missing"
    v = [("logged in", "up"), (f"valid till {k['till']}", "")] if k["in"] else [("not logged in", "down")]
    return row("Kite", v, f"{hook} · orders confirm before sending")


def _premarket(config: AppConfig, now: datetime) -> dict:
    day = now.date().isoformat()
    st = _safe(_st, {})
    q = _safe(_queued, None, config, now)
    orders = _safe(_queued_rows, [], q, st, config, True) if q else []
    n_go = sum(1 for o in (q or {}).get("orders") or [] if o["tag"] == "will send")
    n_held = sum(1 for o in (q or {}).get("orders") or [] if o["tag"] == "held")
    t1 = tile("orders", f"Orders · {ampm(config.real.send_time)}", f"{n_go} will send · {n_held} held" if orders else "nothing queued",
              orders, EMPTY_ORDERS, lambda: _last_orders(day, config))
    t1["rows"] = t1["rows"] + [_safe(_kite_row, row("Kite", "—"), now)]
    # watch before the open
    w = []
    from backend.ai import brief
    b = _safe(brief.latest, None, day)
    lines = [ln for ln in (b or {}).get("lines") or [] if ln]
    if lines:
        w += [row(ln, None, ai=True) for ln in lines[:3]]
    else:
        w.append(row(f"brief at {ampm(config.ai.brief_time)}", None, "the model writes it from the app's own numbers", dim=True))
    vx = _safe(_vix_row, None)
    if vx:
        w.append(vx)
    o = _odds_row(_safe(held_odds, None, config, now), 3, "Held names · candle model")
    if o:
        w.append(o)
    f = _safe(_filings_row, None, day)
    if f:
        w.append(f)
    tc = _safe(tips_channel_row, None, config, now, True)
    if tc:
        w.append(tc)
    t2 = {"key": "watch", "title": "Watch · before the open", "meta": f"the {ampm(config.ai.brief_time)} brief" + ("" if lines else " · not out yet"),
          "last": False, "rows": w, "empty": EMPTY_WATCH}
    # books · yesterday close
    yday = prev_session(day)
    rows = [r for r in (_safe(_real_row, None, config, yday, st, True),) if r]
    if rows:
        ps = _positions(st, live=False)
        near = sum(1 for p in ps if p["stop_pct"] is not None and p["stop_pct"] <= NEAR_PCT)
        rows[0]["s"] = f"{len(ps)} held · {near} within {NEAR_PCT:g} % of the stop"
    rows += _safe(_paper_rows, [], config, yday, True)
    tb = _safe(_tips_yday_row, None, config, now, yday)
    if tb:
        rows.append(tb)
    if _safe(lambda: __import__("backend.options.book", fromlist=["has_account"]).has_account(), False):
        rows.append(row("", [(f"next look {ampm(config.options.window_start)}", "")], f"the 9:15–9:30 range decides · bias from {ampm(config.options.window_start)}",
                        b="Options", icon="🧩"))
    risk = _safe(open_risk, None, st)
    t3 = {"key": "books", "title": "Books · yesterday close", "meta": f"open risk {_inr(risk)} at the stops" if risk is not None else _day_label(yday),
          "last": False, "rows": rows, "empty": EMPTY_BOOKS}
    brief_out = bool(lines)
    k = _safe(kite_login, {}, now)
    one = (f"{ampm(config.real.send_time)}: {n_go} order{'s' if n_go != 1 else ''} will go" + (f" · {n_held} held" if n_held else "")
           if q else f"{ampm(config.real.send_time)}: no orders queued") + \
        f" · Kite {'logged in' if k.get('in') else 'not logged in'} · " + ("the brief is out" if brief_out else f"the brief at {ampm(config.ai.brief_time)}")
    buttons = [{"label": f"Confirm the {ampm(config.real.send_time).replace(' AM', '').replace(' PM', '')} send…", "kind": "go", "arg": "portfolio/real"}] if n_go else []
    return {"tiles": [t1, t2, t3], "one": one, "buttons": buttons,
            "footer": f"orders send at {ampm(config.real.send_time)} after your confirm · the brief " + (f"came at {ampm(config.ai.brief_time)}" if brief_out else f"comes at {ampm(config.ai.brief_time)}")}


def _vix_row() -> Optional[dict]:
    from backend.market import reads as mr
    vx = mr.vix() or {}
    if not vx.get("last"):
        return None
    chg = round((vx["last"] - vx["prev"]) / vx["prev"] * 100, 1) if vx.get("prev") else None
    return row("India VIX", [(f"{vx['last']:.1f}", ""), (_pct(chg) if chg is not None else "", _tone(-(chg or 0)))],
               f"the stored quote · {_t12(vx.get('at'))}" if vx.get("at") else None)


def _filings_row(day: str) -> Optional[dict]:
    """Filings since the last session's close: the count, the names and whether a held name is among them."""
    since = f"{prev_session(day)}T15:30"
    held = set((_st().get("positions") or {}).keys())
    with get_db() as conn:
        rs = conn.execute("SELECT symbol FROM filings WHERE dt >= ? ORDER BY dt", (since,)).fetchall()
        n_notes = conn.execute("SELECT COUNT(*) FROM ai_notes WHERE kind = 'filing' AND created >= ?", (since,)).fetchone()[0]
    if not rs:
        return row("Filings overnight", [("none", "dim")], None)
    syms = [r[0] for r in rs]
    cnt = {s: syms.count(s) for s in dict.fromkeys(syms)}
    names = " · ".join(f"{s} ×{n}" if n > 1 else s for s, n in list(cnt.items())[:4])
    h = sorted(set(cnt) & held)
    return row("Filings overnight", f"{len(rs)} filing{'s' if len(rs) != 1 else ''}" + (f" · {n_notes} note{'s' if n_notes != 1 else ''}" if n_notes else ""),
               f"{names} · " + (f"held: {', '.join(h)}" if h else "no held name"))


def _tips_yday_row(config: AppConfig, now: datetime, yday: str) -> Optional[dict]:
    from backend.tips import kite_orders as ko
    if not _tips_on(config):
        return None
    x = _tips_day(config, yday)
    lite = ko.lite(config, now)
    c, sz = lite["caps"], lite["sizing"]
    v = [(_inr(x["pnl"], True), _tone(x["pnl"])), ("yesterday", "")] if x else [("no trade yesterday", "dim")]
    return row(" · Kite", v, f"caps {c['entries']} of {c['max_entries']} · {c['max_lots']} lots max · risk {_inr(sz['risk_per_trade'])} per trade",
               b="Tips", icon="💡")


def _market(config: AppConfig, now: datetime) -> dict:
    day = now.date().isoformat()
    st = _safe(_st, {})
    sent = _safe(_sent_rows, [], day, st, config)
    log = _safe(_order_log, [], day)
    n_sent = sum(1 for r in log if r["status"] == "sent")
    n_fill = sum(1 for r in sent if r["v"] and r["v"][0]["t"].startswith("filled"))
    first = _t12(min((r["created_at"] for r in log), default=None)) if log else ""
    tips = _safe(_tips_live_rows, [], config, now)
    t1 = tile("orders", "Orders · today", f"{n_sent} sent · {n_fill} filled" + (f" · {first}" if first else "") if log else "none sent today",
              sent, EMPTY_ORDERS, lambda: _last_orders(day, config))
    t1["rows"] = t1["rows"] + tips + [row("next", [(f"stop check every {config.real.stop_check_min} min", "dim")],
                                          f"time stop {ampm(config.tips.intraday_time_stop)} · day check {ampm('15:35')}", dim=True)]
    # watch · live
    w = _safe(_near_rows, [], config, now, st, True)
    n_near = len(w)
    w += _safe(_tips_dist_rows, [], config, now)
    mk = _safe(_market_now_rows, [], config, now)
    t2 = tile("watch", "Watch · live", f"{n_near} near a level" if n_near else "nothing near a level", w + mk, EMPTY_WATCH,
              lambda: _last_watch(config, now, st))
    if not w and mk:                      # the live list is empty: the last session's held names under the market line
        lw = _safe(_last_watch, None, config, now, st)
        if lw and lw[1]:
            t2.update(rows=lw[1] + mk, meta=f"last session · {_day_label(lw[0])}", last=True)
    # books · live
    t3 = _books_tile(config, now, day, "Books · live", live=True)
    sp = spark(_safe(_curve, []), day)
    if sp and t3["rows"] and (t3["rows"][0].get("b") == "Real"):
        t3["rows"].insert(1, sp)
    tips_open = [r for r in tips if r.get("_open")]
    bits = [f"orders done: {n_sent} sent · {n_fill} filled" if log else "no orders sent today"]
    for r in tips_open[:1]:
        bits.append(f"Tips {r['b'].replace('💡 ', '')} open {r['v'][0]['t']}")
    if n_near:
        bits.append(f"{n_near} name{'s' if n_near != 1 else ''} near a level")
    for r in tips:
        r.pop("_open", None)
    return {"tiles": [t1, t2, t3], "one": " · ".join(bits),
            "buttons": [{"label": "Open positions", "kind": "go", "arg": "portfolio/portfolio"}, {"label": "Tips", "kind": "go", "arg": "research/tips"}],
            "footer": "3:30 PM close · 3:40 PM summary · nothing here acts on its own"}


def _tips_live_rows(config: AppConfig, now: datetime) -> list:
    """Every Tips hint armed / open on Kite with its live P&L (the Home rows: stored quotes only)."""
    from backend.tips import home as th
    if not _tips_on(config):
        return []
    d = th.home_tips(config, now)
    out = []
    for r in d.get("rows") or []:
        entered = r["state"] == "entered"
        v = [(_inr(r["pnl"], True), _tone(r["pnl"]))] if entered and r.get("pnl") is not None else []
        v.append((r.get("tag") or r["state"], "" if entered else "dim"))
        x = row(f" · {r.get('name') or r['title']}", v, r.get("detail") or r.get("facts"), b=f"Tips #{r['id']}", icon="💡")
        x["_open"] = entered
        out.append(x)
    return out


def _near_rows(config: AppConfig, now: datetime, st: dict, live: bool) -> list:
    """Held names within NEAR_PCT of the stop (red) or with a target (green), nearest first."""
    odds = _safe(held_odds, None, config, now) or {}
    out = []
    for p in sorted(_positions(st, live), key=lambda p: (p["stop_pct"] if p["stop_pct"] is not None else 99)):
        nb = (odds.get("by") or {}).get(p["symbol"]) or {}
        sf = f" · candle model stop-first {nb['stop_first']} % ({odds.get('mode')})" if nb.get("stop_first") is not None else ""
        if p["stop_pct"] is not None and p["stop_pct"] <= NEAR_PCT:
            out.append(row("", [(_px(p["price"]), ""), (f"stop {_px(p['stop'])} is {abs(p['stop_pct']):.1f} % away", "down")],
                           f"the stop check sends EXIT below {_px(p['stop'])}{sf}", b=p["symbol"]))
        elif p["target_pct"] is not None:
            out.append(row("", [(_px(p["price"]), ""), (f"target {_px(p['target'])} {_pct(p['target_pct'])}", "up")],
                           f"stop {_px(p['stop'])} is {p['stop_pct']:.1f} % away" if p["stop_pct"] is not None else None, b=p["symbol"]))
    return out


def _last_watch(config: AppConfig, now: datetime, st: dict) -> Optional[tuple]:
    """The held names at the last close, nearest the stop first (the watch tile's last-session rows)."""
    ps = sorted(_positions(st, live=False), key=lambda p: (p["stop_pct"] if p["stop_pct"] is not None else 99))
    if not ps:
        return None
    rows = []
    for p in ps[:3]:
        rows.append(row("", [(_px(p["price"]), ""), (f"stop {_px(p['stop'])} is {p['stop_pct']:.1f} % away" if p["stop_pct"] is not None else "no stop",
                                                       "down" if (p["stop_pct"] or 99) <= NEAR_PCT else "")], None, b=p["symbol"], dim=True))
    with get_db() as conn:
        d = conn.execute("SELECT MAX(day) FROM real_equity WHERE day < ?", (now.date().isoformat(),)).fetchone()[0]
    return d or prev_session(now.date().isoformat()), rows


def _tips_dist_rows(config: AppConfig, now: datetime) -> list:
    """Every open Tips position's next target and stop by distance (the card's own distance bar)."""
    from backend.tips import book as bk
    from backend.tips import card as cd
    if not _tips_on(config):
        return []
    out = []
    for h in bk.rows("json_extract(kite, '$.state') = 'entered'", order="id"):
        bar = cd.dist(h)
        if not bar:
            continue
        s, t = bar["stop"], bar["target"]
        out.append(row(f" {t.get('name') or 'target'} {_px(t['px'])}", [(_px(bar.get("dot_px")), ""), (f"{t['pct_text']} to go", "up")],
                       f"stop {_px(s['px'])} is {s['pct_text']}" + (f" · {s['note']}" if s.get("note") and s["note"] != "away" else ""),
                       b=f"#{h['id']}", icon="💡"))
    return out


def _market_now_rows(config: AppConfig, now: datetime) -> list:
    """Market now · NIFTY from the stored candles (code tiles) and the latest stored backdrop line ([AI])."""
    from backend.market import reads as mr
    from backend.market import view as vw
    from backend.market import words as mw
    out = []
    f = vw.index_facts("NIFTY", now)
    if f:
        tr, lv = f["trend"], f["levels"]
        vx = mr.vix() or {}
        g = mr.gate(config)
        sub = " · ".join(x for x in (f"R {vw.fmt(lv.get('resistance'))}", f"S {vw.fmt(lv.get('support'))}",
                                     f"VIX {vx['last']:.1f}" if vx.get("last") else "") if x)
        out.append(row("Market now · NIFTY", [(vw.trend_head(tr), "up" if tr["side"] == "up" else "down" if tr["side"] == "down" else "")], sub,
                       tag=(f"model {g['mode']}", "w")))
    v = mw.panel_view("NIFTY")
    if v.get("state") == "done" and v.get("lines") and str(v.get("at") or "").startswith(now.date().isoformat()):
        out.append(row(v["lines"][0].get("text") or "", None, ai=True))
    return out


# --- the payload -----------------------------------------------------------------------------------------------------------

CHIP = {"evening": "evening", "premarket": "pre-market", "market": "market hours"}


def panel(config: AppConfig, now: datetime, trading: Optional[bool] = None) -> dict:
    if trading is None:
        trading = _trading(now.date())
    ph = panel_phase(now, config, trading)
    body = {"evening": _evening, "premarket": _premarket, "market": _market}[ph]
    out = body(config, now, trading) if ph == "evening" else body(config, now)
    chip = CHIP[ph]
    if ph == "market":
        chip += f" · {ampm(now)}"
    if not trading:
        chip = f"holiday · next session {date.fromisoformat(next_session(now.date().isoformat())):%a}"
    return {"phase": ph, "chip": chip, "step": _step(ph, now, config, trading), **out}
