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
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
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
    for tok in (3329, 408065, 3, 268041):
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
    assert u["matched"] == 3 and u["unmatched"] == ["GONE"] and u["excluded"] == ["EMBASSY"]

    assert tc.post("/api/ingest/sync", json={}).json()["status"] == "started"
    routes._state.worker.join(timeout=30)
    job = tc.get("/api/ingest/status").json()["job"]
    assert job["status"] == "ok" and job["done"] == 4

    rows = tc.get("/api/universe").json()["rows"]
    assert {r["symbol"] for r in rows} == {"NIFTY 500", "ABB", "INFY", "HFCL"}
    assert next(r for r in rows if r["symbol"] == "HFCL")["tradingsymbol"] == "HFCL-BE"
    assert all(r["bars"] > 200 for r in rows)

    c = tc.get("/api/stock/INFY/candles?limit=50").json()
    assert len(c["candles"]) == 50 and c["industry"] == "Information Technology"
    assert len(tc.get("/api/stock/HFCL/candles?limit=5").json()["candles"]) == 5
    assert tc.get("/api/stock/NOPE/candles").status_code == 404

    summ = tc.get("/api/screens/bases/summary").json()      # sync finished -> scan ran
    assert summ["as_of"] == summ["latest_bar"] and summ["stale"] is False
    assert set(summ["counts"]) >= {"forming", "fresh", "climbing", "played_out"}
    for stage in ("forming", "fresh", "climbing", "played_out", "fresh_low_volume"):
        res = tc.get(f"/api/screens/bases/results?stage={stage}")
        assert res.status_code == 200 and all(r["symbol"] for r in res.json()["rows"])
    assert tc.get("/api/screens/bases/results?stage=nope").status_code == 422
    for stage in ("forming", "fresh", "climbing", "played_out"):
        rows = tc.get(f"/api/screens/bases/results?stage={stage}").json()["rows"]
        sp = tc.get(f"/api/screens/bases/sparks?stage={stage}&bars=60").json()["sparks"]
        assert set(sp) == {r["symbol"] for r in rows}
        for r in rows:
            assert r["base_start"] <= r["base_end"] and r["base_low"] <= r["pivot"]
            s = sp[r["symbol"]]
            assert len(s["d"]) == len(s["c"]) == len(s["ma50"]) <= 60 and s["ma50"][-1] is not None
    assert tc.get("/api/screens/bases/sparks?stage=nope").status_code == 422
    assert tc.get("/api/screens/nope/summary").status_code == 404
    assert tc.get("/api/changes").json()["as_of"] == summ["as_of"]
    assert tc.post("/api/screens/scan").json()["as_of"] == summ["as_of"]

    m = tc.get("/api/stock/INFY/metrics").json()
    assert m["as_of"] == summ["as_of"] and 1 <= m["rs"] <= 99 and len(m["trend"]["checks"]) == 8
    assert tc.get("/api/stock/NOPE/metrics").json()["as_of"] is None
    rows = tc.get("/api/universe").json()["rows"]
    assert all(r["rs"] is not None for r in rows if r["kind"] == "stock")

    v = tc.get("/api/stock/INFY/vcp").json()
    assert v["vcp"] is None or {"qualifies", "fails", "contractions", "tightening"} <= set(v["vcp"])
    for sym in ("INFY", "ABB", "HFCL"):
        img = tc.get(f"/api/stock/{sym}/alert-chart.png")
        assert img.status_code in (200, 404)
        if img.status_code == 200:
            assert img.content[:4] == b"\x89PNG" and img.headers["x-alert-kind"] != "auto"
    assert tc.get("/api/stock/INFY/alert-chart.png?kind=nope").status_code == 422
    vs = tc.get("/api/screens/vcp/summary").json()
    assert vs["as_of"] == summ["as_of"] and set(vs["counts"]) >= {"forming", "fresh", "climbing", "played_out"}
    assert tc.get("/api/changes?screen=vcp").json()["screen"] == "vcp"

    b = tc.get("/api/stock/INFY/bases").json()
    assert b["as_of"] and isinstance(b["bases"], list) and b["config"]["min_base_bars"] == 5
    assert tc.get("/api/stock/NOPE/bases").status_code == 404


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
    assert tc.get("/api/config/nope").status_code == 404
    t = tc.put("/api/config/trend", json={"min_rs": "80", "rs_weights": ["0.25", "0.25", "0.25", "0.25"],
                                          "rs_periods": ["63", "126", "189", "252"]}).json()
    assert t["min_rs"] == 80 and t["rs_weights"] == [0.25] * 4 and t["rs_periods"] == [63, 126, 189, 252]
    r = tc.put("/api/config/bases", json={"reset_on_undercut": "false", "vol_baseline": "median20"}).json()
    assert r["reset_on_undercut"] is False and r["vol_baseline"] == "median20"


def test_server_host_from_env(monkeypatch):
    from backend.config import ServerConfig
    monkeypatch.delenv("BP_HOST", raising=False)
    assert ServerConfig().host == "127.0.0.1"                          # the Mac: this computer only
    monkeypatch.setenv("BP_HOST", "0.0.0.0")
    assert ServerConfig().host == "0.0.0.0"                            # Docker: reachable through the port map
