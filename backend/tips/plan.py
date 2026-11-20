"""💡 The plan of a hint and the rules that run it, by code, on quotes only:

  entry   "when comes" zone / at: wait until the premium is in the zone (a buy fills at or under the top, a sell at or
          over the bottom); "above x" / "below x": a trigger; otherwise at the market on the first quote. A waiting hint
          that never fills by the time stop is "no fill" (skipped), unless the channel later says enter / active / cmp.
  stop    a premium stop on the option's own quote, or a spot stop on 15-minute closes of the underlying ("stop: spot
          43,450 (15-min close)"); no stop at all = "stop missing", no plan (the card says so).
  targets half at T1 (partial_at_t1), the stop to cost (trail_to_cost_after_t1), the rest at T2; with "trail" on (the
          channel says trail, or you trail) the rest rides target to target with the stop one target behind.
  time    option buys: the intraday time stop (tips.intraday_time_stop, 3:14 PM; MCX 11:20 PM). Option sells with
          "Target 0" are held to expiry unless the stop / an exit says otherwise.
  after hours  a hint posted after the close (or on a holiday) is planned for the next open.
`step` is one quote's worth of that machine; the record replays stored quotes through the same function."""

import re
from datetime import date, datetime, time, timedelta
from typing import Optional

from backend.config import IST
from backend.ingest.holidays import is_trading_day
from backend.timefmt import ampm

NSE_OPEN, NSE_CLOSE = time(9, 15), time(15, 30)
MCX_OPEN, MCX_CLOSE, MCX_TIME_STOP = time(9, 0), time(23, 30), "23:20"
ZONE_SLACK = 0.0                     # a "when comes" zone fills exactly at its edge (the quote decides)


def hm(s: str) -> time:
    h, m = str(s).split(":")[:2]
    return time(int(h), int(m))


def fmt(v) -> str:
    """320 / 46.2 / 43,450 (no trailing .0)."""
    if v is None:
        return "—"
    f = float(v)
    return f"{f:,.0f}" if f.is_integer() else f"{f:,.2f}".rstrip("0").rstrip(".")


def session(exchange: Optional[str]) -> tuple[time, time]:
    return (MCX_OPEN, MCX_CLOSE) if exchange == "MCX" else (NSE_OPEN, NSE_CLOSE)


def trading_day(d: date, exchange: Optional[str] = None) -> bool:
    return d.weekday() < 5 if exchange == "MCX" else is_trading_day(d)


def plan_day(posted: datetime, exchange: Optional[str] = None) -> tuple[date, bool]:
    """(the day the plan runs, queued for the next open?): the posting day when it is a trading day and the post came
    before the close, else the next trading day."""
    _, close = session(exchange)
    d = posted.date()
    if trading_day(d, exchange) and posted.time() < close:
        return d, False
    d += timedelta(days=1)
    while not trading_day(d, exchange):
        d += timedelta(days=1)
    return d, True


def is_spot(e: Optional[dict]) -> bool:
    """An entry level on the index / underlying (not the option's own price)."""
    return (e or {}).get("level_kind") == "spot" and (e or {}).get("mode") not in (None, "market")


def entry_text(e: dict) -> str:
    """"above 370" (the option's premium) / "SENSEX above 82,400" (the index level) / "near 95–100" / "at market"."""
    e = e or {}
    m = e.get("mode") or "market"
    wc = " when comes" if e.get("when_comes") and m in ("at", "zone") else ""
    pre = f"{e.get('index') or 'spot'} " if is_spot(e) else ""
    if m == "zone":
        return f"{pre}near {fmt(e['lo'])}–{fmt(e['hi'])}{wc}"
    if m == "at":
        return f"{pre}at {fmt(e['lo'])}{wc}"
    if m in ("above", "below"):
        return f"{pre}{m} {fmt(e['lo'])}"
    return "at market"


def stop_text(s: dict) -> str:
    k = (s or {}).get("kind")
    if k == "premium":
        return f"SL {fmt(s['value'])}"
    if k == "spot":
        return f"stop: spot {fmt(s['value'])} ({s.get('tf') or 15}-min close {s.get('dir') or ''})".replace(" )", ")")
    return "stop missing"


def targets_text(tg: list, open_ended: bool) -> str:
    if not tg:
        return "no target"
    return "Tgt " + " / ".join(fmt(t) for t in tg) + ("++" if open_ended else "")


