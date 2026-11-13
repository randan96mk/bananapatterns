"""🧩 The options book on a fake Kite world (NIFTY weekly contracts, Black-Scholes quotes off a spot path, OI per strike,
1-minute index / futures candles): a full paper day (range -> bias -> chain score -> entry -> breakeven -> target), a
premium-stop day, a VWAP-stop day, a no-trade day, the daily loss limit, the expiry-day 35% stop, the time exit, the
hard exit without quotes, the kill switch, one re-entry on a flipped bias only, hold-today, deposits, no login, the
Real switch refused, nothing held overnight."""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from backend.config import IST, AppConfig
from backend.database.models import get_db, upsert_candles
from backend.options import bias as bs
from backend.options import book, greeks
from backend.options import instruments as oi

DAY = date(2026, 11, 17)                      # a Tuesday; 2026-11-24 is a holiday, so the dump's expiry is the 25th
EXPIRY, NEXT = date(2026, 11, 25), date(2026, 12, 2)
PREV = {"date": "2026-11-16", "open": 24950.0, "high": 25080.0, "low": 24900.0, "close": 25000.0, "volume": 0}
FUT_TOKEN, LOT = 53000000, 75
BASIS = 40.0                                  # the fake future trades this much over the index


def minutes(start="09:15", end="15:40"):
    s, e = bs.hm(start), bs.hm(end)
    m = s.hour * 60 + s.minute
    while m <= e.hour * 60 + e.minute:
        yield f"{m // 60:02d}:{m % 60:02d}"
        m += 1


def at(hhmm: str, day: date = DAY) -> datetime:
    return datetime.combine(day, bs.hm(hhmm), tzinfo=IST).replace(second=5)


def path(points: dict):
    """A spot path from {'HH:MM': level, ...}: the level holds from its minute until the next point; before the first
    point the range alternates 25000 / 25100."""
    keys = sorted(points)

    def spot(t):
        lvl = None
        for k in keys:
            if t >= k:
                lvl = points[k]
        if lvl is None:
            return 25000.0 if int(t[3:5]) % 2 else 25100.0
        return float(lvl)
    return spot


def oi_default(strike, kind, t):
    """Puts: a wall at 24800, more OI than calls (PCR > 1.1), building after 9:30 near the money; calls: a wall at
    25300, flat."""
    if kind == "PE":
        base = 300000 if strike == 24800 else 100000
        return base + (20000 if t >= "09:31" and 24600 <= strike <= 25700 else 0)
    return 350000 if strike == 25300 else 80000


