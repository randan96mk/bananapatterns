"""⚡ Tips → Kite orders (click mode, dry run, connection test). The Tips paper book stays as it is; on top, the owner can send a hint's legs
to Kite (the app's own Kite Connect session) by button click, each click through a confirm on the page.

  legs      entry (MARKET, the hint's side) · stop (SL-M at the plan's premium stop, the full open quantity: the ONLY
            order that rests on Kite) · T1 / T2 WATCHED by the app (never resting orders: Kite nets only the first
            pending sell against a long option, a second one counts as a naked short and is refused for margin).
            Targets past the second stay in the paper record only. A spot-based stop is not placeable on Kite: the app
            watches it, you exit by hand.
  watch     every Syncer tick while a Kite position is open (any mode but off; click, auto and the dry run alike), on the
            hint's last LTP (the Tips quote poll; one quote read here when it is older than 6 s): LTP past T1 (≥ for a
            buy hint, ≤ for a sell) → cancel the resting stop → MARKET exit of half (whole lots, as split() does) → on
            the fill a NEW SL-M for the rest at cost (never worse than the previous stop) → T2 watched for the rest;
            one lot, or the last target → cancel the stop → MARKET exit of all → closed. The stop is cancelled only in
            the tick the market exit is sent; a refused / rejected market exit re-places the stop at once (same
            trigger, same quantity) with a red line "target exit refused … stop re-placed · exit by hand"; if that also
            fails: a red line and the Home item "no stop on Kite: exit by hand NOW". A target already passed after a
            restart is acted on at the next tick (no "missed" state).
  migrate   a real hint with a resting T1 / T2 LIMIT from before (the margin trap) has it cancelled on the first sync,
            the leg "watched", one log + Telegram line; a rejected target leg becomes "watched" silently.
  Place all entry now + `auto_rest`: the moment the entry fills the sync places the stop (by "app"); the targets are
            watched as above; when the stop or the last target fills the leftovers are cancelled. Those are the
            consequences of the owner's click (the confirm says so).
  guards    logged in · market hours · the entry window · mode not off · kill switch off · entries today under the cap
            · lots under the cap · today's realised loss short of the loss cap · a complete plan with a contract and a
            lot size · one Kite position per hint (never averaged, never re-entered after a fill) · the hint still open.
  only ours every order carries the tag "bptips"; the sync reads the order book once and keeps our order ids only.
            Holdings and other positions are never read. Nothing on the broker side is ever deleted.
  storage   tips_orders (one row per order, never deleted) and the hint's `kite` JSON (its own UPDATE, so the quotes
            poll saving a hint never overwrites it).
  dry run   kite_mode "dry": the same buttons, legs and follow-on orders, through backend/tips/kite_sim.py (fills from
            live quotes, nothing reaches Kite, no login needed). The entry rows carry by "dry", the order ids "DRY-n", the
            hint's kite "dry": true; the caps count dry rows only while the mode is dry (real caps: real rows only). A
            hint traded dry is not traded real the same day. "Clear dry run" voids today's dry rows (never deleted).
  test      connection_test(): one LIMIT buy of a far OTM NIFTY PE at half its price, cancelled 5 s later (session,
            static IP, order permission), at most once per 5 minutes; its row is hint 0, leg "test".
  ip        the server's public IP (the one to whitelist in Kite Connect), looked up by the Syncer every 10 minutes.
  levels    PR C: an entry with a level is never sent at market at once. A premium "above X" buy (below X sell) is a
            resting SL-M with trigger X; "at X" / a zone is a resting LIMIT (the zone's top for a buy, its bottom for a
            sell); an index level ("SENSEX above 82400") cannot rest on Kite, so the app arms it ("armed_watch") and the
            Syncer buys at market on the index cross. Activation words never gate an entry that has a level: the level
            is the trigger. An at / zone / market entry with wait words ("wait for activation") arms ("armed_wait") and
            is sent when the channel says active (or you press Send now). An unfilled armed / resting entry is cancelled
            at the end of the entry window, at the time stop, on the channel's avoid / cancel / not now, or by you.
  time stop a Kite position still open at the plan's time stop is exited at market by the sync (a hold-to-expiry sell
            is not); the caps count an entry when it fills (or a MARKET entry when it is sent), never when armed.
  auto      kite_mode "auto": backend/tips/kite_auto.py arms every placeable hint by itself (the same guards), follows
            the channel on an open position and tells you; backend/tips/kite_check.py checks every Kite event."""

import json
import re
import threading
import time as _time
import urllib.request
from datetime import datetime, time, timedelta
from typing import Optional

from backend.config import IST
from backend.database.models import get_db, get_setting, now_ist, set_setting
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm
from backend.tips import book as bk
from backend.tips import kite_sim
from backend.tips import plan as pl
from backend.tips.book import TipsError

logger = setup_logger("tips")

KILL_KEY = "tips.kite.kill"
SYNC_KEY = "tips.kite.sync_at"
TAG = "bptips"
LIVE = ("placed", "open")                      # an order still working on Kite
ARMED = ("armed", "armed_watch", "armed_wait")  # an entry not filled yet: resting on Kite / watched by the app / waiting
WORKING = ARMED + ("entered",)                 # a hint with something on Kite (or armed for it)
ARM_KEYS = ("kind", "order_type", "price", "trigger", "level", "level_kind", "index", "dir", "text", "what")
CHASE_MAX = 0.10                               # a level already passed by more than this is not chased at market
SYNC_FAST_S, SYNC_IDLE_S = 10, 30
SYNC_WATCH_S = 5                               # the Syncer's tick while a Kite position is open (the target watch)
QUOTE_STALE_S = 6                              # an LTP older than this is read again by the watch (real Kite only)
NO_STOP = "no stop on Kite: exit by hand NOW"
LEG_NAMES = {"entry": "Entry", "stop": "Stop", "t1": "Target 1", "t2": "Target 2", "exit": "Exit"}
WARNING = ("Real money on your Kite account. The app never averages, never re-enters after a stop, and stops for the day "
           "at the loss cap.")
DRY_WARNING = "DRY RUN · nothing reaches Kite. A simulator fills from live quotes; the caps count the dry run only."
TEST_KEY, IP_KEY = "tips.kite.test", "tips.kite.ip"
TEST_TAG = "bptest"
TEST_WAIT_S = 5                                # the test order rests this long before it is cancelled
TEST_GAP_S = 300                               # one connection test per 5 minutes at most
IP_TTL_S, IP_FAIL_TTL_S = 600, 120             # the public IP looked up every 10 minutes (a failed lookup again in 2)
IP_URLS = ("https://api.ipify.org", "https://ifconfig.me/ip")
_LOCK = threading.RLock()                      # the sync thread and the page's clicks never interleave
_TEST_LOCK = threading.Lock()                  # one connection test at a time


# --- small helpers -----------------------------------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(IST)


def killed() -> bool:
    with get_db() as conn:
        return get_setting(conn, KILL_KEY) == "1"


def mode(config) -> str:
    return str(getattr(config.tips, "kite_mode", "off") or "off")


def is_dry(client) -> bool:
    return bool(getattr(client, "dry", False))


def sim() -> "kite_sim.SimClient":
    """The dry run's simulator (its book lives in app_settings, so any instance sees the same orders)."""
    return kite_sim.SimClient()


def is_dry_row(r: dict) -> bool:
    return r.get("by") == "dry" or str(r.get("order_id") or "").startswith("DRY-")


def routes_dry(config, h: dict) -> bool:
    """Whether this hint's next action goes to the simulator: a working position stays where it was placed; anything
    else follows the mode."""
    k = kstate(h)
    if k["state"] in WORKING:
        return bool(k.get("dry"))
    return mode(config) == "dry"


def client_for(config, state, h: Optional[dict] = None):
    """The client every action and the sync go through: the simulator in a dry run (or for a dry position), else the
    real Kite client (`state` is the app state or a client)."""
    real = getattr(state, "client", state)
    dry = routes_dry(config, h) if h is not None else mode(config) == "dry"
    return sim() if dry else real


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
            "realised": 0.0, "reason": None, "legs": {}, "log": [], "by": None, "day": None, "arm": None, "entry_at": None,
            "stop_moves": [], "check": None, "ai": None, "auto": None, "watch": None, "no_stop": False}


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
    if k.get("dry") and text.startswith("Kite:"):
        text = "Kite DRY:" + text[len("Kite:"):]
    k["log"].append({"t": now.isoformat(timespec="seconds"), "text": text, "src": "kite"})
    k["log"] = k["log"][-40:]


def _push(config, key: str, text: str) -> None:
    bk.push(config, key, text)


def pfx(h: dict, icon: str = "⚡") -> str:
    """'⚡ Kite · ' / '⚡ Kite DRY · ' (a dry run's Telegram lines carry DRY)."""
    return f"{icon} Kite{' DRY' if kstate(h).get('dry') else ''} · "


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
    return [_row(r) for r in conn.execute("SELECT * FROM tips_orders WHERE status IN ('placed', 'open') AND leg != 'test' ORDER BY id")]


def _insert(row: dict, conn=None) -> int:
    if conn is None:
        with get_db() as c:
            return _insert(row, c)
    cols = ("hint_id", "leg", "side", "qty", "order_type", "price", "trigger", "status", "order_id", "filled_px", "filled_qty",
            "placed_at", "updated_at", "by", "note", "day", "tradingsymbol")
    cur = conn.execute(f"INSERT INTO tips_orders ({', '.join(chr(34) + c + chr(34) for c in cols)}) VALUES ({','.join('?' * len(cols))})",
                       tuple(row.get(c) for c in cols))
    return cur.lastrowid


def _update(rid: int, conn=None, **kw) -> None:
    if conn is None:
        with get_db() as c:
            return _update(rid, c, **kw)
    kw["updated_at"] = now_ist()
    conn.execute(f"UPDATE tips_orders SET {', '.join(chr(34) + k + chr(34) + ' = ?' for k in kw)} WHERE id = ?", (*kw.values(), rid))


def counts_entry(r: dict, resting: bool = False) -> bool:
    """An entry row toward the cap: filled, or a MARKET sent (it fills at once); a resting SL-M / LIMIT only with
    `resting` (the guard counts it so the cap can never be passed by fills); cancelled / rejected / voided never."""
    if r.get("leg") != "entry" or r.get("status") in ("rejected", "failed", "voided", "cancelled"):
        return False
    if r.get("status") == "complete" or (r.get("order_type") or "MARKET") == "MARKET":
        return True
    return bool(resting) and r.get("status") in LIVE


def entries_today(day: str, dry: bool = False, resting: bool = False) -> int:
    """Entries today toward the cap: a dry run's only while dry, real ones only otherwise (voided rows never). An entry
    counts when it fills; one armed or resting is not an entry yet (`resting` = count the resting ones too)."""
    with get_db() as conn:
        rows = [_row(r) for r in conn.execute("SELECT * FROM tips_orders WHERE day = ? AND leg = 'entry' AND status NOT IN ('rejected', 'failed', 'voided')",
                                              (day,))]
    return sum(1 for r in rows if is_dry_row(r) == bool(dry) and counts_entry(r, resting))


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


def realised_today(day: str, dry: bool = False) -> float:
    with get_db() as conn:
        hids = [r[0] for r in conn.execute("SELECT DISTINCT hint_id FROM tips_orders WHERE day = ? AND leg != 'test'", (day,))]
        rows = [_row(r) for r in conn.execute(f"SELECT * FROM tips_orders WHERE hint_id IN ({','.join('?' * len(hids))})", hids)] if hids else []
    rows = [r for r in rows if r["leg"] != "test" and r["status"] != "voided" and is_dry_row(r) == bool(dry)]
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
    arm = k.get("arm") or {}
    if arm.get("kind") in ("slm", "limit") and arm.get("level") is not None:
        return float(arm["level"])
    e = entry_of(h)
    if not k.get("arm") and e.get("mode") in ("above", "below") and not pl.is_spot(e) and e.get("lo") is not None:
        return float(e["lo"])                            # a stop-entry fills at its trigger
    ltp = bk.last_ltp(h)
    if ltp is not None:
        return ltp
    e = h.get("entry") or {}
    return e.get("hi") or e.get("lo")


def after_event(config, h: dict, now: datetime, notify: bool = True) -> None:
    """After every Kite event (placed / filled / modified / cancelled / rejected): the order check (code), its red
    lines to Telegram once, and the AI read queued (backend/tips/kite_check.py). Never raises; never places anything."""
    try:
        from backend.tips import kite_check as kc
        kc.after_event(config, h, now, notify)
    except Exception:
        logger.exception("Tips Kite check #%s failed", h.get("id"))


