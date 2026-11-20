"""⚡ Tips → Kite orders (PR A, click mode): a fake broker with place / modify / cancel / orders; the whole flow (entry →
fill → stop + T1 → T1 fill → stop to cost + T2 → T2 fill → stop cancelled), a rejection, the kill switch, every guard,
the caps and the loss cap, the routes, 12-hour strings, no broker name past "Kite", phone-safe markup."""

import re
from pathlib import Path

import pytest

from backend.config import AppConfig, IngestConfig, KiteConfig
from backend.engine import health
from backend.kite_api.client import KiteClient, KiteRequestError
from backend.tips import book as bk
from backend.tips import kite_orders as ko
from backend.tips import track as tr
from backend.tips.book import TipsError
from tests.tips_world import BN12, KEY12, FakeClient, at, cfg, seed, world

ROOT = Path(__file__).resolve().parent.parent
NOPLAN = "Buy bank nifty 50300 PE at 260 add more at 240"


class Broker(FakeClient):
    """The tips world's market data plus an order book: place / modify / cancel / orders, fills by hand, and one order of
    the owner's own that the app must never show or touch."""

    def __init__(self, base: FakeClient):
        super().__init__(base.dumps, base.prices, base.has_token)
        self.book = {"999": {"order_id": "999", "status": "OPEN", "tag": None, "tradingsymbol": "INFY", "quantity": 5,
                             "transaction_type": "BUY", "average_price": 0, "filled_quantity": 0}}
        self.n = 0
        self.placed, self.modified, self.cancelled = [], [], []
        self.refuse = None

    def place_order(self, **kw):
        if self.refuse:
            raise RuntimeError(self.refuse)
        self.n += 1
        oid = f"2511190004{self.n:02d}"
        self.book[oid] = {"order_id": oid, "status": "OPEN", "average_price": 0, "filled_quantity": 0, **kw}
        self.placed.append({**kw, "order_id": oid})
        return oid

    def modify_order(self, order_id, **kw):
        self.modified.append((order_id, kw))
        self.book[order_id].update({("quantity" if k == "quantity" else k): v for k, v in kw.items()})
        return order_id

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        self.book[order_id]["status"] = "CANCELLED"
        return order_id

    def orders(self):
        self.calls.append(("orders",))
        return [dict(o) for o in self.book.values()]

    def fill(self, oid, px):
        self.book[oid].update(status="COMPLETE", average_price=px, filled_quantity=self.book[oid]["quantity"])

    def reject(self, oid, why):
        self.book[oid].update(status="REJECTED", status_message=why)

    def last(self, **match):
        return [p for p in self.placed if all(p.get(k) == v for k, v in match.items())][-1]


@pytest.fixture
def pushed(monkeypatch):
    out = []
    monkeypatch.setattr(bk, "push", lambda config, key, html: out.append(html) or True)
    return out


def setup(mode="click", text=BN12, **cfgkw):
    c = cfg()
    c.tips.kite_mode = mode
    for k, v in cfgkw.items():
        setattr(c.tips, k, v)
    b = Broker(world(**{KEY12: 262.0}))
    hid = seed(c, b, text)["hint_id"]
    return c, b, hid


