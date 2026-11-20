"""💡 The Tips paper book and the hint store. Its own capital (tips.capital), its own trades; nothing else reads it.

Take opens a position for a hint (size: the lots the post names, else what you give, else tips.lots / tips.rupees) at
the live quote: at the zone when the post says "when comes" (or a trigger: above / below), else at the market. From
then on code follows the plan on every quote (stop, half at T1 + stop to cost, rest at T2 / trail, time stop, expiry)
and the channel's own exit / SL hit / SL change when tips.auto_follow_channel_exit is on and you have not overridden
it; every such step is one Telegram line. Fills and exits are at quotes only, with charges per order like a real
account. Real execution: a later PR."""

import json
from datetime import datetime
from typing import Optional

from backend.config import IST
from backend.database.models import get_db, get_setting, now_ist, set_setting
from backend.logging_config.logger import setup_logger
from backend.tips import plan as pl

logger = setup_logger("tips")

JSON_COLS = ("entry", "stop", "targets", "size_hint", "decoded", "plan", "events", "run", "pos", "score", "kite", "shot")
SAVE_COLS = ("symbol", "side", "instrument", "strike", "opt_type", "expiry", "expiry_src", "tradingsymbol", "exchange",
             "lot_size", "entry", "stop", "targets", "open_ended", "size_hint", "decoded", "plan", "status", "ended", "taken",
             "lots", "entry_px", "exit_px", "pnl", "events", "tracked", "untracked_why", "run", "pos", "score")
# `kite` (the hint's Kite legs) is loaded with the hint but written only by backend/tips/kite_orders.py (its own UPDATE), so
# the quotes poll saving a hint never overwrites a leg the order sync just wrote. `shot` (📷 the screenshot's facts) the same:
# written only by backend/tips/vision.py's save_shot.
OPEN_STATUSES = ("new", "waiting", "active", "paused")


class TipsError(Exception):
    """An action refused: `code` 400 (bad input), 404 (no such hint), 409 (not in a state that allows it)."""

    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


# --- the hint store ----------------------------------------------------------------------------------------------------

def from_row(r) -> dict:
    h = dict(r)
    for k in JSON_COLS:
        if k in h:
            try:
                h[k] = json.loads(h[k]) if h[k] else ([] if k in ("events", "targets") else None)
            except (TypeError, ValueError):
                h[k] = None
    h["taken"] = bool(h.get("taken"))
    h["tracked"] = bool(h.get("tracked"))
    h["open_ended"] = bool(h.get("open_ended"))
    return h


def load(hid: int, conn=None) -> Optional[dict]:
    if conn is None:
        with get_db() as c:
            return load(hid, c)
    r = conn.execute("SELECT * FROM tips_hints WHERE id = ?", (hid,)).fetchone()
    return from_row(r) if r else None


def save(h: dict, conn=None) -> None:
    if conn is None:
        with get_db() as c:
            return save(h, c)
    vals = []
    for k in SAVE_COLS:
        v = h.get(k)
        if k in JSON_COLS:
            v = json.dumps(v) if v is not None else None
        elif isinstance(v, bool):
            v = int(v)
        vals.append(v)
    conn.execute(f"UPDATE tips_hints SET {', '.join(k + ' = ?' for k in SAVE_COLS)}, updated = ? WHERE id = ?",
                 (*vals, now_ist(), h["id"]))


def rows(where: str = "", args: tuple = (), order: str = "id DESC", limit: Optional[int] = None, conn=None) -> list[dict]:
    if conn is None:
        with get_db() as c:
            return rows(where, args, order, limit, c)
    q = "SELECT * FROM tips_hints" + (f" WHERE {where}" if where else "") + f" ORDER BY {order}" + (f" LIMIT {int(limit)}" if limit else "")
    return [from_row(r) for r in conn.execute(q, args)]


def open_hints(channel: Optional[str] = None, conn=None) -> list[dict]:
    """The hints a follow-up may refer to: open ones (taken positions still open count), oldest first."""
    hs = rows("(status IN ('new', 'waiting', 'active', 'paused') OR (taken = 1 AND json_extract(pos, '$.state') IN ('armed', 'open')))"
              + (" AND channel = ?" if channel else ""), (channel,) if channel else (), order="id", conn=conn)
    return hs


def event(h: dict, now: datetime, text: str, src: str = "code") -> None:
    h.setdefault("events", [])
    h["events"].append({"t": now.isoformat(timespec="seconds"), "text": text, "src": src})


def _now() -> datetime:
    return datetime.now(IST)


# --- money ------------------------------------------------------------------------------------------------------------

