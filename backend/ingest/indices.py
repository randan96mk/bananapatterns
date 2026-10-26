"""The index ticker bar: Nifty 50, Nifty 500, Bank Nifty, India VIX and Sensex.

One Kite quote call for all five, cached QUOTE_TTL seconds (the dashboard polls every 30 s while open, so
nothing runs when it isn't). Each index also gets:
  spark   today's 5-minute closes (the last session's when the market is shut), refreshed every
          SPARK_TTL seconds while open, once otherwise; the live price is appended
  ma50    the 50-day average from daily candles kept in candles_daily, topped up once a day from Kite
          history (three years the first time, so the index chart opens like a stock's)
Without a Kite login the bar falls back to the last two stored daily closes.
"""

import threading
import time as _time
from datetime import date, datetime, time, timedelta
from typing import Optional

from backend.config import IST
from backend.database.models import get_candles, get_db, upsert_candles
from backend.ingest.holidays import is_trading_day
from backend.logging_config.logger import setup_logger

logger = setup_logger("indices")

# (symbol as stored / charted, Kite quote key, label)
TICKER = [("NIFTY 50", "NSE:NIFTY 50", "Nifty 50"),
          ("NIFTY 500", "NSE:NIFTY 500", "Nifty 500"),
          ("NIFTY BANK", "NSE:NIFTY BANK", "Bank Nifty"),
          ("INDIA VIX", "NSE:INDIA VIX", "India VIX"),
          ("SENSEX", "BSE:SENSEX", "Sensex")]
QUOTE_TTL, SPARK_TTL = 25, 300
OPEN, CLOSE, PRE_OPEN = time(9, 15), time(15, 30), time(9, 0)
HISTORY_DAYS = 1100


def market_status(now: datetime) -> str:
    """open / pre_open / closed / holiday (NSE equity hours, IST)."""
    if not is_trading_day(now.date()):
        return "holiday"
    t = now.time()
    if OPEN <= t <= CLOSE:
        return "open"
    if PRE_OPEN <= t < OPEN:
        return "pre_open"
    return "closed"


def last_session(now: datetime) -> date:
    """Today once the market has opened, else the previous trading day."""
    d = now.date()
    if is_trading_day(d) and now.time() >= OPEN:
        return d
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def _ensure_instrument(conn, symbol: str, key: str, token: int) -> None:
    """An instruments row (segment INDICES) so the chart window can open the index like a stock."""
    if conn.execute("SELECT 1 FROM instruments WHERE instrument_token = ?", (token,)).fetchone():
        return
    conn.execute("INSERT OR REPLACE INTO instruments (instrument_token, tradingsymbol, name, exchange, segment, "
                 "instrument_type, fetched_on) VALUES (?, ?, ?, ?, 'INDICES', 'INDEX', ?)",
                 (token, symbol, symbol, key.split(":")[0], datetime.now(IST).date().isoformat()))


