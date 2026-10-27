"""Track record: the trade rules (entry, no entry, stop, gap, breakeven, half at +20%, exit SMA, 3M mark, snapshots),
the causal replay of breakouts, the build-once-then-add daily update, the portfolio curve, the alert line and the API."""

import json
from dataclasses import replace
from datetime import date, timedelta

import pytest

from backend.config import AppConfig, BaseConfig, ScreenConfig, TrackConfig
from backend.database.models import get_candles, get_db, set_setting, upsert_candles
from backend.engine import track
from tests.test_alerts import BASE, UP
from tests.test_bases import build


def bars(ohlc, start=date(2026, 1, 1)):
    """[(open, high, low, close)] on consecutive weekdays."""
    out, d = [], start
    for o, h, lo, c in ohlc:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({"date": d.isoformat(), "open": o, "high": h, "low": lo, "close": c, "volume": 1_000_000})
        d += timedelta(days=1)
    return out


CFG = TrackConfig(exit_ma=3, max_hold=63)
BREAKOUT = [(98, 101, 97, 100)]           # bar t = 0: the breakout (pivot 99)


def sim(rest, pivot=99.0, stop=93.0, cfg=CFG):
    return track.simulate(track.Bars(bars(BREAKOUT + rest), cfg.exit_ma), 0, pivot, stop, cfg, 0.05)


# --- the rules -------------------------------------------------------------------------

def test_pending_and_no_entry():
    assert sim([])["status"] == "pending"
    t = sim([(104.5, 106, 104, 105)])                                   # opens 5.6% over the 99 pivot
    assert t["status"] == "no_entry" and "beyond the buy zone" in t["reason"] and t["entry"] == 104.5


def test_stop_capped_and_closed_below():
    t = sim([(100, 101, 99, 100), (99, 100, 91, 91.5)], stop=80.0)      # pattern stop 80 -> capped at 100 x 0.92
    assert t["stop"] == 92.0 and t["status"] == "lost" and t["reason"] == "stop"
    assert t["result_pct"] == pytest.approx(-8.5) and t["r"] == pytest.approx(-8.5 / 8, abs=0.01)
    assert t["days"] == 2 and t["events"][-1]["kind"] == "exit"


def test_gap_under_the_stop_exits_at_the_open():
    t = sim([(100, 101, 99, 100), (90, 92, 89, 91)])
    assert t["reason"] == "gap under the stop" and t["result_pct"] == pytest.approx(-10.0)


def test_breakeven_then_stopped_at_entry():
    t = sim([(100, 111, 99, 110.5), (109, 110, 99.5, 99.8)])            # +10.5% close -> stop to 100
    kinds = [e["kind"] for e in t["events"]]
    assert kinds == ["entry", "breakeven", "exit"] and t["reason"] == "breakeven stop"
    assert t["result_pct"] == pytest.approx(-0.2) and t["status"] == "lost"


def test_half_at_20_then_rest_under_the_exit_sma():
    rest = [(100, 105, 99, 104), (104, 112, 103, 111), (111, 121, 110, 118),   # half sold at 120
            (118, 125, 117, 124), (124, 125, 110, 111)]                        # close 111 < SMA3 of 118,124,111
    t = sim(rest)
    assert [e["kind"] for e in t["events"]] == ["entry", "breakeven", "half", "exit"]
    half = next(e for e in t["events"] if e["kind"] == "half")
    assert half["price"] == 120.0 and t["reason"] == "close under the 3-day"
    assert t["result_pct"] == pytest.approx(0.5 * 20 + 0.5 * 11) and t["status"] == "won"


def test_gap_over_the_target_sells_half_at_the_open():
    t = sim([(100, 104, 99, 103), (125, 126, 124, 125)])
    assert next(e for e in t["events"] if e["kind"] == "half")["price"] == 125.0


