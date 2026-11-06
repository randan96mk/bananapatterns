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


def test_the_account_matches_the_backtest_over_the_same_days(monkeypatch):
    store(monkeypatch)
    c = cfg()
    data = tb.load(c)
    s = len(data["cal"]) - 200
    assert paper.start(c, at=data["cal"][s])["accounts"] == ["E"]                     # E+ is the only account
    res = paper.process(c, data)
    assert res["status"] == "ok" and res["sessions"] == 199
    e = check_account(c, data, s, "E")
    assert e["closed"] >= 5 and e["name"] == "E+ · half filter, top 300, late trail"
    assert set(paper.summary(c)["accounts"]) == {"E"} and paper.summary(c)["variants"] == ["E"]
    t = e["trades"][0]
    assert t["costs"] == pytest.approx(t["charges"] + t["slippage"], abs=0.02)
    assert paper.process(c, data)["sessions"] == 0                                    # the same day again: nothing


def test_a_config_still_listing_variant_a_gives_e_only(monkeypatch):
    """A config file (or saved section) from before A was removed: ["A", "E"] means E only; "A" alone means E."""
    from backend.config import load_config
    from backend.database.models import get_setting, set_setting
    store(monkeypatch)
    c = cfg()
    c.paper.variants = ["A", "E"]
    assert paper.variants(c) == ["E"]
    c.paper.variants = ["A"]
    assert paper.variants(c) == ["E"]
    c.paper.variants = []
    assert paper.variants(c) == ["E"]
    with get_db() as conn:                                # the saved sections of an older install
        set_setting(conn, "config.paper", json.dumps({"capital": 250000.0, "variants": ["A", "E"]}))
        set_setting(conn, "config.breakout", json.dumps({"top": 200, "top_e": 320, "late_at": 0.0, "late_at_e": 90.0,
                                                          "late_pct": 25.0, "late_pct_e": 22.0, "regime_mode": "sma200"}))
        set_setting(conn, "config.real", json.dumps({"variant": "A", "capital": 300000.0}))
    loaded = load_config()
    assert loaded.paper.variants == ["E"] and loaded.paper.capital == 250000.0
    assert (loaded.breakout.top, loaded.breakout.late_at, loaded.breakout.late_pct) == (320, 90.0, 22.0)   # E's own values win
    assert loaded.real.variant == "E" and loaded.real.capital == 300000.0
    with get_db() as conn:                                # saved again without the old keys
        b = json.loads(get_setting(conn, "config.breakout"))
        assert "top_e" not in b and b["top"] == 320 and json.loads(get_setting(conn, "config.paper"))["variants"] == ["E"]
        assert json.loads(get_setting(conn, "config.real"))["variant"] == "E"
    data = tb.load(loaded)
    assert paper.start(loaded, at=data["cal"][-50])["accounts"] == ["E"]
    assert set(paper.summary(loaded)["accounts"]) == {"E"} and paper.summary(loaded)["accounts"]["E"]["top"] == 320


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
            f = json.loads(conn.execute("SELECT payload FROM paper_fills WHERE account = 'E' ORDER BY id LIMIT 1").fetchone()[0])
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
    assert "before costs" in text and "E+ · half filter" in text and "Filter:" in text and "🟣" in text
    assert "200-day filter" not in text and "🔵" not in text and text.count("PAPER TRADING") == 1
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
        assert d["started"] and set(d["accounts"]) == {"E"} and d["variants"] == ["E"]
        assert all(a["account"]["capital"] == 300000 and a["account"]["value"] == 300000 for a in d["accounts"].values())
        assert tc.post("/api/paper/start", json={}).status_code == 409
        assert tc.post("/api/paper/reset").json()["status"] == "ok" and tc.get("/api/paper").json()["started"] is False


