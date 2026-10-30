"""Decision charts: context per alert kind, rendering, message lines, dispatch + intraday wiring, preview."""


import pytest

from backend.alerts import charts
from backend.alerts import messages as msg
from backend.alerts.decision import decision_context, decision_lines, render_decision_chart
from backend.alerts.dispatch import Notifier, decision_for, dispatch_scan
from backend.alerts.samples import _example_candles
from backend.config import AlertsConfig, BaseConfig, VcpConfig
from tests.test_alerts import FakeTG, scanned_db

PNG = b"\x89PNG\r\n\x1a\n"
P = {"symbol": "EX", "company": "Ex & Co", "industry": "IT", "rs": 90, "tt_passed": 8, "tt_of": 8,
     "ended_reason": "closed below its 50-day line", "gain_from_pivot_pct": 4.0, "peak_gain_pct": 12.0}


def ctx_for(kind, candles=None, live=None, **alerts):
    cs = candles if candles is not None else _example_candles()
    return decision_context(cs, kind, symbol="EX", company="Ex & Co", industry="IT", rs=90, tt_passed=8, tt_of=8,
                            bases_cfg=BaseConfig(), vcp_cfg=VcpConfig(), alerts_cfg=AlertsConfig(**alerts), live=live)


@pytest.mark.parametrize("kind", ["breakout", "breakout_low_volume", "below_pivot", "played_out", "climbing"])
def test_breakout_kinds_use_the_broken_base(kind):
    c = ctx_for(kind)
    assert c and c["kind"] == kind and c["pivot"] == pytest.approx(100.0, rel=0.02)
    assert c["stop"] < c["pivot"] < c["zone_top"] == pytest.approx(c["pivot"] * 1.05)
    assert c["checks"] and c["verdict"] and c["watch"]


@pytest.mark.parametrize("kind", ["forming", "near_pivot"])
def test_forming_kinds_use_the_open_base(kind):
    c = ctx_for(kind, _example_candles()[:-1])
    assert c and c["close"] <= c["pivot"] and c["stop"] < c["close"]


def test_intraday_uses_live_price():
    cs = _example_candles()[:-1]
    live = {"ltp": 101.0, "open": 99.5, "high": 101.4, "low": 99.2, "volume": 1_500_000}
    c = ctx_for("intraday", cs, live=live)
    assert c["live"] == live and c["close"] == 101.0


def test_position_size_off_by_default_and_on_with_capital():
    assert ctx_for("breakout")["size"] is None
    c = ctx_for("breakout", capital=500_000, risk_pct=0.01)
    s = c["size"]
    assert s["qty"] > 0 and s["qty"] * (s["entry"] - c["stop"]) <= 5_000 + 1e-6
    assert any(line.startswith("Size:") for line in decision_lines(c))


def test_lines_and_message_escape():
    c = ctx_for("breakout")
    lines = decision_lines(c)
    assert lines[0].startswith("Verdict:") and any(x.startswith("Levels:") for x in lines)
    assert any(x.startswith("Watch:") for x in lines)
    html = msg.stage_alert("breakout", "vcp", "EX broke out", dict(P, base_label="B1 · 7w", close=103.0,
                                                                  pivot=100.0), 0.8, c)
    assert "Verdict:" in html and "Levels:" in html


@pytest.mark.parametrize("kind", ["breakout", "forming", "played_out", "intraday"])
def test_render_each_kind(kind):
    cs = _example_candles() if kind in ("breakout", "played_out") else _example_candles()[:-1]
    live = {"ltp": 101.0, "open": 99.5, "high": 101.4, "low": 99.2, "volume": 1e6} if kind == "intraday" else None
    c = ctx_for(kind, cs, live=live)
    png = render_decision_chart(cs, c)
    assert png[:8] == PNG and len(png) > 30_000


def test_decision_for_respects_settings(monkeypatch):
    cs = _example_candles()
    png, ctx = decision_for(cs, P, "breakout", AlertsConfig(), BaseConfig(), VcpConfig())
    assert png[:8] == PNG and ctx
    png, ctx = decision_for(cs, P, "breakout", AlertsConfig(chart_kinds=["forming"]), BaseConfig(), VcpConfig())
    assert png is None and ctx                                  # text lines still come through
    png, _ = decision_for(cs, P, "breakout", AlertsConfig(chart_images=False), BaseConfig(), VcpConfig(),
                          force_image=True)
    assert png[:8] == PNG                                       # dashboard preview ignores the toggles
    monkeypatch.setattr(charts, "CHARTS_AVAILABLE", False)
    png, ctx = decision_for(cs, P, "breakout", AlertsConfig(), BaseConfig(), VcpConfig())
    assert png is None and ctx                                  # no matplotlib: text only


def test_decision_for_never_raises():
    assert decision_for([], P, "breakout", AlertsConfig(), BaseConfig(), VcpConfig()) == (None, None)


def test_dispatch_sends_chart_and_decision_lines():
    as_of, tokens = scanned_db()
    cfg = AlertsConfig(chat_id="42", tiering=False, screens=["bases"], near_pivot_pct=0.05)
    fake = FakeTG()
    dispatch_scan(as_of, cfg, BaseConfig(reset_ma=0), Notifier(cfg, fake), tokens)
    bo = [(k, h) for k, h in fake.sent if "broke out" in h]
    assert bo and bo[0][0] == "photo" and "Verdict:" in bo[0][1] and "Levels:" in bo[0][1]
