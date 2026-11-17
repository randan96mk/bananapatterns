"""📖 The Week page and the week's story: the week's bounds, the aggregation across the books (Real, Paper E+,
Futures, Options, the signal track), by day, by book, the curve normalised to the week's start against NIFTY, best /
worst with the AI score at entry, suggestions, the AI view record, the backtest hint, a week with no trades; the
observations' validation (a figure not in the facts stripped, an empty line dropped, ≤ 3, no broker), the ONE ai_notes
row and its UNIQUE guard, the Sunday job and the guard's Sunday window, the catch-up after the ratings, "Write now"
(409 while running), the Telegram message (AI marker, 12-hour), the API shapes. Fake Ollama; no network."""

import json
from datetime import datetime

import pytest

from backend.ai import client, control, guard, week_story
from backend.ai import queue as q
from backend.alerts import messages as msg
from backend.alerts.dispatch import Notifier
from backend.alerts.jobs import Scheduler
from backend.config import IST, AIConfig, AlertsConfig, AppConfig
from backend.database.models import get_db, now_ist
from backend.engine import week as wk
from tests.test_ai_infra import NIGHT, no_brokers
from tests.test_ai_notes import FakeNotes
from tests.test_ai_view import bars, view_row
from tests.test_alerts import FakeTG
from tests.test_login import LoginState

W = "2026-W46"
MON, TUE, WED, THU, FRI = "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"
TODAY = now_ist()[:10]
GOOD = {"observations": [{"day": "Wed", "text": "TCS gave the week its best trade, net +₹4,000 after a 3 day hold."},
                         {"day": "Fri", "text": "Friday cost ₹1,500 on the stop in INFY."},
                         {"day": "week", "text": "The books beat NIFTY by 1.2 pts with 3 won of 5 trades."}]}


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeNotes(reply=json.dumps(GOOD))
    monkeypatch.setattr(client, "_http", fake)
    return fake


@pytest.fixture
def cfg():
    c = AppConfig()
    c.ai = AIConfig(enabled=True)
    c.alerts = AlertsConfig(chat_id="42", intraday=False, filings=False, job_time="00:00")
    return c


@pytest.fixture(autouse=True)
def calm(monkeypatch):
    monkeypatch.setattr(guard, "_meminfo", lambda: {"ram_total_gb": 23.0, "ram_free_gb": 12.0})
    monkeypatch.setattr(guard, "_load1", lambda: 0.5)
    monkeypatch.setattr(guard, "_cpu_pct", lambda: 12.0)
    guard.record_tick(120)
    control._pull.update(running=False, model=None, pct=0.0, done=[], error=None, at=None)
    q.clear("test setup")
    q._items.join()
    with get_db() as conn:
        conn.execute("INSERT INTO job_runs (job, run_date, started_at, finished_at, status, message) VALUES ('eod', ?, ?, ?, 'ok', '')",
                     (TODAY, now_ist(), now_ist()))


def _ins(sql, *args):
    with get_db() as conn:
        return conn.execute(sql, args).lastrowid


def cash(table, sym, buy, sell, net, d_in, d_out, why="stop", account=None):
    p = json.dumps({"symbol": sym, "qty": 10, "buy": buy, "sell": sell, "net": net, "in": d_in, "out": d_out, "days": 3, "why": why})
    if account:
        return _ins(f"INSERT INTO {table} (account, payload) VALUES (?, ?)", account, p)
    return _ins(f"INSERT INTO {table} (payload) VALUES (?)", p)


def seed_trades():
    """Five trades in W46 across the books, one Paper trade of another account and one trade the week after (both out)."""
    cash("real_trades", "TCS", 4000, 4400, 4000.0, "2026-11-06", WED, "target")
    cash("real_trades", "INFY", 1500, 1350, -1500.0, TUE, FRI, "stop")
    cash("paper_trades", "TCS", 4000, 4300, 2500.0, "2026-11-06", WED, "trail", account="E")
    cash("paper_trades", "OLD", 100, 90, -9999.0, MON, WED, "stop", account="A")
    cash("real_trades", "LATER", 100, 110, 777.0, FRI, "2026-11-16")
    _ins("INSERT INTO futures_trades (account, payload) VALUES (?, ?)", "paper_long",
         json.dumps({"symbol": "HDFC", "side": "long", "entry": 100, "exit": 98, "net": -400.0, "in": MON, "out": TUE, "days": 1, "why": "stop"}))
    _ins("INSERT INTO options_trades (day, payload) VALUES (?, ?)", THU, json.dumps({"bias": "bull", "credit": 50, "cost": 20, "net": 1200.0, "label": "target", "in": "9:50 AM", "out": "10:05 AM"}))
    _ins("INSERT INTO flow_positions (day, symbol, status, payload) VALUES (?, ?, 'closed', ?)", MON, "SBIN",
         json.dumps({"symbol": "SBIN", "side": "long", "entry": 800, "net": 300.0, "reason": "target"}))
    _ins("INSERT INTO flow_positions (day, symbol, status, payload) VALUES (?, ?, 'open', ?)", MON, "OPEN", json.dumps({"symbol": "OPEN", "net": 5.0}))


