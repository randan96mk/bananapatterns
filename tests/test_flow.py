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


def test_a_run_filters_and_signals(monkeypatch):
    stocks = setup(monkeypatch)
    b = CandleBuilder()
    for s in stocks:                           # range 9:15-9:29 flat, then a climb with volume (breakout above the range)
        for x in bars(15, start=150, step=0.0) + bars(40, start=150.5, step=0.2, vol=4000, t0=(9, 30)):
            b.bars.setdefault(s["instrument_token"], []).append(x)
        b.bars[s["instrument_token"]][-1]["v"] = 20_000                       # the last bar on 5× volume
    b.bars[100] = bars(55, start=150, step=-0.2)                                # S0 falls: below VWAP

    class F:
        builder = b
    flow.set_node("filters", {"rsi_max": 100})
    flow.set_node("trigger", {"interval": 1})
    res = flow.run_now(AppConfig(), F(), now=at(10, 10))
    n = res["nodes"]
    assert n["universe"]["in"] == 6 and n["filters"]["drops"].get("below VWAP") == 1
    assert n["alert"]["out"] == 5 and res["signals"] == 5
    st = flow.state(AppConfig(), DAY)
    assert len(st["signals"]) == 5 and {x["id"] for x in st["nodes"]} >= {"trigger", "filters", "orb", "execute"}
    again = flow.run_now(AppConfig(), F(), now=at(10, 15))
    assert again["nodes"]["dedup"]["drops"] == {"already signalled today": 5}           # one per stock per day


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
        assert d["mode"] == "off" and len(d["nodes"]) == 9 and d["nodes"][0]["schema"]
        assert tc.put("/api/flow/node/filters", json={"rvol_min": 2.0, "bogus": 1}).json()["rvol_min"] == 2.0
        assert tc.put("/api/flow/node/nope", json={}).status_code == 404
        assert tc.post("/api/flow/mode", json={"mode": "signals"}).json()["mode"] == "signals"
        assert tc.post("/api/flow/mode", json={"mode": "real"}).status_code == 422
        assert tc.post("/api/flow/run").json()["status"] == "ok"
