"""⚡ Intraday Flow: ticks → 1-minute candles, resampling, indicators, the saved store (fetch only what's missing),
modules filtering, a whole run with drop reasons and signals, the schedule of runs, the day close, the API."""

from datetime import datetime, timedelta

import pytest

from backend.config import IST, AppConfig
from backend.database.models import get_db, upsert_candles
from backend.intraday import flow, store
from backend.intraday import indicators as ind
from backend.intraday.candles import CandleBuilder, resample

DAY = "2026-11-03"


def at(h, m, s=0):
    return datetime(2026, 11, 3, h, m, s, tzinfo=IST)


def test_ticks_build_minute_bars():
    b = CandleBuilder()
    b.add(1, 100, 1000, at(9, 15, 1))
    b.add(1, 102, 1500, at(9, 15, 30))
    b.add(1, 99, 1600, at(9, 15, 50))
    b.add(1, 101, 2000, at(9, 16, 5))
    assert b.day(1) == [{"t": "09:15", "o": 100, "h": 102, "l": 99, "c": 99, "v": 600}]
    b.close_until("09:17")
    assert b.day(1)[-1] == {"t": "09:16", "o": 101, "h": 101, "l": 101, "c": 101, "v": 400}


def bars(n, start=100.0, step=0.1, vol=1000, t0=(9, 15)):
    out, p = [], start
    for i in range(n):
        m = t0[0] * 60 + t0[1] + i
        o, p = p, p + step
        out.append({"t": f"{m // 60:02d}:{m % 60:02d}", "o": o, "h": max(o, p) + .05, "l": min(o, p) - .05, "c": p, "v": vol})
    return out


def test_resample_and_indicators():
    five = resample(bars(15), 5)
    assert [b["t"] for b in five] == ["09:15", "09:20", "09:25"] and five[0]["v"] == 5000
    assert ind.rsi([float(x) for x in range(30)]) == 100.0
    assert ind.ema([1.0] * 20, 9) == pytest.approx(1.0)
    assert ind.vwap([{"h": 11, "l": 9, "c": 10, "v": 10}, {"h": 21, "l": 19, "c": 20, "v": 30}]) == pytest.approx(17.5)
    assert ind.rvol([{"v": 10}] * 20 + [{"v": 30}]) == pytest.approx(3.0)
    assert ind.supertrend_up(bars(60)) is True and ind.supertrend_up(bars(60, step=-0.1)) is False


class Kite:
    has_token = True

    def __init__(self):
        self.calls = []

    def historical_minute(self, token, lo, hi, label=""):
        self.calls.append((token, lo.isoformat(), hi.isoformat()))
        d = lo
        rows = []
        while d <= hi:
            for b in bars(375):
                rows.append({"date": datetime.fromisoformat(f"{d} {b['t']}"), "open": b["o"], "high": b["h"],
                             "low": b["l"], "close": b["c"], "volume": b["v"]})
            d += timedelta(days=1)
        return rows


def test_store_fetches_only_what_is_missing():
    k = Kite()
    store.fetch(k, 7, ["2026-10-30", "2026-11-02"])
    assert store.missing([7, 8], ["2026-10-30", "2026-11-02"]) == [(8, "2026-10-30"), (8, "2026-11-02")]
    assert len(store.load(7, "2026-11-02")) == 375 and len(store.history(7, DAY, 2)) == 750
    store.save(9, DAY, bars(100), "ticks")
    assert store.missing([9], [DAY]) == [(9, DAY)]                            # incomplete: re-fetched at day close
    store.prune(3, "2026-11-10")
    assert len(store.load(7, "2026-10-30")) == 75                              # shrunk to 5-minute bars


def setup(monkeypatch, n=6):
    stocks = []
    with get_db() as conn:
        for k in range(n):
            d0 = datetime(2026, 1, 1)
            daily = [{"date": (d0 + timedelta(days=i)).date().isoformat(), "open": 100, "high": 101, "low": 99,
                      "close": 50 + i * 0.25, "volume": 10_000 * (k + 1)} for i in range(220)]
            upsert_candles(conn, 100 + k, daily)
            stocks.append({"symbol": f"S{k}", "instrument_token": 100 + k})
    monkeypatch.setattr("backend.ingest.universe.resolve_universe", lambda cfg, *a: {"stocks": stocks, "indices": []})
    return stocks


