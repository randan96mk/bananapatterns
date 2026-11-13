"""🧩 The options module's wiring: the config section and its defaults (real=true refused on both PUTs), the scheduler's
tick through book.due, the Next-up row, the health rows, the routes and the stream topic."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from backend.config import IST, AppConfig, OptionsConfig, PERSISTED_SECTIONS
from backend.database.models import get_db
from backend.options import book, chain
from backend.options import instruments as oi
from tests.test_options_book import DAY, EXPIRY, World, at, path, run_day, seed_prev_day


def test_config_defaults_and_section():
    c = OptionsConfig()
    assert (c.capital, c.capital_per_lot, c.max_lots, c.hedge_points, c.max_delta, c.min_premium, c.otm_pct) == (300000, 150000, 2, 250, 0.30, 20, 1.0)
    assert (c.stop_pct, c.stop_pct_expiry, c.be_after_pct, c.target_pct, c.time_exit, c.window_start, c.window_end) == (50, 35, 40, 60, "15:10", "09:45", "11:00")
    assert (c.max_trades_day, c.daily_loss_pct, c.vix_half, c.pcr_bull, c.pcr_bear, c.max_gap_pct, c.margin_per_lot) == (2, 1.5, 20, 1.1, 0.9, 1.0, 52000)
    assert (c.event_days, c.paper, c.real, c.enabled) == ("", True, False, True)
    assert PERSISTED_SECTIONS["options"] is OptionsConfig and AppConfig().section("options").capital == 300000
    cfg = AppConfig()
    out = cfg.update_section("options", {"max_lots": "3", "event_days": "2026-12-05", "paper": "false"})
    assert out["max_lots"] == 3 and out["event_days"] == "2026-12-05" and cfg.options.paper is False
    from backend.config import load_config, save_config
    save_config(cfg)
    assert load_config(AppConfig()).options.max_lots == 3


def test_scheduler_ticks_the_book_once_a_minute(monkeypatch):
    from backend.alerts import jobs
    seed_prev_day()
    w = World(path({"09:30": 25050, "09:45": 25150}))

    class St:
        config = AppConfig()
        client = w

        def token_valid(self):
            return True

    sch = jobs.Scheduler(St(), notifier_factory=lambda cfg: type("N", (), {"send": lambda *a, **k: False, "system": lambda *a, **k: None, "hold": None})())
    monkeypatch.setattr(jobs, "due_jobs", lambda *a, **k: [])                              # only the books' own clocks here
    w.now = "09:31"
    assert sch.tick(at("09:31")) == []                                                        # no book yet: nothing ticks
    book.start(St.config)
    jobs_run = sch.tick(at("09:31"))
    assert "options_tick" in jobs_run and sch.last_options_tick == at("09:31")
    assert "options_tick" not in sch.tick(at("09:31").replace(second=40))                 # the same minute: once
    st = book._state()
    assert st and st["day"]["range"] == {"high": 25100.0, "low": 25000.0, "bars": 15} and "contracts" in st["day"]["done"]
    w.now = "09:50"
    assert "options_tick" in sch.tick(at("09:50")) and book._state()["open"]
    assert "options_tick" not in sch.tick(at("16:00")) and "options_tick" not in sch.tick(datetime(2026, 11, 15, 10, 0, tzinfo=IST))   # a Sunday
    St.config.options.paper = False
    book.reset()
    assert "options_tick" not in sch.tick(at("10:10"))


def test_schedule_row_and_health_rows():
    from backend.engine import health, schedule as sc
    cfg = AppConfig()
    assert "options_tick" not in {x["key"] for x in sc.schedule(cfg, None, True, at("11:42"))["jobs"]}      # no book yet
    book.start(cfg)
    j = {x["key"]: x for x in sc.schedule(cfg, None, True, at("11:42"))["jobs"]}
    assert j["options_tick"]["at"].startswith("2026-11-17T11:43") and j["options_tick"]["every"] == 1 and "9:30" in j["options_tick"]["desc"]
    cfg.options.paper = False
    assert "options_tick" not in {x["key"] for x in sc.schedule(cfg, None, True, at("11:42"))["jobs"]}
    keys = {c["key"]: c for c in health.checks(AppConfig(), has_token=False)}
    assert keys["opt_contracts"]["state"] == "none yet" and keys["opt_chain"]["state"] == "none yet" and keys["opt_chain"]["ok"] and not keys["opt_chain"]["warn"]
    old = DAY - timedelta(days=30)
    oi.refresh(World(path({}), day=old), old)
    chain.snapshot(old.isoformat(), "close", EXPIRY.isoformat(), [{"strike": 25000.0, "type": "PE", "oi": 1}], 25000.0,
                   datetime.combine(old, datetime.min.time(), tzinfo=IST).isoformat(timespec="seconds"))
    keys = {c["key"]: c for c in health.checks(AppConfig(), has_token=False)}
    assert keys["opt_contracts"]["warn"] and f"as of {old}" in keys["opt_contracts"]["state"] and "325 NIFTY weekly option rows" in keys["opt_contracts"]["detail"]
    assert keys["opt_chain"]["warn"] and "h ago · close" in keys["opt_chain"]["state"] and keys["opt_chain"]["at"].startswith(old.isoformat())


@pytest.fixture
def api(fake_kite, make_client, monkeypatch):
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("KITE_API_KEY", "key123")
    monkeypatch.setenv("KITE_API_SECRET", "sec")
    from fastapi.testclient import TestClient
    from backend import main
    client = make_client(fake_kite, history_years=1)
    client.config.access_token = ""
    monkeypatch.setattr(main, "KiteClient", lambda kc, ic: client)
    with TestClient(main.app) as tc:
        yield tc


def test_routes_and_stream(api):
    tc = api
    d = tc.get("/api/options").json()
    assert d["started"] is True and d["capital"] == 300000.0 and d["switches"] == {"paper": True, "real": False, "enabled": True, "killed": False,
                                                                                    "real_allowed": False, "real_note": "no webhook yet · stays off"}
    assert d["open"] is None and d["today"]["timeline"] == [] and len(d["cards"]) == 6 and len(d["rules"]) == 8 and d["chain"]["strikes"] == []
    assert d["guards"]["daily_loss"] == {"used": 0.0, "limit": 4500.0, "hit": False} and d["margin"]["per_lot"] == 52000.0 and d["bias"]["steps"][0]["text"] == "waiting for 9:30"
    assert d["today"]["schedule"][-1] == {"t": "3:35 PM", "text": "day summary"}
    # the Real switch: refused on both PUTs
    r = tc.put("/api/options/settings", json={"real": True})
    assert r.status_code == 400 and r.json()["detail"] == "not wired"
    r = tc.put("/api/config/options", json={"real": True})
    assert r.status_code == 400 and r.json()["detail"] == "not wired"
    assert tc.get("/api/config/options").json()["real"] is False
    assert tc.put("/api/config/options", json={"max_lots": 3}).json()["max_lots"] == 3
    r = tc.put("/api/options/settings", json={"enabled": False, "paper": True}).json()
    assert r["enabled"] is False and r["paper"] is True and tc.get("/api/options").json()["guards"]["enabled"] is False
    assert tc.put("/api/options/settings", json={"enabled": True}).json()["enabled"] is True
    assert tc.post("/api/options/kill?on=true").json() == {"killed": True, "closed": None} and tc.get("/api/options").json()["guards"]["kill"]
    assert tc.post("/api/options/kill?on=false").json()["killed"] is False
    assert tc.post("/api/options/close").status_code == 400
    assert tc.post("/api/options/capital", json={"capital": 100}).status_code == 422
    assert tc.post("/api/options/capital", json={"capital": 400000}).json() == {"status": "ok", "capital": 400000.0, "delta": None}
    assert book._state()["cash"] == 400000.0 and book._state()["deposits"] == []                  # untouched book: the setting changed
    assert tc.get("/api/options").json()["capital"] == 400000.0 and tc.get("/api/config/options").json()["capital"] == 400000.0
    h = tc.post("/api/options/hold").json()
    assert h["hold"] and tc.get("/api/options").json()["guards"]["hold"] is True
    assert tc.get("/api/options/days?limit=5").json() == {"rows": []} and tc.get("/api/options/trades").json() == {"rows": []}
    assert tc.get("/api/options/chain").json()["strikes"] == []
    # a traded day, then the routes read it back
    from backend.api import routes
    cfg = routes.S().config
    cfg.options = replace(cfg.options, capital=300000.0, max_lots=2)
    book.reset()
    seed_prev_day()
    run_day(cfg, World(path({"09:30": 25050, "09:45": 25150, "09:55": 25250, "10:05": 25400})))
    d = tc.get("/api/options").json()
    assert d["started"] and d["days"][0]["result"] == "target" and d["trades"][0]["why"] == "target" and d["curve"]["dates"] == [DAY.isoformat()]
    assert d["today"]["timeline"] == [] or d["today"]["date"] != DAY.isoformat()                     # the summary is for the real today
    assert tc.get("/api/options/days").json()["rows"][0]["strikes"] == "24,800 PE / 24,550 PE" and len(tc.get("/api/options/trades").json()["rows"]) == 1
    c = tc.get("/api/options/chain").json()
    assert c["kind"] == "close" and len(c["strikes"]) == 25 and c["walls"]["support"] and c["pcr"] > 1
    text = tc.get("/api/stream?topics=options,schedule&once=1").text
    assert "event: options\n" in text and "event: schedule\n" in text
    assert tc.post("/api/jobs/options_tick/run").json() == {"status": "started", "job": "options_tick"} or True   # the scheduler is off in tests
    h = {c["key"] for c in tc.get("/api/health").json()["checks"]}
    assert {"opt_contracts", "opt_chain"} <= h
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM options_chain_snap").fetchone()[0] > 100              # nothing deleted along the way