def test_three_month_mark_and_snapshots():
    rest = [(100 + i * 0.1, 101 + i * 0.1, 99.5 + i * 0.1, 100.5 + i * 0.1) for i in range(70)]
    t = sim(rest, cfg=replace(CFG, exit_ma=50))
    assert t["reason"] == "3M mark" and t["days"] == 63
    assert set(t["snaps"]) == {"1w", "2w", "1m", "3m"} and t["snaps"]["3m"] == t["result_pct"]
    r = sim(rest[:7], cfg=replace(CFG, exit_ma=50))
    assert r["status"] == "running" and set(r["snaps"]) == {"1w"}           # 7 sessions in: only 1W known


def test_snapshots_freeze_at_the_exit():
    t = sim([(100, 101, 99, 100), (99, 100, 91, 91.5)])
    assert t["snaps"] == {"1w": -8.5, "2w": -8.5, "1m": -8.5, "3m": -8.5}


# --- replay, build once, add daily ---------------------------------------------------------------

def breakout_series(after=40):
    """Uptrend, a base, a volume breakout, then a steady climb of `after` bars."""
    return build([UP, BASE, ("bo", 1, 1.02, 3_000_000), ("trend", after, 0.30)])   # next open inside the buy zone


def app_config():
    c = AppConfig()
    c.bases = BaseConfig(reset_ma=0)
    c.screen = ScreenConfig(trend_ma=0, min_turnover_cr=0)
    c.track = replace(c.track, history_days=3650)
    return c


def free_config():
    """app_config() without trading costs (sizing tests count shares, not charges)."""
    c = app_config()
    c.track = replace(c.track, cost_pct=0.0, intraday_cost_pct=0.0, slippage_pct=0.0)
    return c


def setup_db(monkeypatch, series: dict):
    with get_db() as conn:
        for tok, (sym, cs) in enumerate(series.items(), start=1):
            upsert_candles(conn, tok, cs)
    stocks = [{"symbol": s, "instrument_token": i} for i, s in enumerate(series, start=1)]
    monkeypatch.setattr("backend.ingest.universe.resolve_universe", lambda cfg, *a: {"stocks": stocks, "indices": []})


def test_build_finds_the_breakout_and_trades_it(monkeypatch):
    setup_db(monkeypatch, {"BRK": breakout_series()})
    r = track.build(app_config())
    assert r["status"] == "ok" and r["added"] == 1 and track.built()
    rows = track.rows("1y")
    assert len(rows) == 1
    t = rows[0]
    assert t["symbol"] == "BRK" and t["pattern"] == "bases" and t["status"] in ("running", "won")
    assert t["trade_no"] == 1 and t["entry_date"] > t["breakout_date"] and t["events"][0]["kind"] == "entry"


