"""🤖 AI on Tips (backend/tips/ai4.py) with a fake model: the evening review, the call grade (code) and its read, the
pre-flight read (never delays the arm), the weekly channel profile, the archive move. Every number comes from code;
the model writes labelled sentences only; a line with a figure not in the input is dropped; nothing here acts on an
order."""

import sqlite3
import threading
import time as _t
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.config import IST, AppConfig
from backend.database import archive
from backend.database.models import get_db, now_ist
from backend.tips import ai4
from backend.tips import book as bk
from backend.tips import home as th
from backend.tips import kite_check as kc
from backend.tips import kite_orders as ko
from backend.tips import track as tr
from tests.test_tips_api import api  # noqa: F401
from tests.test_tips_kite_auto import CALL2, S82300, auto_setup, post, pushed, t19  # noqa: F401
from tests.tips_world import DAY, at, cfg, seed, world

ROOT = Path(__file__).resolve().parent.parent
CH = "aakankshatrading"
MON = date(2026, 11, 16)                       # week 47: Mon 16 – Fri 20 Nov 2026
PLAN = {"ok": True, "entry": {"mode": "zone", "lo": 180.0, "hi": 185.0}, "stop": {"kind": "premium", "value": 100.0}, "targets": [250.0],
        "entry_text": "zone 180–185", "stop_text": "stop 100", "targets_text": "250", "time_stop": "15:14"}


@pytest.fixture
def queued(monkeypatch):
    """The AI queue replaced by a list: (kind, row id, key)."""
    out = []
    monkeypatch.setattr(ai4, "enqueue", lambda config, rid, kind, key, note, notify=True: out.append((kind, rid, key)) or {"id": len(out)})
    return out


def ai_cfg(**tips):
    c = cfg()
    c.ai.enabled = True
    for k, v in tips.items():
        setattr(c.tips, k, v)
    return c


_pid = [1000]


def mk(posted: datetime, symbol="NIFTY", side="BUY", score=None, channel=CH, raw="BUY NIFTY 25400 CE 180-185 SL 100 TGT 250", kite=None,
       plan=None, events=None, status="closed") -> dict:
    """A hint row straight into the store (score / kite as given)."""
    _pid[0] += 1
    with get_db() as conn:
        hid = conn.execute("INSERT INTO tips_hints (channel, post_id, created, posted_at, raw, source, status, events) VALUES (?,?,?,?,?,?,?,?)",
                           (channel, _pid[0], now_ist(), posted.isoformat(timespec="seconds"), raw, "live", status, "[]")).lastrowid
    h = bk.load(hid)
    p = dict(plan or PLAN)
    h.update(symbol=symbol, side=side, instrument="option", strike=25400.0, opt_type="CE", lot_size=75, exchange="NFO",
             tradingsymbol=f"{symbol}26NOV25400CE", plan=p, entry=p["entry"], stop=p["stop"], targets=p["targets"], score=score,
             events=events or [], status=status)
    bk.save(h)
    if kite:
        h["kite"] = {**ko.new_kite(), **kite}
        ko.save_kite(h)
    return bk.load(hid)


def sc(first="t1", end="t1", r=1.0, pnl=1000.0, mins=10):
    return {"first": first, "end": end, "r": r, "pnl_lot": pnl, "pnl_pts": pnl / 75, "minutes_to_first": mins, "open": False, "entry_reached": first != "no_fill"}


def closed_kite(day: date, entry=180.0, exit_px=210.0, by="stop", dry=False, qty=75):
    return {"state": "closed", "entry_px": entry, "entry_at": at("10:01", day).isoformat(), "exit_px": exit_px,
            "closed_at": at("10:40", day).isoformat(), "exit_by": by, "qty": qty, "lots": 1, "qty_open": 0,
            "realised": round((exit_px - entry) * qty, 2), "day": day.isoformat(), "dry": dry, "by": "auto"}


# --- review -----------------------------------------------------------------------------------------------------------------

GOOD = ("entry: Filled at 180 in the zone of the post.\n"
        "management: No follow-up changed the stop of 100.\n"
        "exit: Closed at 210 by the stop order, a gain of ₹2,250.\n"
        "channel: The channel posted no result for this call.\n"
        "one thing: The stop at 100 was never near.")


