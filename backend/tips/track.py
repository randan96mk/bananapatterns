"""💡 Tracking and the record.

  instruments  the option contracts a hint resolves to, from the market-data instruments dumps (NFO for NSE indices
               and stocks, BFO for SENSEX / BANKEX, MCX for CRUDE and the other commodities), loaded once a day per
               exchange into tips_instruments (only the names the hints need; history kept, the latest as_of wins).
               The expiry: the post's own date line wins, else the nearest listed expiry (weekly for indices that
               have one, monthly for stocks and CRUDE); with no instrument table the computed date is stored and the
               tradingsymbol stays empty (untracked: "no instrument").
  quotes       every 5 s while a hint is open or waiting (60 s otherwise): one quote call for the options and their
               underlyings -> tips_quotes; the record run and the paper position step through the plan on each quote.
               Without a market-data login the hints are tracked=false, untracked_why "log in".
  record       every hint, taken or not: entry reached, the first of T1 / stop / time, minutes to it, best and worst
               move, P&L per lot at the plan, R; per channel by instrument and by hour; your takes vs all hints."""

import json
import threading
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Optional

from backend.config import IST
from backend.database.models import get_db, get_setting, now_ist
from backend.logging_config.logger import setup_logger
from backend.tips import book as bk
from backend.tips import decode as dc
from backend.tips import plan as pl

logger = setup_logger("tips")

POLL_FAST_S, POLL_SLOW_S = 5, 60
QUOTE_CHUNK = 400
SPOT_KEYS = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "FINNIFTY": "NSE:NIFTY FIN SERVICE",
             "MIDCPNIFTY": "NSE:NIFTY MID SELECT", "SENSEX": "BSE:SENSEX", "BANKEX": "BSE:BANKEX"}
OPT_EXCH = {"SENSEX": "BFO", "BANKEX": "BFO"}
WEEKLY = {"NIFTY": 1, "SENSEX": 3}                 # weekday of the weekly expiry (Tue / Thu) where a weekly exists
UNTRACKED = {"log in": "log in", "no instrument": "no instrument", "no symbol": "no symbol"}


def _expiry_iso(v) -> str:
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def exchange_of(h: dict) -> Optional[str]:
    sym, inst = h.get("symbol"), h.get("instrument")
    if inst == "commodity":
        return "MCX"
    if inst == "crypto" or not sym:
        return None
    if inst == "cash":
        return "BSE" if sym in ("SENSEX", "BANKEX") else "NSE"
    return OPT_EXCH.get(sym, "NFO")


def dump_name(sym: str) -> str:
    """The instruments dump's `name` for a hint symbol (CRUDE -> CRUDEOIL)."""
    return dc.COMMODITIES.get(sym, sym)


# --- instruments (NFO / BFO / MCX) ---------------------------------------------------------------------------------------

def pick(dump: list[dict], names: set, exchange: str, today: date) -> list[dict]:
    """Rows of `names` from a raw instruments dump: options (CE / PE) and futures on or after `today`."""
    out = []
    for i in dump:
        name = (i.get("name") or "").upper()
        if name not in names:
            continue
        typ = i.get("instrument_type")
        if typ not in ("CE", "PE", "FUT"):
            continue
        exp = _expiry_iso(i.get("expiry"))
        if not exp or exp < today.isoformat():
            continue
        out.append({"exchange": exchange, "tradingsymbol": i["tradingsymbol"], "name": name, "expiry": exp,
                    "strike": float(i.get("strike") or 0), "type": typ, "token": int(i.get("instrument_token") or 0),
                    "lot_size": int(i.get("lot_size") or 1)})
    return out


