"""₿ "Forecast now" (backend/crypto/forecast.py LATEST, GET /api/crypto/forecast_now): the cache fills from the service
pass (one worker call asking for 30 and 60 min), the endpoint shape, 400 on another horizon, the stale flag, the worker
down, and the request handler never calling the worker."""

import time
from types import SimpleNamespace

import pytest

from backend.crypto import feed, forecast, view
from backend.market import reads as rd
from tests.crypto_world import DAY1, T_DAY, FakeDelta, cfg, k, seed_daily, telegram
from tests.test_crypto_api import api  # noqa: F401  (the API fixture)

S = 300


@pytest.fixture(autouse=True)
def _clean_latest():
    forecast.LATEST.clear()
    yield
    forecast.LATEST.clear()


def _world(t=T_DAY, n=40, top=59400.0):
    """n candles stepping down 1 point a candle (close just above S1 59,000 … P 60,000 band: S1 below, P above)."""
    return [k(t - (n - 1 - i) * S, top - i, top - i + 10, top - i - 10, top - i - 1) for i in range(n)]


def multi_worker(calls=None, p_up=0.42):
    """A worker that answers one read per asked horizon (as backend/market/kronos_worker.py does)."""
    def w(payload):
        if calls is not None:
            calls.append(payload)
        last = payload["candles"][-1]["c"]
        reads = []
        for h in payload["horizons"]:
            ps = {q: [round(last + d * (j + 1), 2) for j in range(h)] for q, d in (("q10", -8), ("q25", -5), ("q50", -2), ("q75", 1), ("q90", 4))}
            reads.append({"horizon": h, "p_up": p_up if h <= 6 else p_up - 0.1, "exp_range": 10.0 * h, "paths_summary": ps, "touch": {}, "samples": 10})
        top = next(r for r in reads if r["horizon"] == payload["horizon"])
        return {**top, "reads": reads, "ms": 5}
    return w


def _step(c, t=T_DAY):
    return forecast.step(c, t, _world(t), t + 303)


def test_the_cache_fills_from_the_service_pass_in_one_call(monkeypatch):
    seed_daily()
    calls = []
    monkeypatch.setattr(rd, "WORKER", multi_worker(calls))
    c = cfg()
    c.market.kronos = True
    _step(c)
    assert len(calls) == 1 and calls[0]["horizons"] == [6, 12] and calls[0]["horizon"] == 12        # ONE call, both horizons
    a, b = forecast.LATEST[30], forecast.LATEST[60]
    assert a["t"] == T_DAY and a["worker"] == "ok" and a["horizon_min"] == 30 and a["n"] == 6 and b["n"] == 12
    assert a["p_up"] == 0.42 and a["p_down"] == 0.58 and a["dir"] == "short" and b["p_up"] == 0.32
    assert a["exp_px"] == a["paths"]["median"][-1] and a["exp_range"] == 60.0
    assert set(a["paths"]) == {"median", "p25", "p75", "p10", "p90"} and all(len(v) == 6 for v in a["paths"].values())
    assert a["t0"] == T_DAY + 19800 and a["times"] == [T_DAY + (j + 1) * S + 19800 for j in range(6)]
    close = _world()[-1]["c"]
    assert a["last"] == close and a["next_pivots"]["above"]["px"] > close > a["next_pivots"]["below"]["px"]
    assert a["next_pivots"]["below"] == {"name": "S1", "px": 59000.0}
    _step(c)                                                                                    # the same candle again: no second call
    assert len(calls) == 1
    t2 = T_DAY + S
    _step(c, t2)
    assert forecast.LATEST[30]["t"] == t2 and len(calls) == 2


def test_the_service_tick_fills_it_after_the_alerts(monkeypatch):
    telegram(monkeypatch)
    calls = []
    monkeypatch.setattr(rd, "WORKER", multi_worker(calls))
    c = cfg(history_days=1, scan_days=0)
    c.market.kronos = True
    seed_daily()
    cs = _world()
    series = {"5m": cs, "1d": [k(DAY1 - 86400 * i, 59050, 61000, 59000, 60000) for i in range(-1, 4)]}
    svc = feed.CryptoService(SimpleNamespace(config=c), client=feed.DeltaClient(get=FakeDelta(series), sleep=lambda s: None),
                             clock=lambda: T_DAY + 303)
    svc.tick()
    forecast.drain(20)
    assert forecast.LATEST[30]["t"] == T_DAY and forecast.LATEST[60]["worker"] == "ok"


