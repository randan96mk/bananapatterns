"""✨ The Today card (Home) and the Overnight panel: the phases by the clock, the headline per phase, the one thing's
priority, the orders by phase, the watch and books columns, the brief lines (the only model text, tagged), the actions
(labels only: nothing fires by itself), the overnight moved rule, the sort, the level text, the unchanged count, the
routes and the stream topic. No model call anywhere here."""

import json
from datetime import datetime

import pytest

from backend.ai import today, view
from backend.config import IST, AIConfig, AlertsConfig, AppConfig
from backend.database.models import get_db, now_ist
from backend.engine import real
from tests.test_ai_brief import seed_real
from tests.test_ai_infra import no_brokers
from tests.test_ai_notes import hold
from tests.test_ai_view import bars, view_row, weekdays_back

TODAY = now_ist()[:10]


@pytest.fixture(autouse=True)
def trading(monkeypatch):
    from backend.ingest import holidays as hol
    monkeypatch.setattr(hol, "is_trading_day", lambda d, holidays=None: True)


@pytest.fixture
def cfg():
    c = AppConfig()
    c.ai = AIConfig(enabled=True)
    c.alerts = AlertsConfig(chat_id="42", intraday=False, filings=False)
    return c


def at(hhmm: str, day: str = TODAY) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    d = datetime.fromisoformat(day)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def eod_done(day: str = TODAY):
    with get_db() as conn:
        conn.execute("INSERT INTO job_runs (job, run_date, started_at, finished_at, status, message) VALUES ('eod', ?, ?, ?, 'ok', '')",
                     (day, now_ist(), now_ist()))


def suggestion(symbol="GLENMARK", confidence="high", expires="09:16", status="open", title=None):
    exp = at(expires).isoformat(timespec="seconds") if expires else None
    with get_db() as conn:
        return conn.execute("INSERT INTO ai_suggestions (created, day, kind, symbol, title, confidence, expires_at, status) VALUES (?,?,?,?,?,?,?,?)",
                            (now_ist(), TODAY, "skip", symbol, title or f"Skip {symbol} today", confidence, exp, status)).lastrowid