def index_ltp(h: dict, client=None) -> Optional[float]:
    """The index quote of a hint: the tracker's last stored spot (tips_quotes, every 5 s), else one quote call."""
    with get_db() as conn:
        r = conn.execute("SELECT spot FROM tips_quotes WHERE hint_id = ? AND spot IS NOT NULL ORDER BY t DESC LIMIT 1", (h["id"],)).fetchone()
    if r and r[0]:
        return float(r[0])
    if client is None or is_dry(client) or not hasattr(client, "quote"):
        return None
    try:
        from backend.tips import track as tr
        key = tr.spot_key(h)
        q = client.quote([key]) or {} if key else {}
        v = (q.get(key) or {}).get("last_price") if key else None
        return float(v) if v else None
    except Exception:
        return None


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
    dry = is_dry(client)
    n = entries_today(day, dry, resting=True)
    used = realised_today(day, dry)
    m = mode(config)
    exch = exchange or "NFO"
    seg = "MCX" if exch == "MCX" else "BFO / NFO"
    mk = market_open(exchange, now)
    inwin = _hm(c.kite_entry_from) <= now.time() < _hm(c.kite_entry_until)
    if dry:                                    # a rehearsal: no login, and the hours / the window do not block
        login = _g(True, "dry run · no login needed", "login")
        market = _g(True, f"market hours · {seg} open" if mk else f"market hours · {seg} closed now · a dry run fills at the last price", "market")
        window = _g(True, f"entries only {window_text(config)}" + ("" if inwin else " · not enforced in a dry run"), "window")
    else:
        login = _g(client is not None and bool(getattr(client, "has_token", False)), "Kite logged in · orders go through the app's own session", "login")
        market = _g(mk, f"market hours · {seg} open" if mk else f"market hours · {seg} closed now", "market")
        window = _g(inwin, f"entries only {window_text(config)}", "window")
    return [
        login, market, window,
        _g(m != "off", "Kite orders on · " + {"click": "by your click", "auto": "auto · entries armed on the level / the channel's activation",
                                              "dry": "dry run · nothing reaches Kite"}.get(m, "off"), "mode"),
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
    live = k["state"] in WORKING
    dry = is_dry(client)
    mine = [r for r in rows_for(h["id"]) if r["status"] != "voided" and is_dry_row(r) == dry]
    filled_before = any(r["leg"] == "entry" and r["status"] == "complete" for r in mine)
    mixed = mix_block(h, dry, now)
    out = list(base)
    out.append(_g(1 <= lots <= int(c.kite_max_lots), f"lots per order {lots} ≤ {int(c.kite_max_lots)}" if lots <= int(c.kite_max_lots)
                  else f"lots per order {lots} > the cap {int(c.kite_max_lots)}", "lots"))
    out.append(_g(ok_plan, "a complete plan (stop + target), a contract and a lot size" if ok_plan else
                  "no complete plan: " + (plan.get("why") or ("no contract" if not h.get("tradingsymbol") else "no target" if not plan.get("targets") else "no lot size")), "plan"))
    out.append(_g(not live and not filled_before and not mixed, "one Kite position per hint · never averaged · never re-entered after a stop"
                  if not (live or filled_before or mixed) else ("already on Kite · never averaged" if live else mixed or "already traded on Kite · never re-entered"), "one"))
    out.append(_g(h.get("status") not in ("closed", "expired"), "the hint is still open" if h.get("status") not in ("closed", "expired")
                  else f"the hint is {h.get('status')}", "open"))
    return out


def mix_block(h: dict, dry: bool, now: datetime) -> Optional[str]:
    """Never mixed: a hint traded in a dry run is not traded real the same day; a hint with real orders is not used for a
    dry run. The guard's text, or None."""
    rows = [r for r in rows_for(h["id"]) if r["leg"] != "test"]
    if dry:
        if any(not is_dry_row(r) and r["status"] not in ("rejected", "failed") for r in rows):
            return "already traded on Kite (real) · a dry run uses a fresh hint"
        return None
    if any(is_dry_row(r) and r.get("day") == now.date().isoformat() for r in rows):
        return "already traded on Kite (dry) · use a fresh hint"
    return None


def _check(gs: list[dict]) -> None:
    bad = next((g for g in gs if not g["ok"]), None)
    if bad:
        raise TipsError(409, bad["text"])


def _client_ok(client) -> None:
    if client is None or not getattr(client, "has_token", False):
        raise TipsError(409, "Kite is not logged in")


# --- placing -------------------------------------------------------------------------------------------------------------

import contextvars as _cv
_CFG: "_cv.ContextVar" = _cv.ContextVar("tips_kite_cfg", default=None)


def _tick(px: float) -> float:
    return round(round(float(px) / 0.05) * 0.05, 2)


def sl_price(config, side: str, trigger: float) -> float:
    """The limit of an SL order: `kite_sl_buffer_pct` past the trigger (a SELL stop: below it; a BUY stop-entry: above
    it). Options allow no SL-M: the broker turns one into an SL with a limit of its own, and a later trigger-only
    modify then fails ("difference between limit price and trigger price … beyond the permissible range", 20 Nov)."""
    b = float(getattr(config.tips, "kite_sl_buffer_pct", 1.5)) / 100.0
    px = float(trigger) * (1 - b) if side == "SELL" else float(trigger) * (1 + b)
    return max(0.05, _tick(px))


def _place(config, client, h: dict, leg: str, side: str, qty: int, order_type: str, now: datetime, by: str,
           price: Optional[float] = None, trigger: Optional[float] = None, conn=None) -> dict:
    """One order to Kite and its row. A refusal is stored (status failed, Kite's words) and raised as 409. A stop
    (SL-M asked) goes as SL with its own limit (sl_price)."""
    k = kstate(h)
    if order_type == "SL-M" and trigger is not None:
        order_type, price = "SL", sl_price(config, side, trigger)
    row = {"hint_id": h["id"], "leg": leg, "side": side, "qty": int(qty), "order_type": order_type, "price": price, "trigger": trigger,
           "status": "placed", "order_id": None, "placed_at": now.isoformat(timespec="seconds"), "updated_at": now.isoformat(timespec="seconds"),
           "by": by, "note": None, "day": now.date().isoformat(), "tradingsymbol": h.get("tradingsymbol")}
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
    if order_type == "SL" and price is not None:
        return f"SL {pl.fmt(trigger)} · limit {pl.fmt(price)}"
    if order_type in ("SL-M", "SL"):
        return f"{order_type} {pl.fmt(trigger)}"
    if order_type == "LIMIT":
        return f"LIMIT {pl.fmt(price)}"
    return order_type or "—"


def entry_of(h: dict) -> dict:
    return (h.get("plan") or {}).get("entry") or h.get("entry") or {"mode": "market"}


def needs_activation(h: dict) -> bool:
    """Wait words with no level (an at / zone / market entry) and the channel has not said active yet."""
    return bool(entry_of(h).get("needs_activation")) and not (h.get("run") or {}).get("allowed")


def index_name(h: dict) -> str:
    e = entry_of(h)
    return str(e.get("index") or h.get("symbol") or "the index")


def entry_plan(h: dict, ltp: Optional[float] = None, force_market: bool = False) -> dict:
    """How the entry goes to Kite: {kind market|slm|limit|watch, order_type, price, trigger, level, level_kind, index,
    dir, text (the leg's type text), what (the confirm's words), refuse (a level already passed too far)}."""
    e = entry_of(h)
    side = entry_side(h)
    buy = side == "BUY"
    m = e.get("mode") or "market"
    allowed = bool((h.get("run") or {}).get("allowed"))
    mk = {"kind": "market", "order_type": "MARKET", "price": None, "trigger": None, "level": None, "level_kind": None,
          "text": "MARKET", "what": f"{side} at market now"}
    if force_market or m == "market" or (allowed and not pl.is_spot(e)) or e.get("lo") is None:
        if allowed and m != "market":
            mk["what"] = f"{side} at market now (the channel said active)"
        return mk
    lo, hi = float(e["lo"]), float(e.get("hi") or e["lo"])
    if pl.is_spot(e):
        if allowed:
            return {**mk, "what": f"{side} at market now (the channel said active)"}
        idx = index_name(h)
        if m in ("above", "below"):
            d = m
        else:                                            # the option's favour: a CE buy wants the index at or under the top
            up = buy == (h.get("opt_type") != "PE")
            d = "below" if up else "above"
        lvl = (hi if d == "below" else lo) if m in ("at", "zone") else lo
        sym = "≥" if d == "above" else "≤"
        return {"kind": "watch", "order_type": "MARKET", "price": None, "trigger": None, "level": lvl, "level_kind": "spot",
                "index": idx, "dir": d, "text": f"MARKET when {idx} {sym} {pl.fmt(lvl)}",
                "what": f"the app watches {idx} and {'buys' if buy else 'sells'} at market when it trades {pl.fmt(lvl)} or {'above' if d == 'above' else 'below'}"}
    stop_entry = (m == "above" and buy) or (m == "below" and not buy)
    if stop_entry:
        out = {"kind": "slm", "order_type": "SL-M", "price": None, "trigger": lo, "level": lo, "level_kind": "premium",
               "text": f"SL trigger {pl.fmt(lo)}", "what": f"a {side} stop order rests on Kite at {pl.fmt(lo)} (it fills when the premium trades {pl.fmt(lo)} or {'above' if buy else 'below'})"}
        if ltp is not None and ((buy and ltp >= lo) or (not buy and ltp <= lo)):
            past = abs(ltp - lo) / lo if lo else 0.0
            if past > CHASE_MAX:
                out["refuse"] = f"already {round(past * 100)} % past the level {pl.fmt(lo)} (now {pl.fmt(ltp)}) · not chased"
                return out
            return {**mk, "level": lo, "level_kind": "premium", "what": f"{side} at market now (already past {pl.fmt(lo)}: now {pl.fmt(ltp)})",
                    "past": True}
        return out
    px = lo if m in ("above", "below") else (hi if buy else lo)
    return {"kind": "limit", "order_type": "LIMIT", "price": px, "trigger": None, "level": px, "level_kind": "premium",
            "text": f"LIMIT {pl.fmt(px)}", "what": f"a {side} LIMIT rests on Kite at {pl.fmt(px)}" + (" (the zone's top)" if m == "zone" and buy else " (the zone's bottom)" if m == "zone" else "")}


def _send_entry(config, client, h: dict, ep: dict, now: datetime, by: str) -> dict:
    """The entry order itself (MARKET / SL-M / LIMIT) and the kite state "armed" (resting or working on Kite)."""
    k = kstate(h)
    row_by = "dry" if is_dry(client) else by
    r = _place(config, client, h, "entry", entry_side(h), int(k["qty"]), ep["order_type"], now, row_by, price=ep.get("price"), trigger=ep.get("trigger"))
    k["state"] = "armed"
    k["arm"] = {k2: v for k2, v in ep.items() if k2 in ARM_KEYS}
    k["arm"]["at"] = now.isoformat(timespec="seconds")
    return r


def place_entry(config, client, hid: int, lots: Optional[int] = None, by: str = "click", now: Optional[datetime] = None,
                auto_rest: bool = False, notify: bool = True) -> dict:
    """The entry for `lots` (default: the post's size, else tips.lots), every guard first. A market entry goes at once
    (MARKET); a premium level rests on Kite (SL-M trigger / LIMIT); an index level is armed and watched by the app
    ("armed_watch"); wait words with no level arm it until the channel says active ("armed_wait")."""
    now = now or _now()
    with _LOCK:
        h = bk._get(hid)
        k = kstate(h)
        n = int(lots) if lots is not None else default_lots(config, h)
        _check(guards(config, client, h, n, now, "entry"))
        qty = n * int(h["lot_size"])
        ep = entry_plan(h, bk.last_ltp(h))
        if ep.get("refuse"):
            raise TipsError(409, ep["refuse"])
        k["dry"] = is_dry(client)
        for leg in ("entry", "stop", "t1", "t2", "exit"):
            k["legs"].pop(leg, None)
        k.update(lots=n, qty=qty, qty_open=0, entry_px=None, stop_px=premium_stop(h), auto_rest=bool(auto_rest), reason=None,
                 by=by, day=now.date().isoformat(), arm=None, entry_at=None, stop_moves=[], check=None)
        who = "your click" if by == "click" else by
        wait = needs_activation(h)
        if wait:
            k["state"] = "armed_wait"
            k["arm"] = {**{k2: v for k2, v in ep.items() if k2 in ARM_KEYS},
                        "wait": True, "at": now.isoformat(timespec="seconds")}
            text = f"Kite: entry armed {entry_side(h)} {qty} · {ep['text']} · nothing is sent until the channel says active ({who})"
        elif ep["kind"] == "watch":
            k["state"] = "armed_watch"
            k["arm"] = {k2: v for k2, v in ep.items() if k2 in ARM_KEYS}
            k["arm"]["at"] = now.isoformat(timespec="seconds")
            text = f"Kite: entry armed {entry_side(h)} {qty} · {ep['text']} · the app watches {ep['index']} ({who})"
        else:
            try:
                _send_entry(config, client, h, ep, now, by)
            finally:
                save_kite(h)
            text = f"Kite: entry placed {entry_side(h)} {qty} {ep['text']} ({who})" + (" · resting on Kite" if ep["kind"] != "market" else "") + \
                (f" · {ep['what']}" if ep.get("past") else "")
        if auto_rest:
            text += " · SL the moment it fills · targets watched by the app"
        if entry_of(h).get("wait_words") and not wait:
            text += " · activation words · the level decides"
        _log(k, now, text)
        save_kite(h)
        if notify:
            what = "armed" if k["state"] in ("armed_wait", "armed_watch") else "entry placed"
            tail = (" · waits for the channel's activation" if k["state"] == "armed_wait" else " · resting on Kite" if ep["kind"] in ("slm", "limit") else "")
            _push(config, f"tips|kite|{hid}|entry|{now.isoformat(timespec='seconds')}",
                  f"{pfx(h)}#{hid} {what} {entry_side(h)} {qty} {ep['text']}{tail}" + (" · SL on fill · targets watched" if auto_rest else ""))
        after_event(config, h, now, notify)
        return h


def send_now(config, client, hid: int, now: Optional[datetime] = None, by: str = "click", notify: bool = True) -> dict:
    """"Send now" on an armed entry: skip the channel's activation (armed_wait) or the index watch (armed_watch) and
    send it now (the order the arm holds; a watch goes at market). Every entry guard again."""
    now = now or _now()
    with _LOCK:
        h = bk._get(hid)
        k = kstate(h)
        if k["state"] not in ("armed_wait", "armed_watch"):
            raise TipsError(409, f"#{hid} has no armed entry to send")
        return _fire(config, client, h, now, by, "your Send now" if by == "click" else by, notify, force_market=k["state"] == "armed_watch")


def _fire(config, client, h: dict, now: datetime, by: str, why: str, notify: bool = True, force_market: bool = False) -> dict:
    """An armed entry goes to Kite now (the channel activated / the index crossed / Send now). The entry guards are
    checked again (the caps count it now); a failing guard disarms it with the reason."""
    k = kstate(h)
    hid = h["id"]
    lots = int(k.get("lots") or default_lots(config, h))
    was = k["state"]
    gs = [g for g in guards(config, client, {**h, "kite": {**k, "state": "none"}}, lots, now, "entry") if g["key"] != "one"]
    bad = next((g for g in gs if not g["ok"]), None)
    if bad:
        disarm(config, client, h, now, f"not sent: {bad['text']}", notify=notify)
        raise TipsError(409, bad["text"])
    ltp = bk.last_ltp(h)
    ep = entry_plan(h, ltp, force_market=force_market or was == "armed_watch")
    if ep.get("refuse"):
        disarm(config, client, h, now, ep["refuse"], notify=notify)
        raise TipsError(409, ep["refuse"])
    try:
        _send_entry(config, client, h, ep, now, by)
    except TipsError:
        k.update(state="none", reason="entry refused", arm=None)
        save_kite(h)
        raise
    text = f"Kite: entry sent {entry_side(h)} {k['qty']} {ep['text']} · {why}"
    _log(k, now, text)
    save_kite(h)
    if notify:
        _push(config, f"tips|kite|{hid}|fire|{now.isoformat(timespec='seconds')}", f"{pfx(h)}#{hid} entry sent {entry_side(h)} {k['qty']} {ep['text']} · {why}")
    after_event(config, h, now, notify)
    return h


def disarm(config, client, h: dict, now: datetime, why: str, notify: bool = True) -> bool:
    """An unfilled entry ends: the resting order cancelled (armed) or the arm dropped (armed_watch / armed_wait).
    Never counted toward the caps. One log line + one Telegram line. False when nothing was armed."""
    k = kstate(h)
    if k["state"] not in ARMED:
        return False
    if k["state"] == "armed":
        cl = sim() if k.get("dry") else client
        try:
            _cancel_leg(cl, h, "entry", now)
        except TipsError as exc:
            _log(k, now, f"Kite: the unfilled entry could not be cancelled · {str(exc)[:100]} · cancel it in Kite by hand")
            save_kite(h)
            return False
    k.update(state="none", reason=why, auto_rest=False, arm=None)
    _log(k, now, f"Kite: the unfilled entry cancelled · {why}")
    save_kite(h)
    if notify:
        _push(config, f"tips|kite|{h['id']}|disarm|{now.isoformat(timespec='seconds')}", f"{pfx(h)}#{h['id']} unfilled entry cancelled · {why}")
    after_event(config, h, now, notify)
    return True


def activate(config, client, h: dict, now: datetime, notify: bool = True) -> Optional[str]:
    """The channel said active / activated: an armed_wait entry is sent; a resting level order (or an index watch) not
    filled yet is cancelled and sent at MARKET (the channel considers it live). After the fill: nothing extra."""
    k = kstate(h)
    by = "auto" if k.get("by") == "auto" else "app"
    if k["state"] == "armed_wait":
        _fire(config, client, h, now, by, "the channel activated", notify)
        return "sent on the channel's activation"
    if k["state"] == "armed_watch":
        _fire(config, client, h, now, by, "the channel activated before the index level", notify, force_market=True)
        return "the channel activated before the level · sent at market"
    if k["state"] == "armed" and (k.get("arm") or {}).get("kind") in ("slm", "limit"):
        cl = sim() if k.get("dry") else client
        _cancel_leg(cl, h, "entry", now)
        k["state"] = "armed_watch"                       # momentarily: _fire sends at market
        _fire(config, cl, h, now, by, "the channel activated before the level filled · resting order cancelled", notify, force_market=True)
        return "the channel activated before the level filled · resting order cancelled · sent at market"
    return None


def place_all(config, client, hid: int, lots: Optional[int] = None, now: Optional[datetime] = None) -> dict:
    """Entry now; the stop the moment the entry fills (the sync, by "app"); the targets watched by the app."""
    return place_entry(config, client, hid, lots, "click", now, auto_rest=True)


def _place_stop(config, client, h: dict, now: datetime, by: str, conn=None) -> Optional[dict]:
    """The SL-M for the whole open quantity at the stop (k["stop_px"], else the plan's premium stop)."""
    k = kstate(h)
    if premium_stop(h) is None:
        raise TipsError(409, "spot stop: watched by the app, exit by hand")
    leg = k["legs"].get("stop")
    if leg and leg.get("status") in LIVE:
        raise TipsError(409, "the stop is already on Kite")
    trig = k.get("stop_px") if k.get("stop_px") is not None else premium_stop(h)
    r = _place(config, client, h, "stop", exit_side(h), k["qty_open"], "SL-M", now, by, trigger=trig, conn=conn)
    k["no_stop"] = False
    return r


def _ready(config, client, h: dict, action: str, now: datetime) -> dict:
    if mode(config) == "off" and action not in ("exit", "cancel"):
        raise TipsError(409, "Kite orders are off (Settings › tips › kite_mode)")
    k = kstate(h)
    if action not in ("exit", "cancel") and k["state"] in ("armed", "entered") and bool(k.get("dry")) != (mode(config) == "dry"):
        raise TipsError(409, "a dry-run position: switch to Dry run to place its legs (exit / cancel work in any mode)" if k.get("dry")
                        else "a real Kite position: switch to By my click to place its legs (exit / cancel work in any mode)")
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
        after_event(config, h, now)
        return h


def place_rest(config, client, hid: int, now: Optional[datetime] = None) -> dict:
    """"Place rest": the stop when it is not on Kite yet, and the target watch (the targets never rest on Kite)."""
    now = now or _now()
    with _LOCK:
        h = bk._get(hid)
        k = _ready(config, client, h, "rest", now)
        try:
            if (k["legs"].get("stop") or {}).get("status") in LIVE:
                raise TipsError(409, "the stop is already on Kite · the targets are watched by the app")
            r = _place_stop(config, client, h, now, "click")
            k["auto_rest"] = True
            if k.get("watch") == "refused":
                k["watch"] = None
            _log(k, now, f"Kite: SL-M {pl.fmt(r['trigger'])} ×{r['qty']} placed (your click) · the targets are watched by the app")
        finally:
            save_kite(h)
        after_event(config, h, now)
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
        _move_stop(client, h, px, now, "your click", config)
        save_kite(h)
        after_event(config, h, now)
        return h


def _stop_kw(config, h: dict, trigger: float, qty: Optional[int] = None) -> dict:
    """modify_order arguments for a stop: trigger AND limit together (an SL order), plus the quantity when given."""
    kw = {"order_type": "SL", "trigger_price": float(trigger), "price": sl_price(config, exit_side(h), trigger)}
    if qty is not None:
        kw["quantity"] = int(qty)
    return kw


def _move_stop(client, h: dict, px: float, now: datetime, who: str, config=None) -> None:
    """The working stop to `px` on Kite (trigger and limit together), its row, the leg, the log and the record of
    moves (the check)."""
    from backend.config import AppConfig
    config = config or _CFG.get() or AppConfig()
    k = kstate(h)
    leg = k["legs"].get("stop") or {}
    kw = _stop_kw(config, h, px)
    try:
        client.modify_order(leg["order_id"], **kw)
    except Exception as exc:
        raise TipsError(409, f"Kite refused the change: {str(exc)[:160]}")
    _update(leg["row"], trigger=px, price=kw["price"], order_type="SL")
    leg.update(trigger=px, price=kw["price"], type="SL")
    k["stop_px"] = px
    k.setdefault("stop_moves", []).append({"t": now.isoformat(timespec="seconds"), "to": px, "who": who})
    _log(k, now, f"Kite: stop moved to {pl.fmt(px)} ({who})")


def rest_stop_to(config, client, h: dict, px: float, now: datetime, who: str) -> str:
    """The remainder's stop to the channel's "rest sl N" after a part is booked: never lowered for a buy (raised for a
    sell), never past the price. Returns the line's text; never raises."""
    k = kstate(h)
    st = k["legs"].get("stop") or {}
    if st.get("status") not in LIVE:
        return "no stop on Kite to move"
    cur = st.get("trigger")
    buy = entry_side(h) == "BUY"
    if cur is not None and ((buy and float(px) <= float(cur)) or (not buy and float(px) >= float(cur))):
        return f"rest stop kept at {pl.fmt(cur)}" + ("" if float(px) == float(cur) else f" (the post's {pl.fmt(px)} is {'lower' if buy else 'higher'})")
    ltp = bk.last_ltp(h)
    if ltp is not None and ((buy and float(px) >= ltp) or (not buy and float(px) <= ltp)):
        return f"the rest stop {pl.fmt(px)} is past the price {pl.fmt(ltp)} · not modified · your call"
    try:
        _move_stop(client, h, float(px), now, who, config)
    except TipsError as exc:
        return f"rest stop refused: {str(exc)[:60]}"
    return f"rest stop → {pl.fmt(px)}"


def _resize(client, h: dict, now: datetime) -> list[str]:
    """After a part of the position is sold (a partial booking): the resting stop and targets never sell more than is
    still held (their quantity is cut to the open quantity)."""
    k = kstate(h)
    out = []
    q = int(k.get("qty_open") or 0)
    for leg in ("stop", "t1", "t2"):
        lg = k["legs"].get(leg) or {}
        if lg.get("status") in LIVE and q > 0 and int(lg.get("qty") or 0) > q:
            try:
                client.modify_order(lg["order_id"], quantity=q)
                _update(lg["row"], qty=q)
                lg["qty"] = q
                out.append(f"{LEG_NAMES[leg].lower()} ×{q}")
            except Exception as exc:
                out.append(f"{LEG_NAMES[leg].lower()} resize refused: {str(exc)[:60]}")
    return out


def cancel(config, client, hid: int, leg: str, now: Optional[datetime] = None) -> dict:
    """Cancel one working leg (entry / stop / t1 / t2)."""
    now = now or _now()
    if leg not in ("entry", "stop", "t1", "t2"):
        raise TipsError(400, "leg must be entry, stop, t1 or t2")
    with _LOCK:
        h = bk._get(hid)
        k = _ready(config, client, h, "cancel", now)
        if leg == "entry" and k["state"] in ("armed_watch", "armed_wait"):
            disarm(config, client, h, now, "you cancelled it", notify=False)
            return h
        lg = k["legs"].get(leg) or {}
        if lg.get("status") not in LIVE:
            raise TipsError(409, f"no {LEG_NAMES[leg].lower()} working on Kite")
        _cancel_leg(client, h, leg, now)
        if leg == "entry":
            k.update(state="none", reason="entry cancelled", auto_rest=False, arm=None)
        _log(k, now, f"Kite: {LEG_NAMES[leg].lower()} cancelled (your click)")
        save_kite(h)
        after_event(config, h, now)
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


def exit_now(config, client, hid: int, now: Optional[datetime] = None, by: str = "click", notify: bool = True, why: str = "") -> dict:
    """Cancel the working stop / targets (or the unfilled entry), then a MARKET exit of the open quantity."""
    now = now or _now()
    with _LOCK:
        h = bk._get(hid)
        k = _ready(config, client, h, "exit", now)
        if k["state"] in ARMED:
            if k["state"] == "armed":
                _cancel_leg(client, h, "entry", now)
            k.update(state="none", reason="entry cancelled", auto_rest=False, arm=None)
            _log(k, now, "Kite: the unfilled entry cancelled (exit)")
            save_kite(h)
            after_event(config, h, now, notify)
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
            _log(k, now, f"Kite: exit {r['side']} {r['qty']} MARKET ({'your click' if by == 'click' else by}) · stop and targets cancelled" + (f" · {why}" if why else ""))
        finally:
            save_kite(h)
        if notify:
            _push(config, f"tips|kite|{hid}|exit|{now.isoformat(timespec='seconds')}", f"{pfx(h)}#{hid} exit {exit_side(h)} {k['qty_open']} MARKET · stop and targets cancelled" + (f" · {why}" if why else ""))
        after_event(config, h, now, notify)
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
        hids = sorted({r["hint_id"] for r in live_rows()} | {h["id"] for h in bk.rows("json_extract(kite, '$.state') IN ('armed', 'armed_watch', 'armed_wait', 'entered')")})
        for hid in hids:
            h = bk.load(hid)
            if not h:
                continue
            k = kstate(h)
            cl = sim() if k.get("dry") else client
            try:
                for leg in ("entry", "stop", "t1", "t2"):
                    if cl is not None and _cancel_leg(cl, h, leg, now):
                        out["cancelled"] += 1
                if k["state"] in ARMED:
                    k.update(state="none", reason="kill switch", auto_rest=False, arm=None)
                if k["state"] == "entered" and k["qty_open"] > 0 and (k["legs"].get("exit") or {}).get("status") not in LIVE and cl is not None:
                    _place(config, cl, h, "exit", exit_side(h), k["qty_open"], "MARKET", now, "click")
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
    the owner's click (the stop on the entry fill with Place all / Place rest, a watched target's market exit filled →
    the new stop at cost for the rest, a refused one → the stop re-placed, leftovers cancelled when the position
    closes). {orders, changed, lines}."""
    now = now or _now()
    out = {"orders": 0, "changed": 0, "lines": []}
    with _LOCK:
        dry = is_dry(client)
        live = [r for r in live_rows() if is_dry_row(r) == dry]
        if not live or client is None or not getattr(client, "has_token", False):
            return out
        if dry and hasattr(client, "mark"):    # the simulator fills from each hint's last quote
            ltps = {}
            for hid in {r["hint_id"] for r in live}:
                h = bk.load(hid)
                if h and h.get("tradingsymbol"):
                    ltp = bk.last_ltp(h)
                    if ltp is not None:
                        ltps[h["tradingsymbol"]] = float(ltp)
            client.mark(ltps)
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
            after_event(config, h, now, notify)
            for i, ln in enumerate(lines):
                out["lines"].append(ln)
                if notify:
                    _push(config, f"tips|kite|{hid}|{now.isoformat(timespec='seconds')}|{i}", ln)
    with get_db() as conn:
        set_setting(conn, SYNC_KEY, now.isoformat(timespec="seconds"))
    return out


def sync_all(config, state, now: Optional[datetime] = None, notify: bool = True) -> dict:
    """Re-sync both books: the simulator's (any time) and the real one's. {orders, changed, lines}."""
    out = {"orders": 0, "changed": 0, "lines": []}
    live = live_rows()
    real = getattr(state, "client", state)
    for cl in ([sim()] if any(is_dry_row(r) for r in live) else []) + [real]:
        r = sync(config, cl, now, notify)
        out["orders"] += r["orders"]
        out["changed"] += r["changed"]
        out["lines"] += r["lines"]
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
    P = pfx(h)
    for r in rs:
        leg = r["leg"]
        lg = k["legs"].get(leg)
        if lg is None or lg.get("row") != r["id"]:
            lg = _leg_of(r)
            k["legs"][leg] = lg
        lg.update(status=r["status"], filled_px=r.get("filled_px"), filled_qty=r.get("filled_qty"), note=r.get("note"))
        if r["status"] == "rejected":
            if leg == "entry":
                k.update(state="none", reason="entry rejected", auto_rest=False, arm=None)
            msg = r.get("note") or "no reason given"
            if leg in ("t1", "t2") and (r.get("order_type") or "") == "MARKET":
                lines.append(_exit_refused(config, client, h, leg, msg, now))
                continue
            _log(k, now, f"Kite: {LEG_NAMES[leg].lower()} REJECTED · {msg}")
            lines.append(f"{pfx(h, '⚠️')}#{hid} {LEG_NAMES[leg].lower()} REJECTED · {msg}")
            continue
        if r["status"] == "cancelled":
            if leg == "entry" and k["state"] == "armed":
                k.update(state="none", reason="entry cancelled", auto_rest=False, arm=None)
            _log(k, now, f"Kite: {LEG_NAMES[leg].lower()} cancelled")
            continue
        if r["status"] != "complete":
            continue
        fpx, fq = r.get("filled_px"), int(r.get("filled_qty") or r["qty"])
        if leg == "entry":
            k.update(state="entered", entry_px=fpx, qty_open=fq, qty=fq, entry_at=now.isoformat(timespec="seconds"))
            text = f"{P}#{hid} entry filled {pl.fmt(fpx)} ×{fq}"
            if k.get("auto_rest") and not killed():
                s = _try(_place_stop, config, client, h, now, "app")
                if s:
                    text += f" · SL {pl.fmt(s['trigger'])} placed"
                elif premium_stop(h) is None:
                    text += " · spot stop: watched by the app, exit by hand"
                tg = (h.get("plan") or {}).get("targets") or []
                if tg:
                    text += f" · T1 {pl.fmt(tg[0])} watched by the app"
            _log(k, now, text.replace(f"{P}#{hid} ", "Kite: "))
            lines.append(text)
            continue
        # an exit-side fill
        before = int(k["qty_open"])
        k["qty_open"] = max(0, int(k["qty_open"]) - fq)
        pnl = round((fpx - (k.get("entry_px") or fpx)) * fq * _sign(h), 2)
        k["realised"] = round(float(k.get("realised") or 0.0) + pnl, 2)
        name = {"stop": "stop", "t1": "T1", "t2": "T2", "exit": "exit"}[leg]
        watched = leg in ("t1", "t2") and (r.get("order_type") or "") == "MARKET"
        text = f"{P}#{hid} {name} filled {pl.fmt(fpx)} ×{fq}"
        if watched:                                     # the app's market exit on a watched target
            text = f"{P}#{hid} {name} hit {pl.fmt(fpx)} · sold {fq}{f' of {before}' if k['qty_open'] > 0 else ''} at market"
            k.pop("target_exit", None)
        if watched and k["qty_open"] > 0:
            text += " · " + _stop_for_rest(config, client, h, now)
            tg = (h.get("plan") or {}).get("targets") or []
            if leg == "t1" and len(tg) > 1:
                text += f" · watching T2 {pl.fmt(tg[1])}"
        elif leg == "t1" and k["qty_open"] > 0:
            st = k["legs"].get("stop") or {}
            if st.get("status") in LIVE and k.get("entry_px") is not None:
                try:
                    kw = _stop_kw(config, h, k["entry_px"], k["qty_open"])
                    client.modify_order(st["order_id"], **kw)
                    _update(st["row"], trigger=k["entry_px"], qty=k["qty_open"], price=kw["price"], order_type="SL")
                    st.update(trigger=k["entry_px"], qty=k["qty_open"], price=kw["price"], type="SL")
                    k["stop_px"] = k["entry_px"]
                    k.setdefault("stop_moves", []).append({"t": now.isoformat(timespec="seconds"), "to": k["entry_px"], "who": "T1 filled"})
                    text += f" · stop → cost {pl.fmt(k['entry_px'])}"
                except Exception as exc:
                    text += f" · stop to cost refused: {str(exc)[:80]}"
        else:
            text += f" · {rupees(pnl, sign=True)}"
            if k["qty_open"] > 0 and leg == "exit":                 # a part booked: the rest's orders never oversell
                rs2 = _resize(client, h, now)
                if rs2:
                    text += f" · {k['qty_open']} left · " + ", ".join(rs2)
                if k.get("rest_stop_after_part") is not None:              # the channel's "rest sl N" wins over "TSL at cost"
                    text += " · " + rest_stop_to(config, client, h, float(k["rest_stop_after_part"]), now, "the channel: rest SL")
                    k["rest_stop_after_part"] = None
                    k["cost_after_part"] = False
                if k.get("cost_after_part") and k.get("entry_px") is not None and (k["legs"].get("stop") or {}).get("status") in LIVE:
                    try:
                        _move_stop(client, h, float(k["entry_px"]), now, "the channel: TSL at cost", config)
                        text += f" · stop → cost {pl.fmt(k['entry_px'])}"
                    except TipsError as exc:
                        text += f" · stop to cost refused: {str(exc)[:60]}"
                    k["cost_after_part"] = False
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
            k.update(state="closed", reason=k.get("exit_why") or {"stop": "stop", "t1": "target 1", "t2": "target 2", "exit": "your exit"}[leg], auto_rest=False,
                     no_stop=False, watch=None)
            text += f" · closed {rupees(k['realised'], sign=True)} total" if leg != "stop" else ""
        _log(k, now, text.replace(f"{P}#{hid} ", "Kite: "))
        lines.append(text.replace(P, wpfx(h), 1) if watched else text)
    return lines


# --- the target watch (only the stop rests on Kite) ----------------------------------------------------------------------

def wpfx(h: dict) -> str:
    """The watch's Telegram prefix: '⚡ Kite AUTO · ' for the app's own auto positions, else '⚡ Kite · ' (DRY marked)."""
    k = kstate(h)
    return f"⚡ Kite{' AUTO' if k.get('by') == 'auto' else ''}{' DRY' if k.get('dry') else ''} · "


def _crossed_target(h: dict, ltp: Optional[float], target) -> bool:
    if ltp is None or target is None:
        return False
    return float(ltp) >= float(target) if _sign(h) > 0 else float(ltp) <= float(target)


def _done(lg: Optional[dict]) -> bool:
    return bool(lg) and lg.get("status") == "complete"


def next_target(config, h: dict) -> Optional[dict]:
    """The target the app watches now: {leg t1|t2, i, price, qty, last}, or None (nothing left to watch)."""
    k = kstate(h)
    tg = (h.get("plan") or {}).get("targets") or []
    q_open = int(k.get("qty_open") or 0)
    if not tg or q_open <= 0:
        return None
    lot = int(h.get("lot_size") or 0) or 1
    if not _done(k["legs"].get("t1")):
        q1, q2 = split(h, int(k.get("lots") or 0) or max(1, q_open // lot))
        q1 = (min(q1, q_open) // lot) * lot or q_open
        last = q2 <= 0 or len(tg) < 2 or q_open // lot <= 1 or q1 >= q_open
        return {"leg": "t1", "i": 0, "price": float(tg[0]), "qty": q_open if last else q1, "last": last}
    if len(tg) > 1 and not _done(k["legs"].get("t2")):
        return {"leg": "t2", "i": 1, "price": float(tg[1]), "qty": q_open, "last": True}
    return None


def fresh_ltp(h: dict, client, now: datetime) -> Optional[float]:
    """The hint's last LTP (the Tips quote poll writes it every 5 s); on real Kite one quote read when it is older than
    QUOTE_STALE_S (stored like the poll's, so the page shows the same price)."""
    with get_db() as conn:
        r = conn.execute("SELECT t, ltp FROM tips_quotes WHERE hint_id = ? AND ltp IS NOT NULL ORDER BY t DESC LIMIT 1", (h["id"],)).fetchone()
    age = None
    if r:
        try:
            t0 = datetime.fromisoformat(r[0])
            if t0.tzinfo is None:
                t0 = t0.replace(tzinfo=IST)
            age = (now - t0).total_seconds()
        except ValueError:
            age = None
    if r and age is not None and age <= QUOTE_STALE_S:
        return float(r[1])
    if client is not None and not is_dry(client) and hasattr(client, "quote") and h.get("tradingsymbol"):
        try:
            from backend.tips import track as tr
            key = tr.quote_key(h)
            q = (client.quote([key]) or {}) if key else {}
            v = (q.get(key) or {}).get("last_price") if key else None
            if v:
                with get_db() as conn:
                    conn.execute("INSERT OR REPLACE INTO tips_quotes (hint_id, t, ltp, spot) VALUES (?,?,?,?)",
                                 (h["id"], now.isoformat(timespec="seconds"), float(v), None))
                return float(v)
        except Exception as exc:
            logger.warning("Tips Kite watch quote #%s failed: %s", h["id"], str(exc)[:120])
    if r:
        return float(r[1])
    return bk.last_ltp(h)


def _no_stop(config, h: dict, now: datetime, why: str) -> str:
    """The stop could not be (re-)placed: the hint carries no_stop (a red check item → Home "Needs a look")."""
    k = kstate(h)
    k["no_stop"] = True
    _log(k, now, f"Kite: {NO_STOP} · {why}")
    return f"{pfx(h, '⚠️')}#{h['id']} {why} · {NO_STOP}"


def _replace_stop(config, client, h: dict, now: datetime, trigger, qty: int) -> Optional[str]:
    """The SL-M again (same trigger, same quantity) after a refused target exit. None when placed, else Kite's words."""
    k = kstate(h)
    if premium_stop(h) is None and trigger is None:
        return None
    try:
        r = _place(config, client, h, "stop", exit_side(h), int(qty), "SL-M", now, "app", trigger=trigger)
        k["stop_px"] = trigger
        k["no_stop"] = False
        _log(k, now, f"Kite: SL-M {pl.fmt(r['trigger'])} ×{r['qty']} re-placed")
        return None
    except TipsError as exc:
        return str(exc)


def _exit_refused(config, client, h: dict, leg: str, msg: str, now: datetime) -> str:
    """A target's market exit refused (at once or REJECTED in the book): the stop re-placed at once; the watch stops
    (the owner exits by hand). The red line."""
    k = kstate(h)
    te = k.pop("target_exit", None) or {}
    k["watch"] = "refused"
    name = LEG_NAMES[leg]
    _log(k, now, f"Kite: {name.lower()} exit refused · {str(msg)[:120]}")
    if (k["legs"].get("stop") or {}).get("status") in LIVE:
        return f"{pfx(h, '⚠️')}#{h['id']} {name} exit refused: {str(msg)[:120]} · the stop is still on Kite · exit by hand"
    trig = te.get("trigger") if te.get("trigger") is not None else k.get("stop_px")
    if trig is None and premium_stop(h) is None:
        return f"{pfx(h, '⚠️')}#{h['id']} {name} exit refused: {str(msg)[:120]} · spot stop: watched by the app · exit by hand"
    bad = _replace_stop(config, client, h, now, trig if trig is not None else premium_stop(h), int(te.get("qty") or k["qty_open"]))
    if bad is None:
        return f"{pfx(h, '⚠️')}#{h['id']} target exit refused: {str(msg)[:120]} · stop re-placed · exit by hand"
    return _no_stop(config, h, now, f"target exit refused: {str(msg)[:100]} · the stop re-place refused too: {bad[:80]}")


def _stop_for_rest(config, client, h: dict, now: datetime) -> str:
    """After T1's market exit filled: a NEW SL-M for the rest at cost (a buy never below the previous stop, a sell never
    above). The line's part."""
    k = kstate(h)
    prev = k.get("stop_px")
    cost = k.get("entry_px")
    if cost is None:
        trig = prev
    elif prev is None:
        trig = float(cost)
    else:
        trig = max(float(cost), float(prev)) if _sign(h) > 0 else min(float(cost), float(prev))
    if trig is None:
        return "spot stop: watched by the app, exit by hand"
    st = k["legs"].get("stop") or {}
    if st.get("status") in LIVE:                         # still resting (should not be): resize + move instead
        try:
            kw = _stop_kw(config, h, trig, k["qty_open"])
            client.modify_order(st["order_id"], **kw)
            _update(st["row"], trigger=trig, qty=k["qty_open"], price=kw["price"], order_type="SL")
            st.update(trigger=trig, qty=k["qty_open"], price=kw["price"], type="SL")
            k["stop_px"] = trig
            k.setdefault("stop_moves", []).append({"t": now.isoformat(timespec="seconds"), "to": trig, "who": "T1 hit"})
            return f"stop → cost {pl.fmt(trig)} for the rest"
        except Exception as exc:
            return f"stop to cost refused: {str(exc)[:80]}"
    try:
        r = _place(config, client, h, "stop", exit_side(h), k["qty_open"], "SL-M", now, "app", trigger=trig)
    except TipsError as exc:
        k["no_stop"] = True
        _log(k, now, f"Kite: {NO_STOP} · the stop for the rest refused")
        return f"the stop for the rest refused: {str(exc)[:80]} · {NO_STOP}"
    k["stop_px"] = trig
    k["no_stop"] = False
    k.setdefault("stop_moves", []).append({"t": now.isoformat(timespec="seconds"), "to": trig, "who": "T1 hit"})
    return f"stop → {'cost ' if cost is not None and float(trig) == float(cost) else ''}{pl.fmt(r['trigger'])} for the rest"


def watch_one(config, client, h: dict, now: datetime, notify: bool = True) -> Optional[str]:
    """One tick of the target watch on one open Kite position (see the module doc). The Telegram line, or None."""
    k = kstate(h)
    if k["state"] != "entered" or int(k.get("qty_open") or 0) <= 0 or k.get("watch") == "refused" or killed() or mode(config) == "off":
        return None
    legs = k["legs"]
    if any((legs.get(x) or {}).get("status") in LIVE for x in ("exit", "t1", "t2")):
        return None                                      # an exit already working: its fill decides
    nt = next_target(config, h)
    if not nt:
        return None
    ltp = fresh_ltp(h, client, now)
    if not _crossed_target(h, ltp, nt["price"]):
        return None
    hid = h["id"]
    st = legs.get("stop") or {}
    trig, sq = st.get("trigger"), int(st.get("qty") or k["qty_open"])
    if st.get("status") in LIVE:                         # the stop goes first, only now that the exit is sent in this tick
        try:
            _cancel_leg(client, h, "stop", now)
        except TipsError as exc:
            _log(k, now, f"Kite: {nt['leg'].upper()} {pl.fmt(nt['price'])} reached ({pl.fmt(ltp)}) · the stop could not be cancelled · {str(exc)[:80]} · tried again next tick")
            save_kite(h)
            return None
    k["target_exit"] = {"leg": nt["leg"], "trigger": trig if trig is not None else k.get("stop_px"), "qty": sq, "ltp": ltp,
                        "at": now.isoformat(timespec="seconds")}
    try:
        r = _place(config, client, h, nt["leg"], exit_side(h), nt["qty"], "MARKET", now, "auto" if k.get("by") == "auto" else "app")
    except TipsError as exc:
        line = _exit_refused(config, client, h, nt["leg"], str(exc).replace("Kite refused the target 1: ", "").replace("Kite refused the target 2: ", ""), now)
        save_kite(h)
        if notify:
            _push(config, f"tips|kite|{hid}|watch|{now.isoformat(timespec='seconds')}", line)
        after_event(config, h, now, notify)
        return line
    _log(k, now, f"Kite: {nt['leg'].upper()} {pl.fmt(nt['price'])} reached · now {pl.fmt(ltp)} · stop cancelled · {r['side']} {r['qty']} MARKET sent (the target watch)")
    save_kite(h)
    after_event(config, h, now, notify)
    return None


def migrate_targets(config, client, h: dict, now: datetime, notify: bool = True) -> Optional[str]:
    """A target resting on Kite from before (a LIMIT t1 / t2 placed / open, the margin trap) is cancelled and the leg
    marked watched (real accounts; one log + Telegram line). A rejected / failed target leg becomes watched silently."""
    k = kstate(h)
    out, changed = None, False
    for leg in ("t1", "t2"):
        lg = k["legs"].get(leg) or {}
        if not lg or (lg.get("type") or "LIMIT") != "LIMIT":
            continue
        if lg.get("status") in LIVE:
            try:
                _cancel_leg(client, h, leg, now)
            except TipsError as exc:
                _log(k, now, f"Kite: the resting {LEG_NAMES[leg].lower()} could not be cancelled · {str(exc)[:80]} · cancel it in Kite by hand")
                continue
            k["legs"][leg] = {"status": "watched", "was": lg.get("order_id")}
            changed = True
            _log(k, now, f"Kite: the resting {LEG_NAMES[leg].lower()} LIMIT {pl.fmt(lg.get('price'))} cancelled (a second resting sell is refused for margin) · now watched by the app")
            out = f"{pfx(h)}#{h['id']} resting {LEG_NAMES[leg].lower()} LIMIT {pl.fmt(lg.get('price'))} cancelled · the targets are watched by the app · only the stop rests on Kite"
            if notify and not k.get("dry"):
                _push(config, f"tips|kite|{h['id']}|migrate|{leg}", out)
        elif lg.get("status") in ("rejected", "failed", "cancelled"):
            k["legs"][leg] = {"status": "watched", "was": lg.get("order_id")}
            changed = True
    if changed:
        save_kite(h)
    return out


def watch_all(config, state, now: Optional[datetime] = None, notify: bool = True) -> dict:
    """The Syncer's tick: every open Kite position (dry ones on the simulator at any hour; real ones while their market
    is open): the migration, then the target watch. {open, lines}."""
    now = now or _now()
    real = getattr(state, "client", state)
    out = {"open": 0, "lines": []}
    for h0 in bk.rows("json_extract(kite, '$.state') = 'entered'", order="id"):
        k0 = kstate(h0)
        if int(k0.get("qty_open") or 0) <= 0:
            continue
        dry = bool(k0.get("dry"))
        cl = sim() if dry else real
        if cl is None or (not dry and (not getattr(cl, "has_token", False) or not market_open(h0.get("exchange"), now))):
            continue
        out["open"] += 1
        try:
            with _LOCK:
                h = bk.load(h0["id"]) or h0
                migrate_targets(config, cl, h, now, notify)
                ln = watch_one(config, cl, h, now, notify)
            if ln:
                out["lines"].append(ln)
        except Exception:
            logger.exception("Tips Kite target watch #%s failed", h0.get("id"))
    return out


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
                wait = self.pass_once()
            except Exception:
                logger.exception("Tips Kite sync failed")
            try:
                if mode(self.state.config) != "off":
                    public_ip()                                # cached 10 minutes: one lookup at most per 10 minutes
            except Exception:
                logger.exception("Tips Kite public IP lookup failed")
            self._stop.wait(wait)

    def pass_once(self, now: Optional[datetime] = None) -> int:
        """One pass: the target watch, the order books synced, the housekeeping (index watch, activation, window end,
        time stop), auto arming. Returns the seconds to the next pass (5 while a Kite position is open, 10 while
        anything is working or armed, else 30)."""
        from backend.tips import kite_auto as ka
        now = now or _now()
        wait = SYNC_IDLE_S
        cfg, real = self.state.config, self.state.client
        try:                                           # the target watch first: a market exit it sends is read back below
            watched = watch_all(cfg, real, now)
        except Exception:
            logger.exception("Tips Kite target watch failed")
            watched = {"open": 0}
        live = live_rows()
        if any(is_dry_row(r) for r in live):          # the dry run fills from quotes at any hour
            self.last = sync(cfg, sim(), now)
            wait = SYNC_FAST_S
        mk = market_open("NFO", now) or market_open("MCX", now)
        if any(not is_dry_row(r) for r in live) and mk:
            self.last = sync(cfg, real, now)
            wait = SYNC_FAST_S
        if ka.anything_armed():
            ka.housekeep(cfg, real, now)
            wait = SYNC_FAST_S
        if mode(cfg) == "auto" and mk:
            if ka.auto_scan(cfg, real, now):
                wait = SYNC_FAST_S
        if watched.get("open"):
            wait = min(wait, SYNC_WATCH_S)             # a Kite position open: the targets are watched every 5 s
        return wait


# --- the page's views ----------------------------------------------------------------------------------------------------

def _status_text(lg: Optional[dict], dry: bool = False) -> str:
    if not lg:
        return "not placed"
    s = lg.get("status")
    if s == "complete":
        return f"filled {pl.fmt(lg.get('filled_px'))}"
    if s == "open":
        return "pending" if dry else "pending on Kite"
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
             "status_text": _status_text(lg, bool(k.get("dry"))), "dry": bool(k.get("dry")) and lg is not None, "order_id": (lg or {}).get("order_id"), "at": t12((lg or {}).get("at"), True) if lg else "",
             "by": (lg or {}).get("by"), "note": (lg or {}).get("note"), "detail": detail}
        d["type_text"] = _type_text(d["type"], d["price"], d["trigger"])
        d.update(extra or {})
        out.append(d)

    prem = round(ref * qty, 2) if ref is not None else None
    e_lg = k["legs"].get("entry") or {}
    e_detail = f"{lots} lot{'s' if lots != 1 else ''} · {config.tips.kite_product}" + (f" · ≈ {rupees(prem)}" if prem is not None and e_lg.get("status") != "complete" else "")
    if e_lg.get("status") == "complete" and e_lg.get("filled_px") is not None:
        e_detail = f"{lots} lot{'s' if lots != 1 else ''} · {config.tips.kite_product} · {rupees(e_lg['filled_px'] * (e_lg.get('filled_qty') or qty))}"
    arm = k.get("arm") or {}
    ep = arm if arm else ({"kind": None} if e_lg else entry_plan(h, bk.last_ltp(h)))   # a placed entry shows its own order
    extra = {"premium": prem, "arm_kind": ep.get("kind"), "what": ep.get("what") or "", "refuse": ep.get("refuse")}
    if k["state"] == "armed_watch":
        now_i = index_ltp(h)
        extra.update(status_override="armed", status_text_override="armed", type_text_override=ep.get("text"),
                     index_now=now_i)
        e_detail = f"{ep.get('text')}{f' · now {pl.fmt(now_i)}' if now_i is not None else ''} · armed · the app watches {ep.get('index') or 'the index'} · {e_detail}"
    elif k["state"] == "armed_wait":
        extra.update(status_override="armed", status_text_override="armed · waiting for activation", type_text_override=ep.get("text"))
        e_detail = f"armed · waiting for the channel's activation · then {ep.get('text')} · {e_detail}"
    elif ep.get("kind") in ("slm", "limit", "watch") or (k["state"] == "none" and ep.get("kind") == "market"):
        extra["type_text_override"] = ep.get("text")
        if e_lg.get("status") in LIVE and ep.get("kind") in ("slm", "limit"):
            extra["status_text_override"] = "resting on Kite" if not k.get("dry") else "resting (dry)"
    leg("entry", entry_side(h), qty, ep.get("order_type") or "MARKET", ep.get("price"), ep.get("trigger"), e_detail, extra)
    d0 = out[-1]
    if d0.get("type_text_override"):
        d0["type_text"] = d0["type_text_override"]
    if d0.get("status_override") and d0["status"] == "none":
        d0["status"] = d0["status_override"]
    if d0.get("status_text_override"):
        d0["status_text"] = d0["status_text_override"]
    if premium_stop(h) is None:
        none = ((plan.get("stop") or {}).get("kind") or "none") == "none"
        leg("stop", exit_side(h), open_q, "SL-M", None, None, "no stop in the post: no plan, nothing goes to Kite" if none else "spot stop: watched by the app, exit by hand",
            {"placeable": False})
    else:
        loss = round((stop_px - ref) * open_q * sign, 2) if ref is not None and stop_px is not None else None
        moved = k.get("entry_px") is not None and stop_px == k.get("entry_px")
        leg("stop", exit_side(h), open_q, "SL-M", None, stop_px,
            ("at cost · " if moved else "") + (f"max loss {rupees(loss, sign=True)}" if loss is not None else "max loss —"),
            {"max_loss": loss, "placeable": True})
    if tg:
        gain = round((float(tg[0]) - ref) * q1 * sign, 2) if ref is not None else None
        target_leg(k, out, "t1", exit_side(h), q1, float(tg[0]), ("half" if q2 else "all") + (f" · {rupees(gain, sign=True)} if hit" if gain is not None else ""),
                   {"if_filled": gain})
    if len(tg) > 1 and q2:
        t1s = (k["legs"].get("t1") or {}).get("status")
        target_leg(k, out, "t2", exit_side(h), q2, float(tg[1]),
                   "rest · watched after T1 · the stop moves to cost" if t1s != "complete" else "rest · T1 hit · the stop is at cost",
                   {"after_t1": True})
    now_texts(h, out, bk.last_ltp(h))
    return out


