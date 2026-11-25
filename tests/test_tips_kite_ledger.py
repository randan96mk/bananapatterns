"""⚡ The Kite ledger (tips_kite_trades: one append-only row per closed Kite hint, real or dry) and 🎯 the T3 runner
(kite_t2_part half / one with a 3rd target: T2 sells a part, the stop moves to T1, T3 sells the rest)."""

import json

import pytest

from backend.database.models import get_db
from backend.tips import book as bk
from backend.tips import kite_book as kb
from backend.tips import kite_orders as ko
from backend.tips import track as tr
from tests.test_tips_api import api  # noqa: F401  (the fixture)
from tests.test_tips_kite import Broker
from tests.test_tips_kite_auto import CALL1, S82200, auto_setup, post, t19, tick
from tests.tips_world import BN12, KEY12, at, cfg, seed, world


@pytest.fixture
def pushed(monkeypatch):
    out = []
    monkeypatch.setattr(bk, "push", lambda config, key, html: out.append(html) or True)
    from backend.tips import reader as rd
    monkeypatch.setattr(rd, "PUSH_MAX_AGE_S", 10 ** 9)
    return out


def setup(text=BN12, mode="click", **kw):
    c = cfg()
    c.tips.kite_mode = mode
    c.tips.kite_max_lots = 3
    for k, v in kw.items():
        setattr(c.tips, k, v)
    b = Broker(world(**{KEY12: 262.0}))
    hid = seed(c, b, text)["hint_id"]
    return c, b, hid


def entered(c, b, hid, lots):
    ko.place_entry(c, b, hid, lots, now=at("10:00"), auto_rest=True)
    b.fill(b.last(transaction_type="BUY")["order_id"], 262.0)
    return ko.sync(c, b, at("10:00", sec=10))


def hit(c, b, px, hhmm, fill=None):
    """The LTP to `px`, one watch tick, the market exit filled at `fill` (default px), one sync. The sync's lines."""
    b.prices[KEY12] = px
    tr.poll(c, b, at(hhmm))
    n = len(b.placed)
    ko.watch_all(c, b, at(hhmm))
    assert len(b.placed) > n, "no market exit sent"
    m = b.placed[-1]
    assert m["order_type"] == "MARKET"
    b.fill(m["order_id"], fill if fill is not None else px)
    return m, ko.sync(c, b, at(hhmm, sec=10))["lines"]


def ledger_rows(book=None):
    with get_db() as conn:
        q = "SELECT * FROM tips_kite_trades" + (" WHERE book = ?" if book else "") + " ORDER BY id"
        return [dict(r) for r in conn.execute(q, (book,) if book else ())]


def legs(c, hid):
    return {x["leg"]: x for x in tr.hint_view(bk.load(hid), config=c)["kite"]["legs"]}


# --- 🎯 the T3 runner -----------------------------------------------------------------------------------------------------