def seed_curve():
    """Real equity 1,00,000 before Monday, NIFTY 25,000 before Monday; Friday: books +2%, NIFTY +0.8%."""
    for d, e in (("2026-11-06", 100_000), (MON, 100_500), (TUE, 99_000), (WED, 101_000), (THU, 101_500), (FRI, 102_000)):
        _ins("INSERT INTO real_equity (day, equity, gross) VALUES (?, ?, ?)", d, e, e)
    days = ["2026-11-06", MON, TUE, WED, THU, FRI]
    bars("NIFTY 50", [25000, 25100, 24900, 25050, 25150, 25200], days=days)


# --- the bounds ---------------------------------------------------------------------------------------------------------------

def test_week_bounds_and_parse():
    assert wk.bounds(W) == (MON, FRI) and wk.days_of(W) == [MON, TUE, WED, THU, FRI]
    assert wk.parse("2026-w46") == (2026, 46) and wk.iso_week(datetime(2026, 11, 22).date()) == "2026-W47"
    for bad in ("2026-46", "W46", "2026-W54", "nonsense"):
        with pytest.raises(ValueError):
            wk.parse(bad)


# --- the aggregation ----------------------------------------------------------------------------------------------------------

def test_aggregation_across_books():
    seed_trades()
    p = wk.week(W, TODAY)
    assert p["totals"]["net"] == 6100.0 and p["totals"]["trades"] == 6 and p["totals"]["won"] == 4
    books = {b["book"]: (b["net"], b["trades"], b["won"], b["icon"]) for b in p["by_book"]}
    assert books == {"real": (2500.0, 2, 1, "💰"), "paper": (2500.0, 1, 1, "📝"), "futures": (-400.0, 1, 0, "📐"),
                     "options": (1200.0, 1, 1, "🧩"), "signal": (300.0, 1, 1, "⚡")}
    assert [b["book"] for b in p["by_book"]] == ["real", "paper", "futures", "options", "signal"]


def test_by_day_mon_to_fri():
    seed_trades()
    bd = wk.week(W, TODAY)["by_day"]
    assert [d["day"] for d in bd] == [MON, TUE, WED, THU, FRI]
    assert [(d["net"], d["trades"], d["won"]) for d in bd] == [(300.0, 1, 1), (-400.0, 1, 0), (6500.0, 2, 2), (1200.0, 1, 1), (-1500.0, 1, 0)]


def test_curve_normalised_to_the_week_start_and_nifty():
    seed_curve()
    p = wk.week(W, TODAY)
    c = p["curve"]
    assert [x["t"] for x in c] == [MON, TUE, WED, THU, FRI]
    assert c[0] == {"t": MON, "all_books": 0.5, "nifty": 0.4} and c[-1] == {"t": FRI, "all_books": 2.0, "nifty": 0.8}
    assert p["totals"]["vs_nifty_pts"] == 1.2 and [d["nifty_pct"] for d in p["by_day"]][:2] == [0.4, -0.8]


def test_curve_sums_books_and_carries_a_missing_day():
    seed_curve()
    for d, e in (("2026-11-06", 50_000), (MON, 50_000), (WED, 51_000)):                          # options: Tue missing
        _ins("INSERT INTO options_equity (day, equity, gross) VALUES (?, ?, ?)", d, e, e)
    c = {x["t"]: x["all_books"] for x in wk.week(W, TODAY)["curve"]}
    assert c[TUE] == round((99_000 + 50_000) / 150_000 * 100 - 100, 2) and c[WED] == round((101_000 + 51_000) / 150_000 * 100 - 100, 2)


