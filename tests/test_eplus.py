"""E+ (E with the late trail, Nov 2026): the rule, no look-ahead, A and E (trail off) unchanged, paper and real."""

import copy
import importlib.util
import pathlib
import random
from dataclasses import replace

import pytest

from backend.database.models import upsert_candles, get_db
from backend.engine import paper, real
from backend.engine import trend_breakout as tb
from tests.test_paper import cfg, store


def rocket(monkeypatch, days, start=250, gap=650):
    """One extra stock: flat, then a steady climb (well past +100%), a 40% gap down, then flat."""
    import backend.ingest.universe as uni
    stocks = uni.resolve_universe(None)["stocks"]
    random.seed(1)
    p, rows = 500.0, []
    for k, x in enumerate(days):
        o = p
        if k == gap:
            o = p * 0.6
            c = o * 1.01
        else:
            c = o * (1 + (0.004 if start <= k < gap else 0.0) + random.gauss(0, 0.004))
        rows.append({"date": x, "open": o, "high": max(o, c) * 1.003, "low": min(o, c) * 0.997, "close": c,
                     "volume": 400_000 * (3 if k % 10 == 0 else 1)})
        p = c
    with get_db() as conn:
        upsert_candles(conn, 99, rows)
    stocks = stocks + [{"symbol": "ROCKET", "tradingsymbol": "ROCKET", "instrument_token": 99}]
    monkeypatch.setattr("backend.ingest.universe.resolve_universe",
                        lambda cfg, *a: {"stocks": stocks, "indices": [{"symbol": "NIFTY 50", "instrument_token": 1}]})


def setup(monkeypatch):
    days = store(monkeypatch, n=760)
    rocket(monkeypatch, days)
    c = cfg()
    c.breakout.test_years = 1.8
    return c, tb.load(c)


def sim(c, data, p):
    return tb.simulate(data["cal"], data["S"], data["index_ok"], p, p.slippage_pct, c.breakout.warmup_sessions,
                       filters=data["filters"])


def test_the_rule():
    p = tb.rules(cfg(), "E")
    assert (p.late_at, p.late_pct) == (100.0, 25.0) and tb.rules(cfg(), "A").late_at == 0
    assert tb.late_stop(100, 199.9, p) is None                                   # under +100%: nothing
    assert tb.late_stop(100, 210, p) == pytest.approx(0.75 * 210)                # 2.1x: 25% under the best close
    pos = {"px": 100.0, "stop": 92.0, "best": 100.0}
    assert tb.raise_late(pos, 150, p) is None and pos["stop"] == 92.0            # +50%: unchanged
    assert tb.raise_late(pos, 210, p) == pytest.approx(157.5) and pos["trailed"]
    assert tb.raise_late(pos, 180, p) is None and pos["stop"] == pytest.approx(157.5)   # never lowered
    assert pos["best"] == 210
    assert tb.variant_name("E", 300) == "E+ · half filter, top 300, late trail"
    assert tb.variant_name("A", 200) == "A · 200-day filter, top 200"


def test_a_and_e_without_the_trail_match_the_old_code(monkeypatch):
    """The code before E+ (origin/main, kept as a fixture) on the same data: A and E (trail off) give the same trades."""
    c, data = setup(monkeypatch)
    old_src = pathlib.Path(__file__).parent / "fixtures" / "trend_breakout_before_eplus.py"
    spec = importlib.util.spec_from_file_location("old_tb", old_src)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    for key in ("A", "E"):
        po = old.rules(c, key)
        eq0, _, t0, _ = old.simulate(data["cal"], data["S"], data["index_ok"], po, po.slippage_pct,
                                     c.breakout.warmup_sessions, filters=data["filters"])
        eq1, _, t1, _ = sim(c, data, replace(tb.rules(c, key), late_at=0.0))
        strip = lambda ts: [{k: v for k, v in t.items() if k != "best"} for t in ts]
        assert strip(t1) == t0 and eq1 == eq0
    assert sim(c, data, tb.rules(c, "A"))[2] == sim(c, data, replace(tb.rules(c, "A"), late_pct=5.0))[2]   # A: no trail