def test_whole_flow_entry_stop_watched_t1_cost_t2(pushed):
    c, b, hid = setup()
    h = ko.place_all(c, b, hid, 2, now=at("10:00"))
    e = b.last(transaction_type="BUY")
    assert e["order_type"] == "LIMIT" and e["price"] == 270 and e["quantity"] == 30   # PR C: a zone rests as a LIMIT at its top
    assert e["exchange"] == "NFO" and e["tradingsymbol"] == "BANKNIFTY26NOV50300PE" and e["product"] == "NRML" and e["tag"] == "bptips"
    assert h["kite"]["state"] == "armed" and h["kite"]["auto_rest"]
    b.fill(e["order_id"], 262.0)
    r = ko.sync(c, b, at("10:00", sec=10))
    assert r["lines"] == ["⚡ Kite · #1 entry filled 262 ×30 · SL 190 placed · T1 320 watched by the app"]
    sl = b.last(order_type="SL")
    assert sl["trigger_price"] == 190.0 and sl["quantity"] == 30 and sl["transaction_type"] == "SELL"
    assert len(b.placed) == 2                                  # only the stop rests on Kite
    rows = {x["leg"]: x for x in ko.rows_for(hid)}
    assert rows["stop"]["by"] == "app" and rows["entry"]["by"] == "click" and rows["entry"]["status"] == "complete"
    b.prices[KEY12] = 321.0
    tr.poll(c, b, at("10:20"))
    ko.watch_all(c, b, at("10:20"))
    m1 = b.placed[-1]
    assert sl["order_id"] in b.cancelled and (m1["order_type"], m1["quantity"], m1["transaction_type"]) == ("MARKET", 15, "SELL")
    b.fill(m1["order_id"], 321.0)
    r = ko.sync(c, b, at("10:20", sec=5))
    assert r["lines"] == ["⚡ Kite · #1 T1 hit 321 · sold 15 of 30 at market · stop → cost 262 for the rest · watching T2 350"]
    sl2 = b.placed[-1]
    assert (sl2["order_type"], sl2["trigger_price"], sl2["quantity"]) == ("SL", 262.0, 15)
    b.prices[KEY12] = 351.0
    tr.poll(c, b, at("11:00"))
    ko.watch_all(c, b, at("11:00"))
    m2 = b.placed[-1]
    assert sl2["order_id"] in b.cancelled and (m2["order_type"], m2["quantity"]) == ("MARKET", 15)
    b.fill(m2["order_id"], 351.0)
    r = ko.sync(c, b, at("11:00", sec=5))
    assert r["lines"][0] == "⚡ Kite · #1 T2 hit 351 · sold 15 at market · +₹1,335 · closed +₹2,220 total"
    k = bk.load(hid)["kite"]
    assert k["state"] == "closed" and k["qty_open"] == 0 and k["realised"] == 2220.0
    assert ko.realised_today("2026-11-17") == 2220.0
    assert not [p for p in b.placed if p["order_type"] == "LIMIT" and p["transaction_type"] == "SELL"]   # never a resting target
    with pytest.raises(TipsError, match="never re-entered"):   # one position per hint, never again
        ko.place_entry(c, b, hid, 1, now=at("11:05"))
    assert "999" not in {o["order_id"] for o in ko.today(c, b, at("11:05"))["orders"]}   # only ours
    assert ("999",) not in b.cancelled


def test_stop_fill_cancels_targets_and_counts_the_loss(pushed):
    c, b, hid = setup(kite_loss_cap=1000.0)
    ko.place_all(c, b, hid, 2, now=at("10:00"))
    b.fill(b.last(transaction_type="BUY")["order_id"], 262.0)
    ko.sync(c, b, at("10:01"))
    sl = b.last(order_type="SL")
    b.fill(sl["order_id"], 190.0)
    r = ko.sync(c, b, at("10:30"))
    assert r["lines"] == ["⚡ Kite · #1 stop filled 190 ×30 · −₹2,160"]
    assert bk.load(hid)["kite"]["state"] == "closed"
    t = ko.today(c, b, at("10:31"))
    assert t["caps"]["used_loss"] == -2160.0 and t["net"] == -2160.0
    hid2 = seed(c, b, BN12.replace("50300", "51000"), "10:30", 2)["hint_id"]
    with pytest.raises(TipsError, match="loss today"):          # at / past the loss cap: no new entry
        ko.place_entry(c, b, hid2, 1, now=at("10:40"))


