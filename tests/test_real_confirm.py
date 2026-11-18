"""💰 Real · fill confirmation (after the 18 Nov 2026 incident) and the hand tools.

An accepted webhook is not a fill: every BUY is booked unconfirmed, and nothing ever sends an EXIT for an unconfirmed
position (the stop watch, the morning plan's EXITs, a retry): the stop hit is held, alerted once a day, shown on Home
and the page. Confirmation by you (page, API, Telegram `filled` / `unfilled` + `yes`) or by the read-only fill check
(a fake client here); the migration; sold outside the app, a closed trade added, a trade marked as never filled; the
evening "Real vs webhook side" line; the health row; no broker names; 12-hour times."""

import json
import re
from datetime import date, datetime, timedelta

import pytest

from backend.alerts import messages as msg
from backend.alerts.commands import BotCommands
from backend.alerts.dispatch import Notifier
from backend.broker import tradetron_api as tta
from backend.config import IST, AppConfig
from backend.database.models import get_db, get_setting, set_setting
from backend.engine import attention, real
from backend.engine import trend_breakout as tb
from tests.test_login import BotTG, FakeTG, LoginState, scheduler, update
from tests.test_real import Kite, Sender

TODAY = date.today().isoformat()
KEY = "secret-api-key-xyz"
BROKER = re.compile(r"tradetron|zerodha", re.I)
H24 = re.compile(r"\b(1[3-9]|2[0-3]):[0-5]\d\b")          # a 24-hour afternoon time (13:00 … 23:59) never shows


def seed(cash=200_000.0, buys=("S1", "S2"), exit_s0=False, s0_confirmed=True):
    """A started account with one confirmed position S0 and today's BUYs planned (sent by send_orders)."""
    orders = [{"action": "EXIT", "symbol": "S0", "ts": "S0", "qty": 10, "why": "close under SMA50"}] if exit_s0 else []
    for k, s in enumerate(buys):
        close = 500.0 + 100 * k
        orders.append({"action": "BUY", "symbol": s, "ts": s, "qty": 20, "stop": round(close * 0.94, 2), "close": close,
                       "risk": 1.0, "rank": k + 1, "why": f"breakout #{k + 1}"})
    s0 = {"qty": 10, "px": 1000.0, "last": 1050.0, "d": "2026-10-20", "stop": 900.0, "charges": 12.0, "ts": "S0", "days": 15}
    if s0_confirmed:
        s0.update(confirmed=True, confirmed_by="manual")
    st = {"started": "2026-10-01", "capital": 300_000.0, "cash": cash, "charges": 12.0, "as_of": "2026-11-10",
          "positions": {"S0": s0}, "orders": orders, "decided": "2026-11-10", "cap": 10, "allowed": True}
    real._save(st)
    return st


def booked(c=None):
    """seed + today's BUYs sent and booked (unconfirmed)."""
    c = c or AppConfig()
    seed()
    s = Sender()
    real.send_orders(c, None, TODAY, s)
    return c, s


def pos(sym):
    return real._state()["positions"][sym]


class FakeApi:
    def __init__(self, held):
        self.held, self.calls = held, 0

    def positions(self, sid):
        self.calls += 1
        return dict(self.held)


def with_key(monkeypatch, c, sid=77):
    monkeypatch.setenv("TRADETRON_API_KEY", KEY)
    c.real.tt_sid = sid


def log_rows():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT symbol, action, status, reason FROM real_orders ORDER BY id")]


# --- 1. confirmation state ---------------------------------------------------------------------------------------

def test_a_booked_buy_starts_unconfirmed():
    c, s = booked()
    assert [x[1] for x in s.calls] == ["BUY", "BUY"]
    for sym in ("S1", "S2"):
        p = pos(sym)
        assert p["confirmed"] is False and p["confirmed_by"] is None and p["confirmed_at"] is None and p["unconfirmed_since"]
    rows = {r["symbol"]: r for r in real.summary(c)["positions"]}
    assert rows["S1"]["confirmed"] is False and rows["S1"]["unconfirmed_since"] and rows["S0"]["confirmed"] is True


def test_the_evening_est_fill_correction_does_not_confirm():
    booked()
    st = real._state()
    real._rebook(st, st["positions"]["S1"], 512.0)
    assert st["positions"]["S1"]["est"] is False and st["positions"]["S1"]["confirmed"] is False


