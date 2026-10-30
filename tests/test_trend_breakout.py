"""Trend Breakout: the indicators, delivery charges, the entry and exit rules, the fixed stop (from the entry day),
the fill at the next open (no look-ahead), sizing, the market filter, the run, the evening scan and the API."""

from datetime import date, timedelta
from math import floor

import pytest

from backend.config import AppConfig, BreakoutConfig
from backend.database.models import get_db, upsert_candles
from backend.engine import trend_breakout as tb

P = BreakoutConfig()


def days(n, start=date(2019, 1, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def trend(n=300, breakout_at=None, vol=100_000, start=100.0):
    """A steady climb with a pause every fifth day; the 25 days before `breakout_at` go flat, then a jump on 3x volume."""
    rows, p = [], start
    for i, d in enumerate(days(n)):
        if breakout_at is not None and breakout_at - 25 <= i < breakout_at:
            o = c = p
        elif breakout_at is not None and i == breakout_at:
            o, c = p, p * 1.04
        else:
            o, c = p, p * (1.004 if i % 5 else 0.998)
        p = c
        v = vol * 3 if i == breakout_at else vol
        rows.append({"date": d, "open": o, "high": max(o, c) * 1.002, "low": min(o, c) * 0.998, "close": c, "volume": v})
    return rows


def universe(series):
    cal = days(max(len(r) for r in series.values()))
    pos = {d: i for i, d in enumerate(cal)}
    S = {sym: {"rows": rows, "ind": tb.indicators(rows), "at": {pos[r["date"]]: j for j, r in enumerate(rows)}}
         for sym, rows in series.items()}
    return cal, S


# --- indicators and costs ---------------------------------------------------------------------------

def test_indicators():
    rows = trend(300)
    ind = tb.indicators(rows)
    c = [r["close"] for r in rows]
    assert ind["sma50"][49] == pytest.approx(sum(c[:50]) / 50) and ind["sma50"][48] is None
    assert ind["sma200_20"][250] == ind["sma200"][230]
    assert ind["hi20_prev"][100] == max(r["high"] for r in rows[80:100])           # today not counted
    assert ind["hi252"][299] == max(r["high"] for r in rows[48:300])
    assert ind["ret126"][200] == pytest.approx(c[200] / c[74] - 1)
    tr = [max(rows[i]["high"], c[i - 1]) - min(rows[i]["low"], c[i - 1]) for i in range(1, 15)]
    assert ind["atr"][14] == pytest.approx(sum(tr) / 14)


def test_delivery_charges():
    buy, sell = tb.delivery_charges(100_000, False), tb.delivery_charges(100_000, True)
    exch, sebi = 100_000 * 0.00297 / 100, 100_000 * 10 / 1e7
    assert buy == pytest.approx(100 + exch + sebi + 0.18 * (exch + sebi) + 15)        # stamp 0.015% on the buy
    assert sell == pytest.approx(100 + exch + sebi + 0.18 * (exch + sebi) + 15.93)    # DP on the sale


# --- the rules ------------------------------------------------------------------------------------

def test_entry_needs_the_breakout_and_the_volume():
    cal, S = universe({"UP": trend(300, breakout_at=280)})
    _, buys, n, pool = tb.decide(280, S, {}, True, P)
    r = S["UP"]["rows"][280]
    stop = max(r["close"] - 2 * S["UP"]["ind"]["atr"][280], r["close"] * 0.92)
    assert buys == [("UP", pytest.approx(stop))] and n == 1 and pool == 1
    assert tb.decide(279, S, {}, True, P)[1] == []                                   # the flat day before
    weak = trend(300, breakout_at=280)
    weak[280]["volume"] = 140_000                                                     # 1.4x: under 1.5x
    assert tb.decide(280, universe({"UP": weak})[1], {}, True, P)[1] == []
    _, off, n_off, _ = tb.decide(280, S, {}, False, P)
    assert off == [] and n_off == 1                                                   # market filter off: counted, not bought


def test_ranked_by_six_month_return_and_cheap_stocks_left_out():
    cal, S = universe({"FAST": trend(300, 280, start=100), "SLOW": trend(300, 280, start=300),
                       "CHEAP": trend(300, 280, start=20)})
    S["SLOW"]["ind"]["ret126"][280] = 0.01
    assert [s for s, _ in tb.decide(280, S, {}, True, P)[1]] == ["FAST", "SLOW"]


def sim(series, p=P, first=260, regime=True):
    cal, S = universe(series)
    return cal, S, tb.simulate(cal, S, [regime] * len(cal), p, 0.0, first)


def test_fills_at_the_next_open_not_the_signal_close():
    rows = trend(300, breakout_at=280)
    rows[281]["open"] = rows[280]["close"] * 1.01
    cal, S, (eq, eqg, trades, funnel) = sim({"UP": rows})
    t = trades[0]
    assert t["in"] == cal[281] and t["buy"] == round(rows[281]["open"], 2) and t["why"] == "end"
    assert t["qty"] == floor(min(P.capital / 10, P.capital) / (rows[281]["open"] * 1.0012))
    assert funnel["signals"] >= 1 and eq[-1] == pytest.approx(P.capital + t["net"])


def test_stop_counts_from_the_entry_day():
    rows = trend(300, breakout_at=280)
    rows[281]["low"] = rows[280]["close"] * 0.80                                      # through the stop on day one
    cal, S, (_, _, trades, _) = sim({"UP": rows})
    stop = max(rows[280]["close"] - 2 * S["UP"]["ind"]["atr"][280], rows[280]["close"] * 0.92)
    assert trades[0]["why"] == "stop" and trades[0]["out"] == cal[281]
    assert trades[0]["sell"] == round(min(rows[281]["open"], stop), 2)


def test_trend_exit_at_the_next_open_after_a_close_under_sma50():
    rows = trend(300, breakout_at=280)
    for k in range(283, 300):                                                         # drift down, lows over the stop
        c = rows[282]["close"] * (1 - 0.004 * (k - 282))
        rows[k].update(open=c, close=c, high=c * 1.001, low=c * 0.999)
    cal, S, (_, _, trades, _) = sim({"UP": rows}, p=BreakoutConfig(atr_mult=100))      # stop at -8%
    t = trades[0]
    under = next(k for k in range(282, 300) if rows[k]["close"] < S["UP"]["ind"]["sma50"][k])
    assert t["why"] == "exit" and t["out"] == cal[under + 1] and t["sell"] == round(rows[under + 1]["open"], 2)
    assert t["net"] == pytest.approx(t["gross"] - t["costs"])


def test_market_filter_off_blocks_new_buys():
    _, _, (_, _, trades, funnel) = sim({"UP": trend(300, breakout_at=280)}, regime=False)
    assert trades == [] and funnel["market_off"] >= 1
    _, _, (_, _, trades, _) = sim({"UP": trend(300, breakout_at=280)}, p=BreakoutConfig(regime=False), regime=False)
    assert len(trades) == 1


def test_periods_and_perf():
    assert tb.periods(100) == [("Development", 0, 50), ("Validation", 50, 75), ("Out-of-sample", 75, 100)]
    ret, cagr, dd = tb.perf([100, 120, 90, 121], 100, 248)
    assert ret == pytest.approx(21) and cagr == pytest.approx(21) and dd == pytest.approx(25)


# --- run, scan and API ----------------------------------------------------------------------------

def store(monkeypatch, n=420):
    idx = trend(n, start=10_000.0)
    up = trend(n, breakout_at=n - 40)
    with get_db() as conn:
        upsert_candles(conn, 1, idx)
        upsert_candles(conn, 2, up)
        upsert_candles(conn, 3, trend(n, start=500.0))
    stocks = [{"symbol": "UP", "tradingsymbol": "UP", "instrument_token": 2},
              {"symbol": "STEADY", "tradingsymbol": "STEADY", "instrument_token": 3}]
    monkeypatch.setattr("backend.ingest.universe.resolve_universe",
                        lambda cfg, *a: {"stocks": stocks, "indices": [{"symbol": "NIFTY 50", "instrument_token": 1}]})
    return up


class FakeKite:
    has_token = True

    def holdings(self):
        return [{"tradingsymbol": "STEADY", "quantity": 10, "average_price": 99_999.0},
                {"tradingsymbol": "OTHER", "quantity": 5, "average_price": 100.0, "last_price": 80.0}]


def test_run_scan_and_api(monkeypatch):
    from fastapi.testclient import TestClient
    store(monkeypatch)
    cfg = AppConfig()
    r = tb.run(cfg)
    assert r["status"] == "ok" and r["A"]["trades"] >= 1 and "E" in r
    s = tb.saved()
    for v in ("A", "E"):
        x = s["variants"][v]
        assert [y["name"] for y in x["splits"]] == ["Development", "Validation", "Out-of-sample"]
        assert len(x["robust"]) == 14 and s["sessions"] == len(x["curve"]["normal"]) == len(s["curve"]["dates"])
        assert x["normal"]["net"] == pytest.approx(x["normal"]["gross"] - x["normal"]["costs"], abs=0.02)
        assert 0 <= x["allowed_pct"] <= 100
    sc = tb.run_scan(cfg, FakeKite())
    assert sc["market_on"] and sc["holdings_read"] and sc["filters"]["A"]["state"] == "ON"
    assert sc["filters"]["E"]["state"] == "FULL"
    acts = {h["symbol"]: h["action"] for h in sc["holdings"]}
    assert acts == {"STEADY": "SELL at open", "OTHER": "SELL at open"}                # 8% under the average price
    from backend.alerts import messages as msg
    assert "signals only, no orders" in msg.breakout_scan(sc) and "STEADY" in msg.breakout_scan(sc)
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        d = tc.get("/api/breakout").json()
        assert d["built"] and "trades" not in d["run"]["variants"]["A"] and d["rules"]["max_pos"] == 10
        assert d["scan"]["as_of"] and set(d["run"]["variants"]) == {"A", "E"}
        t = tc.get("/api/breakout/trades?variant=E&scenario=stress&limit=1").json()
        assert t["total"] >= 1 and len(t["rows"]) == 1
        assert tc.get("/api/breakout/trades?scenario=x").status_code == 422
        assert tc.get("/api/breakout/trades?variant=Z").status_code == 422
        a = tc.get("/api/attention").json()
        assert set(a) == {"breakout", "paper"} and a["breakout"]["level"] in ("warn", "sell", "filter", "buy", "running")


def test_no_data_message(monkeypatch):
    monkeypatch.setattr("backend.ingest.universe.resolve_universe", lambda cfg, *a: {"stocks": [], "indices": []})
    assert tb.run(AppConfig())["status"] == "no_data"


def test_old_orb_and_track_tables_are_dropped(tmp_path):
    import sqlite3
    from backend.database.models import init_db
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);"
                       "CREATE TABLE orb_signals (day TEXT); CREATE TABLE candles_5m (ts TEXT);"
                       "CREATE TABLE track_signals (x TEXT);"
                       "INSERT INTO app_settings VALUES ('config.orb', '{}', ''), ('orb.as_of', 'x', ''), ('keep', '1', '');")
    conn.commit()
    conn.close()
    init_db(str(path))
    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not tables & {"orb_signals", "candles_5m", "track_signals"} and "breakout_runs" in tables
    assert [r[0] for r in conn.execute("SELECT key FROM app_settings")] == ["keep"]


