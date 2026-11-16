"""🤖 Local AI, PR 3 · A (suggestions): the config and the table, each kind's birth rule with seeded data, the model's
`why` validated like a note, the birth through the fake Ollama with the open cap and the code fallback, the evening
list, expiry on the scheduler tick, accept through the real code path once (and never twice), dismiss reasons, later,
the outcome math, the scorecard and the mute, the Telegram two-step, the morning flags, attention, the hooks and the
routes. Fake Ollama; no network."""

import json
from datetime import datetime, timedelta

import pytest

from backend.ai import client, notes, suggest
from backend.ai import queue as q
from backend.alerts import messages as msg
from backend.alerts.dispatch import Notifier
from backend.alerts.jobs import Scheduler
from backend.ai import control, guard, quality
from backend.config import IST, AIConfig, AlertsConfig, AppConfig
from backend.database.models import get_db, now_ist
from backend.engine import real
from tests.test_ai_infra import no_brokers
from tests.test_ai_notes import FakeNotes, hold, plan
from tests.test_alerts import FakeTG
from tests.test_login import BotTG, LoginState, update

NIGHT = datetime(2026, 11, 17, 21, 0, tzinfo=IST)        # a Tuesday evening
DAY, TOMORROW = "2026-11-17", "2026-11-18"
WHY = json.dumps({"why": "The promoter pledged 4.43% of the shares tonight and the stock is in tomorrow's plan."})


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeNotes(reply=WHY)
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
    """A quiet box, no breaker, no stop, an empty queue; the evening job is done, so the night window is open."""
    monkeypatch.setattr(guard, "_meminfo", lambda: {"ram_total_gb": 23.0, "ram_free_gb": 12.0})
    monkeypatch.setattr(guard, "_load1", lambda: 0.5)
    monkeypatch.setattr(guard, "_cpu_pct", lambda: 12.0)
    guard.record_tick(120)
    control._pull.update(running=False, model=None, pct=0.0, done=[], error=None, at=None)
    quality._run.update(running=False, done=0, n=0, at=None, error=None)
    q.clear("test setup")
    q._items.join()
    with get_db() as conn:
        conn.execute("INSERT INTO job_runs (job, run_date, started_at, finished_at, status, message) VALUES ('eod', ?, ?, ?, 'ok', '')",
                     (now_ist()[:10], now_ist(), now_ist()))


def note(sym, severity="high", kind="filing", category="pledge", direction="negative", numbers=None, title="Promoter pledge up", day=DAY,
         text="Pledge created on 4.43% of the shares."):
    data = {"category": category, "direction": direction, "numbers": numbers or [{"label": "stake", "value": "4.43", "unit": "%"}],
            "source": {"url": "https://nsearchives.nseindia.com/x.pdf", "dt": f"{day}T18:10:00"}, "acts": [], "alerted": False}
    with get_db() as conn:
        cur = conn.execute("INSERT INTO ai_notes (created, day, kind, symbol, book, severity, title, text, data, prompt_version, source_key) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (now_ist(), day, kind, sym, "plan", severity, title, text, json.dumps(data),
                                                             notes.PROMPT_V, f"{kind}:{sym}:{title}"))
    return cur.lastrowid


def deal_note(sym, discount=4.2, promoter=False, severity="high"):
    text = ("Promoter " if promoter else "") + f"X sold 8,00,000 shares at 412.50, {discount:.1f}% under the day's close."
    nums = [{"label": "qty", "value": "8,00,000", "unit": "shares"}, {"label": "price", "value": "412.50", "unit": "₹"},
            {"label": "vs close", "value": f"{discount:.1f}", "unit": "%"}]
    return note(sym, severity, "deal", "deal", "negative", nums, "Block deal under the close", text=text)


def real_state(buys=("PLAN1", "PLAN2"), held=("HELD1",)):
    orders = [{"action": "BUY", "symbol": s, "ts": s, "qty": 40, "stop": 470.0, "close": 500.0 + 10 * k, "why": "breakout"} for k, s in enumerate(buys)]
    st = {"started": "2026-10-01", "capital": 300_000.0, "cash": 100_000.0, "charges": 0.0, "as_of": DAY, "orders": orders, "decided": DAY,
          "positions": {s: {"qty": 16, "px": 1100.0, "last": 1150.0, "d": "2026-10-20", "stop": 1000.0, "charges": 0.0, "ts": s, "days": 15} for s in held},
          "cap": 10, "allowed": True, "waiting": 0, "filter": {"name": "E+", "state": "FULL", "slots": 10}}
    real._save(st)
    return st


def candles(sym, closes, start=DAY, token=None):
    """Daily bars for `sym` from `start` on weekdays: close = each value (open = close)."""
    from datetime import date
    token = token or (abs(hash(sym)) % 100000 + 1000)
    d = date.fromisoformat(start)
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO instruments (instrument_token, tradingsymbol, exchange, segment, fetched_on) VALUES (?, ?, 'NSE', 'NSE', ?)",
                     (token, sym, now_ist()))
        for c in closes:
            while d.weekday() >= 5:
                d += timedelta(days=1)
            conn.execute("INSERT OR REPLACE INTO candles_daily (instrument_token, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,1000)",
                         (token, d.isoformat(), c, c + 1, c - 1, c))
            d += timedelta(days=1)
    return token


