"""🤖 AI quiet hours and the AI marker on Telegram: an AI message (kind "ai", or a note alert sent with quiet=True)
inside ai.quiet_from-quiet_to is held in alert_outbox (batch ai-quiet:<day>) and flushed in order at quiet_to or with
the morning brief, after one "held overnight" line; the brief, the Kite login, stops and orders are never held. Every
model-written line starts "<i>AI ·</i> "; a message the model wrote entirely opens "[AI] " in its bold header."""

from datetime import datetime

import pytest

from backend.ai import brief, notes
from backend.alerts import dispatch
from backend.alerts import messages as msg
from backend.alerts.dispatch import Notifier, in_quiet, quiet_day
from backend.alerts.jobs import Scheduler
from backend.config import IST, AIConfig, AlertsConfig, AppConfig
from backend.database.models import get_db
from tests.test_alerts import FakeTG
from tests.test_login import LoginState


def at(hh, mm, d=17):
    return datetime(2026, 11, d, hh, mm, tzinfo=IST)


@pytest.fixture
def clock(monkeypatch):
    """Set the quiet-hours clock: clock(2, 0) -> 2:00 AM on 17 Nov."""
    def set_(hh, mm, d=17):
        monkeypatch.setattr(dispatch, "_now", lambda: at(hh, mm, d))
    return set_


@pytest.fixture
def cfg():
    c = AppConfig()
    c.ai = AIConfig(enabled=True)
    c.alerts = AlertsConfig(chat_id="42", intraday=False, filings=False)
    return c


def notifier(cfg, fake):
    return Notifier(cfg.alerts, fake, ai=cfg.ai)


def outbox():
    with get_db() as conn:
        return [tuple(r) for r in conn.execute("SELECT batch, key FROM alert_outbox ORDER BY rowid")]


# --- the window ---------------------------------------------------------------------------------------------------------------------------

def test_window_over_midnight_and_the_batch_day():
    ai = AIConfig()
    assert (ai.quiet_from, ai.quiet_to) == ("23:00", "09:00")
    assert in_quiet(at(23, 0), ai) and in_quiet(at(2, 0), ai) and in_quiet(at(8, 59), ai)
    assert not in_quiet(at(9, 0), ai) and not in_quiet(at(22, 59), ai) and not in_quiet(at(12, 0), ai)
    assert quiet_day(at(23, 30, d=16), ai) == "2026-11-17" and quiet_day(at(2, 0), ai) == "2026-11-17"
    same_day = AIConfig(quiet_from="13:00", quiet_to="14:00")
    assert in_quiet(at(13, 30), same_day) and not in_quiet(at(14, 0), same_day) and not in_quiet(at(2, 0), same_day)
    assert not in_quiet(at(2, 0), AIConfig(quiet_from="09:00", quiet_to="09:00"))          # equal times: off


# --- held ---------------------------------------------------------------------------------------------------------------------------------

def test_a_note_alert_at_2_am_is_held(cfg, clock):
    clock(2, 0)
    fake = FakeTG()
    note = {"id": 5, "day": "2026-11-16", "symbol": "AAA", "kind": "filing", "category": "pledge", "severity": "high",
            "text": "Promoter pledge raised.", "alerted": False, "scope": {"book": "watch"}}
    with get_db() as conn:
        conn.execute("INSERT INTO ai_notes (id, created, day, kind, symbol, severity, title, text, data) VALUES (5, '', '2026-11-16', 'filing', "
                     "'AAA', 'high', 't', 'x', '{}')")
    assert notes.alert_high(note, cfg, notifier(cfg, fake)) is True                         # handled (held), so the note counts as alerted
    assert fake.sent == [] and outbox() == [("ai-quiet:2026-11-17", "2026-11-16|ai-note-5")]
    with get_db() as conn:
        row = conn.execute("SELECT ok, error FROM sent_alerts WHERE key = '2026-11-16|ai-note-5'").fetchone()
    assert tuple(row) == (1, "held for the AI quiet hours")
    assert notifier(cfg, fake).send("2026-11-16|ai-note-5", "filing", "again", quiet=True) is False   # never twice


def test_an_ai_kind_is_held_without_the_flag_and_the_saved_window_is_used(cfg, clock):
    clock(23, 30, d=16)
    fake = FakeTG()
    assert Notifier(cfg.alerts, fake).send("k-ai", "ai", "🧾 why") is True                  # no ai config given: the saved settings
    assert fake.sent == [] and outbox() == [("ai-quiet:2026-11-17", "k-ai")]


def test_stops_orders_login_and_plain_filings_at_2_am_are_not_held(cfg, clock):
    clock(2, 0)
    fake = FakeTG()
    n = notifier(cfg, fake)
    assert n.send("stop", "real", "🛡️ stop hit") and n.send("login", "login", "🔑 log in") and n.send("sys", "system", "Kite login needed")
    assert n.send("fil", "filing", "📊 a filing") and n.send("tip", "tips", "💡 a tip")
    assert len(fake.sent) == 5 and outbox() == []
    assert n.send("sum", "summary", "🌙 day", quiet=True) and outbox() == []               # a never-held kind wins over the flag


# --- flushed --------------------------------------------------------------------------------------------------------------------------------

