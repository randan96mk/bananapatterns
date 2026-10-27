"""Intraday entry backtest: the entry rules on 5-minute candles, the stop / +2% half / ATR trail (5-minute and daily),
failed pivot crosses counted, the portfolio replaying same-day events in time order, the build and the API."""

from datetime import date, datetime, timedelta

import pytest

from backend.config import IntradayConfig
from backend.database.models import get_db
from backend.engine import track, track_intraday as ti
from backend.engine.bases import detect_bases
from tests.test_bases import build
from tests.test_track import BASE, UP, app_config, bars, setup_db

CFG = IntradayConfig()


def five(rows, day="2026-03-02", start="09:15"):
    """[(open, high, low, close, volume)] as 5-minute candles from `start`."""
    t0 = datetime.fromisoformat(f"{day} {start}")
    out = []
    for i, (o, h, lo, c, v) in enumerate(rows):
        t = (t0 + timedelta(minutes=5 * i)).strftime("%H:%M")
        out.append({"ts": f"{day} {t}", "t": t, "open": o, "high": h, "low": lo, "close": c, "volume": v})
    return out


OPEN_RANGE = [(99.5, 99.8, 99.2, 99.6, 20_000), (99.6, 99.9, 99.4, 99.8, 20_000), (99.8, 100.0, 99.5, 99.9, 20_000)]
TRIGGER = (99.9, 100.6, 99.8, 100.5, 30_000)          # 9:30 candle: closes over the pivot (100)


def entry(rows, pivot=100.0, prev_close=99.0, day_open=99.5, avg=75_000, idx=None, cfg=CFG):
    return ti.find_entry(five(rows), idx, pivot, prev_close, day_open, avg, "2026-03-02", cfg)


# --- entry ----------------------------------------------------------------------------------------

def test_entry_on_the_first_qualifying_candle():
    e, why = entry(OPEN_RANGE + [TRIGGER])
    assert why is None and e["time"] == "09:35" and e["price"] == 100.5
    assert e["stop"] == 99.8                                   # the candle low is closer than the 15-minute low (99.2)


def test_no_entry_before_the_opening_range_ends():
    e, why = entry([(99.5, 100.6, 99.4, 100.5, 50_000)] + OPEN_RANGE[1:])
    assert e is None and why == "no 5-minute close over the pivot by 1:30 PM"


def test_a_wide_stop_skips_the_candle_and_a_later_one_qualifies():
    wide = [(99.5, 99.8, 98.0, 99.6, 20_000)] + OPEN_RANGE[1:] + [(99.9, 100.6, 98.5, 100.5, 30_000)]
    e, why = entry(wide)
    assert e is None and "stop more than 1.5% away" in why
    e, _ = entry(wide + [(100.5, 100.8, 100.3, 100.7, 30_000)])
    assert e["time"] == "09:40" and e["stop"] == 100.3


@pytest.mark.parametrize("kw, text", [
    ({"day_open": 103.0}, "gap over 3%"),
    ({"avg": 10_000_000}, "volume pace under 1.5×"),
    ({"idx": five([(22000, 22010, 21990, 22000, 0)] * 3 + [(22000, 22000, 21940, 21950, 0)])}, "Nifty 500 under its open"),
])
def test_entry_filters(kw, text):
    e, why = entry(OPEN_RANGE + [TRIGGER], **kw)
    assert e is None and text in why


def test_no_entry_under_vwap_or_extended():
    heavy = [(101, 101.5, 100.8, 101.2, 500_000)] + OPEN_RANGE[1:]        # VWAP pulled up by the heavy first candle
    assert "under VWAP" in entry(heavy + [TRIGGER])[1]
    assert "more than 3% over the pivot" in entry(OPEN_RANGE + [(99.9, 103.6, 99.8, 103.5, 30_000)])[1]


# --- the trade --------------------------------------------------------------------------------------

