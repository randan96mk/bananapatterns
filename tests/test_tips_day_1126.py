"""📅 26 Nov replayed end to end (the model faked to say "chatter" for everything it is asked): every post of the day
through reader.ingest, then what each one became. Reader fixes A1–A6: strong follow-ups by code, avoid pauses every
not-taken state, a repeated call is the same call, a bare target list, the linker, size + add in one post."""

import pytest

from backend.tips import book as bk
from backend.tips import filter as flt
from backend.tips import reader as rd
from tests.tips_world import at, cfg, world

CH = "index options buying with aakanksha"
# (time, text, the post it replies to): the day's posts in order, post N = the N-th entry; the 10:59 call is post 27
P = [
 ("09:21", "Buy sensex 83500 Ce above 750", None),
 ("09:23", "2 lots\n\nAdd 2 lots near 700 also after activation \n\nSl 640\n\nTgt 820/860/900", 1),
 ("09:24", "Not active avoid", 1),
 ("09:29", "Buy sensex 83500 CE above 665\n\n2 lots", None),
 ("09:30", "Sl 530", None), ("09:30", "Tgt 830/880/940", None), ("09:30", "Tgt 730/780/840", None),
 ("09:32", "Tgt 1 done ✅✅✅", None), ("09:32", "Tgt 2 done ✅✅✅", None), ("09:32", "Hows that", None),
 ("09:33", "Century done", None), ("09:33", "subah subah breakfast done... kaisa laga", None),
 ("09:38", "Target 1 and 2 done ✅✅", None),
 ("09:41", "📊 SENSEX - SUPPORT & RESISTANCE ZONE\n\n\n🟢 SUPPORT ZONE\n• 83,700 – 83,800-83,900\n\n🔴 RESISTANCE ZONE\n• 84,200 – 84,300-84,500", None),
 ("09:44", "83300 ce sensex in watch list", None),
 ("10:07", "Sensex made low at 83760\n\nNow at 84170\n\nBlasted from support", None),
 ("10:13", "Waiting for opportunity", None),
 ("10:45", "Buy sensex 84800 pe above 475", None), ("10:46", "Sl 415", None), ("10:46", "2 lots", None), ("10:46", "Tgt 525/560/600", None),
 ("10:49", "Not active avoid", None), ("10:49", "85000 pe in watch list", None),
 ("10:53", "Buy sensex 85000 pe above 561", None), ("10:53", "2 lots", None), ("10:54", "Sl 490\n\nTgt 650/700/750/800", None),
 ("10:59", "Buy 85000 pe near 520/30", None), ("10:59", "2 lots", 27), ("10:59", "Sl 475\n\nTgt 565/600/635/700", None),
 ("11:00", "Cmp 527", 27), ("11:00", "Avoid this", 24), ("11:00", "Tgt 1 done ✅✅✅", None),
 ("11:01", "527 to 585*+\n\nDEFENSIVE TRADER: BOOK FULL PROFIT NOW\n \nAGGRESSIVE TRADER: BOOK PART & FOR REST QTY KEEP TSL AT NPNL (NO PROFIT NO LOSS)", None),
 ("11:08", "TGT 2 DONE ✅✅✅", None), ("11:09", "635\n\nTGT 3 DONE ✅✅✅", None), ("11:09", "CENTURY DONE 🚀🚀🚀", None),
 ("11:17", "530 TO NOW 665++\n\nWHAT A MOVE", None), ("11:20", "tgt 4 done ✅✅", None), ("11:20", "all tgt done & dusted 💥💥💥", None),
 ("11:20", "hows the josh ???", None),
 ("11:24", "buy nifty 25900 ce above 226++\n\n2 lots", None), ("11:24", "sl 200", None), ("11:25", "tgt 247/257/275", None),
 ("11:28", "not active , avoid", None), ("11:28", "wait for next level", None),
 ("11:31", "buy nifty 25900 ce above 216++\n\nSL 196\n\nTGT 240/250/260\n\n2 lots", None),
 ("11:33", "wait for level to activate", None), ("11:34", "not activated again , lets avoid", None),
 ("11:35", "ye dekha kya??\n\n530 se 800++ ho gaya", None),
 ("11:41", "buy sensex 83800 ce above 553++\n\n2 lots", None), ("11:41", "sl 470", None), ("11:42", "625/660/700/750/800", None),
 ("11:42", "buy sensex 83800 ce above 553++\n\n2 lots\n\nsl 470\n\nTGT 625/660/700/750/800", None),
 ("11:45", "chance hi nhi de raha market \n\navoid again", None), ("11:45", "waiting for right level", None), ("11:46", "not activated , avoid", None),
 ("11:56", "sudden fat gaya.. did not get chance", None), ("11:59", "553 to 750\nOMG", None),
 ("12:16", "Defensive Style:\nSENSEX 83500 CE 3560 Profit booked in demo account with margin used 26640\n\nTotal 2 lots bought at 666 then 1 lot booked at 730 and another 1 lot booked at 780.", None),
 ("13:04", "be ready", None),
 ("13:43", "Buy sensex 83500 ce above 590\n2 lots\nSl 510\ntgt 660/700", None),
 ("13:44", "590+", None), ("13:44", "600", None), ("13:44", "640", None),
 ("13:45", "DEFENSIVE TRADER: BOOK PART & KEEP TSL AT COST FOR REST QTY \n \nAGGRESSIVE TRADER: BOOK PART & FOR REST QTY KEEP TSL AT NPNL (NO PROFIT NO LOSS)", None),
 ("13:45", "tgt 1 done ✅✅✅", None),
 ("14:04", "Buy NIFTY 25800 CE above 186\nSL 176\ntgt 195/205/215+", None), ("14:05", "2 lot", None), ("14:08", "active", None),
 ("15:20", "All trades which we are providing are Intraday trades only , please exit them before market close compulsory \n\nDont carry them until we specifically recommend.", None),
 ("15:36", "Today's profit 8430\nROI - 35.20%", None),
]


