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
    dp = {"real": {"2026-11-03": 100.0, "2026-11-04": 50.0, "2026-11-05": -20.0, today: 30.0}, "paper_E": {today: 10.0}}
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


def test_facts_per_card():
    from backend.engine.overview import facts
    cfg = AppConfig()
    accts = {"real": {"value": 1}, "paper_E": {"return": 1.0, "nifty": 4.7, "positions": 0}, "intraday": {"realised": -86, "loss_limit": 3000}, "follow": {"open": 3, "open_pnl": 10, "pending": 1}}
    pos = [{"book": "real", "symbol": "X", "entry": 100.0, "qty": 10}]
    f = facts(cfg, accts, pos, [["2026-11-06T09:27:00", 2140.0], ["2026-11-06T10:22:00", -969.0]], 3)
    assert f["today"] == {"high": 2140.0, "high_at": "09:27", "low": -969.0, "low_at": "10:22"}
    assert f["real"]["in_use"] == 1000.0 and f["real"]["slots"] == 1 and f["real"]["max_pos"] == cfg.breakout.max_pos
    assert f["paper_E"]["vs_nifty"] == -3.7 and "paper_A" not in f and f["intraday"]["loss_used"] == 86.0 and f["intraday"]["signals"] == 3 and f["follow"]["pending"] == 1


def _real_state(positions: dict) -> None:
    from backend.engine import real
    real._save({"started": "2026-10-01", "capital": 200_000.0, "cash": 100_000.0, "charges": 0.0, "as_of": "2026-11-06",
                "positions": positions, "orders": [], "deposits": []})


def _pos(symbol, px, best=None, stop=None, trailed=False, last=None, d="2026-10-20"):
    p = {"qty": 10, "px": px, "d": d, "stop": stop or px * 0.92, "last": last or best or px, "charges": 10.0, "ts": symbol,
         "variant": "E", "days": 5}
    if best:
        p["best"] = best
    if trailed:
        p["trailed"] = True
    return p


def test_positions_carry_the_trail_and_the_results_date():
    """trail: E+'s late trail per swing row (null on intraday); results_date / results_in only within 7 days."""
    from backend.ingest import events
    cfg = AppConfig()
    today = now_ist()[:10]
    _real_state({"ARMED": _pos("ARMED", 100.0, best=210.0, stop=157.5, trailed=True),
                 "HALF": _pos("HALF", 100.0, best=150.0),
                 "FRESH": _pos("FRESH", 100.0, last=104.0)})
    from datetime import date, timedelta
    soon = (date.fromisoformat(today) + timedelta(days=3)).isoformat()
    far = (date.fromisoformat(today) + timedelta(days=20)).isoformat()
    events.store([{"symbol": "ARMED", "date": soon, "kind": "results", "purpose": "Financial Results", "descr": ""},
                  {"symbol": "HALF", "date": far, "kind": "results", "purpose": "Financial Results", "descr": ""}])
    rows = {p["symbol"]: p for p in overview.positions(cfg, paper_s={"accounts": {}}, live={"rows": {}})}
    assert rows["ARMED"]["trail"] == {"active": True, "pct_to_arm": 0.0, "best": 210.0, "raised": True}
    assert rows["HALF"]["trail"] == {"active": False, "pct_to_arm": 50.0, "best": 150.0, "raised": False}
    assert rows["FRESH"]["trail"]["active"] is False and rows["FRESH"]["trail"]["pct_to_arm"] == 96.0 and rows["FRESH"]["trail"]["best"] is None
    assert rows["ARMED"]["results_date"] == soon and isinstance(rows["ARMED"]["results_in"], int) and rows["ARMED"]["results_in"] <= 3
    assert rows["HALF"]["results_date"] is None and rows["HALF"]["results_in"] is None     # 20 days out: not shown
    assert overview.results_held(list(rows.values())) == [{"symbol": "ARMED", "date": soon}]
    intraday = {"book": "intraday", "symbol": "Z", "side": "long", "qty": 1, "entry": 1, "now": 1, "pnl": 0, "pct": 0,
                "stop": None, "stop_dist": None, "opened": today}
    overview._add_trail(cfg, [intraday], {"positions": []}, {"accounts": {}})
    overview._add_results([intraday], today)
    assert intraday["trail"] is None and intraday["results_date"] is None
    o = overview.overview(cfg)
    assert o["stats"]["results_held"] == [{"symbol": "ARMED", "date": soon}]
    assert {"drift", "next_open"} <= set(o) and o["next_open"] is None and "spark_nifty" in o["accounts"]["real"]