class Stub:
    """FiveMin stand-in: candles per day; a day missing from `days` -> None (not fetched)."""

    def __init__(self, days, forbid=()):
        self.days, self.forbid = days, set(forbid)

    def day(self, d, ahead=1):
        assert d not in self.forbid, f"5-minute candles asked for {d}"
        return self.days.get(d)


def daily(rows):
    return track.Bars(bars(rows, start=date(2026, 3, 2)), 50)


PRE = [(99.0, 99.5, 98.8, 99.2, 1), (99.2, 99.6, 99.0, 99.4, 1), (99.4, 99.8, 99.2, 99.6, 1), (99.6, 100.1, 99.5, 100.0, 1)]
E = {"i": 3, "time": "09:35", "price": 100.0, "stop": 99.0}


def test_half_at_2pct_then_the_5min_trail():
    b = daily([(99, 103, 98.8, 100.5)] * 2)
    d0 = b.dates[0]
    rows = PRE + [(100.0, 102.1, 99.9, 102.0, 1), (102.0, 103.0, 101.9, 102.9, 1), (101.5, 101.6, 100.4, 100.5, 1)]
    t = ti.simulate(b, 0, E, Stub({d0: five(rows, d0)}), CFG, "i5m")
    kinds = [e["kind"] for e in t["events"]]
    assert kinds == ["entry", "half", "exit"] and t["events"][1]["price"] == 102.0
    assert t["events"][2]["why"] == "ATR trail (5-min)" and t["events"][2]["price"] == 100.5
    assert t["result_pct"] == pytest.approx(0.5 * 2 + 0.5 * 0.5) and t["r"] == pytest.approx(1.25) and t["status"] == "won"


def test_stop_and_target_in_one_candle_counts_as_the_stop():
    b = daily([(99, 103, 98.8, 100.5)] * 2)
    d0 = b.dates[0]
    t = ti.simulate(b, 0, E, Stub({d0: five(PRE + [(100.0, 102.5, 98.9, 101.0, 1)], d0)}), CFG, "i5m")
    assert t["status"] == "lost" and t["events"][-1]["why"] == "stop" and t["r"] == pytest.approx(-1.0)


def test_daily_trail_runs_on_daily_candles_after_the_booking():
    b = daily([(99.5, 103, 99.0, 102.9), (103, 106, 105.5, 105.8), (105.8, 106.5, 105.6, 106.2), (106, 106, 101.9, 102.0)])
    d = b.dates
    rows = PRE + [(100.0, 102.1, 99.9, 102.0, 1), (102.0, 103.0, 101.9, 102.9, 1)]
    t = ti.simulate(b, 0, E, Stub({d[0]: five(rows, d[0])}, forbid=d[1:]), CFG, "iday")
    assert t["events"][-1]["why"] == "ATR trail (daily)" and t["exit_date"] == d[3]
    assert t["result_pct"] == pytest.approx(0.5 * 2 + 0.5 * 2.0) and t["days"] == 4
    gap = daily([(99.5, 103, 99.0, 102.9), (99.5, 100.5, 99.0, 100.2)])
    t = ti.simulate(gap, 0, E, Stub({gap.dates[0]: five(rows, gap.dates[0])}), CFG, "iday")
    assert t["events"][-1]["why"] == "gap under breakeven" and t["result_pct"] == pytest.approx(1 - 0.25)


def test_pending_until_the_next_days_candles_are_fetched():
    b = daily([(99, 100.5, 98.8, 100.4), (100.4, 100.6, 99.5, 100.0)])
    t = ti.simulate(b, 0, E, Stub({b.dates[0]: five(PRE + [(100.0, 100.5, 99.5, 100.4, 1)], b.dates[0])}), CFG, "i5m")
    assert t["status"] == "pending"


# --- the portfolio: same-day events in time order -------------------------------------------------------

