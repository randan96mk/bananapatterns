"""Stage rules, feed lines and the universe scan on synthetic series."""

import json

from backend.config import BaseConfig, ScreenConfig
from backend.database.models import get_db, upsert_candles
from backend.engine.bases import detect_bases
from backend.engine.scan import change_line, run_scan
from backend.engine.stages import classify
from tests.test_bases import build

BCFG = BaseConfig(reset_ma=0)
SCFG = ScreenConfig(trend_ma=0)          # synthetic series are shorter than 200 sessions

UP = ("trend", 60, 0.60)
BASE = ("range", 25, (0.92, 0.99), 600_000)


def stage(candles, scfg=SCFG):
    return classify(candles, detect_bases(candles, BCFG), scfg)


def test_forming():
    r = stage(build([UP, BASE, ("bar", 1, 1.02)]))
    assert r.stage == "forming" and r.base_label.startswith("B1") and 0 <= r.dist_to_pivot_pct <= 5


def test_forming_too_far_or_too_young():
    far = build([UP, ("range", 25, (0.80, 0.99)), ("bar", 1, 0.85)])
    assert stage(far).stage is None and "too far" in stage(far).reason
    young = build([UP, ("range", 8, (0.95, 0.99))])
    assert stage(young).stage is None and "old" in stage(young).reason


def test_fresh_then_climbing():
    c = build([UP, BASE, ("bo", 1, 1.05, 3_000_000), ("bar", 1, 1.01)])
    r = stage(c)
    assert r.stage == "fresh" and r.sessions_since_breakout == 1 and r.volume_ok
    c2 = build([UP, BASE, ("bo", 1, 1.05, 3_000_000), ("trend", 12, 0.10)])
    assert stage(c2).stage == "climbing"


def test_low_volume_breakout():
    c = build([UP, BASE, ("bo", 1, 1.05, 300_000), ("bar", 1, 1.01)])
    assert stage(c).stage == "fresh_low_volume"
    assert stage(c, ScreenConfig(trend_ma=0, require_volume=False)).stage == "fresh"


def test_played_out_by_drop_and_by_ma():
    c = build([UP, BASE, ("bo", 1, 1.05, 3_000_000), ("bar", 1, 1.0), ("bar", 1, 0.85)])
    r = stage(c, ScreenConfig(trend_ma=0, played_out_ma=0))
    assert r.stage == "played_out" and "below the pivot" in r.ended_reason
    r2 = stage(c, ScreenConfig(trend_ma=0, played_out_ma=10))
    assert r2.stage == "played_out" and "10-day line" in r2.ended_reason


def test_played_out_window_expires():
    c = build([UP, BASE, ("bo", 1, 1.05, 3_000_000), ("bar", 1, 0.85), ("trend", 30, -0.05)])
    assert stage(c, ScreenConfig(trend_ma=0, played_out_ma=0, played_out_window=5)).stage != "played_out"


def test_below_pivot():
    c = build([UP, BASE, ("bo", 1, 1.05, 3_000_000), ("bar", 1, 0.93)])   # ~1.4% under the pivot
    r = stage(c, ScreenConfig(trend_ma=0, played_out_ma=0))
    assert r.stage == "below_pivot" and r.close < r.pivot


def test_climber_building_next_base_shows_forming():
    c = build([UP, BASE, ("bo", 1, 1.05, 3_000_000), ("trend", 10, 0.08), ("range", 25, (0.95, 0.99))])
    r = stage(c)
    assert r.stage == "forming" and r.base_number == 2


def test_gates():
    c = build([UP, BASE, ("bar", 1, 1.02)], vol=1_000)              # Rs ~0.01 cr turnover
    assert stage(c).stage is None and "illiquid" in stage(c).reason
    assert "history" in stage(build([UP, BASE]), ScreenConfig()).reason   # trend_ma 200 > bars


def test_change_lines():
    before = stage(build([UP, BASE]))
    after = stage(build([UP, BASE, ("bo", 1, 1.05, 3_000_000)]))
    kind, text = change_line("ABC", before, after)
    assert kind == "breakout" and text.startswith("ABC broke out of B1") and "pivot" in text
    assert change_line("ABC", after, after) is None
    po = stage(build([UP, BASE, ("bo", 1, 1.05, 3e6), ("bar", 1, 0.85)]), ScreenConfig(trend_ma=0, played_out_ma=0))
    assert change_line("ABC", after, po)[0] == "played_out"


def test_run_scan_stores_results_and_changes():
    series = {
        "FORM": (1, build([UP, BASE, ("bar", 1, 1.02)])),
        "FRESH": (2, build([UP, BASE, ("bo", 1, 1.05, 3_000_000)])),     # broke out on the last bar
        "NONE": (3, build([("trend", 86, -0.2)])),                       # same length -> same last date
    }
    with get_db() as conn:
        for tok, c in series.values():
            # align all series to end on the same date
            upsert_candles(conn, tok, c[-80:])
    syms = [{"symbol": k, "instrument_token": v[0]} for k, v in series.items()]
    out = run_scan(BCFG, SCFG, syms)
    assert out["scanned"] == 3 and out["stale"] == 0
    assert out["counts"]["fresh"] == 1 and out["counts"]["forming"] == 1
    with get_db() as conn:
        stages = dict(conn.execute("SELECT symbol, stage FROM scan_results WHERE as_of = ?", (out["as_of"],)).fetchall())
        changes = [dict(r) for r in conn.execute("SELECT * FROM scan_changes")]
    assert "NONE" not in stages
    with get_db() as conn:
        payloads = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM scan_results")]
    assert {p["symbol"] for p in payloads} == set(stages)          # rows carry their symbol
    assert any(c["symbol"] == "FRESH" and c["kind"] == "breakout" for c in changes)
