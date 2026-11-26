"""⚡ Tips → Kite orders · PR C: AUTO mode and the Syncer's housekeeping (both modes).

  auto_arm   kite_mode "auto": a hint that becomes placeable (a complete plan, a contract, a lot size and a size: the
             post's lots, else tips.lots, capped by kite_max_lots) is armed by the app with by "auto": the same guards
             as a click (login, market hours, the window, the caps, the loss cap, the kill switch, one per hint) plus
             kite_auto_max_open (open / armed Kite hints at once) and "already traded on Kite" (a second hint on the
             same contract the same day). A failing guard is logged on the card ("auto: not armed · <why>") and pushed
             as ONE Telegram line per hint per cause, never a retry storm; the hint is looked at again quietly on the
             next pass (a hint waiting for the first one to close is armed when it closes).
  follow     auto follows the channel on its own open Kite position: exit / SL hit / book full -> cancel the resting
             orders + MARKET exit; trail / TSL at cost -> the stop to cost (never lower for a buy); "modify SL to N" /
             "REST STOPLOSS N" -> the stop's trigger; book part -> half the open lots at market (one lot open:
             kite_auto_book_part "channel" books it, "hold" keeps it for the target and moves the stop to cost). One
             Telegram line each. In click mode these stay informational (you press the buttons).
  any mode   "add more near 440" is never an order ("add-on ignored (never averaged)"); avoid / cancel / not now / an
             SL hit or exit before the fill cancel an unfilled entry; "active" sends an armed_wait entry, or cancels a
             resting level order not filled yet and sends it at MARKET.
  housekeep  every Syncer pass: an armed_watch entry buys at market on the index cross; an armed_wait one goes when the
             channel has activated; an unfilled entry is cancelled at the end of the entry window, at the time stop
             or the next day; a Kite position still open at the time stop is exited at market (not a hold-to-expiry
             sell); the order check runs on every working hint (a stop missing 60 s after the fill turns red)."""

import json
from datetime import datetime
from typing import Optional

from backend.config import IST
from backend.database.models import get_db, get_setting, set_setting
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm
from backend.tips import book as bk
from backend.tips import kite_orders as ko
from backend.tips import plan as pl
from backend.tips import sizing
from backend.tips.book import TipsError

logger = setup_logger("tips")

AUTO = "🤖 Kite AUTO · "
RETRY_CAUSES = ("market", "window", "max_open", "kill", "login", "entries", "loss", "mismatch", "day_closed")     # these can clear on their own


def _now() -> datetime:
    return datetime.now(IST)


def apfx(h: dict) -> str:
    return "🤖 Kite AUTO DRY · " if ko.kstate(h).get("dry") else AUTO


def app_client():
    """The running app's Kite client (the reader's edit path has none of its own)."""
    try:
        from backend.api import routes
        if routes._state is not None:
            return routes._state.client
    except Exception:
        pass
    return None


# --- the channel closed the day ------------------------------------------------------------------------------------------

DAY_CLOSED_KEY = "tips.day_closed"


def day_closed(now: Optional[datetime] = None, conn=None) -> Optional[dict]:
    """{day, at, at_label, post_id, channel, text} when the channel closed TODAY ("temporarily close the day"), else None."""
    now = now or _now()
    if conn is None:
        with get_db() as c:
            return day_closed(now, c)
    try:
        d = json.loads(get_setting(conn, DAY_CLOSED_KEY) or "null")
    except ValueError:
        return None
    if not d or d.get("day") != now.date().isoformat() or d.get("reopened"):
        return None
    return d


def close_day(conn, channel: str, post_id: int, at: datetime, text: str) -> Optional[str]:
    """The channel closed the day: the per-day flag (auto arms nothing new for the rest of the day; open positions and
    resting stops are untouched). Returns the Telegram line, or None when today is already closed."""
    if day_closed(at, conn):
        return None
    lbl = ampm(at)
    set_setting(conn, DAY_CLOSED_KEY, json.dumps({"day": at.date().isoformat(), "at": at.isoformat(timespec="seconds"), "at_label": lbl,
                                                  "post_id": int(post_id), "channel": channel, "text": (text or "").strip()[:120]}))
    logger.info("Tips: the channel closed the day at %s (post %s)", lbl, post_id)
    q = (text or "").strip().replace("\n", " ")[:60]
    return f'📕 Tips · the channel closed the day at {lbl} ("{q}") · auto arms nothing new today · open positions and resting stops untouched'


