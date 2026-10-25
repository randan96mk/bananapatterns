"""Bull flag: detection rules, tags, breakout, stages, scan screen, alerts, guard stop, decision chart, API."""

import json
from datetime import date, timedelta

import pytest

from backend.config import AlertsConfig, BaseConfig, CupConfig, FlagConfig, ScreenConfig, VcpConfig
from backend.database.models import get_db, upsert_candles
from backend.engine.flags import detect_flags, flag_score
from backend.engine.scan import run_scan
from backend.engine.stages import classify_flag


def flag_series(gain=0.36, pole_bars=12, flag_bars=8, depth=0.08, flag_vol=0.4, rise=0.0, breakout=False,
                trend=120, pole_vol=2.5):
    """A gentle uptrend -> a straight pole (+gain in pole_bars, pole_vol x volume) -> a flag that drifts down to
    `depth` under the top (rise > 0 tilts it upward instead) on flag_vol x the pole's volume -> optional breakout."""
    closes = [100 * (1 + 0.15 * i / (trend - 1)) for i in range(trend)]
    base = closes[-1]
    top = base * (1 + gain)
    closes += [base + (top - base) * (i + 1) / pole_bars for i in range(pole_bars)]
    vols = [1e6] * trend + [1e6 * pole_vol] * pole_bars
    if rise > 0:
        closes += [top * 0.97 * (1 + rise * (i + 1) / flag_bars) for i in range(flag_bars)]
    else:
        low = top * (1 - depth) / 0.997 * 1.003
        closes += [top - (top - low) * (i + 1) / flag_bars for i in range(flag_bars)]
    vols += [1e6 * pole_vol * flag_vol] * flag_bars
    if breakout:
        closes.append(top * 1.04)
        vols.append(4e6)
    out, d, prev = [], date(2025, 1, 1), closes[0]
    for c, v in zip(closes, vols):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({"date": d.isoformat(), "open": prev, "high": max(prev, c) * 1.003, "low": min(prev, c) * 0.997,
                    "close": c, "volume": v})
        prev = c
        d += timedelta(days=1)
    return out


CFG, BCFG = FlagConfig(), BaseConfig()


def test_forming_flag():
    fl = detect_flags(flag_series(), CFG, BCFG)
    assert len(fl) == 1
    f = fl[0]
    assert f.kind == "flag" and f.status == "forming" and f.label == "Flag · 8d"
    sh = f.shape
    assert 33 <= sh["pole"]["gain_pct"] <= 40 and 10 <= sh["pole"]["bars"] <= 13 and sh["pole"]["volume_x"] >= 2
    assert sh["flag"]["bars"] == 8 and 7 <= sh["flag"]["depth_pct"] <= 9 and sh["flag"]["volume_x"] == pytest.approx(0.4)
    assert f.pivot == sh["pole"]["high"] and sh["stop"] == round(sh["flag"]["low"] * 0.99, 2)
    (t0, y0), (t1, y1) = sh["flag"]["top_line"]
    (_, b0), (_, b1) = sh["flag"]["bot_line"]
    assert y1 < y0 and b1 < b0 and y0 - b0 == pytest.approx(y1 - b1, abs=0.02)        # parallel, drifting down
    assert not sh["tight"] and not sh["htf"]                             # 8% of a 36% pole: 30% given back
    assert detect_flags(flag_series(depth=0.04), CFG, BCFG)[0].shape["tight"]


def test_breakout_and_htf():
    fl = detect_flags(flag_series(breakout=True), CFG, BCFG)
    assert len(fl) == 1 and fl[0].status == "broken_out" and fl[0].breakout.volume_ok
    h = detect_flags(flag_series(gain=1.1, pole_bars=25), CFG, BCFG)[0].shape
    assert h["htf"] and h["pole"]["gain_pct"] > 90


