"""🤖 Local AI, PR 2 (filing and deal notes): the prompt (numbers found by code, trimmed text), the JSON contract and its
validator (enums, lengths, numbers not in the source stripped, broker names rejected), the severity by code, the deal
grouping and the discount math, the scope ordering and the nightly cap, the hooks in the evening job and the filings
job, stale jobs, the alert lines and the high-note message, attention, the overview's `ai_note`, the test set scorer
and the routes. Everything against the fake Ollama; no network."""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backend.ai import client, control, guard, notes, quality
from backend.ai import queue as q
from backend.alerts import guard as aguard
from backend.alerts import messages as msg
from backend.alerts import plan as pl
from backend.alerts.dispatch import Notifier
from backend.alerts.jobs import Scheduler
from backend.config import IST, AIConfig, AlertsConfig, AppConfig, VcpConfig
from backend.database.models import get_db, now_ist
from backend.ingest import filings as fl
from tests.test_ai_infra import NIGHT, BROKERS, FakeOllama, at, no_brokers
from tests.test_alerts import FakeTG
from tests.test_login import LoginState

ROOT = Path(__file__).resolve().parent.parent
GOOD = {"category": "results", "severity": "medium", "direction": "positive", "title": "Q2 profit up on higher volumes",
        "line": "Revenue ₹1,412.6 crore, up 21.4% YoY; PAT 128.4 crore. A strong quarter for the owner.",
        "acts": ["check the margin trend", "read the presentation"], "confidence": 0.82}
RESULTS_TEXT = ("Outcome of Board Meeting: the Board approved the unaudited results for the quarter ended September 30, 2026. "
                "Revenue from operations of Rs. 1,412.6 crore, up 21.4% YoY. EBITDA of Rs. 318.2 crore. Net profit of Rs. 128.4 crore.")
PLEDGE_TEXT = ("Disclosure under Regulation 31 of SEBI (SAST) Regulations, 2011: the promoter has created a pledge on 1,25,00,000 "
               "equity shares (4.43% of the share capital) in favour of a lender. Encumbered shares after the event 9.92%.")


class FakeNotes(FakeOllama):
    """The fake Ollama whose /api/generate answer may be a string, a list of strings (one per call) or a function of the
    request body (the prompt decides the answer, like a model would)."""

    def __call__(self, method, path, body=None, timeout=5, stream=False):
        if path == "/api/generate" and body.get("keep_alive") != 0 and body["model"] in self.models:
            self.calls.append((method, path, body))
            self.loaded = [body["model"]]
            r = self.reply
            text = r(body) if callable(r) else (r.pop(0) if isinstance(r, list) else r)
            return json.dumps({"model": body["model"], "response": text, "eval_count": 9, "prompt_eval_count": 31,
                               "total_duration": 850_000_000, "load_duration": 400_000_000, "eval_duration": 300_000_000})
        return super().__call__(method, path, body, timeout, stream)


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
    """A quiet box, no breaker, no stop, an empty queue, no test-set run; the evening job is done, so the night window
    is open whatever the clock says (job_time 00:00 in `cfg`)."""
    monkeypatch.setattr(guard, "_meminfo", lambda: {"ram_total_gb": 23.0, "ram_free_gb": 12.0})
    monkeypatch.setattr(guard, "_load1", lambda: 0.5)
    monkeypatch.setattr(guard, "_cpu_pct", lambda: 12.0)
    guard.record_tick(120)
    control._pull.update(running=False, model=None, pct=0.0, done=[], error=None, at=None)
    quality._run.update(running=False, done=0, n=0, at=None, error=None)
    q.clear("test setup")
    q._items.join()                                              # a job of the previous test never touches this test's DB
    with get_db() as conn:
        conn.execute("INSERT INTO job_runs (job, run_date, started_at, finished_at, status, message) VALUES ('eod', ?, ?, ?, 'ok', '')",
                     (now_ist()[:10], now_ist(), now_ist()))


def filing(seq, sym, cat, text, hours_ago=1.0, kind=None, url=None):
    """Store one filing row (the shape backend.ingest.filings.store takes) and return it."""
    dt = (datetime.now(IST) - timedelta(hours=hours_ago)).replace(tzinfo=None).isoformat(timespec="seconds")
    row = {"seq_id": str(seq), "symbol": sym, "dt": dt, "category": cat, "text": text, "url": url or f"https://nsearchives.nseindia.com/{sym}_{seq}.pdf",
           "kind": kind or fl.kind_of(cat, text)}
    fl.store([row])
    return row


def source_for(row, book="held"):
    return {"kind": "filing", "key": row["seq_id"], "filing": row, "scope": {"book": book, "why": notes.BOOK_WHY[book], "rank": None}}


def hold(*syms):
    aguard.sync({s: {"qty": 5, "avg": 100.0} for s in syms}, VcpConfig())


def watch(*syms):
    with get_db() as conn:
        for s in syms:
            conn.execute("INSERT OR IGNORE INTO watchlist (symbol, added_at) VALUES (?, ?)", (s, now_ist()))


def plan(*syms):
    pl.save_plan(now_ist()[:10], [{"symbol": s, "pivot": 10.0, "zone_top": 10.5, "stop": 9.0} for s in syms])


def deal(sym, day, client_, side, qty, price, source="bulk", pct=None, after=None, promoter=0):
    return {"date": day, "symbol": sym, "client": client_, "side": side, "qty": qty, "price": price, "source": source, "pct": pct,
            "after_pct": after, "promoter": promoter, "url": ""}


# --- config, table ------------------------------------------------------------------------------------------------------------

def test_config_fields_and_table_guard():
    assert (AIConfig().notes_per_night, AIConfig().note_days) == (80, 3)
    with get_db() as conn:
        assert "source_key" in [r[1] for r in conn.execute("PRAGMA table_info(ai_notes)")]
        ins = ("INSERT OR IGNORE INTO ai_notes (created, day, kind, symbol, severity, title, text, data, prompt_version, source_key) "
               "VALUES (?, '2026-11-14', 'filing', 'X', 'low', 't', 'l', '{}', 'notes-v1', '77')")
        assert conn.execute(ins, (now_ist(),)).rowcount == 1 and conn.execute(ins, (now_ist(),)).rowcount == 0     # once per version
        conn.execute(ins.replace("'notes-v1'", "'notes-v2'"), (now_ist(),))
        assert conn.execute("SELECT COUNT(*) FROM ai_notes").fetchone()[0] == 2


# --- the prompt ---------------------------------------------------------------------------------------------------------------

