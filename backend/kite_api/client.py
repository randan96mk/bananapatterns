"""Kite Connect wrapper for market data.

Nothing here swallows errors:
  - TokenException      -> KiteAuthError (fatal: log in again; never retried)
  - PermissionException -> KitePermissionError (fatal: e.g. no historical-data plan)
  - network / 429 / 5xx -> retried with exponential backoff + jitter, then KiteRequestError
  - input / other       -> KiteRequestError (caller decides; ingest marks the symbol failed)
Historical calls share a token bucket (3 req/s default). No GTT code. Orders: only ⚡ Tips → Kite orders
(backend/tips/kite_orders.py) places, modifies and cancels, each tagged "bptips"; an order call is sent ONCE (never
retried: a retried order could be placed twice) and orders() is read-only (callers keep only their own order ids).
"""

import random
import time
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from backend.config import IST, IngestConfig, KiteConfig
from backend.kite_api.throttle import TokenBucket
from backend.logging_config.logger import setup_logger

logger = setup_logger("kite")

KITE_LOGIN_URL = "https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"


class KiteError(Exception):
    """Base for all errors raised by this wrapper."""


class KiteAuthError(KiteError):
    """Token missing/expired/invalid. The whole run must stop; user must log in."""


class KitePermissionError(KiteError):
    """API key lacks permission (e.g. no historical data on the Personal plan)."""


class KiteRequestError(KiteError):
    """A single request failed after retries (or was not retryable)."""


def _classify(exc: Exception) -> str:
    """Return 'auth' | 'permission' | 'retry' | 'fail'."""
    try:
        from kiteconnect import exceptions as kex
    except ImportError:  # pragma: no cover
        kex = None
    try:
        import requests
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return "retry"
    except ImportError:  # pragma: no cover
        pass
    if kex is not None:
        if isinstance(exc, kex.TokenException):
            return "auth"
        if isinstance(exc, kex.PermissionException):
            return "permission"
        code = getattr(exc, "code", None)
        if code == 429 or isinstance(exc, (kex.NetworkException, kex.DataException)):
            return "retry"
        if isinstance(exc, kex.GeneralException) and (code or 0) >= 500:
            return "retry"
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return "retry"
    return "fail"


def _date_chunks(start: date, end: date, chunk_days: int) -> list[tuple[date, date]]:
    """Inclusive [start, end] split into ranges of at most chunk_days days."""
    if start > end:
        return []
    chunks, cur = [], start
    while cur <= end:
        stop = min(end, cur + timedelta(days=chunk_days - 1))
        chunks.append((cur, stop))
        cur = stop + timedelta(days=1)
    return chunks


def _candle_date(value) -> str:
    if isinstance(value, datetime):
        return (value.astimezone(IST) if value.tzinfo else value).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