def test_review_keeps_five_lines_drops_a_bad_figure_and_is_once_per_hint_per_day(queued, pushed):  # noqa: F811
    c = ai_cfg()
    h = mk(at("09:50"), kite=closed_kite(DAY))
    now = at("15:35")
    ids = ai4.review_day(c, now)
    assert len(ids) == 1 and queued[0][0] == "review" and queued[0][2] == f"tips_ai_review:{h['id']}:{DAY.isoformat()}"
    row = ai4.load(ids[0])
    src = row["input_json"]["text"]
    assert "entry 180 at 10:01 AM" in src and "exit 210 at 10:40 AM by stop" in src and "Channel's result: none posted" in src
    assert row["input_json"]["numbers"].startswith("entry 180") and row["input_json"]["numbers"].endswith("all from code")
    bad = GOOD.replace("one thing: The stop at 100 was never near.", "one thing: The stop at 999 was never near.")
    before = [dict(r) for r in ko.rows_for(h["id"])]
    ai4.run_row(c, ids[0], generate=lambda s, u: bad, now=at("15:42"))
    row = ai4.load(ids[0])
    labels = [x["label"] for x in row["lines_json"]["lines"]]
    assert labels == ["entry", "management", "exit", "channel"] and row["ok"] == 1          # the 999 line dropped
    assert any("999" in x for x in row["lines_json"]["dropped"]) and "999" in row["lines_json"]["raw"]
    assert [dict(r) for r in ko.rows_for(h["id"])] == before                                  # nothing acts on an order
    assert ai4.review_day(c, at("18:35")) == [] and len(ai4.rows("review", h["id"])) == 1    # once per hint per day
    v = tr.hint_view(bk.load(h["id"]), at("18:40"), config=c)["ai4"]["review"]
    assert v["state"] == "done" and v["at_label"] == "3:42 PM" and v["numbers"].endswith("all from code")


def test_review_five_lines_and_zero_lines_is_no_usable_read(queued):
    c = ai_cfg()
    h1 = mk(at("09:50"), kite=closed_kite(DAY))
    h2 = mk(at("09:55"), kite=closed_kite(DAY, entry=181.0, exit_px=205.0))
    ids = ai4.review_day(c, at("15:35"))
    ai4.run_row(c, ids[0], generate=lambda s, u: GOOD, now=at("15:40"), notify=False)
    assert [x["label"] for x in ai4.load(ids[0])["lines_json"]["lines"]] == list(ai4.LABELS["review"])
    ai4.run_row(c, ids[1], generate=lambda s, u: "Sure! Here is a review {}\nIt went fine at 777.", now=at("15:41"), notify=False)
    r2 = ai4.load(ids[1])
    assert r2["ok"] == 0 and r2["lines_json"]["lines"] == [] and "777" in r2["lines_json"]["raw"]
    v = ai4.card_view(c, bk.load(h2["id"]))["review"]
    assert v["state"] == "empty" and v["text"] == "no usable read"
    assert ai4.card_view(c, bk.load(h1["id"]))["review"]["state"] == "done"


def test_review_dry_labelled_paper_only_skipped_and_off(queued):
    c = ai_cfg()
    d = mk(at("09:50"), kite=closed_kite(DAY, dry=True))
    paper = mk(at("10:05"))
    with get_db() as conn:
        conn.execute("UPDATE tips_hints SET taken = 1 WHERE id = ?", (paper["id"],))
    ids = ai4.review_day(c, at("15:35"))
    assert len(ids) == 1 and ai4.load(ids[0])["hint_id"] == d["id"]                           # the paper-only hint: skipped
    assert "DRY RUN" in ai4.load(ids[0])["input_json"]["text"] and ai4.load(ids[0])["input_json"]["dry"] is True
    off = ai_cfg(ai_review=False)
    mk(at("10:20"), kite=closed_kite(DAY))
    assert ai4.review_day(off, at("15:35")) == []                                              # the switch off: nothing