def test_drift_real_vs_paper_on_the_shared_names():
    pos = [{"book": "real", "symbol": "A", "pnl": 1000.0}, {"book": "real", "symbol": "B", "pnl": -50.0},
           {"book": "paper_E", "symbol": "A", "pnl": 800.0}, {"book": "paper_E", "symbol": "C", "pnl": 300.0}]
    cv = {"real": [("2026-11-03", 100.0), ("2026-11-04", 110.0), ("2026-11-05", 120.0)],
          "paper_E": [("2026-11-04", 105.0), ("2026-11-05", 125.0), ("2026-11-06", 130.0)]}
    d = overview.drift(pos, cv)
    assert d["names"] == 1 and d["real_pnl"] == 1000.0 and d["paper_pnl"] == 800.0 and d["diff"] == 200.0 and d["pct_of_paper"] == 25.0
    assert d["spark"] == [5.0, -5.0]                       # the days both books have
    assert overview.drift(pos, {"real": cv["real"]})["spark"] == []
    e = overview.drift([{"book": "real", "symbol": "A", "pnl": 10.0}], {})
    assert e["names"] == 0 and e["pct_of_paper"] is None and e["diff"] == 0.0


def test_timeline_shows_the_stops_the_trail_raised_today():
    from backend.database.models import set_setting
    cfg = AppConfig()
    today = now_ist()[:10]
    with get_db() as conn:
        set_setting(conn, "real.trail_raised", json.dumps({"day": today, "raised": [
            {"day": today, "symbol": "DIVISLAB", "stop": 10120.0, "best": 13493.0, "entry": 6000.0, "from": 9394.0}]}))
        set_setting(conn, "paper.trail_raised", json.dumps({"day": "2026-01-01", "raised": [
            {"day": "2026-01-01", "symbol": "OLD", "stop": 1.0, "best": 2.0, "entry": 0.5, "from": 0.9}]}))
    tl = overview.timeline(today, cfg)
    assert tl == [{"t": cfg.alerts.job_time, "kind": "trail", "text": "stop raised DIVISLAB 9,394 → 10,120"}]


def test_evening_runs_persist_the_raised_stops(monkeypatch):
    """real.late_trail records the stop it raised from; real.evening keeps the day's list in app_settings."""
    from backend.database.models import get_setting
    from backend.engine import real
    from backend.engine import trend_breakout as tb
    p = tb.rules(AppConfig(), "E")
    st = {"positions": {"X": {"qty": 1, "px": 100.0, "d": "2026-11-02", "stop": 92.0, "last": 210, "variant": "E"}}}
    data = {"S": {"X": {"rows": [{"date": "2026-11-02", "open": 200, "high": 211, "low": 199, "close": 210}], "at": {0: 0}}}}
    r = real.late_trail(st, data, "2026-11-02", p)
    assert r[0]["from"] == 92.0 and r[0]["stop"] == 157.5
    with get_db() as conn:
        assert get_setting(conn, "real.trail_raised") is None