def build(h: dict, cfg, posted: datetime) -> dict:
    """The plan of a decoded hint: {ok, why, side, entry, wait, stop, targets, partial_at_t1, trail_to_cost, time_stop,
    hold_to_expiry, day, queued, expiry, text, lines}. ok=False ("stop missing" / "no side") still tracks the record."""
    side = h.get("side")
    e = h.get("entry") or {"mode": "market"}
    st = h.get("stop") or {"kind": "none", "value": None}
    tg = [t for t in (h.get("targets") or []) if t is not None]
    exch = h.get("exchange")
    day, queued = plan_day(posted, exch)
    sell_to_zero = side == "SELL" and tg and tg[-1] == 0
    hold = bool(sell_to_zero)
    ts = None if hold else (MCX_TIME_STOP if exch == "MCX" else cfg.intraday_time_stop)
    wait = e.get("mode") in ("above", "below") or (e.get("mode") in ("at", "zone") and bool(e.get("when_comes"))) or is_spot(e)
    needs_act = bool(e.get("needs_activation"))
    ok, why = True, None
    if st.get("kind") == "none" or st.get("value") is None:
        ok, why = False, "stop missing"
    if not side:
        ok, why = False, "side missing"
    partial = bool(cfg.partial_at_t1) and len(tg) > 1 and not hold
    lines = []
    if queued:
        lines.append(f"for the next open ({day:%a %d %b})")
    if needs_act:
        lines.append("wait for the channel's activation" + (f" · then {entry_text(e)}" if e.get("mode") in ("at", "zone") else ""))
    elif is_spot(e):
        lines.append(f"wait for {entry_text(e)} (index)")
    else:
        lines.append("wait for the zone" if wait and e.get("mode") in ("at", "zone") else
                     f"wait for a trade {e['mode']} {fmt(e['lo'])}" if wait else "enter at the market")
    if e.get("wait_words") and not needs_act:
        lines.append("activation words · the level decides")
    if hold:
        lines.append("target 0 · hold to expiry unless the stop or an exit comes")
    elif tg:
        if partial:
            lines.append(f"half out at {fmt(tg[0])}" + (", SL to cost" if cfg.trail_to_cost_after_t1 else "") +
                         f", rest to {fmt(tg[1])} / trail")
        else:
            lines.append(f"out at {fmt(tg[0])}")
    if ts:
        lines.append(f"time stop {ampm(ts)}")
    if wait:
        lines.append("skipped if the zone never comes" if e.get("mode") in ("at", "zone") else
                     f"skipped at {ampm(ts)} if it never comes" if ts else "skipped if it never comes")
    if not ok:
        lines.append(f"no plan: {why}")
    return {"ok": ok, "why": why, "side": side, "entry": e, "wait": wait, "stop": st, "targets": tg,
            "open_ended": bool(h.get("open_ended")), "partial_at_t1": partial, "trail_to_cost": bool(cfg.trail_to_cost_after_t1),
            "time_stop": ts, "hold_to_expiry": hold, "day": day.isoformat(), "queued": queued, "expiry": h.get("expiry"),
            "exchange": exch, "opt_type": h.get("opt_type"), "needs_activation": needs_act,
            "level_kind": e.get("level_kind") if e.get("mode") not in (None, "market") else None, "entry_text": entry_text(e), "stop_text": stop_text(st), "targets_text": targets_text(tg, bool(h.get("open_ended"))),
            "text": " · ".join(lines), "lines": lines}


# --- the machine ----------------------------------------------------------------------------------------------------

def new_run(plan: dict) -> dict:
    """A fresh path through the plan: waiting until the entry fills."""
    st = plan.get("stop") or {}
    return {"state": "waiting", "entry_px": None, "entry_t": None, "stop": st.get("value") if st.get("kind") == "premium" else None,
            "spot_stop": dict(st) if st.get("kind") == "spot" else None, "stop_moved": False, "open_frac": 1.0, "hits": [],
            "best": None, "worst": None, "end": None, "exit_px": None, "exit_t": None, "pnl_pts": 0.0, "allowed": False,
            "paused": False, "trail": False, "last_ltp": None, "first": None, "first_t": None}


def _sign(plan: dict) -> int:
    return 1 if plan.get("side") != "SELL" else -1