def reopen_day(conn, hid: int, at: datetime) -> Optional[str]:
    """A new call from the channel the same day: it is trading again. Returns the Telegram line, or None."""
    d = day_closed(at, conn)
    if not d:
        return None
    d.update(reopened=at.isoformat(timespec="seconds"), reopened_by=hid)
    set_setting(conn, DAY_CLOSED_KEY, json.dumps(d))
    logger.info("Tips: day re-opened by a new call (#%s)", hid)
    return f"📗 Tips · day re-opened by a new call (#{hid}) · the channel closed it at {d.get('at_label')} · auto arms again"


# --- auto: arm -----------------------------------------------------------------------------------------------------------

def placeable(h: dict) -> Optional[str]:
    """None when the hint can go to Kite (a complete plan, a contract, a lot size, a target), else why not."""
    plan = h.get("plan") or {}
    if not plan.get("ok"):
        return f"no plan: {plan.get('why') or 'not decoded'}"
    if not h.get("tradingsymbol"):
        return f"no contract: {h.get('untracked_why') or 'no instrument'}"
    if not h.get("lot_size"):
        return "no lot size"
    if not plan.get("targets"):
        return "no target"
    return None


def auto_lots(config, h: dict) -> int:
    return max(1, min(ko.default_lots(config, h), sizing.max_lots(config)))


def lots_text(config, h: dict, lots: int) -> str:
    """"4 lots (2 × 2)" with lots ×N (", capped at 6" when kite_max_lots cut it), else "2 lots"."""
    n = int(lots)
    txt = f"{n} lot{'s' if n != 1 else ''}"
    try:
        sz = sizing.size(config, h, bk.last_ltp(h) if h.get("id") else None)
    except Exception:
        return txt
    if int(sz.get("mult") or 1) > 1 and sz["src"] != "risk":
        txt += f" ({sz['base']} × {sz['mult']}" + (f" = {sz['raw']}, capped at {n}" if sz["raw"] != n else "") + ")"
    return txt


def _same_contract_today(h: dict, day: str, config=None) -> Optional[str]:
    """Why a second hint on the same contract may not arm: another hint on it is still armed / open (always), or — with
    tips.kite_reenter = "never" — any entry on it today at all. A fresh call after the earlier position closed arms by default
    (23 Nov: the channel re-called a strike it had already booked; the app waited for a click)."""
    sym = h.get("tradingsymbol")
    if any(x["id"] != h["id"] and x.get("tradingsymbol") == sym and ko.kstate(x)["state"] in ko.WORKING
           for x in bk.rows("json_extract(kite, '$.state') IN ('armed', 'armed_watch', 'armed_wait', 'entered')")):
        return "already on Kite (the same contract is armed or open under another hint)"
    if config is not None and getattr(config.tips, "kite_reenter", "new call") == "never":
        with get_db() as conn:
            rows = conn.execute("SELECT o.hint_id FROM tips_orders o JOIN tips_hints t ON t.id = o.hint_id WHERE o.day = ? AND o.leg = 'entry' "
                                "AND o.status NOT IN ('rejected', 'failed', 'voided', 'cancelled') AND t.tradingsymbol = ? AND o.hint_id != ?",
                                (day, sym, h["id"])).fetchall()
        if rows:
            return "already traded on Kite (the same contract today · kite_reenter = never)"
    return None


def open_count(exclude: Optional[int] = None) -> int:
    """Kite hints working now (armed or entered): kite_auto_max_open counts these."""
    return sum(1 for x in bk.rows("json_extract(kite, '$.state') IN ('armed', 'armed_watch', 'armed_wait', 'entered')") if x["id"] != exclude)


def _note(config, h: dict, cause: str, text: str, now: datetime, notify: bool) -> None:
    """auto: not armed · <why>: logged on the card and pushed once per hint per cause."""
    k = ko.kstate(h)
    a = dict(k.get("auto") or {})
    causes = list(a.get("causes") or [])
    a.update(why=text, cause=cause, at=now.isoformat(timespec="seconds"))
    if cause not in causes:
        causes.append(cause)
        a["causes"] = causes
        k["auto"] = a
        ko._log(k, now, f"auto: not armed · {text}")
        ko.save_kite(h)
        if notify:
            bk.push(config, f"tips|kite|auto|{h['id']}|{cause}", f"{apfx(h)}#{h['id']} not armed · {text}")
        return
    a["causes"] = causes
    k["auto"] = a
    ko.save_kite(h)


