"""MWPL F&O ban list: NSE's daily fo_secban.csv ("Securities in Ban For Trade Date 12-NOV-2026: 3", then
"1,SYMBOL" lines). Stored per date in `fo_ban`; a failed fetch keeps the last good list and `stale_days` says how old
it is. An empty list is a valid day (the date is recorded in app_settings `futures.ban_date`).
"""

import re
from datetime import date, datetime
from typing import Optional

import httpx

from backend.database.models import get_db, get_setting, now_ist, set_setting
from backend.logging_config.logger import setup_logger

logger = setup_logger("futures")

BAN_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
TIMEOUT = 10
DATE_KEY = "futures.ban_date"
_DATE_RE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")


def parse(text: str, fallback_date: Optional[str] = None) -> tuple[str, list[str]]:
    """(trade date YYYY-MM-DD, [symbols]) from the CSV text. The header's date wins; else `fallback_date` (today)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    day, symbols = None, []
    for ln in lines:
        m = _DATE_RE.search(ln)
        if m and day is None and "ban" in ln.lower():
            try:
                day = datetime.strptime("-".join(m.groups()), "%d-%b-%Y").date().isoformat()
            except ValueError:
                pass
            continue
        parts = [p.strip().strip('"') for p in ln.split(",")]
        if len(parts) >= 2 and parts[0].isdigit() and parts[1]:
            symbols.append(parts[1].upper())
        elif len(parts) == 1 and parts[0].isalpha() and parts[0].isupper():
            symbols.append(parts[0])
    if day is None:
        if not lines or not fallback_date:
            raise ValueError("ban list: no trade date in the header")
        day = fallback_date
    return day, sorted(set(symbols))


def store(day: str, symbols: list[str]) -> int:
    with get_db() as conn:
        conn.executemany("INSERT OR IGNORE INTO fo_ban (date, symbol) VALUES (?, ?)", [(day, s) for s in symbols])
        prev = get_setting(conn, DATE_KEY)
        if not prev or day >= prev:
            set_setting(conn, DATE_KEY, day)
    return len(symbols)


def _fetch(client: httpx.Client) -> str:
    resp = client.get(BAN_URL)
    resp.raise_for_status()
    return resp.text


def refresh(client: Optional[httpx.Client] = None, today: Optional[str] = None) -> dict:
    """Fetch today's list. Returns {ok, date, symbols, error}; on failure the stored list stays and ok is False."""
    today = today or now_ist()[:10]
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": UA, "Accept": "text/csv,*/*",
                                             "Referer": "https://www.nseindia.com/"}, timeout=TIMEOUT, follow_redirects=True)
    try:
        day, symbols = parse(_fetch(client), today)
    except Exception as exc:
        logger.warning("Ban list fetch failed (%s); keeping the last good list", exc)
        return {"ok": False, "date": latest_date(), "symbols": [], "error": str(exc)[:200]}
    finally:
        if own:
            client.close()
    store(day, symbols)
    logger.info("Ban list %s: %d symbols", day, len(symbols))
    return {"ok": True, "date": day, "symbols": symbols, "error": None}


def latest_date() -> Optional[str]:
    with get_db() as conn:
        d = get_setting(conn, DATE_KEY)
        t = conn.execute("SELECT MAX(date) FROM fo_ban").fetchone()[0]
    return max(d or "", t or "") or None


def symbols(day: Optional[str] = None) -> list[str]:
    """The list for `day` (default: the latest stored day at or before today; the latest when none is older)."""
    day = day or latest_date()
    if not day:
        return []
    with get_db() as conn:
        return [r[0] for r in conn.execute("SELECT symbol FROM fo_ban WHERE date = ? ORDER BY symbol", (day,))]


def banned(symbol: str, day: Optional[str] = None) -> bool:
    """Is `symbol` in the ban list for `day` (the latest list at or before `day`, the latest overall if none)?"""
    with get_db() as conn:
        if day:
            use = conn.execute("SELECT MAX(date) FROM fo_ban WHERE date <= ?", (day,)).fetchone()[0]
            d = get_setting(conn, DATE_KEY)
            if d and d <= day and (not use or d > use):
                use = d
        else:
            use = None
        use = use or latest_date()
        if not use:
            return False
        return conn.execute("SELECT 1 FROM fo_ban WHERE date = ? AND symbol = ?", (use, symbol.upper())).fetchone() is not None


def stale_days(today: Optional[str] = None) -> Optional[int]:
    """Calendar days since the last good list; None when none was ever fetched."""
    d = latest_date()
    if not d:
        return None
    t = date.fromisoformat(today or now_ist()[:10])
    return max(0, (t - date.fromisoformat(d)).days)


def summary(today: Optional[str] = None) -> dict:
    d = latest_date()
    return {"date": d, "symbols": symbols(d) if d else [], "stale_days": stale_days(today)}
