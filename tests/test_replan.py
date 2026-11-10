"""↻ Re-evaluate: the next open's plan rebuilt by hand from the evening's data and the account as it is now. Full
slots list every waiting breakout with its reason and suggest the max_pos that puts the idle cash to work; a raised
max_pos adds BUYs sized at value / N within the cash and the daily limit; sent orders are never duplicated; a deposit
re-plans at once; the filter OFF buys nothing; the API for Real and Paper. Nothing is ever sent."""

import json

import pytest

from backend.database.models import get_db, get_setting
from backend.engine import paper, real
from backend.engine import trend_breakout as tb
from tests.test_paper import cfg, store
from tests.test_real import TOKEN, Sender, first_day_with_buys, setup


def setup120(monkeypatch):
    """Like test_real.setup, with 120 practice stocks: some sessions then have 3-4 breakouts at once."""
    store(monkeypatch, n_stocks=120)
    monkeypatch.setenv("TRADETRON_TOKEN", TOKEN)
    return cfg()


def day_with_breakouts(c, at_least=3):
    """The practice data cut back to a session with at least `at_least` ranked breakouts under the filter."""
    data = tb.load(c)
    p = tb.rules(c)
    for end in range(len(data["cal"]) - 150, len(data["cal"])):
        on, _ = tb.slots(end, data["index_ok"], data["filters"], p)
        _, ranked, _, _ = tb.decide(end, data["S"], {}, True, p)
        if on and len(ranked) >= at_least:
            cut = {**data, "cal": data["cal"][:end + 1], "index_close": data["index_close"][:end + 1],
                   "index_ok": data["index_ok"][:end + 1], "filters": {k: v[:end + 1] for k, v in data["filters"].items()}}
            return cut, end, ranked
    raise AssertionError("no session with that many breakouts in the practice data")


def full_account(c, cut, end, n=None, cash=100_000.0):
    """A real account whose slots are all taken by stocks that are not today's breakouts, with `cash` idle."""
    S, p = cut["S"], tb.rules(c)
    _, ranked, _, _ = tb.decide(end, S, {}, True, p)
    above = lambda s: (tb.val(S[s], S[s]["at"][end], f"sma{p.exit_sma}") or 1e12) < S[s]["rows"][S[s]["at"][end]]["close"]
    others = [s for s in S if s not in {r[0] for r in ranked} and end in S[s]["at"] and above(s)]   # no EXIT due
    n = n or c.breakout.max_pos
    positions = {}
    for s in others[:n]:
        px = S[s]["rows"][S[s]["at"][end]]["close"]
        positions[s] = {"qty": 10, "px": px, "last": px, "d": cut["cal"][0], "stop": px * 0.5, "charges": 0.0, "ts": s, "days": 5}
    value = cash + sum(x["qty"] * x["last"] for x in positions.values())
    st = {"started": cut["cal"][0], "capital": value, "cash": cash, "charges": 0.0, "as_of": cut["cal"][-1],
          "positions": positions, "orders": [], "decided": cut["cal"][-1]}
    real._save(st)
    return st