def seed_sugg(kind="skip", symbol="PLAN1", status="open", day=DAY, expires="default", value=20000.0, action=None, confidence="high", for_day=TOMORROW):
    c = {"day": day, "for_day": for_day, "kind": kind, "symbol": symbol, "book": "real", "title": f"{kind} {symbol}".strip(),
         "evidence": [{"icon": "🤖", "text": "high note · pledge 4.43%"}], "action": action, "confidence": confidence,
         "expires_at": f"{TOMORROW}T09:16:00+05:30" if expires == "default" else expires, "note_id": None, "value": value}
    sid = suggest.save(c, {"why": "because", "numbers_checked": True}, "m")
    if status != "open":
        with get_db() as conn:
            conn.execute("UPDATE ai_suggestions SET status = ?, decided_at = ? WHERE id = ?", (status, now_ist(), sid))
    return sid


# --- config, table ---------------------------------------------------------------------------------------------------------

def test_config_fields_and_the_table_guard():
    assert (AIConfig().max_suggestions_open, AIConfig().suggest_muted, AIConfig().why) == (6, "", True)
    assert seed_sugg() and seed_sugg() is None                                                  # one per kind, symbol and day
    assert suggest.get(1)["status"] == "open" and suggest.get(99) is None


# --- birth rules ---------------------------------------------------------------------------------------------------------------

def test_skip_rule_real_plan_and_paper_only(cfg):
    real_state()
    plan("PAPER1", "PLAN1")
    n1 = note("PLAN1")                                                                       # pledge: high confidence, the real skip
    note("PLAN2", category="results", direction="negative", title="Loss widens", numbers=[{"label": "loss", "value": "12.4", "unit": "crore"}])
    note("PAPER1", category="legal", title="SEBI order")                                      # paper-only plan: note only
    note("ELSE", category="pledge")                                                           # not planned: nothing
    note("PLAN1", severity="medium", category="order", direction="positive", title="Order win")   # medium: nothing
    out = {c["symbol"]: c for c in suggest._skip_candidates(cfg, DAY, TOMORROW, suggest._tonight_notes(DAY))}
    assert set(out) == {"PLAN1", "PLAN2", "PAPER1"}
    s = out["PLAN1"]
    assert s["confidence"] == "high" and s["action"]["path"] == "POST /api/real/skip" and s["action"]["args"] == {"symbol": "PLAN1", "on": True}
    assert s["expires_at"] == f"{TOMORROW}T09:16:00+05:30" and s["note_id"] == n1 and s["value"] == 40 * 500.0 and s["for_day"] == TOMORROW
    assert out["PLAN2"]["confidence"] == "medium" and out["PAPER1"]["action"] is None and out["PAPER1"]["book"] == "paper"
    assert any(e["text"].startswith("high note") for e in s["evidence"]) and "BUY 40" in s["evidence"][1]["text"]


def test_skip_rule_deal_discount_and_promoter(cfg):
    real_state(buys=("D1", "D2", "D3"))
    deal_note("D1", 4.2)                                                                      # 4.2% under the close: medium
    deal_note("D2", 4.2, promoter=True)                                                       # promoter selling: high
    deal_note("D3", 1.5, severity="low")                                                      # 1.5%: nothing
    out = {c["symbol"]: c["confidence"] for c in suggest._skip_candidates(cfg, DAY, TOMORROW, suggest._tonight_notes(DAY))}
    assert out == {"D1": "medium", "D2": "high"}


def test_exit_rule_held_names_navigate_only(cfg):
    real_state(held=("HELD1",))
    hold("CASH1")
    note("HELD1")
    note("CASH1", category="results", direction="negative", title="Loss")
    note("HELD1", category="results", direction="positive", title="Good quarter", severity="high")   # positive high: no exit
    out = {c["symbol"]: c for c in suggest._exit_candidates(cfg, DAY, TOMORROW, suggest._tonight_notes(DAY))}
    assert set(out) == {"HELD1", "CASH1"}
    assert out["HELD1"]["action"] == {"label": "Open Real", "path": "ui", "route": "portfolio/real", "hint": "Exit… on the row",
                                      "confirm_text": "Open the Real page: the exit is the row's own button."}
    assert out["HELD1"]["confidence"] == "medium" and out["HELD1"]["value"] == 16 * 1150.0 and out["CASH1"]["book"] == "paper"
    assert "held in Real: 16 at 1,100.00" in out["HELD1"]["evidence"][1]["text"]


