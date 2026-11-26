"""💡 The Tips paper book: fills at quotes with charges like the options book, half at T1 + stop to cost, the time
stop, the channel's exit followed automatically (and you are told), an override stops that, avoid pauses, your own
exit / trail / skip, sizes (post / lots / half / rupees), the 404 / 409 / 400 refusals."""

import pytest

from backend.options import book as obook
from backend.tips import book as bk
from backend.tips import reader as rd
from backend.tips import track as tr
from tests.tips_world import BN12, BN13, KEY12, KEY13, SPOT_BN, at, cfg, seed, world


@pytest.fixture
def pushed(monkeypatch):
    out = []
    monkeypatch.setattr(bk, "push", lambda config, key, html: out.append((key, html)) or True)
    monkeypatch.setattr(rd, "PUSH_MAX_AGE_S", 10 ** 9)
    return out


def test_charges_match_the_options_formula():
    for prem, sell in ((3930.0, False), (5430.0, True), (120000.0, True)):
        assert bk.charges(prem, sell) == obook.charges(prem, sell)


def test_take_fills_at_the_quote_and_books_t1_half_then_t2(pushed):
    c, w = cfg(), world(**{KEY12: 280.0, SPOT_BN: 50250.0})
    hid = seed(c, w)["hint_id"]
    tr.poll(c, w, at("09:44"))
    h = bk.take(c, hid, who="page", now=at("09:45"))
    assert h["pos"]["state"] == "armed" and h["status"] == "waiting"         # 280: above the zone, waits
    w.prices[KEY12] = 262.0
    tr.poll(c, w, at("09:51"))
    h = bk.load(hid)
    assert h["pos"]["state"] == "open" and h["pos"]["entry_px"] == 262.0 and h["status"] == "active"
    assert h["pos"]["charges_in"] == bk.charges(262 * 15, sell=False)
    w.prices[KEY12] = 321.0
    res = tr.poll(c, w, at("10:00"))
    h = bk.load(hid)
    assert bk.open_qty(h["pos"]) == 8 and h["pos"]["run"]["stop"] == 262.0          # 15 qty: 7 booked, 8 ride
    assert any("T1 321 · half booked" in ln and "SL → cost 262" in ln for ln in res["lines"])
    w.prices[KEY12] = 352.0
    tr.poll(c, w, at("10:08"))
    h = bk.load(hid)
    assert h["status"] == "closed" and h["ended"] == "t2" and h["pos"]["state"] == "closed"
    gross = (321 - 262) * 7 + (352 - 262) * 8
    v = bk.view(c)
    assert len(v["trades"]) == 2 and round(sum(t["gross"] for t in v["trades"]), 2) == gross
    assert v["realised"] == round(gross - sum(t["charges"] for t in v["trades"]), 2) and v["won"] == 1
    assert v["equity"] == round(100000 + v["realised"], 2)


def test_channel_exit_is_followed_and_you_are_told(pushed):
    c, w = cfg(), world(**{KEY12: 262.0})
    hid = seed(c, w)["hint_id"]
    bk.take(c, hid, now=at("09:51"), ltp=262.0)
    w.prices[KEY12] = 300.0
    tr.poll(c, w, at("10:30"))
    r = rd.ingest(c, "aakankshatrading", 17620, at("10:31"), "fired up … exit near cost", False, push=True, model=False, client=w)
    assert r["kind"] == "follow_up" and r["hint_id"] == hid and r["line"].startswith("followed: closed at 300")
    h = bk.load(hid)
    assert h["status"] == "closed" and h["ended"] == "channel_exit"
    assert any(k.startswith("tips|fu|") and "auto, as you set" in html for k, html in pushed)


def test_override_stops_the_auto_follow(pushed):
    c, w = cfg(), world(**{KEY12: 262.0})
    hid = seed(c, w)["hint_id"]
    bk.take(c, hid, now=at("09:51"), ltp=262.0)
    bk.trail(c, hid, 250, now=at("10:00"))
    r = rd.ingest(c, "aakankshatrading", 17621, at("10:31"), "exit now", False, push=False, model=False, client=w)
    assert r["line"].startswith("not followed: you overrode")
    assert bk.load(hid)["pos"]["state"] == "open"


def test_avoid_pauses_a_waiting_hint_and_active_releases_it(pushed):
    c, w = cfg(), world(**{KEY13: 46.2})
    hid = seed(c, w, BN13, "09:46", 17606)["hint_id"]
    tr.poll(c, w, at("09:47"))
    rd.ingest(c, "aakankshatrading", 17611, at("10:38"), "Avoid.. wait", False, push=False, model=False, client=w)
    assert bk.load(hid)["status"] == "paused"
    w.prices[KEY13] = 55.0
    tr.poll(c, w, at("10:40"))
    assert bk.load(hid)["status"] == "paused"                                   # no entry while paused
    rd.ingest(c, "aakankshatrading", 17612, at("10:45"), "Active", False, push=False, model=False, client=w)
    tr.poll(c, w, at("10:46"))
    h = bk.load(hid)
    assert h["status"] == "active" and h["entry_px"] == 55.0


