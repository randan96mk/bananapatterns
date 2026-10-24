"""Position guard: holdings sync (read-only), setup stop matching, sell rules, summary, stop edits."""

from datetime import date, timedelta

from backend.alerts import guard
from backend.alerts.dispatch import Notifier
from backend.config import AlertsConfig, VcpConfig
from backend.database.models import get_db, upsert_candles
from tests.test_alerts import FakeTG, scanned_db


class Kite:
    def __init__(self, holdings, net=()):
        self._h, self._n = holdings, list(net)

    def holdings(self):
        return self._h

    def positions(self):
        return {"net": self._n, "day": []}


def test_kite_holdings_merges_cnc_buys():
    k = Kite([{"tradingsymbol": "AAA", "quantity": 10, "t1_quantity": 5, "average_price": 100},
              {"tradingsymbol": "ZERO", "quantity": 0, "t1_quantity": 0, "average_price": 1},
              {"tradingsymbol": "HFCL-BE", "quantity": 3, "average_price": 80}],
             [{"tradingsymbol": "BBB", "product": "CNC", "quantity": 7, "average_price": 50},
              {"tradingsymbol": "CCC", "product": "MIS", "quantity": 4, "average_price": 9}])
    assert guard.kite_holdings(k) == {"AAA": {"qty": 15, "avg": 100.0}, "HFCL": {"qty": 3, "avg": 80.0},
                                      "BBB": {"qty": 7, "avg": 50.0}}


def test_sync_matches_setup_stop_and_drops_sold():
    as_of, tokens = scanned_db()
    r = guard.sync({"NEAR": {"qty": 10, "avg": 150.0}, "NOSETUP": {"qty": 5, "avg": 200.0}}, VcpConfig())
    assert sorted(r["added"]) == ["NEAR", "NOSETUP"]
    pos = {p["symbol"]: p for p in guard.positions()}
    assert pos["NEAR"]["pivot"] and pos["NEAR"]["stop"] == round(pos["NEAR"]["pivot"] * 0.92, 2)
    assert pos["NOSETUP"]["stop"] == 184.0 and pos["NOSETUP"]["stop_source"].startswith("average")
    r = guard.sync({"NEAR": {"qty": 20, "avg": 151.0}}, VcpConfig())
    assert r == {"added": [], "closed": ["NOSETUP"]}
    assert guard.positions()[0]["qty"] == 20 and guard.set_stop("NEAR", 140.0)
    assert guard.positions()[0]["stop"] == 140.0 and guard.positions()[0]["stop_source"] == "manual"
    assert not guard.set_stop("NOPE", 1.0)


def series(closes, start=date(2026, 6, 1)):
    out, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({"date": d.isoformat(), "open": c, "high": c, "low": c, "close": c, "volume": 1000})
        d += timedelta(days=1)
    return out


def pos(**kw):
    return dict({"symbol": "X", "qty": 10, "avg_price": 100.0, "stop": 92.0, "pivot": 100.0,
                 "first_seen": "2026-01-01", "flags": {}}, **kw)


def test_rules():
    cfg = AlertsConfig()
    flat = series([100.0] * 60)
    assert not any(guard.evaluate(pos(), flat, cfg)["fire"].values())
    ev = guard.evaluate(pos(), series([100.0] * 59 + [90.0]), cfg)
    assert ev["fire"]["stop"] and ev["fire"]["ma50"] and ev["r"] == -1.25
    ev = guard.evaluate(pos(), series([100.0] * 59 + [126.0]), cfg)
    assert ev["fire"]["profit"] and ev["fire"]["extended"] and ev["gain_pct"] == 26.0
    recent = series([100.0] * 60)
    ev = guard.evaluate(pos(first_seen=recent[-3]["date"]), series([100.0] * 59 + [99.0]), cfg)
    assert ev["fire"]["failed"] and ev["days"] == 2
    ev = guard.evaluate(pos(first_seen=recent[-10]["date"]), series([100.0] * 59 + [99.0]), cfg)
    assert not ev["fire"]["failed"]


def test_run_alerts_once_and_stop_daily():
    with get_db() as conn:
        upsert_candles(conn, 7, series([100.0] * 59 + [126.0]))
    guard.sync({"X": {"qty": 10, "avg": 100.0}}, VcpConfig())
    fake = FakeTG()
    n = Notifier(AlertsConfig(chat_id="42"), fake)
    r = guard.run("2026-08-21", {"X": 7}, AlertsConfig(), n)
    texts = [h for _, h in fake.sent]
    assert r == {"positions": 1, "sent": 3}
    assert any("Profit zone" in t for t in texts) and any("extended" in t for t in texts)
    assert "Your positions" in texts[-1] and "+26.0%" in texts[-1]
    guard.run("2026-08-22", {"X": 7}, AlertsConfig(), n)                   # profit / extended: once per position
    assert sum("Profit zone" in h for _, h in fake.sent) == 1
    guard.set_stop("X", 130.0)
    guard.run("2026-08-23", {"X": 7}, AlertsConfig(), n)
    guard.run("2026-08-24", {"X": 7}, AlertsConfig(), n)
    assert sum("below your stop" in h for _, h in fake.sent) == 2           # the stop repeats each day


def test_positions_api(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    guard.sync({"X": {"qty": 1, "avg": 10.0}}, VcpConfig())
    from backend import main
    with TestClient(main.app) as tc:
        assert tc.get("/api/positions").json()["positions"][0]["symbol"] == "X"
        assert tc.put("/api/positions/X", json={"stop": 9.5}).json() == {"symbol": "X", "stop": 9.5}
        assert tc.put("/api/positions/X", json={"stop": -1}).status_code == 422
        assert tc.put("/api/positions/NOPE", json={"stop": 1}).status_code == 404
