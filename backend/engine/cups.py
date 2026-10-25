"""Cup with handle (William O'Neil), as bases the stage engine already understands.

At a bar t a pattern ends with a handle when, looking only at bars up to t:
  right lip r   the highest high 5..25 sessions before t, never exceeded since (the handle is intact)
  handle        bars r+1..t: dips handle_min_depth..handle_max_depth under the right lip, in the
                cup's upper half, on volume <= handle_max_volume x the 50-day average before it
  left lip L    the highest high 7..65 weeks before r, with nothing in between above the lips;
                the right lip within -10% / +5% of it; a >= 30% rise into it
  cup bottom    the lowest low between the lips, 12-33% under the left lip, U-shaped: at least
                u_min_bars sessions with a low within u_low_band of the bottom, and at least
                u_bottom_frac of the closes in the cup's bottom third (a V or spike bottom has fewer)
  pivot         the handle high (= the right lip's high); a close above it is the breakout

Each pattern is returned as a Base (kind "cup"): start = left lip, end = last handle bar,
low = cup bottom, pivot = handle high, depth = cup depth, vol_dryup = handle volume ratio,
plus `shape` with the lips, bottom, handle and stop. So classify() gives the same stages as
for box bases (forming = in the handle near the pivot, fresh / climbing / played out after
the breakout). Causal like detect_bases: a pattern is known exactly when it could have been.
"""

from datetime import date
from typing import Optional

import numpy as np

from backend.config import BaseConfig, CupConfig
from backend.engine.bases import Base, breakout_metrics

MAX_LIP_TRIES = 8


def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


def find_cup(high, low, vol, t: int, cfg: CupConfig, close=None) -> Optional[dict]:
    """The cup-with-handle whose handle ends at bar t, or None. Index-based (no dates)."""
    lo_r, hi_r = t - cfg.handle_max_bars, t - cfg.handle_min_bars
    if lo_r < cfg.min_cup_bars:
        lo_r = cfg.min_cup_bars
    if hi_r < lo_r:
        return None
    r = lo_r + int(np.argmax(high[lo_r:hi_r + 1]))
    lip_r = high[r]
    if high[r + 1:t + 1].max() > lip_r:                       # the handle has already been exceeded
        return None
    h_low = low[r + 1:t + 1].min()
    h_depth = 1 - h_low / lip_r
    if not cfg.handle_min_depth <= h_depth <= cfg.handle_max_depth:
        return None

    a, b = max(0, r - cfg.max_cup_bars), r - cfg.min_cup_bars
    L = None
    for _ in range(MAX_LIP_TRIES):                            # an older, much higher peak isn't this cup's lip
        if b < a:
            return None
        cand = a + int(np.argmax(high[a:b + 1]))
        if lip_r >= high[cand] * (1 - cfg.right_lip_within):
            L = cand
            break
        a = cand + 1
    if L is None:
        return None
    lip_l = high[L]
    if lip_r > lip_l * (1 + cfg.right_lip_above):
        return None
    if r - L > 1 and high[L + 1:r].max() > max(lip_l, lip_r):  # something poked above the rim
        return None
    B = L + int(np.argmin(low[L:r + 1]))
    bottom = low[B]
    depth = 1 - bottom / lip_l
    if not cfg.min_depth <= depth <= cfg.max_depth:
        return None
    u_bars = int((low[L:r + 1] <= bottom * (1 + cfg.u_low_band)).sum())
    if u_bars < cfg.u_min_bars:
        return None
    closes = (close if close is not None else low)[L:r + 1]
    u_frac = float((closes <= bottom + (lip_l - bottom) / 3).mean())   # time spent in the bottom third
    if u_frac < cfg.u_bottom_frac:
        return None
    prior_lo = low[max(0, L - cfg.prior_lookback_bars):L + 1].min()
    advance = lip_l / prior_lo - 1
    if advance < cfg.min_prior_advance:
        return None
    mid = bottom + 0.5 * (lip_l - bottom)
    if cfg.handle_upper_half and h_low < mid:
        return None
    before = vol[max(0, r - 50):r]
    h_vol = vol[r + 1:t + 1].mean() / before.mean() if len(before) and before.mean() > 0 else None
    if h_vol is not None and h_vol > cfg.handle_max_volume:
        return None
    return {"L": L, "B": B, "r": r, "t": t, "pivot": float(lip_r), "lip_l": float(lip_l), "bottom": float(bottom),
            "h_low": float(h_low), "h_depth": float(h_depth), "depth": float(depth), "u_bars": u_bars,
            "u_frac": u_frac,
            "advance": float(advance), "h_vol": None if h_vol is None else float(h_vol)}


