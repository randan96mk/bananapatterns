"""VCP metrics, qualification, the VCP stage classifier, scoring and the scan-table migration."""

import sqlite3
from datetime import date, timedelta

import numpy as np

from backend.config import BaseConfig, ScreenConfig, VcpConfig
from backend.engine.bases import detect_bases
from backend.engine.scoring import vcp_score
from backend.engine.stages import classify_vcp
from backend.engine.vcp import evaluate, swings

BCFG = BaseConfig(reset_ma=0)
SCFG = ScreenConfig(trend_ma=0)


def path(legs, start=100.0, d0=date(2024, 1, 1)):
    """legs: [(bars, target_price, volume)] moving linearly; returns weekday candles."""
    closes, vols, p = [], [], start
    for bars, target, vol in legs:
        for k in range(1, bars + 1):
            closes.append(p + (target - p) * k / bars)
            vols.append(vol)
        p = target
    out, d, prev = [], d0, closes[0]
    for c, v in zip(closes, vols):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({"date": d.isoformat(), "open": prev, "high": max(prev, c) * 1.003,
                    "low": min(prev, c) * 0.997, "close": c, "volume": v})
        prev = c
        d += timedelta(days=1)
    return out


def textbook(final_up=0.99):
    # strong advance, then pullbacks of ~20%, ~10%, ~5% on fading volume, back near the high
    return path([(80, 200, 2e6),
                 (12, 160, 1.2e6), (12, 198, 1.1e6),
                 (8, 178, 9e5), (8, 197, 8e5),
                 (5, 187, 6e5), (5, 200 * final_up, 5e5)])


def expanding():
    return path([(80, 200, 2e6),
                 (5, 190, 1.5e6), (5, 198, 1.5e6),
                 (8, 178, 1.8e6), (8, 197, 1.8e6),
                 (12, 160, 2.2e6), (12, 196, 2.4e6)])


def latest_base(c):
    return detect_bases(c, BCFG)[-1]


def test_textbook_vcp_qualifies():
    c = textbook()
    b = latest_base(c)
    v = evaluate(c, b, rs=90, trend_passed=8, cfg=VcpConfig())
    depths = [x["depth_pct"] for x in v.contractions]
    assert v.qualifies, v.fails
    assert len(depths) == 3 and depths[0] > depths[1] > depths[2]
    assert v.tightening < 1 and v.dryup < 0.85 and v.shrinking is True
    assert v.stop < b.pivot and v.stop_pct <= 8.0


def test_expanding_pullbacks_fail():
    c = expanding()
    v = evaluate(c, latest_base(c), rs=90, trend_passed=8, cfg=VcpConfig())
    assert not v.qualifies
    assert any("shrinking" in f or "last pullback" in f for f in v.fails)


def test_rs_trend_and_contraction_count_rules():
    c = textbook()
    b = latest_base(c)
    assert any("RS" in f for f in evaluate(c, b, 50, 8, VcpConfig()).fails)
    assert any("trend" in f for f in evaluate(c, b, 90, 3, VcpConfig()).fails)
    assert any("contractions >" in f for f in evaluate(c, b, 90, 8, VcpConfig(max_contractions=2)).fails)
    assert evaluate(c, b, None, 8, VcpConfig()).qualifies is False


def test_swings_are_causal():
    c = textbook()
    high = np.array([x["high"] for x in c])
    low = np.array([x["low"] for x in c])
    full, *_ = swings(high, low, 80, len(c) - 1, 0.05)
    for k in range(85, len(c)):
        part, *_ = swings(high, low, 80, k, 0.05)
        assert part == full[:len(part)]          # earlier pivots never change when bars are added


def test_classify_vcp_forming_and_breakout():
    c = textbook(final_up=0.97)
    res, v = classify_vcp(c, detect_bases(c, BCFG), 90, 8, SCFG, VcpConfig())
    assert res.stage == "forming" and v["qualifies"]
    bo = c + path([(1, 206, 4e6)], start=c[-1]["close"], d0=date.fromisoformat(c[-1]["date"]) + timedelta(days=1))
    res2, v2 = classify_vcp(bo, detect_bases(bo, BCFG), 90, 8, SCFG, VcpConfig())
    assert res2.stage == "fresh" and v2["eval_date"] == c[-1]["date"]   # judged on the bar before breakout
    res3, _ = classify_vcp(expanding(), detect_bases(expanding(), BCFG), 90, 8, SCFG, VcpConfig())
    assert res3.stage is None and res3.reason.startswith("not a VCP")


def test_score_orders_better_setups_higher():
    w = VcpConfig().score_weights
    good = vcp_score(w, 95, 0.6, 0.4, 2.0, -3.0, 1, 20)
    weak = vcp_score(w, 72, 1.0, 0.85, 20.0, -20.0, 18, 20)
    assert 0 <= weak < good <= 100


def test_old_scan_tables_are_migrated(tmp_db):
    from backend.database.models import init_db
    conn = sqlite3.connect(tmp_db)
    conn.executescript("DROP TABLE scan_results; CREATE TABLE scan_results (as_of TEXT, symbol TEXT, stage TEXT, "
                       "payload TEXT, PRIMARY KEY (as_of, symbol)); INSERT INTO scan_results VALUES ('d','X','fresh','{}');")
    conn.commit(); conn.close()
    init_db()
    cols = [r[1] for r in sqlite3.connect(tmp_db).execute("PRAGMA table_info(scan_results)")]
    assert "screen" in cols
