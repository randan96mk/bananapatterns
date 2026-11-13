"""🧩 The options module's pure parts: contract selection from the NFO dump (two nearest expiries, the future, the daily
cache, sessions to expiry), Black-Scholes IV / delta sanity, the bias rules on 1-minute bars, the chain reading (walls,
PCR, OI change, the score line) and the strike selection (the Rs 20 floor, the wall rule, the hedge fallback)."""

import math
from datetime import date, datetime, timedelta

import pytest

from backend.config import IST
from backend.database.models import get_db
from backend.options import bias as bs
from backend.options import chain as ch
from backend.options import greeks
from backend.options import instruments as oi
from tests.test_options_book import DAY, EXPIRY, LOT, NEXT, World, path


# --- contracts --------------------------------------------------------------------------------------------------------

def test_pick_contracts_two_nearest_expiries_and_the_future():
    w = World(path({}))
    rows = oi.pick_contracts(w.instruments("NFO"), DAY)
    exps = sorted({r["expiry"] for r in rows if r["type"] != "FUT"})
    assert exps == [EXPIRY.isoformat(), NEXT.isoformat()] and all(r["lot_size"] == LOT for r in rows)
    assert not any(r["tradingsymbol"].startswith("BANKNIFTY") for r in rows) and not any(r["expiry"] < DAY.isoformat() for r in rows)
    futs = [r for r in rows if r["type"] == "FUT"]
    assert len(futs) == 1 and futs[0]["tradingsymbol"] == "NIFTY26NOVFUT" and futs[0]["strike"] == 0.0 and futs[0]["expiry"] == EXPIRY.isoformat()
    n = len([r for r in rows if r["type"] != "FUT"])
    assert n == 2 * 2 * 81                                   # 81 strikes x CE/PE x 2 expiries
    # on expiry day the expiring week still counts
    rows = oi.pick_contracts(w.instruments("NFO"), EXPIRY)
    assert sorted({r["expiry"] for r in rows if r["type"] != "FUT"}) == [EXPIRY.isoformat(), NEXT.isoformat()]
    rows = oi.pick_contracts(w.instruments("NFO"), EXPIRY + timedelta(days=1))
    assert sorted({r["expiry"] for r in rows if r["type"] != "FUT"}) == [NEXT.isoformat()]


def test_refresh_caches_daily_and_the_lookups():
    w = World(path({}))
    r = oi.refresh(w, DAY)
    assert r["skipped"] is False and r["contracts"] == 325 and r["expiries"] == [EXPIRY.isoformat(), NEXT.isoformat()] and w.calls["instruments"] == 1
    assert oi.refresh(w, DAY)["skipped"] is True and w.calls["instruments"] == 1
    assert oi.refresh(w, DAY, force=True)["skipped"] is False and w.calls["instruments"] == 2
    assert oi.near_expiry(DAY.isoformat()) == EXPIRY.isoformat() and oi.near_expiry(EXPIRY.isoformat()) == EXPIRY.isoformat()
    assert oi.near_expiry((EXPIRY + timedelta(days=1)).isoformat()) == NEXT.isoformat() and oi.near_expiry("2027-01-01") is None
    assert len(oi.chain(EXPIRY.isoformat())) == 162 and oi.future()["token"] == 53000000 and oi.lot_size() == LOT
    s = oi.status()
    assert s["as_of"] == DAY.isoformat() and s["contracts"] == 325 and s["future"] == "NIFTY26NOVFUT" and s["lot_size"] == LOT
    assert oi.sessions_to_expiry(EXPIRY, DAY) == 5 and oi.sessions_to_expiry(EXPIRY, EXPIRY) == 0      # the 24th is a holiday
    nxt = DAY + timedelta(days=1)
    oi.refresh(w, nxt)
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(DISTINCT as_of) FROM options_contracts").fetchone()[0] == 2    # yesterday's rows stay
    assert oi.latest_as_of() == nxt.isoformat()


def test_empty_dump_and_no_contracts():
    assert oi.pick_contracts([], DAY) == [] and oi.expiries() == [] and oi.near_expiry() is None and oi.future() is None and oi.lot_size() is None
    assert oi.status() == {"as_of": None, "contracts": 0, "expiries": [], "future": None, "lot_size": None}


