"""📏 lots ×N (tips.kite_lot_multiplier) · 🛑 the provisional stop when the post has no SL (tips.kite_default_stop) ·
the owner's "Set stop…" (page / Telegram) and the channel's SL replacing a provisional / owner stop, both under the one
Kite lock (both orders, armed and entered, one Kite modify per change, the event order, two threads)."""

import threading

import pytest

from backend.config import AppConfig
from backend.tips import book as bk
from backend.tips import kite_auto as ka
from backend.tips import kite_orders as ko
from backend.tips import kite_sim
from backend.tips import plan as pl
from backend.tips import reader as rd
from backend.tips import sizing
from backend.tips import track as tr
from backend.tips.book import TipsError
from tests.test_tips_api import api  # noqa: F401  (the TestClient fixture)
from tests.test_tips_kite_auto import S82400, auto_setup, post, t19, tick
from tests.test_tips_telegram import bot
from tests.tips_world import BN12, KEY12, at, cfg, seed, world

NOSL = "BUY SENSEX 82400 PE ABOVE 306\nTGT 380/420"            # no SL: 306 × 0.75 = 229.5 provisional
NOSL_ZONE = "BUY SENSEX 82400 PE NEAR 300-306\nTGT 380/420"    # a zone without "when comes": fills on the first quote


@pytest.fixture
def pushed(monkeypatch):
    out = []
    monkeypatch.setattr(bk, "push", lambda config, key, html: out.append(html) or True)
    monkeypatch.setattr(rd, "PUSH_MAX_AGE_S", 10 ** 9)
    return out


@pytest.fixture
def mods(monkeypatch):
    """Every modify_order the simulator receives (the dry run's broker)."""
    seen = []
    orig = kite_sim.SimClient.modify_order

    def rec(self, order_id, **kw):
        seen.append((order_id, kw))
        return orig(self, order_id, **kw)
    monkeypatch.setattr(kite_sim.SimClient, "modify_order", rec)
    return seen


def hint(**kw):
    h = {"id": None, "side": "BUY", "lot_size": 15, "entry": {"mode": "at", "lo": 260.0}, "stop": {"kind": "premium", "value": 190.0}}
    h.update(kw)
    return h


def conf(**kw):
    c = AppConfig()
    for k, v in kw.items():
        setattr(c.tips, k, v)
    return c


# --- 📏 lots ×N ------------------------------------------------------------------------------------------------------------

def test_multiplier_post_fallback_fixed_and_not_risk():
    c = conf(kite_lot_multiplier=2, kite_max_lots=6, kite_risk_per_trade=99999.0)
    s = sizing.size(c, hint(size_hint={"lots": 2}))
    assert (s["lots"], s["base"], s["mult"], s["src"]) == (4, 2, 2, "post")
    assert sizing.size(c, hint())["lots"] == 2 and sizing.size(c, hint())["src"] == "fallback"       # tips.lots 1 × 2
    c.tips.kite_size_mode, c.tips.lots = "fixed", 3
    assert sizing.size(c, hint(size_hint={"lots": 2}))["lots"] == 6
    c.tips.kite_size_mode, c.tips.kite_risk_per_trade = "risk", 6000.0
    r = sizing.size(c, hint())                                    # the rupee risk decides, and the risk limit scales ×2: 12000 ÷ (70 × 15) = 11.4 → 11
    assert (r["src"], r["raw"], r["lots"], r["mult"]) == ("risk", 11, 11, 1)
    c1 = conf(kite_size_mode="risk", kite_max_lots=6, kite_risk_per_trade=6000.0)
    assert sizing.size(c1, hint())["lots"] == 5                   # ×1: 6000 ÷ (70 × 15) = 5.71 → 5


def test_multiplier_cell_text_and_the_lots_text():
    c = conf(kite_lot_multiplier=2, kite_max_lots=6, kite_risk_per_trade=99999.0)
    cell = sizing.cell(c, hint(size_hint={"lots": 2}))
    assert cell["line1"].startswith("4 lots · 60 qty") and "the post says 2 lots × 2 (lots ×2)" in cell["line2"]
    assert "lots ×2" in cell["line3"] and cell["mult"] == 2
    assert sizing.mult_text(sizing.size(c, hint(size_hint={"lots": 2}))) == "4 lots (2 × 2)"
    c1 = conf()
    assert "×" not in sizing.cell(c1, hint(size_hint={"lots": 2}))["line2"]


