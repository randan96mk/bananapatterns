"""🔕 Tips Telegram tidy (T1–T7) and the reader fixes' units (A1, A2, A4, A6): what is pushed and what only goes to the
card log; the new call's message with its plan and auto state folded in; plan completed + armed as one line; "add N"."""

import pytest

from backend.alerts.commands import BotCommands
from backend.alerts.dispatch import Notifier
from backend.config import AlertsConfig
from backend.tips import book as bk
from backend.tips import filter as flt
from backend.tips import kite_orders as ko
from backend.tips import plan as pl
from backend.tips import reader as rd
from tests.test_login import BotTG, update
from tests.test_tips_kite_auto import S82400, auto_setup, post, t19, tick
from tests.test_tips_sizing import S83300, SX23, xworld
from tests.tips_world import BN12, KEY12, at, cfg, seed, world


@pytest.fixture
def pushed(monkeypatch):
    out = []
    monkeypatch.setattr(bk, "push", lambda config, key, html: out.append((key, html)) or True)
    monkeypatch.setattr(rd, "PUSH_MAX_AGE_S", 10 ** 9)
    return out


def ing(c, w, pid, hhmm, text, reply_to=None):
    return rd.ingest(c, "aakankshatrading", pid, at(hhmm), text, False, push=True, model=False, client=w, reply_to=reply_to)


def fu_keys(pushed):
    return [k for k, _ in pushed if k.startswith("tips|fu|")]


# --- A1 / A4 / A6 units -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,kind", [
    ("Not active avoid", "avoid"), ("not activated again , lets avoid", "avoid"), ("Avoid this", "avoid"), ("cancel it", "avoid"),
    ("active", "active"), ("Cmp 527", "active"), ("Tgt 1 done ✅✅✅", "result"), ("Target 1 and 2 done ✅✅", "result"),
    ("Century done", "result"), ("all tgt done & dusted 💥💥💥", "result"), ("sl hit", "result"), ("book part now", "book"),
    ("trail sl to cost", "book"), ("wait", "wait"), ("wait for next level", "wait"), ("wait for level to activate", "wait"),
    ("Hows that", None), ("Waiting for opportunity", None), ("590+", None),
    ("All trades which we are providing are Intraday trades only , please exit them before market close", None),
])
def test_strong_kinds(text, kind):
    assert flt.strong_kind(text) == kind


def test_wait_is_no_action_and_not_active_is_avoid():
    assert pl.followup_action("wait") == {"action": "wait"}
    assert pl.followup_action("wait for next level") == {"action": "wait"}
    assert pl.followup_action("not active")["action"] == "avoid"
    assert pl.followup_action("Target 1 and 2 done") == {"action": "targets", "n": 2}
    a = pl.followup_action("2 lots\n\nAdd 2 lots near 700 also after activation \n\nSl 640\n\nTgt 820/860/900")
    assert (a["action"], a["lots"], a["level"], a["size_lots"]) == ("add", 2, 700, 2)
    assert "size_lots" not in pl.followup_action("Add 2 lots near 700")
    assert pl.followup_action("closed")["action"] == "exit" and pl.followup_action("cost 2 cost")["action"] == "exit"   # A7


def test_bare_target_list():
    h = {"side": "BUY", "entry": {"mode": "above", "lo": 553, "hi": 553}}
    assert flt.bare_targets("625/660/700/750/800", h) == [625, 660, 700, 750, 800]
    assert flt.bare_targets("625 660 700", h) == [625, 660, 700]
    assert flt.bare_targets("500/660/700", h) is None                     # under the entry: not targets
    assert flt.bare_targets("700/660", h) is None                         # not rising
    assert flt.bare_targets("635\n\nTGT 3 DONE ✅✅✅") is None               # a result stays a result
    assert flt.bare_targets("640") is None


def test_a_strong_follow_up_names_its_own_strike():
    hs = [{"id": 1, "symbol": "SENSEX", "strike": 83500, "status": "new", "run": {"state": "waiting"}},
          {"id": 2, "symbol": "SENSEX", "strike": 84800, "status": "new", "run": {"state": "waiting"}}]
    assert flt.strong_followup("avoid", hs) == 2
    hs[0]["opt_type"] = hs[1]["opt_type"] = "CE"
    assert flt.strong_followup("83500 ce avoid", hs) == 1
    hs[0]["run"] = {"state": "active"}                                    # the level reached on the record: results go there
    assert flt.strong_followup("tgt 1 done", hs) == 1


# --- A2: avoid pauses every not-taken state -------------------------------------------------------------------------------------