@pytest.mark.parametrize("kw", [
    {"gain": 0.12},                         # pole too small
    {"depth": 0.30},                        # flag too deep
    {"gain": 0.22, "depth": 0.12},          # gives back more than 38% of the pole
    {"flag_vol": 1.0},                      # flag volume not drying up
    {"pole_vol": 1.0},                      # pole without volume
    {"rise": 0.06},                         # rising "flag": a continuation, not a pause
    {"flag_bars": 3},                       # flag too short
    {"flag_bars": 30},                      # flag too long
])
def test_rejections(kw):
    assert detect_flags(flag_series(**kw), CFG, BCFG) == []


def test_stages_and_rs_gate():
    scfg = ScreenConfig(trend_ma=0, min_turnover_cr=0)
    forming = flag_series(depth=0.04)                                     # within 6% of the flag high
    res, shape = classify_flag(forming, detect_flags(forming, CFG, BCFG), 90, 8, scfg, CFG)
    assert res.stage == "forming" and res.base_label == "Flag · 8d" and shape["flag"]["bars"] == 8
    broke = flag_series(breakout=True)
    res, _ = classify_flag(broke, detect_flags(broke, CFG, BCFG), 90, 8, scfg, CFG)
    assert res.stage == "fresh"
    res, _ = classify_flag(broke, detect_flags(broke, CFG, BCFG), 70, 8, scfg, CFG)
    assert res.stage is None and "RS 70 < 80" in res.reason


def test_score_prefers_tight_quiet_strong_flags():
    a = detect_flags(flag_series(gain=0.6, depth=0.05, flag_vol=0.3), CFG, BCFG)[0].shape
    b = detect_flags(flag_series(gain=0.22, depth=0.055, flag_vol=0.75), CFG, BCFG)[0].shape
    assert flag_score(a, 90, CFG) > flag_score(b, 90, CFG)


def scan_with(series: dict, results: dict | None = None):
    with get_db() as conn:
        for tok, (_, cs) in enumerate(series.items(), start=1):
            upsert_candles(conn, tok, cs)
        for sym, d in (results or {}).items():
            conn.execute("INSERT INTO corp_events (symbol, date, kind, purpose, descr, fetched_at) "
                         "VALUES (?, ?, 'results', 'Financial Results', '', 'x')", (sym, d))
    syms = [{"symbol": s, "instrument_token": i} for i, s in enumerate(series, start=1)]
    return run_scan(BaseConfig(reset_ma=0), ScreenConfig(trend_ma=0, min_turnover_cr=0), syms,
                    vcp_cfg=VcpConfig(min_rs=0, min_trend_checks=0), cup_cfg=CupConfig(min_rs=0, min_trend_checks=0),
                    flag_cfg=FlagConfig(min_rs=0, min_trend_checks=0))


def test_scan_screen_tags_and_post_results():
    s = flag_series(depth=0.04)
    pole_start = s[120]["date"]
    out = scan_with({"FLGX": s, "NOPE": flag_series(depth=0.04, gain=0.1)}, results={"FLGX": pole_start})
    assert out["counts"]["flag"]["forming"] == 1
    with get_db() as conn:
        p = json.loads(conn.execute("SELECT payload FROM scan_results WHERE screen = 'flag' AND symbol = 'FLGX'")
                       .fetchone()["payload"])
        assert p["flag"]["post_results"] is True and p["score"] > 0 and p["base_label"] == "Flag · 8d"
        b = conn.execute("SELECT payload FROM scan_results WHERE screen = 'bases' AND symbol = 'FLGX'").fetchone()
        assert b is None or json.loads(b["payload"]).get("flag_tag") is True