def test_trail_exit_uses_only_closes_before_the_exit_day(monkeypatch):
    c, data = setup(monkeypatch)
    p = tb.rules(c, "E")
    _, _, trades, _ = sim(c, data, p)
    t = next(t for t in trades if t["sym"] == "ROCKET")
    rows = data["S"]["ROCKET"]["rows"]
    closes = [r["close"] for r in rows if t["in"] <= r["date"] < t["out"]]        # up to the day BEFORE the exit
    best = max(closes + [t["buy"]])
    out = next(r for r in rows if r["date"] == t["out"])
    assert t["why"] == "trail" and best >= 2 * t["buy"]
    assert t["best"] == pytest.approx(best, abs=0.01) and t["stop"] == pytest.approx(0.75 * best, abs=0.01)
    assert t["sell"] == pytest.approx(min(out["open"], 0.75 * best), abs=0.01)    # the gap: out at the open
    plain = next(x for x in sim(c, data, replace(p, late_at=0.0))[2] if x["sym"] == "ROCKET")
    assert plain["why"] != "trail" and plain["in"] == t["in"]
    st = tb._stats(*sim(c, data, p)[:3], p.capital, 10)
    assert st["exits"]["trail"] >= 1 and st["best_trade_pct"] > 0 and st["avg_win"] > 0 > st["avg_loss"]


def test_eighteen_neighbours_for_e_plus_fourteen_for_a():
    c = cfg()
    assert len(tb.neighbours(tb.rules(c, "A"))) == 14
    nb = tb.neighbours(tb.rules(c, "E"))
    assert len(nb) == 18 and [l for l, _ in nb[-4:]] == ["trail from +50%", "trail from +150%", "trail 20%", "trail 30%"]


def test_saved_run_is_stale_until_rerun(monkeypatch):
    import json
    c = cfg()
    with get_db() as conn:
        conn.execute("INSERT INTO breakout_runs (id, updated_at, payload) VALUES (1, 'x', ?)",
                     (json.dumps({"variants": {"A": {"top": 200}, "E": {"top": 300}}}),))
    assert tb.saved(c)["stale"] == ["E"]                                          # an old E run: never shown as E+


def _step_data(bars):
    rows = [{"date": f"2026-11-{k + 2:02d}", **b, "volume": 1} for k, b in enumerate(bars)]
    return {"S": {"X": {"rows": rows, "at": {k: k for k in range(len(rows))}}}, "cal": [r["date"] for r in rows]}


def test_paper_step_raises_once_never_lowers_then_exits_on_the_trail():
    p = tb.rules(cfg(), "E")
    bar = lambda o, h, l, cl: {"open": o, "high": h, "low": l, "close": cl}
    data = _step_data([bar(200, 211, 199, 210), bar(205, 206, 190, 195), bar(160, 161, 150, 151)])
    st = {"positions": {"X": {"qty": 10, "px": 100.0, "d": "2026-10-01", "stop": 92.0, "last": 200, "prev": 199,
                              "best": 200.0, "charges": 0, "slippage": 0, "days": 30}},
          "cash": 0.0, "charges": 0.0, "slippage": 0.0, "orders": []}
    paper._step(st, data, 0, p)
    assert st["positions"]["X"]["stop"] == pytest.approx(157.5) and len(st["raised"]) == 1   # from 210's close
    paper._step(st, data, 1, p)                                                    # low 190 > 157.5; close 195
    assert st["positions"]["X"]["stop"] == pytest.approx(157.5) and len(st["raised"]) == 1   # not lowered, once
    trades, _ = paper._step(st, data, 2, p)                                        # opens 160, low 150 <= 157.5
    assert trades[0]["why"] == "trail" and trades[0]["sell"] == 157.5 and trades[0]["best"] == 210


