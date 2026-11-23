import json
"""⚡ Tips → Kite orders PR B: the dry run (the simulator's fills, a whole Place all with DRY in every line, dry caps
apart from real ones, never mixed, "Clear dry run" voids and never deletes), the connection test (happy path, the IP
whitelist refusal, market closed, the 5-minute guard), the public IP (cached, failing gracefully), the routes, the
tips_orders migration, and no broker name past "Kite"."""

import re
import sqlite3
from pathlib import Path

import pytest
from datetime import datetime, timedelta

from backend.config import IST

from backend.database.models import get_db, init_db
from backend.tips import book as bk
from backend.tips import kite_orders as ko
from backend.tips import kite_sim
from backend.tips import track as tr
from backend.tips.book import TipsError
from tests.test_tips_kite import Broker
from tests.tips_world import BN12, KEY12, at, cfg, seed, world

ROOT = Path(__file__).resolve().parent.parent
NIFTY_PE = "NIFTY26NOV24150PE"


@pytest.fixture
def pushed(monkeypatch):
    out = []
    monkeypatch.setattr(bk, "push", lambda config, key, html: out.append(html) or True)
    return out


class ProbeBroker(Broker):
    """The PR A broker plus profile() for the connection test."""

    def __init__(self, base):
        super().__init__(base)
        self.profiled = 0
        self.after_place = None          # a status to give an order the moment it is placed (REJECTED ...)

    def profile(self):
        self.profiled += 1
        return {"user_id": "AB1234"}

    def cancel_order(self, order_id):
        if self.book[order_id]["status"] != "OPEN":                  # Kite refuses to cancel a finished order
            raise RuntimeError(f"Order cannot be cancelled as it is being processed or {self.book[order_id]['status'].lower()}")
        return super().cancel_order(order_id)

    def place_order(self, **kw):
        oid = super().place_order(**kw)
        if self.after_place:
            self.book[oid].update(self.after_place)
        return oid


def setup(mode="dry", text=BN12, ltp=262.0, **cfgkw):
    c = cfg()
    c.tips.kite_mode = mode
    for k, v in cfgkw.items():
        setattr(c.tips, k, v)
    b = ProbeBroker(world(**{KEY12: ltp, "NSE:NIFTY 50": 26200.0, f"NFO:{NIFTY_PE}": 1.3}))
    hid = seed(c, b, text)["hint_id"]
    return c, b, hid


def quote(c, b, px, hhmm):
    b.prices[KEY12] = px
    tr.poll(c, b, at(hhmm))


# --- the simulator -----------------------------------------------------------------------------------------------------

def test_sim_fills_market_limit_slm_cancel_and_survives_a_restart():
    prices = {"X": 100.0}
    s = kite_sim.SimClient(ltp_for=lambda ts: prices.get(ts))
    m = s.place_order(tradingsymbol="X", transaction_type="BUY", quantity=30, order_type="MARKET", tag="bptips")
    lim = s.place_order(tradingsymbol="X", transaction_type="SELL", quantity=15, order_type="LIMIT", price=120.0)
    slm = s.place_order(tradingsymbol="X", transaction_type="SELL", quantity=30, order_type="SL-M", trigger_price=90.0)
    blim = s.place_order(tradingsymbol="X", transaction_type="BUY", quantity=15, order_type="LIMIT", price=95.0)
    gone = s.place_order(tradingsymbol="X", transaction_type="SELL", quantity=15, order_type="LIMIT", price=200.0)
    assert m == "DRY-1" and gone == "DRY-5"
    book = {o["order_id"]: o for o in kite_sim.SimClient().orders()}           # a new instance: the stored book
    assert book[m]["status"] == "COMPLETE" and book[m]["average_price"] == 100.0 and book[m]["filled_quantity"] == 30
    assert set(book[m]) >= {"order_id", "status", "average_price", "filled_quantity", "quantity", "tag", "tradingsymbol",
                            "transaction_type", "order_type", "price", "trigger_price", "status_message"}
    assert book[lim]["status"] == book[slm]["status"] == book[blim]["status"] == "OPEN"
    s.modify_order(slm, trigger_price=92.0)
    assert s.cancel_order(gone) == gone
    with pytest.raises(kite_sim.DryRunError):
        s.cancel_order(gone)                                                     # only an open order cancels
    assert s.mark({"X": 121.0}) == 1                                             # the limit sell: ltp >= price
    assert s.mark({"X": 94.0}) == 1                                              # the limit buy: ltp <= price
    assert s.mark({"X": 91.5}) == 1                                              # the SL-M sell: ltp <= trigger, at the trigger
    book = {o["order_id"]: o for o in s.orders()}
    assert (book[lim]["status"], book[lim]["average_price"]) == ("COMPLETE", 120.0)
    assert (book[blim]["status"], book[blim]["average_price"]) == ("COMPLETE", 95.0)
    assert (book[slm]["status"], book[slm]["average_price"]) == ("COMPLETE", 92.0)
    assert book[gone]["status"] == "CANCELLED"
    with pytest.raises(kite_sim.DryRunError):
        s.modify_order(lim, price=1.0)
    # no quote yet: a MARKET order waits for the first one
    prices.clear()
    w = s.place_order(tradingsymbol="Y", transaction_type="BUY", quantity=1, order_type="MARKET")
    assert {o["order_id"]: o for o in s.orders()}[w]["status"] == "OPEN"
    s.mark({"Y": 7.0})
    assert {o["order_id"]: o for o in s.orders()}[w]["average_price"] == 7.0
    assert s.clear() == 6 and s.orders() == []
    assert s.place_order(tradingsymbol="X", quantity=1, order_type="LIMIT", price=1.0) == "DRY-7"   # ids never reused