def feed_with(stocks, falling=(100,)):
    """Range 9:15-9:29 flat at 150, then a climb (or a fall) with a last bar on 5× volume."""
    b = CandleBuilder()
    for s in stocks:
        step = -0.2 if s["instrument_token"] in falling else 0.2
        rows = bars(15, start=150, step=0.0) + bars(40, start=150 + step * 2.5, step=step, vol=4000, t0=(9, 30))
        rows[-1]["v"] = 20_000
        b.bars[s["instrument_token"]] = rows

    class F:
        builder = b
    return F()


def loose():
    """Nodes opened up so the made-up data reaches the end (daily trend off, wide RSI, no VIX / NIFTY data)."""
    flow.set_node("trigger", {"interval": 1})
    flow.set_node("trend", {"above_50": False, "above_200": False, "mirror": False})
    flow.set_node("filters", {"rsi_min": 0, "rsi_max": 100})
    flow.set_node("context", {"follow": False, "vix_on": False})
    flow.set_node("score", {"top_k": 10})
    flow.set_node("stop", {"max_stop_pct": 10, "min_stop_pct": 0})
    flow.set_node("size", {"max_open": 10, "max_trades": 10})


def test_a_run_long_and_short_signals(monkeypatch):
    stocks = setup(monkeypatch)
    loose()
    res = flow.run_now(AppConfig(), feed_with(stocks), now=at(10, 10))
    n = res["nodes"]
    assert n["universe"]["in"] == 6 and n["alert"]["out"] == 6 and res["signals"] == 6
    st = flow.state(AppConfig(), DAY)
    sides = {x["symbol"]: x["side"] for x in st["signals"]}
    assert sides["S0"] == "short" and sides["S1"] == "long" and all(x["qty"] > 0 for x in st["signals"])
    assert n["execute"]["drops"] == {"signals only (mode)": 6} and not st["positions"]       # signals mode: no paper
    again = flow.run_now(AppConfig(), feed_with(stocks), now=at(10, 15))
    assert again["nodes"]["dedup"]["drops"] == {"already signalled today": 6}


def test_capital_drives_size_and_limits(monkeypatch):
    stocks = setup(monkeypatch)
    loose()
    c = AppConfig()
    c.flow.mode = "paper"
    flow.set_node("size", {"capital": 200000, "risk_pct": 0.5, "max_open": 2})
    res = flow.run_now(c, feed_with(stocks), now=at(10, 10))
    assert res["nodes"]["size"]["out"] == 2 and res["nodes"]["size"]["drops"] == {"max open positions": 4}
    sig = flow.state(c, DAY)["signals"]
    assert all(abs(x["risk_rs"] - 1000) < x["entry"] for x in sig)                 # 0.5% of ₹2,00,000 ≈ ₹1,000 a trade
    assert len(flow.state(c, DAY)["positions"]) == 2                               # paper: pending positions opened


