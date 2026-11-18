"""⚡ Tips → Kite orders (PR A: click mode). The Tips paper book stays as it is; on top, the owner can send a hint's legs
to Kite (the app's own Kite Connect session) by button click, each click through a confirm on the page.

  legs      entry (MARKET, the hint's side) · stop (SL-M at the plan's premium stop) · T1 (LIMIT at target 1, half the
            quantity when the plan books half at T1 and the size has 2+ lots) · T2 (LIMIT at target 2 for the rest,
            placed only once T1 has filled: a sell for more than is held is refused). Targets past the second stay in
            the paper record only. A spot-based stop is not placeable on Kite: the app watches it, you exit by hand.
  Place all entry now + `auto_rest`: the moment the entry fills the sync places the stop and T1 (by "app"); when T1
            fills the stop moves to cost and T2 is placed; when the stop or the last target fills the leftovers are
            cancelled. Those are the consequences of the owner's click (the confirm says so). Nothing else is automatic.
  guards    logged in · market hours · the entry window · mode not off · kill switch off · entries today under the cap
            · lots under the cap · today's realised loss short of the loss cap · a complete plan with a contract and a
            lot size · one Kite position per hint (never averaged, never re-entered after a fill) · the hint still open.
  only ours every order carries the tag "bptips"; the sync reads the order book once and keeps our order ids only.
            Holdings and other positions are never read. Nothing on the broker side is ever deleted.
  storage   tips_orders (one row per order, never deleted) and the hint's `kite` JSON (its own UPDATE, so the quotes
            poll saving a hint never overwrites it).
Mode `auto` arrives in the next update: for now it behaves exactly like click."""

import json
import threading
from datetime import datetime, time
from typing import Optional

from backend.config import IST
from backend.database.models import get_db, get_setting, now_ist, set_setting
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm
from backend.tips import book as bk
from backend.tips import plan as pl
from backend.tips.book import TipsError

logger = setup_logger("tips")

KILL_KEY = "tips.kite.kill"
SYNC_KEY = "tips.kite.sync_at"
TAG = "bptips"
LIVE = ("placed", "open")                      # an order still working on Kite
SYNC_FAST_S, SYNC_IDLE_S = 10, 30
LEG_NAMES = {"entry": "Entry", "stop": "Stop", "t1": "Target 1", "t2": "Target 2", "exit": "Exit"}
WARNING = ("Real money on your Kite account. The app never averages, never re-enters after a stop, and stops for the day "
           "at the loss cap.")
_LOCK = threading.RLock()                      # the sync thread and the page's clicks never interleave


# --- small helpers -----------------------------------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(IST)


def killed() -> bool:
    with get_db() as conn:
        return get_setting(conn, KILL_KEY) == "1"


def mode(config) -> str:
    return str(getattr(config.tips, "kite_mode", "off") or "off")


def _hm(s: str) -> time:
    return pl.hm(s)


def window_text(config) -> str:
    return f"{ampm(config.tips.kite_entry_from)} – {ampm(config.tips.kite_entry_until)}"


def t12(iso: Optional[str], seconds: bool = False) -> str:
    """'10:07 AM' / '10:07:14 AM' (12-hour, IST) from a stored ISO time."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    if dt.tzinfo is not None:
        dt = dt.astimezone(IST)
    if not seconds:
        return ampm(dt)
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d}:{dt.second:02d} {'AM' if dt.hour < 12 else 'PM'}"


def rupees(v: Optional[float], sign: bool = False) -> str:
    return bk.inr(v, sign=sign)


def _sign(h: dict) -> int:
    return -1 if h.get("side") == "SELL" else 1


def entry_side(h: dict) -> str:
    return "SELL" if h.get("side") == "SELL" else "BUY"


def exit_side(h: dict) -> str:
    return "BUY" if h.get("side") == "SELL" else "SELL"


def new_kite() -> dict:
    return {"state": "none", "lots": 0, "qty": 0, "qty_open": 0, "entry_px": None, "stop_px": None, "auto_rest": False,
            "realised": 0.0, "reason": None, "legs": {}, "log": [], "by": None, "day": None}


def kstate(h: dict) -> dict:
    k = h.get("kite")
    if not isinstance(k, dict):
        k = new_kite()
    for key, v in new_kite().items():
        k.setdefault(key, v)
    h["kite"] = k
    return k


def save_kite(h: dict, conn=None) -> None:
    if conn is None:
        with get_db() as c:
            return save_kite(h, c)
    conn.execute("UPDATE tips_hints SET kite = ? WHERE id = ?", (json.dumps(h.get("kite")), h["id"]))


def _log(k: dict, now: datetime, text: str) -> None:
    k["log"].append({"t": now.isoformat(timespec="seconds"), "text": text, "src": "kite"})
    k["log"] = k["log"][-40:]


def _push(config, key: str, text: str) -> None:
    bk.push(config, key, text)


def _row(r) -> dict:
    return dict(r)


def rows_for(hid: int, conn=None) -> list[dict]:
    if conn is None:
        with get_db() as c:
            return rows_for(hid, c)
    return [_row(r) for r in conn.execute("SELECT * FROM tips_orders WHERE hint_id = ? ORDER BY id", (hid,))]


def rows_for_hints(hids: list, conn=None) -> list[dict]:
    if not hids:
        return []
    if conn is None:
        with get_db() as c:
            return rows_for_hints(hids, c)
    return [_row(r) for r in conn.execute(f"SELECT * FROM tips_orders WHERE hint_id IN ({','.join('?' * len(hids))}) ORDER BY id", hids)]


def rows_today(day: str, conn=None) -> list[dict]:
    if conn is None:
        with get_db() as c:
            return rows_today(day, c)
    return [_row(r) for r in conn.execute("SELECT * FROM tips_orders WHERE day = ? ORDER BY id", (day,))]


def live_rows(conn=None) -> list[dict]:
    if conn is None:
        with get_db() as c:
            return live_rows(c)
    return [_row(r) for r in conn.execute("SELECT * FROM tips_orders WHERE status IN ('placed', 'open') ORDER BY id")]


def _insert(row: dict, conn=None) -> int:
    if conn is None:
        with get_db() as c:
            return _insert(row, c)
    cols = ("hint_id", "leg", "side", "qty", "order_type", "price", "trigger", "status", "order_id", "filled_px", "filled_qty",
            "placed_at", "updated_at", "by", "note", "day")
    cur = conn.execute(f"INSERT INTO tips_orders ({', '.join(chr(34) + c + chr(34) for c in cols)}) VALUES ({','.join('?' * len(cols))})",
                       tuple(row.get(c) for c in cols))
    return cur.lastrowid


def _update(rid: int, conn=None, **kw) -> None:
    if conn is None:
        with get_db() as c:
            return _update(rid, c, **kw)
    kw["updated_at"] = now_ist()
    conn.execute(f"UPDATE tips_orders SET {', '.join(chr(34) + k + chr(34) + ' = ?' for k in kw)} WHERE id = ?", (*kw.values(), rid))


def entries_today(day: str) -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM tips_orders WHERE day = ? AND leg = 'entry' AND status NOT IN ('rejected', 'failed')",
                            (day,)).fetchone()[0]


def realised_by_hint(rows: list[dict]) -> dict:
    """{hint_id: realised ₹} from fills only: each exit-side fill against its hint's entry fill (charges not included)."""
    by: dict = {}
    entry = {r["hint_id"]: r for r in rows if r["leg"] == "entry" and r["status"] == "complete" and r.get("filled_px") is not None}
    for r in rows:
        if r["leg"] == "entry" or r["status"] != "complete" or r.get("filled_px") is None:
            continue
        e = entry.get(r["hint_id"])
        if not e:
            continue
        sign = 1 if e["side"] == "BUY" else -1
        by[r["hint_id"]] = round(by.get(r["hint_id"], 0.0) + (r["filled_px"] - e["filled_px"]) * int(r.get("filled_qty") or r["qty"] or 0) * sign, 2)
    return by


