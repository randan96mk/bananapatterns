"""Sample (TEST) alerts and the bot check."""

import httpx
from fastapi.testclient import TestClient

from backend.alerts.dispatch import Notifier
from backend.alerts.samples import SAMPLE_KINDS, build_samples, send_samples
from backend.alerts.telegram import TelegramClient
from backend.config import AlertsConfig, AppConfig, BaseConfig
from backend.database.models import get_db
from tests.test_alerts import FakeTG, scanned_db


def config(**alerts):
    c = AppConfig()
    c.alerts = AlertsConfig(chat_id="42", **alerts)
    c.bases = BaseConfig(reset_ma=0)
    return c


def test_every_kind_builds_with_empty_db():
    samples = build_samples("all", config(), {})
    assert [k for k, _, _ in samples] == SAMPLE_KINDS
    assert all(html.startswith("🧪 <b>TEST</b> — ") for _, html, _ in samples)
    charts = {k for k, _, png in samples if png}
    assert charts == {"breakout", "near_pivot", "forming", "below_pivot", "played_out", "intraday",
                      "close_check"}                                                 # decision charts
    assert all(png[:4] == b"\x89PNG" for _, _, png in samples if png)


def test_samples_use_real_scan_stocks():
    as_of, tokens = scanned_db()
    samples = dict((k, html) for k, html, _ in build_samples("all", config(), tokens))
    assert "FRESH" in samples["breakout"] and "NEAR" in samples["near_pivot"]
    assert "EXAMPLE" not in samples["breakout"]
    rep = samples["report"]
    assert "Evening report" in rep and "Breakouts (1)" in rep and "FRESH" in rep and "buttons appear" in rep


def test_samples_are_logged_as_test_and_do_not_block_real_alerts():
    as_of, tokens = scanned_db()
    fake = FakeTG()
    res = send_samples("breakout", config(), tokens, "42", fake)
    assert res == [{"kind": "breakout", "ok": True, "error": None, "chart": True}]
    send_samples("breakout", config(), tokens, "42", fake)          # can repeat at will
    assert len(fake.sent) == 2
    with get_db() as conn:
        kinds = [r[0] for r in conn.execute("SELECT kind FROM sent_alerts")]
    assert kinds == ["test", "test"]
    n = Notifier(AlertsConfig(chat_id="42"), FakeTG())
    assert not n.already_sent(f"{as_of}|bases|FRESH|breakout")      # the real alert can still go out


def test_bad_token_stops_after_first_failure():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    client = TelegramClient(token="bad", http=httpx.Client(transport=httpx.MockTransport(handler)),
                            sleep=lambda s: None)
    res = send_samples("all", config(chart_images=False), {}, "42", client)
    assert len(res) == 1 and not res[0]["ok"] and "Unauthorized" in res[0]["error"] and len(calls) == 1


def test_api_sample_and_bot(monkeypatch):
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    from backend import main
    with TestClient(main.app) as tc:
        assert tc.get("/api/alerts/bot").json() == {"configured": False, "ok": False,
                                                     "error": "TELEGRAM_BOT_TOKEN is not set in .env"}
        assert tc.post("/api/alerts/sample", json={"kind": "all"}).status_code == 400
        assert tc.post("/api/alerts/sample", json={"kind": "nope"}).status_code == 422
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
        fake = FakeTG()
        monkeypatch.setattr("backend.alerts.dispatch.TelegramClient", lambda: fake)
        fake.get_me = lambda: {"username": "bp_bot", "first_name": "BP"}
        assert tc.get("/api/alerts/bot").json()["username"] == "bp_bot"
        r = tc.post("/api/alerts/sample", json={"kind": "system"}).json()
        assert r["sent"] == 1 and r["failed"] == 0 and fake.sent[0][1].startswith("🧪")