# --- greeks -----------------------------------------------------------------------------------------------------------

def test_black_scholes_sanity():
    S, K, T, sig = 25000.0, 24800.0, 5.5 / 252, 0.12
    c, p = greeks.price(S, K, T, sig, "CE"), greeks.price(S, K, T, sig, "PE")
    assert c - p == pytest.approx(S - K * math.exp(-greeks.RATE * T), abs=0.01)             # put-call parity
    assert 0 < greeks.delta(S, K, T, sig, "CE") < 1 and -1 < greeks.delta(S, K, T, sig, "PE") < 0
    assert greeks.delta(S, K, T, sig, "CE") - greeks.delta(S, K, T, sig, "PE") == pytest.approx(1.0, abs=1e-9)
    assert greeks.delta(S, 23000, T, sig, "PE") > -0.02 and greeks.delta(S, 27000, T, sig, "CE") < 0.02
    assert greeks.price(S, K, 0, sig, "CE") == 200.0 and greeks.price(S, K, 0, sig, "PE") == 0.0
    # IV round trip, both sides
    for kind, k in (("PE", 24800.0), ("CE", 25300.0), ("PE", 25100.0)):
        px = greeks.price(S, k, T, sig, kind)
        assert greeks.implied_vol(px, S, k, T, kind) == pytest.approx(sig, abs=1e-4)
    assert greeks.implied_vol(0.0, S, K, T, "PE") is None and greeks.implied_vol(10.0, S, K, T, "CE") is None     # under the intrinsic value
    assert greeks.implied_vol(1.0, S, 20000.0, T, "PE") is None or greeks.implied_vol(1.0, S, 20000.0, T, "PE") > 0.3
    iv, d = greeks.iv_and_delta(None, S, K, T, "PE")
    assert iv is None and d is None
    iv, d = greeks.iv_and_delta(greeks.price(S, K, T, sig, "PE"), S, K, T, "PE")
    assert iv == pytest.approx(sig, abs=1e-4) and d == pytest.approx(greeks.delta(S, K, T, sig, "PE"), abs=1e-3)


def test_time_to_expiry():
    from datetime import time
    assert greeks.time_to_expiry(5, time(9, 15)) == pytest.approx(6 / 252)
    assert greeks.time_to_expiry(5, time(15, 30)) == pytest.approx(5 / 252)
    assert greeks.time_to_expiry(0, time(12, 22)) == pytest.approx((188 / 375) / 252)
    assert greeks.time_to_expiry(0, time(15, 30)) == greeks.MIN_T and greeks.time_to_expiry(-1, time(16, 0)) == greeks.MIN_T
    assert greeks.time_to_expiry(3) == pytest.approx(4 / 252)


# --- bias rules -------------------------------------------------------------------------------------------------------

def bars(levels: dict, start="09:15", end="10:00", vol=0):
    out, m = [], 9 * 60 + 15
    spot = path(levels)
    while m <= int(end[:2]) * 60 + int(end[3:]):
        t = f"{m // 60:02d}:{m % 60:02d}"
        if t >= start:
            px = spot(t)
            out.append({"t": t, "o": px, "h": px + 1, "l": px - 1, "c": px, "v": vol})
        m += 1
    return out


def test_opening_range_vwap_prev_day_and_gap():
    b = bars({"09:30": 25050}, end="09:40")
    assert bs.opening_range(b) == {"high": 25101.0, "low": 24999.0, "bars": 15}
    assert bs.opening_range([]) is None and bs.opening_range([x for x in b if x["t"] >= "09:30"]) is None
    assert bs.vwap(b) is None                                                 # the index has no volume
    fb = bars({"09:30": 25050}, end="09:40", vol=100)
    assert bs.vwap(fb) == pytest.approx(sum(x["c"] for x in fb) / len(fb), abs=0.01)
    assert bs.vwap(fb, until="09:16") == 25000.0 and bs.vwap(fb, until="09:15") is None
    daily = [{"date": "2026-11-13", "high": 1, "low": 0, "close": 1}, {"date": "2026-11-16", "high": 25080, "low": 24900, "close": 25000},
             {"date": "2026-11-17", "high": 9, "low": 9, "close": 9}]
    assert bs.prev_day_levels(daily, "2026-11-17") == {"date": "2026-11-16", "high": 25080.0, "low": 24900.0, "close": 25000.0}
    assert bs.prev_day_levels(daily, "2026-11-13") is None
    assert bs.gap_pct(25100, 25000) == 0.4 and bs.gap_pct(24750, 25000) == -1.0 and bs.gap_pct(None, 25000) is None


