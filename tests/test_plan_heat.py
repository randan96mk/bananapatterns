"""🔥 Live heat on the Plan: the state rules (above / testing / broken / quiet / skipped), the range-bar position and its
clipping, the live quote only while the market is open and only through the market-data client's quote call, the
last-close fallback (`live: false`), the AI levels and reasons, the morning skips, the sort, the counts, the route and
the stream topic's interval. Display only: no model call, no order, no stop."""

import json
from datetime import datetime

import pytest

from backend.alerts import plan as pl
from backend.config import IST, AppConfig
from backend.database.models import get_db, now_ist
from backend.engine import heat, real
from tests.test_ai_infra import no_brokers
from tests.test_ai_view import bars, view_row, weekdays_back

TODAY = now_ist()[:10]
OPEN = datetime.fromisoformat(TODAY).replace(hour=11, minute=42, tzinfo=IST)


class Quotes:
    """Only `quote` exists: the heat may not ask for anything else (no holdings, positions or orders)."""

    def __init__(self, prices: dict, prev: dict = None, fail: bool = False):
        self.prices, self.prev, self.fail, self.calls = prices, prev or {}, fail, []

    def quote(self, keys):
        self.calls.append(list(keys))
        if self.fail:
            raise RuntimeError("quote down")
        return {k: {"last_price": self.prices[k[4:]], "ohlc": {"close": self.prev.get(k[4:])}} for k in keys if k[4:] in self.prices}


@pytest.fixture
def market(monkeypatch):
    state = {"open": False}
    monkeypatch.setattr("backend.engine.overview.market_open", lambda now=None: state["open"])
    return state


def plan_item(sym, pivot, stop, zone_top):
    return {"symbol": sym, "pivot": pivot, "stop": stop, "zone_top": zone_top, "close": pivot}


def seed(levels=True):
    """Five names, last closes vs their levels: ABOVE over the pivot, TEST within 0.5% under it, BROKE under its
    fails_below, CALM in between, SKIP in the morning skips."""
    d = weekdays_back(TODAY, 2)
    for sym, closes in (("ABOVE", [100, 104]), ("TEST", [98, 99.7]), ("BROKE", [95, 91]), ("CALM", [97, 96]), ("SKIP", [100, 101])):
        bars(sym, closes, days=d)
    pl.save_plan(TODAY, [plan_item("ABOVE", 100, 90, 105), plan_item("TEST", 100, 90, 105), plan_item("BROKE", 100, 88, 105),
                         plan_item("CALM", 100, 90, 105), plan_item("SKIP", 100, 90, 105)])
    if levels:
        for s in ("ABOVE", "TEST", "CALM", "SKIP"):
            view_row(s, TODAY, "buy", 70, fb=85.0)
        view_row("BROKE", TODAY, "avoid", 30, fb=92.0)
    real._save({"started": "2026-10-01", "capital": 1.0, "cash": 1.0, "positions": {}, "orders": [], "skips": {TODAY: {"SKIP": {"by": "you"}}}})


# --- the rules ---------------------------------------------------------------------------------------------------------------

def test_state_rules():
    s = heat.state_for
    assert s(104, 100, 90, 85, 95) == "above" and s(100, 100, 90, 85, 95) == "above"          # at the pivot counts as above
    assert s(99.6, 100, 90, 85, 95) == "testing" and s(95.3, 100, 90, 85, 95) == "testing"     # within 0.5% of the pivot / support 1
    assert s(84, 100, 80, 85, 95) == "broken" and s(89, 100, 90, None, None) == "broken"        # under fails_below / under the stop
    assert s(97, 100, 90, 85, 95) == "quiet" and s(None, 100, 90, 85, 95) == "quiet"
    assert s(80, 100, 90, 85, 95, skipped=True) == "skipped"                                     # skipped wins


def test_pos_is_clipped():
    assert heat.pos_for(95, 90, 100) == 0.5 and heat.pos_for(80, 90, 100) == 0.0 and heat.pos_for(120, 90, 100) == 1.0
    assert heat.pos_for(None, 90, 100) is None and heat.pos_for(95, 100, 90) is None and heat.pos_for(95, None, 100) is None


# --- the payload -------------------------------------------------------------------------------------------------------------

def test_market_closed_uses_the_last_close(market):
    seed()
    q = Quotes({"ABOVE": 150})
    h = heat.plan_heat(AppConfig(), OPEN, q.quote)
    assert q.calls == [] and h["live"] is False and h["market_open"] is False and h["at_label"] == "last close · market closed"
    by = {i["symbol"]: i for i in h["items"]}
    assert by["ABOVE"]["price"] == 104 and by["ABOVE"]["change_pct"] == 4.0 and by["ABOVE"]["live"] is False
    assert {s: i["state"] for s, i in by.items()} == {"ABOVE": "above", "TEST": "testing", "BROKE": "broken", "CALM": "quiet", "SKIP": "skipped"}
    assert h["counts"] == {"above": 1, "testing": 1, "broken": 1, "quiet": 1, "skipped": 1}


