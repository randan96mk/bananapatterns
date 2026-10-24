"""Intraday breakout rules (pure, except the day flags): volume projection, volume verdict,
the 15:15 close verdict, and per-day flags (crossed / failed / close_*) kept in the DB so a
restart mid-day neither repeats nor forgets an alert.

Volume projection: NSE volume is heavy at the open and the close, so a straight-line
projection overstates morning volume. VOLUME_CURVE is a typical cumulative share of the
day's volume by minutes since 09:15 (approximate; tune later from intraday data).
"""

from datetime import datetime, time
from typing import Optional

from backend.config import AlertsConfig
from backend.database.models import now_ist

SESSION_START = time(9, 15)
VOLUME_CURVE = [(0, 0.0), (15, 0.10), (45, 0.25), (105, 0.38), (195, 0.50), (255, 0.62), (315, 0.75),
                (360, 0.90), (375, 1.0)]          # 09:15, 09:30, 10:00, 11:00, 12:30, 13:30, 14:30, 15:15, 15:30


def day_share(now: datetime) -> float:
    """Typical share of the day's volume traded by `now` (0..1)."""
    start = datetime.combine(now.date(), SESSION_START, now.tzinfo)
    m = (now - start).total_seconds() / 60
    if m <= 0:
        return 0.0
    for (m0, s0), (m1, s1) in zip(VOLUME_CURVE, VOLUME_CURVE[1:]):
        if m <= m1:
            return s0 + (s1 - s0) * (m - m0) / (m1 - m0)
    return 1.0


def volume_pace(vol_so_far: Optional[float], avg_vol: Optional[float], now: datetime) -> Optional[float]:
    """Projected full-day volume as a multiple of the average (None in the first minutes)."""
    share = day_share(now)
    if not vol_so_far or not avg_vol or share < 0.05:
        return None
    return vol_so_far / share / avg_vol


def volume_verdict(pace: Optional[float], need: float) -> str:
    """strong (>= need) / average (>= 1x) / weak / unknown."""
    if pace is None:
        return "unknown"
    return "strong" if pace >= need else "average" if pace >= 1.0 else "weak"


def day_range(ltp: float, low: float, high: float) -> Optional[float]:
    """Where the price sits in today's range: 0 = low, 1 = high."""
    return (ltp - low) / (high - low) if high > low else None


def close_verdict(ltp: float, pivot: float, zone_top: float, pace: Optional[float], rng: Optional[float],
                  cfg: AlertsConfig) -> Optional[str]:
    """15:15 verdict for a stock above its pivot: holding / weak / extended (None when not above it)."""
    if ltp <= pivot:
        return None
    if ltp > zone_top:
        return "extended"
    strong_volume = pace is not None and pace >= cfg.intraday_volume_mult
    strong_close = rng is not None and rng >= cfg.close_min_range
    return "holding" if strong_volume and strong_close else "weak"


# --- per-day flags ---------------------------------------------------------------

def set_flag(conn, day: str, symbol: str, flag: str, detail: str = "") -> bool:
    """Record a flag once; True if it is new."""
    cur = conn.execute("INSERT OR IGNORE INTO intraday_flags (day, symbol, flag, at, detail) VALUES (?, ?, ?, ?, ?)",
                       (day, symbol, flag, now_ist(), detail))
    return cur.rowcount == 1


def flags(conn, day: str, symbol: Optional[str] = None) -> dict[str, set]:
    """{symbol: {flags}} for a day."""
    q, args = "SELECT symbol, flag FROM intraday_flags WHERE day = ?", [day]
    if symbol:
        q, args = q + " AND symbol = ?", args + [symbol]
    out: dict[str, set] = {}
    for r in conn.execute(q, args):
        out.setdefault(r["symbol"], set()).add(r["flag"])
    return out
