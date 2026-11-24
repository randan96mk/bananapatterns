"""₿ The 16 patterns (backend/crypto/patterns.py, pure): each with a positive and a negative fixture, the touch rule
with its tolerance, the EMA / volume / prior-trend / day filters, the stop / 1R arithmetic with the cap, the
confirmation rule, the cooldown and the confirmation wait through process()."""

import pytest

from backend.crypto import feed
from backend.crypto import patterns as pt
from backend.crypto import signals as sg
from tests.crypto_world import T_DAY, cfg, hammer_world, k, seed_daily, telegram

UP3 = [k(0, 90, 91, 89, 91), k(1, 91, 93, 90, 93), k(2, 93, 95, 92, 95)]       # a rise before the pattern
DOWN3 = [k(0, 110, 111, 104, 105), k(1, 105, 106, 102, 103), k(2, 103, 104, 100, 101)]   # a decline

# name -> (positive candles, negative candles, the candles before)
CASES = {
    "hammer": ([k(5, 99, 100, 90, 99.8)], [k(5, 95, 100, 90, 99.5)], DOWN3),
    "shooting_star": ([k(5, 100, 110, 99, 99.4)], [k(5, 100, 103, 99, 102.5)], UP3),
    "hanging_man": ([k(5, 99, 100, 90, 99.8)], [k(5, 95, 100, 90, 99.5)], UP3),
    "inverted_hammer": ([k(5, 99, 110, 98.8, 99.6)], [k(5, 99, 102, 98.8, 101.5)], DOWN3),
    "dragonfly_doji": ([k(5, 99.6, 100, 90, 99.7)], [k(5, 95, 100, 90, 96)], DOWN3),
    "gravestone_doji": ([k(5, 100.3, 110, 100, 100.2)], [k(5, 104, 110, 100, 105)], UP3),
    "bullish_engulfing": ([k(4, 100, 100.5, 97.5, 98), k(5, 97.8, 101.5, 97, 101)], [k(4, 100, 100.5, 97.5, 98), k(5, 97.8, 100, 97, 99.5)], DOWN3),
    "bearish_engulfing": ([k(4, 98, 100.5, 97.5, 100), k(5, 100.2, 101, 96.5, 97)], [k(4, 98, 100.5, 97.5, 100), k(5, 100.2, 101, 98.5, 99)], UP3),
    "piercing_line": ([k(4, 100, 100.5, 95.5, 96), k(5, 95, 99.5, 94.5, 99)], [k(4, 100, 100.5, 95.5, 96), k(5, 95, 98, 94.5, 97.5)], DOWN3),
    "dark_cloud_cover": ([k(4, 96, 100.5, 95.5, 100), k(5, 101, 101.5, 96.5, 97.5)], [k(4, 96, 100.5, 95.5, 100), k(5, 101, 101.5, 98.5, 99)], UP3),
    "morning_star": ([k(3, 100, 100.5, 95.5, 96), k(4, 95.5, 96, 94, 95.2), k(5, 95.5, 99.5, 95, 99)],
                     [k(3, 100, 100.5, 95.5, 96), k(4, 95.5, 96, 92.5, 93), k(5, 95.5, 99.5, 95, 99)], DOWN3),
    "evening_star": ([k(3, 96, 100.5, 95.5, 100), k(4, 100.2, 102, 100, 100.5), k(5, 100, 100.5, 96.5, 97)],
                     [k(3, 96, 100.5, 95.5, 100), k(4, 100.2, 102, 100, 100.5), k(5, 100, 101.5, 99.5, 101)], UP3),
    "bullish_harami": ([k(4, 100, 100.5, 94.5, 95), k(5, 96, 98.5, 95.5, 98)], [k(4, 100, 100.5, 94.5, 95), k(5, 96, 101.5, 95.5, 101)], DOWN3),
    "bearish_harami": ([k(4, 95, 100.5, 94.5, 100), k(5, 99, 99.5, 96.5, 97)], [k(4, 95, 100.5, 94.5, 100), k(5, 99, 99.5, 93.5, 94)], UP3),
    "tweezer_bottom": ([k(4, 100, 100.5, 95, 96), k(5, 96, 99, 95.05, 98.5)], [k(4, 100, 100.5, 95, 96), k(5, 96, 99, 94.5, 98.5)], DOWN3),
    "tweezer_top": ([k(4, 96, 100, 95.5, 99.5), k(5, 99.5, 100.05, 96, 96.5)], [k(4, 96, 100, 95.5, 99.5), k(5, 99.5, 100.05, 96, 99.8)], UP3),
}


def test_sixteen_patterns_exist():
    assert set(CASES) == set(pt.SIZE) and len(pt.BULLISH) == len(pt.BEARISH) == 8


@pytest.mark.parametrize("name", sorted(CASES))
def test_pattern_positive_and_negative(name):
    pos, neg, before = CASES[name]
    assert pt.shape(name, pos, pt.DEFAULTS, before), f"{name} should match"
    assert pt.shape(name, neg, pt.DEFAULTS, before) is None, f"{name} should not match"