def test_numbers_found_by_code():
    nums = notes.extract_numbers(RESULTS_TEXT)
    assert [(n["label"], n["value"], n["unit"]) for n in nums] == [("revenue", "1,412.6", "crore"), ("PAT", "128.4", "crore"),
                                                                   ("EBITDA", "318.2", "crore"), ("YoY", "21.4", "%")]
    assert notes.numbers_text(nums) == "revenue 1,412.6 crore; PAT 128.4 crore; EBITDA 318.2 crore; YoY 21.4 %"
    assert notes.extract_numbers("no figures here") == [] and notes.numbers_text([]) == "none"


def test_filing_prompt_carries_the_numbers_and_trims_the_text():
    long = RESULTS_TEXT + " blah" * 600
    row = filing(1, "LAURUSLABS", "Outcome of Board Meeting", long, kind="results")
    system, user, numbers = notes.build_prompt(source_for(row, "held"))
    assert system == notes.SYSTEM_PROMPT and "JSON only" in system and "never compute" in system.lower() and no_brokers(system)
    assert user.startswith("Stock: LAURUSLABS (you hold it)\nFiling category (exchange): Outcome of Board Meeting\nFiled: ")
    assert "Numbers found by code: revenue 1,412.6 crore; PAT 128.4 crore; EBITDA 318.2 crore; YoY 21.4 %" in user
    body = user.split("Filing text:\n", 1)[1]
    assert len(body) == notes.TEXT_MAX and body.endswith("…") and len(numbers) == 4
    assert re.search(r"Filed: \d{1,2}:\d{2} [AP]M, \d{1,2} \w{3}", user)                       # 12-hour IST
    row = filing(2, "KEI", "Bagging/Receiving of orders/contracts", "order worth Rs. 548.9 crore", kind="order")
    assert notes.build_prompt(source_for(row, "watch"))[2] == [] and "(on your watchlist)" in notes.build_prompt(source_for(row, "watch"))[1]


def test_deal_prompt_and_discount_numbers():
    g = notes.group_deals([deal("AAA", "2026-11-13", "BIG FUND LTD", "BUY", 200000, 97.0, "block"),
                           deal("AAA", "2026-11-13", "PROMO HOLDINGS", "SELL", 0, 0.0, "sast", pct=2.5, after=41.2, promoter=1)],
                          close_fn=lambda s, d: 100.0)[0]
    user, numbers = notes.deal_prompt(g, {"book": "held", "why": "you hold it"})
    assert "- BIG FUND LTD buy 200,000 shares at 97.00 (block deal), 3.0% under the day's close 100.00" in user
    assert "- promoter PROMO HOLDINGS sell 2.50% of the company (holding after 41.20%)" in user
    assert [n["label"] for n in numbers] == ["qty", "price", "vs close", "stake"] and "Quote only the numbers above" in user


# --- the validator ------------------------------------------------------------------------------------------------------------

def test_validate_good_answer():
    n = notes.validate(json.dumps(GOOD), RESULTS_TEXT, notes.extract_numbers(RESULTS_TEXT))
    assert n == {"category": "results", "severity_model": "medium", "direction": "positive", "title": GOOD["title"], "line": GOOD["line"],
                 "acts": GOOD["acts"], "confidence": 0.82, "numbers_checked": True}
    n = notes.validate(json.dumps({**GOOD, "category": " Results ", "acts": "not a list", "confidence": "x"}), RESULTS_TEXT, [])
    assert n["category"] == "results" and n["acts"] == [] and n["confidence"] == 0.5


def test_validate_rejects_bad_enums_and_non_json():
    for bad in ({**GOOD, "category": "gossip"}, {**GOOD, "severity": "urgent"}, {**GOOD, "direction": "sideways"}, {**GOOD, "title": ""}):
        with pytest.raises(notes.NoteInvalid):
            notes.validate(json.dumps(bad), RESULTS_TEXT, [])
    with pytest.raises(notes.NoteInvalid, match="not JSON"):
        notes.validate("Sure! Here is the JSON: {", RESULTS_TEXT, [])
    with pytest.raises(notes.NoteInvalid, match="not a JSON object"):
        notes.validate("[1, 2]", RESULTS_TEXT, [])


def test_validate_clips_lengths_and_acts():
    d = {**GOOD, "title": "T" * 90, "line": "L" * 300, "acts": ["a" * 100, "b", "", "c", "d"], "confidence": 7}
    n = notes.validate(json.dumps(d), RESULTS_TEXT, [])
    assert len(n["title"]) == notes.TITLE_MAX and n["title"].endswith("…") and len(n["line"]) == notes.LINE_MAX
    assert n["acts"] == ["a" * 59 + "…", "b", "c"] and n["confidence"] == 1.0


def test_validate_strips_numbers_not_in_the_source():
    nums = notes.extract_numbers(RESULTS_TEXT)
    made_up = {**GOOD, "line": "Revenue Rs. 1,412.6 crore, up 21.4% YoY; margin 22.5% and PAT up 311% to 999 crore."}
    n = notes.validate(json.dumps(made_up), RESULTS_TEXT, nums)
    assert n["numbers_checked"] is False and "22.5" not in n["line"] and "311" not in n["line"] and "999" not in n["line"]
    assert n["line"].startswith("Revenue ₹1,412.6 crore, up 21.4% YoY") and "1,412.6" in n["line"]
    ok = {**GOOD, "line": "Revenue 1412.60 crore (Rs 1,412.6 crore in the filing), PAT 128.4 crore, Q2 FY27, 3 plants, 2026."}
    n = notes.validate(json.dumps(ok), RESULTS_TEXT, nums)
    assert n["numbers_checked"] is True                                              # the same numbers written differently
    n = notes.validate(json.dumps({**GOOD, "title": "Profit up 48%"}), RESULTS_TEXT, nums)
    assert n["numbers_checked"] is False and n["title"] == "Profit up"


def test_validate_rejects_broker_names():
    for b in BROKERS[:2]:
        with pytest.raises(notes.NoteInvalid, match="names a broker"):
            notes.validate(json.dumps({**GOOD, "line": f"Buy it on {b} today"}), RESULTS_TEXT, [])
    with pytest.raises(notes.NoteInvalid):
        notes.validate(json.dumps({**GOOD, "acts": [f"open {BROKERS[2]} Securities"]}), RESULTS_TEXT, [])
    assert notes.validate(json.dumps({**GOOD, "line": "KOTAKBANK reported higher deposits."}), RESULTS_TEXT, [])["line"]   # a stock, not a broker


# --- severity by code ---------------------------------------------------------------------------------------------------------

