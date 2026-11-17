"""🧩 Report as if · exit rules: closed spreads replayed on seeded chain snapshots. A 2× rule survives a wiggle the 1.5×
rule stops out on; a real VWAP exit is kept only by a rule with VWAP on (at its real time and fills); the time exit; the
charges; days without snapshots skipped; the API shape and the days param."""

import json
from datetime import date, datetime, timedelta

import pytest

from backend.config import IST, AppConfig
from backend.database.models import get_db
from backend.options import asif, chain
from backend.options.book import charges

DAY = "2026-11-10"
EXPIRY = "2026-11-17"
TODAY = date(2026, 11, 17)


def hm_range(start: str, end: str, step: int = 5):
    s = int(start[:2]) * 60 + int(start[3:])
    e = int(end[:2]) * 60 + int(end[3:])
    while s <= e:
        yield f"{s // 60:02d}:{s % 60:02d}"
        s += step


def seed_snaps(day: str, sold_fn, hedge_fn, start="09:55", end="15:10", kind="live", sold_strike=24800.0, hedge_strike=24550.0, typ="PE", no_bid_at=None):
    """One snapshot every 5 minutes: the two legs (bid / ask = mid ∓ 0.5) and one strike nobody holds."""
    for t in hm_range(start, end):
        ts = f"{day}T{t}:05+05:30"
        rows = []
        for k, f in ((sold_strike, sold_fn), (hedge_strike, hedge_fn), (24700.0, lambda _t: 99.0)):
            m = f(t)
            if t == no_bid_at and k == sold_strike:
                rows.append({"strike": k, "type": typ, "ltp": m, "bid": None, "ask": m + 0.5})      # no bid: the last price is the mid
            else:
                rows.append({"strike": k, "type": typ, "ltp": m - 0.25, "bid": m - 0.5, "ask": m + 0.5})
        rows.append({"strike": sold_strike, "type": "CE" if typ == "PE" else "PE", "ltp": 500.0, "bid": 499.0, "ask": 501.0})   # the other type: never read
        chain.snapshot(day, kind, EXPIRY, rows, 25100.0, ts)


def add_trade(day=DAY, why="premium stop", net=-2000.0, opened="09:50:05", closed="10:00:05", sold_exit=61.0, hedge_exit=9.0, sessions=3, **kw):
    t = {"day": day, "n": 1, "bias": "BULLISH", "expiry": EXPIRY, "sessions": sessions, "lots": 1, "lot_size": 75, "qty": 75,
         "sold": {"strike": 24800.0, "type": "PE", "entry": 40.0, "exit": sold_exit}, "hedge": {"strike": 24550.0, "type": "PE", "entry": 10.0, "exit": hedge_exit},
         "credit": 30.0, "net": net, "why": why, "label": why, "opened_at": f"{day}T{opened}+05:30", "closed_at": f"{day}T{closed}+05:30", "in": "9:50 AM", **kw}
    with get_db() as conn:
        conn.execute("INSERT INTO options_trades (day, payload) VALUES (?, ?)", (day, json.dumps(t)))
    return t


def cfg(stop_pct=50.0, stop_pct_expiry=35.0, vwap=True) -> AppConfig:
    c = AppConfig()
    c.options.stop_pct, c.options.stop_pct_expiry, c.options.vwap_exit = stop_pct, stop_pct_expiry, vwap
    return c


def net_of(sold_mid, hedge_mid, qty=75, credit=30.0, sold_in=40.0, hedge_in=10.0):
    sp, hp = round(sold_mid + 1, 2), round(max(0.05, hedge_mid - 1), 2)
    ch = charges(sold_in * qty, True) + charges(hedge_in * qty, False) + charges(sp * qty, False) + charges(hp * qty, True)
    return round((credit - round(sp - hp, 2)) * qty - ch, 2)


def wiggle(t):
    """The sold premium: 40 at entry, 65 at 10:00 (over the 1.5× stop of 60, under the 2× stop of 80), back to 30, then
    a slow drift to 26 by the close (no breakeven: never at 24; no target: the value stays over 12)."""
    if t == "10:00":
        return 65.0
    return 30.0 if t < "12:00" else 26.0


def hedge_flat(t):
    return 10.0 if t == "10:00" else 8.0