class World:
    """The fake Kite: instruments(), quote() (Black-Scholes off the spot path at the current minute), historical_minute()."""

    def __init__(self, spot, oi_fn=oi_default, iv=0.12, vix=14.0, expiry=EXPIRY, nxt=NEXT, day=DAY, has_token=True, basis=BASIS):
        self.spot, self.oi_fn, self.iv, self.vix, self.expiry, self.nxt, self.day = spot, oi_fn, iv, vix, expiry, nxt, day
        self.basis = basis
        self.has_token = has_token
        self.now = "09:15"
        self.calls = {"quote": 0, "minute": 0, "instruments": 0}
        self.by_symbol = {}
        for e in (expiry, nxt):
            for k in range(23000, 27050, 50):
                for kind in ("CE", "PE"):
                    sym = f"NIFTY{e:%y%b%d}{k}{kind}".upper()
                    self.by_symbol[sym] = {"expiry": e, "strike": float(k), "type": kind}

    def instruments(self, exchange):
        self.calls["instruments"] += 1
        rows = [{"instrument_token": 1000 + i, "tradingsymbol": s, "name": "NIFTY", "expiry": v["expiry"], "strike": v["strike"],
                 "instrument_type": v["type"], "segment": "NFO-OPT", "lot_size": LOT} for i, (s, v) in enumerate(sorted(self.by_symbol.items()))]
        rows.append({"instrument_token": FUT_TOKEN, "tradingsymbol": "NIFTY26NOVFUT", "name": "NIFTY", "expiry": self.expiry, "strike": 0,
                     "instrument_type": "FUT", "segment": "NFO-FUT", "lot_size": LOT})
        rows.append({"instrument_token": FUT_TOKEN + 1, "tradingsymbol": "NIFTY26DECFUT", "name": "NIFTY", "expiry": self.nxt, "strike": 0,
                     "instrument_type": "FUT", "segment": "NFO-FUT", "lot_size": LOT})
        rows.append({"instrument_token": 7, "tradingsymbol": "BANKNIFTY26NOV55000CE", "name": "BANKNIFTY", "expiry": self.expiry, "strike": 55000,
                     "instrument_type": "CE", "segment": "NFO-OPT", "lot_size": 30})
        rows.append({"instrument_token": 8, "tradingsymbol": "NIFTY25OCT25000CE", "name": "NIFTY", "expiry": self.day - timedelta(days=30), "strike": 25000,
                     "instrument_type": "CE", "segment": "NFO-OPT", "lot_size": LOT})
        return rows

    def T(self, t):
        return greeks.time_to_expiry(oi.sessions_to_expiry(self.expiry, self.day), bs.hm(t))

    def premium(self, strike, kind, t=None):
        t = t or self.now
        return round(greeks.price(self.spot(t), strike, self.T(t), self.iv, kind), 2)

    def quote(self, keys):
        self.calls["quote"] += 1
        out = {}
        for k in keys:
            if k == "NSE:NIFTY 50":
                out[k] = {"last_price": self.spot(self.now)}
            elif k == "NSE:INDIA VIX":
                out[k] = {"last_price": self.vix}
            else:
                v = self.by_symbol.get(k.split(":", 1)[1])
                if not v or v["expiry"] != self.expiry:
                    continue
                px = self.premium(v["strike"], v["type"])
                out[k] = {"last_price": max(0.05, px - 0.25), "oi": self.oi_fn(v["strike"], v["type"], self.now),
                          "depth": {"buy": [{"price": max(0.05, px - 0.5)}], "sell": [{"price": px + 0.5}]}}
        return out

    def historical_minute(self, token, start, end):
        """Complete 1-minute bars from 9:15 to the minute before now (index: the spot; future: spot + BASIS, volume)."""
        self.calls["minute"] += 1
        rows = []
        for t in minutes("09:15", "15:29"):
            if t >= self.now:
                break
            px = self.spot(t) + (self.basis if token == FUT_TOKEN else 0.0)
            rows.append({"date": datetime.combine(self.day, bs.hm(t), tzinfo=IST), "open": px, "high": px, "low": px, "close": px,
                         "volume": 1000 if token == FUT_TOKEN else 0})
        return rows


def cfg_for(**opts) -> AppConfig:
    c = AppConfig()
    c.options = replace(c.options, **opts)
    return c


def seed_prev_day():
    with get_db() as conn:
        upsert_candles(conn, oi.INDEX_TOKEN, [PREV])


def run_day(cfg, w, start="09:15", end="15:40", until=None, day=DAY):
    """Tick every minute; `until(st)` stops the day early. Returns the state."""
    st = None
    for t in minutes(start, end):
        w.now = t
        book.tick(cfg, w, at(t, day))
        st = book._state()
        if until and until(st):
            break
    return st


def tl(st):
    return [x["text"] for x in st["day"]["timeline"]]


@pytest.fixture
def prev():
    seed_prev_day()


BULL = {"09:30": 25050, "09:45": 25150, "09:55": 25250, "10:05": 25400, "10:30": 25450}


# --- a full paper day: range -> bias -> chain -> entry -> breakeven -> target ------------------------------------------

