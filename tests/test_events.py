"""Results calendar: NSE event-calendar parsing, storage, session counts, and the warnings it drives
(stock alerts, chart panel, position guard, daily plan, cards / API)."""

from datetime import date, timedelta

import httpx

from backend.alerts import guard
from backend.alerts import plan as pl
from backend.alerts.dispatch import Notifier, decision_for
from backend.config import AlertsConfig, BaseConfig, VcpConfig
from backend.database.models import get_db, now_ist, upsert_candles
from backend.ingest import events
from backend.ingest.events import refresh_from_nse as real_refresh     # bound before conftest blocks the network
from backend.ingest.holidays import is_trading_day
from tests.test_alerts import FakeTG
from tests.test_guard import series

SAMPLE = [
    {"symbol": "AAVAS", "company": "Aavas", "purpose": "Financial Results/Other business matters",
     "bm_desc": "To consider and approve the unaudited financial results", "date": "26-Oct-2026"},
    {"symbol": "DIVCO", "company": "Div Co", "purpose": "Dividend", "bm_desc": "Interim dividend", "date": "27-Oct-2026"},
    {"symbol": "FUNDS", "company": "Funds", "purpose": "Fund Raising", "bm_desc": "QIP", "date": "28-Oct-2026"},
    {"symbol": "BAD", "purpose": "Financial Results", "date": "not a date"},
]


def after_sessions(n: int, start: date | None = None) -> str:
    """The date n trading sessions after `start` (default today)."""
    d = start or date.fromisoformat(now_ist()[:10])
    while n:
        d += timedelta(days=1)
        n -= is_trading_day(d)
    return d.isoformat()


def results_in(symbol: str, n: int) -> str:
    d = after_sessions(n)
    events.store([{"symbol": symbol, "date": d, "kind": "results", "purpose": "Financial Results", "descr": ""}])
    return d


def test_parse_and_kinds():
    rows = events.parse_nse(SAMPLE)
    assert [(r["symbol"], r["date"], r["kind"]) for r in rows] == [
        ("AAVAS", "2026-10-26", "results"), ("DIVCO", "2026-10-27", "dividend"), ("FUNDS", "2026-10-28", "fund_raising")]


def test_refresh_stores_and_upserts():
    seen = []

    def handler(req):
        seen.append(req.url)
        if req.url.path == "/api/event-calendar":
            return httpx.Response(200, json=SAMPLE)
        return httpx.Response(403)                                   # the home page may refuse; the API answers

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert real_refresh(date(2026, 10, 25), client=client) == 3
    assert real_refresh(date(2026, 10, 25), client=client) == 3      # same rows again: upserted, not duplicated
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM corp_events").fetchone()[0] == 3
    q = dict(seen[-1].params)
    assert q["from_date"] == "15-10-2026" and q["to_date"] == "09-12-2026" and q["index"] == "equities"
    assert events.last_fetch()


def test_sessions_and_next_results():
    assert events.sessions_until("2026-10-23", "2026-10-26") == 1      # Friday -> Monday: the next session
    assert events.sessions_until("2026-10-26", "2026-10-26") == 0
    events.store(events.parse_nse(SAMPLE))
    r = events.next_results("AAVAS", "2026-10-23")
    assert r == {"date": "2026-10-26", "sessions": 1}
    assert events.results_label(r) == "Results Mon 26 Oct (next session)"
    assert events.next_results("DIVCO", "2026-10-23") is None            # a dividend meeting is not results
    assert events.next_results("AAVAS", "2026-10-27") is None            # already past


def test_stock_alerts_carry_the_results_warning():
    results_in("SOON", 3)
    results_in("LATER", 12)
    fake = FakeTG()
    n = Notifier(AlertsConfig(chat_id="42"), fake)
    n.send("k1", "breakout", "SOON broke out", "SOON")
    n.send("k2", "breakout", "LATER broke out", "LATER")
    n.send("k3", "digest", "digest", "SOON")                            # not a stock alert kind
    assert "⚠️ <b>Results" in fake.sent[0][1] and "(in 3 sessions)" in fake.sent[0][1]
    assert "Results" not in fake.sent[1][1] and "Results" not in fake.sent[2][1]
    off = FakeTG()
    Notifier(AlertsConfig(chat_id="42", results_calendar=False), off).send("k4", "breakout", "x", "SOON")
    assert "Results" not in off.sent[0][1]


