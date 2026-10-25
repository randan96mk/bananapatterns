"""Cup with handle: detection rules, breakout, stages, scan screen, decision chart, API."""

import json
from datetime import date, timedelta

import pytest

from backend.config import AlertsConfig, BaseConfig, CupConfig, ScreenConfig, VcpConfig
from backend.database.models import get_db, upsert_candles
from backend.engine.cups import cup_score, detect_cups
from backend.engine.scan import run_scan
from backend.engine.stages import classify_cup


def cup_series(depth=0.25, flat=18, side=21, handle=8, handle_depth=0.06, handle_vol=0.5, advance=0.45,
               right_lip=0.99, breakout=True, prior=90, spike=False):
    """Prior rise -> U cup (left side, flat bottom, right side) -> handle -> optional breakout bar.
    spike: the cup drifts to half its depth and only touches the bottom in a 4-bar spike (a V bottom)."""
    top = 100 * (1 + advance)
    closes = [100 + (top - 100) * i / (prior - 1) for i in range(prior)]
    bottom = top * (1 - depth)
    rl = top * right_lip
    if spike:
        mid = top * (1 - depth / 2)
        closes += [top - (top - mid) * (i + 1) / side for i in range(side)]
        closes += [mid * 0.995] * flat + [(mid + bottom) / 2, bottom, (mid + bottom) / 2] + [mid * 0.995] * flat
        closes += [mid + (rl - mid) * (i + 1) / side for i in range(side)]
    else:
        closes += [top - (top - bottom) * (i + 1) / side for i in range(side)]        # left side down
        closes += [bottom * (1 + 0.01 * ((i % 3) - 1)) for i in range(flat)]         # rounded / flat bottom
        closes += [bottom + (rl - bottom) * (i + 1) / side for i in range(side)]     # right side up
    h_low = rl * (1 - handle_depth)
    half = max(1, handle // 2)
    closes += [rl - (rl - h_low) * (i + 1) / half for i in range(half)]              # handle drifts down ...
    closes += [h_low * 1.005] * (handle - half)                                      # ... then sideways
    vols = [1e6] * (len(closes) - handle) + [1e6 * handle_vol] * handle
    if breakout:
        closes.append(rl * 1.03)
        vols.append(3e6)
    out, d, prev = [], date(2024, 1, 1), closes[0]
    for c, v in zip(closes, vols):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({"date": d.isoformat(), "open": prev, "high": max(prev, c) * 1.003,
                    "low": min(prev, c) * 0.997, "close": c, "volume": v})
        prev = c
        d += timedelta(days=1)
    return out


CFG, BCFG = CupConfig(), BaseConfig()


def test_forming_cup_found_in_the_handle():
    cups = detect_cups(cup_series(breakout=False), CFG, BCFG)
    assert len(cups) == 1
    c = cups[0]
    assert c.kind == "cup" and c.status == "forming" and c.label.startswith("C&H · ")
    sh = c.shape
    assert 24 <= sh["cup"]["depth_pct"] <= 27 and sh["handle"]["bars"] >= 5 and 5 <= sh["handle"]["depth_pct"] <= 7
    assert sh["handle"]["volume_x"] == pytest.approx(0.5, abs=0.05) and sh["prior_advance_pct"] >= 40
    assert c.pivot == sh["right_lip"]["high"] and 0 < c.dist_to_pivot_pct < 7
    assert sh["handle"]["low"] * 0.99 == pytest.approx(sh["stop"], rel=1e-3) or sh["stop"] == round(c.pivot * 0.92, 2)


def test_breakout_closes_the_cup():
    cups = detect_cups(cup_series(), CFG, BCFG)
    assert len(cups) == 1 and cups[0].status == "broken_out"
    bo = cups[0].breakout
    assert bo.volume_ok and bo.through_pct > 0 and bo.date == cup_series()[-1]["date"]


@pytest.mark.parametrize("kw", [
    {"spike": True},                            # spike bottom: only touches the low for a day or two
    {"depth": 0.45},                            # too deep
    {"depth": 0.08},                            # too shallow
    {"handle_depth": 0.16},                     # handle too deep
    {"handle_vol": 1.6},                        # handle volume not drying up
    {"advance": 0.10},                          # no prior uptrend
    {"right_lip": 0.85},                        # right side never got back near the left lip
    {"handle": 3},                              # handle too short
    {"side": 6, "flat": 4},                     # cup too short (< 7 weeks)
])
def test_rejections(kw):
    assert detect_cups(cup_series(breakout=False, **kw), CFG, BCFG) == []


def test_handle_must_sit_in_the_upper_half():
    s = cup_series(breakout=False, depth=0.18, handle_depth=0.105)    # handle low under the cup midpoint
    assert detect_cups(s, CFG, BCFG) == []
    assert detect_cups(s, CupConfig(handle_upper_half=False), BCFG) != []


def test_stages_on_the_cup_screen():
    scfg = ScreenConfig(trend_ma=0, min_turnover_cr=0)
    forming = cup_series(breakout=False, handle_depth=0.04)          # within 5% of the pivot
    res, shape = classify_cup(forming, detect_cups(forming, CFG, BCFG), 90, 8, scfg, CFG)
    assert res.stage == "forming" and shape["handle"]["bars"] >= 5 and res.base_label.startswith("C&H")
    broke = cup_series()
    res, _ = classify_cup(broke, detect_cups(broke, CFG, BCFG), 90, 8, scfg, CFG)
    assert res.stage == "fresh"
    res, _ = classify_cup(broke, detect_cups(broke, CFG, BCFG), 40, 8, scfg, CFG)
    assert res.stage is None and "RS 40" in res.reason


def test_score_prefers_tight_quiet_handles():
    a = detect_cups(cup_series(breakout=False, handle_depth=0.04, handle_vol=0.4), CFG, BCFG)[0].shape
    b = detect_cups(cup_series(breakout=False, handle_depth=0.11, handle_vol=0.95), CFG, BCFG)[0].shape
    assert cup_score(a, 90, CFG) > cup_score(b, 90, CFG)


def scan_with(series: dict):
    with get_db() as conn:
        for tok, (_, cs) in enumerate(series.items(), start=1):
            upsert_candles(conn, tok, cs)
    syms = [{"symbol": s, "instrument_token": i} for i, s in enumerate(series, start=1)]
    return run_scan(BaseConfig(reset_ma=0), ScreenConfig(trend_ma=0, min_turnover_cr=0), syms,
                    vcp_cfg=VcpConfig(min_rs=0, min_trend_checks=0), cup_cfg=CupConfig(min_rs=0, min_trend_checks=0))


def test_scan_fills_the_cup_screen_and_tags_bases():
    out = scan_with({"CUPX": cup_series(breakout=False, handle_depth=0.04),
                     "FLAT": cup_series(breakout=False, depth=0.45)})
    assert out["counts"]["cup"]["forming"] == 1
    with get_db() as conn:
        row = conn.execute("SELECT payload FROM scan_results WHERE screen = 'cup' AND symbol = 'CUPX'").fetchone()
        p = json.loads(row["payload"])
        assert p["cup"]["handle"]["bars"] >= 5 and p["score"] > 0 and p["base_label"].startswith("C&H")
        b = conn.execute("SELECT payload FROM scan_results WHERE screen = 'bases' AND symbol = 'CUPX'").fetchone()
        assert b is None or json.loads(b["payload"]).get("cup_tag") is True


def test_decision_chart_for_a_cup():
    from backend.alerts.decision import decision_context, decision_lines, render_decision_chart
    cs = cup_series(breakout=False)
    shape = detect_cups(cs, CFG, BCFG)[0].shape
    ctx = decision_context(cs, "forming", symbol="CUPX", rs=90, tt_passed=8, bases_cfg=BCFG, vcp_cfg=VcpConfig(),
                           alerts_cfg=AlertsConfig(), cup=shape)
    assert ctx["verdict"] == "Cup & handle forming ✓" and ctx["stop"] == shape["stop"]
    assert any("Handle" in t for _, t in ctx["checks"]) and "handle low" in " ".join(ctx["watch"])
    assert decision_lines(ctx)[0].startswith("Verdict: Cup & handle")
    png = render_decision_chart(cs, ctx)
    assert png[:4] == b"\x89PNG"
    bo = cup_series()
    ctx = decision_context(bo, "breakout", symbol="CUPX", rs=90, tt_passed=8, bases_cfg=BCFG, vcp_cfg=VcpConfig(),
                           alerts_cfg=AlertsConfig(), cup=detect_cups(bo, CFG, BCFG)[0].shape)
    assert ctx["verdict"] == "Breakout ✓ · cup & handle" and ctx["base"].breakout.volume_ok


def test_api_lists_the_cup_screen(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    scan_with({"CUPX": cup_series(breakout=False, handle_depth=0.04)})
    from backend import main
    with TestClient(main.app) as tc:
        assert "cup" in [s["id"] for s in tc.get("/api/screens").json()]
        assert tc.get("/api/screens/cup/summary").json()["counts"]["forming"] == 1
        rows = tc.get("/api/screens/cup/results?stage=forming").json()["rows"]
        assert rows[0]["symbol"] == "CUPX" and rows[0]["tier"] in ("A", "B")
        assert tc.get("/api/config/cup").json()["min_depth"] == 0.12
        assert tc.put("/api/config/cup", json={"max_depth": 0.4}).json()["max_depth"] == 0.4
