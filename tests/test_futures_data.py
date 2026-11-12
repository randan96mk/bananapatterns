"""Stock futures data layer: NFO contract picking, continuous candle sync, roll jumps, the ban list, eligibility
reasons, sessions to expiry, config defaults, health rows and the API shapes."""

from datetime import date, datetime, timedelta

import pytest

from backend.config import IST, AppConfig, FuturesConfig
from backend.database.models import get_db, now_ist
from backend.futures import ban, candles, eligibility, instruments
from backend.ingest.holidays import is_trading_day

TODAY = date.fromisoformat(now_ist()[:10])


def _fut(name, ts, token, expiry, lot, segment="NFO-FUT", itype="FUT"):
    return {"instrument_token": token, "exchange_token": token, "tradingsymbol": ts, "name": name, "expiry": expiry,
            "lot_size": lot, "instrument_type": itype, "segment": segment, "exchange": "NFO", "tick_size": 0.05}


def _nfo(today=TODAY):
    e1, e2, e3 = today + timedelta(days=10), today + timedelta(days=40), today + timedelta(days=70)
    old = today - timedelta(days=5)
    return [
        _fut("RELIANCE", "RELIANCE26NOVFUT", 101, e1, 500), _fut("RELIANCE", "RELIANCE26DECFUT", 102, e2, 500),
        _fut("RELIANCE", "RELIANCE27JANFUT", 103, e3, 500), _fut("RELIANCE", "RELIANCE26OCTFUT", 100, old, 500),
        _fut("SAIL", "SAIL26DECFUT", 202, e2, 8000), _fut("SAIL", "SAIL26NOVFUT", 201, e1, 8000),
        _fut("ONLYONE", "ONLYONE26NOVFUT", 301, e1.isoformat(), 1000),                       # a string expiry, one month only
        _fut("NIFTY", "NIFTY26NOVFUT", 401, e1, 75), _fut("BANKNIFTY", "BANKNIFTY26NOVFUT", 402, e1, 30),
        _fut("NIFTYNXT50", "NIFTYNXT5026NOVFUT", 403, e1, 25),
        _fut("RELIANCE", "RELIANCE26NOV2800CE", 501, e1, 500, segment="NFO-OPT", itype="CE"),
    ]


class QuoteKite:
    """FakeKite plus NFO quotes (last_price, oi) and continuous history with OI."""

    def __init__(self, nfo, quotes=None, series=None, fail_quote=False):
        self._nfo, self.quotes, self.series, self.fail_quote = nfo, quotes or {}, series or {}, fail_quote
        self.calls = []

    def set_access_token(self, t):
        pass

    def instruments(self, exchange=None):
        self.calls.append(("instruments", exchange))
        return self._nfo

    def quote(self, keys):
        self.calls.append(("quote", len(keys)))
        if self.fail_quote:
            raise RuntimeError("quote down")
        return {k: self.quotes[k] for k in keys if k in self.quotes}

    def historical_data(self, token, from_s, to_s, interval, continuous=False, oi=False):
        self.calls.append(("historical_data", token, from_s[:10], to_s[:10], interval, continuous, oi))
        lo, hi = from_s[:10], to_s[:10]
        return [r for r in self.series.get(token, []) if lo <= r["date"].date().isoformat() <= hi]


QUOTES = {"NFO:RELIANCE26NOVFUT": {"last_price": 1500.0, "oi": 9_000_000}, "NFO:SAIL26NOVFUT": {"last_price": 120.0, "oi": 50_000_000},
          "NFO:ONLYONE26NOVFUT": {"last_price": 700.0, "oi": 1000}}


def fut_series(start: date, end: date, base=100.0, step=1.0, oi=5000):
    rows, d, i = [], start, 0
    while d <= end:
        if is_trading_day(d):
            c = base + step * i
            rows.append({"date": datetime(d.year, d.month, d.day, tzinfo=IST), "open": c - 0.5, "high": c + 1, "low": c - 1,
                         "close": c, "volume": 1000 + i, "oi": oi + i})
            i += 1
        d += timedelta(days=1)
    return rows


# --- instruments ---