def test_decision_chart_panel_shows_results():
    from tests.test_cups import cup_series, CFG, BCFG
    from backend.engine.cups import detect_cups
    results_in("CUPX", 2)
    cs = cup_series(breakout=False)
    p = {"symbol": "CUPX", "cup": detect_cups(cs, CFG, BCFG)[0].shape, "rs": 90, "tt_passed": 8}
    png, ctx = decision_for(cs, p, "forming", AlertsConfig(), BaseConfig(), VcpConfig(), force_image=True)
    assert ctx["results"].startswith("Results ") and "(in 2 sessions)" in ctx["results"] and png[:4] == b"\x89PNG"


def test_guard_warns_once_before_results():
    today = now_ist()[:10]
    with get_db() as conn:
        upsert_candles(conn, 7, series([100.0] * 60))
    guard.sync({"X": {"qty": 10, "avg": 100.0}}, VcpConfig())
    results_in("X", 1)
    fake = FakeTG()
    n = Notifier(AlertsConfig(chat_id="42"), fake)
    guard.run(today, {"X": 7}, AlertsConfig(), n)
    guard.run(today, {"X": 7}, AlertsConfig(), n)
    warn = [h for _, h in fake.sent if "📅 <b>X</b>" in h]
    assert len(warn) == 1 and "next session" in warn[0] and "hold, trim or sell" in warn[0]
    assert "📅 results" in [h for _, h in fake.sent if "Your positions" in h][0]


def test_guard_quiet_when_results_are_further_out():
    today = now_ist()[:10]
    with get_db() as conn:
        upsert_candles(conn, 7, series([100.0] * 60))
    guard.sync({"X": {"qty": 10, "avg": 100.0}}, VcpConfig())
    results_in("X", 4)
    fake = FakeTG()
    guard.run(today, {"X": 7}, AlertsConfig(), Notifier(AlertsConfig(chat_id="42"), fake))
    assert not any("📅 <b>X</b>" in h for _, h in fake.sent)
    assert "📅 results" in [h for _, h in fake.sent if "Your positions" in h][0]   # still flagged in the summary


def test_plan_items_flag_results(monkeypatch):
    from backend.alerts import messages as msg
    today = now_ist()[:10]
    monkeypatch.setattr(pl, "candidates", lambda as_of, screens: [
        {"symbol": "SOON", "screen": "vcp", "pivot": 100.0, "dist_to_pivot_pct": 1.0, "close": 99.0},
        {"symbol": "CALM", "screen": "vcp", "pivot": 50.0, "dist_to_pivot_pct": 2.0, "close": 49.0}])
    results_in("SOON", 2)
    items = {i["symbol"]: i for i in pl.build_plan(today, AlertsConfig(tiering=False), VcpConfig(), {})}
    assert items["SOON"]["results"]["sessions"] == 2 and items["CALM"]["results"] is None
    text = msg.top_setups(list(items.values()), today)
    assert text.count("⚠️ results") == 1


def test_api_results_on_rows_positions_and_events(monkeypatch):
    from fastapi.testclient import TestClient
    from tests.test_cups import cup_series, scan_with
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    scan_with({"CUPX": cup_series(breakout=False, handle_depth=0.04)})
    results_in("CUPX", 3)
    guard.sync({"CUPX": {"qty": 1, "avg": 10.0}}, VcpConfig())
    from backend import main
    with TestClient(main.app) as tc:
        row = tc.get("/api/screens/cup/results?stage=forming").json()["rows"][0]
        assert row["results"]["sessions"] == 3 and row["results"]["warn"] is True
        assert row["results"]["label"].endswith("(in 3 sessions)")
        assert tc.get("/api/positions").json()["positions"][0]["results"]["sessions"] == 3
        assert tc.get("/api/events?symbol=cupx").json()["events"][0]["kind"] == "results"
        st = tc.get("/api/events").json()
        assert st["upcoming_results"] == 1 and st["last_fetch"]
        assert tc.post("/api/events/refresh").status_code == 502         # no network in tests: reported, not raised


def test_eod_refresh_failure_never_blocks():
    from backend.alerts.jobs import Scheduler
    assert Scheduler.refresh_events() is None                           # conftest makes the fetch fail