def spot_hit(e: dict, side: Optional[str], opt_type: Optional[str], spot: Optional[float]) -> bool:
    """An index-level entry crossed by the index quote: above / below as written; at / zone in the option's favour (a
    CE buy / PE sell when the index is at or under the top, a PE buy / CE sell at or over the bottom)."""
    if spot is None:
        return False
    m = e.get("mode")
    if m == "above":
        return spot >= e["lo"]
    if m == "below":
        return spot <= e["lo"]
    up = (side != "SELL") == (opt_type != "PE")          # the trade gains when the index rises
    return spot <= e["hi"] if up else spot >= e["lo"]


def entry_hit(plan: dict, run: dict, ltp: float, spot: Optional[float] = None) -> bool:
    """Does this quote fill the entry? (market / not "when comes": the first quote; zone / at when comes: inside or
    through the zone in the trade's favour; above / below: the trigger; an index level: the index quote `spot`.)
    Activation words never gate an entry that has a level: only an at / zone / market entry with wait words waits for
    the channel's "active" (run.allowed)."""
    if run.get("allowed"):
        return True
    e = plan.get("entry") or {}
    m = e.get("mode") or "market"
    if is_spot(e):
        return spot_hit(e, plan.get("side"), plan.get("opt_type"), spot)
    if e.get("needs_activation"):
        return False
    if m == "market" or (m in ("at", "zone") and not e.get("when_comes")):
        return True
    if m == "above":
        return ltp >= e["lo"]
    if m == "below":
        return ltp <= e["lo"]
    if plan.get("side") == "SELL":
        return ltp >= e["lo"] * (1 - ZONE_SLACK)
    return ltp <= e["hi"] * (1 + ZONE_SLACK)


def _close(run: dict, plan: dict, frac: float, px: float, now: datetime, kind: str) -> dict:
    frac = min(frac, run["open_frac"])
    run["pnl_pts"] = round(run["pnl_pts"] + frac * (px - run["entry_px"]) * _sign(plan), 4)
    run["open_frac"] = round(run["open_frac"] - frac, 6)
    if run["first"] is None and kind in ("t1", "stop", "trail", "time", "expired", "channel_exit", "owner_exit"):
        run["first"], run["first_t"] = kind, now.isoformat(timespec="seconds")
    if run["open_frac"] <= 1e-9:
        run.update(state="closed", end=kind, exit_px=px, exit_t=now.isoformat(timespec="seconds"))
    return {"kind": kind, "px": px, "frac": frac}


def close_all(run: dict, plan: dict, px: Optional[float], now: datetime, kind: str) -> list[dict]:
    """Close what is open (a waiting run ends as `kind` without a fill)."""
    if run["state"] == "closed":
        return []
    if run["state"] == "waiting" or px is None:
        if run["state"] == "active" and px is None:
            px = run.get("last_ltp") or run["entry_px"]
        else:
            run.update(state="closed", end=kind if kind != "time" else "no_fill", exit_t=now.isoformat(timespec="seconds"))
            return [{"kind": run["end"], "px": None, "frac": 0.0}]
    return [_close(run, plan, run["open_frac"], px, now, kind)]


def spot_breached(stop: dict, spot15: Optional[float]) -> bool:
    if not stop or spot15 is None:
        return False
    return spot15 > stop["value"] if stop.get("dir") == "above" else spot15 < stop["value"]


