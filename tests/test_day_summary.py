"""🌙 One close-of-day summary across the books: the due rule at alerts.day_summary_time, the message shape (one line
per book with anything, value and the day's P&L in the same place), the facts read from each book's tables, the job
(sent once, kind "summary" never held, recorded), the schedule row, and the two day lines it replaces (the options
3:35 PM line and the flow day summary) no longer sent on their own while their data stays."""

import json
from datetime import datetime

import pytest

from backend.alerts import daysum
from backend.alerts import messages as msg
from backend.alerts.dispatch import Notifier
from backend.alerts.jobs import Scheduler, due_jobs
from backend.config import IST, AIConfig, AlertsConfig, AppConfig
from backend.database.models import get_db, now_ist
from backend.engine import real
from backend.engine import schedule as sched
from tests.test_alerts import FakeTG
from tests.test_login import LoginState

DAY = "2026-11-16"                                    # a Monday


def at(hh, mm, d=16):
    return datetime(2026, 11, d, hh, mm, tzinfo=IST)


FULL = {"real": {"value": 308793.0, "today": -1702.0, "stops": ["LAURUSLABS"], "failed": 0, "tomorrow": {"buys": 3, "exits": 1}},
        "paper": [{"name": "E+", "value": 311153.0, "today": -1751.0, "stops": 1, "marks_at": "18:30"}],
        "futures": {"tag": "paper", "value": 490311.0, "today": 3150.0, "lots": 2, "roll": "2026-11-19"},
        "options": {"tag": "paper", "value": 299417.0, "today": -583.0, "trades": 1, "result": "VWAP stop"},
        "flow": {"kind": "signal", "trades": 3, "won": 1, "net": -435.0, "capital": 300000.0},
        "ai": {"ran": False, "after": "18:34", "parts": ["notes", "why", "suggestions", "ratings"]}}


@pytest.fixture
def cfg():
    c = AppConfig()
    c.alerts = AlertsConfig(chat_id="42", intraday=False, filings=False)
    return c


# --- the due rule and the settings --------------------------------------------------------------------------------------------------

def test_due_at_the_day_summary_time_on_trading_days_once():
    c = AlertsConfig(intraday=False, filings=False, login_links=False, system=False)
    assert c.day_summary is True and c.day_summary_time == "15:40"
    assert "day_summary" in due_jobs(at(15, 40), c, set(), True)
    assert "day_summary" not in due_jobs(at(15, 39), c, set(), True)
    assert "day_summary" not in due_jobs(at(15, 45), c, {"day_summary"}, True)              # once a day
    assert "day_summary" not in due_jobs(at(15, 45, d=15), c, set(), False)                # not on a holiday
    c.day_summary = False
    assert "day_summary" not in due_jobs(at(15, 45), c, set(), True)


def test_settings_keys_and_time_validation(cfg):
    assert cfg.update_section("alerts", {"day_summary_time": "16:05"})["day_summary_time"] == "16:05"
    with pytest.raises(ValueError):
        cfg.update_section("alerts", {"day_summary_time": "4 PM"})
    assert cfg.alerts.day_summary_time == "16:05"                                            # rolled back
    assert cfg.update_section("ai", {"quiet_from": "22:30"})["quiet_from"] == "22:30"
    with pytest.raises(ValueError):
        cfg.update_section("ai", {"quiet_to": "9am"})
    assert cfg.ai.quiet_to == "09:00"


# --- the message --------------------------------------------------------------------------------------------------------------------

def test_message_shape_every_book():
    assert msg.day_summary(FULL, DAY).split("\n") == [
        "🌙 <b>Day summary · Mon 16 Nov</b>",
        "💰 Real · ₹3,08,793 · today −₹1,702 · 1 stop hit (LAURUSLABS) · 0 orders failed · tomorrow 3 BUYs 1 EXIT",
        "📝 Paper E+ · ₹3,11,153 · today −₹1,751 · 1 stop hit",
        "📐 Futures · paper · ₹4,90,311 · today +₹3,150 · 2 lots · roll Thu",
        "🧩 Options · paper · ₹2,99,417 · today −₹583 · 1 trade · VWAP stop",
        "⚡ Signal track · 3 trades · 1 won · net −₹435 on ₹3,00,000",
        "🤖 AI · tonight after 6:34 PM: notes, why, suggestions, ratings"]