def test_pick_contracts_near_next_index_exclusion_and_expired():
    p = instruments.pick_contracts(_nfo(), TODAY)
    assert set(p) == {"RELIANCE", "SAIL", "ONLYONE"}                              # indices and options out
    assert p["RELIANCE"]["near"]["tradingsymbol"] == "RELIANCE26NOVFUT" and p["RELIANCE"]["next"]["tradingsymbol"] == "RELIANCE26DECFUT"
    assert p["SAIL"]["near"]["token"] == 201 and p["SAIL"]["next"]["token"] == 202   # sorted by expiry, not dump order
    assert p["ONLYONE"]["next"] is None and p["ONLYONE"]["near"]["expiry"] == (TODAY + timedelta(days=10)).isoformat()


def test_refresh_stores_lot_value_oi_rank_and_history(make_client):
    client = make_client(QuoteKite(_nfo(), QUOTES))
    r = instruments.refresh(client, TODAY)
    assert r["contracts"] == 5 and r["underlyings"] == 3 and r["quoted"] == 3 and not r["skipped"]
    rel = instruments.contracts("RELIANCE")
    assert rel["near"]["lot_value"] == 1500.0 * 500 and rel["near"]["oi_rank"] == 2 and rel["next"]["lot_value"] is None
    assert instruments.near("sail")["oi_rank"] == 1 and instruments.near("NOPE") is None
    u = instruments.universe()
    assert [x["symbol"] for x in u] == ["SAIL", "RELIANCE", "ONLYONE"]
    assert [x["symbol"] for x in instruments.universe(lot_cap=700000)] == ["ONLYONE"]      # 7.0 L; SAIL 9.6 L, RELIANCE 7.5 L
    cal = instruments.expiry_calendar()
    assert [c["month"] for c in cal] == ["near", "next"] and cal[0]["contracts"] == 3
    assert instruments.refresh(client, TODAY)["skipped"] is True                            # once a day
    # the next day: a new as_of; the older rows stay as history and the latest wins
    nxt = TODAY + timedelta(days=1)
    client.kite.quotes["NFO:RELIANCE26NOVFUT"]["last_price"] = 1600.0
    instruments.refresh(client, nxt)
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(DISTINCT as_of) FROM futures_contracts").fetchone()[0] == 2
    assert instruments.near("RELIANCE")["lot_value"] == 800000.0 and instruments.latest_as_of() == nxt.isoformat()


def test_refresh_without_quotes_uses_stored_closes(make_client):
    with get_db() as conn:
        candles.upsert(conn, "RELIANCE", [{"date": "2026-11-10", "open": 1, "high": 1, "low": 1, "close": 1400.0, "volume": 1, "oi": 1}])
    client = make_client(QuoteKite(_nfo(), fail_quote=True))
    r = instruments.refresh(client, TODAY)
    assert r["quoted"] == 0
    assert instruments.near("RELIANCE")["lot_value"] == 700000.0 and instruments.near("RELIANCE")["oi_rank"] is None
    assert instruments.near("SAIL")["lot_value"] is None


def test_sessions_to_expiry_around_a_holiday():
    from backend.ingest import holidays
    hol = {"2026-01-26": "Republic Day"}                                           # Monday
    fri = date(2026, 1, 23)
    assert holidays.is_trading_day(date(2026, 1, 26), hol) is False
    # Fri -> Thu 29 Jan: Tue, Wed, Thu = 3 sessions (Mon is the holiday, the weekend never counts)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("backend.futures.instruments.is_trading_day", lambda d: holidays.is_trading_day(d, hol))
        assert instruments.sessions_to_expiry("2026-01-29", fri) == 3
        assert instruments.sessions_to_expiry(date(2026, 1, 29), date(2026, 1, 29)) == 0
        assert instruments.sessions_to_expiry("2026-01-23", "2026-01-27") == -1       # expired: Tue is one session after
        assert instruments.sessions_to_expiry("2026-01-30", "2026-01-23") == 4


# --- candles ---

