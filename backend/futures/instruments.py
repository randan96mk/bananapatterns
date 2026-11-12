"""NFO stock futures contracts from the Kite instruments dump: per underlying the near and next month
{tradingsymbol, token, expiry, lot_size}, plus the lot value (last price x lot) and the OI rank when quotes are
available. Refreshed once a day into `futures_contracts`; expired rows stay as history (the latest `as_of` wins).
"""

from datetime import date, datetime, timedelta
from typing import Optional

from backend.database.models import get_db, now_ist
from backend.ingest.holidays import is_trading_day
from backend.logging_config.logger import setup_logger

logger = setup_logger("futures")

INDEX_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "NIFTYIT", "SENSEX", "BANKEX"}
QUOTE_CHUNK = 400                       # Kite quote: up to 500 keys a call, 1 call/s (client.quote_bucket)


def _expiry_iso(v) -> str:
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def pick_contracts(nfo: list[dict], today: Optional[date] = None) -> dict[str, dict]:
    """{underlying: {near: {...}, next: {...}}} from a raw NFO dump: stock futures only (segment NFO-FUT, index names
    out), sorted by expiry; contracts already expired before `today` are skipped."""
    today = today or date.today()
    by_name: dict[str, list] = {}
    for i in nfo:
        if i.get("instrument_type") != "FUT" or i.get("segment") != "NFO-FUT":
            continue
        name = i.get("name") or ""
        if not name or name in INDEX_NAMES or "NIFTY" in name:
            continue
        exp = _expiry_iso(i.get("expiry"))
        if not exp or exp < today.isoformat():
            continue
        by_name.setdefault(name, []).append({"tradingsymbol": i["tradingsymbol"], "token": int(i["instrument_token"]),
                                             "expiry": exp, "lot_size": int(i.get("lot_size") or 0)})
    out = {}
    for name, cs in by_name.items():
        cs.sort(key=lambda c: c["expiry"])
        out[name] = {"near": cs[0], "next": cs[1] if len(cs) > 1 else None}
    return out


def _quotes(client, picked: dict) -> dict[str, dict]:
    """{tradingsymbol: {last_price, oi}} for the near contracts; an empty dict when the quote call fails."""
    keys = [f"NFO:{c['near']['tradingsymbol']}" for c in picked.values() if c["near"]]
    out = {}
    try:
        for k in range(0, len(keys), QUOTE_CHUNK):
            for key, q in (client.quote(keys[k:k + QUOTE_CHUNK]) or {}).items():
                out[key.split(":", 1)[1]] = {"last_price": q.get("last_price") or 0, "oi": q.get("oi") or 0}
    except Exception as exc:                                    # lot value / OI rank stay empty; the contracts are still saved
        logger.warning("Futures quotes failed (%s): lot values from stored candles, no OI rank", exc)
    return out


def _last_closes(conn) -> dict[str, float]:
    rows = conn.execute("SELECT underlying, close FROM candles_fut_daily c WHERE date = "
                        "(SELECT MAX(date) FROM candles_fut_daily WHERE underlying = c.underlying)").fetchall()
    return {r["underlying"]: r["close"] for r in rows}


def latest_as_of(conn=None) -> Optional[str]:
    if conn is not None:
        return conn.execute("SELECT MAX(as_of) FROM futures_contracts").fetchone()[0]
    with get_db() as c:
        return latest_as_of(c)


