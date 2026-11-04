"""📈 Follow: every fresh breakout bought at the next open, sold at the next open after the run ends, causal."""

from backend.config import AppConfig
from backend.database.models import get_db, upsert_candles
from backend.engine import follow
from backend.engine.scan import run_scan
from tests.test_bases import build
from tests.test_stages import BASE, BCFG, SCFG, UP


def _config():
    cfg = AppConfig()
    cfg.bases, cfg.screen = BCFG, SCFG                     # short synthetic series: no 200-day trend check
    return cfg


def _series(tail):
    """Up-trend, a base, a volume breakout, then `tail` segments."""
    return build([UP, BASE, ("bo", 1, 1.05, 3_000_000), *tail])


def test_backfill_buys_next_open_and_sells_next_open_after_the_run_ends():
    cfg = _config()
    c = _series([("trend", 6, 0.06), ("bar", 1, 0.80), ("bar", 1, 1.01), ("bar", 1, 1.00)])   # runs up, then −20%
    sig = follow._signals("X", c, c[0]["date"], lambda d: {"X": 80}, cfg)
    assert sig and sig[0][1] == ["bases"]                                                   # fresh on the bases screen
    t = sig[0][0]
    (p,) = follow._simulate("X", 1, c, sig, cfg, 100000)
    assert p["breakout_date"] == c[t]["date"] and p["entry_date"] == c[t + 1]["date"]        # bought the next session
    assert p["entry"] == round(c[t + 1]["open"], 2) and p["qty"] == int(100000 // c[t + 1]["open"])
    drop = t + 7                                                                              # the −20% bar
    assert c[drop]["close"] <= p["stop"]
    assert p["status"] == "closed" and p["exit_date"] == c[drop + 1]["date"] and "below" in p["reason"]
    assert p["exit"] == round(c[drop + 1]["open"], 2) and p["net"] < p["gross"] < 0                 # charges both ways


def test_open_position_is_marked_and_no_future_bar_is_used():
    cfg = _config()
    full = _series([("trend", 8, 0.08), ("bar", 1, 0.80)])
    cut = full[:-2]                                                                          # the fall has not happened
    sig = follow._signals("X", cut, cut[0]["date"], lambda d: {"X": 80}, cfg)
    (p,) = follow._simulate("X", 1, cut, sig, cfg, 100000)
    assert p["status"] == "open" and p["last_date"] == cut[-1]["date"] and p["net"] > 0 and p["days"] > 0
    sig2 = follow._signals("X", full, full[0]["date"], lambda d: {"X": 80}, cfg)
    assert sig2[0][0] == sig[0][0]                                                           # the same signal day


def test_daily_update_from_the_scan_opens_then_fills(monkeypatch):
    monkeypatch.setenv("BANANA_TEST", "1")
    cfg = _config()
    c = _series([("bar", 1, 1.01), ("bar", 1, 1.02)])
    syms = [{"symbol": "X", "instrument_token": 11}]
    with get_db() as conn:
        conn.execute("DELETE FROM screen_follow")
        upsert_candles(conn, 11, c[:-2])
    out = run_scan(cfg.bases, cfg.screen, syms)
    assert out["counts"]["bases"]["fresh"] == 1
    r = follow.update(cfg, out["as_of"], syms)
    assert r["opened"] == 1
    rep = follow.report()
    (p,) = rep["rows"]
    assert p["status"] == "pending" and "bases" in p["screens"] and rep["total"]["open"] == 1
    with get_db() as conn:
        upsert_candles(conn, 11, c[:-1])                                                     # the next session arrives
    follow.update(cfg, c[-2]["date"], syms)
    (p,) = follow.report()["rows"]
    assert p["status"] == "open" and p["entry"] == round(c[-2]["open"], 2) and p["qty"] >= 1
    follow.update(cfg, c[-2]["date"], syms)                                                   # run twice: unchanged
    assert follow.report()["rows"][0]["entry_date"] == c[-2]["date"] and len(follow.report()["rows"]) == 1
    assert follow.set_per_position(50000) == 50000 and follow.per_position() == 50000