def test_2x_survives_the_wiggle_the_1_5x_rule_stops_out_on():
    seed_snaps(DAY, wiggle, hedge_flat)
    add_trade()
    r = asif.report(cfg(), 60, today=TODAY)
    by = {x["key"]: x for x in r["rules"]}
    assert [x["key"] for x in r["rules"]] == ["asis", "x2", "x2_vwap", "x15"] and by["asis"]["actual"] and by["asis"]["label"] == "as is · 1.5× + VWAP"
    assert (by["x2"]["stop_pct"], by["x2"]["stop_pct_expiry"], by["x2"]["vwap"]) == (100.0, 70.0, False) and by["x2_vwap"]["vwap"] is True
    pt = r["per_trade"][0]
    assert pt["by_rule"]["x15"] == {"why": "premium", "net": net_of(65.0, 10.0), "exit_t": "10:00 AM"}
    assert pt["by_rule"]["asis"]["why"] == "premium"
    assert pt["by_rule"]["x2"]["why"] == "time" and pt["by_rule"]["x2"]["exit_t"] == "3:10 PM" and pt["by_rule"]["x2"]["net"] == net_of(26.0, 8.0)
    assert pt["by_rule"]["x2_vwap"] == pt["by_rule"]["x2"]                       # the real exit was not a VWAP one: nothing to keep
    assert pt["actual"] == {"why": "premium", "label": "premium stop", "net": -2000.0} and pt["strikes"] == "24,800 PE / 24,550 PE"
    assert by["x2"]["net"] > by["x15"]["net"] and by["x2"]["won"] == 1 and by["x15"]["won"] == 0
    assert by["x15"]["exits"] == {"premium": 1, "breakeven": 0, "target": 0, "vwap": 0, "time": 0} and by["x2"]["exits"]["time"] == 1
    assert by["x2"]["avg"] == by["x2"]["net"] == by["x2"]["worst"]


def test_charges_are_the_books():
    seed_snaps(DAY, lambda t: 40.0, lambda t: 10.0)
    add_trade(why="time exit", closed="15:10:05")
    res = asif.report(cfg(), 60, today=TODAY)["per_trade"][0]["by_rule"]["x2"]
    # flat: buy back at 41, sell the hedge at 9 -> cost 32 over a 30 credit, less four orders' charges
    ch = charges(40 * 75, True) + charges(10 * 75, False) + charges(41 * 75, False) + charges(9 * 75, True)
    assert res == {"why": "time", "net": round(-2 * 75 - ch, 2), "exit_t": "3:10 PM"} and ch > 80


def test_vwap_exit_kept_only_when_the_rule_has_vwap_on():
    seed_snaps(DAY, lambda t: 36.0 if t < "11:00" else 14.0, lambda t: 9.0 if t < "11:00" else 3.0)   # later the spread hits the 2× rules' target
    add_trade(why="vwap stop", closed="10:32:05", sold_exit=45.0, hedge_exit=8.0, net=-1500.0)
    pt = asif.report(cfg(), 60, today=TODAY)["per_trade"][0]
    real = net_of(44.0, 9.0)                                                     # the real fills 45 / 8 are mid ± 1 of 44 / 9
    assert pt["by_rule"]["x2_vwap"] == {"why": "vwap", "net": real, "exit_t": "10:32 AM"}
    assert pt["by_rule"]["asis"]["why"] == "vwap" and pt["by_rule"]["asis"]["exit_t"] == "10:32 AM"
    assert pt["by_rule"]["x2"]["why"] == "target" and pt["by_rule"]["x2"]["exit_t"] == "11:00 AM" and pt["by_rule"]["x2"]["net"] == net_of(14.0, 3.0)
    assert pt["by_rule"]["x15"]["why"] == "target"
    off = asif.report(cfg(vwap=False), 60, today=TODAY)
    assert off["rules"][0]["label"] == "as is · 1.5× only" and off["per_trade"][0]["by_rule"]["asis"]["why"] == "target"


def test_price_rules_run_before_the_real_vwap_time_and_breakeven():
    """The premium decays 40% (breakeven armed), the value comes back to the credit before the real VWAP exit: the
    breakeven stop fires first, even for a rule with VWAP on."""
    sold = {"09:55": 22.0, "10:00": 41.0}
    seed_snaps(DAY, lambda t: sold.get(t, 41.0), lambda t: 9.0 if t == "09:55" else 11.0)
    add_trade(why="vwap stop", closed="10:30:05")
    pt = asif.report(cfg(), 60, today=TODAY)["per_trade"][0]
    assert pt["by_rule"]["x2_vwap"]["why"] == "breakeven" and pt["by_rule"]["x2_vwap"]["exit_t"] == "10:00 AM"


def test_expiry_day_uses_the_rules_expiry_stop_and_ltp_fallback():
    seed_snaps(DAY, lambda t: 62.0 if t == "10:00" else 30.0, lambda t: 8.0, no_bid_at="10:00")   # 62 > 1.5× 40 = 60 but < 1.7× = 68
    add_trade(sessions=0)
    pt = asif.report(cfg(), 60, today=TODAY)["per_trade"][0]
    assert pt["by_rule"]["x15"]["why"] == "premium"                              # expiry 35%: 54
    assert pt["by_rule"]["x2"]["why"] == "time"                                  # expiry 70%: 68
    assert pt["by_rule"]["x15"]["net"] == net_of(62.0, 8.0)                     # mid = the last price when the bid is missing