def test_five_minute_close_and_the_bias_test():
    b = bars({"09:30": 25050, "09:45": 25150}, end="09:52")
    bar = bs.last_closed_5m(b, "09:50")
    assert bar["t"] == "09:45" and bar["c"] == 25150.0
    assert bs.last_closed_5m(b, "09:49")["t"] == "09:40" and bs.last_closed_5m(b, "09:15") is None
    rng, prev = {"high": 25100.0, "low": 25000.0}, {"high": 25080.0, "low": 24900.0, "close": 25000.0}
    assert bs.test_bias(25150, rng, 25100, prev) == {"bias": "BULLISH", "checks": {"range": 1, "vwap": 1, "prev": 1}}
    assert bs.test_bias(25150, rng, 25200, prev)["bias"] is None                  # under VWAP
    assert bs.test_bias(25090, rng, 25050, {**prev, "high": 25095})["bias"] is None  # the previous high not taken
    assert bs.test_bias(25080, rng, 25050, prev)["bias"] is None                  # inside the range
    assert bs.test_bias(24850, rng, 25000, prev) == {"bias": "BEARISH", "checks": {"range": -1, "vwap": -1, "prev": -1}}
    assert bs.test_bias(24850, rng, None, None)["checks"] == {"range": -1, "vwap": 0, "prev": 0}
    assert bs.event_days(" 2026-11-17, 2026-12-05;bad, 2026-11-17 ") == ["2026-11-17", "2026-12-05"] and bs.event_days("") == []
    assert bs.is_event_day("2026-12-05", "2026-11-17,2026-12-05") and not bs.is_event_day("2026-12-06", "2026-11-17,2026-12-05")
    assert bs.in_window(bs.hm("09:45"), "09:45", "11:00") and bs.in_window(bs.hm("11:00"), "09:45", "11:00") and not bs.in_window(bs.hm("11:01"), "09:45", "11:00")
    assert bs.slot_of(bs.hm("10:07")) == "10:05" and bs.is_5m_close(bs.hm("10:05")) and not bs.is_5m_close(bs.hm("10:07"))


# --- the chain reading ------------------------------------------------------------------------------------------------

def rows_for(spot=25150.0, oi_fn=None, t="09:50", iv=0.12, sessions=5):
    """Chain rows around spot, priced by the fake world (IV / delta included)."""
    from tests.test_options_book import oi_default
    w = World(path({"09:15": spot}), oi_fn=oi_fn or oi_default, iv=iv)
    w.now = t
    oi.refresh(w, DAY)
    rows = ch.pick_rows(oi.chain(EXPIRY.isoformat()), spot)
    return ch.fetch(w, rows, bs.hm(t), sessions, spot, with_vix=True), w


def test_fetch_walls_pcr_and_iv_sides():
    q, w = rows_for()
    assert q["spot"] == 25150.0 and q["vix"] == 14.0 and len(q["rows"]) == 50 and w.calls["quote"] == 1
    r = next(x for x in q["rows"] if x["strike"] == 24800.0 and x["type"] == "PE")
    assert r["bid"] < r["mid"] < r["ask"] and r["mid"] == pytest.approx(w.premium(24800, "PE", "09:50"), abs=0.01) and r["oi"] == 320000
    assert r["iv"] == pytest.approx(0.12, abs=1e-3) and -0.35 < r["delta"] < 0
    w_ = ch.walls(q["rows"], 25150.0)
    assert w_["support"] == 24800.0 and w_["resistance"] == 25300.0 and w_["put_max"] == 24800.0 and w_["call_max"] == 25300.0 and w_["support_oi"] == 320000
    assert ch.pcr(q["rows"], 25150.0) == pytest.approx(sum(x["oi"] for x in q["rows"] if x["type"] == "PE" and 24650 <= x["strike"] <= 25650)
                                                      / sum(x["oi"] for x in q["rows"] if x["type"] == "CE" and 24650 <= x["strike"] <= 25650), abs=0.01)
    assert ch.pcr(q["rows"], 25150.0) > 1.1
    iv = ch.iv_sides(q["rows"], 25150.0)
    assert iv["put"] == pytest.approx(12.0, abs=0.2) and iv["call"] == pytest.approx(12.0, abs=0.2)
    assert ch.strikes_around(25163.0, 2) == [25050.0, 25100.0, 25150.0, 25200.0, 25250.0] and ch.grid(24898.5, how="down") == 24850.0 and ch.grid(25098.5, how="up") == 25100.0
    assert ch.oi_change(q["rows"], None, 25150.0) is None and ch.per_strike_change(q["rows"], None) == {}


