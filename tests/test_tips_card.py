"""💡 The Tips hint card's Kite blocks (backend/tips/card.py, backend/tips/facts.py): the charges estimate, the P&L strip
(open / closed, buy / sell), the distance bar, "channel said next", the trade facts from a tick series, the tick store
(only while armed / open on Kite and 10 min after, the dry flag), the archive move, the day facts row, the taken flag
following a Kite fill, the closed stop leg, and the old "SL + T1" wording gone."""

import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backend.config import IST, AppConfig
from backend.database import archive
from backend.database.models import get_db
from backend.tips import book as bk
from backend.tips import card as cd
from backend.tips import facts as fx
from backend.tips import kite_check as kc
from backend.tips import kite_orders as ko
from backend.tips import track as tr
from tests.test_tips_home import _day
from tests.test_tips_kite import Broker
from tests.test_tips_kite_auto import pushed, t19  # noqa: F401
from tests.tips_world import BN12, KEY12, at, cfg, seed, world

ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 11, 20, 9, 58, 0, tzinfo=IST)


def iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def hint(side="BUY", state="entered", entry=230.0, qty=65, qty_open=65, realised=0.0, stop=175.0, targets=(270,), **kw) -> dict:
    k = {"state": state, "entry_px": entry, "qty": qty, "qty_open": qty_open, "realised": realised, "stop_px": stop, "lots": 1,
         "entry_at": iso(T0 + timedelta(seconds=32)), "legs": {}, "log": [], **kw}
    return {"id": 11, "side": side, "lot_size": 65, "exchange": "NFO", "symbol": "NIFTY", "posted_at": iso(T0),
            "plan": {"targets": list(targets), "stop": {"kind": "premium", "value": stop}, "time_stop": "15:14"},
            "kite": k, "events": [], "run": {}}


@pytest.fixture
def ltp(monkeypatch):
    box = {"v": None}
    monkeypatch.setattr(bk, "last_ltp", lambda h: box["v"])
    return box


# --- charges ------------------------------------------------------------------------------------------------------------------

def test_charges_estimate_against_a_hand_computed_case():
    buy, sell = 230 * 65, 271.05 * 65                               # 14,950 and 17,618.25 of premium
    brk = min(20, 0.0003 * buy) + min(20, 0.0003 * sell)            # 4.485 + 5.28548
    txn = 0.0003503 * (buy + sell)
    sebi = 0.000001 * (buy + sell)
    want = brk + 0.001 * sell + txn + sebi + 0.00003 * buy + 0.18 * (brk + txn + sebi)
    c = fx.charges_est([(230, 65)], [(271.05, 65)], "NFO")
    assert c["total"] == round(want, 2) == 43.1
    assert c["stt"] == 17.62 and c["stamp"] == 0.45
    b = fx.charges_est([(230, 65)], [(271.05, 65)], "BFO")          # BSE options: 0.0325 %
    assert b["txn"] == round(0.000325 * (buy + sell), 2) and b["total"] < c["total"]
    assert fx.charges_est([(1000, 650)], [], "NFO")["brokerage"] == 20.0    # the ₹20 cap


# --- the P&L strip -----------------------------------------------------------------------------------------------------------------

def test_pnl_open_buy_and_sell_mirrored(ltp):
    ltp["v"] = 271.05
    s = cd.pnl_strip(AppConfig(), hint(), {"net_est": 2610.0}, T0 + timedelta(minutes=46, seconds=32), day={})
    assert s["state"] == "open" and s["label"] == "open P&L · 1 lot · 65 qty"
    assert s["pnl"] == s["open"] == round((271.05 - 230) * 65, 2) and s["pnl_text"] == "+₹2,668" and s["pct_text"] == "+17.8 %"
    assert s["tone"] == "up" and s["net_text"] == "≈ +₹2,610" and s["time_v"] == "46 min" and s["time_sub"] == "4 h 29 min to 3:14 PM"
    ltp["v"] = 210.0                                                  # a SELL hint gains when the price falls
    s = cd.pnl_strip(AppConfig(), hint(side="SELL", stop=260.0, targets=(190,)), None, T0 + timedelta(minutes=10), day={})
    assert s["pnl"] == 1300.0 and s["pct_text"] == "+8.7 %" and s["tone"] == "up"
    ltp["v"] = 240.0                                                  # booked part + the rest at the price
    s = cd.pnl_strip(AppConfig(), hint(qty=130, qty_open=65, realised=2600.0), None, T0 + timedelta(minutes=10), day={})
    assert s["pnl"] == 2600.0 + 650.0 and s["label"] == "open P&L · 1 lot · 65 qty"


