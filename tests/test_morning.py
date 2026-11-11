"""☀️ The morning dry run: a per-order skip for today (by you, or by the results guard), hold all, the send that
honours and logs the skips, the morning view's shape and phases, Send now's refusals, the Telegram reminder text
and its skip / unskip / hold replies, and the API."""

from datetime import datetime

import pytest

from backend.alerts import messages as msg
from backend.alerts.commands import BotCommands
from backend.alerts.dispatch import Notifier
from backend.config import IST
from backend.database.models import get_db, now_ist
from backend.engine import real
from tests.test_login import BotTG, LoginState, update
from tests.test_paper import cfg
from tests.test_real import Sender

TODAY = "2026-11-11"                                       # a Wednesday
at = lambda h, m, s=0: datetime(2026, 11, 11, h, m, s, tzinfo=IST)


def seed(decided="2026-11-10", buys=("S1", "S2", "S3", "S4")):
    """A planned real account by hand: one EXIT (S0, held) and four BUYs, decided last evening for today."""
    orders = [{"action": "EXIT", "symbol": "S0", "ts": "S0", "qty": 16, "why": "close under SMA50"}]
    for k, s in enumerate(buys):
        close = 500.0 + 100 * k
        orders.append({"action": "BUY", "symbol": s, "ts": s, "qty": 40 - 5 * k, "stop": round(close * 0.94, 2), "close": close,
                       "risk": 1.0, "rank": k + 1, "variant": "E", "top": 300, "turnover_rank": k + 1,
                       "why": f"breakout #{k + 1} · E · top 300 · turnover rank {k + 1}"})
    st = {"started": "2026-10-01", "capital": 300_000.0, "cash": 103_699.0, "charges": 0.0, "as_of": decided,
          "positions": {"S0": {"qty": 16, "px": 1100.0, "last": 1151.0, "d": "2026-10-20", "stop": 1000.0, "charges": 0.0, "ts": "S0", "days": 15}},
          "orders": orders, "decided": decided, "cap": 10, "allowed": True, "waiting": 2,
          "filter": {"name": "E+ · half filter, top 300, late trail", "state": "FULL", "slots": 10}}
    real._save(st)
    return st


def results_on(symbol, day):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO corp_events (symbol, date, kind, purpose, descr, fetched_at) VALUES (?, ?, 'results', "
                     "'Financial Results', '', ?)", (symbol, day, now_ist()))


def skips(day=TODAY):
    return real._skips(real._state(), day)


def test_skip_unskip_and_hold_are_for_today_only():
    c = cfg()
    seed()
    m = real.skip_order(c, "s2", True, "results week", at(9, 4))
    sk = skips()
    assert set(sk) == {"S2"} and sk["S2"]["by"] == "you" and sk["S2"]["note"] == "results week"
    row = next(o for o in m["orders"] if o["symbol"] == "S2")
    assert row["skip"]["by"] == "you" and m["skipped"] == ["S2"] and m["going_out"] == {"exits": 1, "buys": 3, "value": 40 * 500 + 30 * 700 + 25 * 800}
    real.skip_order(c, "S2", False, now=at(9, 5))
    assert skips() == {} and real._state()["unskipped"][TODAY] == ["S2"]
    m = real.hold_today(c, now=at(9, 6))
    assert set(skips()) == {"S1", "S2", "S3", "S4"} and m["going_out"] == {"exits": 1, "buys": 0, "value": 0}
    assert all(k["by"] == "you" and k["note"] == "hold all" for k in skips().values())
    assert skips("2026-11-12") == {}                                                   # tomorrow is untouched


def test_exits_and_unknown_orders_cannot_be_skipped():
    c = cfg()
    with pytest.raises(ValueError, match="not started"):
        real.skip_order(c, "S0", True, now=at(9, 4))
    seed()
    with pytest.raises(ValueError, match="EXITs cannot be skipped"):
        real.skip_order(c, "S0", True, now=at(9, 4))
    with pytest.raises(ValueError, match="not in today's plan"):
        real.skip_order(c, "ZZZ", True, now=at(9, 4))
    assert skips() == {}


