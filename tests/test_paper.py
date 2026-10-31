"""📝 Paper trading: the Trend Breakout rules run forward session by session give the same fills, trades and account
as the backtest over the same days; the capital drives the sizes; costs are kept apart; every message says PAPER
TRADING; start / reset and the API."""

import json
import random
from datetime import date, timedelta

import pytest

from backend.config import AppConfig
from backend.database.models import get_db, upsert_candles
from backend.engine import paper
from backend.engine import trend_breakout as tb


def store(monkeypatch, n_stocks=40, n=600, seed=3):
    random.seed(seed)
    days, d = [], date(2023, 1, 2)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    idx, p = [], 10_000.0
    for x in days:
        p *= 1 + 0.0008 + random.gauss(0, 0.008)
        idx.append({"date": x, "open": p, "high": p * 1.004, "low": p * 0.996, "close": p, "volume": 1})
    stocks = []
    with get_db() as conn:
        upsert_candles(conn, 1, idx)
        for k in range(n_stocks):
            p, rows, drift = random.uniform(150, 3000), [], random.uniform(0.0, 0.002)
            for x in days:
                o = p * (1 + random.gauss(0, 0.005))
                c = o * (1 + drift + random.gauss(0, 0.017))
                rows.append({"date": x, "open": o, "high": max(o, c) * 1.006, "low": min(o, c) * 0.994, "close": c,
                             "volume": random.randint(100_000, 900_000) * (3 if random.random() < 0.06 else 1)})
                p = c
            upsert_candles(conn, 10 + k, rows)
            stocks.append({"symbol": f"S{k}", "tradingsymbol": f"S{k}", "instrument_token": 10 + k})
    monkeypatch.setattr("backend.ingest.universe.resolve_universe",
                        lambda cfg, *a: {"stocks": stocks, "indices": [{"symbol": "NIFTY 50", "instrument_token": 1}]})
    return days


def cfg(capital=200_000.0):
    c = AppConfig()
    c.breakout.test_years, c.breakout.warmup_years = 1.0, 1.4
    c.paper.capital = capital
    return c


def check_account(c, data, s, key):
    from dataclasses import replace
    p = tb.rules(c, key)
    eq, eqg, trades, _ = tb.simulate(data["cal"], data["S"], data["index_ok"], p, p.slippage_pct, s,
                                     capital=200_000.0, close_end=False, filters=data["filters"])
    sm = paper.summary(c)["accounts"][key]
    assert sm["closed"] == len(trades)
    got = sorted((t["symbol"], t["in"], t["out"], t["qty"], t["net"]) for t in sm["trades"])
    want = sorted((t["sym"], t["in"], t["out"], t["qty"], t["net"]) for t in trades)
    assert [g[:4] for g in got] == [w[:4] for w in want]
    assert [(g, w) for g, w in zip(got, want) if abs(g[4] - w[4]) >= 0.05] == []
    assert sm["account"]["value"] == pytest.approx(eq[-1], abs=0.05)
    assert sm["account"]["gross"] - sm["account"]["costs"] == pytest.approx(sm["account"]["net"], abs=0.05)
    assert sm["account"]["realised"] + sm["account"]["unrealised"] == pytest.approx(sm["account"]["net"], abs=0.05)
    assert sm["compare"]["return"] == pytest.approx(sm["account"]["return"], abs=0.01)
    return sm


def test_both_accounts_match_the_backtest_over_the_same_days(monkeypatch):
    store(monkeypatch)
    c = cfg()
    data = tb.load(c)
    s = len(data["cal"]) - 200
    assert paper.start(c, at=data["cal"][s])["accounts"] == ["A", "E"]
    res = paper.process(c, data)
    assert res["status"] == "ok" and res["sessions"] == 199
    a, e = check_account(c, data, s, "A"), check_account(c, data, s, "E")
    assert a["closed"] >= 5 and e["closed"] >= 5
    t = a["trades"][0]
    assert t["costs"] == pytest.approx(t["charges"] + t["slippage"], abs=0.02)
    assert paper.process(c, data)["sessions"] == 0                                    # the same day again: nothing


def test_a_late_account_starts_on_the_same_day(monkeypatch):
    store(monkeypatch)
    c = cfg()
    c.paper.variants = ["A"]
    data = tb.load(c)
    s = len(data["cal"]) - 150
    paper.start(c, at=data["cal"][s])
    paper.process(c, data)
    c.paper.variants = ["A", "E"]                                                       # E added later: replayed
    paper.process(c, data)
    sm = paper.summary(c)["accounts"]
    assert sm["E"]["start_date"] == sm["A"]["start_date"] == data["cal"][s]
    check_account(c, data, s, "E")


