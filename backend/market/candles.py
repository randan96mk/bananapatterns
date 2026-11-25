"""📈 Candles for the market read.

  index    NIFTY, BANKNIFTY, SENSEX 1-minute candles from Kite historical (minute): the day in progress + the last 2
           sessions at the first fetch, then the day in progress every market.interval_s (one call per index). Stored
           in candles_intraday (one compressed row per index per day, kept in the schema since PR 147): a fetch merges
           into the stored day, rows are never deleted. Logged out of Kite: nothing is fetched (the panel says
           "candles need the Kite login").
  premium  an option's 1-minute OHLC built from tips_ticks (the 5 s poll) while its hint is open / armed on Kite."""

import json
import zlib
from datetime import date, datetime, timedelta
from typing import Optional

from backend.config import IST
from backend.database.models import get_db

INDICES = ("NIFTY", "BANKNIFTY", "SENSEX")
TOKENS = {"NIFTY": 256265, "BANKNIFTY": 260105, "SENSEX": 265}          # Kite instrument tokens of the three indices
QUOTE_KEYS = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "SENSEX": "BSE:SENSEX"}
VIX_KEY = "NSE:INDIA VIX"
LOGIN_TEXT = "candles need the Kite login"
SOURCE = "kite_minute"


def logged_in(client) -> bool:
    return client is not None and bool(getattr(client, "has_token", False))


def _pack(bars: list[dict]) -> bytes:
    return zlib.compress(json.dumps(bars, separators=(",", ":")).encode())


def _unpack(blob) -> list[dict]:
    try:
        return json.loads(zlib.decompress(blob))
    except Exception:
        return []


def store(token: int, day: str, bars: list[dict], source: str = SOURCE, complete: bool = False) -> int:
    """Merge `bars` ({t, o, h, l, c, v}) into the stored day (a minute already stored is replaced by the newer bar);
    returns the bars kept. Never deletes a row."""
    with get_db() as conn:
        r = conn.execute("SELECT bars, complete FROM candles_intraday WHERE instrument_token = ? AND day = ?", (token, day)).fetchone()
        have = {b["t"]: b for b in _unpack(r["bars"])} if r else {}
        for b in bars:
            have[b["t"]] = {k: b[k] for k in ("t", "o", "h", "l", "c", "v") if k in b}
        merged = [have[t] for t in sorted(have)]
        conn.execute("INSERT OR REPLACE INTO candles_intraday (instrument_token, day, interval, bars, n, source, complete) VALUES (?,?,?,?,?,?,?)",
                     (token, day, 1, _pack(merged), len(merged), source, 1 if complete or (r and r["complete"]) else 0))
    from backend.market import reads as rd                 # ⏱ new bars: the cards' index summary is computed afresh
    rd.invalidate("index_facts")
    return len(merged)


def load(token: int, sessions: int = 3, until: Optional[str] = None) -> list[dict]:
    """The last `sessions` stored days (up to `until`), as one list of {d, t, o, h, l, c, v} oldest first."""
    with get_db() as conn:
        q = "SELECT day, bars FROM candles_intraday WHERE instrument_token = ?" + (" AND day <= ?" if until else "") + " ORDER BY day DESC LIMIT ?"
        rows = conn.execute(q, (token, until, sessions) if until else (token, sessions)).fetchall()
    out = []
    for r in reversed(rows):
        out += [{**b, "d": r["day"]} for b in _unpack(r["bars"])]
    return out


def index_bars(symbol: str, sessions: int = 3, until: Optional[str] = None) -> list[dict]:
    return load(TOKENS[symbol], sessions, until)


def stored_days(token: int) -> list[str]:
    with get_db() as conn:
        return [r[0] for r in conn.execute("SELECT day FROM candles_intraday WHERE instrument_token = ? ORDER BY day", (token,))]


def _prev_sessions(day: date, n: int) -> list[date]:
    from backend.ingest.holidays import is_trading_day
    out, d = [], day
    while len(out) < n:
        d -= timedelta(days=1)
        if is_trading_day(d):
            out.append(d)
    return sorted(out)


def fetch(client, symbol: str, now: Optional[datetime] = None) -> dict:
    """One index: the 2 previous sessions once (when not stored complete), then today. {symbol, ok, bars, why}."""
    from backend.options.candles_util import from_kite
    now = now or datetime.now(IST)
    if not logged_in(client):
        return {"symbol": symbol, "ok": False, "why": LOGIN_TEXT, "bars": 0}
    token = TOKENS[symbol]
    have = set(stored_days(token))
    today = now.date()
    prev = _prev_sessions(today, 2)
    start = prev[0] if any(d.isoformat() not in have for d in prev) else today
    rows = client.historical_minute(token, start, today, label=symbol)
    n = 0
    for d, bars in from_kite(rows).items():
        n += store(token, d, bars, complete=d < today.isoformat())
    return {"symbol": symbol, "ok": True, "bars": n, "why": None}


def premium_bars(hid: int, day: Optional[str] = None) -> list[dict]:
    """The hint's option premium as 1-minute OHLC from its ticks (v 0), oldest first; `day` limits to one session."""
    from backend.tips import facts as fx
    out: dict = {}
    for t in fx.ticks(hid):
        if t.get("ltp") is None or not t.get("at"):
            continue
        try:
            at = datetime.fromisoformat(str(t["at"]))
        except ValueError:
            continue
        if at.tzinfo is not None:
            at = at.astimezone(IST)
        d, hm = at.date().isoformat(), at.strftime("%H:%M")
        if day and d != day:
            continue
        px = float(t["ltp"])
        b = out.get((d, hm))
        if b is None:
            out[(d, hm)] = {"d": d, "t": hm, "o": px, "h": px, "l": px, "c": px, "v": 0}
        else:
            b["h"], b["l"], b["c"] = max(b["h"], px), min(b["l"], px), px
    return [out[k] for k in sorted(out)]


def to_worker(bars: list[dict]) -> list[dict]:
    """The worker's candle shape {at 'YYYY-MM-DD HH:MM', o, h, l, c, v}."""
    return [{"at": f"{b['d']} {b['t']}", "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v") or 0} for b in bars]
