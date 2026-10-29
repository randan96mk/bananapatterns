"""ORB + VWAP: VWAP and its session reset, the opening range, relative volume, data checks, signals (long, short,
duplicates, the entry window), the trade (stop first, gaps, square-off), Zerodha charges, sizing, the daily limits,
the turnover universe, the build and the API."""

from datetime import date, datetime, timedelta

import pytest

from backend.config import AppConfig, OrbConfig
from backend.database.models import get_db, upsert_candles
from backend.engine import costs as fees
from backend.engine import intraday_ind as ind
from backend.engine import orb_vwap as orb

CFG = OrbConfig()
DAY = "2026-03-02"


def sess(rows, day=DAY, vol=1000):
    """Candles from 9:15 AM: rows of (open, high, low, close[, volume]), padded flat to 3:25 PM."""
    t0 = datetime.fromisoformat(f"{day} 09:15")
    rows = list(rows)
    last = rows[-1][3]
    rows += [(last, last, last, last)] * (75 - len(rows))
    out = []
    for i, r in enumerate(rows):
        t = (t0 + timedelta(minutes=5 * i)).strftime("%H:%M")
        out.append({"t": t, "ts": f"{day} {t}", "open": r[0], "high": r[1], "low": r[2], "close": r[3],
                    "volume": r[4] if len(r) > 4 else vol})
    return out


PREV = sess([(100, 100.2, 99.8, 100)], day="2026-02-27")
OR = [(100, 100.5, 99.8, 100.3), (100.3, 100.8, 100.1, 100.6), (100.6, 101.0, 100.4, 100.9)]   # range 99.8-101


def long_day(after=None, vol=3000):
    """The 9:30 candle closes over the range high and VWAP on 3x volume; then `after` candles."""
    return sess(OR + [(100.9, 101.6, 100.8, 101.5, vol)] + (after or [(101.5, 101.7, 101.4, 101.6)]))


# --- indicators ---------------------------------------------------------------------------------------

def test_vwap_is_cumulative_typical_price_and_resets_each_session():
    bars = sess([(10, 12, 8, 10, 100), (10, 13, 10, 13, 300)])
    vw = ind.vwap(bars[:2])
    assert vw[0] == pytest.approx(10) and vw[1] == pytest.approx((10 * 100 + 12 * 300) / 400)
    assert ind.vwap(sess([(50, 50, 50, 50, 10)]))[0] == 50                       # a new session starts over
    assert ind.vwap([{"high": 1, "low": 1, "close": 1, "volume": 0}]) == [None]


def test_opening_range_needs_every_candle():
    bars = long_day()
    assert ind.opening_range(bars) == (101.0, 99.8)
    assert ind.opening_range([b for b in bars if b["t"] != "09:20"]) is None


def test_relative_volume_and_slope():
    assert ind.rel_volume([1] * 20 + [3], 20, 20) == 3.0 and ind.rel_volume([1] * 5, 4, 20) is None
    assert ind.vwap_slope([1, 2, 3, 4], 3, 3) == 3 and ind.vwap_slope([None, 2, 3, 4], 3, 3) is None


def test_bad_data_is_caught():
    bars = long_day()
    assert ind.bad_session(bars, "09:15", "15:10") is None
    assert "missing" in ind.bad_session(bars[:5] + bars[6:], "09:15", "15:10")
    broken = [dict(b) for b in bars]
    broken[4]["high"] = broken[4]["close"] - 1
    assert "bad candle" in ind.bad_session(broken, "09:15", "15:10")
    assert "stop before" in ind.bad_session(bars[:40], "09:15", "15:10")
    assert orb.signals(PREV, broken, CFG) == []                                  # bad data: no signal


# --- signals ---------------------------------------------------------------------------------------

def test_long_signal_on_a_close_over_the_range_and_vwap_with_volume():
    s = orb.signals(PREV, long_day(), CFG)
    assert [(x["side"], x["signal_time"]) for x in s] == [("long", "09:30")] and s[0]["rvol"] == 3.0


def test_no_signal_without_volume_or_inside_the_range():
    assert orb.signals(PREV, long_day(vol=1200), CFG) == []
    assert orb.signals(PREV, sess(OR + [(100.9, 100.95, 100.5, 100.9, 5000)]), CFG) == []
    assert orb.signals([], long_day(), CFG) == []                                # no earlier candles for RVOL