def charges(premium: float, sell: bool) -> float:
    """Charges of one option order on `premium` rupees (price x quantity), the same formula the app uses for paper
    options: brokerage Rs 20, STT 0.1% on the sell side, exchange 0.0495%, SEBI Rs 10 a crore, GST 18% on brokerage +
    exchange, stamp 0.003% on the buy side."""
    brokerage = 20.0
    stt = 0.001 * premium if sell else 0.0
    exch = 0.000495 * premium
    sebi = premium / 1e7 * 10
    gst = 0.18 * (brokerage + exch)
    stamp = 0.00003 * premium if not sell else 0.0
    return round(brokerage + stt + exch + sebi + gst + stamp, 2)


def _sign(h: dict) -> int:
    return -1 if h.get("side") == "SELL" else 1


def book_state(config=None) -> dict:
    with get_db() as conn:
        raw = get_setting(conn, "tips.book")
    st = json.loads(raw) if raw else None
    if not st:
        cap = float(config.tips.capital) if config is not None else 100000.0
        st = {"capital": cap, "started": now_ist()[:10]}
    return st


def set_capital(config, capital: float) -> dict:
    st = book_state(config)
    st["capital"] = float(capital)
    with get_db() as conn:
        set_setting(conn, "tips.book", json.dumps(st))
    return st


def open_qty(pos: dict) -> int:
    if not pos or pos.get("state") != "open":
        return 0
    return int(pos.get("qty_left", pos["qty"]))


def mtm(h: dict, ltp: Optional[float]) -> float:
    """The open position's unrealised P&L at `ltp` (0 when flat or no price)."""
    pos = h.get("pos")
    q = open_qty(pos)
    if not q or ltp is None:
        return 0.0
    return round((ltp - pos["run"]["entry_px"]) * q * _sign(h), 2)


def _apply(h: dict, evs: list[dict], now: datetime, why_src: str = "code", conn=None) -> list[str]:
    """Book the position's run events (fill / partial / closes) as money: charges per order, a tips_trades row per
    closing piece. Returns the lines for Telegram."""
    pos = h["pos"]
    lines = []
    sell = h.get("side") == "SELL"
    for e in evs:
        if e["kind"] == "fill":
            pos["state"] = "open"
            pos["entry_px"] = e["px"]
            pos["opened_at"] = now.isoformat(timespec="seconds")
            pos["charges_in"] = charges(e["px"] * pos["qty"], sell=sell)
            h["entry_px"] = e["px"]
            event(h, now, f"filled {pos_size(pos)} at {pl.fmt(e['px'])} (paper)", why_src)
            lines.append(f"filled {pos_size(pos)} at {pl.fmt(e['px'])}")
            continue
        if e.get("frac", 0) <= 0 or e.get("px") is None:
            if e["kind"] not in ("fill",) and pos["run"]["state"] == "closed" and pos["state"] != "closed":
                pos["state"] = "closed"
                pos["ended"] = e["kind"]
            continue
        left_before = pos.get("qty_left", pos["qty"])
        q = left_before if pos["run"]["state"] == "closed" else min(left_before, max(1, int(e["frac"] * pos["qty"])))
        pos["qty_left"] = left_before - q
        share = q / pos["qty"]
        gross = round((e["px"] - pos["entry_px"]) * q * _sign(h), 2)
        ch = round(charges(e["px"] * q, sell=not sell) + pos.get("charges_in", 0.0) * share, 2)
        net = round(gross - ch, 2)
        pos["realised"] = round(pos.get("realised", 0.0) + net, 2)
        pos["charges"] = round(pos.get("charges", 0.0) + ch, 2)
        pos.setdefault("booked", []).append({"t": now.isoformat(timespec="seconds"), "qty": q, "px": e["px"], "why": e["kind"], "net": net})
        row = (h["id"], pos.get("opened_at"), now.isoformat(timespec="seconds"), h.get("side"), title(h), round(q / max(1, pos["lot_size"]), 2),
               q, pos["entry_px"], e["px"], gross, ch, net, e["kind"])
        sql = ("INSERT INTO tips_trades (hint_id, opened_at, closed_at, side, symbol, lots, qty, entry_px, exit_px, gross, charges, net, why) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")
        if conn is not None:
            conn.execute(sql, row)
        else:
            with get_db() as c:
                c.execute(sql, row)
        part = "half booked" if pos["run"]["state"] != "closed" else ("rest closed" if len(pos["booked"]) > 1 else "closed")
        label = {"stop": "stop", "trail": "trailing stop", "time": "time stop", "expired": "expiry", "channel_exit": "channel exit",
                 "owner_exit": "your exit"}.get(e["kind"], e["kind"].upper() if e["kind"].startswith("t") else e["kind"])
        lines.append(f"{label} {pl.fmt(e['px'])} · {part} {inr(net, sign=True)}")
        if pos["run"]["state"] == "closed":
            pos["state"], pos["ended"], pos["exit_px"] = "closed", e["kind"], e["px"]
            h["exit_px"] = e["px"]
            lines[-1] += f" · {inr(pos['realised'], sign=True)} total"
        if e["kind"] == "t1" and pos["run"].get("stop_moved"):
            lines[-1] += f" · SL → cost {pl.fmt(pos['entry_px'])}"
        event(h, now, lines[-1], why_src)
    return lines