def test_rejection_is_reported_and_the_hint_can_be_tried_again(pushed):
    c, b, hid = setup()
    ko.place_entry(c, b, hid, 1, now=at("10:00"))
    b.reject(b.last(transaction_type="BUY")["order_id"], "Insufficient margin")
    r = ko.sync(c, b, at("10:00", sec=10))
    assert r["lines"] == ["⚠️ Kite · #1 entry REJECTED · Insufficient margin"]
    k = bk.load(hid)["kite"]
    assert k["state"] == "none" and k["legs"]["entry"]["status"] == "rejected"
    assert "entry" in tr.hint_view(bk.load(hid), config=c)["kite"]["actions"]
    assert ko.today(c, b, at("10:01"))["caps"]["entries"] == 0     # a rejected entry does not use the cap
    b.refuse = "Markets are closed right now"
    with pytest.raises(TipsError, match="Kite refused the entry"):
        ko.place_entry(c, b, hid, 1, now=at("10:02"))
    assert ko.rows_for(hid)[-1]["status"] == "failed"


def test_kill_switch_cancels_and_exits(pushed):
    c, b, hid = setup()
    ko.place_all(c, b, hid, 2, now=at("10:00"))
    b.fill(b.last(transaction_type="BUY")["order_id"], 262.0)
    ko.sync(c, b, at("10:01"))
    sl = b.last(order_type="SL")
    out = ko.set_kill(c, b, True, at("10:05"))
    assert out == {"kill": True, "cancelled": 1, "exits": 1, "errors": []}
    assert sl["order_id"] in b.cancelled
    ex = b.placed[-1]
    assert ex["order_type"] == "MARKET" and ex["transaction_type"] == "SELL" and ex["quantity"] == 30
    assert pushed[-1].startswith("⛔ Kite · kill switch ON · 1 orders cancelled · 1 exits at market")
    hid2 = seed(c, b, BN12.replace("50300", "51000"), "10:30", 2)["hint_id"]
    with pytest.raises(TipsError, match="kill switch ON"):
        ko.place_entry(c, b, hid2, 1, now=at("10:40"))
    b.fill(ex["order_id"], 250.0)
    r = ko.sync(c, b, at("10:06"))
    assert r["lines"][0].startswith("⚡ Kite · #1 exit filled 250 ×30 · −₹360") and bk.load(hid)["kite"]["state"] == "closed"
    ko.set_kill(c, b, False, at("10:41"))
    assert ko.place_entry(c, b, hid2, 1, now=at("10:42"))["kite"]["state"] == "armed"


def test_every_guard(pushed):
    c, b, hid = setup(kite_max_entries_day=1)
    texts = lambda now, lots=1, client=b, h=None: {g["key"]: g for g in ko.guards(c, client, h or bk.load(hid), lots, now)}
    assert all(g["ok"] for g in texts(at("10:00")).values())
    assert not texts(at("10:00"), client=FakeClient(has_token=False))["login"]["ok"]
    assert not texts(at("16:00"))["market"]["ok"] and "closed now" in texts(at("16:00"))["market"]["text"]
    assert not texts(at("09:16"))["window"]["ok"] and texts(at("09:16"))["window"]["text"] == "entries only 9:20 AM – 2:30 PM"
    assert not texts(at("14:45"))["window"]["ok"]
    assert not texts(at("10:00"), lots=3)["lots"]["ok"] and texts(at("10:00"), lots=3)["lots"]["text"] == "lots per order 3 > the cap 2"
    c.tips.kite_mode = "off"
    assert not texts(at("10:00"))["mode"]["ok"]
    with pytest.raises(TipsError):
        ko.place_entry(c, b, hid, 1, now=at("10:00"))
    c.tips.kite_mode = "auto"                                   # auto == click in this PR
    assert texts(at("10:00"))["mode"]["ok"]
    ko.set_kill(c, None, True)
    assert not texts(at("10:00"))["kill"]["ok"]
    ko.set_kill(c, None, False)
    with pytest.raises(TipsError, match="lots per order 3"):
        ko.place_entry(c, b, hid, 3, now=at("10:00"))
    ko.place_entry(c, b, hid, 1, now=at("10:00"))
    assert not texts(at("10:01"))["one"]["ok"] and texts(at("10:01"))["one"]["text"] == "already on Kite · never averaged"
    hid2 = seed(c, b, BN12.replace("50300", "51000"), "10:30", 2)["hint_id"]
    with pytest.raises(TipsError, match="entries today 1 of 1"):
        ko.place_entry(c, b, hid2, 1, now=at("10:40"))
    hid3 = seed(c, b, NOPLAN, "10:31", 3)["hint_id"]
    g = texts(at("10:40"), h=bk.load(hid3))
    assert not g["plan"]["ok"] and g["plan"]["text"].startswith("no complete plan")
    h = bk.load(hid2)
    h["status"] = "closed"
    assert not texts(at("10:40"), h=h)["open"]["ok"]
    with pytest.raises(TipsError, match="needs a filled entry"):     # the stop / the rest need a filled entry
        ko.place_stop(c, b, hid, now=at("10:02"))
    with pytest.raises(TipsError, match="needs a filled entry"):
        ko.place_rest(c, b, hid, now=at("10:02"))
    assert not hasattr(ko, "place_targets") and not hasattr(ko, "_place_t1")      # the resting-target path is gone


