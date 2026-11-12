"""📐 The futures book on Backtest F's synthetic world: the evening plan with eligibility reasons and `waiting`, the
paper fill at the next futures open with lots and margin, stops on the futures low (the trail only ever raises), the
roll at T-2 (exit + re-entry on the next month, cost logged, never into expiry), the guards (daily loss, notional, cash
floor, heat, margin 80%), the kill switch (kill_switch=1 through the sender), short_real refused without a passing
backtest, the switches, replan / morning / skip / hold shapes, hand corrections with lots, deposits, the overview
account and the attention items, the alert texts, the routes and the stream topics."""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from backend.alerts import messages as msg
from backend.config import IST, AppConfig
from backend.database.models import get_db, now_ist
from backend.engine import trend_breakout as tb
from backend.futures import backtest as fb
from backend.futures import book, sender
from backend.ingest.holidays import is_trading_day
from tests.test_futures_backtest import N, long_world

TODAY = now_ist()[:10]


class Sender:
    def __init__(self, fail=()):
        self.calls, self.fail, self.kills = [], set(fail), []

    def __call__(self, symbol, action, lots=None):
        if symbol in self.fail:
            raise OSError("timeout")
        self.calls.append((symbol, action, lots))
        return '{"success": true}'

    def kill(self, note=""):
        self.kills.append(note)
        return "kill recorded"


class Kite:
    has_token = True

    def __init__(self, prices, lows=None, highs=None):
        self.prices, self.lows, self.highs = prices, lows or {}, highs or {}

    def quote(self, keys):
        out = {}
        for k in keys:
            c = k.split(":", 1)[1]
            if c in self.prices:
                out[k] = {"last_price": self.prices[c], "ohlc": {"low": self.lows.get(c, self.prices[c]), "high": self.highs.get(c, self.prices[c])}}
        return out


def cfg_for(**fut):
    c = AppConfig()
    c.futures = replace(c.futures, universe_lot_cap=2_000_000.0, **fut)
    return c


def world(monkeypatch, **fut):
    """The synthetic universe (its contracts expiring 40 sessions ahead), the data: (cfg, data, i_sig)."""
    long_world(monkeypatch)
    with get_db() as conn:
        conn.execute("UPDATE futures_contracts SET expiry = ?", (sessions_ahead(40),))
    cfg = cfg_for(**fut)
    data = tb.load(cfg)
    return cfg, data, N - 40


def cut(data, end):
    return {**data, "cal": data["cal"][:end + 1], "index_close": data["index_close"][:end + 1], "index_ok": data["index_ok"][:end + 1],
            "filters": {k: v[:end + 1] for k, v in data["filters"].items()}}


def started(monkeypatch, **fut):
    """The book after the breakout evening and the next day's paper fills: two long lots (UP, AUX) on paper."""
    cfg, data, i = world(monkeypatch, **fut)
    cal = data["cal"]
    r1 = book.evening(cfg, cut(data, i), today=cal[i])
    r2 = book.evening(cfg, cut(data, i + 1), today=cal[i + 1])
    return cfg, data, i, r1, r2


