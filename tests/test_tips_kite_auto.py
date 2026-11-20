"""⚡ Tips → Kite orders PR C: entry levels (premium vs index), stop-entry and armed entries, AUTO mode, the 19 Nov
follow-up shapes. The level is the trigger: activation words never gate an entry that has a level."""

from datetime import date

import pytest

from backend.tips import book as bk
from backend.tips import kite_auto as ka
from backend.tips import kite_orders as ko
from backend.tips import plan as pl
from backend.tips import reader as rd
from backend.tips import track as tr
from backend.tips.book import TipsError
from tests.test_tips_kite import Broker
from tests.tips_world import BN12, BN13, KEY13, FakeClient, at, cfg, opt, seed, world

D19 = date(2026, 11, 19)
EXP = date(2026, 11, 20)
CH = "index options buying with aakanksha"
S82200, S82300, S82400 = "BFO:SENSEX26NOV82200CE", "BFO:SENSEX26NOV82300CE", "BFO:SENSEX26NOV82400PE"
SPOT = "BSE:SENSEX"
CALL1 = "BUY SENSEX 82200 CE NEAR 530-540\nSL 480\nTGT 600/650/700"
CALL2 = "BUY SENSEX 82300 CE ONLY ABOVE 370\nSL 200\nTGT 500\nWAIT FOR ACTIVATION"
CALL3 = "BUY SENSEX 82400 PE ABOVE 306\nSL 250\nTGT 380/420"
SPOTCALL = "BUY SENSEX 82300 CE above 82400 spot\nSL 200\nTGT 500"
WAITZONE = "BUY SENSEX 82300 CE NEAR 360-370\nSL 300\nTGT 450\nWAIT FOR ACTIVATION"


def t19(hhmm, sec=0):
    return at(hhmm, D19, sec)


def sworld(**prices) -> FakeClient:
    base = world()
    base.dumps["BFO"] = [opt("SENSEX", s, t, EXP, seg="BFO-OPT", lot=20) for s in (82200, 82300, 82400) for t in ("CE", "PE")]
    base.prices.update({SPOT: 82236.0, **prices})
    return base


@pytest.fixture
def pushed(monkeypatch):
    out = []
    monkeypatch.setattr(bk, "push", lambda config, key, html: out.append(html) or True)
    monkeypatch.setattr(rd, "PUSH_MAX_AGE_S", 10 ** 9)            # the fixed test clock counts as fresh
    return out


def post(c, w, client, pid, hhmm, text):
    """One channel post through the real ingest path (the Kite hooks run with `client`)."""
    for ex, names in (("NFO", {"BANKNIFTY", "NIFTY"}), ("BFO", {"SENSEX"})):
        tr.refresh_instruments(w, ex, names, D19)
    return rd.ingest(c, CH, pid, t19(hhmm), text, False, source="live", push=True, model=False, client=client)


def tick(c, w, prices: dict, hhmm, sec=0):
    """Quotes (the tracker), then the order book and the housekeeping (the Syncer's pass)."""
    w.prices.update(prices)
    now = t19(hhmm, sec)
    tr.poll(c, w, now)
    s = ko.sim()
    watch = ko.watch_all(c, w, now)                           # the Syncer's order: the target watch, then the books
    out = ko.sync(c, s, now)
    out["watch"] = watch
    out["house"] = ka.housekeep(c, s, now)
    out.update(ko.sync(c, s, t19(hhmm, sec + 1)) if out["house"] else {})
    return out


def auto_setup(**kw):
    c = cfg()
    c.tips.kite_mode = "auto"
    c.tips.kite_entry_until = "15:00"
    for k, v in kw.items():
        setattr(c.tips, k, v)
    return c, sworld(), ko.sim()


# --- decode + plan ------------------------------------------------------------------------------------------------------------

def test_plan_step_fills_on_an_index_cross_at_the_option_ltp():
    c = cfg()
    from backend.tips import decode as dc
    d = dc.decode(SPOTCALL, D19)
    h = {**d, "exchange": "BFO"}
    p = pl.build(h, c.tips, t19("10:00"))
    assert p["entry_text"] == "SENSEX above 82,400" and p["level_kind"] == "spot" and "wait for SENSEX above 82,400 (index)" in p["lines"]
    run = pl.new_run(p)
    assert pl.step(run, p, 300.0, t19("10:01"), spot=82300.0) == []
    ev = pl.step(run, p, 318.5, t19("10:02"), spot=82410.0)
    assert ev == [{"kind": "fill", "px": 318.5, "frac": 0.0}] and run["entry_px"] == 318.5
    # wait words with a level: the level decides; wait words with a zone: the channel's "active"
    p2 = pl.build({**dc.decode(CALL2, D19), "exchange": "BFO"}, c.tips, t19("13:52"))
    assert "activation words · the level decides" in p2["lines"] and not p2["needs_activation"]
    r2 = pl.new_run(p2)
    assert pl.step(r2, p2, 371.0, t19("13:55"))[0]["kind"] == "fill"
    p3 = pl.build({**dc.decode(WAITZONE, D19), "exchange": "BFO"}, c.tips, t19("13:52"))
    r3 = pl.new_run(p3)
    assert p3["needs_activation"] and pl.step(r3, p3, 365.0, t19("13:55")) == []
    r3["allowed"] = True
    assert pl.step(r3, p3, 366.0, t19("13:56"))[0]["kind"] == "fill"