def target_leg(k: dict, out: list, key: str, side: str, q: int, price: float, detail: str, extra: dict) -> None:
    """A target on the card: WATCHED by the app (never a resting order): "SELL 32 at ≥ 270 · watched by the app", badge
    watched (amber) → selling (the market exit sent) → "hit · sold 270.4" (green); "refused" when the exit was refused."""
    lg = k["legs"].get(key) or {}
    mkt = (lg.get("type") or "") == "MARKET"
    s = lg.get("status")
    sym = "≥" if side == "SELL" else "≤"
    dry = bool(k.get("dry"))
    if s == "complete":
        status, text = "complete", f"hit · sold {pl.fmt(lg.get('filled_px'))}" if lg.get("filled_px") is not None else "hit"
    elif s in LIVE and mkt:
        status, text = "open", "selling at market" if side == "SELL" else "buying at market"
    elif s in LIVE:                                      # a resting LIMIT from before: cancelled by the next sync
        status, text = "open", "resting on Kite · cancelled by the next sync"
    elif mkt and s in ("rejected", "failed") or (k.get("watch") == "refused" and k["state"] == "entered"):
        status, text = "rejected", "exit refused · exit by hand"
    elif k["state"] in ("closed", "none") and k.get("reason"):
        status, text = "cancelled", f"not reached · {k.get('reason')}"           # the position closed before this target
    elif k["state"] not in ("entered",):
        status, text = "watched", "watched after the entry fills"
    else:
        status, text = "watched", "watched"
    d = {"leg": key, "name": LEG_NAMES[key], "side": lg.get("side") or side, "qty": (lg.get("qty") if mkt else None) or q, "type": "WATCH",
         "price": price, "trigger": None, "status": status, "status_text": text, "dry": dry and k["state"] != "none",
         "order_id": lg.get("order_id") if mkt else None, "at": t12(lg.get("at"), True) if mkt and lg.get("at") else "", "by": lg.get("by") if mkt else "app",
         "note": lg.get("note") if mkt else None, "detail": f"watched by the app · {detail}", "watched": True,
         "type_text": f"at {sym} {pl.fmt(price)}"}
    d.update(extra or {})
    out.append(d)


