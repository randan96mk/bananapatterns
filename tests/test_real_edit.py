"""💰 Real trading, corrections by hand: edit an open position (qty / entry / stop), remove one from tracking, add a
hand-placed trade, edit a closed trade's exit; the maths of cash and charges, the rules following the new values
(EXIT quantity, the 5-minute stop check, the E+ trail that only ever raises), the order-log lines, and the routes
(including the dry run). Nothing here sends anything."""

import json

import pytest

from backend.database.models import get_db
from backend.engine import real
from backend.engine import trend_breakout as tb
from tests.test_real import Kite, Sender, first_day_with_buys, setup


def opened(monkeypatch, **kw):
    """An account with today's BUYs booked (estimated fills). Returns (config, data, today, first position's symbol)."""
    c = setup(monkeypatch, **kw)
    data, cut, end = first_day_with_buys(c)
    real.evening(c, cut)
    today = data["cal"][end + 1]
    real.send_orders(c, None, today, Sender())
    sym = real.summary(c)["positions"][0]["symbol"]
    return c, data, today, sym


def pos(sym):
    return real._state()["positions"][sym]


def log_lines():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT symbol, action, status, reason, qty, price FROM real_orders WHERE action != 'BUY' ORDER BY id")]


def test_edit_entry_moves_cash_and_charges_like_the_evening_correction(monkeypatch):
    c, data, today, sym = opened(monkeypatch)
    st, p = real._state(), pos(sym)
    old_px, old_ch, cash0 = p["px"], p["charges"], st["cash"]
    assert p["est"] is True
    new_px = round(old_px * 1.01, 2)
    out = real.edit_position(c, sym, entry=new_px, note="filled by hand")
    p, st = pos(sym), real._state()
    new_ch = tb.delivery_charges(p["qty"] * new_px, False)
    assert p["px"] == new_px and p["charges"] == pytest.approx(new_ch) and p["est"] is False
    assert p["px_orig"] == round(old_px, 2)
    assert st["cash"] == pytest.approx(cash0 - (new_px - old_px) * p["qty"] - (new_ch - old_ch))
    assert st["charges"] == pytest.approx(new_ch + sum(x["charges"] for s, x in st["positions"].items() if s != sym))
    assert out["position"]["buy"] == new_px and out["position"]["est"] is False and out["effects"]["est_cleared"]
    assert out["effects"]["exit_qty"] == p["qty"] and out["effects"]["trail_arms_at"] == pytest.approx(new_px * 2, abs=0.01)
    assert out["effects"]["pnl_after"] < out["effects"]["pnl_before"]                 # a higher entry: less profit
    assert [e["field"] for e in p["edits"]] == ["entry"] and p["edits"][0]["note"] == "filled by hand"
    line = log_lines()[-1]
    assert line["action"] == "EDIT" and line["status"] == "edited" and line["reason"].startswith(f"edited {sym}: entry ")
    assert "→" in line["reason"] and line["reason"].endswith("filled by hand · by you")
    real.edit_position(c, sym, entry=new_px * 1.02)                                     # the first entry is kept
    assert pos(sym)["px_orig"] == round(old_px, 2)
    row = next(x for x in real.summary(c)["positions"] if x["symbol"] == sym)
    assert row["px_orig"] == round(old_px, 2) and len(row["edits"]) == 2 and row["est"] is False
    # the evening no longer corrects it to the open (est was cleared)
    end = data["cal"].index(today)
    nxt = {**data, "cal": data["cal"][:end + 1], "index_close": data["index_close"][:end + 1],
           "index_ok": data["index_ok"][:end + 1], "filters": {k: v[:end + 1] for k, v in data["filters"].items()}}
    real.evening(c, nxt)
    assert pos(sym)["px"] == pytest.approx(new_px * 1.02)


