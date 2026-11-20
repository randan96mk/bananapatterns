"""💡 Plan rules: zone wait / no fill at the time stop, above / below triggers, half at T1 + SL to cost, trail, the
time stop, spot stops on 15-minute closes, Target 0 held to expiry, after-hours hints for the next open, "stop
missing", the channel's "active" (market entry) and "avoid" (pause)."""

from datetime import datetime

from backend.config import IST, TipsConfig
from backend.tips import decode as dc
from backend.tips import plan as pl
from tests.tips_world import BN12, BN13, at


def mk(text, posted=None, exchange="NFO", expiry="2026-11-25", cfg=None):
    posted = posted or at("09:43")
    d = dc.decode(text, posted)
    d["exchange"], d["expiry"] = exchange, d.get("expiry") or expiry
    return d, pl.build(d, cfg or TipsConfig(), posted)


def test_plan_text_zone_when_comes():
    _, p = mk(BN12)
    assert p["ok"] and p["wait"] and p["partial_at_t1"] and p["time_stop"] == "15:14" and p["day"] == "2026-11-17"
    assert p["text"] == "wait for the zone · half out at 320, SL to cost, rest to 350 / trail · time stop 3:14 PM · skipped if the zone never comes"


def test_zone_waits_then_fills_at_the_quote():
    _, p = mk(BN12)
    run = pl.new_run(p)
    assert pl.step(run, p, 280, at("09:44")) == [] and run["state"] == "waiting"
    ev = pl.step(run, p, 262, at("09:51"))
    assert ev == [{"kind": "fill", "px": 262, "frac": 0.0}] and run["entry_px"] == 262


def test_zone_never_comes_is_no_fill_at_the_time_stop():
    _, p = mk(BN12)
    run = pl.new_run(p)
    pl.step(run, p, 300, at("10:00"))
    ev = pl.step(run, p, 310, at("15:14"))
    assert ev[0]["kind"] == "no_fill" and run["end"] == "no_fill"
    assert pl.result(run, p, 15)["first"] == "no_fill"


def test_above_trigger_and_below_trigger():
    _, p = mk(BN13, at("09:46"))
    run = pl.new_run(p)
    assert pl.step(run, p, 46.2, at("09:47")) == []
    assert pl.step(run, p, 50.5, at("09:52"))[0]["kind"] == "fill"
    _, pb = mk("Buy nifty 24400 PE below 80\nSL 60\nTgt 100")
    rb = pl.new_run(pb)
    assert pl.step(rb, pb, 85, at("10:00")) == [] and pl.step(rb, pb, 79, at("10:01"))[0]["kind"] == "fill"


def test_half_at_t1_stop_to_cost_rest_at_t2():
    _, p = mk(BN12)
    run = pl.new_run(p)
    pl.step(run, p, 262, at("09:51"))
    ev = pl.step(run, p, 321, at("10:00"))
    assert ev == [{"kind": "t1", "px": 321, "frac": 0.5}] and run["stop"] == 262 and run["stop_moved"]
    ev = pl.step(run, p, 355, at("10:08"))
    assert ev[0]["kind"] == "t2" and run["state"] == "closed" and run["end"] == "t2"
    r = pl.result(run, p, 15)
    assert r["pnl_pts"] == 76.0 and r["pnl_lot"] == 1140.0 and r["first"] == "t1" and r["r"] == round(76 / 72, 2)
    assert r["minutes_to_first"] == 9 and r["mfe"] == 93


def test_trail_rides_target_to_target():
    _, p = mk(BN12)
    run = pl.new_run(p)
    pl.step(run, p, 262, at("09:51"))
    pl.step(run, p, 321, at("10:00"))
    run["trail"] = True
    ev = pl.step(run, p, 355, at("10:08"))
    assert ev == [{"kind": "t2", "px": 355, "frac": 0.0}] and run["stop"] == 320 and run["state"] == "active"
    ev = pl.step(run, p, 318, at("10:20"))
    assert ev[0]["kind"] == "trail" and run["end"] == "trail" and run["pnl_pts"] == 57.5


def test_stop_and_time_stop():
    _, p = mk(BN12)
    run = pl.new_run(p)
    pl.step(run, p, 262, at("09:51"))
    assert pl.step(run, p, 189, at("10:30"))[0]["kind"] == "stop"
    run2 = pl.new_run(p)
    pl.step(run2, p, 262, at("09:51"))
    pl.step(run2, p, 280, at("15:00"))
    ev = pl.step(run2, p, 281, at("15:14"))
    assert ev[0]["kind"] == "time" and run2["exit_px"] == 281


