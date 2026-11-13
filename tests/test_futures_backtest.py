"""Backtest F on a tiny synthetic universe: cash + futures candles with a basis that converges into each expiry, the
calendar's expiries, a stock whose lot is too big, a ban day, a results date. The long entry at the next futures open
with the ATR stop capped at max_stop_pct, the roll at T-2 with its cost, the basis after the expiry, never on the old
contract on expiry day, the skip counters with their reasons, the daily loss limit, the short rule (breakdown, lower
highs, inverted filter, time stop), the charges maths, the report shape, the short_real gate and the routes."""

import json
from dataclasses import replace
from datetime import date, timedelta

import pytest

from backend.config import AppConfig
from backend.database.models import get_db, now_ist, upsert_candles
from backend.engine import trend_breakout as tb
from backend.futures import backtest as fb
from backend.futures import candles as fc
from tests.test_trend_breakout import days, trend

N = 420
TODAY = now_ist()[:10]


# --- the synthetic universe ---------------------------------------------------------------------------------

def _expiries(dates):
    """{month: expiry} for every month a date falls in, plus the month after the last."""
    months = sorted({d[:7] for d in dates})
    y, m = int(months[-1][:4]), int(months[-1][5:])
    months.append(f"{y + 1 if m == 12 else y:04d}-{1 if m == 12 else m + 1:02d}")
    return {mo: fc.monthly_expiry(int(mo[:4]), int(mo[5:])) for mo in months}


def fut_from_cash(rows, basis_pct=1.0):
    """Futures bars = cash bars x (1 + basis), the basis converging from basis_pct after a roll to 0 at the expiry (so
    the continuous series jumps by basis_pct on the first bar after each expiry)."""
    exps = sorted(_expiries([r["date"] for r in rows]).values())
    out = []
    for r in rows:
        d = date.fromisoformat(r["date"])
        nxt = next(x for x in exps if x >= d)
        prev = next((x for x in reversed(exps) if x < d), nxt - timedelta(days=30))
        frac = (nxt - d).days / max((nxt - prev).days, 1)
        m = 1 + basis_pct / 100 * frac
        out.append({"date": r["date"], "open": r["open"] * m, "high": r["high"] * m, "low": r["low"] * m,
                    "close": r["close"] * m, "volume": r["volume"], "oi": 1000})
    return out


def downtrend(n=N, breakdown_at=None, start=1000.0, vol=100_000):
    """Without `breakdown_at`: a decline with a +1% bump every fifth day (the index under its 50-day average). With it:
    the same decline for 260 sessions, then a contracting range (10-day triangles from a flat floor to a lower high
    each time: lower 5-bar pivot highs, no new 20-day low), 25 flat sessions at the floor, a -4% breakdown, then a
    quiet drift lower (no bumps) so a short runs into its time stop."""
    rows, p = [], start
    floor_px, cycle_start, peak = None, 260, 1.03
    for i, d in enumerate(days(n)):
        bump = False
        if breakdown_at is None or i < 260:
            bump = i % 5 == 0
            o, c = p, p * (1.01 if bump else 0.995)
        elif i < breakdown_at - 25:
            floor_px = floor_px or p
            k = (i - cycle_start) % 10
            if k == 0 and i > cycle_start:
                peak -= 0.0025
            hi = floor_px * peak
            lvl = floor_px + (hi - floor_px) * (k / 5 if k <= 5 else (10 - k) / 5)
            o, c = p, lvl
        elif i < breakdown_at:
            o = c = floor_px
        elif i == breakdown_at:
            o, c = p, p * 0.96
        else:
            o, c = p, p * 0.998
        p = c
        rows.append({"date": d, "open": o, "high": max(o, c) * (1.004 if bump else 1.001), "low": min(o, c) * 0.999,
                     "close": c, "volume": vol * 3 if i == breakdown_at else vol})
    return rows