def test_review_telegram_shape_once_a_day_and_hidden_when_nothing_taken(queued, pushed):  # noqa: F811
    c = ai_cfg()
    assert ai4.review_message(DAY.isoformat()) is None and not ai4.send_review_day(c, DAY.isoformat())   # nothing taken: hidden
    a = mk(at("09:50"), kite=closed_kite(DAY, by="you", exit_px=195.22), score=sc(pnl=1500.0))
    b = mk(at("13:02"), kite=closed_kite(DAY, entry=440.0, exit_px=439.0, by="stop", dry=True), score=sc("stop", "stop", -1.0, -75.0))
    skipped = mk(at("14:10"), score=sc("no_fill", "no_fill", None, 0.0))
    for h, g in ((a, "B"), (b, "C"), (skipped, "D")):
        ai4.insert("grade", h["id"], DAY.isoformat(), {"grade": g}, at("09:00"))
    ids = ai4.review_day(c, at("15:35"))
    ai4.run_row(c, ids[0], generate=lambda s, u: "one thing: The exit came before the target.", now=at("15:40"))
    assert not pushed                                                                           # the second review is still to come
    ai4.run_row(c, ids[1], generate=lambda s, u: "nothing usable", now=at("15:42"))
    msg = [p for p in pushed if p.startswith("💡 Tips · evening review")]
    assert msg == ["💡 Tips · evening review · Tue 17 Nov\n"
                   f"#{a['id']} NIFTY 25400 CE · +₹1,142 · your exit\n"
                   "[AI] The exit came before the target.\n"
                   f"#{b['id']} DRY NIFTY 25400 CE · −₹75 · stop\n"
                   "[AI] no usable read\n"
                   "day: 2 taken · +₹1,066 · all calls 1 lot +₹1,425\n"
                   f"grades today: #{a['id']} B · #{b['id']} C · 1 skipped (grades D×1)"]
    assert not ai4.send_review_day(c, DAY.isoformat(), at("18:35"))                         # once a day


# --- grade ----------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("rec,shp,words,want", [
    ({"enough": True, "win_pct": 70.0, "avg_r": 0.5}, {"rr": 2.0}, [{"key": "stop_missing"}], "D"),     # stop missing first
    ({"enough": False, "win_pct": 90.0, "avg_r": 1.0}, {"rr": 3.0}, [], "–"),                            # too few calls like this
    ({"enough": True, "win_pct": 70.0, "avg_r": -0.1}, {"rr": 2.0}, [], "D"),                            # the record negative
    ({"enough": True, "win_pct": 60.0, "avg_r": 0.4}, {"rr": 1.5}, [], "A"),
    ({"enough": True, "win_pct": 59.9, "avg_r": 0.4}, {"rr": 1.5}, [], "B"),
    ({"enough": True, "win_pct": 50.0, "avg_r": 0.1}, {"rr": 0.8}, [], "B"),
    ({"enough": True, "win_pct": 40.0, "avg_r": 0.1}, {"rr": 1.2}, [], "B"),
    ({"enough": True, "win_pct": 40.0, "avg_r": 0.1}, {"rr": 1.19}, [], "C"),
    ({"enough": True, "win_pct": 40.0, "avg_r": 0.0}, {"rr": None}, [], "C"),
])
def test_grade_thresholds(rec, shp, words, want):
    assert ai4.grade_of(rec, shp, words)[0] == want
    assert ai4.GRADE == {"days": 60, "min_calls": 3, "A": {"win": 60.0, "rr": 1.5}, "B": {"win": 50.0, "rr": 1.2}}


