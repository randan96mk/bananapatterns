"""Momentum ORB: the evening screens (momentum: 52-week high, RSI > 60; weak: 52-week low, RSI < 40), the next-session
trade (the first 5-minute close beyond the 30-minute range: long over it, short under it; 2% stop; half at 2%; ATR
trail; 3:15 PM square-off), costs on both sides, the build with the next session's lists, and the API."""

from datetime import date, datetime, timedelta

import pytest

from backend.config import AppConfig, OrbConfig
from backend.database.models import get_db, upsert_candles
from backend.engine import momentum_orb as orb

CFG = OrbConfig()
DAY = "2026-03-02"


def daily(n=300, start=250.0, step=0.004, vol=1_000_000, last=None):
    """A steady climb (new highs, RSI high) on weekdays; `last` overrides the final candle."""
    out, d, p = [], date(2025, 1, 1), start
    for i in range(n):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        o, p = p, p * (1 + step * (1 if i % 5 else -1.5))
        out.append({"date": d.isoformat(), "open": o, "high": max(o, p) * 1.003, "low": min(o, p) * 0.997,
                    "close": p, "volume": vol})
        d += timedelta(days=1)
    if last:
        out[-1].update(last)
    return out


def five(rows, day=DAY):
    t0 = datetime.fromisoformat(f"{day} 09:15")
    out = []
    for i, (o, h, lo, c) in enumerate(rows):
        t = (t0 + timedelta(minutes=5 * i)).strftime("%H:%M")
        out.append({"ts": f"{day} {t}", "t": t, "open": o, "high": h, "low": lo, "close": c, "volume": 1000})
    return out


# --- screen ---------------------------------------------------------------------------------------

def test_screen_takes_new_highs_with_momentum():
    cs = daily()
    got = orb.screen(cs, CFG)
    assert got and got[-1]["date"] == cs[-1]["date"] and got[-1]["rsi"] > 60 and got[-1]["close"] > got[-1]["ema"]
    assert all(g["t"] >= CFG.high_bars - 1 for g in got)


@pytest.mark.parametrize("change", [{"volume": 400_000}, {"close": 150.0, "high": 151.0, "low": 149.0}])
def test_screen_skips_low_volume_and_cheap(change):
    cs = daily(last=change)
    assert not orb.screen(cs, CFG) or orb.screen(cs, CFG)[-1]["date"] != cs[-1]["date"]


def falling(n=300, start=900.0):
    """A steady slide: new 52-week lows, RSI low, under the 50-day EMA."""
    return daily(n, start=start, step=-0.004)


def test_screen_puts_new_lows_on_the_weak_list():
    got = orb.screen(falling(), CFG)
    assert got and {g["list"] for g in got} == {"weak"} and got[-1]["rsi"] < 40
    assert {g["list"] for g in orb.screen(daily(), CFG)} == {"momentum"}


def test_screen_needs_a_new_52_week_high():
    cs = daily()
    cs[-1].update(high=cs[-2]["high"] * 0.99, close=cs[-2]["close"] * 0.985)
    assert all(g["date"] != cs[-1]["date"] for g in orb.screen(cs, CFG))


def test_rsi_matches_wilders_example():
    import numpy as np
    c = np.array([44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28,
                  46.28, 46.00])
    assert orb.rsi(c, 14)[14] == pytest.approx(70.5, abs=0.1) and orb.rsi(c, 14)[15] == pytest.approx(66.3, abs=0.1)


# --- the next session --------------------------------------------------------------------------------

# 9:15-9:40 AM: the 30-minute opening range, high 101.5, low 99.5
OPEN = [(100.0, 101.0, 99.5, 100.5), (100.5, 101.2, 100.2, 101.0), (101.0, 101.5, 100.8, 101.2)] * 2
HOLD_UP = [(104.9, 105.0, 104.8, 104.9)] * 70                                       # holds near the high