def test_message_leaves_out_books_with_nothing_and_says_when_paper_marks():
    parts = {"paper": [{"name": "E+", "value": 311153.0, "today": None, "stops": 0, "marks_at": "18:30"}],
             "ai": {"ran": True, "notes": 4, "suggestions": 2, "rated": 11}}
    lines = msg.day_summary(parts, DAY).split("\n")
    assert lines[1:] == ["📝 Paper E+ · ₹3,11,153 · today at the 6:30 PM mark", "🤖 AI · 4 notes · 2 suggestions · 11 rated"]
    assert msg.day_summary({}, DAY) is None
    o = msg.day_summary({"options": {"tag": "paper", "value": 3e5, "today": 0.0, "trades": 0, "result": "no break by 11:00 AM"}}, DAY)
    assert o.endswith("🧩 Options · paper · ₹3,00,000 · today +₹0 · no trade · no break by 11:00 AM") and "AI ·</i>" not in o


# --- the facts ------------------------------------------------------------------------------------------------------------------------

def seed_real():
    real._save({"started": "2026-10-01", "capital": 300000.0, "cash": 100000.0, "charges": 0.0, "as_of": "2026-11-13",
                "positions": {"AAA": {"qty": 100, "px": 1000.0, "d": "2026-11-02", "stop": 950.0, "last": 1010.0, "live": 1020.0, "charges": 0.0}},
                "orders": [], "deposits": []})
    with get_db() as conn:
        conn.execute("INSERT INTO real_equity (day, equity, gross, nifty) VALUES ('2026-11-13', 203000, 203000, 25000)")
        for key, sym, status in (("k1", "LAURUSLABS", "sent"), ("k2", "BBB", "failed")):
            conn.execute("INSERT INTO real_orders (key, day, created_at, symbol, action, qty, reason, status) VALUES (?, ?, ?, ?, 'EXIT', 5, ?, ?)",
                         (key, DAY, now_ist(), sym, "stop 96.0 hit" if status == "sent" else "planned exit", status))


def test_real_part_reads_value_today_stops_and_failed(cfg):
    assert daysum.real_part(cfg, DAY) is None                                                # not started: no line
    seed_real()
    p = daysum.real_part(cfg, DAY)
    assert p == {"value": 202000.0, "today": -1000.0, "stops": ["LAURUSLABS"], "failed": 1, "tomorrow": None}
    st = real._state()
    st.update(as_of=DAY, orders=[{"action": "BUY", "symbol": "X"}, {"action": "BUY", "symbol": "Y"}, {"action": "EXIT", "symbol": "AAA"}])
    real._save(st)
    assert daysum.real_part(cfg, DAY)["tomorrow"] == {"buys": 2, "exits": 1}


def test_options_flow_and_ai_parts(cfg, monkeypatch):
    with get_db() as conn:
        conn.execute("INSERT INTO options_days (day, payload) VALUES (?, ?)",
                     (DAY, json.dumps({"day": DAY, "trades": 1, "pnl": -583.0, "equity": 299417.0, "result": "VWAP stop", "why": "VWAP stop"})))
    assert daysum.options_part(cfg, DAY) == {"tag": "paper", "value": 299417.0, "today": -583.0, "trades": 1, "result": "VWAP stop", "afternoon": False}
    assert daysum.options_part(cfg, "2026-11-17") is None
    assert daysum.flow_part(cfg, DAY) is None                                                # flow off
    from backend.intraday import flow
    cfg.flow.mode = "signals"
    monkeypatch.setattr(flow, "_stats", lambda day: {"trades": 3, "won": 1, "realised": -435.0, "capital": 300000.0})
    assert daysum.flow_part(cfg, DAY) == {"kind": "signal", "trades": 3, "won": 1, "net": -435.0, "capital": 300000.0}
    monkeypatch.setattr(flow, "_stats", lambda day: {"trades": 0, "won": 0, "realised": 0.0, "capital": 300000.0})
    assert daysum.flow_part(cfg, DAY) is None                                                # no trade: no line
    assert daysum.ai_part(cfg, DAY) is None                                                  # the AI off
    cfg.ai = AIConfig(enabled=True, why=False)
    assert daysum.ai_part(cfg, DAY) == {"ran": False, "after": "18:30", "parts": ["notes", "suggestions", "ratings"]}
    with get_db() as conn:
        conn.execute("INSERT INTO ai_notes (created, day, kind, symbol, severity, title, text, data) VALUES (?, ?, 'filing', 'A', 'info', 't', 'x', '{}')",
                     (now_ist(), DAY))
    assert daysum.ai_part(cfg, DAY) == {"ran": True, "notes": 1, "suggestions": 0, "rated": 0}