def test_full_slots_list_every_waiting_breakout_and_suggest_a_max_pos(monkeypatch):
    c = setup120(monkeypatch)
    cut, end, ranked = day_with_breakouts(c)
    st = full_account(c, cut, end)
    _, cap = tb.slots(end, cut["index_ok"], cut["filters"], tb.rules(c))
    st["positions"] = dict(list(st["positions"].items())[:cap])             # exactly the cap (HALF: 5) is full
    real._save(st)
    r = real.replan(c, cut)
    assert r["status"] == "ok" and r["before"] == [] and r["added"] == [] and r["removed"] == []
    assert [o for o in r["after"] if o["action"] == "BUY"] == []
    assert [w["symbol"] for w in r["waiting"]] == [s for s, _ in ranked]
    assert all(w["why"].startswith(f"no free slot ({cap} of {cap})") for w in r["waiting"])
    assert r["slots_used"] == cap and r["slots"] == cap and r["idle_cash"] == pytest.approx(100_000)
    assert r["size"] == pytest.approx(real._value(st) / c.breakout.max_pos, abs=0.01) and r["max_pos"] == 10
    sm = real.summary(c)
    assert sm["waiting"] == len(ranked) and sm["waiting_why"] == r["waiting"] and sm["orders"] == []
    sug = r["suggestion"]
    assert sug and sug["max_pos"] > 10 and sug["max_pos"] <= 10 + len(ranked)
    assert sug["fits"] == len(sug["symbols"]) >= 1 and sug["symbols"] == [w["symbol"] for w in r["waiting"]][:sug["fits"]]
    assert sug["size"] == pytest.approx(real._value(st) / sug["max_pos"], abs=0.01)
    assert sug["used"] + sug["left"] == pytest.approx(r["idle_cash"], abs=0.01) and sug["used"] <= r["idle_cash"]
    assert sm["account"]["cash"] == pytest.approx(100_000)                   # nothing moved, nothing sent


def test_a_raised_max_pos_adds_buys_sized_at_value_over_n(monkeypatch):
    c = setup120(monkeypatch)
    cut, end, ranked = day_with_breakouts(c)
    st = full_account(c, cut, end)
    _, cap = tb.slots(end, cut["index_ok"], cut["filters"], tb.rules(c))
    st["positions"] = dict(list(st["positions"].items())[:cap])
    real._save(st)
    sug = real.replan(c, cut)["suggestion"]
    c.breakout.max_pos = sug["max_pos"]
    r = real.replan(c, cut)
    buys = [o for o in r["after"] if o["action"] == "BUY"]
    assert [o["symbol"] for o in r["added"]] == [o["symbol"] for o in buys] == sug["symbols"]
    value = real._value(real._state())
    size = value / sug["max_pos"]
    for o in buys:                                                          # value / N, whole shares, within the cash
        assert o["qty"] == int(min(size, value * c.real.max_order_pct / 100) / (o["close"] * 1.0012)) >= 1
        assert o["risk"] == pytest.approx(o["qty"] * (o["close"] - o["stop"]), abs=0.01)
    assert sum(o["qty"] * o["close"] * 1.0012 for o in buys) <= 100_000
    assert len(buys) <= c.real.max_buys_per_day
    assert r["idle_cash"] == pytest.approx(100_000 - sum(o["qty"] * o["close"] * 1.0012 for o in buys), abs=0.01)
    assert [w["symbol"] for w in r["waiting"]] == [s for s, _ in ranked][len(buys):]
    assert real.summary(c)["orders"] == r["after"] and real.summary(c)["max_pos"] == sug["max_pos"]
    # the daily limit: one buy a day leaves the rest waiting with that reason
    c.real.max_buys_per_day = 1
    r2 = real.replan(c, cut)
    assert sum(o["action"] == "BUY" for o in r2["after"]) == 1
    assert r2["waiting"][0]["why"] == "daily limit (1 a day)"
    assert [o["symbol"] for o in r2["removed"]] == [o["symbol"] for o in buys[1:]]


def test_sent_orders_are_kept_and_not_duplicated(monkeypatch):
    c = setup(monkeypatch)
    data, cut, end = first_day_with_buys(c)
    res = real.evening(c, cut)
    buys = [o for o in res["orders"] if o["action"] == "BUY"]
    tomorrow = data["cal"][end + 1]
    s = Sender(fail={buys[-1]["ts"]} if len(buys) > 1 else ())
    real.send_orders(c, None, tomorrow, s)
    before = real.summary(c)["orders"]
    assert {o["sent"] for o in before} <= {"sent", "failed"}
    r = real.replan(c, cut)
    assert r["status"] == "ok" and r["added"] == [] and r["removed"] == []
    assert r["after"][:len(before)] == before                                # today's sent and failed orders, untouched
    keys = [(o["symbol"], o["action"]) for o in r["after"]]
    assert len(keys) == len(set(keys))
    assert all(o["symbol"] not in real._state()["positions"] for o in r["after"] if o["action"] == "BUY" and not o.get("sent"))
    # the sent BUYs count toward the daily limit: a bigger account adds nothing past it
    c.real.max_buys_per_day = len(buys)
    assert [o for o in real.replan(c, cut)["after"] if not o.get("sent") and o["action"] == "BUY"] == []
    assert len(s.calls) == len(buys) - (1 if len(buys) > 1 else 0)          # replan sent nothing