def test_flag_rule_deal_and_results_and_one_per_symbol(cfg):
    real_state(buys=("F1", "F2", "F3"))
    deal_note("F1", 3.5)
    with get_db() as conn:
        conn.execute("INSERT INTO corp_events (symbol, date, kind, purpose, descr, fetched_at) VALUES ('F2', '2026-11-20', 'results', 'Results', '', ?)", (now_ist(),))
        conn.execute("INSERT INTO corp_events (symbol, date, kind, purpose, descr, fetched_at) VALUES ('F3', '2026-12-20', 'results', 'Results', '', ?)", (now_ist(),))
    out = {c["symbol"]: c for c in suggest._flag_candidates(cfg, DAY, TOMORROW, suggest._tonight_notes(DAY))}
    assert set(out) == {"F1", "F2"}
    assert out["F1"]["title"] == "Flag F1: Deal 3.5% under the close" and out["F1"]["action"] is None and out["F1"]["confidence"] == "high"
    assert out["F1"]["expires_at"] == f"{TOMORROW}T09:00:00+05:30" and out["F2"]["title"] == "Flag F2: Results in 2 sessions"
    cands = suggest.candidates(cfg, NIGHT)                                                    # F1 is also a skip: the skip wins
    assert [(c["kind"], c["symbol"]) for c in cands] == [("skip", "F1"), ("flag", "F2")] and all(c["day"] == DAY for c in cands)


def test_hold_options_rule_event_day_and_vix(cfg):
    from backend.options import book as obook
    assert suggest._hold_options_candidates(cfg, DAY, TOMORROW) == []                         # no options book yet
    with get_db() as conn:
        conn.execute("INSERT INTO options_state (id, payload) VALUES (1, ?)", (json.dumps({"capital": 300000.0, "cash": 300000.0, "charges": 0.0}),))
        conn.execute("INSERT INTO options_days (day, payload) VALUES (?, ?)", (DAY, json.dumps({"day": DAY, "vix": 22.4})))
    assert obook.has_account()
    out = suggest._hold_options_candidates(cfg, DAY, TOMORROW)
    assert len(out) == 1 and out[0]["action"]["path"] == "POST /api/options/hold" and out[0]["expires_at"] == f"{TOMORROW}T11:00:00+05:30"
    assert out[0]["evidence"][0]["text"] == "India VIX closed at 22.4, over the half-size line 20"
    with get_db() as conn:
        conn.execute("UPDATE options_days SET payload = ?", (json.dumps({"day": DAY, "vix": 12.0}),))
    assert suggest._hold_options_candidates(cfg, DAY, TOMORROW) == []
    with get_db() as conn:
        conn.execute("INSERT INTO corp_events (symbol, date, kind, purpose, descr, fetched_at) VALUES ('NIFTY', ?, 'event', 'RBI monetary policy', '', ?)",
                     (TOMORROW, now_ist()))
    out = suggest._hold_options_candidates(cfg, DAY, TOMORROW)
    assert out and out[0]["evidence"][0]["text"].startswith("event day tomorrow: RBI monetary policy") and out[0]["symbol"] == "NIFTY"


def test_roll_or_exit_rule_thin_oi_or_under_entry(cfg, monkeypatch):
    from backend.futures import book, instruments
    pos = lambda sym, entry, last, side="long": {"symbol": sym, "side": side, "lots": 1, "lot_size": 100, "entry": entry, "last": last, "stop": entry * 0.95, "d": DAY}
    st = {"accounts": {"paper_long": {"positions": {"R1": pos("R1", 500.0, 520.0), "R2": pos("R2", 500.0, 480.0), "R3": pos("R3", 500.0, 520.0)}}}}
    monkeypatch.setattr(book, "_state", lambda: st)
    item = lambda s, due=True: {"account": "paper_long", "symbol": s, "side": "long", "action": "roll", "why": "rule holds", "sessions": 2, "est_cost": 310.0, "due": due}
    monkeypatch.setattr(book, "roll_queue", lambda st_, c, day: {"day": day, "items": [item("R1"), item("R2"), item("R3", due=False)]})
    oi = {"R1": {"near": {"oi": 100000}, "next": {"oi": 5000}}, "R2": {"near": {"oi": 100000}, "next": {"oi": 60000}}, "R3": {"near": {"oi": 100000}, "next": {"oi": 100}}}
    monkeypatch.setattr(instruments, "contracts", lambda s: oi[s])
    out = {c["symbol"]: c for c in suggest._roll_candidates(cfg, DAY, TOMORROW)}
    assert set(out) == {"R1", "R2"}                                                             # R1 thin OI, R2 under entry, R3 not due
    assert "thin" in out["R1"]["evidence"][1]["text"] and "below entry" in out["R2"]["evidence"][1]["text"]
    assert out["R1"]["action"] is None and out["R1"]["expires_at"] == f"{TOMORROW}T09:20:00+05:30" and out["R2"]["value"] == 100 * 480.0


def test_backtest_rule_sunday_only(cfg):
    sunday = datetime(2026, 11, 22, 21, 0, tzinfo=IST)
    with get_db() as conn:
        for i in range(12):
            t = {"symbol": f"B{i}", "side": "long", "out": "2026-11-02", "entry": 100.0, "exit": 94.5, "stop": 94.0, "why": "stop hit", "net": -550.0}
            conn.execute("INSERT INTO futures_trades (account, payload) VALUES ('paper_long', ?)", (json.dumps(t),))
    for i in range(12):
        candles(f"B{i}", [94.5, 95, 96, 97, 98, 99 if i < 9 else 90], start="2026-11-02")   # 9 of 12 recovered
    assert suggest._backtest_candidates(cfg, DAY, NIGHT) == []                                   # a Tuesday: never
    out = suggest._backtest_candidates(cfg, DAY, sunday)
    assert len(out) == 1 and out[0]["confidence"] == "low" and out[0]["expires_at"] is None and out[0]["symbol"] == ""
    assert out[0]["action"]["prefill"] == {"max_stop_pct": 6.0, "roll_mode": "always"} and out[0]["action"]["route"] == "strategies/futures"
    assert out[0]["evidence"][0]["text"].startswith("9 of the last 12 stops")
    cfg.ai.suggest_muted = "backtest"
    assert suggest.candidates(cfg, sunday) == []                                                 # muted: never born