def test_same_day_entry_and_exit_close_the_position():
    days = ["2026-03-02", "2026-03-03"]
    def tr(sym, t_in, t_out):
        r = {"symbol": sym, "breakout_date": days[0], "pattern": "bases", "tier": "A", "stop": 99.0}
        return [({"kind": "entry", "date": days[0], "time": t_in, "price": 100.0}, r),
                ({"kind": "exit", "date": days[0], "time": t_out, "price": 99.0}, r)]
    by_day = {days[0]: tr("A", "10:05", "11:20") + tr("B", "10:10", "12:00")}
    curve, idle, taken, skipped = track._book(days, by_day, {}, 1_000_000, 10, 0.01, 0.20)
    assert taken == 2 and idle[-1] == pytest.approx(1.0)       # both closed the same day: all cash again
    assert curve[-1] == pytest.approx(-0.4)                    # 2 x 2,000 shares x -1
    assert track._book(days, by_day, {}, 1_000_000, 10, 0.01, 0.20, max_new_per_day=1)[2:] == (1, 1)


def test_costs_per_trade_and_in_the_portfolio():
    from backend.config import TrackConfig
    tc = TrackConfig()                     # 0.05% slippage a fill; 0.25% overnight, 0.06% same day (round trip)
    s, over, same = 0.0005, 0.0025, 0.0006
    d0, d1 = "2026-03-02", "2026-03-03"
    r = {"status": "won", "events": [{"kind": "entry", "date": d0, "price": 100.0},
                                     {"kind": "half", "date": d0, "price": 102.0, "frac": 0.5},
                                     {"kind": "exit", "date": d1, "price": 101.0}]}
    buy = 100 * (1 + s)
    want = 0.5 * (102 * (1 - s) / buy - 1 - same) + 0.5 * (101 * (1 - s) / buy - 1 - over)
    assert track.net_pct(r, tc) == pytest.approx(want * 100, abs=0.01)
    assert track.net_pct(dict(r, status="running"), tc) is None
    row = {"symbol": "A", "breakout_date": d0, "pattern": "bases", "tier": "A", "stop": 99.0}
    flat = {d0: [({"kind": "entry", "date": d0, "time": "10:00", "price": 100.0}, row),
                 ({"kind": "exit", "date": d0, "time": "11:00", "price": 100.0}, row)]}
    costs = track._costs(tc)
    curve = track._book([d0], flat, {}, 1_000_000, 10, 0.01, 0.20, costs=costs)[0]
    assert curve[-1] == pytest.approx(-0.2 * (2 * s + same) * 100, abs=0.01)    # a 2L position, flat, same day
    held = {d0: flat[d0][:1], d1: [({"kind": "exit", "date": d1, "time": "11:00", "price": 100.0}, row)]}
    curve = track._book([d0, d1], held, {"A": {d0: 100.0, d1: 100.0}}, 1_000_000, 10, 0.01, 0.20, costs=costs)[0]
    assert curve[-1] == pytest.approx(-0.2 * (2 * s + over) * 100, abs=0.01)    # held overnight: delivery charges


# --- the Nifty 500 filter ----------------------------------------------------------------------------

IDX = five([(22000, 22030, 21990, 22020, 0), (22020, 22040, 22010, 22030, 0), (22030, 22050, 22020, 22040, 0),
            (22040, 22045, 21980, 21990, 0), (22000, 22060, 21995, 22045, 0), (22045, 22080, 22040, 22075, 0)])
# 9:15-9:25 opening range high 22050; 9:30 under its open (22000)... 9:35 over the open, 9:40 over the 22050 high


def test_index_filter_modes():
    assert ti.index_ok(IDX, "09:30", 21900, "any", 15) == "index"             # 21990: under its 22000 open
    assert ti.index_ok(IDX, "09:25", 21900, "any", 15) is None
    assert ti.index_ok(IDX, "09:30", 21900, "up", 15) == "index_up"
    assert ti.index_ok(IDX, "09:35", 21900, "up", 15) is None                  # over open and yesterday's close
    assert ti.index_ok(IDX, "09:35", 22100, "up", 15) == "index_up"           # under yesterday's close
    assert ti.index_ok(IDX, "09:35", 21900, "strong", 15) == "index_strong"   # 22045 not over the 22050 high
    assert ti.index_ok(IDX, "09:40", 21900, "strong", 15) is None
    assert ti.index_ok([], "09:40", 21900, "strong", 15) == "index_data"
    assert ti.index_ok([], "09:40", 21900, "any", 15) is None