def test_a_deposit_replans_and_the_api_reports_it(monkeypatch):
    from fastapi.testclient import TestClient
    c = setup120(monkeypatch)
    cut, end, ranked = day_with_breakouts(c)
    full_account(c, cut, end, cash=1_000.0)
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        from backend.api import routes
        routes.S().config.breakout.test_years, routes.S().config.breakout.warmup_years = 1.0, 1.4
        r = tc.post("/api/real/replan").json()
        assert r["status"] == "ok" and r["suggestion"] is None               # Rs 1,000 funds nothing
        assert all(w["why"].startswith("no free slot") for w in r["waiting"]) and r["cash"] == 1_000
        cap = routes.S().config.real.capital if not real.has_account() else real._state()["capital"]
        out = tc.post("/api/real/capital", json={"capital": cap + 200_000}).json()
        assert out["delta"] == 200_000 and out["replan"]["status"] == "ok"
        rp = out["replan"]
        assert rp["cash"] == pytest.approx(201_000) and rp["idle_cash"] == pytest.approx(201_000)
        assert rp["suggestion"] and rp["suggestion"]["max_pos"] > 10 and rp["suggestion"]["fits"] >= 1
        n = rp["suggestion"]["max_pos"]
        assert tc.post("/api/real/replan", json={"max_pos": 0}).status_code == 422
        r2 = tc.post("/api/real/replan", json={"max_pos": n}).json()
        assert r2["max_pos"] == n and [o["symbol"] for o in r2["added"]] == rp["suggestion"]["symbols"]
        assert tc.get("/api/real").json()["max_pos"] == n and tc.get("/api/config/breakout").json()["max_pos"] == n
        with get_db() as conn:                                               # saved through the settings path, logged
            assert json.loads(get_setting(conn, "config.breakout"))["max_pos"] == n
            assert f"max_pos: 10 → {n}" in json.loads(get_setting(conn, "config.changes"))[0]["what"]
        assert tc.get("/api/real").json()["orders"] == r2["after"]
        assert tc.get("/api/real").json()["log"] == []                       # nothing was sent


def test_filter_off_buys_nothing_and_says_why(monkeypatch):
    c = setup(monkeypatch)
    data = tb.load(c)
    p = tb.rules(c)
    end = next(i for i in range(len(data["cal"]) - 1, 0, -1)
               if not tb.slots(i, data["index_ok"], data["filters"], p)[0] and tb.decide(i, data["S"], {}, True, p)[1])
    cut = {**data, "cal": data["cal"][:end + 1], "index_close": data["index_close"][:end + 1],
           "index_ok": data["index_ok"][:end + 1], "filters": {k: v[:end + 1] for k, v in data["filters"].items()}}
    full_account(c, cut, end, n=0, cash=300_000.0)
    r = real.replan(c, cut)
    assert [o for o in r["after"] if o["action"] == "BUY"] == [] and r["waiting"]
    assert {w["why"] for w in r["waiting"]} == {"filter OFF"} and r["suggestion"] is None and r["slots"] == 0
    assert real.summary(c)["filter"]["state"] == "OFF"


def test_not_started_and_no_data():
    c = cfg()
    assert real.replan(c) == {"status": "not_started"} and paper.replan(c) == {"status": "not_started"}


