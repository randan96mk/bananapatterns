"""Exchange filings: NSE announcements parsing and kinds, storage, news lines on stock alerts,
filing alerts for held / watched / plan stocks, the 15-minute job, and the API / card chips."""

from datetime import date, datetime, timedelta

import httpx
import pytest

from backend.alerts import guard
from backend.alerts import plan as pl
from backend.alerts.dispatch import Notifier, news_note
from backend.alerts.jobs import Scheduler, due_jobs
from backend.config import IST, AlertsConfig, VcpConfig
from backend.database.models import get_db, now_ist
from backend.ingest import filings as fl
from backend.ingest.filings import refresh_from_nse as real_refresh     # bound before conftest blocks the network
from tests.test_alerts import FakeTG
from tests.test_login import LoginState


def ann(seq, sym, cat, text, dt):
    return {"seq_id": str(seq), "symbol": sym, "desc": cat, "attchmntText": text, "an_dt": dt,
            "attchmntFile": f"https://nsearchives.nseindia.com/corporate/{sym}_{seq}.pdf"}


def stamp(hours_ago: float = 1) -> str:
    """An NSE-style timestamp `hours_ago` before now (IST)."""
    return (datetime.now(IST) - timedelta(hours=hours_ago)).strftime("%d-%b-%Y %H:%M:%S")


@pytest.mark.parametrize("cat,text,kind", [
    ("Outcome of Board Meeting", "X has submitted to the Exchange, the financial results for the period", "results"),
    ("Press Release", 'press release titled "Press Release for Financial Results Q2"', "results"),
    ("Copy of Newspaper Publication", "Newspaper publication of the financial results", "noise"),
    ("Bagging/Receiving of orders/contracts", "secured a work order of Rs. 495.86 Crores", "order"),
    ("Acquisition", "acquisition of 51% stake", "deal"),
    ("Credit Rating- Revision", "", "rating"),
    ("Resignation of Statutory Auditor", "", "management"),
    ("Action(s) taken or orders passed", "", "legal"),
    ("Corporate Insolvency Resolution Process", "", "legal"),
    ("News Verification", "The Exchange has sought clarification", "exchange"),
    ("Spurt in Volume", "", "exchange"),
    ("Investor Presentation", "", "update"),
    ("Analysts/Institutional Investor Meet/Con. Call Updates", "schedule of analyst meet", "noise"),
    ("Statement of deviation(s) or variation(s) under Reg. 32", "", "noise"),
    ("Trading Window", "", "noise"),
])
def test_kinds(cat, text, kind):
    assert fl.kind_of(cat, text) == kind


def test_parse_store_dedupe_and_prune():
    rows = fl.parse_nse([ann(1, "AAA", "Credit Rating", "upgrade", "25-Oct-2026 17:06:06"),
                         ann(2, "BBB", "Trading Window", "", "bad date"),
                         {"seq_id": "3", "symbol": "", "desc": "x", "an_dt": "25-Oct-2026 10:00:00"}])
    assert rows == [{"seq_id": "1", "symbol": "AAA", "dt": "2026-10-25T17:06:06", "category": "Credit Rating",
                     "text": "upgrade", "url": "https://nsearchives.nseindia.com/corporate/AAA_1.pdf",
                     "kind": "rating"}]
    assert len(fl.store(rows)) == 1 and fl.store(rows) == []            # seen before: not new again
    old = dict(rows[0], seq_id="9", dt="2000-01-01T10:00:00")
    fl.store([old])
    with get_db() as conn:
        assert [r[0] for r in conn.execute("SELECT seq_id FROM filings")] == ["1"]     # older than KEEP_DAYS


def test_refresh_window_first_run_then_since_newest():
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        if req.url.path == "/api/corporate-announcements":
            return httpx.Response(200, json=[ann(1, "AAA", "Acquisition", "deal", "24-Oct-2026 10:00:00")])
        return httpx.Response(403)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert [f["seq_id"] for f in real_refresh(date(2026, 10, 25), client=client)] == ["1"]
    assert seen[-1]["from_date"] == "22-10-2026" and seen[-1]["to_date"] == "25-10-2026"   # 3 days on the first run
    assert real_refresh(date(2026, 10, 25), client=client) == []
    assert seen[-1]["from_date"] == "23-10-2026"                        # from the day before the newest stored