class KiteClient:
    def __init__(self, kite_config: KiteConfig, ingest_config: IngestConfig,
                 kite=None, sleep: Callable[[float], None] = time.sleep):
        self.config = kite_config
        self.ingest = ingest_config
        self._kite = kite
        self._sleep = sleep
        self.hist_bucket = TokenBucket(ingest_config.hist_rate_per_sec, sleep=sleep)
        self.api_bucket = TokenBucket(5.0, sleep=sleep)  # other endpoints allow 10/s
        self.quote_bucket = TokenBucket(1.0, sleep=sleep)  # quote API: 1 req/s

    # --- session ---

    @property
    def kite(self):
        if self._kite is None:
            from kiteconnect import KiteConnect
            self._kite = KiteConnect(api_key=self.config.api_key)
            if self.config.access_token:
                self._kite.set_access_token(self.config.access_token)
        return self._kite

    @property
    def has_token(self) -> bool:
        return bool(self.config.access_token)

    def login_url(self) -> str:
        return KITE_LOGIN_URL.format(api_key=self.config.api_key)

    def generate_session(self, request_token: str) -> dict:
        data = self._call(self.api_bucket, "generate_session",
                          self.kite.generate_session, request_token, api_secret=self.config.api_secret)
        self.set_access_token(data["access_token"])
        logger.info("Kite session generated for user %s", data.get("user_id", "?"))
        return data

    def set_access_token(self, token: str) -> None:
        self.config.access_token = token
        self.kite.set_access_token(token)

    def clear_access_token(self) -> None:
        self.config.access_token = ""
        if self._kite is not None:
            self._kite.set_access_token(None)

    def profile(self) -> dict:
        return self._call(self.api_bucket, "profile", self.kite.profile)

    # --- data ---

    def instruments(self, exchange: str) -> list[dict]:
        return self._call(self.api_bucket, f"instruments({exchange})", self.kite.instruments, exchange)

    def holdings(self) -> list[dict]:
        """Delivery holdings (read-only)."""
        return self._call(self.api_bucket, "holdings", self.kite.holdings)

    def positions(self) -> dict:
        """Today's net / day positions (read-only)."""
        return self._call(self.api_bucket, "positions", self.kite.positions)

    def quote(self, instruments: list[str]) -> dict:
        """Full quotes for up to 500 'NSE:SYMBOL' keys in one call (last_price, volume, ohlc...)."""
        if not instruments:
            return {}
        return self._call(self.quote_bucket, f"quote({len(instruments)})", self.kite.quote, instruments)

    def historical_daily(self, token: int, start: date, end: date, label: str = "") -> list[dict]:
        """Adjusted daily candles for [start, end], chunked to the API's range limit.

        Returns [{date: 'YYYY-MM-DD', open, high, low, close, volume}] ascending, de-duplicated.
        An empty list means Kite had no bars in range (e.g. not yet listed), not an error.
        """
        by_date: dict[str, dict] = {}
        for c_start, c_end in _date_chunks(start, end, self.ingest.chunk_days):
            rows = self._call(
                self.hist_bucket, f"historical_data({label or token} {c_start}..{c_end})",
                self.kite.historical_data, token,
                f"{c_start} 00:00:00", f"{c_end} 23:59:59", "day",
            )
            for r in rows:
                d = _candle_date(r["date"])
                by_date[d] = {"date": d, "open": float(r["open"]), "high": float(r["high"]),
                              "low": float(r["low"]), "close": float(r["close"]),
                              "volume": int(r.get("volume") or 0)}
        return [by_date[d] for d in sorted(by_date)]

    def historical_intraday(self, token: int, day: date, interval: str = "5minute", label: str = "") -> list[dict]:
        """One day's intraday candles [{time 'HH:MM', close, high, low, volume}] ascending (sparklines, opening range)."""
        rows = self._call(self.hist_bucket, f"historical_data({label or token} {day} {interval})",
                          self.kite.historical_data, token, f"{day} 09:00:00", f"{day} 15:35:00", interval)
        out = []
        for r in rows:
            t = r["date"]
            out.append({"time": t.strftime("%H:%M") if hasattr(t, "strftime") else str(t)[11:16],
                        "close": float(r["close"]), "high": float(r.get("high") or r["close"]),
                        "low": float(r.get("low") or r["close"]), "volume": r.get("volume") or 0})
        return out

    def historical_minute(self, token: int, start: date, end: date, label: str = "") -> list[dict]:
        """1-minute candles for the sessions [start, end] (60-day requests, Kite's minute-data limit)."""
        out = []
        for c_start, c_end in _date_chunks(start, end, 60):
            out += self._call(self.hist_bucket, f"historical_data({label or token} {c_start}..{c_end} minute)",
                              self.kite.historical_data, token, f"{c_start} 09:00:00", f"{c_end} 15:35:00", "minute")
        return out

    # --- orders (⚡ Tips → Kite orders only) ---

    ORDER_TAG = "bptips"

    def place_order(self, exchange: str, tradingsymbol: str, transaction_type: str, quantity: int, product: str,
                    order_type: str, price: Optional[float] = None, trigger_price: Optional[float] = None,
                    validity: str = "DAY", tag: str = ORDER_TAG) -> str:
        """One regular order; returns Kite's order_id. Sent once (an error is raised, never retried)."""
        kw = {"variety": "regular", "exchange": exchange, "tradingsymbol": tradingsymbol, "transaction_type": transaction_type,
              "quantity": int(quantity), "product": product, "order_type": order_type, "validity": validity, "tag": tag}
        if price is not None:
            kw["price"] = price
        if trigger_price is not None:
            kw["trigger_price"] = trigger_price
        out = self._call(self.api_bucket, f"place_order({tradingsymbol} {transaction_type} {quantity} {order_type})",
                         self.kite.place_order, _once=True, **kw)
        return str(out.get("order_id") if isinstance(out, dict) else out)

    def modify_order(self, order_id: str, **kw) -> str:
        """Change a pending order (quantity / price / trigger_price / order_type). Sent once."""
        kw = {k: v for k, v in kw.items() if v is not None}
        out = self._call(self.api_bucket, f"modify_order({order_id})", self.kite.modify_order, _once=True,
                         variety="regular", order_id=order_id, **kw)
        return str(out.get("order_id") if isinstance(out, dict) else (out or order_id))

    def cancel_order(self, order_id: str) -> str:
        """Cancel a pending order. Sent once."""
        out = self._call(self.api_bucket, f"cancel_order({order_id})", self.kite.cancel_order, _once=True,
                         variety="regular", order_id=order_id)
        return str(out.get("order_id") if isinstance(out, dict) else (out or order_id))

    def orders(self) -> list[dict]:
        """Today's order book (read-only; the caller keeps only its own order ids)."""
        return self._call(self.api_bucket, "orders", self.kite.orders) or []

    # --- core ---

    def _call(self, bucket: Optional[TokenBucket], what: str, fn, *args, _once: bool = False, **kwargs):
        attempts = 1 if _once else max(1, self.ingest.max_retries + 1)
        for attempt in range(attempts):
            if bucket is not None:
                bucket.acquire()
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # classified below, never swallowed
                kind = _classify(exc)
                if kind == "auth":
                    raise KiteAuthError(f"{what}: Kite token rejected ({exc}). Log in again.") from exc
                if kind == "permission":
                    raise KitePermissionError(f"{what}: permission denied ({exc}). "
                                              "Historical data needs the Kite Connect plan.") from exc
                if kind == "retry" and attempt < attempts - 1:
                    delay = self.ingest.backoff_base_sec * (2 ** attempt) * (1 + random.random() * 0.25)
                    logger.warning("%s failed (%s: %s); retry %d/%d in %.1fs", what, type(exc).__name__,
                                   exc, attempt + 1, attempts - 1, delay)
                    self._sleep(delay)
                    continue
                raise KiteRequestError(f"{what}: {type(exc).__name__}: {exc}") from exc
        raise KiteRequestError(f"{what}: exhausted retries")  # pragma: no cover