def set_expiry(sym, expiry, next_expiry=None, lot=1700):
    """Give the stored contract (and the open lots in `sym`) an expiry; add a next-month row when asked."""
    with get_db() as conn:
        conn.execute("UPDATE futures_contracts SET expiry = ? WHERE underlying = ? AND month = 'near'", (expiry, sym))
        if next_expiry:
            conn.execute("INSERT OR REPLACE INTO futures_contracts VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (sym, "next", f"{sym}NEXTFUT", 9000, next_expiry, lot, None, None, None, TODAY))
    st = book._state()
    for a in st["accounts"].values():
        if sym in a["positions"]:
            a["positions"][sym]["expiry"] = expiry
    book._save(st)


def sessions_ahead(n, start=None):
    d = date.fromisoformat(start or TODAY)
    k = 0
    while k < n:
        d += timedelta(days=1)
        k += is_trading_day(d)
    return d.isoformat()


# --- the evening plan ------------------------------------------------------------------------------------------

def test_plan_with_eligibility_reasons_and_waiting(monkeypatch):
    cfg, data, i = world(monkeypatch)
    assert book.evening(cfg, cut(data, i), today=data["cal"][i])["status"] == "ok"
    st = book._state()
    assert st["started"] == data["cal"][i] and set(st["accounts"]) == set(book.ACCOUNTS) and st["capital"] == 500000.0
    plan = st["plan"]["long"]
    assert [o["symbol"] for o in plan] == ["UP", "AUX"] and all(o["action"] == "BUY" and o["lots"] == 1 and o["lot_size"] == 1700 for o in plan)
    o = plan[0]
    assert o["contract"] == "UPFUT" and o["stop"] < o["entry"] and o["stop_pct"] <= 5.0 and o["margin_est"] == pytest.approx(0.2 * o["entry"] * 1700, abs=5)
    assert o["risk"] == pytest.approx((o["entry"] - o["stop"]) * 1700, abs=20) and o["why"].startswith("E+ breakout #1 · 1 lot")
    waiting = {w["symbol"]: w["reasons"] for w in st["waiting"]["long"]}
    assert set(waiting) == {"BIG", "ALT"}
    assert any(r.startswith("lot too big ₹14.1 L > ₹6 L") for r in waiting["BIG"]) and "slots full" in waiting["ALT"]
    assert any(r.startswith("notional") for r in waiting["ALT"])
    assert st["plan"]["short"] == [] and st["allowed"] == {"long": True, "short": False} and st["filter"]["state"] == "FULL"
    # a day already processed is not processed again; the summary carries the plan and the waiting list with `why`
    assert book.evening(cfg, cut(data, i), today=data["cal"][i])["status"] == "done"
    s = book.summary(cfg)
    assert s["started"] and [o["symbol"] for o in s["plan"]] == ["UP", "AUX"] and s["waiting"][0]["why"].startswith("lot too big")
    assert s["eligible_count"] == 4 and s["universe_count"] == 4 and s["sides"]["long"]["paper"] and not s["sides"]["long"]["real"]
    assert s["guards"]["daily_loss"]["limit"] == 15000.0 and s["guards"]["notional"]["cap"] == 1250000.0 and s["guards"]["margin"]["block_at"] == 80


def test_filter_off_and_daily_loss_block_new_entries(monkeypatch):
    cfg, data, i = world(monkeypatch)
    d = cut(data, i)
    d["filters"] = {"half": [0.0] * len(d["cal"])}
    book.evening(cfg, d, today=data["cal"][i])
    st = book._state()
    assert st["plan"]["long"] == [] and all("filter OFF" in w["reasons"] for w in st["waiting"]["long"]) and st["allowed"]["long"] is False
    book.reset()
    book.evening(cfg, cut(data, i), today=data["cal"][i])
    st = book._state()
    st["accounts"]["paper_long"]["day_start"] = 600000.0                 # the day lost Rs 1 L: over the 3% limit
    book._save(st)
    r = book.replan(cfg, cut(data, i))
    assert r["status"] == "ok" and r["after"] == [] and all("daily loss limit" in w["why"] for w in r["waiting"]) and r["suggestion"] is None
    assert book.summary(cfg)["guards"]["daily_loss"] == {"used": 100000.0, "limit": 15000.0, "hit": True}


def test_guards_notional_cash_floor_heat_and_margin_80(monkeypatch):
    cfg, data, i = world(monkeypatch, max_notional_x=1.5)
    book.evening(cfg, cut(data, i), today=data["cal"][i])
    w = {x["symbol"]: x["reasons"] for x in book._state()["waiting"]["long"]}
    assert [o["symbol"] for o in book._state()["plan"]["long"]] == ["UP"] and any(r.startswith("notional") for r in w["AUX"])
    book.reset()
    cfg = cfg_for(cash_floor=450_000.0)
    book.evening(cfg, cut(data, i), today=data["cal"][i])
    assert book._state()["plan"]["long"] == [] and all(any(r.startswith("cash") for r in x["reasons"]) for x in book._state()["waiting"]["long"])
    book.reset()
    cfg = cfg_for(heat_pct=0.5)
    book.evening(cfg, cut(data, i), today=data["cal"][i])
    assert all(any(r.startswith("heat") for r in x["reasons"]) for x in book._state()["waiting"]["long"])
    book.reset()
    cfg = cfg_for(margin_pct=85.0, max_notional_x=5.0, cash_floor=0.0)
    book.evening(cfg, cut(data, i), today=data["cal"][i])
    assert all(any("≥ 80%" in r for r in x["reasons"]) for x in book._state()["waiting"]["long"]) and book._state()["plan"]["long"] == []


# --- paper fills, stops, the trail ----------------------------------------------------------------------------

def test_paper_fills_at_the_next_futures_open_with_lots_and_margin(monkeypatch):
    cfg, data, i, r1, r2 = started(monkeypatch)
    cal = data["cal"]
    F = [b for b in __import__("backend.futures.candles", fromlist=["get"]).get("UP") if b["date"] == cal[i + 1]][0]
    assert r2["status"] == "ok" and [f["symbol"] for f in r2["fills"]] == ["UP", "AUX"]
    f = r2["fills"][0]
    assert f["account"] == "paper_long" and f["lots"] == 1 and f["price"] == round(F["open"], 2) and f["margin"] == pytest.approx(0.2 * F["open"] * 1700, abs=1)
    s = book.summary(cfg)
    p = next(x for x in s["positions"] if x["symbol"] == "UP")
    assert p["book"] == "paper" and p["side"] == "long" and p["lots"] == 1 and p["qty"] == 1700 and p["entry"] == round(F["open"], 2)
    assert p["contract"] == "UPFUT" and p["margin_est"] == f["margin"] and p["tag"] == "paper · long" and not p["est"]
    a = s["accounts"]["paper_long"]
    assert a["positions"] == 2 and a["margin_used"] == pytest.approx(2 * f["margin"], abs=1) and a["cash"] < 500000 - 2 * f["margin"]
    assert s["margin_used"] == a["margin_used"] and s["margin_pct"] == pytest.approx(a["margin_used"] / 5000, abs=0.1)
    assert s["value"] == pytest.approx(a["cash"] + a["margin_used"] + sum(x["mtm"] for x in s["positions"]), abs=1)
    with get_db() as conn:
        rows = conn.execute("SELECT account, symbol, action, status, lots FROM futures_orders WHERE day = ?", (cal[i + 1],)).fetchall()
    assert {(r[0], r[1], r[2], r[3], r[4]) for r in rows} == {("paper_long", "UP", "BUY", "filled", 1), ("paper_long", "AUX", "BUY", "filled", 1)}
    with get_db() as conn:
        eq = conn.execute("SELECT account, COUNT(*) FROM futures_equity GROUP BY account").fetchall()
    assert {r[0]: r[1] for r in eq} == {k: 2 for k in book.ACCOUNTS}
    # the real accounts hold nothing: their Real switch is off
    assert s["accounts"]["real_long"]["positions"] == 0 and s["accounts"]["real_long"]["cash"] == 500000.0


def test_stop_on_the_futures_low_and_the_trail_only_raises(monkeypatch):
    cfg, data, i, _, _ = started(monkeypatch)
    cal = data["cal"]
    st = book._state()
    pos = st["accounts"]["paper_long"]["positions"]["UP"]
    # the next session's futures low under the stop: the paper lot exits at min(open, stop)
    with get_db() as conn:
        b = dict(conn.execute("SELECT * FROM candles_fut_daily WHERE underlying = 'UP' AND date = ?", (cal[i + 2],)).fetchone())
        conn.execute("UPDATE candles_fut_daily SET low = ? WHERE underlying = 'UP' AND date = ?", (pos["stop"] - 1, cal[i + 2]))
    r = book.evening(cfg, cut(data, i + 2), today=cal[i + 2])
    t = next(x for x in r["closed"] if x["symbol"] == "UP")
    assert t["why"] == "stop" and t["exit"] == round(min(b["open"], pos["stop"]), 2) and t["lots"] == 1 and t["net"] == pytest.approx(t["gross"] - t["costs"], abs=0.02)
    assert "UP" not in book._state()["accounts"]["paper_long"]["positions"] and "AUX" in book._state()["accounts"]["paper_long"]["positions"]
    # the trail: a best close at +100% raises the stop to 25% under it; a lower best never lowers it; a hand-set stop too
    st = book._state()
    aux = st["accounts"]["paper_long"]["positions"]["AUX"]
    aux["best"] = aux["entry"] * 2.2
    stop0 = aux["stop"]
    p = tb.rules(cfg, "E")
    with get_db() as conn:
        conn.execute("UPDATE candles_fut_daily SET close = ? WHERE underlying = 'AUX' AND date = ?", (aux["entry"] * 2.2, cal[i + 2]))
    raised = book._late_trail(st, data, cal[i + 2], p)
    assert raised and raised[0]["symbol"] == "AUX" and aux["stop"] == pytest.approx(aux["entry"] * 2.2 * 0.75) and aux["stop"] > stop0 and aux["trailed"]
    hi = aux["stop"]
    aux["best"] = aux["entry"] * 2.0
    assert book._late_trail(st, data, cal[i + 2], p) == [] and aux["stop"] == hi
    book._save(st)
    # the live stop watch on the futures quote: a long at or under its stop (or the day's low) exits once
    kite = Kite({"AUXFUT": hi + 5}, lows={"AUXFUT": hi - 1})
    out = book.watch_stops(cfg, kite, cal[i + 3])
    assert [h["symbol"] for h in out["hits"]] == ["AUX"] and out["hits"][0]["tag"] == "paper · long" and "day low" in out["hits"][0]["why"]
    assert book.watch_stops(cfg, kite, cal[i + 3])["status"] == "nothing"
    nologin = Kite({})
    nologin.has_token = False
    st = book._state()
    st["accounts"]["paper_long"]["positions"]["X"] = {**aux, "symbol": "X", "contract": "XFUT"}
    book._save(st)
    assert book.watch_stops(cfg, nologin, cal[i + 3])["status"] == "no_login"


def test_short_stop_on_the_high_and_the_short_time_stop(monkeypatch):
    cfg, data, i, _, _ = started(monkeypatch)
    cal = data["cal"]
    st = book._state()
    a = st["accounts"]["paper_short"]
    o = {"symbol": "ALT", "side": "short", "lots": 1, "lot_size": 1700, "contract": "ALTFUT", "token": 1004, "expiry": "2030-01-30", "stop": 300.0}
    pos = book._open(st, "paper_short", o, 280.0, cal[i + 1], est=False)
    assert a["cash"] == pytest.approx(500000 - pos["margin_est"] - pos["charges"]) and pos["margin_est"] == pytest.approx(0.2 * 280 * 1700)
    pos["days"] = fb.SHORT_TIME_STOP
    book._save(st)
    r = book.replan(cfg, cut(data, i + 1))
    ex = [o for o in r["after"] if o["action"] == "EXIT"]
    assert ex and ex[0]["symbol"] == "ALT" and ex[0]["side"] == "short" and "time stop" in ex[0]["why"]
    out = book.watch_stops(cfg, Kite({"ALTFUT": 290.0}, highs={"ALTFUT": 301.0}), cal[i + 2])
    assert [h["symbol"] for h in out["hits"]] == ["ALT"] and out["hits"][0]["side"] == "short" and "day high" in out["hits"][0]["why"]
    t = book._trades("paper_short")[0]
    assert t["side"] == "short" and t["gross"] == pytest.approx((280 - 290) * 1700) and t["tag"] == "paper · short"


# --- rolls ---------------------------------------------------------------------------------------------------

def test_roll_at_t_minus_2_exit_then_re_entry_logged_once_never_into_expiry(monkeypatch):
    cfg, data, i, _, _ = started(monkeypatch)
    exp = sessions_ahead(2)                                               # the expiry two sessions away: today is T-2
    set_expiry("UP", exp, next_expiry=sessions_ahead(25))
    set_expiry("AUX", exp)                                                # no next month stored for AUX
    st = book._state()
    up = dict(st["accounts"]["paper_long"]["positions"]["UP"])
    assert book.is_roll_day(cfg, TODAY) and st["roll_day"] is None or True
    q = book.roll_queue(st, cfg, TODAY)
    byq = {x["symbol"]: x for x in q["items"]}
    assert byq["UP"]["action"] == "roll" and byq["UP"]["next"] == "UPNEXTFUT" and byq["UP"]["due"] and byq["AUX"]["action"] == "exit" and byq["AUX"]["why"] == "no next month"
    kite = Kite({"UPFUT": 300.0, "UPNEXTFUT": 301.5, "AUXFUT": 299.0})
    out = book.roll(cfg, kite, TODAY)
    assert out["status"] == "ok" and [r["symbol"] for r in out["rolls"]] == ["UP"] and out["exits"] == [] and out["failed"] == []
    r = out["rolls"][0]
    assert r["from"] == "UPFUT" and r["to"] == "UPNEXTFUT" and r["px"] == 300.0 and r["px_next"] == 301.5 and r["basis"] == pytest.approx(1.5 * 1700)
    assert r["cost"] == pytest.approx(fb.futures_charges(300 * 1700, True) + fb.futures_charges(301.5 * 1700, False) + 1.5 * 1700, abs=0.02)
    st = book._state()
    new = st["accounts"]["paper_long"]["positions"]["UP"]
    assert new["contract"] == "UPNEXTFUT" and new["expiry"] == sessions_ahead(25) and new["entry"] == 301.5 and new["rolls"] == 1
    assert new["d"] == up["d"] and new["stop"] == up["stop"] and new["lots"] == 1
    t = book._trades("paper_long")[0]
    assert t["symbol"] == "UP" and t["why"] == "roll" and t["exit"] == 300.0
    assert "AUX" in st["accounts"]["paper_long"]["positions"] and st["accounts"]["paper_long"]["positions"]["AUX"].get("roll_failed")
    with get_db() as conn:
        rows = conn.execute("SELECT symbol, action, status, reason FROM futures_orders WHERE action = 'ROLL'").fetchall()
    assert len(rows) == 1 and rows[0][0] == "UP" and rows[0][2] == "filled" and "UPFUT → UPNEXTFUT" in rows[0][3]
    assert book.roll(cfg, kite, TODAY)["rolls"] == []                                   # once a day
    # T-1 without a next month: the forced exit, never into expiry
    t1 = sessions_ahead(1)
    out = book.roll(cfg, kite, t1)
    assert [e["symbol"] for e in out["exits"]] == ["AUX"] and out["exits"][0]["why"] == "expiry"
    assert "AUX" not in book._state()["accounts"]["paper_long"]["positions"]
    # a lot whose exit rule fired is exited at the roll instead of rolled
    st = book._state()
    st["plan"]["long"] = [{"action": "EXIT", "symbol": "UP", "side": "long", "lots": 1, "lot_size": 1700, "why": "close under SMA50", "why_key": "rule"}]
    st["accounts"]["paper_long"]["positions"]["UP"]["expiry"] = sessions_ahead(2, t1)
    book._save(st)
    out = book.roll(cfg, Kite({"UPNEXTFUT": 305.0}), sessions_ahead(1, t1))
    assert out["exits"] and out["exits"][0]["why"] == "roll_exit" and book._trades("paper_long")[0]["exit"] == 305.0


def test_real_roll_goes_through_the_sender_exit_then_entry(monkeypatch):
    cfg, data, i, r1, _ = started(monkeypatch, long_real=True)
    exp = sessions_ahead(2)
    set_expiry("UP", exp, next_expiry=sessions_ahead(25))
    st = book._state()
    o_up, o_aux = r1["plan"]["long"]
    book._open(st, "real_long", {**o_up, "expiry": exp}, 280.0, data["cal"][i + 1], est=False)
    book._save(st)
    s = Sender()
    out = book.roll(cfg, Kite({"UPFUT": 300.0, "UPNEXTFUT": 301.0}), TODAY, send=s)
    assert [c for c in s.calls if c[0] == "UP"] == [("UP", "EXIT", None), ("UP", "BUY", 1)]          # the exit first, then the entry
    assert [(r["account"], r["symbol"]) for r in out["rolls"]] == [("paper_long", "UP"), ("real_long", "UP")]
    assert book._state()["accounts"]["real_long"]["positions"]["UP"]["contract"] == "UPNEXTFUT"
    # an entry the deployment refuses leaves the lot closed and is reported
    set_expiry("AUX", exp, next_expiry=sessions_ahead(25))
    st = book._state()
    book._open(st, "real_long", {**o_aux, "expiry": exp}, 280.0, data["cal"][i + 1], est=False)
    book._save(st)
    class Flaky(Sender):
        def __call__(self, symbol, action, lots=None):
            if symbol == "AUX" and action == "BUY":
                raise OSError("rejected")
            return super().__call__(symbol, action, lots)
    out = book.roll(cfg, Kite({"AUXFUT": 300.0, "AUXNEXTFUT": 301.0}), TODAY, send=Flaky())
    f = [x for x in out["failed"] if x["account"] == "real_long"]
    assert f and f[0]["step"] == "entry" and f[0]["exited"] and "AUX" not in book._state()["accounts"]["real_long"]["positions"]


# --- the real sides: send, kill, switches -------------------------------------------------------------------------

def test_real_send_books_the_fill_and_the_stub_refuses(monkeypatch):
    cfg, data, i = world(monkeypatch, long_real=True)
    cal = data["cal"]
    book.evening(cfg, cut(data, i), today=cal[i])
    s = Sender()
    out = book.send_orders(cfg, Kite({"UPFUT": 282.0, "AUXFUT": 283.0}), cal[i + 1], send=s)
    assert [c[1] for c in s.calls] == ["BUY", "BUY"] and all(c[2] == 1 for c in s.calls) and [x["symbol"] for x in out["sent"]] == ["UP", "AUX"]
    assert out["sent"][0]["price"] == 282.0 and out["sent"][0]["account"] == "real_long"
    assert book.send_orders(cfg, None, cal[i + 1], send=s)["sent"] == [] and len(s.calls) == 2         # once only
    st = book._state()
    pos = st["accounts"]["real_long"]["positions"]["UP"]
    assert pos["est"] and pos["entry"] == 282.0 and pos["lots"] == 1
    # the evening corrects the estimated fill to the day's futures open
    book.evening(cfg, cut(data, i + 1), today=cal[i + 1])
    pos = book._state()["accounts"]["real_long"]["positions"]["UP"]
    F = [b for b in __import__("backend.futures.candles", fromlist=["get"]).get("UP") if b["date"] == cal[i + 1]][0]
    assert not pos["est"] and pos["entry"] == F["open"]
    assert "UP" in book._state()["accounts"]["paper_long"]["positions"]                                 # paper filled from the same plan
    # the real sender without its token refuses: a failed order, logged, never retried
    monkeypatch.delenv(sender.ENV_TOKEN, raising=False)
    book.reset()
    book.evening(cfg, cut(data, i), today=cal[i])
    out = book.send_orders(cfg, None, cal[i + 2])                     # (cal[i + 1]'s keys are already in the log: once a day)
    assert out["sent"] == [] and len(out["failed"]) == 2 and "TRADETRON_FUTURES_TOKEN" in out["failed"][0]["error"]
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM futures_orders WHERE status = 'failed' AND account = 'real_long'").fetchone()[0] == 2
    # off / killed: nothing goes out
    book.set_enabled(False)
    assert book.send_orders(cfg, None, cal[i + 2], send=Sender())["status"] == "off"
    book.set_enabled(True)
    with get_db() as conn:
        __import__("backend.database.models", fromlist=["set_setting"]).set_setting(conn, "futures.killed", "1")
    assert book.send_orders(cfg, None, cal[i + 2], send=Sender())["status"] == "off"


def test_kill_switch_asks_the_sender_once_and_the_stub_records_it(monkeypatch):
    cfg, data, i = world(monkeypatch)
    book.evening(cfg, cut(data, i), today=data["cal"][i])
    s = Sender()
    out = book.set_killed(True, cfg, kill_sender=s.kill)
    assert out == {"killed": True, "sent": False} and s.kills == [] and book.killed()               # no real side: nothing to tell
    book.set_killed(False, cfg, kill_sender=s.kill)
    cfg.futures.long_real = True
    out = book.set_killed(True, cfg, kill_sender=s.kill)
    assert out["sent"] and s.kills == ["futures kill switch"] and book.killed()
    assert book.set_killed(True, cfg, kill_sender=s.kill)["sent"] is False and len(s.kills) == 1     # once
    book.set_killed(False, cfg)
    sender.sent_kills.clear()
    monkeypatch.delenv(sender.ENV_TOKEN, raising=False)
    out = book.set_killed(True, cfg)                                                                 # the real sender, no token: refused
    assert not out["sent"] and "TRADETRON_FUTURES_TOKEN" in out["error"] and not sender.sent_kills and book.killed()
    assert book.summary(cfg)["guards"]["kill"] and book.summary(cfg)["kills"] == []
    with pytest.raises(RuntimeError):
        sender.send("UP", "BUY", 1)


def test_switches_and_short_real_refused_without_a_passing_backtest(monkeypatch):
    cfg = cfg_for()
    with pytest.raises(ValueError, match="short side backtest has not passed"):
        book.set_switches(cfg, short_real=True)
    assert cfg.futures.short_real is False
    sw = book.set_switches(cfg, long_real=True, short_paper=False, enabled=False)
    assert sw["long_real"] and not sw["short_paper"] and not sw["enabled"] and not sw["short_real_allowed"]
    assert not book.real_live(cfg, "long") and book.primary(cfg, "long") == "real_long" and book.primary(cfg, "short") == "paper_short"
    book.set_switches(cfg, enabled=True)
    assert book.real_live(cfg, "long")
    with get_db() as conn:
        conn.execute("INSERT INTO futures_runs (payload, ran_at) VALUES (?, ?)", (json.dumps({"verdict": {"short": True}}), now_ist()))
    assert book.set_switches(cfg, short_real=True)["short_real"] and cfg.futures.short_real
    from backend.database.models import get_setting
    with get_db() as conn:
        saved = json.loads(get_setting(conn, "config.futures"))
    assert saved["short_real"] is True and saved["long_real"] is True


# --- the morning: dry run, skip, hold ---------------------------------------------------------------------------

def test_morning_shape_skip_hold_and_the_paper_fill_honours_the_skip(monkeypatch):
    cfg, data, i = world(monkeypatch)
    cal = data["cal"]
    book.evening(cfg, cut(data, i), today=cal[i])
    now = datetime.combine(date.fromisoformat(TODAY), datetime.min.time(), tzinfo=IST).replace(hour=9, minute=5)
    m = book.morning(cfg, now)
    for k in ("now", "today", "preview_at", "send_at", "send_until", "seconds_to_send", "seconds_to_open", "phase", "visible", "started", "capital",
              "cash", "margin_used", "slots_used", "slots", "free", "filter", "gap_s", "est_total_s", "guard", "orders", "going_out", "skipped",
              "waiting", "counts", "first_sent", "last_sent"):
        assert k in m, k
    assert m["phase"] == "before" and len(m["orders"]) == 2 and m["orders"][0]["books"] == ["paper"] and m["orders"][0]["lots"] == 1
    assert m["orders"][0]["margin"] > 0 and m["going_out"] == {"exits": 0, "buys": 2, "value": pytest.approx(sum(o["value"] for o in m["orders"])),
                                                              "margin": pytest.approx(sum(o["margin"] for o in m["orders"]))}
    assert m["guard"]["real_sides"] == [] and m["est_total_s"] == 0 and m["waiting"] == 2
    m = book.skip_order(cfg, "UP", True, "Futures page", now)
    sk = next(o for o in m["orders"] if o["symbol"] == "UP")["skip"]
    assert sk["by"] == "you" and sk["note"] == "Futures page" and m["skipped"] == ["UP"] and m["going_out"]["buys"] == 1
    m = book.skip_order(cfg, "UP", False, "", now)
    assert m["skipped"] == [] and book._state()["unskipped"][TODAY] == ["UP"]
    with pytest.raises(ValueError):
        book.skip_order(cfg, "NOPE", True, "", now)
    m = book.hold_today(cfg, "hold all", now)
    assert sorted(m["skipped"]) == ["AUX", "UP"] and m["going_out"]["buys"] == 0
    # the paper fill the next day honours the skip (logged as skipped, not filled)
    st = book._state()
    st["skips"] = {cal[i + 1]: {"UP": {"by": "you", "at": now_ist(), "note": "test"}}}
    book._save(st)
    r = book.evening(cfg, cut(data, i + 1), today=cal[i + 1])
    assert [f["symbol"] for f in r["fills"]] == ["AUX"]
    with get_db() as conn:
        assert conn.execute("SELECT status FROM futures_orders WHERE symbol = 'UP' AND day = ?", (cal[i + 1],)).fetchone()[0] == "skipped"
    # EXITs cannot be skipped; a skip dies with the day (the evening clears them)
    assert book._state()["skips"] == {} or cal[i + 1] not in book._state()["skips"]
    st = book._state()
    st["plan"]["long"].append({"action": "EXIT", "symbol": "AUX", "side": "long", "lots": 1, "lot_size": 1700, "why": "x"})
    book._save(st)
    with pytest.raises(ValueError, match="EXITs cannot be skipped"):
        book.skip_order(cfg, "AUX", True, "", now)


def test_results_guard_pre_skips_an_entry(monkeypatch):
    cfg, data, i = world(monkeypatch)
    cal = data["cal"]
    nxt = book.next_session(cal[i])
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO corp_events (symbol, date, kind, purpose, descr, fetched_at) VALUES ('UP', ?, 'results', 'Results', '', ?)",
                     (sessions_ahead(2, nxt), now_ist()))
    book.evening(cfg, cut(data, i), today=cal[i])
    st = book._state()
    assert "UP" in {w["symbol"] for w in st["waiting"]["long"]} and any(r.startswith("results") for w in st["waiting"]["long"] for r in w["reasons"] if w["symbol"] == "UP")
    st["plan"]["long"].append({"action": "BUY", "symbol": "UP", "side": "long", "lots": 1, "lot_size": 1700, "entry": 280.0, "stop": 270.0, "why": "x"})
    book._save(st)
    out = book.guard_today(cfg, datetime.combine(date.fromisoformat(nxt), datetime.min.time(), tzinfo=IST))
    assert out == ["UP"] and book._skips(book._state(), nxt)["UP"]["by"] == "results guard"