def test_paper_fill_costs_and_exits():
    from backend.intraday import positions as pos
    plan = {"trail": "breakeven", "trail_after_r": 1, "time_stop_min": 0, "squareoff": "15:15", "partial_on": True,
            "partial_r": 1, "partial_pct": 50}
    it = {"symbol": "X", "token": 1, "side": "long", "qty": 10, "entry": 100, "sl": 98, "target": 104}
    p = pos.open_new(DAY, it, "10:00", plan)
    bar = lambda t, o, h, l, c: {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 1}
    assert pos.step(p, bar("10:00", 100, 100, 100, 100), None, plan) == []        # the signal bar itself: no fill
    ev = pos.step(p, bar("10:01", 100, 102.2, 99.5, 102), None, plan)              # fill at the open, partial at 1R
    assert p["entry"] == pytest.approx(100.05) and any("partial" in e.get("why", "") for e in ev) and p["open_qty"] == 5
    assert p["stop"] == pytest.approx(100.05)                                        # breakeven after +1R
    ev = pos.step(p, bar("10:02", 101, 101, 99.9, 100), None, plan)                 # back to the entry: stopped
    assert p["status"] == "closed" and p["reason"] == "trailing stop" and p["costs"] > 0
    assert p["net"] == pytest.approx(p["gross"] - p["costs"], abs=0.02)
    s = pos.open_new(DAY, {**it, "symbol": "Y", "side": "short", "sl": 102, "target": 96}, "10:00", {"squareoff": "15:15"})
    pos.step(s, bar("10:01", 100, 100, 100, 100), None, {"squareoff": "15:15"})
    pos.step(s, bar("10:02", 100, 102.5, 95, 101), None, {"squareoff": "15:15"})    # both touched: the stop counts
    assert s["reason"] == "stop" and s["net"] < 0
    c = pos.intraday_charges(100_000, 100_500)
    assert 80 < c < 85                                                               # ≈ ₹83 a ₹1 lakh round trip


def test_daily_loss_limit_blocks_new_entries(monkeypatch):
    from backend.intraday import positions as pos
    stocks = setup(monkeypatch)
    loose()
    c = AppConfig()
    c.flow.mode = "paper"
    p = pos.open_new(DAY, {"symbol": "L", "token": 9, "side": "long", "qty": 2000, "entry": 100, "sl": 99, "target": None},
                     "09:40", {"squareoff": "15:15"})
    pos.step(p, {"t": "09:41", "o": 100, "h": 100, "l": 97, "c": 97, "v": 1}, None, {"squareoff": "15:15"})
    pos._save(p)
    assert pos.day_stats(DAY)["realised"] < -1500
    res = flow.run_now(c, feed_with(stocks), now=at(10, 10))
    assert res["nodes"]["size"]["drops"] == {"daily loss limit hit": 6} and res["signals"] == 0


def test_runs_at_candle_closes_only():
    c = AppConfig()
    assert not flow.due(at(10, 10, 5), c, None)                                 # mode off
    c.flow.mode = "signals"
    assert flow.due(at(10, 10, 5), c, None) and not flow.due(at(10, 11, 5), c, None)
    assert not flow.due(at(9, 25, 5), c, None) and not flow.due(at(14, 35, 5), c, None)   # outside 9:30–14:30
    assert not flow.due(at(10, 10, 20), c, at(10, 10, 5))                      # once per close


def test_day_cycle_warm_up_and_close(monkeypatch):
    from backend.intraday import feed
    stocks = setup(monkeypatch, n=2)
    k = Kite()

    class St:
        config = AppConfig()
        client = k
    St.config.flow.mode = "signals"
    r = feed.Runner(St())
    assert r.warm_up(DAY) == 4 and len(k.calls) == 2                          # 2 stocks × 2 sessions, 1 request each
    assert r.warm_up(DAY) == 0 and len(k.calls) == 2                          # saved: never fetched again

    class FakeFeed:
        tokens = [s["instrument_token"] for s in stocks]
        builder = CandleBuilder()
        stop = lambda self: None
    FakeFeed.builder.bars = {100: bars(375), 101: bars(90)}
    r.feed = FakeFeed()
    out = r.close_day(DAY)
    assert out["saved"] == 2 and out["repaired"] == 1 and k.calls[-1][0] == 101   # only the gappy stock