def test_click_legs_one_by_one_spot_stop_modify_cancel_exit(pushed):
    c, b, hid = setup()
    ko.place_entry(c, b, hid, 2, now=at("10:00"))
    assert ko.sync(c, b, at("10:00", sec=5))["lines"] == []          # still pending: nothing to say
    b.fill(b.last(transaction_type="BUY")["order_id"], 262.0)
    assert ko.sync(c, b, at("10:01"))["lines"] == ["⚡ Kite · #1 entry filled 262 ×30"]   # no Place all: nothing more
    assert b.last(transaction_type="BUY")["quantity"] == 30 and len(b.placed) == 1
    v = tr.hint_view(bk.load(hid), config=c)["kite"]
    assert {"rest", "stop", "exit"} <= set(v["actions"]) and "targets" not in v["actions"]
    ko.place_stop(c, b, hid, now=at("10:02"))
    assert "rest" not in tr.hint_view(bk.load(hid), config=c)["kite"]["actions"]
    b.prices[KEY12] = 280.0
    tr.poll(c, b, at("10:03"))                                  # the quote the stop is checked against
    with pytest.raises(TipsError, match="wrong side"):
        ko.modify_stop(c, b, hid, 300, now=at("10:03"))
    h = ko.modify_stop(c, b, hid, "cost", now=at("10:03"))
    assert h["kite"]["stop_px"] == 262.0 and b.modified[-1][1] == {"order_type": "SL", "trigger_price": 262.0, "price": 258.05}
    with pytest.raises(TipsError, match="no target 1 working"):     # a watched target has no order to cancel
        ko.cancel(c, b, hid, "t1", now=at("10:04"))
    h = ko.exit_now(c, b, hid, now=at("10:05"))
    assert b.placed[-1]["order_type"] == "MARKET" and b.placed[-1]["quantity"] == 30
    assert h["kite"]["legs"]["stop"]["status"] == "cancelled" and h["kite"]["legs"]["exit"]["status"] == "placed"
    # a spot stop cannot go on Kite
    hid2 = seed(c, b, BN12.replace("50300", "51000"), "10:30", 2)["hint_id"]
    h2 = bk.load(hid2)
    h2["plan"]["stop"] = {"kind": "spot", "value": 50600, "dir": "above", "tf": 15}
    bk.save(h2)
    legs = {x["leg"]: x for x in tr.hint_view(bk.load(hid2), config=c)["kite"]["legs"]}
    assert legs["stop"]["detail"] == "spot stop: watched by the app, exit by hand" and not legs["stop"]["placeable"]