# --- click: stop-entry, LIMIT, spot watch, armed_wait -----------------------------------------------------------------------

def test_click_place_all_rests_a_stop_entry_fills_in_the_sim_then_sl_and_t1(pushed):
    c = cfg()
    c.tips.kite_mode = "dry"                                                # a click through the simulator
    w = sworld(**{S82300: 309.0})
    s = ko.sim()
    hid = post(c, w, w, 1, "13:52", CALL2)["hint_id"]
    tr.poll(c, w, t19("13:52", 30))
    h = ko.place_all(c, s, hid, 1, now=t19("13:53"))
    k = h["kite"]
    assert k["state"] == "armed" and k["arm"]["kind"] == "slm"
    e = ko.rows_for(hid)[0]
    assert (e["order_type"], e["trigger"], e["side"], e["qty"], e["status"]) == ("SL", 370, "BUY", 20, "placed")
    assert e["tradingsymbol"] == "SENSEX26NOV82300CE"
    legs = {x["leg"]: x for x in tr.hint_view(bk.load(hid), config=c)["kite"]["legs"]}
    assert legs["entry"]["type_text"] == "SL trigger 370" and legs["entry"]["status_text"] == "resting (dry)"
    assert any("entry placed BUY 20 SL trigger 370 · resting on Kite" in p for p in pushed)
    assert ko.entries_today(D19.isoformat(), dry=True) == 0                     # resting: not an entry yet
    r = tick(c, w, {S82300: 340.0}, "13:55")
    assert r["lines"] == [] and bk.load(hid)["kite"]["state"] == "armed"         # under the trigger: still resting
    r = tick(c, w, {S82300: 372.0}, "13:58")
    assert r["lines"][0] == "⚡ Kite DRY · #1 entry filled 370 ×20 · SL 200 placed · T1 500 watched by the app"
    assert ko.entries_today(D19.isoformat(), dry=True) == 1
    k = bk.load(hid)["kite"]
    assert k["legs"]["stop"]["trigger"] == 200 and "t1" not in k["legs"] and k["check"]["ok"]
    legs = {x["leg"]: x for x in tr.hint_view(bk.load(hid), config=c)["kite"]["legs"]}
    assert legs["t1"]["status"] == "watched" and legs["t1"]["type_text"] == "at ≥ 500" and legs["t1"]["detail"].startswith("watched by the app")


def test_premium_level_already_passed_goes_at_market_or_is_not_chased(pushed):
    c = cfg()
    c.tips.kite_mode = "dry"                                                # a click through the simulator
    w = sworld(**{S82300: 380.0})
    hid = post(c, w, w, 1, "13:52", CALL2)["hint_id"]
    tr.poll(c, w, t19("13:52", 30))
    h = ko.place_entry(c, ko.sim(), hid, 1, now=t19("13:53"))
    assert ko.rows_for(hid)[0]["order_type"] == "MARKET" and "already past 370" in h["kite"]["log"][-1]["text"]
    hid2 = post(c, w, w, 2, "13:54", CALL2.replace("82300", "82200"))["hint_id"]
    w.prices["BFO:SENSEX26NOV82200CE"] = 455.0
    tr.poll(c, w, t19("13:54", 30))
    with pytest.raises(TipsError, match="not chased"):
        ko.place_entry(c, ko.sim(), hid2, 1, now=t19("13:55"))
    assert ko.rows_for(hid2) == []


def test_zone_rests_as_a_limit_at_its_top_and_fills(pushed):
    c = cfg()
    c.tips.kite_mode = "dry"                                                # a click through the simulator
    w = sworld(**{S82200: 560.0})
    hid = post(c, w, w, 1, "12:50", CALL1)["hint_id"]
    tr.poll(c, w, t19("12:50", 30))
    ko.place_all(c, ko.sim(), hid, 1, now=t19("12:51"))
    e = ko.rows_for(hid)[0]
    assert (e["order_type"], e["price"], e["status"]) == ("LIMIT", 540, "placed")
    r = tick(c, w, {S82200: 538.0}, "12:55")
    assert r["lines"][0].startswith("⚡ Kite DRY · #1 entry filled 540 ×20 · SL 480 placed · T1 600 watched by the app")


def test_spot_watch_arms_and_buys_at_market_on_the_index_cross(pushed):
    c = cfg()
    c.tips.kite_mode = "dry"                                                # a click through the simulator
    w = sworld(**{S82300: 300.0})
    hid = post(c, w, w, 1, "10:00", SPOTCALL)["hint_id"]
    tr.poll(c, w, t19("10:00", 30))
    h = ko.place_all(c, ko.sim(), hid, 1, now=t19("10:01"))
    k = h["kite"]
    assert k["state"] == "armed_watch" and k["arm"]["index"] == "SENSEX" and k["arm"]["level"] == 82400 and ko.rows_for(hid) == []
    v = tr.hint_view(bk.load(hid), config=c)["kite"]
    legs = {x["leg"]: x for x in v["legs"]}
    assert legs["entry"]["type_text"] == "MARKET when SENSEX ≥ 82,400" and legs["entry"]["status_text"] == "armed"
    assert "now 82,236" in legs["entry"]["detail"] and v["actions"] == ["cancel:entry"]
    assert "the app watches SENSEX and buys at market when it trades 82,400 or above" in v["entry_what"]
    r = tick(c, w, {SPOT: 82390.0, S82300: 310.0}, "10:05")
    assert bk.load(hid)["kite"]["state"] == "armed_watch" and ko.rows_for(hid) == []
    r = tick(c, w, {SPOT: 82415.0, S82300: 318.0}, "10:06")
    rows = ko.rows_for(hid)
    assert rows[0]["order_type"] == "MARKET" and rows[0]["by"] == "dry" and rows[0]["filled_px"] == 318.0
    assert any("entry sent BUY 20 MARKET · SENSEX 82,415 ≥ 82,400" in p for p in pushed)
    assert any(ln.startswith("⚡ Kite DRY · #1 entry filled 318 ×20 · SL 200") for ln in r["lines"])