def test_caps_apply_after_multiplying():
    c = conf(kite_lot_multiplier=2, kite_max_lots=6, kite_risk_per_trade=99999.0)
    h = hint(size_hint={"lots": 4}, id=None)
    assert sizing.size(c, h)["lots"] == 8
    g = sizing.check(conf(kite_risk_per_trade=6000.0), h, 8)          # 8 × 15 × 70 = 8,400 over 6,000
    assert g["held"] and "8 lots × 70 points × 15" in sizing.held_text(g)
    assert ka.auto_lots(c, h) == 8                                     # auto: under the cap, which scales too (6 × 2 = 12)
    assert ka.auto_lots(conf(kite_lot_multiplier=2, kite_max_lots=3, kite_risk_per_trade=99999.0), h) == 6   # capped at 3 × 2


def test_paper_size_and_owner_lots_literal():
    c = conf(kite_lot_multiplier=3)
    h = hint(size_hint={"lots": 2})
    assert bk.size_for(h, c) == (6, "post")
    assert bk.size_for(hint(), c) == (3, "default")
    assert bk.size_for(h, c, lots=2) == (2, "you")                     # typed lots: literal


def test_add_lots_times_the_multiplier_then_the_cap():
    c = conf(kite_lot_multiplier=2, kite_max_lots=6, kite_risk_per_trade=99999.0)
    h = hint(kite={"state": "entered", "qty_open": 30, "entry_px": 262.0, "stop_px": 190.0}, plan={"stop": {"kind": "premium", "value": 190.0}})
    n, bad, nums = ko.add_size(c, h, {"lots": 2, "level": 250.0})
    assert (n, bad) == (4, None)                                       # 2 × 2 = 4, with 2 open: 6 ≤ the cap (6 × 2 = 12)
    n, bad, _ = ko.add_size(conf(kite_lot_multiplier=3, kite_max_lots=2, kite_risk_per_trade=99999.0), h, {"lots": 2, "level": 250.0})
    assert n == 4 and bad is None                                      # 6 wanted, 4 left under the cap 2 × 3 = 6


def test_settings_validation():
    c = AppConfig()
    assert c.tips.kite_lot_multiplier == 1 and c.tips.kite_default_stop == 25
    for bad in ({"kite_lot_multiplier": 6}, {"kite_lot_multiplier": 0}, {"kite_default_stop": 5}, {"kite_default_stop": 70}):
        with pytest.raises(ValueError):
            c.update_section("tips", bad)
    c.update_section("tips", {"kite_lot_multiplier": "2", "kite_default_stop": 0})
    assert c.tips.kite_lot_multiplier == 2 and c.tips.kite_default_stop == 0


def test_auto_arm_uses_the_multiplied_size(pushed):
    c, w, s = auto_setup(kite_lot_multiplier=2, kite_max_lots=4, kite_risk_per_trade=99999.0)
    w.prices[S82400] = 280.0
    hid = post(c, w, s, 1, "13:52", "BUY SENSEX 82400 PE ABOVE 306\nSL 250\nTGT 380/420\n2 lots")["hint_id"]
    k = bk.load(hid)["kite"]
    assert k["state"] == "armed" and k["lots"] == 4 and k["qty"] == 80
    assert any(p.split("\n")[-1].startswith("auto DRY: armed 4 lots (2 × 2) · ") for p in pushed)     # the call's own last line (T3)


def test_the_channels_size_line_is_multiplied(pushed):
    c, w, s = auto_setup(kite_lot_multiplier=2, kite_max_lots=4, kite_risk_per_trade=99999.0)
    w.prices[S82400] = 280.0
    hid = post(c, w, s, 1, "13:52", "BUY SENSEX 82400 PE ABOVE 306\nSL 250\nTGT 380/420")["hint_id"]
    assert bk.load(hid)["kite"]["lots"] == 2                            # fallback 1 × 2
    post(c, w, s, 2, "13:53", "2-2 lots")
    assert bk.load(hid)["kite"]["lots"] == 4                            # the channel's 2 × 2


