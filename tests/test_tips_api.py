"""💡 API: GET /api/tips (the contract keys), hints / feed / record, take / skip / exit / trail / pause / resume with
404 / 409 / 400, PUT settings (422 on bad keys), POST /api/tips/import (multipart), POST /api/tips/test-post, the
login steps' refusals, the stream topic `tips`, the health rows."""

import pytest

from backend.config import AppConfig
from backend.engine import health
from tests.tips_world import BN12, KEY12, cfg, seed, world

SUMMARY_KEYS = {"enabled", "reader", "channels", "cards", "hints", "feed", "record", "book", "settings", "quotes_at", "any_open", "at", "kite"}
HINT_KEYS = {"id", "channel", "post_id", "posted_at", "time", "day", "day_label", "raw", "title", "symbol", "side", "instrument", "strike",
             "opt_type", "expiry", "expiry_label", "expiry_src", "tradingsymbol", "exchange", "lot_size", "entry", "entry_text", "stop",
             "stop_text", "targets", "targets_hit", "targets_text", "open_ended", "size_hint", "decoded", "plan", "status", "ended", "taken",
             "lots", "entry_px", "exit_px", "pnl", "ltp", "events", "tracked", "untracked_why", "score", "position", "actions", "telegram", "source", "kite", "shot", "now"}


@pytest.fixture
def api(fake_kite, make_client, monkeypatch):
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("TIPS_TG_SESSION", "")
    from fastapi.testclient import TestClient
    from backend import main
    client = make_client(fake_kite, history_years=1)
    client.config.access_token = ""
    monkeypatch.setattr(main, "KiteClient", lambda kc, ic: client)
    with TestClient(main.app) as tc:
        yield tc


def test_summary_and_hint_keys(api):
    seed(cfg(), world(), BN12)
    d = api.get("/api/tips").json()
    assert set(d) == SUMMARY_KEYS
    assert set(d["reader"]) >= {"state", "account_masked", "last_post_at", "channels", "lag_ms"} and d["reader"]["state"] == "logged_out"
    assert set(d["hints"][0]) == HINT_KEYS and d["hints"][0]["title"] == "BUY BANKNIFTY 50300 PE"
    assert set(d["book"]) >= {"positions", "trades", "equity", "capital"} and d["book"]["capital"] == 100000.0
    assert set(d["record"]) >= {"by_instrument", "by_hour", "totals", "untracked"}
    rows = api.get("/api/tips/hints", params={"status": "new,waiting", "days": 3650}).json()["rows"]
    assert [r["id"] for r in rows] == [1]
    assert api.get("/api/tips/hints/1").json()["posts"][0]["kind"] == "hint"
    assert api.get("/api/tips/feed", params={"kind": "hint"}).json()["rows"][0]["label"] == "hint #1"
    assert "totals" in api.get("/api/tips/record", params={"days": 7}).json()


def test_actions_and_refusals(api):
    seed(cfg(), world(**{KEY12: 300.0}), BN12)
    assert api.post("/api/tips/hints/99/take", json={}).status_code == 404
    assert api.get("/api/tips/hints/99").status_code == 404
    r = api.post("/api/tips/hints/1/take", json={"lots": 2})
    assert r.status_code == 200 and r.json()["position"]["lots"] == 2 and r.json()["taken"]
    assert api.post("/api/tips/hints/1/take", json={}).status_code == 409
    assert api.post("/api/tips/hints/1/skip").status_code == 409
    assert api.post("/api/tips/hints/1/trail", json={"to": -5}).status_code == 400
    assert api.post("/api/tips/hints/1/pause").json()["status"] == "paused"
    assert api.post("/api/tips/hints/1/resume").json()["status"] in ("new", "waiting")
    assert api.post("/api/tips/hints/1/exit").json()["position"]["state"] == "closed"
    assert api.post("/api/tips/hints/1/exit").status_code == 409
    seed(cfg(), world(), "Buy bank nifty 50300 PE at 260 add more at 240", "10:00", 2)
    assert api.post("/api/tips/hints/2/take", json={}).status_code == 400
    assert api.post("/api/tips/hints/2/take", json={"lots": 0}).status_code == 400


def test_settings(api):
    r = api.put("/api/tips/settings", json={"channels": "@aakankshatrading, other_channel", "lots": 2, "size_mode": "rupees"})
    assert r.status_code == 200 and r.json()["channels"] == "aakankshatrading;other_channel" and r.json()["lots"] == 2
    assert api.get("/api/config/tips").json()["size_mode"] == "rupees"
    assert api.put("/api/tips/settings", json={"nope": 1}).status_code == 422
    assert api.put("/api/tips/settings", json={"size_mode": "shares"}).status_code == 422
    assert api.put("/api/tips/settings", json={"intraday_time_stop": "25:00"}).status_code == 422
    assert api.put("/api/tips/settings", json={"channels": "bad channel!"}).status_code == 422
    r = api.put("/api/tips/settings", json={"channels": "Index Options, Buying with Aakanksha\n1002233445566"})   # a title with a comma, an id
    assert r.status_code == 200 and r.json()["channels"] == "Index Options, Buying with Aakanksha;1002233445566"
    src = open("frontend/src/components/TipsPage.jsx", encoding="utf-8").read()
    assert "v.split(/[;\\n]+/)" in src and "<textarea" in src           # the dialog: one channel per line (a title keeps its commas)


def test_import_test_post_and_login_refusals(api, monkeypatch):
    from tests.test_tips_reader import EXPORT
    r = api.post("/api/tips/import", params={"channel": "aakankshatrading"},
                 files=[("files", ("messages.html", EXPORT.encode(), "text/html"))])
    assert r.status_code == 200 and r.json()["posts"] == 4 and r.json()["hints"] == 1
    assert api.post("/api/tips/import", content=b"").status_code == 400
    t = api.post("/api/tips/test-post", json={"text": "Buy CRUDE 5300 PE NEAR 267 AND 263\n\nSL 250\nTGT 275-285-300"}).json()
    assert t["kind"] == "hint" and t["decoded"]["title"] == "BUY CRUDE 5300 PE" and t["decoded"]["exchange"] == "MCX"
    assert t["plan"]["ok"] and "Tgt 275 / 285 / 300" in t["telegram"]
    f = api.post("/api/tips/test-post", json={"text": "2 targets done ✅"}).json()
    assert f["kind"] == "follow_up?" and f["followup"]["action"] == "targets"
    assert api.post("/api/tips/test-post", json={"text": " "}).status_code == 400
    monkeypatch.delenv("TIPS_TG_API_ID", raising=False)
    assert api.post("/api/tips/login", json={"phone": "+919876543210"}).status_code == 400
    assert api.post("/api/tips/login/code", json={"code": "12345"}).status_code == 409


def test_stream_topic_and_health_rows(api):
    body = api.get("/api/stream", params={"topics": "tips", "once": 1}).text
    assert "event: tips" in body
    rows = {c["key"]: c for c in health.checks(AppConfig(), has_token=False)}
    assert rows["tips_reader"]["ok"] and rows["tips_reader"]["state"].startswith("Tips off · ")
    assert rows["tips_quotes"]["state"] == "nothing open"