def test_runner_one_lot_at_each_target_stop_to_t1_and_target_3(pushed):
    c, b, hid = setup(kite_t2_part="one", kite_book_part="one")
    entered(c, b, hid, 3)
    v = legs(c, hid)
    assert v["t1"]["qty"] == 15 and v["t2"]["qty"] == 15 and v["t3"]["qty"] == 15 and v["t3"]["runner"] is True
    assert v["t3"]["name"] == "Target 3" and v["t3"]["sub"].startswith("the last lot · stop at T1 320 · the channel's exit")
    assert "time stop 3:14 PM" in v["t3"]["sub"]
    m1, lines = hit(c, b, 321.0, "10:20")
    assert m1["quantity"] == 15 and "sold 15 of 45" in lines[0] and "stop → cost 262 for the rest" in lines[0]
    m2, lines = hit(c, b, 351.0, "10:40")
    assert m2["quantity"] == 15
    assert lines[0] == "⚡ Kite · #1 T2 hit 351 · sold 15 of 30 at market · stop → T1 320 for the rest · watching T3 400"
    sl = b.placed[-1]
    assert (sl["order_type"], sl["trigger_price"], sl["quantity"]) == ("SL", 320.0, 15)
    k = bk.load(hid)["kite"]
    assert k["state"] == "entered" and k["qty_open"] == 15 and k["stop_px"] == 320.0
    v = legs(c, hid)
    assert v["stop"]["moved_text"] == "moved cost 262 → T1 320 after T2" and v["t2"]["status_text"] == "hit · sold 351"
    assert any("T2 hit 351 · sold 15 of 30" in x["text"] for x in k["log"])
    m3, lines = hit(c, b, 402.0, "11:00")
    assert m3["quantity"] == 15 and sl["order_id"] in b.cancelled
    k = bk.load(hid)["kite"]
    assert k["state"] == "closed" and k["reason"] == "target 3" and k["exit_by"] == "target" and k["qty_open"] == 0
    assert legs(c, hid)["t3"]["status_text"] == "hit · sold 402"
    rows = ledger_rows()
    assert len(rows) == 1 and rows[0]["book"] == "kite" and rows[0]["why"] == "target 3"
    assert [e["leg"] for e in json.loads(rows[0]["exits"])] == ["t1", "t2", "t3"]


def test_runner_half_with_four_lots(pushed):
    c, b, hid = setup(kite_t2_part="half", kite_book_part="one", kite_max_lots=4)
    c.tips.kite_risk_per_trade = 99999.0
    entered(c, b, hid, 4)
    hit(c, b, 321.0, "10:20")                                          # T1: one lot, 3 open
    m2, _ = hit(c, b, 351.0, "10:40")
    assert m2["quantity"] == 15                                        # floor(3 ÷ 2) = 1 lot
    m3, _ = hit(c, b, 401.0, "11:00")
    assert m3["quantity"] == 30 and bk.load(hid)["kite"]["reason"] == "target 3"


def test_all_is_todays_rule(pushed):
    c, b, hid = setup()                                               # kite_t2_part all (default)
    assert c.tips.kite_t2_part == "all"
    entered(c, b, hid, 3)
    assert "t3" not in legs(c, hid)
    hit(c, b, 321.0, "10:20")
    m2, lines = hit(c, b, 351.0, "10:40")
    assert m2["quantity"] == 30 and bk.load(hid)["kite"]["reason"] == "target 2" and lines[0].endswith("total")


def test_two_targets_ignore_the_setting(pushed):
    c, b, hid = setup(BN12.replace("320-350-400++", "320-350"), kite_t2_part="one")
    entered(c, b, hid, 3)
    assert "t3" not in legs(c, hid)
    hit(c, b, 321.0, "10:20")
    m2, _ = hit(c, b, 351.0, "10:40")
    assert m2["quantity"] == 30 and bk.load(hid)["kite"]["state"] == "closed"


def test_one_open_lot_at_t2_closes_all(pushed):
    c, b, hid = setup(kite_t2_part="one")
    entered(c, b, hid, 2)
    assert "t3" not in legs(c, hid)
    hit(c, b, 321.0, "10:20")
    m2, _ = hit(c, b, 351.0, "10:40")
    assert m2["quantity"] == 15 and bk.load(hid)["kite"]["reason"] == "target 2"


def _runner(c, b, hid):
    entered(c, b, hid, 3)
    hit(c, b, 321.0, "10:20")
    hit(c, b, 351.0, "10:40")
    k = bk.load(hid)["kite"]
    assert k["state"] == "entered" and k["qty_open"] == 15 and k["stop_px"] == 320.0


