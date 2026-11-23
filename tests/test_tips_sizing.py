# ruff: noqa: F811  (the shared `pushed` fixture is imported, then named by each test)
"""📏 Tips sizing: post / fixed / risk modes, the per-trade risk guard (and "Take 1 lot…"), the settings check, the book
part size, the paper book mirroring, the card's size cell, the Telegram lines; the channel's adds (logged always, acted on
only with kite_follow_adds: one add, inside the caps, cancelled with the entry, never after a stop); the three 23 Nov
decoder fixes (the monthly series, "2-2 lots" / "buy 2 lots here", "book 156 full")."""

from datetime import date

import pytest

from backend.tips import book as bk
from backend.tips import decode as dc
from backend.tips import kite_orders as ko
from backend.tips import plan as pl
from backend.tips import sizing
from backend.tips import track as tr
from backend.tips.book import TipsError
from tests.test_tips_kite_auto import (BN64, BNCALL, NOV20, S82200, auto_setup, bnworld, post, pushed, sworld, t19,  # noqa: F401
                                       tick)
from tests.tips_world import EXP, cfg, opt

S83300 = "BFO:SENSEX26NOV83300PE"
SX23 = "Buy SENSEX 83300 PE above 715++\n\nSl 615\n\nTgt 800/850/900\n\n2 lots\n\nAdd 2 lots near 660 if comes after activation"
SX_NOADD = "Buy SENSEX 83300 PE above 715++\n\nSl 615\n\nTgt 800/850/900\n\n2 lots"
NF23 = "BUY NIFTY 25500  CE 150-155\n\nSL 90\n\nTGT  190,220\n\nADD MORE NEAR 125\n\nNOV SERIES"
WIDE = "BUY SENSEX 82200 CE NEAR 530-540\nSL 330\nTGT 600/650\n2 lots"


def hx(**kw) -> dict:
    """A planned hint by hand: BUY at a 225–230 zone, SL 175, lot 65 (the mock's numbers)."""
    h = {"side": "BUY", "lot_size": 65, "symbol": "NIFTY", "size_hint": None,
         "plan": {"entry": {"mode": "zone", "lo": 225, "hi": 230}, "stop": {"kind": "premium", "value": 175}, "targets": [260, 300],
                  "partial_at_t1": True}}
    h.update(kw)
    return h


def xworld(**prices):
    w = sworld(**prices)
    w.dumps["BFO"] = w.dumps["BFO"] + [opt("SENSEX", 83300, t, EXP, seg="BFO-OPT", lot=20) for t in ("CE", "PE")]
    return w


# --- the three modes ----------------------------------------------------------------------------------------------------

def test_post_mode_the_post_wins_else_lots():
    c = cfg()
    assert c.tips.kite_size_mode == "post" and c.tips.lots == 1 and c.tips.kite_max_lots == 2 and c.tips.kite_book_part == "half"
    assert c.tips.kite_follow_adds is False and c.tips.kite_risk_per_trade == 6000.0
    s = sizing.size(c, hx())
    assert (s["lots"], s["src"], s["qty"], s["premium"], s["risk_rs"], s["held"]) == (1, "fallback", 65, 14950.0, 3575.0, False)
    s = sizing.size(c, hx(size_hint={"lots": 3}))
    assert (s["lots"], s["src"]) == (3, "post")                 # over the cap: the click guard refuses it, as before
    assert ko.default_lots(c, hx(size_hint={"lots": 3})) == 3
    c.tips.lots = 2
    assert sizing.size(c, hx())["lots"] == 2


def test_fixed_mode_ignores_the_post():
    c = cfg()
    c.tips.kite_size_mode = "fixed"
    s = sizing.size(c, hx(size_hint={"lots": 3}))
    assert (s["lots"], s["src"], s["post"]) == (1, "fixed", 3)