def test_filing_severity_is_decided_by_code():
    sev = lambda cat, direction, book="held", text="", category="": notes.filing_severity(
        {"category": cat, "direction": direction, "severity_model": "info"}, {"text": text, "category": category}, book)
    assert sev("pledge", "negative", text=PLEDGE_TEXT) == "high" and sev("pledge", "neutral", text="created a pledge") == "high"
    assert sev("pledge", "positive", text="released the pledge") == "low"
    assert sev("results", "negative", "held") == "high" and sev("results", "negative", "watch") == "medium" and sev("results", "positive") == "medium"
    assert sev("legal", "negative") == "high" and sev("legal", "neutral", text="the order is in favour of the company") == "medium"
    assert sev("rating", "negative") == "high" and sev("rating", "neutral", text="has downgraded") == "high" and sev("rating", "positive", text="upgraded") == "low"
    assert sev("management", "negative", category="Resignation of Statutory Auditor") == "high" and sev("management", "neutral", text="new CEO") == "medium"
    assert sev("order", "positive") == "medium" and sev("fund_raise", "neutral") == "medium"
    assert sev("deal", "positive") == "low" and sev("dividend", "positive") == "low"
    assert sev("exchange", "neutral") == "info" and sev("other", "neutral") == "info"


def test_deal_grouping_discount_math_and_severity():
    rows = [deal("AAA", "2026-11-13", "BIG FUND LTD", "BUY", 200000, 97.0, "block", pct=1.2),
            deal("AAA", "2026-11-13", "SOME TRADER", "SELL", 1000, 104.0, "bulk"),
            deal("AAA", "2026-11-12", "PROMO HOLDINGS", "SELL", 0, 0.0, "sast", pct=2.5, after=41.2, promoter=1),
            deal("BBB", "2026-11-13", "AN INSURER", "BUY", 500, 50.0, "bulk")]
    closes = {"AAA": 100.0, "BBB": None}
    groups = {g["key"]: g for g in notes.group_deals(rows, close_fn=lambda s, d: closes[s])}
    assert set(groups) == {"AAA|2026-11-13", "AAA|2026-11-12", "BBB|2026-11-13"}
    g = groups["AAA|2026-11-13"]
    assert g["close"] == 100.0 and [r["discount_pct"] for r in g["rows"]] == [-3.0, 4.0] and g["max_pct"] == 1.2 and g["promoter_sell"] is False
    assert groups["AAA|2026-11-12"]["promoter_sell"] is True and groups["AAA|2026-11-12"]["rows"][0]["discount_pct"] is None
    assert groups["BBB|2026-11-13"]["close"] is None and groups["BBB|2026-11-13"]["rows"][0]["discount_pct"] is None
    assert notes.deal_severity(groups["AAA|2026-11-12"], "watch") == "high"                    # a promoter selling
    assert notes.deal_severity(g, "held") == "high" and notes.deal_severity(g, "plan") == "high"   # 1.2% at 3% under the close
    assert notes.deal_severity(g, "watch") == "medium" and notes.deal_severity(groups["BBB|2026-11-13"], "held") == "low"


# --- the scope, the ordering, the cap ---------------------------------------------------------------------------------------------

def test_scope_from_every_book_and_the_ordering():
    hold("HELD")
    watch("WATCH", "BOTH")
    plan("PLAN", "BOTH")
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO futures_state (id, payload) VALUES (1, ?)",
                     (json.dumps({"accounts": {"long_paper": {"positions": {"FUTX": {"symbol": "FUTX"}}}, "short_paper": {"positions": {}}}}),))
    sc = notes.scope()
    assert sc["HELD"] == {"book": "held", "why": "you hold it", "rank": None} and sc["FUTX"]["book"] == "held"
    assert sc["PLAN"] == {"book": "plan", "why": "in the plan", "rank": 1} and sc["BOTH"]["book"] == "plan" and sc["BOTH"]["rank"] == 2
    assert sc["WATCH"] == {"book": "watch", "why": "on your watchlist", "rank": None}
    assert Scheduler._scope() == {"HELD": "you hold it", "FUTX": "you hold it", "PLAN": "in the plan", "BOTH": "in the plan", "WATCH": "on your watchlist"}
    for seq, sym, hours in ((1, "WATCH", 1), (2, "HELD", 5), (3, "PLAN", 2), (4, "HELD", 1)):
        filing(seq, sym, "Acquisition", "deal", hours)
    srcs = notes.filing_sources(sc, (datetime.now(IST) - timedelta(days=3)).replace(tzinfo=None).isoformat())
    assert [s["key"] for s in notes._order(srcs)] == ["4", "2", "3", "1"]                        # held (newest first), plan, watch


def test_wanted_filings_include_pledge_updates_but_not_noise():
    sc = {"AAA": {"book": "held", "why": "you hold it", "rank": None}}
    filing(1, "AAA", "Trading Window", "window closed", 1)                                       # noise
    filing(2, "AAA", "Disclosure under SEBI (SAST) Regulations, 2011 - Reg. 31", PLEDGE_TEXT, 1)   # noise by kind, a pledge by text
    filing(3, "AAA", "Investor Presentation", "slides", 1)                                       # update
    filing(4, "AAA", "Credit Rating", "downgrade", 1)
    filing(5, "ZZZ", "Credit Rating", "not yours", 1)
    assert sorted(s["key"] for s in notes.filing_sources(sc, "2026-01-01")) == ["2", "3", "4"]


def test_enqueue_tonight_respects_the_cap_then_fills_the_rest(ollama, cfg):
    hold("HELD")
    watch("WATCH")
    cfg.ai.notes_per_night = 2
    for i, sym in enumerate(("WATCH", "HELD", "HELD"), start=1):
        filing(i, sym, "Outcome of Board Meeting", RESULTS_TEXT, i, kind="results")
    now = datetime.now(IST)
    out = notes.enqueue_tonight(cfg, now)
    assert (out["queued"], out["dedup"], out["capped"], out["scope"], out["sources"]) == (2, 0, 1, 2, 3)
    assert out["keys"] == ["note:filing:2", "note:filing:3"] and out["day"] == notes.night_day(now, cfg)      # held first, newest first
    for k in out["keys"]:
        q.wait(q._pending("night", k) or 0, 10)
    rows = notes.list_notes()["rows"]
    assert [r["symbol"] for r in rows] == ["HELD", "HELD"] and all(r["book"] == "held" and r["severity"] == "medium" for r in rows)
    assert notes.enqueue_tonight(cfg, now)["queued"] == 0                                         # the cap counts tonight's notes
    cfg.ai.notes_per_night = 80
    out = notes.enqueue_tonight(cfg, now)
    assert out["queued"] == 1 and out["keys"] == ["note:filing:1"]
    q.wait(q._pending("night", "note:filing:1") or 0, 10)
    assert notes.list_notes()["total"] == 3 and notes.enqueue_tonight(cfg, now)["queued"] == 0    # every source noted: nothing left
    assert all(j["model"] == NIGHT and j["kind"] == "night" for j in q.recent(5)) and ollama.calls[-1][2]["model"] == NIGHT


