"""💡 The Tips card v2 (backend/tips/card2.py): every state word and its group (live / armed / waiting stop missing /
waiting level / waiting channel's word / paused / closed target 2 won / closed stop lost / closed channel's exit / time
stop / your exit / cancelled avoid / window end / kill switch / no fill / skipped / expired / paper-only), the five story
steps of a closed Kite trade through the dry-run simulator, the fold counts and the footer, the page groups' order,
and the same state word on Home and in Telegram."""

from datetime import datetime

import pytest

from backend.alerts import messages as msg
from backend.config import IST
from backend.tips import book as bk
from backend.tips import card2
from backend.tips import home as th
from backend.tips import kite_orders as ko
from backend.tips import track as tr
from tests.test_tips_kite import Broker
from tests.tips_world import BN12, BN13, KEY12, at, cfg, seed, world

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=IST)


@pytest.fixture
def pushed(monkeypatch):
    out = []
    monkeypatch.setattr(bk, "push", lambda config, key, html: out.append(html) or True)
    return out


def hint(status="waiting", kite=None, plan_ok=True, why=None, taken=False, pos=None, **kw) -> dict:
    h = {"id": 7, "side": "BUY", "status": status, "tracked": True, "lot_size": 15, "symbol": "BANKNIFTY", "posted_at": "2026-11-17T09:43:00+05:30",
         "entry": {"mode": "above", "lo": 186.0}, "stop": {"kind": "premium", "value": 176.0}, "targets": [195, 205, 215],
         "plan": {"ok": plan_ok, "why": why, "targets": [195, 205, 215], "stop": {"kind": "premium", "value": 176.0},
                  "entry": {"mode": "above", "lo": 186.0}, "time_stop": "15:14", "wait": True},
         "events": [], "run": {"state": "waiting", "hits": []}, "taken": taken, "pos": pos}
    if kite is not None:
        h["kite"] = {**ko.new_kite(), **kite}
    h.update(kw)
    return h


def word(h) -> tuple:
    s = card2.state_of(h)
    return s["word"], s["kind"], s["group"]


# --- the state words ------------------------------------------------------------------------------------------------------------

def test_live_armed_and_waiting_words():
    assert word(hint("active", {"state": "entered", "qty_open": 30, "entry_px": 186.15})) == ("LIVE", "live", "live")
    assert word(hint("active", {"state": "entered", "qty_open": 30, "entry_px": 186.15, "dry": True}))[0] == "DRY · LIVE"
    s = card2.state_of(hint("waiting", {"state": "armed", "arm": {"kind": "slm", "trigger": 186.0}}))
    assert (s["word"], s["kind"], s["group"], s["why"]) == ("ARMED · RESTING", "wait", "waiting", "SL-M resting on Kite")
    assert word(hint("waiting", {"state": "armed_watch", "arm": {"kind": "watch", "index": "SENSEX"}}))[0] == "ARMED · RESTING"
    assert word(hint("new", plan_ok=False, why="stop missing")) == ("WAITING · STOP MISSING", "wait", "waiting")
    assert word(hint("new", plan_ok=False, why="side missing"))[0] == "WAITING · SIDE MISSING"
    assert word(hint("waiting")) == ("WAITING · LEVEL", "wait", "waiting")
    h = hint("waiting")
    h["plan"]["entry"] = {"mode": "zone", "lo": 250, "hi": 270, "needs_activation": True}
    assert word(h)[0] == "WAITING · CHANNEL'S WORD"
    h["run"]["allowed"] = True                                       # the channel said active: back to the level
    assert word(h)[0] == "WAITING · LEVEL"


def test_paused_words():
    assert word(hint("paused")) == ("PAUSED · AVOID", "wait", "waiting")
    assert word(hint("paused", events=[{"t": "x", "text": "paused by you (page)"}]))[0] == "PAUSED · BY YOU"


@pytest.mark.parametrize("kite, want", [
    ({"reason": "target 2", "exit_by": "target", "realised": 2220.0}, ("CLOSED · TARGET 2", "won")),
    ({"reason": "stop", "exit_by": "stop", "realised": -1080.0}, ("CLOSED · STOP", "lost")),
    ({"reason": "the channel's exit", "exit_why": "the channel's exit", "exit_by": "auto", "realised": -210.0}, ("CLOSED · CHANNEL'S EXIT", "lost")),
    ({"reason": "the channel's SL hit", "exit_why": "the channel's SL hit", "exit_by": "auto", "realised": 15.0}, ("CLOSED · CHANNEL'S EXIT", "won")),
    ({"reason": "time stop 3:14 PM", "exit_why": "time stop 3:14 PM", "exit_by": "time_stop", "realised": 300.0}, ("CLOSED · TIME STOP", "won")),
    ({"reason": "your exit", "exit_by": "you", "realised": 0.0}, ("CLOSED · YOUR EXIT", "won")),
])
def test_closed_words(kite, want):
    h = hint("closed", {"state": "closed", "entry_px": 186.0, "qty": 30, "closed_at": "2026-11-17T11:00:05+05:30", **kite})
    s = card2.state_of(h)
    assert (s["word"], s["kind"], s["group"], s["at"]) == (*want, "closed", "2026-11-17T11:00:05+05:30")