# --- a whole dry Place all -------------------------------------------------------------------------------------------------

def test_dry_place_all_whole_flow_with_dry_in_every_line(pushed):
    c, b, hid = setup()
    quote(c, b, 262.0, "10:00")
    s = ko.client_for(c, b, bk.load(hid))
    assert ko.is_dry(s) and isinstance(s, kite_sim.SimClient)
    h = ko.place_all(c, s, hid, 2, now=at("10:00", sec=5))
    assert h["kite"]["dry"] and h["kite"]["state"] == "armed"
    e = ko.rows_for(hid)[0]
    assert e["by"] == "dry" and e["order_id"] == "DRY-1" and e["leg"] == "entry"
    r = ko.sync(c, s, at("10:00", sec=10))
    assert r["lines"] == ["⚡ Kite DRY · #1 entry filled 262 ×30 (2 lots) · SL 190 placed · T1 320 watched by the app"]
    quote(c, b, 325.0, "10:20")
    ko.watch_all(c, b, at("10:20", sec=1))                                 # the Syncer's tick: the target watch, then the sync
    r = ko.sync(c, s, at("10:20", sec=5))
    assert r["lines"] == ["⚡ Kite DRY · #1 T1 hit 325 · sold 15 of 30 at market · stop → cost 262 for the rest · watching T2 350"]
    quote(c, b, 351.0, "10:40")
    ko.watch_all(c, b, at("10:40", sec=1))
    r = ko.sync(c, s, at("10:40", sec=5))
    assert r["lines"][0] == "⚡ Kite DRY · #1 T2 hit 351 · sold 15 at market · +₹1,335 · closed +₹2,280 total"
    k = bk.load(hid)["kite"]
    assert k["state"] == "closed" and k["realised"] == 2280.0
    assert not [o for o in s.orders() if o["order_type"] == "LIMIT" and o["transaction_type"] == "SELL"]   # no resting target, dry too
    kp = [p for p in pushed if "Kite" in p]
    assert len(kp) == 4 and all(p.startswith("⚡ Kite DRY · #1 ") for p in kp)
    assert all(x["text"].startswith("Kite DRY:") for x in k["log"])
    assert b.placed == [] and b.modified == [] and b.cancelled == [] and ("orders",) not in b.calls   # nothing reached Kite
    v = tr.hint_view(bk.load(hid), config=c)["kite"]
    assert v["dry"] and all(x["dry"] for x in v["legs"] if x["status"] != "none")
    t = ko.today(c, b, at("10:41"))
    assert t["dry"] and t["warning"].startswith("DRY RUN · nothing reaches Kite")
    assert t["orders"] and all(o["dry"] for o in t["orders"]) and t["net"] == 2280.0
    g = {x["key"]: x for x in t["guards"]}
    assert g["login"]["ok"] and g["login"]["text"] == "dry run · no login needed"
    assert ko.realised_today("2026-11-17") == 0.0 and ko.realised_today("2026-11-17", dry=True) == 2280.0