def test_nifty_from_the_equity_rows_without_index_candles():
    for d, e, n in (("2026-11-06", 100_000, 25_000), (MON, 100_500, 25_100), (TUE, 101_000, 25_250)):
        _ins("INSERT INTO real_equity (day, equity, gross, nifty) VALUES (?, ?, ?, ?)", d, e, e, n)
    c = wk.week(W, TODAY)["curve"]
    assert c[:2] == [{"t": MON, "all_books": 0.5, "nifty": 0.4}, {"t": TUE, "all_books": 1.0, "nifty": 1.0}]


def test_headline():
    seed_trades()
    seed_curve()
    assert wk.week(W, TODAY)["headline"] == "+₹6,100 · 6 trades · 4 won · beat NIFTY by 1.2 pts"
    assert wk.headline(-900.0, 2, 0, -0.4) == "−₹900 · 2 trades · 0 won · trailed NIFTY by 0.4 pts"
    assert wk.headline(0.0, 0, 0, None) == "no closed trades"


def test_best_worst_with_the_ai_score_at_entry():
    seed_trades()
    view_row("TCS", "2026-11-05", "buy", 74)
    p = wk.week(W, TODAY)
    assert p["best"] == {"symbol": "TCS", "book": "real", "net": 4000.0, "score": 74, "ended": WED, "why": "target"}
    assert p["worst"]["symbol"] == "INFY" and p["worst"]["net"] == -1500.0 and p["worst"]["score"] is None


def test_suggestions_ai_view_and_backtest_hint():
    for st, oc in (("accepted", {"helped": True}), ("accepted", {"helped": False}), ("dismissed", None), ("open", None)):
        _ins("INSERT INTO ai_suggestions (created, day, kind, symbol, title, status, outcome) VALUES (?,?,?,?,?,?,?)",
             now_ist(), TUE, "skip", f"S{st}{oc}", "t", st, json.dumps(oc) if oc else None)
    _ins("INSERT INTO ai_suggestions (created, day, kind, symbol, title, status, action) VALUES (?,?,?,?,?,?,?)", now_ist(), "2026-11-15", "backtest", "",
         "Backtest a stop cap of 4%", "open", json.dumps({"route": "strategies/futures", "prefill": {"max_stop_pct": 4.0}, "label": "Open"}))
    _ins("INSERT INTO ai_suggestions (created, day, kind, symbol, title, status, action) VALUES (?,?,?,?,?,?,?)", now_ist(), WED, "backtest", "",
         "Backtest a stop cap of 4%", "open", json.dumps({"route": "strategies/futures", "prefill": {"max_stop_pct": 4.0}, "label": "Open"}))
    view_row("B1", TUE, "buy", 75, h20=True)
    view_row("B2", TUE, "buy", 75, h20=False)
    view_row("P1", TUE, "wait", 50, source="plan", h20=False)
    p = wk.week(W, TODAY)
    assert p["suggestions"] == {"made": 5, "accepted": 2, "helped": 1}
    assert p["backtest_hint"]["route"] == "strategies/futures" and p["backtest_hint"]["prefill"] == {"max_stop_pct": 4.0}
    assert p["ai_view"]["buy_hit"] == 50.0 and p["ai_view"]["baseline"] == 33.3


def test_week_with_no_trades_and_the_nav():
    p = wk.week(W, TODAY)
    assert p["totals"] == {"net": 0, "trades": 0, "won": 0, "vs_nifty_pts": None} and p["headline"] == "no closed trades"
    assert p["by_book"] == [] and p["best"] is None and p["worst"] is None and p["observations"] == [] and p["curve"] == []
    assert p["prev_week"] == "2026-W45" and p["next_week"] == "2026-W47"
    cur = wk.week(None, TODAY)
    assert cur["week"] == wk.iso_week(datetime.fromisoformat(TODAY).date()) and cur["next_week"] is None
    keys = {"week", "from", "to", "headline", "totals", "by_day", "curve", "by_book", "best", "worst", "suggestions", "ai_view",
            "backtest_hint", "observations", "prev_week", "next_week"}
    assert keys <= set(p) and p["backtest_hint"] is None and p["suggestions"] == {"made": 0, "accepted": 0, "helped": 0}


# --- the observations -----------------------------------------------------------------------------------------------------------

FACTS = "Total: net +₹6,100; 6 trades; 4 won. Wed: TCS net +₹4,000. NIFTY +0.80%"


