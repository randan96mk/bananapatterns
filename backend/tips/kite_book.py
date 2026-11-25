"""⚡ The Kite ledger: one row per closed Kite hint in tips_kite_trades (book "kite" = real fills on the app's own tagged
orders, "dry" = the dry run's simulator), next to the Tips paper book. Append-only: INSERT OR IGNORE on (hint_id, book),
never updated, never deleted; a re-close (a resync) adds nothing.

  row      gross = the hint's realised ₹ from its fills (kite["realised"]); charges = backend.tips.book.charges() per order
           over the entry fill(s) (the add included) and every exit fill, the paper book's per-order model; net = gross −
           charges; exits = [{leg, qty, px, at}] (stop / t1 / t2 / t3 / exit); why = kite["reason"], by = kite["by"].
  backfill once at startup (and POST /api/tips/kite/book/backfill): every closed Kite hint without its row gets one from
           its kite JSON + its tips_orders fills. Existing rows are never touched.
  view     the rows of the mode in force (dry while kite_mode is dry, else the real book): today, since, taken, won,
           totals, the trades newest first. A dry run is never summed as real.
Only the app's own orders are read (tips_orders / the hint's kite JSON); Kite holdings and positions never are."""

import json
from datetime import datetime
from typing import Optional

from backend.config import IST
from backend.database.models import get_db
from backend.logging_config.logger import setup_logger
from backend.tips import book as bk

logger = setup_logger("tips")

COLS = ("hint_id", "book", "day", "opened_at", "closed_at", "side", "symbol", "tradingsymbol", "lots", "qty", "entry_px", "exit_px",
        "exits", "gross", "charges", "net", "why", "by")
EXIT_LEGS = ("exit", "stop", "t1", "t2", "t3")
ENTRY_LEGS = ("entry", "add")


def _now() -> datetime:
    return datetime.now(IST)


def book_of(k: dict) -> str:
    return "dry" if k.get("dry") else "kite"


def mode_book(config) -> str:
    """The book a page shows: dry while kite_mode is dry, else the real one."""
    from backend.tips import kite_orders as ko
    return "dry" if ko.mode(config) == "dry" else "kite"


def _at(r: dict) -> Optional[str]:
    return r.get("updated_at") or r.get("placed_at")


def _fills(rows: list[dict], dry: bool) -> tuple[list[dict], list[dict]]:
    """(entry-side fills, exit-side fills) of one hint's order rows in its own book (voided / test rows never)."""
    from backend.tips import kite_orders as ko
    mine = [r for r in rows if r.get("leg") != "test" and r.get("status") == "complete" and r.get("filled_px") is not None
            and ko.is_dry_row(r) == bool(dry)]
    return [r for r in mine if r["leg"] in ENTRY_LEGS], [r for r in mine if r["leg"] in EXIT_LEGS]


def _q(r: dict) -> int:
    return int(r.get("filled_qty") or r.get("qty") or 0)


def trade_row(h: dict, rows: list[dict], now: Optional[datetime] = None) -> Optional[dict]:
    """The ledger row of a closed Kite hint from its kite JSON and its order rows (pure; None when it has no entry fill
    or is not closed)."""
    from backend.tips import kite_orders as ko
    k = h.get("kite") if isinstance(h.get("kite"), dict) else {}
    if k.get("state") != "closed":
        return None
    dry = bool(k.get("dry"))
    ent, exs = _fills(rows, dry)
    if not ent and k.get("entry_px") is None:
        return None
    buy = h.get("side") != "SELL"
    charges = round(sum(bk.charges(float(r["filled_px"]) * _q(r), not buy) for r in ent)
                    + sum(bk.charges(float(r["filled_px"]) * _q(r), buy) for r in exs), 2)
    if k.get("realised") is not None:
        gross = round(float(k["realised"]), 2)
    else:
        gross = round(float(ko.realised_by_hint([r for r in rows if ko.is_dry_row(r) == dry and r.get("status") != "voided"]).get(h["id"], 0.0)), 2)
    qty = int(k.get("qty") or 0) or sum(_q(r) for r in ent)
    lot = int(h.get("lot_size") or 0) or 1
    entry_px = k.get("entry_px")
    if entry_px is None and ent:
        tq = sum(_q(r) for r in ent)
        entry_px = round(sum(float(r["filled_px"]) * _q(r) for r in ent) / tq, 2) if tq else None
    exit_px = k.get("exit_px")
    if exit_px is None and exs:
        tq = sum(_q(r) for r in exs)
        exit_px = round(sum(float(r["filled_px"]) * _q(r) for r in exs) / tq, 2) if tq else None
    opened = k.get("entry_at") or (_at(ent[0]) if ent else None)
    closed = k.get("closed_at") or (_at(exs[-1]) if exs else None) or (now or _now()).isoformat(timespec="seconds")
    return {"hint_id": h["id"], "book": "dry" if dry else "kite", "day": str(closed)[:10], "opened_at": opened, "closed_at": closed,
            "side": h.get("side") or "BUY", "symbol": h.get("symbol"), "tradingsymbol": h.get("tradingsymbol") or (ent[0].get("tradingsymbol") if ent else None),
            "lots": round(qty / lot, 2) if qty else float(k.get("lots") or 0), "qty": qty, "entry_px": entry_px, "exit_px": exit_px,
            "exits": json.dumps([{"leg": r["leg"], "qty": _q(r), "px": float(r["filled_px"]), "at": _at(r)} for r in exs]),
            "gross": gross, "charges": charges, "net": round(gross - charges, 2), "why": k.get("reason"), "by": k.get("by")}