def test_send_honours_the_skips_and_logs_them():
    c = cfg()
    seed()
    real.skip_order(c, "S2", True, "results week", at(9, 4))
    s = Sender()
    out = real.send_orders(c, None, TODAY, s)
    assert [x[:2] for x in s.calls] == [("S0", "EXIT"), ("S1", "BUY"), ("S3", "BUY"), ("S4", "BUY")]
    assert [o["symbol"] for o in out["skipped"]] == ["S2"] and out["failed"] == []
    row = next(x for x in real.summary(c)["log"] if x["symbol"] == "S2")
    assert row["status"] == "skipped" and row["action"] == "BUY" and row["reply"] == "skipped today · by you · results week"
    with pytest.raises(ValueError, match="already skipped today"):                    # not retried later that day
        real.skip_order(c, "S2", False, now=at(9, 20))
    assert real.send_orders(c, None, TODAY, s)["sent"] == [] and len(s.calls) == 4
    assert real.send_now(c, None, at(9, 20), s)["sent"] == [] and len(s.calls) == 4
    m = real.morning(c, at(9, 20))
    assert m["phase"] == "after" and m["counts"] == {"sent": 4, "failed": 0, "skipped": 1}
    assert next(o for o in m["orders"] if o["symbol"] == "S2")["status"] == "skipped"


def test_results_guard_pre_skips_and_can_be_switched_back():
    c = cfg()
    seed()
    results_on("S2", "2026-11-13")                                                     # Friday: in 2 sessions
    results_on("S3", "2026-11-18")                                                     # in 5 sessions: fine
    st = real._state()
    assert real.results_guard(st, c, TODAY) == ["S2"]
    real._save(st)
    sk = skips()["S2"]
    assert sk["by"] == "results guard" and sk["results_date"] == "2026-11-13" and sk["results_in"] == 2 and "results 2026-11-13" in sk["note"]
    m = real.morning(c, at(9, 4))
    rows = {o["symbol"]: o for o in m["orders"]}
    assert rows["S2"]["results_in"] == 2 and rows["S3"]["results_date"] == "2026-11-18" and rows["S1"]["results_date"] is None
    assert rows["S0"]["results_date"] is None and rows["S0"]["skip"] is None
    real.skip_order(c, "S2", False, now=at(9, 5))                                      # the owner switches it back on
    assert skips() == {} and real.guard_today(c, at(9, 6)) == [] and skips() == {}      # the 9:00 guard leaves it alone
    c.real.results_guard_sessions = 5
    assert real.guard_today(c, at(9, 6)) == ["S3"]                                      # a wider guard catches S3
    c.real.results_guard_sessions = 0
    seed()
    assert real.guard_today(c, at(9, 6)) == [] and skips() == {}                       # 0 = off


def test_the_evening_plan_runs_the_guard_and_clears_the_last_skips(monkeypatch):
    from tests.test_real import first_day_with_buys, setup
    c = setup(monkeypatch)
    data, cut, end = first_day_with_buys(c)
    real._save({"started": cut["cal"][-1], "capital": 200_000.0, "cash": 200_000.0, "charges": 0.0, "positions": {},
                "orders": [], "skips": {"2020-01-01": {"OLD": {"by": "you", "at": "x", "note": ""}}}})
    tomorrow = real.next_session(cut["cal"][-1])
    assert tomorrow == data["cal"][end + 1]
    res = real.evening(c, cut)
    sym = next(o["symbol"] for o in res["orders"] if o["action"] == "BUY")
    st = real._state()
    assert "2020-01-01" not in st["skips"] and skips(tomorrow) == {}
    results_on(sym, tomorrow)                                                          # results on the send day itself
    rep = real.replan(c, cut)
    assert rep["status"] == "ok" and set(skips(tomorrow)) == {sym} and skips(tomorrow)[sym]["by"] == "results guard"
    s = Sender()
    out = real.send_orders(c, None, tomorrow, s)
    assert sym not in [x[0] for x in s.calls] and [o["symbol"] for o in out["skipped"]] == [sym]