def test_risk_mode_floor_at_least_one_and_the_cap():
    c = cfg()
    c.tips.kite_size_mode = "risk"
    s = sizing.size(c, hx(size_hint={"lots": 2}))
    assert s["src"] == "risk" and round(s["ratio"], 3) == 1.678 and s["lots"] == 1          # 6000 ÷ (55 × 65) = 1.67 → 1
    c.tips.kite_risk_per_trade = 20000.0
    s = sizing.size(c, hx())
    assert (s["raw"], s["lots"]) == (5, 2)                                                 # 5.59 → 5, capped at 2
    c.tips.kite_max_lots = 4
    assert sizing.size(c, hx())["lots"] == 4
    c.tips.kite_risk_per_trade = 1000.0
    s = sizing.size(c, hx())
    assert s["lots"] == 1 and s["held"] and s["max_ok"] == 0                               # at least 1, and the guard holds it
    sell = hx(side="SELL", plan={"entry": {"mode": "zone", "lo": 40, "hi": 45}, "stop": {"kind": "premium", "value": 60}, "targets": [20]})
    assert sizing.stop_points(sell, sizing.entry_ref(sell)) == 20                          # a sell: stop − entry (the zone's bottom)
    mk = hx(plan={"entry": {"mode": "market"}, "stop": {"kind": "premium", "value": 175}})
    assert sizing.entry_ref(mk) is None and sizing.entry_ref(mk, 240.0) == 240.0           # a market entry: the LTP


# --- the card's size cell -----------------------------------------------------------------------------------------------

def test_size_cell_text_in_the_three_modes():
    c = cfg()
    v = sizing.cell(c, hx(size_hint={"lots": 1}))
    assert v["line1"] == "1 lot · 65 qty" and v["line2"] == "the post says 1 lot · premium ₹14,950 · at the stop −₹3,575"
    assert v["line3"] == "post wins · fallback 1" and not v["held"]
    c.tips.kite_size_mode = "risk"
    c.tips.kite_max_lots = 4
    v = sizing.cell(c, hx())
    assert v["line2"].startswith("risk ₹6,000 ÷ (230 − 175) × 65 = 1.67 → 1 lot · premium ₹14,950") and v["line3"] == "by risk · cap 4"
    c.tips.kite_size_mode, c.tips.lots = "fixed", 2
    v = sizing.cell(c, hx(size_hint={"lots": 1}))
    assert v["line1"] == "2 lots · 130 qty" and v["line2"] == "fixed · the post said 1 lot (ignored) · premium ₹29,900 · at the stop −₹7,150"
    assert v["held"] and v["line3"] == "⛔ above risk ₹6,000 per trade · not armed"
    assert v["held_text"] == ("size held: 2 lots × 55 points × 65 = ₹7,150 at the stop, above kite_risk_per_trade ₹6,000 · "
                              "lower the lots in Settings or take 1 lot by hand") and v["max_ok"] == 1


# --- the per-trade guard ------------------------------------------------------------------------------------------------

def test_the_guard_holds_a_click_entry_and_take_one_lot_places_the_largest_size(pushed):
    c = cfg()
    c.tips.kite_mode = "dry"
    w = sworld(**{S82200: 560.0})
    hid = post(c, w, w, 1, "12:50", WIDE)["hint_id"]
    tr.poll(c, w, t19("12:50", 30))
    v = tr.hint_view(bk.load(hid), config=c)
    assert v["sizing"]["held"] and v["sizing"]["max_ok"] == 1
    assert v["sizing"]["held_text"].startswith("size held: 2 lots × 210 points × 20 = ₹8,400 at the stop, above kite_risk_per_trade ₹6,000")
    with pytest.raises(TipsError, match="size held: 2 lots"):
        ko.place_all(c, ko.sim(), hid, now=t19("12:51"))
    assert ko.rows_for(hid) == []
    h = ko.place_all(c, ko.sim(), hid, v["sizing"]["max_ok"], now=t19("12:52"))             # "Take 1 lot…" (its confirm on the page)
    assert h["kite"]["lots"] == 1 and ko.rows_for(hid)[0]["qty"] == 20
    assert any("· 1 lot · 20 qty" in p for p in pushed)                                    # the Telegram line says lots and qty


