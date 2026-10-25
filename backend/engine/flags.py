"""Bull flag and high-tight flag, as bases the stage engine already understands.

At a bar t a pattern ends with a flag when, looking only at bars up to t:
  flag top T    the highest high flag_min_bars..flag_max_bars sessions before t (inclusive of t - min),
                never exceeded since: the flag is intact. pivot = high[T] (the flag's high)
  pole          from P, the latest low within 10% of the pole's height above the lowest low of the
                pole_max_bars before T (at least pole_min_bars before it): where the sharp move starts;
                high[T] / low[P] - 1 >= min_pole_gain, on volume >= pole_min_volume x the 50-day average
  flag          bars T+1..t: low no more than max_flag_depth under the top and no more than max_retrace of
                the pole; volume <= max_flag_volume x the pole's; its fitted line drifts sideways or down
                (rises no more than max_flag_rise over the flag)
  tight         retrace <= tight_retrace
  high-tight    pole >= htf_min_gain within htf_max_bars and the flag no deeper than 25%

Each pattern is returned as a Base (kind "flag"): start = pole low, end = last flag bar, low = flag low,
pivot = flag high, depth = flag depth, vol_dryup = flag / pole volume, plus `shape` with the pole, the flag
channel (two parallel lines fitted to the flag) and the stop. classify() then gives the usual stages.
Causal like detect_bases: a pattern is known exactly when it could have been.
"""

from datetime import date
from typing import Optional

import numpy as np

from backend.config import BaseConfig, FlagConfig
from backend.engine.bases import Base, breakout_metrics


def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


def find_flag(high, low, close, vol, t: int, cfg: FlagConfig) -> Optional[dict]:
    """The flag whose last bar is t, or None. Index-based (no dates)."""
    lo_T, hi_T = t - cfg.flag_max_bars, t - cfg.flag_min_bars
    if hi_T < cfg.pole_min_bars + 1:
        return None
    lo_T = max(lo_T, cfg.pole_min_bars + 1)
    T = lo_T + int(np.argmax(high[lo_T:hi_T + 1]))
    top = high[T]
    if high[T + 1:t + 1].max() > top:                      # the top was taken out: no longer a flag
        return None
    a = max(0, T - cfg.pole_max_bars)
    if T - cfg.pole_min_bars < a:
        return None
    win = low[a:T - cfg.pole_min_bars + 1]
    if top <= win.min() * (1 + cfg.min_pole_gain):          # no pole: not even the minimum rise from the lows
        return None
    floor = win.min() + 0.1 * (top - win.min())           # the pole starts where the sharp move does: the
    P = a + int(np.nonzero(win <= floor)[0][-1])           # latest low near the bottom of the window
    pole_low = low[P]
    gain = top / pole_low - 1
    if gain < cfg.min_pole_gain:
        return None
    if high[P:T].max() > top or low[P:T + 1].min() < pole_low:
        return None
    f_low_i = T + 1 + int(np.argmin(low[T + 1:t + 1]))
    f_low = low[f_low_i]
    depth = 1 - f_low / top
    retrace = (top - f_low) / (top - pole_low)
    if depth > cfg.max_flag_depth or retrace > cfg.max_retrace:
        return None
    before = vol[max(0, P - 50):P]
    pole_vol = vol[P + 1:T + 1].mean()
    pole_x = pole_vol / before.mean() if len(before) and before.mean() > 0 else None
    if pole_x is not None and pole_x < cfg.pole_min_volume:
        return None
    flag_x = vol[T + 1:t + 1].mean() / pole_vol if pole_vol > 0 else None
    if flag_x is not None and flag_x > cfg.max_flag_volume:
        return None
    fx = np.arange(T, t + 1)
    slope = float(np.polyfit(fx, close[T:t + 1], 1)[0])
    if slope * (t - T) / top > cfg.max_flag_rise:
        return None
    hi_off = max(high[i] - slope * i for i in fx)
    lo_off = min(low[i] - slope * i for i in fx)
    pole_bars = T - P
    htf = gain >= cfg.htf_min_gain and pole_bars <= cfg.htf_max_bars and depth <= 0.25
    return {"P": P, "T": T, "t": t, "pivot": float(top), "pole_low": float(pole_low), "gain": float(gain),
            "f_low": float(f_low), "f_low_i": f_low_i, "depth": float(depth), "retrace": float(retrace), "pole_x": pole_x,
            "flag_x": flag_x, "slope": slope, "top_line": (slope * T + hi_off, slope * t + hi_off),
            "bot_line": (slope * T + lo_off, slope * t + lo_off), "htf": bool(htf),
            "tight": bool(retrace <= cfg.tight_retrace)}


