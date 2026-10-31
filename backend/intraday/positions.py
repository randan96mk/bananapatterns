"""📝 Paper intraday positions (phase 2): filled at the next 1-minute open with slippage and Zerodha intraday costs,
then managed on every closed 1-minute bar: stop, target, partial booking, trailing, time stop, square-off.
A bar touching both the stop and the target counts as the stop (conservative). Long and short. Never orders."""

import json
from typing import Optional

from backend.database.models import get_db, now_ist

SLIPPAGE_PCT = 0.05


def intraday_charges(buy_value: float, sell_value: float) -> float:
    """Zerodha equity intraday: brokerage min(₹20, 0.03%) an order, STT 0.025% on the sell, NSE 0.00297%,
    SEBI ₹10 / crore, GST 18% on (brokerage + exchange + SEBI), stamp 0.003% on the buy."""
    brk = sum(min(20.0, v * 0.0003) for v in (buy_value, sell_value) if v)
    exch = (buy_value + sell_value) * 0.0000297
    sebi = (buy_value + sell_value) * 10 / 1e7
    return brk + sell_value * 0.00025 + exch + sebi + 0.18 * (brk + exch + sebi) + buy_value * 0.00003


def _mins(t: str) -> int:
    return int(t[:2]) * 60 + int(t[3:5])


def _save(p: dict) -> None:
    with get_db() as conn:
        if p.get("id"):
            conn.execute("UPDATE flow_positions SET status = ?, payload = ? WHERE id = ?", (p["status"], json.dumps(p), p["id"]))
        else:
            cur = conn.execute("INSERT INTO flow_positions (day, symbol, status, payload) VALUES (?, ?, ?, ?)",
                               (p["day"], p["symbol"], p["status"], json.dumps(p)))
            p["id"] = cur.lastrowid
            conn.execute("UPDATE flow_positions SET payload = ? WHERE id = ?", (json.dumps(p), p["id"]))


def load(day: str, status: Optional[tuple] = None) -> list[dict]:
    q, args = "SELECT payload FROM flow_positions WHERE day = ?", [day]
    if status:
        q += f" AND status IN ({','.join('?' * len(status))})"
        args += list(status)
    with get_db() as conn:
        return [json.loads(r[0]) for r in conn.execute(q + " ORDER BY id", args)]


def open_new(day: str, it: dict, signal_t: str, plan: dict) -> dict:
    """A pending paper position: fills at the open of the first 1-minute bar after the signal candle."""
    p = {"day": day, "symbol": it["symbol"], "token": it["token"], "side": it["side"], "qty": it["qty"],
         "signal_t": signal_t, "status": "pending", "plan": plan, "signal_price": it["entry"],
         "stop0": it["sl"], "target": it.get("target"), "created": now_ist()}
    _save(p)
    return p


def _slip(px: float, buy: bool) -> float:
    return px * (1 + SLIPPAGE_PCT / 100) if buy else px * (1 - SLIPPAGE_PCT / 100)


def _exit(p: dict, px: float, t: str, why: str, qty: Optional[int] = None) -> dict:
    """Closes qty (default all) at px (before slippage); books gross / costs / net; returns the event."""
    long = p["side"] == "long"
    q = qty or p["open_qty"]
    fill = _slip(px, buy=not long)
    gross = (fill - p["entry"]) * q if long else (p["entry"] - fill) * q
    buy_v, sell_v = (p["entry"] * q, fill * q) if long else (fill * q, p["entry"] * q)
    costs = intraday_charges(buy_v, sell_v)
    ev = {"t": t, "qty": q, "price": round(fill, 2), "why": why, "gross": round(gross, 2), "costs": round(costs, 2),
          "net": round(gross - costs, 2)}
    p.setdefault("exits", []).append(ev)
    p["open_qty"] -= q
    if p["open_qty"] <= 0:
        p.update(status="closed", exit_t=t, reason=why, gross=round(sum(e["gross"] for e in p["exits"]), 2),
                 costs=round(sum(e["costs"] for e in p["exits"]), 2), net=round(sum(e["net"] for e in p["exits"]), 2))
        p["r"] = round(p["net"] / p["risk"], 2) if p.get("risk") else None
    return ev


