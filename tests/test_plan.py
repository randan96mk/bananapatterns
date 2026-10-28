"""Daily plan: coiled detection, ranking, Top N at 19:00, morning plan, opening (gap) check."""

from datetime import datetime, timedelta

import pytest

from backend.alerts import messages as msg
from backend.alerts import plan as pl
from backend.alerts.dispatch import Notifier
from backend.alerts.jobs import Scheduler, due_jobs
from backend.config import IST, AlertsConfig
from tests.test_alerts import FakeState, FakeTG
from tests.test_intraday import Quotes, forming_db


def ist(*a):
    return datetime(*a, tzinfo=IST)


def candles(n=60, last_range=1.0, last_vol=300.0, inside=False):
    cs = [{"date": f"d{i}", "open": 100, "high": 102, "low": 98, "close": 100, "volume": 1000.0} for i in range(n)]
    lo = 100 - last_range / 2
    cs[-1] = {"date": "last", "open": 100, "high": lo + last_range, "low": lo, "close": 100, "volume": last_vol}
    if inside:
        cs[-3].update(high=101, low=99)                           # a narrower bar earlier in the 7 ...
        cs[-1].update(high=101.9, low=98.1)                       # ... so this one is inside, but not NR7
    return cs


# --- pure rules ------------------------------------------------------------------

def test_coiled():
    cfg = AlertsConfig()
    c = pl.coiled(candles(), 1.5, cfg)
    assert c == {"nr7": True, "inside": True, "volume_x": 0.3}
    assert pl.coiled(candles(inside=True), 1.5, cfg)["nr7"] is False
    assert pl.coiled(candles(last_vol=900), 1.5, cfg) is None           # volume not dried up
    assert pl.coiled(candles(), 4.0, cfg) is None                       # too far under the pivot
    assert pl.coiled(candles(n=20), 1.0, cfg) is None                   # not enough history
    wide = candles(last_range=8.0)
    wide[-2].update(high=101, low=99)
    assert pl.coiled(wide, 1.0, cfg) is None                            # neither NR7 nor inside


def test_rank_score():
    p = {"score": 70, "dist_to_pivot_pct": 4.0, "rs": 90}
    assert pl.rank_score(p, "B", None) == 62.0
    assert pl.rank_score(p, "A", None) == 72.0
    assert pl.rank_score(p, "B", {"nr7": True}) == 70.0
    assert pl.rank_score({"rs": 80, "dist_to_pivot_pct": 1.0}, None, None) == 46.0      # no VCP score: 0.6 x RS


@pytest.mark.parametrize("open_px,verdict", [(106, "chase"), (101, "above"), (100, None), (97, None), (94, "broken")])
def test_gap_verdict(open_px, verdict):
    assert pl.gap_verdict(open_px, {"pivot": 100, "zone_top": 105, "stop": 95}) == verdict


def test_due_plan_jobs():
    cfg = AlertsConfig(login_links=False)
    assert "top_setups" in due_jobs(ist(2026, 10, 26, 19, 0), cfg, set(), True)
    assert "top_setups" not in due_jobs(ist(2026, 10, 26, 18, 59), cfg, set(), True)
    assert "top_setups" not in due_jobs(ist(2026, 10, 26, 19, 5), cfg, {"top_setups"}, True)
    assert "morning_plan" in due_jobs(ist(2026, 10, 26, 8, 50), cfg, set(), True)
    assert "morning_plan" not in due_jobs(ist(2026, 10, 26, 12, 0), cfg, set(), True)
    assert "gap_check" in due_jobs(ist(2026, 10, 26, 9, 10), cfg, set(), True)
    assert "gap_check" not in due_jobs(ist(2026, 10, 26, 10, 0), cfg, set(), True)
    assert not {"top_setups", "morning_plan", "gap_check"} & set(due_jobs(ist(2026, 10, 25, 9, 10), cfg, set(), False))
    off = AlertsConfig(login_links=False, top_setups=False, morning_plan=False, gap_check=False)
    assert not {"top_setups", "morning_plan", "gap_check"} & set(due_jobs(ist(2026, 10, 26, 9, 10), off, set(), True))


# --- jobs --------------------------------------------------------------------------