def refresh_instruments(client, exchange: str, names: set, today: Optional[date] = None, force: bool = False) -> dict:
    """Load one exchange's dump once a day and keep the rows of `names` (MCX: CRUDEOIL and the other commodities the
    hints name). {exchange, as_of, rows, skipped}."""
    today = today or date.fromisoformat(now_ist()[:10])
    as_of = today.isoformat()
    names = {dump_name(n) for n in names}
    with get_db() as conn:
        have = {r[0] for r in conn.execute("SELECT DISTINCT name FROM tips_instruments WHERE as_of = ? AND exchange = ?", (as_of, exchange))}
    if not force and names <= (have | _asked_today(exchange, as_of)):
        return {"exchange": exchange, "as_of": as_of, "rows": None, "skipped": True}
    names = names | have
    rows = pick(client.instruments(exchange), names, exchange, today)
    with get_db() as conn:
        conn.executemany("INSERT OR REPLACE INTO tips_instruments (as_of, exchange, tradingsymbol, name, expiry, strike, type, token, lot_size) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", [(as_of, r["exchange"], r["tradingsymbol"], r["name"], r["expiry"], r["strike"],
                                                         r["type"], r["token"], r["lot_size"]) for r in rows])
        from backend.database.models import set_setting
        set_setting(conn, f"tips.instr.{exchange}", as_of)
        set_setting(conn, f"tips.instr.{exchange}.names", json.dumps(sorted(names)))
    logger.info("Tips instruments %s %s: %d rows for %s", exchange, as_of, len(rows), sorted(names))
    return {"exchange": exchange, "as_of": as_of, "rows": len(rows), "skipped": False}


def _asked_today(exchange: str, as_of: str) -> set:
    """Names already looked up today (one that the dump did not have is not asked again the same day)."""
    with get_db() as conn:
        if get_setting(conn, f"tips.instr.{exchange}") != as_of:
            return set()
        return set(json.loads(get_setting(conn, f"tips.instr.{exchange}.names") or "[]"))


def contracts(name: str, exchange: str) -> list[dict]:
    with get_db() as conn:
        as_of = conn.execute("SELECT MAX(as_of) FROM tips_instruments WHERE name = ? AND exchange = ?", (name, exchange)).fetchone()[0]
        if not as_of:
            return []
        return [dict(r) for r in conn.execute("SELECT * FROM tips_instruments WHERE name = ? AND exchange = ? AND as_of = ? "
                                              "ORDER BY expiry, strike", (name, exchange, as_of))]


def computed_expiry(sym: str, instrument: str, day: date) -> date:
    """The expiry when no instrument table has it: the weekly (NIFTY Tuesday, SENSEX Thursday) on or after `day`,
    else the monthly (the last Tuesday; BSE the last Thursday; MCX crude options about the 17th), moved back over
    holidays."""
    from backend.tips.plan import trading_day

    def back(d: date, exch: Optional[str] = None) -> date:
        while not trading_day(d, exch):
            d -= timedelta(days=1)
        return d

    if instrument == "commodity":
        d = date(day.year, day.month, 17)
        d = back(d, "MCX")
        if d < day:
            m = day.month % 12 + 1
            d = back(date(day.year + (day.month == 12), m, 17), "MCX")
        return d
    if sym in WEEKLY:
        d = day + timedelta(days=(WEEKLY[sym] - day.weekday()) % 7)
        e = back(d)
        if e < day:
            e = back(d + timedelta(days=7))
        return e
    wd = 3 if sym in ("BANKEX",) else 1

    def last_wd(y: int, m: int) -> date:
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        d = nxt - timedelta(days=1)
        while d.weekday() != wd:
            d -= timedelta(days=1)
        return back(d)

    e = last_wd(day.year, day.month)
    if e < day:
        e = last_wd(day.year + (day.month == 12), day.month % 12 + 1)
    return e


