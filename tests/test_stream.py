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
