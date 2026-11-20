"""🤖 Local AI, PR 3 · C (the morning brief): the settings, the guard's brief window, the facts from the books (Real
morning plan, options chain line, the night's high notes, results on held names), the validator (≤ 5 lines of
≤ 160 chars, numbers only from the facts, no broker names), the run (one call, one retry, the row, the message, once a
day), the scheduler's due rule and job, the readers and the routes. Everything against the fake Ollama; no network."""

import json
from datetime import datetime

import pytest

from backend.ai import brief, client, control, guard, notes
from backend.ai import queue as q
from backend.alerts.dispatch import Notifier
from backend.alerts.jobs import Scheduler
from backend.config import IST, AIConfig, AlertsConfig, AppConfig
from backend.database.models import get_db, now_ist
from backend.engine import real
from backend.options import book as obook
from tests.test_ai_infra import NIGHT, DAY, no_brokers
from tests.test_ai_notes import FakeNotes, _note_row, hold
from tests.test_alerts import FakeTG
from tests.test_login import LoginState

TODAY = now_ist()[:10]
LINES = ["Real sends 3 orders at 9:16 AM: EXIT S0; BUY S1, S2. Cash after 103,699; 9 free slots.",
         "S3 is skipped by the results guard.",
         "Options: last chain PCR 0.82, put wall 25,000, call wall 25,500; VIX 13.2."]


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeNotes(reply=json.dumps(LINES))
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


