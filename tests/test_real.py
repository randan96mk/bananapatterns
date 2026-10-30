"""💰 Real trading: the Tradetron payload (exactly the user's send()), the token never stored, the evening plan
(sizing, limits, the A / E switch for new buys only), the morning send (EXITs first, once each, kill switch / OFF),
the stop watch (EXIT once at or under the stop), the evening correction to the day's open, the schedule and the API."""

import json
from datetime import datetime

import pytest

from backend.broker import tradetron
from backend.config import IST
from backend.database.models import get_db
from backend.engine import real
from backend.engine import trend_breakout as tb
from tests.test_paper import cfg, store

TOKEN = "test-token-123"


def test_payload_is_the_users_send_format(monkeypatch):
    assert tradetron.body("RELIANCE", "BUY", 12, "t") == {
        "auth-token": "t", "key": "sig_symbol", "value": "RELIANCE", "key1": "sig_qty", "value1": "12",
        "key2": "sig_action", "value2": "BUY"}
    assert tradetron.body("RELIANCE", "EXIT", None, "t") == {
        "auth-token": "t", "key": "sig_symbol", "value": "RELIANCE", "key1": "sig_action", "value1": "EXIT"}
    assert "auth-token" not in tradetron.public(tradetron.body("X", "EXIT", None, "t"))
    monkeypatch.delenv("TRADETRON_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        tradetron.send("X", "EXIT")


class Sender:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, symbol, action, qty=None):
        if symbol in self.fail:
            raise OSError("timeout")
        self.calls.append((symbol, action, qty))
        return '{"success": true}'


class Kite:
    has_token = True

    def __init__(self, prices):
        self.prices = prices

    def quote(self, keys):
        return {k: {"last_price": self.prices[k.split(":")[1]]} for k in keys if k.split(":")[1] in self.prices}


def setup(monkeypatch, **real_cfg):
    store(monkeypatch)
    monkeypatch.setenv("TRADETRON_TOKEN", TOKEN)
    c = cfg()
    for k, v in real_cfg.items():
        setattr(c.real, k, v)
    return c


def first_day_with_buys(c):
    """Load the data cut back to the first session whose plan has BUYs (the plan reads the last session)."""
    data = tb.load(c)
    for end in range(len(data["cal"]) - 150, len(data["cal"])):
        cut = {**data, "cal": data["cal"][:end + 1], "index_close": data["index_close"][:end + 1],
               "index_ok": data["index_ok"][:end + 1], "filters": {k: v[:end + 1] for k, v in data["filters"].items()}}
        st = {"started": cut["cal"][-1], "capital": 200_000.0, "cash": 200_000.0, "charges": 0.0, "positions": {}, "orders": []}
        real._plan(st, cut, end, c)
        if any(o["action"] == "BUY" for o in st["orders"]):
            return data, cut, end
    raise AssertionError("no breakout day in the practice data")


def test_plan_send_once_and_track(monkeypatch):
    c = setup(monkeypatch)
    data, cut, end = first_day_with_buys(c)
    res = real.evening(c, cut)
    buys = [o for o in res["orders"] if o["action"] == "BUY"]
    assert res["status"] == "ok" and 0 < len(buys) <= c.real.max_buys_per_day
    assert all(o["qty"] * o["close"] <= 200_000 * c.real.max_order_pct / 100 for o in buys)
    tomorrow = data["cal"][end + 1]
    s = Sender()
    out = real.send_orders(c, Kite({o["ts"]: o["close"] for o in buys}), tomorrow, s)
    assert [x[1] for x in s.calls] == ["BUY"] * len(buys) and all(x[2] for x in s.calls)
    assert real.send_orders(c, None, tomorrow, s)["sent"] == [] and len(s.calls) == len(buys)    # once only
    sm = real.summary(c)
    assert sm["account"]["positions"] == len(buys) and len(sm["log"]) == len(buys)
    with get_db() as conn:
        assert TOKEN not in json.dumps([dict(r) for r in conn.execute("SELECT * FROM real_orders")])
    assert out["status"] == "ok"
    nxt = {**data, "cal": data["cal"][:end + 2], "index_close": data["index_close"][:end + 2],
           "index_ok": data["index_ok"][:end + 2], "filters": {k: v[:end + 2] for k, v in data["filters"].items()}}
    real.evening(c, nxt)                                                               # booked at the day's open
    S = data["S"]
    for p in real.summary(c)["positions"]:
        assert p["buy"] == round(S[p["symbol"]]["rows"][S[p["symbol"]]["at"][end + 1]]["open"], 2) and not p["est"]


def test_off_and_kill_switch_send_nothing(monkeypatch):
    c = setup(monkeypatch)
    data, cut, end = first_day_with_buys(c)
    real.evening(c, cut)
    s = Sender()
    real.set_killed(True)
    assert real.send_orders(c, None, data["cal"][end + 1], s)["status"] == "off" and s.calls == []
    real.set_killed(False)
    c.real.enabled = False
    assert real.send_orders(c, None, data["cal"][end + 1], s)["status"] == "off" and s.calls == []