# --- 🛑 the provisional stop -------------------------------------------------------------------------------------------------

def mk(text, **kw):
    from backend.config import TipsConfig
    from backend.tips import decode as dc
    d = dc.decode(text, at("09:43"))
    d["exchange"], d["expiry"] = "BFO", "2026-11-20"
    return d, pl.build(d, TipsConfig(**kw), at("09:43"))


def test_provisional_stop_in_the_plan():
    d, p = mk(NOSL)
    assert p["ok"] and d["stop"] == {"kind": "premium", "value": 229.5, "src": "provisional", "pct": 25, "level": 306.0}
    assert "provisional stop 229.5 (−25 %) · the channel's SL replaces it" in p["text"]
    assert pl.stop_text(d["stop"]) == "stop 229.5 (provisional −25 %)"
    _, p = mk(NOSL, kite_default_stop=0)                              # off: today's rule
    assert not p["ok"] and p["why"] == "stop missing"
    _, p = mk("BUY SENSEX 82400 PE above 82500 spot\nTGT 380")         # an index level: never a bogus stop
    assert not p["ok"] and p["why"] == "stop missing"
    _, p = mk("BUY SENSEX 82400 PE\nTGT 380")                          # a market entry: no level
    assert not p["ok"]
    d, p = mk("SELL SENSEX 82400 PE ABOVE 301\nTGT 200", kite_default_stop=30)
    assert p["ok"] and d["stop"]["value"] == 391.3                      # a sell: over the level, on the 0.05 tick
    d, _ = mk("BUY SENSEX 82400 PE ABOVE 333\nTGT 400", kite_default_stop=15)
    assert d["stop"]["value"] == 283.05                                 # 333 × 0.85 = 283.05 (a valid tick)


def test_no_sl_call_arms_on_the_provisional_stop_and_the_channels_sl_replaces_it_before_the_fill(pushed, mods):
    c, w, s = auto_setup()
    w.prices[S82400] = 280.0
    hid = post(c, w, s, 1, "13:52", NOSL)["hint_id"]
    h = bk.load(hid)
    assert h["plan"]["ok"] and h["stop"]["src"] == "provisional" and h["kite"]["state"] == "armed" and h["kite"]["stop_px"] == 229.5
    assert any("no SL in the post · provisional stop 229.5 (−25 %) · plan complete" in e["text"] for e in h["events"])
    tile = tr.stop_tile(h)
    assert tile["value_text"] == "229.5 · provisional" and tile["cls"] == "prov" and tile["chip"].startswith("stop provisional −25 %")
    r = post(c, w, s, 2, "13:53", "Sl 300")
    h = bk.load(hid)
    assert r["kind"] == "follow_up" and r["hint_id"] == hid
    assert h["stop"]["value"] == 300 and h["stop"]["src"] == "channel" and h["plan"]["stop"]["value"] == 300
    assert h["kite"]["stop_px"] == 300 and mods == []
    assert any(e["text"].startswith('channel: "Sl 300" · stop 229.5 (provisional) → 300 (the channel\'s)') for e in h["events"])
    assert any('channel: "Sl 300" · stop 229.5 (provisional) → 300' in p for p in pushed)
    assert tr.stop_tile(h)["sub"].startswith("was 229.5 provisional (−25 %)")
    tick(c, w, {S82400: 310.0}, "13:55")                               # the fill: the SL goes at the channel's 300
    assert bk.load(hid)["kite"]["legs"]["stop"]["trigger"] == 300