def test_dry_run_ignores_hours_and_window_but_not_the_caps(pushed):
    c, b, hid = setup(kite_max_entries_day=1, kite_max_lots=1)
    quote(c, b, 262.0, "10:00")
    s = ko.sim()
    with pytest.raises(TipsError, match="lots per order 2 > the cap 1"):
        ko.place_entry(c, s, hid, 2, now=at("19:05"))
    h = ko.place_entry(c, s, hid, 1, now=at("19:05"))                      # the evening: a MARKET still fills at the last LTP
    r = ko.sync(c, s, at("19:05", sec=10))
    assert r["lines"] == ["⚡ Kite DRY · #1 entry filled 262 ×15 (1 lot)"] and bk.load(hid)["kite"]["state"] == "entered"
    assert h["kite"]["dry"]
    g = {x["key"]: x for x in ko.guards(c, s, bk.load(hid), 1, at("19:06"))}
    assert g["market"]["ok"] and "a dry run fills at the last price" in g["market"]["text"]
    assert g["window"]["ok"] and g["window"]["text"].endswith("not enforced in a dry run")
    hid2 = seed(c, b, BN12.replace("50300", "51000"), "10:30", 2)["hint_id"]
    with pytest.raises(TipsError, match="entries today 1 of 1"):
        ko.place_entry(c, s, hid2, 1, now=at("19:10"))


def test_dry_caps_apart_from_real_and_never_mixed(pushed):
    c, b, hid = setup(kite_max_entries_day=1)
    quote(c, b, 262.0, "10:00")
    s = ko.sim()
    ko.place_all(c, s, hid, 1, now=at("10:00"))
    ko.sync(c, s, at("10:00", sec=10))
    quote(c, b, 180.0, "10:05")
    ko.sync(c, s, at("10:05", sec=5))                                     # the dry stop fills: a dry loss
    assert ko.realised_today("2026-11-17", dry=True) == -1080.0 and ko.realised_today("2026-11-17") == 0.0
    c.tips.kite_mode = "click"                                             # the view resets: real rows only
    t = ko.today(c, b, at("10:06"))
    assert t["caps"]["entries"] == 0 and t["caps"]["used_loss"] == 0.0 and t["orders"] == [] and not t["dry"]
    assert ko.client_for(c, b, bk.load(hid)) is b
    with pytest.raises(TipsError, match=re.escape("already traded on Kite (dry)")):
        ko.place_entry(c, b, hid, 1, now=at("10:07"))
    assert "all" not in tr.hint_view(bk.load(hid), config=c)["kite"]["actions"]
    hid2 = seed(c, b, BN12.replace("50300", "51000"), "10:30", 2)["hint_id"]
    ko.place_entry(c, b, hid2, 1, now=at("10:40"))                         # the real cap is untouched by the dry run
    assert b.placed[-1]["tag"] == "bptips" and ko.rows_for(hid2)[0]["by"] == "click"
    c.tips.kite_mode = "dry"
    t = ko.today(c, b, at("10:41"))
    assert t["caps"]["entries"] == 1 and all(o["dry"] for o in t["orders"])
    g = {x["key"]: x for x in ko.guards(c, ko.sim(), bk.load(hid2), 1, at("10:42"))}
    assert not g["one"]["ok"] and g["one"]["text"] == "already on Kite · never averaged"     # a real position stays real
    assert ko.client_for(c, b, bk.load(hid2)) is b                          # its exit goes to Kite, even in a dry run
    assert set(tr.hint_view(bk.load(hid2), config=c)["kite"]["actions"]) <= {"exit", "cancel:entry"}
    with pytest.raises(TipsError, match="a real Kite position"):
        ko.place_rest(c, b, hid2, now=at("10:43"))