def test_edit_qty_and_the_exit_sells_the_new_quantity(monkeypatch):
    c, data, today, sym = opened(monkeypatch)
    p = pos(sym)
    old_q, old_px, cash0 = p["qty"], p["px"], real._state()["cash"]
    st = real._state()                                                                  # tomorrow's planned EXIT
    st["orders"].append({"action": "EXIT", "symbol": sym, "ts": p["ts"], "qty": old_q, "why": "test"})
    real._save(st)
    out = real.edit_position(c, sym, qty=old_q + 3)
    p, st = pos(sym), real._state()
    assert p["qty"] == old_q + 3 and out["effects"]["exit_qty"] == old_q + 3
    assert st["cash"] == pytest.approx(cash0 - 3 * old_px - (tb.delivery_charges((old_q + 3) * old_px, False) - tb.delivery_charges(old_q * old_px, False)))
    assert next(o for o in st["orders"] if o["action"] == "EXIT")["qty"] == old_q + 3
    s = Sender()
    tomorrow = data["cal"][data["cal"].index(today) + 1]
    real.send_orders(c, Kite({p["ts"]: old_px}), tomorrow, s)
    assert (p["ts"], "EXIT", None) in s.calls                                            # EXIT closes the whole position
    t = real.summary(c)["trades"][0]
    assert t["symbol"] == sym and t["qty"] == old_q + 3 and "id" in t
    with get_db() as conn:
        assert conn.execute("SELECT qty FROM real_orders WHERE symbol = ? AND action = 'EXIT'", (sym,)).fetchone()[0] == old_q + 3


def test_edit_stop_is_manual_the_trail_only_raises_and_the_check_uses_it(monkeypatch):
    c, data, today, sym = opened(monkeypatch)
    p = pos(sym)
    new_stop = round(p["last"] * 0.97, 2)
    real.edit_position(c, sym, stop=new_stop)
    p = pos(sym)
    assert p["stop"] == new_stop and p["manual_stop"] is True and p.get("est") is True   # the entry is still estimated
    pe = real._rules(c, "E")
    assert tb.raise_late(dict(p), p["px"] * 1.5, pe) is None                            # under +100%: nothing moves
    q = dict(p)
    assert tb.raise_late(q, new_stop * 0.5, pe) is None and q["stop"] == new_stop        # never lowered
    q = dict(p)
    assert tb.raise_late(q, p["px"] * 2.5, pe) == pytest.approx(p["px"] * 2.5 * (1 - pe.late_pct / 100))
    assert q["stop"] > new_stop and q["manual_stop"] is True
    # the next day the 5-minute check exits at the edited stop
    end = data["cal"].index(today)
    nxt = {**data, "cal": data["cal"][:end + 1], "index_close": data["index_close"][:end + 1],
           "index_ok": data["index_ok"][:end + 1], "filters": {k: v[:end + 1] for k, v in data["filters"].items()}}
    real.evening(c, nxt)
    stop_now = pos(sym)["stop"]
    assert stop_now >= new_stop
    s = Sender()
    above = {x["ts"]: x["stop"] * 1.05 for x in real._state()["positions"].values()}
    assert real.watch_stops(c, Kite(above), "2099-01-02", s)["hits"] == []
    out = real.watch_stops(c, Kite({**above, p["ts"]: stop_now - 0.5}), "2099-01-02", s)
    assert [h["symbol"] for h in out["hits"]] == [sym] and s.calls == [(p["ts"], "EXIT", None)]


def test_edit_validation(monkeypatch):
    c, data, today, sym = opened(monkeypatch)
    p = pos(sym)
    for kw, msg in ((dict(qty=0), "qty"), (dict(qty=2.5), "whole"), (dict(entry=-1), "entry"), (dict(stop=p["last"]), "below the last"),
                    (dict(stop=p["last"] * 2), "below the last"), (dict(), "nothing to change"), (dict(qty="x"), "number")):
        with pytest.raises(ValueError, match=msg):
            real.edit_position(c, sym, **kw)
    with pytest.raises(ValueError, match="no open position"):
        real.edit_position(c, "NOPE", qty=1)
    assert pos(sym) == p and log_lines() == []                                          # nothing changed, nothing logged
    dry = real.edit_position(c, sym, entry=p["px"] * 1.1, dry=True)
    assert dry["dry"] and dry["position"]["buy"] == round(p["px"] * 1.1, 2) and pos(sym) == p and log_lines() == []