def test_armed_wait_holds_until_active_and_send_now_overrides(pushed):
    c = cfg()
    c.tips.kite_mode = "dry"                                                # a click through the simulator
    w = sworld(**{S82300: 365.0})
    hid = post(c, w, w, 1, "11:00", WAITZONE)["hint_id"]
    tr.poll(c, w, t19("11:00", 30))
    h = ko.place_all(c, ko.sim(), hid, 1, now=t19("11:01"))
    assert h["kite"]["state"] == "armed_wait" and ko.rows_for(hid) == []
    v = tr.hint_view(bk.load(hid), config=c)["kite"]
    assert v["needs_activation"] and set(v["actions"]) == {"send_now", "cancel:entry"}
    assert {x["leg"]: x for x in v["legs"]}["entry"]["status_text"] == "armed · waiting for activation"
    tick(c, w, {S82300: 366.0}, "11:05")
    assert ko.rows_for(hid) == []                                          # nothing is sent until the channel says active
    post(c, w, w, 2, "11:10", "activated")
    rows = ko.rows_for(hid)
    assert rows[0]["order_type"] == "MARKET" and rows[0]["by"] == "dry"     # the channel considers it live
    # Send now on another armed_wait hint skips the activation
    hid2 = post(c, w, w, 3, "11:20", WAITZONE.replace("82300", "82200"))["hint_id"]
    w.prices[S82200] = 362.0
    tr.poll(c, w, t19("11:20", 30))
    ko.place_entry(c, ko.sim(), hid2, 1, now=t19("11:21"))
    h2 = ko.send_now(c, ko.sim(), hid2, now=t19("11:22"))
    assert ko.rows_for(hid2)[0]["order_type"] == "LIMIT" and ko.rows_for(hid2)[0]["price"] == 370 and h2["kite"]["state"] == "armed"


def test_window_end_time_stop_and_avoid_cancel_an_unfilled_entry_and_never_count(pushed):
    c = cfg()
    c.tips.kite_mode = "dry"                                                # a click through the simulator
    w = sworld(**{S82300: 309.0, S82200: 300.0, S82400: 280.0})
    s = ko.sim()
    h1 = post(c, w, w, 1, "13:52", CALL2)["hint_id"]
    h2 = post(c, w, w, 2, "13:53", CALL2.replace("82300", "82200"))["hint_id"]
    h3 = post(c, w, w, 3, "13:54", CALL3)["hint_id"]
    tr.poll(c, w, t19("13:54", 30))
    for hid in (h1, h2, h3):
        ko.place_all(c, s, hid, 1, now=t19("13:55"))
    post(c, w, w, 4, "14:00", "avoid this sensex 82200 ce")               # the channel's avoid: cancelled (any mode)
    assert bk.load(h2)["kite"]["state"] == "none" and ko.rows_for(h2)[0]["status"] == "cancelled"
    ko.cancel(c, s, h3, "entry", now=t19("14:05"))                         # the owner's Cancel on the leg
    assert bk.load(h3)["kite"]["state"] == "none"
    tick(c, w, {}, "14:30")                                                 # the entry window ends at 2:30 PM
    k = bk.load(h1)["kite"]
    assert k["state"] == "none" and "entry window ended 2:30 PM" in k["reason"] and ko.rows_for(h1)[0]["status"] == "cancelled"
    assert any("unfilled entry cancelled · entry window ended 2:30 PM" in p for p in pushed)
    assert ko.entries_today(D19.isoformat(), dry=True) == 0                  # a cancelled unfilled entry never counts


def test_time_stop_exits_an_open_kite_position(pushed):
    c = cfg()
    c.tips.kite_mode = "dry"                                                # a click through the simulator
    w = sworld(**{S82300: 380.0})
    hid = post(c, w, w, 1, "13:52", CALL2.replace("ABOVE 370", "ABOVE 375"))["hint_id"]
    tr.poll(c, w, t19("13:52", 30))
    ko.place_all(c, ko.sim(), hid, 1, now=t19("13:53"))
    tick(c, w, {S82300: 381.0}, "13:54")
    assert bk.load(hid)["kite"]["state"] == "entered"
    tick(c, w, {S82300: 420.0}, "15:14")
    k = bk.load(hid)["kite"]
    assert k["state"] == "closed" and k["reason"] == "time stop 3:14 PM" and k["legs"]["stop"]["status"] == "cancelled"
    assert any("exit SELL 20 MARKET · stop and targets cancelled · time stop 3:14 PM" in p for p in pushed)


