"""💡 Shared fakes for the Tips tests: a market-data client (instruments dumps + quotes, no network) and helpers that
seed hints through the real ingest path."""

import json
from datetime import date, datetime
from pathlib import Path

from backend.config import IST, AppConfig

DAY = date(2026, 11, 17)                     # a Tuesday (a trading day)
EXP = date(2026, 11, 25)
FIXTURE = Path(__file__).parent / "fixtures" / "tips_posts.json"


def at(hhmm: str, day: date = DAY, sec: int = 0) -> datetime:
    h, m = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(h), int(m), sec, tzinfo=IST)


def posts() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def opt(name, strike, typ, expiry=EXP, seg="NFO-OPT", lot=15, token=None):
    ts = f"{name}{expiry:%y%b}{int(strike)}{typ}".upper()
    return {"name": name, "instrument_type": typ, "segment": seg, "expiry": expiry, "strike": float(strike), "tradingsymbol": ts,
            "instrument_token": token or abs(hash(ts)) % 10_000_000, "lot_size": lot}


def fut(name, expiry, seg="MCX-FUT", lot=100):
    return {"name": name, "instrument_type": "FUT", "segment": seg, "expiry": expiry, "strike": 0.0,
            "tradingsymbol": f"{name}{expiry:%y%b}FUT".upper(), "instrument_token": 77, "lot_size": lot}


class FakeClient:
    """has_token, instruments(exchange) from `dumps`, quote(keys) from `prices` (key -> last price)."""

    def __init__(self, dumps=None, prices=None, has_token=True):
        self.dumps = dumps or {}
        self.prices = dict(prices or {})
        self.has_token = has_token
        self.calls = []

    def instruments(self, exchange):
        self.calls.append(("instruments", exchange))
        return self.dumps.get(exchange, [])

    def quote(self, keys):
        self.calls.append(("quote", tuple(keys)))
        return {k: {"last_price": self.prices[k]} for k in keys if k in self.prices}


def world(**prices) -> FakeClient:
    nfo = [opt("BANKNIFTY", s, t) for s in (49600, 50300, 51000) for t in ("CE", "PE")] + \
          [opt("NIFTY", s, t, date(2026, 11, 24 - 0), lot=75) for s in (24150, 24400) for t in ("CE", "PE")] + \
          [opt("NIFTY", 24150, "PE", date(2026, 12, 1), lot=75)]
    bfo = [opt("SENSEX", 75200, "CE", date(2026, 11, 19), seg="BFO-OPT", lot=20)]
    mcx = [opt("CRUDEOIL", 5300, "PE", date(2026, 12, 16), seg="MCX-OPT", lot=100), fut("CRUDEOIL", date(2026, 12, 18))]
    return FakeClient({"NFO": nfo, "BFO": bfo, "MCX": mcx}, prices)


def cfg() -> AppConfig:
    c = AppConfig()
    c.ai.enabled = False
    return c


BN12 = "Buy bank nifty 50300 PE near 250-270 when comes \nSl 190\nTgt 320-350-400++\n\nResistance at 50200-300 zone"
BN13 = "BANKNIFTY 49600 PE BUY ABOVE 50 \n\nSL 40 \n\nTG 60 80 100 PLUS"
KEY12 = "NFO:BANKNIFTY26NOV50300PE"
KEY13 = "NFO:BANKNIFTY26NOV49600PE"
SPOT_BN = "NSE:NIFTY BANK"


def seed(config, client, text=BN12, hhmm="09:43", post_id=17605, channel="aakankshatrading", day: date = DAY, source="live"):
    """Load the dumps the hint needs, then ingest the post (no push)."""
    from backend.tips import reader as rd
    from backend.tips import track as tr
    for ex, names in (("NFO", {"BANKNIFTY", "NIFTY"}), ("BFO", {"SENSEX"}), ("MCX", {"CRUDE"})):
        tr.refresh_instruments(client, ex, names, day)
    return rd.ingest(config, channel, post_id, at(hhmm, day), text, False, source=source, push=False, model=False, client=client)
