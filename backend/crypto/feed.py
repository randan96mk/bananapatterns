"""₿ The candle feed: Delta Exchange India's public REST (no key), stored in crypto_candles (never deleted; the archive
job moves rows older than crypto.archive_days), read 24×7 by CryptoService on its own thread.

  GET https://api.india.delta.exchange/v2/history/candles?resolution=5m&symbol=BTCUSD&start=<unix>&end=<unix>
  -> {"result": [{time, open, high, low, close, volume}, ...]} newest first; `time` = the candle's open (unix, UTC)

At the first start (per symbol / timeframe) the history is backfilled crypto.history_days deep, paged (PAGE candles a
call, ≤ 2 calls a second). Then the loop wakes on the timeframe boundary + 3 s, fetches the last few candles, upserts
them and hands every newly CLOSED candle to backend.crypto.signals. The daily candles (resolution 1d, 60 days) for the
pivots are refreshed once a day after 5:30 AM IST (Delta's day starts 00:00 UTC). Health: the last candle, the lag in
candles and the fetch errors (a Home "Needs a look" item when the feed is more than LAG_WARN candles behind).
Alerts only: this module reads prices, nothing else."""

import json
import threading
import time as _time
from datetime import datetime, timedelta
from typing import Callable, Optional

from backend.config import IST
from backend.crypto.pivots import TF_SECONDS
from backend.database.models import get_db, get_setting, set_setting
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm

logger = setup_logger("crypto")

URL = "https://api.india.delta.exchange/v2/history/candles"
SOURCE = "Delta Exchange"
PAGE = 2000                     # candles per call
MIN_GAP_S = 0.5                 # ≤ 2 requests a second
CLOSE_LAG_S = 3                 # the loop wakes 3 s after a candle's close
DAILY_DAYS = 60
DAILY_AT = (5, 30)              # IST: Delta's new day (00:00 UTC)
LAG_WARN = 3                    # candles behind: a "Needs a look" item
HEALTH_KEY = "crypto.health"


def _now() -> datetime:
    return datetime.now(IST)


class FeedError(RuntimeError):
    pass


class DeltaClient:
    """The public candle endpoint, paced to ≤ 2 calls a second. `get(url, params) -> dict` is injectable (the tests'
    fake Delta); the default is httpx through the environment's proxy settings."""

    def __init__(self, get: Optional[Callable[[str, dict], dict]] = None, sleep: Callable[[float], None] = _time.sleep):
        self._get = get
        self.sleep = sleep
        self._last = 0.0
        self._lock = threading.Lock()
        self.calls = 0

    def _http(self, url: str, params: dict) -> dict:
        import httpx
        r = httpx.get(url, params=params, timeout=15)
        if r.status_code != 200:
            raise FeedError(f"HTTP {r.status_code}: {r.text[:120]}")
        return r.json()

    def candles(self, symbol: str, resolution: str, start: int, end: int) -> list[dict]:
        """[{t, o, h, l, c, v}] oldest first (parsed; bad rows skipped)."""
        with self._lock:
            wait = MIN_GAP_S - (_time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = _time.monotonic()
            self.calls += 1
            params = {"resolution": resolution, "symbol": symbol, "start": int(start), "end": int(end)}
            body = (self._get or self._http)(URL, params)
        return parse(body)


def parse(body) -> list[dict]:
    """Delta's reply -> candles oldest first; a reply without `result` raises FeedError."""
    if not isinstance(body, dict) or not isinstance(body.get("result"), list):
        raise FeedError(f"unexpected reply: {str(body)[:120]}")
    out = {}
    for x in body["result"]:
        try:
            out[int(x["time"])] = {"t": int(x["time"]), "o": float(x["open"]), "h": float(x["high"]), "l": float(x["low"]),
                                   "c": float(x["close"]), "v": float(x.get("volume") or 0)}
        except (KeyError, TypeError, ValueError):
            continue
    return [out[k] for k in sorted(out)]


# --- the store ------------------------------------------------------------------------------------------------------------

def upsert(symbol: str, tf: str, candles: list[dict]) -> int:
    """Insert or update (the in-progress candle changes until it closes). Never deletes."""
    if not candles:
        return 0
    with get_db() as conn:
        conn.executemany("INSERT INTO crypto_candles (symbol, tf, t, o, h, l, c, v) VALUES (?,?,?,?,?,?,?,?) "
                         "ON CONFLICT(symbol, tf, t) DO UPDATE SET o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c, v=excluded.v",
                         [(symbol, tf, b["t"], b["o"], b["h"], b["l"], b["c"], b.get("v")) for b in candles])
    return len(candles)


def load(symbol: str, tf: str, start: Optional[int] = None, end: Optional[int] = None, limit: Optional[int] = None) -> list[dict]:
    """Stored candles oldest first; `limit` = the newest N in range."""
    q, a = "SELECT t, o, h, l, c, v FROM crypto_candles WHERE symbol = ? AND tf = ?", [symbol, tf]
    if start is not None:
        q += " AND t >= ?"
        a.append(int(start))
    if end is not None:
        q += " AND t <= ?"
        a.append(int(end))
    q += " ORDER BY t DESC" + (f" LIMIT {int(limit)}" if limit else "")
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(q, a)]
    return rows[::-1]