def resolve(h: dict, client=None, today: Optional[date] = None) -> dict:
    """Exchange, expiry, tradingsymbol and lot size of a decoded hint (in place). Loads the exchange's dump once a day
    when a logged-in client is given."""
    today = today or date.fromisoformat((h.get("plan") or {}).get("day") or now_ist()[:10])
    sym, inst = h.get("symbol"), h.get("instrument")
    exch = exchange_of(h)
    h["exchange"] = exch
    if not sym:
        h["tradingsymbol"], h["tracked"], h["untracked_why"] = None, False, "no symbol"
        return h
    if inst == "crypto" or exch is None:
        h["tradingsymbol"], h["tracked"], h["untracked_why"] = None, False, "not covered"
        return h
    if inst == "cash":
        h.update(tradingsymbol=sym, lot_size=1, expiry=None, expiry_src=None, untracked_why=None)
        return h
    name = dump_name(sym)
    if client is not None and getattr(client, "has_token", False):
        try:
            refresh_instruments(client, exch, {sym}, today)
        except Exception as exc:
            logger.warning("Tips instruments %s failed: %s", exch, str(exc)[:120])
    rows = contracts(name, exch)
    is_opt = h.get("strike") is not None and h.get("opt_type")
    if is_opt:
        cands = [r for r in rows if r["type"] == h["opt_type"] and abs(r["strike"] - float(h["strike"])) < 1e-6]
    else:
        cands = [r for r in rows if r["type"] == "FUT"]
    explicit = h.get("expiry") if h.get("expiry_src") == "post" else None
    pick_row = None
    if explicit:
        pick_row = next((r for r in cands if r["expiry"] == explicit), None)
    else:
        pick_row = next((r for r in cands if r["expiry"] >= today.isoformat()), None)
    if pick_row:
        h.update(tradingsymbol=pick_row["tradingsymbol"], lot_size=pick_row["lot_size"], expiry=pick_row["expiry"],
                 expiry_src=h.get("expiry_src") or "listed", untracked_why=None)
        return h
    if not explicit:
        h["expiry"], h["expiry_src"] = computed_expiry(sym, inst, today).isoformat(), "computed"
    h.update(tradingsymbol=None, tracked=False, untracked_why="no instrument")
    if not h.get("lot_size"):
        any_row = next((r for r in rows), None)
        h["lot_size"] = any_row["lot_size"] if any_row else None
    return h


def spot_key(h: dict) -> Optional[str]:
    sym = h.get("symbol")
    if sym in SPOT_KEYS:
        return SPOT_KEYS[sym]
    if h.get("instrument") == "commodity":
        fut = next((r for r in contracts(dump_name(sym), "MCX") if r["type"] == "FUT"), None)
        return f"MCX:{fut['tradingsymbol']}" if fut else None
    if h.get("instrument") in ("option", "future", "cash") and sym:
        return f"NSE:{sym}"
    return None


def quote_key(h: dict) -> Optional[str]:
    if not h.get("tradingsymbol") or not h.get("exchange"):
        return None
    return f"{h['exchange']}:{h['tradingsymbol']}"


def session_open(exchange: Optional[str], now: datetime) -> bool:
    o, c = pl.session(exchange)
    return pl.trading_day(now.date(), exchange) and o <= now.time() < c


# --- the poll -------------------------------------------------------------------------------------------------------------

def tracked_hints(conn=None) -> list[dict]:
    """Hints the poll looks at: open / waiting ones, skipped ones whose record still runs, open paper positions."""
    return bk.rows("status IN ('new', 'waiting', 'active', 'paused') OR (status = 'skipped' AND json_extract(run, '$.state') != 'closed') "
                   "OR (taken = 1 AND json_extract(pos, '$.state') IN ('armed', 'open'))", order="id", conn=conn)


def _spot_samples(conn, hid: int, day: str) -> list:
    return [(r[0], r[1]) for r in conn.execute("SELECT t, spot FROM tips_quotes WHERE hint_id = ? AND t >= ? ORDER BY t", (hid, day))]