# --- the model's why -------------------------------------------------------------------------------------------------------------

def test_validate_why_numbers_and_brokers():
    ev = "high note · pledge 4.43% · BUY 40 at ~500.00"
    v = suggest.validate_why(json.dumps({"why": "Pledge of 4.43% tonight; the plan buys 40 shares."}), ev)
    assert v == {"why": "Pledge of 4.43% tonight; the plan buys 40 shares.", "numbers_checked": True}
    v = suggest.validate_why(json.dumps({"why": "Pledge of 4.43% tonight, worth 12.5 crore."}), ev)
    assert v["numbers_checked"] is False and "12.5" not in v["why"] and "4.43%" in v["why"]
    assert len(suggest.validate_why(json.dumps({"why": "x" * 400}), ev)["why"]) == suggest.WHY_MAX
    for bad in ("not json", "[1]", json.dumps({"why": ""}), json.dumps({"why": "Buy it on " + "Zerodha"})):
        with pytest.raises(suggest.SuggestInvalid):
            suggest.validate_why(bad, ev)


def test_birth_through_the_fake_model_with_cap_and_fallback(ollama, cfg):
    real_state(buys=("PLAN1", "PLAN2", "PLAN3"))
    for s in ("PLAN1", "PLAN2", "PLAN3"):
        note(s)
    cfg.ai.max_suggestions_open = 2
    ollama.reply = [WHY, "garbage", "still garbage"]                                            # #2: invalid twice -> the evidence line stands in
    res = suggest.birth(cfg, NIGHT)
    assert len(res["born"]) == 2 and res["capped"] == 1 and res["failed"] == 1 and res["tokens"] == 9
    rows = suggest.list_suggestions("open")
    assert [r["symbol"] for r in rows] == ["PLAN2", "PLAN3"] and rows[1]["why_source"] == "model" and rows[0]["why_source"] == "code"   # newest note first
    assert rows[1]["why"].startswith("The promoter pledged 4.43%") and rows[1]["model"] == "qwen2.5:7b-instruct" and rows[1]["prompt_version"] == "sugg-v1"
    assert rows[0]["why"].startswith("high note · Promoter pledge up")
    body = ollama.calls[0][2]
    assert body["model"] == "qwen2.5:7b-instruct" and body["format"] == "json" and "Quote only the numbers above" in body["prompt"]
    assert "pledge 4.43%" not in body["system"] and "never advise" in body["system"]
    ollama.reply = WHY
    assert suggest.birth(cfg, NIGHT)["born"] == [] and suggest.open_count() == 2                 # born already / over the cap: nothing new


def test_run_night_sends_the_evening_list_once(ollama, cfg):
    real_state(buys=("PLAN1",))
    note("PLAN1")
    fake = FakeTG()
    out = suggest.run_night(cfg, NIGHT, Notifier(cfg.alerts, fake))
    assert out["note"].startswith("1 suggestion(s) born") and len(fake.sent) == 1
    text = fake.sent[0][1]
    assert text.startswith("🤖 <b>[AI] 1 suggestion</b> for Wednesday") and "<b>#1</b> ⏸ <b>Skip PLAN1 tomorrow</b> · high" in text
    assert "until 9:16 AM, 18 Nov" in text and "accept N" in text and no_brokers(text)
    suggest.run_night(cfg, NIGHT, Notifier(cfg.alerts, fake))
    assert len(fake.sent) == 1                                                                   # the same key: once a day
    job = suggest.enqueue_tonight(cfg, NIGHT)
    assert job["key"] == f"suggest:{DAY}" and job["dedup"] is False and suggest.enqueue_tonight(cfg, NIGHT)["dedup"] is True
    q.wait(job["id"], 10)


def test_scorecard_line_on_the_first_night_of_a_month(ollama, cfg):
    seed_sugg(day="2026-10-20", status="accepted")
    fake = FakeTG()
    suggest.run_night(cfg, NIGHT, Notifier(cfg.alerts, fake))                                   # no births; October had suggestions
    assert len(fake.sent) == 1 and fake.sent[0][1].startswith("🤖 <b>Suggestions · October 2026</b> · 1 made · 1 accepted")
    suggest.run_night(cfg, NIGHT, Notifier(cfg.alerts, fake))
    assert len(fake.sent) == 1


# --- expiry, accept, dismiss, later ------------------------------------------------------------------------------------------------

