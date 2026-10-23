from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.config import IST
from backend.database.models import get_db
from backend.kite_api import auth
from tests.conftest import make_series
from tests.test_universe import CSV, INSTRUMENTS, Resp


@pytest.fixture
def api(fake_kite, make_client, monkeypatch):
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("KITE_API_KEY", "key123")
    monkeypatch.setenv("KITE_API_SECRET", "sec")
    from backend import main
    from backend.api import routes

    client = make_client(fake_kite, history_years=1)
    client.config.access_token = ""
    monkeypatch.setattr(main, "KiteClient", lambda kc, ic: client)
    monkeypatch.setattr("backend.ingest.universe.httpx.get", lambda *a, **k: Resp(CSV))
    fake_kite._instruments = INSTRUMENTS
    for tok in (3329, 408065, 268041):
        fake_kite.series[tok] = make_series(date(2025, 10, 1), date.today())
    with TestClient(main.app) as tc:
        yield tc, routes


def test_status_without_login(api):
    tc, _ = api
    s = tc.get("/api/kite/status").json()
    assert s["needs_login"] is True and s["has_token"] is False and s["login_url"]


def test_data_endpoints_require_login(api):
    tc, _ = api
    assert tc.post("/api/universe/refresh").status_code == 401
    assert tc.post("/api/ingest/sync", json={}).status_code == 401


def test_callback_login_then_full_flow(api):
    tc, routes = api
    r = tc.get("/api/kite/callback?request_token=abc&status=success", follow_redirects=False)
    assert r.status_code in (302, 307) and "kite=connected" in r.headers["location"]
    s = tc.get("/api/kite/status?verify=true").json()
    assert s["has_token"] and s["token_source"] == "db" and s["user_id"] == "AB1234"

    u = tc.post("/api/universe/refresh").json()
    assert u["matched"] == 2 and u["unmatched"] == ["GONE"]

    assert tc.post("/api/ingest/sync", json={}).json()["status"] == "started"
    routes._state.worker.join(timeout=30)
    job = tc.get("/api/ingest/status").json()["job"]
    assert job["status"] == "ok" and job["done"] == 3

    rows = tc.get("/api/universe").json()["rows"]
    assert {r["symbol"] for r in rows} == {"NIFTY 500", "ABB", "INFY"}
    assert all(r["bars"] > 200 for r in rows)

    c = tc.get("/api/stock/INFY/candles?limit=50").json()
    assert len(c["candles"]) == 50 and c["industry"] == "Information Technology"
    assert tc.get("/api/stock/NOPE/candles").status_code == 404


def test_token_expires_at_six_while_running(api):
    tc, _ = api
    tc.post("/api/kite/token", json={"access_token": "t1"})
    assert tc.get("/api/kite/status").json()["has_token"]
    with get_db() as conn:  # pretend it was issued before the last 06:00 boundary
        auth.store_token(conn, "t1", auth.last_expiry_boundary() - timedelta(minutes=1))
    assert tc.get("/api/kite/status").json()["needs_login"] is True
    assert tc.post("/api/universe/refresh").status_code == 401


def test_config_roundtrip_and_validation(api):
    tc, _ = api
    assert tc.put("/api/config/ingest", json={"history_years": 5}).json()["history_years"] == 5
    assert tc.get("/api/config/ingest").json()["history_years"] == 5
    assert tc.put("/api/config/ingest", json={"nope": 1}).status_code == 422
    assert tc.get("/api/config/vcp").status_code == 404
