"""₿ Delta derivatives (backend/crypto/derivs.py): the ticker parsed and stored per closed candle through
DeltaClient.get (the fake Delta), the derived OI / funding states, a failed poll skipped without blocking the candle
step, the API keys of v2 (levels, sessions, derivatives, the row fields, the scores), the settings, the old-row backfill."""

from types import SimpleNamespace

import pytest

from backend.crypto import derivs, feed
from backend.crypto import signals as sg
from backend.database.models import get_db, init_db
from tests.crypto_world import DAY1, T_DAY, FakeDelta, cfg, k
from tests.test_crypto_api import api  # noqa: F401  (the API fixture)
from tests.test_crypto_trigger import T_LON, _store

TICK = {"symbol": "BTCUSD", "oi": "1017.6", "funding_rate": "0.012", "mark_price": "59905.5"}


def test_parse_the_ticker():
    assert derivs.parse({"result": TICK}) == {"oi": 1017.6, "funding": 0.012, "mark": 59905.5}
    assert derivs.parse({"result": {"oi_value_usd": "5", "funding_rate": None}})["oi"] == 5.0
    with pytest.raises(ValueError):
        derivs.parse({"error": "x"})


def test_states_from_a_fake_ticker_series():
    t = T_DAY
    for tt, oi, f in ((t - 86400, 1000.0, 0.01), (t - 1800, 1024.0, 0.01), (t, 1017.6, 0.012)):
        derivs.store("BTCUSD", tt, {"oi": oi, "funding": f, "mark": 59905.5})
    d = derivs.derived("BTCUSD", t + 100)
    assert (d["oi_30m_pct"], d["oi_24h_pct"], d["funding_8h"], d["funding_state"], d["oi_state"]) == (-0.62, 1.76, 0.012, "neutral", "flushed")
    assert derivs.chip(d) == "OI 24h +1.8 % · last 30 min −0.6 % · funding +0.012 % (8 h) · neutral"
    assert [derivs.funding_state(x) for x in (0.03, -0.03, 0.029)] == ["stretched long", "stretched short", "neutral"]
    assert [derivs.oi_state(x) for x in (-0.3, 0.3, 0.1, None)] == ["flushed", "building", "flat", None]
    assert derivs.steps({"oi_state": "building", "funding_state": "stretched short"}, "long") == {"oi": -1, "funding": 1}
    assert derivs.steps({"oi_state": "flushed", "funding_state": "stretched short"}, "short") == {"oi": 1, "funding": 0}
    rv = derivs.row_view(derivs.for_detection("BTCUSD", t), "long")
    assert rv == {"chip": "OI −0.6 % · fund neutral", "oi_state": "flushed", "funding_state": "neutral", "tone": "ok"}
    assert derivs.row_view({"oi_30m_pct": 0.9, "oi_state": "building", "funding_state": "neutral"}, "short")["chip"] == "OI +0.9 % against"
    assert derivs.derived("BTCUSD", t + 3 * 3600) is None                        # stale: not "now"
    n = derivs.now(cfg(), t + 100)
    assert n["chip"] == derivs.chip(d) and n["mark"] == 59905.5 and n["tone"] == "ok" and n["at_label"] == "10:00 AM"


def _svc(c, ticker):
    series = {"5m": [k(t, 59000, 59010, 58990, 59000) for t in range(T_DAY - 2 * 86400, T_DAY + 301, 300)],
              "1d": [k(DAY1 - 86400 * i, 1, 2, 0, 1) for i in range(5)]}
    fake = FakeDelta(series, ticker=ticker)
    return fake, feed.CryptoService(SimpleNamespace(config=c), client=feed.DeltaClient(get=fake, sleep=lambda s: None), clock=lambda: T_DAY + 303)


def test_one_poll_per_tick_stored_for_the_closed_candle():
    fake, svc = _svc(cfg(history_days=1, scan_days=0), TICK)
    assert svc.tick()["processed"] == [T_DAY]
    assert fake.ticker_calls == ["https://api.india.delta.exchange/v2/tickers/BTCUSD"]
    with get_db() as conn:
        assert [tuple(r) for r in conn.execute("SELECT symbol, t, oi, funding, mark FROM crypto_ticks")] == [("BTCUSD", T_DAY, 1017.6, 0.012, 59905.5)]
    fake2, svc2 = _svc(cfg(history_days=1, scan_days=0, derivs=False), TICK)
    svc2.tick()
    assert fake2.ticker_calls == []


def test_a_failed_poll_is_skipped_and_never_blocks_the_candle_step(monkeypatch):
    seen = []
    monkeypatch.setattr(sg, "process", lambda config, t, now_ts=None, candles=None: seen.append(t) or [])
    fake, svc = _svc(cfg(history_days=1, scan_days=0), None)                     # the ticker answers 503
    out = svc.tick()
    assert out["processed"] == [T_DAY] and seen == [T_DAY] and len(fake.ticker_calls) == 1
    assert svc.last_error is None
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM crypto_ticks").fetchone()[0] == 0