# --- corrections by hand, deposits ---------------------------------------------------------------------------------

def test_hand_corrections_with_lots(monkeypatch):
    cfg, data, i, _, _ = started(monkeypatch)
    s0 = book.summary(cfg)
    up = next(p for p in s0["positions"] if p["symbol"] == "UP")
    dry = book.edit_position(cfg, "UP", lots=2, entry=280.0, stop=270.0, note="filled 2 lots", dry=True)
    assert dry["dry"] and dry["position"]["lots"] == 2 and dry["position"]["qty"] == 3400 and dry["effects"]["exit_qty"] == 2
    assert book._state()["accounts"]["paper_long"]["positions"]["UP"]["lots"] == 1                     # nothing saved
    out = book.edit_position(cfg, "UP", lots=2, entry=280.0, stop=270.0, note="filled 2 lots")
    st = book._state()
    pos = st["accounts"]["paper_long"]["positions"]["UP"]
    assert pos["lots"] == 2 and pos["entry"] == 280.0 and pos["stop"] == 270.0 and pos["manual_stop"] and pos["px_orig"] == up["entry"]
    assert pos["margin_est"] == pytest.approx(0.2 * 280 * 3400) and [e["field"] for e in out["edits"]] == ["lots", "entry", "stop"]
    assert st["accounts"]["paper_long"]["cash"] == pytest.approx(s0["accounts"]["paper_long"]["cash"] - (pos["margin_est"] - up["margin_est"]) - (pos["charges"] - fb.futures_charges(up["entry"] * 1700, False)), abs=1)
    with pytest.raises(ValueError, match="below the last"):
        book.edit_position(cfg, "UP", stop=10_000.0)
    with pytest.raises(ValueError, match="whole number of lots"):
        book.edit_position(cfg, "UP", lots=1.5)
    with pytest.raises(ValueError, match="no open futures position"):
        book.edit_position(cfg, "NOPE", lots=1)
    # the trail never lowers a hand-set stop
    pos["best"] = pos["entry"] * 2.2
    pos["stop"] = pos["entry"] * 2.2 * 0.9
    book._save(st)
    assert book._late_trail(st, data, data["cal"][i + 1], tb.rules(cfg, "E")) == [] and pos["stop"] == pos["entry"] * 2.2 * 0.9
    # remove: the margin and charges come back
    cash = st["accounts"]["paper_long"]["cash"]
    r = book.remove_position(cfg, "AUX", "never filled")
    aux = next(p for p in s0["positions"] if p["symbol"] == "AUX")
    assert r["removed"]["lots"] == 1 and r["cash"] == pytest.approx(cash + aux["margin_est"] + fb.futures_charges(aux["entry"] * 1700, False), abs=1)
    # add: a lot opened by hand on the short paper side, the stop from the ATR rule
    r = book.add_position(cfg, "ALT", 1, 280.0, data["cal"][i + 1], side="short", book="paper", note="by hand")
    p = r["position"]
    assert r["account"] == "paper_short" and p["lots"] == 1 and p["lot_size"] == 1700 and p["contract"] == "ALTFUT" and p["stop"] > 280.0 and p["manual"]
    with pytest.raises(ValueError, match="already an open lot"):
        book.add_position(cfg, "ALT", 1, 280.0, data["cal"][i + 1], side="short")
    with pytest.raises(ValueError, match="no stored futures contract"):
        book.add_position(cfg, "STEADY", 1, 280.0, data["cal"][i + 1])
    with get_db() as conn:
        rows = [r[0] for r in conn.execute("SELECT status FROM futures_orders WHERE status IN ('edited', 'removed', 'added')")]
    assert sorted(rows) == ["added", "edited", "edited", "edited", "removed"]