def test_full_paper_day_target(prev):
    cfg = cfg_for()
    w = World(path(BULL))
    st = run_day(cfg, w)
    d = st["day"]
    assert d["range"] == {"high": 25100.0, "low": 25000.0, "bars": 15} and d["prev"]["high"] == 25080.0 and d["gap_pct"] == 0.0
    assert d["expiry"] == EXPIRY.isoformat() and d["sessions"] == 5 and d["lot_size"] == LOT
    assert d["bias"] == "BULLISH" and d["bias_at"] == "9:50 AM" and d["checks"][0]["t"] == "09:45" and d["checks"][0]["bias"] is None
    assert d["score"] == 3 and "→ 3/3" in d["chain_line"] and "support 24,800" in d["chain_line"] and "PCR" in d["chain_line"]
    steps = d["steps"]
    assert [s["name"] for s in steps] == ["Opening range", "VWAP (futures)", "Previous day", "Option chain"] and all(s["ok"] for s in steps)
    assert "25,150" in steps[0]["text"] and "VWAP" in steps[1]["text"] and "25,080" in steps[2]["text"] and steps[3]["text"] == d["chain_line"]
    trades = book.trades()
    assert len(trades) == 1 and d["trades"] == 1 and st["open"] is None
    t = trades[0]
    assert t["bias"] == "BULLISH" and t["sold"]["type"] == "PE" and t["sold"]["strike"] == 24800.0 and t["hedge"]["strike"] == 24550.0   # the wall rule
    assert t["lots"] == 2 and t["qty"] == 150 and t["sold"]["entry"] == pytest.approx(w.premium(24800, "PE", "09:50") - 1, abs=0.01)
    assert t["hedge"]["entry"] == pytest.approx(w.premium(24550, "PE", "09:50") + 1, abs=0.01) and t["credit"] == pytest.approx(t["sold"]["entry"] - t["hedge"]["entry"], abs=0.01)
    assert t["why"] == "target" and t["net"] > 0 and t["be_active"] is True and t["charges"] > 80 and t["in"] == "9:50 AM"
    assert any("stop now breakeven" in x for x in tl(st)) and any(x.startswith("target") for x in tl(st))
    assert d["status"] == "done" and d["why"] == "target" and "summary" in d["done"] and "close_snap" in d["done"]
    row = book.days()[0]
    assert row["day"] == DAY.isoformat() and row["bias"] == "BULLISH" and row["score"] == 3 and row["strikes"] == "24,800 PE / 24,550 PE"
    assert row["result"] == "target" and row["pnl"] == t["net"] and row["lots"] == 2 and row["trades"] == 1
    with get_db() as conn:
        assert conn.execute("SELECT equity FROM options_equity WHERE day = ?", (DAY.isoformat(),)).fetchone()[0] == pytest.approx(300000 + t["net"], abs=0.01)
        kinds = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM options_chain_snap GROUP BY kind")}
    assert kinds["open"] == 50 and kinds["close"] == 50 and kinds["bias"] >= 50 and kinds["live"] > 0
    assert st["cash"] == pytest.approx(300000 + t["net"], abs=0.01)
    kinds_alerts = [a["kind"] for a in st["alerts"]]
    assert kinds_alerts == ["entry", "exit", "summary"] and all(a["tag"] == "paper" for a in st["alerts"])


def test_premium_stop_day(prev):
    cfg = cfg_for()
    w = World(path({"09:30": 25050, "09:45": 25150, "09:51": 24900}))
    st = run_day(cfg, w, until=lambda s: s["day"]["closed"])
    t = book.trades()[0]
    assert t["why"] == "premium stop" and t["net"] < 0 and t["out"] == "9:51 AM" and st["day"]["realised"] == t["net"]
    assert t["sold"]["exit"] >= t["sold"]["entry"] * 1.5 - 1 and st["open"] is None


def test_vwap_stop_day(prev):
    cfg = cfg_for()
    w = World(path({"09:30": 25050, "09:45": 25150, "09:51": 25080}))
    st = run_day(cfg, w, until=lambda s: s["day"]["closed"])
    t = book.trades()[0]
    assert t["why"] == "vwap stop" and t["label"] == "VWAP stop" and t["out"] == "9:55 AM"
    assert t["sold"]["exit"] < t["sold"]["entry"] * 1.5 and st["day"]["vwap"] > 25080


def test_no_trade_day(prev):
    cfg = cfg_for()
    w = World(path({"09:30": 25050}))
    st = run_day(cfg, w)
    d = st["day"]
    assert d["bias"] is None and d["status"] == "no_trade" and d["why"] == "no break by 11:00 AM" and book.trades() == []
    assert any(x.startswith("no trade today") for x in tl(st)) and [a["kind"] for a in st["alerts"]] == ["no_trade", "summary"]
    assert len(d["checked"]) == 16 and d["checked"][0] == "09:45" and d["checked"][-1] == "11:00"
    row = book.days()[0]
    assert row["status"] == "no_trade" and row["trades"] == 0 and row["pnl"] == 0.0 and row["result"] == "no break by 11:00 AM"