def test_the_channels_sl_after_the_fill_modifies_the_kite_stop_once(pushed, mods):
    c, w, s = auto_setup()
    w.prices[S82400] = 280.0
    hid = post(c, w, s, 1, "13:52", NOSL)["hint_id"]
    tick(c, w, {S82400: 310.0}, "13:55")
    k = bk.load(hid)["kite"]
    assert k["state"] == "entered" and k["legs"]["stop"]["trigger"] == 229.5
    post(c, w, s, 2, "13:57", "Sl 300")
    k = bk.load(hid)["kite"]
    assert len(mods) == 1 and mods[0][1]["trigger_price"] == 300.0
    assert k["legs"]["stop"]["trigger"] == 300 and k["stop_px"] == 300
    assert any("stop moved to 300 (the channel's SL)" in x["text"] for x in k["log"])
    assert texts(hid)[-1].endswith("· Kite: SL order modified 229.5 → 300 · applied on its own")
    post(c, w, s, 3, "13:58", "Sl 300")                                # the channel's again: not provisional any more
    assert len(mods) == 1


def test_closed_on_the_provisional_stop_the_later_sl_is_only_logged(pushed):
    c = cfg()
    w = world()
    from tests.test_tips_kite_auto import sworld
    w = sworld(**{S82400: 303.0})
    hid = post(c, w, w, 1, "13:52", NOSL_ZONE)["hint_id"]
    tr.poll(c, w, t19("13:53"))
    w.prices[S82400] = 220.0
    tr.poll(c, w, t19("13:56"))
    h = bk.load(hid)
    assert h["status"] == "closed" and h["ended"] == "stop"
    for ex, names in (("NFO", {"BANKNIFTY", "NIFTY"}), ("BFO", {"SENSEX"})):
        tr.refresh_instruments(w, ex, names, t19("13:58").date())
    rd.ingest(c, "index options buying with aakanksha", 2, t19("13:58"), "Sl 250", False, source="live", push=True, model=False,
              client=w, reply_to=1)                                    # a reply to the closed call
    h = bk.load(hid)
    assert h["stop"]["src"] == "provisional" and h["stop"]["value"] == 229.5
    assert h["events"][-1]["text"] == 'channel: "Sl 250" · the channel\'s SL 250 · logged · the trade closed at the provisional stop'


def test_paper_record_uses_the_provisional_stop(pushed):
    from tests.test_tips_kite_auto import sworld
    c = cfg()
    w = sworld(**{S82400: 303.0})
    hid = post(c, w, w, 1, "13:52", NOSL_ZONE)["hint_id"]
    h = bk.take(c, hid, ltp=303.0, now=t19("13:53"), notify=False)
    assert h["pos"]["run"]["stop"] == 229.5 and h["run"]["stop"] == 229.5


# --- 👤 "Set stop…" and the channel's SL: one lock, the later wins, one modify each ------------------------------------------

def armed(pushed):
    c, w, s = auto_setup()
    w.prices[S82400] = 280.0
    hid = post(c, w, s, 1, "13:52", NOSL)["hint_id"]
    return c, w, s, hid


def entered(pushed):
    c, w, s, hid = armed(pushed)
    tick(c, w, {S82400: 310.0}, "13:55")
    assert bk.load(hid)["kite"]["state"] == "entered"
    return c, w, s, hid


def texts(hid):
    return [e["text"] for e in bk.load(hid)["events"]]


def test_set_stop_before_the_fill_rewrites_the_plan_and_stop_px(pushed, mods):
    c, w, s, hid = armed(pushed)
    h = bk.set_stop(c, hid, 290, now=t19("13:53", 5))
    assert h["stop"]["value"] == 290 and h["stop"]["src"] == "owner" and h["plan"]["stop"]["value"] == 290 and h["plan"]["ok"]
    assert h["kite"]["stop_px"] == 290 and mods == []
    assert texts(hid)[-1] == "you set the stop to 290 (page) · 1:53:05 PM · replaced the provisional 229.5 · Kite: plan stop 229.5 → 290 (you) · the stop placed on the fill uses it"
    t = tr.stop_tile(h)
    assert t["value_text"] == "290 · yours" and t["cls"] == "mine" and t["sub"].startswith("was 229.5 provisional (−25 %)")
    tick(c, w, {S82400: 310.0}, "13:55")
    assert bk.load(hid)["kite"]["legs"]["stop"]["trigger"] == 290


