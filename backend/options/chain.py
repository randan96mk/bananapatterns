"""The NIFTY option chain for the book: one Kite quote call for +-12 strikes of the near expiry (last price, bid / ask
from the depth, OI), IV and delta per strike (Black-Scholes off the mid), snapshots in `options_chain_snap` (never
deleted), the walls, the PCR, the OI change against the 9:30 snapshot (else the previous close's), the 0-3 confirmation
score with its one-line reading, and the strike selection (sold leg + hedge) of the plan."""

import math
from datetime import time
from typing import Optional

from backend.database.models import get_db, now_ist
from backend.options import greeks
from backend.options.bias import BEARISH, BULLISH
from backend.options.instruments import STRIKE_STEP

STRIKES_EACH_SIDE = 12          # the quote call: +-12 strikes = 48 symbols (<= 500 a call)
PCR_STRIKES = 10                # the PCR over +-10 strikes
OI_CHG_PCT = 2.0                # the OI change over strikes within +-2% of spot
WALL_NEAR_PCT = 1.5             # the wall rule applies when the wall is within 1.5% of spot
WALL_CLEAR_PCT = 0.5            # +1 when spot is >= 0.5% past the wall on its side
CROWDED_HI, CROWDED_LO = 1.5, 0.6
HEDGE_MIN_PREMIUM = 2.0         # a hedge under Rs 2: 200 points instead
HEDGE_FALLBACK = 200
MAX_DELTA_NEARER = 0.35         # stepping nearer for the Rs 20 floor stops at |delta| 0.35
INDEX_KEY, VIX_KEY = "NSE:NIFTY 50", "NSE:INDIA VIX"

SOLD_TYPE = {BULLISH: "PE", BEARISH: "CE"}


def grid(x: float, step: float = STRIKE_STEP, how: str = "nearest") -> float:
    f = {"nearest": round, "down": math.floor, "up": math.ceil}[how]
    return float(f(x / step) * step)


def strikes_around(spot: float, n: int = STRIKES_EACH_SIDE, step: float = STRIKE_STEP) -> list[float]:
    atm = grid(spot, step)
    return [atm + k * step for k in range(-n, n + 1)]


def pick_rows(contracts: list[dict], spot: float, n: int = STRIKES_EACH_SIDE, strikes: Optional[list[float]] = None) -> list[dict]:
    """The contract rows (CE and PE) of the strikes around spot, or of the given strikes."""
    want = set(strikes if strikes is not None else strikes_around(spot, n))
    return [c for c in contracts if c["strike"] in want]


# --- quotes -----------------------------------------------------------------------------------------------

def _mid(q: dict) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """(bid, ask, mid) from a Kite quote's depth; the last price stands in when a side is empty."""
    d = q.get("depth") or {}
    bid = ((d.get("buy") or [{}])[0] or {}).get("price") or None
    ask = ((d.get("sell") or [{}])[0] or {}).get("price") or None
    ltp = q.get("last_price") or None
    if bid and ask:
        return bid, ask, round((bid + ask) / 2, 2)
    px = bid or ask or ltp
    return bid, ask, (round(px, 2) if px else None)


def fetch(client, rows: list[dict], at: Optional[time] = None, sessions_left: int = 0, spot: Optional[float] = None,
          with_vix: bool = False) -> dict:
    """One quote call for the rows (plus the index and, when asked, India VIX). Returns {spot, vix, at, rows: [{strike,
    type, tradingsymbol, token, ltp, bid, ask, mid, oi, iv, delta}]}; rows without a quote are left out."""
    keys = [f"NFO:{r['tradingsymbol']}" for r in rows] + [INDEX_KEY] + ([VIX_KEY] if with_vix else [])
    q = client.quote(keys) or {}
    idx = q.get(INDEX_KEY) or {}
    spot = idx.get("last_price") or spot
    vix = (q.get(VIX_KEY) or {}).get("last_price") if with_vix else None
    T = greeks.time_to_expiry(sessions_left, at)
    out = []
    for r in rows:
        v = q.get(f"NFO:{r['tradingsymbol']}")
        if not v:
            continue
        bid, ask, mid = _mid(v)
        iv, dl = greeks.iv_and_delta(mid, spot, r["strike"], T, r["type"]) if spot and mid else (None, None)
        out.append({"strike": r["strike"], "type": r["type"], "tradingsymbol": r["tradingsymbol"], "token": r["token"],
                    "ltp": v.get("last_price"), "bid": bid, "ask": ask, "mid": mid, "oi": int(v.get("oi") or 0), "iv": iv, "delta": dl})
    return {"spot": spot, "vix": vix, "at": now_ist(), "rows": out, "T": T}