def test_a_failing_part_is_left_out(cfg, monkeypatch):
    seed_real()
    monkeypatch.setattr(daysum, "PARTS", (("real", daysum.real_part), ("futures", lambda c, d: 1 / 0)))
    assert set(daysum.parts(cfg, DAY)) == {"real"}


# --- the job --------------------------------------------------------------------------------------------------------------------------

def test_run_day_summary_sends_once_never_held_and_records(cfg):
    seed_real()
    st = LoginState()
    st.config = cfg
    fake = FakeTG()
    sch = Scheduler(st, notifier_factory=lambda c: Notifier(c, fake))
    sch.hold = DAY                                                                           # an evening hold in place: kind "summary" still goes
    out = sch.run_day_summary(at(15, 40))
    assert out["status"] == "ok" and out["sent"] is True and len(fake.sent) == 1
    text = fake.sent[0][1]
    assert text.startswith("🌙 <b>Day summary · Mon 16 Nov</b>\n💰 Real · ₹2,02,000 · today −₹1,000 · 1 stop hit (LAURUSLABS) · 1 order failed")
    with get_db() as conn:
        runs = conn.execute("SELECT status, message FROM job_runs WHERE job = 'day_summary'").fetchall()
        sent = conn.execute("SELECT kind FROM sent_alerts WHERE key = ?", (f"{DAY}|day-summary",)).fetchone()
    assert [tuple(r) for r in runs] == [("ok", "sent")] and sent["kind"] == "summary"
    assert "day_summary" in sch.done_today(at(15, 40).date())
    assert sch.run_day_summary(at(15, 41))["sent"] is False and len(fake.sent) == 1        # the key: once


def test_nothing_to_report_sends_nothing(cfg):
    st = LoginState()
    st.config = cfg
    fake = FakeTG()
    out = Scheduler(st, notifier_factory=lambda c: Notifier(c, fake)).run_day_summary(at(15, 40))
    assert out["text"] is None and fake.sent == []


def test_flow_day_summary_is_no_longer_sent_on_its_own(cfg):
    st = LoginState()
    st.config = cfg
    fake = FakeTG()
    sch = Scheduler(st, notifier_factory=lambda c: Notifier(c, fake))
    sch._flow_alert("summary", {"trades": 3, "won": 1, "realised": -435.0, "capital": 300000.0})
    assert fake.sent == []
    sch._flow_alert("loss_limit", {"realised": -3000.0, "loss_limit": 3000.0})               # the other flow lines still go
    assert len(fake.sent) == 1


def test_schedule_row(cfg):
    rows = {j["key"]: j for j in sched.jobs(cfg, at(10, 0))}
    assert rows["day_summary"]["at"] == at(15, 40).isoformat() and rows["day_summary"]["icon"] == "🌙" and rows["day_summary"]["name"] == "Day summary"
    cfg.alerts.day_summary = False
    assert "day_summary" not in {j["key"] for j in sched.jobs(cfg, at(10, 0))}


def test_paper_part_unmarked_then_marked(cfg):
    st = {"variant": "E", "name": "E+ · half filter, top 300, late trail", "capital": 300000.0, "cash": 311153.0, "charges": 0.0, "slippage": 0.0,
          "as_of": "2026-11-13", "positions": {}, "orders": [], "deposits": [], "started": "2026-10-01"}
    with get_db() as conn:
        conn.execute("INSERT INTO paper_state (account, payload) VALUES ('E', ?)", (json.dumps(st),))
        conn.execute("INSERT INTO paper_equity (account, day, equity, gross, nifty) VALUES ('E', '2026-11-13', 312904, 312904, 25000)")
        conn.execute("INSERT INTO paper_trades (account, payload) VALUES ('E', ?)", (json.dumps({"symbol": "LAURUSLABS", "out": DAY, "why": "stop hit", "net": -1751.0}),))
    assert daysum.paper_parts(cfg, DAY) == [{"name": "E+", "value": 311153.0, "today": None, "stops": 1, "marks_at": "18:30"}]
    st["as_of"] = DAY                                                                        # the evening job marked today
    with get_db() as conn:
        conn.execute("UPDATE paper_state SET payload = ? WHERE account = 'E'", (json.dumps(st),))
    assert daysum.paper_parts(cfg, DAY)[0]["today"] == -1751.0