def auto_arm(config, client, h: dict, now: Optional[datetime] = None, notify: bool = True, fold: Optional[str] = None) -> Optional[dict]:
    """Arm one hint in auto mode (see the module doc). Returns the hint when it was armed, else None. `fold`: the
    caller's own message carries the outcome, so "armed" (the plan completed + armed line) or "all" (the new call's
    "auto: …" line: the "not armed · …" line too) is not pushed here (still logged on the card)."""
    now = now or _now()
    push_armed = notify and not fold
    if fold == "all":
        notify = False
    if ko.mode(config) != "auto":
        return None
    h = bk.load(h["id"]) or h
    k = ko.kstate(h)
    if k["state"] != "none" or h.get("status") in ("closed", "expired", "skipped", "paused") or h.get("source") not in ("live", "preview"):
        return None
    if "refused" in ((k.get("auto") or {}).get("causes") or []) or "plan_day" in ((k.get("auto") or {}).get("causes") or []):
        return None
    plan = h.get("plan") or {}
    if plan.get("day") and plan["day"] != now.date().isoformat():
        return None                                      # planned for another day (a post after the close: the next open)
    why = placeable(h)
    if why:
        _note(config, h, "plan", why, now, notify)
        return None
    from backend.tips import vision as vs                # 📷 vision_mode "check": an open strike mismatch holds the hint
    why = vs.blocks_auto(config, h)
    if why:
        _note(config, h, "mismatch", why, now, notify)
        return None
    closed = day_closed(now)
    if closed:                                           # the channel closed the day: logged on the card, no push
        _note(config, h, "day_closed", f"the channel closed the day at {closed.get('at_label')}", now, False)
        return None
    rearm = bool((k.get("auto") or {}).get("rearm"))
    if [r for r in ko.rows_for(h["id"]) if r["leg"] == "entry" and r["status"] != "voided" and not (rearm and r["status"] == "cancelled")]:
        return None                                      # one Kite entry per hint, never re-entered
    same = _same_contract_today(h, now.date().isoformat(), config)
    if same:
        _note(config, h, "one", same, now, notify)
        return None
    mx = int(getattr(config.tips, "kite_auto_max_open", 1) or 1)
    if open_count(h["id"]) >= mx:
        _note(config, h, "max_open", f"waits: {mx} Kite position{'s' if mx != 1 else ''} open at most (kite_auto_max_open)", now, notify)
        return None
    lots = auto_lots(config, h)
    bad = next((g for g in ko.guards(config, client, h, lots, now, "entry") if not g["ok"]), None)
    if bad:
        _note(config, h, bad["key"], bad["text"], now, notify)
        return None
    try:
        h = ko.place_entry(config, client, h["id"], lots, by="auto", now=now, auto_rest=True, notify=False)
    except TipsError as exc:
        _note(config, bk.load(h["id"]) or h, "refused", str(exc), now, notify)
        return None
    k = ko.kstate(h)
    k["auto"] = {"armed_at": now.isoformat(timespec="seconds"), "causes": (k.get("auto") or {}).get("causes") or []}
    ko.save_kite(h)
    arm = k.get("arm") or {}
    tail = _tail(h)
    if push_armed:
        bk.push(config, f"tips|kite|auto|{h['id']}|armed",
                f"{apfx(h)}#{h['id']} armed · {ko.entry_side(h)} {lots_text(config, h, k['lots'])} · {k['qty']} qty {bk.title(h).split(' ', 1)[1]} · {arm.get('text') or 'MARKET'} · {tail}")
    return h


def _tail(h: dict) -> str:
    k = ko.kstate(h)
    arm = k.get("arm") or {}
    return ("waits for the channel's activation" if k["state"] == "armed_wait" else
            f"the app watches {arm.get('index') or 'the index'}" if k["state"] == "armed_watch" else
            "resting on Kite" if arm.get("kind") in ("slm", "limit") else "sent at market") \
        + (" · activation words: the level decides" if ko.entry_of(h).get("wait_words") and k["state"] != "armed_wait" else "")