def test_trades_api_carries_the_sparkline(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    setup_db(monkeypatch, {"BRK": breakout_series()})
    from backend import main
    with TestClient(main.app) as tc:
        main_state = __import__("backend.api.routes", fromlist=["S"]).S()
        main_state.config.bases, main_state.config.screen = app_config().bases, app_config().screen
        main_state.config.track = app_config().track
        track.build(main_state.config)
        t = tc.get("/api/track/trades").json()["stocks"][0]["trades"][0]
        assert t["spark"]["d"][3] == t["entry_date"] and len(t["spark"]["d"]) == len(t["spark"]["c"]) > 10
        assert tc.get("/api/track").json()["rules"]["book_half_pct"] == 20.0


def test_replay_is_causal(monkeypatch):
    full = breakout_series()
    setup_db(monkeypatch, {"BRK": full})
    u = track._universe(app_config())
    later = track.find_signals(u, "BRK", "2000-01-01")
    k = next(i for i, c in enumerate(full) if c["date"] == later[0]["breakout_date"])
    with get_db() as conn:
        conn.execute("DELETE FROM candles_daily")
        upsert_candles(conn, 1, full[:k + 1])                               # only up to the breakout day
    early = track.find_signals(track._universe(app_config()), "BRK", "2000-01-01")
    keys = ("breakout_date", "pattern", "pivot", "pattern_stop")
    assert [{k_: s[k_] for k_ in keys} for s in early] == [{k_: s[k_] for k_ in keys} for s in later]


def test_daily_update_adds_and_never_rewrites_closed(monkeypatch):
    full = breakout_series(after=40)
    setup_db(monkeypatch, {"BRK": full[:-20]})
    track.build(app_config())
    first = track.rows("1y")[0]
    assert first["status"] == "running"
    with get_db() as conn:                                 # a closed row elsewhere must never be touched
        conn.execute("INSERT INTO track_signals (symbol, breakout_date, pattern, pivot, pattern_stop, status, "
                     "snaps, events, updated_at) VALUES ('OLD', '2023-01-02', 'vcp', 10, 9, 'won', '{}', '[]', 'x')")
        upsert_candles(conn, 1, full)                      # 20 more sessions arrive
    r = track.build(app_config())
    assert r["status"] == "ok" and r["added"] == 0 and not r["built"]
    after = {x["symbol"]: x for x in track.rows("1y") + [dict(x) for x in _all_rows() if x["symbol"] == "OLD"]}
    assert after["BRK"]["days"] > first["days"]                          # the running trade moved on
    assert after["OLD"]["updated_at"] == "x"


def _all_rows():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM track_signals")]


def test_one_open_trade_per_stock_and_reentry():
    # two breakouts 5 days apart while the first trade runs -> the second is "in_trade"
    candles = bars(BREAKOUT + [(100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(10)])
    u = type("U", (), {"cfg": AppConfig(), "series": {"X": candles}})()
    u.cfg.track = CFG
    sigs = [{"symbol": "X", "breakout_date": candles[0]["date"], "pattern": "vcp", "pivot": 99.0,
             "pattern_stop": 93.0, "tier": "A", "market": "uptrend"},
            {"symbol": "X", "breakout_date": candles[5]["date"], "pattern": "bases", "pivot": 103.0,
             "pattern_stop": 96.0, "tier": "B", "market": "uptrend"}]
    rows = track._replay_symbol(u, "X", [], sigs)
    assert [r[11] for r in rows] == ["running", "in_trade"] and rows[0][13] == 1 and rows[1][13] is None


# --- portfolio, summary, alert line, API ------------------------------------------------------------

def put_rows(rows, as_of="2026-03-31"):
    with get_db() as conn:
        for r in rows:
            conn.execute(track.UPSERT, track._row_values(r, r, r.get("trade_no")))
        set_setting(conn, "track.built_at", "2026-01-01T00:00:00")
        set_setting(conn, "track.as_of", as_of)


def trade(sym, d, entry, stop, exit_px, status, pattern="vcp", tier="A", mkt="uptrend", exit_d=None):
    r = round((exit_px / entry - 1) * 100, 2)
    ev = [{"kind": "entry", "date": d, "price": entry}]
    if status != "running":
        ev.append({"kind": "exit", "date": exit_d or d, "price": exit_px, "why": "stop"})
    return {"symbol": sym, "breakout_date": d, "pattern": pattern, "pivot": entry, "pattern_stop": stop, "tier": tier,
            "market": mkt, "status": status, "reason": "stop" if status == "lost" else None, "trade_no": 1,
            "entry_date": d, "entry": entry, "stop": stop, "exit_date": None if status == "running" else exit_d or d,
            "result_pct": r, "r": round(r / ((entry - stop) / entry * 100), 2), "days": 5,
            "snaps": {"1w": r}, "events": ev}


def test_portfolio_sizes_by_the_stop_and_caps_positions(monkeypatch):
    days = bars([(100, 101, 99, 100)] * 30, start=date(2026, 3, 2))
    series = {f"S{i}": days for i in range(12)}
    setup_db(monkeypatch, series)
    d0 = days[0]["date"]
    rows = [trade(f"S{i}", d0, 100.0, 92.0, 100.0, "running") for i in range(12)]
    p = track.portfolio(rows, free_config())
    # 1% of 10L = 10,000 risk / 8 per share = 1,250 shares = 1.25L each: cash runs out after 8 positions
    assert p["taken"] == 8 and p["skipped"] == 4
    assert p["strategy"][0] == pytest.approx(0.0, abs=0.01) and p["compare"]["strategy"]["cash_idle"] < 1
    # a tighter stop means a bigger position (4% stop: 2,500 shares = 2.5L), but the 20% cap holds it to 2L: 5 fit
    tight = [trade(f"S{i}", d0, 100.0, 96.0, 100.0, "running") for i in range(12)]
    p2 = track.portfolio(tight, free_config())
    assert p2["taken"] == 5 and p2["settings"]["position_cap"] == 0.20
    # without the cap only 4 fit; the comparison runs the same trades through each sizing option
    cfg = free_config()
    cfg.track.position_cap = 0.0
    assert track.portfolio(tight, cfg)["taken"] == 4
    sz = {x["label"]: x for x in p2["sizing"]}
    assert sz["1% risk, cash-limited"]["skipped"] == 8 and sz["1% risk, max 10% per position"]["skipped"] == 2
    assert sz["0.75% risk"]["skipped"] == 6     # 1,875 shares = 1.875L each: 5 fit, a 6th gets the cash left
    assert sz["1% risk, max 20% per position"]["current"] and sum(x["current"] for x in p2["sizing"]) == 1
    # a 16% stop: 625 shares = 62,500 each, so the 10-position cap binds first
    p3 = track.portfolio([trade(f"S{i}", d0, 100.0, 84.0, 100.0, "running") for i in range(12)], free_config())
    assert p3["taken"] == 10 and p3["skipped"] == 2


def test_stats_and_alert_line():
    rows = ([trade(f"W{i}", "2026-02-02", 100, 92, 116, "won") for i in range(7)]
            + [trade(f"L{i}", "2026-02-03", 100, 92, 92, "lost") for i in range(4)]
            + [trade("R1", "2026-03-02", 100, 92, 104, "running")])
    put_rows(rows)
    s = track.stats(track.rows("1y"))
    assert s["counts"]["closed"] == 11 and s["counts"]["running"] == 1 and s["win_rate"] == pytest.approx(63.6)
    assert s["expectancy_r"] == pytest.approx((7 * 2 + 4 * -1) / 11, abs=0.01)
    assert track.line("vcp", "A", "uptrend") == "📈 Track record (1Y): VCP · tier A · uptrend → 64% won, +0.91R avg, 11 trades"
    assert "too few past Cup trades" in track.line("cup", "A", "uptrend")


def test_alert_carries_the_track_line():
    from backend.alerts.dispatch import Notifier
    from backend.config import AlertsConfig
    from tests.test_alerts import FakeTG, scanned_db
    as_of, _ = scanned_db()
    put_rows([trade(f"W{i}", "2026-02-02", 100, 92, 116, "won", pattern="bases", tier="B", mkt=None)
              for i in range(10)])
    fake = FakeTG()
    Notifier(AlertsConfig(chat_id="42"), fake).send("k1", "breakout", "🚀 <b>FRESH</b>", "FRESH")
    assert "Track record (1Y): Bases" in fake.sent[0][1]
    Notifier(AlertsConfig(chat_id="42", track_line=False), fake).send("k2", "breakout", "🚀 x", "FRESH")
    assert "Track record" not in fake.sent[1][1]


def test_api(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        assert tc.get("/api/track").json()["built"] is False
        put_rows([trade("W1", "2026-02-02", 100, 92, 116, "won"), trade("L1", "2026-02-03", 100, 92, 92, "lost", "cup")])
        d = tc.get("/api/track?period=1y").json()
        assert d["built"] and d["counts"]["closed"] == 2 and {x["pattern"] for x in d["by_pattern"]} == {"vcp", "cup"}
        assert tc.get("/api/track?pattern=cup").json()["counts"]["closed"] == 1
        assert tc.get("/api/track?status=running").json()["counts"]["closed"] == 0
        t = tc.get("/api/track/trades").json()["stocks"]
        assert [s["symbol"] for s in t] == ["L1", "W1"] and t[0]["trades"][0]["events"][0]["kind"] == "entry"
        assert tc.get("/api/config/track").json()["max_positions"] == 10
        assert tc.get("/api/config/track").json()["position_cap"] == 0.20
        assert [x["label"] for x in d["portfolio"]["sizing"]][-1] == "1% risk, max 20% per position"