def test_expiry_on_the_scheduler_tick(monkeypatch):
    from backend.alerts import jobs
    a = seed_sugg("skip", "A", expires=f"{TOMORROW}T09:16:00+05:30")
    b = seed_sugg("flag", "B", expires=f"{TOMORROW}T09:00:00+05:30")
    c = seed_sugg("backtest", "", expires=None)
    st = LoginState()
    sch = Scheduler(st, notifier_factory=lambda cfg_: Notifier(cfg_, FakeTG()))
    monkeypatch.setattr(jobs, "due_jobs", lambda *a_, **k: [])
    sch.tick(datetime(2026, 11, 18, 8, 59, tzinfo=IST))
    assert [suggest.get(i)["status"] for i in (a, b, c)] == ["open", "open", "open"]
    sch.tick(datetime(2026, 11, 18, 9, 5, tzinfo=IST))
    assert [suggest.get(i)["status"] for i in (a, b, c)] == ["open", "expired", "open"] and suggest.get(b)["decided_at"]
    assert suggest.expire_open(datetime(2026, 11, 18, 9, 30, tzinfo=IST)) == 1 and suggest.get(a)["status"] == "expired"
    assert suggest.get(c)["status"] == "open"                                                   # no expiry: never


def test_accept_runs_the_real_skip_once_and_never_twice(cfg):
    real_state(buys=("PLAN1", "PLAN2"))
    sid = seed_sugg("skip", "PLAN1", action={"label": "Skip PLAN1", "path": "POST /api/real/skip", "args": {"symbol": "PLAN1", "on": True}, "confirm_text": "?"})
    when = datetime(2026, 11, 18, 8, 30, tzinfo=IST)
    out = suggest.accept(sid, cfg, when, who="page")
    assert out["status"] == "accepted" and out["action_result"]["ran"] is True and out["action_result"]["result"]["skipped"] == ["PLAN1"]
    sk = real._skips(real._state(), TOMORROW)
    assert set(sk) == {"PLAN1"} and sk["PLAN1"]["note"] == f"AI suggestion #{sid}" and sk["PLAN1"]["by"] == "you"
    with pytest.raises(suggest.SuggestionState):
        suggest.accept(sid, cfg, when)
    assert real._skips(real._state(), TOMORROW)["PLAN1"]["at"] == sk["PLAN1"]["at"]             # the action ran exactly once
    ui = seed_sugg("exit", "HELD1", action={"label": "Open Real", "path": "ui", "route": "portfolio/real", "hint": "Exit…", "confirm_text": "?"})
    assert suggest.accept(ui, cfg, when)["action_result"] == {"ran": False, "path": "ui", "result": None}
    plain = seed_sugg("flag", "PLAN2")
    assert suggest.accept(plain, cfg, when)["action_result"]["ran"] is False and suggest.get(plain)["status"] == "accepted"


def test_accept_refused_action_stays_accepted_and_options_hold_runs(cfg):
    from backend.options import book as obook
    real_state(buys=("PLAN1",))
    sid = seed_sugg("skip", "GONE", action={"label": "x", "path": "POST /api/real/skip", "args": {"symbol": "GONE", "on": True}, "confirm_text": "?"})
    with pytest.raises(ValueError, match="not in today's plan"):
        suggest.accept(sid, cfg, datetime(2026, 11, 18, 8, 30, tzinfo=IST))
    s = suggest.get(sid)
    assert s["status"] == "accepted" and "not in today's plan" in s["result"]["error"]
    hid = seed_sugg("hold_options", "NIFTY", action={"label": "Hold", "path": "POST /api/options/hold", "args": {}, "confirm_text": "?"})
    when = datetime(2026, 11, 18, 8, 40, tzinfo=IST)
    out = suggest.accept(hid, cfg, when)
    assert out["action_result"]["result"] == {"status": "ok", "hold": True, "day": TOMORROW} and obook._state()["day"]["hold"] is True


def test_dismiss_reasons_and_later_once():
    sid = seed_sugg()
    with pytest.raises(ValueError, match="reason must be one of"):
        suggest.dismiss(sid, "meh")
    s = suggest.dismiss(sid, "knew", who="telegram")
    assert s["status"] == "dismissed" and s["reason"] == "knew" and s["decided_at"]
    with pytest.raises(suggest.SuggestionState, match="already dismissed"):
        suggest.dismiss(sid, "wrong")
    with pytest.raises(suggest.SuggestionState):
        suggest.later(sid)
    sid2 = seed_sugg("exit", "X")
    now = datetime(2026, 11, 18, 8, 0, tzinfo=IST)
    s = suggest.later(sid2, now)
    assert s["expires_at"] == f"{TOMORROW}T13:16:00+05:30" and s["later"] is True
    with pytest.raises(suggest.SuggestionState, match="already pushed once"):
        suggest.later(sid2, now)
    sid3 = seed_sugg("backtest", "", expires=None)
    assert suggest.later(sid3, now)["expires_at"] == "2026-11-18T12:00:00+05:30"               # no expiry: 4 h from now
    with pytest.raises(suggest.SuggestionState, match="no such"):
        suggest.dismiss(999)


# --- outcomes, scorecard, mute -----------------------------------------------------------------------------------------------------------