def test_enqueue_new_filings_only_the_new_in_scope_ones(ollama, cfg):
    hold("HELD")
    old = filing(1, "HELD", "Acquisition", "old deal", 2)
    new = [filing(2, "HELD", "Acquisition", "new deal", 1), filing(3, "OTHER", "Acquisition", "not yours", 1)]
    out = notes.enqueue_new_filings(cfg, new, datetime.now(IST))
    assert out["queued"] == 1 and out["keys"] == ["note:filing:2"] and q._pending("night", "note:filing:1") is None
    q.wait(q.recent(1)[0]["id"], 10)
    assert notes.enqueue_new_filings(cfg, [], datetime.now(IST))["queued"] == 0 and old["seq_id"] == "1"


def test_stale_jobs_are_skipped_and_failed_ones_not_requeued(ollama, cfg):
    hold("HELD")
    row = filing(1, "HELD", "Acquisition", "deal", 1)
    out = notes.enqueue_sources(cfg, [source_for(row)], datetime.now(IST) - timedelta(hours=25))
    job = q.wait(q.recent(1)[0]["id"])
    assert out["queued"] == 1 and job["status"] == "skipped" and job["error"] == "stale" and notes.list_notes()["total"] == 0
    ollama.reply = ["nope", "still nope"]
    notes.enqueue_sources(cfg, [source_for(row)], datetime.now(IST))
    job = q.wait(q.recent(1)[0]["id"])
    assert job["status"] == "failed" and "NoteInvalid" in job["error"] and guard.breaker()["failures"] == 1
    assert notes.enqueue_sources(cfg, [source_for(row)], datetime.now(IST))["queued"] == 0       # failed tonight: left alone


# --- one note ------------------------------------------------------------------------------------------------------------------

def test_run_one_writes_the_row_with_severity_by_code(ollama, cfg):
    hold("GLENMARK")
    row = filing(9, "GLENMARK", "Updates", PLEDGE_TEXT, 1)
    ollama.reply = json.dumps({"category": "pledge", "severity": "low", "direction": "negative", "title": "Promoter pledge raised to 9.92%",
                               "line": "The promoter pledged 4.43% more; encumbered shares are now 9.92%.", "acts": ["watch the lender"], "confidence": 0.7})
    out = notes.run_one(source_for(row, "held"), cfg, at("21:30"))
    n = notes.get_note(out["id"])
    assert out["note"] == "GLENMARK · high · Promoter pledge raised to 9.92%" and out["tokens"] == 9
    assert n["severity"] == "high" and n["severity_model"] == "low" and n["category"] == "pledge" and n["direction"] == "negative"
    assert n["kind"] == "filing" and n["book"] == "held" and n["source_key"] == "9" and n["prompt_version"] == "notes-v2" and n["model"] == NIGHT
    assert n["day"] == "2026-11-17" and n["ms"] == 850 and n["numbers_checked"] is True and n["numbers"] == [] and n["acts"] == ["watch the lender"]
    assert n["source"] == {"seq_id": "9", "url": row["url"], "dt": row["dt"], "category": "Updates", "kind": "update"} and n["url"] == row["url"]
    assert n["scope"] == {"book": "held", "why": "you hold it", "rank": None} and n["alerted"] is False
    assert notes.note_for("filing", "9")["id"] == out["id"] and notes.note_for("filing", "nope") is None
    assert notes.run_one(source_for(row, "held"), cfg, at("21:30"))["id"] is None                 # noted already: the guard holds
    assert notes.night_day(at("02:00"), cfg) == "2026-11-16" and notes.night_day(at("06:00"), cfg) == "2026-11-17"


def test_run_one_retries_once_then_fails(ollama, cfg):
    row = filing(1, "AAA", "Acquisition", "deal text", 1)
    ollama.reply = ["not json at all", json.dumps({**GOOD, "category": "deal"})]
    out = notes.run_one(source_for(row, "watch"), cfg)
    assert out["tokens"] == 18 and notes.get_note(out["id"])["ms"] == 1700 and notes.get_note(out["id"])["severity"] == "low"
    assert "was not valid:" in ollama.calls[-1][2]["system"] and ollama.calls[-1][2]["system"].startswith(notes.SYSTEM_PROMPT) and ollama.calls[-2][2]["system"] == notes.SYSTEM_PROMPT
    ollama.reply = ["{}", json.dumps({**GOOD, "severity": "urgent"})]
    with pytest.raises(notes.NoteInvalid, match="severity"):
        notes.run_one(source_for(filing(2, "AAA", "Acquisition", "x", 1), "watch"), cfg)


def test_deal_note_through_the_queue(ollama, cfg):
    hold("AAA")
    with get_db() as conn:
        for r in (deal("AAA", "2026-11-13", "PROMO HOLDINGS", "SELL", 0, 0.0, "sast", pct=2.5, after=41.2, promoter=1),
                  deal("AAA", "2026-11-13", "BIG FUND LTD", "BUY", 200000, 97.0, "block")):
            conn.execute("INSERT INTO deals (date, symbol, client, side, qty, price, source, pct, after_pct, promoter, url, fetched_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (*[r[k] for k in ("date", "symbol", "client", "side", "qty", "price", "source", "pct",
                                                                               "after_pct", "promoter", "url")], now_ist()))
    ollama.reply = json.dumps({"category": "deal", "severity": "medium", "direction": "negative", "title": "Promoter sold 2.50%",
                               "line": "PROMO HOLDINGS sold 2.50% while BIG FUND LTD bought 200,000 shares at 97.00.", "acts": [], "confidence": 0.6})
    now = datetime.now(IST)
    cfg.ai.note_days = 30
    sent = FakeTG()
    out = notes.enqueue_tonight(cfg, now, Notifier(cfg.alerts, sent))
    assert out["queued"] == 1 and out["keys"] == ["note:deal:AAA|2026-11-13"]
    q.wait(q._pending("night", out["keys"][0]) or 0, 10)
    assert q.recent(1)[0]["status"] == "done", q.recent(1)[0]
    n = notes.list_notes(kind="deal")["rows"][0]
    assert n["severity"] == "high" and n["book"] == "held" and n["source"]["n"] == 2 and n["source"]["date"] == "2026-11-13" and n["numbers_checked"]
    assert sorted(x["label"] for x in n["numbers"]) == ["price", "qty", "stake"]
    assert len(sent.sent) == 1 and sent.sent[0][1].startswith("📰 <b>[AI] AAA · deal</b>\n🤖 <i>AI ·</i> <i>high · PROMO HOLDINGS sold 2.50%")
    assert notes.get_note(n["id"])["alerted"] is True