def test_one_lot_is_not_split_and_views_are_12_hour(pushed):
    c, b, hid = setup()
    v = tr.hint_view(bk.load(hid), config=c)["kite"]
    legs = {x["leg"]: x for x in v["legs"]}
    assert set(legs) == {"entry", "stop", "t1"} and legs["t1"]["qty"] == 15 and legs["t1"]["detail"].startswith("watched by the app · all")
    assert legs["stop"]["detail"] == "max loss −₹1,200" and legs["entry"]["detail"] == "1 lot · NRML · ≈ ₹4,050"   # no quote yet: the zone top
    assert v["beyond_t2"] == 1 and v["actions"] == ["all", "entry"]
    ko.place_entry(c, b, hid, 2, now=at("10:07", sec=14))
    t = ko.today(c, b, at("10:08"))
    assert t["window"] == "9:20 AM – 2:30 PM" and t["orders"][0]["time"] == "10:07 AM" and t["orders"][0]["time_s"] == "10:07:14 AM"
    assert t["orders"][0]["label"] == "Entry BANKNIFTY 50300 PE" and t["caps"] == {"entries": 0, "max_entries": 4, "max_lots": 2,
                                                                                   "loss_cap": 6000.0, "used_loss": 0.0}
    legs = {x["leg"]: x for x in tr.hint_view(bk.load(hid), config=c)["kite"]["legs"]}
    assert legs["entry"]["at"] == "10:07:14 AM" and set(legs) == {"entry", "stop", "t1", "t2"}
    blob = repr(t) + repr(legs)
    assert not re.search(r"\b(1[3-9]|2[0-3]):\d\d\b", blob)              # no 24-hour times on screens


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
    b = Broker(world(**{KEY12: 262.0}))
    monkeypatch.setattr(ko, "_now", lambda: at("10:00"))
    with TestClient(main.app) as api:
        routes._state.client = b
        seed(routes._state.config, b, BN12)
        t = api.get("/api/tips/kite").json()
        assert set(t) >= {"mode", "kill", "caps", "orders", "guards", "open", "net", "last_sync", "window", "warning"}
        assert t["mode"] == "off" and api.get("/api/tips").json()["kite"]["mode"] == "off"
        assert api.post("/api/tips/hints/1/kite/all", json={"lots": 2}).status_code == 409
        assert api.put("/api/tips/settings", json={"kite_mode": "maybe"}).status_code == 422
        assert api.put("/api/tips/settings", json={"kite_product": "CNC"}).status_code == 422
        assert api.put("/api/tips/settings", json={"kite_entry_from": "25:00"}).status_code == 422
        assert api.put("/api/tips/settings", json={"kite_entry_from": "15:00"}).status_code == 422
        assert api.put("/api/tips/settings", json={"kite_mode": "click"}).json()["kite_mode"] == "click"
        assert api.post("/api/tips/hints/1/kite/nope").status_code == 404
        assert api.post("/api/tips/hints/99/kite/entry", json={}).status_code == 404
        r = api.post("/api/tips/hints/1/kite/all", json={"lots": 2})
        assert r.status_code == 200 and r.json()["kite"]["state"] == "armed"
        assert api.post("/api/tips/hints/1/kite/entry", json={}).status_code == 409
        assert api.post("/api/tips/hints/1/kite/modify_stop", json={}).status_code in (400, 409)
        b.fill(b.last(transaction_type="BUY")["order_id"], 262.0)
        r = api.post("/api/tips/kite/resync").json()
        assert r["changed"] == 1 and [o["leg"] for o in r["orders"]] == ["stop", "entry"]
        h = api.get("/api/tips/hints/1").json()
        assert {x["leg"]: x["status"] for x in h["kite"]["legs"]} == {"entry": "complete", "stop": "placed", "t1": "watched", "t2": "watched"}
        assert api.post("/api/tips/hints/1/kite/targets").status_code == 404           # no resting-target action any more
        assert api.post("/api/tips/hints/1/kite/modify_stop", json={"to": "cost"}).json()["kite"]["stop_px"] == 262.0
        assert api.post("/api/tips/hints/1/kite/cancel", json={"leg": "x"}).status_code == 400
        k = api.post("/api/tips/kite/kill", json={"on": True}).json()
        assert k["kill"] and k["exits"] == 1 and k["cancelled"] == 1           # the stop only: the targets are watched
        assert api.get("/api/tips").json()["kite"]["kill"] is True
    rows = {r["key"]: r for r in health.checks(AppConfig(), has_token=False)}
    assert rows["tips_kite"]["state"].startswith("off · kill switch ON · ")