def test_set_stop_after_the_fill_modifies_kite_once_and_refuses_bad_ones(pushed, mods):
    c, w, s, hid = entered(pushed)
    with pytest.raises(TipsError) as e:
        bk.set_stop(c, hid, 320, now=t19("13:56"))                     # over the price 310 for a buy
    assert e.value.code == 400 and "wrong side" in str(e.value) and mods == []
    with pytest.raises(TipsError) as e:
        bk.set_stop(c, hid, -3)
    assert e.value.code == 400
    h = bk.set_stop(c, hid, 290, now=t19("13:56"))
    assert len(mods) == 1 and h["kite"]["legs"]["stop"]["trigger"] == 290 and h["stop_kite"] == "Kite: SL order modified 229.5 → 290"
    bk.set_stop(c, hid, 290, now=t19("13:56", 20))                    # idempotent: the SL already sits at 290
    assert len(mods) == 1
    with pytest.raises(TipsError) as e:                                # stale: the page saw rev 1 (the provisional stop)
        bk.set_stop(c, hid, 295, now=t19("13:57"), rev=1)
    assert e.value.code == 409 and str(e.value).startswith("the stop changed to 290 (yours) at 1:56:20 PM · set again if you still want 295")
    ko.exit_now(c, s, hid, now=t19("13:58"))
    tick(c, w, {S82400: 312.0}, "13:59")
    with pytest.raises(TipsError) as e:
        bk.set_stop(c, hid, 280, now=t19("14:00"))
    assert e.value.code == 409


@pytest.mark.parametrize("state", ["armed", "entered"])
def test_owner_then_channel_the_channel_wins(pushed, mods, state):
    c, w, s, hid = (armed if state == "armed" else entered)(pushed)
    bk.set_stop(c, hid, 290, now=t19("13:56", 5))
    post(c, w, s, 2, "13:56", "Sl 300")                               # the channel's, posted 2 s later
    h = bk.load(hid)
    assert h["stop"]["value"] == 300 and h["stop"]["src"] == "channel" and h["kite"]["stop_px"] == 300
    ev = [x for x in texts(hid) if "290" in x or "Sl 300" in x]
    assert ev[0].startswith("you set the stop to 290 (page) · 1:56:05 PM")
    assert ev[1].startswith('channel: "Sl 300" · your 290 → 300 (the channel\'s)') and "· replaced yours · Kite: " in ev[1]
    if state == "entered":
        assert [m[1]["trigger_price"] for m in mods] == [290.0, 300.0]   # one modify per change
        assert h["kite"]["legs"]["stop"]["trigger"] == 300
    else:
        assert mods == []
    sub = tr.stop_tile(h)["sub"]
    assert sub.startswith("was 290 yours (1:56:05 PM) · before that 229.5 provisional (−25 %)")


@pytest.mark.parametrize("state", ["armed", "entered"])
def test_channel_then_owner_the_owner_wins(pushed, mods, state):
    c, w, s, hid = (armed if state == "armed" else entered)(pushed)
    post(c, w, s, 2, "13:56", "Sl 300")
    h = bk.set_stop(c, hid, 290, now=t19("13:56", 9), rev=bk.stop_rev(bk.load(hid)))
    assert h["stop"]["value"] == 290 and h["stop"]["src"] == "owner" and h["kite"]["stop_px"] == 290
    ev = [x for x in texts(hid) if "290" in x or "Sl 300" in x]
    assert ev[0].startswith('channel: "Sl 300" · stop 229.5 (provisional) → 300 (the channel\'s)')
    assert ev[1].startswith("you set the stop to 290 (page) · 1:56:09 PM · replaced the channel's 300 · Kite: ")
    if state == "entered":
        assert [m[1]["trigger_price"] for m in mods] == [300.0, 290.0]
    else:
        assert mods == []
    post(c, w, s, 3, "13:57", "Sl 300")                               # the channel's SL comes again: it replaces yours
    h = bk.load(hid)
    assert h["stop"]["value"] == 300 and h["stop"]["src"] == "channel"
    assert len(mods) == (3 if state == "entered" else 0)