def test_long_entry_stop_target_and_square_off():
    rows = OPEN + [(101.2, 102.1, 101.1, 102.0)] + [(102.0, 104.2, 101.9, 104.1)] + HOLD_UP
    t = orb.trade(five(rows), DAY, CFG)
    assert t["side"] == "long" and t["entry"] == 102.0 and t["entry_time"] == "09:50"
    assert t["stop"] == pytest.approx(102.0 * 0.98) and t["target"] == pytest.approx(102.0 * 1.02)
    assert [(e["kind"], e["time"]) for e in t["events"]][1] == ("half", "09:55") and t["booked"]
    assert t["events"][-1]["why"] == "3:15 PM square-off" and t["events"][-1]["time"] == "15:15"
    want = (0.5 * 0.02 + 0.5 * (104.9 / 102.0 - 1)) * 100
    assert t["result_pct"] == pytest.approx(want, abs=0.01) and t["status"] == "won" and t["net_pct"] < want


def test_short_under_the_range():
    hold_down = [(97.0, 97.1, 96.9, 97.0)] * 70
    rows = OPEN + [(99.6, 99.7, 98.9, 99.0)] + [(99.0, 99.1, 96.9, 97.0)] + hold_down
    t = orb.trade(five(rows), DAY, CFG)
    assert t["side"] == "short" and t["entry"] == 99.0 and t["stop"] == pytest.approx(99.0 * 1.02)
    assert t["events"][1]["kind"] == "half" and t["events"][1]["price"] == pytest.approx(99.0 * 0.98)
    want = (0.5 * 0.02 + 0.5 * (1 - 97.0 / 99.0)) * 100
    assert t["result_pct"] == pytest.approx(want, abs=0.01) and t["status"] == "won"


def test_short_stop_and_trail():
    up = OPEN + [(99.6, 99.7, 98.9, 99.0), (99.0, 101.5, 98.8, 101.2)]               # 101.5 >= the 100.98 stop
    t = orb.trade(five(up), DAY, CFG)
    assert t["status"] == "lost" and t["events"][-1]["why"] == "stop" and t["r"] == pytest.approx(-1.0, abs=0.01)
    rows = OPEN + [(99.6, 99.7, 98.9, 99.0), (99.0, 99.1, 96.9, 97.0), (97.0, 97.1, 96.0, 96.1),
                   (96.1, 98.9, 96.0, 98.8)]
    assert orb.trade(five(rows), DAY, CFG)["events"][-1]["why"] == "ATR trail"


def test_stop_first_when_one_candle_touches_both():
    t = orb.trade(five(OPEN + [(101.2, 102.1, 101.1, 102.0), (102.0, 104.5, 99.0, 102.0)]), DAY, CFG)
    assert t["status"] == "lost" and t["events"][-1]["why"] == "stop"
    assert t["events"][-1]["price"] == pytest.approx(102.0 * 0.98) and t["r"] == pytest.approx(-1.0)


def test_long_trail_after_the_half():
    rows = OPEN + [(101.2, 102.1, 101.1, 102.0), (102.0, 104.2, 101.9, 104.1), (104.1, 105.0, 104.0, 104.9),
                   (104.9, 105.0, 102.2, 102.3)]
    t = orb.trade(five(rows), DAY, CFG)
    assert t["events"][-1]["why"] == "ATR trail" and t["events"][-1]["price"] == 102.3


def test_first_close_decides_and_the_weak_list_is_short_only():
    up_first = OPEN + [(101.2, 102.1, 101.1, 102.0), (102.0, 102.1, 98.5, 98.6)]
    t = orb.trade(five(up_first), DAY, CFG, allowed=("short",))
    assert t["status"] == "no_entry" and t["reason"] == "broke above the range first (shorts only)"
    assert orb.trade(five(up_first), DAY, CFG)["side"] == "long"                    # momentum list: the long


def test_no_entry_reasons():
    assert orb.trade([], DAY, CFG)["reason"] == "no 5-minute candles for the day"
    t = orb.trade(five(OPEN + [(101.2, 101.4, 101.0, 101.3)] * 60), DAY, CFG)
    assert t["status"] == "no_entry" and t["reason"] == "no 5-minute close beyond the 30-minute range by 1:30 PM"
    late = OPEN + [(101.2, 101.4, 101.0, 101.3)] * 45 + [(101.3, 102.0, 101.2, 101.9)]   # closes over H at 13:40
    assert orb.trade(five(late), DAY, CFG)["status"] == "no_entry"