@pytest.mark.parametrize("reason, want", [
    ('the channel: "avoid this trade"', "CANCELLED · AVOID"),
    ("entry window ended 2:30 PM · never filled", "CANCELLED · WINDOW END"),
    ("kill switch", "CANCELLED · KILL SWITCH"),
    ("time stop 3:14 PM · never filled", "NO FILL · TIME STOP"),
    ("you cancelled it", "CANCELLED · BY YOU"),
])
def test_not_taken_kite_words(reason, want):
    s = card2.state_of(hint("waiting", {"state": "none", "reason": reason}))
    assert (s["word"], s["kind"], s["group"], s["why"]) == (want, "off", "not_taken", reason)


def test_skipped_expired_no_fill_and_record_only():
    assert word(hint("skipped")) == ("SKIPPED BY YOU", "off", "not_taken")
    assert word(hint("expired", tracked=False, untracked_why="history · no quotes")) == ("EXPIRED", "off", "not_taken")
    assert word(hint("closed", ended="no_fill")) == ("NO FILL · TIME STOP", "off", "not_taken")
    s = card2.state_of(hint("closed", ended="t1"))
    assert (s["word"], s["group"], s["why"]) == ("NOT TAKEN", "not_taken", "the record: target 1")
    assert word(hint("waiting", {"state": "none", "reason": "dry run cleared"}))[0] == "WAITING · LEVEL"   # cleared: no Kite word


def test_paper_only_words():
    pos = {"state": "open", "lots": 1, "entry_px": 186.0, "run": {"entry_t": "2026-11-17T10:00:00+05:30"}}
    assert word(hint("active", taken=True, pos=pos)) == ("PAPER · OPEN", "live", "live")
    pos = {"state": "closed", "lots": 1, "entry_px": 186.0, "realised": -150.0, "ended": "stop", "run": {"exit_t": "2026-11-17T10:30:00+05:30"}}
    s = card2.state_of(hint("closed", taken=True, pos=pos))
    assert (s["word"], s["kind"], s["group"], s["why"]) == ("PAPER · CLOSED", "lost", "closed", "stop")
    assert word(hint("waiting", taken=True, pos={"state": "armed", "lots": 1, "run": {}}))[0] == "PAPER · WAITING"


def test_tstamp_today_and_another_day():
    assert card2.tstamp("2026-11-17T14:04:00+05:30", NOW) == "2:04"
    assert card2.tstamp("2026-11-19T14:04:00+05:30", NOW.replace(day=20)) == "Thu 2:04 PM"
    assert card2.tstamp(None, NOW) == ""


# --- the five steps of a closed Kite trade (the dry-run simulator) -------------------------------------------------------------

def _dry_trade(c, b):
    hid = seed(c, b, BN12)["hint_id"]
    tr.poll(c, b, at("10:00"))
    s = ko.sim()
    ko.place_all(c, s, hid, 2, now=at("10:00", sec=1))
    ko.sync(c, s, at("10:00", sec=5))
    return hid, s