def test_remove_from_tracking_gives_the_cost_back(monkeypatch):
    c, data, today, sym = opened(monkeypatch)
    st, p = real._state(), pos(sym)
    cash0, ch0 = st["cash"], st["charges"]
    st["orders"].append({"action": "EXIT", "symbol": sym, "ts": p["ts"], "qty": p["qty"], "why": "test"})
    real._save(st)
    out = real.remove_position(c, sym, "never filled at the broker")
    st = real._state()
    assert sym not in st["positions"] and out["cash"] == pytest.approx(cash0 + p["qty"] * p["px"] + p["charges"], abs=0.01)
    assert st["charges"] == pytest.approx(ch0 - p["charges"])
    assert sym not in {x["symbol"] for x in real.summary(c)["positions"]} and real.summary(c)["trades"] == []
    assert not any(o["symbol"] == sym and o["action"] == "EXIT" for o in st["orders"])   # its planned EXIT is dropped
    line = log_lines()[-1]
    assert line["action"] == "REMOVE" and line["status"] == "removed" and line["reason"].startswith("removed from tracking: never filled at the broker")
    with pytest.raises(ValueError):
        real.remove_position(c, sym)
    s = Sender()                                                                        # a stop on the removed name sends nothing
    assert real.watch_stops(c, Kite({p["ts"]: 1.0}), today, s)["hits"] == [] and s.calls == []


def test_add_a_hand_placed_trade(monkeypatch):
    c, data, today, sym = opened(monkeypatch)
    st = real._state()
    held = set(st["positions"])
    new = next(s for s in sorted(data["S"]) if s not in held)
    rows = data["S"][new]["rows"]
    day, entry = rows[-5]["date"], round(rows[-5]["close"], 2)
    cash0, n0 = st["cash"], len(st["positions"])
    with pytest.raises(ValueError, match="not in the universe"):
        real.add_position(c, "NOPE", 5, 100, day, data=data)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        real.add_position(c, new, 5, entry, "3 Nov", data=data)
    with pytest.raises(ValueError, match="not enough cash"):
        real.add_position(c, new, 10_000_000, entry, day, data=data)
    c.breakout.max_pos = n0
    with pytest.raises(ValueError, match="no free slot"):
        real.add_position(c, new, 5, entry, day, data=data)
    c.breakout.max_pos = n0 + 1
    out = real.add_position(c, new, 5, entry, day, note="placed by hand", data=data)
    p = pos(new)
    j = next(k for k, r in enumerate(rows) if r["date"] == day)
    want = tb.stop_of(entry, data["S"][new]["ind"]["atr"][j], real._rules(c, "E"))
    assert p["stop"] == pytest.approx(want) and p["manual_stop"] is False and p["manual"] is True and p["est"] is False
    assert p["qty"] == 5 and p["px"] == entry and p["d"] == day and p["days"] == 4 and p["last"] == rows[-1]["close"]
    assert p["best"] >= max(entry, max(r["close"] for r in rows[-5:]))
    ch = tb.delivery_charges(5 * entry, False)
    assert real._state()["cash"] == pytest.approx(cash0 - 5 * entry - ch) and out["position"]["symbol"] == new
    assert new in {x["symbol"] for x in real.summary(c)["positions"]}
    line = log_lines()[-1]
    assert line["action"] == "ADD" and line["status"] == "added" and line["reason"].startswith(f"added {new}: 5 @ ")
    with pytest.raises(ValueError, match="already an open position"):
        real.add_position(c, new, 5, entry, day, data=data)
    # a given stop is kept and marked manual; one above the last close is refused
    c.breakout.max_pos = n0 + 2
    other = next(s for s in sorted(data["S"]) if s not in held and s != new)
    with pytest.raises(ValueError, match="below the last close"):
        real.add_position(c, other, 1, entry, day, stop=data["S"][other]["rows"][-1]["close"] * 2, data=data)
    real.add_position(c, other, 1, 100.0, day, stop=50.0, data=data)
    assert pos(other)["stop"] == 50.0 and pos(other)["manual_stop"] is True
    real.reset()
    with pytest.raises(ValueError, match="not started"):
        real.add_position(c, new, 5, entry, day, data=data)


