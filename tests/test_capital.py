"""The capital is Rs 3,00,000 everywhere (Nov 2026): the defaults, the paper capital box (a change after the start is
money added / withdrawn, never profit), the one-time start-up migration and its API."""

import json

import pytest

from backend.config import AppConfig
from backend.database import models
from backend.database.models import get_db, get_setting, set_setting
from backend.engine import paper, real
from backend.engine import trend_breakout as tb
from tests.test_paper import cfg, store


def test_defaults_are_three_lakh():
    c = AppConfig()
    assert c.breakout.capital == c.paper.capital == c.real.capital == 300_000


# --- paper set_capital -------------------------------------------------------------------------------

def test_paper_capital_before_the_start_is_just_the_setting():
    c = cfg()
    assert paper.set_capital(c, 250_000) == {"status": "ok", "capital": 250_000}
    assert c.paper.capital == 250_000 and paper.summary(c)["capital"] == 250_000
    assert not paper.summary(c)["started"]


def test_paper_capital_after_the_start_is_a_deposit(monkeypatch):
    store(monkeypatch)
    c = cfg()
    data = tb.load(c)
    s = len(data["cal"]) - 200
    paper.start(c, at=data["cal"][s])
    paper.process(c, data)
    before = paper.summary(c)["accounts"]["E"]
    net, cash, value = before["account"]["net"], before["account"]["cash"], before["account"]["value"]
    out = paper.set_capital(c, 300_000)
    assert out["status"] == "ok" and out["delta"] == 100_000 and out["deltas"] == {"E": 100_000}
    assert c.paper.capital == 300_000
    after = paper.summary(c)["accounts"]["E"]
    A = after["account"]
    assert A["capital"] == 300_000 and A["first_capital"] == 200_000 and A["deposited"] == 100_000
    assert A["cash"] == pytest.approx(cash + 100_000) and A["value"] == pytest.approx(value + 100_000)
    assert A["net"] == pytest.approx(net, abs=0.01)                                   # adding money is not profit
    assert A["return"] == pytest.approx(net / 300_000 * 100, abs=0.01)
    assert len(A["deposits"]) == 1 and A["deposits"][0]["amount"] == 100_000 and A["deposits"][0]["capital"] == 300_000
    assert after["deposits"] == A["deposits"]
    assert after["curve"]["net"] == before["curve"]["net"]                           # the curve stays (money added today)
    assert after["curve"]["nifty"] == before["curve"]["nifty"]
    assert after["drawdown"] == before["drawdown"]
    # the stored state carries the deposit; a later equity row includes the cash and the report takes it out again
    with get_db() as conn:
        st = json.loads(conn.execute("SELECT payload FROM paper_state WHERE account = 'E'").fetchone()[0])
        assert st["deposits"] == A["deposits"] and st["cash"] == pytest.approx(cash + 100_000)
        conn.execute("INSERT INTO paper_equity (account, day, equity, gross, nifty) VALUES ('E', '2099-01-01', ?, ?, 1)",
                     (st["cash"] + 1, st["cash"] + 1))
    rep = paper.summary(c)["accounts"]["E"]
    assert rep["curve"]["net"][-1] == pytest.approx(st["cash"] + 1 - 100_000)
    # withdrawing more than the cash is refused and changes nothing
    with pytest.raises(ValueError):
        paper.set_capital(c, 10_000)
    assert paper.summary(c)["accounts"]["E"]["account"]["capital"] == 300_000 and len(A["deposits"]) == 1


