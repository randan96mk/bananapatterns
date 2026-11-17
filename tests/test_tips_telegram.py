"""💡 Telegram: the hint / taken / follow-up lines (12-hour IST, per the mock), the commands tips · tip N · take N
[lots|half] · skip N (a number: Tips; a symbol: the morning skip) · exit N · trail N px · pause / resume N · tips
channels, and kind "tips" never held by the AI quiet hours."""

from datetime import datetime

import pytest

from backend.alerts import messages as msg
from backend.alerts.commands import BotCommands
from backend.alerts.dispatch import NEVER_HELD, Notifier
from backend.config import IST, AlertsConfig
from backend.database.models import get_db
from backend.tips import book as bk
from backend.tips import track as tr
from tests.test_login import BotTG, update
from tests.tips_world import BN12, BN13, KEY12, at, cfg, seed, world

NOW = at("10:00")


class St:
    def __init__(self, client):
        self.config = cfg()
        self.config.alerts = AlertsConfig(chat_id="42", intraday=False, filings=False)
        self.client = client
        self.token_source = "db"


def bot(texts, client):
    st = St(client)
    fake = BotTG([update(i + 1, t, at=NOW) for i, t in enumerate(texts)])
    return BotCommands(st, clock=lambda: NOW), Notifier(st.config.alerts, fake), fake, st


def test_hint_line_per_the_mock():
    c, w = cfg(), world()
    hid = seed(c, w, BN12)["hint_id"]
    h = bk.load(hid)
    t = msg.tips_hint(h)
    lines = t.split("\n")
    assert lines[0] == "💡 <b>Tip #1 · aakankshatrading · " + msg._tip_t(h["posted_at"]) + "</b>"
    assert lines[1].startswith("BUY BANKNIFTY 50300 PE · near 250–270 when comes · SL 190 · Tgt 320 / 350 / 400++ · expiry Wed 25 Nov")
    assert lines[2] == "<i>plan: wait for the zone · half out at 320, SL to cost, rest to 350 / trail · time stop 3:20 PM · skipped if the zone never comes</i>"
    assert "<b>take 1</b> · <b>take 1 2</b> (lots) · <b>skip 1</b> · <b>tip 1</b>" in lines[-1] and "size not stated → <b>take 1 N</b>" in lines[-1]
    assert msg._tip_t(datetime(2026, 11, 16, 14, 0, tzinfo=IST)).endswith("2:00 PM")


def test_take_n_2_and_half_and_skip_routing(monkeypatch):
    w = world(**{KEY12: 262.0})
    b, n, fake, st = bot(["take 1 2", "take 2 half", "skip 3", "skip NMDC"], w)
    seed(st.config, w, BN12, "09:43", 1)
    seed(st.config, w, BN12, "09:44", 2)
    seed(st.config, w, BN13, "09:46", 3)
    tr.poll(st.config, w, at("09:50"))
    called = []
    monkeypatch.setattr(b, "morning", lambda command, arg, notifier: called.append((command, arg)) or "morning")
    assert b.poll_once(n) == 4
    texts = [t for _, t in fake.sent]
    assert texts[0].startswith("✅ <b>#1 taken · 2 lots at 262 · 10:00 AM</b> · paper · stop 190 · T1 320")
    assert texts[1].startswith("✅ <b>#2 taken · ½ lot at 262")
    assert texts[2] == "⏭ #3 skipped · still scored for the channel's record"
    assert called == [("/skip", "NMDC")]                                         # a symbol stays the morning skip
    assert bk.load(1)["pos"]["lots"] == 2 and bk.load(2)["pos"]["half"]


def test_tip_exit_trail_pause_resume_tips_and_channels():
    w = world(**{KEY12: 262.0})
    b, n, fake, st = bot(["tip 1", "trail 1 250", "exit 1", "pause 2", "resume 2", "tips", "tips channels", "take 9", "exit 2"], w)
    seed(st.config, w, BN12, "09:43", 1)
    seed(st.config, w, BN13, "09:46", 2)
    bk.take(st.config, 1, ltp=262.0, now=at("09:51"), notify=False)
    tr.poll(st.config, w, at("09:52"))
    assert b.poll_once(n) == 9
    t = [x for _, x in fake.sent]
    assert t[0].startswith("💡 <b>#1 BUY BANKNIFTY 50300 PE</b> · active") and "Buy bank nifty 50300 PE" in t[0]
    assert t[1] == "💡 #1 · stop moved to 250 · trail on · 10:00 AM"
    assert t[2].startswith("💡 #1 · closed by you at 262 · −₹") and t[2].endswith("total · 10:00 AM")
    assert t[3] == "💡 #2 · paused · no entry until you resume · 10:00 AM" and t[4] == "💡 #2 · resumed · 10:00 AM"
    assert t[5].startswith("<b>tips</b> → ") and "30 d: " in t[5]
    assert t[6].startswith("💡 <b>Tips channels</b>: aakankshatrading")
    assert t[7] == "Not changed: no hint #9" and t[8] == "Not changed: #2 has no open position"


def test_followup_line_and_taken_line():
    c, w = cfg(), world(**{KEY12: 262.0})
    hid = seed(c, w, BN12)["hint_id"]
    h = bk.take(c, hid, ltp=262.0, now=at("09:51"), notify=False)
    assert msg.tips_taken(h, at("09:51")) == "✅ <b>#1 taken · 1 lot at 262 · 9:51 AM</b> · paper · stop 190 · T1 320"
    line = msg.tips_followup(h, "exit near cost", "followed: rest closed at 372 · +₹2,148 total", at("10:31"))
    assert line == '💡 #1 · channel: "exit near cost" · followed: rest closed at 372 · +₹2,148 total · 10:31 AM · auto, as you set'


def test_quiet_hours_never_hold_tips(monkeypatch):
    from tests.test_alerts import FakeTG
    assert "tips" in NEVER_HELD
    monkeypatch.setattr("backend.alerts.dispatch._now", lambda: datetime(2026, 11, 17, 23, 30, tzinfo=IST))
    fake = FakeTG()
    c = cfg()
    n = Notifier(AlertsConfig(chat_id="42"), fake, ai=c.ai)
    assert n.send("t1", "tips", "💡 #1 · x", quiet=True) is True
    assert n.send("a1", "ai", "🤖 note") is True and len(fake.sent) == 1          # the AI one is held, the tip went out
    n.hold = "2026-11-17"
    assert n.send("t2", "tips", "💡 #2 · y") and len(fake.sent) == 2              # the evening batch never holds it either
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_outbox WHERE kind = 'tips'").fetchone()[0] == 0


def test_book_push_uses_kind_tips(monkeypatch):
    sent = []

    class N:
        def __init__(self, cfg, ai=None):
            pass

        def send(self, key, kind, html):
            sent.append(kind)
            return True
    monkeypatch.setattr("backend.alerts.dispatch.Notifier", N)
    assert bk.push(cfg(), "k", "x") and sent == ["tips"]


@pytest.mark.parametrize("word", ["take", "exit", "trail", "pause", "resume", "tip"])
def test_commands_need_a_number(word):
    b, n, fake, st = bot([f"{word} abc"], world())
    assert b.poll_once(n) == 0