def test_breakeven_stop(prev):
    """The premium decays 40%, the stop moves to breakeven, then the spread's value comes back to the credit."""
    cfg = cfg_for()
    w = World(path({"09:30": 25050, "09:45": 25150, "09:55": 25300, "10:05": 25080}))
    run_day(cfg, w, until=lambda s: s["day"]["closed"])
    t = book.trades()[0]
    assert t["be_active"] and t["why"] == "breakeven stop" and t["out"] == "10:05 AM" and abs(t["net"]) < 0.6 * t["credit_rs"]


def test_time_exit_flat_day(prev):
    cfg = cfg_for()
    w = World(path({"09:30": 25050, "09:45": 25150}), basis=0.0)       # the futures VWAP stays under the flat index
    st = run_day(cfg, w)
    t = book.trades()[0]
    assert t["why"] == "time exit" and t["out"] == "3:10 PM" and st["open"] is None and "time_exit" in st["day"]["done"]
    assert book.days()[0]["result"] == "time exit"


def test_hard_exit_without_quotes_nothing_held_overnight(prev):
    cfg = cfg_for()
    w = World(path({"09:30": 25050, "09:45": 25150}), basis=0.0)
    run_day(cfg, w, end="15:05")
    w.has_token = False                                           # the login died before the time exit
    st = run_day(cfg, w, start="15:06", end="15:40")
    t = book.trades()[0]
    assert t["why"] == "hard exit" and t["est"] is True and t["out"] == "3:20 PM" and st["open"] is None
    assert any("retrying" in x for x in tl(st)) and any("last known mid" in x for x in tl(st))
    assert "summary" in st["day"]["done"]
    # the next day starts clean: nothing carried over
    nxt = DAY + timedelta(days=1)
    w.has_token, w.day = True, nxt
    w.now = "09:15"
    book.tick(cfg, w, at("09:15", nxt))
    st = book._state()
    assert st["day"]["date"] == nxt.isoformat() and st["open"] is None and st["day"]["trades"] == 0 and st["last_day"]["day"] == DAY.isoformat()


def test_kill_switch_closes_at_once(prev):
    cfg = cfg_for()
    w = World(path({"09:30": 25050, "09:45": 25150}))
    st = run_day(cfg, w, until=lambda s: s["open"])
    assert st["open"]
    res = book.set_killed(True, cfg, w, at("10:00"))
    assert res["killed"] and res["closed"]["why"] == "kill" and book._state()["open"] is None and book.killed()
    w.now = "10:05"
    book.tick(cfg, w, at("10:05"))
    assert book._state()["day"]["trades"] == 1                    # no new entry while killed
    book.set_killed(False, cfg)
    assert not book.killed()
    assert book.summary(cfg)["guards"]["kill"] is False


def test_daily_loss_limit_blocks_the_reentry(prev):
    cfg = cfg_for(daily_loss_pct=0.5)                             # Rs 1,500: the first stop uses it up
    w = World(path({"09:30": 25050, "09:45": 25150, "09:51": 24900, "10:00": 24850}))
    st = run_day(cfg, w, end="11:05")
    d = st["day"]
    assert d["trades"] == 1 and book.trades()[0]["why"] == "premium stop"
    assert any("BEARISH · no entry · daily loss limit" in x for x in tl(st)) and d["bias"] == "BEARISH"
    g = book.summary(cfg, at("11:05"))["guards"]["daily_loss"]
    assert g["hit"] and g["limit"] == 1500.0 and g["used"] >= 1500


def test_reentry_once_on_a_flipped_bias(prev):
    cfg = cfg_for()
    w = World(path({"09:30": 25050, "09:45": 25150, "09:51": 25020, "10:00": 24850, "10:11": 25150, "10:20": 25400}))
    st = run_day(cfg, w, end="11:05")
    d = st["day"]
    ts = sorted(book.trades(), key=lambda t: t["n"])
    assert d["trades"] == 2 and d["biases"] == ["BULLISH", "BEARISH"] and [t["bias"] for t in ts] == ["BULLISH", "BEARISH"]
    assert ts[1]["sold"]["type"] == "CE" and ts[1]["n"] == 2 and ts[1]["why"] == "premium stop" and ts[1]["lots"] == 1 and ts[1]["score"] == 1
    assert any("re-entry · sold" in x for x in tl(st)) and d["status"] == "done"
    assert d["checked"][-1] <= "10:15" and st["day"]["bias"] == "BEARISH"          # two trades done: the bias is not looked for again