def test_grade_record_falls_back_like_then_symbol_then_all_and_says_which():
    new = mk(at("14:20"), status="new", score=None)
    assert ai4.record_like(new)["calls"] == 0 and ai4.grade_input(new, at("14:20"))["grade"] == "–"
    assert "too few calls like this" in ai4.grade_input(new, at("14:20"))["record_text"]
    for d in (3, 4):                                                       # 2 NIFTY buys after 2 PM: not enough
        mk(at("14:30", DAY - timedelta(days=d)), score=sc())
    mk(at("09:30", DAY - timedelta(days=5)), side="SELL", score=sc("stop", "stop", -1.0, -500.0))    # a 3rd NIFTY call
    r = ai4.record_like(new)
    assert r["level"] == "symbol" and r["calls"] == 3 and r["label"] == "NIFTY calls (any side, any hour)"
    mk(at("14:05", DAY - timedelta(days=6)), score=sc("stop", "stop", -1.0, -500.0))               # the 3rd like it
    r = ai4.record_like(new)
    assert r["level"] == "like" and r["calls"] == 3 and r["label"] == "NIFTY buys posted after 2 PM" and r["won"] == 2
    assert r["t1_first"] == 2 and r["stop_first"] == 1 and r["win_pct"] == 66.7
    other = mk(at("11:00"), symbol="SENSEX", status="new", score=None)
    assert ai4.record_like(other)["level"] == "all" and ai4.record_like(other)["calls"] == 4
    mk(at("10:00", DAY - timedelta(days=70)), symbol="SENSEX", score=sc())                           # older than 60 days: out
    assert ai4.record_like(other)["calls"] == 4
    g = ai4.grade_input(new, at("14:20"))
    assert g["shape"]["rr"] == round((250 - 182.5) / (182.5 - 100), 2) and "R/R 0.82" in g["shape_text"]
    assert g["grade"] == "B" and g["record"]["level"] == "like"


def test_grade_at_ingest_regraded_once_on_plan_change_only_and_the_chip_on_home(queued):
    c = ai_cfg()
    w = world()
    r = seed(c, w, "BUY NIFTY 24400 CE 180-185 when comes\nSL 100\nTGT 250\nBtst", hhmm="14:20")
    hid = r["hint_id"]
    gs = ai4.rows("grade", hid)
    assert len(gs) == 1 and queued[-1][0] == "grade"
    d = gs[0]["input_json"]
    assert d["grade"] == "–" and "the post says carry / Btst" in d["shape_text"] and 'the post says "if comes"' in d["shape_text"]
    h = bk.load(hid)
    assert ai4.grade_hint(c, h, at("14:21")) is None                                         # the same plan: no new grade
    h["plan"] = {**h["plan"], "stop": {"kind": "premium", "value": 150.0}}
    bk.save(h)
    assert ai4.grade_hint(c, bk.load(hid), at("14:25")) and len(ai4.rows("grade", hid)) == 2  # the plan changed: once
    h = bk.load(hid)
    h["plan"] = {**h["plan"], "stop": {"kind": "premium", "value": 160.0}}
    bk.save(h)
    assert ai4.grade_hint(c, bk.load(hid), at("14:30")) is None and len(ai4.rows("grade", hid)) == 2
    v = tr.hint_view(bk.load(hid), at("14:31"), config=c)["ai4"]["grade"]
    assert v["grade"] == "–" and v["tone"] == "" and v["read"]["state"] == "pending" and v["regraded"]
    ai4.run_row(c, ai4.rows("grade", hid)[-1]["id"], generate=lambda s, u: "read: Too few calls like this to judge; the post says Btst.")
    v = ai4.card_view(c, bk.load(hid))["grade"]
    assert v["read"]["lines"] == [{"label": "read", "text": "Too few calls like this to judge; the post says Btst."}]
    home = th.home_tips(c, at("14:35"))
    assert [t["grade"] for t in home["today"] if t["id"] == hid] == [{"g": "–", "c": ""}]
    off = ai_cfg(ai_grade=False)
    r2 = seed(off, w, "BUY NIFTY 24150 PE 90-95\nSL 60\nTGT 140", hhmm="14:40", post_id=99)
    assert ai4.rows("grade", r2["hint_id"]) == []                                            # the switch off: no grade


def test_auto_ignores_the_grade_arming_identical_with_grade_d(pushed, queued, monkeypatch):  # noqa: F811
    c, w, s = auto_setup()
    c.ai.enabled = True
    monkeypatch.setattr(ai4, "grade_of", lambda rec, shp, words: ("D", "forced for the test"))
    w.prices[S82300] = 309.0
    r = post(c, w, s, 1, "13:52", CALL2)
    assert ai4.grade_letter(r["hint_id"]) == "D"
    k = bk.load(r["hint_id"])["kite"]
    assert k["state"] == "armed" and k["by"] == "auto" and ko.rows_for(r["hint_id"])[0]["order_type"] == "SL"
    line = [p for p in pushed if "armed" in p]                          # the call's message carries the arm (T3)
    assert len(line) == 1 and line[0].split("\n")[-1] == "auto DRY: armed 1 lot · SL trigger 370 · resting on Kite · activation words: the level decides"
    src = (ROOT / "backend/tips/kite_auto.py").read_text() + (ROOT / "backend/tips/kite_orders.py").read_text()
    assert "grade" not in src.replace("upgrade", "")                                         # the arming code never reads it