# --- snapshots --------------------------------------------------------------------------------------------

def snapshot(day: str, kind: str, expiry: str, rows: list[dict], spot: Optional[float], ts: Optional[str] = None) -> int:
    """Append the rows as one snapshot of `kind` (open / bias / live / close). Nothing is ever deleted."""
    ts = ts or now_ist()
    with get_db() as conn:
        conn.executemany("INSERT INTO options_chain_snap (ts, day, kind, expiry, strike, type, ltp, bid, ask, oi, iv, delta, spot) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         [(ts, day, kind, expiry, r["strike"], r["type"], r.get("ltp"), r.get("bid"), r.get("ask"), r.get("oi"),
                           r.get("iv"), r.get("delta"), spot) for r in rows])
    return len(rows)


def _snap_rows(conn, ts: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT strike, type, ltp, bid, ask, oi, iv, delta, spot FROM options_chain_snap WHERE ts = ?", (ts,))]


def baseline(day: str, expiry: str) -> Optional[dict]:
    """The OI baseline for the day's OI change: today's 9:30 snapshot, else the latest close snapshot before today
    (the same expiry). {kind, ts, rows} or None."""
    with get_db() as conn:
        r = conn.execute("SELECT MIN(ts) FROM options_chain_snap WHERE day = ? AND kind = 'open' AND expiry = ?", (day, expiry)).fetchone()
        if r and r[0]:
            return {"kind": "open", "ts": r[0], "rows": _snap_rows(conn, r[0])}
        r = conn.execute("SELECT MAX(ts) FROM options_chain_snap WHERE day < ? AND kind = 'close' AND expiry = ?", (day, expiry)).fetchone()
        if r and r[0]:
            return {"kind": "close", "ts": r[0], "rows": _snap_rows(conn, r[0])}
    return None


def last_snapshot() -> Optional[dict]:
    """{ts, day, kind, expiry, rows} of the latest snapshot, for the health row and GET /api/options/chain."""
    with get_db() as conn:
        r = conn.execute("SELECT ts, day, kind, expiry, spot FROM options_chain_snap ORDER BY id DESC LIMIT 1").fetchone()
        if not r:
            return None
        return {"ts": r["ts"], "day": r["day"], "kind": r["kind"], "expiry": r["expiry"], "spot": r["spot"], "rows": _snap_rows(conn, r["ts"])}


# --- the reading ------------------------------------------------------------------------------------------

def _oi(rows: list[dict], kind: str) -> dict[float, int]:
    return {r["strike"]: int(r.get("oi") or 0) for r in rows if r["type"] == kind}


def walls(rows: list[dict], spot: float) -> dict:
    """support = the biggest put OI strike below spot, resistance = the biggest call OI strike above; put_max /
    call_max = the biggest OI strike of each side anywhere in the chain."""
    puts, calls = _oi(rows, "PE"), _oi(rows, "CE")
    below = {k: v for k, v in puts.items() if k < spot}
    above = {k: v for k, v in calls.items() if k > spot}
    best = lambda d: max(d, key=lambda k: (d[k], -abs(k - spot))) if d and max(d.values()) > 0 else None   # ties: nearest spot
    return {"support": best(below), "resistance": best(above), "put_max": best(puts), "call_max": best(calls),
            "support_oi": below.get(best(below)) if best(below) else None, "resistance_oi": above.get(best(above)) if best(above) else None}


def pcr(rows: list[dict], spot: float, n: int = PCR_STRIKES) -> Optional[float]:
    """Put OI / call OI over the +-n strikes around spot."""
    want = set(strikes_around(spot, n))
    p = sum(v for k, v in _oi(rows, "PE").items() if k in want)
    c = sum(v for k, v in _oi(rows, "CE").items() if k in want)
    return round(p / c, 2) if c else None


def oi_change(rows: list[dict], base_rows: Optional[list[dict]], spot: float, pct: float = OI_CHG_PCT) -> Optional[dict]:
    """{puts, calls} = the OI added since the baseline on strikes within +-pct of spot (None without a baseline)."""
    if not base_rows:
        return None
    lo, hi = spot * (1 - pct / 100), spot * (1 + pct / 100)
    out = {}
    for kind, name in (("PE", "puts"), ("CE", "calls")):
        now, base = _oi(rows, kind), _oi(base_rows, kind)
        out[name] = sum(v - base.get(k, 0) for k, v in now.items() if lo <= k <= hi and k in base)
    return out


def per_strike_change(rows: list[dict], base_rows: Optional[list[dict]]) -> dict[tuple, int]:
    """{(strike, type): OI change since the baseline} for the chain display."""
    if not base_rows:
        return {}
    base = {(r["strike"], r["type"]): int(r.get("oi") or 0) for r in base_rows}
    return {(r["strike"], r["type"]): int(r.get("oi") or 0) - base[(r["strike"], r["type"])] for r in rows if (r["strike"], r["type"]) in base}


def iv_sides(rows: list[dict], spot: float, n: int = 5) -> dict:
    """Average IV (%) of the OTM puts and OTM calls within n strikes: displayed, not a rule."""
    lo, hi = spot - n * STRIKE_STEP, spot + n * STRIKE_STEP
    p = [r["iv"] for r in rows if r["type"] == "PE" and r["iv"] and lo <= r["strike"] < spot]
    c = [r["iv"] for r in rows if r["type"] == "CE" and r["iv"] and spot < r["strike"] <= hi]
    avg = lambda xs: round(sum(xs) / len(xs) * 100, 1) if xs else None
    return {"put": avg(p), "call": avg(c)}


def _k(n: Optional[float]) -> str:
    return f"{n:,.0f}" if n is not None else "–"


def _oi_text(n: int) -> str:
    a = abs(n)
    s = f"{a / 1e5:.1f}L" if a >= 1e5 else f"{a / 1e3:.0f}k" if a >= 1e3 else str(a)
    return ("+" if n > 0 else "−" if n < 0 else "") + s


def score(bias: str, rows: list[dict], spot: float, base_rows: Optional[list[dict]], pcr_bull: float, pcr_bear: float) -> dict:
    """The 0-3 confirmation at the moment of the bias: walls, OI change, PCR. {score, walls, pcr, oi_chg, crowded,
    parts: [{name, ok, text}], line}."""
    w = walls(rows, spot)
    ratio = pcr(rows, spot)
    chg = oi_change(rows, base_rows, spot)
    parts = []
    if bias == BULLISH:
        wall_ok = bool((w["call_max"] is not None and spot > w["call_max"]) or (w["support"] is not None and spot >= w["support"] * (1 + WALL_CLEAR_PCT / 100)))
        oi_ok = bool(chg and chg["puts"] > 0 and chg["calls"] <= 0)
        pcr_ok = ratio is not None and ratio > pcr_bull
    else:
        wall_ok = bool((w["put_max"] is not None and spot < w["put_max"]) or (w["resistance"] is not None and spot <= w["resistance"] * (1 - WALL_CLEAR_PCT / 100)))
        oi_ok = bool(chg and chg["calls"] > 0 and chg["puts"] <= 0)
        pcr_ok = ratio is not None and ratio < pcr_bear
    tick = lambda ok: "✓" if ok else "✗"
    parts.append({"name": "walls", "ok": wall_ok, "text": f"support {_k(w['support'])} · resistance {_k(w['resistance'])} {tick(wall_ok)}"})
    oi_text = f"OI puts {_oi_text(chg['puts'])} / calls {_oi_text(chg['calls'])} {tick(oi_ok)}" if chg else "OI change: no baseline yet ✗"
    parts.append({"name": "oi_change", "ok": oi_ok, "text": oi_text})
    parts.append({"name": "pcr", "ok": pcr_ok, "text": f"PCR {ratio if ratio is not None else '–'} {tick(pcr_ok)}"})
    s = int(wall_ok) + int(oi_ok) + int(pcr_ok)
    crowded = ratio is not None and (ratio > CROWDED_HI or ratio < CROWDED_LO)
    line = " · ".join(p["text"] for p in parts) + f" → {s}/3" + (" · crowded" if crowded else "")
    return {"score": s, "walls": w, "pcr": ratio, "oi_chg": chg, "crowded": crowded, "parts": parts, "line": line}


# --- strike selection ---------------------------------------------------------------------------------------

def _row(rows: list[dict], strike: float, kind: str) -> Optional[dict]:
    return next((r for r in rows if r["strike"] == strike and r["type"] == kind), None)


def select_strikes(bias: str, rows: list[dict], spot: float, w: dict, otm_pct: float, max_delta: float, min_premium: float,
                   hedge_points: int) -> dict:
    """The sold leg = the farther of (otm_pct OTM on the grid) and (the first strike with |delta| <= max_delta), pulled
    to the wall when it is within 1.5% (at or below the put wall for bullish, at or above the call wall for bearish);
    premium >= min_premium else one strike nearer at a time while |delta| <= 0.35. The hedge = sold -/+ hedge_points
    (200 when its premium is under Rs 2). Returns {ok, sold, hedge, credit, why} (why = the reason when not ok)."""
    kind = SOLD_TYPE[bias]
    sign = -1 if kind == "PE" else 1                     # away from spot
    side = [r for r in rows if r["type"] == kind and (r["strike"] < spot if kind == "PE" else r["strike"] > spot)]
    side.sort(key=lambda r: -r["strike"] if kind == "PE" else r["strike"])     # nearest first
    if not side:
        return {"ok": False, "why": "no quoted strikes on the sold side"}
    otm = grid(spot * (1 - otm_pct / 100), how="down") if kind == "PE" else grid(spot * (1 + otm_pct / 100), how="up")
    by_delta = next((r["strike"] for r in side if r["delta"] is not None and abs(r["delta"]) <= max_delta), None)
    sold = otm if by_delta is None else (min(otm, by_delta) if kind == "PE" else max(otm, by_delta))
    wall = w.get("support") if kind == "PE" else w.get("resistance")
    if wall is not None and abs(wall / spot - 1) * 100 <= WALL_NEAR_PCT:
        sold = min(sold, wall) if kind == "PE" else max(sold, wall)
    r = _row(rows, sold, kind)
    while r is None or (r["mid"] or 0) < min_premium:
        nearer = sold - sign * STRIKE_STEP
        rn = _row(rows, nearer, kind)
        if r is None and rn is None:
            return {"ok": False, "why": f"strike {sold:,.0f} {kind} not quoted"}
        if rn is None or (rn["delta"] is not None and abs(rn["delta"]) > MAX_DELTA_NEARER) or (kind == "PE" and nearer >= spot) or (kind == "CE" and nearer <= spot):
            return {"ok": False, "why": f"sold premium under Rs {min_premium:.0f} at {sold:,.0f} {kind} and the next strike is past delta {MAX_DELTA_NEARER}"}
        sold, r = nearer, rn
    hedge_k = sold + sign * hedge_points
    h = _row(rows, hedge_k, kind)
    if h is not None and (h["mid"] or 0) < HEDGE_MIN_PREMIUM:
        alt = _row(rows, sold + sign * HEDGE_FALLBACK, kind)
        if alt is not None:
            hedge_k, h = sold + sign * HEDGE_FALLBACK, alt
    if h is None:
        return {"ok": False, "why": f"hedge {hedge_k:,.0f} {kind} not quoted"}
    return {"ok": True, "sold": r, "hedge": h, "credit": round((r["mid"] or 0) - (h["mid"] or 0), 2), "width": abs(hedge_k - sold),
            "why": "", "otm_strike": otm, "delta_strike": by_delta, "wall": wall}