# --- the hooks in the jobs ------------------------------------------------------------------------------------------------------------

def test_run_eod_and_the_filings_job_enqueue_notes(monkeypatch, ollama, cfg):
    st = LoginState()
    st.config = cfg
    sch = Scheduler(st, notifier_factory=lambda c: Notifier(c, FakeTG()))
    calls = []
    monkeypatch.setattr(sch, "_run_eod", lambda now, force: {"status": "ok"})
    monkeypatch.setattr(notes, "enqueue_tonight", lambda c, now, notifier=None: calls.append(("tonight", now)) or {"queued": 3})
    monkeypatch.setattr(notes, "enqueue_new_filings", lambda c, new, now, notifier=None: calls.append(("new", [f["seq_id"] for f in new])) or {"queued": 1})
    assert sch.run_eod(at("18:45"))["ai_notes"] == {"queued": 3} and calls == [("tonight", at("18:45"))]
    cfg.ai.enabled = False
    assert sch.run_eod(at("18:46"))["ai_notes"] == {"status": "off"} and len(calls) == 1
    monkeypatch.setattr(sch, "_run_eod", lambda now, force: {"status": "waiting_login"})
    cfg.ai.enabled = True
    assert "ai_notes" not in sch.run_eod(at("18:47")) and len(calls) == 1                         # nothing queued before the eod record
    new = [{"seq_id": "5", "symbol": "HELD", "dt": now_ist(), "category": "Acquisition", "text": "x", "url": "", "kind": "deal"}]
    monkeypatch.setattr(fl, "refresh_from_nse", lambda: new)
    from backend.ingest import holders
    monkeypatch.setattr(holders, "refresh_from_nse", lambda: [])
    assert sch.run_filings(at("20:00"))["status"] == "ok" and calls[-1] == ("new", ["5"])
    monkeypatch.setattr(fl, "refresh_from_nse", lambda: [])
    sch.run_filings(at("20:15"))
    assert len(calls) == 2                                                                        # nothing new: no call
    monkeypatch.setattr(notes, "enqueue_tonight", lambda c, now, notifier=None: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(sch, "_run_eod", lambda now, force: {"status": "ok"})
    assert sch.run_eod(at("18:50"))["ai_notes"]["status"] == "failed"                           # never breaks the evening job


# --- the alerts ----------------------------------------------------------------------------------------------------------------------

def test_alert_lines_when_a_note_exists():
    f = {"seq_id": "1", "symbol": "HELD", "dt": "2026-11-14T18:32:00", "category": "Outcome of Board Meeting", "text": "results", "url": "", "kind": "results"}
    note = {"severity": "high", "text": "Loss of 4.2 crore; margins fell <b>sharply</b>."}
    plain = msg.filing_alert(f, "you hold it")
    assert plain.endswith("Outcome of Board Meeting · 14 Nov, 6:32 PM</i>") and msg.filing_alert(f, "you hold it", None) == plain
    assert msg.filing_alert(f, "you hold it", note) == plain + "\n🤖 <i>AI ·</i> <i>high · Loss of 4.2 crore; margins fell &lt;b&gt;sharply&lt;/b&gt;.</i>"
    item = {"label": "Promoter sold 2.5%", "text": "X sold 2.50%, disclosed 13 Nov"}
    assert msg.deal_alert("HELD", "🚩", item, "you hold it", note).endswith("\n🤖 <i>AI ·</i> <i>high · Loss of 4.2 crore; margins fell &lt;b&gt;sharply&lt;/b&gt;.</i>")
    assert "🤖" not in msg.deal_alert("HELD", "🚩", item, "you hold it")


def test_filing_alert_carries_the_note_and_the_note_is_not_pushed_again(ollama, cfg):
    hold("HELD")
    row = filing(1, "HELD", "Outcome of Board Meeting", RESULTS_TEXT, 1, kind="results")
    ollama.reply = json.dumps({**GOOD, "direction": "negative", "title": "Weak quarter", "line": "Revenue fell; PAT 128.4 crore."})
    out = notes.run_one(source_for(row, "held"), cfg, notifier=Notifier(cfg.alerts, FakeTG()))
    assert notes.get_note(out["id"])["severity"] == "high" and notes.get_note(out["id"])["alerted"] is True
    with get_db() as conn:
        conn.execute("DELETE FROM sent_alerts")                                                   # pretend the alert never went: the filing alert is first
        d = json.loads(conn.execute("SELECT data FROM ai_notes WHERE id = ?", (out["id"],)).fetchone()[0])
        d["alerted"] = False
        conn.execute("UPDATE ai_notes SET data = ? WHERE id = ?", (json.dumps(d), out["id"]))
    st = LoginState()
    st.config = cfg
    fake = FakeTG()
    sch = Scheduler(st, notifier_factory=lambda c: Notifier(c, fake))
    assert sch.alert_filings([row], datetime.now(IST)) == 1
    assert fake.sent[0][1].endswith("\n🤖 <i>AI ·</i> <i>high · Revenue fell; PAT 128.4 crore.</i>") and notes.get_note(out["id"])["alerted"] is True
    assert notes.alert_high(notes.get_note(out["id"]), cfg, Notifier(cfg.alerts, fake)) is False and len(fake.sent) == 1


def test_high_note_message_where_and_skip_hint(ollama, cfg):
    from tests.test_home import _pos, _real_state
    _real_state({"REALX": _pos("REALX", 100.0, last=104.2)})
    hold("REALX", "HELDY")
    plan("P1", "GLENMARK")
    watch("WATCHZ")
    cfg.real.enabled = True
    assert notes.where_text("REALX", cfg) == ("held in Real (+4.2%)", None)
    assert notes.where_text("HELDY", cfg) == ("held", None)
    assert notes.where_text("GLENMARK", cfg) == ("in tomorrow's plan at #2", "reply skip GLENMARK before 9:16 AM to leave it out")
    cfg.real.enabled = False
    assert notes.where_text("GLENMARK", cfg) == ("in tomorrow's plan at #2", None)
    assert notes.where_text("WATCHZ", cfg) == ("on your watchlist", None) and notes.where_text("NOBODY", cfg)[0] == "on your watchlist"
    note = {"id": 7, "day": "2026-11-14", "symbol": "GLENMARK", "kind": "filing", "category": "pledge", "severity": "high",
            "text": "Promoter pledge raised to 9.92% of the share capital.", "source_at": "2026-11-14T18:10:00",
            "url": "https://nsearchives.nseindia.com/x.pdf", "alerted": False, "scope": {"book": "plan", "rank": 3}}
    cfg.real.enabled = True
    text = msg.note_alert(note, *notes.where_text("GLENMARK", cfg, note["scope"]))
    assert text == ("📰 <b>[AI] GLENMARK · pledge</b>\n🤖 <i>AI ·</i> <i>high · Promoter pledge raised to 9.92% of the share capital.</i>\n"
                    "<i>in tomorrow's plan at #3 · reply skip GLENMARK before 9:16 AM to leave it out · 14 Nov, 6:10 PM · "
                    '<a href="https://nsearchives.nseindia.com/x.pdf">filing</a></i>')
    assert no_brokers(text) and not re.search(r"\b\d{2}:\d{2}\b(?! [AP]M)", text.replace("18:10", ""))
    with get_db() as conn:
        conn.execute("INSERT INTO ai_notes (id, created, day, kind, symbol, book, severity, title, text, data, prompt_version, source_key) "
                     "VALUES (7, ?, '2026-11-14', 'filing', 'GLENMARK', 'plan', 'high', 't', ?, ?, 'notes-v1', '9')",
                     (now_ist(), note["text"], json.dumps({"category": "pledge", "scope": note["scope"], "source": {"url": note["url"], "dt": note["source_at"]}})))
    fake = FakeTG()
    assert notes.alert_high(notes.get_note(7), cfg, Notifier(cfg.alerts, fake)) is True and fake.sent[0][1] == text
    with get_db() as conn:
        assert conn.execute("SELECT key, kind FROM sent_alerts").fetchone()[:] == ("2026-11-14|ai-note-7", "filing")
    assert notes.alert_high(notes.get_note(7), cfg, Notifier(cfg.alerts, fake)) is False and len(fake.sent) == 1   # once
    medium = dict(notes.get_note(7), severity="medium", alerted=False)
    assert notes.alert_high(medium, cfg, Notifier(cfg.alerts, fake)) is False                   # medium / low are not pushed


# --- attention, overview ----------------------------------------------------------------------------------------------------------------

def _note_row(symbol, severity, book="held", title="t", day=None, created=None, kind="filing", key=None):
    with get_db() as conn:
        cur = conn.execute("INSERT INTO ai_notes (created, day, kind, symbol, book, severity, title, text, data, prompt_version, source_key) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (created or now_ist(), day or now_ist()[:10], kind, symbol, book, severity, title,
                                                             "line", "{}", "notes-v1", key or f"{symbol}-{severity}-{title}"))
    return cur.lastrowid