def test_avoid_pauses_waiting_allowed_and_level_reached_never_an_open_position():
    c, w = cfg(), world(**{KEY12: 262.0})
    for state, allowed in (("waiting", True), ("active", False)):
        hid = seed(c, w, BN12, "09:43", 100 + len(state))["hint_id"]
        h = bk.load(hid)
        h["run"].update(state=state, allowed=allowed, entry_px=262.0 if state == "active" else None, best=0.0, worst=0.0)
        line = bk.channel_action(c, h, {"action": "avoid"}, "Not active avoid", at("09:50"))
        assert line == "hint paused · no entry until the channel says enter"
        assert h["run"]["paused"] and not h["run"]["allowed"] and h["status"] == "paused"
    hid = seed(c, w, BN12.replace("250-270", "250-275"), "10:00", 200)["hint_id"]
    bk.take(c, hid, ltp=262.0, now=at("10:01"), notify=False)
    h = bk.load(hid)
    assert h["pos"]["state"] == "open"
    assert bk.channel_action(c, h, {"action": "avoid"}, "avoid", at("10:02")) == "noted (a paper position is open)"
    assert not h["pos"]["run"].get("paused")


# --- T1 / T2: what is pushed ---------------------------------------------------------------------------------------------------

def test_quiet_follow_ups_go_to_the_card_log_only(pushed):
    c, w = cfg(), world()
    hid = seed(c, w, BN12, "09:43", 1)["hint_id"]
    r = ing(c, w, 2, "09:44", "262", reply_to=1)                          # a price
    assert r["kind"] == "follow_up" and r["quiet"] and not fu_keys(pushed)
    r = ing(c, w, 3, "09:45", "wait for next level")
    assert r["hint_id"] == hid and r["act"]["action"] == "wait" and not fu_keys(pushed)
    r = ing(c, w, 4, "09:46", "Defensive Style:\nBANKNIFTY 50300 PE 3560 Profit booked in demo account", reply_to=1)
    assert r["kind"] == "follow_up" and not fu_keys(pushed)
    assert any('channel: "262"' in e["text"] for e in bk.load(hid)["events"])          # logged on the card
    r = ing(c, w, 5, "09:47", "Not active avoid")                         # an instruction: pushed
    assert fu_keys(pushed) == ["tips|fu|aakankshatrading|5"]
    c.tips.push_level = "all"                                             # all = every line, as before
    ing(c, w, 6, "09:48", "265", reply_to=1)
    assert fu_keys(pushed)[-1] == "tips|fu|aakankshatrading|6"


def test_no_target_yet_once_and_only_for_a_hint_we_hold(pushed):
    c, w = cfg(), world(**{KEY12: 262.0})
    seed(c, w, BN12, "09:43", 1)
    ing(c, w, 2, "09:44", "Tgt 1 done ✅✅", reply_to=1)
    assert not fu_keys(pushed)                                            # not held: logged only
    c2, w2 = cfg(), w
    hid = seed(c2, w2, BN12.replace("250-270", "250-275"), "10:00", 10)["hint_id"]
    bk.take(c2, hid, ltp=262.0, now=at("10:01"), notify=False)
    r1 = ing(c2, w2, 11, "10:02", "Tgt 1 done ✅✅", reply_to=10)
    r2 = ing(c2, w2, 12, "10:03", "Tgt 2 done ✅✅", reply_to=10)
    assert r1["line"].startswith(rd.NO_TARGET_YET) and r2["line"].startswith(rd.NO_TARGET_YET)
    assert fu_keys(pushed) == ["tips|fu|aakankshatrading|11"]             # once per hint


# --- T6: the provisional → the channel's SL ------------------------------------------------------------------------------------

def test_the_channels_sl_over_a_provisional_stop_is_pushed_only_when_held(pushed):
    c, w = cfg(), world(**{KEY12: 262.0})
    call = "Buy bank nifty 50300 PE near 250-270 when comes \nTgt 320-350-400++"
    hid = seed(c, w, call, "09:43", 1)["hint_id"]
    assert bk.load(hid)["stop"]["src"] == "provisional"
    ing(c, w, 2, "09:44", "Sl 190")
    assert bk.load(hid)["stop"]["value"] == 190 and not [k for k, _ in pushed if k.startswith("tips|stop|")]
    hid2 = seed(c, w, call.replace("250-270", "250-275"), "10:00", 10)["hint_id"]
    bk.take(c, hid2, ltp=262.0, now=at("10:01"), notify=False)
    ing(c, w, 11, "10:02", "Sl 195", reply_to=10)
    assert bk.load(hid2)["stop"]["value"] == 195 and [k for k, _ in pushed if k.startswith(f"tips|stop|{hid2}|")]


# --- T3: the new call's message -------------------------------------------------------------------------------------------------

def test_the_call_message_plan_once_provisional_stop_and_auto_off(pushed):
    c, w = cfg(), world()
    ing(c, w, 1, "09:43", "Buy bank nifty 50300 PE near 250-270 when comes \nTgt 320-350-400++")
    hint = [h for k, h in pushed if k.startswith("tips|hint|")]
    assert len(hint) == 1
    lines = hint[0].split("\n")
    assert "· stop 202.5 (provisional −25 %) · the channel's SL replaces it ·" in lines[1]
    assert sum("plan:" in x for x in lines) == 1 and not any("provisional stop" in x for x in lines)
    assert lines[-2] == "reply <b>take 1</b> · <b>take 1 2</b> · <b>skip 1</b> · <b>stop 1 N</b> · size not stated"
    assert lines[-1] == "auto: off"
    c.tips.kite_default_stop = 0
    ing(c, w, 2, "09:50", "Buy bank nifty 51000 CE near 100-110 when comes")
    t = [h for k, h in pushed if k == "tips|hint|2"][0]
    assert t.count("no plan:") == 1 and "<b>no plan: stop missing</b></i>" not in t and "<b>no plan: stop missing</b>" in t


