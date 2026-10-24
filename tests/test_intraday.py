"""Intraday breakout rules: NSE volume curve, volume verdict, failed breakout, 15:15 close check,
and the 18:30 cross-reference."""

import json
from datetime import datetime

import pytest

from backend.alerts import intraday as itd
from backend.alerts import messages as msg
from backend.alerts.dispatch import Notifier, dispatch_scan
from backend.alerts.jobs import Scheduler, due_jobs
from backend.config import IST, AlertsConfig, BaseConfig, ScreenConfig
from backend.database.models import get_db, upsert_candles
from backend.engine.scan import run_scan
from tests.test_alerts import BASE, UP, FakeState, FakeTG, scanned_db
from tests.test_bases import build


def ist(*a):
    return datetime(*a, tzinfo=IST)


DAY = (2026, 10, 26)            # a Monday


# --- pure rules ------------------------------------------------------------------

@pytest.mark.parametrize("hm,share", [((9, 15), 0.0), ((9, 30), 0.10), ((10, 0), 0.25), ((12, 30), 0.50),
                                      ((15, 15), 0.90), ((15, 30), 1.0), ((15, 45), 1.0)])
def test_volume_curve(hm, share):
    assert itd.day_share(ist(*DAY, *hm)) == pytest.approx(share)


def test_morning_projection_is_not_inflated():
    # 10:00, half the average traded: the straight line said ~4.2x; the NSE curve says 2x
    assert itd.volume_pace(500, 1000, ist(*DAY, 10, 0)) == pytest.approx(2.0)
    assert itd.volume_pace(500, 1000, ist(*DAY, 9, 17)) is None                    # too early
    assert itd.volume_pace(None, 1000, ist(*DAY, 11, 0)) is None


def test_volume_verdict():
    assert itd.volume_verdict(1.6, 1.5) == "strong"
    assert itd.volume_verdict(1.2, 1.5) == "average"
    assert itd.volume_verdict(0.7, 1.5) == "weak"
    assert itd.volume_verdict(None, 1.5) == "unknown"


def test_close_verdict():
    cfg = AlertsConfig()
    assert itd.close_verdict(99, 100, 105, 2.0, 0.9, cfg) is None
    assert itd.close_verdict(106, 100, 105, 2.0, 0.9, cfg) == "extended"
    assert itd.close_verdict(102, 100, 105, 1.6, 0.6, cfg) == "holding"
    assert itd.close_verdict(102, 100, 105, 1.2, 0.9, cfg) == "weak"               # volume
    assert itd.close_verdict(102, 100, 105, 2.0, 0.3, cfg) == "weak"               # closing low in the range
    assert itd.close_verdict(102, 100, 105, None, 0.9, cfg) == "weak"
    assert itd.day_range(102, 100, 103) == pytest.approx(2 / 3) and itd.day_range(5, 5, 5) is None


def test_due_close_check():
    cfg = AlertsConfig(login_links=False)
    assert "close_check" in due_jobs(ist(*DAY, 15, 15), cfg, set(), True)
    assert "close_check" not in due_jobs(ist(*DAY, 15, 14), cfg, set(), True)
    assert "close_check" not in due_jobs(ist(*DAY, 15, 31), cfg, set(), True)
    assert "close_check" not in due_jobs(ist(*DAY, 15, 16), cfg, {"close_check"}, True)
    assert "close_check" not in due_jobs(ist(*DAY, 15, 16), cfg, set(), False)                  # holiday
    assert "close_check" not in due_jobs(ist(*DAY, 15, 16), AlertsConfig(close_check=False), set(), True)
    assert "close_check" not in due_jobs(ist(*DAY, 15, 16), AlertsConfig(intraday=False), set(), True)


def test_close_check_time_validated():
    from backend.config import AppConfig
    c = AppConfig()
    with pytest.raises(ValueError):
        c.update_section("alerts", {"close_check_time": "3pm"})
    assert c.alerts.close_check_time == "15:15"


# --- jobs ------------------------------------------------------------------------