def test_pnl_closed_your_exit_and_the_channel(ltp):
    ltp["v"] = 271.05
    h = hint(state="closed", qty_open=0, realised=round((247.55 - 230) * 65, 2), closed_at=iso(T0 + timedelta(minutes=53, seconds=32)),
             exit_by="you", exit_px=247.55)
    h["run"] = {"state": "closed", "hits": [{"k": 1, "t": iso(T0 + timedelta(minutes=119)), "px": 271.05}]}
    s = cd.pnl_strip(AppConfig(), h, None, T0 + timedelta(hours=3), day={})
    assert s["state"] == "closed" and s["label"] == "closed · your exit 10:51 AM" and s["pnl_text"] == "+₹1,141" and s["pct_text"] == "+7.6 %"
    assert s["time_k"] == "held" and s["time_v"] == "53 min" and s["time_sub"] == "fill 9:58 → exit 10:51"
    assert s["channel"] == {"text": "T1 at 271.05", "left": 1527.5, "note": "left ₹1,528", "tone": "down"}
    h["kite"]["exit_px"], h["kite"]["realised"] = 280.0, 3250.0                 # you did better than the record
    assert cd.pnl_strip(AppConfig(), h, None, T0 + timedelta(hours=3), day={})["channel"]["note"] == "beat the channel by ₹582"
    h2 = hint(side="SELL", state="closed", qty_open=0, realised=-650.0, exit_px=240.0, closed_at=iso(T0 + timedelta(minutes=5)), exit_by="stop")
    s = cd.pnl_strip(AppConfig(), h2, None, T0 + timedelta(hours=1), day={})
    assert s["pnl"] == -650.0 and s["tone"] == "down" and s["label"].startswith("closed · stop")
    assert cd.pnl_strip(AppConfig(), {**hint(), "kite": {"state": "none"}}, None, T0, day={}) is None      # paper-only: no strip


# --- the distance bar ----------------------------------------------------------------------------------------------------------------

def test_dist_mapping_clamp_almost_there_close_to_the_stop_and_targets(ltp):
    ltp["v"] = 271.05
    d = cd.dist(hint())
    assert d["stop"]["pct_text"] == "−35.4 %" and d["target"]["note"] == "almost there" and d["target"]["pct_text"] == "−0.4 %"
    assert d["dot"] == 100.0 and d["fill"]["pos"] == round((230 - 175) / 95 * 100, 1) and d["zones"] == [16, 84]
    ltp["v"] = 150.0                                                  # past the stop: clamped at 0
    assert cd.dist(hint())["dot"] == 0.0
    ltp["v"] = 179.0                                                  # within 3 % of the stop
    d = cd.dist(hint())
    assert d["stop"]["note"] == "close to the stop" and d["stop"]["tone"] == "down" and d["danger"]
    ltp["v"] = 250.0                                                  # multi-target: to the LAST target, a tick at T1
    d = cd.dist(hint(targets=(270, 300)))
    assert d["last"] == 300 and [t["label"] for t in d["ticks"]] == ["T1"] and d["ticks"][0]["pos"] == 76.0 and d["dot"] == 60.0
    assert d["target"]["px"] == 270 and d["target"]["name"] == "T1" and d["target"]["note"] == ""
    ltp["v"] = 200.0                                                  # a sell: stop above, target below, mirrored
    d = cd.dist(hint(side="SELL", entry=230.0, stop=260.0, targets=(190,)))
    assert d["dot"] == round((200 - 260) / (190 - 260) * 100, 1) and d["sell"]
    h = hint(state="closed", qty_open=0, exit_px=247.55, closed_at=iso(T0 + timedelta(minutes=53)))
    h["run"] = {"hits": [{"k": 1, "t": iso(T0.replace(hour=11, minute=57)), "px": 271.05}]}
    d = cd.dist(h)
    assert d["closed"] and d["dot_px"] == 247.55 and d["target"]["note"] == "hit at 11:57 AM after your exit"
    h["run"] = {}
    assert cd.dist(h)["target"]["note"] == "not reached"


