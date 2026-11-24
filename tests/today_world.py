"""✨ A seeded world for the Today panel's three phases (tests/test_today_phases.py; also the scratch screenshots).

Wed 25 Nov 2026 is the last session (Tue 24 is a holiday), Thu 26 the next one. Real holds DEF (stop 805), GHI (stop
2,120) and MNO (target 1,900); the evening plan for Thu: BUY ABC, BUY PQR, EXIT GHI, BUY JKL (skipped). Everything goes
in through the tables the app writes; no model, quote or broker call."""

import json
from datetime import datetime

from backend.config import IST, AppConfig
from backend.database.models import get_db, set_setting
from backend.engine import real

MON, WED, THU = "2026-11-23", "2026-11-25", "2026-11-26"
HOLIDAYS = {"2026-11-24"}


def at(day: str, hhmm: str) -> datetime:
    d = datetime.fromisoformat(day)
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def trading(d, holidays=None) -> bool:
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def cfg() -> AppConfig:
    c = AppConfig()
    c.ai.enabled = True
    c.tips.enabled = True
    c.tips.kite_mode = "dry"
    return c


TOK = {"DEF": 9101, "GHI": 9102, "MNO": 9103, "ABC": 9104, "PQR": 9105, "JKL": 9106}


def candles(sym: str, closes: list, days: list) -> None:
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO instruments (instrument_token, tradingsymbol, exchange, segment, fetched_on) VALUES (?, ?, 'NSE', 'NSE', ?)",
                     (TOK[sym], sym, f"{MON}T08:00:00+05:30"))
        for d, c in zip(days, closes):
            conn.execute("INSERT OR REPLACE INTO candles_daily (instrument_token, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                         (TOK[sym], d, c, c + 1, c - 1, c, 1000))


def positions(live: bool = False) -> dict:
    p = {"DEF": {"qty": 36, "px": 842.0, "last": 831.0, "d": "2026-11-12", "stop": 805.0, "charges": 30.0, "ts": "DEF", "days": 9, "confirmed": True},
         "GHI": {"qty": 10, "px": 1980.0, "last": 2131.0, "d": "2026-11-13", "stop": 2120.0, "charges": 30.0, "ts": "GHI", "days": 10, "confirmed": True},
         "MNO": {"qty": 12, "px": 1650.0, "last": 1705.0, "d": "2026-11-16", "stop": 1600.0, "target": 1900.0, "charges": 30.0, "ts": "MNO", "days": 7,
                 "confirmed": False}}
    if live:
        p["DEF"]["live"] = 829.0
    return p


def plan_orders() -> list:
    return [{"action": "EXIT", "symbol": "GHI", "ts": "GHI", "qty": 10, "why": "the stop was raised to 2,120 at the close"},
            {"action": "BUY", "symbol": "ABC", "ts": "ABC", "qty": 24, "stop": 1196.0, "close": 1254.0, "why": "breakout above 1,254"},
            {"action": "BUY", "symbol": "PQR", "ts": "PQR", "qty": 36, "stop": 805.0, "close": 829.4, "why": "the 50-day filter passed"},
            {"action": "BUY", "symbol": "JKL", "ts": "JKL", "qty": 10, "stop": 900.0, "close": 950.0, "why": "breakout above 948"}]


def seed_real(state: str = "evening") -> dict:
    """evening / premarket: the plan for THU waits (JKL skipped for THU); market: THU's send is done (ABC bought,
    GHI sold)."""
    st = {"started": "2026-10-01", "capital": 300_000.0, "cash": 2_29_749.0, "charges": 0.0, "as_of": WED, "positions": positions(),
          "orders": plan_orders(), "cap": 14, "allowed": True, "waiting": 0, "variant": "E",
          "skips": {THU: {"JKL": {"by": "you", "at": f"{WED}T20:10:00+05:30", "note": "results Friday"}}},
          "filter": {"name": "E+", "state": "FULL", "slots": 14}}
    with get_db() as conn:
        for d, eq in ((MON, 3_01_957.0), (WED, 3_01_435.0)):
            conn.execute("INSERT OR REPLACE INTO real_equity (day, equity, gross, nifty) VALUES (?, ?, ?, ?)", (d, eq, eq + 900, 25_800.0))
        for sym, act, px, hhmm in (("STU", "BUY", 412.5, "09:16"), ("VWX", "EXIT", 655.0, "09:16")):
            conn.execute("INSERT INTO real_orders (key, day, created_at, symbol, action, qty, reason, status, price) VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"{WED}|{sym}|{act}", WED, at(WED, hhmm).isoformat(timespec="seconds"), sym, act, 20, "plan", "sent", px))
    if state == "market":
        st["cash"] = 2_20_886.0                           # GHI sold (2,131 × 10), ABC bought (1,257.2 × 24)
        st["positions"] = positions(live=True)
        st["positions"].pop("GHI")
        st["positions"]["ABC"] = {"qty": 24, "px": 1257.2, "last": 1257.2, "d": THU, "stop": 1196.0, "charges": 20.0, "ts": "ABC", "days": 0,
                                  "est": True, "confirmed": False}
        for o in st["orders"]:
            o["sent"] = "skipped" if o["symbol"] == "JKL" else "sent"
        with get_db() as conn:
            for sym, act, px, why, status in (("GHI", "EXIT", 2131.0, "the stop was raised", "sent"), ("ABC", "BUY", 1257.2, "breakout", "sent"),
                                              ("PQR", "BUY", 834.0, "breakout", "sent"), ("JKL", "BUY", None, "skipped today · by you", "skipped")):
                conn.execute("INSERT INTO real_orders (key, day, created_at, symbol, action, qty, reason, status, price) VALUES (?,?,?,?,?,?,?,?,?)",
                             (f"{THU}|{sym}|{act}", THU, at(THU, "09:16").isoformat(timespec="seconds"), sym, act, 10, why, status, px))
    real._save(st)
    for sym, closes in (("DEF", [842.0, 831.0]), ("GHI", [2118.0, 2131.0]), ("MNO", [1690.0, 1705.0])):
        candles(sym, closes, [MON, WED])
    return st


def seed_night(day: str = WED) -> None:
    """The night batch of `day`: the evening job, the scan, two filing notes (LTTS, model-written), the jobs."""
    t = lambda hhmm: at(day, hhmm).isoformat(timespec="seconds")      # noqa: E731
    with get_db() as conn:
        conn.execute("INSERT INTO job_runs (job, run_date, started_at, finished_at, status, message) VALUES ('eod', ?, ?, ?, 'ok', '')",
                     (day, t("18:30"), t("18:36")))
        conn.execute("INSERT INTO scan_runs (as_of, ran_at, scanned, stale, counts, config) VALUES (?, ?, 581, 0, '{}', '{}')", (day, t("18:36")))
        for k, (kind, key, status, fin) in enumerate((("night", "note:filing:1", "done", "19:20"), ("night", "note:filing:2", "done", "19:27"),
                                                       ("night", f"why:{day}", "skipped", None), ("night", f"suggest:{day}", "done", "19:27"),
                                                       ("night", f"view:{day}", "done", "19:27"), ("market", f"market:score:{day}", "queued", None))):
            conn.execute("INSERT INTO ai_jobs (created, started, finished, kind, key, status, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (t("18:37"), t(fin) if fin else None, t(fin) if fin else None, kind, key, status,
                          "nothing closed today" if status == "skipped" else None))
        for i, text in enumerate(("a cybersecurity incident clarification, no financial impact stated", "a board meeting date for the results")):
            conn.execute("INSERT INTO ai_notes (created, day, kind, symbol, book, severity, title, text, data, model, prompt_version) "
                         "VALUES (?, ?, 'filing', 'LTTS', 'watch', 'info', 'Filing', ?, '{}', 'night-7b', 'notes-v1')", (t(f"19:2{i}"), day, text))
        conn.execute("INSERT INTO ai_views (created, day, symbol, source, score, verdict, status, change, prompt_version) "
                     "VALUES (?, ?, 'DEF', 'held', 48, 'trim', 'ok', 'DEF slipped toward its stop on lighter volume', 'view-v1')", (t("19:26"), day))


def seed_morning(day: str = THU) -> None:
    """The 9:00 AM brief (model-written, stored), the stored VIX quote, a stored Kite token, an overnight filing."""
    with get_db() as conn:
        lines = ["Two buys and one stop-exit go at 9:16; JKL is held for its results", "DEF sits 3.1 % above its stop"]
        conn.execute("INSERT INTO ai_notes (created, day, kind, severity, title, text, data, model, prompt_version) VALUES (?, ?, 'brief', 'info', 't', ?, ?, 'm', 'brief-v1')",
                     (at(day, "09:00").isoformat(), day, "\n".join(lines), json.dumps({"lines": lines})))
        set_setting(conn, "market.vix", json.dumps({"last": 17.7, "prev": 17.2, "at": at(WED, "15:30").isoformat()}))
        conn.execute("INSERT INTO filings (seq_id, symbol, dt, category, text, url, kind, fetched_at) VALUES ('f1', 'LTTS', ?, 'info', 'x', '', 'info', ?)",
                     (f"{WED}T19:05:00", f"{WED}T19:06:00"))
    from backend.kite_api import auth
    with get_db() as conn:
        auth.store_token(conn, "tok-test", at(day, "07:55"))


def held_reads(day: str = WED) -> None:
    """Stored daily candle-model reads on the held names (3-session horizon): DEF stop-first 52 %, GHI up 66 %, MNO up 57 %."""
    from backend.market import reads as mr
    for sym, p_up, sf, stop in (("DEF", 0.41, 0.52, 805.0), ("GHI", 0.66, 0.2, 2120.0), ("MNO", 0.57, 0.1, 1600.0)):
        mr.store(at(day, "19:05"), sym, {"version": mr.VERSION}, {"horizon": 3, "p_up": p_up, "touch": {f"{stop:g}": sf}}, {"asof": day},
                 book="real", interval="1d")