# --- preflight ------------------------------------------------------------------------------------------------------------------

def test_preflight_off_queues_nothing(pushed, queued):  # noqa: F811
    c, w, s = auto_setup()
    c.ai.enabled = True
    assert c.tips.ai_preflight is False                                                       # off by default
    w.prices[S82300] = 309.0
    r = post(c, w, s, 1, "13:52", CALL2)
    assert bk.load(r["hint_id"])["kite"]["state"] == "armed"
    assert ai4.rows("preflight") == [] and not [q for q in queued if q[0] == "preflight"]
    assert ai4.card_view(c, bk.load(r["hint_id"]))["preflight"] is None


def test_preflight_on_is_queued_before_the_arm_and_never_delays_it(pushed, monkeypatch):  # noqa: F811
    c, w, s = auto_setup(ai_preflight=True)
    c.ai.enabled = True
    seq, threads = [], []

    def slow(system, user):
        _t.sleep(1.5)                                                                          # a slow model
        return "plan vs post: Buy SENSEX 82300 CE above 370, stop 200, target 500. agrees\nflag: none"

    def fake_enqueue(config, rid, kind, key, note, notify=True):
        seq.append((kind, _t.monotonic()))
        if kind == "preflight":
            th_ = threading.Thread(target=lambda: ai4.run_row(config, rid, generate=slow, now=t19("13:52", 3)))
            th_.start()
            threads.append(th_)
        return {"id": 1}
    monkeypatch.setattr(ai4, "enqueue", fake_enqueue)
    real_place = ko._place

    def place(*a, **kw):
        seq.append(("order", _t.monotonic()))
        return real_place(*a, **kw)
    monkeypatch.setattr(ko, "_place", place)
    w.prices[S82300] = 309.0
    t0 = _t.monotonic()
    r = post(c, w, s, 1, "13:52", CALL2)
    took = _t.monotonic() - t0
    assert bk.load(r["hint_id"])["kite"]["state"] == "armed" and took < 1.0                  # the arm did not wait for the read
    kinds = [k for k, _ in seq if k in ("preflight", "order")]
    assert kinds[:2] == ["preflight", "order"]                                                # queued first, the order at once after
    row = ai4.rows("preflight", r["hint_id"])[0]
    assert row["ok"] is None and "Contract: SENSEX26NOV82300CE" in row["input_json"]["text"]    # still being read
    for x in threads:
        x.join(5)
    v = ai4.card_view(c, bk.load(r["hint_id"]))["preflight"]
    assert v["state"] == "done" and v["agrees"] is True and v["flag"] is None and v["switch"] == "on · Settings › tips · ai_preflight"
    assert not [p for p in pushed if "pre-flight" in p]


def test_preflight_differs_telegram_and_home_flag_on_the_card_only(pushed, queued):  # noqa: F811
    c, w, s = auto_setup(ai_preflight=True)
    c.ai.enabled = True
    w.prices[S82300] = 309.0
    r = post(c, w, s, 1, "13:52", CALL2)
    hid = r["hint_id"]
    rid = ai4.rows("preflight", hid)[0]["id"]
    ai4.run_row(c, rid, generate=lambda s_, u: "plan vs post: Buy above 370, stop 200, target 500. differs: the post asks to wait for activation\nflag: none",
                now=t19("13:53"))
    assert f"[AI] #{hid} pre-flight: plan differs from the post · the post asks to wait for activation · armed anyway · check" in pushed
    att = kc.attention_items(t19("13:54"))
    assert ("info", f"💡 [AI] #{hid} pre-flight: plan differs from the post · the post asks to wait for activation · armed anyway · check") in att
    assert bk.load(hid)["kite"]["state"] == "armed"                                           # told, never blocked
    # a flag: an amber line on the card only
    rid2 = ai4.insert("preflight", hid, "2026-11-19", ai4.load(rid)["input_json"] | {"arm": "x"}, t19("13:55"))
    n = len(pushed)
    ai4.run_row(c, rid2, generate=lambda s_, u: "plan vs post: Buy above 370, stop 200. agrees\nflag: the post says wait for activation", now=t19("13:56"))
    v = ai4.card_view(c, bk.load(hid))["preflight"]
    assert v["agrees"] is True and v["flag"] == "the post says wait for activation" and len(pushed) == n


