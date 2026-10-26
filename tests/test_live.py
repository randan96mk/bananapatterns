"""Market hours: entry checklist and verdict, size, opening range, live position guard (under stop, heavy selling,
holdings re-read, 09:10 and 15:15 position checks, Quiet today), the ⬆️ upgrade note, and the Today page."""

from datetime import datetime, timedelta

import pytest

from backend.alerts import guard
from backend.alerts import intraday as itd
from backend.alerts import live as lv
from backend.alerts.dispatch import Notifier
from backend.alerts.jobs import Scheduler
from backend.config import IST, AlertsConfig, VcpConfig
from tests.test_alerts import FakeState, FakeTG
from tests.test_intraday import forming_db


def ist(*a):
    return datetime(*a, tzinfo=IST)


DAY = (2026, 10, 26)                                   # a Monday
CFG = AlertsConfig()


# --- pure rules ----------------------------------------------------------------------

def test_checklist_verdicts():
    ok = dict(vwap=101.0, pace=2.0, stock_pct=2.0, index_pct=0.5, or_high=101.5)
    v = lambda ltp, **kw: lv.checklist(ltp, 100.0, CFG, **{**ok, **kw})                   # noqa: E731
    assert v(102.0)["verdict"] == "valid" and v(102.0)["passed"] == 5
    w = v(102.0, pace=1.1)
    assert w["verdict"] == "wait" and w["passed"] == 4 and "volume" in w["reason"]
    assert v(102.0, or_high=None)["verdict"] == "wait"                    # opening range not over: not passed
    assert v(102.0, stock_pct=0.2)["verdict"] == "wait"                   # weaker than the Nifty 500
    s = v(106.0)
    assert s["verdict"] == "skip" and "extended" in s["reason"] and s["zone_top"] == 105.0
    assert v(102.0, vwap=103.0, pace=0.8)["verdict"] == "skip"           # under VWAP on weak volume
    assert v(102.0, vwap=103.0, pace=1.2)["verdict"] == "wait"           # under VWAP, volume fine
    assert v(99.0)["verdict"] is None                                    # not above the pivot
    texts = [t for _, _, t in v(102.0)["checks"]]
    assert texts[0].startswith("In the buy zone: +2.0%") and "Above VWAP" in texts[1]


def test_size_matches_the_sample():
    cfg = AlertsConfig(capital=10_00_000, risk_pct=0.01)
    s = lv.size(2052.40, 1853.43, cfg, 0.5)
    assert s["qty"] == 25 and s["risk_rs"] == 4974
    assert lv.size(2052.40, 1853.43, AlertsConfig(), 1.0) is None        # capital not set
    assert lv.size(100, 101, cfg) is None and lv.size(100, None, cfg) is None


def bars(highs, start=(9, 15)):
    t = datetime(2026, 1, 1, *start)
    return [{"time": (t + timedelta(minutes=5 * i)).strftime("%H:%M"), "close": h - 1, "high": h, "low": h - 2}
            for i, h in enumerate(highs)]


def test_opening_high():
    rows = bars([10, 12, 11, 15, 13, 14, 20, 30])                        # 09:15 .. 09:50
    assert lv.opening_high(rows, 30) == 15                               # bars starting before 09:45
    assert lv.opening_high(rows, 30, ist(*DAY, 9, 40)) is None           # the window isn't over yet
    assert lv.opening_high(rows, 30, ist(*DAY, 9, 45)) == 15
    assert lv.opening_high([], 30) is None


def test_guard_rules():
    pos = {"stop": 92.0, "avg_price": 100.0}
    assert lv.guard_rules(pos, 91.0, -3.0, 1.0, CFG) == ["under_stop"]
    assert lv.guard_rules(pos, 95.0, -5.0, 2.5, CFG) == ["heavy"]
    assert lv.guard_rules(pos, 95.0, -5.0, 1.2, CFG) == []              # a light-volume dip is not heavy selling
    assert lv.guard_rules(pos, 95.0, -2.0, 3.0, CFG) == []
    assert lv.guard_rules(pos, 95.0, -5.0, None, CFG) == []             # volume unknown
    assert lv.r_multiple(pos, 104.0) == 0.5


# --- scheduler ------------------------------------------------------------------------

class Live:
    """Kite stub: quotes (ltp, previous close, volume, VWAP, open), 5-minute history, holdings."""

    def __init__(self):
        self.px, self.hold, self.or_highs, self.hist_calls, self.holdings_calls = {}, [], {}, 0, 0

    def quote(self, keys):
        out = {}
        for k in keys:
            sym = k.split(":")[1]
            if sym == "NIFTY 500":
                out[k] = {"last_price": 100.5, "ohlc": {"close": 100.0}}
            elif sym in self.px:
                d = self.px[sym]
                out[k] = {"last_price": d["ltp"], "volume": d.get("vol", 0), "average_price": d.get("vwap"),
                          "ohlc": {"close": d["prev"], "open": d.get("open", d["prev"]), "high": d["ltp"], "low": d["ltp"]}}
        return out

    def historical_intraday(self, token, day, interval="5minute", label=""):
        self.hist_calls += 1
        return bars([self.or_highs.get(label, 1.0)] * 8)

    def holdings(self):
        self.holdings_calls += 1
        return self.hold

    def positions(self):
        return {"net": []}