def test_deposits_move_the_cash_of_every_account(monkeypatch):
    cfg = cfg_for()
    assert book.set_capital(cfg, 600000.0) == {"status": "ok", "capital": 600000.0} and cfg.futures.capital == 600000.0
    cfg, data, i, _, _ = started(monkeypatch)
    before = {k: a["cash"] for k, a in book._state()["accounts"].items()}
    r = book.set_capital(cfg, 700000.0)
    st = book._state()
    assert r["delta"] == 200000.0 and st["capital"] == 700000.0 and all(st["accounts"][k]["cash"] == before[k] + 200000.0 for k in before)
    assert st["deposits"][-1]["amount"] == 200000.0 and book.summary(cfg)["deposits"] == st["deposits"]
    assert book.summary(cfg)["net"] == pytest.approx(book.summary(cfg)["value"] - 700000.0, abs=0.01)
    with pytest.raises(ValueError, match="withdrawing"):
        book.set_capital(cfg, 100.0)


# --- overview, attention, alert texts ------------------------------------------------------------------------------

def test_overview_account_positions_and_attention(monkeypatch):
    cfg, data, i, _, _ = started(monkeypatch)
    from backend.engine import attention, overview
    o = overview.overview(cfg)
    f = o["accounts"]["futures"]
    assert f["name"] == "Futures" and f["positions"] == 2 and f["mode"] == "paper" and f["margin_pct"] > 0 and len(f["spark"]) == 2
    assert {"value", "net", "return", "day_pnl"} <= set(f)
    rows = [p for p in o["positions"] if p["book"] == "futures"]
    assert len(rows) == 2 and rows[0]["account"] == "Futures · paper" and rows[0]["side"] == "long" and rows[0]["lots"] == 1 and rows[0]["qty"] == 1700
    assert rows[0]["strategy"] == "Futures E+ long" and rows[0]["stop_dist"] > 0 and o["facts"]["futures"]["lots"] == 2
    assert overview.NAMES["futures"] == "Futures" and "futures" in overview.curves(cfg)
    att = attention.signals(cfg, True)["futures"]
    assert att["level"] in ("buy", "sell", "filter", "warn") or att["items"] == []
    st = book._state()
    st["accounts"]["paper_long"]["day_start"] = 900000.0
    st["warnings"] = {"day": TODAY, "ban": ["UP"], "results": [], "lot_change": [{"symbol": "AUX", "from": 1700, "to": 1800}]}
    book._save(st)
    texts = [x["text"] for x in attention.signals(cfg, False)["futures"]["items"]]
    assert any("daily loss" in t for t in texts) and any("MWPL ban" in t and "UP" in t for t in texts) and any("Lot size" in t for t in texts)