def test_candle_sync_incremental_overlap_and_no_deletes(make_client):
    start = TODAY - timedelta(days=60)
    kite = QuoteKite(_nfo(), QUOTES, series={101: fut_series(start, TODAY - timedelta(days=10))})
    client = make_client(kite)
    instruments.refresh(client, TODAY)
    r = candles.sync(client, ["RELIANCE", "NOFUT"], years=3, end=TODAY)
    n1 = len(candles.get("RELIANCE"))
    assert r["synced"] == 1 and r["added"] == n1 > 20 and r["failed"] == [{"symbol": "NOFUT", "error": "no contract"}]
    assert r["to"] == candles.get("RELIANCE")[-1]["date"]
    first = kite.calls[-1]
    assert first[0] == "historical_data" and first[1] == 101 and first[5] is True and first[6] is True   # continuous + oi
    assert date.fromisoformat(first[2]) <= TODAY - timedelta(days=3 * 365)                              # years back the first time
    assert candles.get("RELIANCE", limit=3)[-1]["oi"] > 0
    # more bars arrive; the incremental call starts overlap_bars days before the last stored date, nothing is lost
    kite.series[101] = fut_series(start, TODAY)
    last = candles.get("RELIANCE")[-1]["date"]
    r2 = candles.sync(client, ["RELIANCE"], end=TODAY)
    call = kite.calls[-1]
    assert call[2] == (date.fromisoformat(last) - timedelta(days=candles.OVERLAP_BARS)).isoformat()
    n2 = len(candles.get("RELIANCE"))
    assert r2["added"] == n2 - n1 > 0 and candles.get("RELIANCE")[0]["date"] == candles.get("RELIANCE", start=None)[0]["date"]
    # Kite returning nothing changes nothing
    kite.series[101] = []
    r3 = candles.sync(client, ["RELIANCE"], end=TODAY)
    assert r3["added"] == 0 and len(candles.get("RELIANCE")) == n2
    assert candles.status() == {"underlyings": 1, "to": candles.get("RELIANCE")[-1]["date"], "bars": n2}
    # a per-symbol failure is recorded, the run continues
    kite.series[101] = fut_series(start, TODAY)
    def boom(*a, **k):
        raise ValueError("bad token")
    kite.historical_data = boom
    r4 = candles.sync(client, ["RELIANCE"], end=TODAY)
    assert r4["failed"][0]["symbol"] == "RELIANCE" and len(candles.get("RELIANCE")) == n2


def test_monthly_expiry_rule_and_roll_jump():
    assert candles.monthly_expiry(2025, 6) == date(2025, 6, 26)              # last Thursday
    assert candles.monthly_expiry(2025, 10) == date(2025, 10, 28)            # last Tuesday from Sep 2025
    assert candles.monthly_expiry(2026, 1).weekday() == 1
    # a series over Oct–Nov 2025 with a +2% gap on the day after the October expiry
    bars, d = [], date(2025, 10, 1)
    px = 100.0
    exp = candles.monthly_expiry(2025, 10).isoformat()
    bumped = False
    while d <= date(2025, 11, 20):
        if is_trading_day(d):
            o = px
            if d.isoformat() > exp and not bumped:
                o, bumped = px * 1.02, True
            c = o + 0.1
            bars.append({"date": d.isoformat(), "open": o, "high": c + 1, "low": o - 1, "close": c, "volume": 1, "oi": 1})
            px = c
        d += timedelta(days=1)
    with get_db() as conn:
        candles.upsert(conn, "X", bars)
    j = candles.roll_jump("X")
    assert len(j) == 1 and j[0]["expiry"] == exp and j[0]["date"] > exp and 1.9 < j[0]["jump_pct"] < 2.1
    # a stored contract expiry overrides the rule for its month
    with get_db() as conn:
        conn.execute("INSERT INTO futures_contracts VALUES ('X','near','XFUT',1,'2025-11-18',1,NULL,NULL,NULL,'2025-11-01')")
    assert candles.expiry_days("2025-10-01", "2025-11-30") == [exp, "2025-11-18"]
    assert candles.roll_jump("NONE") == []


# --- ban list ---

CSV = ("Securities in Ban For Trade Date 12-NOV-2026: 3\n"
       "1,RBLBANK\n"
       "2,SAIL\n"
       "3,MANAPPURAM\n")


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status = text, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _Client:
    def __init__(self, text, status=200):
        self.text, self.status = text, status

    def get(self, url):
        return _Resp(self.text, self.status)