# --- auto -------------------------------------------------------------------------------------------------------------------

def test_auto_arms_a_placeable_hint_with_one_telegram_line(pushed):
    c, w, s = auto_setup()
    w.prices[S82300] = 309.0
    r = post(c, w, s, 1, "13:52", CALL2)
    k = bk.load(r["hint_id"])["kite"]
    assert k["state"] == "armed" and k["by"] == "auto" and k["arm"]["kind"] == "slm" and k["auto_rest"]
    assert ko.rows_for(r["hint_id"])[0]["order_type"] == "SL"            # NOT gated by "WAIT FOR ACTIVATION"
    line = [p for p in pushed if "armed" in p]
    assert line == ["🤖 Kite AUTO DRY · #1 armed · BUY 20 SENSEX 82300 CE · SL trigger 370 · resting on Kite · activation words: the level decides"]
    v = tr.hint_view(bk.load(r["hint_id"]), config=c)["kite"]
    assert v["auto"] == {"state": "armed", "text": "auto · armed"}


def test_auto_guards_log_once_per_cause_and_never_average(pushed):
    c, w, s = auto_setup()
    c.tips.kite_mode = "auto"
    w.prices[S82300] = 309.0
    hid = post(c, w, FakeClient(has_token=False), 1, "13:52", CALL2)["hint_id"]   # the real client, not logged in
    k = bk.load(hid)["kite"]
    assert k["state"] == "none" and k["auto"]["why"].startswith("Kite logged in")
    for i in range(3):                                                    # the Syncer looks again: no retry storm
        ka.auto_scan(c, FakeClient(has_token=False), t19("13:53", i))
    assert len([p for p in pushed if "not armed" in p]) == 1
    assert [x["text"] for x in bk.load(hid)["kite"]["log"]].count("auto: not armed · " + k["auto"]["why"]) == 1
    v = tr.hint_view(bk.load(hid), config=c)["kite"]
    assert v["auto"]["state"] == "not_armed" and v["auto"]["text"].startswith("auto · not armed: Kite logged in")
    ka.auto_scan(c, s, t19("13:54"))                                       # logged in now: armed
    assert bk.load(hid)["kite"]["state"] == "armed"
    post(c, w, s, 2, "13:55", "ADD MORE IF COMES NEAR 340")
    post(c, w, s, 3, "13:56", "add 5 lots at 560 cmp")
    assert len([r for r in ko.rows_for(hid) if r["leg"] == "entry"]) == 1
    assert sum("add-on ignored (never averaged)" in x["text"] for x in bk.load(hid)["kite"]["log"]) == 2
    from backend.database.models import get_db
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tips_hints").fetchone()[0] == 1      # an add-on is never a new hint


def test_auto_max_open_one_and_same_contract_and_kill_switch(pushed):
    c, w, s = auto_setup()
    w.prices.update({S82300: 309.0, S82400: 280.0})
    h1 = post(c, w, s, 1, "13:52", CALL2)["hint_id"]
    h2 = post(c, w, s, 2, "14:04", CALL3)["hint_id"]
    assert bk.load(h1)["kite"]["state"] == "armed" and bk.load(h2)["kite"]["state"] == "none"
    assert bk.load(h2)["kite"]["auto"]["why"].startswith("waits: 1 Kite position open at most")
    ko.cancel(c, s, h1, "entry", now=t19("14:05"))                          # the first one ends: the second is armed next pass
    ka.auto_scan(c, s, t19("14:06"))
    assert bk.load(h2)["kite"]["state"] == "armed"
    h3 = post(c, w, s, 3, "14:07", CALL3.replace("306", "310"))["hint_id"]    # the same contract the same day
    c.tips.kite_auto_max_open = 5
    ka.auto_scan(c, s, t19("14:08"))
    assert bk.load(h3)["kite"]["state"] == "none" and "already traded on Kite" in bk.load(h3)["kite"]["auto"]["why"]
    ko.set_kill(c, s, True, t19("14:09"))
    assert bk.load(h2)["kite"]["state"] == "none"
    h4 = post(c, w, s, 4, "14:10", CALL1)["hint_id"]
    assert bk.load(h4)["kite"]["state"] == "none" and "kill switch ON" in bk.load(h4)["kite"]["auto"]["why"]