def test_oi_change_score_lines_and_snapshots():
    q, w = rows_for()
    w.now = "09:30"
    base = ch.fetch(w, ch.pick_rows(oi.chain(EXPIRY.isoformat()), 25050.0), bs.hm("09:30"), 5, 25050.0)
    chg = ch.oi_change(q["rows"], base["rows"], 25150.0)
    assert chg["calls"] == 0 and chg["puts"] == 20000 * len([k for k in ch.strikes_around(25150.0) if 25150 * 0.98 <= k <= 25150 * 1.02 and k in {r["strike"] for r in base["rows"]}])
    sc = ch.score("BULLISH", q["rows"], 25150.0, base["rows"], 1.1, 0.9)
    assert sc["score"] == 3 and all(p["ok"] for p in sc["parts"]) and sc["line"].endswith("→ 3/3") and not sc["crowded"]
    assert "support 24,800 · resistance 25,300 ✓" in sc["line"] and "OI puts +" in sc["line"] and "PCR 1." in sc["line"]
    sc = ch.score("BEARISH", q["rows"], 25150.0, base["rows"], 1.1, 0.9)
    assert sc["score"] == 1 and [p["ok"] for p in sc["parts"]] == [True, False, False]        # spot under the resistance by > 0.5%
    sc = ch.score("BEARISH", q["rows"], 25150.0, None, 1.1, 0.9)
    assert "no baseline yet ✗" in sc["line"]
    heavy = [{**r, "oi": r["oi"] * (3 if r["type"] == "PE" else 1)} for r in q["rows"]]
    assert ch.score("BULLISH", heavy, 25150.0, None, 1.1, 0.9)["crowded"] and ch.score("BULLISH", heavy, 25150.0, None, 1.1, 0.9)["line"].endswith("· crowded")
    # snapshots: appended, never replaced; the baseline prefers today's 9:30, else the last close
    assert ch.baseline(DAY.isoformat(), EXPIRY.isoformat()) is None
    ch.snapshot((DAY - timedelta(days=1)).isoformat(), "close", EXPIRY.isoformat(), base["rows"], 25000.0, "2026-11-16T15:30:05+05:30")
    b = ch.baseline(DAY.isoformat(), EXPIRY.isoformat())
    assert b["kind"] == "close" and len(b["rows"]) == 50
    ch.snapshot(DAY.isoformat(), "open", EXPIRY.isoformat(), base["rows"], 25050.0, "2026-11-17T09:30:05+05:30")
    ch.snapshot(DAY.isoformat(), "bias", EXPIRY.isoformat(), q["rows"], 25150.0, "2026-11-17T09:50:05+05:30")
    assert ch.baseline(DAY.isoformat(), EXPIRY.isoformat())["kind"] == "open"
    last = ch.last_snapshot()
    assert last["kind"] == "bias" and last["spot"] == 25150.0 and len(last["rows"]) == 50
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM options_chain_snap").fetchone()[0] == 150