def test_ban_parse_store_and_stale(monkeypatch):
    assert ban.parse(CSV) == ("2026-11-12", ["MANAPPURAM", "RBLBANK", "SAIL"])
    assert ban.parse("Securities in Ban For Trade Date 13-NOV-2026: 0\n") == ("2026-11-13", [])
    with pytest.raises(ValueError):
        ban.parse("")
    assert ban.summary("2026-11-12") == {"date": None, "symbols": [], "stale_days": None}
    monkeypatch.setattr(ban, "_fetch", lambda c: _Client(CSV).get(ban.BAN_URL).text)
    r = ban.refresh(client=_Client(CSV), today="2026-11-12")
    assert r["ok"] and r["date"] == "2026-11-12" and r["symbols"] == ["MANAPPURAM", "RBLBANK", "SAIL"]
    assert ban.banned("sail") and ban.banned("SAIL", "2026-11-12") and not ban.banned("TCS")
    assert ban.banned("SAIL", "2026-11-13") is True                                   # the latest list at or before the day
    assert ban.banned("SAIL", "2026-11-11") is True                                   # nothing older: the latest overall
    assert ban.stale_days("2026-11-12") == 0 and ban.stale_days("2026-11-15") == 3
    # a failed fetch keeps the last good list and says so
    def down(c):
        raise RuntimeError("HTTP 403")
    monkeypatch.setattr(ban, "_fetch", down)
    r = ban.refresh(client=_Client("", 403), today="2026-11-13")
    assert r["ok"] is False and r["date"] == "2026-11-12" and "403" in r["error"]
    assert ban.banned("SAIL") and ban.summary("2026-11-16")["stale_days"] == 4
    # an empty day is a valid day: the date moves on, nothing is banned
    monkeypatch.setattr(ban, "_fetch", lambda c: "Securities in Ban For Trade Date 13-NOV-2026: 0\n")
    r = ban.refresh(client=_Client(""), today="2026-11-13")
    assert r["ok"] and r["date"] == "2026-11-13" and r["symbols"] == []
    assert ban.latest_date() == "2026-11-13" and not ban.banned("SAIL") and ban.banned("SAIL", "2026-11-12")
    assert ban.symbols("2026-11-12") == ["MANAPPURAM", "RBLBANK", "SAIL"] and ban.symbols() == []


# --- eligibility ---

def _contract(lot=500, rank=5):
    return {"underlying": "RELIANCE", "month": "near", "tradingsymbol": "RELIANCE26NOVFUT", "token": 101,
            "expiry": (TODAY + timedelta(days=10)).isoformat(), "lot_size": lot, "lot_value": None, "oi": 1, "oi_rank": rank}


