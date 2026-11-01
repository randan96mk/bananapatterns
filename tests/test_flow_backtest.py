"""🧪 Intraday Flow backtest: setups, history fetched once, replay with no look-ahead, report, live paper untouched."""

from datetime import date, timedelta

from backend.config import AppConfig
from backend.database.models import get_db, upsert_candles
from backend.intraday import backtest as bt
from backend.intraday import flow, store
from tests.test_flow import Kite, bars, loose, setup


def days(n=8, end=date(2026, 7, 20)):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


def session(fall=False):
    step = -0.2 if fall else 0.2
    rows = bars(15, start=150, step=0.0) + bars(40, start=150 + step * 2.5, step=step, vol=4000, t0=(9, 30))
    rows[-1]["v"] = 20_000
    return rows + bars(320, start=rows[-1]["c"], step=0.0, t0=(10, 10))


def data(monkeypatch):
    stocks = setup(monkeypatch)
    ds = days()
    with get_db() as conn:
        upsert_candles(conn, flow.NIFTY_TOKEN, [{"date": d, "open": 1, "high": 1, "low": 1, "close": 100 + k, "volume": 0}
                                                for k, d in enumerate(ds)])
    for k, d in enumerate(ds):
        for s in stocks:
            store.save(s["instrument_token"], d, session(fall=(s["instrument_token"] + k) % 2 == 0), "kite")
    loose()
    return stocks, ds


def test_setups_save_load_delete():
    loose()
    bt.save_setup("loose 1m")
    flow.set_node("trigger", {"interval": 15})
    assert flow.node_configs()["trigger"]["interval"] == 15
    assert bt.configs_of("loose 1m")["trigger"]["interval"] == 1
    bt.load_setup("loose 1m")
    assert flow.node_configs()["trigger"]["interval"] == 1
    bt.delete_setup("loose 1m")
    assert "loose 1m" not in bt.setups()


def test_fetch_only_missing_once():
    k = Kite()
    ds = days(5)
    got = bt.fetch_missing(k, [7, 8], ds)
    assert got == {"missing": 0, "fetched": 10} and len(k.calls) == 2              # one request per stock
    assert bt.fetch_missing(k, [7, 8], ds)["fetched"] == 0 and len(k.calls) == 2   # saved: never again
    assert bt.fetch_missing(None, [9], ds) == {"missing": 5, "fetched": 0}         # no login: counted, not fetched


def test_replay_uses_no_future_bars(monkeypatch):
    stocks, ds = data(monkeypatch)
    cfgs = bt.configs_of(bt.CANVAS)
    full = {s["instrument_token"]: session() for s in stocks}
    cut = {t: [b for b in v if b["t"] < "10:30"] for t, v in full.items()}       # the afternoon does not exist
    a = bt.replay_day(ds[-1], full, {}, {}, {s["symbol"]: 1 for s in stocks}, stocks, cfgs)
    b = bt.replay_day(ds[-1], cut, {}, {}, {s["symbol"]: 1 for s in stocks}, stocks, cfgs)
    key = lambda ps: sorted((p["symbol"], p["side"], p["entry_t"], p["entry"]) for p in ps)
    assert a and key(a) == key(b)                                                    # same entries without the future


def test_run_two_setups_report_and_live_untouched(monkeypatch):
    stocks, ds = data(monkeypatch)
    bt.save_setup("long only")
    with get_db() as conn:
        import json
        from backend.database.models import get_setting, set_setting
        s = json.loads(get_setting(conn, "flow.setups"))
        s["long only"]["nodes"]["context"]["direction"] = "long"
        set_setting(conn, "flow.setups", json.dumps(s))
    res = bt.run(AppConfig(), None, [bt.CANVAS, "long only"], "6m")
    assert res["status"] == "ok" and res["covered"] == len(ds)
    r = {x["setup"]: x for x in bt.results("6m")}
    a, b = r[bt.CANVAS], r["long only"]
    assert a["trades"] > b["trades"] > 0 and set(a["by_side"]) == {"long", "short"} and set(b["by_side"]) == {"long"}
    assert abs(a["net"] - sum(t["net"] for t in a["rows"])) < 0.5 and len(a["curve"]["net"]) == len(ds)
    assert a["stress"]["net"] < a["net"] and a["costs"] > 0 and [x["name"] for x in a["splits"]][0] == "Development"
    assert all(t["entry_t"] > "09:30" for t in a["rows"])
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM flow_positions").fetchone()[0] == 0      # live paper untouched
    bt.run(AppConfig(), None, [bt.CANVAS], "6m")
    assert len(bt.results("6m")) == 2                                                    # latest per setup and period


def test_api(monkeypatch):
    from fastapi.testclient import TestClient
    data(monkeypatch)
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        assert tc.post("/api/flow/setups", json={"name": ""}).status_code == 422
        assert tc.post("/api/flow/setups", json={"name": "A1"}).status_code == 200
        d = tc.get("/api/flow/backtest").json()
        assert list(d["setups"]) == [bt.CANVAS, "A1"] and d["periods"] == ["6m", "1y", "2y"]
        assert tc.post("/api/flow/backtest", json={"setups": ["nope"], "period": "1y"}).json()["status"] == "bad_request"
        assert tc.post("/api/flow/setups/load", json={"name": "zz"}).status_code == 404
        assert tc.post("/api/flow/setups/delete", json={"name": "A1"}).json() == {"deleted": "A1"}