def armed_bits(config, h: dict) -> str:
    """"armed 1 lot · SL trigger 750 · resting on Kite" (the hint's Kite entry as armed)."""
    k = ko.kstate(h)
    arm = k.get("arm") or {}
    return f"armed {lots_text(config, h, int(k.get('lots') or 1))} · {arm.get('text') or 'MARKET'} · {_tail(h)}"


def state_line(config, h: dict) -> str:
    """💡 The new call's last line (T3): "auto: armed 1 lot · SL trigger 750 · resting on Kite" | "auto: waits for the
    plan" | "auto: not armed · <why>" | "auto: off"."""
    if ko.mode(config) != "auto":
        return "auto: off"
    k = ko.kstate(h)
    if k["state"] in ko.ARMED:
        return ("auto DRY: " if k.get("dry") else "auto: ") + armed_bits(config, h)
    if k["state"] == "entered":
        return "auto: entered"
    a = k.get("auto") or {}
    if a.get("cause") == "plan":
        return "auto: waits for the plan"
    if a.get("why"):
        return f"auto: not armed · {a['why']}"
    return "auto: not armed"


def auto_scan(config, client, now: Optional[datetime] = None, notify: bool = True) -> int:
    """The Syncer's pass in auto mode: every open hint of today not on Kite yet is offered to auto_arm. How many armed."""
    now = now or _now()
    if ko.mode(config) != "auto":
        return 0
    n = 0
    for h in bk.rows("status IN ('new', 'waiting', 'active') AND (kite IS NULL OR json_extract(kite, '$.state') IN ('none'))", order="id"):
        try:
            if auto_arm(config, client, h, now, notify):
                n += 1
        except Exception:
            logger.exception("Tips Kite auto arm #%s failed", h.get("id"))
    return n


# --- the channel's follow-ups ---------------------------------------------------------------------------------------------

def _line(config, h: dict, now: datetime, text: str, notify: bool, key: str) -> None:
    k = ko.kstate(h)
    ko._log(k, now, text if text.startswith(("Kite", "auto")) else f"auto: {text}")
    ko.save_kite(h)
    if notify:
        bk.push(config, f"tips|kite|auto|{h['id']}|{key}|{now.isoformat(timespec='seconds')}", f"{apfx(h)}#{h['id']} {text}")


def on_followup(config, client, hid: int, act: dict, text: str, now: Optional[datetime] = None, notify: bool = True) -> Optional[str]:
    """The Kite consequence of a channel follow-up (see the module doc). Returns the line, or None (nothing to do)."""
    now = now or _now()
    h = bk.load(hid)
    if not h:
        return None
    k = ko.kstate(h)
    a = (act or {}).get("action")
    quote = (text or "").strip().replace("\n", " ")[:50]
    cl = ko.sim() if k.get("dry") else client
    auto = ko.mode(config) == "auto"
    with ko._LOCK:
        if a == "add":
            if act.get("size_lots"):                    # 📏 "2 lots\n\nAdd 2 lots near 700": the size first, then the add
                _on_size(config, cl, h, {"lots": act["size_lots"]}, now)
                h = bk.load(hid) or h
            return _on_add(config, cl, h, act, quote, now, notify)
        sized = _on_size(config, cl, h, act, now) if a in ("size", "active") and act.get("lots") else None
        if a == "size":
            return sized
        if k["state"] in ko.ARMED:
            if a in ("avoid", "sl_hit", "exit"):
                ko.disarm(config, cl, h, now, f'the channel: "{quote}"', notify)
                return "unfilled entry cancelled"
            if a == "active":
                try:
                    return ko.activate(config, cl, h, now, notify)
                except TipsError as exc:
                    return f"not sent: {exc}"
            if a == "stop" and not act.get("spot") and act.get("value") is not None:
                old = k.get("stop_px")
                k["stop_px"] = float(act["value"])
                ko._log(k, now, f"Kite: plan stop revised {pl.fmt(old)} → {pl.fmt(act['value'])} (channel) · the stop placed on the fill uses it")
                ko.save_kite(h)
                return "stop revised for the fill"
            return None
        if k["state"] != "entered" or k.get("by") != "auto" or not auto or int(k.get("qty_open") or 0) <= 0:
            return None
        if ko.killed():
            return None
        try:
            return _follow_open(config, cl, h, act, quote, now, notify)
        except TipsError as exc:
            _line(config, h, now, f'auto: the channel "{quote}" · not followed: {exc}', notify, "fail")
            return f"not followed: {exc}"


