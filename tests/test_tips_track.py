"""💡 Tracking and the record: NFO / BFO / MCX instruments (CRUDE), the expiry (the post's line, else the nearest
listed, else computed with no instrument), the 5-second poll writing tips_quotes, "log in" without a market-data
login, the record maths (T1 first / stop first / no fill / avg R / by hour / by instrument / untracked), thinning."""

from datetime import date

from backend.database.models import get_db
from backend.tips import book as bk
from backend.tips import decode as dc
from backend.tips import track as tr
from tests.tips_world import BN12, BN13, DAY, KEY12, KEY13, SPOT_BN, at, cfg, seed, world


def test_mcx_instruments_load_for_crude():
    w = world()
    r = tr.refresh_instruments(w, "MCX", {"CRUDE"}, DAY)
    assert r["rows"] == 2 and not r["skipped"]
    assert tr.refresh_instruments(w, "MCX", {"CRUDE"}, DAY)["skipped"]               # once a day
    h = dc.decode("Buy CRUDE 5300 PE NEAR 267 AND 263\n\nSL 250\nTGT 275-285-300", DAY)
    tr.resolve(h, None, DAY)
    assert h["exchange"] == "MCX" and h["tradingsymbol"] == "CRUDEOIL26DEC5300PE" and h["lot_size"] == 100
    assert tr.spot_key(h) == "MCX:CRUDEOIL26DECFUT"
    assert [c for c in w.calls if c[0] == "instruments"] == [("instruments", "MCX")]


def test_expiry_explicit_nearest_and_computed():
    w = world()
    tr.refresh_instruments(w, "NFO", {"NIFTY", "BANKNIFTY"}, DAY)
    h = dc.decode("Buy nifty 24150 PE at 190\nSL 170\nTgt 210", DAY)
    tr.resolve(h, None, DAY)
    assert h["expiry"] == "2026-11-24" and h["expiry_src"] == "listed"
    h2 = dc.decode("For 1st December expiry\nBuy nifty 24150 PE at 190\nSL 170\nTgt 210", DAY)
    tr.resolve(h2, None, DAY)
    assert h2["expiry"] == "2026-12-01" and h2["tradingsymbol"] == "NIFTY26DEC24150PE"
    h3 = dc.decode("Buy FINNIFTY 23200 CE at 300\nSL 280\nTgt 330", DAY)
    tr.resolve(h3, None, DAY)
    assert h3["tradingsymbol"] is None and h3["untracked_why"] == "no instrument" and h3["expiry_src"] == "computed"
    assert h3["expiry"] == "2026-11-23"                    # the last Tuesday of November (24th) is a holiday: the day before
    assert tr.computed_expiry("NIFTY", "option", date(2026, 11, 18)) == date(2026, 11, 23)        # Tue 24th: holiday
    assert tr.computed_expiry("SENSEX", "option", DAY) == date(2026, 11, 19)
    assert tr.computed_expiry("CRUDE", "commodity", date(2026, 11, 18)) == date(2026, 12, 17)


def test_poll_writes_quotes_and_marks_log_in_without_a_login():
    c, w = cfg(), world(**{KEY12: 280.0, SPOT_BN: 50200.0})
    hid = seed(c, w)["hint_id"]
    res = tr.poll(c, w, at("09:44"))
    assert res["quoted"] == 1
    with get_db() as conn:
        q = conn.execute("SELECT ltp, spot FROM tips_quotes WHERE hint_id = ?", (hid,)).fetchall()
    assert [tuple(r) for r in q] == [(280.0, 50200.0)]
    quote_calls = [k for k in w.calls if k[0] == "quote"]
    assert len(quote_calls) == 1 and set(quote_calls[0][1]) == {KEY12, SPOT_BN}
    w.has_token = False
    tr.poll(c, w, at("09:45"))
    h = bk.load(hid)
    assert h["tracked"] is False and h["untracked_why"] == "log in"
    assert tr.hint_view(h, at("09:45"))["untracked_why"] == "log in"


def test_no_quotes_outside_the_session():
    c, w = cfg(), world(**{KEY12: 280.0})
    seed(c, w)
    tr.poll(c, w, at("16:00"))
    assert not [k for k in w.calls if k[0] == "quote"]


def test_record_maths_and_thinning():
    c, w = cfg(), world(**{KEY12: 262.0, KEY13: 46.0})
    a = seed(c, w, BN12, "09:43", 1)["hint_id"]                            # fills 262, T1 first
    b = seed(c, w, BN13, "09:46", 2)["hint_id"]                            # never above 50: no fill
    s = seed(c, w, BN12, "10:15", 3)["hint_id"]                            # fills 262, stop first
    u = seed(c, w, "Buy FINNIFTY 23200 CE at 300\nSL 280\nTgt 330", "11:30", 4)["hint_id"]   # untracked: no instrument
    for sec in range(0, 30, 5):
        tr.poll(c, w, at("10:16", sec=sec))
    w.prices[KEY12] = 321.0
    tr.poll(c, w, at("10:30"))
    h = bk.load(s)
    h["run"]["stop"] = 330.0                                               # force s to stop out on the next quote
    bk.save(h)
    w.prices[KEY12] = 325.0
    tr.poll(c, w, at("10:31"))
    w.prices[KEY12] = 360.0
    tr.poll(c, w, at("10:40"))
    tr.poll(c, w, at("15:14"))
    ha, hb, hs = bk.load(a), bk.load(b), bk.load(s)
    assert ha["score"]["first"] == "t1" and ha["ended"] == "t2"
    assert hb["score"]["first"] == "no_fill" and hb["ended"] == "no_fill"
    assert hs["ended"] == "trail"
    rec = tr.record(30, None, at("16:00"))
    t = rec["totals"]
    assert (t["hints"], t["t1_first"], t["no_fill"], t["open"]) == (3, 2, 1, 0)
    assert rec["untracked"] == {"count": 1, "why": {"no instrument": 1}}
    assert [r["label"] for r in rec["by_instrument"]] == ["Index options · buy"]
    assert {r["label"]: r["hints"] for r in rec["by_hour"]} == {"9–10 AM posts": 2, "10–11 AM posts": 1}
    ra = ha["score"]["r"]
    assert ra == round(((321 - 262) * 0.5 + (360 - 262) * 0.5) / 72, 2)
    assert t["avg_r"] == round((ra + hs["score"]["r"]) / 2, 2)
    assert rec["you_vs_all"]["took"] == 0 and rec["you_vs_all"]["all"] == t["pnl_lot"]
    with get_db() as conn:                                                 # a's 5-second quotes: one per minute after it closed
        n = conn.execute("SELECT COUNT(*) FROM tips_quotes WHERE hint_id = ? AND t LIKE '2026-11-17T10:16%'", (a,)).fetchone()[0]
    assert n == 1
    assert u


def test_summary_shape():
    c, w = cfg(), world(**{KEY12: 280.0})
    seed(c, w)
    s = tr.summary(c, at("09:50"))
    assert set(s) >= {"enabled", "reader", "channels", "cards", "hints", "feed", "record", "book", "settings", "any_open", "at"}
    assert set(s["cards"]) == {"open", "book", "channel30", "you_vs_all", "kite"}
    assert s["reader"]["state"] == "logged_out" and s["any_open"] and s["hints"][0]["actions"][:2] == ["take", "take_half"]
    assert s["feed"][0]["label"] == "hint #1"