def test_attention_shows_last_nights_high_notes(cfg):
    from backend.engine import attention
    now = at("21:00")
    assert attention._ai(cfg, now) == []
    _note_row("GLENMARK", "high", "plan", "Promoter pledge raised", day="2026-11-17")
    _note_row("HELDX", "high", "held", "Loss this quarter", day="2026-11-17")
    _note_row("HELDX", "medium", "held", "Order win", day="2026-11-17")
    _note_row("OLD", "high", "held", "yesterday", day="2026-11-16")
    assert attention._ai(cfg, now) == [("sell", "🤖 HELDX · Loss this quarter"), ("warn", "🤖 GLENMARK · Promoter pledge raised")]
    assert attention._ai(cfg, at("05:00", (2026, 11, 18)))[0][1] == "🤖 HELDX · Loss this quarter"       # the same night until 6:00 AM
    assert attention._ai(cfg, at("07:00", (2026, 11, 18))) == []
    sig = attention.signals(cfg, True)["ai"]
    assert sig["level"] in ("sell", "warn", None) and isinstance(sig["items"], list)


def test_overview_rows_carry_the_latest_note(cfg):
    from backend.engine import overview
    from tests.test_home import _pos, _real_state
    _real_state({"NOTED": _pos("NOTED", 100.0), "QUIET": _pos("QUIET", 100.0), "STALE": _pos("STALE", 100.0)})
    _note_row("NOTED", "medium", title="Older note", key="a")
    nid = _note_row("NOTED", "high", title="Weak quarter", key="b")
    _note_row("STALE", "high", title="too old", created=(datetime.now(IST) - timedelta(days=9)).isoformat(timespec="seconds"), key="c")
    rows = {p["symbol"]: p for p in overview.positions(cfg, paper_s={"accounts": {}}, live={"rows": {}})}
    assert rows["NOTED"]["ai_note"] == {"title": "Weak quarter", "severity": "high", "at": notes.get_note(nid)["created"]}
    assert rows["QUIET"]["ai_note"] is None and rows["STALE"]["ai_note"] is None
    intraday = {"book": "intraday", "symbol": "NOTED", "side": "long", "qty": 1, "entry": 1, "now": 1, "pnl": 0, "pct": 0, "stop": None, "stop_dist": None, "opened": "x"}
    overview._add_ai_notes([intraday], now_ist()[:10])
    assert intraday["ai_note"] is None
    assert notes.latest_for_symbol("NOTED")["title"] == "Weak quarter" and notes.latest_for_symbol("STALE", 7) is None
    assert notes.latest_for_symbol("STALE", 30)["title"] == "too old"


# --- reading the notes, the stats, tonight's rows -------------------------------------------------------------------------------------------