def test_validate_strips_numbers_and_drops_empty_lines():
    raw = json.dumps({"observations": [{"day": "Wed", "text": "TCS made ₹4,000 and 77% more"}, {"day": "Mon", "text": "12345 999"},
                                       {"day": "sunday", "text": "6 trades and 4 won across the week"}]})
    v = week_story.validate(raw, FACTS)
    assert v["numbers_checked"] is False and len(v["observations"]) == 2
    assert v["observations"][0] == {"text": "TCS made ₹4,000 and more", "ai": True, "checked": True, "day": "Wed"}
    assert v["observations"][1]["day"] == "week"                                            # not Mon–Fri: the week


def test_validate_caps_three_and_clips():
    raw = json.dumps([{"day": "Mon", "text": "word " * 60}] + [{"day": "Tue", "text": f"line number {i} says a thing"} for i in range(5)])
    v = week_story.validate(raw, FACTS + " 0 1 2 3 4")
    assert len(v["observations"]) == 3 and len(v["observations"][0]["text"]) <= 160 and v["observations"][0]["text"].endswith("…")


def test_validate_refuses_bad_answers():
    for raw, why in (("nonsense", "not JSON"), (json.dumps({"observations": []}), "no observation"),
                     (json.dumps(["Book it through Zerodha"]), "names a broker"), (json.dumps(["88 99"]), "nothing left")):
        with pytest.raises(week_story.WeekInvalid, match=why):
            week_story.validate(raw, FACTS)
    assert week_story.validate(json.dumps({"lines": ["six trades and four wins"]}), FACTS)["observations"][0]["day"] == "week"


def test_facts_lines(cfg):
    seed_trades()
    seed_curve()
    p, lines = week_story.facts(W, TODAY)
    text = "\n".join(lines)
    assert lines[0] == "Week 46: Mon 9 Nov to Fri 13 Nov" and "net +₹6,100; 6 trades; 4 won" in lines[1] and "(+1.2 pts)" in lines[1]
    assert "Trade: TCS · real · entry 4,000 · exit 4,400 · net +₹4,000 · ended Wed 11 Nov (target) · held 3 days" in text
    assert "Wed 11 Nov: net +₹6,500; 2 trades; 2 won" in text and no_brokers(text)


# --- the run, the row, the guard ------------------------------------------------------------------------------------------------

def test_run_writes_one_row_and_sends_once(ollama, cfg):
    seed_trades()
    seed_curve()
    fake = FakeTG()
    out = week_story.run(cfg, W, Notifier(cfg.alerts, fake), TODAY)
    assert out["id"] and out["note"] == "week 2026-W46 · 3 observation(s)"
    body = ollama.calls[-1][2]
    assert body["model"] == NIGHT and body["format"] == "json" and "Facts:\n- Week 46" in body["prompt"] and "never advise" in body["system"]
    with get_db() as conn:
        r = conn.execute("SELECT * FROM ai_notes WHERE kind = 'week'").fetchall()
    assert len(r) == 1 and r[0]["source_key"] == f"week:{W}" and r[0]["prompt_version"] == "week-v1" and r[0]["day"] == FRI
    data = json.loads(r[0]["data"])
    assert data["week"] == W and data["model"] == NIGHT and data["prompt_version"] == "week-v1" and "ms" in data and data["alerted"] is True
    assert r[0]["text"].split("\n")[0].startswith("TCS gave the week")                  # lines, not JSON (real ₹ in the feed)
    obs = wk.week(W, TODAY)["observations"]
    assert [o["day"] for o in obs] == ["Wed", "Fri", "week"] and obs[0]["day_iso"] == WED and all(o["ai"] and o["checked"] for o in obs)
    assert len(fake.sent) == 1 and fake.sent[0][1].startswith("📖 <b>[AI] Week 46 · the story</b>\n<i>AI ·</i> TCS gave")
    assert week_story.run(cfg, W, Notifier(cfg.alerts, fake), TODAY) == {"skip": "this week's story exists"} and len(fake.sent) == 1


def test_unique_guard_on_the_row(cfg):
    res = {"observations": [{"text": "a b c", "ai": True, "checked": True, "day": "week"}], "numbers_checked": True}
    assert week_story.save(W, FRI, res, NIGHT, 100, 3) is not None
    assert week_story.save(W, FRI, res, NIGHT, 100, 3) is None
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ai_notes WHERE kind = 'week'").fetchone()[0] == 1