def _pct(a: float, b: float) -> str:
    """'+17 %' / '−34 %': how far `a` is from `b`, in % of `b`."""
    v = (float(a) - float(b)) / float(b) * 100 if b else 0.0
    return f"{'+' if v >= 0 else '−'}{abs(v):.1f} %".replace(".0 %", " %")


def now_texts(h: dict, legs: list[dict], ltp: Optional[float]) -> None:
    """The live price on every leg (the 5 s Tips quote poll; no Kite call): where the option trades against the plan.
    Entry "now 196.1 · in the zone 191–198" / "now 337 · level 370 is 9.8 % away" / "now 460 · at market"; stop "now
    196.1 · stop 130 is −34 %"; targets "now 196.1 · target 230 is +17 %"."""
    if ltp is None:
        return
    now = f"now {pl.fmt(ltp)}"
    e = entry_of(h)
    m = e.get("mode") or "market"
    for d in legs:
        if d["leg"] == "entry":
            if d.get("status") == "complete":
                fpx = kstate(h).get("entry_px")
                d["now_text"] = f"{now} · {_pct(ltp, fpx)} from the fill" if fpx else now
                continue
            if pl.is_spot(e):
                d["now_text"] = f"{now} · {e.get('index') or 'the index'} level {pl.fmt(e.get('lo'))}"
            elif m == "zone" or m == "at":
                lo, hi = float(e["lo"]), float(e.get("hi") or e["lo"])
                zone = f"{pl.fmt(lo)}–{pl.fmt(hi)}" if m == "zone" else pl.fmt(lo)
                if lo <= ltp <= hi:
                    d["now_text"] = f"{now} · in the zone {zone}" if m == "zone" else f"{now} · at {zone}"
                else:
                    edge = hi if ltp > hi else lo
                    d["now_text"] = f"{now} · {'zone ' + zone if m == 'zone' else 'level ' + zone} is {abs((ltp - edge) / ltp * 100):.1f} % away"
            elif m in ("above", "below"):
                lv = float(e["lo"])
                past = (ltp >= lv) if m == "above" else (ltp <= lv)
                d["now_text"] = f"{now} · {'past the level ' + pl.fmt(lv) if past else f'level {pl.fmt(lv)} is {abs((lv - ltp) / ltp * 100):.1f} % away'}"
            else:
                d["now_text"] = f"{now} · at market"
        elif d["leg"] == "stop" and d.get("trigger") is not None:
            d["now_text"] = f"{now} · stop {pl.fmt(d['trigger'])} is {_pct(d['trigger'], ltp)}"
        elif d["leg"] in ("t1", "t2") and d.get("price") is not None:
            d["now_text"] = f"{now} · target {pl.fmt(d['price'])} is {_pct(d['price'], ltp)}"