def test_list_notes_filters_and_stats(cfg):
    _note_row("AAA", "high", "held", "a1", key="1")
    _note_row("AAA", "low", "held", "a2", key="2", kind="deal")
    _note_row("BBB", "medium", "watch", "b1", key="3")
    _note_row("CCC", "high", "plan", "old", key="4", day="2026-11-01", created="2026-11-01T22:00:00")
    all_ = notes.list_notes()
    assert all_["total"] == 4 and [r["title"] for r in all_["rows"]] == ["old", "b1", "a2", "a1"]        # newest first
    assert [r["title"] for r in notes.list_notes(symbol="aaa")["rows"]] == ["a2", "a1"]
    assert notes.list_notes(kind="deal")["total"] == 1 and notes.list_notes(severity="high")["total"] == 2
    assert notes.list_notes(symbol="AAA", severity="high", limit=1)["rows"][0]["title"] == "a1"
    assert [r["title"] for r in notes.list_notes(limit=2, offset=2)["rows"]] == ["a2", "a1"]
    assert set(all_["rows"][0]) >= {"id", "created", "day", "kind", "symbol", "book", "severity", "title", "text", "model", "prompt_version", "ms",
                                    "source_key", "category", "direction", "acts", "confidence", "severity_model", "numbers", "numbers_checked",
                                    "source", "url", "source_at", "scope", "alerted"}
    now = datetime.now(IST).replace(hour=22, minute=0)          # an evening clock: tonight's day is today whatever the wall clock
    s = notes.stats(now, cfg)
    assert s["today"] == 3 and s["high"] == 1
    assert s["last_night"]["n"] == 3 and s["last_night"]["by_kind"] == {"deal": 1, "filing": 2} and s["last_night"]["high"] == 1
    assert s["last_night"]["day"] == now_ist()[:10] and s["last_night"]["ms_total"] == 0 and s["last_night"]["at"]
    assert notes.stats(at("22:00", (2026, 11, 5)), cfg)["last_night"]["day"] == "2026-11-01"
    assert notes.stats(at("22:00", (2026, 10, 5)), cfg) == {"today": 0, "high": 0, "last_night": None}


def test_tonight_rows_and_running(ollama, cfg):
    hold("HELD")
    for i in range(3):
        filing(i + 1, "HELD", "Acquisition", f"deal {i}", i + 1)
    cfg.ai.enabled = False                                                                        # the guard skips: the jobs stay as rows
    assert notes.running() is False
    rows = notes.tonight(cfg, datetime.now(IST))
    assert rows == [{"kind": "filing", "label": "Filing notes", "n": 3, "queued": 0, "est_s": 36},
                    {"kind": "deal", "label": "Deal notes", "n": 0, "queued": 0, "est_s": 0}]
    cfg.ai.notes_per_night = 2
    assert notes.tonight(cfg)[0]["n"] == 2                                                        # the cap shows
    with get_db() as conn:
        conn.execute("INSERT INTO ai_jobs (created, kind, key, status) VALUES (?, 'night', 'note:filing:1', 'queued')", (now_ist(),))
        conn.execute("INSERT INTO ai_jobs (created, kind, key, status, ms) VALUES (?, 'night', 'note:filing:9', 'done', 20000)", (now_ist(),))
    cfg.ai.notes_per_night = 80
    r = notes.tonight(cfg)[0]
    assert r["queued"] == 1 and r["n"] == 3 and r["est_s"] == 60 and notes.running() is True     # 20 s each from the done job


# --- the test set -------------------------------------------------------------------------------------------------------------------------

def test_testset_file_is_well_formed():
    items = quality.load()
    assert len(items) == 30 and len({i["id"] for i in items}) == 30
    from collections import Counter
    assert Counter(i["kind"] for i in items) == {"results": 8, "update": 5, "order": 4, "legal": 3, "rating": 3, "management": 3, "deal": 2, "noise": 2}
    for i in items:
        e = i["expected"]
        assert e["category"] in notes.CATEGORIES and e["severity"] in notes.SEVERITIES and e["direction"] in notes.DIRECTIONS
        assert i["scope"] in notes.BOOKS and len(i["text"]) > 80 and no_brokers(i["text"]) and i["symbol"].isupper()
    assert sum(1 for i in items if i["expected"]["category"] == "results" and i["expected"]["direction"] == "negative") == 3
    assert sum(1 for i in items if i["expected"]["category"] == "pledge") == 5


def test_testset_scorer_last_and_best(ollama, cfg):
    items = quality.load()[:3] + [quality.load()[8]]                                              # 3 results + the GLENMARK pledge
    answers = {"LAURUSLABS": {"category": "results", "severity": "medium", "direction": "positive"},
               "AVANTIFEED": {"category": "results", "severity": "low", "direction": "positive"},        # severity wrong
               "RAJRATAN": {"category": "results", "severity": "high", "direction": "negative"},
               "GLENMARK": {"category": "other", "severity": "high", "direction": "negative"}}            # category wrong

    def reply(body):
        sym = re.search(r"Stock: (\w+)", body["prompt"]).group(1)
        return json.dumps({**answers[sym], "title": f"{sym} note", "line": "A plain line without figures.", "acts": [], "confidence": 0.9})

    ollama.reply = reply
    res = quality.run_testset(cfg, items, timeout_s=20)
    assert (res["n"], res["category_ok"], res["severity_ok"], res["direction_ok"], res["failed"]) == (4, 3, 3, 4, 0)
    assert res["model"] == NIGHT and res["prompt_version"] == "notes-v2" and res["ms_avg"] == 850 and res["at"]
    assert res["rows"][1] == {"id": 2, "symbol": "AVANTIFEED", "expected": items[1]["expected"], "got": answers["AVANTIFEED"],
                              "ok": {"category": True, "severity": False, "direction": True}, "ms": 850, "status": "done", "error": None}
    assert all(j["kind"] == "test" and j["model"] == NIGHT for j in q.recent(4)) and all(c[2]["model"] == NIGHT for c in ollama.calls if c[1] == "/api/generate")
    st = quality.status()
    assert st["last"] == res and st["best"] == res and st["running"] is False and st["progress"]["done"] == 4
    assert notes.list_notes()["total"] == 0                                                       # a test writes no note
    ollama.reply = ["garbage"] * 8                                                                 # a worse run: every item fails
    res2 = quality.run_testset(cfg, items, timeout_s=20)
    assert res2["failed"] == 4 and res2["category_ok"] == 0 and quality.status()["last"] == res2 and quality.status()["best"] == res
    quality._run["running"] = True
    with pytest.raises(quality.TestsetRunning):
        quality.run_testset(cfg, items)


# --- the routes -----------------------------------------------------------------------------------------------------------------------------

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