def _on_add(config, cl, h: dict, act: dict, quote: str, now: datetime, notify: bool) -> str:
    """📏 The channel's add ("we will add 2 more near 125"): ALWAYS logged on the card + one Telegram line; acted on only
    with tips.kite_follow_adds on (auto: armed now on its own entered position; click: the "Place add…" button)."""
    k = ko.kstate(h)
    if act.get("level") is None:                        # "add more" with no price: nothing to arm
        _line(config, h, now, f'the channel "{quote}" · add-on with no level · ignored (never averaged)', notify, "add")
        return "add-on ignored (never averaged)"
    off = {"lots": act.get("lots"), "level": act.get("level"), "cond": act.get("cond"), "text": quote, "at": now.isoformat(timespec="seconds")}
    if not isinstance(k.get("add"), dict):
        k["add_offer"] = off
    txt = ko.add_text(off)
    if not getattr(config.tips, "kite_follow_adds", False):
        _line(config, h, now, f"{txt} · add-on ignored (never averaged) · {ko.ADD_OFF}", notify, "add")
        return f"{txt} · add-on ignored (never averaged) · {ko.ADD_OFF}"
    if ko.mode(config) == "auto" and k.get("by") == "auto" and k["state"] == "entered":
        try:
            out = ko._arm_add(config, cl, h, now, "auto", notify=False)
        except TipsError as exc:
            out = str(exc)
        _line(config, h, now, f"{txt} · {out}", notify, "add")
        ko.after_event(config, h, now, notify)
        return out
    why = ko.add_block(config, cl, h, now) if k["state"] == "entered" else ("waits for the first fill" if k["state"] in ko.ARMED else None)
    out = f"{txt} · " + (f"not armed: {why}" if why and why != "waits for the first fill" else
                         "armed after the first fill" if why else f"Place add… on the card (your click) · reply add {h['id']}")
    _line(config, h, now, out, notify, "add")
    return out


def _on_size(config, cl, h: dict, act: dict, now: datetime) -> Optional[str]:
    """📏 "2-2 lots" / "buy 2 lots here" before the fill (post mode): an armed entry is re-planned to the new size (the
    caps and the risk guard first; a resting order's quantity modified on Kite); after the fill it is logged only."""
    k = ko.kstate(h)
    n = int(act.get("lots") or 0)
    if n < 1:
        return None
    if k["state"] in ("entered", "closed"):
        ko._log(k, now, f"Kite: the channel's size {n} lots · after the fill: logged only")
        ko.save_kite(h)
        return "size logged (after the fill)"
    if k["state"] not in ko.ARMED:
        return None
    if str(getattr(config.tips, "kite_size_mode", "post")) != "post":
        ko._log(k, now, f"Kite: the channel's size {n} lots · kite_size_mode {config.tips.kite_size_mode}: not used")
        ko.save_kite(h)
        return "size noted (not post mode)"
    lot = int(h.get("lot_size") or 0) or 1
    m = sizing.mult_of(config)                          # 📏 the channel's "2-2 lots" × lots ×N, then the caps
    if m > 1:
        n *= m
    if k.get("by") == "auto":
        n = max(1, min(n, sizing.max_lots(config)))
    if n > sizing.max_lots(config):
        why = f"{n} lots > the cap {sizing.max_lots(config)}"
    else:
        g = sizing.check(config, h, n, bk.last_ltp(h))
        why = sizing.held_text(g) if g["held"] else None
    if why:
        ko._log(k, now, f"Kite: the channel's size {n} lots · not re-planned: {why}")
        ko.save_kite(h)
        return f"size not re-planned: {why}"
    if n == int(k.get("lots") or 0):
        return None
    old = int(k.get("lots") or 0)
    if k["state"] == "armed":
        lg = k["legs"].get("entry") or {}
        if lg.get("status") in ko.LIVE:
            try:
                cl.modify_order(lg["order_id"], quantity=n * lot)
                ko._update(lg["row"], qty=n * lot)
                lg["qty"] = n * lot
            except Exception as exc:
                ko._log(k, now, f"Kite: the channel's size {n} lots · the resting entry could not be resized: {str(exc)[:80]}")
                ko.save_kite(h)
                return "size not re-planned: Kite refused the change"
    k.update(lots=n, qty=n * lot)
    ko._log(k, now, f"Kite: size re-planned {old} → {n} lots ({n * lot} qty) · the channel's size before the fill")
    ko.save_kite(h)
    return f"size re-planned {old} → {n} lots"


