"""₿ Pivot levels by code (pure): TradingView's ta.pivot_point_levels formulas from the PREVIOUS closed period's high,
low and close (Woodie also reads the current period's open, DM the previous period's open).

  candles  [{t (unix s, the candle's open, UTC), o, h, l, c}] oldest first
  period   the pivot timeframe: daily | weekly | monthly; "auto" = daily for tf ≤ 15m, weekly above
  boundary "utc" (Delta's day starts 00:00 UTC = 5:30 AM IST; TradingView's BTCUSD on Delta) | "ist" (midnight IST)

Levels used for the patterns: S1–S3 (bullish), R1–R3 (bearish); P only when crypto.use_p."""

from datetime import datetime, timedelta, timezone
from typing import Optional

IST_OFFSET = 5 * 3600 + 1800
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
NAMES = ("P", "R1", "R2", "R3", "R4", "R5", "S1", "S2", "S3", "S4", "S5")


def auto_period(tf: str, pivot_tf: str = "auto") -> str:
    """auto: daily for a timeframe up to 15 minutes, weekly above."""
    if pivot_tf != "auto":
        return pivot_tf
    return "daily" if TF_SECONDS.get(tf, 300) <= 900 else "weekly"


def period_key(t: int, period: str, boundary: str = "utc") -> str:
    """The period a candle opening at unix `t` belongs to: '2026-11-24' (daily), '2026-W48' (weekly, Monday start),
    '2026-11' (monthly), on the UTC or the IST calendar."""
    d = datetime.fromtimestamp(int(t) + (IST_OFFSET if boundary == "ist" else 0), timezone.utc).date()
    if period == "weekly":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    if period == "monthly":
        return f"{d.year}-{d.month:02d}"
    return d.isoformat()


def period_start(t: int, period: str, boundary: str = "utc") -> int:
    """Unix start of the period holding `t`."""
    off = IST_OFFSET if boundary == "ist" else 0
    d = datetime.fromtimestamp(int(t) + off, timezone.utc).date()
    if period == "weekly":
        d = d - timedelta(days=d.weekday())
    elif period == "monthly":
        d = d.replace(day=1)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) - off


def aggregate(candles: list[dict], period: str, boundary: str = "utc") -> list[dict]:
    """Candles -> one bar per period {key, t (period start), o, h, l, c}, oldest first."""
    out: dict[str, dict] = {}
    for b in sorted(candles, key=lambda x: x["t"]):
        k = period_key(b["t"], period, boundary)
        x = out.get(k)
        if x is None:
            out[k] = {"key": k, "t": period_start(b["t"], period, boundary), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
        else:
            x["h"], x["l"], x["c"] = max(x["h"], b["h"]), min(x["l"], b["l"]), b["c"]
    return [out[k] for k in sorted(out, key=lambda k: out[k]["t"])]


def levels(kind: str, h: float, l: float, c: float, o: Optional[float] = None, cur_open: Optional[float] = None) -> dict:
    """{P, R1…R5, S1…S5} (the ones the type has) from one period's H / L / C (TradingView's formulas)."""
    kind = (kind or "traditional").lower()
    rng = h - l
    p = (h + l + c) / 3
    if kind == "traditional":
        out = {"P": p, "R1": 2 * p - l, "S1": 2 * p - h, "R2": p + rng, "S2": p - rng, "R3": 2 * p + (h - 2 * l), "S3": 2 * p - (2 * h - l),
               "R4": 3 * p + (h - 3 * l), "S4": 3 * p - (3 * h - l), "R5": 4 * p + (h - 4 * l), "S5": 4 * p - (4 * h - l)}
    elif kind == "fibonacci":
        out = {"P": p, "R1": p + 0.382 * rng, "S1": p - 0.382 * rng, "R2": p + 0.618 * rng, "S2": p - 0.618 * rng, "R3": p + rng, "S3": p - rng}
    elif kind == "woodie":
        op = cur_open if cur_open is not None else c
        p = (h + l + 2 * op) / 4
        r3, s3 = h + 2 * (p - l), l - 2 * (h - p)
        out = {"P": p, "R1": 2 * p - l, "S1": 2 * p - h, "R2": p + rng, "S2": p - rng, "R3": r3, "S3": s3, "R4": r3 + rng, "S4": s3 - rng}
    elif kind == "classic":
        out = {"P": p, "R1": 2 * p - l, "S1": 2 * p - h, "R2": p + rng, "S2": p - rng, "R3": p + 2 * rng, "S3": p - 2 * rng,
               "R4": p + 3 * rng, "S4": p - 3 * rng}
    elif kind == "camarilla":
        r = {i: c + 1.1 * rng / d for i, d in ((1, 12), (2, 6), (3, 4), (4, 2))}
        s = {i: c - 1.1 * rng / d for i, d in ((1, 12), (2, 6), (3, 4), (4, 2))}
        out = {"P": p, **{f"R{i}": v for i, v in r.items()}, **{f"S{i}": v for i, v in s.items()},
               "R5": r[4] + 1.168 * (r[4] - r[3]), "S5": s[4] - 1.168 * (s[3] - s[4])}
    elif kind == "dm":
        op = o if o is not None else c
        x = h + 2 * l + c if c < op else 2 * h + l + c if c > op else h + l + 2 * c
        out = {"P": x / 4, "R1": x / 2 - l, "S1": x / 2 - h}
    else:
        raise ValueError(f"unknown pivot type {kind}")
    return {k: round(v, 2) for k, v in out.items()}


def levels_at(bars: list[dict], t: int, period: str, boundary: str = "utc", kind: str = "traditional") -> Optional[dict]:
    """The levels in force for a candle opening at `t`: from the last period bar CLOSED before t's period.
    `bars` are period bars (aggregate()) or daily candles (re-aggregated for weekly / monthly). {period, from (key),
    levels} or None without a previous period."""
    if period != "daily" or any("key" not in b for b in bars):
        bars = aggregate(bars, period, boundary)
    start = period_start(t, period, boundary)
    prev = [b for b in bars if b["t"] < start]
    if not prev:
        return None
    pb = prev[-1]
    cur = next((b for b in bars if b["t"] == start), None)
    return {"period": period, "from": pb.get("key") or period_key(pb["t"], period, boundary), "start": start,
            "levels": levels(kind, pb["h"], pb["l"], pb["c"], pb["o"], cur["o"] if cur else None)}


def pattern_levels(lv: dict, use_p: bool = False) -> dict:
    """{'long': [(name, px)], 'short': [...]}: S1–S3 for bullish patterns, R1–R3 for bearish; P for both when asked."""
    sup = [(n, lv[n]) for n in ("S1", "S2", "S3") if n in lv]
    res = [(n, lv[n]) for n in ("R1", "R2", "R3") if n in lv]
    if use_p and "P" in lv:
        sup.append(("P", lv["P"]))
        res.append(("P", lv["P"]))
    return {"long": sup, "short": res}