# --- "channel said next" --------------------------------------------------------------------------------------------------------------

def test_said_next_ordering_and_the_more_chip():
    h = hint(state="closed", qty_open=0, exit_px=247.55, closed_at=iso(T0 + timedelta(minutes=53)), exit_by="you")
    h["events"] = [{"t": iso(T0 + timedelta(minutes=51)), "src": "channel", "text": 'channel: "Book part, TSL at cost" · followed: half booked at 270.7 · SL → cost'},
                   {"t": iso(T0 + timedelta(minutes=14)), "src": "channel", "text": 'the channel edited the post (was: "x")'},
                   {"t": iso(T0 + timedelta(minutes=1)), "src": "code", "text": "entry reached"}]
    h["kite"]["log"] = [{"t": iso(T0 + timedelta(minutes=51, seconds=20)), "text": '[AI] agrees · reads "Book part, TSL at cost" as book part', "src": "kite"}]
    h["run"] = {"hits": [{"k": 1, "t": iso(T0 + timedelta(minutes=119)), "px": 271.05}]}
    c = cd.said_next(h)
    assert [x["kind"] for x in c] == ["edit", "fu", "ai", "exit", "record"]
    assert c[1]["text"] == "Book part, TSL at cost → half booked at 270.7" and c[1]["at_label"] == "10:49 AM"
    assert c[2]["text"] == "[AI] 10:49 AM · reads as book part · agrees" and c[3]["text"] == "your exit · 247.55" and c[4]["text"] == "T1 271.05 · record"
    h["events"] += [{"t": iso(T0 + timedelta(minutes=20 + i)), "src": "channel", "text": f'channel: "hold {i}" · follow-up linked'} for i in range(6)]
    c = cd.said_next(h)
    assert len(c) == 8 and c[0] == {"kind": "more", "text": "+4", "n": 4, "at": None, "at_label": ""}
    assert c[-1]["kind"] == "record"                                  # the newest stay


# --- the facts -------------------------------------------------------------------------------------------------------------------------

def test_facts_build_on_a_seeded_tick_series():
    h = hint(qty=65)
    h["kite"]["legs"] = {"entry": {"at": iso(T0 + timedelta(seconds=5)), "status": "complete"}}
    h["events"] = [{"t": iso(T0 + timedelta(minutes=51)), "src": "channel", "text": 'channel: "Book part" · followed: half booked'}]
    ts = [{"at": iso(T0), "ltp": 229.25, "spot": 25900.0, "bid": 229.0, "ask": 229.4}]
    path = [(1, 230, 25912), (3, 226.1, 25905), (20, 260, 25930), (34, 270.5, 25950), (40, 272.3, 25960), (46, 271.05, 25966)]
    ts += [{"at": iso(T0 + timedelta(minutes=m)), "ltp": p, "spot": s, "bid": None, "ask": None} for m, p, s in path]
    f = fx.build(h, ts, T0 + timedelta(minutes=46, seconds=30))
    assert f["mfe"] == {"rs": round(42.3 * 65, 2), "pct": 18.4, "at": iso(T0 + timedelta(minutes=40))}
    assert f["mae"] == {"rs": round(-3.9 * 65, 2), "pct": -1.7, "at": iso(T0 + timedelta(minutes=3))}
    assert f["time_to_t1_s"] == 34 * 60 - 32 and f["time_to_stop_s"] is None
    assert f["stop_nearest_pct"] == round((226.1 - 175) / 226.1 * 100, 1)
    assert f["slippage_entry"] == {"rs": 0.75, "pct": 0.3, "quote": 229.25} and f["spread_entry"] == 0.4
    assert f["spot_at_fill"] == 25900 and f["spot_at_exit"] == 25966 and f["post_to_fill_s"] == 32
    assert f["followups"][0]["action"] == "partial" and f["followups"][0]["delay_s"] == 51 * 60
    assert f["charges_est"] == 43.1 and f["net_est"] == round((271.05 - 230) * 65 - 43.1, 2) and not f["final"]
    row = cd.facts_row(h, f)
    assert [x["k"] for x in row] == ["post → fill", "slippage at entry", "spread at entry", "index move in trade", "stop nearest", "exit by"]
    assert [x["v"] for x in row] == ["0 min 32 s", "+0.75 · 0.3 %", "0.4", "NIFTY +0.3 %", "22.6 % · never near", "—"]
    assert "best +18.4 %" in fx.summary_line(f) and "nearest the stop 22.6 %" in fx.summary_line(f)
    assert fx.build(hint(state="armed", entry=None), ts, T0) is None