def order_log(symbol, action, status="sent", reason="", price=100.0, hhmm="09:16"):
    with get_db() as conn:
        conn.execute("INSERT INTO real_orders (key, day, created_at, symbol, action, qty, reason, status, price) VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"{TODAY}|{symbol}|{action}|{reason}", TODAY, at(hhmm).isoformat(timespec="seconds"), symbol, action, 10, reason, status, price))


# --- the phase ----------------------------------------------------------------------------------------------------------------

def test_phases_by_the_clock(cfg):
    p = lambda hhmm, **k: today.phase_for(at(hhmm), cfg, **k)
    assert p("06:00", trading=True, eod_done=False) == "morning" and p("09:15", trading=True, eod_done=False) == "morning"
    assert p("09:16", trading=True, eod_done=False) == "session" and p("15:39", trading=True, eod_done=False) == "session"
    assert p("15:40", trading=True, eod_done=False) == "close" and p("20:00", trading=True, eod_done=False) == "close"
    assert p("20:00", trading=True, eod_done=True) == "evening"
    assert p("10:00", trading=False) == "evening"                                  # a weekend / a holiday
    cfg.real.send_time = "09:20"
    assert p("09:18", trading=True, eod_done=False) == "morning"


def test_phase_reads_the_evening_job(cfg):
    assert today.phase_for(at("19:00"), cfg, trading=True) == "close"
    eod_done()
    assert today.phase_for(at("19:00"), cfg, trading=True) == "evening"


# --- headline, orders, actions -------------------------------------------------------------------------------------------------

def test_morning_card_orders_headline_and_actions(cfg):
    seed_real()
    view_row("S1", "2026-11-16", "buy", 82)
    c = today.today_card(cfg, at("08:30"))
    assert c["phase"] == "morning" and c["headline"] == "3 orders go out at 9:16 AM"
    orders = c["columns"]["orders"]
    assert [(o["symbol"], o["action"]) for o in orders] == [("S0", "EXIT"), ("S1", "BUY"), ("S2", "BUY"), ("S3", "BUY")]
    s1 = next(o for o in orders if o["symbol"] == "S1")
    assert (s1["qty"], s1["score"], s1["verdict"], s1["status"]) == (40, 82, "buy", "pending")
    assert next(o for o in orders if o["symbol"] == "S3")["skip"] is True
    assert c["actions"] == [{"label": "Skip S1", "kind": "skip", "arg": "S1"}, {"label": "Skip S2", "kind": "skip", "arg": "S2"},
                            {"label": "Open the dry run", "kind": "go", "arg": "portfolio/real"}]
    assert c["counts"]["orders"] == 4 and c["counts"]["pending"] == 3 and c["counts"]["skipped"] == 1


def test_actions_never_fire_by_themselves(cfg):
    st = seed_real()
    for hhmm in ("08:30", "09:20", "16:00", "21:00"):
        today.today_card(cfg, at(hhmm))
    assert real._state()["skips"] == st["skips"]                                    # no skip was added
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM real_orders").fetchone()[0] == 0     # nothing was sent
        assert conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == 0          # no model call queued


def test_session_card_reads_the_order_log(cfg):
    seed_real()
    order_log("S0", "EXIT")
    order_log("S1", "BUY")
    order_log("S2", "BUY", status="failed", price=None)
    order_log("S9", "EXIT", reason="stop hit", hhmm="11:05")
    c = today.today_card(cfg, at("11:30"))
    assert c["phase"] == "session" and c["headline"] == "2 of 3 orders filled · 1 stop hit"
    rows = c["columns"]["orders"]
    assert [(o["symbol"], o["action"], o["status"]) for o in rows][:4] == [("S0", "EXIT", "filled"), ("S1", "BUY", "filled"),
                                                                          ("S2", "BUY", "failed"), ("S9", "STOP", "filled")]
    assert rows[3]["at"] == "11:05 AM" and not any(a["kind"] == "skip" for a in c["actions"])    # after the send: no skip
    assert c["counts"]["stops"] == 1 and c["counts"]["filled"] == 3 and c["counts"]["failed"] == 1


def test_close_and_evening_cards_have_no_orders(cfg):
    seed_real()
    c = today.today_card(cfg, at("16:00"))
    assert c["phase"] == "close" and c["columns"]["orders"] == []
    assert c["headline"].startswith("💰 Real · ₹") and "<" not in c["headline"]          # the day summary's first line, plain text
    eod_done()
    c = today.today_card(cfg, at("21:00"))
    assert c["phase"] == "evening" and c["columns"]["orders"] == [] and c["headline"] == "tonight's queue: notes, why, suggestions, ratings"


def test_evening_headline_before_the_job_and_on_a_holiday(cfg, monkeypatch):
    from backend.ingest import holidays as hol
    monkeypatch.setattr(hol, "is_trading_day", lambda d, holidays=None: False)
    assert today.today_card(cfg, at("10:00"))["headline"] == "tonight: notes, why, suggestions, ratings after 6:30 PM"
    cfg.ai.why = False
    cfg.ai.view_enabled = False
    assert today.today_card(cfg, at("10:00"))["headline"] == "tonight: notes, suggestions after 6:30 PM"
    cfg.ai.enabled = False
    assert today.today_card(cfg, at("10:00"))["headline"] == "tonight: the evening job at 6:30 PM"


def test_morning_without_orders(cfg):
    assert today.today_card(cfg, at("08:00"))["headline"] == "no orders go out at 9:16 AM"


# --- the one thing ---------------------------------------------------------------------------------------------------------------

def _exit_watch(sym="ITC", close=250.0, fb=261.0, also=()):
    hold(sym, *also)
    view_row(sym, "2026-11-16", "exit-watch", 31, source="held", feats={"close": close}, fb=fb)


def test_the_one_thing_priority(cfg):
    seed_real()
    sid = suggestion()
    _exit_watch()
    assert today.today_card(cfg, at("08:30"))["the_one_thing"] == "decide on GLENMARK before 9:16 AM"
    with get_db() as conn:
        conn.execute("UPDATE ai_suggestions SET confidence = 'low' WHERE id = ?", (sid,))
    assert today.today_card(cfg, at("08:30"))["the_one_thing"] == "ITC closed under its support"
    with get_db() as conn:
        conn.execute("DELETE FROM ai_views")
    one = today.today_card(cfg, at("08:30"))["the_one_thing"]
    assert one.startswith("BUY S2 · 40 shares (₹") and one.endswith("goes out at 9:16 AM")     # the biggest pending (S3 is skipped)


def test_the_one_thing_hides_without_anything(cfg):
    assert today.today_card(cfg, at("08:30"))["the_one_thing"] is None
    suggestion(expires="08:00")                                                                     # past its expiry
    assert today.today_card(cfg, at("08:30"))["the_one_thing"] is None


def test_exit_watch_over_its_support_reads_the_score(cfg):
    _exit_watch(close=270.0)
    assert today.today_card(cfg, at("08:30"))["the_one_thing"] == "ITC is on exit-watch (score 31)"


# --- watch, books, brief --------------------------------------------------------------------------------------------------------

def test_watch_column(cfg):
    suggestion("GLENMARK", "low", "15:00")
    _exit_watch(also=("HELD",))
    with get_db() as conn:
        conn.execute("INSERT INTO corp_events VALUES ('HELD', ?, 'results', 'Results', '', ?)", (weekdays_back(TODAY, 1)[0], now_ist()))
        conn.execute("INSERT INTO ai_notes (created, day, kind, symbol, book, severity, title, text, data, prompt_version, source_key) "
                     "VALUES (?, ?, 'deal', 'ABC', 'held', 'info', 'Bulk deal: a fund bought 1.2%', 'line', '{}', 'notes-v1', 'd1')", (now_ist(), TODAY))
    w = today.today_card(cfg, at("08:30"))["columns"]["watch"]
    kinds = [x["kind"] for x in w]
    assert kinds == ["suggestion", "exit_watch", "results", "deal"]
    assert w[0]["until"] == "3:00 PM" and w[0]["text"] == "Skip GLENMARK today"
    assert w[2]["text"].startswith("HELD results ") and w[2]["text"].endswith("(today)")
    assert w[3]["text"] == "ABC · Bulk deal: a fund bought 1.2%"


def test_books_and_brief_lines(cfg):
    seed_real()
    with get_db() as conn:
        conn.execute("INSERT INTO ai_notes (created, day, kind, severity, title, text, data, model, prompt_version, source_key) "
                     "VALUES (?, ?, 'brief', 'info', 't', ?, ?, 'm', 'brief-v1', ?)",
                     (now_ist(), TODAY, "Real sends 3 orders\nS3 is skipped", json.dumps({"lines": ["Real sends 3 orders", "S3 is skipped"]}), f"brief:{TODAY}"))
    c = today.today_card(cfg, at("08:30"))
    assert c["brief_lines"] == [{"text": "Real sends 3 orders", "ai": True}, {"text": "S3 is skipped", "ai": True}]
    b = c["columns"]["books"]
    assert b[0]["book"] == "real" and b[0]["icon"] == "💰" and b[0]["label"] == "Real" and b[0]["value"] > 0 and "today" in b[0]
    assert c["counts"]["brief"] == 2


def test_card_keys_and_no_broker_names(cfg):
    seed_real()
    suggestion()
    c = today.today_card(cfg, at("08:30"))
    assert set(c) >= {"phase", "day", "at", "headline", "the_one_thing", "columns", "brief_lines", "actions", "counts"}
    assert set(c["columns"]) == {"orders", "watch", "books"}
    assert c["actions"][-1] == {"label": "Suggestions (1)", "kind": "open", "arg": "suggestions"}
    assert [s["label"] for s in c["steps"]] == ["9:00 AM brief", "9:16 AM orders", "3:40 PM summary"]
    assert no_brokers(json.dumps(c, ensure_ascii=False)) and c["at_label"] == "8:30 AM"


# --- overnight -------------------------------------------------------------------------------------------------------------------

def pair(sym, prev_score, now_score, prev_verdict="buy", now_verdict="buy", prev_feats=None, now_feats=None, fb=90.0, change="score moved",
         day="2026-11-17", prev_day="2026-11-16", source="plan"):
    p = view_row(sym, prev_day, prev_verdict, prev_score, source=source, feats=prev_feats or {"close": 100.0}, fb=fb)
    n = view_row(sym, day, now_verdict, now_score, source=source, feats=now_feats or {"close": 100.0}, fb=fb)
    with get_db() as conn:
        conn.execute("UPDATE ai_views SET previous_id = ?, change = ? WHERE id = ?", (p, change, n))
    return n


def test_overnight_moved_rule_sort_and_unchanged():
    pair("UP", 50, 58)                                                 # +8: moved
    pair("SAME", 60, 62)                                               # +2: unchanged
    pair("WORSE", 64, 58, "buy", "wait")                               # verdict worsened: first
    pair("BREAK", 70, 69, now_feats={"close": 85.0})                   # under the old fails_below: moved
    pair("BIG", 40, 61, "wait", "buy")                                 # +21, verdict better
    view_row("NEW", "2026-11-17", "buy", 70)                           # no previous view: not listed
    o = view.overnight("2026-11-17")
    assert [r["symbol"] for r in o["rows"]] == ["WORSE", "BIG", "UP", "SAME", "BREAK"]
    assert {r["symbol"]: r["moved"] for r in o["rows"]} == {"WORSE": True, "BIG": True, "UP": True, "SAME": False, "BREAK": True}
    assert o["unchanged"] == 1 and o["day"] == "2026-11-17"
    w = o["rows"][0]
    assert w["prev"] == {"score": 64, "verdict": "buy", "day": "2026-11-16"} and w["now"] == {"score": 58, "verdict": "wait"}
    assert w["delta"] == -6 and w["change"] == {"text": "score moved", "ai": True}


def test_overnight_level_text():
    d = weekdays_back("2026-11-17", 3)
    bars("HOLDS", [100, 98, 99], days=d)
    pair("HOLDS", 60, 62, prev_day=d[0], prev_feats={"close": 100.0}, now_feats={"close": 99.0}, fb=95.0)
    bars("BROKE", [100, 94, 99], days=d)
    pair("BROKE", 60, 62, prev_day=d[0], prev_feats={"close": 100.0}, now_feats={"close": 99.0}, fb=95.0)
    pair("PIV", 50, 66, prev_day=d[0], prev_feats={"close": 63.0, "pivot": 65}, now_feats={"close": 65.5, "pivot": 65}, fb=60.0)
    pair("NOLEVEL", 50, 51, prev_day=d[0], fb=None)
    rows = {r["symbol"]: r for r in view.overnight("2026-11-17")["rows"]}
    assert rows["HOLDS"]["level"] == "support 1 95 held" and rows["BROKE"]["level"] == "support 1 95 broken"
    assert rows["PIV"]["level"] == "pivot 65 reclaimed" and rows["NOLEVEL"]["level"] == "—"
    assert view._fmt_level(1261.0) == "1,261" and view._fmt_level(61.5) == "61.5" and view._fmt_level(2229.5) == "2,229.5"


def test_overnight_default_day_and_empty():
    assert view.overnight() == {"day": None, "rows": [], "unchanged": 0, "total": 0}
    pair("A", 50, 60, day="2026-11-13", prev_day="2026-11-12")
    view_row("B", "2026-11-14", "buy", 70)                             # the latest day with ok views has no pair
    o = view.overnight()
    assert o["day"] == "2026-11-14" and o["rows"] == []
    assert len(view.overnight("2026-11-13")["rows"]) == 1


# --- routes -----------------------------------------------------------------------------------------------------------------------

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
    kc = make_client(fake_kite, history_years=1)
    kc.config.access_token = ""
    monkeypatch.setattr(main, "KiteClient", lambda kc_, ic: kc)
    with TestClient(main.app) as tc:
        yield tc


def test_routes_today_card_overnight_and_stream(api):
    seed_real()
    pair("UP", 50, 58, day=TODAY, prev_day="2026-11-10")
    c = api.get("/api/today-card").json()
    assert c["phase"] in today.PHASES and set(c["columns"]) == {"orders", "watch", "books"}
    o = api.get("/api/ai/overnight").json()
    assert o["day"] == TODAY and o["rows"][0]["symbol"] == "UP" and o["unchanged"] == 0
    assert api.get("/api/ai/overnight?day=2026-11-10").json()["rows"] == []
    assert api.get("/api/ai/overnight?day=nonsense").status_code == 400
    body = api.get("/api/stream?topics=today_card&once=1").text
    assert "event: today_card" in body and '"headline"' in body
    from backend.api import routes
    assert routes._stream_topics()["today_card"][1]({}) == 60