class Quotes:
    """Kite quote stub: per-symbol (ltp, volume, low, high)."""

    def __init__(self):
        self.px = {}

    def quote(self, keys):
        out = {}
        for k in keys:
            if k.split(":")[1] not in self.px:
                continue                                  # like Kite: unknown instruments are left out
            ltp, vol, low, high = self.px[k.split(":")[1]]
            out[k] = {"last_price": ltp, "volume": vol, "ohlc": {"open": low, "high": high, "low": low}}
        return out


def forming_db(names):
    """Forming stocks (same shape as NEAR in test_alerts) -> ({symbol: token}, {symbol: pivot})."""
    tokens = {}
    with get_db() as conn:
        for i, name in enumerate(names):
            tokens[name] = 100 + i
            upsert_candles(conn, tokens[name], build([UP, BASE, ("bar", 1, 1.035)]))
    run_scan(BaseConfig(reset_ma=0), ScreenConfig(trend_ma=0),
             [{"symbol": k, "instrument_token": v} for k, v in tokens.items()])
    return tokens, pivots()


def pivots():
    with get_db() as conn:
        return {r["symbol"]: json.loads(r["payload"])["pivot"] for r in conn.execute(
            "SELECT symbol, payload FROM scan_results WHERE screen = 'bases' AND stage = 'forming'")}


def sched(monkeypatch, tokens, fake):
    st, q = FakeState(), Quotes()
    st.client = q
    monkeypatch.setattr("backend.alerts.jobs.uni.resolve_universe",
                        lambda cfg: {"stocks": [{"symbol": s, "instrument_token": t} for s, t in tokens.items()]})
    return Scheduler(st, notifier_factory=lambda cfg: Notifier(cfg, fake)), q, st


def test_heads_up_volume_verdicts(monkeypatch):
    tokens, piv = forming_db(["S1", "S2", "S3"])
    fake = FakeTG()
    sch, q, _ = sched(monkeypatch, tokens, fake)
    q.px = {"S1": (piv["S1"] * 1.02, 50e6, piv["S1"] * 0.99, piv["S1"] * 1.02),     # strong
            "S2": (piv["S2"] * 1.02, 1.0, piv["S2"] * 0.99, piv["S2"] * 1.02),      # weak
            "S3": (piv["S3"] * 1.09, 50e6, piv["S3"], piv["S3"] * 1.09)}            # beyond the buy zone
    r = sch.run_intraday(ist(*DAY, 11, 0))
    assert r["sent"] == 3
    text = {h.split("</b>")[0].split("<b>")[-1]: h for _, h in fake.sent}
    assert "Strong volume" in text["S1"] and "Weak volume" in text["S2"] and "wait for the 15:15 check" in text["S2"]
    assert "Extended" in text["S3"] and "Strong volume" not in text["S3"]


def test_failed_breakout_once_and_only_after_crossing(monkeypatch):
    tokens, piv = forming_db(["S1", "S2"])
    fake = FakeTG()
    sch, q, _ = sched(monkeypatch, tokens, fake)
    p1, p2 = piv["S1"], piv["S2"]
    q.px = {"S1": (p1 * 1.01, 50e6, p1 * 0.99, p1 * 1.012), "S2": (p2 * 0.97, 1e6, p2 * 0.96, p2 * 0.98)}
    sch.run_intraday(ist(*DAY, 10, 0))
    q.px["S1"] = (p1 * 0.995, 50e6, p1 * 0.99, p1 * 1.012)                           # inside the 1% buffer
    assert sch.run_intraday(ist(*DAY, 10, 5))["failed"] == 0
    q.px["S1"] = (p1 * 0.98, 50e6, p1 * 0.97, p1 * 1.012)
    assert sch.run_intraday(ist(*DAY, 10, 10))["failed"] == 1
    assert sch.run_intraday(ist(*DAY, 10, 15))["failed"] == 0                         # once a day
    failed = [h for _, h in fake.sent if "breakout failing" in h]
    assert len(failed) == 1 and "S1" in failed[0] and "today's high" in failed[0]   # S2 never crossed