def test_capital_drives_the_quantities(monkeypatch):
    store(monkeypatch)
    qty = {}
    for cap in (1_000_000.0, 4_000_000.0):
        paper.reset()
        c = cfg(cap)
        data = tb.load(c)
        paper.start(c, at=data["cal"][-200])
        paper.process(c, data)
        with get_db() as conn:
            import json
            f = json.loads(conn.execute("SELECT payload FROM paper_fills WHERE account = 'A' ORDER BY id LIMIT 1").fetchone()[0])
        qty[cap] = (f["symbol"], f["value"])
    assert qty[4_000_000.0][0] == qty[1_000_000.0][0]
    qty = {k: v[1] for k, v in qty.items()}
    assert qty[4_000_000.0] == pytest.approx(4 * qty[1_000_000.0], rel=0.05)


def test_messages_say_paper_trading(monkeypatch):
    from backend.alerts import messages as msg
    from backend.alerts import report
    store(monkeypatch)
    c = cfg()
    data = tb.load(c)
    paper.start(c, at=data["cal"][-120])
    rep = paper.process(c, data)["report"]
    text = msg.paper_report(rep)
    assert text.startswith("📝 <b>PAPER TRADING</b>") and "simulated, no real orders" in text
    assert "before costs" in text and "A · 200-day filter" in text and "E+ · half filter" in text and "Filter:" in text
    html, _ = report.build(rep["as_of"], [], paper=rep)
    assert "PAPER TRADING" in html


def test_start_reset_and_api(monkeypatch):
    from fastapi.testclient import TestClient
    store(monkeypatch, n_stocks=5)
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        assert tc.get("/api/paper").json()["started"] is False
        assert tc.post("/api/paper/start", json={"capital": -5}).status_code == 422
        r = tc.post("/api/paper/start", json={"capital": 300000}).json()
        assert r["status"] == "ok"
        d = tc.get("/api/paper").json()
        assert d["started"] and set(d["accounts"]) == {"A", "E"}
        assert all(a["account"]["capital"] == 300000 and a["account"]["value"] == 300000 for a in d["accounts"].values())
        assert tc.post("/api/paper/start", json={}).status_code == 409
        assert tc.post("/api/paper/reset").json()["status"] == "ok" and tc.get("/api/paper").json()["started"] is False


def test_an_account_from_before_the_variants_still_works(tmp_path, monkeypatch):
    """A paper account started before A / E (no name, single-account tables) becomes A, and E joins on its day."""
    from backend.database.models import init_db
    store(monkeypatch)
    c = cfg()
    data = tb.load(c)
    s = len(data["cal"]) - 100
    paper.start(c, at=data["cal"][s])
    with get_db() as conn:                                # roll the tables back to the single-account layout
        st = json.loads(conn.execute("SELECT payload FROM paper_state WHERE account = 'A'").fetchone()[0])
        for key in ("name", "variant", "filter"):
            st.pop(key, None)
        conn.execute("DELETE FROM paper_state")
        conn.execute("DROP TABLE paper_state")
        conn.execute("CREATE TABLE paper_state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        conn.execute("INSERT INTO paper_state (id, payload) VALUES (1, ?)", (json.dumps(st),))
        conn.execute("DROP TABLE paper_equity")
        conn.execute("CREATE TABLE paper_equity (day TEXT PRIMARY KEY, equity REAL NOT NULL, gross REAL NOT NULL, nifty REAL)")
        conn.execute("INSERT INTO paper_equity VALUES (?, 200000, 200000, 1)", (data["cal"][s],))
    init_db()
    assert paper.summary(c)["accounts"]["A"]["name"] == "A · 200-day filter, top 200"
    assert paper.process(c, data)["status"] == "ok"
    sm = paper.summary(c)["accounts"]
    assert set(sm) == {"A", "E"} and sm["E"]["start_date"] == sm["A"]["start_date"]
    from backend.engine import attention
    assert attention.signals(c, False)["paper"] is not None


def test_a_universe_change_is_noted_and_keeps_the_positions(monkeypatch):
    store(monkeypatch)
    c = cfg()
    data = tb.load(c)
    paper.start(c, at=data["cal"][-120])
    short = {**data, "cal": data["cal"][:-30], "index_close": data["index_close"][:-30],
             "index_ok": data["index_ok"][:-30], "filters": {k: v[:-30] for k, v in data["filters"].items()}}
    paper.process(c, short)
    c.breakout.top_e = 250
    paper.process(c, data)
    e = paper.summary(c)["accounts"]["E"]
    assert e["top"] == 250 and "top 300 → top 250" in e["notes"][0]["text"] and "top 250" in e["name"]
    assert paper.summary(c)["accounts"]["A"]["top"] == 200 and not paper.summary(c)["accounts"]["A"]["notes"]


def test_verify_tool_runs_read_only(monkeypatch, capsys):
    from backend.tools import verify_e300
    store(monkeypatch)
    c = cfg()
    monkeypatch.setattr("backend.tools.verify_e300.load_config", lambda: c)
    data = tb.load(c)
    paper.start(c, at=data["cal"][-30])
    paper.process(c, data)
    before = paper.summary(c)
    verify_e300.main()
    out = capsys.readouterr().out
    assert "Sample day" in out and "E · half filter, top 300" in out and "paper E:" in out and "real: not started" in out
    assert paper.summary(c) == before                                                  # nothing saved