def test_auto_the_call_carries_its_state_then_plan_completed_and_armed_is_one_line(pushed):
    c, w, s = auto_setup()
    w.prices[S82400] = 280.0
    r = post(c, w, s, 1, "13:52", "BUY SENSEX 82400 PE ABOVE 306\nSL 250")
    hid = r["hint_id"]
    texts = [h for _, h in pushed]
    assert len(texts) == 1 and texts[0].startswith("💡 <b>Tip #1 · ")
    assert texts[0].split("\n")[-1] == "auto: waits for the plan"              # no separate "not armed · no plan" message
    assert "not armed" in " ".join(x["text"] for x in bk.load(hid)["kite"]["log"])  # still on the card
    pushed.clear()
    post(c, w, s, 2, "13:53", "TGT 380/420")
    assert bk.load(hid)["kite"]["state"] == "armed"
    assert [h for _, h in pushed] == ["🤖 Kite AUTO DRY · #1 · stop 250 · targets 380/420 · armed 1 lot · SL trigger 306 · resting on Kite"]


def test_auto_a_call_armed_at_once(pushed):
    c, w, s = auto_setup()
    w.prices[S82400] = 280.0
    post(c, w, s, 1, "13:52", "BUY SENSEX 82400 PE ABOVE 306\nSL 250\nTGT 380/420\n2 lots")
    texts = [h for _, h in pushed]
    assert len(texts) == 1 and texts[0].split("\n")[-1] == "auto DRY: armed 2 lots · SL trigger 306 · resting on Kite"


# --- T5: "add N" ----------------------------------------------------------------------------------------------------------------

class St:
    def __init__(self, config, client):
        self.config = config
        self.client = client
        self.token_source = "db"


def test_the_add_line_offers_reply_add_and_add_n_places_it(pushed):
    c, _, s = auto_setup(kite_max_lots=4, kite_follow_adds=True)
    c.tips.kite_mode = "dry"
    c.alerts = AlertsConfig(chat_id="42", intraday=False, filings=False)
    w = xworld(**{S83300: 700.0})
    call = SX23.replace("\n\nAdd 2 lots near 660 if comes after activation", "")
    hid = post(c, w, s, 1, "12:50", call)["hint_id"]
    ko.place_entry(c, s, hid, 2, by="click", now=t19("12:51"), notify=False)
    tick(c, w, {S83300: 716.0}, "12:55")
    assert bk.load(hid)["kite"]["state"] == "entered"
    post(c, w, s, 2, "12:56", "Add 2 lots near 660")
    assert any(h.endswith(f"Place add… on the card (your click) · reply add {hid}") for _, h in pushed)
    now = t19("12:57")
    fake = BotTG([update(1, f"add {hid}", at=now)])
    b, n = BotCommands(St(c, w), clock=lambda: now), Notifier(c.alerts, fake)
    assert b.poll_once(n) == 1
    t = fake.sent[-1][1]
    assert t.startswith(f"📏 #{hid} · add armed · BUY LIMIT 660 ×40 · 2 lots") and bk.load(hid)["kite"]["add"]["state"] == "armed"
    fake2 = BotTG([update(2, f"add {hid}", at=now)])
    assert BotCommands(St(c, w), clock=lambda: now).poll_once(Notifier(c.alerts, fake2)) == 1
    assert fake2.sent[-1][1].startswith("Not changed: add not armed: one add per call")


# --- T7: the evening review's grades line ---------------------------------------------------------------------------------------

def test_the_grades_line_lists_the_taken_calls_then_counts_the_skipped(monkeypatch):
    from backend.tips import ai4
    c, w = cfg(), world()
    ids = [seed(c, w, BN12.replace("250-270", f"250-{270 + 3 * i}"), "09:43", 300 + i)["hint_id"] for i in range(7)]
    h = bk.load(ids[0])
    k = ko.kstate(h)
    k.update(state="closed", entry_px=262.0, realised=500.0, exit_by="target")
    ko.save_kite(h)
    grades = dict(zip(ids, "ABBBDDB"))
    monkeypatch.setattr(ai4, "reviewable", lambda h, day: True)
    monkeypatch.setattr(ai4, "latest", lambda kind, hid: {"input_json": {"grade": grades[hid]}} if kind == "grade" else None)
    monkeypatch.setattr(ai4, "rows", lambda *a, **kw: [])
    m = ai4.review_message(at("09:43").date().isoformat(), at("18:35"))
    assert m.split("\n")[-1] == f"grades today: #{ids[0]} A · 6 skipped (grades B×4 · D×2)"