def test_kill_switch_closes_the_runner(pushed):
    c, b, hid = setup(kite_t2_part="one", kite_book_part="one")
    _runner(c, b, hid)
    out = ko.set_kill(c, b, True, at("10:50"))
    ex = b.placed[-1]
    assert out["exits"] == 1 and (ex["order_type"], ex["quantity"]) == ("MARKET", 15)
    b.fill(ex["order_id"], 360.0)
    ko.sync(c, b, at("10:50", sec=10))
    assert bk.load(hid)["kite"]["state"] == "closed"
    ko.set_kill(c, b, False, at("10:51"))


def test_exit_now_closes_the_runner(pushed):
    c, b, hid = setup(kite_t2_part="one", kite_book_part="one")
    _runner(c, b, hid)
    ko.exit_now(c, b, hid, now=at("10:50"))
    ex = b.placed[-1]
    assert (ex["order_type"], ex["quantity"]) == ("MARKET", 15)
    b.fill(ex["order_id"], 360.0)
    ko.sync(c, b, at("10:50", sec=10))
    k = bk.load(hid)["kite"]
    assert k["state"] == "closed" and k["reason"] == "your exit"


def _auto_runner(**kw):
    c, w, s = auto_setup(kite_max_lots=3, kite_t2_part="one", kite_book_part="one", kite_risk_per_trade=99999.0, **kw)
    w.prices.update({S82200: 538.0})
    hid = post(c, w, s, 1, "12:50", CALL1 + "\n3 lots")["hint_id"]
    tick(c, w, {S82200: 538.0}, "12:51")
    assert bk.load(hid)["kite"]["qty_open"] == 60
    tick(c, w, {S82200: 601.0}, "12:52")
    tick(c, w, {S82200: 651.0}, "12:53")
    k = bk.load(hid)["kite"]
    assert k["state"] == "entered" and k["qty_open"] == 20 and k["stop_px"] == 600.0 and k["legs"]["t2"]["status"] == "complete"
    return c, w, s, hid


def test_channel_exit_closes_the_runner(pushed):
    c, w, s, hid = _auto_runner()
    from backend.tips import reader as rd
    r = rd.ingest(c, "index options buying with aakanksha", 2, t19("12:55"), "exit", False, source="live", push=True, model=False,
                  client=s, reply_to=1)
    assert r["kind"] == "follow_up" and r["hint_id"] == hid
    tick(c, w, {S82200: 640.0}, "12:56")
    k = bk.load(hid)["kite"]
    assert k["state"] == "closed" and k["qty_open"] == 0
    rows = ledger_rows("dry")
    assert len(rows) == 1 and rows[0]["hint_id"] == hid and not ledger_rows("kite")


def test_time_stop_closes_the_runner(pushed):
    c, w, s, hid = _auto_runner()
    tick(c, w, {S82200: 640.0}, "15:14")
    k = bk.load(hid)["kite"]
    assert k["state"] == "closed" and k["reason"].startswith("time stop")


def test_the_runner_in_the_dry_run_fills_t3(pushed):
    c, w, s, hid = _auto_runner()
    tick(c, w, {S82200: 701.0}, "13:30")
    k = bk.load(hid)["kite"]
    assert k["state"] == "closed" and k["reason"] == "target 3" and k["legs"]["t3"]["filled_px"] == 701.0


def test_t2_part_setting_is_validated():
    c = cfg()
    with pytest.raises(ValueError, match="kite_t2_part"):
        c.update_section("tips", {"kite_t2_part": "third"})
    c.update_section("tips", {"kite_t2_part": "one"})
    assert c.tips.kite_t2_part == "one"