def pos_size(pos: dict) -> str:
    lots = pos["lots"]
    s = "½ lot" if pos.get("half") and lots == 1 else f"{lots:g} lot" + ("s" if lots != 1 else "") + (" (half)" if pos.get("half") else "")
    return s


def inr(v: Optional[float], sign: bool = False) -> str:
    from backend.alerts.messages import inr as _inr
    return _inr(v, 0, sign)


def title(h: dict) -> str:
    from backend.tips.decode import title as t
    return t(h)


# --- status -----------------------------------------------------------------------------------------------------------

def refresh(h: dict) -> dict:
    """Status, ended, entry / exit and P&L from the record run and (when taken) the position."""
    run, pos = h.get("run") or {}, h.get("pos")
    src = pos["run"] if h.get("taken") and pos else run
    lot = h.get("lot_size") or 0
    if h.get("status") not in ("skipped", "expired"):
        if pos and h.get("taken") and pos.get("state") == "closed":
            h["status"], h["ended"] = "closed", pos.get("ended")
        elif src.get("state") == "closed":
            h["status"], h["ended"] = "closed", src.get("end")
        elif src.get("paused") or run.get("paused"):
            h["status"] = "paused"
        elif src.get("state") == "active":
            h["status"] = "active"
        elif src.get("state") == "waiting":
            h["status"] = "waiting" if run.get("seen") else "new"
    elif h["status"] == "skipped" and run.get("state") == "closed":
        h["ended"] = h.get("ended") or run.get("end")
    if h.get("taken") and pos:
        h["entry_px"] = pos.get("entry_px")
        h["exit_px"] = pos.get("exit_px")
        h["pnl"] = round(pos.get("realised", 0.0) + mtm(h, run.get("last_ltp")) - (pos.get("charges_in", 0.0) * open_qty(pos) / pos["qty"] if open_qty(pos) else 0.0), 2)
        h["lots"] = pos["lots"] / (2 if pos.get("half") else 1)
    else:
        h["entry_px"] = run.get("entry_px")
        h["exit_px"] = run.get("exit_px")
        if run.get("entry_px") is not None and lot:
            pts = run.get("pnl_pts", 0.0) + ((run.get("last_ltp") or run["entry_px"]) - run["entry_px"]) * _sign(h) * run.get("open_frac", 0)
            h["pnl"] = round(pts * lot, 2)
        else:
            h["pnl"] = None
    plan = h.get("plan")
    if plan and run and run.get("state") == "closed":
        h["score"] = pl.result(run, plan, lot)
    elif plan and run:
        h["score"] = {**pl.result(run, plan, lot), "open": True}
    return h


# --- Telegram -------------------------------------------------------------------------------------------------------------

def push(config, key: str, html: str) -> bool:
    """One Tips line to Telegram (kind "tips": never held by the AI quiet hours or the evening batch)."""
    try:
        from backend.alerts.dispatch import Notifier
        return Notifier(config.alerts, ai=config.ai).send(key, "tips", html)
    except Exception:
        logger.exception("Tips Telegram send failed")
        return False


# --- the owner's actions ------------------------------------------------------------------------------------------------

def _get(hid: int) -> dict:
    h = load(hid)
    if not h:
        raise TipsError(404, f"no hint #{hid}")
    return h


def last_ltp(h: dict) -> Optional[float]:
    with get_db() as conn:
        r = conn.execute("SELECT ltp FROM tips_quotes WHERE hint_id = ? AND ltp IS NOT NULL ORDER BY t DESC LIMIT 1", (h["id"],)).fetchone()
    return r[0] if r else ((h.get("run") or {}).get("last_ltp") or ((h.get("pos") or {}).get("run") or {}).get("last_ltp"))