def test_alerts_guard_and_preference():
    from backend.alerts import guard
    from backend.alerts.dispatch import Notifier, dispatch_scan
    from backend.alerts.messages import setup_stop
    from tests.test_alerts import FakeTG
    out = scan_with({"FLGX": flag_series(depth=0.025)})
    with get_db() as conn:
        p = json.loads(conn.execute("SELECT payload FROM scan_results WHERE screen = 'flag'").fetchone()["payload"])
    assert setup_stop(p) == p["flag"]["stop"]
    cfg = AlertsConfig(chat_id="42", tiering=False, screens=["bases", "flag"], digest=False, near_pivot_pct=0.05)
    fake = FakeTG()
    dispatch_scan(out["as_of"], cfg, BaseConfig(reset_ma=0), Notifier(cfg, fake), {"FLGX": 1})
    near = [h for _, h in fake.sent if "FLGX" in h and "under its" in h]
    assert len(near) == 1 and "Bull flag" in near[0] and "Pole +" in near[0] and "tight" in near[0]
    pos = guard.add_manual("FLGX", 140.0, VcpConfig())
    assert pos["stop_source"] == "setup (flag low)" and pos["stop"] == p["flag"]["stop"]


def test_decision_chart_for_a_flag():
    from backend.alerts.decision import decision_context, decision_lines, render_decision_chart
    cs = flag_series()
    shape = detect_flags(cs, CFG, BCFG)[0].shape
    ctx = decision_context(cs, "forming", symbol="FLGX", rs=90, tt_passed=8, bases_cfg=BCFG, vcp_cfg=VcpConfig(),
                           alerts_cfg=AlertsConfig(), flag=shape)
    assert ctx["verdict"] == "Bull flag forming ✓" and ctx["stop"] == shape["stop"]
    assert any(t.startswith("Pole +") for _, t in ctx["checks"]) and "flag low" in " ".join(ctx["watch"])
    assert "Flags are fast" in ctx["watch"][-1] and decision_lines(ctx)[0].startswith("Verdict: Bull flag")
    assert render_decision_chart(cs, ctx)[:4] == b"\x89PNG"
    bo = flag_series(breakout=True)
    ctx = decision_context(bo, "breakout", symbol="FLGX", rs=90, tt_passed=8, bases_cfg=BCFG, vcp_cfg=VcpConfig(),
                           alerts_cfg=AlertsConfig(), flag=detect_flags(bo, CFG, BCFG)[0].shape)
    assert ctx["verdict"] == "Breakout ✓ · bull flag" and ctx["base"].breakout.volume_ok
    h = flag_series(gain=1.1, pole_bars=25)
    ctx = decision_context(h, "forming", symbol="HTF", rs=95, tt_passed=8, bases_cfg=BCFG, vcp_cfg=VcpConfig(),
                           alerts_cfg=AlertsConfig(), flag=detect_flags(h, CFG, BCFG)[0].shape)
    assert ctx["verdict"] == "High-tight flag forming ✓"


def test_api_and_settings(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    scan_with({"FLGX": flag_series(depth=0.04)})
    monkeypatch.setattr("backend.ingest.universe.resolve_universe",
                        lambda cfg: {"stocks": [{"symbol": "FLGX", "instrument_token": 1}], "indices": []})
    from backend import main
    with TestClient(main.app) as tc:
        assert "flag" in [s["id"] for s in tc.get("/api/screens").json()]
        assert tc.get("/api/screens/flag/summary").json()["counts"]["forming"] == 1
        row = tc.get("/api/screens/flag/results?stage=forming").json()["rows"][0]
        assert row["symbol"] == "FLGX" and row["flag"]["pole"]["gain_pct"] > 30
        s = tc.get("/api/screens/flag/sparks?stage=forming&bars=20").json()["sparks"]["FLGX"]
        assert s["d"][0] < row["flag"]["pole"]["start"]                    # the card shows the whole pole
        assert tc.get("/api/config/flag").json()["min_pole_gain"] == 0.2
        assert tc.put("/api/config/flag", json={"max_retrace": 0.5}).json()["max_retrace"] == 0.5
        assert "flag" in tc.get("/api/config/alerts").json()["screens"]
