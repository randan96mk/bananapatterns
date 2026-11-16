"""🤖 Local AI, PR 3 · D (ask the app): the whitelist of read-only tools and their typed arguments (coerced and bounded
by code), the tool pick (JSON, the refusal for an unknown tool), the tools on seeded data, the answer check (numbers
only from the tool's result, no broker names, the length cap), the whole flow through the queue (day model only, any
time, the `ai_asks` row, the retry and the rejection, the timeout), the Telegram `ask <question>` flow, attention and
the routes. Everything against the fake Ollama; no network."""

import json
from datetime import datetime

import pytest

from backend.ai import ask, client, control, guard
from backend.ai import queue as q
from backend.alerts import messages as msg
from backend.alerts.commands import BotCommands
from backend.alerts.dispatch import Notifier
from backend.config import AIConfig, AlertsConfig, AppConfig
from backend.database.models import get_db, now_ist
from tests.test_ai_brief import seed_futures, seed_real
from tests.test_ai_infra import DAY, NIGHT, at, no_brokers
from tests.test_ai_notes import FakeNotes, _note_row, filing, plan
from tests.test_login import BotTG, update

TODAY = now_ist()[:10]
PICK = {"tool": "why_skipped", "args": {"symbol": "S3", "day": TODAY}}
ANSWER = {"answer": "S3 was skipped by the results guard (results soon). It is #1 in the plan."}


def by_prompt(pick=PICK, answer=ANSWER):
    """A fake model: the pick JSON for the routing prompt, the answer JSON for the answer prompt."""
    def reply(body):
        return json.dumps(pick if "You route a question" in (body.get("system") or "") else answer)
    return reply


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeNotes(reply=by_prompt())
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


def gen_calls(fake):
    return [b for _, p, b in fake.calls if p == "/api/generate"]


# --- the whitelist and the arguments -------------------------------------------------------------------------------------------

def test_tool_list_matches_the_plan():
    assert ask.TOOL_NAMES == ("real_morning", "real_summary", "paper_summary", "futures_summary", "options_summary", "options_days", "trades",
                              "filings", "notes", "suggestions", "plan", "why_skipped", "positions_near_stop", "backtest_f", "levels")
    args = {k: {a: t.__name__ for a, t in v["args"].items()} for k, v in ask.TOOLS.items()}
    assert args["real_morning"] == {"day": "str"} and args["trades"] == {"book": "str", "days": "int"} and args["why_skipped"] == {"symbol": "str", "day": "str"}
    assert args["positions_near_stop"] == {"pct": "float"} and args["options_days"] == {"limit": "int"} and args["backtest_f"] == {}
    text = ask.tool_list_text()
    assert text.startswith("real_morning(day: str) — ") and "filings(symbol: str, days: int) — " in text and no_brokers(text)
    assert all(callable(v["fn"]) for v in ask.TOOLS.values())


def test_coerce_args_defaults_bounds_and_cleaning():
    assert ask.coerce_args("trades", {}) == {"book": "real", "days": 7}
    assert ask.coerce_args("trades", {"book": "Futures", "days": "400", "extra": 1}) == {"book": "futures", "days": 90}
    assert ask.coerce_args("trades", {"book": "crypto", "days": 0}) == {"book": "real", "days": 1}
    assert ask.coerce_args("real_morning", {"day": "thursday"}) == {"day": ask._today()} and ask.coerce_args("plan", {"day": "2026-11-13T09:00"}) == {"day": "2026-11-13"}
    assert ask.coerce_args("positions_near_stop", {"pct": "abc"}) == {"pct": 2.0} and ask.coerce_args("positions_near_stop", {"pct": 99}) == {"pct": 20.0}
    assert ask.coerce_args("filings", {"symbol": "nmdc'; drop table --", "days": 3.9}) == {"symbol": "nmdc drop table --", "days": 3}
    assert ask.coerce_args("suggestions", {"status": "weird"}) == {"status": "open"} and ask.coerce_args("options_days", {"limit": 500}) == {"limit": 30}
    assert ask.coerce_args("backtest_f", {"anything": 1}) == {}