def test_clear_dry_run_voids_never_deletes(pushed):
    c, b, hid = setup()
    quote(c, b, 262.0, "10:00")
    s = ko.sim()
    ko.place_all(c, s, hid, 2, now=at("10:00"))
    ko.sync(c, s, at("10:00", sec=10))
    n = len(ko.rows_for(hid))
    assert n == 2                                                          # the entry and the stop: the targets are watched
    out = ko.clear_dry(c, at("10:10"))
    assert out == {"voided": 2, "sim_orders": 2, "hints": 1}
    rows = ko.rows_for(hid)
    assert len(rows) == n and {r["status"] for r in rows} == {"voided"}       # kept, voided
    k = bk.load(hid)["kite"]
    assert k["state"] == "none" and k["legs"] == {} and k["log"][-1]["text"].startswith("Kite DRY: dry run cleared")
    assert s.orders() == [] and ko.today(c, b, at("10:11"))["orders"] == []
    assert ko.entries_today("2026-11-17", dry=True) == 0
    ko.place_entry(c, s, hid, 1, now=at("10:12"))                            # rehearse again
    assert ko.rows_for(hid)[-1]["order_id"] == "DRY-3"
    c.tips.kite_mode = "click"
    ko.clear_dry(c, at("10:13"))
    with pytest.raises(TipsError, match=re.escape("already traded on Kite (dry)")):   # still not real the same day
        ko.place_entry(c, b, hid, 1, now=at("10:14"))


# --- the connection test -----------------------------------------------------------------------------------------------

def test_connection_test_happy_path(pushed):
    c, b, _ = setup(mode="click")
    slept = []
    res = ko.connection_test(c, b, at("12:04"), sleep=slept.append)
    assert res["passed"] is True and res["outcome"] == "passed" and slept == [5]
    assert [x["key"] for x in res["steps"]] == ["session", "contract", "price", "place", "wait", "cancel", "protection"]
    assert all(x["ok"] for x in res["steps"])
    p = b.placed[-1]
    assert p["tradingsymbol"] == NIFTY_PE and p["order_type"] == "LIMIT" and p["price"] == 0.7     # max(0.05, round(0.5 × 1.3, 1))
    assert p["transaction_type"] == "BUY" and p["quantity"] == 75 and p["tag"] == "bptest" and p["market_protection"] is None
    assert p["order_id"] in b.cancelled
    assert "cannot fill" in res["steps"][2]["text"] and res["steps"][6]["text"] == "market protection: sent with real orders (5%)"
    assert res["at_label"] == "12:04 PM"
    row = [r for r in ko.rows_today("2026-11-17") if r["leg"] == "test"][0]
    assert row["hint_id"] == 0 and row["by"] == "test" and row["status"] == "cancelled"
    t = ko.today(c, b, at("12:05"))
    assert t["test"]["passed"] and t["orders"][0]["label"] == "Connection test"
    assert pushed[-1].startswith("🔌 Kite connection test passed")
    with pytest.raises(TipsError, match="one connection test per 5 minutes · the next from 12:09:00 PM"):
        ko.connection_test(c, b, at("12:06"), sleep=slept.append)
    assert ko.connection_test(c, b, at("12:10"), sleep=slept.append)["passed"]


def test_connection_test_price_is_half_the_last():
    c, b, _ = setup(mode="click")
    b.prices[f"NFO:{NIFTY_PE}"] = 3.3
    res = ko.connection_test(c, b, at("12:04"), sleep=lambda s: None)
    assert b.placed[-1]["price"] == 1.6 and "LIMIT 1.6 (half of the last 3.3)" in res["steps"][2]["text"]
    assert "~8 % under NIFTY 26,200" in res["steps"][1]["text"]