def setup(monkeypatch, names=("S1",), **cfg):
    tokens, piv = forming_db(list(names))
    fake, st, client = FakeTG(), FakeState(), Live()
    st.client = client
    for k, v in cfg.items():
        setattr(st.config.alerts, k, v)
    monkeypatch.setattr("backend.alerts.jobs.uni.resolve_universe",
                        lambda c: {"stocks": [{"symbol": s, "instrument_token": t} for s, t in tokens.items()]})
    monkeypatch.setattr("backend.ingest.universe.resolve_universe",
                        lambda c: {"stocks": [{"symbol": s, "instrument_token": t} for s, t in tokens.items()]})
    return Scheduler(st, notifier_factory=lambda c: Notifier(c, fake)), client, fake, tokens, piv


def hold(client, sym, qty=10, avg=100.0):
    client.hold.append({"tradingsymbol": sym, "quantity": qty, "average_price": avg})
    guard.sync(guard.kite_holdings(client), VcpConfig())
    return next(p for p in guard.positions() if p["symbol"] == sym)


def pace_volume(token, pace, now):
    """The day's volume so far that projects to `pace` x the 50-day average at `now`."""
    return lv.avg_volume(token) * itd.day_share(now) * pace


def test_live_guard_under_stop_once_with_buttons(monkeypatch):
    sch, client, fake, tokens, piv = setup(monkeypatch, ["S1"])
    pos = hold(client, "HELD", avg=100.0)                                 # no setup: stop = average - 8%
    assert pos["stop"] == 92.0
    now = ist(*DAY, 11, 0)
    client.px = {"HELD": {"ltp": 91.0, "prev": 95.0}}
    r = sch.run_intraday(now)
    under = [h for _, h in fake.sent if "under your stop" in h]
    assert r["guard"]["sent"] == 1 and len(under) == 1 and "Unconfirmed until the close" in under[0]
    assert [b[2] for b in fake.buttons] == ["c:HELD", "q:HELD"]
    sch.run_intraday(now + timedelta(minutes=5))
    Scheduler(sch.state, notifier_factory=sch.notifier_factory).run_intraday(now + timedelta(minutes=10))  # restart
    assert sum("under your stop" in h for _, h in fake.sent) == 1         # once per day
    sch.state.config.alerts.hard_stop = True
    assert "exit now" in __import__("backend.alerts.messages", fromlist=["x"]).live_stop_alert(
        pos, 91.0, -1.1, None, True)


def test_heavy_selling_and_quiet_today(monkeypatch):
    sch, client, fake, tokens, piv = setup(monkeypatch, ["S1", "S2"])
    now = ist(*DAY, 11, 0)
    s1 = hold(client, "S1", avg=piv["S1"])
    s2 = hold(client, "S2", avg=piv["S2"])
    client.px = {"S1": {"ltp": s1["avg_price"] * 0.95, "prev": s1["avg_price"], "vol": pace_volume(tokens["S1"], 2.5, now)},
                 "S2": {"ltp": s2["avg_price"] * 0.95, "prev": s2["avg_price"], "vol": pace_volume(tokens["S2"], 2.5, now)}}
    from backend.alerts.commands import BotCommands
    bot = BotCommands(sch.state, clock=lambda: now)
    notifier = Notifier(sch.state.config.alerts, fake)
    fake.answer_callback = lambda *a: None
    assert bot.handle_button({"id": "1", "data": "q:S2", "message": {"chat": {"id": 42}}}, notifier)
    sch.run_intraday(now)
    heavy = [h for _, h in fake.sent if "heavy selling" in h]
    assert len(heavy) == 1 and "<b>S1</b>" in heavy[0] and "2.5× normal volume" in heavy[0]   # S2 is quiet today


def test_holdings_reread_announces_new_buys(monkeypatch):
    sch, client, fake, tokens, piv = setup(monkeypatch, ["S1"])
    now = ist(*DAY, 10, 0)
    sch.run_intraday(now)
    assert client.holdings_calls == 1 and not any("Now following" in h for _, h in fake.sent)
    client.hold.append({"tradingsymbol": "S1", "quantity": 24, "average_price": piv["S1"] * 1.01})
    sch.run_intraday(now + timedelta(minutes=5))
    assert client.holdings_calls == 1                                     # every 30 minutes, not every tick
    sch.run_intraday(now + timedelta(minutes=30))
    new = [h for _, h in fake.sent if "Now following" in h]
    assert client.holdings_calls == 2 and len(new) == 1 and "<b>S1</b> 24 sh" in new[0]
    sch.last_holdings = None
    sch.run_intraday(now + timedelta(minutes=35))
    assert sum("Now following" in h for _, h in fake.sent) == 1