def test_a_position_without_the_flag_counts_as_unconfirmed():
    assert real.confirmed({}) is False and real.confirmed({"confirmed": "yes"}) is False and real.confirmed({"confirmed": True})


# --- 2. the EXIT gate ----------------------------------------------------------------------------------------------

def test_stop_hit_on_an_unconfirmed_buy_sends_nothing():
    c, _ = booked()
    s = Sender()
    hit = {"S1": pos("S1")["stop"] - 1, "S2": pos("S2")["stop"] * 1.1, "S0": 1050.0}
    out = real.watch_stops(c, Kite(hit), TODAY, s)
    assert s.calls == [] and out["hits"] == []
    assert [g["symbol"] for g in out["gated"]] == ["S1"] and out["gated"][0]["why"] == "stop hit"
    assert "S1" in real._state()["positions"] and pos("S1")["gate"]["day"] == TODAY
    assert not any(r["action"] == "EXIT" for r in log_rows())          # nothing logged as an order: it was never sent
    out = real.watch_stops(c, Kite(hit), TODAY, s)                     # every later check: still nothing
    assert s.calls == [] and [g["symbol"] for g in out["gated"]] == ["S1"]


def test_the_assert_before_every_exit_sender_call():
    booked()
    with pytest.raises(real.UnconfirmedExit):
        real._assert_exit_ok(real._state(), "S1")
    with pytest.raises(real.UnconfirmedExit):
        real._assert_exit_ok(real._state(), "NOPE")
    real._assert_exit_ok(real._state(), "S0")                          # confirmed: allowed


def test_the_morning_plans_exit_is_held_for_an_unconfirmed_position():
    c = AppConfig()
    seed(exit_s0=True, s0_confirmed=False)
    s = Sender()
    out = real.send_orders(c, None, TODAY, s)
    assert ("S0", "EXIT", None) not in s.calls and [x[1] for x in s.calls] == ["BUY", "BUY"]
    assert [g["symbol"] for g in out["gated"]] == ["S0"] and "S0" in real._state()["positions"]
    assert "EXIT due" in msg.real_unconfirmed(out["gated"][0])


def test_a_confirmed_position_exits_as_before():
    c, _ = booked()
    real.confirm_position(c, "S1")
    s = Sender()
    out = real.watch_stops(c, Kite({"S1": pos("S1")["stop"] - 1}), TODAY, s)
    assert s.calls == [("S1", "EXIT", None)] and out["hits"][0]["symbol"] == "S1" and "S1" not in real._state()["positions"]


def test_retry_never_sends_an_exit_for_an_unconfirmed_position(monkeypatch):
    c, _ = booked()
    st = real._state()
    st["orders"].append({"action": "EXIT", "symbol": "S1", "ts": "S1", "qty": 20, "why": "close under SMA50", "sent": "failed"})
    real._save(st)
    with get_db() as conn:
        conn.execute("INSERT INTO real_orders (key, day, created_at, symbol, action, qty, reason, status, reply, payload) "
                     "VALUES (?, ?, ?, 'S1', 'EXIT', 20, 'x', 'failed', 'timeout', '{}')", (f"{TODAY}|S1|EXIT", TODAY, TODAY))
    s = Sender()
    now = datetime.combine(date.today(), datetime.min.time(), tzinfo=IST).replace(hour=10)
    r = real.retry_order(c, None, TODAY, "S1", "EXIT", now, s)
    assert r["status"] == "error" and "never confirmed" in r["error"] and s.calls == []


def test_the_gate_alert_goes_once_a_day_never_held_and_home_shows_it_red(monkeypatch):
    fake, st = FakeTG(), LoginState()
    seed()
    real.send_orders(st.config, None, TODAY, Sender())
    monkeypatch.setattr(real, "_quotes", lambda *a, **k: {"S1": (1.0, 1.0)})
    sch = scheduler(st, fake)
    sch.hold = "2026-11-18"                                            # the evening job's summary hold: never for "real"
    now = datetime.now(IST).replace(hour=11, minute=0)
    sch.run_real_stops(now)
    sch.run_real_stops(now + timedelta(minutes=5))
    texts = [h for _, h in fake.sent]
    assert len(texts) == 1
    assert texts[0] == ("⚠️ <b>REAL</b> · <b>S1</b> stop hit but the buy was never confirmed · no EXIT sent · confirm it "
                        "(reply <b>filled S1</b>) or remove it (<b>unfilled S1</b>)")
    items = attention._real(st.config, True)
    assert ("warn", "Real · S1: stop hit but the buy was never confirmed · no EXIT sent · confirm (✓ filled) or remove "
                    "(✗ never filled)") in items
    row = next(r for r in real.summary(st.config)["positions"] if r["symbol"] == "S1")
    assert row["gate"]["why"] == "stop hit" and row["confirmed"] is False