def test_entry_waits_for_the_nifty_500():
    rows = OPEN_RANGE + [TRIGGER, (100.5, 100.8, 100.3, 100.6, 30_000), (100.6, 100.9, 100.4, 100.8, 30_000)]
    e, _ = entry(rows, idx=IDX)                                                # any: 9:30 index 21990 < open
    assert e["time"] == "09:40"
    e, _ = ti.find_entry(five(rows), IDX, 100.0, 99.0, 99.5, 75_000, "2026-03-02", CFG, 21900, "strong")
    assert e["time"] == "09:45"                                                # waits for the index breakout
    e, why = ti.find_entry(five(rows), IDX, 100.0, 99.0, 99.5, 75_000, "2026-03-02", CFG, 22100, "up")
    assert e is None and "Nifty 500 not up" in why


def test_swing_entry_with_the_filter():
    rows = OPEN_RANGE + [TRIGGER, (100.5, 100.8, 100.3, 100.6, 30_000), (100.6, 100.9, 100.4, 100.8, 30_000)]
    ent, _ = ti.swing_entry(five(rows), IDX, 21900, "up", 100.0, 93.0, CFG, 0.08, 0.05)
    assert ent == (100.6, "09:40")
    ent, why = ti.swing_entry(five(rows), IDX, 21900, "up", 99.0, 93.0, CFG, 0.08, 0.005)
    assert ent is None and "beyond the buy zone" in why                       # 100.6 / 100.8 > 99 x 1.005
    t = track.simulate(daily([(99, 100, 98, 99.5), (99.5, 101, 99, 100.9), (100.9, 102, 100, 101.5)]), 0, 99.0,
                       93.0, app_config().track, 0.05, entry=(100.6, "09:40"))
    assert t["entry"] == 100.6 and t["events"][0]["time"] == "09:40" and t["stop"] == pytest.approx(93.0, abs=0.01)


# --- the watchlist: failed crosses count -------------------------------------------------------------

def series_with_a_failed_cross():
    cs = build([UP, BASE, ("bar", 1, 1.03)])
    pivot = detect_bases(cs, app_config().bases)[-1].pivot
    last = cs[-1]
    nxt = date.fromisoformat(last["date"]) + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    fail = {"date": nxt.isoformat(), "open": last["close"], "high": pivot * 1.01, "low": last["close"] * 0.995,
            "close": pivot * 0.995, "volume": 1_500_000}
    return cs + [fail], pivot


def test_watchlist_counts_a_cross_that_closed_back_under():
    cs, pivot = series_with_a_failed_cross()
    u = track.Universe(app_config(), [], None)
    u.series, u.dates = {"X": cs}, {"X": [c["date"] for c in cs]}
    u.at = {"X": {d: i for i, d in enumerate(u.dates["X"])}}
    sig = ti.watch_signals(u, "X", cs[0]["date"])
    assert [s["breakout_date"] for s in sig] == [cs[-1]["date"]] and sig[0]["pivot"] == pytest.approx(pivot, abs=0.01)


# --- build and API ----------------------------------------------------------------------------------