def test_paper_capital_api(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        assert tc.post("/api/paper/capital", json={"capital": 5000}).status_code == 422
        out = tc.post("/api/paper/capital", json={"capital": 350000}).json()
        assert out == {"status": "ok", "capital": 350000, "replan": {"status": "not_started"}}   # replan runs after a deposit
        assert tc.get("/api/paper").json()["capital"] == 350000
        with get_db() as conn:                                                       # saved for the next start
            assert json.loads(get_setting(conn, "config.paper"))["capital"] == 350000


# --- the start-up migration ----------------------------------------------------------------------------

def _seed(real_cap=200_000.0, paper_cap=200_000.0):
    with get_db() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (models.CAPITAL_DONE_KEY,))
        set_setting(conn, "config.breakout", json.dumps({"capital": 200000.0, "max_pos": 10}))
        set_setting(conn, "config.paper", json.dumps({"capital": paper_cap, "variants": ["E"]}))
        set_setting(conn, "config.real", json.dumps({"capital": real_cap, "max_order_pct": 15.0}))
        # old Intraday Flow settings (the flow was removed on 20 Nov 2026): left in app_settings, never touched
        set_setting(conn, "flow.nodes", json.dumps({"size": {"capital": 100000, "risk_pct": 1.0}, "orb": {"minutes": 15}}))
        set_setting(conn, "flow.setups", json.dumps({"mine": {"nodes": {"size": {"capital": 200000}}, "saved_at": "x"},
                                                     "tuned": {"nodes": {"size": {"capital": 50000}}, "saved_at": "x"}}))
        conn.execute("INSERT OR REPLACE INTO paper_state (account, payload) VALUES ('E', ?)", (json.dumps(
            {"variant": "E", "name": "E+", "started": "2026-10-01", "capital": paper_cap, "cash": 120_000.0, "charges": 0.0,
             "slippage": 0.0, "as_of": "2026-11-06", "positions": {}, "orders": []}),))
        conn.execute("INSERT INTO candles_daily (instrument_token, date, open, high, low, close, volume) VALUES (1, '2026-11-06', 1, 1, 1, 1, 1)")
    real._save({"started": "2026-10-01", "capital": real_cap, "cash": 50_000.0, "charges": 0.0, "as_of": "2026-11-06",
                "positions": {"AAA": {"qty": 10, "px": 1000.0, "last": 1100.0, "d": "2026-10-02", "stop": 900.0, "charges": 0.0}},
                "orders": [{"action": "BUY", "symbol": "BBB", "ts": "BBB", "close": 500.0, "stop": 460.0, "qty": 40, "why": "x"}]})


def _settings():
    with get_db() as conn:
        out = {k: json.loads(get_setting(conn, k)) for k in ("config.breakout", "config.paper", "config.real", "flow.nodes", "flow.setups")}
        out["real"] = json.loads(conn.execute("SELECT payload FROM real_state WHERE id = 1").fetchone()[0])
        out["paper"] = json.loads(conn.execute("SELECT payload FROM paper_state WHERE account = 'E'").fetchone()[0])
        out["candles"] = conn.execute("SELECT COUNT(*) FROM candles_daily").fetchone()[0]
        out["done"] = get_setting(conn, models.CAPITAL_DONE_KEY)
    return out


def test_migration_bumps_the_old_defaults_once():
    _seed()
    with get_db() as conn:
        changed = models._migrate_capital_300k(conn)
    assert changed == ["breakout.capital", "paper.capital", "real.capital", "Real account +1,00,000 deposit", "Paper E +1,00,000 deposit"]
    s = _settings()
    assert s["config.breakout"] == {"capital": 300000.0, "max_pos": 10}
    assert s["config.paper"]["capital"] == 300000 and s["config.real"]["capital"] == 300000
    assert s["flow.nodes"] == {"size": {"capital": 100000, "risk_pct": 1.0}, "orb": {"minutes": 15}}   # the removed flow: a no-op
    assert s["flow.setups"]["mine"]["nodes"]["size"]["capital"] == 200000
    r = s["real"]
    assert r["capital"] == 300000 and r["cash"] == 150_000 and r["deposits"][0]["amount"] == 100000
    assert r["deposits"][0]["capital"] == 300000 and r["deposits"][0]["at"]
    assert r["orders"][0]["qty"] == 32                                               # re-sized: value 1,61,000 ÷ 10 / (500 x 1.0012)
    p = s["paper"]
    assert p["capital"] == 300000 and p["cash"] == 220_000 and p["deposits"][0]["amount"] == 100000
    assert s["candles"] == 1 and s["done"]
    # the engines' reports see the deposit: value up, profit unchanged
    c = AppConfig()
    assert real.summary(c)["account"]["capital"] == 300000 and real.summary(c)["deposits"] == r["deposits"]
    assert paper.summary(c)["accounts"]["E"]["account"]["deposited"] == 100000
    # a second run is a no-op
    with get_db() as conn:
        assert models._migrate_capital_300k(conn) == []
    assert _settings() == s
    # ... and so is init_db again (the flag is kept)
    models.init_db()
    assert _settings() == s