def _insert(row: dict, conn) -> bool:
    cur = conn.execute(f"INSERT OR IGNORE INTO tips_kite_trades ({', '.join(chr(34) + c + chr(34) for c in COLS)}) "
                       f"VALUES ({','.join('?' * len(COLS))})", tuple(row.get(c) for c in COLS))
    return cur.rowcount > 0


def record(config, h: dict, now: Optional[datetime] = None) -> bool:
    """The close of a Kite hint → its ledger row (INSERT OR IGNORE). True when a row was added."""
    from backend.tips import kite_orders as ko
    row = trade_row(h, ko.rows_for(h["id"]), now)
    if not row:
        return False
    with get_db() as conn:
        return _insert(row, conn)


def backfill(config=None, now: Optional[datetime] = None) -> list[int]:
    """Every closed Kite hint without its ledger row gets one (from its kite JSON + its tips_orders fills). Never
    updates or deletes a row. Returns the hint ids added."""
    from backend.tips import kite_orders as ko
    hs = bk.rows("kite IS NOT NULL AND json_extract(kite, '$.state') = 'closed'", order="id")
    if not hs:
        return []
    with get_db() as conn:
        have = {(r[0], r[1]) for r in conn.execute("SELECT hint_id, book FROM tips_kite_trades")}
    need = [h for h in hs if (h["id"], book_of(h["kite"])) not in have]
    if not need:
        return []
    by_hint: dict = {}
    for r in ko.rows_for_hints([h["id"] for h in need]):
        by_hint.setdefault(r["hint_id"], []).append(r)
    done = []
    with get_db() as conn:
        for h in need:
            row = trade_row(h, by_hint.get(h["id"], []), now)
            if row and _insert(row, conn):
                done.append(h["id"])
    if done:
        logger.info("Tips Kite ledger: %s row(s) backfilled (%s)", len(done), ", ".join(f"#{i}" for i in done))
    return done


def rows(book: str, since_day: Optional[str] = None) -> list[dict]:
    q = "SELECT * FROM tips_kite_trades WHERE book = ?" + (" AND day >= ?" if since_day else "") + " ORDER BY closed_at DESC, id DESC"
    with get_db() as conn:
        out = [dict(r) for r in conn.execute(q, (book, since_day) if since_day else (book,))]
    for r in out:
        try:
            r["exits"] = json.loads(r.get("exits") or "[]")
        except ValueError:
            r["exits"] = []
    return out


def by_hint(book: str = "kite") -> dict:
    """{hint_id: ledger row} of one book."""
    return {r["hint_id"]: r for r in rows(book)}


def _when(iso: Optional[str]) -> str:
    from backend.tips import kite_orders as ko
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    if dt.tzinfo is not None:
        dt = dt.astimezone(IST)
    return f"{dt:%a} {dt.day} {dt:%b} {ko.t12(iso)}"


ENDED = {"stop": "stop", "target 1": "T1", "target 2": "T2", "target 3": "T3", "your exit": "your exit"}


def open_positions(book: str) -> list[dict]:
    """The hints entered on Kite now in this book (state entered, qty open)."""
    out = []
    for h in bk.rows("json_extract(kite, '$.state') = 'entered'", order="id"):
        k = h["kite"]
        if int(k.get("qty_open") or 0) > 0 and book_of(k) == book:
            out.append(h)
    return out