def refresh(client, today: Optional[date] = None, force: bool = False) -> dict:
    """Read Kite's NFO dump once a day (client.instruments throttles) and store every stock's near / next contract.
    Returns {as_of, contracts, underlyings, quoted, skipped}."""
    today = today or date.fromisoformat(now_ist()[:10])
    as_of = today.isoformat()
    with get_db() as conn:
        if not force and latest_as_of(conn) == as_of:
            n = conn.execute("SELECT COUNT(*) FROM futures_contracts WHERE as_of = ?", (as_of,)).fetchone()[0]
            return {"as_of": as_of, "contracts": n, "underlyings": None, "quoted": None, "skipped": True}
    picked = pick_contracts(client.instruments("NFO"), today)
    quotes = _quotes(client, picked)
    with get_db() as conn:
        closes = _last_closes(conn) if not quotes else {}
        ranked = sorted((c for c in picked.values() if c["near"]),
                        key=lambda c: -(quotes.get(c["near"]["tradingsymbol"], {}).get("oi") or 0))
        rank = {c["near"]["tradingsymbol"]: i + 1 for i, c in enumerate(ranked)} if quotes else {}
        rows = []
        for name, c in picked.items():
            near = c["near"]
            q = quotes.get(near["tradingsymbol"]) or {}
            px = q.get("last_price") or closes.get(name) or 0
            lot_value = round(px * near["lot_size"], 2) if px else None
            oi = q.get("oi") if quotes else None
            for month in ("near", "next"):
                k = c[month]
                if k:
                    rows.append((name, month, k["tradingsymbol"], k["token"], k["expiry"], k["lot_size"],
                                 lot_value if month == "near" else None, oi if month == "near" else None,
                                 rank.get(near["tradingsymbol"]) if month == "near" else None, as_of))
        conn.executemany("INSERT OR REPLACE INTO futures_contracts (underlying, month, tradingsymbol, token, expiry, "
                         "lot_size, lot_value, oi, oi_rank, as_of) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    logger.info("Futures contracts %s: %d rows for %d stocks (%d quoted)", as_of, len(rows), len(picked), len(quotes))
    return {"as_of": as_of, "contracts": len(rows), "underlyings": len(picked), "quoted": len(quotes), "skipped": False}


def _rows(conn, where: str = "", args: tuple = ()) -> list[dict]:
    as_of = latest_as_of(conn)
    if not as_of:
        return []
    q = "SELECT * FROM futures_contracts WHERE as_of = ?" + (f" AND {where}" if where else "") + " ORDER BY underlying, month"
    return [dict(r) for r in conn.execute(q, (as_of, *args))]


def contracts(underlying: str) -> dict:
    """{near: row|None, next: row|None} at the latest as_of."""
    with get_db() as conn:
        rows = _rows(conn, "underlying = ?", (underlying.upper(),))
    out = {"near": None, "next": None}
    for r in rows:
        out[r["month"]] = r
    return out


def near(underlying: str) -> Optional[dict]:
    return contracts(underlying)["near"]


def universe(lot_cap: Optional[float] = None) -> list[dict]:
    """Every underlying at the latest as_of: [{symbol, tradingsymbol, token, lot_size, lot_value, expiry, oi, oi_rank}]
    by OI rank then name; lot_cap keeps the ones with a known lot value at most the cap."""
    with get_db() as conn:
        rows = _rows(conn, "month = 'near'")
    out = [{"symbol": r["underlying"], "tradingsymbol": r["tradingsymbol"], "token": r["token"], "lot_size": r["lot_size"],
            "lot_value": r["lot_value"], "expiry": r["expiry"], "oi": r["oi"], "oi_rank": r["oi_rank"]} for r in rows]
    if lot_cap is not None:
        out = [u for u in out if u["lot_value"] is not None and u["lot_value"] <= lot_cap]
    out.sort(key=lambda u: (u["oi_rank"] is None, u["oi_rank"] or 0, u["symbol"]))
    return out


def expiry_calendar() -> list[dict]:
    """The distinct expiries at the latest as_of: [{expiry, month, contracts}] ascending."""
    with get_db() as conn:
        as_of = latest_as_of(conn)
        if not as_of:
            return []
        rows = conn.execute("SELECT expiry, month, COUNT(*) AS n FROM futures_contracts WHERE as_of = ? "
                            "GROUP BY expiry, month ORDER BY expiry", (as_of,)).fetchall()
    return [{"expiry": r["expiry"], "month": r["month"], "contracts": r["n"]} for r in rows]


def sessions_to_expiry(expiry, today=None) -> int:
    """Trading sessions after `today` up to and including the expiry (0 = expiry day, negative = expired), using
    the NSE holiday calendar the app already keeps."""
    e = date.fromisoformat(str(expiry)[:10]) if not isinstance(expiry, date) else expiry
    t = today or date.fromisoformat(now_ist()[:10])
    t = date.fromisoformat(t) if isinstance(t, str) else t
    if e < t:
        n, d = 0, e
        while d < t:
            d += timedelta(days=1)
            n -= is_trading_day(d)
        return n
    n, d = 0, t
    while d < e:
        d += timedelta(days=1)
        n += is_trading_day(d)
    return n