# --- E: the half filter ---------------------------------------------------------------------------

def index_path(n=400):
    """NIFTY closes: a rise (over both averages), a slide under the 200-day but over the 50-day, then a fall."""
    out, p = [], 100.0
    for i in range(n):
        p *= 1.003 if i < 300 else (0.997 if i < 330 else (1.002 if i < 360 else 0.99))
        out.append(p)
    return out


def test_filter_uses_only_data_up_to_the_day():
    closes = index_path()
    for mode in ("sma200", "half"):
        full = [tb.filter_day(mode, i, closes) for i in range(len(closes))]
        assert all(tb.filter_day(mode, i, closes[:i + 1]) == full[i] for i in range(0, len(closes), 7))
    half = tb.market_filters(closes)["half"]
    assert set(half) == {0.0, 0.5, 1.0}


def test_half_filter_caps_new_buys_at_five_and_sizes_at_a_tenth():
    series = {f"S{k}": trend(300, breakout_at=280, start=100 + k) for k in range(8)}
    cal, S = universe(series)
    E = BreakoutConfig(regime_mode="half")
    half = {"half": [0.5] * len(cal)}                          # NIFTY between its 50-day and 200-day SMAs
    _, _, all_trades, _ = tb.simulate(cal, S, [True] * len(cal), E, 0.0, 260, filters={"half": [1.0] * len(cal)})
    assert len({t["sym"] for t in all_trades}) == 8                                  # FULL: all eight fit
    _, _, t2, f2 = tb.simulate(cal, S, [False] * len(cal), E, 0.0, 260, filters=half)
    assert len(t2) == 5 and f2["no_slot"] >= 3                                        # HALF: five, the rest wait
    assert all(t["qty"] * t["buy"] <= P.capital / 10 for t in t2)                     # still equity / 10 each


def test_the_filter_never_sells():
    cal, S = universe({"UP": trend(300, breakout_at=280)})
    E = BreakoutConfig(regime_mode="half")
    f = [1.0] * 282 + [0.0] * (len(cal) - 282)                                        # OFF the day after the buy
    _, _, trades, _ = tb.simulate(cal, S, [True] * 282 + [False] * (len(cal) - 282), E, 0.0, 260, filters={"half": f})
    assert len(trades) == 1 and trades[0]["why"] == "end"                             # held to the end


def test_variant_a_is_unchanged_by_the_filters_argument():
    cal, S = universe({"UP": trend(300, breakout_at=280), "B": trend(300, breakout_at=270, start=300)})
    ok = [True] * len(cal)
    a = tb.simulate(cal, S, ok, P, 0.1, 260)
    b = tb.simulate(cal, S, ok, P, 0.1, 260, filters={"half": [0.5] * len(cal)})
    assert a[0] == b[0] and a[2] == b[2]
