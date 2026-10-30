"""⏱ Next up: the schedule follows the settings and trading days, interval jobs step from their window, the market
state, and the API."""

from datetime import datetime

from backend.config import IST, AppConfig
from backend.engine import schedule as sc


def at(d, h, m):
    return datetime(2026, 11, d, h, m, tzinfo=IST)


def keys(r):
    return [j["key"] for j in r["jobs"]]


def test_market_hours_on_a_trading_day():
    r = sc.schedule(AppConfig(), None, True, at(3, 11, 42))                # Tuesday
    assert r["market"]["open"] and r["market"]["closes_at"].endswith("15:30:00+05:30")
    j = {x["key"]: x for x in r["jobs"]}
    assert j["intraday"]["at"].startswith("2026-11-03T11:45") and j["intraday"]["every"] == 5   # 9:20 + 5-min steps
    assert j["eod"]["at"].startswith("2026-11-03T18:30") and j["reminder"]["skip"] == "logged in"
    assert r["jobs"] == sorted(r["jobs"], key=lambda x: x["at"])


def test_weekend_moves_to_monday():
    r = sc.schedule(AppConfig(), None, False, at(1, 10, 0))                # Sunday
    j = {x["key"]: x for x in r["jobs"]}
    assert not r["market"]["open"] and r["market"]["opens_at"].startswith("2026-11-02T09:15")
    assert j["eod"]["at"].startswith("2026-11-02T18:30") and j["filings"]["at"].startswith("2026-11-01T10:00")
    assert j["kite_expiry"]["skip"] == "not logged in"


def test_settings_switch_jobs_off():
    c = AppConfig()
    c.alerts.intraday = False
    c.alerts.filings = False
    assert "intraday" not in keys(sc.schedule(c, None, True, at(3, 11, 0))) and "real_stops" not in keys(sc.schedule(c, None, True, at(3, 11, 0)))


def test_last_run_steps_the_interval():
    class Sch:
        last_intraday = at(3, 11, 41)
        last_filings = None
        last_real_stops = None
    j = {x["key"]: x for x in sc.schedule(AppConfig(), Sch(), True, at(3, 11, 42))["jobs"]}
    assert j["intraday"]["at"].startswith("2026-11-03T11:46")


def test_api(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        d = tc.get("/api/schedule").json()
        assert {"now", "market", "jobs", "kite", "real"} <= set(d) and d["jobs"] and d["real"]["variant"] == "E"