def test_api(monkeypatch):
    from fastapi.testclient import TestClient
    setup(monkeypatch)
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        d = tc.get("/api/flow").json()
        assert d["mode"] == "off" and len(d["nodes"]) == 14 and d["nodes"][0]["schema"] and "stats" in d
        assert tc.put("/api/flow/node/filters", json={"rvol_min": 2.0, "bogus": 1}).json()["rvol_min"] == 2.0
        assert tc.put("/api/flow/node/nope", json={}).status_code == 404
        assert tc.post("/api/flow/mode", json={"mode": "signals"}).json()["mode"] == "signals"
        assert tc.post("/api/flow/mode", json={"mode": "real"}).status_code == 422
        assert tc.post("/api/flow/mode", json={"mode": "paper"}).json()["mode"] == "paper"
        assert "total" in tc.get("/api/flow/journal").json()
        assert tc.put("/api/flow/layout", json={"orb": [40, 700], "nope": [1, 1]}).json() == {"orb": [40, 700]}
        assert next(n for n in tc.get("/api/flow").json()["nodes"] if n["id"] == "orb")["y"] == 700
        assert tc.put("/api/flow/layout", json={}).json() == {}
        assert tc.post("/api/flow/run").json()["status"] == "ok"
        assert tc.post("/api/flow/stop").json()["squared"] == 0 and tc.get("/api/flow").json()["stopped"]
        assert tc.post("/api/flow/start").json() == {"stopped": False} and not tc.get("/api/flow").json()["stopped"]


def test_manage_runs_minute_by_minute_and_survives_a_restart():
    from backend.intraday import positions as pos
    it = {"symbol": "M", "token": 5, "side": "long", "qty": 10, "entry": 100, "sl": 99, "target": 103}
    pos.open_new(DAY, it, "10:00", {"squareoff": "15:15"})
    day = bars(5, start=100, step=0.8, t0=(10, 1))                                  # 10:01 … 10:05, climbing
    pos.manage(DAY, lambda tok: day[:2], "10:03")                                   # fill at 10:01, 10:02 seen
    p = pos.load(DAY)[0]
    assert p["status"] == "open" and p["last_t"] == "10:02"
    ev = pos.manage(DAY, lambda tok: day, "10:06")                                  # after a "restart": continues at 10:03
    p = pos.load(DAY)[0]
    assert p["status"] == "closed" and p["reason"] == "target" and [e["why"] for _, e in ev] == ["target"]


def test_stop_squares_off_and_blocks_everything(monkeypatch):
    from backend.intraday import feed
    from backend.intraday import positions as pos
    stocks = setup(monkeypatch)
    loose()

    class St:
        config = AppConfig()
        client = type("C", (), {"has_token": True})()
    St.config.flow.mode = "paper"
    r = feed.Runner(St())
    r.day = DAY
    it = {"symbol": "S", "token": 7, "side": "short", "qty": 10, "entry": 100, "sl": 102, "target": 96}
    o = pos.open_new(DAY, it, "10:00", {"squareoff": "15:15"})
    pos.step(o, {"t": "10:01", "o": 100, "h": 100, "l": 100, "c": 100, "v": 1}, None, {"squareoff": "15:15"})
    pos._save(o)
    pos.open_new(DAY, {**it, "symbol": "P", "token": 8}, "10:05", {"squareoff": "15:15"})   # still pending
    f = feed_with(stocks)
    f.builder.add(7, 98, 0, at(10, 6, 10))                                         # last price 98 (short: a profit)

    class FakeFeed:
        builder, tokens = f.builder, []
        stop = lambda self: None
    r.feed = FakeFeed()
    sent = []
    out = r.stop_all(at(10, 6, 30), lambda k, d: sent.append(k))
    ps = {p["symbol"]: p for p in pos.load(DAY)}
    assert out["squared"] == 1 and ps["S"]["status"] == "closed" and ps["S"]["reason"] == "stopped" and ps["S"]["gross"] > 0
    assert ps["P"]["status"] == "cancelled" and pos.day_stats(DAY)["trades"] == 1
    assert sent == ["event", "stopped"] and r.feed is None and r.kept is f.builder and flow.stopped()
    assert r.tick(at(10, 10, 5), True) is None                                     # no feed, no runs while stopped
    res = flow.run_now(St.config, f, now=at(10, 10))
    assert len(pos.load(DAY)) == 2 and res["signals"] >= 0                          # no new paper positions
    r.start_all(lambda k, d: sent.append(k))
    assert not flow.stopped() and sent[-1] == "started"
    assert r.tick(at(10, 10, 5), True) in ("warm_up", "feed")                       # resumes