def _to_cost(config, cl, h: dict, now: datetime, why: str) -> str:
    k = ko.kstate(h)
    px = k.get("entry_px")
    st = k["legs"].get("stop") or {}
    if px is None or st.get("status") not in ko.LIVE:
        return "no stop on Kite to move"
    cur = st.get("trigger")
    buy = ko.entry_side(h) == "BUY"
    if cur is not None and ((buy and float(cur) >= float(px)) or (not buy and float(cur) <= float(px))):
        return f"stop already at {pl.fmt(cur)} (cost {pl.fmt(px)} or better) · kept"
    ltp = bk.last_ltp(h)
    if ltp is not None and ((buy and ltp <= px) or (not buy and ltp >= px)):
        return f"the price {pl.fmt(ltp)} is not past cost {pl.fmt(px)} · stop kept at {pl.fmt(cur)}"
    ko._move_stop(cl, h, float(px), now, why, config)
    return f"stop → cost {pl.fmt(px)}"


def _follow_open(config, cl, h: dict, act: dict, quote: str, now: datetime, notify: bool) -> Optional[str]:
    k = ko.kstate(h)
    a = act.get("action")
    hid = h["id"]
    if a in ("exit", "sl_hit"):
        if (k["legs"].get("exit") or {}).get("status") in ko.LIVE:
            return None
        k["exit_why"] = "the channel's exit" if a == "exit" else "the channel's SL hit"
        ko.save_kite(h)
        ko.exit_now(config, cl, hid, now=now, by="auto", notify=False, why=f'the channel: "{quote}"')
        h = bk.load(hid)
        _line(config, h, now, f'followed the channel "{quote}" · resting orders cancelled · exit {ko.exit_side(h)} {ko.kstate(h)["qty_open"]} MARKET', notify, "exit")
        return "exit at market"
    if a == "trail":
        out = _to_cost(config, cl, h, now, "auto: the channel trails")
        ko.save_kite(h)
        _line(config, h, now, f'followed the channel "{quote}" · {out}', notify, "trail")
        ko.after_event(config, h, now, notify)
        return out
    if a == "stop" and not act.get("spot"):
        st = k["legs"].get("stop") or {}
        if st.get("status") not in ko.LIVE:
            out = "no stop on Kite to modify"
        else:
            ltp = bk.last_ltp(h)
            px = float(act["value"])
            buy = ko.entry_side(h) == "BUY"
            if ltp is not None and ((buy and px >= ltp) or (not buy and px <= ltp)):
                out = f"the new stop {pl.fmt(px)} is past the price {pl.fmt(ltp)} · not modified · your call"
            else:
                ko._move_stop(cl, h, px, now, "auto: the channel's SL", config)
                out = f"stop → {pl.fmt(px)}"
        ko.save_kite(h)
        _line(config, h, now, f'followed the channel "{quote}" · {out}', notify, "stop")
        ko.after_event(config, h, now, notify)
        return out
    if a == "partial":
        lot = int(h.get("lot_size") or 1)
        open_lots = int(k["qty_open"]) // lot
        if open_lots > 1:
            q = ko.part_lots(open_lots, config) * lot            # kite_book_part: half (floor) or one lot
            if (k["legs"].get("exit") or {}).get("status") in ko.LIVE:
                return None
            rs = act.get("rest_stop") if not act.get("spot") else None
            k["rest_stop_after_part"] = float(rs) if rs is not None else None
            k["cost_after_part"] = bool(act.get("cost")) and rs is None     # an explicit rest stop wins over "cost"
            # margin: a second sell beside the resting stop is refused (20 Nov) → the stop is cut to the rest FIRST
            st = k["legs"].get("stop") or {}
            rest = int(k["qty_open"]) - q
            if st.get("status") in ko.LIVE and int(st.get("qty") or 0) > rest:
                try:
                    kw = ko._stop_kw(config, h, float(st.get("trigger") or k.get("stop_px")), rest)
                    cl.modify_order(st["order_id"], **kw)
                    ko._update(st["row"], qty=rest, price=kw["price"], order_type="SL")
                    st.update(qty=rest, price=kw["price"], type="SL")
                except Exception as exc:
                    out = f"not booked: the stop could not be cut to {rest} first (a second sell beside it would be refused for margin) · Kite: {str(exc)[:100]}"
                    ko.save_kite(h)
                    _line(config, h, now, f'the channel "{quote}" · {out}', notify, "part")
                    return out
            ko._place(config, cl, h, "exit", ko.exit_side(h), q, "MARKET", now, "auto")
            out = (f"booked part: {ko.exit_side(h)} {q} of {k['qty_open']} MARKET · stop cut to {rest} first"
                   + (f" · rest stop → {pl.fmt(rs)}" if rs is not None else " · the stop to cost on the fill" if act.get("cost") else ""))
            ko.save_kite(h)
            _line(config, h, now, f'followed the channel "{quote}" · {out}', notify, "part")
            ko.after_event(config, h, now, notify)
            return out
        if open_lots == 1 and str(getattr(config.tips, "kite_auto_book_part", "channel")) == "channel":
            k["exit_why"] = "the channel's book part (one lot: booked)"
            ko.save_kite(h)
            ko.exit_now(config, cl, hid, now=now, by="auto", notify=False, why="book part with one lot open: the lot is booked")
            h = bk.load(hid)
            _line(config, h, now, f'followed the channel "{quote}" · one lot open: booked at market', notify, "part")
            return "one lot booked"
        out = "one lot open: kept for the target"
        if act.get("rest_stop") is not None and not act.get("spot"):
            out += " · " + ko.rest_stop_to(config, cl, h, float(act["rest_stop"]), now, "auto: book part, rest SL")
        elif act.get("cost"):
            out += " · " + _to_cost(config, cl, h, now, "auto: book part, TSL at cost")
        ko.save_kite(h)
        _line(config, h, now, f'the channel "{quote}" · {out}', notify, "part")
        ko.after_event(config, h, now, notify)
        return out
    return None