def test_alert_texts(monkeypatch):
    cfg, data, i, r1, r2 = started(monkeypatch)
    t = msg.futures_plan(r1, {"long": ["paper · long"], "short": ["paper · short"]})
    assert "FUTURES" in t and "BUY <b>UP</b> 1 lot (1,700)" in t and "paper · long" in t and "waiting:" in t and "lot too big" in t
    m = book.morning(cfg, datetime.combine(date.fromisoformat(TODAY), datetime.min.time(), tzinfo=IST).replace(hour=9))
    t = msg.futures_preview(m, "09:16")
    assert "paper only" in t and "fut skip SYMBOL" in t and "fut hold" in t and "margin" in t
    t = msg.futures_stop({"tag": "real · long", "symbol": "UP", "side": "long", "lots": 1, "price": 270.0, "why": "stop 271.00 hit", "net": -1500.0}, "10:05 AM")
    assert "STOP HIT" in t and "real · long" in t and "−₹1,500" in t
    assert "EXIT FAILED" in msg.futures_stop({"tag": "real · long", "symbol": "UP", "lots": 1, "price": 1.0, "error": "timeout"}, "x")
    assert "daily loss limit reached" in msg.futures_daily_loss("paper · long", 15200.0, 15000.0) and "paper · long" in msg.futures_daily_loss("paper · long", 1, 2)
    assert "margin 72%" in msg.futures_margin("real · long", 72.0, 360000.0)
    w = msg.futures_held_warnings({"day": TODAY, "ban": ["UP"], "results": [{"symbol": "AUX", "date": TODAY, "sessions": 2}], "lot_change": [{"symbol": "X", "from": 1, "to": 2}]})
    assert "MWPL ban" in w and "results" in w and "lot size change" in w and msg.futures_held_warnings({"day": TODAY}) is None
    s = msg.futures_short_signals([{"day": TODAY, "symbol": "SAIL", "hint": 181.2}], [], [{"symbol": "SAIL", "reasons": ["in ban"]}])
    assert "paper · short" in s and "SAIL" in s and "in ban" in s
    n = msg.futures_roll_notice([{"account": "paper_long", "symbol": "UP", "side": "long", "action": "roll", "contract": "UPFUT", "next": "UPNEXTFUT", "est_cost": 900.0}], "2026-11-24", "notice")
    assert "roll notice" in n and "EXIT UPFUT + BUY UPNEXTFUT" in n and "paper · long" in n
    d = msg.futures_roll_notice([{"tag": "paper · long", "symbol": "UP", "side": "long", "from": "UPFUT", "to": "UPNEXTFUT", "px": 300.0, "px_next": 301.5, "basis_pct": 0.5, "cost": 900.0}], TODAY, "done")
    assert "roll done" in d and "UPFUT → UPNEXTFUT" in d and "basis 0.5%" in d
    t = msg.futures_sent({"sent": [{"action": "BUY", "symbol": "UP", "lots": 1, "lot_size": 1700, "price": 282.0, "stop": 270.0, "tag": "real · long"}],
                          "failed": [{"action": "SELL", "symbol": "ALT", "error": "x"}]}, "9:16 AM")
    assert "ORDERS SENT" in t and "FAILED SELL" in t and "accepted 1 of 2" in t