def step(run: dict, plan: dict, ltp: Optional[float], now: datetime, spot15: Optional[float] = None,
         spot: Optional[float] = None) -> list[dict]:
    """One quote through the plan. Returns the events: fill, t1, t2, ..., stop, trail, time, expired, no_fill (each
    {kind, px, frac}); `run` is updated in place. `spot` is the index quote (an index-level entry fills on its cross,
    at the option's LTP at that moment)."""
    ev: list[dict] = []
    if run["state"] == "closed":
        return ev
    day = date.fromisoformat(plan["day"])
    _, close_t = session(plan.get("exchange"))
    expiry = date.fromisoformat(plan["expiry"]) if plan.get("expiry") else None
    if now.date() < day:
        return ev
    ts = hm(plan["time_stop"]) if plan.get("time_stop") else None
    if ltp is not None:
        run["last_ltp"] = ltp
    if run["state"] == "waiting":
        if expiry and now.date() > expiry:
            return close_all(run, plan, None, now, "expired")
        past_day = now.date() > day
        if past_day or (ts and now.time() >= ts) or (not ts and now.time() >= close_t and expiry and now.date() >= expiry):
            return close_all(run, plan, None, now, "no_fill")
        if ltp is None or run.get("paused") or not (session(plan.get("exchange"))[0] <= now.time() < close_t):
            return ev
        if not entry_hit(plan, run, ltp, spot):
            return ev
        run.update(state="active", entry_px=ltp, entry_t=now.isoformat(timespec="seconds"), best=0.0, worst=0.0)
        ev.append({"kind": "fill", "px": ltp, "frac": 0.0})
        return ev                                       # exits from the next quote on
    # active
    sign = _sign(plan)
    if ltp is not None:
        move = (ltp - run["entry_px"]) * sign
        run["best"] = max(run["best"] or 0.0, move)
        run["worst"] = min(run["worst"] or 0.0, move)
    if expiry and (now.date() > expiry or (now.date() == expiry and now.time() >= close_t)):
        return close_all(run, plan, ltp, now, "expired")
    if ltp is not None:
        stopk = None
        if run.get("stop") is not None and (ltp <= run["stop"] if sign > 0 else ltp >= run["stop"]):
            stopk = "trail" if run.get("stop_moved") else "stop"
        elif run.get("spot_stop") and spot_breached(run["spot_stop"], spot15):
            stopk = "stop"
        if stopk:
            return close_all(run, plan, ltp, now, stopk)
        tg = plan.get("targets") or []
        while run["state"] == "active" and len(run["hits"]) < len(tg):
            k = len(run["hits"])
            t = tg[k]
            if not (ltp >= t if sign > 0 else ltp <= max(t, 0.05)):
                break
            run["hits"].append({"k": k + 1, "t": now.isoformat(timespec="seconds"), "px": ltp})
            kind = f"t{k + 1}"
            last = k + 1 >= len(tg)
            if k == 0 and plan.get("partial_at_t1") and not last:
                ev.append(_close(run, plan, 0.5, ltp, now, kind))
                if plan.get("trail_to_cost"):
                    run["stop"] = max(run["stop"] or 0, run["entry_px"]) if sign > 0 else min(run["stop"] or 1e12, run["entry_px"])
                    run["spot_stop"], run["stop_moved"] = None, True
            elif last or not run.get("trail"):
                ev.append(_close(run, plan, run["open_frac"], ltp, now, kind))
            else:                                       # trail: the rest rides, the stop one target behind
                prev = tg[k - 1] if k >= 1 else run["entry_px"]
                run["stop"] = max(run["stop"] or 0, prev) if sign > 0 else min(run["stop"] or 1e12, prev)
                run["spot_stop"], run["stop_moved"] = None, True
                ev.append({"kind": kind, "px": ltp, "frac": 0.0})
                if run["first"] is None:
                    run["first"], run["first_t"] = kind, now.isoformat(timespec="seconds")
        if run["state"] == "closed":
            return ev
    if ts and now.time() >= ts and now.date() >= day:
        ev += close_all(run, plan, ltp, now, "time")
    return ev


def result(run: dict, plan: dict, lot_size: Optional[int]) -> dict:
    """The record of one path: entry reached, the first of T1 / stop / time, minutes to it, best / worst move (points),
    P&L per lot at the plan, R (risk = entry to the premium stop)."""
    risk = abs(run["entry_px"] - plan["stop"]["value"]) if run.get("entry_px") is not None and (plan.get("stop") or {}).get("kind") == "premium" else None
    mins = None
    if run.get("first_t") and run.get("entry_t"):
        mins = round((datetime.fromisoformat(run["first_t"]) - datetime.fromisoformat(run["entry_t"])).total_seconds() / 60)
    pts = run.get("pnl_pts") or 0.0
    first = run.get("first")
    if first and first.startswith("t") and first[1:].isdigit():
        first = "t1"
    return {"entry_reached": run.get("entry_px") is not None, "entry_px": run.get("entry_px"), "entry_t": run.get("entry_t"),
            "first": first if run.get("entry_px") is not None else ("no_fill" if run["state"] == "closed" else None),
            "minutes_to_first": mins, "mfe": run.get("best"), "mae": run.get("worst"), "end": run.get("end"),
            "exit_px": run.get("exit_px"), "pnl_pts": round(pts, 2), "pnl_lot": round(pts * lot_size, 2) if lot_size else None,
            "r": round(pts / risk, 2) if risk else None, "open": run["state"] != "closed"}