def test_the_leg_check_allows_t3_and_the_migration_keeps_rows(tmp_path):
    import sqlite3

    from backend.database import models
    p = tmp_path / "old.db"
    conn = sqlite3.connect(str(p))
    old = models.SCHEMA.split("CREATE TABLE IF NOT EXISTS tips_orders (", 1)[1].split(");", 1)[0].replace("'t3', ", "")
    conn.execute("CREATE TABLE tips_orders (" + old + ")")
    conn.execute("ALTER TABLE tips_orders ADD COLUMN tradingsymbol TEXT")
    conn.execute("INSERT INTO tips_orders (hint_id, leg, status, tradingsymbol) VALUES (1, 'entry', 'complete', 'X')")
    conn.commit()
    conn.close()
    models.init_db(str(p))
    conn = sqlite3.connect(str(p))
    assert "'t3'" in conn.execute("SELECT sql FROM sqlite_master WHERE name = 'tips_orders'").fetchone()[0]
    assert conn.execute("SELECT hint_id, leg, tradingsymbol FROM tips_orders").fetchall() == [(1, "entry", "X")]
    conn.execute("INSERT INTO tips_orders (hint_id, leg, status) VALUES (1, 't3', 'placed')")
    conn.close()


# --- ⚡ the ledger ----------------------------------------------------------------------------------------------------------

def test_close_writes_one_row_and_a_resync_adds_none(pushed):
    c, b, hid = setup()
    entered(c, b, hid, 1)
    hit(c, b, 330.0, "10:20", fill=329.0)
    rows = ledger_rows()
    assert len(rows) == 1
    r = rows[0]
    assert (r["hint_id"], r["book"], r["qty"], r["lots"], r["entry_px"], r["exit_px"], r["why"], r["by"]) == \
        (hid, "kite", 15, 1.0, 262.0, 329.0, "target 1", "click")
    assert r["gross"] == 1005.0
    want = bk.charges(262.0 * 15, False) + bk.charges(329.0 * 15, True)
    assert r["charges"] == round(want, 2) and r["net"] == round(1005.0 - want, 2)
    assert json.loads(r["exits"])[0]["leg"] == "t1" and r["day"] == "2026-11-17" and r["closed_at"]
    h = bk.load(hid)
    assert ko.ledger(c, h, at("10:30")) is None and kb.record(c, h, at("10:30")) is False      # re-close: ignored
    ko.sync_all(c, b, at("10:31"))
    assert kb.backfill(c) == [] and len(ledger_rows()) == 1


def _closed_hint(c, b, dry=False):
    """A hint closed on Kite before the ledger existed: its kite JSON + its order rows, no ledger row."""
    hid = seed(c, b, BN12, "09:50", 900 + int(dry))["hint_id"]
    h = bk.load(hid)
    k = ko.kstate(h)
    k.update(state="closed", lots=2, qty=30, qty_open=0, entry_px=262.0, realised=1350.0, reason="target 2", by="click",
             day="2026-11-17", entry_at=at("10:00").isoformat(), closed_at=at("11:00").isoformat(), exit_px=307.0, dry=dry)
    ko.save_kite(h)
    by = "dry" if dry else "click"
    oid = (lambda n: f"DRY-{n}") if dry else (lambda n: f"77{n}")
    base = {"hint_id": hid, "status": "complete", "day": "2026-11-17", "tradingsymbol": h.get("tradingsymbol"), "by": by}
    ko._insert({**base, "leg": "entry", "side": "BUY", "qty": 30, "order_type": "MARKET", "order_id": oid(1), "filled_px": 262.0,
                "filled_qty": 30, "placed_at": at("10:00").isoformat(), "updated_at": at("10:00").isoformat()})
    ko._insert({**base, "leg": "t1", "side": "SELL", "qty": 15, "order_type": "MARKET", "order_id": oid(2), "filled_px": 320.0,
                "filled_qty": 15, "placed_at": at("10:30").isoformat(), "updated_at": at("10:30").isoformat(), "by": "app" if not dry else "dry"})
    ko._insert({**base, "leg": "t2", "side": "SELL", "qty": 15, "order_type": "MARKET", "order_id": oid(3), "filled_px": 294.0,
                "filled_qty": 15, "placed_at": at("11:00").isoformat(), "updated_at": at("11:00").isoformat(), "by": "app" if not dry else "dry"})
    return hid