def test_costs_both_sides():
    assert orb.net_pct("long", 100.0, [(1.0, 100.0)], CFG) == pytest.approx(-(0.06 + 0.05 * 2), abs=0.01)
    assert orb.net_pct("short", 100.0, [(1.0, 100.0)], CFG) == pytest.approx(-(0.06 + 0.05 * 2), abs=0.01)
    assert orb.net_pct("short", 100.0, [(1.0, 98.0)], CFG) == pytest.approx(2 - 0.16, abs=0.01)


def test_short_portfolio_makes_money_when_the_price_falls(monkeypatch):
    monkeypatch.setattr("backend.ingest.universe.resolve_universe", lambda cfg, *a: {"stocks": [], "indices": []})
    r = {"status": "won", "side": "short", "symbol": "X", "signal_date": DAY, "trade_date": DAY, "entry": 100.0,
         "stop": 102.0, "rsi": 30, "events": [{"kind": "entry", "date": DAY, "time": "09:50", "price": 100.0},
                                              {"kind": "exit", "date": DAY, "time": "11:00", "price": 98.0}]}
    cfg = AppConfig()
    cfg.orb.cost_pct = cfg.orb.slippage_pct = 0.0
    p = orb.portfolio([r], cfg)
    assert p["return"] == pytest.approx(0.2 * 2, abs=0.01)                          # 20% of the account, +2%


# --- build and API ----------------------------------------------------------------------------------

class FakeKite:
    """5-minute candles: an opening range, then a breakout to the day's high, then flat."""
    has_token = True

    def __init__(self, daily_by_token):
        self.daily, self.calls = daily_by_token, 0

    def historical_bars(self, token, start, end, interval="5minute", label=""):
        self.calls += 1
        out = []
        for c in self.daily.get(token, []):
            if start.isoformat() <= c["date"] <= end.isoformat():
                o, h, lo, cl = c["open"], c["high"], c["low"], c["close"]
                mid = (o + lo) / 2
                rows = [(o, o * 1.001, lo, mid)] * 6 + [(mid, h, mid, h * 0.999)] + [(h * 0.999, h * 0.999, h * 0.998, cl)] * 68
                out += [dict(r, volume=1000) for r in five(rows, c["date"])]
        return out


def setup(monkeypatch, series):
    with get_db() as conn:
        for tok, cs in enumerate(series.values(), start=1):
            upsert_candles(conn, tok, cs)
    stocks = [{"symbol": s, "instrument_token": i} for i, s in enumerate(series, start=1)]
    monkeypatch.setattr("backend.ingest.universe.resolve_universe", lambda cfg, *a: {"stocks": stocks, "indices": []})


def test_build_update_and_api(monkeypatch):
    from fastapi.testclient import TestClient
    cs = daily(420)
    setup(monkeypatch, {"MOMO": cs, "DULL": daily(420, step=0.0)})
    cfg = AppConfig()
    cfg.orb.history_days = 3650
    kite = FakeKite({1: cs})
    r = orb.build(cfg, kite)
    assert r["status"] == "ok" and r["added"] > 10 and kite.calls > 0
    rows = orb.rows("2y")
    assert {x["symbol"] for x in rows} == {"MOMO"}
    assert rows[0]["signal_date"] == cs[-1]["date"] and rows[0]["status"] == "pending"     # tomorrow's list
    assert any(x["status"] in ("won", "lost") for x in rows)
    calls = kite.calls
    assert orb.build(cfg, kite)["added"] == 0 and kite.calls == calls                        # nothing new to fetch
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    from backend.api.routes import S
    with TestClient(main.app) as tc:
        S().config.orb.history_days = 3650
        d = tc.get("/api/orb?period=2y").json()
        assert d["built"] and d["counts"]["entered"] > 0 and d["watchlist"][0]["symbol"] == "MOMO"
        assert [y["year"] for y in d["by_year"]] == ["y1", "y2"] and len(d["by_range"]) == 3
        assert d["portfolio"]["dates"] and d["rules"]["target_pct"] == 2.0 and len(d["by_set"]) == 3
        assert tc.get("/api/orb?period=5y").status_code == 422
        t = tc.get("/api/orb/trades?period=2y&status=closed&limit=5").json()
        assert len(t["rows"]) <= 5 and t["total"] >= len(t["rows"])
        assert tc.post("/api/orb/build").status_code == 400                                 # no Kite login in tests