# --- the tick store, the taken flag, the day facts --------------------------------------------------------------------------------------

def _ticks(hid):
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM tips_ticks WHERE hint_id = ? ORDER BY at", (hid,))]


def test_ticks_only_while_armed_or_open_plus_ten_minutes_dry_flag_and_taken(pushed):  # noqa: F811
    c, w, s, h1, h2, h3 = _day()
    t1, t2, t3 = _ticks(h1), _ticks(h2), _ticks(h3)
    assert t3 and all(x["dry"] == 1 for x in t1 + t2 + t3)
    assert [x["at"][11:16] for x in t3] == ["14:05", "14:10"]          # open on Kite (dry run)
    assert [x["at"][11:16] for x in t2] == ["13:53", "14:05", "14:10"]  # armed: SL-M resting
    closed = datetime.fromisoformat(ko.kstate(bk.load(h1))["closed_at"])
    assert all(datetime.fromisoformat(x["at"]) <= closed + timedelta(minutes=10) for x in t1)
    assert not any(x["at"][11:16] in ("13:53", "14:05", "14:10") for x in t1)      # later than 10 min after its close: none
    k = bk.load(h3)["kite"]
    assert k["taken"]["by"] == "auto" and k["taken"]["dry"] and bk.kite_taken_text(bk.load(h3)) == "DRY · taken by auto in the dry run"
    assert bk.load(h2)["kite"].get("taken") is None
    v = tr.hint_view(bk.load(h3), t19("14:12"), c)
    assert v["kite"]["footer"] == "DRY · taken by auto in the dry run" and v["pnl_strip"]["state"] == "open" and v["now_tile"]["pulse"]
    assert v["facts"] and v["trade_facts"]["entry_px"] == 306.0 and bk.load(h3)["facts"]["entry_px"] == 306.0     # stored by the poll
    h = {"kite": {"state": "entered"}}
    assert fx.wants_ticks(h, "auto", t19("14:00")) and not fx.wants_ticks(h, "off", t19("14:00"))
    h = {"kite": {"state": "closed", "closed_at": iso(t19("14:00"))}}
    assert fx.wants_ticks(h, "dry", t19("14:10")) and not fx.wants_ticks(h, "dry", t19("14:10", 1))
    assert not fx.wants_ticks({"kite": {"state": "none"}}, "auto", t19("14:00"))
    # the day check writes the day's facts row
    kc.day_check(c, t19("15:35"), notify=False)
    with get_db() as conn:
        row = json.loads(conn.execute("SELECT json FROM tips_day_facts WHERE day = ?", ("2026-11-19",)).fetchone()[0])
    assert {"calls", "taken", "won", "lost", "realised", "net", "cap_used", "slowest_fill_s", "reader_down_min", "ai_disagreed", "closed_by"} <= set(row)
    assert row["calls"] == 3 and row["taken"] == 2 and row["won"] == 1 and row["closed_by"]["target"] == 1 and row["realised"] == 1400.0
    assert "Trade facts: best" in kc.ai_input(bk.load(h3))           # the order read knows the best / worst move