def test_outcome_scoring_math():
    from backend.options import instruments as oi
    a = seed_sugg("skip", "DOWN", day="2026-11-02", value=20000.0, status="accepted")
    b = seed_sugg("exit", "UP", day="2026-11-02", value=10000.0, status="dismissed")
    h = seed_sugg("hold_options", "NIFTY", day="2026-11-02", for_day="2026-11-03", status="expired")
    r = seed_sugg("roll_or_exit", "RL", day="2026-11-02", value=50000.0, status="expired")
    bt = seed_sugg("backtest", "", day="2026-11-02", status="dismissed", expires=None)
    late = seed_sugg("skip", "LATE", day="2026-11-02", value=1000.0, status="expired")
    candles("DOWN", [100, 99, 98, 98, 97.5, 97], start="2026-11-02")                             # -3% after 5 sessions
    candles("UP", [200, 201, 202, 203, 204, 205], start="2026-11-02")                            # +2.5%
    candles("RL", [100, 100, 99, 99, 98, 98], start="2026-11-02")                                # -2%: against the long lot
    candles("LATE", [100, 101, 102], start="2026-11-02")                                         # not enough bars yet
    with get_db() as conn:
        conn.executemany("INSERT INTO candles_daily (instrument_token, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,0)",
                         [(oi.INDEX_TOKEN, "2026-11-02", 25000, 25100, 24900, 25000), (oi.INDEX_TOKEN, "2026-11-03", 25000, 25300, 24800, 25100)])
    assert suggest.score_outcomes(datetime(2026, 11, 10, 21, 0, tzinfo=IST)) == 5
    o = suggest.get(a)["outcome"]
    assert (o["pct_move"], o["helped"], o["rs"]) == (-3.0, True, 600.0) and o["note"] == "DOWN moved -3.00% in 5 sessions"
    o = suggest.get(b)["outcome"]
    assert (o["pct_move"], o["helped"], o["rs"]) == (2.5, False, -250.0)
    o = suggest.get(h)["outcome"]
    assert (o["pct_move"], o["helped"]) == (2.0, True) and o["rs"] is None                       # NIFTY range 500 / 25000
    o = suggest.get(r)["outcome"]
    assert (o["pct_move"], o["helped"], o["rs"]) == (-2.0, True, 1000.0)
    assert suggest.get(bt)["outcome"]["helped"] is None and suggest.get(late)["outcome"] is None
    assert suggest.score_outcomes(datetime(2026, 11, 10, 21, 0, tzinfo=IST)) == 0                # scored once


def test_scorecard_and_mute(cfg):
    sids = [seed_sugg("skip", "A", status="accepted"), seed_sugg("skip", "B", status="dismissed"), seed_sugg("exit", "C", status="expired"),
            seed_sugg("flag", "D"), seed_sugg("skip", "E", day="2026-10-05", status="accepted")]
    with get_db() as conn:
        for sid, helped, rs in ((sids[0], True, 600.0), (sids[1], True, 300.0), (sids[2], False, -100.0)):
            conn.execute("UPDATE ai_suggestions SET outcome = ? WHERE id = ?", (json.dumps({"helped": helped, "rs": rs, "pct_move": -1.0}), sid))
    sc = suggest.scorecard("2026-11", cfg)
    k = sc["kinds"]
    assert k["skip"] == {"made": 2, "open": 0, "accepted": 1, "dismissed": 1, "expired": 0, "helped_when_accepted": 1, "would_have_helped": 1, "scored": 2, "muted": False}
    assert k["exit"]["expired"] == 1 and k["exit"]["would_have_helped"] == 0 and k["flag"]["open"] == 1 and k["backtest"]["made"] == 0
    assert sc["totals"] == {"month": "2026-11", "made": 4, "accepted": 1, "dismissed": 1, "expired": 1, "accepted_outcome_rs": 600.0,
                            "dismissed_outcome_rs": 300.0, "best_kind": "skip"}
    assert suggest.scorecard("2026-10")["totals"]["made"] == 1 and suggest.scorecard("2026-09")["totals"]["best_kind"] is None
    assert suggest.mute(cfg, "exit", True) == ["exit"] and cfg.ai.suggest_muted == "exit" and suggest.scorecard("2026-11", cfg)["kinds"]["exit"]["muted"]
    assert suggest.mute(cfg, "skip", True) == ["skip", "exit"] and suggest.mute(cfg, "exit", False) == ["skip"]
    with pytest.raises(ValueError):
        suggest.mute(cfg, "nope", True)
    assert suggest.apply_settings(cfg, {"suggest_muted": "flag, skip", "max_suggestions_open": "4", "enabled": True}) == {"enabled": True}
    assert cfg.ai.suggest_muted == "skip,flag" and cfg.ai.max_suggestions_open == 4
    with pytest.raises(ValueError):
        suggest.apply_settings(cfg, {"max_suggestions_open": 0})
    sm = suggest.summary(cfg)
    assert sm["open"] == 1 and sm["by_kind"] == {"flag": 1} and sm["scorecard_month"]["made"] == 4 and sm["muted"] == ["skip", "flag"] and sm["max_open"] == 4
    assert suggest.attention_items(cfg) == [("warn", "🤖 #4 flag D · flag D")]


# --- Telegram -----------------------------------------------------------------------------------------------------------------------------