def test_run_retries_once_then_fails(ollama, cfg):
    seed_trades()
    ollama.reply = ["nonsense", json.dumps(GOOD)]
    out = week_story.run(cfg, W, Notifier(cfg.alerts, FakeTG()), TODAY)
    assert out["tokens"] == 18 and "was not valid: not JSON" in ollama.calls[-1][2]["system"]
    with get_db() as conn:
        conn.execute("DELETE FROM ai_notes")
    ollama.reply = ["nonsense", "still nonsense"]
    with pytest.raises(week_story.WeekInvalid):
        week_story.run(cfg, W, Notifier(cfg.alerts, FakeTG()), TODAY)
    assert week_story.latest(W) is None


def test_run_skips_a_week_without_trades(ollama, cfg):
    assert week_story.run(cfg, W, Notifier(cfg.alerts, FakeTG()), TODAY) == {"skip": "no closed trade this week"} and ollama.calls == []


def test_queue_job_kind_and_key(ollama, cfg):
    seed_trades()
    job = week_story.enqueue(cfg, W, Notifier(cfg.alerts, FakeTG()))
    assert job["key"] == f"week:{W}" and job["dedup"] is False
    row = q.wait(job["id"], 10)
    assert row["status"] == "done" and row["kind"] == "night" and week_story.latest(W)
    cfg.ai.week = False
    assert week_story.enqueue(cfg, W) == {"status": "off"}


def test_closed_week_and_enqueue_due(ollama, cfg, monkeypatch):
    sun = datetime(2026, 11, 15, 20, 5, tzinfo=IST)
    assert week_story.closed_week(sun) == W and week_story.closed_week(datetime(2026, 11, 17, 21, 0, tzinfo=IST)) == W
    assert week_story.closed_week(datetime(2026, 11, 13, 15, 29, tzinfo=IST)) == "2026-W45"
    assert week_story.closed_week(datetime(2026, 11, 13, 15, 30, tzinfo=IST)) == W
    seen = []
    monkeypatch.setattr(week_story, "enqueue", lambda c, w, n=None, telegram=True: seen.append(w) or {"id": 1, "dedup": False, "key": f"week:{w}"})
    assert week_story.enqueue_due(cfg, sun)["week"] == W and seen == [W]
    week_story.save(W, FRI, {"observations": [{"text": "a b c", "ai": True, "checked": True, "day": "week"}], "numbers_checked": True}, NIGHT, 1, 1)
    assert week_story.enqueue_due(cfg, sun) == {"status": "exists", "week": W} and seen == [W]
    cfg.ai.week = False
    assert week_story.enqueue_due(cfg, sun) == {"status": "off"}


def test_due_on_sunday_evening(cfg):
    d = lambda y, m, dd, h, mi, done=(): week_story.due(datetime(y, m, dd, h, mi, tzinfo=IST), cfg, set(done))
    assert d(2026, 11, 15, 20, 0) == ["ai_week"] and d(2026, 11, 15, 22, 59) == ["ai_week"]
    assert d(2026, 11, 15, 19, 59) == [] and d(2026, 11, 15, 23, 0) == [] and d(2026, 11, 14, 20, 30) == []   # Saturday: no
    assert d(2026, 11, 15, 20, 30, done=("ai_week",)) == []
    cfg.ai.week = False
    assert d(2026, 11, 15, 20, 30) == []


def test_guard_opens_the_night_window_on_sunday_evening(ollama, cfg):
    cfg.alerts.job_time = "18:30"
    assert guard.allowed("night", datetime(2026, 11, 15, 20, 30, tzinfo=IST), cfg) == (True, "ok")
    assert guard.allowed("night", datetime(2026, 11, 15, 19, 0, tzinfo=IST), cfg)[0] is False
    assert guard.allowed("night", datetime(2026, 11, 14, 20, 30, tzinfo=IST), cfg)[0] is False                # Saturday: closed


def test_scheduler_queues_the_sunday_story_once(ollama, cfg, monkeypatch):
    from backend.alerts import jobs as aj
    monkeypatch.setattr(aj, "due_jobs", lambda *a, **k: [])
    seed_trades()
    st = LoginState()
    st.config = cfg
    fake = FakeTG()
    sch = Scheduler(st, notifier_factory=lambda c: Notifier(c, fake))
    sun = datetime(2026, 11, 15, 20, 10, tzinfo=IST)
    assert "ai_week" in sch.tick(sun)
    q._items.join()
    with get_db() as conn:
        run = conn.execute("SELECT status, message FROM job_runs WHERE job = 'ai_week'").fetchall()
        job = conn.execute("SELECT kind, key, status FROM ai_jobs WHERE key LIKE 'week:%'").fetchone()
    assert len(run) == 1 and run[0]["message"].startswith("queued as AI job") and tuple(job) == ("night", f"week:{W}", "done")
    assert any(h.startswith("📖 <b>[AI] Week 46 · the story</b>") for _, h in fake.sent)
    assert "ai_week" not in sch.tick(datetime(2026, 11, 15, 20, 40, tzinfo=IST))