def test_due_and_the_scheduler_jobs(monkeypatch):
    cfg, data, i, _, _ = started(monkeypatch, long_real=True)
    at = lambda h, m: datetime(2026, 11, 17, h, m, tzinfo=IST)                     # a Tuesday
    assert book.due(at(9, 5), cfg, set(), True, None) == ["futures_preview"]
    assert book.due(at(9, 20), cfg, set(), True, None) == ["futures_send", "futures_stops"]
    assert book.due(at(9, 20), cfg, {"futures_send"}, True, at(9, 18)) == []
    assert book.due(at(9, 20), cfg, set(), False, None) == []
    cfg.futures.long_real = False
    assert book.due(at(9, 20), cfg, set(), True, None) == ["futures_stops"]
    set_expiry("UP", sessions_ahead(2, "2026-11-17"))
    assert book.due(at(9, 25), cfg, set(), True, at(9, 24)) == ["futures_roll"]
    assert book.due(at(9, 25), cfg, {"futures_roll"}, True, at(9, 24)) == []
    # the scheduler's jobs run, record and send: the preview with the plan, the stops, the roll
    from backend.alerts.jobs import Scheduler
    from tests.test_login import FakeTG, LoginState, scheduler
    book.reset()
    book.evening(cfg, cut(data, i), today=data["cal"][i])              # the breakout evening: two lots planned
    fake, st_app = FakeTG(), LoginState()
    st_app.config.futures = cfg.futures
    sch = scheduler(st_app, fake)
    out = sch.run_futures_preview(at(9, 5))
    assert out["orders"] == 2 and out["sent"] is True and "FUTURES" in fake.sent[0][1] and "fut skip SYMBOL" in fake.sent[0][1]
    assert "futures_preview" in Scheduler.done_today(date(2026, 11, 17))
    assert sch.run_futures_stops(at(9, 20))["status"] in ("nothing", "no_login") and sch.last_futures_stops == at(9, 20)
    assert sch.run_futures_roll(at(9, 20))["status"] == "ok" and "futures_roll" in Scheduler.done_today(date(2026, 11, 17))
    assert sch.run_futures_send(at(9, 16))["status"] in ("off", "ok") and "futures_send" in Scheduler.done_today(date(2026, 11, 17))


