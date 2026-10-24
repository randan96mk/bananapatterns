"""NSE trading calendar: weekends + exchange holidays (equity segment).

The list ships in config/nse_holidays.json (official NSE holiday-master, CM
segment) and is refreshed weekly from NSE; a failed refresh keeps the file.
Special sessions (e.g. Diwali Muhurat trading) are not modelled: the evening
job checks whether Kite actually returned a bar for the day.
"""

import json
from datetime import date, datetime
from typing import Optional

import httpx

from backend.config import CONFIG_DIR
from backend.logging_config.logger import setup_logger

logger = setup_logger("holidays")

HOLIDAY_FILE = CONFIG_DIR / "nse_holidays.json"
NSE_HOME = "https://www.nseindia.com/"
NSE_API = "https://www.nseindia.com/api/holiday-master?type=trading"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")

_cache: Optional[dict[str, str]] = None


def load_holidays(path=HOLIDAY_FILE) -> dict[str, str]:
    """{YYYY-MM-DD: description}; empty if the file is missing."""
    global _cache
    if _cache is None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _cache = {h["date"]: h["description"] for h in data.get("holidays", [])}
        except FileNotFoundError:
            logger.warning("No holiday file at %s: only weekends are skipped", path)
            _cache = {}
    return _cache


def is_trading_day(d: date, holidays: Optional[dict] = None) -> bool:
    holidays = load_holidays() if holidays is None else holidays
    return d.weekday() < 5 and d.isoformat() not in holidays


def parse_nse(payload: dict) -> list[dict]:
    """NSE holiday-master JSON -> [{date, description}] for the equity (CM) segment."""
    out = []
    for h in payload.get("CM", []):
        d = datetime.strptime(h["tradingDate"], "%d-%b-%Y").date().isoformat()
        out.append({"date": d, "description": h.get("description", "")})
    if not out:
        raise ValueError("NSE holiday payload has no CM entries")
    return out


def refresh_from_nse(path=HOLIDAY_FILE, client: Optional[httpx.Client] = None) -> int:
    """Fetch the official list and replace the file; merges with past years already in the file."""
    global _cache
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": UA, "Accept": "application/json",
                                             "Referer": "https://www.nseindia.com/"}, timeout=20,
                                    follow_redirects=True)
    try:
        try:
            client.get(NSE_HOME)                    # sets cookies; may 403, the API often still answers
        except httpx.HTTPError:
            pass
        resp = client.get(NSE_API)
        resp.raise_for_status()
        fresh = parse_nse(resp.json())
    finally:
        if own:
            client.close()
    old = {}
    if path.exists():
        old = {h["date"]: h for h in json.loads(path.read_text(encoding="utf-8")).get("holidays", [])}
    old.update({h["date"]: h for h in fresh})
    data = {"source": f"{NSE_API} (segment CM, fetched {date.today().isoformat()})",
            "holidays": sorted(old.values(), key=lambda h: h["date"])}
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    _cache = None
    logger.info("Holiday list refreshed from NSE: %d entries", len(data["holidays"]))
    return len(fresh)