def poll(config, client, now: Optional[datetime] = None) -> dict:
    """One pass: quotes for every tracked hint (one call), the record runs and the paper positions stepped, the lines
    pushed. {hints, quoted, events, lines}."""
    now = now or datetime.now(IST)
    hs = tracked_hints()
    if not hs:
        return {"hints": 0, "quoted": 0, "events": 0, "lines": []}
    has_token = client is not None and bool(getattr(client, "has_token", False))
    keys = set()
    for h in hs:
        if h.get("plan") and not h.get("tradingsymbol") and h.get("untracked_why") in ("no instrument", "log in", None) and has_token:
            try:
                resolve(h, client, now.date())
            except Exception as exc:
                logger.warning("Tips resolve #%s failed: %s", h["id"], str(exc)[:120])
        if has_token and quote_key(h) and session_open(h.get("exchange"), now):
            keys.add(quote_key(h))
            if spot_key(h):
                keys.add(spot_key(h))
    q = {}
    if keys:
        try:
            ks = sorted(keys)
            for k in range(0, len(ks), QUOTE_CHUNK):
                q.update(client.quote(ks[k:k + QUOTE_CHUNK]) or {})
        except Exception as exc:
            logger.warning("Tips quotes failed: %s", str(exc)[:160])
            q = {}
    t = now.isoformat(timespec="seconds")
    n_ev, quoted, lines = 0, 0, []
    with get_db() as conn:
        for h in hs:
            plan = h.get("plan")
            if not plan:
                continue
            if not h.get("tradingsymbol"):
                h["tracked"] = False
            elif not has_token:
                h["tracked"], h["untracked_why"] = False, "log in"
            else:
                h["tracked"], h["untracked_why"] = True, None
            ltp = spot = None
            qk = quote_key(h)
            if qk and qk in q:
                ltp = float(q[qk].get("last_price") or 0) or None
                sk = spot_key(h)
                spot = float((q.get(sk) or {}).get("last_price") or 0) or None if sk else None
                if ltp is not None:
                    conn.execute("INSERT OR REPLACE INTO tips_quotes (hint_id, t, ltp, spot) VALUES (?,?,?,?)", (h["id"], t, ltp, spot))
                    quoted += 1
            run = h.get("run") or pl.new_run(plan)
            run["seen"] = True
            h["run"] = run
            s15 = pl.spot_close15(_spot_samples(conn, h["id"], now.date().isoformat()), now) if (plan.get("stop") or {}).get("kind") == "spot" else None
            was = run["state"]
            evs = pl.step(run, plan, ltp, now, s15, spot)
            for e in evs:
                n_ev += 1
                if e["kind"] == "fill":
                    bk.event(h, now, f"entry reached · {pl.fmt(e['px'])} on the quote", "code")
                    if not h.get("taken") and h.get("status") not in ("skipped",):
                        lines.append((f"tips|reach|{h['id']}", _line(h, f"entry reached · now {pl.fmt(e['px'])} · <b>take {h['id']}</b>", now)))
                elif e["kind"] == "no_fill":
                    bk.event(h, now, "no fill · the entry never came", "code")
                elif e["kind"] in ("stop", "trail", "time", "expired") or e["kind"].startswith("t"):
                    bk.event(h, now, f"record: {e['kind'].upper() if e['kind'].startswith('t') and e['kind'][1:].isdigit() else e['kind']}"
                                     + (f" at {pl.fmt(e['px'])}" if e.get("px") is not None else ""), "code")
            pos = h.get("pos") if h.get("taken") else None
            if pos and pos.get("state") in ("armed", "open"):
                pos["run"]["last_ltp"] = ltp if ltp is not None else pos["run"].get("last_ltp")
                pevs = pl.step(pos["run"], plan, ltp, now, s15, spot)
                for ln in bk._apply(h, pevs, now, "code", conn):
                    lines.append((f"tips|pos|{h['id']}|{len(h['events'])}", _line(h, ln, now)))
            bk.refresh(h)
            if was != "closed" and run["state"] == "closed" and not (pos and pos.get("state") in ("armed", "open")):
                thin(conn, h["id"])
            bk.save(h, conn)
    for key, html in lines:                     # the book's steps always; "entry reached" with push_hints
        if key.startswith("tips|reach|") and not config.tips.push_hints:
            continue
        bk.push(config, key, html)
    try:
        bk.snapshot_equity(config, now)
    except Exception:
        logger.exception("Tips equity snapshot failed")
    with get_db() as conn:
        from backend.database.models import set_setting
        set_setting(conn, "tips.quotes_at", t if quoted else (get_setting(conn, "tips.quotes_at") or ""))
    return {"hints": len(hs), "quoted": quoted, "events": n_ev, "lines": [ln for _, ln in lines]}


def _line(h: dict, text: str, now: datetime) -> str:
    from backend.alerts import messages as msg
    return msg.tips_line(h["id"], text, now)


def thin(conn, hid: int) -> int:
    """After a hint closes its 5-second quotes are thinned to the last one of each minute."""
    n = conn.execute("DELETE FROM tips_quotes WHERE hint_id = ? AND t NOT IN (SELECT MAX(t) FROM tips_quotes WHERE hint_id = ? "
                     "GROUP BY substr(t, 1, 16))", (hid, hid)).rowcount
    return n


class Tracker:
    """The quotes poll in its own thread (like the options book's minute, but every 5 s while a hint is open or
    waiting, every 60 s otherwise). Never raises out of its loop."""

    def __init__(self, state):
        self.state = state
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last: Optional[dict] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="tips-tracker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            wait = POLL_SLOW_S
            try:
                self.last = poll(self.state.config, self.state.client)
                if self.last["hints"]:
                    wait = POLL_FAST_S
            except Exception:
                logger.exception("Tips tracker pass failed")
            self._stop.wait(wait)