class IndexTicker:
    def __init__(self, client, clock=lambda: datetime.now(IST)):
        self.client, self.clock = client, clock
        self.lock = threading.Lock()
        self.quotes: dict = {}
        self.quoted_at = 0.0
        self.spark: dict[str, dict] = {}            # symbol -> {day, at, points}
        self.history_day: dict[str, str] = {}       # symbol -> date the daily candles were last topped up

    # --- data ---

    def _quotes(self, now_ts: float) -> dict:
        if self.quotes and now_ts - self.quoted_at < QUOTE_TTL:
            return self.quotes
        self.quotes = self.client.quote([k for _, k, _ in TICKER]) or {}
        self.quoted_at = now_ts
        return self.quotes

    def _top_up_history(self, symbol: str, token: int, session: date) -> None:
        if self.history_day.get(symbol) == session.isoformat():
            return
        with get_db() as conn:
            last = get_candles(conn, token, limit=1)
        start = (date.fromisoformat(last[-1]["date"]) + timedelta(days=1)) if last \
            else session - timedelta(days=HISTORY_DAYS)
        end = session - timedelta(days=1)                       # today's bar comes from the live quote
        if start <= end:
            rows = self.client.historical_daily(token, start, end, label=symbol)
            if rows:
                with get_db() as conn:
                    upsert_candles(conn, token, rows)
        self.history_day[symbol] = session.isoformat()

    def _spark(self, symbol: str, token: int, session: date, status: str, now_ts: float) -> list[float]:
        s = self.spark.get(symbol)
        fresh = s and s["day"] == session.isoformat() and (status != "open" or now_ts - s["at"] < SPARK_TTL)
        if not fresh:
            rows = self.client.historical_intraday(token, session, label=symbol)
            s = {"day": session.isoformat(), "at": now_ts, "points": [r["close"] for r in rows]}
            self.spark[symbol] = s
        return list(s["points"])

    # --- the bar ---

    def snapshot(self, live: bool) -> dict:
        now = self.clock()
        status = market_status(now)
        with self.lock:
            if live:
                try:
                    return self._live(now, status)
                except Exception as exc:                        # Kite hiccup: show the stored closes
                    logger.warning("Index ticker live data failed: %s", exc)
                    out = self._stored(now, status)
                    out["error"] = str(exc)[:200]
                    return out
            return self._stored(now, status)

    def _live(self, now: datetime, status: str) -> dict:
        now_ts = _time.time()
        q = self._quotes(now_ts)
        session = last_session(now)
        items = []
        for symbol, key, label in TICKER:
            d = q.get(key)
            if not d:
                items.append({"symbol": symbol, "label": label, "missing": True})
                continue
            token, ltp = int(d["instrument_token"]), float(d["last_price"])
            prev = float((d.get("ohlc") or {}).get("close") or 0) or None
            with get_db() as conn:
                _ensure_instrument(conn, symbol, key, token)
            ma50 = spark = None
            try:
                self._top_up_history(symbol, token, session)
                with get_db() as conn:
                    closes = [c["close"] for c in get_candles(conn, token, limit=60)
                              if c["date"] < session.isoformat()]
                closes = (closes + [ltp])[-50:]
                ma50 = round(sum(closes) / 50, 2) if len(closes) == 50 else None
                spark = self._spark(symbol, token, session, status, now_ts)
                if status == "open" and spark:
                    spark.append(ltp)
            except Exception as exc:                            # history is a nicety: keep the price
                logger.info("Index %s history: %s", symbol, exc)
            items.append(_item(symbol, label, ltp, prev, ma50, spark))
        return {"status": status, "live": True, "as_of": now.isoformat(timespec="seconds"), "items": items}

    def _stored(self, now: datetime, status: str) -> dict:
        """No login: the last stored daily close vs the one before, for the indices we have candles for."""
        items = []
        with get_db() as conn:
            for symbol, key, label in TICKER:
                row = conn.execute("SELECT instrument_token FROM instruments WHERE tradingsymbol = ? AND "
                                   "segment = 'INDICES'", (symbol,)).fetchone()
                cs = get_candles(conn, row["instrument_token"], limit=50) if row else []
                if len(cs) < 2:
                    items.append({"symbol": symbol, "label": label, "missing": True})
                    continue
                closes = [c["close"] for c in cs]
                ma50 = round(sum(closes) / 50, 2) if len(closes) == 50 else None
                items.append(dict(_item(symbol, label, closes[-1], closes[-2], ma50, None), date=cs[-1]["date"]))
        return {"status": status, "live": False, "as_of": now.isoformat(timespec="seconds"), "items": items}


def _item(symbol: str, label: str, ltp: float, prev: Optional[float], ma50: Optional[float],
          spark: Optional[list]) -> dict:
    change = round(ltp - prev, 2) if prev else None
    return {"symbol": symbol, "label": label, "last": round(ltp, 2), "prev_close": prev, "change": change,
            "change_pct": round(change / prev * 100, 2) if prev else None, "ma50": ma50,
            "above_ma50": (ltp > ma50) if ma50 else None, "spark": spark}