def test_snapshots_after_open_only_and_the_other_kinds():
    seed_snaps(DAY, lambda t: 70.0, lambda t: 10.0, start="09:30", end="09:45", kind="bias")             # before the entry: never read
    seed_snaps(DAY, lambda t: 30.0, lambda t: 8.0, start="09:55", end="11:00", kind="bias")
    seed_snaps(DAY, lambda t: 25.0, lambda t: 7.0, start="15:30", end="15:30", kind="close")
    add_trade()
    r = asif.report(cfg(), 60, today=TODAY)
    pt = r["per_trade"][0]
    assert pt["by_rule"]["x15"]["why"] == "time" and pt["by_rule"]["x15"]["exit_t"] == "3:30 PM" and pt["by_rule"]["x15"]["net"] == net_of(25.0, 7.0)
    assert pt["thin"] is True and r["thin"] == 1 and r["snapshots_ok"] == 0 and "thin" in r["note"]


def test_skipped_days_and_the_days_window():
    seed_snaps(DAY, wiggle, hedge_flat)
    add_trade()
    add_trade(day="2026-11-11")                                                  # no snapshots that day
    with get_db() as conn:                                                       # an old row without legs
        conn.execute("INSERT INTO options_trades (day, payload) VALUES (?, ?)", ("2026-11-12", json.dumps({"day": "2026-11-12", "why": "target", "net": 100.0, "bias": "BEARISH"})))
    add_trade(day="2026-08-01")                                                  # older than 60 days
    r = asif.report(cfg(), 60, today=TODAY)
    assert r["trades"] == 3 and r["skipped"] == 2 and r["replayed"] == 1 and r["snapshots_ok"] == 1 and r["from"] == "2026-09-18" and r["to"] == "2026-11-17"
    assert all(x["trades"] == 1 for x in r["rules"]) and "2 trades skipped" in r["note"] and r["note"].startswith(asif.NOTE)
    by_day = {x["day"]: x for x in r["per_trade"]}
    assert by_day["2026-11-11"]["skipped"] == "no snapshots that day" and by_day["2026-11-11"]["by_rule"] == {}
    assert by_day["2026-11-12"]["skipped"] == "no legs recorded" and [x["day"] for x in r["per_trade"]] == ["2026-11-12", "2026-11-11", DAY]
    assert asif.report(cfg(), 120, today=TODAY)["trades"] == 4


def test_empty_report():
    r = asif.report(cfg(), 60, today=TODAY)
    assert r["trades"] == 0 and r["per_trade"] == [] and all(x["trades"] == 0 and x["avg"] is None and x["worst"] is None and x["net"] == 0 for x in r["rules"])


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


def test_api_shape_and_days_param(api):
    today = datetime.now(IST).date()
    recent, old = (today - timedelta(days=5)).isoformat(), (today - timedelta(days=75)).isoformat()
    seed_snaps(recent, wiggle, hedge_flat)
    add_trade(day=recent)
    add_trade(day=old)
    r = api.get("/api/options/asif").json()
    assert set(r) >= {"from", "to", "trades", "snapshots_ok", "rules", "per_trade", "note"} and r["trades"] == 1 and r["days"] == 60
    assert set(r["rules"][0]) >= {"key", "label", "stop_pct", "stop_pct_expiry", "vwap", "trades", "won", "net", "avg", "worst", "exits"}
    assert set(r["rules"][0]["exits"]) == {"premium", "breakeven", "target", "vwap", "time"}
    assert r["rules"][0]["stop_pct"] == 100.0 and r["rules"][0]["label"] == "as is · 2× + VWAP"           # the defaults
    pt = r["per_trade"][0]
    assert set(pt) >= {"day", "bias", "actual", "by_rule"} and set(pt["by_rule"]) == {"asis", "x2", "x2_vwap", "x15"} and set(pt["by_rule"]["x2"]) == {"why", "net", "exit_t"}
    r90 = api.get("/api/options/asif?days=90").json()
    assert r90["trades"] == 2 and r90["skipped"] == 1 and r90["days"] == 90
    assert api.get("/api/options/asif?days=0").status_code == 422
    assert api.put("/api/options/settings", json={"vwap_exit": False}).json()["vwap_exit"] is False
    assert api.get("/api/options/asif").json()["rules"][0]["label"] == "as is · 2× only"
    assert api.get("/api/config/options").json()["vwap_exit"] is False