def test_spot_stop_on_15_minute_closes_and_target_zero_to_expiry():
    t = "Sell Bank nifty 43600 CE at 36\ntarget 0 \nStop loss - 15 mins candle above 43450 spot"
    _, p = mk(t, at("12:55"), expiry="2026-11-17")
    assert p["hold_to_expiry"] and p["time_stop"] is None and "hold to expiry" in p["text"]
    assert pl.stop_text(p["stop"]) == "stop: spot 43,450 (15-min close above)"
    samples = [(at("13:01").isoformat(), 43400.0), (at("13:14").isoformat(), 43460.0), (at("13:16").isoformat(), 43500.0)]
    assert pl.spot_close15(samples, at("13:20")) == 43460.0 and pl.spot_close15(samples, at("13:10")) is None
    run = pl.new_run(p)
    pl.step(run, p, 36, at("13:00"))
    assert pl.step(run, p, 38, at("13:05"), spot15=43440) == []
    assert pl.step(run, p, 40, at("13:20"), spot15=43460)[0]["kind"] == "stop"
    run2 = pl.new_run(p)
    pl.step(run2, p, 36, at("13:00"))
    ev = pl.step(run2, p, 5, at("15:30"))
    assert ev[0]["kind"] == "expired" and run2["pnl_pts"] == 31


def test_after_hours_hint_waits_for_the_next_open():
    _, p = mk(BN12, datetime(2026, 11, 17, 16, 10, tzinfo=IST))
    assert p["queued"] and p["day"] == "2026-11-18" and p["text"].startswith("for the next open (Wed 18 Nov)")
    run = pl.new_run(p)
    assert pl.step(run, p, 260, datetime(2026, 11, 17, 16, 30, tzinfo=IST)) == []        # nothing before the open
    assert pl.step(run, p, 260, at("09:20", datetime(2026, 11, 18).date()))[0]["kind"] == "fill"


def test_stop_missing_is_no_plan():
    _, p = mk("Buy fin nifty 23350 Pe at 100.. add more at 80-90")
    assert not p["ok"] and p["why"] == "stop missing" and "no plan: stop missing" in p["text"]


def test_channel_active_allows_market_entry_and_avoid_pauses():
    _, p = mk(BN12)
    run = pl.new_run(p)
    run["paused"] = True
    assert pl.step(run, p, 260, at("10:00")) == []
    run["paused"], run["allowed"] = False, True
    assert pl.step(run, p, 300, at("10:01"))[0]["kind"] == "fill"


def test_followup_actions():
    fa = pl.followup_action
    assert fa("fired up … exit near cost")["action"] == "exit"
    assert fa("2 targets done ✅")["action"] == "targets" and fa("2 targets done ✅")["n"] == 2
    assert fa("modify SL to 230") == {"action": "stop", "value": 230, "spot": False}
    assert fa("cmp 93")["action"] == "active" and fa("93 level active")["action"] == "active"
    assert fa("Avoid.. wait")["action"] == "avoid" and fa("SL hit")["action"] == "sl_hit"
    assert fa("Defensive traders book Partly")["action"] == "partial" and fa("Good")["action"] == "note"


NOV20 = ("bnf 440 to 500++\nDEFENSIVE TRADERS: book part qty and keep TSL at cost for rest qty\nAGGRESSIVE TRADERS: book part qty and "
         "for rest qty keep TSL at NPNL\nNote- If you are trading with 1 lot only, you can book now. If you are trading with more lots, "
         "book part lots and then trail for target with balance lots.\nrest sl 440")


def test_followup_precedence_booking_outranks_a_stop_change_in_one_post():
    fa = pl.followup_action
    assert fa(NOV20) == {"action": "partial", "cost": True, "rest_stop": 440}          # 20 Nov: partial, 440 for the remainder
    assert fa("rest sl 440") == {"action": "stop", "value": 440, "spot": False}         # a stop figure alone stays a stop
    for t, v in (("rest stoploss 440", 440), ("sl for rest 445", 445), ("balance sl 450", 450), ("remaining sl 455", 455)):
        assert fa(t) == {"action": "stop", "value": v, "spot": False}
        assert fa("book part · " + t) == {"action": "partial", "cost": False, "rest_stop": v}
    assert fa("book half, modify SL to 230")["rest_stop"] == 230
    for t in ("defensive traders book", "book it", "book now", "Defensive traders book Partly", "partial booking now"):
        assert fa(t)["action"] == "partial" and fa(t)["rest_stop"] is None
    for t in ("book full", "book all", "book everything", "book profits fully", "exit", "exit now · rest sl 440", "sl hit"):
        assert fa(t)["action"] in ("exit", "sl_hit")
    assert fa("trade slow only")["action"] == "note"
    assert fa("keep trailing sl and part part booking")["action"] == "trail"           # booking named beside a trail: the trail
    assert fa("tgt 1 done trail sl to cost") == {"action": "trail", "cost": True}         # trail > targets


def test_the_channel_closes_the_day():
    for t in ("temporarily close the day", "Close the day here", "done for the day", "No more trades today", "That's it for today",
              "that’s it for today"):
        assert pl.day_closed(t), t
    for t in ("trade slow only", "close it", "book now", "today is a trending day"):
        assert not pl.day_closed(t), t