def setup(monkeypatch, names=("P1", "P2", "P3", "P4", "P5", "P6")):
    tokens, piv = forming_db(list(names))
    fake, st, q = FakeTG(), FakeState(), Quotes()
    st.client = q
    st.config.alerts.plan_top_n = 5
    monkeypatch.setattr("backend.alerts.jobs.uni.resolve_universe",
                        lambda cfg: {"stocks": [{"symbol": s, "instrument_token": t} for s, t in tokens.items()]})
    monkeypatch.setattr(Scheduler, "_tokens", lambda self: tokens)
    return Scheduler(st, notifier_factory=lambda cfg: Notifier(cfg, fake)), fake, st, q, piv


def scan_day():
    from backend.database.models import get_db
    with get_db() as conn:
        return conn.execute("SELECT MAX(as_of) FROM scan_runs").fetchone()[0]


def test_top_setups_waits_for_the_evening_scan(monkeypatch):
    sch, fake, *_ = setup(monkeypatch)
    r = sch.run_top_setups(ist(2026, 10, 26, 19, 0))
    assert r["status"] == "waiting" and "top_setups" in sch.not_before and fake.sent == []


def test_top_setups_plan_morning_and_gap(monkeypatch):
    sch, fake, st, q, piv = setup(monkeypatch)
    st.config.alerts.alert_style = "every"                  # the separate messages (summary: tests/test_report.py)
    day = datetime.fromisoformat(scan_day()).replace(tzinfo=IST)
    r = sch.run_top_setups(day.replace(hour=19), force=True)
    assert r["items"] == 5 and r["sent"] == 6                          # header + 5 charts
    head = fake.sent[0][1]
    assert "Top 5 setups for the next session" in head and head.count(". ") >= 5 and all(f"{n}. " in head for n in range(1, 6))
    assert all(k == "photo" for k, _ in fake.sent[1:])
    plan_as_of, items = pl.latest_plan()
    assert plan_as_of == scan_day() and len(items) == 5 and items[0]["rank"] >= items[-1]["rank"]
    assert all("payload" not in i for i in items)

    nxt = day + timedelta(days=1)
    m = sch.run_morning_plan(nxt.replace(hour=8, minute=50))
    assert m["sent"] and "Today's plan" in fake.sent[-1][1] and f"from the {scan_day()} scan" in fake.sent[-1][1]

    syms = [i["symbol"] for i in items]
    q.px = {syms[0]: (items[0]["zone_top"] * 1.02,) * 2 + (0, 0), syms[1]: (items[1]["pivot"] * 1.01,) * 2 + (0, 0),
            syms[2]: (items[2]["stop"] * 0.98,) * 2 + (0, 0), syms[3]: (items[3]["pivot"] * 0.99,) * 2 + (0, 0)}
    for k, (ltp, vol, low, high) in list(q.px.items()):
        q.px[k] = (ltp, 1e6, ltp, ltp)                                  # the stub's ohlc.open is the low = ltp
    g = sch.run_gap_check(nxt.replace(hour=9, minute=10))
    assert g["notes"] == 3 and g["sent"]
    gap = fake.sent[-1][1]
    assert "Don't chase" in gap and "watch whether it holds" in gap and "setup broken" in gap
    assert syms[3] not in gap


def test_gap_check_without_login_or_plan(monkeypatch):
    sch, fake, st, *_ = setup(monkeypatch, names=("P1",))
    assert sch.run_gap_check(ist(2026, 10, 27, 9, 10))["status"] == "no_plan"
    day = datetime.fromisoformat(scan_day()).replace(tzinfo=IST)
    sch.run_top_setups(day.replace(hour=19), force=True)
    st._token = False
    assert sch.run_gap_check((day + timedelta(days=1)).replace(hour=9, minute=10))["status"] == "no_login"


def test_plan_api(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    pl.save_plan("2026-10-23", [{"symbol": "X", "pivot": 10.0}])
    from backend import main
    with TestClient(main.app) as tc:
        assert tc.get("/api/plan").json() == {"as_of": "2026-10-23", "items": [{"symbol": "X", "pivot": 10.0}]}


def test_plan_messages_escape():
    i = {"symbol": "A&B", "base_label": "B1 <5w>", "pivot": 100.0, "dist_to_pivot_pct": 1.2, "zone_top": 105.0,
         "stop": 95.0, "tier": "A", "coiled": {"nr7": True, "inside": False, "volume_x": 0.42}, "size": {"qty": 12}}
    t = msg.top_setups([i], "2026-10-23")
    assert "🅰️ <b>A&amp;B</b> B1 &lt;5w&gt;" in t and "🌀 coiled (NR7, volume 0.42×)" in t and "~12 sh" in t
    assert "Patience" in msg.morning_plan([], "2026-10-23", "2026-10-26")