def test_sizes_post_lots_half_rupees(pushed):
    c, w = cfg(), world(**{KEY12: 262.0})
    hid = seed(c, w)["hint_id"]
    h = bk.take(c, hid, lots=2, half=True, now=at("09:51"), ltp=262.0)
    assert h["pos"]["lots"] == 2 and h["pos"]["qty"] == 15 and h["pos"]["half"]
    hid2 = seed(c, w, BN12.replace("Sl 190", "Sl 190 · 3 lots"), "09:50", 17630)["hint_id"]
    assert bk.take(c, hid2, now=at("09:51"), ltp=262.0)["pos"]["lots"] == 3          # the post's size
    hid3 = seed(c, w, BN12, "09:52", 17631)["hint_id"]
    assert bk.take(c, hid3, rupees=8000, now=at("09:53"), ltp=262.0)["pos"]["lots"] == 2   # 8000 // (262 x 15)


def test_refusals(pushed):
    c, w = cfg(), world(**{KEY12: 262.0})
    c.tips.kite_default_stop = 0                        # no provisional stop: today's "no plan: stop missing"
    with pytest.raises(bk.TipsError) as e:
        bk.take(c, 999)
    assert e.value.code == 404
    hid = seed(c, w)["hint_id"]
    bk.take(c, hid, now=at("09:51"), ltp=262.0)
    with pytest.raises(bk.TipsError) as e:
        bk.take(c, hid)
    assert e.value.code == 409
    with pytest.raises(bk.TipsError) as e:
        bk.skip(c, hid)
    assert e.value.code == 409
    with pytest.raises(bk.TipsError) as e:
        bk.trail(c, hid, 300)                                                     # above the price for a buy
    assert e.value.code == 400
    nostop = seed(c, w, "Buy bank nifty 50300 PE at 260 add more at 240", "09:55", 17640)["hint_id"]
    with pytest.raises(bk.TipsError) as e:
        bk.take(c, nostop)
    assert e.value.code == 400 and "stop missing" in str(e.value)
    with pytest.raises(bk.TipsError) as e:
        bk.take(c, hid, lots=0.5)
    assert e.value.code == 409


def test_owner_exit_skip_and_cancel(pushed):
    c, w = cfg(), world(**{KEY12: 262.0})
    hid = seed(c, w)["hint_id"]
    bk.take(c, hid, now=at("09:51"), ltp=262.0)
    w.prices[KEY12] = 250.0
    tr.poll(c, w, at("10:00"))
    h = bk.exit_now(c, hid, now=at("10:01"))
    assert h["ended"] == "owner_exit" and h["pos"]["exit_px"] == 250.0 and h["pnl"] < 0
    hid2 = seed(c, w, BN12, "10:05", 17650)["hint_id"]
    h2 = bk.skip(c, hid2, now=at("10:06"))
    assert h2["status"] == "skipped" and h2["events"][-1]["src"] == "owner"
    w.prices[KEY12] = 300.0
    hid3 = seed(c, w, BN12, "10:10", 17651)["hint_id"]
    bk.take(c, hid3, now=at("10:11"), ltp=300.0)                                  # armed: the zone has not come
    h3 = bk.exit_now(c, hid3, now=at("10:12"))
    assert h3["pos"]["state"] == "closed" and h3["pos"]["entry_px"] is None


def test_the_owner_sets_the_index_of_a_nameless_post(monkeypatch):
    from backend.tips import reader as rd
    c, w = cfg(), world()
    r = rd.ingest(c, "aakankshatrading", 9901, at("09:32"), "buy 82400 pe near 680-700 cmp\n2 lots", False, push=False, model=False, client=w)
    h = bk.load(r["hint_id"])
    assert h["symbol"] == "SENSEX" and h["decoded"]["symbol"] == "strike"
    assert any("read from the strike level" in e["text"] for e in h["events"])
    h = bk.set_symbol(c, h["id"], "bankex")
    assert h["symbol"] == "BANKEX" and h["decoded"]["symbol"] == "owner" and h["exchange"] == "BFO"
    assert any("you set the symbol: SENSEX → BANKEX" in e["text"] for e in h["events"])
    with pytest.raises(bk.TipsError):
        bk.set_symbol(c, h["id"], "GOLD")
