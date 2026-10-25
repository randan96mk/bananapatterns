"""Exchange filings (NSE corporate announcements): what a stock has told the market lately.

Source: NSE's corporate-announcements feed (https://www.nseindia.com/api/corporate-announcements), the
exchange's own public data. Fetched every filings_interval_min (07:00-23:00, any day: results often come
out in the evening or on a Saturday) and by the evening job; a failed fetch keeps what is stored.

Each filing is sorted into a kind from NSE's category and text:
  results    financial results (board-meeting outcome, results press release)
  order      orders / contracts won
  deal       acquisition, merger, agreement, buyback, fund raising, capacity / production start
  rating     credit rating
  management change in management / auditors, director or KMP resignation
  legal      regulatory / legal action, orders passed, insolvency, litigation, default, penalty
  exchange   the exchange asked about a news item, a volume spurt or a price move
  update     press release, presentation, other updates (shown, never pushed on their own)
  noise      newspaper copies, analyst-meet schedules, trading window, ESOPs, certificates ... (stored, hidden)
"""

from datetime import date, datetime, timedelta
from typing import Optional

import httpx

from backend.database.models import get_db, now_ist
from backend.ingest.holidays import NSE_HOME, UA
from backend.logging_config.logger import setup_logger

logger = setup_logger("filings")

NSE_FILINGS = "https://www.nseindia.com/api/corporate-announcements"
KEEP_DAYS = 120

LABELS = {"results": "Results", "order": "Order win", "deal": "Deal", "rating": "Credit rating",
          "management": "Management change", "legal": "Regulatory / legal", "exchange": "Exchange query",
          "update": "Update", "noise": "Filing"}
IMPORTANT = ("results", "order", "deal", "legal", "rating", "exchange", "management")     # most telling first
SHOWN = IMPORTANT + ("update",)

# (kind, substrings of NSE's lower-cased category); first match wins
CATEGORY_RULES = [
    ("order", ("bagging", "receiving of orders", "award of order")),
    ("deal", ("acquisition", "amalgamation", "merger", "scheme of arrangement", "agreement", "joint venture",
              "buyback", "buy back", "capacity addition", "commencement of commercial", "fund raising",
              "qualified institutions placement", "preferential issue", "rights issue", "divestment",
              "disinvestment", "sale or disposal", "demerger", "open offer")),
    ("rating", ("credit rating",)),
    ("management", ("change in management", "change in auditor", "resignation", "cessation", "change in director",
                    "appointment of chief", "change in key managerial")),
    ("legal", ("action(s) taken", "action(s) initiated", "orders passed", "insolvency", "litigation", "default",
               "fraud", "forensic", "search", "penalt")),
    ("exchange", ("news verification", "spurt in volume", "price movement", "clarification")),
    ("noise", ("analysts/institutional investor meet", "con. call")),
    ("update", ("press release", "investor presentation", "general updates", "updates", "outcome of board meeting",
                "financial result")),
]


def kind_of(category: str, text: str = "") -> str:
    c, t = category.lower(), text.lower()
    if "newspaper" not in c and "newspaper" not in t and "financial result" in f"{c} {t}" \
            and ("outcome of board meeting" in c or "financial result" in c or "press release" in c
                 or "integrated filing" in c or c in ("updates", "general updates")):
        return "results"
    for kind, keys in CATEGORY_RULES:
        if any(k in c for k in keys):
            return kind
    return "noise"


def parse_nse(payload: list) -> list[dict]:
    """NSE corporate-announcements JSON -> [{seq_id, symbol, dt (ISO, IST), category, text, url, kind}]."""
    out = []
    for a in payload or []:
        try:
            dt = datetime.strptime(a.get("an_dt") or a["exchdisstime"], "%d-%b-%Y %H:%M:%S").isoformat()
        except (KeyError, TypeError, ValueError):
            continue
        sym, seq = (a.get("symbol") or "").strip().upper(), str(a.get("seq_id") or "")
        if not sym or not seq:
            continue
        cat, text = a.get("desc") or "", (a.get("attchmntText") or "").strip()
        out.append({"seq_id": seq, "symbol": sym, "dt": dt, "category": cat, "text": text,
                    "url": a.get("attchmntFile") or "", "kind": kind_of(cat, text)})
    return out