def step(p: dict, bar: dict, prev: Optional[dict], plan: dict) -> list[dict]:
    """One closed 1-minute bar for a position; returns the events (fill / exits)."""
    events = []
    long = p["side"] == "long"
    if p["status"] == "pending":
        if bar["t"] <= p["signal_t"]:
            return events
        p.update(status="open", entry=round(_slip(bar["o"], buy=long), 2), entry_t=bar["t"], open_qty=p["qty"],
                 stop=p["stop0"], best=bar["o"], partial_done=False, bars=0)
        p["risk"] = round(abs(p["entry"] - p["stop"]) * p["qty"], 2)
        events.append({"t": bar["t"], "fill": p["entry"], "qty": p["qty"]})
    if p["status"] != "open":
        return events
    p["bars"] += 1
    r1 = abs(p["entry"] - p["stop0"])
    hit_stop = bar["l"] <= p["stop"] if long else bar["h"] >= p["stop"]
    tgt = p.get("target")
    hit_tgt = tgt is not None and (bar["h"] >= tgt if long else bar["l"] <= tgt)
    if hit_stop:                                              # both touched: the stop counts
        px = min(bar["o"], p["stop"]) if long else max(bar["o"], p["stop"])
        events.append(_exit(p, px, bar["t"], "stop" if p["stop"] == p["stop0"] else "trailing stop"))
        return events
    if plan.get("partial_on") and not p["partial_done"]:
        lvl = p["entry"] + plan["partial_r"] * r1 if long else p["entry"] - plan["partial_r"] * r1
        if (bar["h"] >= lvl if long else bar["l"] <= lvl):
            q = int(p["qty"] * plan["partial_pct"] / 100)
            if 0 < q < p["open_qty"]:
                events.append(_exit(p, lvl, bar["t"], f"partial {plan['partial_r']}R", q))
            p["partial_done"] = True
    if hit_tgt:
        events.append(_exit(p, tgt, bar["t"], "target"))
        return events
    if plan.get("time_stop_min") and p["bars"] >= plan["time_stop_min"]:
        moved = (bar["c"] - p["entry"]) if long else (p["entry"] - bar["c"])
        if moved < plan.get("time_stop_r", 0.5) * r1:
            events.append(_exit(p, bar["c"], bar["t"], "time stop"))
            return events
    if bar["t"] >= plan.get("squareoff", "15:15"):
        events.append(_exit(p, bar["c"], bar["t"], "square-off"))
        return events
    p["best"] = max(p["best"], bar["h"]) if long else min(p["best"], bar["l"])
    _trail(p, bar, prev, plan, r1)
    return events


def _trail(p: dict, bar: dict, prev: Optional[dict], plan: dict, r1: float) -> None:
    """Moves the stop (never back): breakeven after N R, then the chosen trail."""
    long = p["side"] == "long"
    gain = (p["best"] - p["entry"]) if long else (p["entry"] - p["best"])
    if gain < plan.get("trail_after_r", 1) * r1:
        return
    mode = plan.get("trail", "off")
    new = None
    if mode == "breakeven":
        new = p["entry"]
    elif mode == "prev_bar" and prev:
        new = prev["l"] if long else prev["h"]
    elif mode == "atr" and plan.get("atr"):
        new = p["best"] - plan["trail_atr_mult"] * plan["atr"] if long else p["best"] + plan["trail_atr_mult"] * plan["atr"]
    elif mode == "percent":
        k = plan.get("trail_pct", 0.5) / 100
        new = p["best"] * (1 - k) if long else p["best"] * (1 + k)
    if mode != "off" and plan.get("breakeven_first", True):
        new = max(new or p["entry"], p["entry"]) if long else min(new or p["entry"], p["entry"])
    if new is not None:
        p["stop"] = round(max(p["stop"], new), 2) if long else round(min(p["stop"], new), 2)


def manage(day: str, bars_of, now_t: str) -> list[dict]:
    """Every pending / open position over its new closed 1-minute bars (bars_of(token) -> today's bars).
    Returns [(position, event)] for alerts."""
    out = []
    for p in load(day, ("pending", "open")):
        bars = [b for b in bars_of(p["token"]) if b["t"] < now_t]
        last = p.get("last_t", "")
        prev = None
        for b in bars:
            if b["t"] <= last:
                prev = b
                continue
            for ev in step(p, b, prev, p["plan"]):
                out.append((p, ev))
            p["last_t"] = b["t"]
            prev = b
            if p["status"] == "closed":
                break
        if bars:
            p["last_price"] = bars[-1]["c"]
        _save(p)
    return out


def square_off_all(day: str, price_of, t: str, why: str = "stopped") -> list:
    """⏹ Stop intraday: every open position out at its last price, pending ones cancelled. Returns [(position, event)]."""
    out = []
    for p in load(day, ("pending", "open")):
        if p["status"] == "pending":
            p.update(status="cancelled", reason=why)
        else:
            px = price_of(p["token"]) or p.get("last_price") or p["entry"]
            out.append((p, _exit(p, px, t, why)))
        _save(p)
    return out


def day_stats(day: str) -> dict:
    ps = [p for p in load(day) if p["status"] != "cancelled"]
    closed = [p for p in ps if p["status"] == "closed"]
    open_ = [p for p in ps if p["status"] in ("pending", "open")]
    realised = sum(p["net"] for p in closed)
    partial = sum(e["net"] for p in open_ for e in p.get("exits", []))
    unreal = 0.0
    for p in open_:
        if p["status"] == "open" and p.get("last_price"):
            d = (p["last_price"] - p["entry"]) if p["side"] == "long" else (p["entry"] - p["last_price"])
            unreal += d * p["open_qty"]
    risk = sum(abs(p.get("entry", p["signal_price"]) - p.get("stop", p["stop0"])) * p.get("open_qty", p["qty"]) for p in open_)
    return {"trades": len(ps), "open": len(open_), "closed": len(closed), "won": sum(p["net"] > 0 for p in closed),
            "realised": round(realised + partial, 2), "unrealised": round(unreal, 2), "at_risk": round(risk, 2)}
