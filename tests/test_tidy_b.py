"""Telegram tidy, the parts outside Tips (B1–B7): the Kite login reminder's two deadlines, the 8:50 plan riding in the
9:00 AM brief as one message, the 3:15 PM check's wording (tests/test_intraday.py), the 3:40 PM day summary without the
unmarked paper line and the full one at 6:35 PM, the exits-explained guard against a line that only repeats the header,
the three-line options entry message (tests/test_options_alerts.py) and the options VWAP stop's two settings."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from backend.ai import brief, why
from backend.alerts import jobs as aj
from backend.alerts import messages as msg
from backend.alerts.dispatch import Notifier
from backend.alerts.jobs import Scheduler, due_jobs
from backend.config import IST, AIConfig, AlertsConfig, AppConfig
from backend.engine import schedule as sched
from backend.options import bias as bs
from backend.options import book as obook
from tests.test_alerts import FakeTG
from tests.test_day_summary import FULL
from tests.test_login import LoginState

DAY = "2026-11-16"                                    # a Monday


def at(hh, mm, d=16):
    return datetime(2026, 11, d, hh, mm, tzinfo=IST)


@pytest.fixture
def cfg():
    c = AppConfig()
    c.alerts = AlertsConfig(chat_id="42", intraday=False, filings=False)
    return c


def scheduler(cfg):
    st = LoginState()
    st.config = cfg
    fake = FakeTG()
    return Scheduler(st, notifier_factory=lambda c: Notifier(c, fake)), fake


# --- B1 the Kite login reminder ---------------------------------------------------------------------------------------------------

def test_login_reminder_names_both_deadlines():
    t = msg.login_link_alert("new_day", at(9, 0))
    assert t.split("\n")[-1] == ("Tap <b>Log in to Kite</b> before 9:00 AM (the link works once); "
                                 "the session then lasts until 6:00 AM IST tomorrow.")


# --- B2 the morning: one message ----------------------------------------------------------------------------------------------------

def test_brief_carries_plan_rule(cfg):
    assert aj.brief_carries_plan(cfg) is False                                      # the AI off: the plan alone at 8:50
    cfg.ai = AIConfig(enabled=True)
    assert aj.brief_carries_plan(cfg) is True and aj.brief_carries_plan(cfg, at(9, 14)) is True
    assert aj.brief_carries_plan(cfg, at(9, 15)) is False                           # the brief window is over
    cfg.ai.brief_time = "07:15"                                                     # a brief before the plan: separate
    assert aj.brief_carries_plan(cfg) is False
    cfg.ai.brief_time, cfg.ai.brief = "09:00", False
    assert aj.brief_carries_plan(cfg) is False


def test_plan_waits_for_the_brief_then_goes_alone_if_it_did_not(cfg, monkeypatch):
    monkeypatch.setattr(aj, "morning_plan_html", lambda config, today, mc=None: ("☀️ <b>Today's plan</b> X", [{"symbol": "X"}]))
    cfg.ai = AIConfig(enabled=True)
    sch, fake = scheduler(cfg)
    r = sch.run_morning_plan(at(8, 50))
    assert r["status"] == "waiting" and fake.sent == [] and sch.not_before["morning_plan"] == at(9, 15)
    assert "morning_plan" not in sch.done_today(at(8, 50).date())
    r = sch.run_morning_plan(at(9, 15))                                             # no brief came: the plan alone
    assert r["sent"] is True and fake.sent[-1][1] == "☀️ <b>Today's plan</b> X"


def test_plan_alone_at_850_when_the_brief_is_off(cfg, monkeypatch):
    monkeypatch.setattr(aj, "morning_plan_html", lambda config, today, mc=None: ("☀️ <b>Today's plan</b> X", [{"symbol": "X"}]))
    sch, fake = scheduler(cfg)
    assert sch.run_morning_plan(at(8, 50))["sent"] is True and len(fake.sent) == 1


def test_the_brief_sends_plan_first_then_the_ai_lines(cfg, monkeypatch):
    monkeypatch.setattr(aj, "morning_plan_html", lambda config, today, mc=None: ("☀️ <b>Today's plan</b> X", [{"symbol": "X"}]))
    from backend.market import real_reads as rr
    monkeypatch.setattr(rr, "brief_line", lambda config: None)
    cfg.ai = AIConfig(enabled=True)
    fake = FakeTG()
    n = Notifier(cfg.alerts, fake)
    f = {"day": DAY, "at": "09:00"}
    assert brief.send(1, f, ["one line"], cfg, n) is True
    assert len(fake.sent) == 1
    text = fake.sent[0][1]
    assert text.startswith("☀️ <b>Today's plan</b> X\n\n☀️ <b>[AI] Morning brief") and "<i>AI ·</i> one line" in text
    assert n.already_sent(f"{DAY}|morning")                                         # the 9:15 fallback sends nothing more
    sch, fake2 = scheduler(cfg)
    r = sch.run_morning_plan(at(9, 15))
    assert r["sent"] is False and r["note"] == "sent with the morning brief" and fake2.sent == []


def test_schedule_row_moves_to_the_brief(cfg):
    rows = {j["key"]: j for j in sched.jobs(cfg, at(7, 0))}
    assert rows["morning_plan"]["at"] == at(8, 50).isoformat()
    cfg.ai = AIConfig(enabled=True)
    rows = {j["key"]: j for j in sched.jobs(cfg, at(7, 0))}
    assert rows["morning_plan"]["at"] == at(9, 0).isoformat() and "with the AI brief" in rows["morning_plan"]["desc"]


# --- B4 the day summary -------------------------------------------------------------------------------------------------------------

UNMARKED = {**FULL, "paper": [{"name": "E+", "value": 311153.0, "today": None, "stops": 0, "marks_at": "18:30"}]}


def test_340_summary_says_when_paper_comes():
    lines = msg.day_summary(UNMARKED, DAY, "18:30").split("\n")
    assert "📝 paper marked at 6:30 PM · full summary then" in lines and not any("today at the" in x for x in lines)
    assert lines[2] == "📝 paper marked at 6:30 PM · full summary then"             # in the paper line's place
    assert "today at the 6:30 PM mark" in msg.day_summary(UNMARKED, DAY)           # day_summary_final off: as before
    assert msg.day_summary(FULL, DAY, full=True).split("\n")[0] == "🌙 <b>Day summary · Mon 16 Nov · full</b>"


def test_final_summary_due_rule():
    c = AlertsConfig(intraday=False, filings=False, login_links=False, system=False)
    assert c.day_summary_final is True and aj.final_summary_time(c).strftime("%H:%M") == "18:35"
    assert "day_summary_final" in due_jobs(at(18, 35), c, set(), True)
    assert "day_summary_final" not in due_jobs(at(18, 34), c, set(), True)
    assert "day_summary_final" not in due_jobs(at(18, 40), c, {"day_summary_final"}, True)
    assert "day_summary_final" not in due_jobs(at(18, 40, d=15), c, set(), False)
    c.day_summary_final = False
    assert "day_summary_final" not in due_jobs(at(18, 40), c, set(), True)


def test_final_summary_waits_for_the_paper_mark(cfg, monkeypatch):
    from backend.alerts import daysum
    parts = {"paper": [dict(UNMARKED["paper"][0])]}
    monkeypatch.setattr(daysum, "parts", lambda config, day: parts)
    sch, fake = scheduler(cfg)
    r = sch.run_day_summary_final(at(18, 35))
    assert r["status"] == "waiting" and fake.sent == [] and sch.not_before["day_summary_final"] == at(18, 45)
    parts["paper"][0]["today"] = -1751.0
    r = sch.run_day_summary_final(at(18, 45))
    assert r["sent"] is True and fake.sent[0][1].startswith("🌙 <b>Day summary · Mon 16 Nov · full</b>\n📝 Paper E+ · ₹3,11,153 · today −₹1,751")
    assert "day_summary_final" in sch.done_today(at(18, 45).date())


def test_340_job_drops_the_unmarked_line(cfg, monkeypatch):
    from backend.alerts import daysum
    monkeypatch.setattr(daysum, "parts", lambda config, day: {"paper": [dict(UNMARKED["paper"][0])]})
    sch, fake = scheduler(cfg)
    assert sch.run_day_summary(at(15, 40))["sent"] is True
    assert fake.sent[0][1].split("\n")[1] == "📝 paper marked at 6:30 PM · full summary then"
    cfg.alerts.day_summary_final = False
    assert "today at the 6:30 PM mark" in msg.day_summary(daysum.parts(cfg, DAY), DAY)


def test_final_summary_schedule_row(cfg):
    rows = {j["key"]: j for j in sched.jobs(cfg, at(10, 0))}
    assert rows["day_summary_final"]["at"] == at(18, 35).isoformat()
    cfg.alerts.day_summary_final = False
    assert "day_summary_final" not in {j["key"] for j in sched.jobs(cfg, at(10, 0))}


# --- B5 exits, explained --------------------------------------------------------------------------------------------------------------

def test_why_line_that_repeats_the_header_is_dropped():
    rows = [{"book": "real", "symbol": "RT", "text": "likely cause: the stop hit at −₹620", "trade": {"label": "stop hit", "net": -620.0}},
            {"book": "paper", "symbol": "PT", "text": "likely cause: Pharma fell 2.10% with NIFTY", "trade": {"label": "stop hit", "net": -390.0}}]
    lines = msg.ai_why_evening(rows, DAY).split("\n")
    assert lines[1] == "💰 <b>RT</b> · stop hit · −₹620 <i>· no added cause</i>"
    assert lines[2] == "📝 <b>PT</b> · stop hit · −₹390" and lines[3] == "   🤖 <i>AI ·</i> <i>likely cause: Pharma fell 2.10% with NIFTY</i>"
    assert msg.why_repeats_header("likely cause: stop hit", "RT · stop hit · −₹620")
    assert not msg.why_repeats_header("likely cause: results on Friday", "RT · stop hit · −₹620")


def test_why_prompt_asks_for_the_fact_not_in_the_header():
    t = {"symbol": "RT", "book": "real", "label": "stop hit", "net": -620.0}
    p = why.prompt(t, [])
    assert 'already sees this line: "RT · stop hit · real net -620"' in p and "NOT already in that line" in p and why.PROMPT_V == "why-v2"


# --- B7 the options VWAP stop -----------------------------------------------------------------------------------------------------------

def rule(closes=2, margin=10.0):
    return SimpleNamespace(vwap_stop_closes=closes, vwap_stop_margin_pts=margin)


def test_vwap_stop_closes_and_margin():
    o = {"bias": bs.BULLISH}
    assert obook.vwap_stop_fires(o, rule(), 25995.0, 26000.0) is False            # one close 5 pts under: no stop
    assert obook.vwap_stop_fires(o, rule(), 25996.0, 26000.0) is True             # the second in a row: stop
    o = {"bias": bs.BULLISH}
    assert obook.vwap_stop_fires(o, rule(), 25988.0, 26000.0) is True             # one close 12 pts under: stop
    o = {"bias": bs.BULLISH}
    assert obook.vwap_stop_fires(o, rule(), 25995.0, 26000.0) is False
    assert obook.vwap_stop_fires(o, rule(), 26005.0, 26000.0) is False and o["vwap_through"] == 0   # back over: the count restarts
    assert obook.vwap_stop_fires(o, rule(), 25995.0, 26000.0) is False
    o = {"bias": bs.BEARISH}
    assert obook.vwap_stop_fires(o, rule(), 26012.0, 26000.0) is True             # bearish: a close over VWAP
    o = {"bias": bs.BULLISH}
    assert obook.vwap_stop_fires(o, rule(1, 0.0), 25999.5, 26000.0) is True       # margin 0 + closes 1 = the old rule
    assert obook.vwap_stop_fires({"bias": bs.BULLISH}, rule(1, 0.0), 26001.0, 26000.0) is False


def test_vwap_stop_settings_and_text(cfg):
    assert cfg.options.vwap_stop_closes == 2 and cfg.options.vwap_stop_margin_pts == 10.0
    assert obook.vwap_rule_text(cfg.options) == "2 5-minute closes back through VWAP in a row, or one 10 pts through"
    assert obook.vwap_rule_text(rule(1, 0.0)) == "a 5-minute close back through VWAP"
    assert cfg.update_section("options", {"vwap_stop_closes": 3})["vwap_stop_closes"] == 3
    with pytest.raises(ValueError):
        cfg.update_section("options", {"vwap_stop_closes": 0})
    with pytest.raises(ValueError):
        cfg.update_section("options", {"vwap_stop_margin_pts": -1})
    assert cfg.options.vwap_stop_closes == 3 and cfg.options.vwap_stop_margin_pts == 10.0
