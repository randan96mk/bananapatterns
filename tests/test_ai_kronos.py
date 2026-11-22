"""📈 System › AI · Candle model: GET /api/market/status in the off / ready / down states with a fake service (the
in-memory last read and error, one count query, /health cached 30 s), the "Read now" route's guard (off / down → 409,
one a minute → 429, never the order path) and the page's markup (the block under Local AI, the Settings scorecard
reused, not copied)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.market import reads as rd
from tests.test_market_read import HistClient, cfg, fake_result, now_at, seed_index

ROOT = Path(__file__).resolve().parent.parent
KEYS = {"model", "params", "state", "loaded_in_ms", "last_read", "reads_today", "scored_today", "mode", "gate", "settings"}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(rd, "LAST", {"read": None, "error": None})
    monkeypatch.setattr(rd, "_health_cache", {"t": None, "port": None, "v": None})
    monkeypatch.setattr(rd, "_sc_cache", {"key": None, "sc": None})
    monkeypatch.setattr(rd, "_now", lambda: now_at("10:24"))


def fake_health(monkeypatch, reply):
    calls = []

    def h(config, timeout=1.0):
        calls.append(timeout)
        return dict(reply)
    monkeypatch.setattr(rd, "health", h)
    return calls


def svc(last_read=None, last_error=None):
    return SimpleNamespace(snapshot=lambda: {"last_read": last_read, "last_error": last_error})


def test_status_off_never_calls_the_worker(monkeypatch):
    calls = fake_health(monkeypatch, {"state": "ready", "load_s": 1.5})
    s = rd.model_status(cfg(kronos=False), svc(), now_at("10:24"))
    assert KEYS <= set(s) and calls == []
    assert s["state"] == "off" and s["mode"] == "off" and s["gate"] == {"ok": False, "reason": "the candle model is off (Settings › market · kronos)"}
    assert s["model"] == "Kronos-mini" and s["params"] == 4_100_000 and s["loaded_in_ms"] is None and s["last_read"] is None
    assert s["settings"] == {"kronos": False, "kronos_show": "shadow", "interval_s": 60, "samples": 10, "port": 8765}
    assert s["reads_today"] == 0 and s["scored_today"] == 0


def test_status_ready_with_the_service_last_read_and_today_counts(monkeypatch):
    calls = fake_health(monkeypatch, {"state": "ready", "load_s": 1.5, "version": "kronos-mini-v1"})
    seed_index()
    monkeypatch.setattr(rd, "WORKER", fake_result)
    rd.read_index(cfg(), "NIFTY", now_at("10:20"))                                 # two rows (15 and 30 min) today
    rd.read_index(cfg(), "NIFTY", now_at("10:20", "2026-11-18"))                   # an earlier day: not today
    s = rd.model_status(cfg(), svc(last_read={"at": "2026-11-19T10:24:00+05:30", "symbol": "NIFTY", "ms": 2100}), now_at("10:24"))
    assert s["state"] == "ready" and s["loaded_in_ms"] == 1500 and s["error"] is None
    assert s["last_read"] == {"at": "2026-11-19T10:24:00+05:30", "symbol": "NIFTY", "ms": 2100}
    assert s["reads_today"] == 2 and s["scored_today"] == 0
    assert s["mode"] == "shadow" and s["gate"]["ok"] is False and "needs 100" in s["gate"]["reason"] and s["hit"] is None
    rd.model_status(cfg(), svc(), now_at("10:24"))
    assert len(calls) == 1 and calls[0] == 1.0                                     # /health with a 1 s timeout, cached
    s2 = rd.model_status(cfg(), None, now_at("10:24"))                             # no service: the module's in-memory last read
    assert s2["last_read"]["symbol"] == "NIFTY" and s2["last_read"]["ms"] == 412


def test_status_last_read_falls_back_to_the_store_after_a_restart(monkeypatch):
    fake_health(monkeypatch, {"state": "ready", "load_s": 2})
    seed_index()
    monkeypatch.setattr(rd, "WORKER", fake_result)
    rd.read_index(cfg(), "NIFTY", now_at("10:10"))
    s = rd.model_status(cfg(), svc(), now_at("10:24"))
    assert s["last_read"]["symbol"] == "NIFTY" and s["last_read"]["ms"] == 412 and s["last_read"]["at"].startswith("2026-11-19T10:10")


def test_status_cards_set_but_held_shows_the_reason(monkeypatch):
    fake_health(monkeypatch, {"state": "ready", "load_s": 1.5})
    s = rd.model_status(cfg(kronos_show="cards"), svc(), now_at("10:24"))
    assert s["mode"] == "shadow" and s["mode_set"] == "cards" and s["gate"]["reason"].startswith("held in shadow:")


def test_status_down_carries_the_last_error(monkeypatch):
    fake_health(monkeypatch, {"state": "down", "load_s": None})
    s = rd.model_status(cfg(), svc(last_error={"at": "2026-11-19T10:20:00+05:30", "text": "Connection refused"}), now_at("10:24"))
    assert s["state"] == "down" and s["error"] == "Connection refused" and s["loaded_in_ms"] is None
    fake_health(monkeypatch, {"state": "error", "error": "OSError: no weights"})
    rd._health_cache.update(v=None)
    s = rd.model_status(cfg(), svc(), now_at("10:24"))
    assert s["state"] == "down" and s["error"] == "OSError: no weights"


def _client(config, with_service=True):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api import routes
    st = routes.AppState(config, HistClient(has_token=False))
    if with_service:
        st.market = rd.MarketService(st)
    routes.set_state(st)
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app), st


def test_status_route_shape(monkeypatch):
    from backend.api import routes
    fake_health(monkeypatch, {"state": "ready", "load_s": 1.5})
    try:
        tc, _ = _client(cfg(kronos=False))
        a = tc.get("/api/market/status").json()
        assert KEYS <= set(a) and a["state"] == "off" and a["gate"]["ok"] is False
        tc, _ = _client(cfg())
        b = tc.get("/api/market/status").json()
        assert b["state"] == "ready" and b["mode"] == "shadow" and b["loaded_in_ms"] == 1500
    finally:
        routes.set_state(None)


def test_read_now_guard(monkeypatch):
    from backend.api import routes
    seed_index()
    monkeypatch.setattr(rd, "WORKER", fake_result)
    pushed = []
    monkeypatch.setattr(rd, "queue_hint", lambda *a, **kw: pushed.append(a))
    try:
        tc, _ = _client(cfg(kronos=False))
        r = tc.post("/api/market/read", json={})
        assert r.status_code == 409 and "off" in r.json()["detail"]
        tc, _ = _client(cfg(kronos=False), with_service=False)
        assert tc.post("/api/market/read", json={}).status_code == 409
        fake_health(monkeypatch, {"state": "down"})
        tc, _ = _client(cfg())
        r = tc.post("/api/market/read", json={})
        assert r.status_code == 409 and "down" in r.json()["detail"]
        fake_health(monkeypatch, {"state": "ready", "load_s": 1.5})
        rd._health_cache.update(v=None)
        r = tc.post("/api/market/read", json={})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["ok"] and len(j["ids"]) == 2 and j["last_read"]["symbol"] == "NIFTY" and j["status"]["reads_today"] == 2
        r = tc.post("/api/market/read", json={"symbol": "NIFTY"})
        assert r.status_code == 429 and "one read a minute" in r.json()["detail"]
        assert tc.post("/api/market/read", json={"symbol": "FOO"}).status_code == 400
        assert len(rd.rows()) == 2 and pushed == []                                # one read stored; no hint, no order
    finally:
        routes.set_state(None)


def test_read_now_never_touches_the_order_path():
    src = (ROOT / "backend/market/reads.py").read_text()
    body = src[src.index("    def read_now("):src.index("    def start(")]
    for w in ("kite_orders", "kite_auto", "place", "queue_hint", "client."):
        assert w not in body, w
    r = (ROOT / "backend/api/routes.py").read_text()
    route = r[r.index('@router.post("/market/read")'):r.index("class TipsWeeklyNow")]
    assert "svc.read_now(" in route and "kite" not in route.lower()


def test_page_markup_markers():
    page = (ROOT / "frontend/src/components/AiPage.jsx").read_text(encoding="utf-8")
    sp = (ROOT / "frontend/src/components/SettingsPanel.jsx").read_text(encoding="utf-8")
    api = (ROOT / "frontend/src/services/api.js").read_text(encoding="utf-8")
    for w in ("📈 Candle model", "function KronosBlock", "<KronosBlock phone={phone} />", "import { Scorecard } from './SettingsPanel'", "<Scorecard />",
              "api.getMarketStatus()", "api.marketReadNow('NIFTY')", 'href="#/system/settings/market"', "Settings › market", "Read now",
              "○ off (market.kronos off)", "loaded in", "last read", "reads today", "hit rate", "kronos_show", "interval_s"):
        assert w in page, w
    assert page.index("🤖 Local AI") < page.index("<KronosBlock phone={phone} />") < page.index('label="Notes · last night"')
    assert "export function Scorecard()" in sp and sp.count('className="mk-score"') == 1 and "mk-score" not in page   # reused, not copied
    assert "'/api/market/status'" in api and "'/api/market/read'" in api


def test_readme_line():
    t = (ROOT / "README.md").read_text()
    s = t[t.index("## 📈 Market read"):]
    assert "System › AI shows the candle model's state and scorecard" in s[:s.index("\n## ", 5)]