def _as_base(f: dict, dates: list, status: str, cfg: FlagConfig) -> Base:
    P, T, t = f["P"], f["T"], f["t"]
    stop = max(f["f_low"] * (1 - cfg.stop_buffer), f["pivot"] * (1 - cfg.max_stop))
    days = (date.fromisoformat(dates[t]) - date.fromisoformat(dates[P])).days
    shape = {"pole": {"start": dates[P], "low": _r(f["pole_low"]), "top": dates[T], "high": _r(f["pivot"]),
                      "bars": T - P, "gain_pct": _r(f["gain"] * 100), "volume_x": _r(f["pole_x"])},
             "flag": {"start": dates[T + 1], "end": dates[t], "bars": t - T, "low": _r(f["f_low"]),
                      "depth_pct": _r(f["depth"] * 100), "retrace_pct": _r(f["retrace"] * 100),
                      "volume_x": _r(f["flag_x"]),
                      "top_line": [[dates[T], _r(f["top_line"][0])], [dates[t], _r(f["top_line"][1])]],
                      "bot_line": [[dates[T], _r(f["bot_line"][0])], [dates[t], _r(f["bot_line"][1])]]},
             "htf": f["htf"], "tight": f["tight"], "stop": _r(stop)}
    return Base(number=1, status=status, start_date=dates[P], end_date=dates[t], start_index=P, end_index=t,
                bars=t - P + 1, weeks=max(1, days // 7), pivot=_r(f["pivot"]), pivot_date=dates[T],
                low=_r(f["f_low"]), low_date=dates[f["f_low_i"]], depth_pct=_r(f["depth"] * 100),
                vol_dryup=_r(f["flag_x"]), kind="flag", shape=shape)


def detect_flags(candles: list[dict], cfg: FlagConfig, bases_cfg: BaseConfig) -> list[Base]:
    """Flags, oldest first: those that broke out in the last history_bars sessions, then the one still
    forming at the last bar (if any)."""
    n = len(candles)
    if n < cfg.pole_min_bars + cfg.flag_min_bars + 3:
        return []
    dates = [c["date"] for c in candles]
    high = np.array([c["high"] for c in candles], dtype=float)
    low = np.array([c["low"] for c in candles], dtype=float)
    close = np.array([c["close"] for c in candles], dtype=float)
    vol = np.array([c["volume"] for c in candles], dtype=float)

    out: list[Base] = []
    t, last_bo = max(cfg.pole_min_bars + cfg.flag_min_bars + 2, n - cfg.history_bars), -1
    while t < n:
        # necessary for a breakout at t: a close above the last flag_min_bars highs (all inside the flag)
        if t - 1 > last_bo and close[t] > high[t - cfg.flag_min_bars:t].max():
            f = find_flag(high, low, close, vol, t - 1, cfg)
            if f is not None and close[t] > f["pivot"] and f["P"] > last_bo:
                base = _as_base(f, dates, "broken_out", cfg)
                base.breakout = breakout_metrics(t, f["pivot"], dates, high, low, close, vol, bases_cfg)
                out.append(base)
                last_bo = t
        t += 1

    f = find_flag(high, low, close, vol, n - 1, cfg)
    if f is not None and f["P"] > last_bo and close[-1] <= f["pivot"]:
        base = _as_base(f, dates, "forming", cfg)
        base.dist_to_pivot_pct = _r((f["pivot"] - close[-1]) / f["pivot"] * 100)
        out.append(base)
    return out


def flag_score(shape: dict, rs: Optional[int], cfg: FlagConfig) -> float:
    """0-100: flag tightness 30, volume dry-up 20, pole strength 20, RS 20, flag length 10."""
    f, p = shape["flag"], shape["pole"]
    tight = 30 * max(0.0, min(1.0, (cfg.max_retrace * 100 - (f["retrace_pct"] or 0))
                              / ((cfg.max_retrace - cfg.tight_retrace) * 100)))
    fx = f["volume_x"] if f["volume_x"] is not None else cfg.max_flag_volume
    dry = 20 * max(0.0, min(1.0, (cfg.max_flag_volume - fx) / (cfg.max_flag_volume - 0.3)))
    pole = 20 * max(0.0, min(1.0, ((p["gain_pct"] or 0) - cfg.min_pole_gain * 100) / 40))    # +60%: full marks
    if shape.get("htf"):
        pole = 20.0
    length = 10 if 5 <= f["bars"] <= 15 else 5
    return round(tight + dry + pole + length + 20 * (rs or 0) / 99, 1)