def test_auto_follows_exit_trail_modify_and_partial(pushed):
    c, w, s = auto_setup(kite_auto_max_open=5, kite_max_lots=4)
    w.prices.update({S82200: 538.0})
    hid = post(c, w, s, 1, "12:50", CALL1 + "\n4 lots")["hint_id"]
    tick(c, w, {S82200: 538.0}, "12:51")
    k = bk.load(hid)["kite"]
    assert k["state"] == "entered" and k["qty_open"] == 80 and k["legs"]["stop"]["trigger"] == 480
    tick(c, w, {S82200: 580.0}, "13:00")
    post(c, w, s, 2, "13:01", "sl now 520")                                 # modify SL to N
    assert bk.load(hid)["kite"]["legs"]["stop"]["trigger"] == 520
    post(c, w, s, 3, "13:02", "TSL at cost")                                 # trail: the stop to cost
    k = bk.load(hid)["kite"]
    assert k["legs"]["stop"]["trigger"] == 540 and k["stop_px"] == 540
    post(c, w, s, 4, "13:05", "BOOK PART & KEEP TSL AT COST FOR REST QTY")   # 4 lots: half at market
    tick(c, w, {S82200: 590.0}, "13:06")
    k = bk.load(hid)["kite"]
    assert k["qty_open"] == 40 and k["legs"]["stop"]["qty"] == 40 and "t1" not in k["legs"]
    assert any("booked part: SELL 40 of 80 MARKET" in p for p in pushed)
    post(c, w, s, 5, "13:10", "tsl gone for rest quantity")                  # SL hit: exit what is open
    tick(c, w, {S82200: 560.0}, "13:11")
    k = bk.load(hid)["kite"]
    assert k["state"] == "closed" and k["qty_open"] == 0 and k["reason"] == "the channel's SL hit"
    auto_lines = [p for p in pushed if p.startswith("🤖")]
    assert any("stop → 520" in p for p in auto_lines) and any("stop → cost 540" in p for p in auto_lines)


def test_auto_book_part_with_one_lot(pushed):
    c, w, s = auto_setup()
    hid = post(c, w, s, 1, "12:50", CALL1)["hint_id"]
    tick(c, w, {S82200: 538.0}, "12:51")
    tick(c, w, {S82200: 590.0}, "13:00")
    c.tips.kite_auto_book_part = "hold"
    post(c, w, s, 2, "13:01", "book part qty and keep TSL at cost")
    k = bk.load(hid)["kite"]
    assert k["state"] == "entered" and k["legs"]["stop"]["trigger"] == 540          # held for the target, stop to cost
    c.tips.kite_auto_book_part = "channel"
    post(c, w, s, 3, "13:02", "book part qty")
    tick(c, w, {S82200: 591.0}, "13:03")
    assert bk.load(hid)["kite"]["state"] == "closed"                                 # one lot: "you can book now"


def test_click_mode_follow_ups_stay_informational(pushed):
    c = cfg()
    c.tips.kite_mode = "dry"                                                # a click through the simulator
    w = sworld(**{S82200: 538.0})
    s = ko.sim()
    hid = post(c, w, w, 1, "12:50", CALL1)["hint_id"]
    tr.poll(c, w, t19("12:50", 30))
    ko.place_all(c, s, hid, 1, now=t19("12:51"))
    tick(c, w, {S82200: 538.0}, "12:52")
    post(c, w, w, 2, "13:00", "REST STOPLOSS 530")
    post(c, w, w, 3, "13:01", "exit")
    k = bk.load(hid)["kite"]
    assert k["state"] == "entered" and k["legs"]["stop"]["trigger"] == 480          # the owner presses the buttons


def test_edited_post_rechecks_an_armed_entry(pushed):
    c, w, s = auto_setup()
    w.prices.update({S82300: 309.0})
    hid = post(c, w, s, 1, "13:52", CALL2)["hint_id"]
    assert ko.rows_for(hid)[0]["trigger"] == 370
    rd.edited(c, CH, 1, CALL2.replace("ABOVE 370", "ABOVE 360"), t19("13:53"), client=s)
    rows = ko.rows_for(hid)
    assert rows[0]["status"] == "cancelled" and rows[1]["trigger"] == 360 and bk.load(hid)["kite"]["state"] == "armed"
    assert any("the post was edited (level changed)" in x["text"] for x in bk.load(hid)["kite"]["log"])


# --- the three calls of 19 Nov, end to end in the sim, auto mode ---------------------------------------------------------------