def test_routes(api, ollama, monkeypatch):
    tc = api
    monkeypatch.setattr(notes, "night_day", lambda now, config: now_ist()[:10])   # the API reads the wall clock: pin the night to today
    hold("HELD")
    filing(1, "HELD", "Acquisition", "deal one", 1)
    _note_row("HELD", "high", "held", "Weak quarter", key="n1")
    _note_row("HELD", "low", "held", "Order win", key="n2", kind="deal")
    d = tc.get("/api/ai").json()
    assert d["notes"]["today"] == 2 and d["notes"]["high"] == 1 and d["notes"]["last_night"]["n"] == 2
    assert set(d["testset"]) == {"last", "best", "running", "progress"} and d["testset"]["last"] is None
    assert d["queue"]["tonight"][0] == {"kind": "filing", "label": "Filing notes", "n": 1, "queued": 0, "est_s": 12} and "night_note" in d["queue"]
    r = tc.get("/api/ai/notes").json()
    assert r["total"] == 2 and [x["title"] for x in r["rows"]] == ["Order win", "Weak quarter"]
    assert tc.get("/api/ai/notes?severity=high").json()["total"] == 1 and tc.get("/api/ai/notes?kind=deal&symbol=held").json()["rows"][0]["kind"] == "deal"
    assert tc.get("/api/ai/notes?limit=1&offset=1").json()["rows"][0]["title"] == "Weak quarter" and tc.get("/api/ai/notes?symbol=ZZ").json() == {"rows": [], "total": 0}
    one = tc.get(f"/api/ai/notes/{r['rows'][1]['id']}").json()
    assert one["title"] == "Weak quarter" and one["acts"] == [] and tc.get("/api/ai/notes/9999").status_code == 404
    with get_db() as conn:
        conn.execute("INSERT INTO ai_jobs (created, kind, key, status) VALUES (?, 'night', 'note:filing:7', 'running')", (now_ist(),))
    assert tc.post("/api/ai/notes/run").status_code == 409
    with get_db() as conn:
        conn.execute("UPDATE ai_jobs SET status = 'done'")
    out = tc.post("/api/ai/notes/run").json()
    assert out["queued"] == 1 and out["keys"] == ["note:filing:1"]                                # AI off on the app's config: queued, the guard skips
    with get_db() as conn:
        conn.execute("INSERT INTO ai_jobs (created, kind, key, status) VALUES (?, 'night', 'note:filing:8', 'queued')", (now_ist(),))
    assert tc.post("/api/ai/notes/run").status_code == 409                                        # ... and while one is queued: 409
    with get_db() as conn:
        conn.execute("UPDATE ai_jobs SET status = 'skipped' WHERE key = 'note:filing:8'")
    q.wait(q._pending("night", "note:filing:1") or 0, 10)
    monkeypatch.setattr(quality, "run_testset", lambda c, claimed=False: None)
    s = tc.post("/api/ai/testset/run").json()
    assert s["running"] is True or s["progress"]["at"]
    quality._run["running"] = True
    assert tc.post("/api/ai/testset/run").status_code == 409 and tc.get("/api/ai").json()["testset"]["running"] is True
    quality._run["running"] = False
    text = tc.get("/api/stream?topics=ai&once=1").text
    assert "event: ai\n" in text and '"tonight"' in text


def test_no_broker_names_and_12_hour_times_in_the_new_modules():
    for name in ("notes.py", "quality.py"):
        assert no_brokers((ROOT / "backend" / "ai" / name).read_text()), name
    assert no_brokers((ROOT / "config" / "ai_testset.json").read_text())
    assert {b.lower() for b in BROKERS[:2]} <= set(notes.BLOCKED) and all(w == w.lower() for w in notes.BLOCKED)
    assert notes.DEFAULT_EST_S == 12 and notes.STALE_HOURS == 24 and notes.PROMPT_V == "notes-v2"
    assert not re.search(r"\b\d{2}:\d{2}\b", notes.SYSTEM_PROMPT)


def test_synonyms_map_to_listed_values_and_retry_names_the_fault(monkeypatch):
    """v2: a synonym the model reaches for ("acquisition", "bullish", "critical") is mapped before the enum check; an
    answer that is still invalid is retried with the fault and the allowed values in the system prompt; the generation
    options cap the answer."""
    raw = json.dumps({"category": "Acquisition", "severity": "critical", "direction": "bullish", "title": "Buys a plant",
                      "line": "the company buys a plant", "acts": [], "confidence": 0.8})
    n = notes.validate(raw, "the company buys a plant", [])
    assert (n["category"], n["severity_model"], n["direction"]) == ("deal", "high", "positive")
    n2 = notes.validate(raw.replace("Acquisition", "stake-sale").replace("bullish", "mixed"), "the company buys a plant", [])
    assert (n2["category"], n2["direction"]) == ("deal", "neutral")
    with pytest.raises(notes.NoteInvalid):
        notes.validate(raw.replace("Acquisition", "weather"), "x", [])
    calls = []

    def fake_generate(model, prompt, system=None, **kw):
        calls.append((system, kw.get("options")))
        bad = json.dumps({"category": "weather", "severity": "high", "direction": "negative", "title": "t", "line": "l"})
        good = json.dumps({"category": "legal", "severity": "high", "direction": "negative", "title": "t", "line": "l"})
        return {"text": bad if len(calls) == 1 else good, "tokens": 5, "prompt_tokens": 50, "ms": 100, "load_ms": 0, "eval_ms": 90, "model": model}
    monkeypatch.setattr(notes.client, "generate", fake_generate)
    note, r = notes.ask(AppConfig(), notes.SYSTEM_PROMPT, "user", "l", [])
    assert note["category"] == "legal" and len(calls) == 2
    assert "weather" in calls[1][0] and "legal" in calls[1][0] and calls[1][0].startswith(notes.SYSTEM_PROMPT)
    assert calls[0][1] == notes.GEN_OPTIONS and calls[0][1]["num_predict"] <= 400 and calls[0][1]["temperature"] <= 0.2
    assert notes.PROMPT_V == "notes-v2" and "neutral = only when" in notes.SYSTEM_PROMPT


def test_figures_keep_their_kind_and_currency_is_normalised():
    """A figure quoted as a percent must be a percent in the facts, a rupee amount a rupee amount; $ / Rs become ₹."""
    src = "Total: net ₹8,156 lost; 9 trades; 1 won; books +52.78% vs NIFTY +0.63%; PNB net +₹9,230"
    allowed = notes.typed_tokens(src)
    t, ok = notes._strip_numbers("net -₹8,156, 1 win in 9 trades, +52.78% vs NIFTY +0.63%", allowed)
    assert ok and "52.78%" in t and "₹8,156" in t
    t, ok = notes._strip_numbers("PNB rose 8,156% on the week", allowed)                  # a rupee figure dressed as a percent
    assert not ok and "8,156" not in t
    t, ok = notes._strip_numbers("net ₹52.78 on PNB", allowed)                             # a percent dressed as rupees
    assert not ok and "52.78" not in t
    t, ok = notes._strip_numbers("cash $9,230 and Rs 8,156 left", allowed)
    assert ok and t == "cash ₹9,230 and ₹8,156 left"
    plain = notes._digit_tokens(src)                                                        # no typed memory: the old check only
    assert notes._strip_numbers("8,156% is fine here", plain) == ("8,156% is fine here", True)