def now_at(hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    d = datetime.fromisoformat(TODAY)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def seed_real():
    """A Real account: one held name (S0, EXIT planned), BUYs S1 S2, S3 skipped by the results guard."""
    orders = [{"action": "EXIT", "symbol": "S0", "ts": "S0", "qty": 16, "why": "close under SMA50"}]
    for k, s in enumerate(("S1", "S2", "S3")):
        orders.append({"action": "BUY", "symbol": s, "ts": s, "qty": 40, "stop": 470.0, "close": 500.0 + k, "why": f"breakout #{k + 1}"})
    st = {"started": "2026-10-01", "capital": 300_000.0, "cash": 103_699.0, "charges": 0.0, "as_of": TODAY,
          "positions": {"S0": {"qty": 16, "px": 1100.0, "last": 1151.0, "d": "2026-10-20", "stop": 1000.0, "charges": 0.0, "ts": "S0", "days": 15}},
          "orders": orders, "decided": TODAY, "cap": 10, "allowed": True, "waiting": 0,
          "skips": {TODAY: {"S3": {"by": "results guard", "at": now_ist(), "note": "results soon"}}},
          "filter": {"name": "E+", "state": "FULL", "slots": 10}}
    real._save(st)
    return st


def seed_options(cfg, vix=13.2):
    st = obook._new(cfg, "2026-10-01")
    st["day"] = {**obook._new_day(st, TODAY), "vix": vix, "chain": {"at": f"{TODAY}T09:30:00", "pcr": 0.82, "walls": {"support": 25000, "resistance": 25500},
                                                                     "line": "bearish 3/5", "vix": vix, "strikes": []}}
    obook._save(st)


# --- settings, guard ----------------------------------------------------------------------------------------------------------

def test_config_fields_and_asks_table():
    c = AIConfig()
    assert (c.brief, c.brief_time, c.ask) == (True, "09:00", True)
    with get_db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(ai_asks)")]
    assert cols[:9] == ["id", "created", "who", "question", "tool", "args", "answer", "ms", "model"]


def test_guard_brief_window_and_ask_any_time(ollama, cfg):
    cfg.alerts.job_time = "17:30"                                                    # the night window: eod's end to 6:00
    assert guard.allowed("brief", now_at("06:31"), cfg) == (True, "ok")              # after night_end, before 9:00
    assert guard.allowed("night", now_at("06:31"), cfg)[0] is False                  # the night window itself ended
    assert guard.allowed("brief", now_at("09:10"), cfg) == (True, "ok")              # 9:00 brief: before the 9:16 send
    ok, why = guard.allowed("brief", now_at("09:16"), cfg)
    assert ok is False and why == "outside the brief window (evening job's end to 9:15 AM)"
    cfg.ai.brief = False
    assert guard.allowed("brief", now_at("06:31"), cfg) == (False, "morning brief off")
    for hhmm in ("03:00", "12:30", "22:15"):
        assert guard.allowed("ask", now_at(hhmm), cfg) == (True, "ok")                 # 🤖 ask: any time
    cfg.ai.ask = False
    assert guard.allowed("ask", now_at("12:30"), cfg) == (False, "ask off")
    cfg.ai.ask, cfg.ai.day_answers = True, False
    assert guard.allowed("ask", now_at("12:30"), cfg) == (False, "day answers off")


# --- the facts -------------------------------------------------------------------------------------------------------------

def test_facts_empty_without_books(cfg):
    f = brief.facts(cfg, now_at("06:30"))
    assert f["empty"] is True and f["lines"] == [f"Day: {brief.day_label(TODAY)}, 6:30 AM"]


def test_facts_from_the_books(cfg):
    seed_real()
    seed_options(cfg)
    hold("HELD")
    with get_db() as conn:
        conn.execute("INSERT INTO corp_events VALUES ('HELD', ?, 'results', 'Results', '', ?)", (TODAY, now_ist()))
    _note_row("S1", "high", "plan", "Pledge raised", day=TODAY, key="b1")
    f = brief.facts(cfg, now_at("06:30"))
    assert f["empty"] is False
    r = f["real"]
    assert r["exits"] == ["S0"] and r["buys"] == ["S1", "S2"] and r["skipped"] == ["S3 (results guard)"] and r["cash"] == 103699.0 and r["free"] == 9
    o = f["options"]
    assert (o["pcr"], o["support"], o["resistance"], o["vix"], o["line"]) == (0.82, 25000, 25500, 13.2, "bearish 3/5")
    assert f["notes"][0]["symbol"] == "S1" and f["results"] == [f"HELD {brief.day_label(TODAY)} (in 0 sessions)"]
    text = "\n".join(f["lines"])
    assert "Real: 3 orders at 9:16 AM: EXIT S0; BUY S1, S2. Cash after 103,699; 9 free slots of 10. Skipped: S3 (results guard)." in text
    assert "last chain PCR 0.82, put wall 25,000, call wall 25,500 (bearish 3/5); VIX 13.2;" in text
    assert "Note (high, plan): S1 filing: Pledge raised." in text and "Results within 3 sessions on held names: HELD" in text


def test_facts_mark_real_off_and_vix_over_half(cfg):
    seed_real()
    cfg.real.enabled = False
    seed_options(cfg, vix=22.5)
    cfg.options.event_days = TODAY
    text = "\n".join(brief.facts(cfg, now_at("06:30"))["lines"])
    assert "(real sending is OFF: a dry run only)" in text and "VIX 22.5 (over the half-size line)" in text and "today is an event day" in text


def test_a_failing_section_is_left_out(cfg, monkeypatch):
    seed_real()
    monkeypatch.setattr(obook, "has_account", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    f = brief.facts(cfg, now_at("06:30"))
    assert f["options"] is None and f["real"]["exits"] == ["S0"] and f["empty"] is False


# --- the validator ------------------------------------------------------------------------------------------------------------

SRC = "Real: 3 orders at 9:16 AM: EXIT S0; BUY S1. Cash after 103,699; 9 free slots. VIX 13.2. PCR 0.82."


def test_validate_list_and_dict_shapes():
    assert brief.validate('["Real sends 3 orders at 9:16 AM.", "VIX 13.2."]', SRC) == {"lines": ["Real sends 3 orders at 9:16 AM.", "VIX 13.2."], "numbers_checked": True}
    assert brief.validate('{"lines": ["Cash after 103,699."]}', SRC)["lines"] == ["Cash after 103,699."]
    assert brief.validate('{"brief": ["PCR 0.82 on the last chain."]}', SRC)["numbers_checked"] is True


def test_validate_caps_lines_and_length():
    v = brief.validate(json.dumps([f"line {i} " + "x" * 200 for i in range(1, 8)]), SRC)
    assert len(v["lines"]) == 5 and all(len(ln) <= 160 for ln in v["lines"]) and v["lines"][0].endswith("…")


def test_validate_strips_numbers_not_in_facts_and_rejects_brokers():
    v = brief.validate('["Cash after 1.1 lakh, 4 free slots.", "VIX 13.2."]', SRC)
    assert v["numbers_checked"] is False and "1.1" not in v["lines"][0] and v["lines"][1] == "VIX 13.2."
    with pytest.raises(brief.BriefInvalid, match="broker"):
        brief.validate('["Orders go to Zerodha at 9:16 AM."]', SRC)
    for bad in ("not json", "[]", '{"a": {"b": 1}}', '[""]'):
        with pytest.raises(brief.BriefInvalid):
            brief.validate(bad, SRC)


# --- the run ----------------------------------------------------------------------------------------------------------------------

def test_run_writes_the_row_and_sends_once(ollama, cfg):
    seed_real()
    fake = FakeTG()
    n = Notifier(cfg.alerts, fake)
    out = brief.run(cfg, now_at("06:30"), n)
    assert out["id"] and out["tokens"] == 9 and out["note"] == "brief · 3 line(s)"
    body = ollama.calls[-1][2]
    assert body["model"] == NIGHT and body["format"] == "json" and "Facts:\n- Day:" in body["prompt"] and "never advise" in body["system"]
    row = notes.get_note(out["id"])
    assert row["kind"] == "brief" and row["day"] == TODAY and row["symbol"] is None and row["severity"] == "info" and row["source_key"] == f"brief:{TODAY}"
    assert row["title"] == f"Morning brief · {brief.day_label(TODAY)}" and row["prompt_version"] == "brief-v1" and row["alerted"] is True
    assert row["text"].split("\n")[0].startswith("Real sends 3 orders") and len(row["text"].split("\n")) == 3
    assert len(fake.sent) == 1
    html = fake.sent[0][1]
    assert html.startswith(f"☀️ <b>[AI] Morning brief · {brief.day_label(TODAY)} · 6:30 AM</b>\n<i>AI ·</i> Real sends 3 orders at 9:16 AM")
    assert html.endswith("<i>written from the plans and filings · no action taken</i>") and no_brokers(html)
    assert brief.run(cfg, now_at("06:31"), n) == {"skip": "today's brief exists"} and len(fake.sent) == 1
    assert brief.status() == {"last_at": row["created"], "lines": row["text"].split("\n"), "day": TODAY, "id": out["id"]}


def test_run_retries_once_then_fails(ollama, cfg):
    seed_real()
    ollama.reply = ["nonsense", json.dumps(["Real sends 3 orders at 9:16 AM."])]
    out = brief.run(cfg, now_at("06:30"), Notifier(cfg.alerts, FakeTG()))
    assert out["tokens"] == 18 and len(ollama.calls) == 2 and "was not valid: not JSON" in ollama.calls[-1][2]["system"]
    assert brief.latest()["lines"] == ["Real sends 3 orders at 9:16 AM."]
    with get_db() as conn:
        conn.execute("DELETE FROM ai_notes")
    ollama.reply = ["nonsense", '["Send it through Zerodha."]']
    with pytest.raises(brief.BriefInvalid):
        brief.run(cfg, now_at("06:30"), Notifier(cfg.alerts, FakeTG()))
    assert brief.latest() is None


def test_run_skips_without_facts(ollama, cfg):
    assert brief.run(cfg, now_at("06:30"), Notifier(cfg.alerts, FakeTG())) == {"skip": "nothing to brief"} and ollama.calls == []


# --- the job ----------------------------------------------------------------------------------------------------------------------

def test_due_rule(cfg):
    assert brief.due(now_at("09:00"), cfg, set(), True) == ["ai_brief"] and brief.due(now_at("09:14"), cfg, set(), True) == ["ai_brief"]
    assert brief.due(now_at("08:59"), cfg, set(), True) == [] and brief.due(now_at("09:15"), cfg, set(), True) == []
    assert brief.due(now_at("09:00"), cfg, set(), False) == [] and brief.due(now_at("09:00"), cfg, {"ai_brief"}, True) == []
    cfg.ai.brief_time = "07:15"
    assert brief.due(now_at("07:00"), cfg, set(), True) == [] and brief.due(now_at("07:15"), cfg, set(), True) == ["ai_brief"]
    cfg.ai.brief = False
    assert brief.due(now_at("07:15"), cfg, set(), True) == []
    cfg.ai.brief, cfg.ai.enabled = True, False
    assert brief.due(now_at("07:15"), cfg, set(), True) == []


def test_scheduler_queues_the_brief_once(ollama, cfg, monkeypatch):
    from backend.alerts import jobs as aj
    from backend.ingest import holidays as hol
    monkeypatch.setattr(hol, "is_trading_day", lambda d, holidays=None: True)
    monkeypatch.setattr(aj, "due_jobs", lambda *a, **k: [])                              # only the books' own due rules
    seed_real()
    st = LoginState()
    st.config = cfg
    fake = FakeTG()
    sch = Scheduler(st, notifier_factory=lambda c: Notifier(c, fake))
    when = now_at("09:01")
    jobs = sch.tick(when)
    assert "ai_brief" in jobs
    q._items.join()
    with get_db() as conn:
        run = conn.execute("SELECT status, message FROM job_runs WHERE job = 'ai_brief'").fetchall()
        job = conn.execute("SELECT kind, key, status, model FROM ai_jobs ORDER BY id DESC LIMIT 1").fetchone()
    assert len(run) == 1 and run[0]["status"] == "ok" and run[0]["message"].startswith("queued as AI job")
    assert tuple(job) == ("brief", f"brief:{TODAY}", "done", NIGHT)
    assert brief.latest()["day"] == TODAY and any(h.startswith("☀️ <b>[AI] Morning brief") for _, h in fake.sent)
    assert "ai_brief" not in sch.tick(now_at("06:32"))                                  # recorded: once a day


def test_queue_runs_brief_under_its_window(ollama, cfg):
    seed_real()
    job = brief.enqueue(cfg, now_at("06:31"))
    assert job["dedup"] is False and brief.enqueue(cfg, now_at("06:31"))["dedup"] is True
    row = q.wait(job["id"], 10)
    assert row["status"] == "done" and row["note"] == "brief · 3 line(s)"
    with get_db() as conn:
        conn.execute("DELETE FROM ai_notes")
    cfg.ai.brief = False
    job = brief.enqueue(cfg, now_at("06:31"))
    row = q.wait(job["id"], 10)
    assert row["status"] == "skipped" and row["error"] == "morning brief off"


def test_message_text_is_html_escaped_and_12_hour():
    from backend.alerts import messages as msg
    html = msg.ai_brief("2026-11-17", "06:30", ["A & B <ok>", "second"])
    assert html == ("☀️ <b>[AI] Morning brief · Tue 17 Nov · 6:30 AM</b>\n<i>AI ·</i> A &amp; B &lt;ok&gt;\n<i>AI ·</i> second\n"
                    "<i>written from the plans and filings · no action taken</i>")


# --- the routes --------------------------------------------------------------------------------------------------------------------

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


def test_routes_show_the_brief(api, ollama, cfg):
    tc = api
    d = tc.get("/api/ai").json()
    assert d["brief"] == {"last_at": None, "lines": [], "day": None, "id": None} and d["asks"] == {"today": 0, "last": None}
    assert d["settings"]["brief"] is True and d["settings"]["brief_time"] == "09:00" and d["settings"]["ask"] is True
    seed_real()
    out = brief.run(cfg, now_at("06:30"), Notifier(cfg.alerts, FakeTG()))
    d = tc.get("/api/ai").json()
    assert d["brief"]["id"] == out["id"] and len(d["brief"]["lines"]) == 3 and d["brief"]["day"] == TODAY
    r = tc.get("/api/ai/notes?kind=brief").json()
    assert r["total"] == 1 and r["rows"][0]["kind"] == "brief" and r["rows"][0]["title"].startswith("Morning brief")
    assert DAY not in (r["rows"][0]["model"] or "")                                        # the night model wrote it


def test_validate_accepts_the_shapes_json_mode_produces():
    """Under JSON mode the 7B model answers with an object: {"lines": [...]}, numbered string values, a list of
    {"text": ...}, or one string with newlines — all become the list of lines; a bare list still works."""
    facts = "Real sends 3 orders at 9:16 AM. VIX 15.9."
    want = ["Real sends 3 orders at 9:16 AM", "VIX 15.9"]
    for raw in (json.dumps(want), json.dumps({"lines": want}), json.dumps({"brief": want}),
                json.dumps({"line1": want[0], "line2": want[1]}), json.dumps([{"text": want[0]}, {"line": want[1]}]),
                json.dumps({"morning_brief": want[0] + "\n" + want[1]}), json.dumps({"summary": {"k": "x"}, "lines": want})):
        assert brief.validate(raw, facts)["lines"] == want, raw
    with pytest.raises(brief.BriefInvalid):
        brief.validate(json.dumps({"a": {"b": 1}}), facts)
    assert '{"lines": ["…", "…"]}' in brief.SYSTEM_PROMPT