def view(config, days: Optional[int] = None, now: Optional[datetime] = None) -> dict:
    """{book, dry, today, since, taken, won, open, lots_open, trades_today, net, trades [newest first], totals}."""
    from datetime import timedelta
    from backend.tips import plan as pl
    now = now or _now()
    book = mode_book(config)
    today = now.date().isoformat()
    since_day = (now.date() - timedelta(days=int(days))).isoformat() if days else None
    rs = rows(book, since_day)
    names: dict = {}
    for hid in {r["hint_id"] for r in rs}:
        h = bk.load(hid)
        names[hid] = bk.title(h) if h else f"#{hid}"
    trades = []
    for r in rs:
        exits = r.get("exits") or []
        trades.append({**r, "title": names.get(r["hint_id"], f"#{r['hint_id']}"), "when_label": _when(r.get("closed_at")),
                       "opened_label": _when(r.get("opened_at")) if r.get("opened_at") else "",
                       "entry_exit": (f"{pl.fmt(r.get('entry_px'))} → " + (" / ".join(pl.fmt(x.get("px")) for x in exits) if len(exits) > 1
                                                                          else pl.fmt(r.get("exit_px")))) if r.get("entry_px") is not None else "—",
                       "exits_text": " · ".join(f"{str(x.get('leg') or '').upper()} {x.get('qty')} @ {pl.fmt(x.get('px'))}" for x in exits),
                       "ended": ENDED.get(str(r.get("why") or ""), r.get("why") or "—"), "dry": book == "dry"})
    opn = open_positions(book)
    lots_open = 0.0
    for h in opn:
        lot = int(h.get("lot_size") or 0) or 1
        lots_open += int(h["kite"].get("qty_open") or 0) / lot
    tot = {"gross": round(sum(r["gross"] or 0 for r in rs), 2), "charges": round(sum(r["charges"] or 0 for r in rs), 2),
           "net": round(sum(r["net"] or 0 for r in rs), 2), "n": len(rs), "won": sum(1 for r in rs if (r["net"] or 0) > 0)}
    today_rs = [r for r in rs if r.get("day") == today]
    return {"book": book, "dry": book == "dry", "today": round(sum(r["net"] or 0 for r in today_rs), 2), "trades_today": len(today_rs),
            "since": min((r["day"] for r in rs if r.get("day")), default=None), "taken": len(rs), "won": tot["won"], "net": tot["net"],
            "open": len(opn), "closed": len(rs), "lots_open": round(lots_open, 2) if lots_open % 1 else int(lots_open),
            "trades": trades, "totals": tot}


def card(config, v: Optional[dict] = None, now: Optional[datetime] = None) -> dict:
    """The summary strip's `kite`: {mode off|kite|dry, today, net, lots_open, trades_today, since, taken, won}."""
    from backend.tips import kite_orders as ko
    m = ko.mode(config)
    v = v if v is not None else view(config, now=now)
    return {"mode": "off" if m == "off" else "dry" if m == "dry" else "kite", "today": v["today"], "net": v["net"],
            "lots_open": v["lots_open"], "trades_today": v["trades_today"], "since": v["since"], "taken": v["taken"], "won": v["won"]}


def days_view(book: str = "kite", days: Optional[int] = None, now: Optional[datetime] = None) -> list[dict]:
    """The Track tab's "Kite trades by day": [{day, label, n, net, gross, charges, won}] newest first (real book by
    default: a dry run is never part of the record)."""
    from datetime import timedelta
    now = now or _now()
    since_day = (now.date() - timedelta(days=int(days))).isoformat() if days else None
    groups: dict = {}
    for r in rows(book, since_day):
        g = groups.setdefault(r["day"], {"day": r["day"], "n": 0, "net": 0.0, "gross": 0.0, "charges": 0.0, "won": 0})
        g["n"] += 1
        g["net"] = round(g["net"] + (r["net"] or 0), 2)
        g["gross"] = round(g["gross"] + (r["gross"] or 0), 2)
        g["charges"] = round(g["charges"] + (r["charges"] or 0), 2)
        g["won"] += 1 if (r["net"] or 0) > 0 else 0
    out = []
    for d in sorted(groups, reverse=True):
        g = groups[d]
        try:
            dt = datetime.fromisoformat(d)
            g["label"] = f"{dt:%a} {dt.day} {dt:%b}"
        except (TypeError, ValueError):
            g["label"] = str(d)
        out.append(g)
    return out
