"""RS rating, industry ranks and the Trend Template."""

import json

import pytest

from backend.config import BaseConfig, ScreenConfig, TrendConfig
from backend.database.models import get_db, upsert_candles
from backend.engine.rs import industry_ranks, rs_ratings, rs_score
from backend.engine.scan import run_scan
from backend.engine.trend_template import trend_template
from tests.test_bases import build

CFG = TrendConfig()


def test_rs_score_weights_and_short_history():
    closes = [100.0] * 300 + [200.0]                 # doubled on the last bar: every ROC = +100%
    assert rs_score(closes, CFG) == pytest.approx(1.0)
    young = [100.0] * 100 + [150.0]                  # only the 63-session period exists
    assert rs_score(young, CFG) == pytest.approx(0.5)
    assert rs_score([100.0] * 50, CFG) is None       # under rs_min_bars


def test_rs_ratings_percentiles_and_ties():
    r = rs_ratings({"a": -0.5, "b": 0.0, "c": 0.0, "d": 1.0, "e": None})
    assert r["a"] == 1 and r["d"] == 99 and r["e"] is None
    assert r["b"] == r["c"] == 50                    # tied in the middle
    assert rs_ratings({"x": 0.3}) == {"x": 99}


def test_industry_ranks_by_median():
    ranks = industry_ranks({"a": 90, "b": 80, "c": 20, "d": None}, {"a": "IT", "b": "IT", "c": "Banks", "d": "IT"})
    assert ranks["IT"]["rank"] == 1 and ranks["IT"]["median_rs"] == 85 and ranks["IT"]["stocks"] == 2
    assert ranks["Banks"] == {"rank": 2, "of": 2, "median_rs": 20, "stocks": 1}


def test_trend_template_uptrend_passes_downtrend_fails():
    up = build([("trend", 300, 1.5)])
    tt = trend_template(up, 90, CFG)
    assert tt["passed_all"] and tt["passed"] == 8
    down = build([("trend", 300, -0.6)])
    td = trend_template(down, 10, CFG)
    assert td["passed"] == 0 and not td["passed_all"]
    keys = [c["key"] for c in tt["checks"]]
    assert keys == ["above_mid_slow", "mid_above_slow", "slow_rising", "fast_above", "above_fast",
                    "above_low52", "near_high52", "rs"]


def test_trend_template_short_history_fails_ma_checks():
    tt = trend_template(build([("trend", 120, 0.5)]), 95, CFG)
    by = {c["key"]: c["pass"] for c in tt["checks"]}
    assert not by["above_mid_slow"] and not by["slow_rising"] and by["rs"]


def test_scan_stores_metrics_for_every_stock():
    series = {"UP": (1, build([("trend", 300, 1.5)])), "DOWN": (2, build([("trend", 300, -0.5)])),
              "FLAT": (3, build([("trend", 300, 0.0)]))}
    with get_db() as conn:
        for tok, c in series.values():
            upsert_candles(conn, tok, c)
    syms = [{"symbol": k, "instrument_token": v[0], "industry": "IT" if k != "FLAT" else "Banks"}
            for k, v in series.items()]
    run_scan(BaseConfig(reset_ma=0), ScreenConfig(trend_ma=0), syms, CFG)
    with get_db() as conn:
        rows = {r["symbol"]: dict(r) for r in conn.execute("SELECT * FROM stock_metrics")}
    assert set(rows) == {"UP", "DOWN", "FLAT"}
    assert rows["UP"]["rs"] == 99 and rows["DOWN"]["rs"] == 1 and rows["UP"]["tt_passed"] == 8
    assert json.loads(rows["UP"]["payload"])["trend"]["passed_all"] is True
    assert rows["FLAT"]["industry_rank"] in (1, 2) and rows["UP"]["industry_of"] == 2