def hold_two(cfg, clock):
    clock(2, 0)
    n = notifier(cfg, FakeTG())
    n.send("2026-11-16|ai-why", "ai", "🧾 first")
    n.send("2026-11-16|ai-note-9", "filing", "📰 second", "BBB", quiet=True)
    assert len(outbox()) == 2


def test_flushed_at_9_in_order_after_one_line(cfg, clock):
    hold_two(cfg, clock)
    st = LoginState()
    st.config = cfg
    fake = FakeTG()
    sch = Scheduler(st, notifier_factory=lambda c: Notifier(c, fake))
    assert sch.flush_quiet(at(8, 59)) == 0 and fake.sent == []                               # still inside the window
    assert sch.flush_quiet(at(9, 0)) == 2
    assert [h for _, h in fake.sent] == ["🤖 held overnight · 2 messages", "🧾 first", "📰 second"]
    assert outbox() == [] and sch.flush_quiet(at(9, 1)) == 0 and len(fake.sent) == 3
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sent_alerts WHERE ok = 1 AND error IS NULL").fetchone()[0] == 2


def test_a_failed_flush_keeps_the_rest_held(cfg, clock):
    hold_two(cfg, clock)
    assert notifier(cfg, FakeTG(fail=True)).flush_quiet() == 0 and len(outbox()) == 2


def test_the_brief_at_9_is_not_held_and_brings_the_held_messages(cfg, clock):
    hold_two(cfg, clock)
    clock(9, 0)
    fake = FakeTG()
    with get_db() as conn:
        conn.execute("INSERT INTO ai_notes (id, created, day, kind, severity, title, text, data) VALUES (3, '', '2026-11-17', 'brief', 'info', 't', 'x', '{}')")
    assert brief.send(3, {"day": "2026-11-17", "at": "09:00"}, ["Real sends 2 orders."], cfg, notifier(cfg, fake)) is True
    assert [h for _, h in fake.sent][0].startswith("☀️ <b>[AI] Morning brief") and len(fake.sent) == 4 and outbox() == []
    assert fake.sent[1][1] == "🤖 held overnight · 2 messages"


def test_a_brief_inside_the_window_is_still_sent(cfg, clock):
    clock(8, 30)
    fake = FakeTG()
    assert notifier(cfg, fake).send("2026-11-17|ai-brief", "system", msg.ai_brief("2026-11-17", "08:30", ["x"])) and len(fake.sent) == 1


def test_the_samples_report_ignores_the_quiet_batch(cfg, clock):
    hold_two(cfg, clock)
    with get_db() as conn:
        batch = conn.execute("SELECT MAX(batch) FROM alert_outbox WHERE batch != 'sample' AND batch NOT LIKE 'ai-quiet:%'").fetchone()[0]
    assert batch is None


# --- the AI marker ------------------------------------------------------------------------------------------------------------------------------

def test_every_model_line_is_marked_and_whole_ai_messages_open_with_the_tag():
    note = {"symbol": "AAA", "category": "pledge", "severity": "high", "text": "Pledge up."}
    assert msg.note_alert(note, "held").split("\n")[:2] == ["📰 <b>[AI] AAA · pledge</b>", "🤖 <i>AI ·</i> <i>high · Pledge up.</i>"]
    assert msg.ai_line(note) == "\n🤖 <i>AI ·</i> <i>high · Pledge up.</i>" and msg.ai_line(None) == ""
    b = msg.ai_brief("2026-11-17", "09:00", ["one", "two"]).split("\n")
    assert b[0].startswith("☀️ <b>[AI] ") and b[1:3] == ["<i>AI ·</i> one", "<i>AI ·</i> two"] and "AI ·" not in b[3]
    s = msg.ai_suggestions([{"id": 1, "title": "Skip X", "why": "results tomorrow", "why_source": "model"},
                            {"id": 2, "title": "Flag Y", "why": "evidence line", "why_source": "code"}], "2026-11-17")
    assert s.startswith("🤖 <b>[AI] 2 suggestions</b>") and "   <i>AI ·</i> <i>results tomorrow</i>" in s
    assert "   <i>evidence line</i>" in s and "<i>AI ·</i> <i>evidence line</i>" not in s       # code wrote that why: no marker
    assert msg.ai_ask_reply({"question": "q", "answer": "a"}).startswith("💬 <b>[AI] ask</b> q\n<i>AI ·</i> a")
    assert msg.ai_view_line({"score": 82, "verdict": "buy", "reasons": ["tight base"]}) == "🤖 AI 82 buy · <i>AI ·</i> <i>tight base</i>"


def test_code_written_lines_carry_no_marker():
    assert "AI ·</i>" not in msg.ai_view_night({"n": 3, "by_verdict": {"buy": 2, "wait": 1}})
    assert "AI ·</i>" not in msg.quiet_flushed(1) and msg.quiet_flushed(1) == "🤖 held overnight · 1 message"
    f = {"symbol": "AAA", "kind": "results", "text": "Q2", "category": "Outcome", "dt": "2026-11-16T18:00:00"}
    assert "AI ·" not in msg.filing_alert(f, "you hold it")
    assert "[AI]" not in msg.day_summary({"ai": {"ran": True, "notes": 1, "suggestions": 0, "rated": 0}}, "2026-11-16")
