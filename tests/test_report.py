"""Summary alert style: the evening's alerts held in alert_outbox, one silent evening report with buttons, the buttons
(an album of charts + action buttons, the full texts), multi-row keyboards and albums in the Telegram client."""

import json
from datetime import datetime, timedelta

from backend.alerts import report
from backend.alerts.dispatch import Notifier, dispatch_scan
from backend.alerts.jobs import due_jobs
from backend.alerts.telegram import TelegramClient, keyboard
from backend.config import IST, AlertsConfig, BaseConfig
from backend.database.models import get_db
from tests.test_alerts import FakeTG, scanned_db


def cfg():
    return AlertsConfig(chat_id="42", tiering=False, screens=["bases"])


def test_holding_stores_instead_of_sending():
    fake = FakeTG()
    n = Notifier(cfg(), fake, hold="2026-03-02")
    assert n.send("k1", "breakout", "🚀 <b>X</b>", "X", meta={"tier": "A"}) and fake.sent == []
    assert n.already_sent("k1") and not n.send("k1", "breakout", "again", "X")         # not held twice
    assert n.send("s1", "system", "⚙️ sync failed") and fake.sent == [("msg", "⚙️ sync failed")]   # never held
    rows = report.held("2026-03-02")
    assert [(r["key"], r["kind"], r["meta"]["tier"]) for r in rows] == [("k1", "breakout", "A")]


def test_evening_report_from_the_held_alerts():
    as_of, tokens = scanned_db()
    fake = FakeTG()
    sent = dispatch_scan(as_of, cfg(), BaseConfig(reset_ma=0), Notifier(cfg(), fake, hold=as_of), tokens)
    assert sent["stage"] >= 1 and fake.sent == []                                         # all held
    items = [{"symbol": "NEAR", "tier": "A", "pivot": 100.0, "dist_to_pivot_pct": 1.2, "stop": 95.0}]
    html, buttons = report.build(as_of, items, "<i>Market uptrend</i>")
    assert "Evening report" in html and "Breakouts (1)" in html and "<b>FRESH</b>" in html
    assert "Top 1 for the next session" in html and "<b>NEAR</b>" in html
    assert [b[2] for b in buttons] == [f"r:b:{as_of}", f"r:t:{as_of}", f"r:d:{as_of}"]
    assert len(html) < 4096
    assert report.send(Notifier(cfg(), fake), as_of, items) and fake.silent and len(fake.sent) == 1
    assert not report.send(Notifier(cfg(), fake), as_of, items)                          # once a day


def test_quiet_day_report():
    html, buttons = report.build("2026-03-02", [])
    assert "A quiet day" in html and buttons == []


def test_buttons_send_an_album_and_the_details(monkeypatch):
    as_of, tokens = scanned_db()
    dispatch_scan(as_of, cfg(), BaseConfig(reset_ma=0), Notifier(cfg(), FakeTG(), hold=as_of), tokens)
    monkeypatch.setattr("backend.alerts.preview.alert_chart", lambda config, sym, kind="auto": (b"png", "breakout"))
    fake = FakeTG()
    n = Notifier(cfg(), fake)
    assert report.tap("b", as_of, n, None).startswith("📈 1 breakout chart")
    assert fake.sent[0][0] == "photo" and "FRESH" in fake.sent[0][1]                    # one chart: a photo
    assert fake.buttons == [[("📌 Watch FRESH", None, "w:FRESH"), ("✅ I bought FRESH", None, "b:FRESH")]]
    fake.sent.clear()
    toast = report.tap("d", as_of, n, None)
    assert toast.startswith("🧾") and fake.sent and "FRESH" in fake.sent[0][1]
    with get_db() as conn:
        conn.execute("INSERT INTO daily_plan (as_of, created_at, payload) VALUES (?, ?, ?)",
                     (as_of, "x", json.dumps([{"symbol": "NEAR", "base_label": "B1", "pivot": 100.0,
                                                "dist_to_pivot_pct": 1.0, "zone_top": 105.0, "stop": 95.0},
                                               {"symbol": "FRESH", "base_label": "B1", "pivot": 90.0,
                                                "dist_to_pivot_pct": 2.0, "zone_top": 94.5, "stop": 85.0}])))
    fake.sent.clear()
    assert report.tap("t", as_of, n, None) == "📈 2 top setup charts" and fake.sent[0][0] == "album"


def test_report_button_through_the_bot(monkeypatch):
    from backend.alerts.commands import BotCommands
    from tests.test_alerts import FakeState
    calls = []
    monkeypatch.setattr("backend.alerts.report.tap", lambda what, as_of, n, config: calls.append((what, as_of)) or "ok")
    fake = FakeTG()
    fake.answer_callback = lambda cid, text="": None
    bot = BotCommands(FakeState())
    cq = {"id": "1", "data": "r:d:2026-03-02", "message": {"chat": {"id": 42}}}
    assert bot.handle_button(cq, Notifier(cfg(), fake)) and calls == [("d", "2026-03-02")]


def test_report_job_is_due_in_summary_style_even_without_top_setups():
    at = datetime(2026, 10, 27, 19, 5, tzinfo=IST)
    c = AlertsConfig(top_setups=False)
    assert "top_setups" in due_jobs(at, c, set(), True)
    c.alert_style = "every"
    assert "top_setups" not in due_jobs(at, c, set(), True)


def test_keyboard_rows_and_album_request():
    assert json.loads(keyboard([("a", None, "x")]))["inline_keyboard"] == [[{"text": "a", "callback_data": "x"}]]
    two = json.loads(keyboard([[("a", None, "x"), ("b", None, "y")], [("c", "https://e.x", None)]]))
    assert [len(r) for r in two["inline_keyboard"]] == [2, 1] and two["inline_keyboard"][1][0]["url"] == "https://e.x"
    client, seen = TelegramClient(token="t"), {}
    client._call = lambda method, data=None, files=None: seen.update(method=method, data=data, files=files) or []
    client.send_album("42", [(b"1", "<b>A</b>"), (b"2", "B")])
    media = json.loads(seen["data"]["media"])
    assert seen["method"] == "sendMediaGroup" and [m["media"] for m in media] == ["attach://p0", "attach://p1"]
    assert set(seen["files"]) == {"p0", "p1"} and seen["data"]["disable_notification"] == "true"
    client.send_message("42", "hi", silent=True)
    assert seen["data"]["disable_notification"] == "true"


def test_seven_pm_job_sends_one_silent_report(monkeypatch):
    from tests.test_plan import scan_day, setup
    sch, fake, st, *_ = setup(monkeypatch)
    day = datetime.fromisoformat(scan_day()).replace(tzinfo=IST)
    r = sch.run_top_setups(day.replace(hour=19), force=True)
    assert r["items"] == 5 and r["sent"] == 1 and len(fake.sent) == 1 and fake.silent
    assert "Evening report" in fake.sent[0][1] and "Top 5 for the next session" in fake.sent[0][1]
    assert any(b[2].startswith("r:t:") for b in fake.buttons)