def test_same_bias_cannot_reenter(prev):
    cfg = cfg_for()
    w = World(path({"09:30": 25050, "09:45": 25150, "09:51": 24900, "10:00": 25200}))
    st = run_day(cfg, w, end="10:30")
    assert st["day"]["trades"] == 1 and any("re-entry only on a flipped bias" in x for x in tl(st))


def test_expiry_day_uses_the_35_percent_stop(prev):
    cfg = cfg_for()
    w = World(path({"09:30": 25050, "09:45": 25150, "09:51": 25040}), expiry=DAY, iv=0.18)
    st = run_day(cfg, w, until=lambda s: s["open"])
    o = st["open"]
    assert st["day"]["sessions"] == 0 and o["expiry_day"] and o["stop_pct"] == 35.0 and o["stop_premium"] == pytest.approx(o["sold"]["entry"] * 1.35, abs=0.01)
    assert o["sold"]["entry"] >= 19.0 and o["sold"]["strike"] > 24800                     # the Rs 20 floor pulled the strike nearer
    st = run_day(cfg, w, start="09:51", until=lambda s: s["day"]["closed"])
    t = book.trades()[0]
    assert t["why"] == "premium stop" and t["sold"]["exit"] >= t["sold"]["entry"] * 1.35 - 1
    assert book.summary(cfg, at("10:00"))["margin"]["expiry_day"] is True


def test_chain_disagrees_no_trade(prev):
    def oi_bear(strike, kind, t):                                  # calls heavy: PCR low, calls building, the put wall right under spot
        if kind == "PE":
            return 120000 if strike == 25100 else 50000
        return (400000 if strike == 25300 else 200000) + (20000 if t >= "09:31" else 0)
    cfg = cfg_for()
    w = World(path({"09:30": 25050, "09:45": 25150}), oi_fn=oi_bear)
    st = run_day(cfg, w, end="10:00")
    d = st["day"]
    assert d["bias"] == "BULLISH" and d["score"] == 0 and d["trades"] == 0 and book.trades() == []
    assert any("chain disagrees" in x for x in tl(st)) and [a["kind"] for a in st["alerts"]] == ["chain_disagrees"]


def test_score_one_halves_to_zero_lots_and_vix_halves(prev):
    def oi_pcr_only(strike, kind, t):                              # PCR 1.3 only: the put wall right under spot, OI flat
        return (200000 if strike == 25100 else 130000) if kind == "PE" else (200000 if strike == 25300 else 100000)
    cfg = cfg_for()
    w = World(path({"09:30": 25050, "09:45": 25150}), oi_fn=oi_pcr_only, vix=24.0)
    st = run_day(cfg, w, end="10:00")
    d = st["day"]
    assert d["score"] == 1 and d["trades"] == 0 and "VIX 24.0 > 20 · half size" in d["size_note"] and "chain score 1 · half size" in d["size_note"]
    assert any(a["kind"] == "vix_half" for a in st["alerts"]) and any(a["kind"] == "no_trade_size" for a in st["alerts"])


def test_event_day_gap_and_hold_block_entries(prev):
    cfg = cfg_for(event_days=f"{DAY.isoformat()}, 2026-12-05")
    w = World(path({"09:30": 25050, "09:45": 25150}))
    st = run_day(cfg, w, end="10:00")
    assert st["day"]["trades"] == 0 and any("no entry · event day" in x for x in tl(st)) and [a["kind"] for a in st["alerts"]] == ["event_day"]
    book.reset()
    cfg = cfg_for(max_gap_pct=0.15)                                # the open (25000) gapped 0.2% over a 24950 close
    with get_db() as conn:
        upsert_candles(conn, oi.INDEX_TOKEN, [{**PREV, "close": 24950.0}])
    st = run_day(cfg, World(path({"09:30": 25050, "09:45": 25150})), end="10:00")
    assert st["day"]["trades"] == 0 and any("gap +0.20% > 0.15%" in x for x in tl(st))
    book.reset()
    cfg = cfg_for()
    book.hold_today(cfg, at("09:16"))
    st = run_day(cfg, World(path({"09:30": 25050, "09:45": 25150})), end="10:00")
    assert st["day"]["hold"] and st["day"]["trades"] == 0 and any("no entry · hold today" in x for x in tl(st))