# --- weekly ------------------------------------------------------------------------------------------------------------------------

def seed_week():
    d = lambda i, hhmm: at(hhmm, MON + timedelta(days=i))
    mk(d(0, "09:30"), score=sc("t1", "t1", 1.5, 1500.0, 12))
    mk(d(0, "10:45"), score=sc("t1", "t2", 2.0, 2000.0, 20),
       kite=closed_kite(MON, entry=180.0, exit_px=200.0, by="you"), events=[{"t": d(0, "11:00").isoformat(), "text": 'channel: "book 200"', "src": "channel"}])
    mk(d(1, "12:30"), score=sc("stop", "stop", -1.0, -600.0, 5))
    mk(d(2, "14:10"), score=sc("no_fill", "no_fill", None, 0.0, None))
    mk(d(2, "09:40"), symbol="BANKNIFTY", score=sc("stop", "stop", -1.0, -900.0, 7))
    mk(d(3, "10:00"), symbol="BANKNIFTY", score=sc("t1", "channel_exit", 0.5, 400.0, 30))
    mk(d(4, "15:00"), symbol="SENSEX", score=sc("time", "time", -0.2, -100.0, None))


def test_weekly_numbers_and_bar_widths_from_code():
    seed_week()
    mk(at("10:00", MON + timedelta(days=7)), score=sc())                                     # next week: not counted
    n = ai4.week_numbers(CH, MON)
    tw = n["this_week"]
    assert (tw["calls"], tw["t1_first"], tw["stop_first"], tw["no_fill"], tw["took"]) == (7, 3, 2, 1, 1)
    assert tw["all_1lot"] == 2300.0 and tw["yours"] == 1500.0 and tw["avg_r"] == round((1.5 + 2.0 - 1.0 - 1.0 + 0.5 - 0.2) / 6, 2)
    assert n["label"] == "week 47 · 16–20 Nov"
    assert [(b["label"], b["n"], b["won"], b["w"], b["tone"]) for b in n["by_index"]] == [
        ("NIFTY", 4, 2, 100, "ok"), ("BANKNIFTY", 2, 1, 50, "ok"), ("SENSEX", 1, 0, 25, "bad")]
    assert [(b["label"], b["n"], b["w"]) for b in n["by_hour"]] == [("9:15–10:30", 3, 100), ("10:30–12", 1, 33), ("12–2 PM", 1, 33), ("after 2 PM", 2, 67)]
    assert [(b["label"], b["n"], b["w"]) for b in n["ends"]] == [("target", 2, 100), ("booked early", 1, 50), ("stop", 2, 100), ("time stop", 1, 50)]
    assert n["median_t1_min"] == 20 and n["median_stop_min"] == 6
    hand = n["hand_exits"][0]
    assert hand["yours"] == 1500.0 and hand["watch"] == round(2000.0 / 75 * 75, 2) and hand["diff"] == 500.0
    assert n["followups"]["count"] == 1
    txt = ai4.week_text(n)
    assert "This week: 7 calls · T1 first 3 · stop first 2 · no fill 1" in txt and "the watch would have added +₹500" in txt