def test_trend_shapes_need_their_trend():
    """The hanging man needs a rise before it, the inverted hammer a decline."""
    assert pt.shape("hanging_man", CASES["hanging_man"][0], pt.DEFAULTS, DOWN3) is None
    assert pt.shape("inverted_hammer", CASES["inverted_hammer"][0], pt.DEFAULTS, UP3) is None


def test_thresholds_are_inputs():
    b = [k(5, 99, 101.5, 93, 99.8)]                     # lower wick 6 = 70.6 % of range, 7.5× body
    assert pt.shape("hammer", b, pt.DEFAULTS)
    assert pt.shape("hammer", b, {**pt.DEFAULTS, "wick_min": 0.75}) is None
    assert pt.shape("hammer", b, {**pt.DEFAULTS, "wick_mult": 8}) is None
    tw = [k(4, 100, 100.5, 95, 96), k(5, 96, 99, 95.2, 98.5)]        # lows 0.2 apart: 0.1 % of 98.5 = 0.0985
    assert pt.shape("tweezer_bottom", tw, pt.DEFAULTS) is None
    assert pt.shape("tweezer_bottom", tw, {**pt.DEFAULTS, "eq_tol_pct": 0.25})
    ms = CASES["morning_star"][0]                       # middle body 0.3 = 7.5 % of the first
    assert pt.shape("morning_star", ms, {**pt.DEFAULTS, "star_body": 0.05}) is None


def test_touch_rule_with_tolerance():
    focus = [k(1, 99, 100, 90, 99.8)]
    assert pt.touch(focus, [("S1", 95)], 0, "long") == ("S1", 95)              # inside the range
    assert pt.touch(focus, [("S1", 85)], 2, "long") is None                   # 5 under the low, tolerance 2
    assert pt.touch(focus, [("S1", 85)], 5, "long") == ("S1", 85)             # within 5 points
    assert pt.touch(focus, [("S1", 104.9)], 5, "long") == ("S1", 104.9)       # over the high, within tolerance
    assert pt.touch(focus, [("S1", 97), ("S2", 91)], 0, "long") == ("S2", 91)  # nearest the low for a bullish pattern
    assert pt.touch(focus, [("R1", 97), ("R2", 91)], 0, "short") == ("R1", 97) # nearest the high for a bearish one


def test_stop_and_1r_with_the_cap():
    assert pt.stop_r1([k(1, 95, 100, 90, 99)], "long", 10, 50) == {"entry": 100, "stop": 80, "r1": 120, "capped": False}
    assert pt.stop_r1([k(1, 95, 100, 40, 99)], "long", 10, 50) == {"entry": 100, "stop": 50, "r1": 150, "capped": True}
    assert pt.stop_r1([k(1, 95, 100, 90, 92)], "short", 10, 50) == {"entry": 90, "stop": 110, "r1": 70, "capped": False}
    assert pt.stop_r1([k(1, 95, 160, 90, 92)], "short", 10, 50) == {"entry": 90, "stop": 140, "r1": 40, "capped": True}


def _series(close_last=99.8, vol_last=100.0, day_t=None):
    """20 candles falling 120 -> 101 then a hammer at 99.8 touching S1 90 (EMA 3 well above unless close_last is high)."""
    cs = [k(i * 300, 121 - i, 122 - i, 120 - i, 120 - i) for i in range(20)]
    o = min(close_last, 99)
    cs.append(k(day_t if day_t is not None else 20 * 300, o, close_last + 0.2, 90, close_last, vol_last))
    return cs


LV = {"long": [("S1", 90.0)], "short": []}
BASE = {"hammer": "alert", "ema_len": 3, "vol_len": 5, "vol_mult": 1.0, "ema_on": True, "vol_on": False, "trend_on": False,
        "days": list(pt.DAYS), "touch_points": 0.0}


def test_ema_filter():
    d = next(x for x in pt.detect(_series(), LV, BASE, names=("hammer",)))
    assert d["ema_ok"] is False and "close under EMA 3" in d["why"]
    d = next(x for x in pt.detect(_series(), LV, {**BASE, "ema_on": False}, names=("hammer",)))
    assert d["why"] is None and d["ema_ok"] is False                  # the filter off: shown, not enforced


def test_volume_filter():
    d = pt.detect(_series(vol_last=50), LV, {**BASE, "ema_on": False, "vol_on": True}, names=("hammer",))[0]
    assert d["vol_ok"] is False and "volume" in d["why"]
    d = pt.detect(_series(vol_last=300), LV, {**BASE, "ema_on": False, "vol_on": True}, names=("hammer",))[0]
    assert d["vol_ok"] is True and d["vol_x"] == pytest.approx(300 / ((4 * 100 + 300) / 5), abs=0.01) and d["why"] is None