def refresh_from_nse(today: Optional[date] = None, days_back: Optional[int] = None,
                     client: Optional[httpx.Client] = None) -> list[dict]:
    """Fetch filings since the newest stored day (3 days on the first run) and store them.
    Returns the filings that were new to the table."""
    today = today or date.fromisoformat(now_ist()[:10])
    if days_back is None:
        newest = latest_dt()
        days_back = 3 if not newest else min(7, max(1, (today - date.fromisoformat(newest[:10])).days + 1))
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": UA, "Accept": "application/json",
                                             "Referer": "https://www.nseindia.com/"}, timeout=30,
                                    follow_redirects=True)
    fmt = "%d-%m-%Y"
    params = {"index": "equities", "from_date": (today - timedelta(days=days_back)).strftime(fmt),
              "to_date": today.strftime(fmt)}
    try:
        try:
            client.get(NSE_HOME)                    # sets cookies; may 403, the API often still answers
        except httpx.HTTPError:
            pass
        resp = client.get(NSE_FILINGS, params=params)
        resp.raise_for_status()
        rows = parse_nse(resp.json())
    finally:
        if own:
            client.close()
    return store(rows)


def store(rows: list[dict]) -> list[dict]:
    """Insert the filings not seen before; returns those. Drops filings older than KEEP_DAYS."""
    now, new = now_ist(), []
    with get_db() as conn:
        for r in rows:
            cur = conn.execute("INSERT OR IGNORE INTO filings (seq_id, symbol, dt, category, text, url, kind, "
                               "fetched_at) VALUES (?,?,?,?,?,?,?,?)",
                               (r["seq_id"], r["symbol"], r["dt"], r["category"], r["text"], r["url"], r["kind"], now))
            if cur.rowcount:
                new.append(r)
        cutoff = (date.fromisoformat(now[:10]) - timedelta(days=KEEP_DAYS)).isoformat()
        conn.execute("DELETE FROM filings WHERE dt < ?", (cutoff,))
    logger.info("Filings: %d fetched, %d new", len(rows), len(new))
    return new


def latest_dt() -> Optional[str]:
    with get_db() as conn:
        return conn.execute("SELECT MAX(dt) FROM filings").fetchone()[0]


def last_fetch() -> Optional[str]:
    with get_db() as conn:
        return conn.execute("SELECT MAX(fetched_at) FROM filings").fetchone()[0]


def recent(symbol: str, since: str, kinds=SHOWN, limit: int = 50) -> list[dict]:
    """A stock's filings from `since` (ISO date or datetime), newest first."""
    with get_db() as conn:
        return [dict(r, label=LABELS[r["kind"]]) for r in conn.execute(
            f"SELECT seq_id, symbol, dt, category, text, url, kind FROM filings WHERE symbol = ? AND dt >= ? "
            f"AND kind IN ({','.join('?' * len(kinds))}) ORDER BY dt DESC LIMIT ?", (symbol, since, *kinds, limit))]


def latest_important(symbols: list[str], since: str) -> dict[str, dict]:
    """{symbol: its newest important filing since `since`} (for card chips)."""
    if not symbols:
        return {}
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT symbol, dt, kind, text, url FROM filings WHERE dt >= ? AND symbol IN "
            f"({','.join('?' * len(symbols))}) AND kind IN ({','.join('?' * len(IMPORTANT))}) ORDER BY dt",
            (since, *symbols, *IMPORTANT)).fetchall()
    return {r["symbol"]: dict(r, label=LABELS[r["kind"]]) for r in rows}      # later rows win: newest


def short_date(dt: str) -> str:
    d = datetime.fromisoformat(dt)
    return f"{d.day} {d.strftime('%b')}"