def test_paper_migration_backfills_once_and_rerun_is_a_no_op(monkeypatch):
    c, data = setup(monkeypatch)
    c.breakout.late_at_e = 0.0                                                     # the account before the update
    s = len(data["cal"]) - 250
    paper.start(c, at=data["cal"][s])
    paper.process(c, data)
    before = paper._states()["E"]
    assert before["positions"] and not before.get("eplus_since")
    c.breakout.late_at_e = 100.0                                                   # the update: E -> E+
    assert paper.process(c, data)["sessions"] == 0                                 # no day re-simulated
    st = paper._states()["E"]
    for sym, x in st["positions"].items():
        want = max(r["close"] for r in data["S"][sym]["rows"] if x["d"] <= r["date"] <= st["as_of"])
        assert x["best"] == pytest.approx(want) and x["stop"] >= before["positions"][sym]["stop"]
    assert sum(n["text"].startswith("strategy updated to E+ on") for n in st["notes"]) == 1
    assert st["name"].startswith("E+") and st["cash"] == before["cash"]
    snap = paper.summary(c)
    paper.process(c, data)                                                          # again: nothing changes
    assert paper.summary(c) == snap and paper._states()["A"] == paper._states()["A"]


def test_real_trail_only_for_e_positions_and_never_lowered(monkeypatch):
    p = tb.rules(cfg(), "E")
    data = _step_data([{"open": 200, "high": 211, "low": 199, "close": 210}, {"open": 205, "high": 206, "low": 190, "close": 195}])
    st = {"positions": {"X": {"qty": 1, "px": 100.0, "d": "2026-11-02", "stop": 92.0, "last": 210, "variant": "E"},
                        "Y": {"qty": 1, "px": 100.0, "d": "2026-11-02", "stop": 92.0, "last": 210, "variant": "A"}}}
    data["S"]["Y"] = data["S"]["X"]
    r1 = real.late_trail(st, data, "2026-11-02", p)
    assert [x["symbol"] for x in r1] == ["X"] and st["positions"]["X"]["stop"] == pytest.approx(157.5)
    assert st["positions"]["Y"]["stop"] == 92.0 and "best" not in st["positions"]["Y"]           # A untouched
    assert real.late_trail(st, data, "2026-11-03", p) == [] and st["positions"]["X"]["stop"] == pytest.approx(157.5)
    assert len(st["notes"]) == 1


def test_real_raised_stop_sends_nothing_while_off_or_killed(monkeypatch):
    c = cfg()
    st = {"positions": {"X": {"qty": 1, "px": 100.0, "d": "2026-11-02", "stop": 157.5, "last": 160, "variant": "E",
                              "best": 210.0, "trailed": True, "ts": "X", "charges": 0.0}}, "cash": 0, "charges": 0, "capital": 1e5}
    real._save(copy.deepcopy(st))
    sent = []
    client = type("K", (), {"has_token": True})()
    monkeypatch.setattr(real, "_quotes", lambda *a, **k: {"X": (150.0, 150.0)})
    c.real.enabled = False
    assert real.watch_stops(c, client, "2026-11-03", lambda *a, **k: sent.append(a))["status"] == "off"
    c.real.enabled = True
    real.set_killed(True)
    assert real.watch_stops(c, client, "2026-11-03", lambda *a, **k: sent.append(a))["status"] == "off" and sent == []
    real.set_killed(False)
    res = real.watch_stops(c, client, "2026-11-03", lambda *a, **k: sent.append(a) or "ok")
    assert len(sent) == 1 and res["hits"][0]["why"].startswith("stop 157.50")
    with get_db() as conn:
        import json
        t = json.loads(conn.execute("SELECT payload FROM real_trades").fetchone()[0])
    assert t["why"] == "trail" and t["best"] == 210.0


def test_verify_tool_runs_read_only(monkeypatch, capsys):
    from backend.tools import verify_eplus
    c, data = setup(monkeypatch)
    c.breakout.test_years = 1.0
    monkeypatch.setattr("backend.tools.verify_eplus.load_config", lambda: c)
    monkeypatch.setattr(tb, "NEIGHBOURS", (("vol_mult", (1.3,)),))                # keep it quick
    paper.start(c, at=data["cal"][-60])
    paper.process(c, data)
    before = paper.summary(c)
    verify_eplus.main()
    out = capsys.readouterr().out
    assert "E+ · half filter, top 300, late trail" in out and "expected 21.2%" in out and "late trail" in out
    assert "paper E:" in out and "real: not started" in out
    assert paper.summary(c) == before                                                  # nothing saved