def now_cell(h: dict) -> Optional[dict]:
    """The card's NOW cell (the 5 s Tips quote poll; no Kite call): {ltp, tone up|down|"", sub}. Entered (on Kite, else
    the paper position): green / red against the fill ("−5.6 % from 230"); not entered: neutral, where the price sits
    against the entry ("in the zone 191–198" / "level 370 is 9.8 % away" / "at market")."""
    ltp = bk.last_ltp(h)
    if ltp is None:
        return None
    k = kstate(h)
    pos = h.get("pos") if h.get("taken") else None
    fill = k.get("entry_px") if k["state"] == "entered" else None
    if fill is None and pos and pos.get("state") == "open":
        fill = pos.get("entry_px")
    if fill:
        d = (float(ltp) - float(fill)) * _sign(h)
        return {"ltp": float(ltp), "tone": "up" if d > 0 else "down" if d < 0 else "", "sub": f"{_pct(ltp, fill)} from {pl.fmt(fill)}", "entered": True}
    leg = [{"leg": "entry"}]
    now_texts(h, leg, ltp)
    sub = str(leg[0].get("now_text") or "").split(" · ", 1)
    return {"ltp": float(ltp), "tone": "", "sub": sub[1] if len(sub) > 1 else "", "entered": False}


def kite_actions(config, h: dict) -> list[str]:
    k = kstate(h)
    if mode(config) == "off":
        return ["exit"] if k["state"] in WORKING else []
    if k["state"] in WORKING and bool(k.get("dry")) != (mode(config) == "dry"):
        return [a for a in _actions(config, h, k) if a == "exit" or a.startswith("cancel:")]     # getting out only
    return _actions(config, h, k)