def test_migration_leaves_values_the_owner_changed():
    _seed(real_cap=250_000.0, paper_cap=500_000.0)
    with get_db() as conn:
        set_setting(conn, "config.breakout", json.dumps({"capital": 150000.0}))
        set_setting(conn, "flow.nodes", json.dumps({"size": {"capital": 75000}}))
        conn.execute("DELETE FROM app_settings WHERE key = 'flow.setups'")
    with get_db() as conn:
        assert models._migrate_capital_300k(conn) == []
    s = _settings_no_setups()
    assert s["config.breakout"] == {"capital": 150000.0} and s["config.real"]["capital"] == 250000
    assert s["config.paper"]["capital"] == 500000 and s["flow.nodes"] == {"size": {"capital": 75000}}
    assert "deposits" not in s["real"] and s["real"]["cash"] == 50_000
    assert "deposits" not in s["paper"] and s["paper"]["capital"] == 500000
    assert s["done"]


def _settings_no_setups():
    with get_db() as conn:
        set_setting(conn, "flow.setups", "{}")
    return _settings()


def test_migration_on_an_empty_db_only_sets_the_flag():
    with get_db() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (models.CAPITAL_DONE_KEY,))
        assert models._migrate_capital_300k(conn) == []
        assert get_setting(conn, models.CAPITAL_DONE_KEY)
        assert conn.execute("SELECT COUNT(*) FROM app_settings WHERE key LIKE 'config.%'").fetchone()[0] == 0


# --- after the Intraday Flow / Futures removal (20 Nov 2026) ---------------------------------------------------------

def test_saved_flow_and_futures_sections_are_ignored_and_the_data_tables_stay():
    """A database from before the removal: config.flow / config.futures (and the flow.* / futures.* keys) stay in
    app_settings and load_config ignores them; the four downloaded tables and the nine book tables stay in the schema
    with their rows (nothing is dropped, init_db again changes nothing)."""
    from dataclasses import asdict
    from backend.config import PERSISTED_SECTIONS, load_config
    with get_db() as conn:
        set_setting(conn, "config.flow", json.dumps({"mode": "signals", "warmup_days": 2}))
        set_setting(conn, "config.futures", json.dumps({"capital": 500000.0, "long_real": True}))
        set_setting(conn, "config.paper", json.dumps({"capital": 310000.0}))
        set_setting(conn, "futures.overlap", json.dumps({"x": 1}))
        conn.execute("INSERT INTO candles_intraday (instrument_token, day, interval, bars, n, source) VALUES (1, '2026-11-02', 1, x'00', 1, 'kite')")
        conn.execute("INSERT INTO candles_fut_daily (underlying, date, open, high, low, close) VALUES ('X', '2026-11-02', 1, 1, 1, 1)")
        conn.execute("INSERT INTO fo_ban (date, symbol) VALUES ('2026-11-02', 'X')")
        conn.execute("INSERT INTO futures_trades (account, payload) VALUES ('paper_long', '{}')")
        conn.execute("INSERT INTO flow_positions (day, symbol, status, payload) VALUES ('2026-11-02', 'X', 'closed', '{}')")
    c = load_config(AppConfig())
    assert not hasattr(c, "flow") and not hasattr(c, "futures") and "flow" not in PERSISTED_SECTIONS and "futures" not in PERSISTED_SECTIONS
    assert c.paper.capital == 310000.0 and asdict(c)
    with pytest.raises(KeyError):
        c.section("futures")
    models.init_db()
    with get_db() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        kept = {k: get_setting(conn, k) for k in ("config.flow", "config.futures", "futures.overlap")}
        rows = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("candles_intraday", "candles_fut_daily", "fo_ban", "futures_trades", "flow_positions")}
    assert {"candles_intraday", "futures_contracts", "candles_fut_daily", "fo_ban"} <= tables          # downloaded data
    assert {"flow_runs", "flow_signals", "flow_positions", "flow_backtests", "futures_runs", "futures_state", "futures_orders",
            "futures_trades", "futures_equity"} <= tables                                                # the books' history
    assert all(kept.values()) and rows == {t: 1 for t in rows}