# --- 3a. manual confirmation: page / API and Telegram ------------------------------------------------------------

def client(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    return TestClient(main.app)


def test_confirm_and_remove_via_the_api(monkeypatch):
    booked()
    with client(monkeypatch) as tc:
        r = tc.post("/api/real/position/s1/confirm", json={"note": "saw it on the broker"}).json()
        assert r["status"] == "ok" and r["position"]["confirmed"] is True
        assert pos("S1")["confirmed_by"] == "manual" and pos("S1")["confirmed_at"]
        assert tc.post("/api/real/position/S1/confirm").status_code == 400            # already confirmed
        assert tc.post("/api/real/position/NOPE/confirm").status_code == 400
        assert tc.request("DELETE", "/api/real/position/S2", json={"note": "never filled"}).json()["status"] == "ok"
        d = tc.get("/api/real").json()
        assert {p["symbol"] for p in d["positions"]} == {"S0", "S1"}
        assert d["log"][1]["action"] == "CONFIRM" and d["log"][1]["status"] == "confirmed" and "by you" in d["log"][1]["reason"]
        assert d["fills"]["mode"] == "manual (no API key)" and d["fills"]["key"] is False


def bot(updates, now):
    st, fake = LoginState(), BotTG(list(updates))
    return BotCommands(st, clock=lambda: now), Notifier(st.config.alerts, fake), fake


def test_telegram_filled():
    booked()
    now = datetime.now(IST)
    b, n, fake = bot([update(1, "filled S1", at=now), update(2, "filled", at=now), update(3, "filled NOPE", at=now)], now)
    assert b.poll_once(n) == 3
    t = [h for _, h in fake.sent]
    assert t[0].startswith("✓ <b>S1</b> confirmed filled") and "nothing was sent" in t[0]
    assert t[1] == "Which stock? filled SYMBOL" and t[2].startswith("Not changed: no open position in NOPE")
    assert pos("S1")["confirmed"] is True and pos("S1")["confirmed_by"] == "manual"


def test_telegram_unfilled_asks_then_yes_removes():
    booked()
    cash0, p = real._state()["cash"], pos("S2")
    now = datetime.now(IST)
    b, n, fake = bot([update(1, "unfilled S2", at=now)], now)
    assert b.poll_once(n) == 1 and "Reply <b>yes</b> within 10 min" in fake.sent[0][1] and "S2" in real._state()["positions"]
    later = now + timedelta(minutes=3)
    b, n, fake = bot([update(2, "yes", at=later)], later)
    assert b.poll_once(n) == 1 and fake.sent[0][1].startswith("✗ <b>S2</b> removed from tracking")
    assert "S2" not in real._state()["positions"]
    assert real._state()["cash"] == pytest.approx(cash0 + p["qty"] * p["px"] + p["charges"], abs=0.01)
    b, n, fake = bot([update(3, "yes", at=later)], later)                       # nothing waits any more
    assert b.poll_once(n) == 1 and fake.sent[0][1] == "Nothing is waiting for a yes."


def test_telegram_unfilled_yes_after_ten_minutes_is_too_late():
    booked()
    now = datetime.now(IST)
    b, n, _ = bot([update(1, "unfilled S1", at=now)], now)
    b.poll_once(n)
    late = now + timedelta(minutes=11)
    b, n, fake = bot([update(2, "yes", at=late)], late)
    assert b.poll_once(n) == 1 and fake.sent[0][1].startswith("Too late") and "S1" in real._state()["positions"]


def test_yes_with_a_number_is_still_the_suggestions():
    now = datetime.now(IST)
    b, n, fake = bot([update(1, "yes 3", at=now)], now)
    assert b.poll_once(n) == 1 and fake.sent[0][1] == "No suggestion #3."


# --- 3b. the read-only fill check (a fake client) ---------------------------------------------------------------

def test_api_confirms_a_position_held_there(monkeypatch):
    c, _ = booked()
    with_key(monkeypatch, c)
    api = FakeApi({"S1": 20, "S2": 5, "OTHER": 3})
    res = real.confirm_from_api(c, api)
    assert api.calls == 1 and res["status"] == "ok" and res["confirmed"] == ["S1"] and res["still"] == ["S2"]
    assert pos("S1")["confirmed"] is True and pos("S1")["confirmed_by"] == "api"
    assert pos("S2")["confirmed"] is False                                   # under its quantity: stays
    assert res["extra"] == ["OTHER"] and res["n_confirmed"] == 2 and res["total"] == 3
    assert any(r["action"] == "CONFIRM" and "by the fill check" in r["reason"] for r in log_rows())


def test_api_absent_stays_unconfirmed_and_nothing_is_ever_sent(monkeypatch):
    c, _ = booked()
    with_key(monkeypatch, c)
    sent = []
    monkeypatch.setattr("backend.broker.tradetron.send", lambda *a, **k: sent.append(a))
    res = real.confirm_from_api(c, FakeApi({}))
    assert res["confirmed"] == [] and res["still"] == ["S1", "S2"] and sent == []
    assert pos("S1")["confirmed"] is False


def test_no_key_is_skipped_quietly(monkeypatch):
    c, _ = booked()
    monkeypatch.delenv("TRADETRON_API_KEY", raising=False)
    api = FakeApi({"S1": 20})
    res = real.confirm_from_api(c, api)
    assert res["status"] == "no key" and api.calls == 0 and pos("S1")["confirmed"] is False
    monkeypatch.setenv("TRADETRON_API_KEY", KEY)
    c.real.tt_sid = 0
    assert real.confirm_from_api(c, api)["status"] == "no deployment id" and api.calls == 0
    assert real.fills_state(c)["mode"] == "manual (set real.tt_sid)"


def test_api_error_changes_nothing_and_never_shows_the_key(monkeypatch):
    c, _ = booked()
    with_key(monkeypatch, c)

    class Boom:
        def positions(self, sid):
            raise RuntimeError(f"401 bad token {KEY}")
    res = real.confirm_from_api(c, Boom())
    assert res["status"] == "error" and KEY not in json.dumps(res) and pos("S1")["confirmed"] is False


def test_the_tolerant_parser():
    doc = {"data": {"positions": [{"Instrument": "NSE:S1-EQ", "Quantity": "20"}, {"symbol": "s2", "net_qty": 0},
                                  {"tradingsymbol": "S3", "qty": -4}, {"underlying": "S4", "open_qty": 7.0}],
                    "summary": [{"symbol": "S1", "qty": 20}, {"symbol": "S5"}], "meta": {"count": 4}}}
    assert tta.parse_positions(doc) == {"S1": 20, "S4": 7}                   # repeated S1: the largest, never a sum
    assert tta.parse_positions([]) == {} and tta.parse_positions("x") == {}


def test_the_client_header_paths_rate_limit_and_scrub(monkeypatch):
    monkeypatch.setenv("TRADETRON_API_KEY", KEY)
    monkeypatch.delenv("TRADETRON_API_URL", raising=False)
    tta._calls.clear()
    seen = []

    class Resp:
        def read(self):
            return json.dumps([{"symbol": "S1", "quantity": 3}]).encode()

    def opener(req, timeout=0):
        seen.append((req.full_url, req.get_header("Authorization")))
        return Resp()
    cl = tta.Client(opener)
    assert cl.positions(77) == {"S1": 3}
    assert seen[0] == ("https://api.tradetron.tech/api/v1/deployments/77/positions", f"Bearer {KEY}")
    assert tta.DEPLOYMENTS_PATH == "/deployments" and tta.POSITIONS_PATH == "/deployments/{sid}/positions"
    for _ in range(tta.RATE_PER_MIN - 1):
        cl.deployments()
    with pytest.raises(tta.RateLimited):
        cl.deployments()
    assert len(seen) == tta.RATE_PER_MIN                                     # the 13th call never went out
    tta._calls.clear()

    def bad(req, timeout=0):
        raise OSError(f"refused for {req.get_header('Authorization')}")
    with pytest.raises(RuntimeError) as e:
        tta.Client(bad).positions(1)
    assert KEY not in str(e.value)
    tta._calls.clear()


def test_the_scheduler_runs_the_fill_check_at_confirm_time():
    c = AppConfig()
    mon = datetime(2026, 11, 16, 10, 0, tzinfo=IST)                          # a Monday
    assert "real_confirm" in real.due(mon, c, set(), True, None)
    assert "real_confirm" not in real.due(mon, c, {"real_confirm"}, True, None)
    assert "real_confirm" not in real.due(mon.replace(hour=9, minute=59), c, set(), True, None)
    assert real.due(mon, c, set(), False, None) == []
    c.real.confirm_time = ""
    assert "real_confirm" not in real.due(mon, c, set(), True, None)


def test_the_confirm_job_and_the_evening_line(monkeypatch):
    fake, st = FakeTG(), LoginState()
    seed()
    real.send_orders(st.config, None, TODAY, Sender())
    with_key(monkeypatch, st.config)
    api = FakeApi({"S0": 10, "S1": 20, "S2": 20, "XTRA": 1})
    monkeypatch.setattr(tta, "Client", lambda: api)
    sch = scheduler(st, fake)
    out = sch.run_real_confirm(datetime.now(IST).replace(hour=10, minute=0))
    assert out["confirmed"] == ["S1", "S2"] and fake.sent[0][1].startswith("✅ <b>REAL</b> · fill check: <b>S1</b>, <b>S2</b> confirmed")
    sch._real_fills(TODAY, evening=True)
    assert fake.sent[-1][1] == "🔎 Real vs webhook side: 3 of 3 positions confirmed · extra on the webhook side: XTRA"
    with get_db() as conn:
        assert conn.execute("SELECT status FROM job_runs WHERE job = 'real_confirm'").fetchone()[0] == "ok"


def test_the_mismatch_line():
    assert real.fills_line({"status": "ok", "n_confirmed": 11, "total": 11, "extra": []}) == \
        "🔎 Real vs webhook side: 11 of 11 positions confirmed · extra on the webhook side: none"
    assert real.fills_line({"status": "ok", "n_confirmed": 9, "total": 11, "extra": ["MOTILALOFS"]}).endswith("MOTILALOFS")
    assert real.fills_line({"status": "no key"}) is None


def test_the_api_route_check_fills_now(monkeypatch):
    booked()
    monkeypatch.delenv("TRADETRON_API_KEY", raising=False)
    with client(monkeypatch) as tc:
        r = tc.post("/api/real/confirm").json()
        assert r["status"] == "no key" and r["still"] == ["S1", "S2"]
        assert tc.get("/api/real").json()["fills"]["last"]["status"] == "no key"


# --- the migration --------------------------------------------------------------------------------------------------

def test_the_migration_confirms_the_positions_open_before_it():
    from backend.database import models
    seed(s0_confirmed=False)
    st = real._state()
    st["positions"]["NEW"] = {"qty": 1, "px": 10.0, "last": 10.0, "d": TODAY, "stop": 9.0, "charges": 0.0, "confirmed": False}
    real._save(st)
    with get_db() as conn:
        set_setting(conn, models.REAL_CONFIRM_DONE_KEY, "")
        assert models._migrate_real_confirm(conn) == ["S0"]
        assert get_setting(conn, models.REAL_CONFIRM_DONE_KEY)
        assert models._migrate_real_confirm(conn) == []                        # once
    assert pos("S0")["confirmed"] is True and pos("S0")["confirmed_by"] == "migration"
    assert pos("NEW")["confirmed"] is False                                    # a flagged position is left alone


def test_a_hand_added_position_is_confirmed_by_owner(monkeypatch):
    from tests.test_real import first_day_with_buys, setup
    c = setup(monkeypatch)
    data, cut, end = first_day_with_buys(c)
    real.evening(c, cut)
    sym = sorted(data["S"])[0]
    rows = data["S"][sym]["rows"]
    real.add_position(c, sym, 2, rows[-3]["close"], rows[-3]["date"], data=data)
    assert pos(sym)["confirmed"] is True and pos(sym)["confirmed_by"] == "owner"


# --- 4. hand tools ---------------------------------------------------------------------------------------------------

def test_sold_outside_the_app_full():
    c, _ = booked()
    st0, p = real._state(), pos("S1")
    r = real.close_position(c, "S1", 560.0, note="sold on the app at 11:05 AM")
    t = r["trade"]
    exit_ch = tb.delivery_charges(20 * 560.0, True)
    assert t["why"] == "hand" and t["by_hand"] is True and t["qty"] == 20 and t["sell"] == 560.0
    assert t["net"] == pytest.approx((560.0 - p["px"]) * 20 - p["charges"] - exit_ch, abs=0.01)
    assert real._state()["cash"] == pytest.approx(st0["cash"] + 20 * 560.0 - exit_ch, abs=0.01)
    assert "S1" not in real._state()["positions"]
    assert log_rows()[-1]["status"] == "closed" and "by you · sold on the app at 11:05 AM" in log_rows()[-1]["reason"]


def test_sold_outside_the_app_partial_keeps_the_rest():
    c, _ = booked()
    p = dict(pos("S1"))
    r = real.close_position(c, "S1", 520.0, qty=5, at=TODAY)
    assert r["left"] == 15 and r["trade"]["qty"] == 5 and pos("S1")["qty"] == 15
    assert pos("S1")["charges"] == pytest.approx(p["charges"] * 15 / 20, abs=1e-6)
    assert r["trade"]["costs"] == pytest.approx(p["charges"] * 5 / 20 + tb.delivery_charges(5 * 520.0, True), abs=0.01)
    with pytest.raises(ValueError):
        real.close_position(c, "S1", 520.0, qty=16)
    with pytest.raises(ValueError):
        real.close_position(c, "S1", 0)


def test_add_a_closed_trade_the_app_never_saw():
    c, _ = booked()
    cash0 = real._state()["cash"]
    r = real.add_trade(c, "glenmark", 10, 1500.0, "2026-11-03", 1550.0, "2026-11-05", "bought by hand")
    t = r["trade"]
    costs = tb.delivery_charges(10 * 1500.0, False) + tb.delivery_charges(10 * 1550.0, True)
    assert t["symbol"] == "GLENMARK" and t["by_hand"] is True and t["costs"] == pytest.approx(costs, abs=0.01)
    assert t["net"] == pytest.approx(500.0 - costs, abs=0.01) and t["days"] == 2
    assert real._state()["cash"] == pytest.approx(cash0 + t["net"], abs=0.01)
    sm = real.summary(c)
    assert sm["account"]["realised"] == pytest.approx(t["net"], abs=0.01) and sm["closed"] == 1
    with pytest.raises(ValueError):
        real.add_trade(c, "X", 1, 10, "2026-11-05", 11, "2026-11-03")
    assert log_rows()[-1]["status"] == "added" and "by you · bought by hand" in log_rows()[-1]["reason"]


def test_void_a_trade_is_kept_tagged_and_left_out_of_the_pnl():
    from backend.ai import why
    from backend.engine import overview
    c, _ = booked()
    t = real.add_trade(c, "HEGAM", 10, 100.0, TODAY, 90.0, TODAY)["trade"]
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO real_equity (day, equity, gross, nifty) VALUES (?, 1000, 1100, 1)", (TODAY,))
    cash0 = real._state()["cash"]
    r = real.void_trade(c, t["id"], "never filled on the broker's side")
    assert r["trade"]["void"] is True and real._state()["cash"] == pytest.approx(cash0 - t["net"], abs=0.01)
    sm = real.summary(c)
    assert [x["id"] for x in sm["trades"]] == [t["id"]] and sm["trades"][0]["void"] is True     # kept, never deleted
    assert sm["closed"] == 0 and sm["won"] == 0 and sm["account"]["realised"] == 0 and sm["voided"] == 1
    with get_db() as conn:
        eq = conn.execute("SELECT equity, gross FROM real_equity WHERE day = ?", (TODAY,)).fetchone()
    assert eq[0] == pytest.approx(1000 - t["net"], abs=0.01) and eq[1] == pytest.approx(1100 - t["gross"], abs=0.01)
    assert not [x for x in overview.trades(c) if x["book"] == "real"]
    assert not [x for x in why.closed_today(TODAY) if x["book"] == "real"]
    with pytest.raises(ValueError):
        real.void_trade(c, t["id"])
    with pytest.raises(ValueError):
        real.edit_trade(c, t["id"], exit=95.0)
    assert log_rows()[-1]["status"] == "voided" and "by you · never filled on the broker's side" in log_rows()[-1]["reason"]


def test_the_hand_tool_routes(monkeypatch):
    booked()
    with client(monkeypatch) as tc:
        r = tc.post("/api/real/position/S2/close", json={"price": 610, "qty": 5, "note": "half sold"}).json()
        assert r["trade"]["by_hand"] is True and r["left"] == 15
        assert tc.post("/api/real/position/NOPE/close", json={"price": 1}).status_code == 400
        r = tc.post("/api/real/trade", json={"symbol": "ABC", "qty": 2, "entry": 10, "entry_day": "2026-11-02", "exit": 12,
                                             "exit_day": "2026-11-04", "note": "seen late"}).json()
        tid = r["trade"]["id"]
        assert tc.post("/api/real/trade", json={"symbol": "ABC", "qty": 2, "entry": 10, "entry_day": "bad", "exit": 12,
                                                "exit_day": "2026-11-04"}).status_code == 400
        assert tc.post(f"/api/real/trade/{tid}/void", json={"note": "never filled"}).json()["trade"]["void"] is True
        assert tc.post("/api/real/trade/99999/void", json={}).status_code == 400
        d = tc.get("/api/real").json()
        assert {x["status"] for x in d["log"][:3]} == {"closed", "added", "voided"}


# --- health, strings, times, settings -------------------------------------------------------------------------------

def test_the_health_row():
    seed()
    st = real._state()
    st["positions"]["NEWB"] = {"qty": 1, "px": 10.0, "last": 10.0, "d": TODAY, "stop": 9.0, "charges": 0.0, "confirmed": False}
    real._save(st)
    h = real.fills_health(AppConfig(), TODAY)
    assert h["key"] == "real_fills" and h["ok"] is True and h["warn"] is False and h["state"] == "1 unconfirmed"
    st["positions"]["NEWB"]["d"] = (date.today() - timedelta(days=10)).isoformat()
    real._save(st)
    h = real.fills_health(AppConfig(), TODAY)
    assert h["warn"] is True and h["ok"] is False and "NEWB" in h["detail"] and "fill checks: manual (no API key)" in h["detail"]


def test_no_broker_names_on_screen_or_telegram(monkeypatch):
    c, _ = booked()
    texts = [msg.real_unconfirmed({"symbol": "S1", "why": "stop hit"}), msg.real_unconfirmed({"symbol": "S1", "why": "close under SMA50"}),
             msg.real_fills_checked({"confirmed": ["S1"], "still": ["S2"], "n_confirmed": 2, "total": 3}),
             msg.real_hand("closed S1 by hand"), real.fills_line({"status": "ok", "n_confirmed": 1, "total": 1, "extra": []}),
             json.dumps(real.fills_health(c, TODAY)), json.dumps(real.fills_state(c)),
             json.dumps(real.confirm_from_api(c)), json.dumps(attention._real(c, True)),
             real.close_position(c, "S1", 600.0)["text"], real.add_trade(c, "Q", 1, 1, TODAY, 2, TODAY)["text"]]
    r = real.retry_order(c, None, TODAY, "S2", "EXIT", datetime.now(IST).replace(hour=10), Sender())
    texts.append(r["error"])
    for t in texts:
        assert not BROKER.search(t), t
    src = open("frontend/src/components/RealPage.jsx", encoding="utf-8").read()
    new = src[src.index("// ✓ filled"):src.index("// ✓ filled") + 6000] if "// ✓ filled" in src else src
    assert not BROKER.search(new)


def test_twelve_hour_times():
    from backend.timefmt import ampm
    assert ampm(AppConfig().real.confirm_time) == "10:00 AM"
    for t in (msg.real_unconfirmed({"symbol": "S1", "why": "stop hit"}),
              msg.real_fills_checked({"confirmed": ["S1"], "still": [], "n_confirmed": 1, "total": 1})):
        assert not H24.search(t)
    src = open("frontend/src/components/RealPage.jsx", encoding="utf-8").read()
    assert "ampm(d.fills.confirm_time)" in src


def test_the_settings_keys(monkeypatch):
    c = AppConfig()
    assert (c.real.tt_sid, c.real.confirm_time, c.real.confirm_alert) == (0, "10:00", True)
    with pytest.raises(ValueError):
        c.update_section("real", {"confirm_time": "25:00"})
    assert c.real.confirm_time == "10:00"
    c.update_section("real", {"tt_sid": "123", "confirm_time": "", "confirm_alert": "false"})
    assert (c.real.tt_sid, c.real.confirm_time, c.real.confirm_alert) == (123, "", False)
    src = open("frontend/src/components/SettingsPanel.jsx", encoding="utf-8").read()
    for k in ("tt_sid:", "confirm_time:", "confirm_alert:"):
        assert k in src