def test_the_guard_holds_an_auto_arm_with_the_reason(pushed):
    c, w, s = auto_setup(kite_max_lots=4)
    w.prices[S82200] = 560.0
    hid = post(c, w, s, 1, "12:50", WIDE)["hint_id"]
    k = bk.load(hid)["kite"]
    assert k["state"] == "none" and k["auto"]["why"].startswith("size held: 2 lots × 210 points × 20 = ₹8,400")
    assert ko.rows_for(hid) == [] and sum("not armed · size held" in p for p in pushed) == 1


# --- the settings check -------------------------------------------------------------------------------------------------

def test_check_tips_refuses_lots_over_the_risk_with_the_arithmetic():
    c = cfg()
    c.update_section("tips", {"lots": 2})                       # Kite off: lots sizes the paper book only
    c.update_section("tips", {"lots": 1, "kite_mode": "click"})
    with pytest.raises(ValueError, match=r"refused: lots 2 × 65 \(NIFTY lot size\) × 50 points .* = ₹6,500 at the stop, above kite_risk_per_trade ₹6,000"):
        c.update_section("tips", {"lots": 2})
    assert c.tips.lots == 1                                     # rolled back
    with pytest.raises(ValueError, match="kite_loss_cap"):
        c.update_section("tips", {"kite_loss_cap": 3000})
    c.update_section("tips", {"lots": 2, "kite_risk_per_trade": 9000, "kite_loss_cap": 9000})
    assert c.tips.lots == 2
    for bad in ({"kite_size_mode": "rupees"}, {"kite_book_part": "third"}, {"kite_risk_per_trade": 0}):
        with pytest.raises(ValueError):
            c.update_section("tips", bad)


# --- the book part size -------------------------------------------------------------------------------------------------

def test_book_part_half_or_one_lot():
    c = cfg()
    h = hx()
    assert [ko.split(h, n, c) for n in (1, 2, 3, 4)] == [(65, 0), (65, 65), (65, 130), (130, 130)]
    c.tips.kite_book_part = "one"
    assert [ko.split(h, n, c) for n in (1, 2, 3, 4)] == [(65, 0), (65, 65), (65, 130), (65, 195)]


@pytest.mark.parametrize("part,booked,rest", [("half", 2, 2), ("one", 1, 3)])
def test_auto_book_part_with_four_lots_and_the_strip(pushed, part, booked, rest):
    c, _, s = auto_setup(kite_max_lots=4, kite_book_part=part)
    w = bnworld(**{BN64: 438.0})
    hid = post(c, w, s, 1, "12:50", BNCALL + "\n4 lots")["hint_id"]
    tick(c, w, {BN64: 438.0}, "12:51")
    assert bk.load(hid)["kite"]["qty_open"] == 120
    tick(c, w, {BN64: 495.0}, "13:06")
    post(c, w, s, 2, "13:07", NOV20)
    tick(c, w, {BN64: 495.0}, "13:07", 30)
    k = bk.load(hid)["kite"]
    assert k["qty_open"] == rest * 30 and k["legs"]["stop"]["qty"] == rest * 30
    st = tr.hint_view(bk.load(hid), config=c)["pnl_strip"]
    assert st["booked_text"] == f"booked so far {bk.inr(k['realised'], sign=True)} · {booked} lot{'s' if booked != 1 else ''} at 495"
    assert st["rest_text"] == f"rest {rest} lots · {rest * 30} qty · stop 440"


# --- the paper book uses the same mode ------------------------------------------------------------------------------------