def test_upgrade_deletes_paper_a_and_keeps_e(monkeypatch, capsys):
    """The one-time migration: Paper A's rows go from every paper table and the A variant from the saved backtest
    run; E's rows, the candles and everything else stay; a second start finds nothing to do."""
    from backend.database.models import init_db
    store(monkeypatch)
    c = cfg()
    data = tb.load(c)
    paper.start(c, at=data["cal"][-60])
    paper.process(c, data)
    tables = ("paper_state", "paper_trades", "paper_fills", "paper_equity")
    with get_db() as conn:                                # an A account next to E, as before the upgrade
        for t in tables:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})") if r[1] != "id"]
            names = ", ".join(cols)
            picks = ", ".join("'A'" if col == "account" else col for col in cols)
            conn.execute(f"INSERT INTO {t} ({names}) SELECT {picks} FROM {t} WHERE account = 'E'")
        conn.execute("INSERT INTO breakout_runs (id, updated_at, payload) VALUES (1, 'x', ?)",
                     (json.dumps({"variants": {"A": {"top": 200}, "E": {"top": 300}}, "stale": ["A"], "sessions": 5}),))
        before = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE account = 'E'").fetchone()[0] for t in tables}
        assert all(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE account = 'A'").fetchone()[0] == before[t] > 0 for t in tables)
        candles = conn.execute("SELECT COUNT(*) FROM candles_daily").fetchone()[0]
    init_db()
    out = capsys.readouterr().out
    assert "Trend Breakout A removed" in out and "paper_trades" in out and "saved backtest run" in out
    with get_db() as conn:
        assert all(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE account = 'A'").fetchone()[0] == 0 for t in tables)
        assert {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE account = 'E'").fetchone()[0] for t in tables} == before
        run = json.loads(conn.execute("SELECT payload FROM breakout_runs WHERE id = 1").fetchone()[0])
        assert run == {"variants": {"E": {"top": 300}}, "sessions": 5}
        assert conn.execute("SELECT COUNT(*) FROM candles_daily").fetchone()[0] == candles
    assert set(paper.summary(c)["accounts"]) == {"E"} and paper.summary(c)["accounts"]["E"]["closed"] == before["paper_trades"]
    init_db()                                             # again: nothing to do, nothing said
    assert "Trend Breakout A removed" not in capsys.readouterr().out
    from backend.engine import attention
    assert attention.signals(c, False)["paper"] is not None


def test_an_account_from_before_the_variants_is_dropped(tmp_path, monkeypatch):
    """A paper account started before the variants (no name, single-account tables) was A's rules: it becomes A
    and is then deleted by the upgrade, so paper trading is not started; E starts afresh."""
    from backend.database.models import init_db
    store(monkeypatch)
    c = cfg()
    data = tb.load(c)
    s = len(data["cal"]) - 100
    paper.start(c, at=data["cal"][s])
    with get_db() as conn:                                # roll the tables back to the single-account layout
        st = json.loads(conn.execute("SELECT payload FROM paper_state WHERE account = 'E'").fetchone()[0])
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
    assert paper.summary(c)["started"] is False and paper.process(c, data)["status"] == "not_started"
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_equity").fetchone()[0] == 0
    assert paper.start(c, at=data["cal"][s])["accounts"] == ["E"]
    assert set(paper.summary(c)["accounts"]) == {"E"}


def test_a_universe_change_is_noted_and_keeps_the_positions(monkeypatch):
    store(monkeypatch)
    c = cfg()
    data = tb.load(c)
    paper.start(c, at=data["cal"][-120])
    short = {**data, "cal": data["cal"][:-30], "index_close": data["index_close"][:-30],
             "index_ok": data["index_ok"][:-30], "filters": {k: v[:-30] for k, v in data["filters"].items()}}
    paper.process(c, short)
    c.breakout.top = 250
    paper.process(c, data)
    e = paper.summary(c)["accounts"]["E"]
    assert e["top"] == 250 and "top 300 → top 250" in e["notes"][0]["text"] and "top 250" in e["name"]


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