def test_eligibility_ok_and_every_reason():
    cfg = AppConfig()
    c = _contract()
    ok = eligibility.check(cfg, "RELIANCE", "long", 1000.0, 960.0, set(), set(), [], contract=c)
    assert ok["ok"] and ok["reasons"] == [] and ok["lot_size"] == 500 and ok["lot_value"] == 500000.0
    assert ok["risk"] == 20000.0 and ok["risk_pct"] == 4.0 and ok["margin_est"] == 100000.0 and ok["contract"] is c
    r = lambda **kw: eligibility.check(cfg, "RELIANCE", "long", kw.pop("entry", 1000.0), kw.pop("stop", 960.0),
                                       kw.pop("held_cash", set()), kw.pop("held_fut", set()), kw.pop("open_lots", []),
                                       contract=kw.pop("contract", c), **kw)["reasons"]
    assert eligibility.check(cfg, "X", "long", 1, 1, set(), set(), [], contract=None)["reasons"] == ["no future"]
    assert r(entry=1800.0, stop=1750.0) == ["lot too big ₹9.0 L > ₹6 L"]                 # 1800 x 500 = 9.0 L
    assert r(stop=936.0) == ["risk 6.4% > 6%"]                                           # 64 x 500 = 32,000 = 6.4%
    assert r(contract=_contract(rank=140)) == ["OI rank 140 > 100"]
    assert r(contract=_contract(rank=None)) == []                                         # no quotes: the rank is not checked
    ban.store(TODAY.isoformat(), ["RELIANCE"])
    assert r() == ["in ban"]
    ban.store(TODAY.isoformat(), [])
    with get_db() as conn:
        conn.execute("DELETE FROM fo_ban")
    assert r() == []
    from tests.test_events import results_in
    d = results_in("RELIANCE", 2)
    assert r() == [f"results {date.fromisoformat(d).strftime('%a')}"]
    cfg.futures.results_guard_sessions = 1
    assert r() == []
    assert r(held_cash={"reliance"}) == ["held in cash"]
    assert r(held_fut={"RELIANCE"}) == ["held in futures"]
    assert r(open_lots=[{"symbol": "RELIANCE", "lot_value": 400000, "margin": 80000, "risk": 1000}]) == ["held in futures"]
    big = {"symbol": "TCS", "lot_value": 460000, "margin": 92000, "risk": 10000}
    small = {"symbol": "SAIL", "lot_value": 250000, "margin": 50000, "risk": 10000}
    e3 = dict(entry=400.0, stop=390.0)                                                 # a 2.0 L lot, 1% risk
    assert r(open_lots=[big, small], **e3) == ["slots full"]                          # 2 open, one not small
    assert r(open_lots=[small, dict(small, symbol="HAL")], **e3) == []                # the small-lot third slot
    assert r(open_lots=[small, dict(small, symbol="HAL")], entry=900.0, stop=880.0) == ["slots full"]   # the third itself must be small (4.5 L)
    assert r(open_lots=[small, dict(small, symbol="HAL"), dict(small, symbol="X")], **e3) == ["slots full"]
    cfg.futures.max_pos = 5
    lots = [dict(small, symbol=f"S{i}", lot_value=400000, margin=80000, risk=5000) for i in range(4)]
    assert r(open_lots=lots) == ["notional ₹21.0 L > ₹12.5 L", "cash ₹0.8 L < ₹1.5 L", "margin 84% ≥ 80%"]
    cfg.futures.max_notional_x = 10
    assert r(open_lots=lots[:3]) == []                                                   # 1.6 L free, 68% margin
    assert r(open_lots=lots[:3], cash=200000.0) == ["cash ₹1.0 L < ₹1.5 L"]
    cfg.futures.cash_floor = 0
    hot = [dict(small, symbol=f"H{i}", lot_value=100000, margin=1000, risk=25000) for i in range(2)]
    assert r(open_lots=hot) == ["heat 14% > 12%"]                                        # 50,000 + 20,000 = 14%
    assert eligibility.lakh(450000, True) == "₹4.5 L" and eligibility.lakh(1000000, True) == "₹10 L" and eligibility.lakh(900000) == "₹9.0 L" and eligibility.pct(6.0) == "6%"


def test_eligibility_reads_the_stored_contract(make_client):
    instruments.refresh(make_client(QuoteKite(_nfo(), QUOTES)), TODAY)
    out = eligibility.check(FuturesConfig(), "SAIL", "short", 120.0, 125.0, set(), set(), [])
    assert out["lot_value"] == 960000.0 and out["reasons"] == ["lot too big ₹9.6 L > ₹6 L", "risk 8% > 6%"] and out["contract"]["token"] == 201


# --- config, health, API ---

def test_futures_config_defaults_and_section():
    from backend.config import PERSISTED_SECTIONS
    f = FuturesConfig()
    assert (f.capital, f.max_pos, f.small_lot_value, f.max_lot_value, f.max_risk_pct, f.max_stop_pct, f.top_oi) == \
        (500000, 2, 450000, 600000, 6.0, 5.0, 100)
    assert (f.margin_pct, f.max_notional_x, f.cash_floor, f.daily_loss_pct, f.heat_pct, f.roll_days) == (20.0, 2.5, 150000, 3.0, 12.0, 2)
    assert (f.long_paper, f.long_real, f.short_paper, f.short_real) == (True, False, True, False)
    assert f.universe_lot_cap == 600000 and f.history_years == 3 and f.results_guard_sessions == 3
    assert PERSISTED_SECTIONS["futures"] is FuturesConfig and AppConfig().section("futures").capital == 500000
    cfg = AppConfig()
    assert cfg.update_section("futures", {"max_pos": "3", "long_real": "true"})["max_pos"] == 3 and cfg.futures.long_real is True


