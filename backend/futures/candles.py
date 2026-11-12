"""Continuous daily futures candles with open interest, per underlying, in `candles_fut_daily`.

Kite's `historical_data(near-month token, ..., "day", continuous=True, oi=True)` stitches the near contract of each
day into one series; it is NOT back-adjusted, so the day after an expiry carries the basis jump of the roll. Bars are
stored as they come and `roll_jump()` lists those jumps for the backtest to use as the roll cost. The sync is
incremental (last stored date minus `overlap_bars` days; `years` back the first time) and never deletes.
"""

from calendar import monthrange
from datetime import date, timedelta
from typing import Optional

from backend.database.models import get_db, now_ist
from backend.futures import instruments
from backend.ingest.holidays import is_trading_day
from backend.kite_api.client import KiteAuthError, KitePermissionError, _candle_date, _date_chunks
from backend.logging_config.logger import setup_logger

logger = setup_logger("futures")

OVERLAP_BARS = 5
TUESDAY_EXPIRY_FROM = date(2025, 9, 1)      # NSE moved stock derivatives' monthly expiry from the last Thursday to Tuesday


def fetch(client, token: int, start: date, end: date, label: str = "") -> list[dict]:
    """Continuous daily bars with OI for [start, end], through the client's historical token bucket and retries."""
    by_date: dict[str, dict] = {}
    for c_start, c_end in _date_chunks(start, end, client.ingest.chunk_days):
        rows = client._call(client.hist_bucket, f"historical_data({label or token} {c_start}..{c_end} continuous)",
                            client.kite.historical_data, token, f"{c_start} 00:00:00", f"{c_end} 23:59:59", "day",
                            continuous=True, oi=True)
        for r in rows or []:
            d = _candle_date(r["date"])
            by_date[d] = {"date": d, "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
                          "close": float(r["close"]), "volume": int(r.get("volume") or 0), "oi": int(r.get("oi") or 0)}
    return [by_date[d] for d in sorted(by_date)]


def last_date(conn, underlying: str) -> Optional[str]:
    return conn.execute("SELECT MAX(date) FROM candles_fut_daily WHERE underlying = ?", (underlying,)).fetchone()[0]


def upsert(conn, underlying: str, bars: list[dict]) -> int:
    conn.executemany("INSERT OR REPLACE INTO candles_fut_daily (underlying, date, open, high, low, close, volume, oi) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     [(underlying, b["date"], b["open"], b["high"], b["low"], b["close"], b["volume"], b["oi"]) for b in bars])
    return len(bars)


def sync(client, underlyings: list[str], years: int = 3, end: Optional[date] = None,
         overlap_bars: int = OVERLAP_BARS) -> dict:
    """Bring each underlying's continuous series up to `end` (today). Returns {underlyings, synced, added, failed:
    [{symbol, error}], to}. Login / permission errors abort the run; other errors mark the symbol failed."""
    end = end or date.fromisoformat(now_ist()[:10])
    added, synced, failed, to = 0, 0, [], None
    for sym in underlyings:
        near = instruments.near(sym)
        if not near:
            failed.append({"symbol": sym, "error": "no contract"})
            continue
        with get_db() as conn:
            last = last_date(conn, sym)
        start = (date.fromisoformat(last) - timedelta(days=overlap_bars)) if last else end - timedelta(days=int(years * 365.25))
        if start > end:
            synced += 1
            continue
        try:
            bars = fetch(client, near["token"], start, end, label=near["tradingsymbol"])
        except (KiteAuthError, KitePermissionError):
            raise
        except Exception as exc:
            logger.warning("Futures candles %s failed: %s", sym, exc)
            failed.append({"symbol": sym, "error": str(exc)[:200]})
            continue
        new = [b for b in bars if not last or b["date"] > last]
        with get_db() as conn:
            upsert(conn, sym, bars)                       # overlap rows are rewritten, nothing is removed
        added += len(new)
        synced += 1
        if bars:
            to = max(to or "", bars[-1]["date"])
    logger.info("Futures candles: %d/%d synced, %d new bars, %d failed", synced, len(underlyings), added, len(failed))
    return {"underlyings": len(underlyings), "synced": synced, "added": added, "failed": failed, "to": to}


def get(underlying: str, limit: Optional[int] = None, start: Optional[str] = None) -> list[dict]:
    """Bars ascending [{date, open, high, low, close, volume, oi}]; `limit` keeps the most recent N."""
    q, params = "SELECT date, open, high, low, close, volume, oi FROM candles_fut_daily WHERE underlying = ?", [underlying.upper()]
    if start:
        q += " AND date >= ?"
        params.append(start)
    if limit:
        q = f"SELECT * FROM ({q} ORDER BY date DESC LIMIT ?) ORDER BY date"
        params.append(limit)
    else:
        q += " ORDER BY date"
    with get_db() as conn:
        return [dict(r) for r in conn.execute(q, params)]


def status() -> dict:
    """{underlyings, to, bars} over the whole table."""
    with get_db() as conn:
        r = conn.execute("SELECT COUNT(DISTINCT underlying), MAX(date), COUNT(*) FROM candles_fut_daily").fetchone()
    return {"underlyings": r[0], "to": r[1], "bars": r[2]}


def monthly_expiry(year: int, month: int) -> date:
    """The exchange's monthly stock-futures expiry: the last Thursday (last Tuesday from Sep 2025), moved to the previous
    trading day when it falls on a holiday."""
    last = date(year, month, monthrange(year, month)[1])
    weekday = 1 if last >= TUESDAY_EXPIRY_FROM else 3
    d = last - timedelta(days=(last.weekday() - weekday) % 7)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def expiry_days(start: str, end: str) -> list[str]:
    """Expiry dates in [start, end]: every expiry ever stored in futures_contracts, plus the calendar rule for the months
    none is stored for."""
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    with get_db() as conn:
        known = {r[0] for r in conn.execute("SELECT DISTINCT expiry FROM futures_contracts WHERE expiry BETWEEN ? AND ?",
                                            (start, end))}
    months_known = {k[:7] for k in known}
    y, m = s.year, s.month
    while (y, m) <= (e.year, e.month):
        if f"{y:04d}-{m:02d}" not in months_known:
            x = monthly_expiry(y, m)
            if s <= x <= e:
                known.add(x.isoformat())
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sorted(known)


def roll_jump(underlying: str) -> list[dict]:
    """The continuous series' jumps at each roll: for every expiry day in the stored range, the first bar after it
    against the expiry-day close. [{expiry, date, prev_close, open, close, jump_pct (open / prev close − 1, %)}].
    The jump mixes the basis with the overnight cash move; the backtest treats it as the roll cost."""
    bars = get(underlying)
    if len(bars) < 2:
        return []
    exps = expiry_days(bars[0]["date"], bars[-1]["date"])
    out, i = [], 0
    for x in exps:
        while i < len(bars) and bars[i]["date"] <= x:
            i += 1
        if i == 0 or i >= len(bars):
            continue
        prev, nxt = bars[i - 1], bars[i]
        if prev["date"] < (date.fromisoformat(x) - timedelta(days=7)).isoformat():
            continue                                    # a gap in the data around that expiry: no measurable jump
        out.append({"expiry": x, "date": nxt["date"], "prev_close": prev["close"], "open": nxt["open"], "close": nxt["close"],
                    "jump_pct": round((nxt["open"] / prev["close"] - 1) * 100, 3) if prev["close"] else None})
    return out