def closed(candles: list[dict], tf: str, now_ts: float) -> list[dict]:
    """Only the candles whose period has ended (t + tf ≤ now)."""
    s = TF_SECONDS[tf]
    return [b for b in candles if b["t"] + s <= now_ts]


def bounds(symbol: str, tf: str) -> tuple[Optional[int], Optional[int], int]:
    with get_db() as conn:
        r = conn.execute("SELECT MIN(t), MAX(t), COUNT(*) FROM crypto_candles WHERE symbol = ? AND tf = ?", (symbol, tf)).fetchone()
    return r[0], r[1], r[2] or 0


def backfill(client: DeltaClient, symbol: str, tf: str, days: int, now_ts: float, stop: Optional[threading.Event] = None) -> dict:
    """Page the history `days` deep (PAGE candles a call), skipping what is already stored at the newest end: from the
    newest stored candle when the oldest one reaches back far enough, else the whole range. {calls, candles}."""
    s = TF_SECONDS[tf]
    start = int(now_ts) - int(days) * 86400
    start -= start % s
    lo, hi, n = bounds(symbol, tf)
    key = f"crypto.backfill.{symbol}.{tf}"            # the deepest start already paged (Delta may hold less history)
    with get_db() as conn:
        done = get_setting(conn, key)
    if hi and ((lo is not None and lo <= start + s) or (done and int(done) <= start + s)):
        start = hi
    else:
        with get_db() as conn:
            set_setting(conn, key, str(start))
    got = calls = 0
    t, now_i = start, int(now_ts)
    while t < now_i and not (stop and stop.is_set()):
        end = min(now_i, t + PAGE * s)
        page = client.candles(symbol, tf, t, end)
        calls += 1
        got += upsert(symbol, tf, page)
        t = end
    return {"calls": calls, "candles": got}


# --- health ----------------------------------------------------------------------------------------------------------------