def test_sorted_broken_testing_above_quiet_skipped(market):
    seed()
    h = heat.plan_heat(AppConfig(), OPEN)
    assert [i["state"] for i in h["items"]] == ["broken", "testing", "above", "quiet", "skipped"]
    assert [i["rank"] for i in h["items"]] == [3, 2, 1, 4, 5]


def test_live_quotes_while_the_market_is_open(market):
    market["open"] = True
    seed()
    q = Quotes({"CALM": 100.2, "BROKE": 95.0}, prev={"CALM": 96.0})
    h = heat.plan_heat(AppConfig(), OPEN, q.quote)
    assert len(q.calls) == 1 and all(k.startswith("NSE:") for k in q.calls[0]) and len(q.calls[0]) == 5
    by = {i["symbol"]: i for i in h["items"]}
    assert by["CALM"]["live"] is True and by["CALM"]["price"] == 100.2 and by["CALM"]["state"] == "above" and by["CALM"]["change_pct"] == 4.37
    assert by["BROKE"]["state"] == "quiet" and by["ABOVE"]["live"] is False                     # no quote for ABOVE: its last close
    assert h["live"] is True and h["at_label"] == "11:42 AM · live"


def test_no_quote_falls_back_to_the_close(market):
    market["open"] = True
    seed()
    for q in (None, Quotes({}, fail=True).quote, Quotes({}).quote):
        h = heat.plan_heat(AppConfig(), OPEN, q)
        assert h["live"] is False and h["at_label"] == "last close" and all(i["price"] for i in h["items"])


def test_levels_and_reasons_from_the_ai_view(market):
    seed()
    by = {i["symbol"]: i for i in heat.plan_heat(AppConfig(), OPEN)["items"]}
    a = by["ABOVE"]
    assert (a["fails_below"], a["support_1"], a["resistance"], a["score"], a["verdict"]) == (85.0, 85.0, None, 70, "buy")
    assert a["reasons"] == [{"text": "tight base", "ai": True}] and (a["pivot"], a["stop"], a["zone_top"]) == (100, 90, 105)
    assert a["pos"] == 0.933 and by["SKIP"]["skipped"] is True
    keys = {"symbol", "rank", "price", "change_pct", "state", "pos", "stop", "fails_below", "pivot", "zone_top", "support_1", "resistance",
            "score", "verdict", "reasons", "skipped"}
    assert keys <= set(a)


def test_without_views_the_stop_decides(market):
    seed(levels=False)
    by = {i["symbol"]: i for i in heat.plan_heat(AppConfig(), OPEN)["items"]}
    assert by["BROKE"]["state"] == "quiet" and by["BROKE"]["score"] is None and by["BROKE"]["reasons"] == []   # 91 over the stop 88
    assert by["TEST"]["state"] == "testing"


def test_empty_plan(market):
    h = heat.plan_heat(AppConfig(), OPEN)
    assert h["items"] == [] and h["counts"] == {"above": 0, "testing": 0, "broken": 0, "quiet": 0, "skipped": 0} and h["as_of"] is None


def test_heat_never_touches_orders_or_stops(market):
    market["open"] = True
    seed()
    before = real._state()
    heat.plan_heat(AppConfig(), OPEN, Quotes({"ABOVE": 50.0}).quote)
    assert real._state() == before
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM real_orders").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == 0
    src = open(heat.__file__).read()
    assert "holdings" not in src.replace("never reads holdings", "") and ".positions(" not in src and ".orders(" not in src


# --- route and stream ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def api(fake_kite, make_client, monkeypatch):
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("KITE_API_KEY", "key123")
    monkeypatch.setenv("KITE_API_SECRET", "sec")
    from fastapi.testclient import TestClient
    from backend import main
    kc = make_client(fake_kite, history_years=1)
    kc.config.access_token = ""
    monkeypatch.setattr(main, "KiteClient", lambda kc_, ic: kc)
    with TestClient(main.app) as tc:
        yield tc


def test_route_and_stream_topic(api, market):
    seed()
    d = api.get("/api/plan/heat").json()
    assert set(d) >= {"at", "live", "items", "counts"} and d["live"] is False and len(d["items"]) == 5
    assert no_brokers(json.dumps(d, ensure_ascii=False))
    body = api.get("/api/stream?topics=plan_heat&once=1").text
    assert "event: plan_heat" in body and '"counts"' in body
    from backend.api import routes
    every = routes._stream_topics()["plan_heat"][1]
    assert every({"market_open": True}) == 60 and every({"market_open": False}) == 600 and every(None) == 600