def test_client_order_calls_are_sent_once():
    calls = []

    class SDK:
        def place_order(self, **kw):
            calls.append(kw)
            if kw["quantity"] == 99:
                raise ConnectionError("reset")
            return "123"

        def modify_order(self, **kw):
            calls.append(kw)
            return {"order_id": kw["order_id"]}

        def cancel_order(self, **kw):
            calls.append(kw)
            return {"order_id": kw["order_id"]}

        def orders(self):
            return [{"order_id": "123"}]

    cl = KiteClient(KiteConfig(api_key="k", access_token="t"), IngestConfig(backoff_base_sec=0.0), kite=SDK(), sleep=lambda s: None)
    assert cl.place_order("NFO", "X", "BUY", 30, "NRML", "MARKET") == "123"
    assert calls[-1] == {"variety": "regular", "exchange": "NFO", "tradingsymbol": "X", "transaction_type": "BUY", "quantity": 30,
                         "product": "NRML", "order_type": "MARKET", "validity": "DAY", "tag": "bptips"}
    n = len(calls)
    with pytest.raises(KiteRequestError):
        cl.place_order("NFO", "X", "BUY", 99, "NRML", "MARKET")
    assert len(calls) == n + 1                                          # never retried
    assert cl.modify_order("123", trigger_price=262.0, price=None) == "123" and calls[-1] == {"variety": "regular", "order_id": "123", "trigger_price": 262.0}
    assert cl.cancel_order("123") == "123" and cl.orders() == [{"order_id": "123"}]


def test_no_broker_name_past_kite_and_phone_safe_markup():
    src = "\n".join((ROOT / f"frontend/src/components/{f}").read_text(encoding="utf-8") for f in ("TipsPage.jsx", "tipsKite.js"))   # the confirms' words are shared with Home
    eng = (ROOT / "backend/tips/kite_orders.py").read_text(encoding="utf-8")
    for s in (src, eng):
        assert not re.search(r"zerodha", s, re.I)
    assert "Place all on Kite" in src and "Real money on your Kite account" in src and "Kite orders today" in src
    assert "By my click" in src and "entries armed on the level" in src
    table = src[src.index("Kite orders today"):]
    for col in ("'Type'", "'Price'", "'Order id'", "'By'"):
        assert re.search(r"\{ h: " + col + r"[^}]*lo: true", table), col          # phones keep Time / Hint / Leg / Side / Qty / Status
    css = (ROOT / "frontend/src/redesign.css").read_text(encoding="utf-8")
    assert ".rd .tp-legs" in css and re.search(r"@media \(max-width: 700px\)[^@]*\.rd \.tp-legs \{ grid-template-columns: minmax\(0, 1fr\)", css)


def test_market_orders_carry_market_protection_and_limits_do_not():
    from backend.config import AppConfig
    from backend.kite_api.client import KiteClient
    sent = []

    class K:
        def place_order(self, **kw):
            sent.append(kw)
            return {"order_id": "1"}
    c = AppConfig()
    kc = KiteClient(c.kite, c.ingest, kite=K(), sleep=lambda s: None)
    kc.place_order(exchange="BFO", tradingsymbol="X", transaction_type="BUY", quantity=20, product="NRML", order_type="MARKET", market_protection=5.0)
    kc.place_order(exchange="BFO", tradingsymbol="X", transaction_type="SELL", quantity=20, product="NRML", order_type="LIMIT", price=520.0)
    assert sent[0]["market_protection"] == 5.0 and "market_protection" not in sent[1] and sent[0]["tag"] == "bptips"
    assert c.tips.kite_market_protection == 5.0