def test_backfill_from_a_prepared_closed_hint_and_the_view():
    c = cfg()
    b = world()
    hid = _closed_hint(c, b)
    hdry = _closed_hint(c, b, dry=True)
    assert ledger_rows() == []
    assert sorted(kb.backfill(c)) == sorted([hid, hdry])
    assert kb.backfill(c) == []                                                         # idempotent
    real, dry = ledger_rows("kite"), ledger_rows("dry")
    assert len(real) == 1 and len(dry) == 1 and real[0]["hint_id"] == hid and dry[0]["hint_id"] == hdry
    r = real[0]
    want = bk.charges(262.0 * 30, False) + bk.charges(320.0 * 15, True) + bk.charges(294.0 * 15, True)
    assert r["gross"] == 1350.0 and r["charges"] == round(want, 2) and r["net"] == round(1350.0 - want, 2)
    assert [e["leg"] for e in json.loads(r["exits"])] == ["t1", "t2"] and r["opened_at"] == at("10:00").isoformat()
    v = kb.view(c, now=at("16:00"))
    assert v["book"] == "kite" and v["taken"] == 1 and v["won"] == 1 and v["since"] == "2026-11-17"
    assert v["today"] == r["net"] and v["totals"] == {"gross": 1350.0, "charges": r["charges"], "net": r["net"], "n": 1, "won": 1}
    t = v["trades"][0]
    assert t["title"] == "BUY BANKNIFTY 50300 PE" and t["entry_exit"] == "262 → 320 / 294" and t["when_label"] == "Tue 17 Nov 11:00 AM"
    assert t["ended"] == "T2"
    c.tips.kite_mode = "dry"
    vd = kb.view(c, now=at("16:00"))
    assert vd["book"] == "dry" and vd["dry"] and vd["taken"] == 1 and vd["trades"][0]["hint_id"] == hdry
    assert kb.card(c, vd)["mode"] == "dry"
    c.tips.kite_mode = "off"
    assert kb.card(c, now=at("16:00"))["mode"] == "off"
    days = kb.days_view("kite")
    assert days == [{"day": "2026-11-17", "n": 1, "net": r["net"], "gross": 1350.0, "charges": r["charges"], "won": 1, "label": "Tue 17 Nov"}]


def test_you_vs_all_adds_the_real_net_never_the_dry_run():
    c = cfg()
    b = world()
    hid = _closed_hint(c, b)
    hdry = _closed_hint(c, b, dry=True)
    kb.backfill(c)
    net = ledger_rows("kite")[0]["net"]
    y = tr.record(30, None, at("16:00"))
    yv = y["you_vs_all"]
    assert yv["on_kite"] == 1 and yv["paper"] == 0 and yv["took"] == 1 and yv["you"] == net and yv["kite_net"] == net
    assert y["kite_days"][0]["n"] == 1
    assert hdry != hid


def test_api_carries_the_kite_book_and_the_card(api):  # noqa: F811
    c = cfg()
    hid = _closed_hint(c, world())
    r = api.post("/api/tips/kite/book/backfill")
    assert r.status_code == 200 and r.json()["added"] == [hid]
    d = api.get("/api/tips").json()
    assert d["kite_book"]["book"] == "kite" and d["kite_book"]["taken"] == 1 and d["kite_book"]["trades"][0]["hint_id"] == hid
    assert set(d["cards"]["kite"]) == {"mode", "today", "net", "lots_open", "trades_today", "since", "taken", "won"}
    assert d["cards"]["kite"]["mode"] == "off" and d["cards"]["kite"]["taken"] == 1
    assert {"on_kite", "paper"} <= set(d["cards"]["you_vs_all"])
    assert api.get("/api/tips/kite/book").json()["taken"] == 1


def test_add_line_says_if_it_fills():
    src = (ko.__file__ and open(ko.__file__).read())
    assert "filled: avg" not in src and src.count("if it fills: avg") == 2