def test_paper_book_mirrors_the_size_mode():
    c = cfg()
    assert bk.size_for(hx(size_hint={"lots": 3}), c) == (3, "post")
    c.tips.kite_size_mode = "fixed"
    assert bk.size_for(hx(size_hint={"lots": 3}), c) == (1, "fixed")
    c.tips.kite_size_mode, c.tips.kite_risk_per_trade = "risk", 12000.0
    assert bk.size_for(hx(), c) == (2, "risk")
    assert bk.size_for(hx(), c, lots=4) == (4, "you")                         # what you give still wins


# --- the channel's adds -----------------------------------------------------------------------------------------------------

def test_adds_are_parsed_and_logged_with_the_switch_off_and_never_placed(pushed):
    c, w, s = auto_setup(kite_max_lots=4)
    w = xworld(**{S83300: 700.0})
    r = post(c, w, s, 1, "12:50", SX23)
    hid = r["hint_id"]
    h = bk.load(hid)
    assert h["adds"] == [{"lots": 2, "level": 660, "cond": "after activation", "at": "2026-11-19"}] and h["size_hint"] == {"lots": 2}
    assert any("channel adds 2 lots near 660 (after activation) · the app never adds unless kite_follow_adds is on" in e["text"] for e in h["events"])
    assert any("📏 channel adds 2 lots near 660" in p for p in pushed)
    tick(c, w, {S83300: 716.0}, "12:55")
    k = bk.load(hid)["kite"]
    assert k["state"] == "entered" and k["qty_open"] == 40
    post(c, w, s, 2, "12:56", "we will add 2 more near 680")
    tick(c, w, {S83300: 655.0}, "12:58")
    assert [x["leg"] for x in ko.rows_for(hid)] == ["entry", "stop"] and not any(o.get("transaction_type") == "BUY" and o["order_id"] != ko.rows_for(hid)[0]["order_id"]
                                                                                for o in s.orders())
    log = [x["text"] for x in bk.load(hid)["kite"]["log"]]
    assert any("channel adds 2 lots near 680 · add-on ignored (never averaged) · the app never adds unless kite_follow_adds is on" in t for t in log)


def test_add_armed_with_the_switch_on_fills_raises_the_stop_qty_and_averages(pushed):
    c, w, s = auto_setup(kite_max_lots=4, kite_follow_adds=True)
    w = xworld(**{S83300: 700.0})
    hid = post(c, w, s, 1, "12:50", SX23)["hint_id"]
    r = tick(c, w, {S83300: 716.0}, "12:55")
    assert any("add armed BUY LIMIT 660 ×40 · 2 lots (auto)" in ln for ln in r["lines"])
    add = next(x for x in ko.rows_for(hid) if x["leg"] == "add")
    assert (add["order_type"], add["price"], add["qty"], add["side"]) == ("LIMIT", 660, 40, "BUY")
    r = tick(c, w, {S83300: 659.0}, "13:00")
    assert "⚡ Kite AUTO DRY · #1 add filled 660 ×40 · avg 687.5 · stop 615 now 80 qty" in r["lines"]
    k = bk.load(hid)["kite"]
    assert (k["entry_px"], k["qty_open"], k["lots"], k["legs"]["stop"]["qty"], k["legs"]["stop"]["trigger"]) == (687.5, 80, 4, 80, 615)
    v = tr.hint_view(bk.load(hid), config=c)
    assert v["pnl_strip"]["avg_text"] == "avg 687.5 · 4 lots" and v["kite"]["avg"] == 687.5
    assert not any(x["level"] == "red" for x in (k.get("check") or {}).get("items") or [])
    # one add only
    post(c, w, s, 2, "13:01", "we will add 2 more near 640")
    assert len([x for x in ko.rows_for(hid) if x["leg"] == "add"]) == 1
    assert any("one add per call" in x["text"] for x in bk.load(hid)["kite"]["log"])
    # the partial works on the total: half of 4 lots
    tick(c, w, {S83300: 760.0}, "13:10")
    post(c, w, s, 3, "13:11", "book part & keep tsl at cost for rest qty")
    tick(c, w, {S83300: 760.0}, "13:11", 30)
    k = bk.load(hid)["kite"]
    assert k["qty_open"] == 40 and k["legs"]["stop"]["qty"] == 40
    assert ko.realised_by_hint(ko.rows_for(hid))[hid] == round((760 - 687.5) * 40, 2)