def test_morning_view_shape_and_phases():
    c = cfg()
    m = real.morning(c, at(9, 4))
    assert m["started"] is False and m["visible"] is False and m["orders"] == [] and m["phase"] == "before"
    seed()
    m = real.morning(c, at(9, 4, 18))
    assert set(m) >= {"now", "send_at", "send_until", "seconds_to_send", "phase", "capital", "cash", "slots_used", "slots", "free",
                      "filter", "gap_s", "est_total_s", "guard", "orders", "going_out", "skipped", "waiting", "visible", "counts"}
    assert m["now"] == "2026-11-11T09:04:18+05:30" and m["send_at"] == "2026-11-11T09:16:00+05:30" and m["seconds_to_send"] == 11 * 60 + 42
    assert m["phase"] == "before" and m["visible"] is True and m["capital"] == 300_000 and m["cash"] == 103_699
    assert m["slots_used"] == 1 and m["slots"] == 10 and m["free"] == 9 and m["filter"] == "FULL" and m["waiting"] == 2
    assert m["gap_s"] == 10 and m["est_total_s"] == 50 and m["guard"] == {"enabled": True, "killed": False, "results_guard_sessions": 3}
    assert [o["action"] for o in m["orders"]] == ["EXIT", "BUY", "BUY", "BUY", "BUY"]
    ex, b1 = m["orders"][0], m["orders"][1]
    assert ex["close"] == 1151 and ex["value"] == 16 * 1151 and ex["stop"] is None and ex["skip"] is None and not ex["sent"]
    assert b1 == {"symbol": "S1", "action": "BUY", "qty": 40, "close": 500, "value": 20_000, "stop": 470, "why": "breakout #1 · E · top 300 · turnover rank 1",
                  "results_date": None, "results_in": None, "skip": None, "status": None, "sent": False, "failed": False, "at": None,
                  "price": None, "reply": None}
    assert m["going_out"] == {"exits": 1, "buys": 4, "value": 20_000 + 35 * 600 + 30 * 700 + 25 * 800} and m["skipped"] == []
    assert real.morning(c, at(8, 30))["visible"] is False                              # before the 9:00 reminder
    assert real.morning(c, datetime(2026, 11, 14, 9, 30, tzinfo=IST))["visible"] is False   # Saturday: no panel
    assert real.morning(c, at(9, 16))["phase"] == "sending" and real.morning(c, at(9, 29))["phase"] == "sending"
    assert real.morning(c, at(9, 30))["phase"] == "after"
    real.send_orders(c, None, TODAY, Sender(fail={"S4"}))
    m = real.morning(c, at(9, 17))
    assert m["phase"] == "after" and m["counts"] == {"sent": 4, "failed": 1, "skipped": 0} and m["first_sent"] and m["last_sent"]
    assert next(o for o in m["orders"] if o["symbol"] == "S4")["failed"] is True
    real.set_killed(True)
    assert real.morning(c, at(9, 17))["guard"]["killed"] is True


def test_send_now_refusals_and_the_send_itself():
    c = cfg()
    seed()
    s = Sender()
    assert real.send_now(c, None, at(9, 4), s)["error"] == "the market is closed (9:15 AM – 3:30 PM)"
    assert real.send_now(c, None, at(9, 31), s)["error"].startswith("the send window closed at 09:30")
    assert real.send_now(c, None, datetime(2026, 11, 14, 9, 20, tzinfo=IST), s)["error"] == "not a trading day"   # Saturday
    real.set_killed(True)
    assert real.send_now(c, None, at(9, 15), s)["error"] == "real trading is off or the kill switch is on"
    real.set_killed(False)
    c.real.enabled = False
    assert real.send_now(c, None, at(9, 15), s)["status"] == "error" and s.calls == []
    c.real.enabled = True
    real.skip_order(c, "S1", True, now=at(9, 14))
    out = real.send_now(c, None, at(9, 15), s)
    assert out["status"] == "ok" and len(out["sent"]) == 4 and [o["symbol"] for o in out["skipped"]] == ["S1"]
    assert real.send_now(c, None, at(9, 16), s)["sent"] == [] and len(s.calls) == 4    # once