def test_select_strikes_wall_rule_floor_and_hedge():
    q, w = rows_for()
    walls = ch.walls(q["rows"], 25150.0)
    sel = ch.select_strikes("BULLISH", q["rows"], 25150.0, walls, 1.0, 0.30, 20.0, 250)
    assert sel["ok"] and sel["sold"]["strike"] == 24800.0 and sel["hedge"]["strike"] == 24550.0 and sel["width"] == 250
    assert sel["otm_strike"] == 24850.0 and sel["delta_strike"] == 24950.0 and sel["wall"] == 24800.0 and sel["credit"] > 20
    # without a wall nearby: the farther of 1% OTM and the delta strike
    sel = ch.select_strikes("BULLISH", q["rows"], 25150.0, {"support": 24550.0, "resistance": None}, 1.0, 0.30, 20.0, 250)
    assert sel["sold"]["strike"] == 24850.0 and sel["hedge"]["strike"] == 24600.0
    # a tighter delta cap pulls the sold strike further out (24800 at |delta| 0.196)
    sel = ch.select_strikes("BULLISH", q["rows"], 25150.0, {"support": None, "resistance": None}, 1.0, 0.20, 20.0, 250)
    assert sel["ok"] and sel["sold"]["strike"] == 24800.0 and sel["hedge"]["strike"] == 24550.0 and abs(sel["sold"]["delta"]) <= 0.20
    # the premium floor steps the strike nearer, one at a time (24850 at Rs 61 fails a Rs 70 floor; 24900 at Rs 74 passes)
    sel = ch.select_strikes("BULLISH", q["rows"], 25150.0, {"support": None, "resistance": None}, 1.0, 0.30, 70.0, 250)
    assert sel["ok"] and sel["sold"]["strike"] == 24900.0 and sel["sold"]["mid"] >= 70 and sel["hedge"]["strike"] == 24650.0
    # a hedge outside the quoted chain: no trade, with the reason
    sel = ch.select_strikes("BULLISH", q["rows"], 25150.0, {"support": None, "resistance": None}, 1.0, 0.10, 20.0, 250)
    assert not sel["ok"] and sel["why"] == "hedge 24,350 PE not quoted"
    # the floor cannot be met before delta 0.35: no trade
    sel = ch.select_strikes("BULLISH", q["rows"], 25150.0, {"support": None, "resistance": None}, 1.0, 0.30, 500.0, 250)
    assert not sel["ok"] and "premium under Rs 500" in sel["why"]
    # bearish mirror: calls, at or above the call wall when it is near
    sel = ch.select_strikes("BEARISH", q["rows"], 25150.0, {"support": None, "resistance": 25300.0}, 1.0, 0.30, 20.0, 250)
    assert sel["ok"] and sel["sold"]["type"] == "CE" and sel["sold"]["strike"] >= 25300.0 and sel["hedge"]["strike"] == sel["sold"]["strike"] + 250
    # the hedge under Rs 2 falls back to 200 points
    cheap = [{**r, "mid": (0.5 if r["strike"] == 24550.0 else r["mid"])} for r in q["rows"]]
    sel = ch.select_strikes("BULLISH", cheap, 25150.0, walls, 1.0, 0.30, 20.0, 250)
    assert sel["hedge"]["strike"] == 24600.0 and sel["width"] == 200
    # the sold side not quoted at all
    assert not ch.select_strikes("BULLISH", [r for r in q["rows"] if r["type"] == "CE"], 25150.0, walls, 1.0, 0.30, 20.0, 250)["ok"]
    # expiry day: tiny premiums push the strike nearer until Rs 20 (the delta cap at 0.35 still holds)
    qx, _ = rows_for(iv=0.18, sessions=0, t="09:50")
    sel = ch.select_strikes("BULLISH", qx["rows"], 25150.0, {"support": None, "resistance": None}, 1.0, 0.30, 20.0, 250)
    assert sel["ok"] and sel["sold"]["mid"] >= 20.0 and abs(sel["sold"]["delta"]) <= 0.35


def test_mid_from_depth():
    assert ch._mid({"depth": {"buy": [{"price": 10.0}], "sell": [{"price": 11.0}]}, "last_price": 10.3}) == (10.0, 11.0, 10.5)
    assert ch._mid({"depth": {"buy": [], "sell": [{"price": 11.0}]}, "last_price": 10.3}) == (None, 11.0, 11.0)
    assert ch._mid({"depth": {"buy": [{"price": 0}], "sell": [{"price": 0}]}, "last_price": 10.3}) == (None, None, 10.3)
    assert ch._mid({}) == (None, None, None)


def test_now_ist_shape_for_snapshots():
    ts = datetime(2026, 11, 17, 9, 30, 5, tzinfo=IST).isoformat(timespec="seconds")
    assert ts == "2026-11-17T09:30:05+05:30" and date.fromisoformat(ts[:10]) == DAY