def test_a_two_lot_add_on_banknifty_over_the_risk_is_refused(pushed):
    c, _, s = auto_setup(kite_max_lots=4, kite_follow_adds=True)
    w = bnworld(**{BN64: 445.0})
    hid = post(c, w, s, 1, "12:50", "BUY BANKNIFTY 64300 CE NEAR 430-440\nSL 360\nTGT 500/550")["hint_id"]
    tick(c, w, {BN64: 438.0}, "12:51")
    assert bk.load(hid)["kite"]["entry_px"] == 440
    post(c, w, s, 2, "12:52", "Add 2 lots near 425")
    assert not [x for x in ko.rows_for(hid) if x["leg"] == "add"]
    k = bk.load(hid)["kite"]
    assert "add" not in k or k.get("add") is None
    assert k["add_why"] == "(avg 430 − stop 360) × 90 = ₹6,300 at the stop with 2 more lots, above kite_risk_per_trade ₹6,000"
    assert any("not armed" in x["text"] and "₹6,300" in x["text"] for x in k["log"])


def test_add_over_the_lots_cap_is_not_armed(pushed):
    c, w, s = auto_setup(kite_follow_adds=True)                                   # kite_max_lots 2: 2 + 2 lots is over
    w = xworld(**{S83300: 700.0})
    hid = post(c, w, s, 1, "12:50", SX23)["hint_id"]
    tick(c, w, {S83300: 716.0}, "12:55")
    assert not [x for x in ko.rows_for(hid) if x["leg"] == "add"]
    assert any("2 + 2 lots > kite_max_lots 2" in x["text"] for x in bk.load(hid)["kite"]["log"])


def test_the_add_is_cancelled_with_the_exit_and_never_after_a_stop(pushed):
    c, w, s = auto_setup(kite_max_lots=4, kite_follow_adds=True)
    w = xworld(**{S83300: 700.0})
    hid = post(c, w, s, 1, "12:50", SX23)["hint_id"]
    tick(c, w, {S83300: 716.0}, "12:55")
    assert bk.load(hid)["kite"]["add"]["state"] == "armed"
    post(c, w, s, 2, "12:57", "exit")
    tick(c, w, {S83300: 700.0}, "12:58")
    k = bk.load(hid)["kite"]
    add = next(x for x in ko.rows_for(hid) if x["leg"] == "add")
    assert add["status"] == "cancelled" and k["add"]["state"] == "cancelled" and k["state"] == "closed"
    # a second call: the stop hits first → the resting add is cancelled, and a later "add" is never armed
    c2, _, s2 = auto_setup(kite_max_lots=4, kite_follow_adds=True)
    ce = S83300.replace("PE", "CE")
    w.prices[ce] = 700.0
    h2 = post(c2, w, s2, 11, "13:20", SX_NOADD.replace("83300 PE", "83300 CE"))["hint_id"]
    tick(c2, w, {ce: 716.0}, "13:21")
    post(c2, w, s2, 12, "13:22", "Add 2 lots near 660")
    assert bk.load(h2)["kite"]["add"]["state"] == "armed"
    post(c2, w, s2, 13, "13:23", "sl hit")                      # the channel's SL hit: the resting add goes with the exit
    tick(c2, w, {ce: 680.0}, "13:24")
    k2 = bk.load(h2)["kite"]
    rows = {x["leg"]: x for x in ko.rows_for(h2)}
    assert rows["add"]["status"] == "cancelled" and k2["add"]["state"] == "cancelled" and k2["state"] == "closed"
    post(c2, w, s2, 14, "13:26", "we will add 2 more near 600")                     # after the stop: never armed
    assert len([x for x in ko.rows_for(h2) if x["leg"] == "add"]) == 1
    assert ko.add_block(c2, s2, bk.load(h2), t19("13:26")) in ("the position is closed", "one add per call (already cancelled)")