# --- the record -----------------------------------------------------------------------------------------------------------

def group_of(h: dict) -> tuple[str, str]:
    side = (h.get("side") or "?").lower()
    sym, inst = h.get("symbol"), h.get("instrument")
    if inst == "commodity":
        return f"{sym}:{side}", f"{sym} · {side}"
    if inst == "option":
        if sym in dc.INDEX_NAMES:
            return f"index_opt:{side}", f"Index options · {side}"
        return f"stock_opt:{side}", f"Stock options · {side}"
    if inst == "future":
        return f"fut:{side}", f"Futures · {side}"
    if inst == "cash":
        return f"cash:{side}", f"Stocks · {side}"
    return f"other:{side}", f"Other · {side}"


def hour_of(h: dict) -> tuple[str, str]:
    t = datetime.fromisoformat(h["posted_at"])
    if t.hour < 9:
        return "h0", "before 9 AM"
    if t.hour == 9:
        return "h9", "9–10 AM posts"
    if t.hour == 10:
        return "h10", "10–11 AM posts"
    return "h11", "after 11 AM"


def _agg(hs: list[dict]) -> dict:
    sc = [h.get("score") or {} for h in hs]
    rs = [s["r"] for s in sc if s.get("r") is not None and not s.get("open")]
    return {"hints": len(hs), "t1_first": sum(1 for s in sc if s.get("first") == "t1"),
            "stop_first": sum(1 for s in sc if s.get("first") in ("stop", "trail")),
            "time_first": sum(1 for s in sc if s.get("first") in ("time", "expired")),
            "no_fill": sum(1 for s in sc if s.get("first") == "no_fill"),
            "open": sum(1 for h in hs if h.get("status") in bk.OPEN_STATUSES),
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
            "pnl_lot": round(sum(s.get("pnl_lot") or 0 for s in sc if not s.get("open")), 2)}