def realised_today(day: str) -> float:
    with get_db() as conn:
        hids = [r[0] for r in conn.execute("SELECT DISTINCT hint_id FROM tips_orders WHERE day = ?", (day,))]
        rows = [_row(r) for r in conn.execute(f"SELECT * FROM tips_orders WHERE hint_id IN ({','.join('?' * len(hids))})", hids)] if hids else []
    by = realised_by_hint(rows)
    # only exits done today count against today's cap
    today_ids = {r["hint_id"] for r in rows if r["leg"] != "entry" and r["status"] == "complete" and r.get("day") == day}
    return round(sum(v for k, v in by.items() if k in today_ids), 2)


# --- sizes and the planned legs --------------------------------------------------------------------------------------------

def default_lots(config, h: dict) -> int:
    sh = h.get("size_hint") or {}
    lot = int(h.get("lot_size") or 0) or 1
    if sh.get("lots"):
        return int(sh["lots"])
    if sh.get("qty"):
        return max(1, int(sh["qty"]) // lot)
    return int(config.tips.lots)


def split(h: dict, lots: int) -> tuple[int, int]:
    """(T1 qty, T2 qty) for `lots`: half at T1 in whole lots when the plan books half at T1 and there are 2+ lots and two
    targets; otherwise everything at T1 (T2 0)."""
    lot = int(h.get("lot_size") or 1)
    plan = h.get("plan") or {}
    tg = plan.get("targets") or []
    qty = lots * lot
    if plan.get("partial_at_t1") and len(tg) > 1 and lots >= 2:
        t1 = (lots // 2) * lot
        return t1, qty - t1
    return qty, 0


def premium_stop(h: dict) -> Optional[float]:
    st = (h.get("plan") or {}).get("stop") or {}
    return float(st["value"]) if st.get("kind") == "premium" and st.get("value") is not None else None


def _ref_px(h: dict, k: dict) -> Optional[float]:
    if k.get("entry_px") is not None:
        return k["entry_px"]
    ltp = bk.last_ltp(h)
    if ltp is not None:
        return ltp
    e = h.get("entry") or {}
    return e.get("hi") or e.get("lo")


# --- the guards ----------------------------------------------------------------------------------------------------------

def _g(ok: bool, text: str, key: str) -> dict:
    return {"ok": bool(ok), "text": text, "key": key}


def market_open(exchange: Optional[str], now: datetime) -> bool:
    o, c = pl.session(exchange)
    return pl.trading_day(now.date(), exchange) and o <= now.time() < c


def base_guards(config, client, now: datetime, exchange: Optional[str] = None) -> list[dict]:
    """The guards that do not depend on a hint (the page's Guards panel)."""
    c = config.tips
    day = now.date().isoformat()
    n = entries_today(day)
    used = realised_today(day)
    m = mode(config)
    exch = exchange or "NFO"
    seg = "MCX" if exch == "MCX" else "BFO / NFO"
    return [
        _g(client is not None and bool(getattr(client, "has_token", False)), "Kite logged in · orders go through the app's own session", "login"),
        _g(market_open(exchange, now), f"market hours · {seg} open" if market_open(exchange, now) else f"market hours · {seg} closed now", "market"),
        _g(_hm(c.kite_entry_from) <= now.time() < _hm(c.kite_entry_until), f"entries only {window_text(config)}", "window"),
        _g(m != "off", "Kite orders on · " + {"click": "by your click", "auto": "auto (works as by your click for now)"}.get(m, "off"), "mode"),
        _g(not killed(), "kill switch off" if not killed() else "kill switch ON · no new order", "kill"),
        _g(n < int(c.kite_max_entries_day), f"entries today {n} of {int(c.kite_max_entries_day)}", "entries"),
        _g(used > -float(c.kite_loss_cap), f"loss today {rupees(min(used, 0.0))} of the {rupees(float(c.kite_loss_cap))} cap", "loss"),
    ]


def guards(config, client, h: dict, lots: Optional[int], now: Optional[datetime] = None, action: str = "entry") -> list[dict]:
    """Every guard for `action` on hint `h` ([{ok, text, key}]); the first failing one blocks it (409). An entry checks
    them all; the stop / targets / rest need the login, the market, the mode and the kill switch off; an exit or a
    cancel only the login (getting out is never blocked by a cap)."""
    now = now or _now()
    c = config.tips
    k = kstate(h)
    base = base_guards(config, client, now, h.get("exchange"))
    if action in ("exit", "cancel"):
        return [g for g in base if g["key"] == "login"]
    if action in ("stop", "targets", "rest", "modify_stop"):
        keep = ("login", "market", "mode") + (("kill",) if action != "modify_stop" else ())
        out = [g for g in base if g["key"] in keep]
        out.append(_g(k["state"] == "entered" and k.get("qty_open", 0) > 0, "the entry has filled on Kite" if k["state"] == "entered"
                      else "needs a filled entry on Kite first", "filled"))
        if action in ("stop", "rest"):
            out.append(_g(premium_stop(h) is not None, "a premium stop" if premium_stop(h) is not None
                          else "spot stop: watched by the app, exit by hand", "premium"))
        return out
    lots = int(lots or 0)
    plan = h.get("plan") or {}
    ok_plan = bool(plan.get("ok")) and bool(h.get("tradingsymbol")) and bool(h.get("lot_size")) and bool(plan.get("targets"))
    live = k["state"] in ("armed", "entered")
    filled_before = any(r["leg"] == "entry" and r["status"] == "complete" for r in rows_for(h["id"]))
    out = list(base)
    out.append(_g(1 <= lots <= int(c.kite_max_lots), f"lots per order {lots} ≤ {int(c.kite_max_lots)}" if lots <= int(c.kite_max_lots)
                  else f"lots per order {lots} > the cap {int(c.kite_max_lots)}", "lots"))
    out.append(_g(ok_plan, "a complete plan (stop + target), a contract and a lot size" if ok_plan else
                  "no complete plan: " + (plan.get("why") or ("no contract" if not h.get("tradingsymbol") else "no target" if not plan.get("targets") else "no lot size")), "plan"))
    out.append(_g(not live and not filled_before, "one Kite position per hint · never averaged · never re-entered after a stop" if not (live or filled_before)
                  else ("already on Kite · never averaged" if live else "already traded on Kite · never re-entered"), "one"))
    out.append(_g(h.get("status") not in ("closed", "expired"), "the hint is still open" if h.get("status") not in ("closed", "expired")
                  else f"the hint is {h.get('status')}", "open"))
    return out


def _check(gs: list[dict]) -> None:
    bad = next((g for g in gs if not g["ok"]), None)
    if bad:
        raise TipsError(409, bad["text"])


def _client_ok(client) -> None:
    if client is None or not getattr(client, "has_token", False):
        raise TipsError(409, "Kite is not logged in")


# --- placing -------------------------------------------------------------------------------------------------------------

def _place(config, client, h: dict, leg: str, side: str, qty: int, order_type: str, now: datetime, by: str,
           price: Optional[float] = None, trigger: Optional[float] = None, conn=None) -> dict:
    """One order to Kite and its row. A refusal is stored (status failed, Kite's words) and raised as 409."""
    k = kstate(h)
    row = {"hint_id": h["id"], "leg": leg, "side": side, "qty": int(qty), "order_type": order_type, "price": price, "trigger": trigger,
           "status": "placed", "order_id": None, "placed_at": now.isoformat(timespec="seconds"), "updated_at": now.isoformat(timespec="seconds"),
           "by": by, "note": None, "day": now.date().isoformat()}
    try:
        oid = client.place_order(exchange=h["exchange"], tradingsymbol=h["tradingsymbol"], transaction_type=side, quantity=int(qty),
                                 product=config.tips.kite_product, order_type=order_type, price=price, trigger_price=trigger, tag=TAG,
                                 market_protection=float(config.tips.kite_market_protection) if order_type in ("MARKET", "SL-M") else None)
    except Exception as exc:
        row.update(status="failed", note=str(exc)[:200])
        row["id"] = _insert(row, conn)
        k["legs"][leg] = _leg_of(row)
        _log(k, now, f"Kite: {LEG_NAMES[leg].lower()} refused · {str(exc)[:120]}")
        raise TipsError(409, f"Kite refused the {LEG_NAMES[leg].lower()}: {str(exc)[:160]}")
    row["order_id"] = str(oid)
    row["id"] = _insert(row, conn)
    k["legs"][leg] = _leg_of(row)
    return row


def _leg_of(row: dict) -> dict:
    return {"row": row.get("id"), "order_id": row.get("order_id"), "status": row.get("status"), "qty": row.get("qty"), "side": row.get("side"),
            "type": row.get("order_type"), "price": row.get("price"), "trigger": row.get("trigger"), "filled_px": row.get("filled_px"),
            "filled_qty": row.get("filled_qty"), "at": row.get("placed_at"), "by": row.get("by"), "note": row.get("note")}


def _type_text(order_type: str, price, trigger) -> str:
    if order_type in ("SL-M", "SL"):
        return f"{order_type} {pl.fmt(trigger)}"
    if order_type == "LIMIT":
        return f"LIMIT {pl.fmt(price)}"
    return order_type or "—"


def place_entry(config, client, hid: int, lots: Optional[int] = None, by: str = "click", now: Optional[datetime] = None,
                auto_rest: bool = False, notify: bool = True) -> dict:
    """MARKET entry (BUY; a SELL hint sells) for `lots` (default: the post's size, else tips.lots). Every guard first."""
    now = now or _now()
    with _LOCK:
        h = bk._get(hid)
        k = kstate(h)
        n = int(lots) if lots is not None else default_lots(config, h)
        _check(guards(config, client, h, n, now, "entry"))
        qty = n * int(h["lot_size"])
        try:
            _place(config, client, h, "entry", entry_side(h), qty, "MARKET", now, by)
        finally:
            save_kite(h)
        k.update(state="armed", lots=n, qty=qty, qty_open=0, entry_px=None, stop_px=premium_stop(h), auto_rest=bool(auto_rest),
                 reason=None, by=by, day=now.date().isoformat())
        for leg in ("stop", "t1", "t2", "exit"):
            k["legs"].pop(leg, None)
        text = f"Kite: entry placed {entry_side(h)} {qty} MARKET ({'your click' if by == 'click' else by})" + \
            (" · SL + T1 the moment it fills" if auto_rest else "")
        _log(k, now, text)
        save_kite(h)
        if notify:
            _push(config, f"tips|kite|{hid}|entry|{now.isoformat(timespec='seconds')}",
                  f"⚡ Kite · #{hid} entry placed {entry_side(h)} {qty} MARKET" + (" · SL + T1 on fill" if auto_rest else ""))
        return h


def place_all(config, client, hid: int, lots: Optional[int] = None, now: Optional[datetime] = None) -> dict:
    """Entry now; the stop and T1 the moment the entry fills (the sync, by "app")."""
    return place_entry(config, client, hid, lots, "click", now, auto_rest=True)


def _place_stop(config, client, h: dict, now: datetime, by: str, conn=None) -> Optional[dict]:
    k = kstate(h)
    if premium_stop(h) is None:
        raise TipsError(409, "spot stop: watched by the app, exit by hand")
    leg = k["legs"].get("stop")
    if leg and leg.get("status") in LIVE:
        raise TipsError(409, "the stop is already on Kite")
    trig = k.get("stop_px") if k.get("stop_px") is not None else premium_stop(h)
    return _place(config, client, h, "stop", exit_side(h), k["qty_open"], "SL-M", now, by, trigger=trig, conn=conn)


def _place_t1(config, client, h: dict, now: datetime, by: str, conn=None) -> Optional[dict]:
    k = kstate(h)
    tg = (h.get("plan") or {}).get("targets") or []
    if not tg:
        raise TipsError(409, "no target in the plan")
    leg = k["legs"].get("t1")
    if leg and leg.get("status") in LIVE + ("complete",):
        raise TipsError(409, "target 1 is already on Kite")
    q1, _ = split(h, int(k["lots"]))
    q1 = min(q1, k["qty_open"])
    return _place(config, client, h, "t1", exit_side(h), q1, "LIMIT", now, by, price=float(tg[0]), conn=conn)


def _place_t2(config, client, h: dict, now: datetime, by: str, conn=None) -> Optional[dict]:
    k = kstate(h)
    tg = (h.get("plan") or {}).get("targets") or []
    t1 = k["legs"].get("t1") or {}
    if t1.get("status") != "complete":
        raise TipsError(409, "target 2 goes on Kite after target 1 fills (a sell for more than you hold is refused)")
    if len(tg) < 2 or k["qty_open"] <= 0:
        raise TipsError(409, "nothing left for target 2")
    leg = k["legs"].get("t2")
    if leg and leg.get("status") in LIVE + ("complete",):
        raise TipsError(409, "target 2 is already on Kite")
    return _place(config, client, h, "t2", exit_side(h), k["qty_open"], "LIMIT", now, by, price=float(tg[1]), conn=conn)


def _ready(config, client, h: dict, action: str, now: datetime) -> dict:
    if mode(config) == "off" and action not in ("exit", "cancel"):
        raise TipsError(409, "Kite orders are off (Settings › tips › kite_mode)")
    _check(guards(config, client, h, None, now, action))
    return kstate(h)


def place_stop(config, client, hid: int, now: Optional[datetime] = None, by: str = "click") -> dict:
    """SL-M at the plan's premium stop for the open quantity (needs a filled entry)."""
    now = now or _now()
    with _LOCK:
        h = bk._get(hid)
        k = _ready(config, client, h, "stop", now)
        try:
            r = _place_stop(config, client, h, now, by)
            _log(k, now, f"Kite: SL-M {pl.fmt(r['trigger'])} ×{r['qty']} placed ({'your click' if by == 'click' else by})")
        finally:
            save_kite(h)
        return h


def place_targets(config, client, hid: int, now: Optional[datetime] = None, by: str = "click") -> dict:
    """T1 when it is not on Kite yet; once T1 has filled, T2 for the rest ("Place now")."""
    now = now or _now()
    with _LOCK:
        h = bk._get(hid)
        k = _ready(config, client, h, "targets", now)
        t1 = k["legs"].get("t1") or {}
        try:
            if t1.get("status") == "complete":
                r = _place_t2(config, client, h, now, by)
                _log(k, now, f"Kite: T2 LIMIT {pl.fmt(r['price'])} ×{r['qty']} placed ({'your click' if by == 'click' else by})")
            else:
                r = _place_t1(config, client, h, now, by)
                _log(k, now, f"Kite: T1 LIMIT {pl.fmt(r['price'])} ×{r['qty']} placed ({'your click' if by == 'click' else by})")
        finally:
            save_kite(h)
        return h


def place_rest(config, client, hid: int, now: Optional[datetime] = None) -> dict:
    """The stop + T1 now (+ T2 when T1 has filled), whichever are not on Kite yet."""
    now = now or _now()
    with _LOCK:
        h = bk._get(hid)
        k = _ready(config, client, h, "rest", now)
        done = []
        try:
            if (k["legs"].get("stop") or {}).get("status") not in LIVE:
                r = _place_stop(config, client, h, now, "click")
                done.append(f"SL-M {pl.fmt(r['trigger'])} ×{r['qty']}")
            t1 = k["legs"].get("t1") or {}
            if t1.get("status") not in LIVE + ("complete",):
                r = _place_t1(config, client, h, now, "click")
                done.append(f"T1 LIMIT {pl.fmt(r['price'])} ×{r['qty']}")
            elif t1.get("status") == "complete" and (k["legs"].get("t2") or {}).get("status") not in LIVE + ("complete",) \
                    and len((h.get("plan") or {}).get("targets") or []) > 1 and k["qty_open"] > 0:
                r = _place_t2(config, client, h, now, "click")
                done.append(f"T2 LIMIT {pl.fmt(r['price'])} ×{r['qty']}")
            if not done:
                raise TipsError(409, "every leg is already on Kite")
            k["auto_rest"] = True
            _log(k, now, "Kite: " + " and ".join(done) + " placed (your click)")
        finally:
            save_kite(h)
        return h


def modify_stop(config, client, hid: int, to, now: Optional[datetime] = None) -> dict:
    """Move the stop on Kite: a price, or "cost" (the entry fill)."""
    now = now or _now()
    with _LOCK:
        h = bk._get(hid)
        k = _ready(config, client, h, "modify_stop", now)
        leg = k["legs"].get("stop") or {}
        if leg.get("status") not in LIVE:
            raise TipsError(409, "no stop on Kite to modify")
        if isinstance(to, str) and to.strip().lower() == "cost":
            px = k.get("entry_px")
        else:
            try:
                px = float(to)
            except (TypeError, ValueError):
                raise TipsError(400, 'modify the stop to a price, or "cost"')
        if px is None or px <= 0:
            raise TipsError(400, "the stop must be a positive price")
        ltp = bk.last_ltp(h)
        if ltp is not None and ((_sign(h) > 0 and px >= ltp) or (_sign(h) < 0 and px <= ltp)):
            raise TipsError(400, f"the stop {pl.fmt(px)} is on the wrong side of the price {pl.fmt(ltp)}")
        try:
            client.modify_order(leg["order_id"], trigger_price=px)
        except Exception as exc:
            raise TipsError(409, f"Kite refused the change: {str(exc)[:160]}")
        _update(leg["row"], trigger=px)
        leg["trigger"] = px
        k["stop_px"] = px
        _log(k, now, f"Kite: stop moved to {pl.fmt(px)} (your click)")
        save_kite(h)
        return h


def cancel(config, client, hid: int, leg: str, now: Optional[datetime] = None) -> dict:
    """Cancel one working leg (entry / stop / t1 / t2)."""
    now = now or _now()
    if leg not in ("entry", "stop", "t1", "t2"):
        raise TipsError(400, "leg must be entry, stop, t1 or t2")
    with _LOCK:
        h = bk._get(hid)
        k = _ready(config, client, h, "cancel", now)
        lg = k["legs"].get(leg) or {}
        if lg.get("status") not in LIVE:
            raise TipsError(409, f"no {LEG_NAMES[leg].lower()} working on Kite")
        _cancel_leg(client, h, leg, now)
        if leg == "entry":
            k.update(state="none", reason="entry cancelled", auto_rest=False)
        _log(k, now, f"Kite: {LEG_NAMES[leg].lower()} cancelled (your click)")
        save_kite(h)
        return h


def _cancel_leg(client, h: dict, leg: str, now: datetime, conn=None) -> bool:
    k = kstate(h)
    lg = k["legs"].get(leg) or {}
    if lg.get("status") not in LIVE:
        return False
    try:
        client.cancel_order(lg["order_id"])
    except Exception as exc:
        _log(k, now, f"Kite: cancel {LEG_NAMES[leg].lower()} refused · {str(exc)[:120]}")
        raise TipsError(409, f"Kite refused the cancel: {str(exc)[:160]}")
    lg["status"] = "cancelled"
    _update(lg["row"], conn, status="cancelled")
    return True


def exit_now(config, client, hid: int, now: Optional[datetime] = None, by: str = "click", notify: bool = True) -> dict:
    """Cancel the working stop / targets (or the unfilled entry), then a MARKET exit of the open quantity."""
    now = now or _now()
    with _LOCK:
        h = bk._get(hid)
        k = _ready(config, client, h, "exit", now)
        if k["state"] == "armed":
            _cancel_leg(client, h, "entry", now)
            k.update(state="none", reason="entry cancelled", auto_rest=False)
            _log(k, now, "Kite: the unfilled entry cancelled (exit)")
            save_kite(h)
            return h
        if k["state"] != "entered" or k["qty_open"] <= 0:
            raise TipsError(409, f"#{hid} has nothing open on Kite")
        if (k["legs"].get("exit") or {}).get("status") in LIVE:
            raise TipsError(409, "an exit is already working on Kite")
        try:
            for leg in ("stop", "t1", "t2"):
                _cancel_leg(client, h, leg, now)
            r = _place(config, client, h, "exit", exit_side(h), k["qty_open"], "MARKET", now, by)
            k["auto_rest"] = False
            _log(k, now, f"Kite: exit {r['side']} {r['qty']} MARKET ({'your click' if by == 'click' else by}) · stop and targets cancelled")
        finally:
            save_kite(h)
        if notify:
            _push(config, f"tips|kite|{hid}|exit|{now.isoformat(timespec='seconds')}", f"⚡ Kite · #{hid} exit {exit_side(h)} {k['qty_open']} MARKET · stop and targets cancelled")
        return h


def set_kill(config, client, on: bool, now: Optional[datetime] = None) -> dict:
    """The kill switch: on = no new order, every working order of ours cancelled, every open quantity exited at market.
    Off = new orders allowed again (nothing is placed by turning it off)."""
    now = now or _now()
    with get_db() as conn:
        set_setting(conn, KILL_KEY, "1" if on else "0")
    out = {"kill": bool(on), "cancelled": 0, "exits": 0, "errors": []}
    if not on:
        return out
    with _LOCK:
        hids = sorted({r["hint_id"] for r in live_rows()} | {h["id"] for h in bk.rows("json_extract(kite, '$.state') IN ('armed', 'entered')")})
        for hid in hids:
            h = bk.load(hid)
            if not h:
                continue
            k = kstate(h)
            try:
                for leg in ("entry", "stop", "t1", "t2"):
                    if client is not None and _cancel_leg(client, h, leg, now):
                        out["cancelled"] += 1
                if k["state"] == "armed":
                    k.update(state="none", reason="kill switch", auto_rest=False)
                if k["state"] == "entered" and k["qty_open"] > 0 and (k["legs"].get("exit") or {}).get("status") not in LIVE and client is not None:
                    _place(config, client, h, "exit", exit_side(h), k["qty_open"], "MARKET", now, "click")
                    out["exits"] += 1
                k["auto_rest"] = False
                _log(k, now, "Kite: kill switch · working orders cancelled · open quantity exited at market")
            except TipsError as exc:
                out["errors"].append(f"#{hid}: {exc}")
            finally:
                save_kite(h)
    _push(config, f"tips|kite|kill|{now.isoformat(timespec='seconds')}",
          f"⛔ Kite · kill switch ON · {out['cancelled']} orders cancelled · {out['exits']} exits at market" + (f" · {len(out['errors'])} errors" if out["errors"] else ""))
    return out


# --- the sync ------------------------------------------------------------------------------------------------------------

def kite_status(s: str) -> str:
    s = (s or "").upper()
    if s == "COMPLETE":
        return "complete"
    if s == "REJECTED":
        return "rejected"
    if s.startswith("CANCELLED"):
        return "cancelled"
    return "open"


def sync(config, client, now: Optional[datetime] = None, notify: bool = True) -> dict:
    """Read the order book ONCE, keep our order ids, update the rows and each hint's legs, and take the consequences of
    the owner's click (stop + T1 on the entry fill with Place all / Place rest, stop to cost + T2 on T1, leftovers
    cancelled when the position closes). {orders, changed, lines}."""
    now = now or _now()
    out = {"orders": 0, "changed": 0, "lines": []}
    with _LOCK:
        live = live_rows()
        if not live or client is None or not getattr(client, "has_token", False):
            return out
        book = client.orders() or []
        mine = {str(r["order_id"]) for r in live if r.get("order_id")}
        ours = {str(o.get("order_id")): o for o in book if str(o.get("order_id")) in mine}
        out["orders"] = len(ours)
        by_hint: dict = {}
        for r in live:
            o = ours.get(str(r.get("order_id")))
            if not o:
                continue
            st = kite_status(o.get("status"))
            fpx = o.get("average_price") or None
            fq = int(o.get("filled_quantity") or 0) or None
            if st == r["status"]:
                continue
            note = (o.get("status_message") or None) if st == "rejected" else r.get("note")
            _update(r["id"], status=st, filled_px=float(fpx) if fpx else None, filled_qty=fq, note=note)
            r.update(status=st, filled_px=float(fpx) if fpx else None, filled_qty=fq, note=note)
            by_hint.setdefault(r["hint_id"], []).append(r)
            out["changed"] += 1
        for hid, rs in by_hint.items():
            h = bk.load(hid)
            if not h:
                continue
            try:
                lines = _apply(config, client, h, rs, now)
            except Exception:
                logger.exception("Tips Kite sync #%s failed", hid)
                lines = []
            save_kite(h)
            for i, ln in enumerate(lines):
                out["lines"].append(ln)
                if notify:
                    _push(config, f"tips|kite|{hid}|{now.isoformat(timespec='seconds')}|{i}", ln)
    with get_db() as conn:
        set_setting(conn, SYNC_KEY, now.isoformat(timespec="seconds"))
    return out


def _try(fn, *a) -> Optional[dict]:
    try:
        return fn(*a)
    except TipsError as exc:
        logger.warning("Tips Kite follow-on order refused: %s", exc)
        return None


def _apply(config, client, h: dict, rs: list[dict], now: datetime) -> list[str]:
    """One hint's changed rows → its legs, the follow-on orders, the Telegram lines."""
    k = kstate(h)
    hid = h["id"]
    lines = []
    for r in rs:
        leg = r["leg"]
        lg = k["legs"].get(leg)
        if lg is None or lg.get("row") != r["id"]:
            lg = _leg_of(r)
            k["legs"][leg] = lg
        lg.update(status=r["status"], filled_px=r.get("filled_px"), filled_qty=r.get("filled_qty"), note=r.get("note"))
        if r["status"] == "rejected":
            if leg == "entry":
                k.update(state="none", reason="entry rejected", auto_rest=False)
            msg = r.get("note") or "no reason given"
            _log(k, now, f"Kite: {LEG_NAMES[leg].lower()} REJECTED · {msg}")
            lines.append(f"⚠️ Kite · #{hid} {LEG_NAMES[leg].lower()} REJECTED · {msg}")
            continue
        if r["status"] == "cancelled":
            if leg == "entry" and k["state"] == "armed":
                k.update(state="none", reason="entry cancelled", auto_rest=False)
            _log(k, now, f"Kite: {LEG_NAMES[leg].lower()} cancelled")
            continue
        if r["status"] != "complete":
            continue
        fpx, fq = r.get("filled_px"), int(r.get("filled_qty") or r["qty"])
        if leg == "entry":
            k.update(state="entered", entry_px=fpx, qty_open=fq, qty=fq)
            text = f"⚡ Kite · #{hid} entry filled {pl.fmt(fpx)} ×{fq}"
            if k.get("auto_rest") and not killed():
                bits = []
                s = _try(_place_stop, config, client, h, now, "app")
                if s:
                    bits.append(f"SL {pl.fmt(s['trigger'])}")
                t = _try(_place_t1, config, client, h, now, "app")
                if t:
                    bits.append(f"T1 {pl.fmt(t['price'])} ×{t['qty']}")
                if bits:
                    text += " · " + " + ".join(bits) + " placed"
                elif premium_stop(h) is None:
                    text += " · spot stop: watched by the app, exit by hand"
            _log(k, now, text.replace(f"⚡ Kite · #{hid} ", "Kite: "))
            lines.append(text)
            continue
        # an exit-side fill
        k["qty_open"] = max(0, int(k["qty_open"]) - fq)
        pnl = round((fpx - (k.get("entry_px") or fpx)) * fq * _sign(h), 2)
        k["realised"] = round(float(k.get("realised") or 0.0) + pnl, 2)
        name = {"stop": "stop", "t1": "T1", "t2": "T2", "exit": "exit"}[leg]
        text = f"⚡ Kite · #{hid} {name} filled {pl.fmt(fpx)} ×{fq}"
        if leg == "t1" and k["qty_open"] > 0:
            st = k["legs"].get("stop") or {}
            if st.get("status") in LIVE and k.get("entry_px") is not None:
                try:
                    client.modify_order(st["order_id"], trigger_price=k["entry_px"], quantity=k["qty_open"])
                    _update(st["row"], trigger=k["entry_px"], qty=k["qty_open"])
                    st.update(trigger=k["entry_px"], qty=k["qty_open"])
                    k["stop_px"] = k["entry_px"]
                    text += f" · stop → cost {pl.fmt(k['entry_px'])}"
                except Exception as exc:
                    text += f" · stop to cost refused: {str(exc)[:80]}"
            t2 = _try(_place_t2, config, client, h, now, "app") if not killed() else None
            if t2:
                text += f" · T2 {pl.fmt(t2['price'])} ×{t2['qty']} placed"
        else:
            text += f" · {rupees(pnl, sign=True)}"
        if k["qty_open"] <= 0:
            left = []
            for other in ("stop", "t1", "t2"):
                if other != leg and (k["legs"].get(other) or {}).get("status") in LIVE:
                    try:
                        _cancel_leg(client, h, other, now)
                        left.append(other)
                    except TipsError:
                        pass
            if left:
                text += " · " + ("targets cancelled" if leg == "stop" else "stop cancelled" if left == ["stop"] else "leftovers cancelled")
            k.update(state="closed", reason={"stop": "stop", "t1": "target 1", "t2": "target 2", "exit": "your exit"}[leg], auto_rest=False)
            text += f" · closed {rupees(k['realised'], sign=True)} total" if leg != "stop" else ""
        _log(k, now, text.replace(f"⚡ Kite · #{hid} ", "Kite: "))
        lines.append(text)
    return lines


class Syncer:
    """The order sync in its own thread: every 10 s while the market is open and an order of ours is working, else a
    cheap look every 30 s (one DB query). Never raises out of its loop; never on the request path."""

    def __init__(self, state):
        self.state = state
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last: Optional[dict] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="tips-kite-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            wait = SYNC_IDLE_S
            try:
                now = _now()
                if live_rows() and (market_open("NFO", now) or market_open("MCX", now)):
                    self.last = sync(self.state.config, self.state.client, now)
                    wait = SYNC_FAST_S
            except Exception:
                logger.exception("Tips Kite sync failed")
            self._stop.wait(wait)


# --- the page's views ----------------------------------------------------------------------------------------------------

def _status_text(lg: Optional[dict]) -> str:
    if not lg:
        return "not placed"
    s = lg.get("status")
    if s == "complete":
        return f"filled {pl.fmt(lg.get('filled_px'))}"
    if s == "open":
        return "pending on Kite"
    if s == "rejected":
        return "rejected" + (f" · {str(lg['note'])[:40]}" if lg.get("note") else "")
    if s == "failed":
        return "refused"
    return s or "not placed"


def legs_view(config, h: dict) -> list[dict]:
    """The card's four legs (entry / stop / T1 / T2), placed or planned, each with its numbers from code."""
    k = kstate(h)
    plan = h.get("plan") or {}
    lot = int(h.get("lot_size") or 0) or 1
    lots = int(k.get("lots") or 0) or default_lots(config, h)
    qty = lots * lot
    q1, q2 = split(h, lots)
    tg = plan.get("targets") or []
    ref = _ref_px(h, k)
    sign = _sign(h)
    stop_px = k.get("stop_px") if k.get("stop_px") is not None else premium_stop(h)
    open_q = int(k.get("qty_open") or 0) or qty
    out = []

    def leg(key, side, q, typ, price, trigger, detail, extra=None):
        lg = k["legs"].get(key)
        d = {"leg": key, "name": LEG_NAMES[key], "side": (lg or {}).get("side") or side, "qty": (lg or {}).get("qty") or q,
             "type": (lg or {}).get("type") or typ, "price": (lg or {}).get("price") if lg else price,
             "trigger": (lg or {}).get("trigger") if lg else trigger, "status": (lg or {}).get("status") or "none",
             "status_text": _status_text(lg), "order_id": (lg or {}).get("order_id"), "at": t12((lg or {}).get("at"), True) if lg else "",
             "by": (lg or {}).get("by"), "note": (lg or {}).get("note"), "detail": detail}
        d["type_text"] = _type_text(d["type"], d["price"], d["trigger"])
        d.update(extra or {})
        out.append(d)

    prem = round(ref * qty, 2) if ref is not None else None
    e_lg = k["legs"].get("entry") or {}
    e_detail = f"{lots} lot{'s' if lots != 1 else ''} · {config.tips.kite_product}" + (f" · ≈ {rupees(prem)}" if prem is not None and e_lg.get("status") != "complete" else "")
    if e_lg.get("status") == "complete" and e_lg.get("filled_px") is not None:
        e_detail = f"{lots} lot{'s' if lots != 1 else ''} · {config.tips.kite_product} · {rupees(e_lg['filled_px'] * (e_lg.get('filled_qty') or qty))}"
    leg("entry", entry_side(h), qty, "MARKET", None, None, e_detail, {"premium": prem})
    if premium_stop(h) is None:
        leg("stop", exit_side(h), open_q, "SL-M", None, None, "spot stop: watched by the app, exit by hand", {"placeable": False})
    else:
        loss = round((stop_px - ref) * open_q * sign, 2) if ref is not None and stop_px is not None else None
        moved = k.get("entry_px") is not None and stop_px == k.get("entry_px")
        leg("stop", exit_side(h), open_q, "SL-M", None, stop_px,
            ("at cost · " if moved else "") + (f"max loss {rupees(loss, sign=True)}" if loss is not None else "max loss —"),
            {"max_loss": loss, "placeable": True})
    if tg:
        gain = round((float(tg[0]) - ref) * q1 * sign, 2) if ref is not None else None
        leg("t1", exit_side(h), q1, "LIMIT", float(tg[0]), None,
            ("half" if q2 else "all") + (f" · {rupees(gain, sign=True)} if filled" if gain is not None else ""), {"if_filled": gain})
    if len(tg) > 1 and q2:
        t1s = (k["legs"].get("t1") or {}).get("status")
        leg("t2", exit_side(h), q2, "LIMIT", float(tg[1]), None,
            "rest · placed after T1 · the stop moves to cost" if t1s != "complete" else "rest · T1 filled · the stop is at cost",
            {"after_t1": True})
    return out


def kite_actions(config, h: dict) -> list[str]:
    k = kstate(h)
    if mode(config) == "off":
        return ["exit"] if k["state"] in ("armed", "entered") else []
    acts = []
    legs = k["legs"]
    live = lambda leg: (legs.get(leg) or {}).get("status") in LIVE
    if k["state"] in ("none",) and h.get("status") not in ("closed", "expired") and (h.get("plan") or {}).get("ok") and h.get("tradingsymbol") \
            and not any(r["leg"] == "entry" and r["status"] == "complete" for r in rows_for(h["id"])):
        acts += ["all", "entry"]
    if k["state"] == "armed":
        acts += ["cancel:entry", "exit"]
    if k["state"] == "entered":
        t1s = (legs.get("t1") or {}).get("status")
        need_stop = not live("stop") and premium_stop(h) is not None
        need_t1 = t1s not in LIVE + ("complete",)
        need_t2 = t1s == "complete" and (legs.get("t2") or {}).get("status") not in LIVE + ("complete",) and split(h, int(k["lots"]))[1] > 0 and k["qty_open"] > 0
        if need_stop or need_t1 or need_t2:
            acts.append("rest")
        if need_stop:
            acts.append("stop")
        if need_t1 or need_t2:
            acts.append("targets")
        if live("stop"):
            acts += ["modify_stop", "cancel:stop"]
        for leg in ("t1", "t2"):
            if live(leg):
                acts.append(f"cancel:{leg}")
        if k["qty_open"] > 0:
            acts.append("exit")
    return acts


def hint_kite(config, h: dict) -> dict:
    """The hint's `kite` for the card: state, sizes, the legs, the actions, the log."""
    k = kstate(h)
    lots = int(k.get("lots") or 0) or default_lots(config, h)
    return {"state": k["state"], "lots": lots, "qty": int(k.get("qty") or 0) or lots * (int(h.get("lot_size") or 0) or 1),
            "qty_open": int(k.get("qty_open") or 0), "entry_px": k.get("entry_px"), "stop_px": k.get("stop_px"),
            "auto_rest": bool(k.get("auto_rest")), "realised": k.get("realised") or 0.0, "reason": k.get("reason"),
            "legs": legs_view(config, h), "actions": kite_actions(config, h), "log": k.get("log") or [],
            "beyond_t2": max(0, len((h.get("plan") or {}).get("targets") or []) - 2)}


def _order_view(r: dict, names: dict, entry_qty: dict) -> dict:
    nm = names.get(r["hint_id"], "")
    label = {"entry": f"Entry {nm}".strip(), "stop": "Stop", "t1": "Target 1", "t2": "Target 2 (rest)", "exit": "Exit at market"}[r["leg"]]
    if r["leg"] == "t1" and entry_qty.get(r["hint_id"]) and int(r["qty"] or 0) < entry_qty[r["hint_id"]]:
        label += " (half)"
    lg = {"status": r["status"], "filled_px": r.get("filled_px"), "note": r.get("note")}
    return {"id": r["id"], "hint_id": r["hint_id"], "leg": r["leg"], "label": label, "side": r["side"], "qty": r["qty"],
            "order_type": r["order_type"], "type_text": _type_text(r["order_type"], r.get("price"), r.get("trigger")),
            "price": r.get("filled_px") if r.get("filled_px") is not None else r.get("price"), "status": r["status"],
            "status_text": _status_text(lg) if r["status"] != "open" else "pending", "order_id": r.get("order_id"),
            "time": t12(r.get("placed_at")), "time_s": t12(r.get("placed_at"), True), "by": r.get("by"), "note": r.get("note")}


def today(config, client=None, now: Optional[datetime] = None) -> dict:
    """GET /api/tips/kite: {mode, kill, caps, orders (today, ours only), guards, open, net, last_sync, window, warning}."""
    now = now or _now()
    day = now.date().isoformat()
    rows = rows_today(day)
    names = {}
    for hid in {r["hint_id"] for r in rows}:
        h = bk.load(hid)
        if h:
            names[hid] = bk.title(h).replace("BUY ", "", 1).replace("SELL ", "", 1)
    with get_db() as conn:
        last = get_setting(conn, SYNC_KEY) or None
    live = [r for r in rows if r["status"] in LIVE]
    used = realised_today(day)
    out = lite(config, now, rows=rows, used=used)
    eq = {r["hint_id"]: int(r["qty"] or 0) for r in rows_for_hints(list(names)) if r["leg"] == "entry" and r["status"] not in ("rejected", "failed")}
    out.update(orders=[_order_view(r, names, eq) for r in reversed(rows)], guards=base_guards(config, client, now),
               net=used, last_sync=last, last_sync_label=t12(last, True) if last else None, open=len(live),
               warning=WARNING, at=now.isoformat(timespec="seconds"))
    return out


def lite(config, now: Optional[datetime] = None, rows: Optional[list] = None, used: Optional[float] = None) -> dict:
    """The summary's `kite`: {mode, kill, caps, open, window, product}."""
    now = now or _now()
    day = now.date().isoformat()
    rows = rows if rows is not None else rows_today(day)
    used = used if used is not None else realised_today(day)
    c = config.tips
    n = sum(1 for r in rows if r["leg"] == "entry" and r["status"] not in ("rejected", "failed"))
    return {"mode": mode(config), "kill": killed(), "open": sum(1 for r in rows if r["status"] in LIVE),
            "caps": {"entries": n, "max_entries": int(c.kite_max_entries_day), "max_lots": int(c.kite_max_lots),
                     "loss_cap": float(c.kite_loss_cap), "used_loss": round(min(used, 0.0), 2)},
            "window": window_text(config), "product": c.kite_product}


def health_row(config) -> dict:
    """🩺 tips_kite: mode, open orders, the last sync, the kill switch."""
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM tips_orders WHERE status IN ('placed', 'open')").fetchone()[0]
        last = get_setting(conn, SYNC_KEY) or None
    m = mode(config)
    k = killed()
    return {"key": "tips_kite", "name": "Tips → Kite orders", "ok": True, "warn": bool(k and m != "off"),
            "state": f"{m}{' · kill switch ON' if k else ''} · {n} open order{'s' if n != 1 else ''}",
            "detail": "by your click, each through a confirm · synced every 10 s while an order is working" + (f" · last sync {t12(last, True)}" if last else ""),
            "at": last}
