"""Corporate events calendar from NSE (board meetings: results, dividends, fund raising).

Source: NSE's event calendar (https://www.nseindia.com/api/event-calendar), the exchange's own public
data, fetched once in the evening job (or with Refresh on the Data page). A failed fetch keeps what is
stored and never blocks the scan or alerts. Used to warn before results: holding a swing trade through
results risks a gap past the stop either way.
"""

from datetime import date, datetime, timedelta
from typing import Optional

import httpx

from backend.database.models import get_db, now_ist
from backend.ingest.holidays import NSE_HOME, UA, is_trading_day
from backend.logging_config.logger import setup_logger

logger = setup_logger("events")

NSE_EVENTS = "https://www.nseindia.com/api/event-calendar"


def kind_of(purpose: str, desc: str = "") -> str:
    t = f"{purpose} {desc}".lower()
    if "financial result" in t or "results" in t:
        return "results"
    if "dividend" in t:
        return "dividend"
    if "fund raising" in t or "fund-raising" in t:
        return "fund_raising"
    return "other"


def parse_nse(payload: list) -> list[dict]:
    """NSE event-calendar JSON -> [{symbol, date (ISO), kind, purpose, descr}]."""
    out = []
    for e in payload or []:
        try:
            d = datetime.strptime(e["date"], "%d-%b-%Y").date().isoformat()
        except (KeyError, ValueError):
            continue
        purpose, desc = e.get("purpose") or "", e.get("bm_desc") or ""
        out.append({"symbol": e.get("symbol", "").strip().upper(), "date": d, "kind": kind_of(purpose, desc),
                    "purpose": purpose, "descr": desc})
    return [e for e in out if e["symbol"]]


def refresh_from_nse(today: Optional[date] = None, days_back: int = 10, days_ahead: int = 45,
                     client: Optional[httpx.Client] = None) -> int:
    """Fetch the calendar for [today - days_back, today + days_ahead] and upsert it. Returns rows stored."""
    today = today or date.today()
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": UA, "Accept": "application/json",
                                             "Referer": "https://www.nseindia.com/"}, timeout=20,
                                    follow_redirects=True)
    fmt = "%d-%m-%Y"
    params = {"index": "equities", "from_date": (today - timedelta(days=days_back)).strftime(fmt),
              "to_date": (today + timedelta(days=days_ahead)).strftime(fmt)}
    try:
        try:
            client.get(NSE_HOME)                    # sets cookies; may 403, the API often still answers
        except httpx.HTTPError:
            pass
        resp = client.get(NSE_EVENTS, params=params)
        resp.raise_for_status()
        rows = parse_nse(resp.json())
    finally:
        if own:
            client.close()
    return store(rows)


def store(rows: list[dict]) -> int:
    now = now_ist()
    with get_db() as conn:
        conn.executemany(
            "INSERT INTO corp_events (symbol, date, kind, purpose, descr, fetched_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(symbol, date, purpose) DO UPDATE SET kind=excluded.kind, descr=excluded.descr, "
            "fetched_at=excluded.fetched_at",
            [(r["symbol"], r["date"], r["kind"], r["purpose"], r["descr"], now) for r in rows])
    logger.info("Events calendar: %d rows stored", len(rows))
    return len(rows)


def sessions_until(start: str, end: str) -> int:
    """Trading sessions after `start` up to and including `end` (0 = same day, 1 = the next session)."""
    d, e, n = date.fromisoformat(start), date.fromisoformat(end), 0
    while d < e:
        d += timedelta(days=1)
        n += is_trading_day(d)
    return n


def upcoming_results(as_of: str, symbols: Optional[list[str]] = None, horizon_days: int = 45) -> dict[str, dict]:
    """{symbol: {date, sessions}}: each stock's next results meeting on or after `as_of`."""
    end = (date.fromisoformat(as_of) + timedelta(days=horizon_days)).isoformat()
    q = "SELECT symbol, MIN(date) AS date FROM corp_events WHERE kind = 'results' AND date >= ? AND date <= ?"
    args: list = [as_of, end]
    if symbols is not None:
        if not symbols:
            return {}
        q += f" AND symbol IN ({','.join('?' * len(symbols))})"
        args += list(symbols)
    with get_db() as conn:
        rows = conn.execute(q + " GROUP BY symbol", args).fetchall()
    return {r["symbol"]: {"date": r["date"], "sessions": sessions_until(as_of, r["date"])} for r in rows}


def next_results(symbol: str, as_of: str) -> Optional[dict]:
    return upcoming_results(as_of, [symbol]).get(symbol)


def results_label(r: Optional[dict]) -> str:
    """'Results Tue 28 Oct (in 3 sessions)' / '(today)' / '(next session)'."""
    if not r:
        return ""
    d = date.fromisoformat(r["date"])
    when = {0: "today", 1: "next session"}.get(r["sessions"], f"in {r['sessions']} sessions")
    return f"Results {d.strftime('%a')} {d.day} {d.strftime('%b')} ({when})"


def for_symbol(symbol: str, since: Optional[str] = None) -> list[dict]:
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT symbol, date, kind, purpose, descr FROM corp_events WHERE symbol = ? AND date >= ? ORDER BY date",
            (symbol, since or "0000"))]


def last_fetch() -> Optional[str]:
    with get_db() as conn:
        return conn.execute("SELECT MAX(fetched_at) FROM corp_events").fetchone()[0]