def test_19_nov_three_calls_end_to_end(pushed):
    c, w, s = auto_setup(kite_auto_max_open=2)
    w.prices.update({S82200: 560.0, S82300: 309.0, S82400: 280.0})
    # call 1 · 12:50 · zone 530-540 -> LIMIT 540 rests and fills; REST STOPLOSS 530; tsl gone -> exit
    h1 = post(c, w, s, 101, "12:50", CALL1)["hint_id"]
    assert ko.rows_for(h1)[0]["order_type"] == "LIMIT" and ko.rows_for(h1)[0]["price"] == 540
    tick(c, w, {S82200: 536.0}, "12:58")
    assert bk.load(h1)["kite"]["state"] == "entered"
    tick(c, w, {S82200: 575.0}, "13:10")
    post(c, w, s, 102, "13:11", "REST STOPLOSS 530")
    assert bk.load(h1)["kite"]["legs"]["stop"]["trigger"] == 530
    post(c, w, s, 103, "13:12", "570")                                             # a bare number: a price note
    assert bk.load(h1)["kite"]["legs"]["stop"]["trigger"] == 530
    post(c, w, s, 104, "13:40", "tsl gone")
    tick(c, w, {S82200: 531.0}, "13:41")
    assert bk.load(h1)["kite"]["state"] == "closed"
    # call 2 · 1:52 PM · ONLY ABOVE 370 + WAIT FOR ACTIVATION -> SL-M 370 rests (not gated) and fills on the cross
    h2 = post(c, w, s, 105, "13:52", CALL2)["hint_id"]
    e2 = ko.rows_for(h2)[0]
    assert (e2["order_type"], e2["trigger"]) == ("SL", 370)
    post(c, w, s, 106, "13:55", "ADD MORE IF COMES NEAR 440")
    tick(c, w, {S82300: 455.0}, "14:00")                                          # 370 -> 455, no "activated" ever posted
    k2 = bk.load(h2)["kite"]
    assert k2["state"] == "entered" and k2["entry_px"] == 370
    post(c, w, s, 107, "14:01", "rest stoploss 370")
    assert bk.load(h2)["kite"]["legs"]["stop"]["trigger"] == 370
    # call 3 · 2:04 PM · above 306 -> SL-M 306; "activated" 3 minutes after the cross -> nothing extra; REVISE SL 298
    h3 = post(c, w, s, 108, "14:04", CALL3)["hint_id"]
    assert ko.rows_for(h3)[0]["trigger"] == 306
    tick(c, w, {S82400: 312.0}, "14:05")
    assert bk.load(h3)["kite"]["state"] == "entered"
    n = len(ko.rows_for(h3))
    post(c, w, s, 109, "14:08", "activated")
    assert len(ko.rows_for(h3)) == n                                              # after the fill: nothing extra
    tick(c, w, {S82400: 330.0}, "14:10")
    post(c, w, s, 110, "14:11", "REVISE SL 298")
    assert bk.load(h3)["kite"]["legs"]["stop"]["trigger"] == 298
    for hid in (h1, h2, h3):
        chk = bk.load(hid)["kite"]["check"]
        assert chk["ok"], (hid, chk)
        assert len([r for r in ko.rows_for(hid) if r["leg"] == "entry" and r["status"] != "cancelled"]) == 1
    from backend.tips import kite_check as kc
    d = kc.day_check(c, t19("15:35"))
    assert d["summary"].startswith("🔎 Kite day · ") and d["positions"] == 3


# --- routes -------------------------------------------------------------------------------------------------------------------

def test_routes_send_now_and_daycheck(fake_kite, make_client, monkeypatch, pushed):
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TIPS_TG_SESSION", "")
    from fastapi.testclient import TestClient
    from backend import main
    from backend.api import routes
    kc = make_client(fake_kite, history_years=1)
    kc.config.access_token = ""
    monkeypatch.setattr(main, "KiteClient", lambda a, b: kc)
    monkeypatch.setattr(ko, "_now", lambda: t19("11:05"))
    w = sworld(**{S82300: 365.0})
    with TestClient(main.app) as api:
        routes._state.client = w
        c = routes._state.config
        c.tips.kite_mode = "dry"
        hid = post(c, w, w, 1, "11:00", WAITZONE)["hint_id"]
        tr.poll(c, w, t19("11:00", 30))
        r = api.post(f"/api/tips/hints/{hid}/kite/all", json={})
        assert r.status_code == 200 and r.json()["kite"]["state"] == "armed_wait" and r.json()["kite"]["needs_activation"]
        r = api.post(f"/api/tips/hints/{hid}/kite/send_now", json={})
        assert r.status_code == 200 and r.json()["kite"]["state"] in ("armed", "entered")
        assert api.post(f"/api/tips/hints/{hid}/kite/send_now", json={}).status_code == 409
        r = api.post("/api/tips/kite/daycheck")
        assert r.status_code == 200 and r.json()["result"]["summary"].startswith("🔎 Kite day")
        assert r.json()["day_check"]["summary"] == r.json()["result"]["summary"]
        c.tips.kite_mode = "off"


def test_bn_levels_still_premium():
    from backend.tips import decode as dc
    assert dc.decode(BN13, D19)["entry"]["level_kind"] == "premium"
    assert dc.decode(BN12, D19)["entry"]["level_kind"] == "premium"
    assert KEY13 and seed and Broker                                            # the shared fakes stay importable


# --- live price on the legs, bare entry prices, a stop revised before the entry (19 Nov hint #8) ------------------------------

N25400 = "NFO:NIFTY26NOV25400CE"
HINT8 = "BUY nifty 25400 ce 191-198\nsl 130\ntgt 230"


def nworld(**prices):
    w = sworld(**prices)
    w.dumps["NFO"] = w.dumps["NFO"] + [opt("NIFTY", 25400, t, EXP, lot=75) for t in ("CE", "PE")]
    return w


def test_a_bare_range_or_price_after_the_strike_is_the_entry():
    from backend.tips import decode as dc
    e = dc.decode(HINT8, D19)["entry"]
    assert (e["mode"], e["lo"], e["hi"], e["level_kind"]) == ("zone", 191, 198, "premium")
    e = dc.decode("buy 82000 ce 460\nsl 410\ntgt 520", D19)["entry"]
    assert (e["mode"], e["lo"]) == ("at", 460)
    e = dc.decode("BUY 25400 CE 2 lots at 190 sl 150 tgt 230", D19)["entry"]
    assert (e["mode"], e["lo"]) == ("at", 190)                                       # "2 lots" is a size, never the entry
    assert dc.decode("buy nifty 25400 ce\nsl 130\ntgt 230", D19)["entry"]["mode"] == "market"