def on_new(config, client, hid: int, now: Optional[datetime] = None, notify: bool = True, fold: Optional[str] = None) -> Optional[dict]:
    """A new hint, a completed plan, a symbol set: auto arms it when it is placeable."""
    if ko.mode(config) != "auto":
        return None
    h = bk.load(hid)
    if not h:
        return None
    with ko._LOCK:
        return auto_arm(config, client, h, now or _now(), notify, fold)


def on_edited(config, client, hid: int, before: dict, now: Optional[datetime] = None, notify: bool = True) -> Optional[str]:
    """The channel edited a hint's post: an armed Kite entry is checked against the new text (strike / option type /
    level changed -> the resting order cancelled and logged; auto re-arms it)."""
    now = now or _now()
    h = bk.load(hid)
    if not h:
        return None
    k = ko.kstate(h)
    if k["state"] not in ko.ARMED:
        return None
    e0, e1 = before.get("entry") or {}, h.get("entry") or {}
    changed = [x for x in ("strike", "opt_type", "symbol") if before.get(x) != h.get(x)]
    if (e0.get("mode"), e0.get("lo"), e0.get("hi"), e0.get("level_kind")) != (e1.get("mode"), e1.get("lo"), e1.get("hi"), e1.get("level_kind")):
        changed.append("level")
    if not changed:
        ko._log(k, now, "Kite: the post was edited · strike and level unchanged · the armed entry stays")
        ko.save_kite(h)
        return "unchanged"
    cl = ko.sim() if k.get("dry") else client
    with ko._LOCK:
        ko.disarm(config, cl, h, now, f"the post was edited ({' / '.join(changed)} changed)", notify)
        if ko.mode(config) == "auto":
            h = bk.load(hid)
            k = ko.kstate(h)
            k["auto"] = {"rearm": True}
            ko.save_kite(h)
            auto_arm(config, client, h, now, notify)
    return "re-checked: " + " / ".join(changed) + " changed"


# --- housekeeping (both modes) ------------------------------------------------------------------------------------------------

def _crossed(arm: dict, spot: Optional[float]) -> bool:
    if spot is None or arm.get("level") is None:
        return False
    return spot >= float(arm["level"]) if arm.get("dir") != "below" else spot <= float(arm["level"])


def _since(iso: Optional[str], now: datetime) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt


def housekeep(config, client, now: Optional[datetime] = None, notify: bool = True, dry: Optional[bool] = None) -> list[str]:
    """One pass over the working hints (dry ones with the simulator; real ones while their market is open). `dry`
    limits it to one book. Returns the lines."""
    now = now or _now()
    out = []
    c = config.tips
    for h in bk.rows("json_extract(kite, '$.state') IN ('armed', 'armed_watch', 'armed_wait', 'entered')", order="id"):
        k = ko.kstate(h)
        is_dry = bool(k.get("dry"))
        if dry is not None and is_dry != dry:
            continue
        cl = ko.sim() if is_dry else client
        if cl is None or (not is_dry and not ko.market_open(h.get("exchange"), now)):
            continue
        try:
            with ko._LOCK:
                ln = _keep_one(config, cl, bk.load(h["id"]) or h, now, notify, c)
            if ln:
                out.append(ln)
        except TipsError as exc:
            out.append(f"#{h['id']}: {exc}")
        except Exception:
            logger.exception("Tips Kite housekeeping #%s failed", h.get("id"))
    return out


def _keep_one(config, cl, h: dict, now: datetime, notify: bool, c) -> Optional[str]:
    k = ko.kstate(h)
    plan = h.get("plan") or {}
    ts = plan.get("time_stop")
    hid = h["id"]
    started = _since((k.get("arm") or {}).get("at") or k.get("entry_at"), now)

    def passed(hhmm: Optional[str]) -> bool:
        """`hhmm` has passed today and the arm / entry came before it (an evening rehearsal is not cancelled at once)."""
        if not hhmm:
            return False
        cut = now.replace(hour=pl.hm(hhmm).hour, minute=pl.hm(hhmm).minute, second=0, microsecond=0)
        return now >= cut and (started is None or started < cut)

    if k["state"] in ko.ARMED:
        if k.get("day") and k["day"] < now.date().isoformat():
            ko.disarm(config, cl, h, now, "a new day · the entry was for yesterday", notify)
            return f"#{hid} disarmed: a new day"
        if h.get("status") in ("closed", "expired"):
            ko.disarm(config, cl, h, now, f"the hint is {h.get('status')}", notify)
            return f"#{hid} disarmed: hint {h.get('status')}"
        if passed(ts):
            ko.disarm(config, cl, h, now, f"time stop {ampm(ts)} · never filled", notify)
            return f"#{hid} disarmed: time stop"
        if passed(c.kite_entry_until):
            ko.disarm(config, cl, h, now, f"entry window ended {ampm(c.kite_entry_until)} · never filled", notify)
            return f"#{hid} disarmed: window end"
        if k["state"] == "armed_wait" and (h.get("run") or {}).get("allowed"):
            return ko.activate(config, cl, h, now, notify)
        if k["state"] == "armed_watch":
            arm = k.get("arm") or {}
            spot = ko.index_ltp(h, cl)
            if _crossed(arm, spot):
                by = "auto" if k.get("by") == "auto" else "app"
                sym = "≥" if arm.get("dir") != "below" else "≤"
                ko._fire(config, cl, h, now, by, f"{arm.get('index') or 'the index'} {pl.fmt(spot)} {sym} {pl.fmt(arm.get('level'))}", notify, force_market=True)
                return f"#{hid} index crossed"
        return None
    if k["state"] == "entered" and int(k.get("qty_open") or 0) > 0:
        if (k["legs"].get("add") or {}).get("status") in ko.LIVE and (passed(ts) or passed(c.kite_entry_until) or ko.killed()):
            ko.cancel_add(cl, h, now, "time stop" if passed(ts) else "the kill switch" if ko.killed() else f"entry window ended {ampm(c.kite_entry_until)}")
            ko.save_kite(h)
        if ts and not plan.get("hold_to_expiry") and passed(ts) and (k["legs"].get("exit") or {}).get("status") not in ko.LIVE:
            k["exit_why"] = f"time stop {ampm(ts)}"
            ko.save_kite(h)
            ko.exit_now(config, cl, hid, now=now, by="auto" if k.get("by") == "auto" else "app", notify=notify, why=f"time stop {ampm(ts)}")
            return f"#{hid} time stop exit"
        from backend.tips import kite_check as kc
        kc.after_event(config, h, now, notify, ai=False)       # a stop missing 60 s after the fill turns red here
    return None


def anything_armed() -> bool:
    with get_db() as conn:
        return bool(conn.execute("SELECT 1 FROM tips_hints WHERE json_extract(kite, '$.state') IN ('armed_watch', 'armed_wait', 'armed', 'entered') LIMIT 1").fetchone())
