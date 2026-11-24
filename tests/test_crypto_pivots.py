"""₿ Pivots (backend/crypto/pivots.py, pure): TradingView's formulas against hand-computed days, the Auto rule, the day
boundary utc vs ist, the levels in force from the PREVIOUS closed period."""

from datetime import datetime, timezone

import pytest

from backend.crypto import pivots as pv
from tests.crypto_world import k

UTC = timezone.utc


def test_traditional_hand_computed():
    lv = pv.levels("traditional", 61000, 59000, 60000)
    assert lv == {"P": 60000, "R1": 61000, "S1": 59000, "R2": 62000, "S2": 58000, "R3": 63000, "S3": 57000,
                  "R4": 64000, "S4": 56000, "R5": 65000, "S5": 55000}
    lv = pv.levels("traditional", 60500, 59200, 59800)            # P = (60500 + 59200 + 59800) / 3 = 59833.33
    assert lv["P"] == pytest.approx(59833.33)
    assert lv["R1"] == pytest.approx(2 * 59833.333 - 59200, abs=0.01) and lv["S1"] == pytest.approx(59166.67, abs=0.01)
    assert lv["R2"] == pytest.approx(59833.33 + 1300, abs=0.01)


def test_fibonacci_and_camarilla():
    f = pv.levels("fibonacci", 61000, 59000, 60000)
    assert (f["R1"], f["S1"], f["R2"], f["S2"], f["R3"], f["S3"]) == (60764, 59236, 61236, 58764, 62000, 58000)
    assert "R4" not in f
    cm = pv.levels("camarilla", 61000, 59000, 60000)            # C ± 1.1 × range / 12, 6, 4, 2
    assert cm["R1"] == pytest.approx(60183.33) and cm["S3"] == pytest.approx(59450) and cm["R4"] == pytest.approx(61100)
    dm = pv.levels("dm", 61000, 59000, 60500, o=60000)            # close > open: X = 2H + L + C
    assert dm["P"] == pytest.approx((122000 + 59000 + 60500) / 4)
    with pytest.raises(ValueError):
        pv.levels("nope", 1, 0, 1)


def test_auto_rule():
    assert pv.auto_period("1m") == pv.auto_period("5m") == pv.auto_period("15m") == "daily"
    assert pv.auto_period("1h") == "weekly"
    assert pv.auto_period("5m", "monthly") == "monthly" and pv.auto_period("1h", "daily") == "daily"


def test_day_boundary_utc_vs_ist():
    a = int(datetime(2026, 11, 23, 20, 0, tzinfo=UTC).timestamp())    # 1:30 AM IST on the 24th
    b = int(datetime(2026, 11, 24, 0, 15, tzinfo=UTC).timestamp())    # 5:45 AM IST on the 24th
    assert pv.period_key(a, "daily", "utc") == "2026-11-23" and pv.period_key(b, "daily", "utc") == "2026-11-24"
    assert pv.period_key(a, "daily", "ist") == pv.period_key(b, "daily", "ist") == "2026-11-24"
    # 5-minute candles: the 23rd (UTC) has a spike at 9 PM UTC (= 2:30 AM IST on the 24th)
    d23 = int(datetime(2026, 11, 23, tzinfo=UTC).timestamp())
    cs = [k(d23 + i * 3600, 100, 101, 99, 100) for i in range(24)] + [k(d23 + 86400 + i * 3600, 100, 101, 99, 100) for i in range(24)]
    cs[21] = k(cs[21]["t"], 100, 150, 99, 100)
    t = int(datetime(2026, 11, 24, 12, 0, tzinfo=UTC).timestamp())
    utc = pv.levels_at(cs, t, "daily", "utc")
    ist = pv.levels_at(cs, t, "daily", "ist")
    assert utc["from"] == "2026-11-23" and utc["levels"]["P"] == pytest.approx((150 + 99 + 100) / 3, abs=0.01)
    # noon UTC = 5:30 PM IST on the 24th: the previous IST day (the 23rd) ended 6:30 PM UTC, before the spike
    assert ist["from"] == "2026-11-23" and ist["levels"]["P"] == pytest.approx(100)
    t2 = int(datetime(2026, 11, 24, 20, 0, tzinfo=UTC).timestamp())     # 1:30 AM IST on the 25th
    ist2 = pv.levels_at(cs, t2, "daily", "ist")
    assert ist2["from"] == "2026-11-24" and ist2["levels"]["P"] == pytest.approx((150 + 99 + 100) / 3, abs=0.01)


def test_weekly_from_daily_and_pattern_levels():
    mon = int(datetime(2026, 11, 16, tzinfo=UTC).timestamp())
    days = [k(mon + i * 86400, 100 + i, 110 + i, 90 + i, 105 + i) for i in range(14)]
    w = pv.levels_at(days, mon + 9 * 86400, "weekly", "utc")
    assert w["from"] == "2026-W47" and w["levels"]["P"] == pytest.approx((116 + 90 + 111) / 3, abs=0.01)
    assert pv.levels_at(days, mon + 3600, "weekly") is None            # no closed week before
    pl = pv.pattern_levels({"P": 1, "R1": 2, "R2": 3, "R3": 4, "R4": 5, "S1": 0, "S2": -1, "S3": -2, "S4": -3})
    assert [n for n, _ in pl["long"]] == ["S1", "S2", "S3"] and [n for n, _ in pl["short"]] == ["R1", "R2", "R3"]
    assert ("P", 1) in pv.pattern_levels({"P": 1, "S1": 0, "R1": 2}, use_p=True)["long"]