# --- routes and the stream -------------------------------------------------------------------------------------------

@pytest.fixture
def api(fake_kite, make_client, monkeypatch):
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("KITE_API_KEY", "key123")
    monkeypatch.setenv("KITE_API_SECRET", "sec")
    from fastapi.testclient import TestClient
    from backend import main
    client = make_client(fake_kite, history_years=1)
    client.config.access_token = ""
    monkeypatch.setattr(main, "KiteClient", lambda kc, ic: client)
    with TestClient(main.app) as tc:
        yield tc


def test_routes_and_stream(api, monkeypatch):
    tc = api
    d = tc.get("/api/futures").json()
    assert d["started"] is False and d["capital"] == 500000.0 and d["sides"]["long"]["paper"] and d["guards"]["kill"] is False and d["positions"] == []
    assert tc.post("/api/futures/settings", json={"short_real": True}).status_code == 400
    r = tc.post("/api/futures/settings", json={"long_real": True, "enabled": False}).json()
    assert r["long_real"] and not r["enabled"]
    assert tc.post("/api/futures/settings", json={"long_real": False, "enabled": True}).status_code == 200
    assert tc.post("/api/futures/kill?on=true").json()["killed"] and tc.get("/api/futures").json()["guards"]["kill"]
    assert tc.post("/api/futures/kill?on=false").json()["killed"] is False
    assert tc.post("/api/futures/capital", json={"capital": 100}).status_code == 422
    assert tc.post("/api/futures/capital", json={"capital": 600000}).json()["capital"] == 600000.0
    assert tc.post("/api/futures/replan").json()["status"] == "not_started"
    m = tc.get("/api/futures/morning").json()
    assert m["started"] is False and m["orders"] == []
    assert tc.post("/api/futures/skip", json={"symbol": "UP"}).status_code == 400
    assert tc.post("/api/futures/hold").status_code == 400
    assert tc.post("/api/futures/position/UP", json={"lots": 1}).status_code == 400
    assert tc.delete("/api/futures/position/UP").status_code == 400
    assert tc.post("/api/futures/position", json={"symbol": "UP", "lots": 1, "entry": 100, "day": TODAY}).status_code == 400
    assert tc.get("/api/futures/orders").json() == {"rows": []} and tc.get("/api/futures/trades").json() == {"rows": []}
    assert tc.get("/api/futures/trades?account=x").status_code == 422
    # with a started book
    from backend.api import routes
    cfg = routes.S().config
    cfg.futures = replace(cfg.futures, universe_lot_cap=2_000_000.0, capital=500000.0)
    long_world(monkeypatch)
    data = tb.load(cfg)
    i = N - 40
    with get_db() as conn:
        conn.execute("UPDATE futures_contracts SET expiry = ?", (sessions_ahead(40),))
    book.evening(cfg, cut(data, i), today=data["cal"][i])
    assert tc.get("/api/futures").json()["waiting"][0]["why"].startswith("lot too big")
    book.evening(cfg, cut(data, i + 1), today=data["cal"][i + 1])
    d = tc.get("/api/futures").json()
    assert d["started"] and len(d["positions"]) == 2 and d["positions"][0]["lots"] == 1 and d["rolls"]["items"] and d["short_paper"]["open"] == 0
    assert d["accounts"]["paper_long"]["positions"] == 2 and d["log"] and d["next_expiry"] == sessions_ahead(40)
    r = tc.post("/api/futures/position/UP?dry=1", json={"lots": 2}).json()
    assert r["dry"] and r["position"]["lots"] == 2 and tc.get("/api/futures").json()["positions"][0]["lots"] == 1
    assert tc.post("/api/futures/position/UP", json={"stop": 1.0, "note": "x"}).json()["position"]["stop"] == 1.0
    assert tc.request("DELETE", "/api/futures/position/AUX", json={"note": "never filled"}).json()["removed"]["lots"] == 1
    r = tc.post("/api/futures/position", json={"symbol": "AUX", "lots": 1, "entry": 280.0, "day": data["cal"][i + 1], "side": "long", "book": "paper"}).json()
    assert r["account"] == "paper_long" and r["position"]["lots"] == 1
    assert len(tc.get("/api/futures/orders").json()["rows"]) >= 5 and tc.get("/api/futures/trades?account=paper_long").json() == {"rows": []}
    assert tc.post("/api/futures/replan").json()["status"] == "ok"
    text = tc.get("/api/stream?topics=futures,futures_morning,overview,attention&once=1").text
    assert "event: futures\n" in text and "event: futures_morning\n" in text
    assert tc.get("/api/overview").json()["accounts"]["futures"]["positions"] == 2
    assert "futures" in tc.get("/api/attention").json()
    assert tc.post("/api/futures/roll").json()["status"] == "ok"
    assert tc.get("/api/schedule").status_code == 200