class FakeKite:
    """5-minute candles made from each daily candle: a straight walk from the open to the close."""
    has_token = True

    def __init__(self, daily_by_token):
        self.daily, self.calls = daily_by_token, 0

    def historical_bars(self, token, start, end, interval="5minute", label=""):
        self.calls += 1
        out = []
        for c in self.daily.get(token, []):
            if not start.isoformat() <= c["date"] <= end.isoformat():
                continue
            o, cl = c["open"], c["close"]
            px = [o + (cl - o) * i / 74 for i in range(75)]
            out += [dict(r, ts=r["ts"]) for r in five([(p, p * 1.001, p * 0.999, p, c["volume"] // 75) for p in px], c["date"])]
        return out


def test_build_and_api(monkeypatch):
    from fastapi.testclient import TestClient
    cs, pivot = series_with_a_failed_cross()
    fail_i = len(cs) - 1
    cs = cs + build([("trend", 30, 0.2, 3_000_000)], start=cs[-1]["close"] * 1.012)   # a breakout on volume, a climb
    d = date(2024, 1, 1)
    for c in cs:                                               # one weekday calendar for the joined series
        while d.weekday() >= 5:
            d += timedelta(days=1)
        c["date"] = d.isoformat()
        d += timedelta(days=1)
    setup_db(monkeypatch, {"X": cs})
    cfg = app_config()
    fake = FakeKite({1: cs})
    assert track.build(cfg)["status"] == "ok"
    r = ti.build(cfg, fake)
    assert r["status"] == "ok" and r["added"] >= 2 and r["pending"] == 0
    for rules in [k for k in ti.RULES if "@" not in k]:                # no Nifty 500 candles in this test
        got = {x["breakout_date"]: x for x in track.rows("1y", rules=rules)}
        assert cs[fail_i]["date"] in got                       # the failed cross is in the record
        assert any(x["status"] in ("won", "lost", "running") for x in got.values())
    filtered = track.rows("1y", rules="iday@up") + track.rows("1y", rules="swing@strong")
    assert filtered and all(x["status"] in ("no_entry", "in_trade") for x in filtered)
    assert any("no Nifty 500 candles" in (x["reason"] or "") for x in filtered)
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM candles_5m").fetchone()[0] > 0
    calls = fake.calls
    assert ti.build(cfg, fake)["added"] == 0 and fake.calls == calls      # the daily update re-uses stored candles
    for x in track.rows("2y", rules="iday_s75"):                       # the stop is at least 0.75% under the entry
        if x["status"] in ("won", "lost", "running"):
            assert x["stop"] <= round(x["entry"] * (1 - 0.0075), 2) + 0.01
    with get_db() as conn:                                             # an older build: re-run from scratch
        conn.execute("UPDATE app_settings SET value = '1/3650' WHERE key = 'track_intraday.version'")
    assert "new rule sets" in ti.stale_reason(cfg)
    assert ti.build(cfg, fake)["added"] >= 2 and ti.stale_reason(cfg) is None and fake.calls == calls
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    from backend.api.routes import S
    with TestClient(main.app) as tc:
        S().config.track.history_days = cfg.track.history_days      # the window the test built
        d = tc.get("/api/track?rules=i5m&period=1y").json()
        assert d["built"] and d["rules_set"] == "i5m" and d["rules"]["target_pct"] == 2.0
        assert [x["rules"] for x in d["rule_sets"]] == ["swing", "i5m", "iday", "iday_s50", "iday_s75"]
        assert all("net_avg_pct" in x for x in d["rule_sets"]) and d["stale"] is None
        d = tc.get("/api/track?rules=iday_s50&nifty=up&period=1y").json()
        assert d["built"] and d["rules_set"] == "iday_s50@up" and d["name"].endswith("Nifty 500 up")
        assert all("return_any" in x for x in d["rule_sets"] if x["rules"] != "swing")
        assert [x["key"] for x in d["rule_sets"]] == ["swing@up", "i5m@up", "iday@up", "iday_s50@up", "iday_s75@up"]
        assert tc.get("/api/track?nifty=sideways").status_code == 422
        S().config.track.history_days = 365                          # another window: stale until re-run
        d = tc.get("/api/track?rules=iday").json()
        assert not d["built"] and "365 days" in d["stale"] and len(d["rule_sets"]) <= 1
        S().config.track.history_days = cfg.track.history_days
        assert tc.get("/api/track?rules=nope").status_code == 422
        assert tc.post("/api/track/intraday/build").status_code == 400            # no Kite login in tests
        t = tc.get("/api/track/trades?rules=iday&period=1y").json()["stocks"]
        assert [g["symbol"] for g in t] == ["X"]