def test_taken_flag_follows_a_real_kite_fill(pushed):  # noqa: F811
    c = cfg()
    c.tips.kite_mode = "click"
    b = Broker(world(**{KEY12: 262.0}))
    hid = seed(c, b, BN12)["hint_id"]
    ko.place_all(c, b, hid, 2, now=at("10:00"))
    assert bk.load(hid)["kite"].get("taken") is None                  # placed is not taken
    b.fill(b.last(transaction_type="BUY")["order_id"], 262.0)
    ko.sync(c, b, at("10:00", sec=10))
    h = bk.load(hid)
    assert h["kite"]["taken"] == {"by": "you", "at": iso(at("10:00", sec=10)), "dry": False}
    assert bk.kite_taken_text(h) == "taken by you on Kite" and not h["taken"]          # the paper book's own take untouched
    assert tr.hint_view(h, at("10:01"), c)["kite"]["footer"] == "taken by you on Kite"


def test_closed_stop_leg_hides_place_sl(ltp):
    ltp["v"] = 247.55
    h = hint(state="closed", qty_open=0, exit_px=247.55, closed_at=iso(T0 + timedelta(minutes=53)))
    legs = [{"leg": "stop", "status": "cancelled", "trigger": 175.0, "detail": "max loss"}, {"leg": "t1", "status": "cancelled", "price": 270.0, "detail": "x"}]
    out = cd.adjust_legs(h, legs, {"stop_nearest_pct": 22.6})
    assert out[0]["closed"] and out[0]["detail"] == "never within 22.6 % of the stop"
    src = (ROOT / "frontend/src/components/TipsPage.jsx").read_text()
    assert "lg.placeable !== false && !lg.closed && k.state !== 'closed') out.push(btn('Place SL…'" in src
    ltp["v"] = 268.0                                                  # open: the target's now line green within 3 %, the stop's red
    legs = [{"leg": "t1", "status": "watched", "price": 270.0}, {"leg": "stop", "status": "open", "trigger": 175.0}]
    assert cd.adjust_legs(hint(), legs, None)[0]["tone"] == "near"
    ltp["v"] = 178.0
    assert cd.adjust_legs(hint(), [{"leg": "stop", "status": "open", "trigger": 175.0}], None)[0]["tone"] == "danger"


def test_archive_moves_old_ticks_and_keeps_them():
    now = datetime(2026, 11, 20, 18, 0, tzinfo=IST)
    old, young = iso(now - timedelta(days=200)), iso(now - timedelta(days=100))
    with get_db() as conn:
        fx.write_tick(conn, 5, old, 100.0, 25000.0, 99.9, 100.1, False)
        fx.write_tick(conn, 5, young, 101.0, None, None, None, True)
    r = archive.run(AppConfig(), now)
    assert r["moved_by_table"]["tips_ticks"] == 1
    with get_db() as conn:
        live = [x[0] for x in conn.execute("SELECT at FROM tips_ticks")]
    a = sqlite3.connect(str(archive.archive_path()))
    arch = a.execute("SELECT at, ltp, bid, ask FROM tips_ticks").fetchall()
    a.close()
    assert live == [young] and arch == [(old, 100.0, 99.9, 100.1)]


def test_old_wording_is_gone():
    files = sorted((ROOT / "backend/tips").glob("*.py")) + [ROOT / "frontend/src/components/TipsPage.jsx"]
    for p in files:
        t = p.read_text()
        assert "SL + T1" not in t and not re.search(r"\bthe SL the moment it fills\b", t), p.name
    assert "SL rests the moment it fills · target watched by the app" in (ROOT / "backend/tips/kite_orders.py").read_text()


# --- the event log fold (log_open) and the backfill of old closed hints ------------------------------------------------------------