def health(symbol: str, tf: str, now_ts: Optional[float] = None, errors: int = 0, last_error: Optional[str] = None) -> dict:
    """{last_t, last_at (ISO, IST), last_label "9:40 AM", lag_candles, behind (> LAG_WARN), errors, last_error}. The last
    candle = the newest CLOSED one stored; lag = whole candles since its close."""
    now_ts = _time.time() if now_ts is None else now_ts
    s = TF_SECONDS.get(tf, 300)
    with get_db() as conn:
        r = conn.execute("SELECT MAX(t) FROM crypto_candles WHERE symbol = ? AND tf = ? AND t + ? <= ?", (symbol, tf, s, int(now_ts))).fetchone()
    last = r[0] if r else None
    if last is None:
        return {"last_t": None, "last_at": None, "last_label": None, "lag_candles": None, "behind": False, "errors": errors, "last_error": last_error}
    at = datetime.fromtimestamp(last, IST)
    lag = max(0, int((now_ts - (last + s)) // s))
    return {"last_t": last, "last_at": at.isoformat(timespec="seconds"), "last_label": ampm(at), "lag_candles": lag, "behind": lag > LAG_WARN,
            "errors": errors, "last_error": last_error}


def saved_health() -> dict:
    with get_db() as conn:
        raw = get_setting(conn, HEALTH_KEY)
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def attention_items(config, now_ts: Optional[float] = None) -> list:
    """Home "Needs a look": the feed (switched on) more than LAG_WARN candles behind, or failing."""
    c = config.crypto
    if not c.enabled:
        return []
    h = health(c.symbol, c.tf, now_ts, **{k: saved_health().get(k) or (0 if k == "errors" else None) for k in ("errors", "last_error")})
    if h["last_t"] is None:
        return [("warn", f"₿ BTC feed: no {c.tf} candles yet from {SOURCE}")]
    if h["behind"]:
        return [("warn", f"₿ BTC feed {h['lag_candles']} candles behind · last candle {h['last_label']}",
                 h.get("last_error") or "the feed retries on each candle")]
    return []


# --- the service -------------------------------------------------------------------------------------------------------------

class CryptoService:
    """The 24×7 loop (its own daemon thread), started with the app; idle while crypto.enabled is off. Each pass:
    backfill once per symbol / timeframe, the daily candles once a day, the last few candles, every newly closed candle
    to the signals, the forming heads-up (when on), the quiet-hours digest. Never raises out of its loop."""

    def __init__(self, state, client: Optional[DeltaClient] = None, clock: Callable[[], float] = _time.time):
        self.state = state
        self.client = client or DeltaClient()
        self.clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.errors = 0
        self.last_error: Optional[str] = None
        self.backfilled: set = set()
        self.running = False
        self.forming_done: set = set()
        self._busy = threading.Lock()

    @property
    def config(self):
        return self.state.config

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="crypto-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def poke(self) -> None:
        """Wake the loop now (Settings saved, Refresh pressed)."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            c = self.config.crypto
            if not c.enabled:
                self.running = False
                self._wake.wait(30)
                self._wake.clear()
                continue
            self.running = True
            try:
                self.tick()
            except Exception as exc:
                self._error(exc)
                logger.exception("₿ crypto tick failed")
            self._wake.wait(self.sleep_s())
            self._wake.clear()

    def sleep_s(self, now_ts: Optional[float] = None) -> float:
        """Until the next candle close + CLOSE_LAG_S (or the forming point when the heads-up is on), 30 s at most."""
        c = self.config.crypto
        now_ts = self.clock() if now_ts is None else now_ts
        s = TF_SECONDS.get(c.tf, 300)
        nxt = (int(now_ts) // s + 1) * s + CLOSE_LAG_S
        if c.forming:
            fp = (int(now_ts) // s) * s + 0.8 * s
            if fp > now_ts:
                nxt = min(nxt, fp)
        return max(1.0, min(30.0, nxt - now_ts))

    def _error(self, exc) -> None:
        self.errors += 1
        self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
        self._save_health()

    def _save_health(self) -> None:
        with get_db() as conn:
            set_setting(conn, HEALTH_KEY, json.dumps({"errors": self.errors, "last_error": self.last_error,
                                                       "at": _now().isoformat(timespec="seconds")}))

    def refresh_daily(self, now_ts: float, force: bool = False) -> int:
        """The 1d candles (DAILY_DAYS) once a day after 5:30 AM IST, or when none are stored."""
        c = self.config.crypto
        now = datetime.fromtimestamp(now_ts, IST)
        day = (now - timedelta(hours=DAILY_AT[0], minutes=DAILY_AT[1])).date().isoformat()
        key = f"crypto.daily.{c.symbol}"
        with get_db() as conn:
            done = get_setting(conn, key)
        if done == day and not force and bounds(c.symbol, "1d")[2]:
            return 0
        n = upsert(c.symbol, "1d", self.client.candles(c.symbol, "1d", int(now_ts) - DAILY_DAYS * 86400, int(now_ts)))
        with get_db() as conn:
            set_setting(conn, key, day)
        return n

    def tick(self, now_ts: Optional[float] = None) -> dict:
        """One pass (see the class doc). {fetched, processed [t], forming, digest}."""
        from backend.crypto import signals
        with self._busy:
            c = self.config.crypto
            now_ts = self.clock() if now_ts is None else now_ts
            sym, tf, s = c.symbol, c.tf, TF_SECONDS[c.tf]
            if (sym, tf) not in self.backfilled:
                r = backfill(self.client, sym, tf, int(c.history_days), now_ts, self._stop)
                logger.info("₿ backfill %s %s: %d candles in %d calls", sym, tf, r["candles"], r["calls"])
                self.backfilled.add((sym, tf))
            self.refresh_daily(now_ts)
            fresh = self.client.candles(sym, tf, int(now_ts) - 6 * s, int(now_ts))
            upsert(sym, tf, fresh)
            key = f"crypto.processed.{sym}.{tf}"
            with get_db() as conn:
                last = int(get_setting(conn, key) or 0)
            done = closed(load(sym, tf, limit=6), tf, now_ts)
            new = [b["t"] for b in done if b["t"] > last][-3:]        # after a gap: the last 3 closed candles at most
            if not last and done:
                new = [done[-1]["t"]]
            out = {"fetched": len(fresh), "processed": [], "forming": None, "digest": 0}
            for t in new:
                signals.process(self.config, t, now_ts=now_ts)
                out["processed"].append(t)
                with get_db() as conn:
                    set_setting(conn, key, str(t))
            if c.forming and fresh:
                cur = fresh[-1]
                frac = (now_ts - cur["t"]) / s
                if 0.8 <= frac < 1 and (sym, tf, cur["t"]) not in self.forming_done:
                    self.forming_done.add((sym, tf, cur["t"]))
                    out["forming"] = signals.forming(self.config, cur["t"], now_ts=now_ts)
            out["digest"] = signals.digest_if_due(self.config, now_ts=now_ts)
            self.last_error = None                      # the count stays (since the start); the last error clears on a good pass
            self._save_health()
            return out