def test_api_keys(api, monkeypatch):  # noqa: F811
    from tests.crypto_world import telegram
    telegram(monkeypatch)
    from backend.api import routes
    c = routes.S().config
    derivs.store("BTCUSD", T_LON, {"oi": 1000.0, "funding": 0.012, "mark": 59000.0})
    _store(T_LON)
    sg.process(c, T_LON, now_ts=T_LON + 303)
    import time
    monkeypatch.setattr(time, "time", lambda: T_LON + 303)
    j = api.get("/api/crypto").json()
    assert {"levels_now", "sessions", "derivs_now"} <= set(j)
    assert {"kind", "family", "name", "label", "px", "span"} <= set(j["levels_now"][0])
    assert {"windows", "current"} <= set(j["sessions"]) and j["sessions"]["current"] == "london"
    assert {"oi", "oi_24h_pct", "oi_30m_pct", "funding", "funding_state", "oi_state", "mark", "chip", "at_label", "tone"} <= set(j["derivs_now"])
    sc = j["scores"]
    assert set(sc["by_level_kind"]) == {"pd", "session", "vwap", "profile", "pivot"} and set(sc["by_session"]) == {"asia", "london", "ny"}
    assert set(sc["counted"]) == {"counted", "not", "alerts", "marks"} and sc["counted"]["counted"] >= 1
    s = next(x for x in j["signals"] if x["pattern"] == "sweep" and x["dir"] == "long")
    assert {"trigger", "shape", "levels", "levels_text", "session", "approach", "derivs", "counted", "why"} <= set(s)
    assert s["trigger"] == "sweep" and s["levels_text"] == "PDL 59,000 + S1 59,000" and s["levels"][0] == "PDL 59,000 · prev day low"
    assert s["trigger_text"] == "sweep · reclaim · hammer" and s["counted"] is True and s["session_label"] == "London"
    assert set(s["approach"]) == {"atr", "n", "text", "from_t", "from_px"} and s["approach"]["text"].endswith(f"/ {s['approach']['n']}")
    assert s["approach"]["from_t"] == T_LON - 300 * s["approach"]["n"] and s["approach"]["from_px"]
    assert set(s["derivs"]) == {"chip", "oi_state", "funding_state", "tone"}
    ch = api.get(f"/api/crypto/candles?tf=5m&from={T_LON - 7200}&to={T_LON + 300}").json()
    assert ch["levels_time"] == "unix" and ch["levels"] and {"kind", "family", "name", "label", "px", "span"} <= set(ch["levels"][0])
    assert set(ch["levels"][0]["span"]) == {"start", "end"}
    assert ch["vwap"] and set(ch["vwap"][0]) == {"t", "vwap", "up", "dn"}
    assert ch["sessions"] and set(ch["sessions"][0]) == {"name", "label", "start", "end"} and ch["sessions"][-1]["name"] == "london"
    r = api.put("/api/config/crypto", json={"trigger": "touch", "approach_atr": 1.5, "approach_candles": 20, "touches_per_session": 0,
                                            "session_asia": "alerts", "atr_floor": 80, "derivs": False, "stop_mode": "fixed",
                                            "levels_profile": False})
    assert r.status_code == 200 and r.json()["trigger"] == "touch" and r.json()["atr_floor"] == 80.0
    for bad in ({"trigger": "cross"}, {"approach_atr": 2}, {"approach_candles": 10}, {"touches_per_session": 3}, {"session_ny": "maybe"},
                {"atr_floor": 50}, {"stop_mode": "atr"}):
        assert api.put("/api/config/crypto", json=bad).status_code == 422


def test_defaults_are_the_mocks():
    from backend.config import CryptoConfig
    d = CryptoConfig()
    assert (d.levels_pd, d.levels_sessions, d.levels_vwap, d.levels_profile, d.levels_pivots) == (True,) * 5
    assert (d.trigger, d.approach_atr, d.approach_candles, d.touches_per_session) == ("sweep", 1.0, 12, 2)
    assert (d.session_asia, d.session_london, d.session_ny, d.atr_floor, d.derivs) == ("marks", "alerts", "alerts", 40.0, True)
    assert (d.stop_mode, d.stop_wick_pad, d.stop_cap_atr) == ("wick", 10.0, 1.0)


def test_old_rows_backfilled_on_start():
    with get_db() as conn:
        for t in (T_DAY, T_LON, T_DAY + 12 * 3600):                            # 10:00 AM · 2:30 PM · 10:00 PM IST
            conn.execute("INSERT INTO crypto_signals (symbol, tf, t, pattern, dir, state) VALUES ('BTCUSD', '5m', ?, 'hammer', 'long', 'mark')", (t,))
        conn.execute("UPDATE crypto_signals SET trigger = NULL, session = NULL, counted = NULL")
    init_db()
    with get_db() as conn:
        got = [tuple(r) for r in conn.execute("SELECT trigger, session, counted, levels_json, derivs_json FROM crypto_signals ORDER BY t")]
    assert got == [("touch", "asia", 1, None, None), ("touch", "london", 1, None, None), ("touch", "ny", 1, None, None)]