def test_connection_test_ip_refusal_market_closed_permission_and_guards(pushed):
    c, b, _ = setup(mode="click")
    ko.public_ip(at("09:59"), fetch=lambda u: "129.154.1.2")
    b.refuse = "Request from this IP is not allowed. Add your static IP to the whitelist."
    res = ko.connection_test(c, b, at("10:00"), sleep=lambda s: None)
    assert res["passed"] is False and res["outcome"] == "failed"
    assert res["steps"][-1]["text"] == "Kite refused the order · static IP not whitelisted · add this server's IP 129.154.1.2 in Kite Connect"
    assert res["steps"][-1]["detail"].startswith("Request from this IP")
    assert [r for r in ko.rows_today("2026-11-17") if r["leg"] == "test"][-1]["status"] == "failed"
    b.refuse = "Markets are closed right now. Use GTT for placing long standing orders instead."
    res = ko.connection_test(c, b, at("19:00"), sleep=lambda s: None)
    assert res["passed"] is None and res["outcome"] == "market_closed"
    assert res["summary"] == "market closed: run the test between 9:15 AM and 3:30 PM"
    b.refuse = "Insufficient permission for that call."
    res = ko.connection_test(c, b, at("19:06"), sleep=lambda s: None)
    assert res["summary"] == "order permission missing on the app key"
    b.refuse = None                                                          # accepted, then rejected by the exchange: closed
    b.after_place = {"status": "REJECTED", "status_message": "Your order could not be placed as the market is closed."}
    res = ko.connection_test(c, b, at("19:12"), sleep=lambda s: None)
    assert res["outcome"] == "market_closed"
    b.after_place = {"status": "COMPLETE", "filled_quantity": 75, "average_price": 0.65}   # impossible: said loudly
    res = ko.connection_test(c, b, at("19:18"), sleep=lambda s: None)
    assert res["passed"] is False and "exit it by hand in Kite" in res["summary"]
    c.tips.kite_mode = "off"
    with pytest.raises(TipsError, match="Kite orders are off"):
        ko.connection_test(c, b, at("19:30"))
    c.tips.kite_mode = "dry"
    b.has_token = False
    with pytest.raises(TipsError, match="log in to Kite first"):
        ko.connection_test(c, b, at("19:30"))
    with pytest.raises(TipsError, match="log in to Kite first"):
        ko.connection_test(c, ko.sim(), at("19:30"))                        # the simulator is never "tested"


def test_session_refused_stops_the_test(pushed):
    c, b, _ = setup(mode="click")
    b.profile = lambda: (_ for _ in ()).throw(RuntimeError("Incorrect `api_key` or `access_token`."))
    res = ko.connection_test(c, b, at("10:00"), sleep=lambda s: None)
    assert res["passed"] is False and len(res["steps"]) == 1 and b.placed == []


# --- the public IP -----------------------------------------------------------------------------------------------------

def test_public_ip_cached_and_failing_gracefully():
    calls = []

    def ok(url):
        calls.append(url)
        return "129.154.1.2"
    assert ko.cached_ip() is None
    assert ko.public_ip(at("10:00"), fetch=ok) == "129.154.1.2" and calls == ["https://api.ipify.org"]
    assert ko.public_ip(at("10:09"), fetch=ok) == "129.154.1.2" and len(calls) == 1          # cached 10 minutes
    assert ko.public_ip(at("10:11"), fetch=ok) == "129.154.1.2" and len(calls) == 2
    assert ko.cached_ip() == "129.154.1.2"

    def first_down(url):
        calls.append(url)
        if "ipify" in url:
            raise TimeoutError("timed out")
        return "129.154.9.9\n".strip()
    assert ko.public_ip(at("10:30"), fetch=first_down) == "129.154.9.9" and calls[-2:] == list(ko.IP_URLS)

    def down(url):
        raise OSError("no network")
    assert ko.public_ip(at("10:45"), fetch=down) is None and ko.cached_ip() is None
    assert ko.public_ip(at("10:46"), fetch=ok) is None                    # a failed lookup is tried again after 2 minutes
    assert ko.public_ip(at("10:48"), fetch=ok) == "129.154.1.2"
    assert ko.public_ip(at("11:00"), fetch=lambda u: "<html>nope</html>", force=True) is None   # junk is never shown
    c = cfg()
    c.tips.kite_mode = "click"
    assert ko.today(c, None, at("11:01"))["public_ip"] is None


# --- routes, migration, names ---------------------------------------------------------------------------------------------

