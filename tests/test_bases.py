"""Base detector on synthetic series with known answers."""

from datetime import date, timedelta

import pytest

from backend.config import BaseConfig
from backend.engine.bases import detect_bases


def build(segments, start=100.0, vol=1_000_000):
    """segments: list of (kind, bars, arg[, volume]).
    'trend' arg = total % move (linear); 'range' arg = (lo_frac, hi_frac) of the segment's start price,
    oscillating inside; 'bo' arg = close as a multiple of the price before the last range (breakout bar);
    'bar' arg = close multiple of previous close (single bar)."""
    closes, vols = [], []
    p = ref = start
    for seg in segments:
        kind, bars, arg = seg[:3]
        v = seg[3] if len(seg) > 3 else vol
        if kind == "trend":
            step = (1 + arg) ** (1 / bars)
            for _ in range(bars):
                p *= step
                closes.append(p); vols.append(v)
        elif kind == "range":
            lo, hi = arg
            base_p = ref = p
            for i in range(bars):
                frac = lo + (hi - lo) * (0.5 + 0.5 * ((-1) ** i) * (1 - i / bars))
                closes.append(base_p * frac); vols.append(v)
            p = closes[-1]
        elif kind == "bo":
            p = ref * arg
            closes.append(p); vols.append(v)
        elif kind == "bar":
            p *= arg
            closes.append(p); vols.append(v)
    candles, d = [], date(2024, 1, 1)
    prev = closes[0]
    for c, v in zip(closes, vols):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        o = prev
        candles.append({"date": d.isoformat(), "open": o, "high": max(o, c) * 1.005,
                        "low": min(o, c) * 0.995, "close": c, "volume": v})
        prev = c
        d += timedelta(days=1)
    return candles


CFG = BaseConfig(reset_ma=0, prior_advance_bars=63, min_prior_advance=0.20)


def staircase():
    return build([
        ("trend", 60, 0.60),
        ("range", 20, (0.90, 0.99), 600_000),        # base 1: 4 weeks, quiet volume
        ("bo", 1, 1.06, 3_000_000),                   # breakout on 3x+ volume
        ("trend", 30, 0.25),
        ("range", 30, (0.85, 0.99), 500_000),         # base 2: 6 weeks
        ("bo", 1, 1.03, 400_000),                     # low-volume breakout
        ("trend", 10, 0.05),
    ])


def test_staircase_two_numbered_bases():
    bases = detect_bases(staircase(), CFG)
    assert [b.number for b in bases] == [1, 2]
    b1, b2 = bases
    assert b1.status == b2.status == "broken_out"
    assert b1.weeks == 4 and b2.weeks == 6
    assert b1.breakout.volume_ok and "low volume" not in b1.breakout.tags
    assert not b2.breakout.volume_ok and "low volume" in b2.breakout.tags
    assert b1.vol_dryup < 1.0                       # volume faded in the base
    assert b1.label == "B1 · 4w"


def test_pivot_is_base_high_and_breakout_closes_above_it():
    candles = staircase()
    b = detect_bases(candles, CFG)[0]
    seg = candles[b.start_index:b.end_index + 1]
    assert b.pivot == pytest.approx(max(c["high"] for c in seg), abs=0.006)
    assert b.low == pytest.approx(min(c["low"] for c in seg), abs=0.006)
    assert all(c["close"] <= b.pivot + 0.006 for c in seg)
    assert b.breakout.close > b.pivot and b.breakout.through_pct > 0
    assert b.breakout.date == candles[b.end_index + 1]["date"]


def test_short_pause_is_not_a_base():
    candles = build([("trend", 60, 0.6), ("range", 8, (0.95, 0.99)), ("bo", 1, 1.05), ("trend", 20, 0.1)])
    assert detect_bases(candles, CFG) == []


def test_deep_decline_is_not_a_base_and_resets_count():
    candles = build([
        ("trend", 60, 0.6), ("range", 20, (0.9, 0.99)), ("bo", 1, 1.06, 3e6),
        ("trend", 20, -0.50),                          # 50% crash: no base, count resets
        ("trend", 60, 0.9), ("range", 20, (0.9, 0.99)), ("bo", 1, 1.06, 3e6), ("trend", 5, 0.03),
    ])
    bases = detect_bases(candles, CFG)
    assert all(b.depth_pct <= CFG.max_base_depth * 100 for b in bases)
    assert [b.number for b in bases] == [1, 1]


def test_slow_grind_up_is_not_a_base():
    # closes never clear the prior bar's wick high, but the pivot keeps creeping up
    candles = build([("trend", 60, 0.6), ("trend", 40, 0.20), ("trend", 5, 0.02)])
    assert detect_bases(candles, CFG) == []


def test_no_prior_advance_no_base():
    candles = build([("range", 80, (0.9, 0.99)), ("bo", 1, 1.05), ("trend", 5, 0.02)])
    assert detect_bases(candles, CFG) == []


def test_forming_base_at_end():
    candles = build([("trend", 60, 0.6), ("range", 25, (0.9, 0.99))])
    bases = detect_bases(candles, CFG)
    assert len(bases) == 1 and bases[0].status == "forming" and bases[0].breakout is None
    assert bases[0].dist_to_pivot_pct >= 0


def test_detection_is_causal():
    """Bases that ended before bar k are identical whether or not later bars exist."""
    candles = staircase()
    full = {b.start_date: b for b in detect_bases(candles, CFG) if b.status == "broken_out"}
    for k in range(70, len(candles)):
        for b in detect_bases(candles[:k], CFG):
            if b.status == "broken_out":
                f = full[b.start_date]
                assert (b.end_date, b.pivot, b.low, b.number, b.breakout.date) == \
                       (f.end_date, f.pivot, f.low, f.number, f.breakout.date)


def test_squat_flag_and_pending():
    candles = build([("trend", 60, 0.6), ("range", 20, (0.9, 0.99)), ("bo", 1, 1.03, 3e6),
                     ("bar", 1, 0.95), ("trend", 6, 0.01)])
    b = detect_bases(candles, CFG)[0]
    assert b.breakout.squat is True and "squat" in b.breakout.tags
    b2 = detect_bases(candles[:b.breakout.index + 1], CFG)[0]
    assert b2.breakout.squat is None                # outcome not known on breakout day


def test_ma_reset_restarts_count():
    candles = staircase()
    cfg = BaseConfig(reset_ma=10, prior_advance_bars=63, min_prior_advance=0.20)
    # base 2 dips below its own 10-SMA, so with a short MA every base restarts at B1
    assert [b.number for b in detect_bases(candles, cfg)] == [1, 1]