def test_short_mirror_and_one_signal_per_side():
    down = [(100, 100.2, 99.4, 99.5), (99.5, 99.6, 99.0, 99.1), (99.1, 99.2, 98.8, 98.9)]
    bars = sess(down + [(98.9, 98.9, 98.2, 98.3, 3000), (98.3, 98.3, 97.5, 97.6, 4000)])
    s = orb.signals(PREV, bars, CFG)
    assert [(x["side"], x["signal_time"]) for x in s] == [("short", "09:30")]    # the second breakout is ignored


def test_signals_only_in_the_entry_window():
    late = OR + [(100.9, 100.95, 100.5, 100.9)] * 26 + [(100.9, 101.6, 100.8, 101.5, 9000)]   # the 11:35 candle
    assert orb.signals(PREV, sess(late), CFG) == []


# --- the trade ------------------------------------------------------------------------------------

def test_fill_next_open_stop_under_the_breakout_low_target_2r():
    bars = long_day([(101.5, 103.5, 101.4, 103.4)])
    sig = orb.signals(PREV, bars, CFG)[0]
    o = orb.outcome(bars, sig, CFG, 0.05)
    assert o["entry_px"] == 101.5 and o["entry_fill"] == pytest.approx(101.5 * 1.0005, abs=1e-4)
    assert o["stop"] == pytest.approx(100.8 - 0.1)                               # 0.05% of 100.8 = 0.0504 -> 0.10
    assert o["target"] == pytest.approx(o["entry_fill"] + 2 * (o["entry_fill"] - o["stop"]), abs=0.01)
    assert o["reason"] == "target" and o["exit_fill"] == pytest.approx(o["target"] * 0.9995, abs=1e-3)


def test_stop_counts_first_and_square_off():
    both = long_day([(101.5, 104.0, 100.0, 102.0)])
    assert orb.outcome(both, orb.signals(PREV, both, CFG)[0], CFG, 0.0)["reason"] == "stop"
    flat = long_day()
    o = orb.outcome(flat, orb.signals(PREV, flat, CFG)[0], CFG, 0.0)
    assert o["reason"] == "close" and o["exit_time"] == "15:10"


def test_gap_through_the_stop_fills_at_the_open():
    bars = long_day([(101.5, 101.7, 101.4, 101.6), (99.0, 99.2, 98.8, 99.0)])
    o = orb.outcome(bars, orb.signals(PREV, bars, CFG)[0], CFG, 0.0)
    assert o["reason"] == "stop" and o["exit_px"] == 99.0


def test_stop_buffer_rounds_up_to_the_tick():
    assert orb.stop_buffer(100.0, CFG) == 0.05 and orb.stop_buffer(2000.0, CFG) == 1.0
    assert orb.stop_buffer(150.0, CFG) == 0.1                                    # 0.075 -> 0.10


# --- charges and sizing -------------------------------------------------------------------------------

def test_zerodha_intraday_charges():
    c = fees.charges(100_000, 100_500, CFG)
    assert c["brokerage"] == 40.0 and c["stt"] == pytest.approx(25.13, abs=0.01)
    assert c["exchange"] == pytest.approx(200_500 * 0.0000297, abs=0.01) and c["stamp"] == 3.0
    assert c["gst"] == pytest.approx(0.18 * (40 + c["exchange"] + c["sebi"]), abs=0.01)
    assert c["total"] == pytest.approx(sum(v for k, v in c.items() if k != "total"), abs=0.02)
    assert fees.charges(1000, 1000, CFG)["brokerage"] == 0.6                     # 0.03% under Rs 20


def test_size_keeps_loss_plus_charges_within_the_risk():
    qty, why = orb.size(1000.0, 995.0, 1010.0, "long", 180_000, CFG)
    loss = qty * 5 + fees.charges(qty * 1000, qty * 995, CFG)["total"]
    assert why is None and loss <= 500 and (qty + 1) * 5 + fees.charges((qty + 1) * 1000, (qty + 1) * 995, CFG)["total"] > 500
    assert orb.size(1000.0, 999.0, 1002.0, "long", 180_000, CFG)[0] == 180      # the allocation caps it
    assert orb.size(1000.0, 999.9, 1000.2, "long", 180_000, CFG) == (0, "reward under the charges")


# --- the account ----------------------------------------------------------------------------------

def cand(sym, t, net_move, side="long", day=DAY, rvol=2.0):
    """A signal at 1000 with a 5-rupee stop; exits at 1000 + net_move by 2:00 PM."""
    s = 1 if side == "long" else -1
    return {"day": day, "symbol": sym, "side": side, "entry_time": t, "entry_px": 1000.0, "entry_fill": 1000.0,
            "stop": 1000 - 5 * s, "stop_fill": 1000 - 5 * s, "target": 1000 + 10 * s, "exit_time": "14:00",
            "exit_px": 1000 + net_move * s, "exit_fill": 1000 + net_move * s, "rvol": rvol, "reason": "close"}