def _actions(config, h: dict, k: dict) -> list[str]:
    acts = []
    legs = k["legs"]
    live = lambda leg: (legs.get(leg) or {}).get("status") in LIVE
    dry = mode(config) == "dry"
    if k["state"] in ("none",) and h.get("status") not in ("closed", "expired") and (h.get("plan") or {}).get("ok") and h.get("tradingsymbol") \
            and not any(r["leg"] == "entry" and r["status"] == "complete" and is_dry_row(r) == dry for r in rows_for(h["id"]) if r["status"] != "voided") \
            and not mix_block(h, dry, _now()):
        acts += ["all", "entry"]
    if k["state"] == "armed":
        acts += ["cancel:entry", "exit"]
    elif k["state"] == "armed_watch":
        acts += ["cancel:entry"]
    elif k["state"] == "armed_wait":
        acts += ["send_now", "cancel:entry"]
    if k["state"] == "entered":
        need_stop = not live("stop") and premium_stop(h) is not None and k["qty_open"] > 0 \
            and not any(live(x) for x in ("exit", "t1", "t2"))
        if need_stop:
            acts += ["rest", "stop"]
        if live("stop"):
            acts += ["modify_stop", "cancel:stop"]
        for leg in ("t1", "t2"):
            if live(leg) and (legs.get(leg) or {}).get("type") == "LIMIT":     # a resting target from before only
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
            "dry": bool(k.get("dry")) and k["state"] != "none", "sim": routes_dry(config, h),
            "beyond_t2": max(0, len((h.get("plan") or {}).get("targets") or []) - 2),
            "arm": k.get("arm"), "by": k.get("by"), "check": k.get("check"), "ai": k.get("ai"), "auto": auto_tag(config, h),
            "needs_activation": needs_activation(h) and k["state"] in ("none", "armed_wait"),
            "entry_what": (k.get("arm") or entry_plan(h, bk.last_ltp(h))).get("what") or "",
            "level_kind": entry_of(h).get("level_kind") if entry_of(h).get("mode") not in (None, "market") else None,
            "time_stop": (h.get("plan") or {}).get("time_stop"), "hold_to_expiry": bool((h.get("plan") or {}).get("hold_to_expiry"))}