def test_preview_text_lists_skips_results_and_the_reply_line():
    c = cfg()
    seed()
    results_on("S2", "2026-11-13")
    real.guard_today(c, at(9, 0))
    m = real.morning(c, at(9, 0))
    orders = real._state()["orders"]
    t = msg.real_preview(orders, "E", "09:16", True, False, m)
    assert "4 orders go out at 9:16 AM" in t and "EXIT <b>S0</b> 16" in t and "BUY <b>S1</b> 40 · stop ₹470 · ~₹20,000" in t
    assert "BUY <b>S2</b>" not in t and "skipped today: S2 (results Fri)" in t
    assert "cash ₹103,699 · 9 free slots · filter FULL · gap 10 s" in t
    assert "Reply <b>skip SYMBOL</b> or <b>hold</b> before 9:16 AM · or open the Real page" in t and "kill switch" in t
    results_on("S3", "2026-11-18")
    t = msg.real_preview(orders, "E", "09:16", True, False, real.morning(c, at(9, 0)))
    assert "BUY <b>S3</b> 30 · stop ₹658 · ~₹21,000 · ⚠️ results Wed" in t
    assert "Reply" not in msg.real_preview(orders, "E", "09:16", True, True, m) and "kill switch ON" in msg.real_preview(orders, "E", "09:16", True, True, m)
    old = msg.real_preview(orders, "E", "09:16", True, False)                           # without the morning view: the orders only
    assert "5 orders go out" in old and "skipped today" not in old and "cash" not in old


def bot(updates, now):
    st, fake = LoginState(), BotTG(list(updates))
    return BotCommands(st, clock=lambda: now), Notifier(st.config.alerts, fake), fake


def test_telegram_skip_unskip_hold():
    seed()
    now = at(9, 5)
    b, n, fake = bot([update(1, "skip S2", at=now), update(2, "/unskip s2", at=now), update(3, "skip S0", at=now),
                      update(4, "skip", at=now), update(5, "hold", at=now), update(6, "/help", at=now)], now)
    assert b.poll_once(n) == 6
    texts = [h for _, h in fake.sent]
    assert texts[0].startswith("⏸ S2 skipped today.") and "skipped today: S2 (Telegram)" in texts[0] and "4 orders go out" in texts[0]
    assert texts[1].startswith("▶️ S2 goes out again.") and "5 orders go out" in texts[1]
    assert texts[2] == "Not changed: EXITs cannot be skipped: an exit always goes out"
    assert texts[3] == "Which stock? /skip SYMBOL"
    assert texts[4].startswith("⏸ Every BUY held today; the EXITs still go out.") and "1 order go" in texts[4] and "EXIT <b>S0</b>" in texts[4]
    assert "skip SYM / unskip SYM / hold" in texts[5]
    assert set(skips()) == {"S1", "S2", "S3", "S4"}
    late = at(9, 31)
    b, n, fake = bot([update(7, "unskip S1", at=late)], late)
    assert b.poll_once(n) == 1 and fake.sent[0][1] == "Too late: today's orders went out at 9:16 AM (the window closed at 9:30 AM)."
    assert set(skips()) == {"S1", "S2", "S3", "S4"}
    b, n, fake = bot([update(8, "skipper S1", at=now), update(9, "hold", at=now, chat=999)], now)
    assert b.poll_once(n) == 0                                                          # not a command; not our chat


def test_preview_job_runs_the_guard_and_sends_the_text():
    from tests.test_login import FakeTG, scheduler
    seed()
    results_on("S2", "2026-11-13")
    fake, st = FakeTG(), LoginState()
    sch = scheduler(st, fake)
    out = sch.run_real_preview(at(9, 0))
    assert out == {"orders": 5, "skipped": ["S2"], "sent": True} and skips()["S2"]["by"] == "results guard"
    assert "skipped today: S2 (results Fri)" in fake.sent[0][1] and "4 orders go out" in fake.sent[0][1]


def test_api(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    seed()
    from backend import main
    with TestClient(main.app) as tc:
        m = tc.get("/api/real/morning").json()
        assert m["started"] and len(m["orders"]) == 5 and m["phase"] in ("before", "sending", "after")
        assert tc.post("/api/real/skip", json={"symbol": "S0", "on": True}).status_code == 400
        r = tc.post("/api/real/skip", json={"symbol": "S3", "on": True, "note": "earnings"})
        assert r.status_code == 200 and r.json()["skipped"] == ["S3"]
        assert tc.post("/api/real/skip", json={"symbol": "S3", "on": False}).json()["skipped"] == []
        assert tc.post("/api/real/hold").json()["going_out"]["buys"] == 0
        r = tc.post("/api/real/send-now")
        assert r.status_code == 400 and ("market" in r.json()["detail"] or "window" in r.json()["detail"] or "trading day" in r.json()["detail"])
        assert "event: real_morning" in tc.get("/api/stream?topics=real_morning&once=1").text
        assert tc.get("/api/real").json()["settings"]["results_guard_sessions"] == 3