def record(days: int = 30, channel: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """The channel's record over `days`: {by_instrument, by_hour, totals, untracked, you_vs_all, days, channel}."""
    now = now or datetime.now(IST)
    since = (now - timedelta(days=days)).isoformat(timespec="seconds")
    hs = bk.rows("posted_at >= ? AND channel != 'rehearsal'" + (" AND channel = ?" if channel else ""), (since, channel) if channel else (since,), order="id")
    with get_db() as conn:
        quoted = {r[0] for r in conn.execute("SELECT DISTINCT hint_id FROM tips_quotes q JOIN tips_hints h ON h.id = q.hint_id "
                                             "WHERE h.posted_at >= ?", (since,))}
    scored = [h for h in hs if h.get("plan") and h.get("tradingsymbol") and (h["id"] in quoted or (h.get("score") or {}).get("entry_reached"))]
    untracked = [h for h in hs if h not in scored]
    why: dict = {}
    for h in untracked:
        k = h.get("untracked_why") or ("not decoded" if not h.get("plan") else "no quotes yet")
        why[k] = why.get(k, 0) + 1

    def table(keyf) -> list[dict]:
        groups: dict = {}
        for h in scored:
            k, label = keyf(h)
            groups.setdefault(k, {"key": k, "label": label, "items": []})["items"].append(h)
        return [{"key": g["key"], "label": g["label"], **_agg(g["items"])} for g in sorted(groups.values(), key=lambda g: g["key"])]

    by_hour = table(hour_of)
    tot = _agg(scored)
    best = max((r for r in by_hour if r["avg_r"] is not None), key=lambda r: r["avg_r"], default=None)
    times = [datetime.fromisoformat(h["posted_at"]) for h in scored if (h.get("score") or {}).get("first") == "t1"]
    tot["best_hour"] = best["label"] if best else None
    tot["best_time"] = None
    if times:
        from backend.timefmt import ampm
        best_t = max(scored, key=lambda h: (h.get("score") or {}).get("r") or -99)
        tot["best_time"] = ampm(datetime.fromisoformat(best_t["posted_at"]))
    taken = [h for h in hs if h.get("taken") and (h.get("pos") or {}).get("entry_px") is not None]
    skipped = [h for h in scored if h.get("status") == "skipped" and not (h.get("score") or {}).get("open")]
    you = round(sum(h.get("pnl") or 0 for h in taken), 2)
    return {"days": days, "channel": channel, "by_instrument": table(group_of), "by_hour": by_hour, "totals": tot,
            "untracked": {"count": len(untracked), "why": why},
            "you_vs_all": {"you": you, "all": tot["pnl_lot"], "took": len(taken), "of": len(scored),
                           "skips_right": sum(1 for h in skipped if ((h.get("score") or {}).get("pnl_lot") or 0) <= 0), "skips": len(skipped)}}


# --- the page's views ---------------------------------------------------------------------------------------------------------

ACTIONS = {"new": ["take", "take_half", "skip", "pause"], "waiting": ["take", "take_half", "skip", "pause"],
           "paused": ["take", "skip", "resume"], "active": ["take", "take_half", "skip"]}


def _app_config():
    """The running app's config (the page's views), else the defaults (Kite orders off)."""
    try:
        from backend.api import routes
        if routes._state is not None:
            return routes._state.config
    except Exception:
        pass
    from backend.config import AppConfig
    return AppConfig()


def hint_view(h: dict, now: Optional[datetime] = None, config=None) -> dict:
    """One hint for the page (the keys the frontend reads). `kite`: the hint's Kite legs (⚡ Tips → Kite orders)."""
    from backend.timefmt import ampm
    from backend.tips import kite_orders as ko
    from backend.tips import vision as vs
    now = now or datetime.now(IST)
    run, pos = h.get("run") or {}, h.get("pos") if h.get("taken") else None
    posted = datetime.fromisoformat(h["posted_at"])
    st = h.get("status")
    acts = list(ACTIONS.get(st, []))
    if pos and pos.get("state") in ("armed", "open"):
        acts = ["exit", "trail"] + (["pause"] if run.get("state") == "waiting" else [])
    elif h.get("taken"):
        acts = []
    if not (h.get("plan") or {}).get("ok") or not h.get("tradingsymbol"):
        acts = [a for a in acts if a not in ("take", "take_half")]
    tg = (h.get("plan") or {}).get("targets") or h.get("targets") or []
    hits = len(((pos or {}).get("run") or run).get("hits") or [])
    exp = h.get("expiry")
    return {"id": h["id"], "channel": h["channel"], "post_id": h["post_id"], "posted_at": h["posted_at"],
            "time": ampm(posted), "day": posted.date().isoformat(), "day_label": "today" if posted.date() == now.date() else f"{posted:%a %d %b}",
            "raw": h.get("raw") or "", "title": dc.title(h), "symbol": h.get("symbol"), "side": h.get("side"),
            "instrument": h.get("instrument"), "strike": h.get("strike"), "opt_type": h.get("opt_type"), "expiry": exp,
            "expiry_label": (f"{date.fromisoformat(exp):%a} {date.fromisoformat(exp).day} {date.fromisoformat(exp):%b}" if exp else None),
            "expiry_src": h.get("expiry_src"), "tradingsymbol": h.get("tradingsymbol"), "exchange": h.get("exchange"),
            "lot_size": h.get("lot_size"), "entry": h.get("entry"), "entry_text": pl.entry_text(h.get("entry") or {}),
            "stop": h.get("stop"), "stop_text": pl.stop_text(h.get("stop") or {}), "targets": h.get("targets") or [],
            "targets_hit": hits, "targets_text": pl.targets_text(tg, bool(h.get("open_ended"))), "open_ended": bool(h.get("open_ended")),
            "size_hint": h.get("size_hint"), "decoded": h.get("decoded") or {}, "plan": h.get("plan"), "status": st, "ended": h.get("ended"),
            "taken": bool(h.get("taken")), "lots": h.get("lots"), "entry_px": h.get("entry_px"), "exit_px": h.get("exit_px"),
            "pnl": h.get("pnl"), "ltp": run.get("last_ltp"), "events": h.get("events") or [], "tracked": bool(h.get("tracked")),
            "untracked_why": h.get("untracked_why"), "score": h.get("score"),
            "position": ({"state": pos.get("state"), "lots": pos.get("lots"), "half": pos.get("half"), "qty": pos.get("qty"),
                          "qty_open": bk.open_qty(pos), "entry_px": pos.get("entry_px"), "stop": pos["run"].get("stop"),
                          "spot_stop": pos["run"].get("spot_stop"), "trail": pos["run"].get("trail"), "realised": pos.get("realised"),
                          "charges": pos.get("charges"), "ended": pos.get("ended"), "override": pos.get("override"),
                          "booked": pos.get("booked") or [], "who": pos.get("who"), "size_src": pos.get("size_src")} if pos else None),
            "actions": acts, "telegram": f"take {h['id']} · take {h['id']} 2 · skip {h['id']}", "source": h.get("source"),
            "kite": ko.hint_kite(config or _app_config(), h), "shot": vs.shot_view(h)}


def feed_rows(kind: Optional[str] = None, limit: int = 50, channel: Optional[str] = None) -> list[dict]:
    from backend.timefmt import ampm
    from backend.tips import vision as vs
    where, args = [], []
    if kind:
        ks = [k.strip() for k in kind.split(",") if k.strip()]
        where.append(f"kind IN ({','.join('?' * len(ks))})")
        args += ks
    if channel:
        where.append("channel = ?")
        args.append(channel.lower())
    q = "SELECT * FROM tips_posts" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY posted_at DESC, id DESC LIMIT ?"
    with get_db() as conn:
        rs = [dict(r) for r in conn.execute(q, (*args, int(limit)))]
    out = []
    for r in rs:
        k = r["kind"]
        label = {"hint": f"hint #{r['hint_id']}" if r["hint_id"] else "hint", "follow_up": f"follow-up #{r['hint_id']}" if r["hint_id"] else "follow-up",
                 "image": "image", "promo": "promo", "chatter": "chatter"}[k]
        if k == "follow_up":
            a = pl.followup_action(r["text"]).get("action")
            if a == "avoid":
                label = "follow-up · avoid"
        try:                                                 # 📷 the screenshot's read (kept forever; the file goes after 2 days)
            ocr = json.loads(r.get("ocr") or "null") or {}
        except (TypeError, ValueError):
            ocr = {}
        if k == "image" and r["hint_id"]:
            label = f"screenshot #{r['hint_id']}"
        out.append({"id": r["id"], "channel": r["channel"], "post_id": r["post_id"], "posted_at": r["posted_at"],
                    "time": ampm(datetime.fromisoformat(r["posted_at"])), "text": r["text"] if r["text"] or not r["has_media"] else "",
                    "has_media": bool(r["has_media"]), "kind": k, "hint_id": r["hint_id"], "classified_by": r["classified_by"],
                    "ai": r["classified_by"] == "ai", "label": label, "edited": bool(r["edited_at"]), "deleted": bool(r["deleted_at"]),
                    "source": r["source"], "ocr_line": vs.first_line(ocr) if not ocr.get("error") else None,
                    "ocr_error": ocr.get("error"), "ocr_facts": ocr.get("facts") or None})
    return out


def hints_list(status: Optional[str] = None, days: Optional[int] = None, channel: Optional[str] = None, now: Optional[datetime] = None,
               limit: int = 200) -> list[dict]:
    now = now or datetime.now(IST)
    where, args = [], []
    if status:
        ss = [s.strip() for s in status.split(",") if s.strip()]
        where.append(f"status IN ({','.join('?' * len(ss))})")
        args += ss
    if days:
        where.append("posted_at >= ?")
        args.append((now - timedelta(days=int(days))).isoformat(timespec="seconds"))
    if channel:
        where.append("channel = ?")
        args.append(channel.lower())
    return [hint_view(h, now) for h in bk.rows(" AND ".join(where), tuple(args), limit=limit)]


def summary(config, now: Optional[datetime] = None) -> dict:
    """GET /api/tips: {enabled, reader, channels, cards {open, book, channel30, you_vs_all}, hints, feed, record, book,
    settings, quotes_at, any_open, at, kite (⚡ Tips → Kite orders: mode, kill, caps, open)}."""
    from backend.tips import kite_orders as ko
    from backend.tips import reader as rd
    now = now or datetime.now(IST)
    today = now.date().isoformat()
    open_h = bk.rows("status IN ('new', 'waiting', 'active', 'paused') OR (taken = 1 AND json_extract(pos, '$.state') IN ('armed', 'open'))")
    today_h = bk.rows("posted_at >= ? AND channel != 'rehearsal'", (today,))
    last = bk.rows(limit=20)
    seen, hs = set(), []
    for h in open_h + today_h + last:
        if h["id"] not in seen:
            seen.add(h["id"])
            hs.append(h)
    views = [hint_view(h, now, config) for h in hs]
    rec = record(30, None, now)
    book = bk.view(config, now)
    with get_db() as conn:
        posts_today = conn.execute("SELECT kind, COUNT(*) FROM tips_posts WHERE posted_at >= ? GROUP BY kind", (today,)).fetchall()
        quotes_at = get_setting(conn, "tips.quotes_at") or None
    kinds = {r[0]: r[1] for r in posts_today}
    active = [v for v in views if v["status"] == "active"]
    waiting = [v for v in views if v["status"] in ("new", "waiting")]
    paused = [v for v in views if v["status"] == "paused"]
    first = active[0] if active else (waiting[0] if waiting else None)
    today_real = sum(t["net"] or 0 for t in book["trades"] if (t.get("closed_at") or "")[:10] == today)
    cards = {
        "open": {"active": len(active), "new": len(waiting), "paused": len(paused),
                 "line": " · ".join([f"#{v['id']} {v['title'].split(' ', 1)[1]}" for v in active[:1]] + [f"#{v['id']} waiting" for v in waiting[:2]]),
                 "first": first["id"] if first else None,
                 "today": {"hints": kinds.get("hint", 0), "posts": sum(kinds.values()), "chatter": kinds.get("chatter", 0)}},
        "book": {"equity": book["equity"], "today": round(today_real + book["open_pnl"], 2), "lots_open": sum(p["lots"] for p in book["positions"]),
                 "since": book["since"], "taken": book["taken"], "won": book["won"], "capital": book["capital"]},
        "channel30": {**{k: rec["totals"][k] for k in ("hints", "t1_first", "stop_first", "no_fill", "avg_r", "best_hour", "best_time")}},
        "you_vs_all": rec["you_vs_all"],
    }
    reader = rd.status()
    reader["channels"] = rd.channels_of(config)
    return {"enabled": bool(config.tips.enabled), "reader": reader, "channels": rd.channels_of(config), "cards": cards, "hints": views,
            "feed": feed_rows(limit=50), "record": rec, "book": book, "settings": asdict(config.tips),
            "quotes_at": quotes_at, "any_open": bool(open_h), "at": now.isoformat(timespec="seconds"), "kite": ko.lite(config, now)}


def health_rows(config) -> list[dict]:
    """🩺 tips_reader (live / preview / down / logged out) and tips_quotes (the last quote while a hint is open)."""
    from backend.tips import reader as rd
    on = bool(config.tips.enabled)
    st = rd.status()
    s = st["state"]
    with get_db() as conn:
        qat = get_setting(conn, "tips.quotes_at") or None
        n_open = conn.execute("SELECT COUNT(*) FROM tips_hints WHERE status IN ('new', 'waiting', 'active', 'paused')").fetchone()[0]
    off = "" if on else "Tips off · "
    rows = [{"key": "tips_reader", "name": "Tips reader", "ok": (not on) or s in ("live", "preview"), "warn": on and s == "preview" or (on and s == "logged_out"),
             "state": off + s + (f" · {st['reason']}" if st.get("reason") and s != "live" else ""),
             "detail": (f"{st.get('account_masked') or 'your account'} · " if s == "live" else "") + "channels: " + ", ".join(rd.channels_of(config)),
             "at": st.get("last_post_at")}]
    age = None
    if qat:
        try:
            age = (datetime.now(IST) - datetime.fromisoformat(qat)).total_seconds()
        except ValueError:
            age = None
    from backend.engine.overview import market_open
    stale = bool(n_open and market_open() and (age is None or age > 120))
    rows.append({"key": "tips_quotes", "name": "Tips quotes", "ok": True, "warn": stale,
                 "state": f"{n_open} open · last quote {round(age)} s ago" if age is not None and n_open else (f"{n_open} open · no quote yet" if n_open else "nothing open"),
                 "detail": "every 5 s while a hint is open or waiting · needs the market-data login", "at": qat})
    from backend.tips import kite_orders as ko
    rows.append(ko.health_row(config))                         # ⚡ tips_kite: mode, open orders, last sync, kill switch
    return rows