def test_worker_down_marks_the_cache(monkeypatch):
    seed_daily()
    c = cfg()
    c.market.kronos = True
    monkeypatch.setattr(rd, "WORKER", lambda p: None)
    _step(c)
    assert forecast.LATEST[30]["worker"] == "down" and "paths" not in forecast.LATEST[30]
    v = view.forecast_now(c, 30, now_ts=T_DAY + 303)
    assert v["worker"] == "down" and v["empty"] is False
    monkeypatch.setattr(rd, "WORKER", multi_worker())
    _step(c, T_DAY + S)
    monkeypatch.setattr(rd, "WORKER", lambda p: None)
    _step(c, T_DAY + 2 * S)                                                                    # a kept read is marked down, not dropped
    r = forecast.LATEST[30]
    assert r["worker"] == "down" and r["t"] == T_DAY + S and r["paths"]["median"]


def test_the_view_shape_and_stale(monkeypatch):
    seed_daily()
    monkeypatch.setattr(rd, "WORKER", multi_worker())
    c = cfg()
    c.market.kronos = True
    assert view.forecast_now(c, 30, now_ts=T_DAY + 303)["empty"] is True
    _step(c)
    v = view.forecast_now(c, 60, now_ts=T_DAY + 303)
    keys = {"t", "at", "p_up", "p_down", "dir", "exp_px", "exp_range", "paths", "times", "t0", "horizon_min", "n", "next_pivots", "worker",
            "model_chip", "next_at_label", "stale"}
    assert keys <= set(v) and v["horizon_min"] == 60 and v["stale"] is False and v["empty"] is False
    assert v["candle_label"] == "10:00 AM" and v["next_at_label"] == "10:10 AM"                   # IST, 12-hour
    assert v["model_chip"] == "shadow · no BTC reads scored yet"
    assert view.forecast_now(c, 30, now_ts=T_DAY + 3 * S)["stale"] is False                     # 2 candles after the close: fresh
    assert view.forecast_now(c, 30, now_ts=T_DAY + 3 * S + 1)["stale"] is True                  # older than 2 candles
    c.crypto.symbol = "ETHUSD"                                                                  # another symbol: nothing cached
    assert view.forecast_now(c, 30, now_ts=T_DAY + 303)["empty"] is True


def test_the_endpoint_shape_400_and_never_the_worker(api, monkeypatch):  # noqa: F811
    from backend.api import routes
    calls = []

    def boom(*a, **kw):
        calls.append(1)
        raise AssertionError("the request handler called the candle-model worker")
    monkeypatch.setattr(rd, "call_worker", boom)
    monkeypatch.setattr(rd, "WORKER", boom)
    c = routes.S().config
    j = api.get("/api/crypto/forecast_now").json()
    assert j["empty"] is True and j["horizon_min"] == 30 and "next_at_label" in j and "model_chip" in j
    now = int(time.time())
    t = now - now % S - S
    ps = {"q50": [1.0, 2.0], "q25": [1.0, 2.0], "q75": [1.0, 2.0], "q10": [1.0, 2.0], "q90": [1.0, 2.0]}
    forecast.LATEST[60] = forecast.now_read({"p_up": 0.7, "exp_range": 120.0, "paths_summary": ps}, c.crypto.symbol, c.crypto.tf, t, 60, 1.0,
                                            {"S1": 0.5, "R1": 3.0})
    j = api.get("/api/crypto/forecast_now?horizon=60").json()
    assert j["worker"] == "ok" and j["stale"] is False and j["dir"] == "long" and j["paths"]["median"] == [1.0, 2.0]
    assert j["next_pivots"] == {"above": {"name": "R1", "px": 3.0}, "below": {"name": "S1", "px": 0.5}}
    assert api.get("/api/crypto/forecast_now?horizon=30").json()["empty"] is True
    assert api.get("/api/crypto/forecast_now?horizon=15").status_code == 400
    s = api.get("/api/crypto").json()
    assert s["forecast_now"]["horizon_min"] == 30 and "next_at_label" in s["forecast_now"]
    assert calls == []