def test_close_check_verdicts_and_charts(monkeypatch):
    tokens, piv = forming_db(["H1", "W1", "X1", "B1"])
    fake = FakeTG()
    sch, q, _ = sched(monkeypatch, tokens, fake)
    q.px = {"H1": (piv["H1"] * 1.02, 50e6, piv["H1"] * 0.98, piv["H1"] * 1.021),    # holding
            "W1": (piv["W1"] * 1.02, 1.0, piv["W1"] * 0.98, piv["W1"] * 1.021),     # weak volume
            "X1": (piv["X1"] * 1.08, 50e6, piv["X1"], piv["X1"] * 1.08),            # extended
            "B1": (piv["B1"] * 0.99, 50e6, piv["B1"] * 0.98, piv["B1"])}            # below: ignored
    assert "close_check" in due_jobs(ist(*DAY, 15, 15), sch.state.config.alerts, set(), True)
    sch.run_close_check(ist(*DAY, 15, 15))
    heads = [h for _, h in fake.sent]                       # first line may be the market line
    assert any("H1: breakout holding into the close" in h for h in heads)
    assert any("W1: above the pivot, but weak" in h for h in heads)
    assert any("X1: too far above the pivot" in h for h in heads)
    assert not any("<b>B1:" in h for h in heads) and len(fake.sent) == 3          # 3 names: one each
    holding = next(h for _, h in fake.sent if "H1: breakout holding" in h)
    assert "act before 15:30" in holding and "Verdict: Holding into the close" in holding
    assert ("photo", holding) in fake.sent
    with get_db() as conn:
        assert itd.flags(conn, "2026-10-26") == {"H1": {"close_holding"}, "W1": {"close_weak"},
                                                 "X1": {"close_extended"}}
    assert "close_check" in Scheduler.done_today(ist(*DAY).date())                  # once a day


def test_close_check_summarises_many(monkeypatch):
    names = ["A1", "A2", "A3", "A4", "A5"]
    tokens, piv = forming_db(names)
    fake = FakeTG()
    sch, q, _ = sched(monkeypatch, tokens, fake)
    q.px = {n: (piv[n] * 1.02, (i + 1) * 20e6, piv[n] * 0.98, piv[n] * 1.021) for i, n in enumerate(names)}
    r = sch.run_close_check(ist(*DAY, 15, 15))
    assert r["counts"]["holding"] == 5 and r["sent"] == 4
    charts = [h for k, h in fake.sent if k == "photo"]
    assert [h.split(":")[0].split("<b>")[1] for h in charts] == ["A5", "A4", "A3"]  # strongest volume first
    summary = [h for k, h in fake.sent if k == "msg"][0]
    assert "5 names above their pivot" in summary and "Also holding: A2" in summary and "A1" in summary
    assert "A5" not in summary


def test_close_check_without_login_is_recorded(monkeypatch):
    tokens, _ = forming_db(["S1"])
    sch, _, st = sched(monkeypatch, tokens, FakeTG())
    st._token = False
    assert sch.run_close_check(ist(*DAY, 15, 15))["status"] == "no_login"
    assert "close_check" in Scheduler.done_today(ist(*DAY).date())


# --- 18:30 cross-reference ---------------------------------------------------------

def test_evening_alerts_refer_to_the_close_check():
    as_of, tokens = scanned_db()
    with get_db() as conn:
        itd.set_flag(conn, as_of, "FRESH", "close_holding")
        itd.set_flag(conn, as_of, "NEAR", "close_holding")
    cfg = AlertsConfig(chat_id="42", tiering=False, screens=["bases"], near_pivot_pct=0.05)
    fake = FakeTG()
    dispatch_scan(as_of, cfg, BaseConfig(reset_ma=0), Notifier(cfg, fake), tokens)
    bo = next(h for _, h in fake.sent if "FRESH" in h and "broke out" in h)
    assert "Flagged at 15:15" in bo
    nc = [h for _, h in fake.sent if "didn't confirm on the close" in h]
    assert len(nc) == 1 and "NEAR" in nc[0]


def test_messages_escape():
    p = {"symbol": "A&B", "pivot": 100.0, "base_label": "B1 <5w>"}
    assert "A&amp;B" in msg.failed_alert(p, 98.5, 101.2, 95.0)
    assert "B1 &lt;5w&gt;" in msg.close_alert(p, "weak", 101.0, 1.1, 0.4, 105.0)
    s = msg.close_summary([{"symbol": "A&B", "verdict": "weak", "ltp": 101.0, "pivot": 100.0, "pace": None}], set())
    assert "A&amp;B (+1.0%, n/a)" in s