def store(monkeypatch, stocks: dict, index_rows, lots: dict, lot_values: dict = None):
    """Cash candles for the index (token 1) and the stocks (tokens 2..), futures contracts and continuous futures
    candles for every stock in `lots`."""
    with get_db() as conn:
        upsert_candles(conn, 1, index_rows)
        uni = []
        for k, (sym, rows) in enumerate(stocks.items(), 2):
            upsert_candles(conn, k, rows)
            uni.append({"symbol": sym, "tradingsymbol": sym, "instrument_token": k})
            if sym in lots:
                exp = fc.monthly_expiry(2020, 8).isoformat()
                lv = (lot_values or {}).get(sym, rows[-1]["close"] * lots[sym])
                conn.execute("INSERT OR REPLACE INTO futures_contracts VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (sym, "near", f"{sym}FUT", 1000 + k, exp, lots[sym], lv, 1000, k, TODAY))
                fc.upsert(conn, sym, fut_from_cash(rows))
    monkeypatch.setattr("backend.ingest.universe.resolve_universe",
                        lambda cfg, *a: {"stocks": uni, "indices": [{"symbol": "NIFTY 50", "instrument_token": 1}]})


LOT = 1700                                       # ~Rs 4.75 L a lot at the entry: over small_lot_value, under the 6 L cap


def long_world(monkeypatch, lot=LOT, **lots):
    """The index and three identical breakouts (UP, AUX, ALT: lot 1700 ~ Rs 4.75 L; equal 6-month returns rank them
    UP, BIG, AUX, ALT), a steady name and BIG, the same breakout with a lot of 5000 (~Rs 14 L: too big)."""
    up = trend(N, breakout_at=N - 40)
    stocks = {"UP": up, "AUX": up, "ALT": up, "BIG": up, "STEADY": trend(N, start=500.0)}
    store(monkeypatch, stocks, trend(N, start=10_000.0), {"UP": lot, "AUX": lot, "ALT": lot, "BIG": 5000, **lots},
          {"BIG": 1_600_000.0})
    return up


def cfg_for(**fut):
    c = AppConfig()
    c.futures = replace(c.futures, universe_lot_cap=2_000_000.0, **fut)
    return c


# --- costs -----------------------------------------------------------------------------------------------------

def test_futures_charges_maths():
    buy = fb.futures_charges(100_000, sell=False)
    sell = fb.futures_charges(100_000, sell=True)
    exch, sebi = 1.73, 0.10
    gst = 0.18 * (20 + exch + sebi)
    assert buy == pytest.approx(exch + sebi + gst + 2.0 + 20, abs=1e-6)                  # stamp 0.002% on the buy
    assert sell == pytest.approx(20.0 + exch + sebi + gst + 20, abs=1e-6)                 # STT 0.02% on the sell
    assert fb.fill_price(100, True, 0.05) == pytest.approx(100.05) and fb.fill_price(100, False, 0.05) == pytest.approx(99.95)


def test_pivot_highs_do_not_look_ahead():
    highs = [1, 2, 5, 2, 1, 1, 4, 1, 1, 3, 1, 1]
    last, prev = fb.pivot_highs(highs)
    assert last[3] is None and last[4] == 5 and last[7] == 5 and last[8] == 4 and prev[8] == 5 and last[11] == 3 and prev[11] == 4


# --- the long side -----------------------------------------------------------------------------------------------

def test_long_entry_at_next_futures_open_with_atr_stop_and_cap(monkeypatch):
    long_world(monkeypatch)
    cfg = cfg_for()
    data = fb.load(cfg)
    assert "error" not in data and set(data["F"]) == {"UP", "AUX", "ALT", "BIG"} and data["first"] == 260
    assert any(n.startswith("Lot sizes") for n in data["notes"])
    p = tb.rules(cfg, "E")
    sim = fb.simulate(data, cfg.futures, p, "long", fb.SLIP_PCT)
    t = next(x for x in sim["trades"] if x["sym"] == "UP")
    i_sig = N - 40
    cal = data["cal"]
    F = data["F"]["UP"]
    b = F["rows"][F["at"][i_sig + 1]]
    assert t["in"] == cal[i_sig + 1] and t["entry"] == round(b["open"], 2)                 # the next session's futures open
    atr = F["atr"][F["at"][i_sig]]
    assert t["stop"] == pytest.approx(b["open"] - p.atr_mult * atr, abs=0.01) and t["stop"] > b["open"] * 0.95
    assert t["qty"] == LOT and t["lots"] == 1 and t["why"] == "end" and t["rolls"] == 2 and t["contract"] == "near"
    # the cap: with a 0.5% cap the stop sits exactly 0.5% under the entry
    sim2 = fb.simulate(data, replace(cfg.futures, max_stop_pct=0.5), p, "long", fb.SLIP_PCT)
    t2 = next(x for x in sim2["trades"] if x["sym"] == "UP")
    assert t2["stop"] == pytest.approx(b["open"] * 0.995, abs=0.01)
    # BIG's lot is too big, the third of the equal lots has no slot (lot values over small_lot_value)
    assert sim["skipped"]["lot"] == 1 and sim["skipped"]["slots"] == 1 and sim["no_slot_days"] == 1
    assert {x["sym"] for x in sim["trades"]} == {"UP", "AUX"}
    assert all(k in sim["skipped"] for k in ("lot", "risk", "slots", "ban", "results", "notional", "cash", "heat"))
    # equity is marked to the futures close every day and the margin is reserved from cash
    assert len(sim["eq"]) == len(cal) - data["first"] and sim["margin_worst_pct"] == pytest.approx(2 * 0.2 * b["open"] * LOT / 500000 * 100, abs=0.5)
    assert sim["cash_floor_held"] is True


def test_roll_at_t_minus_2_with_cost_and_basis_after_expiry(monkeypatch):
    long_world(monkeypatch)
    cfg = cfg_for()
    data = fb.load(cfg)
    p = tb.rules(cfg, "E")
    sim = fb.simulate(data, cfg.futures, p, "long", fb.SLIP_PCT)
    cal = data["cal"]
    t = next(x for x in sim["trades"] if x["sym"] == "UP")
    exps = [x for x in data["expiries"] if t["in"] <= x["expiry"] < t["out"]]
    assert len(exps) == 2 and all(fc.monthly_expiry(int(x["expiry"][:4]), int(x["expiry"][5:7])).isoformat() == x["expiry"] for x in exps)
    rolls = [r for r in sim["rolls"] if r["sym"] == "UP" and r["kind"] == "roll"]
    basis = [r for r in sim["rolls"] if r["sym"] == "UP" and r["kind"] == "basis"]
    assert [r["date"] for r in rolls] == [cal[x["i"] - 2] for x in exps]                 # T-2 sessions before each expiry
    assert [r["date"] for r in basis] == [cal[x["i"] + 1] for x in exps]                 # the jump day after each expiry
    F = data["F"]["UP"]
    for r in rolls:                                                                      # an exit's and an entry's charges + slippage
        px = F["rows"][F["at"][cal.index(r["date"])]]["close"]
        f_out, f_in = fb.fill_price(px, False, fb.SLIP_PCT), fb.fill_price(px, True, fb.SLIP_PCT)
        want = fb.futures_charges(f_out * LOT, True) + fb.futures_charges(f_in * LOT, False) + (px - f_out + f_in - px) * LOT
        assert r["cost"] == pytest.approx(want, abs=0.02) and r["cost"] > 0
    for r in basis:                                                                      # ~1% of the notional, net of the cash move
        px = F["rows"][F["at"][cal.index(r["date"])] - 1]["close"]
        assert r["cost"] == pytest.approx(0.01 * px * LOT, rel=0.05)
    assert t["net"] == pytest.approx(t["gross"] - t["costs"], abs=0.02) and t["costs"] > sum(r["cost"] for r in rolls + basis)
    # never on the old contract on expiry day: every position held across an expiry rolled or entered the next month
    for x in data["expiries"]:
        for tr in sim["trades"]:
            if tr["in"] <= x["expiry"] < tr["out"]:
                assert tr["rolls"] >= 1 or tr["contract"] == "next"
    # roll T-1 / T-3 move the roll sessions
    for rd in (1, 3):
        s2 = fb.simulate(data, replace(cfg.futures, roll_days=rd), p, "long", fb.SLIP_PCT)
        assert [r["date"] for r in s2["rolls"] if r["sym"] == "UP" and r["kind"] == "roll"] == [cal[x["i"] - rd] for x in exps]


def test_skips_counted_with_reasons(monkeypatch):
    long_world(monkeypatch)
    cfg = cfg_for()
    data = fb.load(cfg)
    p = tb.rules(cfg, "E")
    cal = data["cal"]
    entry_day = cal[N - 40 + 1]
    # the risk cap, the notional cap and the cash floor, each with its own counter
    assert fb.simulate(data, replace(cfg.futures, max_risk_pct=1.0), p, "long", fb.SLIP_PCT)["skipped"]["risk"] >= 1
    s = fb.simulate(data, replace(cfg.futures, max_notional_x=1.5), p, "long", fb.SLIP_PCT)
    assert s["skipped"]["notional"] >= 1 and len(s["trades"]) == 1
    s = fb.simulate(data, replace(cfg.futures, cash_floor=450_000.0), p, "long", fb.SLIP_PCT)
    assert s["skipped"]["cash"] >= 1 and len(s["trades"]) == 0
    assert fb.simulate(data, replace(cfg.futures, heat_pct=0.5), p, "long", fb.SLIP_PCT)["skipped"]["heat"] >= 1
    # a small lot gets the third slot
    s = fb.simulate(data, replace(cfg.futures, small_lot_value=10_000_000.0, max_notional_x=5.0), p, "long", fb.SLIP_PCT)
    assert s["skipped"]["slots"] == 0 and len(s["trades"]) == 3
    s = fb.simulate(data, replace(cfg.futures, small_lot_value=10_000_000.0), p, "long", fb.SLIP_PCT)
    assert s["skipped"]["notional"] == 1 and len(s["trades"]) == 2                            # ... within the 2.5x notional
    # a ban list in force on the entry day holds AUX; results for ALT two sessions after the entry
    with get_db() as conn:
        conn.execute("INSERT INTO fo_ban VALUES (?, 'AUX')", (entry_day,))
        conn.execute("INSERT INTO corp_events VALUES ('ALT', ?, 'results', 'Results', '', ?)", (cal[N - 40 + 3], now_ist()))
    data = fb.load(cfg)
    sim = fb.simulate(data, cfg.futures, p, "long", fb.SLIP_PCT)
    assert sim["skipped"]["ban"] == 1 and sim["skipped"]["results"] == 1 and sim["skipped"]["lot"] == 1 and sim["skipped"]["slots"] == 0
    assert {x["sym"] for x in sim["trades"]} == {"UP"}
    assert not any("rule was skipped" in n for n in data["notes"])
    # 3 lots a name: three of these lots (~Rs 14 L) never fit the notional cap at this capital
    s = fb.simulate(data, cfg.futures, p, "long", fb.SLIP_PCT, lots=3)
    assert s["trades"] == [] and s["skipped"]["notional"] >= 1 and s["skipped"]["lot"] == 1
    # ... smaller lots do, three at a time
    s = fb.simulate(data, replace(cfg.futures, max_notional_x=10.0), p, "long", fb.SLIP_PCT, lots=3)
    assert len(s["trades"]) == 1 and s["trades"][0]["qty"] == 3 * LOT and s["trades"][0]["lots"] == 3


def test_daily_loss_limit_blocks_entries(monkeypatch):
    long_world(monkeypatch)
    cfg = cfg_for()
    data = fb.load(cfg)
    p = tb.rules(cfg, "E")
    base = fb.simulate(data, cfg.futures, p, "long", fb.SLIP_PCT)
    assert len(base["trades"]) == 2 and base["skipped"]["daily_loss"] == 0
    s = fb.simulate(data, replace(cfg.futures, daily_loss_pct=0.0), p, "long", fb.SLIP_PCT)   # a 0% limit: every day is a loss day
    assert s["trades"] == [] and s["skipped"]["daily_loss"] >= 3 and s["loss_days"] == len(s["eq"])


def test_market_filter_off_blocks_longs(monkeypatch):
    long_world(monkeypatch)
    cfg = cfg_for()
    data = fb.load(cfg)
    p = tb.rules(cfg, "E")
    data["filters"] = {"half": [0.0] * len(data["cal"])}
    assert fb.simulate(data, cfg.futures, p, "long", fb.SLIP_PCT)["trades"] == []
    data["filters"] = {"half": [0.5] * len(data["cal"])}                                   # HALF: one slot
    s = fb.simulate(data, cfg.futures, p, "long", fb.SLIP_PCT)
    assert len(s["trades"]) == 1 and s["skipped"]["slots"] == 3


# --- the short side ---------------------------------------------------------------------------------------------

def test_short_rule_fires_on_a_breakdown_with_the_inverted_filter_and_time_stop(monkeypatch):
    bd = N - 40
    down = downtrend(N, breakdown_at=bd)
    store(monkeypatch, {"DOWN": down, "UPISH": trend(N)}, downtrend(N, start=10_000.0), {"DOWN": 100, "UPISH": 100})
    cfg = cfg_for()
    data = fb.load(cfg)
    p = tb.rules(cfg, "E")
    cal = data["cal"]
    assert data["index_close"][bd] < data["index_m50"][bd]                                  # NIFTY under its 50-day
    assert fb.market(data, bd, "short", p, cfg.futures) == (True, 2) and fb.market(data, bd, "long", p, cfg.futures)[0] is False
    sigs = fb.short_signals(data, bd, p)
    assert [s for s, _ in sigs] == ["DOWN"]
    s_ind = data["F"]["DOWN"]["short"]
    j = data["S"]["DOWN"]["at"][bd]
    assert s_ind["ph_last"][j] < s_ind["ph_prev"][j] and down[bd]["close"] < s_ind["lo20_prev"][j]
    assert all(fb.short_signals(data, k, p) == [] for k in range(data["first"], bd))        # nothing before the breakdown
    sim = fb.simulate(data, cfg.futures, p, "short", fb.SLIP_PCT)
    assert sim["trades"] and all(x["sym"] == "DOWN" and x["why"] in ("time", "end") for x in sim["trades"])
    t = sim["trades"][0]
    F = data["F"]["DOWN"]
    b = F["rows"][F["at"][bd + 1]]
    assert t["side"] == "short" and t["in"] == cal[bd + 1] and t["entry"] == round(b["open"], 2)
    assert t["why"] == "time" and t["days"] == fb.SHORT_TIME_STOP and t["out"] == cal[bd + 1 + fb.SHORT_TIME_STOP]
    assert b["open"] < t["stop"] <= b["open"] * 1.05 and t["stop"] <= sigs[0][1] and t["gross"] > 0
    assert t["net"] == pytest.approx(t["gross"] - t["costs"], abs=0.02)
    # a stock above its averages never shorts; a climbing market never allows one
    assert fb.simulate(data, cfg.futures, p, "long", fb.SLIP_PCT)["trades"] == []
    data["index_m50"] = [0.0] * len(cal)
    assert fb.simulate(data, cfg.futures, p, "short", fb.SLIP_PCT)["trades"] == []


def test_short_results_guard_and_stop_above_entry(monkeypatch):
    bd = N - 40
    down = downtrend(N, breakdown_at=bd)
    store(monkeypatch, {"DOWN": down}, downtrend(N, start=10_000.0), {"DOWN": 100})
    cfg = cfg_for()
    data = fb.load(cfg)
    cal = data["cal"]
    with get_db() as conn:
        conn.execute("INSERT INTO corp_events VALUES ('DOWN', ?, 'results', 'Results', '', ?)", (cal[bd + 2], now_ist()))
    data = fb.load(cfg)
    p = tb.rules(cfg, "E")
    sim = fb.simulate(data, cfg.futures, p, "short", fb.SLIP_PCT)
    assert sim["skipped"]["results"] >= 1 and sim["trades"] and all(t["in"] >= cal[bd + 4] for t in sim["trades"])
    # the stop: ATR x atr_mult over the entry, within the cap, never over the swing high
    assert fb.stop_for("short", 100.0, 1.0, 101.5, p, cfg.futures) == 101.5
    assert fb.stop_for("short", 100.0, 1.0, None, p, cfg.futures) == pytest.approx(102.0)
    assert fb.stop_for("short", 100.0, 10.0, None, p, cfg.futures) == pytest.approx(105.0)
    assert fb.stop_for("long", 100.0, 10.0, None, p, cfg.futures) == pytest.approx(95.0)


# --- the report, the run, the gate and the routes -------------------------------------------------------------------

def test_report_shape_run_saved_and_verdict(monkeypatch):
    long_world(monkeypatch)
    cfg = cfg_for()
    out = fb.build(cfg)
    assert set(out) == {"run", "long", "short", "cash_same_signals", "nifty", "verdict"}
    assert out["run"]["from"] == days(N)[260] and out["run"]["sessions"] == 160 and out["run"]["capital"] == 500000
    assert out["run"]["settings"]["futures"]["max_notional_x"] == 2.5 and out["run"]["notes"]
    for side in ("long", "short"):
        x = out[side]
        for k in ("cagr", "net", "gross", "costs", "rolls", "trades", "win_rate", "avg_win", "avg_loss", "profit_factor", "drawdown",
                  "worst_month", "margin_worst_pct", "cash_floor_held", "skipped", "no_slot_days", "curve", "splits", "years",
                  "robust", "robust_pass", "exits", "trades_rows", "stress", "verdict", "reasons"):
            assert k in x, k
        assert set(x["rolls"]) == {"n", "cost", "basis"} and set(x["exits"]) == set(fb.EXIT_KEYS)
        assert [s["name"] for s in x["splits"]] == list(tb.SPLITS) and set(x["splits"][0]) == {"name", "from", "to", "net", "stress", "nifty"}
        assert len(x["curve"]["dates"]) == len(x["curve"]["net"]) == len(x["curve"]["stress"]) == 160
        labels = [r["label"] for r in x["robust"]]
        assert labels == ["stop cap 4%", "stop cap 6%", "lot cap ₹5 L", "lot cap ₹7 L", "roll T-1", "roll T-3", "slippage +0.3%",
                          "3 lots", "no roll (close at T-2)", "roll winners only", "walk-forward (years)"]
        assert x["robust_of"] == fb.ROBUST_COUNTED == 7 and 0 <= x["robust_pass"] <= 7
        assert isinstance(x["verdict"], bool) and x["net"] == pytest.approx(x["gross"] - x["costs"], abs=0.02)
    assert out["long"]["trades"] == 2 and out["long"]["rolls"]["n"] == 4 and out["long"]["exits"]["end"] == 2
    assert out["short"]["trades"] == 0 and out["short"]["verdict"] is False and out["short"]["reasons"]
    assert set(out["cash_same_signals"]) >= {"cagr", "drawdown", "trades", "costs", "worst_month", "no_slot_days"}
    assert set(out["nifty"]) >= {"cagr", "curve"} and len(out["nifty"]["curve"]) == 160
    assert out["verdict"] == {"long": out["long"]["verdict"], "short": False,
                              "reasons": {"long": out["long"]["reasons"], "short": out["short"]["reasons"]}}
    # the run persists; saved() is the latest row; summary strips the trade lists
    assert fb.saved() is None and fb.short_passed() is False
    r = fb.run(cfg)
    assert r["status"] == "ok" and set(r) == {"status", "long", "short"} and fb.status()["state"] == "idle"
    s = fb.saved()
    assert s["ran_at"] and len(s["long"]["trades_rows"]) == 2 and s["long"]["trades_rows"][0]["sym"] in ("UP", "UP2")
    summ = fb.summary(cfg)
    assert summ["built"] and "trades_rows" not in summ["run"]["long"] and summ["rules"]["robust_min"] == 5
    assert len(fb.csv_rows("long")) == 2 and fb.csv_rows("short") == []
    with get_db() as conn:
        conn.execute("INSERT INTO futures_runs (payload, ran_at) VALUES (?, ?)", (json.dumps({**s, "verdict": {**s["verdict"], "short": True}}), now_ist()))
    assert fb.short_passed() is True


def test_no_data_messages(monkeypatch):
    cfg = cfg_for()
    monkeypatch.setattr("backend.ingest.universe.resolve_universe", lambda c, *a: {"stocks": [], "indices": []})
    assert fb.run(cfg)["status"] == "no_data"
    long_world(monkeypatch)
    with get_db() as conn:
        conn.execute("DELETE FROM futures_contracts")
    assert "contracts" in fb.load(cfg)["error"]


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
    client = make_client(fake_kite, history_years=1)
    client.config.access_token = ""
    monkeypatch.setattr(main, "KiteClient", lambda kc, ic: client)
    with TestClient(main.app) as tc:
        yield tc


def test_routes_and_short_real_gate(api, monkeypatch):
    import threading
    tc = api
    d = tc.get("/api/futures/backtest").json()
    assert d["built"] is False and d["run"] is None and d["progress"]["state"] == "idle" and d["rules"]["futures"]["max_pos"] == 2
    assert tc.get("/api/futures/backtest/trades?side=long").json() == {"total": 0, "rows": []}
    assert tc.get("/api/futures/backtest/trades?side=x").status_code == 422
    assert tc.get("/api/futures/backtest/csv?side=short").text.startswith("sym,side,in,out")
    # shorts cannot go real before a passing run
    r = tc.put("/api/config/futures", json={"short_real": True})
    assert r.status_code == 400 and r.json()["detail"] == "short side backtest has not passed"
    assert tc.put("/api/config/futures", json={"short_real": "true"}).status_code == 400
    assert tc.put("/api/config/futures", json={"short_real": False, "long_paper": True}).status_code == 200
    # a run from the API
    long_world(monkeypatch)
    from backend.api import routes
    routes.S().config.futures = replace(routes.S().config.futures, universe_lot_cap=2_000_000.0)
    assert tc.post("/api/futures/backtest/run").json() == {"status": "started"}
    for t in threading.enumerate():
        if t.name == "futures-backtest":
            t.join(timeout=120)
    d = tc.get("/api/futures/backtest").json()
    assert d["built"] and d["run"]["long"]["trades"] == 2 and "trades_rows" not in d["run"]["long"] and d["run"]["ran_at"]
    t = tc.get("/api/futures/backtest/trades?side=long&limit=1").json()
    assert t["total"] == 2 and len(t["rows"]) == 1 and t["rows"][0]["side"] == "long"
    csv_text = tc.get("/api/futures/backtest/csv?side=long").text
    assert csv_text.count("\n") == 3 and "UP" in csv_text
    assert tc.put("/api/config/futures", json={"short_real": True}).status_code == 400       # the short verdict is False
    s = fb.saved()
    with get_db() as conn:
        conn.execute("INSERT INTO futures_runs (payload, ran_at) VALUES (?, ?)", (json.dumps({**s, "verdict": {**s["verdict"], "short": True}}), now_ist()))
    assert tc.put("/api/config/futures", json={"short_real": True}).status_code == 200
    assert tc.put("/api/config/futures", json={"short_real": False}).status_code == 200
    assert tc.post("/api/futures/backtest/run").json() == {"status": "started"}             # idle again: a second run may start
    for t in threading.enumerate():
        if t.name == "futures-backtest":
            t.join(timeout=120)


def test_roll_mode_never_and_winners(monkeypatch):
    long_world(monkeypatch)
    cfg = cfg_for()
    data = fb.load(cfg)
    p = tb.rules(cfg, "E")
    cal = data["cal"]
    base = fb.simulate(data, cfg.futures, p, "long", fb.SLIP_PCT)
    t = next(x for x in base["trades"] if x["sym"] == "UP")
    exps = [x for x in data["expiries"] if t["in"] <= x["expiry"] < t["out"]]
    assert exps and t["rolls"] == len(exps)
    # never: no position pays a roll; UP closes at T-2 before its first expiry as a roll exit
    never = fb.simulate(data, replace(cfg.futures, roll_mode="never"), p, "long", fb.SLIP_PCT)
    assert not [r for r in never["rolls"] if r["kind"] == "roll"]
    u = next(x for x in never["trades"] if x["sym"] == "UP")
    assert u["why"] == "roll_exit" and u["rolls"] == 0 and u["out"] == cal[exps[0]["i"] - 2 + 1]
    for x in data["expiries"]:                                                   # nothing is held across an expiry on the near month
        for tr in never["trades"]:
            if tr["in"] <= x["expiry"] < tr["out"]:
                assert tr["contract"] == "next"
    # winners: UP (above its entry at T-2) rolls exactly as in the base run
    win = fb.simulate(data, replace(cfg.futures, roll_mode="winners"), p, "long", fb.SLIP_PCT)
    w = next(x for x in win["trades"] if x["sym"] == "UP")
    assert w["rolls"] == t["rolls"] and w["out"] == t["out"] and w["why"] == t["why"]
    labels = [n[0] for n in fb.neighbours(cfg.futures)]
    assert "no roll (close at T-2)" in labels and "roll winners only" in labels
    assert [n[4] for n in fb.neighbours(cfg.futures)].count(True) == fb.ROBUST_COUNTED