def test_the_five_steps_open_then_closed_at_target_2(pushed):
    c = cfg()
    c.tips.kite_mode = "dry"
    b = Broker(world(**{KEY12: 262.0}))
    hid, s = _dry_trade(c, b)
    v = tr.hint_view(bk.load(hid), now=at("10:05"), config=c)
    assert v["state"]["word"] == "DRY · LIVE" and v["state"]["group"] == "live"
    st = {x["step"]: x for x in v["story"]}
    assert [x["step"] for x in v["story"]] == ["post", "entry", "stop", "targets", "now"]
    assert st["entry"]["head"] == "filled 262 · 2 lots" and st["entry"]["t"] == "10:00" and st["entry"]["sub"].startswith("post → fill 17 min")
    assert st["stop"]["head"] == "190" and st["stop"]["sub"] == "resting · −27.5 % away" and st["stop"]["settable"] is True
    assert st["targets"]["head"] == "T1 320 half · T2 350 rest" and st["targets"]["sub"] == "T3 400+ paper only"
    assert st["now"]["head"] == "262 · +0 % from the fill" and st["now"]["sub"] == "stop −27.5 % away · T1 +22.1 % away · time stop 3:14 PM"
    assert v["foot"].startswith("30 open in the dry run · only the stop rests")
    for px, hm in ((320.0, "10:20"), (352.0, "11:00")):
        b.prices[KEY12] = px
        tr.poll(c, b, at(hm))
        ko.watch_all(c, b, at(hm, sec=1))
        ko.sync(c, s, at(hm, sec=5))
    v = tr.hint_view(bk.load(hid), now=at("15:00"), config=c)
    assert (v["state"]["word"], v["state"]["kind"], v["state"]["group"]) == ("DRY · CLOSED · TARGET 2", "won", "closed")
    st = {x["step"]: x for x in v["story"]}
    assert [x["step"] for x in v["story"]] == ["post", "entry", "stop", "targets", "result"] and all(x["done"] for x in v["story"])
    assert st["post"] == {"step": "post", "t": "9:43", "head": "near 250–270 when comes · SL 190 · tgt 320/350/400+",
                          "sub": "the post names no size · 1 lot → 2 lots on Kite", "tone": "", "done": True}
    assert st["stop"]["head"] == "190 → 262 (cost after T1)" and st["stop"]["sub"].endswith("cancelled at the exit") and st["stop"]["settable"] is False
    assert st["targets"]["head"] == "T1 320 half ✓ · T2 350 rest ✓" and st["targets"]["tone"] == "up" and st["targets"]["t"] == "10:20"
    assert st["result"]["head"].startswith("+₹2,220 · net +₹") and st["result"]["sub"].startswith("best +34.4 %") and st["result"]["tone"] == "up"
    assert st["result"]["t"] == "11:00"
    assert v["foot"] == "closed in the dry run · target 2 · +₹2,220 · DRY · taken by you in the dry run"
    assert v["fold_counts"]["legs"] == len(v["kite"]["legs"]) == 4 and v["fold_counts"]["said_next"] == len(v["said_next"])
    assert v["fold_counts"]["log"] == len(v["events"]) + len(v["kite"]["log"])
    # Telegram: "tip N" carries the same word
    assert "<b>DRY · CLOSED · TARGET 2</b>" in msg.tips_card({**v, "pos": None}).split("\n")[0]


def test_waiting_card_steps_and_footer():
    c = cfg()
    hid = seed(c, world(**{KEY12: 300.0}), BN12)["hint_id"]
    tr.poll(c, world(**{KEY12: 300.0}), at("09:50"))
    v = tr.hint_view(bk.load(hid), now=at("09:50"), config=c)
    assert v["state"]["word"] == "WAITING · LEVEL"
    st = {x["step"]: x for x in v["story"]}
    assert st["entry"]["head"] == "waiting for 270" and st["entry"]["tone"] == "warn" and not st["entry"]["done"]
    assert st["now"]["head"] == "300 · +11.1 % from the level"
    assert st["targets"]["head"] == "T1 320 · T2 350 · T3 400+ (runner)"
    assert v["foot"] == v["plan"]["text"]


def test_groups_order_in_the_summary(pushed):
    c = cfg()
    w = world(**{KEY12: 300.0})
    a = seed(c, w, BN12, hhmm="09:43", post_id=1)["hint_id"]
    b2 = seed(c, w, BN13, hhmm="09:50", post_id=2)["hint_id"]
    sk = seed(c, w, BN12, hhmm="10:10", post_id=3)["hint_id"]
    bk.skip(c, sk, now=at("10:11"))
    d = tr.summary(c, now=at("10:12"))
    g = d["groups"]
    assert g["waiting"] == [a, b2] and g["not_taken"] == [sk] and g["live"] == [] and g["closed"] == []
    assert card2.groups([{"id": 1, "posted_at": "a", "state": {"group": "closed", "at": "2"}},
                         {"id": 2, "posted_at": "b", "state": {"group": "closed", "at": "1"}},
                         {"id": 3, "posted_at": "c", "state": {"group": "live"}}, {"id": 4, "posted_at": "d", "state": {"group": "live"}}]) \
        == {"live": [4, 3], "waiting": [], "closed": [1, 2], "not_taken": []}


def test_home_today_and_the_telegram_hint_carry_the_word():
    c = cfg()
    w = world(**{KEY12: 300.0})
    hid = seed(c, w, BN12)["hint_id"]
    bk.skip(c, hid, now=at("09:45"))
    rows = th.home_tips(c, now=at("10:00"))["today"]
    assert rows[0]["state"]["word"] == "SKIPPED BY YOU" and rows[0]["state"]["group"] == "not_taken"
    hid2 = seed(c, w, BN13, hhmm="09:50", post_id=2)["hint_id"]
    assert "now 55 · WAITING · LEVEL" in msg.tips_hint(bk.load(hid2), 55.0)