@pytest.fixture
def day(monkeypatch):
    monkeypatch.setattr(rd, "PUSH_MAX_AGE_S", 10 ** 9)
    monkeypatch.setattr(flt, "model_allowed", lambda config: True)
    monkeypatch.setattr(flt, "model_classify", lambda config, text, hints: {"kind": "chatter", "hint_id": None, "model": "fake", "ms": 1})
    c, w = cfg(), world()
    c.tips.kite_default_stop = 25
    out = {}
    for i, (t, txt, rt) in enumerate(P, 1):
        out[i] = rd.ingest(c, CH, i, at(t), txt, False, push=False, model=True, client=w, reply_to=rt)
    hint_of = {r["hint_id"]: i for i, r in out.items() if r["kind"] == "hint"}       # hint id -> the post that made it
    return out, {i: h for h, i in hint_of.items()}


def fu(out, i):
    return out[i]["kind"], out[i]["hint_id"]


def test_the_calls_of_the_day_and_no_second_hint_for_the_repeat(day):
    out, H = day
    assert sorted(H) == [1, 4, 18, 24, 27, 41, 46, 50, 61, 67]            # 10 calls: 11:42's repeat (post 53) is not one
    assert fu(out, 53) == ("follow_up", H[50])
    assert out[53]["line"] == "the call again · stop 470 · targets 625/660/700/750/800 · nothing new"
    assert len(bk.rows(order="id")) == 10


def test_size_and_add_in_one_post_and_avoid_pauses(day):
    out, H = day
    h1 = bk.load(H[1])
    assert fu(out, 2) == ("follow_up", H[1]) and h1["size_hint"] == {"lots": 2}                      # A6: the size AND the add
    assert h1["stop"]["value"] == 640 and h1["targets"] == [820, 860, 900]
    assert out[2]["act"]["action"] == "add" and out[2]["act"]["size_lots"] == 2 and out[2]["act"]["level"] == 700
    assert fu(out, 3) == ("follow_up", H[1]) and h1["status"] == "paused"


def test_strong_follow_ups_by_code_land_on_the_right_call(day):
    out, H = day
    for i in (8, 9, 11, 13):                                                 # results of the 9:29 call
        assert fu(out, i) == ("follow_up", H[4]), i
        assert out[i]["act"]["action"] == "targets"
    assert fu(out, 22) == ("follow_up", H[18]) and bk.load(H[18])["status"] == "paused"
    assert fu(out, 28) == ("follow_up", H[27]) and bk.load(H[27])["size_hint"] == {"lots": 2}         # reply to the 10:59 call
    assert fu(out, 30) == ("follow_up", H[27]) and out[30]["line"] == "entry allowed at the market"   # "Cmp 527"
    assert fu(out, 31) == ("follow_up", H[24]) and bk.load(H[24])["status"] == "paused"              # "Avoid this" (reply)
    for i in (32, 33, 34, 35, 36, 38, 39):                                   # the 10:59 call's run, results and the booking
        assert fu(out, i) == ("follow_up", H[27]), i
    assert out[33]["act"]["action"] == "exit" and out[35]["act"] == {"action": "targets", "n": 3}
    assert fu(out, 44) == ("follow_up", H[41]) and bk.load(H[41])["status"] == "paused"
    assert fu(out, 45) == ("follow_up", H[41]) and out[45]["act"]["action"] == "wait"
    assert out[45]["line"] == "the channel says wait · no action"
    assert fu(out, 47) == ("follow_up", H[46]) and out[47]["act"]["action"] == "wait"
    assert fu(out, 48) == ("follow_up", H[46]) and bk.load(H[46])["status"] == "paused"
    assert [fu(out, i) for i in (54, 55, 56)] == [("follow_up", H[50])] * 3 and bk.load(H[50])["status"] == "paused"
    assert fu(out, 65) == ("follow_up", H[61]) and out[65]["act"]["action"] == "partial"
    assert fu(out, 66) == ("follow_up", H[61])
    assert fu(out, 69) == ("follow_up", H[67]) and out[69]["line"] == "entry allowed at the market"


def test_plan_posts_go_to_the_newest_call_lacking_the_piece(day):
    out, H = day
    assert [fu(out, i) for i in (5, 6, 7)] == [("follow_up", H[4])] * 3
    assert bk.load(H[4])["targets"] == [730, 780, 840] and bk.load(H[4])["stop"]["value"] == 530
    assert fu(out, 26) == ("follow_up", H[24]) and bk.load(H[24])["targets"] == [650, 700, 750, 800]
    assert fu(out, 29) == ("follow_up", H[27])                               # #28's targets never land on #27 (A5)
    assert bk.load(H[27])["targets"] == [565, 600, 635, 700] and bk.load(H[27])["stop"]["value"] == 475
    assert bk.load(H[24])["stop"]["value"] == 490
    assert fu(out, 52) == ("follow_up", H[50]) and out[52]["line"] == "plan completed: targets 625/660/700/750/800"   # A4
    assert fu(out, 20) == ("follow_up", H[18]) and fu(out, 25) == ("follow_up", H[24]) and fu(out, 68) == ("follow_up", H[67])


def test_chatter_stays_chatter(day):
    out, _ = day
    for i in (10, 12, 14, 15, 16, 17, 23, 37, 40, 49, 57, 58, 59, 60, 62, 63, 64, 70, 71):
        assert out[i]["kind"] == "chatter", (i, P[i - 1][1][:40])