def test_log_open_only_while_working_today():
    now = T0 + timedelta(hours=1)
    assert cd.log_open(hint(day=T0.date().isoformat()), now)                                    # open on Kite today
    assert cd.log_open(hint(state="armed"), now)
    assert not cd.log_open(hint(state="closed", qty_open=0), now)                               # closed
    assert not cd.log_open(hint(), now + timedelta(days=1))                                     # not from today
    rec = {**hint(), "kite": {"state": "none"}, "status": "active", "taken": False}             # record-only
    assert not cd.log_open(rec, now)
    assert cd.log_open({**rec, "taken": True}, now)                                             # a paper take still open
    assert not cd.log_open({**rec, "status": "skipped"}, now)
    assert cd.blocks(AppConfig(), hint(state="closed", qty_open=0), now)["log_open"] is False
    src = (ROOT / "frontend/src/components/TipsPage.jsx").read_text()
    assert "defOpen={h?.log_open}" in src and "show all" in src and "▾ show less" in src and "bp.tips.log." in src
    assert "hidden={!open && i < more}" in src                                                  # folded lines stay in the DOM


def _old_hint(kite: dict, run: dict, score: dict) -> int:
    with get_db() as conn:
        cur = conn.execute("INSERT INTO tips_hints (channel, post_id, created, posted_at, raw, symbol, side, instrument, exchange, lot_size, "
                           "status, plan, run, score, kite, events) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           ("chan", 9011, iso(T0), iso(T0), "Buy NIFTY 26000 CE 230", "NIFTY", "BUY", "option", "NFO", 65, "closed",
                            json.dumps({"targets": [270.0], "stop": {"kind": "premium", "value": 175.0}, "time_stop": "15:14"}),
                            json.dumps(run), json.dumps(score), json.dumps(kite), "[]"))
        return cur.lastrowid


