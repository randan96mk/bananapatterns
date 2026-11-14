"""The bias rules, as pure functions on 1-minute bars ({t: 'HH:MM', o, h, l, c, v}): the opening range (NIFTY 50
index, 9:15-9:30), the VWAP (the near-month NIFTY future: the index has no volume), the previous day's high / low /
close from the stored daily NIFTY candles, the gap at the open, the 5-minute close test and the day filters.

  BULLISH  a 5-minute close above the range high AND above the VWAP AND at or over the previous day's high
  BEARISH  the mirror: below the range low AND below the VWAP AND at or under the previous day's low
Looked for on 5-minute closes inside the window only (09:45-11:00 by default)."""

from datetime import date, time
from typing import Optional

from backend.intraday.candles import resample

RANGE_START, RANGE_END = "09:15", "09:30"
BULLISH, BEARISH = "BULLISH", "BEARISH"


def hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def opening_range(bars: list[dict], start: str = RANGE_START, end: str = RANGE_END) -> Optional[dict]:
    """{high, low, bars} of the index 1-minute bars with start <= t < end; None without bars."""
    rng = [b for b in bars if start <= b["t"] < end]
    if not rng:
        return None
    return {"high": max(b["h"] for b in rng), "low": min(b["l"] for b in rng), "bars": len(rng)}


def vwap(bars: list[dict], until: Optional[str] = None) -> Optional[float]:
    """The volume-weighted average of the typical price (h+l+c)/3 over the futures bars with t < until (all bars
    without it); None without volume."""
    pv = vol = 0.0
    for b in bars:
        if until and b["t"] >= until:
            break
        v = float(b.get("v") or 0)
        pv += (b["h"] + b["l"] + b["c"]) / 3 * v
        vol += v
    return round(pv / vol, 2) if vol else None


def prev_day_levels(daily: list[dict], today: str) -> Optional[dict]:
    """{date, high, low, close} of the last stored daily bar before `today`."""
    rows = [r for r in daily if r["date"] < today]
    if not rows:
        return None
    r = rows[-1]
    return {"date": r["date"], "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"])}


def gap_pct(open_px: Optional[float], prev_close: Optional[float]) -> Optional[float]:
    if not open_px or not prev_close:
        return None
    return round((open_px / prev_close - 1) * 100, 2)


def last_closed_5m(bars: list[dict], at: str) -> Optional[dict]:
    """The last complete 5-minute bar at the minute `at` (the bar starting 5 minutes before a slot boundary)."""
    five = [b for b in resample(bars, 5) if b["t"] < at]
    if not five:
        return None
    last = five[-1]
    slot_end = _add(last["t"], 5)
    return last if slot_end <= at else (five[-2] if len(five) > 1 else None)


def _add(t: str, minutes: int) -> str:
    m = int(t[:2]) * 60 + int(t[3:5]) + minutes
    return f"{m // 60:02d}:{m % 60:02d}"


def test_bias(close: float, rng: dict, vw: Optional[float], prev: Optional[dict]) -> dict:
    """The three checks on one 5-minute close: {bias: BULLISH|BEARISH|None, checks: {range, vwap, prev}} where each
    check is +1 (bullish side), -1 (bearish side) or 0."""
    r = 1 if close > rng["high"] else -1 if close < rng["low"] else 0
    v = 0 if vw is None else 1 if close > vw else -1 if close < vw else 0
    p = 0 if prev is None else 1 if close >= prev["high"] else -1 if close <= prev["low"] else 0
    bias = BULLISH if r == v == p == 1 else BEARISH if r == v == p == -1 else None
    return {"bias": bias, "checks": {"range": r, "vwap": v, "prev": p}}


def event_days(spec: str) -> list[str]:
    """The settings' comma-separated ISO dates, cleaned."""
    out = []
    for x in (spec or "").replace(";", ",").split(","):
        x = x.strip()[:10]
        try:
            out.append(date.fromisoformat(x).isoformat())
        except ValueError:
            continue
    return sorted(set(out))


def is_event_day(day: str, spec: str) -> bool:
    return day in event_days(spec)


def in_window(t: time, start: str, end: str) -> bool:
    """Inside the entry window [start, end] (the end's 5-minute close counts)."""
    return hm(start) <= t <= hm(end)


def is_5m_close(t: time) -> bool:
    return t.minute % 5 == 0


def slot_of(t: time) -> str:
    """The 5-minute slot boundary at or before t, 'HH:MM'."""
    return f"{t.hour:02d}:{t.minute - t.minute % 5:02d}"
