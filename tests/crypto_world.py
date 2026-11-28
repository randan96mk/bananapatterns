"""₿ Shared fixtures for tests/test_crypto_*.py: a fake Delta Exchange, a fake Telegram, hand-built candles at a pivot."""

from datetime import datetime, timezone

from backend.alerts.dispatch import Notifier
from backend.config import AlertsConfig, AppConfig
from backend.crypto import feed
from backend.crypto import signals as sg
from tests.test_alerts import FakeTG

UTC = timezone.utc
DAY0 = int(datetime(2026, 11, 22, tzinfo=UTC).timestamp())        # Sun 22 Nov 00:00 UTC
DAY1 = DAY0 + 86400                                                 # Mon 23 Nov
T_DAY = int(datetime(2026, 11, 24, 4, 30, tzinfo=UTC).timestamp())    # Tue 24 Nov 10:00 AM IST (inside the alert hours)
T_NIGHT = int(datetime(2026, 11, 23, 20, 40, tzinfo=UTC).timestamp())  # Tue 24 Nov 2:10 AM IST (quiet hours)
H, L, C = 61000.0, 59000.0, 60000.0                                 # the previous day: P 60,000 · S1 59,000 · R1 61,000
TOUCH = dict(trigger="touch", session_asia="alerts")                # the old rule (a pattern at a pivot) with Asia alerting (T_DAY is Asia)


def cfg(**crypto) -> AppConfig:
    c = AppConfig()
    c.crypto.enabled = True
    c.market.kronos = False                       # no candle-model worker in tests (test_crypto_alerts turns a fake one on)
    c.alerts = AlertsConfig(chat_id="42")
    for k, v in crypto.items():
        setattr(c.crypto, k, v)
    return c


def k(t, o, h, l, c, v=100.0) -> dict:
    return {"t": int(t), "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": float(v)}


def seed_daily(h=H, l=L, c=C, symbol="BTCUSD"):
    feed.upsert(symbol, "1d", [k(d, c, h, l, c, 1e6) for d in (DAY0 - 86400, DAY0, DAY1, DAY1 + 86400)])


def flat(t_end: int, n: int = 29, px: float = 59050.0, tf_s: int = 300) -> list[dict]:
    """n flat candles ending just before t_end."""
    return [k(t_end - (n - i) * tf_s, px, px + 10, px - 10, px) for i in range(n)]


def hammer_world(t: int = T_DAY, store: bool = True) -> list[dict]:
    """29 flat candles at 59,050 then a hammer on S1 59,000 at `t` (close 59,100 above the EMA, volume 1.28× its
    20 average). Stored in crypto_candles with the daily candles."""
    cs = flat(t) + [k(t, 59090, 59105, 58990, 59100, 130)]
    if store:
        seed_daily()
        feed.upsert("BTCUSD", "5m", cs)
    return cs


class FakeDelta:
    """Delta's /v2/history/candles over a stored series: newest first, [start, end] inclusive, PAGE at most; and
    /v2/tickers/<symbol> from `ticker` (a dict of the result; None = the call fails)."""

    def __init__(self, series: dict, ticker=None):
        self.series = series                     # resolution -> candles oldest first
        self.calls = []
        self.fail = 0
        self.ticker = ticker
        self.ticker_calls = []

    def __call__(self, url, params):
        if "/tickers/" in url:
            self.ticker_calls.append(url)
            if self.ticker is None:
                raise feed.FeedError("HTTP 503")
            return {"success": True, "result": dict(self.ticker)}
        self.calls.append(dict(params))
        if self.fail:
            self.fail -= 1
            raise feed.FeedError("HTTP 502")
        rs = [b for b in self.series.get(params["resolution"], []) if params["start"] <= b["t"] <= params["end"]][:feed.PAGE]
        return {"success": True, "result": [{"time": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "volume": b["v"]}
                                            for b in reversed(rs)]}


def telegram(monkeypatch):
    """A fake Telegram behind the crypto notifier; returns its FakeTG (sent = [(kind, html)])."""
    tg = FakeTG()
    tg.photos = []
    real = tg.send_photo

    def photo(chat, png, html, buttons=None, silent=False):
        tg.photos.append(png)
        return real(chat, png, html, buttons, silent)
    tg.send_photo = photo
    monkeypatch.setattr(sg, "NOTIFIER", Notifier(AlertsConfig(chat_id="42"), tg))
    return tg
