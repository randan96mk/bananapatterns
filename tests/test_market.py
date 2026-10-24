"""Market health signals and status, A/B tiers, alert gating / decoration, caption splitting, sizing."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from backend.alerts import messages as msg
from backend.alerts.dispatch import Notifier, dispatch_scan, split_caption
from backend.alerts.priority import MarketContext, decorate, gate
from backend.alerts.samples import _example_candles
from backend.config import AlertsConfig, AppConfig, BaseConfig, MarketConfig, ScreenConfig, VcpConfig
from backend.database.models import get_db
from backend.engine import market
from backend.engine.scan import run_scan
from tests.test_alerts import FakeTG, scanned_db


def bars(closes, vols=None, start=date(2025, 1, 1)):
    out, d = [], start
    for i, c in enumerate(closes):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({"date": d.isoformat(), "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                    "volume": (vols[i] if vols else 1000)})
        d += timedelta(days=1)
    return out


# --- signals ------------------------------------------------------------------------

def test_index_signals():
    up = market.index_signals(bars([100 + i for i in range(260)]))
    assert up["above_50"] and up["above_200"] and up["ma50_rising"] and up["change_pct"] > 0
    down = market.index_signals(bars([400 - i for i in range(260)]))
    assert down["above_50"] is False and down["above_200"] is False and down["ma50_rising"] is False
    short = market.index_signals(bars([100 + i for i in range(30)]))
    assert short["above_50"] is None and short["above_200"] is None
    assert market.index_signals([])["close"] is None


def test_breadth_and_new_highs():
    rising, falling = bars([100 + i for i in range(260)]), bars([400 - i for i in range(260)])
    as_of = rising[-1]["date"]
    b = market.breadth({"A": rising, "B": rising, "C": rising, "D": falling, "OLD": rising[:-5]}, as_of)
    assert b["above_50_pct"] == 75.0 and b["above_200_pct"] == 75.0 and b["counted"] == 4
    assert b["new_highs"] == 3 and b["new_lows"] == 1


def test_distribution_days():
    cfg = MarketConfig(dist_window=10)
    closes = [100.0] * 20
    vols = [1000] * 20
    for i in (12, 15, 18):                     # down 1% on higher volume
        closes[i] = closes[i - 1] * 0.99
        closes[i + 1:] = [closes[i]] * (20 - i - 1)
        vols[i] = 2000
    closes[16] = closes[15] * 0.99             # down, but on lower volume: not counted
    vols[16] = 500
    idx = bars(closes)
    stock = bars([50] * 20, vols)
    assert market.distribution_days(idx, {"S": stock}, cfg) == 3
    assert market.distribution_days(idx[:5], {"S": stock}, cfg) is None


def test_breakout_success():
    st = lambda stage, n: SimpleNamespace(stage=stage, sessions_since_breakout=n)  # noqa: E731
    r = market.breakout_success([st("fresh", 2), st("climbing", 10), st("below_pivot", 4), st("played_out", 30),
                                 st("forming", None)], MarketConfig())
    assert r == {"working": 2, "total": 3, "rate": 0.67}


def sig(above_50=True, above_200=True, breadth=60.0, dd=2, rate=0.7):
    return {"index": {"above_50": above_50, "above_200": above_200}, "breadth": {"above_50_pct": breadth},
            "dist_days": dd, "breakouts": {"rate": rate}}


@pytest.mark.parametrize("kw,status", [
    ({}, "uptrend"),
    ({"breadth": 45.0}, "pressure"),
    ({"dd": 5}, "pressure"),
    ({"rate": 0.3}, "pressure"),
    ({"above_50": False, "breadth": 40.0}, "pressure"),
    ({"above_200": False}, "correction"),
    ({"above_50": False, "breadth": 30.0}, "correction"),
    ({"dd": 7}, "correction"),
    ({"rate": None, "dd": None}, "uptrend"),                     # unknown signals are skipped
    ({"above_50": None, "above_200": None, "breadth": None}, "pressure"),   # nothing to go on
    ({"above_50": None, "above_200": None, "breadth": 10.0}, "correction"),  # no index data, very weak breadth
    ({"above_50": True, "breadth": 30.0}, "pressure"),                       # index still above its 50-day
])
def test_status_rules(kw, status):
    assert market.classify(sig(**kw), MarketConfig()) == status


# --- tiers and gating ------------------------------------------------------------------

A_SETUP = {"symbol": "X", "vcp": {"stop": 1}, "rs": 90, "tt_passed": 8, "industry_rank": 3, "industry_of": 30}


@pytest.mark.parametrize("change", [{"vcp": None}, {"rs": 80}, {"tt_passed": 6}, {"industry_rank": 11},
                                    {"industry_rank": None}])
def test_tier_b_when_any_rule_fails(change):
    cfg = AlertsConfig()
    assert market.tier(A_SETUP, "uptrend", cfg) == "A"
    assert market.tier(dict(A_SETUP, **change), "uptrend", cfg) == "B"


def test_tier_b_in_correction_and_bases_vcp_tag():
    cfg = AlertsConfig()
    assert market.tier(A_SETUP, "correction", cfg) == "B"
    assert market.tier(dict(A_SETUP, vcp=None, vcp_tag=True), "pressure", cfg) == "A"


def ctx(status, **cfg):
    c = AlertsConfig(**cfg)
    h = {"status": status, "breadth": {"above_50_pct": 61.0}, "dist_days": 3, "breakouts": {"working": 7, "total": 10}}
    return MarketContext(h, status, msg.market_line(h), market.SIZE_FACTOR[status], market.SIZE_NOTE[status]), c


def test_gate():
    mc, cfg = ctx("uptrend")
    assert gate("breakout", A_SETUP, mc, cfg) == (True, "A")
    assert gate("breakout", dict(A_SETUP, rs=50), mc, cfg) == (False, "B")              # digest only
    mc, cfg = ctx("uptrend", b_alerts="push")
    assert gate("near_pivot", dict(A_SETUP, rs=50), mc, cfg) == (True, "B")
    mc, cfg = ctx("uptrend", tiering=False)
    assert gate("breakout", {}, mc, cfg) == (True, None)
    mc, cfg = ctx("correction", correction_mode="mute_buys")
    assert gate("breakout", A_SETUP, mc, cfg) == (False, None)
    for sell in ("below_pivot", "played_out", "failed", "system"):                       # never held back
        assert gate(sell, {}, mc, cfg) == (True, None)


def test_decorate():
    mc, _ = ctx("uptrend")
    out = decorate("🚀 <b>X</b> broke out", "breakout", "A", mc)
    assert out.split("\n")[0] == ("<i>Market 🟢 Uptrend · breadth 61% · 3 dist. days · breakouts working 7/10</i>")
    assert out.split("\n")[1].startswith("🅰️ 🚀")
    mc, _ = ctx("correction")
    assert "Market in correction" in decorate("x", "intraday", "B", mc)
    assert "Market in correction" not in decorate("x", "below_pivot", None, mc)
    assert decorate("x", "breakout", None, MarketContext(None, None, None, 1.0, "")) == "x"


# --- messages ---------------------------------------------------------------------

def test_market_block_and_index_move():
    h = {"status": "pressure", "index": {"above_50": True, "above_200": True, "ma50_rising": False, "change_pct": -1.2},
         "breadth": {"above_50_pct": 44.0, "above_200_pct": 58.0, "new_highs": 12, "new_lows": 9}, "dist_days": 5,
         "breakouts": {"working": 3, "total": 8}, "windows": {"dist": 25, "breakouts": 20}}
    b = msg.market_block(h, {"status": "uptrend"})
    assert "Market 🟡 Under pressure</b> (was 🟢 Uptrend)" in b and "50-day rising ✗" in b and "-1.2% today" in b
    assert "44% above the 50-day, 58% above the 200-day" in b and "Distribution days: 5" in b
    assert "Nifty 500 -0.8% today" in msg.market_line(h, -0.8)


# --- captions ---------------------------------------------------------------------

def test_split_caption_keeps_tags_whole():
    html = "\n".join(f"<b>line {i}</b> " + "x" * 80 for i in range(20))
    cap, rest = split_caption(html, 300)
    assert len(cap) <= 300 and cap.count("<b>") == cap.count("</b>") and rest.startswith("<b>line")
    assert cap + "\n" + rest == html
    assert split_caption("short", 300) == ("short", "")
    cap, rest = split_caption("<b>" + "y" * 400 + "</b>\nnext", 100)
    assert len(cap) <= 100 and "<b>" not in cap and rest == "next"


def test_long_photo_caption_sends_rest_as_message():
    fake = FakeTG()
    n = Notifier(AlertsConfig(chat_id="42"), fake)
    assert n.send("k1", "breakout", "\n".join("z" * 100 for _ in range(15)), "X", b"\x89PNG")
    assert [k for k, _ in fake.sent] == ["photo", "msg"]


# --- sizing -------------------------------------------------------------------------

def test_market_sizing():
    from backend.alerts.decision import decision_context, decision_lines
    kw = dict(symbol="EX", bases_cfg=BaseConfig(), vcp_cfg=VcpConfig(), alerts_cfg=AlertsConfig(capital=500_000))
    full = decision_context(_example_candles(), "breakout", **kw)
    half = decision_context(_example_candles(), "breakout", size_factor=0.5, size_note=market.SIZE_NOTE["pressure"],
                            **kw)
    assert half["size"]["qty"] == pytest.approx(full["size"]["qty"] / 2, abs=1)
    assert any("half size: market under pressure" in x for x in decision_lines(half))


# --- scan + dispatch ----------------------------------------------------------------

def test_scan_saves_market_health():
    as_of, tokens = scanned_db()
    with get_db() as conn:
        h = market.latest(conn)
    assert h["as_of"] == as_of and h["status"] in market.STATUS_TEXT and h["index"]["close"] is None   # no index
    syms = [{"symbol": s, "instrument_token": t} for s, t in tokens.items()]
    out = run_scan(BaseConfig(reset_ma=0), ScreenConfig(trend_ma=0), syms, index_token=tokens["FRESH"])
    with get_db() as conn:
        h = market.latest(conn)
    assert out["market"] == h["status"] and h["index"]["close"] is not None and h["breakouts"]["total"] >= 1


def test_dispatch_holds_b_and_lists_it_in_digest(monkeypatch):
    as_of, tokens = scanned_db()
    cfg = AlertsConfig(chat_id="42", screens=["bases"], near_pivot_pct=0.05)
    fake = FakeTG()
    sent = dispatch_scan(as_of, cfg, BaseConfig(reset_ma=0), Notifier(cfg, fake), tokens)
    individual = [h for _, h in fake.sent if "EOD" not in h]
    assert sent["held"] >= 1 and not any("broke out" in h for h in individual)      # B breakout: digest only
    digest = next(h for _, h in fake.sent if "EOD" in h)
    assert "📊 <b>Market" in digest and "🅱️ " in digest

    monkeypatch.setattr(market, "tier", lambda p, status, cfg: "A")
    fake = FakeTG()
    cfg2 = AlertsConfig(chat_id="42", screens=["bases"], near_pivot_pct=0.05, digest=False)
    with get_db() as conn:
        conn.execute("DELETE FROM sent_alerts")
    dispatch_scan(as_of, cfg2, BaseConfig(reset_ma=0), Notifier(cfg2, fake), tokens)
    bo = next(h for _, h in fake.sent if "broke out" in h)
    assert bo.startswith("<i>Market ") and "\n🅰️ " in bo


def test_api_market_and_row_tiers(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    as_of, _ = scanned_db()
    from backend import main
    with TestClient(main.app) as tc:
        r = tc.get("/api/market/health").json()
        assert r["health"]["as_of"] == as_of and r["history"][-1]["status"] == r["health"]["status"]
        assert r["config"]["breadth_up"] == 50.0
        rows = tc.get("/api/screens/bases/results?stage=forming").json()["rows"]
        assert rows and all(x["tier"] in ("A", "B") for x in rows)
        assert tc.put("/api/config/alerts", json={"b_alerts": "loud"}).status_code == 422
        assert tc.put("/api/config/market", json={"breadth_up": 55}).json()["breadth_up"] == 55.0


def test_config_choice_validation():
    c = AppConfig()
    with pytest.raises(ValueError):
        c.update_section("alerts", {"correction_mode": "panic"})
    assert c.alerts.correction_mode == "caution"
