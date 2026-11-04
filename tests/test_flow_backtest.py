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


def _wait(cond, secs=60):
    import time
    t0 = time.time()
    while time.time() - t0 < secs and not cond():
        time.sleep(0.2)
    return cond()


def test_own_process_run_and_stop(monkeypatch):
    stocks, ds = data(monkeypatch)
    assert bt.start(AppConfig(), None, [bt.CANVAS], "6m")["status"] == "started"
    assert bt.start(AppConfig(), None, [bt.CANVAS], "6m")["status"] == "busy"
    assert _wait(lambda: bt.status()["state"] == "idle", 120)                  # the replay process finished
    assert bt.status()["message"].startswith("done") and bt.results("6m")[0]["trades"] > 0
    bt._job.join(30)                                                            # the first run has fully ended
    bt._proc = None
    assert bt.start(AppConfig(), None, [bt.CANVAS], "6m")["status"] == "started"
    assert _wait(lambda: bt._proc is not None and bt._proc.is_alive(), 60)            # the replay process is running
    assert bt.cancel()["status"] == "stopping"
    bt._job.join(30)
    st = bt.status()
    assert st["state"] == "idle" and (st["message"].startswith("⏹ stopped") or st["message"].startswith("done"))
    assert not (bt._proc and bt._proc.is_alive())


def test_stale_running_status_is_cleared():
    bt._set(state="running", message="replaying", pct=50)
    assert bt.status()["state"] == "idle" and "restarted" in bt.status()["message"]


def test_backtest_with_limit_entry(monkeypatch):
    stocks, ds = data(monkeypatch)
    flow.set_node("stop", {"limit_on": True, "limit_min": 10})
    assert bt.run(AppConfig(), None, [bt.CANVAS], "6m")["status"] == "ok"
    r = bt.results("6m")[0]
    assert r["trades"] > 0
    assert all((t["entry"] <= t["signal_price"] + 1e-6) if t["side"] == "long" else (t["entry"] >= t["signal_price"] - 1e-6)
               for t in r["rows"])                                                    # never worse than the signal price
    assert bt.configs_of(bt.CANVAS)["stop"]["limit_on"] is True


def test_five_minute_signals_fill_only_after_the_candle_closes(monkeypatch):
    """A signal on the 9:30-9:34 candle is known at 9:35: the paper fill must be the 9:35 bar or later, never a bar
    inside the candle (that was a look-ahead before)."""
    stocks, ds = data(monkeypatch)
    flow.set_node("trigger", {"interval": 5})
    flow.set_node("filters", {"rvol_on": False})
    cfgs = bt.configs_of(bt.CANVAS)
    full = {s["instrument_token"]: session() for s in stocks}
    ps = bt.replay_day(ds[-1], full, {}, {}, {s["symbol"]: 1 for s in stocks}, stocks, cfgs)
    assert ps
    m = lambda t: int(t[:2]) * 60 + int(t[3:5])
    for p in ps:
        assert (m(p["signal_t"]) + 1 - (9 * 60 + 15)) % 5 == 0                       # the candle's last minute
        assert m(p["entry_t"]) >= m(p["signal_t"]) + 1                               # filled after it, not inside it
    assert flow.candle_last_minute("09:25", 5) == "09:29" and flow.candle_last_minute("10:05", 1) == "10:05"


def test_indicators_continued_candle_by_candle_equal_the_whole_list():
    """The backtest continues RSI and VWAP from the candle before: the same numbers as over every bar."""
    import random
    from backend.intraday import indicators as ind
    random.seed(3)
    px, rows = 100.0, []
    for k in range(120):
        px *= 1 + random.gauss(0, 0.003)
        rows.append({"t": f"{9 + (15 + k) // 60:02d}:{(15 + k) % 60:02d}", "o": px, "h": px * 1.001, "l": px * 0.999, "c": px,
                     "v": random.randint(100, 900)})
    closes = [b["c"] for b in rows]
    st = sv = None
    for k in range(0, 120, 7):                                      # fed in uneven slices, as candles close
        r, st = ind.rsi_state(closes[k:k + 7], state=st)
        v, sv = ind.vwap_state(rows[k:k + 7], sv)
        full = ind.rsi(closes[:k + 7])
        assert (r is None and full is None) or abs(r - full) < 1e-9
        assert abs(v - ind.vwap(rows[:k + 7])) < 1e-9
    assert ind.rsi(closes[:14]) is None and ind.rsi(closes[:15]) is not None


def test_replay_indicators_match_the_live_context(monkeypatch):
    """_Replay's cached RSI / VWAP / RVOL at each candle equal flow.Ctx's plain computation on the same bars."""
    stocks, ds = data(monkeypatch)
    it = {"symbol": stocks[0]["symbol"], "token": stocks[0]["instrument_token"]}
    today, hist = session(), {it["token"]: session(fall=True)}
    view, cache = bt._View({it["token"]: today}), {}
    for t in ("09:30", "09:45", "10:10", "11:00", "15:29"):
        view.t = t
        rp = bt._Replay(ds[-1], 5, view, hist, {}, {}, None, "paper", t, book=[], signalled=set())
        rp.cache = cache
        plain = flow.Ctx(ds[-1], 5, view, hist, {}, {}, None, "paper", t)
        for name in ("rsi_of", "vwap_of", "rvol_of"):
            a, b = getattr(rp, name)(it), getattr(plain, name)(it)
            assert (a is None and b is None) or abs(a - b) < 1e-9, (t, name, a, b)
    assert rp.bars(it) == plain.bars(it)


def test_report_entry_time_buckets_in_time_order_and_saved_periods(monkeypatch):
    data(monkeypatch)
    mk = lambda t, net: {"day": "2026-07-20", "entry_t": t, "net": net, "net_stress": net, "plain": net, "gross": net, "costs": 0,
                         "side": "long", "reason": "stop", "r": 1}
    rep = bt.report([mk("12:10", 1), mk("09:20", 2), mk("10:40", 3), mk("09:50", 4)], ["2026-07-20"], 100000, {})
    assert list(rep["by_time"]) == ["9:15–9:35", "9:35–10:00", "10:30–12:00", "after 12:00"]
    assert bt.saved() == {"periods": {}, "latest": None}
