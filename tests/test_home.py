"""🏠 Home: the stats strip, risk, today's timeline and the market pulse on the overview."""

import json

from backend.config import AppConfig
from backend.database.models import get_db, now_ist
from backend.engine import overview


def test_home_blocks_from_the_books():
    cfg = AppConfig()
    today = now_ist()[:10]
    with get_db() as conn:
        conn.execute("INSERT INTO real_orders (key, day, created_at, symbol, action, qty, reason, status, price) VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"{today}|X|BUY", today, f"{today}T09:16:05", "X", "BUY", 10, "breakout", "sent", 101.0))
        conn.execute("INSERT INTO real_orders (key, day, created_at, symbol, action, qty, reason, status, price) VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"{today}|Y|BUY", today, f"{today}T09:16:07", "Y", "BUY", 5, "breakout", "failed", None))
        conn.execute("INSERT INTO flow_signals (day, at, symbol, payload) VALUES (?, ?, ?, ?)", (today, f"{today}T09:40:00", "Z", json.dumps({"symbol": "Z", "signal": "BUY"})))
    dp = {"real": {"2026-11-03": 100.0, "2026-11-04": 50.0, "2026-11-05": -20.0, today: 30.0}, "paper_A": {today: 10.0}}
    s = overview.stats(cfg, dp, today)
    assert s["win_streak"] == 1 and s["days_since_loss"] == 1 and s["best_day"]["net"] == 100.0 and s["worst_day"]["net"] == -20.0
    assert s["signals_today"] == 1 and s["orders_week"] == 2 and s["orders_failed_week"] == 1
    pos = [{"book": "real", "symbol": "X", "side": "long", "qty": 10, "entry": 100.0, "now": 104.0, "stop": 95.0, "stop_dist": 8.65},
           {"book": "intraday", "symbol": "Z", "side": "long", "qty": 4, "entry": 50.0, "now": 50.5, "stop": 50.0, "stop_dist": 0.99}]
    r = overview.risk(cfg, {"real": {"value": 1}, "intraday": {"realised": -200, "loss_limit": 3000, "capital": 100000}}, pos)
    assert r["in_use"] == 1200.0 and r["worst_case"] == -92.0 and r["biggest"]["symbol"] == "X" and r["near_stop"] == ["Z"]
    assert r["orders_today"] == {"sent": 1, "failed": 1} and r["loss_used"] == 200.0
    tl = overview.timeline(today)
    assert [e["kind"] for e in tl] == ["order", "failed", "signal"] and tl[0]["t"] == "09:16"
    p = overview.pulse(cfg, {"feed": "off", "rows": {}})
    assert p["signals_live"] == 0
    o = overview.overview(cfg)
    assert {"stats", "risk", "timeline", "pulse", "today_curve"} <= set(o) and isinstance(o["today_curve"], list)
