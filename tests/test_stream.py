"""📡 /api/stream: one connection, a topic per event, unknown topics ignored."""

import json


def test_stream_once(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        r = tc.get("/api/stream?topics=schedule,attention,kite,flow,flow_live,backtest,nope&once=1")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = {}
        for block in r.text.split("\n\n"):
            lines = dict(x.split(": ", 1) for x in block.splitlines() if x.startswith(("event: ", "data: ")))
            if "event" in lines:
                events[lines["event"]] = json.loads(lines["data"])
        assert set(events) == {"schedule", "attention", "kite", "flow", "flow_live", "backtest"}
        assert "jobs" in events["schedule"] and "nodes" in events["flow"] and "rows" in events["flow_live"]
        assert events["backtest"]["state"] == "idle" and "has_token" in events["kite"]


def test_overview_and_portfolio(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        o = tc.get("/api/overview").json()
        assert "intraday" in o["accounts"] and o["positions"] == [] and "today" in o
        p = tc.get("/api/portfolio").json()
        assert set(p) == {"positions", "trades", "orders", "daily", "curves", "names"}


def test_overview_unifies_the_accounts(monkeypatch):
    from backend.engine import overview as ov
    from backend.engine import paper
    from backend.engine import trend_breakout as tb
    from tests.test_paper import cfg, store
    store(monkeypatch)
    c = cfg()
    data = tb.load(c)
    paper.start(c, at=data["cal"][-200])
    paper.process(c, data)
    o = ov.overview(c)
    sm = paper.summary(c)["accounts"]
    n_pos = sum(len(a["positions"]) for a in sm.values())
    assert len(o["positions"]) == n_pos and {p["book"] for p in o["positions"]} <= {"paper_A", "paper_E"}
    assert set(o["accounts"]) >= {"paper_A", "paper_E", "intraday"} and o["accounts"]["paper_E"]["name"] == "Paper E+"
    p = ov.portfolio(c, days=3650)
    assert len(p["trades"]) == sum(a["closed"] for a in sm.values()) and p["trades"][0]["closed"] >= p["trades"][-1]["closed"]
    assert set(p["curves"]) >= {"paper_A", "paper_E"} and len(p["daily"]["paper_A"]) == len(p["curves"]["paper_A"]) - 1
    assert all(t["account"] and t["symbol"] and t["net"] is not None for t in p["trades"]) and p["orders"]