def test_edit_a_closed_trade(monkeypatch):
    c, data, today, sym = opened(monkeypatch)
    tomorrow = data["cal"][data["cal"].index(today) + 1]
    p = pos(sym)
    st = real._state()
    st["orders"] = [{"action": "EXIT", "symbol": sym, "ts": p["ts"], "qty": p["qty"], "why": "test"}]
    real._save(st)
    real.send_orders(c, Kite({p["ts"]: p["px"] * 1.1}), tomorrow, Sender())
    t = real.summary(c)["trades"][0]
    cash0 = real._state()["cash"]
    assert t["est"] is True
    dry = real.edit_trade(c, t["id"], exit=t["sell"] - 10, dry=True)
    assert dry["dry"] and real.summary(c)["trades"][0]["sell"] == t["sell"] and real._state()["cash"] == cash0
    out = real.edit_trade(c, t["id"], exit=t["sell"] - 10)
    t2 = real.summary(c)["trades"][0]
    costs = tb.delivery_charges(t["qty"] * t["buy"], False) + tb.delivery_charges(t["qty"] * (t["sell"] - 10), True)
    assert t2["sell"] == round(t["sell"] - 10, 2) and t2["gross"] == pytest.approx((t2["sell"] - t["buy"]) * t["qty"], abs=0.01)
    assert t2["costs"] == pytest.approx(costs, abs=0.01) and t2["net"] == pytest.approx(t2["gross"] - costs, abs=0.01) and t2["est"] is False
    assert real._state()["cash"] == pytest.approx(cash0 + t2["net"] - t["net"], abs=0.01)
    assert out["effects"]["net_before"] == t["net"] and out["effects"]["net_after"] == t2["net"] and out["effects"]["est_cleared"]
    line = log_lines()[-1]
    assert line["reason"].startswith(f"edited trade {sym}: exit ") and line["status"] == "edited"
    real.edit_trade(c, t["id"], qty=t["qty"] + 1)
    assert real.summary(c)["trades"][0]["qty"] == t["qty"] + 1
    with pytest.raises(ValueError, match="no closed trade"):
        real.edit_trade(c, 999, exit=1)
    with pytest.raises(ValueError, match="nothing to change"):
        real.edit_trade(c, t["id"])
    # the evening no longer corrects the exit to the open
    end = data["cal"].index(tomorrow)
    nxt = {**data, "cal": data["cal"][:end + 1], "index_close": data["index_close"][:end + 1],
           "index_ok": data["index_ok"][:end + 1], "filters": {k: v[:end + 1] for k, v in data["filters"].items()}}
    real.evening(c, nxt)
    assert real.summary(c)["trades"][0]["sell"] == round(t["sell"] - 10, 2)


def test_hand_lines_are_not_failed_orders(monkeypatch):
    from datetime import date
    from backend.engine import attention, overview
    c, data, today, sym = opened(monkeypatch)
    real.edit_position(c, sym, stop=pos(sym)["last"] * 0.9)
    with get_db() as conn:
        conn.execute("UPDATE real_orders SET day = ?", (date.today().isoformat(),))              # as if today
    assert not any("Not sent today" in text for _, text in attention._real(c, True))
    assert all(r["real"] != "edited" for r in real.reconcile(c)["rows"])
    tl = overview.timeline(date.today().isoformat(), c)
    assert [e["kind"] for e in tl if e["kind"] in ("edit", "failed")] == ["edit"]
    assert any(e["text"].startswith(f"edited {sym}: stop ") for e in tl)