def test_prior_trend_and_days_filters():
    d = pt.detect(_series(), LV, {**BASE, "ema_on": False, "trend_on": True}, names=("hammer",))[0]
    assert d["trend_ok"] is True and d["why"] is None                 # the hammer follows a decline
    day = pt.ist_day(20 * 300)                                         # 1 Jan 1970, a Thursday in IST
    d = pt.detect(_series(), LV, {**BASE, "ema_on": False, "days": [x for x in pt.DAYS if x != day]}, names=("hammer",))[0]
    assert d["day_ok"] is False and f"{day} is off" in d["why"]
    assert pt.detect(_series(), LV, {**BASE, "hammer": "off"}, names=("hammer",)) == []
    assert pt.detect(_series(), {"long": [("S1", 50.0)]}, BASE, names=("hammer",)) == []       # no level touches


def test_confirm_rule():
    det = {"pattern": "bullish_harami", "dir": "long", "candles": CASES["bullish_harami"][0]}
    assert pt.confirm(det, k(6, 98, 99.5, 97.5, 99))                  # green, above the harami high 98.5
    assert not pt.confirm(det, k(6, 98, 98.6, 97.5, 98.4))            # green but under the high
    inv = {"pattern": "inverted_hammer", "dir": "long", "candles": CASES["inverted_hammer"][0]}
    assert pt.confirm(inv, k(6, 99.6, 101, 99.5, 100.5)) and not pt.confirm(inv, k(6, 100, 100.2, 98, 98.5))
    hm = {"pattern": "hanging_man", "dir": "short", "candles": CASES["hanging_man"][0]}
    assert pt.confirm(hm, k(6, 99.8, 100, 97, 97.5)) and not pt.confirm(hm, k(6, 99, 101, 98.5, 100.5))


def test_ema_matches_tradingview_seed():
    assert pt.ema([1, 2, 3, 4], 3) == [None, None, 2.0, 3.0]
    assert pt.sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]


# --- through process(): the cooldown and the confirmation wait ----------------------------------------------------------

def test_cooldown_dedupes_per_level_and_direction(monkeypatch):
    telegram(monkeypatch)
    c = cfg(vol_len=20, dragonfly_doji="off", tweezer_bottom="off")
    cs = hammer_world()
    sg.process(c, T_DAY, now_ts=T_DAY + 303)
    second = [k(T_DAY + 300, 59100, 59110, 59090, 59100), k(T_DAY + 600, 59090, 59105, 58990, 59100, 130)]
    feed.upsert("BTCUSD", "5m", second)
    sg.process(c, T_DAY + 300, now_ts=T_DAY + 603)
    sg.process(c, T_DAY + 600, now_ts=T_DAY + 903)
    rs = sg.rows("pattern = 'hammer'", order="t")
    assert [r["state"] for r in rs] == ["sent", "dropped"] and rs[1]["why"].startswith("cooldown · hammer on S1 at 10:00 AM")
    later = T_DAY + 7 * 300                                            # 7 candles on: the cooldown (6) has passed
    feed.upsert("BTCUSD", "5m", [k(T_DAY + i * 300, 59100, 59110, 59090, 59100) for i in range(3, 7)] + [k(later, 59090, 59105, 58990, 59100, 130)])
    sg.process(c, later, now_ts=later + 303)
    assert sg.rows("pattern = 'hammer'", order="t")[-1]["state"] == "sent"
    assert len(cs) == 30


def test_confirmation_wait_then_sent_or_dropped(monkeypatch):
    tg = telegram(monkeypatch)
    c = cfg(bullish_harami="alert", hammer="off", dragonfly_doji="off", tweezer_bottom="off", ema_on=False, vol_len=20)
    seed_daily()
    base = [k(T_DAY - (30 - i) * 300, 59300, 59310, 59290, 59300) for i in range(29)]
    harami = [k(T_DAY - 300, 59300, 59310, 58990, 59000), k(T_DAY, 59050, 59200, 59040, 59150)]     # prior low on S1 59,000
    feed.upsert("BTCUSD", "5m", base + harami)
    sg.process(c, T_DAY, now_ts=T_DAY + 303)
    r = sg.rows("pattern = 'bullish_harami'")[0]
    assert r["state"] == "confirm_wait" and not tg.sent
    feed.upsert("BTCUSD", "5m", [k(T_DAY + 300, 59150, 59260, 59140, 59250)])                       # green, above the harami high
    sg.process(c, T_DAY + 300, now_ts=T_DAY + 603)
    r = sg.load(r["id"])
    assert r["state"] == "sent" and r["sent_at"] and "confirmed 10:05 AM" in tg.sent[-1][1]
    # a second harami that the next candle fails
    t2 = T_DAY + 3600
    feed.upsert("BTCUSD", "5m", [k(T_DAY + i * 300, 59300, 59310, 59290, 59300) for i in range(2, 11)]
                + [k(t2 - 300, 59300, 59310, 58990, 59000), k(t2, 59050, 59200, 59040, 59150), k(t2 + 300, 59150, 59160, 59000, 59010)])
    for t in range(T_DAY + 600, t2 + 301, 300):
        sg.process(c, t, now_ts=t + 303)
    r2 = sg.rows("pattern = 'bullish_harami'", order="t")[-1]
    assert r2["t"] == t2 and r2["state"] == "dropped" and r2["why"].startswith("not confirmed")