def _order(conn, hid, leg, status, px, qty, by, at_, side="BUY"):
    conn.execute('INSERT INTO tips_orders (hint_id, leg, side, qty, order_type, status, order_id, filled_px, filled_qty, placed_at, '
                 'updated_at, "by", day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (hid, leg, side, qty, "MARKET", status, f"O{leg}{hid}", px, qty if px is not None else None, at_, at_, by, at_[:10]))


def test_backfill_close_facts_from_the_order_rows_idempotent(ltp):
    entry_at, exit_at = iso(T0 + timedelta(seconds=32)), iso(T0 + timedelta(minutes=53, seconds=32))
    k = {"state": "closed", "entry_px": 230.0, "qty": 65, "qty_open": 0, "lots": 1, "realised": round((247.55 - 230) * 65, 2),
         "by": "auto", "dry": None, "entry_at": entry_at, "reason": "your exit", "day": T0.date().isoformat(), "stop_px": 175.0,
         "legs": {}, "log": [{"t": exit_at, "text": "Kite: exit SELL 65 MARKET (your click) · stop and targets cancelled", "src": "kite"}]}
    run = {"state": "active", "hits": []}                                     # the record's run: no hits kept on it
    score = {"entry_reached": True, "first": "t1", "end": "t1", "exit_px": 271.05, "open": False}
    hid = _old_hint(k, run, score)
    with get_db() as conn:
        _order(conn, hid, "entry", "complete", 230.0, 65, "auto", iso(T0 + timedelta(seconds=5)))
        _order(conn, hid, "stop", "cancelled", None, 65, "app", entry_at, side="SELL")
        _order(conn, hid, "exit", "complete", 247.55, 65, "click", exit_at, side="SELL")
        before = [dict(r) for r in conn.execute("SELECT * FROM tips_orders WHERE hint_id = ?", (hid,))]
    assert ko.backfill_close_facts() == [hid]
    kk = bk.load(hid)["kite"]
    assert kk["exit_px"] == 247.55 and kk["closed_at"] == exit_at and kk["exit_by"] == "you"
    assert kk["taken"] == {"by": "auto", "at": entry_at, "dry": False} and kk["log"] == k["log"]   # nothing removed from the log
    assert ko.backfill_close_facts() == [] and bk.load(hid)["kite"] == kk                         # idempotent
    with get_db() as conn:
        after = [dict(r) for r in conn.execute("SELECT * FROM tips_orders WHERE hint_id = ?", (hid,))]
    assert after == before                                                                       # the order rows untouched
    h = bk.load(hid)
    ltp["v"] = 271.05
    s = cd.pnl_strip(AppConfig(), h, None, T0 + timedelta(hours=3), day={})
    assert s["label"] == "closed · your exit 10:51 AM" and s["time_v"] == "53 min"
    assert s["channel"] == {"text": "T1 at 271.05", "left": 1527.5, "note": "left ₹1,528", "tone": "down"}   # the record's call
    t = cd.now_tile(h)
    assert t["value"] == 247.55 and t["label"] == "at exit" and t["sub"] == "exit fill · 10:51 AM"   # no "now" after the exit: the legs keep it
    assert bk.kite_taken_text(h) == "taken by auto on Kite"
    row = cd.facts_row(h, fx.for_hint(h, T0 + timedelta(hours=3)))
    assert row[0]["v"] == "0 min 32 s" and row[-1]["v"] == "you · 10:51 AM" and row[2]["v"] == "—"   # no ticks: "—"


def test_backfill_exit_by_rules_and_the_card_before_backfill(ltp):
    base = {"state": "closed", "entry_px": 100.0, "qty": 50, "qty_open": 0, "by": "app", "entry_at": iso(T0), "log": []}
    rows = lambda leg, by: [{"leg": "entry", "status": "complete", "filled_px": 100.0, "by": by, "updated_at": iso(T0)},   # noqa: E731
                            {"leg": leg, "status": "complete", "filled_px": 90.0, "filled_qty": 50, "by": by, "updated_at": iso(T0 + timedelta(minutes=9))}]
    assert ko.close_facts({"kite": dict(base)}, rows("stop", "app"))["exit_by"] == "stop"
    assert ko.close_facts({"kite": dict(base)}, rows("t1", "app"))["exit_by"] == "target"
    assert ko.close_facts({"kite": dict(base)}, rows("exit", "app"))["exit_by"] == "auto"
    assert ko.close_facts({"kite": dict(base)}, rows("exit", "click"))["exit_by"] == "you"
    ts = {**base, "log": [{"t": iso(T0), "text": "Kite: exit SELL 50 MARKET (app) · stop and targets cancelled · time stop 3:14 PM"}]}
    assert ko.close_facts({"kite": ts}, rows("exit", "app"))["exit_by"] == "time_stop"
    two = [{"leg": "t1", "status": "complete", "filled_px": 120.0, "filled_qty": 25, "by": "app"},
           {"leg": "t2", "status": "complete", "filled_px": 130.0, "filled_qty": 25, "by": "app", "updated_at": iso(T0)}]
    assert ko.close_facts({"kite": dict(base)}, two)["exit_px"] == 125.0                          # the average exit
    assert ko.close_facts({"kite": {**base, "state": "entered"}}, rows("exit", "click")) == {}     # only closed hints
    full = {**base, "exit_px": 95.0, "closed_at": iso(T0), "exit_by": "you", "taken": {"by": "you"}}
    assert ko.close_facts({"kite": full}, rows("exit", "app")) == {}                              # nothing overwritten
    h = hint(state="closed", qty_open=0, realised=1140.75)                                      # before a backfill: no exit price
    ltp["v"] = 271.05
    t = cd.now_tile(h)
    assert t["value"] == 271.05 and "exit fill" not in t["sub"].replace("exit fill not stored", "") and t["label"] == "now · after exit"
    h["run"] = {"state": "closed", "hits": [{"k": 1, "t": iso(T0 + timedelta(hours=2)), "px": 271.05}]}
    assert cd.channel_vs_you(h, None) == {"text": "T1 at 271.05", "note": "", "tone": ""}           # the record, not "no result yet"
    h["run"] = {"state": "active", "hits": []}
    assert cd.channel_vs_you(h, None) is None                                                    # genuinely open: no result yet