def auto_tag(config, h: dict) -> Optional[dict]:
    """The card's auto tag (mode auto only): {state armed|waiting|entered|closed|not_armed|waiting_turn, text}."""
    if mode(config) != "auto":
        return None
    k = kstate(h)
    a = k.get("auto") or {}
    st = k["state"]
    if st == "armed_wait":
        return {"state": "waiting", "text": "auto · waiting for activation"}
    if st in ("armed", "armed_watch"):
        return {"state": "armed", "text": "auto · armed"}
    if st == "entered":
        return {"state": "entered", "text": "auto · in the trade" if k.get("by") == "auto" else "your click · auto follows only its own"}
    if st == "closed" and k.get("by") == "auto":
        why = f" · {k['reason']}" if k.get("reason") else ""
        return {"state": "closed", "text": f"auto · closed{why}"}
    if a.get("why"):
        return {"state": "not_armed", "text": f"auto · not armed: {a['why']}"}
    return None


def _order_view(r: dict, names: dict, entry_qty: dict) -> dict:
    nm = names.get(r["hint_id"], "")
    label = {"entry": f"Entry {nm}".strip(), "stop": "Stop", "t1": "Target 1", "t2": "Target 2 (rest)", "exit": "Exit at market",
             "test": "Connection test"}[r["leg"]]
    if r["leg"] == "t1" and entry_qty.get(r["hint_id"]) and int(r["qty"] or 0) < entry_qty[r["hint_id"]]:
        label += " (half)"
    if r["leg"] in ("t1", "t2") and r.get("order_type") == "MARKET":
        label += " · hit, sold at market"
    lg = {"status": r["status"], "filled_px": r.get("filled_px"), "note": r.get("note")}
    dry = is_dry_row(r)
    return {"dry": dry, "id": r["id"], "hint_id": r["hint_id"], "leg": r["leg"], "label": label, "side": r["side"], "qty": r["qty"],
            "order_type": r["order_type"], "type_text": _type_text(r["order_type"], r.get("price"), r.get("trigger")),
            "price": r.get("filled_px") if r.get("filled_px") is not None else r.get("price"), "status": r["status"],
            "status_text": _status_text(lg, dry) if r["status"] != "open" else "pending", "order_id": r.get("order_id"),
            "time": t12(r.get("placed_at")), "time_s": t12(r.get("placed_at"), True), "by": r.get("by"), "note": r.get("note")}


def today(config, client=None, now: Optional[datetime] = None) -> dict:
    """GET /api/tips/kite: {mode, kill, caps, orders (today, ours only), guards, open, net, last_sync, window, warning}."""
    now = now or _now()
    day = now.date().isoformat()
    dry = mode(config) == "dry"
    rows = view_rows(rows_today(day), dry)
    names = {}
    for hid in {r["hint_id"] for r in rows}:
        h = bk.load(hid)
        if h:
            names[hid] = bk.title(h).replace("BUY ", "", 1).replace("SELL ", "", 1)
    with get_db() as conn:
        last = get_setting(conn, SYNC_KEY) or None
    live = [r for r in rows if r["status"] in LIVE]
    used = realised_today(day, dry)
    out = lite(config, now, rows=rows, used=used)
    eq = {r["hint_id"]: int(r["qty"] or 0) for r in rows_for_hints(list(names)) if r["leg"] == "entry" and r["status"] not in ("rejected", "failed")}
    out.update(orders=[_order_view(r, names, eq) for r in reversed(rows)], guards=base_guards(config, sim() if dry else client, now),
               net=used, last_sync=last, last_sync_label=t12(last, True) if last else None, open=len(live),
               warning=DRY_WARNING if dry else WARNING, at=now.isoformat(timespec="seconds"), dry=dry,
               market_open=market_open("NFO", now), test=last_test(), public_ip=cached_ip(),
               dry_rows=sum(1 for r in rows if is_dry_row(r) and r["status"] != "voided"), day_check=_day_check())
    if mode(config) == "auto":
        out["guards"] = out["guards"] + auto_guards(config, now)
    return out


def _day_check() -> Optional[dict]:
    try:
        from backend.tips import kite_check as kc
        return kc.last_day()
    except Exception:
        return None


def auto_guards(config, now: datetime) -> list[dict]:
    """The Guards panel's auto rows: max open at once, the activation rule."""
    from backend.tips import kite_auto as ka
    mx = int(getattr(config.tips, "kite_auto_max_open", 1))
    n = ka.open_count()
    bp = getattr(config.tips, "kite_auto_book_part", "channel")
    closed = ka.day_closed(now)
    day = [_g(False, f"auto: the channel closed the day at {closed.get('at_label')} · nothing new armed today · a new call re-opens it", "day_closed")] if closed else []
    return day + [_g(n < mx, f"auto: open at once {n} of {mx} · a second hint waits", "auto_open"),
            _g(True, "auto: a level is the trigger · wait words alone wait for the channel's activation", "auto_act"),
            _g(True, "auto: never averages · add-ons ignored · book part with one lot: " + ("booked" if bp == "channel" else "held, stop to cost"), "auto_add")]


def view_rows(rows: list[dict], dry: bool) -> list[dict]:
    """The rows a mode shows: a dry run's while dry, the real ones otherwise (the connection test's in both); voided
    rows are kept in the table of record, not shown."""
    return [r for r in rows if r["status"] != "voided" and (r["leg"] == "test" or is_dry_row(r) == dry)]


def lite(config, now: Optional[datetime] = None, rows: Optional[list] = None, used: Optional[float] = None) -> dict:
    """The summary's `kite`: {mode, kill, caps, open, window, product}."""
    now = now or _now()
    day = now.date().isoformat()
    dry = mode(config) == "dry"
    rows = rows if rows is not None else view_rows(rows_today(day), dry)
    used = used if used is not None else realised_today(day, dry)
    c = config.tips
    n = sum(1 for r in rows if counts_entry(r))
    return {"mode": mode(config), "kill": killed(), "open": sum(1 for r in rows if r["status"] in LIVE),
            "caps": {"entries": n, "max_entries": int(c.kite_max_entries_day), "max_lots": int(c.kite_max_lots),
                     "loss_cap": float(c.kite_loss_cap), "used_loss": round(min(used, 0.0), 2)},
            "window": window_text(config), "product": c.kite_product, "day_closed": _day_closed(now),
            "auto": {"max_open": int(getattr(c, "kite_auto_max_open", 1)), "book_part": getattr(c, "kite_auto_book_part", "channel"),
                     "line": "auto: entries armed on the level / the channel's activation · SL on the fill · targets watched by the app · the channel's exits followed · you are told"}
            if mode(config) == "auto" else None}


def _day_closed(now: datetime) -> Optional[dict]:
    try:
        from backend.tips import kite_auto as ka
        d = ka.day_closed(now)
        return {"at": d.get("at"), "at_label": d.get("at_label"), "text": d.get("text")} if d else None
    except Exception:
        return None


def health_row(config) -> dict:
    """🩺 tips_kite: mode, open orders, the last sync, the kill switch."""
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM tips_orders WHERE status IN ('placed', 'open')").fetchone()[0]
        last = get_setting(conn, SYNC_KEY) or None
    m = mode(config)
    k = killed()
    d = _day_check()
    with get_db() as conn:
        reds = conn.execute("SELECT COALESCE(SUM(json_extract(kite, '$.check.red')), 0) FROM tips_hints WHERE json_extract(kite, '$.day') = ?",
                            (_now().date().isoformat(),)).fetchone()[0] or 0
    chk = f" · last check {t12(d['at'])} · {d.get('red', 0)} red" if d else (f" · {reds} red" if reds else "")
    fresh = bool(d and d.get("day") == _now().date().isoformat())
    return {"key": "tips_kite", "name": "Tips → Kite orders", "ok": True, "warn": bool(k and m != "off") or bool(reds) or bool(fresh and d.get("red")),
            "state": f"{m}{' · kill switch ON' if k else ''} · {n} open order{'s' if n != 1 else ''}{chk}",
            "detail": ("dry run: a simulator fills from live quotes · nothing reaches Kite" if m == "dry" else
                       "auto: entries armed on the level / the channel's activation · the channel's exits followed" if m == "auto" else
                       "by your click, each through a confirm · synced every 10 s while an order is working") + (f" · last sync {t12(last, True)}" if last else ""),
            "at": last}


# --- the dry run: clear ----------------------------------------------------------------------------------------------------

def clear_dry(config, now: Optional[datetime] = None) -> dict:
    """"Clear dry run": today's dry rows end "voided" (never deleted), the simulator's book is emptied and every dry
    hint's legs are reset so the owner can rehearse again. A hint traded dry today still is not traded real today."""
    now = now or _now()
    day = now.date().isoformat()
    with _LOCK:
        rows = [r for r in rows_today(day) if is_dry_row(r) and r["status"] != "voided" and r["leg"] != "test"]
        with get_db() as conn:
            for r in rows:
                _update(r["id"], conn, status="voided")
        dropped = sim().clear()
        hints = 0
        for h in bk.rows("json_extract(kite, '$.dry') = 1"):
            k = kstate(h)
            if k["state"] == "none" and not k["legs"]:
                continue
            k.update(state="none", lots=0, qty=0, qty_open=0, entry_px=None, stop_px=None, auto_rest=False, realised=0.0,
                     reason="dry run cleared", legs={}, by=None)
            _log(k, now, "Kite: dry run cleared · the legs voided, nothing reached Kite")
            save_kite(h)
            hints += 1
    return {"voided": len(rows), "sim_orders": dropped, "hints": hints}


# --- the connection test ---------------------------------------------------------------------------------------------------