def test_the_night_batch_catches_up_after_the_ratings(ollama, cfg, monkeypatch):
    st = LoginState()
    st.config = cfg
    sch = Scheduler(st, notifier_factory=lambda c: Notifier(c, FakeTG()))
    seen = []
    monkeypatch.setattr(week_story, "enqueue_due", lambda c, now, n=None: seen.append(now) or {"status": "exists", "week": W})
    for name in ("run_ai_notes", "run_ai_why", "run_ai_suggest", "run_ai_view"):
        monkeypatch.setattr(sch, name, lambda now, name=name: seen.append(name) or {"status": "ok"})
    monkeypatch.setattr(sch, "_run_eod", lambda now, force: {"status": "ok"})
    out = sch.run_eod(datetime(2026, 11, 16, 19, 0, tzinfo=IST))
    assert seen[:4] == ["run_ai_notes", "run_ai_why", "run_ai_suggest", "run_ai_view"] and out["ai_week_story"] == {"status": "exists", "week": W}
    cfg.ai.enabled = False
    assert sch.run_ai_week_story(datetime(2026, 11, 16, 19, 0, tzinfo=IST)) == {"status": "off"}


# --- the message --------------------------------------------------------------------------------------------------------------

def test_telegram_line_marker_and_12_hour():
    html = msg.ai_week_story(W, ["A & B <ok>", "second"], "8:10 PM", 9)
    assert html == ("📖 <b>[AI] Week 46 · the story</b>\n<i>AI ·</i> A &amp; B &lt;ok&gt;\n<i>AI ·</i> second\n"
                    "<i>checked against 9 trade rows · written 8:10 PM · no action taken</i>")
    assert no_brokers(html) and "20:10" not in html


def test_config_key():
    assert AIConfig().week is True


# --- routes ---------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def api(fake_kite, make_client, monkeypatch, ollama):
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("KITE_API_KEY", "key123")
    monkeypatch.setenv("KITE_API_SECRET", "sec")
    from fastapi.testclient import TestClient
    from backend import main
    kc = make_client(fake_kite, history_years=1)
    kc.config.access_token = ""
    monkeypatch.setattr(main, "KiteClient", lambda kc_, ic: kc)
    with TestClient(main.app) as tc:
        yield tc


def test_routes_week_and_write_now(api, monkeypatch):
    tc = api
    seed_trades()
    d = tc.get(f"/api/week?week={W}").json()
    assert d["week"] == W and d["totals"]["trades"] == 6 and d["story"]["at"] is None and no_brokers(json.dumps(d, ensure_ascii=False))
    assert tc.get("/api/week?week=bad").status_code == 400
    assert tc.get("/api/week").json()["week"] == wk.iso_week(datetime.fromisoformat(TODAY).date())
    assert tc.post("/api/week/write?week=2099-W01").status_code == 400                                   # not over yet
    with get_db() as conn:
        conn.execute("INSERT INTO ai_jobs (created, kind, key, status) VALUES (?, 'night', ?, 'queued')", (now_ist(), f"week:{W}"))
    r = tc.post(f"/api/week/write?week={W}")
    assert r.status_code == 409 and "being written" in r.json()["detail"]
    with get_db() as conn:
        conn.execute("UPDATE ai_jobs SET status = 'skipped'")
    from backend.api import routes
    routes.S().config.ai.enabled = True
    routes.S().config.alerts.job_time = "00:00"                                                          # the night window is open
    r = tc.post(f"/api/week/write?week={W}")
    assert r.status_code == 200 and r.json()["key"] == f"week:{W}"
    assert q.wait(r.json()["id"], 10)["status"] == "done"
    assert tc.post(f"/api/week/write?week={W}").status_code == 409                                       # written: 409
    d = tc.get(f"/api/week?week={W}").json()
    assert len(d["observations"]) == 3 and d["story"]["rows_checked"] == 6 and d["story"]["at_label"]


def test_settings_week_key(api):
    tc = api
    assert tc.get("/api/ai").json()["settings"]["week"] is True
    tc.put("/api/ai/settings", json={"week": False})
    assert tc.get("/api/ai").json()["settings"]["week"] is False
    assert tc.post(f"/api/week/write?week={W}").status_code == 400