def test_parse_pick_whitelist():
    assert ask.parse_pick(json.dumps(PICK)) == ("why_skipped", {"symbol": "S3", "day": TODAY})
    assert ask.parse_pick('{"tool": "Real-Summary"}') == ("real_summary", {})
    assert ask.parse_pick('{"name": "trades", "arguments": {"book": "paper"}}') == ("trades", {"book": "paper", "days": 7})
    for raw in ('{"tool": "none"}', '{"tool": "place_order", "args": {}}', '{"tool": "os.system"}', "no json", "[1, 2]", "{}"):
        assert ask.parse_pick(raw) == (None, {}), raw


# --- the tools on seeded data --------------------------------------------------------------------------------------------------

def test_why_skipped_and_plan_and_morning(cfg):
    seed_real()
    plan("S3", "S1")
    w = ask.run_tool(cfg, "why_skipped", {"symbol": "s3", "day": TODAY})
    assert w["real_skip"]["by"] == "results guard" and w["plan_rank"] == 1 and w["in_real_orders"] is True and w["unskipped"] is False
    assert ask.run_tool(cfg, "why_skipped", {"symbol": "ZZ", "day": TODAY})["real_skip"] is None
    p = ask.run_tool(cfg, "plan", {"day": TODAY})
    assert [i["symbol"] for i in p["items"]] == ["S3", "S1"] and p["items"][0]["rank"] == 1 and p["as_of"] == TODAY
    m = ask.run_tool(cfg, "real_morning", {"day": TODAY})
    assert m["skipped"] == ["S3"] and m["going_out"] == {"exits": 1, "buys": 2, "value": 40 * 500.0 + 40 * 501.0} and m["free"] == 9
    assert m["orders"][0] == {"symbol": "S0", "action": "EXIT", "qty": 16, "close": 1151.0, "stop": None, "why": "close under SMA50",
                              "results_date": None, "results_in": None, "status": None, "sent": False}
    s = ask.run_tool(cfg, "real_summary", {})
    assert s["positions"][0]["symbol"] == "S0" and s["account"]["cash"] == 103699.0 and "log" not in s and "curve" not in s


def test_filings_notes_trades_and_near_stop(cfg):
    seed_real()
    seed_futures(cfg)
    filing(1, "S0", "Acquisition", "deal one", 1)
    _note_row("S0", "high", "held", "Weak quarter", day=TODAY, key="a1")
    with get_db() as conn:
        conn.execute("INSERT INTO real_trades (payload) VALUES (?)", (json.dumps({"symbol": "OLD", "in": "2026-01-02", "out": ask._since(20), "net": -10.0}),))
        conn.execute("INSERT INTO real_trades (payload) VALUES (?)", (json.dumps({"symbol": "NEW", "in": TODAY, "out": TODAY, "net": 25.5, "why": "stop"}),))
    f = ask.run_tool(cfg, "filings", {"symbol": "s0", "days": 3})
    assert f["symbol"] == "S0" and f["filings"][0]["category"] == "Acquisition" and f["deals"] == []
    n = ask.run_tool(cfg, "notes", {"symbol": "S0", "days": 7})
    assert n["notes"][0]["title"] == "Weak quarter" and n["notes"][0]["severity"] == "high"
    t = ask.run_tool(cfg, "trades", {"book": "real", "days": 7})
    assert [x["symbol"] for x in t["trades"]] == ["NEW"] and ask.run_tool(cfg, "trades", {"book": "real", "days": 90})["trades"][-1]["symbol"] == "OLD"
    near = ask.run_tool(cfg, "positions_near_stop", {"pct": 10})
    assert [(x["book"], x["symbol"], x["distance_pct"]) for x in near["positions"]] == [("futures", "HELDF", 8.65)]
    assert ask.run_tool(cfg, "positions_near_stop", {"pct": 5})["positions"] == []            # S0: 13.1% away
    fs = ask.run_tool(cfg, "futures_summary", {})
    assert fs["positions"][0]["symbol"] == "HELDF" and fs["roll_day"] == "2026-11-20" and fs["warnings"]["results"][0]["symbol"] == "HELDF"