def test_routes(fake_kite, make_client, monkeypatch, pushed):
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TIPS_TG_SESSION", "")
    from fastapi.testclient import TestClient
    from backend import main
    from backend.api import routes
    kc = make_client(fake_kite, history_years=1)
    kc.config.access_token = ""
    monkeypatch.setattr(main, "KiteClient", lambda a, b: kc)
    monkeypatch.setattr(ko, "_now", lambda: at("10:00"))
    monkeypatch.setattr(ko, "TEST_WAIT_S", 0)
    b = ProbeBroker(world(**{KEY12: 262.0, "NSE:NIFTY 50": 26200.0, f"NFO:{NIFTY_PE}": 1.3}))
    with TestClient(main.app) as api:
        routes._state.client = b
        seed(routes._state.config, b, BN12)
        assert api.post("/api/tips/kite/test").status_code == 409               # mode off
        assert api.put("/api/tips/settings", json={"kite_mode": "dry"}).json()["kite_mode"] == "dry"
        tr.poll(routes._state.config, b, at("10:00"))
        t = api.get("/api/tips/kite").json()
        assert t["dry"] and "public_ip" in t and "test" in t and t["warning"].startswith("DRY RUN")
        r = api.post("/api/tips/hints/1/kite/all", json={"lots": 2})
        assert r.status_code == 200
        k = r.json()["kite"]
        assert k["dry"] and k["state"] == "entered"                             # the simulator filled at once
        assert {x["leg"]: x["status"] for x in k["legs"]} == {"entry": "complete", "stop": "placed", "t1": "watched", "t2": "watched"}
        assert b.placed == []
        r = api.post("/api/tips/kite/resync").json()
        assert [o["dry"] for o in r["orders"]] == [True, True]
        r = api.post("/api/tips/kite/test").json()
        assert r["result"]["passed"] and b.placed[-1]["tag"] == "bptest"
        assert api.post("/api/tips/kite/test").status_code == 409               # one per 5 minutes
        r = api.post("/api/tips/kite/dry/clear").json()
        assert r["voided"] == 2 and [o["leg"] for o in r["orders"]] == ["test"]
        assert api.get("/api/tips/hints/1").json()["kite"]["state"] == "none"


def test_migration_keeps_pr_a_rows(tmp_path):
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.executescript("""CREATE TABLE tips_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, hint_id INTEGER NOT NULL,
        leg TEXT NOT NULL CHECK(leg IN ('entry', 'stop', 't1', 't2', 'exit')), side TEXT, qty INTEGER, order_type TEXT,
        price REAL, "trigger" REAL, status TEXT NOT NULL DEFAULT 'placed'
            CHECK(status IN ('placed', 'open', 'complete', 'rejected', 'cancelled', 'failed')),
        order_id TEXT, filled_px REAL, filled_qty INTEGER, placed_at TEXT, updated_at TEXT,
        "by" TEXT CHECK("by" IN ('click', 'app', 'auto')), note TEXT, day TEXT);
        CREATE INDEX idx_tips_orders_day ON tips_orders(day);
        INSERT INTO tips_orders (hint_id, leg, side, qty, status, order_id, "by", day) VALUES (41, 'entry', 'BUY', 40, 'complete', '251119000412', 'click', '2026-11-17');""")
    conn.commit()
    conn.close()
    init_db(str(p))
    init_db(str(p))                                                               # twice: runs once
    with get_db(str(p)) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM tips_orders")]
        assert len(rows) == 1 and rows[0]["order_id"] == "251119000412" and rows[0]["id"] == 1
        conn.execute("INSERT INTO tips_orders (hint_id, leg, status, \"by\") VALUES (0, 'test', 'voided', 'dry')")
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'idx_tips_orders_day'").fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'tips_orders_pr_a'").fetchone()


def test_no_broker_name_past_kite_and_the_page_carries_dry_and_the_test():
    src = "\n".join((ROOT / f"frontend/src/components/{f}").read_text(encoding="utf-8") for f in ("TipsPage.jsx", "tipsKite.js"))   # the confirms' words are shared with Home
    for f in ("backend/tips/kite_orders.py", "backend/tips/kite_sim.py"):
        assert not re.search(r"zerodha", (ROOT / f).read_text(encoding="utf-8"), re.I)
    assert not re.search(r"zerodha", src, re.I)
    for s in ("Dry run", "DRY · ", "DRY RUN · nothing reaches Kite", "Clear dry run", "Test connection…", "Kite connection test",
              "server public IP", "whitelist this one in Kite Connect", "nothing reaches Kite"):
        assert s in src, s


