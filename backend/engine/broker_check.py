"""Broker check: the positions the broker really holds, pasted by hand, against the app's Real book.

Tradetron has no read API and the broker's holdings are never read by the app, so the owner pastes the positions
table (the deployment's table as copied from the browser, a broker positions / holdings CSV, or plain `SYMBOL qty`
lines). `parse` reads it tolerantly, `compare` splits the names three ways (both, app only, broker only) against the
open Real positions, and the last paste is kept in app_settings `real.broker_check` so the panel reopens with it and
Home can say "paste the positions after the 9:16 send". Nothing here changes the book or sends anything: the fixes go
through the existing hand corrections (edit / remove / add), each behind its own confirm.
"""

import csv
import json
import re
from typing import Optional

from backend.config import AppConfig
from backend.database.models import get_db, get_setting, latest_universe, now_ist, set_setting

KEY = "real.broker_check"
# column names as the exports spell them (lower-cased, dots / underscores dropped); first match wins, in this order
SYM_COLS = ("tradingsymbol", "trading symbol", "instrument", "symbol", "scrip", "stock", "name")
QTY_COLS = ("net qty", "net quantity", "netqty", "quantity", "qty", "net")
PRICE_COLS = ("buy avg", "buy average", "avg price", "average price", "avg cost", "average cost", "avg", "average",
              "buy price", "price", "ltp")
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9&\-]*$")
_SUFFIX = re.compile(r"-(EQ|BE|BZ|SM|ST)$")
_NUMBER = re.compile(r"^[+\-]?\d[\d,]*(\.\d+)?$")
_NOISE = {"TOTAL", "INSTRUMENT", "INSTRUMENTS", "SYMBOL", "SCRIP", "STOCK", "HIDE", "SHOW", "NIL", "QTY", "NET", "NAME"}


def _clean(s: str) -> str:
    return s.replace("₹", " ").replace("\xa0", " ").replace("Rs.", " ").replace("Rs", " ").strip()


def _num(tok: str) -> Optional[float]:
    t = tok.strip().replace(",", "")
    if not _NUMBER.match(t):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _symbol(tok: str) -> Optional[str]:
    s = _clean(tok).upper().strip().strip('"').strip("'")
    s = s.split(":", 1)[1] if s.startswith(("NSE:", "BSE:")) else s
    s = _SUFFIX.sub("", s.split()[0]) if s else s
    return s if s and _SYMBOL.match(s) and s not in _NOISE else None


def _header(fields: list[str]) -> Optional[tuple[int, Optional[int], Optional[int]]]:
    """(symbol column, qty column, price column) when the fields look like an export's header, else None."""
    names = [re.sub(r"[._]", "", _clean(f)).strip().lower() for f in fields]
    sym = next((i for n in SYM_COLS for i, h in enumerate(names) if h == n), None)
    if sym is None:
        return None
    qty = next((i for n in QTY_COLS for i, h in enumerate(names) if h == n), None)
    if qty is None:
        qty = next((i for n in QTY_COLS for i, h in enumerate(names) if n in h and "buy" not in h and "sell" not in h), None)
    price = next((i for n in PRICE_COLS for i, h in enumerate(names) if h == n), None)
    if price is None:
        price = next((i for n in PRICE_COLS for i, h in enumerate(names) if n in h and "sell" not in h), None)
    return sym, qty, price


def _split(line: str, delim: Optional[str]) -> list[str]:
    if delim:
        return next(csv.reader([line], delimiter=delim))
    if "\t" in line:
        return [f for f in line.split("\t")]
    return re.split(r"\s{2,}", line.strip())


def _delimiter(line: str) -> Optional[str]:
    """The export's delimiter when the header is comma / semicolon / tab separated, else None."""
    best = max(("\t", ";", ","), key=lambda d: len(next(csv.reader([line], delimiter=d))))
    return best if len(next(csv.reader([line], delimiter=best))) >= 2 else None