def test_two_threads_never_deadlock_and_end_consistent(pushed, mods):
    c, w, s, hid = entered(pushed)
    errs = []

    def owner():
        try:
            bk.set_stop(c, hid, 290, now=t19("13:56", 5))
        except Exception as exc:                      # noqa: BLE001
            errs.append(exc)

    def channel():
        try:
            post(c, w, s, 2, "13:56", "Sl 300")
        except Exception as exc:                      # noqa: BLE001
            errs.append(exc)
    ts = [threading.Thread(target=owner), threading.Thread(target=channel)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert not any(t.is_alive() for t in ts) and errs == []
    h = bk.load(hid)
    k = h["kite"]
    assert h["stop"]["value"] in (290, 300) and k["stop_px"] == h["stop"]["value"] == k["legs"]["stop"]["trigger"]
    assert h["plan"]["stop"]["value"] == h["stop"]["value"]
    assert 1 <= len(mods) <= 2 and mods[-1][1]["trigger_price"] == float(h["stop"]["value"])


# --- the API and Telegram -------------------------------------------------------------------------------------------------------

def test_api_set_stop(api):  # noqa: F811
    seed(cfg(), world(), BN12)
    r = api.post("/api/tips/hints/1/stop", json={"value": 200})
    assert r.status_code == 200 and r.json()["stop_tile"]["value_text"] == "200 · yours"
    assert r.json()["stop"]["src"] == "owner" and r.json()["plan"]["stop"]["value"] == 200
    r = api.post("/api/tips/hints/1/stop", json={"value": 210, "rev": 1})
    assert r.status_code == 409 and "the stop changed to 200 (yours)" in r.json()["detail"]
    assert api.post("/api/tips/hints/1/stop", json={"value": -1}).status_code == 400
    assert api.post("/api/tips/hints/9/stop", json={"value": 200}).status_code == 404


def test_telegram_stop_command():
    w = world(**{KEY12: 262.0})
    b, n, fake, st = bot(["stop 1 250", "stop 1 300", "stop 1", "stop 9 100"], w)
    seed(st.config, w, BN12, "09:43", 1)
    bk.take(st.config, 1, ltp=262.0, now=at("09:51"), notify=False)
    tr.poll(st.config, w, at("09:52"))
    assert b.poll_once(n) == 4
    t = [x for _, x in fake.sent]
    assert t[0] == "👤 #1 · stop set to 250 (you) · was 190 the channel's"
    assert t[1] == "Not changed: the stop 300 is on the wrong side of the price 262"
    assert t[2] == "Stop at what? stop 1 &lt;price&gt;" and t[3] == "Not changed: no hint #9"
    h = bk.load(1)
    assert h["stop"]["value"] == 250 and h["pos"]["run"]["stop"] == 250


def test_caps_scale_with_the_multiplier():
    """The owner's ask: once lots ×N is set, kite_max_lots and kite_risk_per_trade scale with it on their own (they are set
    for 1×); the daily loss cap does not. The strip payload carries both the base and the effective figures."""
    from backend.tips import sizing, kite_orders as ko
    c = cfg()
    c.tips.kite_max_lots, c.tips.kite_risk_per_trade, c.tips.kite_lot_multiplier = 6, 20000.0, 1
    assert (sizing.max_lots(c), sizing.risk_limit(c)) == (6, 20000.0)
    c.tips.kite_lot_multiplier = 3
    assert (sizing.max_lots(c), sizing.risk_limit(c)) == (18, 60000.0)
    assert sizing.cfg_of(c)["cap"] == 18 and sizing.cfg_of(c)["risk"] == 60000.0
    lite = ko.lite(c)
    assert lite["caps"]["max_lots"] == 18 and lite["caps"]["max_lots_base"] == 6
    assert lite["sizing"]["risk_per_trade"] == 60000.0 and lite["sizing"]["risk_per_trade_base"] == 20000.0
    assert lite["caps"]["loss_cap"] == float(c.tips.kite_loss_cap)                 # unchanged
