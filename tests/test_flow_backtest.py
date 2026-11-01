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
        assert list(d["setups"]) == [bt.CANVAS, *bt.PRESETS, "A1"] and d["setups"][next(iter(bt.PRESETS))]["preset"] and d["periods"] == ["6m", "1y", "2y"]
        assert tc.post("/api/flow/backtest", json={"setups": ["nope"], "period": "1y"}).json()["status"] == "bad_request"
        assert tc.post("/api/flow/setups/load", json={"name": "zz"}).status_code == 404
        assert tc.post("/api/flow/setups/delete", json={"name": "A1"}).json() == {"deleted": "A1"}
        assert tc.post("/api/flow/setups/delete", json={"name": next(iter(bt.PRESETS))}).status_code == 422
        assert tc.post("/api/flow/setups", json={"name": next(iter(bt.PRESETS))}).status_code == 422


def test_charges_add_up_and_presets_run(monkeypatch):
    stocks, ds = data(monkeypatch)
    names = [bt.CANVAS, *bt.PRESETS]
    assert bt.run(AppConfig(), None, names, "6m")["status"] == "ok"
    for r in bt.results("6m"):
        c = r["charges"]
        assert abs(c["plain"] - c["slip"] - c["costs"] - c["net"]) < 1 and abs(c["net"] - r["net"]) < 1
        assert len(r["curve"]["plain"]) == len(ds) and (not r["trades"] or c["costs"] > 0)
    r = {x["setup"]: x for x in bt.results("6m")}
    assert r[bt.CANVAS]["trades"] > 0 and r[bt.CANVAS]["charges"]["plain"] > r[bt.CANVAS]["net"]


def test_in_play_open_direction_and_daily_atr_stop(monkeypatch):
    from backend.intraday import modules as m
    stocks, ds = data(monkeypatch)
    cfgs = bt.configs_of(bt.CANVAS)
    bars = {s["instrument_token"]: bt_session(k) for k, s in enumerate(stocks)}
    ctx = bt._Replay(ds[-1], 5, bt._View(bars), {}, {s["symbol"]: {"atr14": 4.0} for s in stocks},
                     {s["symbol"]: 1e6 for s in stocks}, None, "paper", "10:00", book=[], signalled=set())
    ctx.feed.t = "10:00"
    items = [{"symbol": s["symbol"], "token": s["instrument_token"]} for s in stocks]
    out, drops = m.Universe().run(ctx, items, {**cfgs["universe"], "in_play": True, "in_play_top": 2})
    assert [x["symbol"] for x in out] == ["S5", "S4"] and drops["not in play"] == 4       # the biggest openings
    got, _ = m.OpeningRangeBreakout().run(ctx, [{**x, "sides": ["long", "short"]} for x in out],
                                         {"minutes": 5, "with_open": True})
    assert got and all(x["side"] == "long" for x in got)                                # green opening: long only
    st, _ = m.StopTarget().run(ctx, got, {**cfgs["stop"], "stop": "daily_atr", "daily_atr_frac": 0.1, "min_stop_pct": 0})
    assert st and abs(st[0]["entry"] - st[0]["sl"] - 0.4) < 0.01                        # 10% of the daily ATR


def bt_session(k):
    """A green first 5 minutes with volume growing by stock, then a climb."""
    rows = bars(5, start=150, step=0.1, vol=1000 * (k + 1)) + bars(40, start=150.6, step=0.2, vol=1000, t0=(9, 20))
    return rows


def test_costs_model_rebuilds_the_report(monkeypatch):
    from backend.intraday import positions as pos
    stocks, ds = data(monkeypatch)
    bt.run(AppConfig(), None, [bt.CANVAS], "6m")
    base = bt.results("6m")[0]
    z = bt.results("6m", dict(bt.DEFAULT_COSTS))[0]
    assert abs(z["net"] - base["net"]) < 1 and z["charges"]["brk"] > 0 and "orders" not in z["rows"][0]
    free = bt.results("6m", {"slip": False, "gov": False, "brk_pct": 0, "brk_rs": 0})[0]
    assert abs(free["net"] - free["charges"]["plain"]) < 0.5 and free["charges"]["costs"] == 0      # nothing taken
    flat = bt.results("6m", {"slip": True, "gov": True, "brk_pct": 0, "brk_rs": 10})[0]
    assert abs(flat["charges"]["brk"] - 10 * 1.18 * 2 * flat["trades"]) < 1                           # ₹10 an order + GST
    assert free["net"] > flat["net"] and free["cagr"] > flat["cagr"] and len(free["curve"]["net"]) == len(ds)
    b, g = bt.order_costs([[100_000, True], [100_500, False]], bt.DEFAULT_COSTS)
    assert abs(b + g - pos.intraday_charges(100_000, 100_500)) < 0.01                                 # same as live
    assert abs(bt.brokerage(10_000, {"brk_pct": 0.03, "brk_rs": 20}) - 3) < 1e-9 and bt.brokerage(10_000, {"brk_pct": 0, "brk_rs": 0}) == 0


def test_api_costs_params(monkeypatch):
    from fastapi.testclient import TestClient
    data(monkeypatch)
    bt.run(AppConfig(), None, [bt.CANVAS], "6m")
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        a = tc.get("/api/flow/backtest").json()["results"][0]
        b = tc.get("/api/flow/backtest?slip=false&gov=false&brk_pct=0&brk_rs=0").json()["results"][0]
        assert b["net"] > a["net"] and b["costs_model"]["brk_rs"] == 0 and "raw" not in a
        c = tc.get(f"/api/flow/backtest/csv?setup={bt.CANVAS}&period=6m&brk_pct=0&brk_rs=0")
        lines = c.text.strip().splitlines()
        assert c.status_code == 200 and lines[1].startswith("date,stock,side") and len(lines) == 2 + a["trades"]
        assert "attachment" in c.headers["content-disposition"]
        assert tc.get("/api/flow/backtest/csv?setup=nope&period=6m").status_code == 404


def test_capital_rescales_the_report(monkeypatch):
    stocks, ds = data(monkeypatch)
    bt.run(AppConfig(), None, [bt.CANVAS], "6m")
    one = bt.results("6m", {**bt.DEFAULT_COSTS, "brk_rs": 0, "brk_pct": 0})[0]
    two = bt.results("6m", {**bt.DEFAULT_COSTS, "brk_rs": 0, "brk_pct": 0, "capital": 2 * one["capital"]})[0]
    assert two["capital"] == 2 * one["capital"] and two["run_capital"] == one["capital"]
    assert abs(two["net"] - 2 * one["net"]) < 0.02 * abs(one["net"]) + 5 and abs(two["return"] - one["return"]) < 0.5
    t = two["rows"][0]
    assert {"signal_t", "signal_price", "sl", "target", "exits", "plain", "brk", "gov", "net"} <= set(t)