def spot_close15(samples: list[tuple[str, Optional[float]]], now: datetime) -> Optional[float]:
    """The last completed 15-minute close of the underlying from (t, spot) samples: the last spot inside the newest
    15-minute bucket that has ended by `now`."""
    edge = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    best = None
    for t, s in samples:
        if s is None:
            continue
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        if dt < edge:
            best = s
    return best


# --- the channel's follow-ups ------------------------------------------------------------------------------------------

def followup_action(text: str) -> dict:
    """What a follow-up says, by code: {action: exit|sl_hit|stop|trail|partial|add|active|avoid|targets|price|note, n,
    value, spot, cost}. "add more near 440" is an add-on (never averaged); bare numbers are price notes. Words only: the channel's "target done" never sets a price or a P&L."""
    low = (text or "").lower()
    if re.search(r"\b(t?sl|stop\s*loss|stoploss)\s*(hit|triggered|done|gone)\b", low):
        return {"action": "sl_hit"}
    if re.search(r"^\W*add\b|\badd\s+(?:more|\d+\s*(?:-\s*\d+\s*)?lots?)\b", low):
        return {"action": "add"}                        # "add more near 440" / "add 5 lots at 560": never averaged
    m = re.search(r"\b(?:rest|revise|revised|new)\s*(?:the\s*)?(?:t?sl|stop\s*loss|stoploss)\s*(?:to|at|now|is)?\s*(\d+(?:\.\d+)?)"
                  r"|\b(?:t?sl|stop\s*loss|stoploss)\s*(?:now|is\s*now)\s*(?:at|to)?\s*(\d+(?:\.\d+)?)", low)
    if m:
        v = float(m.group(1) or m.group(2))
        return {"action": "stop", "value": int(v) if v.is_integer() else v, "spot": "spot" in low}
    m = re.search(r"\b(?:modify|modified|change|changed|move|moved|revise|revised|trail)\s*(?:the\s*)?(?:sl|stop\s*loss)\s*(?:to|at)?\s*(\d+(?:\.\d+)?)"
                  r"|\b(?:sl|stop\s*loss)\s*(?:modified|changed|moved|revised)\s*(?:to|at)?\s*(\d+(?:\.\d+)?)", low)
    if m:
        v = float(m.group(1) or m.group(2))
        return {"action": "stop", "value": int(v) if v.is_integer() else v, "spot": "spot" in low}
    if re.search(r"\b(exit|exited|close (it|all)|book (all|full|profits?)|square\s*off|cost to cost|exit near cost)\b", low):
        return {"action": "exit"}
    m = re.search(r"\b(\d|all)\s*(?:targets?|tgts?)\s*(?:done|hit|achieved)|\b(?:target|tgt)\s*(\d)\s*(?:done|hit|achieved)|hit\s*tgt\s*(\d)", low)
    if m:
        n = m.group(1) or m.group(2) or m.group(3)
        return {"action": "targets", "n": None if n == "all" else int(n)}
    if re.search(r"\b(trail|trailing)\b", low):
        return {"action": "trail", "cost": bool(re.search(r"\bcost\b", low))}
    if re.search(r"\b(part(ly|ial)?\s*book|book\s*(part|partly|partial|half)|half\s*book)", low):
        return {"action": "partial", "cost": bool(re.search(r"\bcost\b", low))}
    if re.search(r"\b(t?sl|stop\s*loss|stoploss)\s*(?:to|at)\s*cost\b", low):
        return {"action": "trail", "cost": True}
    if re.search(r"\b(avoid|don'?t (enter|take)|not now|cancel)\b", low) or re.fullmatch(r"\s*wait\W*", low):
        return {"action": "avoid"}
    if re.search(r"\b(active|activated|triggered|start taking|enter now|entry done|take it)\b", low) or re.search(r"\bcmp\b", low):
        m = re.search(r"\bcmp\b\D{0,20}(\d+(?:\.\d+)?)", low)
        return {"action": "active", "value": float(m.group(1)) if m else None}
    if re.search(r"\d", low):
        return {"action": "price"}
    return {"action": "note"}
