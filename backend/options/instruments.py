"""NIFTY weekly option contracts from the Kite NFO instruments dump: every CE / PE of the nearest expiry and the next
{expiry, strike, type, token, tradingsymbol, lot_size}, plus the near-month NIFTY future (type FUT, strike 0) whose
1-minute candles give the VWAP. Cached once a day in `options_contracts`; old days stay as history (the latest `as_of`
wins). The expiry is never hard-coded: it is whatever the dump says."""

from datetime import date, datetime
from typing import Optional

from backend.database.models import get_db, now_ist
from backend.futures.instruments import sessions_to_expiry  # noqa: F401  (the same sessions count, same holiday calendar)
from backend.logging_config.logger import setup_logger

logger = setup_logger("options")

NAME = "NIFTY"
INDEX_TOKEN = 256265              # NIFTY 50 (index 1-minute candles)
STRIKE_STEP = 50.0


def _expiry_iso(v) -> str:
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def pick_contracts(nfo: list[dict], today: Optional[date] = None, expiries: int = 2) -> list[dict]:
    """From a raw NFO dump: NIFTY options of the nearest `expiries` expiries on or after `today` (sorted by expiry,
    strike, type) and the near-month NIFTY future. [{expiry, strike, type, token, tradingsymbol, lot_size}]."""
    today = (today or date.today()).isoformat()
    opts, futs = [], []
    for i in nfo:
        if (i.get("name") or "") != NAME:
            continue
        exp = _expiry_iso(i.get("expiry"))
        if not exp or exp < today:
            continue
        row = {"expiry": exp, "strike": float(i.get("strike") or 0), "type": i.get("instrument_type"), "token": int(i["instrument_token"]),
               "tradingsymbol": i["tradingsymbol"], "lot_size": int(i.get("lot_size") or 0)}
        if i.get("segment") == "NFO-OPT" and row["type"] in ("CE", "PE"):
            opts.append(row)
        elif i.get("segment") == "NFO-FUT" and row["type"] == "FUT":
            futs.append({**row, "strike": 0.0})
    keep = sorted({o["expiry"] for o in opts})[:expiries]
    out = sorted((o for o in opts if o["expiry"] in keep), key=lambda o: (o["expiry"], o["strike"], o["type"]))
    if futs:
        out.append(min(futs, key=lambda f: f["expiry"]))
    return out


def latest_as_of(conn=None) -> Optional[str]:
    if conn is not None:
        return conn.execute("SELECT MAX(as_of) FROM options_contracts").fetchone()[0]
    with get_db() as c:
        return latest_as_of(c)


def refresh(client, today: Optional[date] = None, force: bool = False) -> dict:
    """Read the NFO dump once a day and store the NIFTY weekly contracts. {as_of, contracts, expiries, skipped}."""
    today = today or date.fromisoformat(now_ist()[:10])
    as_of = today.isoformat()
    with get_db() as conn:
        if not force and latest_as_of(conn) == as_of:
            n = conn.execute("SELECT COUNT(*) FROM options_contracts WHERE as_of = ?", (as_of,)).fetchone()[0]
            return {"as_of": as_of, "contracts": n, "expiries": expiries(as_of), "skipped": True}
    rows = pick_contracts(client.instruments("NFO"), today)
    with get_db() as conn:
        conn.executemany("INSERT OR REPLACE INTO options_contracts (as_of, expiry, strike, type, token, tradingsymbol, lot_size) "
                         "VALUES (?,?,?,?,?,?,?)",
                         [(as_of, r["expiry"], r["strike"], r["type"], r["token"], r["tradingsymbol"], r["lot_size"]) for r in rows])
    exps = sorted({r["expiry"] for r in rows if r["type"] != "FUT"})
    logger.info("Options contracts %s: %d rows, expiries %s", as_of, len(rows), exps)
    return {"as_of": as_of, "contracts": len(rows), "expiries": exps, "skipped": False}


def _rows(conn, where: str = "", args: tuple = ()) -> list[dict]:
    as_of = latest_as_of(conn)
    if not as_of:
        return []
    q = "SELECT * FROM options_contracts WHERE as_of = ?" + (f" AND {where}" if where else "") + " ORDER BY expiry, strike, type"
    return [dict(r) for r in conn.execute(q, (as_of, *args))]


def expiries(as_of: Optional[str] = None) -> list[str]:
    """The option expiries at the latest as_of, ascending."""
    with get_db() as conn:
        as_of = as_of or latest_as_of(conn)
        if not as_of:
            return []
        return [r[0] for r in conn.execute("SELECT DISTINCT expiry FROM options_contracts WHERE as_of = ? AND type != 'FUT' ORDER BY expiry", (as_of,))]


def near_expiry(today: Optional[str] = None) -> Optional[str]:
    """The nearest stored expiry on or after `today` (expiry day counts)."""
    today = today or now_ist()[:10]
    return next((e for e in expiries() if e >= today), None)


def chain(expiry: str) -> list[dict]:
    """Every CE / PE row of one expiry at the latest as_of."""
    with get_db() as conn:
        return _rows(conn, "expiry = ? AND type != 'FUT'", (expiry,))


def future() -> Optional[dict]:
    """The near-month NIFTY future row (its token feeds the VWAP), or None."""
    with get_db() as conn:
        rows = _rows(conn, "type = 'FUT'")
    return rows[0] if rows else None


def lot_size(expiry: Optional[str] = None) -> Optional[int]:
    rows = chain(expiry) if expiry else None
    if not rows:
        with get_db() as conn:
            rows = _rows(conn, "type != 'FUT'")
    return rows[0]["lot_size"] if rows else None


def status() -> dict:
    """{as_of, contracts, expiries, future, lot_size} for the health row and the API."""
    with get_db() as conn:
        as_of = latest_as_of(conn)
        n = conn.execute("SELECT COUNT(*) FROM options_contracts WHERE as_of = ?", (as_of,)).fetchone()[0] if as_of else 0
    f = future()
    return {"as_of": as_of, "contracts": n, "expiries": expiries(as_of) if as_of else [], "future": f["tradingsymbol"] if f else None,
            "lot_size": lot_size() if as_of else None}