def test_live_price_on_every_leg_of_an_open_hint(pushed):
    c = cfg()
    c.tips.kite_mode = "dry"
    w = nworld(**{N25400: 196.1})
    hid = post(c, w, w, 8, "11:00", HINT8)["hint_id"]
    tr.poll(c, w, t19("11:00", 30))
    legs = {x["leg"]: x for x in tr.hint_view(bk.load(hid), config=c)["kite"]["legs"]}
    assert legs["entry"]["now_text"] == "now 196.1 · in the zone 191–198"
    assert legs["stop"]["now_text"] == "now 196.1 · stop 130 is −33.7 %"
    assert legs["t1"]["now_text"] == "now 196.1 · target 230 is +17.3 %"
    hid2 = post(c, w, w, 9, "11:01", CALL2)["hint_id"]
    w.prices[S82300] = 337.0
    tr.poll(c, w, t19("11:01", 30))
    legs = {x["leg"]: x for x in tr.hint_view(bk.load(hid2), config=c)["kite"]["legs"]}
    assert legs["entry"]["now_text"] == "now 337 · level 370 is 9.8 % away"


def test_a_stop_revised_before_the_entry_updates_the_plan_and_the_armed_entry(pushed):
    c, w, s = auto_setup()
    w = nworld(**{N25400: 205.0})
    hid = post(c, w, s, 8, "11:00", HINT8)["hint_id"]
    tr.poll(c, w, t19("11:00", 30))
    assert ko.rows_for(hid)[0]["order_type"] == "LIMIT" and ko.rows_for(hid)[0]["price"] == 198   # resting: not filled yet
    r = post(c, w, s, 9, "11:02", "210 revise sl 193")
    h = bk.load(hid)
    assert r["kind"] == "follow_up" and r["line"] == "plan stop revised 130 → 193 (channel)"
    assert h["stop"]["value"] == 193 and h["plan"]["stop"]["value"] == 193 and h["run"]["stop"] == 193
    assert h["kite"]["stop_px"] == 193 and any("plan stop revised 130 → 193" in x["text"] for x in h["kite"]["log"])
    tick(c, w, {N25400: 197.0}, "11:05")
    k = bk.load(hid)["kite"]
    assert k["state"] == "entered" and k["legs"]["stop"]["trigger"] == 193        # the stop placed on the fill is the revised one


def test_a_partial_booking_cuts_the_stop_before_the_second_sell(pushed):
    """20 Nov: a market sell beside the resting stop for the full quantity is refused for margin; the stop is cut to the
    rest first (one modify, no new sell), then the part is sold."""
    c, w, s = auto_setup(kite_auto_max_open=5, kite_max_lots=4)
    w.prices.update({S82200: 538.0})
    hid = post(c, w, s, 1, "12:50", CALL1 + "\n4 lots")["hint_id"]
    tick(c, w, {S82200: 538.0}, "12:51")
    k = bk.load(hid)["kite"]
    assert k["qty_open"] == 80 and k["legs"]["stop"]["qty"] == 80 and k["legs"]["stop"]["type"] == "SL" and k["legs"]["stop"]["price"] is not None
    tick(c, w, {S82200: 590.0}, "13:00")
    n_before = len(s.orders())
    post(c, w, s, 2, "13:01", "BOOK PART & KEEP TSL AT COST FOR REST QTY")
    book = s.orders()
    stop = next(o for o in book if o["order_id"] == k["legs"]["stop"]["order_id"])
    sells = [o for o in book[n_before:] if o["transaction_type"] == "SELL" and o["order_type"] == "MARKET"]
    assert int(stop["quantity"]) == 40 and len(sells) == 1 and int(sells[0]["quantity"]) == 40     # the stop was cut, one sell
    assert any("stop cut to 40 first" in p for p in pushed)


# --- 20 Nov: a booking outranks a stop change in one post · the day closed by the channel -------------------------------------

BN64 = "NFO:BANKNIFTY26NOV64300CE"
BNCALL = "BUY BANKNIFTY 64300 CE NEAR 430-440\nSL 400\nTGT 500/550"
NOV20 = ("bnf 440 to 500++\nDEFENSIVE TRADERS: book part qty and keep TSL at cost for rest qty\nAGGRESSIVE TRADERS: book part qty and "
         "for rest qty keep TSL at NPNL\nNote- If you are trading with 1 lot only, you can book now. If you are trading with more lots, "
         "book part lots and then trail for target with balance lots.\nrest sl 440")


def bnworld(**prices):
    w = sworld(**prices)
    w.dumps["NFO"] = w.dumps["NFO"] + [opt("BANKNIFTY", 64300, t, EXP, lot=30) for t in ("CE", "PE")]
    return w


def test_20_nov_one_lot_books_at_market_and_never_moves_the_stop(pushed):
    c, _, s = auto_setup()
    w = bnworld(**{BN64: 438.0})
    hid = post(c, w, s, 1, "12:50", BNCALL)["hint_id"]
    tick(c, w, {BN64: 438.0}, "12:51")
    k = bk.load(hid)["kite"]
    assert k["state"] == "entered" and k["qty_open"] == 30 and k["legs"]["stop"]["trigger"] == 400
    tick(c, w, {BN64: 495.0}, "13:06")
    r = post(c, w, s, 2, "13:07", NOV20)
    assert r["act"] == {"action": "partial", "cost": True, "rest_stop": 440}
    tick(c, w, {BN64: 495.0}, "13:07", 30)
    k = bk.load(hid)["kite"]
    assert k["state"] == "closed" and k["qty_open"] == 0 and not k.get("stop_moves")            # booked, the stop never moved to 440
    ex = next(x for x in ko.rows_for(hid) if x["leg"] == "exit")
    assert ex["order_type"] == "MARKET" and abs(float(ex["filled_px"]) - 495) < 1 and k["realised"] > 1500
    assert any("one lot open: booked at market" in p for p in pushed)