def _as_base(c: dict, dates: list, status: str, max_stop: float, cfg: CupConfig) -> Base:
    L, B, r, t = c["L"], c["B"], c["r"], c["t"]
    days = (date.fromisoformat(dates[t]) - date.fromisoformat(dates[L])).days
    stop = max(c["h_low"] * (1 - cfg.stop_buffer), c["pivot"] * (1 - max_stop))
    shape = {"left_lip": {"date": dates[L], "high": _r(c["lip_l"])},
             "bottom": {"date": dates[B], "low": _r(c["bottom"])},
             "right_lip": {"date": dates[r], "high": _r(c["pivot"])},
             "handle": {"start": dates[r + 1], "end": dates[t], "low": _r(c["h_low"]), "bars": t - r,
                        "depth_pct": _r(c["h_depth"] * 100), "volume_x": _r(c["h_vol"])},
             "cup": {"weeks": max(1, (date.fromisoformat(dates[r]) - date.fromisoformat(dates[L])).days // 7),
                     "depth_pct": _r(c["depth"] * 100), "u_bars": c["u_bars"], "u_frac": _r(c["u_frac"])},
             "prior_advance_pct": _r(c["advance"] * 100), "stop": _r(stop)}
    return Base(number=1, status=status, start_date=dates[L], end_date=dates[t], start_index=L, end_index=t,
                bars=t - L + 1, weeks=max(1, days // 7), pivot=_r(c["pivot"]), pivot_date=dates[r],
                low=_r(c["bottom"]), low_date=dates[B], depth_pct=_r(c["depth"] * 100), vol_dryup=_r(c["h_vol"]),
                kind="cup", shape=shape)


def detect_cups(candles: list[dict], cfg: CupConfig, bases_cfg: BaseConfig, max_stop: float = 0.08) -> list[Base]:
    """Cups with handles, oldest first: those that broke out in the last history_bars sessions, then the
    one still forming at the last bar (if any)."""
    n = len(candles)
    if n < cfg.min_cup_bars + cfg.handle_min_bars + 2:
        return []
    dates = [c["date"] for c in candles]
    high = np.array([c["high"] for c in candles], dtype=float)
    low = np.array([c["low"] for c in candles], dtype=float)
    close = np.array([c["close"] for c in candles], dtype=float)
    vol = np.array([c["volume"] for c in candles], dtype=float)

    out: list[Base] = []
    t, last_bo = max(cfg.min_cup_bars + cfg.handle_min_bars + 1, n - cfg.history_bars), -1
    while t < n:
        # necessary for a breakout at t: a close above the last handle_min_bars highs (all inside the handle)
        if t - 1 > last_bo and close[t] > high[t - cfg.handle_min_bars:t].max():
            c = find_cup(high, low, vol, t - 1, cfg, close)
            if c is not None and close[t] > c["pivot"] and c["L"] > last_bo:
                base = _as_base(c, dates, "broken_out", max_stop, cfg)
                base.breakout = breakout_metrics(t, c["pivot"], dates, high, low, close, vol, bases_cfg)
                out.append(base)
                last_bo = t
        t += 1

    c = find_cup(high, low, vol, n - 1, cfg, close)
    if c is not None and c["L"] > last_bo and close[-1] <= c["pivot"]:
        base = _as_base(c, dates, "forming", max_stop, cfg)
        base.dist_to_pivot_pct = _r((c["pivot"] - close[-1]) / c["pivot"] * 100)
        out.append(base)
    return out


def cup_score(shape: dict, rs: Optional[int], cfg: CupConfig) -> float:
    """0-100: handle tightness 30, volume dry-up 20, U-shape 15, cup depth 15, RS 20."""
    h = shape["handle"]
    tight = 30 * max(0.0, 1 - (h["depth_pct"] or 0) / (cfg.handle_max_depth * 100))
    hv = h["volume_x"] if h["volume_x"] is not None else 1.0
    dry = 20 * max(0.0, min(1.0, (1.2 - hv) / 0.8))           # 0.4x or less: full marks; 1.2x: none
    u = 15 * min(1.0, (shape["cup"].get("u_frac") or 0) / 0.5)    # half the cup in its bottom third: full marks
    d = shape["cup"]["depth_pct"] or 0
    depth = 15 if 15 <= d <= 30 else 8
    return round(tight + dry + u + depth + 20 * (rs or 0) / 99, 1)
