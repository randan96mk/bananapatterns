"""Splits / bonuses found in the candles and adjusted away in get_candles."""

from backend.database.models import get_candles, get_db, upsert_candles
from backend.ingest import corp_actions as ca
from tests.test_bases import build


def _split(c, at, k, vol_k=None):
    """From bar `at` on, prices ÷ k and volume × k (an unadjusted k-for-1 split)."""
    out = []
    for i, b in enumerate(c):
        if i >= at:
            b = {**b, **{x: b[x] / k for x in ("open", "high", "low", "close")}, "volume": int(b["volume"] * (vol_k or k))}
        out.append(b)
    return out


def test_split_found_and_adjusted_crash_left_alone():
    c = build([("trend", 60, 0.3), ("range", 20, (0.95, 1.0))])
    s = _split(c, 50, 2)
    acts = ca.detect(s)
    assert len(acts) == 1 and acts[0][0] == c[50]["date"] and abs(acts[0][1] - 0.5) < 0.02
    with get_db() as conn:
        upsert_candles(conn, 77, s)
    assert ca.refresh([77]) == 1
    with get_db() as conn:
        adj, raw = get_candles(conn, 77), get_candles(conn, 77, adjusted=False)
    assert abs(adj[49]["close"] / adj[50]["close"] - raw[49]["close"] / raw[50]["close"] * 0.5) < 0.01   # continuous
    assert abs(adj[49]["close"] - raw[49]["close"] * acts[0][1]) < 0.01 and adj[49]["volume"] == round(raw[49]["volume"] / acts[0][1])
    assert adj[60] == raw[60]                                                                            # after: untouched
    crash = [{**b, **({x: b[x] * 0.5 for x in ("open", "high", "low", "close")} | {"volume": b["volume"] * 12} if i >= 50 else {})}
             for i, b in enumerate(c)]
    assert ca.detect(crash) == []                                                                       # 12× turnover: a move
    assert ca.detect(c) == []


def test_health_checks_cover_every_source():
    from backend.config import AppConfig
    from backend.engine import health
    cs = health.checks(AppConfig(), has_token=False)
    keys = [c["key"] for c in cs]
    assert keys[:3] == ["kite", "candles", "scan"] and "errors" in keys and "corp" in keys
    assert cs[0]["ok"] is False and all({"name", "ok", "warn", "state", "detail"} <= set(c) for c in cs)