def parse(text: str, universe: Optional[set] = None) -> list[dict]:
    """Rows {symbol, qty, price, source_line, empty, unknown} from a pasted positions table / CSV / plain lines.
    Blank and unknown lines are ignored; a "Total" row too; qty 0 rows are kept and flagged `empty`; a symbol not in
    the universe (when one is stored) is flagged `unknown`; the same symbol twice adds up."""
    rows, order = {}, []
    cols, delim = None, None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if cols is None:
            d = _delimiter(line)
            fields = _split(line, d)
            hdr = _header(fields) if len(fields) >= 2 else None
            if hdr:
                cols, delim = hdr, d
                continue
        sym = qty = price = None
        if cols:
            fields = _split(line, delim)
            si, qi, pi = cols
            if si < len(fields):
                sym = _symbol(fields[si])
                if qi is not None and qi < len(fields):
                    qty = _num(_clean(fields[qi]))
                if pi is not None and pi < len(fields):
                    price = _num(_clean(fields[pi]))
        if sym is None or qty is None:                     # no header, or a row not laid out like it: plain tokens
            toks = _clean(line).split()
            sym = _symbol(toks[0]) if toks else None
            nums = [v for v in (_num(t) for t in toks[1:]) if v is not None]
            if sym is None or not nums:
                continue
            qty, price = nums[0], (nums[1] if len(nums) > 1 else None)
        q = int(round(abs(qty)))
        px = round(price, 2) if price and price > 0 else None
        if sym in rows:
            rows[sym]["qty"] += q
            rows[sym]["price"] = rows[sym]["price"] or px
        else:
            rows[sym] = {"symbol": sym, "qty": q, "price": px, "source_line": raw.strip()[:120]}
            order.append(sym)
    out = []
    for sym in order:
        r = rows[sym]
        r["empty"] = r["qty"] == 0
        r["unknown"] = bool(universe) and sym not in universe
        out.append(r)
    return out


def universe_symbols() -> set:
    with get_db() as conn:
        _, members = latest_universe(conn)
    return {m["symbol"].upper() for m in members}


def compare(config: AppConfig, rows: list[dict]) -> dict:
    """The pasted rows against the open Real positions: both (qty match or not, the app's entry and whether it is
    estimated, the broker's price), app only, broker only, and the counts. A check only: nothing is changed."""
    from backend.engine import real
    s = real.summary(config)
    app = {p["symbol"]: p for p in s.get("positions", [])}
    broker = {r["symbol"]: r for r in rows}
    both, app_only, broker_only = [], [], []
    for sym, p in app.items():
        r = broker.get(sym)
        if r is None:
            app_only.append({"symbol": sym, "qty": p["qty"], "entry": p["buy"], "est": bool(p.get("est"))})
        else:
            both.append({"symbol": sym, "app_qty": p["qty"], "broker_qty": r["qty"], "match": p["qty"] == r["qty"],
                         "app_entry": p["buy"], "broker_price": r["price"], "est": bool(p.get("est")), "empty": r["empty"]})
    for sym, r in broker.items():
        if sym not in app:
            broker_only.append({"symbol": sym, "qty": r["qty"], "price": r["price"], "empty": r["empty"], "unknown": r["unknown"]})
    both.sort(key=lambda x: (x["match"], not (x["est"] and x["broker_price"]), x["symbol"]))   # differences first
    app_only.sort(key=lambda x: x["symbol"])
    broker_only.sort(key=lambda x: (x["empty"], x["symbol"]))
    held = [x for x in broker_only if not x["empty"]]
    counts = {"app": len(app), "broker": sum(not r["empty"] for r in rows), "parsed": len(rows), "both": len(both),
              "match": sum(x["match"] for x in both), "differ": sum(not x["match"] for x in both),
              "app_only": len(app_only), "broker_only": len(held), "empty": sum(x["empty"] for x in broker_only),
              "unknown": sum(x["unknown"] for x in broker_only)}
    counts["differences"] = counts["differ"] + counts["app_only"] + counts["broker_only"]
    return {"both": both, "app_only": app_only, "broker_only": broker_only, "counts": counts}


def check(config: AppConfig, text: str) -> dict:
    """Parse, compare and keep the paste (text, time, counts) as the last broker check."""
    rows = parse(text, universe_symbols())
    out = compare(config, rows)
    at = now_ist()
    with get_db() as conn:
        set_setting(conn, KEY, json.dumps({"text": text, "at": at, "counts": out["counts"]}))
    return {"at": at, "text": text, **out}


def saved() -> Optional[dict]:
    with get_db() as conn:
        raw = get_setting(conn, KEY)
    return json.loads(raw) if raw else None


def last(config: AppConfig) -> dict:
    """The last paste compared again with the book as it is now (so a fix made since shows at once)."""
    sv = saved()
    if not sv:
        return {"at": None, "text": "", "both": [], "app_only": [], "broker_only": [], "counts": {}}
    return {"at": sv["at"], "text": sv["text"], **compare(config, parse(sv["text"], universe_symbols()))}


def stale_since() -> Optional[str]:
    """The last day with a sent order (BUY / EXIT) when no broker check was pasted on or after it, else None."""
    with get_db() as conn:
        day = conn.execute("SELECT MAX(day) FROM real_orders WHERE status = 'sent' AND action IN ('BUY', 'EXIT')").fetchone()[0]
    if not day:
        return None
    sv = saved()
    return None if sv and sv["at"][:10] >= day else day