def test_routes_including_the_dry_run(monkeypatch):
    from fastapi.testclient import TestClient
    c, data, today, sym = opened(monkeypatch)
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        d = tc.get("/api/real").json()
        row = next(x for x in d["positions"] if x["symbol"] == sym)
        assert row["est"] is True and row["edits"] == [] and d["enabled"] is True and d["killed"] is False
        r = tc.post(f"/api/real/position/{sym}?dry=1", json={"entry": row["buy"] + 5}).json()
        assert r["dry"] is True and r["effects"]["exit_qty"] == row["qty"] and r["position"]["buy"] == round(row["buy"] + 5, 2)
        assert next(x for x in tc.get("/api/real").json()["positions"] if x["symbol"] == sym)["est"] is True   # not saved
        assert tc.post(f"/api/real/position/{sym}", json={"stop": row["last"] + 1}).status_code == 400
        assert tc.post("/api/real/position/NOPE", json={"qty": 1}).status_code == 400
        r = tc.post(f"/api/real/position/{sym}", json={"entry": row["buy"] + 5, "note": "by hand"}).json()
        assert r["dry"] is False and r["position"]["est"] is False
        d = tc.get("/api/real").json()
        row2 = next(x for x in d["positions"] if x["symbol"] == sym)
        assert row2["buy"] == round(row["buy"] + 5, 2) and row2["px_orig"] == row["buy"] and row2["edits"][0]["field"] == "entry"
        assert d["log"][0]["action"] == "EDIT" and d["log"][0]["status"] == "edited" and "by hand" in d["log"][0]["reason"]
        assert d["enabled"] is True and d["killed"] is False                           # the switches are untouched
        other = next(x["symbol"] for x in d["positions"] if x["symbol"] != sym) if len(d["positions"]) > 1 else None
        if other:
            assert tc.request("DELETE", f"/api/real/position/{other}", json={"note": "never filled"}).json()["status"] == "ok"
            assert other not in {x["symbol"] for x in tc.get("/api/real").json()["positions"]}
        assert tc.request("DELETE", "/api/real/position/NOPE", json={"note": ""}).status_code == 400
        held = {x["symbol"] for x in tc.get("/api/real").json()["positions"]}
        new = next(s for s in sorted(data["S"]) if s not in held)
        rows = data["S"][new]["rows"]
        body = {"symbol": new.lower(), "qty": 2, "entry": rows[-3]["close"], "day": rows[-3]["date"]}
        assert tc.post("/api/real/position", json={**body, "symbol": "NOPE"}).status_code == 400
        r = tc.post("/api/real/position", json=body).json()
        assert r["position"]["symbol"] == new and r["position"]["manual"] is True
        assert tc.post("/api/real/trade/999", json={"exit": 1}).status_code == 400
        assert json.dumps(tc.get("/api/real").json()).count('"EDIT"') >= 1


def test_send_gap_setting_spaces_the_webhooks(monkeypatch):
    """real.send_gap_s is the pause between two webhooks (floor 1 s, cap 30 s)."""
    from backend.config import AppConfig
    from backend.broker import tradetron
    from backend.engine import real
    cfg = AppConfig()
    cfg.real.send_gap_s = 7
    assert real._gap(cfg) == 7.0
    cfg.real.send_gap_s = 0.2
    assert real._gap(cfg) == 1.0
    cfg.real.send_gap_s = 99
    assert real._gap(cfg) == 30.0
    waits = []
    monkeypatch.setattr(tradetron.time, "sleep", lambda s: waits.append(s))
    monkeypatch.setattr(tradetron.time, "monotonic", lambda: 100.0)
    tradetron._last_sent[0] = 100.0
    tradetron._pace(7)
    assert waits == [7.0]
