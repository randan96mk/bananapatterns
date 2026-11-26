"""📏 Tips sizing: how many lots a hint gets, by code, and the per-trade risk guard. Used by the Kite orders (click,
auto, the dry run), the paper book and the card's size cell; nothing here places, waits on the AI or reads a quote.

  modes   tips.kite_size_mode  post (default: the post's size wins, else tips.lots) · fixed (always tips.lots; the
          post's size is shown and ignored) · risk (lots = floor(kite_risk_per_trade ÷ ((entry − stop) × lot size)), 1 at
          least). kite_max_lots caps the risk mode here; in post / fixed a size over the cap is refused by the guard
          (click) or capped (auto), as before.
  entry   the price the risk is measured from: the plan's entry level (a zone's top for a buy, its bottom for a sell;
          the level for above / below / at), the last price for a market entry (or an index-level entry).
  guard   (entry − stop) × qty (a sell: stop − entry) over kite_risk_per_trade → the entry is held: never armed, never
          placed (auto: the guard text; click: the Place buttons off with the reason; "Take n lot…" places the largest
          size under the guard, through its confirm).
  check   check_settings (kite_mode not off): lots × lot size × the median stop distance of the last 30 days' calls per
          index (else a fixed table) must fit kite_risk_per_trade and kite_loss_cap, or the save is refused with the
          arithmetic.
  mult    tips.kite_lot_multiplier (1–5): post / fixed / risk_fallback lots × mult (the risk mode's own figure is never
          multiplied: the rupee risk decides). Lots you type are literal. kite_max_lots and the guard apply after."""

import math
from datetime import datetime, timedelta
from statistics import median
from typing import Optional

from backend.tips import plan as pl

STOP_TABLE = {"NIFTY": (50, 65), "BANKNIFTY": (40, 30), "SENSEX": (60, 20)}   # index: (usual stop points, lot size)
MODES = ("post", "fixed", "risk")


def _inr(v: Optional[float], sign: bool = False) -> str:
    from backend.tips import book as bk
    return bk.inr(v, sign=sign)


def _plural(n: int, w: str = "lot") -> str:
    return f"{n} {w}{'' if n == 1 else 's'}"


def cfg_of(config) -> dict:
    t = config.tips
    return {"mode": str(getattr(t, "kite_size_mode", "post") or "post"), "lots": int(getattr(t, "lots", 1) or 1),
            "cap": max_lots(config), "risk": risk_limit(config),
            "mult": mult_of(config)}


def max_lots(config) -> int:
    """The effective lots cap: kite_max_lots × kite_lot_multiplier (the cap is set for 1×; ×N scales it with the size)."""
    t = getattr(config, "tips", None)
    return int(getattr(t, "kite_max_lots", 2) or 2) * mult_of(config)


def risk_limit(config) -> float:
    """The effective per-trade risk: kite_risk_per_trade × kite_lot_multiplier (set for 1×; ×N scales it with the size)."""
    t = getattr(config, "tips", None)
    return float(getattr(t, "kite_risk_per_trade", 6000.0) or 6000.0) * mult_of(config)


def mult_of(config) -> int:
    """tips.kite_lot_multiplier, 1–5 (1 when unset or out of range)."""
    try:
        m = int(getattr(getattr(config, "tips", None), "kite_lot_multiplier", 1) or 1)
    except (TypeError, ValueError):
        return 1
    return m if 1 <= m <= 5 else 1