def size_for(h: dict, config, lots: Optional[float] = None, rupees: Optional[float] = None, ltp: Optional[float] = None) -> tuple[int, str]:
    """(lots, where the size came from): what you give > the post's lots / qty > tips.rupees (rupees mode) > tips.lots."""
    lot = int(h.get("lot_size") or 0) or 1
    if lots is not None:
        if float(lots) < 1 or float(lots) != int(lots):
            raise TipsError(400, "lots must be a whole number, 1 or more")
        return int(lots), "you"
    if rupees is not None:
        if rupees <= 0:
            raise TipsError(400, "rupees must be positive")
        if not ltp:
            raise TipsError(409, "no quote yet: give lots instead")
        return max(1, int(float(rupees) // (ltp * lot))), "rupees"
    sh = h.get("size_hint") or {}
    if sh.get("lots"):
        return int(sh["lots"]), "post"
    if sh.get("qty"):
        return max(1, int(sh["qty"]) // lot), "post"
    if config.tips.size_mode == "rupees" and ltp:
        return max(1, int(float(config.tips.rupees) // (ltp * lot))), "rupees"
    return int(config.tips.lots), "default"


INDEXES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX")


def set_symbol(config, hid: int, symbol: str, who: str = "page", now: Optional[datetime] = None) -> dict:
    """You name the index of a hint whose post did not ("buy 82400 pe near 680"): the contract is resolved, the plan
    rebuilt and tracking starts. Only before the hint is taken or filled; the choice is logged."""
    from backend.tips import track as tr
    now = now or _now()
    sym = (symbol or "").strip().upper()
    if sym not in INDEXES:
        raise TipsError(400, "symbol must be one of " + ", ".join(INDEXES))
    h = _get(hid)
    if h.get("taken") or h.get("status") in ("closed", "expired"):
        raise TipsError(409, f"#{hid} is {'taken' if h.get('taken') else h.get('status')}: the symbol cannot change now")
    if not h.get("strike") or not h.get("opt_type"):
        raise TipsError(409, f"#{hid} has no strike / option type to resolve")
    old = h.get("symbol")
    h["symbol"], h["instrument"] = sym, "option"
    dec = dict(h.get("decoded") or {})
    dec["symbol"] = "owner"
    h["decoded"] = dec
    posted = datetime.fromisoformat(h["posted_at"])
    tr.resolve(h, None, posted.date())
    h["plan"] = pl.build(h, config.tips, posted)
    run = h.get("run") or {}
    h["run"] = pl.new_run(h["plan"])
    h["run"]["allowed"], h["run"]["paused"] = bool(run.get("allowed")), bool(run.get("paused"))
    h["tracked"] = bool(h.get("tradingsymbol"))
    if not h["tracked"]:
        h["untracked_why"] = h.get("untracked_why") or "no instrument"
    event(h, now, f"you set the symbol: {old or '?'} → {sym} ({who}) · " + (f"contract {h['tradingsymbol']}" if h.get("tradingsymbol") else f"no contract: {h.get('untracked_why')}"), "owner")
    refresh(h)
    save(h)
    try:                                                    # ⚡ auto: the hint may be placeable now
        from backend.tips import kite_auto as ka
        if ka.on_new(config, ka.app_client(), hid, now):
            h = load(hid) or h
    except Exception:
        logger.exception("Tips Kite auto after the symbol failed")
    return h


def _shot_choice(config, hid: int, choice: str, who: str, now: Optional[datetime], strike=None, opt_type: Optional[str] = None,
                 symbol: Optional[str] = None) -> dict:
    from backend.tips import vision as vs
    now = now or _now()
    h = _get(hid)
    mm = vs.open_mismatch(h)
    if not mm:
        raise TipsError(409, f"#{hid} has no open screenshot mismatch")
    t = dict(mm.get("text") or {})
    if choice == "shot":
        if h.get("taken") or h.get("status") in ("closed", "expired"):
            raise TipsError(409, f"#{hid} is {'taken' if h.get('taken') else h.get('status')}: the strike cannot change now")
        s = mm.get("shot") or {}
        strike = strike if strike is not None else s.get("strike")
        opt_type = (opt_type or s.get("opt_type") or h.get("opt_type") or "").upper() or None
        symbol = (symbol or s.get("symbol") or h.get("symbol") or "").upper() or None
        try:
            strike = float(strike)
        except (TypeError, ValueError):
            raise TipsError(400, "strike must be a number")
        if strike <= 0 or opt_type not in ("CE", "PE") or (symbol and symbol not in INDEXES):
            raise TipsError(400, "a positive strike, CE / PE and an index name")
        try:                                                # an armed entry on the old contract goes first
            from backend.tips import kite_auto as ka
            from backend.tips import kite_orders as ko
            k = ko.kstate(h)
            if k["state"] == "entered":
                raise TipsError(409, f"#{hid} has filled on Kite: the strike cannot change now")
            if k["state"] in ko.ARMED:
                with ko._LOCK:
                    ko.disarm(config, ko.sim() if k.get("dry") else ka.app_client(), h, now, "you chose the screenshot's strike")
                h = _get(hid)
        except TipsError:
            raise
        except Exception:
            logger.exception("Tips Kite disarm before the strike change failed")
        vs._retarget(config, h, symbol=symbol if symbol != h.get("symbol") else None, strike=strike, opt_type=opt_type, src="owner")
        event(h, now, f"you chose the screenshot's strike {vs.contract_text({'strike': strike, 'opt_type': opt_type})} (text said "
                      f"{vs.contract_text(t)}) ({who}) · " + (f"contract {h['tradingsymbol']}" if h.get("tradingsymbol") else f"no contract: {h.get('untracked_why')}"), "owner")
    else:
        event(h, now, f"you kept the text's strike {vs.contract_text(t)} (the screenshot showed {vs.contract_text(mm.get('shot') or {})}) ({who})", "owner")
    shot = dict(h.get("shot") or {})
    shot["mismatch"] = {**mm, "resolved": {"choice": choice, "at": now.isoformat(timespec="seconds"), "who": who}}
    h["shot"] = shot
    refresh(h)
    save(h)
    vs.save_shot(h)
    try:                                                    # ⚡ auto may arm again now
        from backend.tips import kite_auto as ka
        from backend.tips import kite_orders as ko
        hk = load(hid) or h
        k = ko.kstate(hk)
        if k["state"] == "none":
            k["auto"] = {**(k.get("auto") or {}), "rearm": True, "causes": [c for c in ((k.get("auto") or {}).get("causes") or []) if c != "mismatch"]}
            ko.save_kite(hk)
        if ka.on_new(config, ka.app_client(), hid, now):
            h = load(hid) or h
    except Exception:
        logger.exception("Tips Kite auto after the strike choice failed")
    return load(hid) or h


def set_strike(config, hid: int, strike=None, opt_type: Optional[str] = None, symbol: Optional[str] = None, who: str = "page",
               now: Optional[datetime] = None) -> dict:
    """📷 "Use screenshot 82200": like set_symbol, the screenshot's contract replaces the text's (re-resolved, the plan
    rebuilt), the mismatch resolved and logged. Only before the hint is taken or filled."""
    return _shot_choice(config, hid, "shot", who, now, strike, opt_type, symbol)


def keep_strike(config, hid: int, who: str = "page", now: Optional[datetime] = None) -> dict:
    """📷 "Keep 82300": the text's contract stays, the mismatch is marked resolved (logged)."""
    return _shot_choice(config, hid, "text", who, now)


def take(config, hid: int, lots: Optional[float] = None, rupees: Optional[float] = None, half: bool = False,
         who: str = "page", now: Optional[datetime] = None, ltp: Optional[float] = None, notify: bool = True) -> dict:
    """Open the paper position of a hint. 404 no hint · 409 already taken / closed / skipped / not trackable · 400 no
    plan (stop missing) or a bad size. Fills at once when the quote meets the entry, else waits (armed)."""
    now = now or _now()
    h = _get(hid)
    if h.get("taken"):
        raise TipsError(409, f"#{hid} is already taken")
    if h.get("status") in ("closed", "expired"):
        raise TipsError(409, f"#{hid} is {h['status']}")
    if not (h.get("plan") or {}).get("ok"):
        raise TipsError(400, f"#{hid} has no plan: {(h.get('plan') or {}).get('why') or 'not decoded'}")
    if not h.get("tradingsymbol"):
        raise TipsError(409, f"#{hid} cannot be tracked: {h.get('untracked_why') or 'no instrument'}")
    ltp = ltp if ltp is not None else last_ltp(h)
    n, src = size_for(h, config, lots, rupees, ltp)
    lot = int(h.get("lot_size") or 1)
    qty = n * lot // 2 if half else n * lot
    if qty < 1:
        raise TipsError(400, "size rounds to zero")
    run = pl.new_run(h["plan"])
    rec = h.get("run") or {}
    run["paused"] = rec.get("paused", False)
    run["allowed"] = rec.get("allowed", False)
    pos = {"state": "armed", "lots": n, "half": bool(half), "lot_size": lot, "qty": qty, "qty_left": qty, "size_src": src, "who": who,
           "taken_at": now.isoformat(timespec="seconds"), "run": run, "realised": 0.0, "charges": 0.0, "override": False,
           "entry_px": None, "exit_px": None, "ended": None, "booked": []}
    h["pos"], h["taken"] = pos, True
    if h.get("status") == "skipped":
        h["status"] = "waiting"
    event(h, now, f"you took {pos_size(pos)} ({who})", "owner")
    if ltp is not None and pl.trading_day(now.date(), h.get("exchange")):
        _apply(h, pl.step(run, h["plan"], ltp, now), now, "code")
    refresh(h)
    save(h)
    if notify:
        push(config, f"tips|take|{hid}|{now.isoformat(timespec='seconds')}", taken_line(h, now))
    return h


def taken_line(h: dict, now: datetime) -> str:
    from backend.alerts import messages as msg
    return msg.tips_taken(h, now)


def skip(config, hid: int, who: str = "page", now: Optional[datetime] = None) -> dict:
    """Skip a hint (it is still scored for the channel's record). 409 when taken and open, or already over."""
    now = now or _now()
    h = _get(hid)
    if h.get("taken") and (h.get("pos") or {}).get("state") in ("armed", "open"):
        raise TipsError(409, f"#{hid} is taken: exit it instead")
    if h.get("status") in ("skipped", "closed", "expired"):
        raise TipsError(409, f"#{hid} is already {h['status']}")
    h["status"] = "skipped"
    event(h, now, f"you skipped ({who}) · still scored for the channel's record", "owner")
    refresh(h)
    save(h)
    return h


def exit_now(config, hid: int, who: str = "page", now: Optional[datetime] = None, ltp: Optional[float] = None) -> dict:
    """Close the position now at the quote (an armed one is just cancelled). 409 when nothing is open, or no quote."""
    now = now or _now()
    h = _get(hid)
    pos = h.get("pos")
    if not h.get("taken") or not pos or pos.get("state") not in ("armed", "open"):
        raise TipsError(409, f"#{hid} has no open position")
    if pos["state"] == "armed":
        pos["run"].update(state="closed", end="owner_exit")
        pos.update(state="closed", ended="owner_exit")
        event(h, now, f"you cancelled the take ({who}) · nothing was filled", "owner")
    else:
        ltp = ltp if ltp is not None else last_ltp(h)
        if ltp is None:
            raise TipsError(409, "no quote yet to exit at")
        evs = pl.close_all(pos["run"], h["plan"], ltp, now, "owner_exit")
        _apply(h, evs, now, "owner")
    pos["override"] = True
    refresh(h)
    save(h)
    return h


def trail(config, hid: int, to: float, who: str = "page", now: Optional[datetime] = None) -> dict:
    """Move the position's stop to `to` (premium). 400 on the wrong side of the last quote; 409 when nothing is open."""
    now = now or _now()
    h = _get(hid)
    pos = h.get("pos")
    if not h.get("taken") or not pos or pos.get("state") not in ("armed", "open"):
        raise TipsError(409, f"#{hid} has no open position")
    try:
        to = float(to)
    except (TypeError, ValueError):
        raise TipsError(400, "trail to a price")
    if to <= 0:
        raise TipsError(400, "trail to a positive price")
    ltp = last_ltp(h)
    if ltp is not None and pos["state"] == "open" and ((_sign(h) > 0 and to >= ltp) or (_sign(h) < 0 and to <= ltp)):
        raise TipsError(400, f"the stop {pl.fmt(to)} is on the wrong side of the price {pl.fmt(ltp)}")
    pos["run"].update(stop=to, spot_stop=None, stop_moved=True, trail=True)
    pos["override"] = True
    event(h, now, f"you moved the stop to {pl.fmt(to)} ({who})", "owner")
    refresh(h)
    save(h)
    return h


def pause(config, hid: int, on: bool = True, who: str = "page", now: Optional[datetime] = None) -> dict:
    """Pause (no entry until resumed) / resume a hint. 409 when it is over."""
    now = now or _now()
    h = _get(hid)
    if h.get("status") in ("closed", "expired", "skipped"):
        raise TipsError(409, f"#{hid} is {h['status']}")
    run = h.get("run") or pl.new_run(h["plan"] or {"stop": {}})
    run["paused"] = bool(on)
    h["run"] = run
    if h.get("pos"):
        h["pos"]["run"]["paused"] = bool(on)
    if not on and h["status"] == "paused":
        h["status"] = "waiting" if run.get("state") == "waiting" else "active"
    event(h, now, ("paused" if on else "resumed") + f" by you ({who})", "owner")
    refresh(h)
    save(h)
    return h


# --- the channel's follow-ups -----------------------------------------------------------------------------------------------

def channel_action(config, h: dict, act: dict, text: str, now: datetime, ltp: Optional[float] = None, conn=None) -> str:
    """Apply a follow-up to the hint (record run: avoid / active; the position: exit, SL hit, SL change, trail, partial
    booking when auto_follow_channel_exit is on and you have not overridden). Returns the line after the quote."""
    a = act.get("action")
    run = h.get("run")
    pos = h.get("pos") if h.get("taken") else None
    auto = bool(config.tips.auto_follow_channel_exit) and pos is not None and not pos.get("override")
    ltp = ltp if ltp is not None else (run or {}).get("last_ltp")
    out = ""
    if a == "avoid":
        for r in (run, (pos or {}).get("run")):
            if r and r.get("state") == "waiting":
                r["paused"] = True
        if run and run.get("state") == "waiting":
            out = "hint paused · no entry until the channel says enter"
        else:
            out = "noted"
    elif a == "active":
        for r in (run, (pos or {}).get("run")):
            if r:
                r["paused"] = False
                r["allowed"] = True
        out = "entry allowed at the market" if run and run.get("state") == "waiting" else "noted"
    elif a == "targets":
        hits = len(((pos or {}).get("run") or run or {}).get("hits") or [])
        tg = (h.get("plan") or {}).get("targets") or []
        bits = [f"T{i + 1} {pl.fmt(t)} ✓" for i, t in enumerate(tg[:hits])]
        out = " · ".join(bits) if bits else "no target reached on our quotes yet"
    elif a == "add":
        out = "add-on ignored (never averaged)"
    elif a == "trail" and pos and pos.get("state") == "open" and auto:
        r = pos["run"]
        r["trail"] = True
        if ltp is not None and (ltp - r["entry_px"]) * _sign(h) > 0:
            r["stop"] = max(r["stop"] or 0, r["entry_px"]) if _sign(h) > 0 else min(r["stop"] or 1e12, r["entry_px"])
            r["stop_moved"], r["spot_stop"] = True, None
        out = "trail on · you were informed"
    elif a == "stop" and not act.get("spot") and act.get("value") is not None and (pos is None or pos.get("state") == "armed") \
            and (h.get("kite") or {}).get("state") != "entered":
        # a stop revised before you are in ("210 revise sl 193"): the plan carries it, so a later entry uses it
        old = (h.get("stop") or {}).get("value")
        h["stop"] = {"kind": "premium", "value": act["value"]}
        posted = datetime.fromisoformat(h["posted_at"]) if h.get("posted_at") else now
        allowed, paused = bool((run or {}).get("allowed")), bool((run or {}).get("paused"))
        h["plan"] = pl.build(h, config.tips, posted)
        if run is not None and run.get("state") != "closed":
            run["stop"], run["spot_stop"] = act["value"], None
            run["allowed"], run["paused"] = allowed, paused
        if pos is not None:
            pos["run"]["stop"], pos["run"]["spot_stop"] = act["value"], None
        out = f"plan stop revised {pl.fmt(old)} → {pl.fmt(act['value'])} (channel)"
    elif a == "stop" and auto:
        r = pos["run"]
        if act.get("spot"):
            r["spot_stop"] = {"kind": "spot", "value": act["value"], "dir": (r.get("spot_stop") or h["plan"]["stop"]).get("dir", "below"), "tf": 15}
            r["stop"] = None
        else:
            r["stop"], r["stop_moved"] = act["value"], False
        out = f"followed: stop → {pl.fmt(act['value'])}" + (" spot" if act.get("spot") else "")
    elif a in ("exit", "sl_hit") and auto:
        if pos["state"] == "armed":
            pos["run"].update(state="closed", end="channel_exit")
            pos.update(state="closed", ended="channel_exit")
            out = "followed: the take is cancelled (not filled)"
        elif pos["state"] == "open" and ltp is not None:
            evs = pl.close_all(pos["run"], h["plan"], ltp, now, "channel_exit")
            _apply(h, evs, now, "channel", conn)
            out = f"followed: {'rest ' if len(pos.get('booked') or []) > 1 else ''}closed at {pl.fmt(ltp)} · {inr(pos['realised'], sign=True)} total"
        elif pos["state"] == "open":
            out = "no quote to follow it at · exit it yourself"
    elif a == "partial" and auto and pos["state"] == "open" and (ltp is not None or act.get("rest_stop") is not None):
        # partial first (half booked while more than half is open), then the remainder's stop: the post's "rest sl N"
        # (an explicit figure wins over "TSL at cost"; never lowered for a buy), else cost when the post says so
        r = pos["run"]
        bits = []
        if ltp is not None and r["open_frac"] > 0.5:
            frac = r["open_frac"] / 2
            e = pl._close(r, h["plan"], frac, ltp, now, "channel_exit")
            _apply(h, [e], now, "channel", conn)
            bits.append(f"half booked at {pl.fmt(ltp)}")
        rs = act.get("rest_stop")
        if r["state"] != "closed" and rs is not None and not act.get("spot"):
            cur = r.get("stop")
            if cur is not None and ((_sign(h) > 0 and float(rs) < float(cur)) or (_sign(h) < 0 and float(rs) > float(cur))):
                bits.append(f"rest stop kept at {pl.fmt(cur)} (the post's {pl.fmt(rs)} is lower)")
            else:
                r["stop"], r["stop_moved"], r["spot_stop"] = rs, True, None
                bits.append(f"rest stop → {pl.fmt(rs)}")
        elif act.get("cost") and r["state"] != "closed":
            r["stop"] = max(r["stop"] or 0, r["entry_px"]) if _sign(h) > 0 else min(r["stop"] or 1e12, r["entry_px"])
            r["stop_moved"], r["spot_stop"] = True, None
            bits.append(f"SL → cost {pl.fmt(r['entry_px'])}")
        out = ("followed: " + " · ".join(bits)) if bits else "follow-up linked"
    elif a in ("exit", "sl_hit", "stop", "trail", "partial") and pos is not None and not auto:
        out = "not followed: " + ("you overrode this position" if pos.get("override") else "auto-follow is off") + " · your call"
    elif a in ("exit", "sl_hit"):
        out = "noted (not taken)"
    else:
        out = "follow-up linked"
    event(h, now, f'channel: "{(text or "").strip()[:80]}" · {out}', "channel")
    refresh(h)
    return out


# --- the view ---------------------------------------------------------------------------------------------------------------

def view(config, now: Optional[datetime] = None) -> dict:
    """{positions, trades, equity, capital, realised, open_pnl, charges, cash, taken, won} for the page."""
    st = book_state(config)
    with get_db() as conn:
        trades = [dict(r) for r in conn.execute("SELECT * FROM tips_trades ORDER BY id DESC LIMIT 50")]
        realised = conn.execute("SELECT COALESCE(SUM(net), 0), COALESCE(SUM(charges), 0) FROM tips_trades").fetchone()
        curve = [dict(r) for r in conn.execute("SELECT day, equity FROM tips_equity ORDER BY day")]
    taken = rows("taken = 1")
    positions, open_pnl = [], 0.0
    for h in taken:
        pos = h.get("pos") or {}
        if pos.get("state") in ("armed", "open"):
            ltp = (h.get("run") or {}).get("last_ltp")
            m = mtm(h, ltp) - (pos.get("charges_in", 0.0) * open_qty(pos) / pos["qty"] if open_qty(pos) else 0.0)
            open_pnl += m
            positions.append({"hint_id": h["id"], "title": title(h), "state": pos["state"], "lots": pos["lots"], "half": pos.get("half"),
                              "qty": pos["qty"], "qty_open": open_qty(pos), "entry_px": pos.get("entry_px"), "ltp": ltp,
                              "stop": pos["run"].get("stop"), "spot_stop": pos["run"].get("spot_stop"), "trail": pos["run"].get("trail"),
                              "realised": pos.get("realised", 0.0), "open_pnl": round(m, 2), "pnl": round(pos.get("realised", 0.0) + m, 2),
                              "override": pos.get("override", False)})
    closed = [h for h in taken if (h.get("pos") or {}).get("state") == "closed" and (h.get("pos") or {}).get("entry_px") is not None]
    real = round(realised[0], 2)
    equity = round(st["capital"] + real + open_pnl, 2)
    return {"capital": st["capital"], "since": st.get("started"), "realised": real, "open_pnl": round(open_pnl, 2), "charges": round(realised[1], 2),
            "equity": equity, "positions": positions, "trades": trades, "curve": curve, "taken": len(taken),
            "won": sum(1 for h in closed if (h["pos"].get("realised") or 0) > 0), "closed": len(closed)}


def snapshot_equity(config, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    v = view(config, now)
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO tips_equity (day, equity, realised, open_pnl) VALUES (?,?,?,?)",
                     (now.date().isoformat(), v["equity"], v["realised"], v["open_pnl"]))
    return v