def test_suggestions_tool_without_and_with_rows(cfg):
    assert ask.run_tool(cfg, "suggestions", {"status": "open"}) == {"status": "open", "suggestions": []}
    with get_db() as conn:
        conn.execute("INSERT INTO ai_suggestions (created, day, kind, symbol, title, status) VALUES (?, ?, 'skip', 'S3', 'Skip S3', 'open')", (now_ist(), TODAY))
    s = ask.run_tool(cfg, "suggestions", {"status": "open"})
    assert s["suggestions"][0]["symbol"] == "S3" and ask.run_tool(cfg, "suggestions", {"status": "expired"})["suggestions"] == []


# --- the answer check ------------------------------------------------------------------------------------------------------------

SRC = '{"symbol":"S3","real_skip":{"by":"results guard","results_in":2},"plan_rank":1,"cash":103699.0}'


def test_validate_answer_numbers_and_brokers():
    assert ask.validate_answer('{"answer": "Skipped by the results guard: results in 2 sessions; #1 in the plan."}', SRC) == \
        "Skipped by the results guard: results in 2 sessions; #1 in the plan."
    assert ask.validate_answer('{"answer": "Cash is 1,03,699."}', SRC) == "Cash is 1,03,699."
    assert ask.validate_answer('"a bare string"', SRC) == "a bare string"
    assert ask.validate_answer('{"answer": "On the 13th it was skipped."}', SRC, "why skipped on the 13th") == "On the 13th it was skipped."
    with pytest.raises(ask.AskInvalid, match="figure"):
        ask.validate_answer('{"answer": "Cash after is 1.1 lakh, 4 slots free."}', SRC)
    with pytest.raises(ask.AskInvalid, match="broker"):
        ask.validate_answer('{"answer": "The order went to Zerodha."}', SRC)
    with pytest.raises(ask.AskInvalid):
        ask.validate_answer("not json", SRC)
    with pytest.raises(ask.AskInvalid):
        ask.validate_answer('{"answer": ""}', SRC)
    assert len(ask.validate_answer(json.dumps({"answer": "x" * 900}), SRC)) == 400


# --- the flow through the queue ------------------------------------------------------------------------------------------------