def test_limits_entries_a_day_and_open_positions():
    cs = [cand("A", "09:35", 5), cand("B", "09:35", 5, rvol=3.0), cand("C", "09:40", 5)]
    taken, skipped = orb.simulate(cs, CFG)
    assert [t["symbol"] for t in taken] == ["B", "A"]                            # higher relative volume first
    assert skipped == {"entries a day": 1}
    assert sum(t["value"] for t in taken) <= CFG.allocated


def test_daily_loss_limit_blocks_after_losses():
    cfg = OrbConfig(max_entries_per_day=5, max_concurrent=1)
    cs = [cand("A", "09:35", -5), dict(cand("B", "14:10", -5), exit_time="14:30"), dict(cand("C", "14:35", 5))]
    taken, skipped = orb.simulate(cs, cfg)
    assert [t["symbol"] for t in taken] == ["A", "B"] and skipped == {"daily loss limit": 1}
    assert all(-t["net"] <= cfg.risk_per_trade + 1e-6 for t in taken)


def test_one_position_per_stock_and_new_day_resets():
    cs = [cand("A", "09:35", 5), cand("A", "09:40", 5, side="short"), cand("A", "09:35", 5, day="2026-03-03")]
    taken, skipped = orb.simulate(cs, CFG)
    assert len(taken) == 2 and skipped == {"already in the stock": 1}
    t = taken[0]
    assert t["gross"] == pytest.approx(5 * t["qty"]) and t["net"] == pytest.approx(t["gross"] - t["charges"])


def test_universe_is_causal_by_median_turnover():
    def series(vols, close=500.0):
        d = date(2026, 1, 1)
        return [{"date": (d + timedelta(days=i)).isoformat(), "close": close, "volume": v} for i, v in enumerate(vols)]
    cfg = OrbConfig(top_n=1, turnover_days=3)
    u = orb.universe({"BIG": series([100, 100, 100, 1]), "SMALL": series([10, 10, 10, 10_000]),
                      "CHEAP": series([1e6] * 4, close=50)}, cfg)
    assert u["2026-01-04"] == ["BIG"]                                            # today's volume isn't known yet


# --- build and API ----------------------------------------------------------------------------------

class FakeKite:
    """Every session: the long breakout of long_day(), ending at the target."""
    has_token = True

    def __init__(self):
        self.calls = 0

    def historical_bars(self, token, start, end, interval="5minute", label=""):
        self.calls += 1
        out, d = [], start
        while d <= end:
            if d.weekday() < 5:
                out += long_day([(101.5, 103.5, 101.4, 103.4)])
                out[-75:] = [dict(b, ts=f"{d} {b['t']}") for b in out[-75:]]
            d += timedelta(days=1)
        return out


def test_build_update_and_api(monkeypatch):
    from fastapi.testclient import TestClient
    d, cs = date(2026, 1, 1), []
    while len(cs) < 40:
        if d.weekday() < 5:
            cs.append({"date": d.isoformat(), "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1_000_000})
        d += timedelta(days=1)
    with get_db() as conn:
        upsert_candles(conn, 1, cs)
    monkeypatch.setattr("backend.ingest.universe.resolve_universe",
                        lambda cfg, *a: {"stocks": [{"symbol": "LIQ", "instrument_token": 1}], "indices": []})
    cfg = AppConfig()
    kite = FakeKite()
    r = orb.build(cfg, kite)
    assert r["status"] == "ok" and r["sessions"] == 20 and r["scanned"] == 20 and kite.calls > 0
    s = orb.summary(cfg)
    assert s["total"]["normal"]["trades"] == 20 and s["total"]["normal"]["exits"] == {"target": 20}
    assert [x["split"] for x in s["splits"]] == ["dev", "val", "oos"]
    assert [x["normal"]["trades"] for x in s["splits"]] == [10, 5, 5]
    assert s["total"]["stress"]["net"] < s["total"]["normal"]["net"]
    calls = kite.calls
    assert orb.build(cfg, kite)["sessions"] == 0 and kite.calls == calls           # nothing new
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        d = tc.get("/api/orb").json()
        assert d["built"] and d["rules"]["risk_per_trade"] == 500 and len(d["curve"]["normal"]) == 20
        t = tc.get("/api/orb/trades?scenario=stress&split=oos&side=long&limit=3").json()
        assert t["total"] == 5 and len(t["rows"]) == 3
        assert tc.get("/api/orb/trades?split=year").status_code == 422
        assert tc.post("/api/orb/build").status_code == 400                         # no Kite login in tests
