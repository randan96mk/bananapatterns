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


def test_backup_reconcile_and_evening_line(monkeypatch, tmp_path):
    import json
    from backend.config import AppConfig
    from backend.engine import backup, follow, real
    out = backup.run()
    assert out["name"].startswith("bananapatterns-") and backup.listing()[0]["name"] == out["name"]
    cfg = AppConfig()
    with get_db() as conn:
        conn.execute("INSERT INTO paper_fills (account, day, payload) VALUES (?, ?, ?)",
                     (cfg.real.variant, "2026-11-03", json.dumps({"symbol": "X", "action": "BUY", "qty": 10, "price": 100.0, "why": "breakout"})))
        conn.execute("INSERT INTO paper_fills (account, day, payload) VALUES (?, ?, ?)",
                     (cfg.real.variant, "2026-11-03", json.dumps({"symbol": "Y", "action": "BUY", "qty": 5, "price": 50.0, "why": "breakout"})))
        conn.execute("INSERT INTO real_orders (key, day, created_at, symbol, action, qty, reason, status, price) VALUES (?,?,?,?,?,?,?,?,?)",
                     ("2026-11-03|X|BUY", "2026-11-03", "2026-11-03T09:16:00", "X", "BUY", 10, "breakout", "sent", 101.0))
    r = real.reconcile(cfg, 9999)
    by = {x["symbol"]: x for x in r["rows"]}
    assert by["X"]["real"] == "sent" and by["X"]["price_diff_pct"] == 1.0 and by["Y"]["real"] == "missing"
    assert r["counts"]["sent"] == 1 and r["counts"]["missing"] == 1
    assert follow.evening_line("2026-11-03") is None


def test_swing_positions_marked_live_in_market_hours(monkeypatch):
    from datetime import datetime
    from backend.config import IST
    from backend.engine import overview
    rows = [{"book": "real", "symbol": "X", "entry": 100.0, "qty": 10, "stop": 95.0, "now": 100.0, "pnl": 0.0, "pct": 0.0},
            {"book": "intraday", "symbol": "Y", "entry": 50.0, "qty": 1, "now": 50.0}]
    monkeypatch.setattr(overview, "market_open", lambda now=None: True)
    overview._mark_live(rows, lambda keys: {"NSE:X": {"last_price": 110.0}})
    assert rows[0]["live"] and rows[0]["now"] == 110.0 and 80 < rows[0]["pnl"] < 100 and rows[0]["stop_dist"] > 0   # 100 gross − sell charges
    assert "live" not in rows[1]
    monkeypatch.setattr(overview, "market_open", lambda now=None: False)
    r2 = [{"book": "real", "symbol": "X", "entry": 100.0, "qty": 10, "stop": 95.0, "now": 100.0}]
    overview._mark_live(r2, lambda keys: {"NSE:X": {"last_price": 110.0}})
    assert "live" not in r2[0]                                                  # closed market: the stored mark
    assert overview.market_open(datetime(2026, 11, 4, 10, 0, tzinfo=IST)) and not overview.market_open(datetime(2026, 11, 4, 16, 0, tzinfo=IST))