def test_ask_flow_writes_the_row(ollama, cfg):
    seed_real()
    plan("S3")
    row = ask.ask(cfg, "  why did we   skip S3 today ", "page")
    assert row["ok"] is True and row["status"] == "done" and row["tool"] == "why_skipped" and row["args"] == {"symbol": "S3", "day": TODAY}
    assert row["answer"] == ANSWER["answer"] and row["ms"] == 1700 and row["who"] == "page" and row["model"] == DAY
    assert row["question"] == "why did we skip S3 today" and row["read"] == f"why_skipped(symbol=S3, day={TODAY})" and row["error"] is None
    calls = gen_calls(ollama)
    assert len(calls) == 2 and all(b["model"] == DAY and b["format"] == "json" for b in calls) and NIGHT not in {b["model"] for b in calls}
    assert "Question: why did we skip S3 today" in calls[0]["prompt"] and "real_morning(day: str)" in calls[0]["system"]
    assert '"real_skip":{"by":"results guard"' in calls[1]["prompt"] and "never advise" in calls[1]["system"]
    with get_db() as conn:
        job = conn.execute("SELECT kind, key, status, model FROM ai_jobs ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(job) == ("ask", f"ask:{row['id']}", "done", DAY)
    assert ask.stats()["today"] == 1 and ask.stats()["last"]["id"] == row["id"] and ask.list_asks(5)[0]["answer"] == ANSWER["answer"]


def test_ask_refuses_an_unknown_tool(ollama, cfg):
    ollama.reply = by_prompt(pick={"tool": "place_order", "args": {"symbol": "S3"}})
    row = ask.ask(cfg, "buy S3 now", "page")
    assert row["ok"] is True and row["tool"] is None and row["answer"] == ask.REFUSAL and row["read"] is None
    assert len(gen_calls(ollama)) == 1                                                     # no second call, nothing run
    ollama.reply = by_prompt(pick={"tool": "none"})
    assert ask.ask(cfg, "what is the weather", "page")["answer"] == ask.REFUSAL


def test_ask_rejects_a_number_not_in_the_tool_result(ollama, cfg):
    seed_real()
    bad = {"answer": "Cash after is 1.1 lakh with 4 free slots."}
    ollama.reply = by_prompt(pick={"tool": "real_summary"}, answer=bad)
    row = ask.ask(cfg, "how much cash is free", "page")
    assert row["ok"] is False and row["answer"] == ask.REJECTED and row["error"] == "a figure is not in the tool result" and row["tool"] == "real_summary"
    calls = gen_calls(ollama)
    assert len(calls) == 3 and "was not valid: a figure is not in the tool result" in calls[2]["system"]
    assert row["status"] == "done"                                                           # the user got a reply: no breaker count
    from backend.engine import attention
    items = attention._ai(cfg)
    assert ("info", "🤖 ask not answered: how much cash is free (a figure is not in the tool result)") in items and "info" in attention.ORDER
    assert attention._pack(items)["level"] == "info"


def test_ask_retry_fixes_a_bad_first_answer(ollama, cfg):
    seed_real()
    answers = iter(["nonsense", json.dumps({"answer": "Cash is 103699.0 with 1 position."})])

    def reply(body):
        return json.dumps({"tool": "real_summary"}) if "You route a question" in body["system"] else next(answers)
    ollama.reply = reply
    row = ask.ask(cfg, "how much cash", "page")
    assert row["ok"] is True and row["answer"] == "Cash is 103699.0 with 1 position." and len(gen_calls(ollama)) == 3


def test_ask_when_the_guard_says_no_and_on_an_empty_question(ollama, cfg):
    cfg.ai.enabled = False
    row = ask.ask(cfg, "anything", "page")
    assert row["ok"] is False and row["status"] == "skipped" and row["answer"] == "AI not available: AI switched off." and row["error"] == "AI switched off"
    with pytest.raises(ValueError):
        ask.ask(cfg, "   ", "page")
    cfg.ai.enabled = True
    ollama.fail = "down"
    row = ask.ask(cfg, "anything", "page")
    assert row["ok"] is False and row["status"] == "skipped" and "service unreachable" in row["answer"]


def test_ask_model_error_fails_the_job_and_times_out_cleanly(ollama, cfg, monkeypatch):
    def reply(body):
        raise client.AIError("model crashed")
    ollama.reply = reply
    row = ask.ask(cfg, "why", "page")
    assert row["ok"] is False and row["status"] == "failed" and row["error"].startswith("AIError: model crashed") and row["answer"].startswith("The model could not answer")
    assert guard.breaker()["failures"] == 1
    monkeypatch.setattr(q, "wait", lambda job_id, timeout_s: {"status": "running"})
    ollama.reply = by_prompt()
    row = ask.ask(cfg, "slow one", "telegram", timeout_s=0.1)
    assert row["ok"] is False and row["status"] == "running" and "No answer within 0.1 s" in row["answer"]
    q._items.join()


def test_ask_allowed_at_night_on_the_day_model(ollama, cfg, monkeypatch):
    import backend.ai.queue as qq
    seed_real()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at("02:30")
    monkeypatch.setattr(qq, "datetime", Clock)
    row = ask.ask(cfg, "why did we skip S3", "page")
    assert row["ok"] is True and {b["model"] for b in gen_calls(ollama)} == {DAY}


# --- Telegram -------------------------------------------------------------------------------------------------------------------

class St:
    def __init__(self, cfg):
        self.config = cfg
        self.client = type("K", (), {"has_token": False})()
        self.token_source = None

    def token_valid(self):
        return False


def test_telegram_ask_flow(ollama, cfg):
    seed_real()
    plan("S3")
    when = at("10:12")
    fake = BotTG([update(1, "ask why did we skip S3 today", at=when), update(2, "ask", at=when), update(3, "/ask who", chat=999, at=when),
                  update(4, "/help", at=when), update(5, "ASK   what is in <the> plan", at=when)])
    b, n = BotCommands(St(cfg), clock=lambda: when), Notifier(cfg.alerts, fake)
    assert b.poll_once(n) == 4
    texts = [h for _, h in fake.sent]
    assert texts[0] == "💬 <b>ask</b> why did we skip S3 today\n<i>thinking…</i>"
    assert texts[1].startswith("💬 <b>[AI] ask</b> why did we skip S3 today\n<i>AI ·</i> S3 was skipped by the results guard")
    assert texts[1].endswith(f"<i>(why_skipped(symbol=S3, day={TODAY}) · 2 s · read-only)</i>")
    assert texts[2].startswith("Ask what?") and "ask &lt;question&gt;" in texts[3]
    assert texts[4].startswith("💬 <b>ask</b> what is in &lt;the&gt; plan") and all(no_brokers(t) for t in texts)
    rows = ask.list_asks(5)
    assert [r["who"] for r in rows] == ["telegram", "telegram"] and rows[0]["question"] == "what is in <the> plan"
    cfg.ai.ask = False
    assert b.ask("anything", n) and fake.sent[-1][1] == "🤖 Ask is off (System › AI)."
    assert len(ask.list_asks(5)) == 2                                                        # nothing asked while off


def test_ask_reply_text():
    row = {"question": "cash?", "answer": "Cash 1 & 2", "read": "real_summary()", "ms": 5400, "ok": True}
    assert msg.ai_ask_reply(row) == "💬 <b>[AI] ask</b> cash?\n<i>AI ·</i> Cash 1 &amp; 2 <i>(real_summary() · 5 s · read-only)</i>"
    assert msg.ai_ask_reply({"question": "x", "answer": None, "error": "boom", "read": None, "ms": None}) == "💬 <b>[AI] ask</b> x\nboom <i>(read-only)</i>"


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


def test_routes(api, ollama):
    from backend.api import routes
    tc = api
    seed_real()
    plan("S3")
    assert tc.post("/api/ai/ask", json={"question": "why"}).json()["detail"] == "AI switched off"      # the app's own config
    routes.S().config.ai = AIConfig(enabled=True)
    r = tc.post("/api/ai/ask", json={"question": "why did we skip S3"})
    assert r.status_code == 200
    d = r.json()
    assert {"answer", "tool", "ms", "id", "ok", "status", "read", "args"} <= set(d) and d["tool"] == "why_skipped" and d["answer"] == ANSWER["answer"]
    assert tc.post("/api/ai/ask", json={"question": "   "}).status_code == 400
    routes.S().config.ai.ask = False
    assert tc.post("/api/ai/ask", json={"question": "why"}).json()["detail"] == "ask is off"
    routes.S().config.ai.ask = True
    rows = tc.get("/api/ai/asks?limit=5").json()["rows"]
    assert len(rows) == 1 and rows[0]["id"] == d["id"] and rows[0]["who"] == "page"
    s = tc.get("/api/ai").json()
    assert s["asks"]["today"] == 1 and s["asks"]["last"]["id"] == d["id"] and set(s["brief"]) == {"last_at", "lines", "day", "id"}
    assert s["settings"]["ask"] is True