def test_weekly_sunday_job_write_now_guard_and_telegram(queued, pushed):  # noqa: F811
    from backend.alerts.jobs import Scheduler
    c = ai_cfg()
    seed_week()
    sun = datetime(2026, 11, 22, 19, 5, tzinfo=IST)
    assert ai4.due(sun, c, set(), False) == ["tips_ai_weekly"] and ai4.due(sun - timedelta(minutes=1), c, set(), False) == []
    assert ai4.due(sun, c, {"tips_ai_weekly"}, False) == [] and ai4.due(sun - timedelta(days=1), c, set(), False) == []
    sch = Scheduler(SimpleNamespace(config=c))
    res = sch.run_tips_ai_weekly(sun)
    assert res["status"] == "ok" and len(res["queued"]) == 1 and queued[-1][0] == "weekly"
    assert "tips_ai_weekly" in Scheduler.done_today(sun.date())
    rid = res["queued"][0]
    assert ai4.weekly_due(c, sun) == [rid] and len(ai4.rows("weekly")) == 1                  # one a week: the pending row is queued again
    ai4.run_row(c, rid, generate=lambda s, u: ("works: Morning NIFTY calls reached T1 first 3 times.\n"
                                                "fails: SENSEX lost at the time stop.\n"
                                                "your exits: Your hand exit left ₹500 to the watch.\n"
                                                "follow-ups: 1 follow-up this week.\n"
                                                "extra: nothing"), now=sun)
    tg = [p for p in pushed if p.startswith("📊")]
    assert tg == ["📊 Tips · channel profile · week 47\n7 calls · T1 first 3 · stop first 2 · avg +0.3 R\n"
                  "[AI] Morning NIFTY calls reached T1 first 3 times.\n[AI] SENSEX lost at the time stop.\n"
                  "[AI] Your hand exit left ₹500 to the watch.\n[AI] 1 follow-up this week.\n"
                  "you: 1 of 7 · +₹1,500 · all: +₹2,300\nfull profile: Research › Tips › Track"]
    page = ai4.weekly_page(c, MON.isoformat(), now=sun)
    p = page["panels"][0]
    assert len(page["weeks"]) == 8 and page["weeks"][0]["week"] == MON.isoformat() and p["ai"]["state"] == "done"
    assert ai4.weekly_page(c, now=sun)["week"] == MON.isoformat()                            # Sunday: the week just ended
    fri = datetime(2026, 11, 27, 12, 0, tzinfo=IST)                                           # a weekday: this week so far, then the last 7
    assert [w["label"] for w in ai4.weekly_page(c, now=fri)["weeks"][:2]] == ["week 48 · 23–27 Nov · so far", "week 47 · 16–20 Nov"]
    assert [x["label"] for x in p["ai"]["lines"]] == ["works", "fails", "your exits", "follow-ups"] and p["written"] == "written Sun 7:05 PM"
    out = ai4.weekly_now(c, MON, sun + timedelta(minutes=5))
    assert len(out["rows"]) == 1 and len(ai4.rows("weekly")) == 2                            # Write now: a new row, the old one kept
    with pytest.raises(ValueError, match="one Write now per hour"):
        ai4.weekly_now(c, MON, sun + timedelta(minutes=30))
    assert ai4.weekly_now(c, MON, sun + timedelta(minutes=66))["rows"]


def test_weekly_route_and_write_now_refused_while_the_ai_is_off(api, queued):  # noqa: F811
    client = api
    seed_week()
    r = client.get(f"/api/tips/ai/weekly?week={MON.isoformat()}")
    assert r.status_code == 200 and r.json()["panels"][0]["numbers"]["this_week"]["calls"] == 7
    assert client.post("/api/tips/ai/weekly/write", json={}).status_code == 409             # the local AI is off by default


# --- the store, the README --------------------------------------------------------------------------------------------------------

def test_tips_ai_rows_are_moved_by_the_archive_never_deleted():
    now = datetime(2026, 11, 20, 18, 0, tzinfo=IST)
    old = ai4.insert("weekly", None, "2026-05-04", {"text": "old"}, now - timedelta(days=200))
    young = ai4.insert("review", 3, "2026-09-01", {"text": "young"}, now - timedelta(days=100))
    r = archive.run(AppConfig(), now)
    assert r["moved_by_table"]["tips_ai"] == 1
    with get_db() as conn:
        live = [x[0] for x in conn.execute("SELECT id FROM tips_ai")]
    a = sqlite3.connect(str(archive.archive_path()))
    arch = a.execute("SELECT id, input_json FROM tips_ai").fetchall()
    a.close()
    assert live == [young] and arch == [(old, '{"text": "old"}')]