def test_the_connection_test_loads_the_nifty_list_itself(monkeypatch):
    """19 Nov: 'no NIFTY option in the instrument list yet' — the test must load the NFO dump on its own."""
    from datetime import date
    from backend.tips import kite_orders as ko
    from backend.tips import track as tr
    now = datetime.now(IST).replace(hour=10, minute=0)
    exp = (date.today() + timedelta(days=5)).isoformat()
    loaded = []

    class K:
        has_token = True

        def instruments(self, exchange):
            loaded.append(exchange)
            return [{"exchange": "NFO", "tradingsymbol": "NIFTY25NOV24000PE", "name": "NIFTY", "expiry": date.fromisoformat(exp), "strike": 24000.0,
                     "instrument_type": "PE", "instrument_token": 1, "lot_size": 75, "segment": "NFO-OPT"},
                    {"exchange": "NFO", "tradingsymbol": "NIFTY25NOV25000CE", "name": "NIFTY", "expiry": date.fromisoformat(exp), "strike": 25000.0,
                     "instrument_type": "CE", "instrument_token": 2, "lot_size": 75, "segment": "NFO-OPT"}]

        def quote(self, keys):
            return {k: {"last_price": 25100.0 if "NIFTY 50" in k else 120.0} for k in keys}
    monkeypatch.setattr(tr, "_asked_today", lambda *a, **k: set(), raising=False)
    assert ko._nifty_rows(now) == []
    c = ko.test_contract(K(), now)
    assert loaded == ["NFO"] and c and c["tradingsymbol"] == "NIFTY25NOV24000PE" and c["lot_size"] == 75


def test_a_rehearsal_hint_appears_only_in_the_dry_run_and_never_in_the_record(monkeypatch):
    from backend.tips import kite_orders as ko
    from backend.tips import track as tr
    c = cfg()
    c.tips.kite_mode = "click"
    with pytest.raises(TipsError):
        ko.rehearsal_hint(c, None, None)
    c.tips.kite_mode = "dry"
    r = ko.rehearsal_hint(c, None, None)
    h = bk.load(r["hint_id"])
    assert h["channel"] == "rehearsal" and h["symbol"] == "NIFTY" and h["opt_type"] == "CE" and h["side"] == "BUY"
    assert h["stop"]["value"] == 80 and h["targets"] == [120, 140] and h["plan"]["ok"] is True
    assert "REHEARSAL" in r["text"] and "dry run only" in r["text"]
    rec = tr.record(30)
    assert all(x.get("channel") != "rehearsal" for grp in rec.get("by_instrument", []) for x in grp.get("items", []))
    assert "rehearsal" not in json.dumps(rec)


def test_the_five_minute_gap_counts_only_tests_that_placed_an_order(monkeypatch):
    from backend.database.models import set_setting
    from backend.tips import kite_orders as ko
    c = cfg()
    c.tips.kite_mode = "click"
    now = datetime.now(IST).replace(hour=10, minute=0)
    with get_db() as conn:                                            # a test that failed before placing, a minute ago
        set_setting(conn, ko.TEST_KEY, json.dumps({"at": (now - timedelta(minutes=1)).isoformat(timespec="seconds"), "outcome": "failed",
                                                   "steps": [{"key": "session", "ok": True}, {"key": "contract", "ok": False}]}))

    class K:
        has_token = True

        def profile(self):
            return {}

        def instruments(self, exchange):
            return []

        def quote(self, keys):
            return {}
    r = ko.connection_test(c, K(), now, sleep=lambda s: None)        # allowed: nothing was placed last time
    assert r["outcome"] == "failed" and "no NIFTY option list" in r["steps"][-1]["text"]
    with get_db() as conn:                                            # a test that placed, a minute ago → the gap holds
        set_setting(conn, ko.TEST_KEY, json.dumps({"at": (now - timedelta(minutes=1)).isoformat(timespec="seconds"), "outcome": "passed",
                                                   "steps": [{"key": "place", "ok": True}]}))
    with pytest.raises(TipsError) as e:
        ko.connection_test(c, K(), now, sleep=lambda s: None)
    assert "one connection test per 5 minutes" in str(e.value)