def test_next_open_from_the_saved_scan():
    from backend.database.models import set_setting
    cfg = AppConfig()
    pos = [{"book": "real", "symbol": "A"}, {"book": "real", "symbol": "B"}, {"book": "paper_E", "symbol": "C"}]
    assert overview.next_open(cfg, pos) is None
    scan = {"as_of": "2026-11-06", "filters": {"E": {"state": "HALF", "slots": 5}},
            "buys": [{"symbol": "P", "close": 100.0, "stop": 92.0, "stop_pct": 8.0, "qty": 200, "value": 20000.0, "risk": 1600.0, "ret126": 30.0},
                     {"symbol": "Q", "close": 50.0, "stop": 47.0, "stop_pct": 6.0, "qty": 400, "value": 20000.0, "risk": 1200.0, "ret126": 20.0}]}
    with get_db() as conn:
        set_setting(conn, "breakout.scan", json.dumps(scan))
    n = overview.next_open(cfg, pos)
    assert n["as_of"] == "2026-11-06" and n["filter"] == "HALF" and n["slots_free"] == cfg.breakout.max_pos - 2
    assert n["buys"] == [{"symbol": "P", "qty": 200, "value": 20000.0, "stop_pct": 8.0, "risk": 1600.0},
                         {"symbol": "Q", "qty": 400, "value": 20000.0, "stop_pct": 6.0, "risk": 1200.0}]
    assert n["total_value"] == 40000.0 and n["fits"] == 2
    assert overview.next_open(cfg, [{"book": "real", "symbol": s} for s in "ABCDEFGHIJ"])["slots_free"] == 0


def test_pulse_at_the_close_from_the_daily_candles(monkeypatch):
    from backend.database.models import upsert_candles
    cfg = AppConfig()
    bars = {11: [(100, 110, 90, 104), (104, 106, 100, 105)],        # up, close over the typical price (103.67)
            12: [(50, 51, 49, 50), (50, 52, 44, 45)],               # down, under the typical price (47)
            13: [(10, 11, 9, 10), (10, 10, 10, 10)],                # flat
            14: [(20, 21, 19, 20)]}                                 # no bar on the last session: not counted
    idx = [(100, 101, 99, 100), (100, 108, 99, 100)]                # NIFTY under its typical price (102.33)
    with get_db() as conn:
        for tok, rows in {**bars, 1: idx}.items():
            upsert_candles(conn, tok, [{"date": f"2026-11-0{5 + k}", "open": o, "high": h, "low": lo, "close": c, "volume": 1}
                                       for k, (o, h, lo, c) in enumerate(rows)])
    monkeypatch.setattr("backend.ingest.universe.resolve_universe",
                        lambda c, *a: {"stocks": [{"symbol": f"S{t}", "instrument_token": t} for t in bars],
                                       "indices": [{"symbol": "NIFTY 50", "instrument_token": 1}]})
    p = overview.pulse(cfg, {"feed": "off", "rows": {}})
    assert p["live"] is False and p["as_of_close"] is True and p["proxy"] is True and p["close_date"] == "2026-11-06"
    assert p["advancing"] == 1 and p["declining"] == 1 and p["above_vwap_pct"] == 33.3 and p["nifty_vs_vwap"] == "below"


def test_spark_nifty_rebased_to_the_capital():
    with get_db() as conn:
        for k, (d, n) in enumerate([("2026-11-03", 25000.0), ("2026-11-04", 25500.0), ("2026-11-05", 24750.0)]):
            conn.execute("INSERT INTO real_equity (day, equity, gross, nifty) VALUES (?, ?, ?, ?)", (d, 100.0 + k, 100.0 + k, n))
            conn.execute("INSERT INTO paper_equity (account, day, equity, gross, nifty) VALUES ('E', ?, ?, ?, ?)", (d, 1.0, 1.0, n))
    assert overview.spark_nifty("real_equity", None, 200_000.0) == [200000.0, 204000.0, 198000.0]
    assert overview.spark_nifty("paper_equity", "E", 100_000.0, n=2) == [102000.0, 99000.0]
    assert overview.spark_nifty("paper_equity", "A", 100_000.0) == []
