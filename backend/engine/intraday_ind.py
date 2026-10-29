"""Intraday indicators on 5-minute candles ({t 'HH:MM', open, high, low, close, volume}, one session, ascending):
session VWAP, the opening range, the VWAP slope, relative volume, and the data checks. Completed candles only:
a value at candle i uses candles up to and including i."""

from datetime import datetime, timedelta
from typing import Optional


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def bad_session(bars: list[dict], start: str = "09:15", until: Optional[str] = None) -> Optional[str]:
    """Why a session's candles can't be trusted (None = fine): every 5-minute candle from `start` (to the candle
    starting at `until`, when given) present once and in order, positive prices, high / low around open and close,
    volume not negative. Bad data means no signal that day."""
    if not bars:
        return "no candles"
    want = _minutes(start)
    for b in bars:
        if until is not None and _minutes(b["t"]) > _minutes(until):
            break
        if _minutes(b["t"]) != want:
            return f"candle missing or out of order at {b['t']}"
        o, h, lo, c = b["open"], b["high"], b["low"], b["close"]
        if min(o, h, lo, c) <= 0 or h < max(o, c) or lo > min(o, c) or (b.get("volume") or 0) < 0:
            return f"bad candle at {b['t']}"
        want += 5
    if until is not None and want <= _minutes(until):
        return f"candles stop before {until}"
    return None


def vwap(bars: list[dict]) -> list[Optional[float]]:
    """Session VWAP after each candle: cumulative typical price (H+L+C)/3 x volume over cumulative volume. Reset
    every session (pass one session). None until some volume has traded."""
    pv = vol = 0.0
    out = []
    for b in bars:
        v = b.get("volume") or 0
        pv += (b["high"] + b["low"] + b["close"]) / 3 * v
        vol += v
        out.append(pv / vol if vol > 0 else None)
    return out


def opening_range(bars: list[dict], start: str = "09:15", end: str = "09:30") -> Optional[tuple[float, float]]:
    """(high, low) of the candles starting in [start, end); None unless every one of them is there."""
    s, e = _minutes(start), _minutes(end)
    inside = [b for b in bars if s <= _minutes(b["t"]) < e]
    if len(inside) != (e - s) // 5 or not inside:
        return None
    return max(b["high"] for b in inside), min(b["low"] for b in inside)


def vwap_slope(vw: list[Optional[float]], i: int, n: int = 3) -> Optional[float]:
    """VWAP now minus n candles earlier (same session); None when either is missing."""
    if i - n < 0 or vw[i] is None or vw[i - n] is None:
        return None
    return vw[i] - vw[i - n]


def rel_volume(volumes: list[float], i: int, n: int = 20) -> Optional[float]:
    """Candle i's volume over the average of the n candles before it (they may run back into the previous session:
    pass the previous session's candles first). None with fewer than n before it or no volume in them."""
    if i < n:
        return None
    avg = sum(volumes[i - n:i]) / n
    return volumes[i] / avg if avg > 0 else None


def plus(hhmm: str, minutes: int) -> str:
    return (datetime(2000, 1, 1, *map(int, hhmm.split(":"))) + timedelta(minutes=minutes)).strftime("%H:%M")
