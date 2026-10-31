"""Saved intraday candles: one compressed row per stock per day (1-minute bars; days older than the retention are
shrunk to 5-minute bars). Fetched from Kite once, never again: a day is marked complete once saved."""

import json
import zlib
from datetime import date, timedelta
from typing import Optional

from backend.database.models import get_db

FULL_DAY = 375                       # 1-minute bars from 9:15 to 15:29
MIN_COMPLETE = 360                   # a day with fewer is checked against Kite at the day close


def _pack(bars: list[dict]) -> bytes:
    return zlib.compress(json.dumps(bars, separators=(",", ":")).encode(), 6)


def _unpack(blob: bytes) -> list[dict]:
    return json.loads(zlib.decompress(blob))


def save(token: int, day: str, bars: list[dict], source: str, interval: int = 1) -> None:
    complete = interval > 1 or len(bars) >= MIN_COMPLETE
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO candles_intraday (instrument_token, day, interval, bars, n, source, complete) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (token, day, interval, _pack(bars), len(bars), source, int(complete)))


def load(token: int, day: str) -> Optional[list[dict]]:
    with get_db() as conn:
        r = conn.execute("SELECT bars FROM candles_intraday WHERE instrument_token = ? AND day = ?", (token, day)).fetchone()
    return _unpack(r[0]) if r else None


def saved_days(token: int, before: str, n: int) -> list[str]:
    with get_db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT day FROM candles_intraday WHERE instrument_token = ? AND day < ? ORDER BY day DESC LIMIT ?",
            (token, before, n))][::-1]


def history(token: int, before: str, days: int) -> list[dict]:
    """The bars of the last `days` saved sessions before `before`, oldest first (indicator warm-up)."""
    out = []
    for d in saved_days(token, before, days):
        out += load(token, d) or []
    return out


def missing(tokens: list[int], days: list[str]) -> list[tuple[int, str]]:
    """(token, day) pairs not saved yet, or saved incomplete."""
    with get_db() as conn:
        have = {(r[0], r[1]) for r in conn.execute(
            f"SELECT instrument_token, day FROM candles_intraday WHERE complete = 1 AND day IN ({','.join('?' * len(days))})",
            days)} if days else set()
    return [(t, d) for t in tokens for d in days if (t, d) not in have]


def from_kite(rows: list[dict]) -> dict[str, list[dict]]:
    """Kite historical minute rows -> {day: bars}."""
    out: dict[str, list[dict]] = {}
    for r in rows:
        dt = r["date"]
        d, t = dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        out.setdefault(d, []).append({"t": t, "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"],
                                      "v": int(r.get("volume") or 0)})
    return out


def fetch(client, token: int, days: list[str]) -> int:
    """Fetches the given days of 1-minute bars from Kite (one request per call, up to 60 days) and saves them."""
    if not days or client is None or not getattr(client, "has_token", False):
        return 0
    lo, hi = date.fromisoformat(min(days)), date.fromisoformat(max(days))
    rows = client.historical_minute(token, lo, hi)
    by_day = from_kite(rows)
    got = [d for d in days if d in by_day]
    for d in got:
        save(token, d, by_day[d], "kite")
    return len(got)


def prune(keep_days: int, today: str) -> int:
    """Days older than keep_days are shrunk from 1-minute to 5-minute bars (kept for long replays)."""
    from backend.intraday.candles import resample
    cut = (date.fromisoformat(today) - timedelta(days=keep_days)).isoformat()
    n = 0
    with get_db() as conn:
        rows = conn.execute("SELECT instrument_token, day, bars, source FROM candles_intraday WHERE interval = 1 AND day < ?",
                            (cut,)).fetchall()
    for token, d, blob, src in rows:
        save(token, d, resample(_unpack(blob), 5), src, interval=5)
        n += 1
    return n