def test_telegram_commands_two_step(cfg):
    from backend.alerts.commands import BotCommands
    real_state(buys=("PLAN1",), held=("HELD1",))
    sk = seed_sugg("skip", "PLAN1", action={"label": "Skip PLAN1", "path": "POST /api/real/skip", "args": {"symbol": "PLAN1", "on": True}, "confirm_text": "Skip PLAN1 tomorrow?"})
    ex = seed_sugg("exit", "HELD1", action={"label": "Open Real", "path": "ui", "route": "portfolio/real", "hint": "Exit… on the row", "confirm_text": "Open the Real page?"})
    fl = seed_sugg("flag", "PLAN1")
    when = datetime(2026, 11, 18, 8, 30, tzinfo=IST)
    st = LoginState()
    st.config = cfg
    ups = [update(1, "sugg", at=when), update(2, "accept", at=when), update(3, "accept 9", at=when), update(4, f"why {sk}", at=when),
           update(5, f"accept {sk}", at=when), update(6, f"accept {ex}", at=when), update(7, f"yes {fl}", at=when), update(8, f"yes {ex}", at=when),
           update(9, f"dismiss {fl} wrong", at=when), update(10, f"dismiss {fl}", at=when), update(11, "sugg", at=when), update(12, "yes 1", chat=999, at=when)]
    fake = BotTG(ups)
    b, n = BotCommands(st, clock=lambda: when), Notifier(cfg.alerts, fake)
    assert b.poll_once(n) == 11
    lst, which, none, why, acc, confirm, noyes, yes, dis, dis2, lst2 = (h for _, h in fake.sent)
    assert lst.startswith("🤖 <b>[AI] 3 suggestions</b>") and f"<b>#{sk}</b> ⏸" in lst and "accept N" in lst
    assert which.startswith("Which one? accept N") and none == "No suggestion #9."
    assert why.startswith(f"<b>#{sk}</b> ⏸ <b>skip PLAN1</b>") and "🤖 high note · pledge 4.43%" in why and "accept → POST /api/real/skip" in why
    assert acc == f"✅ <b>#{sk} accepted</b> · skip PLAN1 · the action ran." and real._skips(real._state(), TOMORROW)["PLAN1"]["note"] == f"AI suggestion #{sk}"
    assert confirm.startswith(f"🤖 <b>#{ex} · exit HELD1</b>\nOpen the Real page?\nReply <b>yes {ex}</b> within 10 minutes")
    assert noyes.startswith(f"Not changed: nothing is waiting for a yes on #{fl}")
    assert yes == f"✅ <b>#{ex} accepted</b> · exit HELD1 · noted (nothing to run)." and suggest.get(ex)["status"] == "accepted"
    assert dis == f"✗ #{fl} dismissed · wrong · flag PLAN1" and suggest.get(fl)["reason"] == "wrong"
    assert dis2.startswith("Not changed: already dismissed") and lst2 == "🤖 No open suggestions."
    assert all(no_brokers(t) for _, t in fake.sent)
    # the yes window: 10 minutes
    late = seed_sugg("exit", "HELD2", action={"label": "Open Real", "path": "ui", "route": "portfolio/real", "hint": "h", "confirm_text": "?"})
    assert suggest.telegram_accept(late, cfg, when)[0] == "confirm"
    with pytest.raises(suggest.SuggestionState, match="window"):
        suggest.telegram_yes(late, cfg, when + timedelta(minutes=11))
    assert suggest.get(late)["status"] == "open"
    fake2 = BotTG([update(20, "/help", at=when)])
    b.poll_once(Notifier(cfg.alerts, fake2))
    assert "sugg / accept N / yes N / dismiss N [wrong|knew|later] / why N" in fake2.sent[0][1]


# --- the morning flags, attention, the hooks --------------------------------------------------------------------------------------------------

def test_morning_flags_and_the_preview_line(cfg):
    real_state(buys=("PLAN1", "PLAN2"))
    seed_sugg("flag", "PLAN1")
    seed_sugg("flag", "OLD", for_day=DAY)                                                       # another session: not today's flag
    m = real.morning(cfg, datetime(2026, 11, 18, 9, 0, tzinfo=IST))
    assert m["ai_flags"] == {"PLAN1": "flag PLAN1 — because"}
    text = msg.real_preview(real._state()["orders"], "E", "09:16", True, False, m)
    assert "🟢 BUY <b>PLAN1</b> 40" in text and "\n   🤖 <i>AI ·</i> <i>flag PLAN1 — because</i>\n🟢 BUY <b>PLAN2</b>" in text
    assert "🤖" not in msg.real_preview(real._state()["orders"], "E", "09:16", True, False, {**m, "ai_flags": {}})


def test_attention_lists_open_high_confidence_suggestions(cfg):
    from backend.engine import attention
    seed_sugg("skip", "A", confidence="high")
    seed_sugg("exit", "B", confidence="medium")
    items = attention.signals(cfg, False)["ai"]["items"]
    assert ("warn", "🤖 #1 skip A · skip A") in [(i["level"], i["text"]) for i in items] and not any("#2" in i["text"] for i in items)


