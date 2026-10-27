"""Backtest building blocks: the portfolio replay (sizing, cap, cash, same-day order, costs), 5-minute candles fetched
once and stored, curve statistics, and the removal of the old Track record tables."""

from datetime import date

import pytest

from backend.database.models import get_db, get_setting, init_db, set_setting
from backend.engine import backtest as bt

D0, D1 = "2026-03-02", "2026-03-03"


def trade(sym, stop=99.0, tier="A"):
    return {"symbol": sym, "breakout_date": D0, "stop": stop, "tier": tier}


def ev(kind, d, price, time=None, frac=None):
    e = {"kind": kind, "date": d, "price": price}
    if time:
        e["time"] = time
    if frac:
        e["frac"] = frac
    return e


# --- the portfolio replay ------------------------------------------------------------------------

def test_sizing_by_the_stop_the_cap_and_cash():
    rows = [trade(f"S{i}", stop=92.0) for i in range(12)]
    by_day = {D0: [(ev("entry", D0, 100.0), r) for r in rows]}
    closes = {r["symbol"]: {D0: 100.0} for r in rows}
    # 1% of 10L = 10,000 risk / 8 a share = 1,250 shares = 1.25L each: cash runs out after 8
    curve, idle, taken, skipped = bt.book([D0], by_day, closes, 1_000_000, 10, 0.01, 0.0)
    assert (taken, skipped) == (8, 4) and idle[-1] < 0.01 and curve[-1] == pytest.approx(0.0)
    # a 20% cap on a 2% stop (5,000 shares = 5L) holds each to 2L: 5 fit
    tight = {D0: [(ev("entry", D0, 100.0), trade(f"S{i}", stop=98.0)) for i in range(12)]}
    assert bt.book([D0], tight, closes, 1_000_000, 10, 0.01, 0.20)[2] == 5


def test_same_day_events_run_in_time_order_and_new_entries_are_capped():
    a, b = trade("A"), trade("B")
    by_day = {D0: [(ev("entry", D0, 100.0, "10:05"), a), (ev("exit", D0, 99.0, "11:20"), a),
                   (ev("entry", D0, 100.0, "10:10"), b), (ev("exit", D0, 99.0, "12:00"), b)]}
    curve, idle, taken, _ = bt.book([D0], by_day, {}, 1_000_000, 10, 0.01, 0.20)
    assert taken == 2 and idle[-1] == pytest.approx(1.0) and curve[-1] == pytest.approx(-0.4)
    assert bt.book([D0], by_day, {}, 1_000_000, 10, 0.01, 0.20, max_new_per_day=1)[2:] == (1, 1)


def test_half_sale_uses_its_fraction():
    r = trade("A")
    by_day = {D0: [(ev("entry", D0, 100.0, "10:00"), r), (ev("half", D0, 102.0, "11:00", frac=0.5), r),
                   (ev("exit", D0, 101.0, "15:15"), r)]}
    curve = bt.book([D0], by_day, {}, 1_000_000, 10, 0.01, 0.20)[0]   # 2,000 shares: 1,000 at +2, 1,000 at +1
    assert curve[-1] == pytest.approx((1000 * 2 + 1000 * 1) / 1_000_000 * 100)


def test_costs_same_day_vs_overnight():
    c = bt.costs(0.05, 0.25, 0.06)
    s, over, same = c
    r = trade("A")
    flat = {D0: [(ev("entry", D0, 100.0, "10:00"), r), (ev("exit", D0, 100.0, "11:00"), r)]}
    assert bt.book([D0], flat, {}, 1_000_000, 10, 0.01, 0.20, costs=c)[0][-1] == \
        pytest.approx(-0.2 * (2 * s + same) * 100, abs=0.01)
    held = {D0: flat[D0][:1], D1: [(ev("exit", D1, 100.0, "11:00"), r)]}
    curve = bt.book([D0, D1], held, {"A": {D0: 100.0, D1: 100.0}}, 1_000_000, 10, 0.01, 0.20, costs=c)[0]
    assert curve[-1] == pytest.approx(-0.2 * (2 * s + over) * 100, abs=0.01)


def test_curve_statistics():
    assert bt.drawdown([0, 10, -1, 5]) == pytest.approx((99 / 110 - 1) * 100, abs=0.01)
    assert bt.drawdown([0, 1, 2]) == 0
    assert bt.months_up(["2026-01-30", "2026-02-27", "2026-03-31"], [1.0, 2.0, 1.5]) == (1, 2)


# --- 5-minute candles ------------------------------------------------------------------------------

class FakeKite:
    has_token = True

    def __init__(self):
        self.calls = []

    def historical_bars(self, token, start, end, interval="5minute", label=""):
        self.calls.append((start, end))
        return [{"ts": f"{start} 09:15", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10}]


def test_five_minute_candles_are_fetched_once_and_stored():
    kite = FakeKite()
    five = bt.FiveMin(kite, 7, "X", [D0, D1])
    day = five.day(D0, ahead=2)
    assert day[0]["t"] == "09:15" and kite.calls == [(date(2026, 3, 2), date(2026, 3, 3))]
    assert five.day(D1) == [] and len(kite.calls) == 1            # covered by the first request: no candles
    again = bt.FiveMin(kite, 7, "X", [D0, D1])                    # a new run reads the database
    assert again.day(D0)[0]["close"] == 1.5 and len(kite.calls) == 1
    assert bt.FiveMin(None, 8, "Y", [D0]).day(D0) is None          # no Kite: not available


# --- the old Track record is gone ---------------------------------------------------------------------

def test_track_record_tables_and_settings_are_dropped():
    with get_db() as conn:
        conn.execute("CREATE TABLE track_signals (symbol TEXT)")
        conn.execute("CREATE TABLE track_intraday (rules TEXT)")
        set_setting(conn, "track.built_at", "x")
        set_setting(conn, "track_intraday.version", "3/730")
        set_setting(conn, "config.track", "{}")
        set_setting(conn, "trackless.keep", "y")
    init_db()
    with get_db() as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert not names & {"track_signals", "track_intraday"} and {"candles_5m", "candles_5m_days"} <= names
        assert get_setting(conn, "track.built_at") is None and get_setting(conn, "config.track") is None
        assert get_setting(conn, "track_intraday.version") is None and get_setting(conn, "trackless.keep") == "y"


def test_track_routes_are_gone(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        assert tc.get("/api/track").status_code == 404
        assert tc.get("/api/config/track").status_code in (404, 422)
        assert tc.get("/api/config/alerts").status_code == 200