def test_paper_replan_after_a_deposit_and_a_raised_max_pos(monkeypatch):
    store(monkeypatch)
    c = cfg()
    data = tb.load(c)
    s = len(data["cal"]) - 200
    paper.start(c, at=data["cal"][s])
    paper.process(c, data)
    st = paper._states()["E"]
    p = tb.rules(c)
    S, i = data["S"], len(data["cal"]) - 1
    _, ranked, _, _ = tb.decide(i, S, {}, True, p)
    above = lambda x: (tb.val(S[x], S[x]["at"][i], f"sma{p.exit_sma}") or 1e12) < S[x]["rows"][S[x]["at"][i]]["close"]
    others = [x for x in S if x not in {r[0] for r in ranked} and i in S[x]["at"] and x not in st["positions"] and above(x)]
    for x in others:                                      # fill the slots with other stocks
        if len(st["positions"]) >= (st.get("cap", p.max_pos) if st.get("allowed", True) else p.max_pos):
            break
        px = S[x]["rows"][S[x]["at"][i]]["close"]
        st["positions"][x] = {"qty": 5, "px": px, "last": px, "prev": px, "best": px, "d": data["cal"][s], "stop": px * 0.5,
                              "charges": 0.0, "slippage": 0.0, "days": 3}
    st["cash"] = 100_000.0
    paper._save("E", st, [], [], [])
    before = paper.summary(c)["accounts"]["E"]
    r = paper.replan(c, data)
    assert r["status"] == "ok" and r["account"] == "E" and set(r["accounts"]) == {"E"}
    assert r["decided"] == before["decided"] == data["cal"][-1]
    assert [o["symbol"] for o in r["after"] if o["action"] == "BUY"] == [x for x, _ in ranked]   # paper keeps every BUY row
    if r["waiting"]:
        why = "no free slot" if st.get("allowed", True) else "filter OFF"
        assert r["slots_used"] >= r["slots"] and all(w["why"].startswith(why) for w in r["waiting"])
        assert r["taken"] == [] and r["idle_cash"] == pytest.approx(100_000)
        sug = r["suggestion"]
        if sug:
            c.breakout.max_pos = sug["max_pos"]
            r2 = paper.replan(c, data)
            assert [t["symbol"] for t in r2["taken"]] == sug["symbols"] and r2["max_pos"] == sug["max_pos"]
            for t in r2["taken"]:                         # sized at value / N
                assert t["qty"] == int(min(r2["value"] / sug["max_pos"], 100_000) / (t["close"] * (1 + p.slippage_pct / 100) * 1.0012))
            assert r2["idle_cash"] == pytest.approx(sug["left"], abs=1.0)
    out = paper.set_capital(c, st["capital"] + 50_000)
    assert out["delta"] == 50_000
    r3 = paper.replan(c, data)
    assert r3["cash"] == pytest.approx(150_000) and r3["size"] == pytest.approx(r3["value"] / c.breakout.max_pos, abs=0.01)
    assert paper.summary(c)["accounts"]["E"]["orders"] == r3["after"]
    # nothing was filled or sent
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_fills WHERE day > ?", (data["cal"][-1],)).fetchone()[0] == 0


def test_paper_api(monkeypatch):
    from fastapi.testclient import TestClient
    store(monkeypatch)
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        from backend.api import routes
        routes.S().config.breakout.test_years, routes.S().config.breakout.warmup_years = 1.0, 1.4
        assert tc.post("/api/paper/replan").json() == {"status": "not_started"}
        assert tc.post("/api/paper/start", json={"capital": 200000}).json()["status"] == "ok"
        r = tc.post("/api/paper/replan").json()
        assert r["status"] == "ok" and r["max_pos"] == 10 and "waiting" in r and "suggestion" in r
        out = tc.post("/api/paper/capital", json={"capital": 250000}).json()
        assert out["status"] == "ok" and out["replan"]["status"] == "ok" and out["replan"]["capital"] == 250000
        r2 = tc.post("/api/paper/replan", json={"max_pos": 12}).json()
        assert r2["max_pos"] == 12 and r2["size"] == pytest.approx(r2["value"] / 12, abs=0.01)
        assert tc.get("/api/paper").json()["max_pos"] == 12 and tc.get("/api/config/breakout").json()["max_pos"] == 12