def test_run_eod_queues_why_and_suggest_after_the_notes(monkeypatch, ollama, cfg):
    from backend.ai import why
    st = LoginState()
    st.config = cfg
    sch = Scheduler(st, notifier_factory=lambda c: Notifier(c, FakeTG()))
    calls = []
    monkeypatch.setattr(sch, "_run_eod", lambda now, force: {"status": "ok"})
    monkeypatch.setattr(notes, "enqueue_tonight", lambda c, now, notifier=None: calls.append("notes") or {"queued": 3})
    monkeypatch.setattr(why, "enqueue_tonight", lambda c, now, notifier=None: calls.append("why") or {"id": 1})
    monkeypatch.setattr(suggest, "enqueue_tonight", lambda c, now, notifier=None: calls.append("suggest") or {"id": 2})
    out = sch.run_eod(NIGHT)
    assert calls == ["notes", "why", "suggest"] and out["ai_why"] == {"id": 1} and out["ai_suggest"] == {"id": 2}
    cfg.ai.enabled = False
    assert sch.run_eod(NIGHT)["ai_suggest"] == {"status": "off"} and len(calls) == 3
    cfg.ai.enabled = True
    monkeypatch.setattr(suggest, "enqueue_tonight", lambda c, now, notifier=None: (_ for _ in ()).throw(RuntimeError("boom")))
    assert sch.run_eod(NIGHT)["ai_suggest"]["status"] == "failed"                               # never breaks the evening job


# --- the routes ------------------------------------------------------------------------------------------------------------------------------------

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


def test_routes(api):
    tc = api
    real_state(buys=("PLAN1",))
    sk = seed_sugg("skip", "PLAN1", action={"label": "Skip PLAN1", "path": "POST /api/real/skip", "args": {"symbol": "PLAN1", "on": True}, "confirm_text": "?"})
    ex = seed_sugg("exit", "HELD1", confidence="medium")
    d = tc.get("/api/ai").json()["suggestions"]
    assert d["open"] == 2 and d["by_kind"] == {"skip": 1, "exit": 1} and d["scorecard_month"]["made"] == 2 and d["muted"] == []
    r = tc.get("/api/ai/suggestions").json()
    assert r["open"] == 2 and [x["id"] for x in r["rows"]] == [ex, sk] and set(r["rows"][0]) >= {"id", "kind", "symbol", "title", "why", "evidence", "action",
                                                                                                "confidence", "expires_at", "status", "outcome", "icon"}
    assert tc.get("/api/ai/suggestions?status=weird").status_code == 400 and tc.get("/api/ai/suggestions?kind=exit").json()["rows"][0]["id"] == ex
    assert tc.get(f"/api/ai/suggestions/{sk}").json()["symbol"] == "PLAN1" and tc.get("/api/ai/suggestions/99").status_code == 404
    assert tc.post("/api/ai/suggestions/99/accept").status_code == 404
    a = tc.post(f"/api/ai/suggestions/{sk}/accept").json()
    assert a["status"] == "accepted" and a["action_result"]["ran"] is True and set(real._skips(real._state(), now_ist()[:10])) == {"PLAN1"}
    assert tc.post(f"/api/ai/suggestions/{sk}/accept").status_code == 409 and tc.post(f"/api/ai/suggestions/{sk}/dismiss", json={"reason": "wrong"}).status_code == 409
    assert tc.post(f"/api/ai/suggestions/{ex}/dismiss", json={"reason": "bogus"}).status_code == 400
    assert tc.post(f"/api/ai/suggestions/{ex}/later").json()["later"] is True and tc.post(f"/api/ai/suggestions/{ex}/later").status_code == 409
    assert tc.post(f"/api/ai/suggestions/{ex}/dismiss", json={"reason": "knew"}).json()["reason"] == "knew"
    assert tc.get("/api/ai/suggestions?status=open").json()["rows"] == []
    sc = tc.get("/api/ai/suggestions/scorecard").json()
    assert sc["kinds"]["skip"]["accepted"] == 1 and sc["kinds"]["exit"]["dismissed"] == 1 and tc.get("/api/ai/suggestions/scorecard?month=nov").status_code == 400
    assert tc.get("/api/ai/suggestions/scorecard?month=2026-10").json()["totals"]["made"] == 0
    assert tc.post("/api/ai/suggestions/mute", json={"kind": "flag"}).json() == {"muted": ["flag"]} and tc.post("/api/ai/suggestions/mute", json={"kind": "x"}).status_code == 400
    assert tc.get("/api/ai/suggestions/scorecard").json()["kinds"]["flag"]["muted"] is True
    r = tc.put("/api/ai/settings", json={"suggest_muted": "exit", "max_suggestions_open": 3, "why": False})
    assert r.status_code == 200 and tc.get("/api/ai").json()["settings"]["suggest_muted"] == "exit"
    s = tc.get("/api/ai").json()["settings"]
    assert s["max_suggestions_open"] == 3 and s["why"] is False and tc.put("/api/ai/settings", json={"max_suggestions_open": 99}).status_code == 400
    with get_db() as conn:
        conn.execute("INSERT INTO ai_jobs (created, kind, key, status) VALUES (?, 'night', 'suggest:x', 'running')", (now_ist(),))
    assert tc.post("/api/ai/suggestions/run").status_code == 409
    with get_db() as conn:
        conn.execute("UPDATE ai_jobs SET status = 'done'")
    job = tc.post("/api/ai/suggestions/run").json()
    assert job["key"] == f"suggest:{notes.night_day(datetime.now(IST), AppConfig())}" and job["dedup"] is False   # AI off on the app's config: the guard skips it
    q.wait(job["id"], 10)