def last_test() -> Optional[dict]:
    with get_db() as conn:
        raw = get_setting(conn, TEST_KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


CLOSED_RE = re.compile(r"market(s)? (is |are )?(now )?closed|not open|outside (the )?market hours|after market|exchange is closed|"
                       r"closed for (the )?day|\bamo\b|pre-?open", re.I)
IP_RE = re.compile(r"\bips?\b|whitelist|white-list|white list|allowlist|allow-list|static ip", re.I)
PERM_RE = re.compile(r"permission|not (allowed|enabled|permitted)|forbidden|unauthori[sz]ed", re.I)


def refusal(text: str) -> tuple[str, str]:
    """Kite's refusal → (kind, a plain line): "ip" / "closed" / "permission" / "other" (Kite's own words)."""
    t = str(text or "")
    if IP_RE.search(t):
        ip = cached_ip()
        return "ip", f"static IP not whitelisted · add this server's IP{f' {ip}' if ip else ''} in Kite Connect"
    if CLOSED_RE.search(t):
        return "closed", "market closed: run the test between 9:15 AM and 3:30 PM"
    if PERM_RE.search(t):
        return "permission", "order permission missing on the app key"
    return "other", t[:200] or "Kite gave no reason"


def _nifty_spot(client) -> Optional[float]:
    """NIFTY from the app's last quote (a NIFTY hint's stored spot), else one quote call."""
    with get_db() as conn:
        r = conn.execute("SELECT q.spot FROM tips_quotes q JOIN tips_hints h ON h.id = q.hint_id WHERE h.symbol = 'NIFTY' "
                         "AND q.spot IS NOT NULL ORDER BY q.t DESC LIMIT 1").fetchone()
    if r and r[0]:
        return float(r[0])
    try:
        q = client.quote(["NSE:NIFTY 50"]) or {}
        v = (q.get("NSE:NIFTY 50") or {}).get("last_price")
        return float(v) if v else None
    except Exception:
        return None


def _nifty_rows(now: datetime) -> list[dict]:
    with get_db() as conn:
        as_of = conn.execute("SELECT MAX(as_of) FROM tips_instruments WHERE exchange = 'NFO' AND name = 'NIFTY' AND type = 'PE'").fetchone()[0]
        if not as_of:
            return []
        return [dict(r) for r in conn.execute("SELECT tradingsymbol, expiry, strike, lot_size, type FROM tips_instruments WHERE as_of = ? AND "
                                              "exchange = 'NFO' AND name = 'NIFTY' AND expiry >= ? ORDER BY expiry, strike",
                                              (as_of, now.date().isoformat()))]


def nifty_contracts(client, now: datetime) -> list[dict]:
    """Today's NIFTY option rows; the NFO list is loaded once a day when nothing has needed it yet (no hint today)."""
    rows = _nifty_rows(now)
    if rows or client is None or is_dry(client):
        return rows
    try:
        from backend.tips import track as tr
        tr.refresh_instruments(client, "NFO", {"NIFTY"}, now.date())
    except Exception as exc:
        logger.warning("Kite test: NFO list not loaded: %s", str(exc)[:120])
        return []
    return _nifty_rows(now)


def test_contract(client, now: datetime) -> Optional[dict]:
    """A far OTM NIFTY PE (nearest listed expiry, the strike ~8 % under the spot; with no spot the lowest strike, the
    lowest premium) from tips_instruments, loading the list when it is not there yet. {tradingsymbol, exchange, lot_size,
    strike, expiry, spot}."""
    rows = [r for r in nifty_contracts(client, now) if r["type"] == "PE"]
    if not rows:
        return None
    pool = [r for r in rows if r["expiry"] == rows[0]["expiry"]]
    spot = _nifty_spot(client)
    pick = min(pool, key=lambda r: abs(float(r["strike"]) - spot * 0.92)) if spot else pool[0]
    return {"tradingsymbol": pick["tradingsymbol"], "exchange": "NFO", "lot_size": int(pick["lot_size"] or 1),
            "strike": pick["strike"], "expiry": pick["expiry"], "spot": spot}


REHEARSAL_CHANNEL = "rehearsal"


def rehearsal_hint(config, client, state=None, now: Optional[datetime] = None) -> dict:
    """Dry run only: a made-up call on the nearest NIFTY CE close to the money, at its last price, SL 20 % under,
    targets 20 % / 40 % over, 1 lot, in channel "rehearsal" (never pushed, never in the channel's record). The owner
    then presses Place all and watches the DRY legs. {hint_id, text}."""
    from backend.tips import reader as rd
    now = now or _now()
    if mode(config) != "dry":
        raise TipsError(409, "a rehearsal hint is for the dry run: switch the segment to Dry run first")
    real = getattr(state, "client", None) if state is not None else None
    rows = [r for r in nifty_contracts(real, now) if r["type"] == "CE"]
    spot = _nifty_spot(real) if real is not None else None
    if rows and spot:
        pool = [r for r in rows if r["expiry"] == rows[0]["expiry"]]
        pick = min(pool, key=lambda r: abs(float(r["strike"]) - spot))
        strike = int(float(pick["strike"]))
        ltp = None
        try:
            q = real.quote([f"NFO:{pick['tradingsymbol']}"]) or {}
            v = (q.get(f"NFO:{pick['tradingsymbol']}") or {}).get("last_price")
            ltp = float(v) if v else None
        except Exception:
            ltp = None
        px = ltp or 100.0
    else:
        strike, px = 25000, 100.0
    sl, t1, t2 = round(px * 0.8), round(px * 1.2), round(px * 1.4)
    text = f"REHEARSAL · BUY NIFTY {strike} CE at {round(px)}\nSL {sl}\nTGT {t1}/{t2}\n1 lot · dry run only"
    pid = int(now.timestamp())
    r = rd.ingest(config, REHEARSAL_CHANNEL, pid, now, text, False, source="live", push=False, model=False, client=real)
    hid = r.get("hint_id")
    if not hid:
        raise TipsError(409, "the rehearsal post did not decode (kind %s)" % r.get("kind"))
    _push(config, f"tips|kite|rehearsal|{hid}", f"🧪 Kite DRY · rehearsal hint #{hid} added · press Place all on the Tips page")
    return {"hint_id": hid, "text": text}


def connection_test(config, client, now: Optional[datetime] = None, sleep=None) -> dict:
    """"Test connection…": the session, a far OTM NIFTY PE, one LIMIT buy at half its price (it cannot fill), cancelled
    5 s later and read back from the order book. Proves the session, the static IP and the order permission on the real
    account. Stored in app_settings tips.kite.test; one test per 5 minutes; never in mode off; needs a login."""
    now = now or _now()
    sleep = sleep or _time.sleep
    if mode(config) == "off":
        raise TipsError(409, "Kite orders are off: the connection test runs in Dry run, By my click or Auto")
    if client is None or is_dry(client) or not getattr(client, "has_token", False):
        raise TipsError(409, "log in to Kite first: the test uses the app's own Kite session")
    last = last_test()
    placed = any(s.get("key") == "place" and s.get("ok") for s in (last or {}).get("steps") or [])
    if last and last.get("at") and placed:                 # the gap counts only when an order really went out
        try:
            nxt = datetime.fromisoformat(last["at"]) + timedelta(seconds=TEST_GAP_S)
        except ValueError:
            nxt = None
        if nxt is not None and now < nxt:
            raise TipsError(409, f"one connection test per 5 minutes · the next from {t12(nxt.isoformat(), True)}")
    if not _TEST_LOCK.acquire(blocking=False):
        raise TipsError(409, "a connection test is running")
    try:
        return _run_test(config, client, now, sleep)
    finally:
        _TEST_LOCK.release()


def _run_test(config, client, now: datetime, sleep) -> dict:
    steps: list = []

    def step(key, ok, text, detail=None):
        steps.append({"key": key, "ok": ok, "text": text, "detail": detail})

    def finish(outcome: str, summary: str) -> dict:
        res = {"at": now.isoformat(timespec="seconds"), "at_label": t12(now.isoformat(), False),
               "passed": True if outcome == "passed" else None if outcome == "market_closed" else False,
               "outcome": outcome, "summary": summary, "steps": steps}
        with get_db() as conn:
            set_setting(conn, TEST_KEY, json.dumps(res))
        word = {"passed": "passed", "failed": "FAILED", "market_closed": "market closed"}[outcome]
        _push(config, f"tips|kite|test|{res['at']}", f"🔌 Kite connection test {word} · {summary}")
        return res

    # 1 · the session
    try:
        client.profile()
        step("session", True, "login · session valid")
    except Exception as exc:
        step("session", False, "login · Kite refused the session: log in again", str(exc)[:200])
        return finish("failed", "the Kite session is not valid: log in again")
    # 2 · the contract
    c = test_contract(client, now)
    if not c:
        step("contract", False, "no NIFTY option list: Kite's instrument download failed · try again in a minute")
        return finish("failed", "no contract to test with")
    ts, lot = c["tradingsymbol"], c["lot_size"]
    where = f"~8 % under NIFTY {pl.fmt(round(c['spot']))}" if c.get("spot") else "the lowest strike listed (no NIFTY quote)"
    step("contract", True, f"contract · {ts} · 1 lot = {lot} · {where}")
    # 3 · the price
    ltp = None
    try:
        q = client.quote([f"NFO:{ts}"]) or {}
        v = (q.get(f"NFO:{ts}") or {}).get("last_price")
        ltp = float(v) if v else None
    except Exception:
        ltp = None
    price = max(0.05, round(0.5 * ltp, 1)) if ltp else 0.05
    step("price", True, f"price · LIMIT {pl.fmt(price)}" + (f" (half of the last {pl.fmt(ltp)})" if ltp else " (no quote: the lowest tick)")
         + " · a buy this far under the market cannot fill")
    # 4 · place
    row = {"hint_id": 0, "leg": "test", "side": "BUY", "qty": lot, "order_type": "LIMIT", "price": price, "trigger": None,
           "status": "placed", "order_id": None, "placed_at": now.isoformat(timespec="seconds"), "updated_at": now.isoformat(timespec="seconds"),
           "by": "test", "note": ts, "day": now.date().isoformat()}
    try:
        oid = str(client.place_order(exchange="NFO", tradingsymbol=ts, transaction_type="BUY", quantity=lot, product=config.tips.kite_product,
                                     order_type="LIMIT", price=price, market_protection=None, tag=TEST_TAG))
    except Exception as exc:
        kind, line = refusal(str(exc))
        row.update(status="failed", note=f"{ts} · {str(exc)[:160]}")
        _insert(row)
        if kind == "closed":
            step("place", None, line, str(exc)[:200])
            return finish("market_closed", line)
        step("place", False, f"Kite refused the order · {line}", str(exc)[:200])
        return finish("failed", line)
    row["order_id"] = oid
    rid = _insert(row)
    step("place", True, f"placed · {ts} 1 lot LIMIT {pl.fmt(price)} · order {oid} · static IP and order permission accepted")
    # 5 · wait
    sleep(TEST_WAIT_S)
    step("wait", True, f"rested {TEST_WAIT_S} s on Kite")
    # 6 · cancel and read it back
    cancel_err = None
    try:
        client.cancel_order(oid)
    except Exception as exc:
        cancel_err = str(exc)[:200]
    try:
        o = next((x for x in (client.orders() or []) if str(x.get("order_id")) == oid), None)
    except Exception as exc:
        o, cancel_err = None, (cancel_err or "") + f" · order book: {str(exc)[:120]}"
    if o is None:
        step("cancel", False, f"order {oid} is not in Kite's order book: check it in Kite by hand", cancel_err)
        return finish("failed", f"order {oid} not found after the cancel: check Kite by hand")
    st, fq = kite_status(o.get("status")), int(o.get("filled_quantity") or 0)
    _update(rid, status=st, filled_px=float(o.get("average_price") or 0) or None, filled_qty=fq or None,
            note=f"{ts}" + (f" · {o.get('status_message')}" if o.get("status_message") else ""))
    if st == "complete" or fq > 0:
        step("cancel", False, f"FILLED: {fq} bought at {pl.fmt(o.get('average_price'))} · exit it by hand in Kite NOW", cancel_err)
        return finish("failed", f"the test order FILLED ({fq} of {ts}): exit it by hand in Kite now")
    if st == "open":
        step("cancel", False, f"still open on Kite: cancel order {oid} by hand", cancel_err)
        return finish("failed", f"the test order is still open: cancel {oid} in Kite by hand")
    if st == "rejected":
        kind, line = refusal(o.get("status_message") or "")
        if kind == "closed":
            step("cancel", None, line + " · nothing filled", o.get("status_message"))
            return finish("market_closed", line)
        if kind in ("ip", "permission"):
            step("cancel", False, f"Kite rejected it · {line}", o.get("status_message"))
            return finish("failed", line)
        step("cancel", True, f"Kite rejected it ({line}) · nothing filled", o.get("status_message"))
    else:
        step("cancel", True, f"cancelled {TEST_WAIT_S} s later · nothing filled")
    # 7 · market protection (informational: no second order is placed)
    step("protection", True, f"market protection: sent with real orders ({pl.fmt(float(config.tips.kite_market_protection))}%)")
    return finish("passed", f"session, static IP and order permission OK · {ts} LIMIT {pl.fmt(price)} placed and cancelled · nothing was bought")


# --- the server's public IP (the one to whitelist in Kite Connect) -------------------------------------------------------

_IP_OK = re.compile(r"^(\d{1,3}(\.\d{1,3}){3}|[0-9a-fA-F:]{3,45})$")


def _fetch_ip(url: str, timeout: float = 3.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5", "Accept": "text/plain"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(64).decode("ascii", "replace").strip()


def _ip_cache() -> dict:
    with get_db() as conn:
        raw = get_setting(conn, IP_KEY)
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def cached_ip() -> Optional[str]:
    """The last looked-up public IP (None = unknown); never a network call."""
    return _ip_cache().get("ip") or None


def public_ip(now: Optional[datetime] = None, fetch=None, force: bool = False) -> Optional[str]:
    """api.ipify.org, then ifconfig.me (3 s each), cached 10 minutes in app_settings tips.kite.ip (a failed lookup is
    tried again after 2). Called by the Syncer, never on the request path."""
    now = now or _now()
    fetch = fetch or _fetch_ip
    c = _ip_cache()
    if c.get("at") and not force:
        try:
            age = (now - datetime.fromisoformat(c["at"])).total_seconds()
        except ValueError:
            age = None
        if age is not None and 0 <= age < (IP_TTL_S if c.get("ip") else IP_FAIL_TTL_S):
            return c.get("ip") or None
    ip = None
    for url in IP_URLS:
        try:
            v = fetch(url)
        except Exception:
            continue
        if v and _IP_OK.match(v):
            ip = v
            break
    with get_db() as conn:
        set_setting(conn, IP_KEY, json.dumps({"ip": ip, "at": now.isoformat(timespec="seconds")}))
    return ip