def post_lots(h: dict) -> Optional[int]:
    """The size the post (or a channel follow-up "2-2 lots") names, in lots, or None."""
    sh = h.get("size_hint") or {}
    lot = int(h.get("lot_size") or 0) or 1
    if sh.get("lots"):
        return int(sh["lots"])
    if sh.get("qty"):
        return max(1, int(sh["qty"]) // lot)
    return None


def entry_ref(h: dict, ltp: Optional[float] = None) -> Optional[float]:
    """The entry price the risk is measured from (see the module doc)."""
    e = (h.get("plan") or {}).get("entry") or h.get("entry") or {}
    m = e.get("mode") or "market"
    if m == "market" or pl.is_spot(e) or e.get("lo") is None:
        return float(ltp) if ltp is not None else None
    lo, hi = float(e["lo"]), float(e.get("hi") or e["lo"])
    if m == "zone":
        return hi if h.get("side") != "SELL" else lo
    return lo


def premium_stop(h: dict) -> Optional[float]:
    st = (h.get("plan") or {}).get("stop") or h.get("stop") or {}
    return float(st["value"]) if st.get("kind") == "premium" and st.get("value") is not None else None


def stop_points(h: dict, entry: Optional[float], stop: Optional[float] = None) -> Optional[float]:
    """Entry − stop for a buy (stop − entry for a sell); None without a premium stop or an entry price."""
    stop = stop if stop is not None else premium_stop(h)
    if entry is None or stop is None:
        return None
    d = (float(entry) - float(stop)) if h.get("side") != "SELL" else (float(stop) - float(entry))
    return round(d, 4)


def size(config, h: dict, ltp: Optional[float] = None, entry: Optional[float] = None, stop: Optional[float] = None) -> dict:
    """{mode, lots, raw, src, post, fallback, cap, lot, qty, entry, points, risk_rs, risk_cap, held, max_ok, premium,
    ratio}: the size the mode gives (lots; `raw` before the risk mode's cap) and the guard's numbers."""
    c = cfg_of(config)
    lot = int(h.get("lot_size") or 0) or 1
    pst = post_lots(h)
    e = entry if entry is not None else entry_ref(h, ltp)
    d = stop_points(h, e, stop)
    ratio = None
    if c["mode"] == "fixed":
        raw, src = c["lots"], "fixed"
    elif c["mode"] == "risk" and d and d > 0:
        ratio = c["risk"] / (d * lot)
        raw, src = max(1, int(math.floor(ratio + 1e-9))), "risk"
    elif c["mode"] == "risk":
        raw, src = pst or c["lots"], "risk_fallback"           # no premium stop: nothing to measure, the post / lots
    else:
        raw, src = (pst, "post") if pst else (c["lots"], "fallback")
    base, mult = raw, (c["mult"] if src != "risk" else 1)
    raw = base * mult                                       # 📏 lots ×N: never the risk mode's own figure; caps after
    lots = min(raw, c["cap"]) if c["mode"] == "risk" else raw
    return {**_guard(c, lot, lots, e, d), "mode": c["mode"], "raw": raw, "src": src, "post": pst, "fallback": c["lots"],
            "cap": c["cap"], "ratio": ratio, "base": base, "mult": mult}


def mult_text(s: dict) -> str:
    """"4 lots (2 × 2)" when the multiplier applies, else "2 lots"."""
    if int(s.get("mult") or 1) > 1:
        return f"{_plural(int(s['raw']))} ({s['base']} × {s['mult']})"
    return _plural(int(s.get("lots") or 0))


def _guard(c: dict, lot: int, lots: int, e: Optional[float], d: Optional[float]) -> dict:
    qty = int(lots) * lot
    risk_rs = round(d * qty, 2) if d is not None and d > 0 else None
    held = risk_rs is not None and risk_rs > c["risk"] + 1e-6
    max_ok = int(math.floor(c["risk"] / (d * lot) + 1e-9)) if d and d > 0 else None
    if max_ok is not None:
        max_ok = min(max_ok, c["cap"])
    return {"lots": int(lots), "lot": lot, "qty": qty, "entry": e, "points": d, "risk_rs": risk_rs, "risk_cap": c["risk"],
            "held": held, "max_ok": max_ok, "premium": round(e * qty, 2) if e is not None else None}


def check(config, h: dict, lots: int, ltp: Optional[float] = None, entry: Optional[float] = None, stop: Optional[float] = None) -> dict:
    """The guard for `lots` on hint `h` (the numbers of size(), for a given size)."""
    c = cfg_of(config)
    lot = int(h.get("lot_size") or 0) or 1
    e = entry if entry is not None else entry_ref(h, ltp)
    return _guard(c, lot, int(lots), e, stop_points(h, e, stop))


def held_text(g: dict) -> str:
    """"size held: 2 lots × 55 points × 65 = ₹7,150 at the stop, above kite_risk_per_trade ₹6,000 · lower the lots in
    Settings or take 1 lot by hand"."""
    tail = (f" · lower the lots in Settings or take {_plural(g['max_ok'])} by hand" if g.get("max_ok") else
            " · even 1 lot is over: lower the lots or raise kite_risk_per_trade in Settings")
    return (f"size held: {_plural(g['lots'])} × {pl.fmt(g['points'])} points × {g['lot']} = {_inr(g['risk_rs'])} at the stop, "
            f"above kite_risk_per_trade {_inr(g['risk_cap'])}{tail}")


def guard_text(g: dict) -> str:
    if g.get("held"):
        return held_text(g)
    if g.get("risk_rs") is None:
        return "risk per trade · no premium stop to measure"
    return f"risk at the stop {_inr(g['risk_rs'])} ≤ {_inr(g['risk_cap'])} per trade"


def cell(config, h: dict, ltp: Optional[float] = None, lots: Optional[int] = None) -> Optional[dict]:
    """The card's size cell: {line1, line2, line3, held, held_text, max_ok, lots, qty}. None without a lot size."""
    if not h.get("lot_size"):
        return None
    s = size(config, h, ltp)
    if lots is not None and int(lots) != s["lots"]:              # the size Kite actually has (an add counts)
        s.update(check(config, h, int(lots), ltp))
    n, lot = s["lots"], s["lot"]
    prem = f"premium {_inr(s['premium'])}" if s["premium"] is not None else "premium —"
    loss = f"at the stop {_inr(-s['risk_rs'], sign=True)}" if s["risk_rs"] is not None else "no premium stop"
    if s["mode"] == "risk" and s["src"] == "risk":
        e, st = pl.fmt(s["entry"]), pl.fmt(premium_stop(h))
        a, b = (e, st) if h.get("side") != "SELL" else (st, e)
        why = f"risk {_inr(s['risk_cap'])} ÷ ({a} − {b}) × {lot} = {math.floor(s['ratio'] * 100) / 100:.2f} → {_plural(n)} · {prem}"
        rule = f"by risk · cap {s['cap']}"
    elif s["mode"] == "risk":
        why = f"no premium stop to size by · {'the post says ' + _plural(s['post']) if s['post'] else 'fallback ' + _plural(s['fallback'])} · {prem}"
        rule = f"by risk · cap {s['cap']}"
    elif s["mode"] == "fixed":
        why = ("fixed · " + (f"the post said {_plural(s['post'])} (ignored) · " if s["post"] else "") + f"{prem} · {loss}")
        rule = f"fixed {_plural(s['fallback'])} · cap {s['cap']}"
    else:
        why = (f"the post says {_plural(s['post'])}" if s["post"] else f"the post names no size · {_plural(s['fallback'])}") + f" · {prem} · {loss}"
        rule = f"post wins · fallback {s['fallback']}"
    if s["mode"] == "risk" and s["src"] == "risk":
        why += f" · {loss}"
    held = bool(s["held"])
    if int(s.get("mult") or 1) > 1:                      # "the post says 2 lots × 2 (lots ×2)"
        m = s["mult"]
        why = why.replace(f"the post says {_plural(s['post'])}", f"the post says {_plural(s['post'])} × {m} (lots ×{m})") \
            if s["post"] and s["src"] in ("post", "risk_fallback") else why
        if s["src"] == "fallback":
            why = why.replace(f"the post names no size · {_plural(s['fallback'])}", f"the post names no size · {_plural(s['fallback'])} × {m} (lots ×{m})")
        elif s["src"] == "fixed":
            why = why.replace("fixed · ", f"fixed {_plural(s['fallback'])} × {m} (lots ×{m}) · ", 1)
        elif s["src"] == "risk_fallback" and not s["post"]:
            why = why.replace(f"fallback {_plural(s['fallback'])}", f"fallback {_plural(s['fallback'])} × {m} (lots ×{m})")
        rule += f" · lots ×{m}"
    if held:
        rule = f"⛔ above risk {_inr(s['risk_cap'])} per trade · not armed"
    return {"line1": f"{_plural(n)} · {n * lot} qty", "line2": why, "line3": rule, "held": held,
            "held_text": held_text(s) if held else None, "max_ok": s["max_ok"], "lots": n, "qty": n * lot, "mode": s["mode"],
            "risk_rs": s["risk_rs"], "premium": s["premium"], "mult": int(s.get("mult") or 1), "base": s.get("base")}


# --- the settings check -----------------------------------------------------------------------------------------------------

def usual_stops(days: int = 30, now: Optional[datetime] = None) -> dict:
    """{index: (median stop points, lot size, src)} from the last `days` days' calls (premium stops with an entry level),
    else the fixed table."""
    out = {k: (float(p), int(lot), "table") for k, (p, lot) in STOP_TABLE.items()}
    try:
        from backend.database.models import get_db
        from backend.tips import book as bk
        since = ((now or datetime.now()) - timedelta(days=days)).isoformat(timespec="seconds")
        with get_db() as conn:
            hs = bk.rows("posted_at >= ? AND symbol IN ('NIFTY', 'BANKNIFTY', 'SENSEX')", (since,), conn=conn)
    except Exception:
        return out
    by: dict = {}
    lots: dict = {}
    for h in hs:
        d = stop_points(h, entry_ref(h))
        if d and d > 0:
            by.setdefault(h["symbol"], []).append(d)
            if h.get("lot_size"):
                lots[h["symbol"]] = int(h["lot_size"])
    for sym, ds in by.items():
        out[sym] = (round(float(median(ds)), 2), lots.get(sym) or STOP_TABLE.get(sym, (0, 1))[1], "calls")
    return out


def check_settings(sec) -> Optional[str]:
    """The refusal (with the arithmetic) when tips.lots at the usual stop of an index is over kite_risk_per_trade or
    kite_loss_cap, else None. With kite_mode off `lots` sizes paper takes only (nothing reaches Kite): not checked."""
    if str(getattr(sec, "kite_mode", "off") or "off") == "off":
        return None
    lots = int(getattr(sec, "lots", 1) or 1)
    risk = float(getattr(sec, "kite_risk_per_trade", 6000.0) or 0)
    cap = float(getattr(sec, "kite_loss_cap", 6000.0) or 0)
    for sym, (pts, lot, src) in usual_stops().items():
        rs = round(lots * lot * pts, 2)
        why = "the median stop of the last 30 days' calls" if src == "calls" else "the usual stop"
        for name, lim in (("kite_risk_per_trade", risk), ("kite_loss_cap", cap)):
            if lim > 0 and rs > lim + 1e-6:
                return (f"refused: lots {lots} × {lot} ({sym} lot size) × {pl.fmt(pts)} points ({sym}: {why}) = {_inr(rs)} at the stop, "
                        f"above {name} {_inr(lim)} · lower lots or raise {name}")
    return None