def test_a_gap_through_the_add_and_the_stop_never_leaves_the_add_held(pushed):
    c, w, s = auto_setup(kite_max_lots=4, kite_follow_adds=True)
    w = xworld(**{S83300: 700.0})
    hid = post(c, w, s, 1, "12:50", SX23)["hint_id"]
    tick(c, w, {S83300: 716.0}, "12:55")
    r = tick(c, w, {S83300: 600.0}, "12:58")                    # one read fills the stop 615 AND the add 660
    assert any("add filled 660 ×40 after the stop · sold at market now" in ln for ln in r["lines"])
    tick(c, w, {S83300: 600.0}, "12:59")
    k = bk.load(hid)["kite"]
    assert k["state"] == "closed" and k["qty_open"] == 0


def test_never_with_the_kill_switch_or_the_switch_off_and_click_offers_place_add(pushed):
    c = cfg()
    c.tips.kite_mode, c.tips.kite_max_lots, c.tips.kite_follow_adds = "dry", 4, True
    w = xworld(**{S83300: 700.0})
    hid = post(c, w, w, 1, "12:50", SX23)["hint_id"]
    tr.poll(c, w, t19("12:50", 30))
    ko.place_all(c, ko.sim(), hid, now=t19("12:51"))
    tick(c, w, {S83300: 716.0}, "12:55")
    v = tr.hint_view(bk.load(hid), config=c)["kite"]
    assert "add" in v["actions"] and v["add_text"] == "channel adds 2 lots near 660 (after activation)"
    ko.set_kill(c, ko.sim(), True, now=t19("12:56"))
    with pytest.raises(TipsError):
        ko.place_add(c, ko.sim(), hid, now=t19("12:56", 30))
    ko.set_kill(c, ko.sim(), False, now=t19("12:57"))
    c.tips.kite_follow_adds = False
    hid2 = post(c, w, w, 2, "13:00", SX23.replace("83300 PE", "83300 CE"))["hint_id"]
    assert ko.add_block(c, ko.sim(), bk.load(hid2), t19("13:00")) == ko.ADD_OFF


# --- the three 23 Nov decoder / follow-up fixes ---------------------------------------------------------------------------

def test_nov_series_is_the_last_listed_monthly_expiry():
    d = dc.decode(NF23, date(2026, 11, 23))
    assert d["expiry_src"] == "series" and d["decoded"]["expiry"] == "series"
    assert d["entry"]["mode"] == "zone" and (d["entry"]["lo"], d["entry"]["hi"]) == (150, 155)
    assert d["stop"] == {"kind": "premium", "value": 90} and d["targets"] == [190, 220]
    assert d["adds"] == [{"lots": None, "level": 125, "cond": None, "at": "2026-11-23"}] and dc.looks_like_hint(d, NF23)
    from tests.tips_world import FakeClient
    w = FakeClient({"NFO": [opt("NIFTY", 25500, "CE", e, lot=65) for e in (date(2026, 11, 24), date(2026, 11, 25), date(2026, 12, 1))]})
    tr.refresh_instruments(w, "NFO", {"NIFTY"}, date(2026, 11, 23))
    h = {k: d.get(k) for k in ("symbol", "side", "instrument", "strike", "opt_type", "expiry", "expiry_src", "entry", "stop", "targets")}
    tr.resolve(h, None, date(2026, 11, 23))
    assert h["expiry"] == "2026-11-25" and h["tradingsymbol"] == "NIFTY26NOV25500CE" and h["expiry_src"] == "series"
    p = pl.build({**h, "exchange": "NFO"}, cfg().tips, t19("10:00"))
    assert p["expiry_kind"] == "monthly"
    h2 = {**{k: d.get(k) for k in ("symbol", "instrument", "strike", "opt_type", "expiry", "expiry_src")}, "strike": 25600}
    tr.resolve(h2, None, date(2026, 11, 23))                     # the month's last listed expiry, even with no such strike
    assert h2["expiry"] == "2026-11-25" and h2["tradingsymbol"] is None
    h3 = {**{k: d.get(k) for k in ("instrument", "strike", "opt_type", "expiry", "expiry_src")}, "symbol": "BANKNIFTY", "strike": 58000}
    tr.resolve(h3, None, date(2026, 11, 23))                     # nothing listed at all: the last Tuesday of November, over holidays
    assert h3["expiry"] == "2026-11-23"          # Tue 24 Nov 2026 is a holiday: moved back
    for t in ("monthly expiry", "monthly", "DEC SERIES"):
        assert dc.decode(f"BUY NIFTY 25500 CE 150-155\nSL 90\nTGT 190\n{t}", date(2026, 11, 23))["expiry_src"] == "series"
    assert dc.decode("BUY NIFTY 25500 CE 150-155\nSL 90\nTGT 190\nDEC SERIES", date(2026, 11, 23))["expiry"][:7] == "2026-12"