def test_20_nov_two_lots_cut_the_stop_sell_half_then_the_rest_stop(pushed):
    c, _, s = auto_setup(kite_auto_max_open=5, kite_max_lots=4)
    w = bnworld(**{BN64: 438.0})
    hid = post(c, w, s, 1, "12:50", BNCALL + "\n2 lots")["hint_id"]
    tick(c, w, {BN64: 438.0}, "12:51")
    k = bk.load(hid)["kite"]
    assert k["qty_open"] == 60 and k["legs"]["stop"]["qty"] == 60
    tick(c, w, {BN64: 495.0}, "13:06")
    n_before = len(s.orders())
    post(c, w, s, 2, "13:07", NOV20)
    book = s.orders()
    stop = next(o for o in book if o["order_id"] == k["legs"]["stop"]["order_id"])
    sells = [o for o in book[n_before:] if o["transaction_type"] == "SELL" and o["order_type"] == "MARKET"]
    assert int(stop["quantity"]) == 30 and len(sells) == 1 and int(sells[0]["quantity"]) == 30       # the stop cut first, half sold
    assert any("booked part: SELL 30 of 60 MARKET · stop cut to 30 first · rest stop → 440" in p for p in pushed)
    tick(c, w, {BN64: 495.0}, "13:07", 30)
    k = bk.load(hid)["kite"]
    assert k["qty_open"] == 30 and k["legs"]["stop"]["qty"] == 30 and k["legs"]["stop"]["trigger"] == 440 and k["stop_px"] == 440
    assert k.get("rest_stop_after_part") is None and not k.get("cost_after_part")
    p = bk.load(hid)["pos"]                                                    # the paper book: half booked, the rest's stop 440
    if p:
        assert p["run"]["stop"] == 440


def test_a_rest_stop_is_never_lowered_and_hold_moves_it(pushed):
    c, _, s = auto_setup()
    c.tips.kite_auto_book_part = "hold"
    w = bnworld(**{BN64: 438.0})
    hid = post(c, w, s, 1, "12:50", BNCALL)["hint_id"]
    tick(c, w, {BN64: 438.0}, "12:51")
    tick(c, w, {BN64: 495.0}, "13:06")
    post(c, w, s, 2, "13:07", "book part · rest sl 380")                      # lower than the stop 400: kept
    k = bk.load(hid)["kite"]
    assert k["state"] == "entered" and k["legs"]["stop"]["trigger"] == 400
    post(c, w, s, 3, "13:08", "book part · rest sl 440")                      # one lot held: the stop to the rest stop
    k = bk.load(hid)["kite"]
    assert k["state"] == "entered" and k["legs"]["stop"]["trigger"] == 440


def test_the_channel_closes_the_day_and_a_new_call_reopens_it(pushed):
    c, w, s = auto_setup(kite_auto_max_open=5)
    w.prices.update({S82200: 545.0, S82400: 280.0})
    h1 = post(c, w, s, 1, "13:00", "BUY SENSEX 82200 CE NEAR 530-540")["hint_id"]     # no stop / target yet: not placeable
    assert bk.load(h1)["kite"]["state"] in ("none", None) if bk.load(h1).get("kite") else True
    r = post(c, w, s, 2, "13:07", "Temporarily close the day")
    assert r.get("day_closed") and ka.day_closed(t19("13:08"))["at_label"] == "1:07 PM"
    assert len([p for p in pushed if "closed the day at 1:07 PM" in p]) == 1
    post(c, w, s, 3, "13:09", "SL 480\nTGT 600/650")                            # the plan completes after the close: not armed
    k = bk.load(h1)["kite"]
    assert k["state"] == "none" and k["auto"]["why"] == "the channel closed the day at 1:07 PM"
    assert not any("not armed · the channel closed" in p for p in pushed)     # logged on the card, one line each way only
    assert any(g["key"] == "day_closed" and not g["ok"] for g in ko.auto_guards(c, t19("13:10")))
    assert ko.lite(c, t19("13:10"))["day_closed"]["at_label"] == "1:07 PM"
    post(c, w, s, 4, "13:12", "trade slow only")                                # not a close, no new line
    assert len([p for p in pushed if "closed the day" in p]) == 1
    h2 = post(c, w, s, 5, "14:04", CALL3)["hint_id"]                           # a new call: the channel is trading again
    assert ka.day_closed(t19("14:05")) is None and ko.lite(c, t19("14:05"))["day_closed"] is None
    assert len([p for p in pushed if "day re-opened by a new call (#%d)" % h2 in p]) == 1
    assert bk.load(h2)["kite"]["state"] == "armed"
    assert any("day re-opened by a new call" in e["text"] for e in bk.load(h2)["events"])
    ka.auto_scan(c, s, t19("14:06"))
    assert bk.load(h1)["kite"]["state"] in ko.WORKING                          # the earlier hint is armed again too