def test_health_rows(make_client):
    from backend.engine import health
    keys = {c["key"]: c for c in health.checks(AppConfig(), has_token=False)}
    assert {"fut_contracts", "fut_candles", "fo_ban"} <= set(keys)
    assert keys["fut_contracts"]["state"] == "none yet" and keys["fo_ban"]["state"] == "none yet" and keys["fut_candles"]["ok"]
    old = TODAY - timedelta(days=9)
    instruments.refresh(make_client(QuoteKite(_nfo(old), QUOTES)), old)
    ban.store((TODAY - timedelta(days=5)).isoformat(), ["SAIL"])
    with get_db() as conn:
        candles.upsert(conn, "SAIL", [{"date": old.isoformat(), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "oi": 1}])
    keys = {c["key"]: c for c in health.checks(AppConfig(), has_token=False)}
    assert keys["fut_contracts"]["warn"] is True and f"as of {old}" in keys["fut_contracts"]["state"] and "9 d old" in keys["fut_contracts"]["state"]
    assert keys["fut_candles"]["state"] == f"to {old}"
    assert keys["fo_ban"]["warn"] is True and "1 in ban" in keys["fo_ban"]["state"] and "stale" in keys["fo_ban"]["state"]


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
    kite = QuoteKite(_nfo(), QUOTES, series={101: fut_series(TODAY - timedelta(days=30), TODAY), 301: fut_series(TODAY - timedelta(days=30), TODAY)})
    client = make_client(kite, history_years=1)
    client.config.access_token = ""
    monkeypatch.setattr(main, "KiteClient", lambda kc, ic: client)
    with TestClient(main.app) as tc:
        yield tc, client


def test_api_data_summary_and_refresh(api):
    tc, client = api
    d = tc.get("/api/futures/data").json()
    assert d["contracts"] == 0 and d["as_of"] is None and d["candles"] == {"underlyings": 0, "to": None, "bars": 0}
    assert d["ban"] == {"date": None, "symbols": [], "stale_days": None} and d["universe"] == []
    assert tc.post("/api/futures/data/refresh").status_code == 401                        # needs the Kite login
    client.set_access_token("tok")
    from backend.api import routes
    routes.S().token_source = "env"
    assert tc.post("/api/futures/data/refresh").json() == {"status": "started", "job": "futures_data"}
    import threading
    for t in threading.enumerate():
        if t.name == "futures-data":
            t.join(timeout=30)
    d = tc.get("/api/futures/data").json()
    assert d["contracts"] == 5 and d["as_of"] == TODAY.isoformat() and d["universe_lot_cap"] == 600000
    assert d["candles"]["underlyings"] == 0 or d["candles"]["to"]                           # universe empty at 6 L cap: SAIL 9.6, REL 7.5, ONLYONE 7.0
    assert d["universe"] == [] and d["ban"]["date"] is None                                 # the ban fetch is offline in tests
    # a wider cap brings ONLYONE in; its candles sync on the next run (forced: the contracts were already read today)
    tc.put("/api/config/futures", json={"universe_lot_cap": 700000})
    tc.post("/api/futures/data/refresh?force=true")
    for t in threading.enumerate():
        if t.name == "futures-data":
            t.join(timeout=30)
    d = tc.get("/api/futures/data").json()
    assert [u["symbol"] for u in d["universe"]] == ["ONLYONE"] and set(d["universe"][0]) == {"symbol", "lot_size", "lot_value", "expiry", "oi_rank"}
    assert d["candles"]["underlyings"] == 1 and d["candles"]["to"] and d["ban"]["stale_days"] is None
    s = tc.get("/api/config/futures").json()
    assert s["universe_lot_cap"] == 700000 and "long_real" in s


def test_evening_step_records_its_own_run(make_client):
    from backend.alerts.jobs import Scheduler
    from backend.api.routes import AppState
    cfg = AppConfig()
    client = make_client(QuoteKite(_nfo(), QUOTES, series={101: fut_series(TODAY - timedelta(days=30), TODAY)}))
    st = AppState(cfg, client)
    sch = Scheduler(st, notifier_factory=lambda a: None)
    out = sch.run_futures_data(TODAY)
    assert out["status"] == "waiting_login"
    client.set_access_token("tok")
    st.token_source = "env"
    cfg.futures.universe_lot_cap = 800000
    out = sch.run_futures_data(TODAY)
    assert out["status"] == "partial" and out["contracts"]["contracts"] == 5 and out["candles"]["synced"] == 2   # ban list offline
    assert out["errors"] and "ban list" in out["errors"][0]
    with get_db() as conn:
        row = conn.execute("SELECT * FROM job_runs WHERE job = 'futures_data' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "partial" and "contracts 5" in row["message"] and "ban" in row["message"]