def test_parse_labels_figures_words_and_broker_names():
    src = "entry 180 · stop 100 · best +12.5 % · ₹1,650"
    out = ai4.parse("review", "1. **Entry:** filled at 180 at the zone top.\n- [AI] management: best +12.5 % worth ₹1,650.\nexit - stopped at 12.5 rupees ₹12.5\n"
                    "Channel: the broker zerodha said so\nONE THING: " + " ".join(["word"] * 40), src)
    labs = {x["label"]: x["text"] for x in out["lines"]}
    assert labs["entry"] == "filled at 180 at the zone top." and labs["management"] == "best +12.5 % worth ₹1,650."
    assert "exit" not in labs and "channel" not in labs and labs["one thing"].endswith("…") and len(labs["one thing"].split()) == 30


def test_readme_has_the_ai_on_tips_section():
    t = (ROOT / "README.md").read_text()
    i = t.index("### 🤖 AI on Tips")
    sec = t[i:i + 6000]
    for w in ("ai_review", "ai_grade", "ai_preflight", "ai_weekly", "Evening review", "Call grade", "Pre-flight read", "Weekly channel profile",
              "nothing here places, modifies, cancels, holds or blocks an order"):
        assert w in sec, w


def test_parse_drops_bare_values_that_are_not_sentences():
    """20 Nov's Sunday message carried "[AI] none" / "[AI] 1" / "[AI] 3 calls out of 9": a line needs MIN_WORDS words."""
    from backend.tips import ai4
    src = "9 calls · T1 first 4 · stop first 0 · 3 calls · 2 calls no fill · 57 follow-ups"
    raw = "works: none\nfails: 1\nyour exits: 3 calls out of 9\nfollow-ups: 57 follow-ups were read the same way by the app and the second opinion"
    assert not ai4._is_sentence("2 calls no fill") and not ai4._is_sentence("57 total, no AI agreement") and not ai4._is_sentence("#11 at 247.55 (10:51 AM) +₹1,141")
    assert ai4._is_sentence("1 follow-up this week.") and ai4._is_sentence("best +12.5 % worth ₹1,650.")
    out = ai4.parse("weekly", raw, src)
    assert [x["label"] for x in out["lines"]] == ["follow-ups"]
    assert len(out["dropped"]) == 3


def test_parse_keeps_the_preflight_flag_none():
    from backend.tips import ai4
    out = ai4.parse("preflight", "plan vs post: the plan matches the post, buy NIFTY 25400 CE between 180 and 185 · agrees\nflag: none",
                    "NIFTY 25400 CE 180 185")
    assert [x["label"] for x in out["lines"]] == ["plan vs post", "flag"]


def test_weekly_message_has_no_ai_line_when_nothing_usable():
    from backend.tips import ai4
    row = {"day": "2026-11-16", "id": 1, "input_json": {"channel": "x", "numbers": {"week": "2026-11-16", "this_week": {"calls": 9, "t1_first": 4, "stop_first": 0, "avg_r": 0.7, "took": 3, "yours": 832, "all_1lot": 15427}}},
           "lines_json": {"lines": []}}
    msg = ai4.weekly_message(row)
    assert "[AI]" not in msg and "no usable read" not in msg
    assert "9 calls" in msg and "you: 3 of 9" in msg


def test_weekly_sunday_pushes_only_configured_or_busy_channels(monkeypatch):
    from backend.tips import ai4
    from datetime import date
    cfg = type("C", (), {"tips": type("T", (), {"channels": "Index Options Buying with Aakanksha"})()})()
    monkeypatch.setattr(ai4, "channels_in", lambda mon: ["aakankshatrading", "index options buying with aakanksha", "other"])
    monkeypatch.setattr(ai4, "week_numbers", lambda ch, mon: {"this_week": {"calls": {"aakankshatrading": 1, "index options buying with aakanksha": 9, "other": 2}[ch]}})
    assert ai4.weekly_channels_due(cfg, date(2026, 11, 16)) == ["index options buying with aakanksha", "other"]
    sent = []
    monkeypatch.setattr(ai4, "weekly_write", lambda config, ch, mon, now, force=False, notify=True: sent.append((ch, notify)) or 1)
    monkeypatch.setattr(ai4, "last_week", lambda now: date(2026, 11, 16))
    ai4.weekly_due(cfg, notify=True)
    assert dict(sent) == {"aakankshatrading": False, "index options buying with aakanksha": True, "other": True}
