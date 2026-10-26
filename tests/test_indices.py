"""Index ticker bar: market status, live quotes with change / 50-day / sparkline, caching, history top-up,
chart instrument rows, the no-login fallback and the API."""

from datetime import date, datetime, timedelta

from backend.config import IST
from backend.database.models import get_candles, get_db, upsert_candles
from backend.ingest import indices as ix

OPEN_TIME = datetime(2026, 10, 23, 11, 0, tzinfo=IST)          # a Friday, market open
TOKENS = {"NSE:NIFTY 50": 256265, "NSE:NIFTY 500": 268041, "NSE:NIFTY BANK": 260105, "NSE:INDIA VIX": 264969,
          "BSE:SENSEX": 265}


def daily(n, start=date(2026, 6, 1), base=100.0):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append({"date": d.isoformat(), "open": base, "high": base + 1, "low": base - 1, "close": base,
                        "volume": 0})
            base += 1
        d += timedelta(days=1)
    return out


class FakeClient:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def quote(self, keys):
        self.calls.append(("quote", tuple(keys)))
        if self.fail:
            raise RuntimeError("kite down")
        return {k: {"instrument_token": t, "last_price": 1000.0 + t % 7, "ohlc": {"close": 990.0}}
                for k, t in TOKENS.items()}

    def historical_daily(self, token, start, end, label=""):
        self.calls.append(("daily", label, start, end))
        return [r for r in daily(120, start=date(2026, 4, 1)) if start.isoformat() <= r["date"] <= end.isoformat()]

    def historical_intraday(self, token, day, interval="5minute", label=""):
        self.calls.append(("intraday", label, day))
        return [{"time": "09:15", "close": 991.0}, {"time": "09:20", "close": 995.0}]


def test_market_status_and_last_session():
    assert ix.market_status(OPEN_TIME) == "open"
    assert ix.market_status(OPEN_TIME.replace(hour=9, minute=5)) == "pre_open"
    assert ix.market_status(OPEN_TIME.replace(hour=16)) == "closed"
    assert ix.market_status(datetime(2026, 10, 24, 11, 0, tzinfo=IST)) == "holiday"          # Saturday
    assert ix.last_session(OPEN_TIME) == date(2026, 10, 23)
    assert ix.last_session(OPEN_TIME.replace(hour=8)) == date(2026, 10, 22)                  # before the open
    assert ix.last_session(datetime(2026, 10, 25, 11, 0, tzinfo=IST)) == date(2026, 10, 23)  # Sunday


def test_live_snapshot():
    fake = FakeClient()
    t = ix.IndexTicker(fake, clock=lambda: OPEN_TIME)
    s = t.snapshot(live=True)
    assert s["status"] == "open" and s["live"] and [i["symbol"] for i in s["items"]] == [
        "NIFTY 50", "NIFTY 500", "NIFTY BANK", "INDIA VIX", "SENSEX"]
    n50 = s["items"][0]
    assert n50["last"] == 1000.0 + 256265 % 7 and n50["prev_close"] == 990.0
    assert n50["change"] == round(n50["last"] - 990.0, 2) and n50["change_pct"] == round(n50["change"] / 990 * 100, 2)
    assert 150 < n50["ma50"] < 300 and n50["above_ma50"] is True               # 49 stored closes + the live price
    assert n50["spark"] == [991.0, 995.0, n50["last"]]                           # live price appended while open
    with get_db() as conn:
        row = conn.execute("SELECT segment, exchange FROM instruments WHERE tradingsymbol = 'SENSEX'").fetchone()
        assert dict(row) == {"segment": "INDICES", "exchange": "BSE"}            # the chart can open it
        assert get_candles(conn, 265, limit=1)[-1]["date"] < "2026-10-23"       # history up to yesterday
    quotes = sum(c[0] == "quote" for c in fake.calls)
    t.snapshot(live=True)
    assert sum(c[0] == "quote" for c in fake.calls) == quotes                   # cached for QUOTE_TTL
    assert sum(c[0] == "daily" for c in fake.calls) == 5                         # history once a day
    assert sum(c[0] == "intraday" for c in fake.calls) == 5                      # sparkline every SPARK_TTL


def test_history_top_up_is_incremental():
    fake = FakeClient()
    with get_db() as conn:
        upsert_candles(conn, 256265, daily(10, start=date(2026, 10, 1)))
    ix.IndexTicker(fake, clock=lambda: OPEN_TIME).snapshot(live=True)
    n50 = next(c for c in fake.calls if c[0] == "daily" and c[1] == "NIFTY 50")
    assert n50[2] == date(2026, 10, 15) and n50[3] == date(2026, 10, 22)        # only the missing days


def test_kite_failure_falls_back_to_stored_closes():
    with get_db() as conn:
        conn.execute("INSERT INTO instruments (instrument_token, tradingsymbol, exchange, segment, fetched_on) "
                     "VALUES (256265, 'NIFTY 50', 'NSE', 'INDICES', '2026-10-23')")
        upsert_candles(conn, 256265, daily(60))
    s = ix.IndexTicker(FakeClient(fail=True), clock=lambda: OPEN_TIME).snapshot(live=True)
    assert not s["live"] and "kite down" in s["error"]
    n50 = s["items"][0]
    assert n50["last"] == 159.0 and n50["change"] == 1.0 and n50["ma50"] == 134.5 and n50["above_ma50"] is True
    assert all(i.get("missing") for i in s["items"][1:])                         # no stored candles for the rest


def test_api_without_login(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        d = tc.get("/api/indices").json()
        assert d["live"] is False and len(d["items"]) == 5 and d["status"] in ("open", "pre_open", "closed", "holiday")