def test_opening_and_close_position_checks(monkeypatch):
    sch, client, fake, tokens, piv = setup(monkeypatch, ["S1"])
    gap = hold(client, "GAPPY", avg=100.0)                                # stop 92
    hold(client, "FINE", avg=50.0)                                        # stop 46
    client.px = {"GAPPY": {"ltp": 90.0, "prev": 95.0, "open": 90.0}, "FINE": {"ltp": 51.0, "prev": 50.0}}
    r = sch.run_gap_check(ist(*DAY, 9, 10))
    opening = [h for _, h in fake.sent if "Opening check · your positions" in h]
    assert r["positions"]["under"] == 1 and len(opening) == 1
    assert "GAPPY</b> opened ₹90.00, <b>under your stop</b> ₹92.00" in opening[0] and "1 other opened above" in opening[0]
    client.px["GAPPY"]["ltp"] = 93.0                                      # recovered by 15:15
    sch.run_close_check(ist(*DAY, 15, 15))
    close = [h for _, h in fake.sent if "Positions at 15:15" in h]
    assert len(close) == 1 and "✅ <b>GAPPY</b> ₹93.00, back above the stop" in close[0] and "FINE: fine" in close[0]
    assert gap["stop"] == 92.0


def test_close_positions_still_under_says_exit(monkeypatch):
    sch, client, fake, tokens, piv = setup(monkeypatch, ["S1"])
    hold(client, "LOSER", avg=100.0)
    client.px = {"LOSER": {"ltp": 90.0, "prev": 93.0}}
    sch.run_close_check(ist(*DAY, 15, 15))
    close = [h for _, h in fake.sent if "Positions at 15:15" in h]
    assert len(close) == 1 and "still under the stop ₹92.00: <b>your rule says exit</b>" in close[0]


def test_heads_up_checklist_then_one_upgrade(monkeypatch):
    sch, client, fake, tokens, piv = setup(monkeypatch, ["S1"], capital=1_000_000.0)
    now = ist(*DAY, 11, 0)
    p = piv["S1"]
    client.or_highs["S1"] = p * 1.015
    client.px = {"S1": {"ltp": p * 1.02, "prev": p, "vwap": p * 1.01, "vol": pace_volume(tokens["S1"], 1.1, now)}}
    sch.run_intraday(now)
    first = next(h for _, h in fake.sent if "<b>S1</b> trading" in h)
    assert "🟡 <b>Wait</b> · 4 of 5" in first and "❌ Volume pace 1.1× (needs 1.5×)" in first
    assert "You'll get a note if it turns green today" in first
    client.px["S1"]["vol"] = pace_volume(tokens["S1"], 2.0, now + timedelta(minutes=5))
    r = sch.run_intraday(now + timedelta(minutes=5))
    up = [h for _, h in fake.sent if "valid entry" in h]
    assert r["upgraded"] == 1 and len(up) == 1 and "🎯 <b>Buy up to" in up[0] and "<b>Qty " in up[0]
    sch.run_intraday(now + timedelta(minutes=10))
    assert sum("valid entry" in h for _, h in fake.sent) == 1 and client.hist_calls == 1   # opening range: once a day


def test_today_snapshot(monkeypatch):
    sch, client, fake, tokens, piv = setup(monkeypatch, ["S1"])
    st = sch.state
    hold(client, "HELD", avg=100.0)
    now = ist(*DAY, 11, 0)
    p = piv["S1"]
    client.or_highs["S1"] = p * 0.99
    client.px = {"HELD": {"ltp": 91.0, "prev": 95.0},
                 "S1": {"ltp": p * 1.02, "prev": p, "vwap": p * 1.01, "vol": pace_volume(tokens["S1"], 2.0, now)}}
    board = lv.LiveBoard(st, clock=lambda: now)
    d = board.snapshot(live=True)
    assert d["live"] and d["status"] == "open" and d["market"]["nifty500_pct"] == 0.5
    held = d["positions"][0]
    assert held["symbol"] == "HELD" and held["live"]["state"] == "under_stop" and held["live"]["day_pct"] == pytest.approx(-4.21)
    s1 = next(s for s in d["setups"] if s["symbol"] == "S1")
    assert s1["live"]["verdict"] == "valid" and s1["live"]["vs_pivot_pct"] == pytest.approx(2.0)
    assert s1["live"]["size"] is None                                     # capital not set
    off = board.snapshot(live=False)
    assert not off["live"] and off["positions"][0]["live"] is None
    sp = board.sparks([{"symbol": "S1", "token": tokens["S1"]}], now)
    assert len(sp["S1"]) == 8
    board.sparks([{"symbol": "S1", "token": tokens["S1"]}], now)
    assert client.hist_calls == 2                                         # opening range + sparkline, then cached


def test_today_api_without_login(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        d = tc.get("/api/today").json()
        assert d["live"] is False and d["positions"] == [] and "setups" in d
        assert tc.get("/api/today/sparks").json() == {"sparks": {}}
        assert tc.get("/api/config/alerts").json()["live_drop_pct"] == 4.0