def test_a_failure_is_not_retried(monkeypatch):
    c = setup(monkeypatch)
    data, cut, end = first_day_with_buys(c)
    res = real.evening(c, cut)
    sym = next(o["ts"] for o in res["orders"] if o["action"] == "BUY")
    s = Sender(fail={sym})
    out = real.send_orders(c, None, data["cal"][end + 1], s)
    assert [f["ts"] for f in out["failed"]] == [sym]
    s.fail.clear()
    real.send_orders(c, None, data["cal"][end + 1], s)
    assert sym not in [x[0] for x in s.calls]


def test_stop_watch_sends_exit_once(monkeypatch):
    c = setup(monkeypatch)
    data, cut, end = first_day_with_buys(c)
    res = real.evening(c, cut)
    day = data["cal"][end + 1]
    real.send_orders(c, None, day, Sender())
    pos = real.summary(c)["positions"][0]
    s = Sender()
    above = {p["symbol"]: p["stop"] * 1.05 for p in real.summary(c)["positions"]}
    assert real.watch_stops(c, Kite(above), day, s)["hits"] == [] and s.calls == []
    hit = dict(above, **{pos["symbol"]: pos["stop"] - 1})
    out = real.watch_stops(c, Kite(hit), day, s)
    assert [h["symbol"] for h in out["hits"]] == [pos["symbol"]] and s.calls == [(pos["symbol"], "EXIT", None)]
    real.watch_stops(c, Kite(hit), day, s)
    assert len(s.calls) == 1                                                           # once
    assert real.summary(c)["trades"][0]["why"] == "stop"
    nologin = Kite({})
    nologin.has_token = False
    assert real.watch_stops(c, nologin, day, s)["status"] in ("no_login", "nothing")


def test_switching_the_variant_changes_only_new_buys(monkeypatch):
    c = setup(monkeypatch)
    data, cut, end = first_day_with_buys(c)
    real.evening(c, cut)
    real.send_orders(c, None, data["cal"][end + 1], Sender())
    c.real.variant = "A"
    assert {p["variant"] for p in real.summary(c)["positions"]} == {"E"}


def test_schedule():
    c = cfg()
    at = lambda h, m: datetime(2026, 11, 2, h, m, tzinfo=IST)
    assert real.due(at(9, 10), c, set(), True, None) == []
    assert real.due(at(9, 16), c, set(), True, None) == ["real_send", "real_stops"]
    assert real.due(at(9, 20), c, {"real_send"}, True, at(9, 16)) == []
    assert real.due(at(9, 21), c, {"real_send"}, True, at(9, 16)) == ["real_stops"]                 # every 5 min
    assert real.due(at(15, 31), c, set(), True, None) == [] and real.due(at(9, 16), c, set(), False, None) == []


def test_api(monkeypatch):
    from fastapi.testclient import TestClient
    setup(monkeypatch)
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        d = tc.get("/api/real").json()
        assert d["enabled"] is True and d["variant"] == "E" and d["token_set"] and d["started"] is False
        assert tc.post("/api/real/settings", json={"variant": "Z"}).status_code == 422
        assert tc.post("/api/real/settings", json={"variant": "A", "enabled": False}).json() == {"enabled": False, "variant": "A"}
        assert tc.post("/api/real/kill?on=true").json()["killed"] is True
        assert "real" in tc.get("/api/attention").json()
        assert TOKEN not in tc.get("/api/real").text


def test_capital_drives_the_sizes_and_a_change_is_money_added(monkeypatch):
    c = setup(monkeypatch)
    data, cut, end = first_day_with_buys(c)
    small = {o["symbol"]: o["qty"] for o in real.evening(c, cut)["orders"] if o["action"] == "BUY"}
    assert real.summary(c)["limits"]["size"] == pytest.approx(20_000)
    out = real.set_capital(c, 400_000)                                     # before sending: +2L, BUYs re-sized
    assert out["delta"] == 200_000 and out["size"] == pytest.approx(40_000) and out["max_order"] == pytest.approx(60_000)
    big = {o["symbol"]: o["qty"] for o in real.summary(c)["orders"] if o["action"] == "BUY"}
    assert all(big[k] >= 2 * small[k] - 1 for k in small)
    sm = real.summary(c)
    assert sm["account"]["capital"] == 400_000 and sm["account"]["value"] == pytest.approx(400_000)
    assert sm["account"]["net"] == pytest.approx(0, abs=0.01)                    # adding money is not profit
    real.send_orders(c, None, data["cal"][end + 1], Sender())               # the BUYs use most of the cash
    with pytest.raises(ValueError):
        real.set_capital(c, 10_000)                                        # can't withdraw more than the cash
    assert c.real.capital == 400_000 and real.summary(c)["account"]["capital"] == 400_000


def test_capital_api(monkeypatch):
    from fastapi.testclient import TestClient
    setup(monkeypatch)
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        assert tc.post("/api/real/capital", json={"capital": 5000}).status_code == 422
        assert tc.post("/api/real/capital", json={"capital": 300000}).json()["capital"] == 300000
        d = tc.get("/api/real").json()
        assert d["settings"]["capital"] == 300000 and d["limits"]["size"] == 30000 and d["limits"]["max_order"] == 45000
