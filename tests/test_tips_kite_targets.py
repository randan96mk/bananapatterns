"""⚡ Tips → Kite: only the stop rests on Kite; the targets are WATCHED by the app (20 Nov, #11: a second resting sell
on one long option is refused for margin). T1 hit with 2 lots: the stop cancelled, half sold at market, a new stop at
cost for the rest, T2 hit: the rest sold, closed; 1 lot: a full exit; a sell hint mirrored; a refused / rejected market
exit re-places the stop at once (red line); a failed re-place → the Home item; a target already passed at a restart;
click mode watched the same; the migration of a resting target; never a LIMIT target; the check item; the AI says qty;
the card's NOW cell."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.tips import book as bk
from backend.tips import kite_check as kc
from backend.tips import kite_orders as ko
from backend.tips import track as tr
from tests.test_tips_kite import Broker
from tests.tips_world import BN12, KEY12, at, cfg, seed, world

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def pushed(monkeypatch):
    out = []
    monkeypatch.setattr(bk, "push", lambda config, key, html: out.append(html) or True)
    return out


def setup(mode="click"):
    c = cfg()
    c.tips.kite_mode = mode
    b = Broker(world(**{KEY12: 262.0}))
    hid = seed(c, b, BN12)["hint_id"]
    return c, b, hid


def entered(c, b, hid, lots=2, place_all=True, by="click"):
    """The entry filled at 262 (and with Place all the stop SL-M 190 resting)."""
    if place_all:
        ko.place_entry(c, b, hid, lots, by=by, now=at("10:00"), auto_rest=True)
    else:
        ko.place_entry(c, b, hid, lots, now=at("10:00"))
    b.fill(b.last(transaction_type=ko.entry_side(bk.load(hid)))["order_id"], 262.0)
    return ko.sync(c, b, at("10:00", sec=10))


def price(c, b, px, hhmm, sec=0):
    b.prices[KEY12] = px
    tr.poll(c, b, at(hhmm, sec=sec))


def no_limit_sell(b):
    return not [p for p in b.placed if p["order_type"] == "LIMIT" and p["transaction_type"] == ko.exit_side({"side": "BUY"})]


def test_two_lots_t1_half_new_stop_at_cost_t2_rest_closed_auto_line(pushed):
    c, b, hid = setup("auto")
    r = entered(c, b, hid, by="auto")
    assert r["lines"] == ["⚡ Kite · #1 entry filled 262 ×30 · SL 190 placed · T1 320 watched by the app"]
    sl = b.last(order_type="SL-M")
    price(c, b, 300.0, "10:10")
    assert ko.watch_all(c, b, at("10:10"))["open"] == 1 and b.placed[-1] is sl      # under T1: nothing
    price(c, b, 320.0, "10:20")                                                     # ≥ T1
    ko.watch_all(c, b, at("10:20"))
    m1 = b.placed[-1]
    assert b.cancelled[-1] == sl["order_id"] and (m1["order_type"], m1["transaction_type"], m1["quantity"]) == ("MARKET", "SELL", 15)
    v = {x["leg"]: x for x in tr.hint_view(bk.load(hid), config=c)["kite"]["legs"]}
    assert v["t1"]["status_text"] == "selling at market"
    assert ko.watch_all(c, b, at("10:20", sec=5))["lines"] == [] and b.placed[-1] is m1    # never a second exit while one works
    b.fill(m1["order_id"], 320.5)
    r = ko.sync(c, b, at("10:20", sec=10))
    assert r["lines"] == ["⚡ Kite AUTO · #1 T1 hit 320.5 · sold 15 of 30 at market · stop → cost 262 for the rest · watching T2 350"]
    assert pushed[-1] == r["lines"][0]
    sl2 = b.placed[-1]
    assert (sl2["order_type"], sl2["trigger_price"], sl2["quantity"], sl2["transaction_type"]) == ("SL-M", 262.0, 15, "SELL")
    v = {x["leg"]: x for x in tr.hint_view(bk.load(hid), config=c)["kite"]["legs"]}
    assert v["t1"]["status"] == "complete" and v["t1"]["status_text"] == "hit · sold 320.5" and v["t2"]["status"] == "watched"
    assert v["stop"]["trigger"] == 262.0 and v["stop"]["qty"] == 15
    price(c, b, 352.0, "11:00")
    ko.watch_all(c, b, at("11:00"))
    m2 = b.placed[-1]
    assert b.cancelled[-1] == sl2["order_id"] and (m2["order_type"], m2["quantity"]) == ("MARKET", 15)
    b.fill(m2["order_id"], 351.0)
    r = ko.sync(c, b, at("11:00", sec=10))
    k = bk.load(hid)["kite"]
    assert k["state"] == "closed" and k["qty_open"] == 0 and k["reason"] == "target 2"
    assert r["lines"][0].startswith("⚡ Kite AUTO · #1 T2 hit 351 · sold 15 at market") and r["lines"][0].endswith("total")
    assert no_limit_sell(b)


def test_one_lot_t1_is_a_full_exit(pushed):
    c, b, hid = setup()
    entered(c, b, hid, lots=1)
    sl = b.last(order_type="SL-M")
    price(c, b, 330.0, "10:20")
    ko.watch_all(c, b, at("10:20"))
    m = b.placed[-1]
    assert sl["order_id"] in b.cancelled and (m["order_type"], m["quantity"]) == ("MARKET", 15)
    b.fill(m["order_id"], 329.0)
    r = ko.sync(c, b, at("10:20", sec=10))
    assert r["lines"] == ["⚡ Kite · #1 T1 hit 329 · sold 15 at market · +₹1,005 · closed +₹1,005 total"]
    assert bk.load(hid)["kite"]["state"] == "closed" and no_limit_sell(b)


def test_never_worse_than_the_previous_stop(pushed):
    c, b, hid = setup()
    entered(c, b, hid)
    price(c, b, 300.0, "10:05")
    ko.modify_stop(c, b, hid, 280, now=at("10:05"))                  # the stop already above cost
    price(c, b, 321.0, "10:20")
    ko.watch_all(c, b, at("10:20"))
    b.fill(b.placed[-1]["order_id"], 321.0)
    r = ko.sync(c, b, at("10:20", sec=10))
    assert b.placed[-1]["trigger_price"] == 280.0 and "stop → 280 for the rest" in r["lines"][0]


def test_a_sell_hint_is_mirrored(pushed):
    c, b, hid = setup()
    h = bk.load(hid)
    h["side"] = "SELL"
    h["plan"]["side"] = "SELL"
    h["plan"]["stop"] = {"kind": "premium", "value": 300.0}
    h["plan"]["targets"] = [200.0, 180.0]
    bk.save(h)
    entered(c, b, hid)
    sl = b.last(order_type="SL-M")
    assert (sl["transaction_type"], sl["trigger_price"]) == ("BUY", 300.0)
    price(c, b, 210.0, "10:10")
    ko.watch_all(c, b, at("10:10"))
    assert b.placed[-1] is sl                                        # 210 is not ≤ 200
    price(c, b, 199.0, "10:20")
    ko.watch_all(c, b, at("10:20"))
    m = b.placed[-1]
    assert sl["order_id"] in b.cancelled and (m["order_type"], m["transaction_type"], m["quantity"]) == ("MARKET", "BUY", 15)
    b.fill(m["order_id"], 199.0)
    ko.sync(c, b, at("10:20", sec=10))
    s2 = b.placed[-1]
    assert (s2["order_type"], s2["transaction_type"], s2["trigger_price"], s2["quantity"]) == ("SL-M", "BUY", 262.0, 15)
    assert not [p for p in b.placed if p["order_type"] == "LIMIT" and p["transaction_type"] == "BUY" and p is not b.placed[0]]


def test_a_rejected_market_exit_re_places_the_stop_at_once(pushed):
    c, b, hid = setup()
    entered(c, b, hid)
    sl = b.last(order_type="SL-M")
    price(c, b, 321.0, "10:20")
    ko.watch_all(c, b, at("10:20"))
    m = b.placed[-1]
    b.reject(m["order_id"], "Insufficient funds")
    r = ko.sync(c, b, at("10:20", sec=5))
    assert r["lines"] == ["⚠️ Kite · #1 target exit refused: Insufficient funds · stop re-placed · exit by hand"]
    s2 = b.placed[-1]
    assert (s2["order_type"], s2["trigger_price"], s2["quantity"]) == ("SL-M", sl["trigger_price"], sl["quantity"])
    k = bk.load(hid)["kite"]
    assert k["legs"]["stop"]["status"] == "placed" and k["watch"] == "refused" and not k["no_stop"]
    n = len(b.placed)
    ko.watch_all(c, b, at("10:21"))                                  # the watch stops: the owner exits by hand
    assert len(b.placed) == n


def test_a_refused_exit_and_a_refused_re_place_raise_the_home_item(pushed):
    c, b, hid = setup()
    entered(c, b, hid)
    price(c, b, 321.0, "10:20")
    b.refuse = "Insufficient funds. Margin required: 195887.12"
    out = ko.watch_all(c, b, at("10:20"))
    assert out["lines"][0].startswith("⚠️ Kite · #1 target exit refused: Insufficient funds")
    assert out["lines"][0].endswith("no stop on Kite: exit by hand NOW") and out["lines"][0] in pushed
    k = bk.load(hid)["kite"]
    assert k["no_stop"] and "no stop on Kite: exit by hand NOW" in [x["text"] for x in k["check"]["items"] if x["level"] == "red"]
    assert ("warn", "⚡ Kite #1 · no stop on Kite: exit by hand NOW") in kc.attention_items(at("10:21"))
    b.refuse = None                                                  # the owner's Place rest… puts the stop back
    h = ko.place_rest(c, b, hid, now=at("10:22"))
    assert not h["kite"]["no_stop"] and h["kite"]["legs"]["stop"]["status"] == "placed"


def test_a_restart_with_the_price_already_past_t1_acts_on_the_first_tick(pushed):
    c, b, hid = setup()
    entered(c, b, hid)
    price(c, b, 335.0, "10:30")                                      # passed while the Syncer was down
    sy = ko.Syncer(SimpleNamespace(config=c, client=b))
    wait = sy.pass_once(at("10:30", sec=2))
    m = [p for p in b.placed if p["order_type"] == "MARKET"]
    assert len(m) == 1 and m[0]["quantity"] == 15 and wait == ko.SYNC_WATCH_S
    b.fill(m[0]["order_id"], 334.0)
    sy.pass_once(at("10:30", sec=7))
    assert bk.load(hid)["kite"]["qty_open"] == 15 and b.placed[-1]["trigger_price"] == 262.0


def test_click_mode_entry_then_place_sl_gets_the_same_watch(pushed):
    c, b, hid = setup()
    r = entered(c, b, hid, place_all=False)
    assert r["lines"] == ["⚡ Kite · #1 entry filled 262 ×30"]
    h = ko.place_rest(c, b, hid, now=at("10:01"))                    # "Place rest…" = the stop only + the watch
    assert [p["order_type"] for p in b.placed] == ["LIMIT", "SL-M"] and h["kite"]["auto_rest"]
    v = tr.hint_view(bk.load(hid), config=c)["kite"]
    assert "rest" not in v["actions"] and "targets" not in v["actions"]
    price(c, b, 320.0, "10:20")
    ko.watch_all(c, b, at("10:20"))
    assert b.placed[-1]["order_type"] == "MARKET" and b.placed[-1]["quantity"] == 15 and no_limit_sell(b)


def test_dry_run_watches_with_the_simulator(pushed):
    c, b, hid = setup("dry")
    price(c, b, 262.0, "10:00")
    s = ko.sim()
    ko.place_all(c, s, hid, 1, now=at("10:00", sec=1))
    ko.sync(c, s, at("10:00", sec=5))
    price(c, b, 330.0, "10:20")
    ko.watch_all(c, b, at("10:20", sec=1))
    r = ko.sync(c, s, at("10:20", sec=5))
    assert r["lines"] == ["⚡ Kite DRY · #1 T1 hit 330 · sold 15 at market · +₹1,020 · closed +₹1,020 total"]
    assert b.placed == [] and not [o for o in s.orders() if o["order_type"] == "LIMIT" and o["transaction_type"] == "SELL"]


def test_the_migration_cancels_a_resting_target_and_the_check_flags_it(pushed):
    c, b, hid = setup()
    entered(c, b, hid)
    h = bk.load(hid)
    t1 = ko._place(c, b, h, "t1", "SELL", 15, "LIMIT", at("10:01"), "app", price=320.0)     # the old resting path's T1
    t2 = ko._place(c, b, h, "t2", "SELL", 15, "LIMIT", at("10:01"), "app", price=350.0)
    b.reject(t2["order_id"], "Insufficient funds")                  # like #11: rejected for margin
    ko.save_kite(h)
    ko.sync(c, b, at("10:01", sec=5))
    res = kc.check(c, bk.load(hid), now=at("10:02"))
    assert any(x["text"].startswith("a target resting on Kite (target 1 LIMIT 320)") for x in res["items"] if x["level"] == "red")
    price(c, b, 300.0, "10:05")
    ko.watch_all(c, b, at("10:05"))
    k = bk.load(hid)["kite"]
    assert t1["order_id"] in b.cancelled and k["legs"]["t1"]["status"] == "watched" and k["legs"]["t2"]["status"] == "watched"
    mig = [p for p in pushed if "resting target 1 LIMIT 320 cancelled" in p]
    assert len(mig) == 1 and not [p for p in pushed if "resting target 2" in p]       # the rejected one silently
    assert not any("a target resting" in x["text"] for x in kc.check(c, bk.load(hid), now=at("10:06"))["items"])
    price(c, b, 321.0, "10:20")                                      # and from now on it is watched
    ko.watch_all(c, b, at("10:20"))
    assert b.placed[-1]["order_type"] == "MARKET"


def test_the_ai_input_says_qty_and_the_prompt_forbids_lots():
    c, b, hid = setup()
    entered(c, b, hid)
    src = kc.ai_input(bk.load(hid))
    assert "30 qty (2 lots of 15)" in src and "Order stop SELL 30 qty (2 lots of 15)" in src and " lots = " not in src
    assert "never call a quantity 'lots'" in kc.SYSTEM and "say qty and lots exactly as given" in kc.SYSTEM.lower()
    assert kc.qty_text({"lot_size": 65}, 65) == "65 qty (1 lot of 65)"


def test_the_card_has_a_now_cell_and_the_watched_legs(pushed):
    c, b, hid = setup()
    price(c, b, 262.0, "09:50")
    v = tr.hint_view(bk.load(hid), config=c)
    assert v["now"] == {"ltp": 262.0, "tone": "", "sub": "in the zone 250–270", "entered": False}
    entered(c, b, hid)
    price(c, b, 247.3, "10:05")
    v = tr.hint_view(bk.load(hid), config=c)
    assert v["now"]["tone"] == "down" and v["now"]["sub"] == "−5.6 % from 262"
    t1 = {x["leg"]: x for x in v["kite"]["legs"]}["t1"]
    assert (t1["status"], t1["type_text"], t1["now_text"]) == ("watched", "at ≥ 320", "now 247.3 · target 320 is +29.4 %")
    assert t1["detail"] == "watched by the app · half · +₹870 if hit"
    src = (ROOT / "frontend/src/components/TipsPage.jsx").read_text(encoding="utf-8")
    assert 'className={`nowc ' in src and '<div className="kk">now</div>' in src and "tp-dec${nowCell ? ' n7' : ''}" in src
    assert "Place T1…" not in src and "Place now…" not in src and "placed after T1" not in src
    kite_js = (ROOT / "frontend/src/components/tipsKite.js").read_text(encoding="utf-8")
    assert "a resting target would be rejected for margin on a long option" in kite_js
    home = (ROOT / "frontend/src/components/HomeTips.jsx").read_text(encoding="utf-8")
    assert "nowv" in home