def test_switches_and_real_refused(prev):
    cfg = cfg_for()
    with pytest.raises(ValueError, match="not wired"):
        book.set_switches(cfg, real=True)
    sw = book.set_switches(cfg, enabled=False)
    assert sw == {"paper": True, "real": False, "enabled": False, "killed": False, "real_allowed": False, "real_note": "no webhook yet · stays off"}
    st = run_day(cfg, World(path({"09:30": 25050, "09:45": 25150})), end="10:00")
    assert st["day"]["trades"] == 0 and any("switched off" in x for x in tl(st))
    assert book.set_switches(cfg, enabled=True, paper=False)["paper"] is False and cfg.options.real is False


def test_no_login_records_no_data_once(prev):
    cfg = cfg_for()
    w = World(path({"09:30": 25050, "09:45": 25150}), has_token=False)
    st = run_day(cfg, w)
    d = st["day"]
    assert d["no_data"] and d["status"] == "no_data" and d["why"] == "no data · Kite login missing" and d["trades"] == 0
    assert [a["kind"] for a in st["alerts"]] == ["no_data", "summary"] and sum(x.startswith("no data") for x in tl(st)) == 1
    assert book.days()[0]["no_data"] is True and w.calls["quote"] == 0


def test_close_now_capital_and_summary_shape(prev):
    cfg = cfg_for()
    with pytest.raises(ValueError):
        book.close_now(cfg, None, at("09:16"))
    w = World(path({"09:30": 25050, "09:45": 25150}))
    st = run_day(cfg, w, until=lambda s: s["open"])
    s = book.summary(cfg, at("09:50"))
    assert {"started", "capital", "cash", "equity", "net", "return", "mtm_today", "switches", "settings", "today", "guards", "margin", "cards",
            "open", "bias", "chain", "rules", "days", "trades", "alerts", "deposits", "curve", "contracts"} <= set(s)
    assert s["started"] and s["capital"] == 300000.0 and len(s["cards"]) == 6 and [c["key"] for c in s["cards"]][:3] == ["capital", "equity", "today"]
    o = s["open"]
    assert o["bias"] == "BULLISH" and len(o["legs"]) == 2 and o["legs"][0]["side"] == "SELL" and o["legs"][1]["side"] == "BUY" and "time exit 3:10 PM" in o["footer"]
    assert o["margin_est"] == 104000.0 and o["worst_case"] == pytest.approx((250 - o["credit"]) * 150, abs=0.01) and o["lots"] == 2
    assert s["margin"]["sessions_to_expiry"] == 5 and s["margin"]["lot_size"] == LOT and s["margin"]["expiry"] == EXPIRY.isoformat()
    assert s["bias"]["state"] == "BULLISH" and len(s["bias"]["steps"]) == 4 and s["bias"]["steps"][3]["text"] == s["chain"]["line"]
    assert s["chain"]["walls"] == {"support": 24800.0, "resistance": 25300.0} and s["chain"]["pcr"] > 1.1 and s["chain"]["score"] == 3
    assert len(s["chain"]["strikes"]) == 25 and s["chain"]["strikes"][0]["pe"]["oi"] and s["chain"]["iv"]["put"] == pytest.approx(12.0, abs=0.3)
    assert s["today"]["status"] == "open" and s["today"]["timeline"] and s["today"]["schedule"][0]["t"] == "9:30 AM" and s["guards"]["window"]["open"]
    assert s["switches"]["real_note"] == "no webhook yet · stays off" and len(s["rules"]) == 8
    r = book.set_capital(cfg, 350000)
    assert r["delta"] == 50000.0 and book._state()["cash"] == pytest.approx(st["cash"] + 50000, abs=0.01) and cfg.options.capital == 350000.0
    res = book.close_now(cfg, w, at("09:52"))
    assert res["trade"]["why"] == "closed by you" and book._state()["open"] is None
    s2 = book.summary(cfg, at("09:53"))
    assert s2["open"] is None and s2["today"]["status"] == "closed" and s2["realised_today"] == res["trade"]["net"] and s2["deposits"][0]["amount"] == 50000.0
    assert s2["equity"] == pytest.approx(350000 + res["trade"]["net"], abs=0.01) and s2["trades"][0]["label"] == "closed by you"