def test_news_note_on_stock_alerts():
    cfg = AlertsConfig()
    assert news_note("AAA", "breakout", cfg) == ""                       # never fetched: says nothing
    fl.store(fl.parse_nse([ann(1, "AAA", "Bagging/Receiving of orders/contracts", "secured a work order of Rs. 495 Cr",
                               stamp(20)),
                           ann(2, "AAA", "Investor Presentation", "", stamp(10)),
                           ann(3, "AAA", "Trading Window", "", stamp(5)),
                           ann(4, "ZZZ", "Acquisition", "x", stamp(5))]))
    note = news_note("AAA", "breakout", cfg)
    assert note.startswith("\n📰 Order win ") and "Update" in note and "Filing" not in note
    assert "secured a work order of Rs. 495 Cr" in note
    assert "No exchange filings in 3 days" in news_note("BBB", "breakout", cfg)
    assert news_note("BBB", "near_pivot", cfg) == ""                     # "no news" only matters on a breakout
    assert news_note("AAA", "breakout", AlertsConfig(filings=False)) == ""
    fake = FakeTG()
    Notifier(AlertsConfig(chat_id="42"), fake).send("k1", "breakout", "AAA broke out", "AAA")
    assert "📰 Order win" in fake.sent[0][1]


def scheduler():
    st, fake = LoginState(), FakeTG()
    return Scheduler(st, notifier_factory=lambda cfg: Notifier(cfg, fake)), fake


def test_filing_alerts_for_held_watched_and_plan_stocks():
    guard.sync({"HELD": {"qty": 5, "avg": 100.0}}, VcpConfig())
    with get_db() as conn:
        conn.execute("INSERT INTO watchlist (symbol, added_at) VALUES ('WATCH', ?)", (now_ist(),))
    pl.save_plan("2026-10-24", [{"symbol": "PLAN", "pivot": 10.0}])
    new = fl.store(fl.parse_nse([
        ann(1, "HELD", "Bagging/Receiving of orders/contracts", "order worth Rs 500 Cr", stamp(1)),
        ann(2, "WATCH", "Action(s) taken or orders passed", "SEBI order", stamp(2)),
        ann(3, "PLAN", "Outcome of Board Meeting", "the financial results for the quarter", stamp(3)),
        ann(4, "OTHER", "Acquisition", "not yours", stamp(1)),
        ann(5, "HELD", "Investor Presentation", "slides", stamp(1)),             # an update: not pushed
        ann(6, "HELD", "Credit Rating", "old news", stamp(30)),                  # older than 24 h
    ]))
    sch, fake = scheduler()
    assert sch.alert_filings(new, datetime.now(IST)) == 3
    texts = [h for _, h in fake.sent]
    assert "📦 <b>HELD</b> · Order win <i>(you hold it)</i>" in texts[2]          # oldest first
    assert "⚖️ <b>WATCH</b> · Regulatory / legal <i>(on your watchlist)</i>" in texts[1]
    assert "📊 <b>PLAN</b> · Results <i>(in the plan)</i>" in texts[0]
    assert 'href="https://nsearchives.nseindia.com/corporate/HELD_1.pdf"' in texts[2]
    assert sch.alert_filings(new, datetime.now(IST)) == 0                     # once per filing


def test_filings_job_timing_and_failure():
    cfg = AlertsConfig(login_links=False, top_setups=False, morning_plan=False, gap_check=False, intraday=False,
                       job_enabled=False, system=False)
    sat = datetime(2026, 10, 24, 9, 0, tzinfo=IST)                        # a Saturday
    assert due_jobs(sat, cfg, set(), False) == ["filings"]                # weekends and holidays too
    assert due_jobs(sat, cfg, set(), False, last_filings=sat - timedelta(minutes=10)) == []
    assert due_jobs(sat, cfg, set(), False, last_filings=sat - timedelta(minutes=15)) == ["filings"]
    assert due_jobs(sat.replace(hour=6, minute=59), cfg, set(), False) == []
    assert due_jobs(sat.replace(hour=23), cfg, set(), False) == []
    sch, _ = scheduler()
    assert sch.run_filings(sat)["status"] == "failed" and sch.last_filings == sat     # no network: kept, retried later


def test_api_filings_and_chips(monkeypatch):
    from fastapi.testclient import TestClient
    from tests.test_cups import cup_series, scan_with
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    scan_with({"CUPX": cup_series(breakout=False, handle_depth=0.04)})
    fl.store(fl.parse_nse([ann(1, "CUPX", "Bagging/Receiving of orders/contracts", "big order", stamp(2)),
                           ann(2, "CUPX", "Trading Window", "", stamp(1))]))
    from backend import main
    with TestClient(main.app) as tc:
        row = tc.get("/api/screens/cup/results?stage=forming").json()["rows"][0]
        assert row["news"]["label"] == "Order win" and row["news"]["text"] == "big order"
        fs = tc.get("/api/filings?symbol=cupx").json()["filings"]
        assert [f["label"] for f in fs] == ["Order win"]                  # noise hidden
        assert tc.get("/api/filings").json()["last_fetch"]
        assert tc.post("/api/filings/refresh").status_code == 502