def test_23_nov_follow_up_shapes():
    assert pl.followup_action("buy 2 lots here") == {"action": "active", "lots": 2, "value": None}
    assert pl.followup_action("2 lots here")["lots"] == 2 and pl.followup_action("2 lots here")["action"] == "active"
    assert pl.followup_action("2-2 lots") == {"action": "size", "lots": 2}
    assert pl.followup_action("we will add 2 more near 125") == {"action": "add", "lots": 2, "level": 125, "cond": None}
    assert pl.followup_action("ADD MORE NEAR 125") == {"action": "add", "lots": None, "level": 125, "cond": None}
    for t in ("Der can book 156 full", "book full at 156", "can book 156 full", "defensive can book 156 full"):
        assert pl.followup_action(t)["action"] == "exit", t
    assert pl.followup_action("book part at 500")["action"] == "partial"
    a = pl.followup_action("DEFENSIVE TRADER: BOOK PART & KEEP TSL AT COST FOR REST QTY\n\nAGGRESSIVE TRADER: BOOK PART & FOR REST QTY KEEP TSL AT NPNL (NO PROFIT NO LOSS)")
    assert a["action"] == "partial" and a["cost"] is True
    d = dc.decode(SX23, date(2026, 11, 23))
    assert d["size_hint"] == {"lots": 2} and d["adds"][0]["lots"] == 2 and d["adds"][0]["level"] == 660 and d["adds"][0]["cond"] == "after activation"
    assert d["entry"]["mode"] == "above" and d["entry"]["lo"] == 715 and d["stop"]["value"] == 615 and d["targets"] == [800, 850, 900]


def test_size_follow_up_replans_before_the_fill_and_is_logged_after(pushed):
    c, w, s = auto_setup(kite_max_lots=4)
    w = xworld(**{S83300: 700.0})
    hid = post(c, w, s, 1, "12:50", SX_NOADD.replace("\n\n2 lots", ""))["hint_id"]
    k = bk.load(hid)["kite"]
    assert k["state"] == "armed" and k["lots"] == 1
    r = post(c, w, s, 2, "12:51", "2-2 lots")
    assert r["act"] == {"action": "size", "lots": 2} and bk.load(hid)["size_hint"] == {"lots": 2}
    k = bk.load(hid)["kite"]
    assert (k["lots"], k["qty"], ko.rows_for(hid)[0]["qty"]) == (2, 40, 40)
    tick(c, w, {S83300: 716.0}, "12:55")
    assert bk.load(hid)["kite"]["qty_open"] == 40
    post(c, w, s, 3, "12:56", "3-3 lots")
    k = bk.load(hid)["kite"]
    assert k["qty_open"] == 40 and any("after the fill: logged only" in x["text"] for x in k["log"])