def test_steps_and_due_are_pure():
    cfg = cfg_for()
    st = book._new(cfg, DAY.isoformat())
    st["day"] = book._new_day(st, DAY.isoformat())
    assert book.steps(at("09:16"), cfg, st) == ["contracts"]
    st["day"]["done"] = ["contracts"]
    assert book.steps(at("09:30"), cfg, st) == ["range", "bias"] or book.steps(at("09:30"), cfg, st) == ["range"]
    st["day"]["done"].append("range")
    assert book.steps(at("09:44"), cfg, st) == [] and book.steps(at("09:45"), cfg, st) == ["bias"] and book.steps(at("11:02"), cfg, st) == ["bias", "no_trade"]
    st["day"]["checked"] = ["11:00"]
    assert book.steps(at("11:02"), cfg, st) == ["no_trade"]
    st["day"]["status"] = "open"
    st["open"] = {"n": 1}
    assert book.steps(at("12:00"), cfg, st) == ["mtm"] and book.steps(at("15:10"), cfg, st) == ["mtm", "time_exit"]
    assert book.steps(at("15:21"), cfg, st) == ["mtm", "time_exit", "hard_exit"] and book.steps(at("15:21"), cfg, st, is_killed=True)[:1] == ["kill_exit"]
    assert book.steps(at("15:35"), cfg, st) == ["mtm", "time_exit", "hard_exit", "close_snap"]      # no summary while a spread is open
    st["open"] = None
    assert book.steps(at("15:36"), cfg, st) == ["close_snap", "summary"]
    assert book.due(at("09:15"), cfg, set(), True, None) == []                              # no book yet
    book.start(cfg)
    assert book.due(at("09:15"), cfg, set(), True, None) == ["options_tick"] and book.due(at("09:15"), cfg, set(), False, None) == []
    assert book.due(at("09:15"), cfg, set(), True, at("09:15")) == [] and book.due(at("09:16"), cfg, set(), True, at("09:15")) == ["options_tick"]
    assert book.due(at("15:41"), cfg, set(), True, None) == [] and book.due(at("09:14"), cfg, set(), True, None) == []
    cfg.options.paper = False
    assert book.due(at("10:00"), cfg, set(), True, None) == []
    st = book._state()
    st["open"] = {"n": 1}
    book._save(st)
    assert book.due(at("10:00"), cfg, set(), True, None) == ["options_tick"]              # paper off: an open spread still exits


def test_lots_and_charges():
    c = cfg_for().options
    assert book.lots_for(c, 300000, 14.0, 3, False) == (2, [])
    assert book.lots_for(c, 300000, 24.0, 3, False)[0] == 1 and book.lots_for(c, 150000, 24.0, 3, False)[0] == 1
    assert book.lots_for(c, 300000, 14.0, 1, False)[0] == 1 and book.lots_for(c, 150000, 14.0, 1, False)[0] == 0
    assert book.lots_for(c, 300000, 14.0, 2, True) == (1, ["PCR crowded · half size"])
    assert book.lots_for(c, 450000, 14.0, 3, False)[0] == 2                                      # max_lots
    sell = book.charges(100 * 75, sell=True)
    buy = book.charges(100 * 75, sell=False)
    assert sell == pytest.approx(20 + 7.5 + 3.7125 + 0.0075 + 0.18 * 23.7125, abs=0.01) and buy == pytest.approx(20 + 3.7125 + 0.0075 + 0.18 * 23.7125 + 0.225, abs=0.01)
    assert sell > buy


def test_nothing_deleted_and_days_rewritten_not_dropped(prev):
    cfg = cfg_for()
    run_day(cfg, World(path(BULL)))
    with get_db() as conn:
        n_snap = conn.execute("SELECT COUNT(*) FROM options_chain_snap").fetchone()[0]
        n_days = conn.execute("SELECT COUNT(*) FROM options_days").fetchone()[0]
        row = json.loads(conn.execute("SELECT payload FROM options_days").fetchone()[0])
    assert n_snap > 100 and n_days == 1 and row["result"] == "target"
    nxt = DAY + timedelta(days=1)
    w = World(path({"09:30": 25050}), day=nxt)
    run_day(cfg, w, day=nxt)
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM options_chain_snap").fetchone()[0] > n_snap
        assert conn.execute("SELECT COUNT(*) FROM options_days").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM options_trades").fetchone()[0] == 1
    assert book.days()[0]["day"] == nxt.isoformat() and book.days()[1]["result"] == "target"
    assert book.summary(cfg, at("16:00", nxt))["curve"]["dates"] == [DAY.isoformat(), nxt.isoformat()]
