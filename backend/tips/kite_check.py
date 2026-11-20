"""⚡ Tips → Kite orders · PR C: the order check, after EVERY Kite event (placed / filled / modified / cancelled /
rejected), code first and the AI second; and the end-of-day reconciliation at 3:35 PM.

  code     check(): the post text + the decoded plan against the order rows: the contract (the strike is in the post,
           the tradingsymbol is the hint's, an index read from the strike is allowed but noted), the side (a BUY hint
           buys its entry and sells its exits), the quantity (lots × lot size, lots under the cap), the entry fill on
           the right side of the level (a buy above X fills at X·0.98 or more; a zone buy at or under its top), the
           stop trigger = the plan's stop (or a logged move), the target prices = the plan's, one entry per hint, a
           stop resting within 60 s of the entry fill, no target resting on Kite (only the stop rests; the targets are watched
           by the app), no position left without a stop (the target watch's refused re-place), nothing open after the
           time stop. {ok, items [{level red|amber,
           text, hand}], red, at} on the hint's kite ("check"). A red item: one Telegram line "⚠️ Kite check · #6 ·
           <text> · your call" (kind tips, never held), a red line on the card and a Home "Needs a look" item naming
           the hand action. The check never places, modifies or cancels anything.
  AI       ai_read(): the day model through the AI queue (kind "tips_kite", prompt kite-check-v1) reads the post, the
           follow-ups, the plan, the order rows and the code check, and writes 3 lines tagged [AI]: what the app did and
           why · does it match what the channel meant · what to look at. A line with a figure that is not in the input
           is dropped. Kept in ai_notes (never deleted); "AI read pending" while the model is unreachable.
  day      day_check() at 3:35 PM on trading days (backend/alerts/jobs.py): every tips_orders row of today against the
           hints: a fill with no hint, a filled entry with no stop ever resting, an open quantity after the time stop,
           a resting order still open, a target resting on Kite (only the stop rests), the targets watched / hit, the caps used. One Telegram summary and the Tips page's "Kite day check"."""

import json
import re
from datetime import datetime, time, timedelta
from typing import Callable, Optional

from backend.config import IST
from backend.database.models import get_db, get_setting, now_ist, set_setting
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm
from backend.tips import book as bk
from backend.tips import kite_orders as ko
from backend.tips import plan as pl

logger = setup_logger("tips")

PROMPT_V = "kite-check-v1"
AI_KIND = "tips_kite"
DAY_KEY = "tips.kite.daycheck"
DAY_JOB = "tips_kite_day"
DAY_TIME = time(15, 35)
STOP_GRACE_S = 60
FILL_SLACK = 0.02
SYSTEM = ("You check a trading app's broker orders against a Telegram channel's call. Write exactly 3 short lines, plain "
          "text, no markdown, at most 25 words each: 1) what the app did and why; 2) does it match what the channel meant; "
          "3) what to look at now. Use only figures that appear in the input. Never advise buying more. Say qty and lots "
          "exactly as given; never call a quantity 'lots'. Only the stop rests on Kite; the app watches the targets. Plain "
          "sentences only: no JSON, no braces, no bullet points.")


def _now() -> datetime:
    return datetime.now(IST)


def _strike_txt(v) -> str:
    f = float(v)
    return str(int(f)) if f.is_integer() else str(f)


def _dt(iso: Optional[str]) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=IST)
    return d


# --- the code check -------------------------------------------------------------------------------------------------------

def check(config, h: dict, rows: Optional[list] = None, now: Optional[datetime] = None) -> dict:
    """The deterministic check of one hint's Kite orders (see the module doc)."""
    now = now or _now()
    k = ko.kstate(h)
    rows = [r for r in (rows if rows is not None else ko.rows_for(h["id"])) if r["status"] != "voided" and r["leg"] != "test"]
    items: list[dict] = []

    def red(text, hand=None):
        items.append({"level": "red", "text": text, "hand": hand})

    def amber(text):
        items.append({"level": "amber", "text": text, "hand": None})

    if not rows and k["state"] not in ("armed_watch", "armed_wait", "entered"):
        ai_items = list(k.get("ai_fu_items") or [])
        return {"ok": True, "items": ai_items, "red": 0, "at": now.isoformat(timespec="seconds")}
    raw = re.sub(r"[,\s]", "", (h.get("raw") or "").lower())
    st = h.get("strike")
    ts = h.get("tradingsymbol") or ""
    opt = h.get("opt_type") or ""
    if st is not None:
        s = _strike_txt(st)
        if s not in raw:
            red(f"the strike {s} is not in the post", "check the contract on Kite")
        if ts and not ts.upper().endswith(f"{s}{opt}".upper()):
            red(f"the contract {ts} is not {h.get('symbol')} {s} {opt}", "exit on Kite now")
    src = (h.get("decoded") or {}).get("symbol")
    if src == "strike":
        amber(f"index {h.get('symbol')} read from the strike level (the post named none)")
    elif src == "owner":
        amber(f"index {h.get('symbol')} set by you (the post named none)")
    if h.get("expiry") and k.get("day") and h["expiry"] < k["day"]:
        red(f"the contract expired {h['expiry']}", "check the contract on Kite")
    for r in rows:
        if r.get("tradingsymbol") and ts and r["tradingsymbol"] != ts:
            red(f"order {r.get('order_id')} went out on {r['tradingsymbol']}, the hint is {ts}", "exit on Kite now")
    eside, xside = ko.entry_side(h), ko.exit_side(h)
    lot = int(h.get("lot_size") or 0) or 1
    lots = int(k.get("lots") or 0)
    ents = [r for r in rows if r["leg"] == "entry" and r["status"] not in ("rejected", "failed", "cancelled")]
    for r in rows:
        want = eside if r["leg"] == "entry" else xside
        if r.get("side") and r["side"] != want and r["status"] not in ("rejected", "failed"):
            red(f"{ko.LEG_NAMES.get(r['leg'], r['leg']).lower()} is a {r['side']} (should be {want})",
                "exit on Kite now" if r["leg"] == "entry" else f"cancel the {ko.LEG_NAMES.get(r['leg'], r['leg']).lower()}")
    for r in ents:
        if lots and int(r.get("qty") or 0) != lots * lot:
            red(f"entry qty {r.get('qty')} ≠ {lots} lot{'s' if lots != 1 else ''} × {lot}", "check the position on Kite")
    if lots > int(config.tips.kite_max_lots):
        red(f"{lots} lots > the cap {int(config.tips.kite_max_lots)}", "exit the extra on Kite")
    if len([r for r in ents if r["status"] in ko.LIVE + ("complete",)]) > 1:
        red("a second entry for this hint", "cancel the extra entry on Kite")
    e = ko.entry_of(h)
    filled = next((r for r in ents if r["status"] == "complete" and r.get("filled_px") is not None), None)
    if filled and e.get("mode") not in (None, "market") and not pl.is_spot(e) and e.get("lo") is not None:
        fpx, lo, hi = float(filled["filled_px"]), float(e["lo"]), float(e.get("hi") or e["lo"])
        buy = eside == "BUY"
        bad = None
        if e["mode"] == "above" and buy and fpx < lo * (1 - FILL_SLACK):
            bad = f"filled {pl.fmt(fpx)} under the level {pl.fmt(lo)}"
        elif e["mode"] == "below" and not buy and fpx > lo * (1 + FILL_SLACK):
            bad = f"filled {pl.fmt(fpx)} over the level {pl.fmt(lo)}"
        elif e["mode"] in ("at", "zone") and buy and fpx > hi * (1 + FILL_SLACK):
            bad = f"filled {pl.fmt(fpx)} over the zone top {pl.fmt(hi)}"
        elif e["mode"] in ("at", "zone") and not buy and fpx < lo * (1 - FILL_SLACK):
            bad = f"filled {pl.fmt(fpx)} under the zone bottom {pl.fmt(lo)}"
        if bad:
            amber(bad + (" · the channel activated first" if (h.get("run") or {}).get("allowed") else ""))
    ps = ko.premium_stop(h)
    moves = {float(m["to"]) for m in (k.get("stop_moves") or []) if m.get("to") is not None}
    ok_stops = {float(ps)} | moves | ({float(k["entry_px"])} if k.get("entry_px") is not None else set()) if ps is not None else moves
    for r in rows:
        if r["leg"] == "stop" and r["status"] in ko.LIVE + ("complete",) and r.get("trigger") is not None and ok_stops and float(r["trigger"]) not in ok_stops:
            red(f"the stop trigger {pl.fmt(r['trigger'])} is not the plan's {pl.fmt(ps)} (nor a logged move)", "modify the stop on Kite")
    if ps is not None and k.get("stop_px") is not None and float(k["stop_px"]) != float(ps) and float(k["stop_px"]) in moves:
        amber(f"stop moved {pl.fmt(ps)} → {pl.fmt(k['stop_px'])} (logged)")
    for r in rows:
        if r["leg"] in ("t1", "t2") and r["status"] in ko.LIVE and (r.get("order_type") or "") != "MARKET":
            red(f"a target resting on Kite ({ko.LEG_NAMES[r['leg']].lower()} {ko._type_text(r.get('order_type') or '', r.get('price'), r.get('trigger'))})",
                f"cancel the resting {ko.LEG_NAMES[r['leg']].lower()} on Kite (the app watches the targets)")
    q_open = int(k.get("qty_open") or 0)
    if k["state"] == "entered" and q_open > 0:
        lg = k["legs"].get("stop") or {}
        if lg.get("status") in ko.LIVE and int(lg.get("qty") or 0) > q_open:
            red(f"the resting stop sells {lg.get('qty')}, only {q_open} held", "modify the stop's quantity on Kite")
        stop_live = (k["legs"].get("stop") or {}).get("status") in ko.LIVE
        exiting = any((k["legs"].get(x) or {}).get("status") in ko.LIVE for x in ("exit", "t1", "t2"))
        at = _dt(k.get("entry_at"))
        if k.get("no_stop") and not stop_live:
            red(ko.NO_STOP)
        elif ps is None:
            amber("spot stop: watched by the app · exit by hand when it breaks")
        elif not stop_live and at is not None and (now - at).total_seconds() > STOP_GRACE_S and not exiting:
            red("no stop resting", "place the SL on Kite now")
        plan = h.get("plan") or {}
        tstop = plan.get("time_stop")
        if tstop and not plan.get("hold_to_expiry"):
            cut = now.replace(hour=pl.hm(tstop).hour, minute=pl.hm(tstop).minute, second=0, microsecond=0)
            if now >= cut + timedelta(seconds=STOP_GRACE_S) and (at is None or at < cut):
                red(f"{q_open} open after the time stop {ampm(tstop)}", "exit on Kite now")
    items.extend(x for x in (k.get("ai_fu_items") or []) if x not in items)     # 🤖 the AI second opinion's "differs" (amber)
    reds = sum(1 for x in items if x["level"] == "red")
    return {"ok": reds == 0, "items": items, "red": reds, "at": now.isoformat(timespec="seconds")}


def after_event(config, h: dict, now: Optional[datetime] = None, notify: bool = True, ai: bool = True) -> dict:
    """The check after a Kite event: stored on the hint's kite, each NEW red item pushed once, the AI read queued."""
    now = now or _now()
    k = ko.kstate(h)
    res = check(config, h, None, now)
    sent = list(k.get("check_sent") or [])
    new = [x for x in res["items"] if x["level"] == "red" and x["text"] not in sent]
    k["check"] = res
    for x in new:
        sent.append(x["text"])
        if notify:
            hand = f" · {x['hand']}" if x.get("hand") else ""
            bk.push(config, f"tips|kite|check|{h['id']}|{x['text'][:60]}", f"⚠️ Kite check · #{h['id']} · {x['text']}{hand} · your call")
    k["check_sent"] = sent[-30:]
    ko.save_kite(h)
    if ai:
        enqueue_ai(config, h)
    return res


# --- the AI read ------------------------------------------------------------------------------------------------------------

def qty_text(h: dict, qty) -> str:
    """'65 qty (1 lot of 65)' / '130 qty (2 lots of 65)': the model is never handed a bare 'lots' figure to misread."""
    q = int(qty or 0)
    lot = int(h.get("lot_size") or 0)
    if not lot or q % lot:
        return f"{q} qty"
    n = q // lot
    return f"{q} qty ({n} lot{'s' if n != 1 else ''} of {lot})"


def ai_input(h: dict, rows: Optional[list] = None) -> str:
    """The model's input: the post, the follow-ups so far, the plan, the order rows, the code check."""
    k = ko.kstate(h)
    rows = [r for r in (rows if rows is not None else ko.rows_for(h["id"])) if r["status"] != "voided" and r["leg"] != "test"]
    plan = h.get("plan") or {}
    fu = [e["text"] for e in (h.get("events") or []) if e.get("src") == "channel"][-8:]
    lines = [f"Hint #{h['id']}: {bk.title(h)} · contract {h.get('tradingsymbol') or '-'} · lot {h.get('lot_size') or '-'}",
             "Post: " + " / ".join(x.strip() for x in (h.get("raw") or "").split("\n") if x.strip())[:400],
             "Follow-ups: " + (" | ".join(x[:120] for x in fu) if fu else "none"),
             f"Plan: entry {plan.get('entry_text') or '-'} · {plan.get('stop_text') or '-'} · {plan.get('targets_text') or '-'}"
             + (f" · time stop {ampm(plan['time_stop'])}" if plan.get("time_stop") else ""),
             f"Kite: state {k['state']} · by {k.get('by') or '-'} · {qty_text(h, k.get('qty'))} · open {qty_text(h, k.get('qty_open'))}"
             + (f" · entry fill {pl.fmt(k['entry_px'])}" if k.get("entry_px") is not None else ""),
             "Targets: watched by the app (never resting on Kite); only the stop rests on Kite"]
    for r in rows:
        lines.append(f"Order {r['leg']} {r.get('side')} {qty_text(h, r.get('qty'))} {ko._type_text(r.get('order_type') or '', r.get('price'), r.get('trigger'))} "
                     f"{r['status']}" + (f" at {pl.fmt(r['filled_px'])}" if r.get("filled_px") is not None else "") + f" by {r.get('by')}")
    chk = k.get("check") or {}
    lines.append("Code check: " + ("; ".join(f"{x['level']}: {x['text']}" for x in chk.get("items") or []) or "all clear"))
    try:                                               # 💡 the trade facts (best / worst move, the stop's nearest) for the read
        from backend.tips import facts as fx
        lines.append(fx.summary_line(fx.for_hint(h)))
    except Exception:
        logger.exception("Tips facts line #%s failed", h.get("id"))
    return "\n".join(lines)


def _figures_ok(line: str, allowed: set) -> bool:
    from backend.ai import notes
    pct, rs = notes._typed(allowed)
    for t in notes._DIGITS.findall(line):
        c = t.replace(",", "")
        if len(c) <= 1:
            continue
        if (c not in allowed and not notes._partial(t, allowed)) or not notes._kind_ok(line, t, pct, rs):
            return False
    return True


def parse_lines(raw: str, src: str) -> tuple[list[str], int]:
    """The model's 3 lines (numbering and bullets stripped); a line with a figure not in `src` is dropped."""
    from backend.ai import notes
    allowed = notes.typed_tokens(src)
    out, dropped = [], 0
    for ln in (raw or "").split("\n"):
        t = re.sub(r"^\s*(?:[-*•]|\d[.)]|\[?AI\]?:?)\s*", "", ln).strip().strip("*").strip()
        if not t or not re.search(r"[A-Za-z]{3}", t):     # "{}", "[]", a bare number: not a sentence (the model's empty JSON showed as "{}")
            continue
        if _figures_ok(t, allowed):
            out.append(t[:240])
        else:
            dropped += 1
        if len(out) >= 3:
            break
    return out, dropped


def ai_read(config, hid: int, generate: Optional[Callable[[str, str], str]] = None, now: Optional[datetime] = None) -> dict:
    """One AI read of a hint's Kite orders (the queue's job). `generate(system, user)` -> text (the day model by
    default). Stored on the hint's kite ("ai") and in ai_notes (kind tips_kite). {lines, dropped, tokens}."""
    now = now or _now()
    h = bk.load(hid)
    if not h:
        return {"skip": "no hint"}
    src = ai_input(h)
    tokens, model = None, getattr(config.ai, "day_model", None)
    if generate is None:
        from backend.ai import client
        r = client.generate(config.ai.day_model, src + "\n\nWrite the 3 lines now, plain text:", system=SYSTEM, json_mode=False,
                            timeout=float(config.ai.timeout_s), keep_alive=config.ai.keep_alive, options={"temperature": 0, "num_predict": 160})
        raw, tokens, model = r.get("text") or "", r.get("tokens"), r.get("model") or model
    else:
        raw = generate(SYSTEM, src)
        model = "fake"
    lines, dropped = parse_lines(raw, src)
    with ko._LOCK:
        h = bk.load(hid) or h
        k = ko.kstate(h)
        k["ai"] = {"state": "done" if lines else "empty", "lines": lines, "dropped": dropped, "at": now.isoformat(timespec="seconds"), "model": model}
        ko.save_kite(h)
    with get_db() as conn:
        conn.execute("INSERT INTO ai_notes (created, day, kind, symbol, book, severity, title, text, data, model, prompt_version, ms) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (now_ist(), now.date().isoformat(), AI_KIND, h.get("tradingsymbol") or h.get("symbol"), "tips", "info",
                      f"Kite check #{hid}", "\n".join(lines) or "[AI] nothing usable from the model (the code check stands)", json.dumps({"hint_id": hid, "raw": raw[:2000], "dropped": dropped, "input": src[:4000]}),
                      model, PROMPT_V, None))
    return {"lines": lines, "dropped": dropped, "tokens": tokens, "note": f"tips kite #{hid}"}


def enqueue_ai(config, h: dict) -> Optional[dict]:
    """Queue the AI read (kind tips_kite, deduped per hint while queued). "pending" on the card until it is written."""
    if not getattr(config.ai, "enabled", False):
        return None
    k = ko.kstate(h)
    prev = k.get("ai") or {}
    k["ai"] = {**prev, "state": "pending"}
    ko.save_kite(h)
    try:
        from backend.ai import queue as q
        hid = h["id"]
        return q.enqueue(config, AI_KIND, f"tips_kite:{hid}", lambda: ai_read(config, hid), model=config.ai.day_model, note=f"Kite check #{hid}")
    except Exception as exc:
        logger.info("Tips Kite AI read not queued: %s", str(exc)[:120])
        return None


# --- the 3:35 PM reconciliation ------------------------------------------------------------------------------------------------

def due(now: datetime, config, done: set, trading: bool) -> list[str]:
    """["tips_kite_day"] from 3:35 PM on a trading day when Kite orders are on or any Tips order went out today."""
    if not trading or DAY_JOB in done or not (DAY_TIME <= now.time() < time(23, 0)):
        return []
    if ko.mode(config) == "off" and not ko.rows_today(now.date().isoformat()):
        return []
    return [DAY_JOB]


def day_check(config, now: Optional[datetime] = None, client=None, notify: bool = True) -> dict:
    """Every tips_orders row of today against the hints (see the module doc). Stored in app_settings and pushed."""
    now = now or _now()
    day = now.date().isoformat()
    rows = [r for r in ko.rows_today(day) if r["leg"] != "test" and r["status"] != "voided"]
    items: list[dict] = []
    hids = sorted({r["hint_id"] for r in rows})
    positions = opened = watched = hit = 0
    for hid in hids:
        h = bk.load(hid)
        mine = [r for r in rows if r["hint_id"] == hid]
        if not h:
            for r in mine:
                if r["status"] == "complete":
                    items.append({"level": "red", "text": f"a fill with no hint (order {r.get('order_id')})", "hand": "check it on Kite"})
            continue
        k = ko.kstate(h)
        tag = f"#{hid}{' DRY' if k.get('dry') else ''}"
        filled = any(r["leg"] == "entry" and r["status"] == "complete" for r in mine)
        if filled:
            positions += 1
            tg = ((h.get("plan") or {}).get("targets") or [])[:2]
            q2 = ko.split(h, int(k.get("lots") or 0) or 1)[1]
            for leg in ("t1", "t2")[:len(tg) if q2 else 1]:
                if (k["legs"].get(leg) or {}).get("status") == "complete":
                    hit += 1
                elif k["state"] == "entered":
                    watched += 1
            if ko.premium_stop(h) is not None and not any(r["leg"] == "stop" and r["status"] in ko.LIVE + ("complete", "cancelled") for r in mine):
                items.append({"level": "red", "text": f"{tag} entry filled, no stop ever resting", "hand": "check the position on Kite"})
        if k["state"] == "entered" and int(k.get("qty_open") or 0) > 0:
            opened += 1
            if not (h.get("plan") or {}).get("hold_to_expiry"):
                items.append({"level": "red", "text": f"{tag} {k['qty_open']} open after the time stop", "hand": "exit on Kite now"})
        for r in mine:
            if r["status"] in ko.LIVE and r["leg"] in ("t1", "t2") and (r.get("order_type") or "") != "MARKET":
                items.append({"level": "red", "text": f"{tag} a target resting on Kite ({ko.LEG_NAMES[r['leg']].lower()} "
                                                       f"{ko._type_text(r.get('order_type') or '', r.get('price'), r.get('trigger'))})",
                              "hand": f"cancel the resting {ko.LEG_NAMES[r['leg']].lower()} on Kite (the app watches the targets)"})
            elif r["status"] in ko.LIVE:
                items.append({"level": "red", "text": f"{tag} {ko.LEG_NAMES.get(r['leg'], r['leg']).lower()} still open on Kite "
                                                       f"({ko._type_text(r.get('order_type') or '', r.get('price'), r.get('trigger'))})",
                              "hand": f"cancel the resting {ko.LEG_NAMES.get(r['leg'], r['leg']).lower()} on Kite"})
        for x in ((k.get("check") or {}).get("items") or []):
            if x["level"] == "red" and not any(x["text"] in y["text"] for y in items):
                items.append({"level": "red", "text": f"{tag} {x['text']}", "hand": x.get("hand")})
    if client is not None and not ko.is_dry(client) and getattr(client, "has_token", False) and hasattr(client, "orders"):
        try:
            mine_ids = {str(r.get("order_id")) for r in ko.rows_today(day)}
            for o in client.orders() or []:
                if o.get("tag") == ko.TAG and str(o.get("order_id")) not in mine_ids and ko.kite_status(o.get("status")) == "complete":
                    items.append({"level": "red", "text": f"a fill with no hint (Kite order {o.get('order_id')} {o.get('tradingsymbol') or ''})".strip(),
                                  "hand": "check it on Kite"})
        except Exception as exc:
            items.append({"level": "amber", "text": f"Kite order book not read: {str(exc)[:80]}", "hand": None})
    dry = ko.mode(config) == "dry"
    caps = {"entries": ko.entries_today(day, dry), "max_entries": int(config.tips.kite_max_entries_day),
            "loss": ko.realised_today(day, dry), "loss_cap": float(config.tips.kite_loss_cap)}
    reds = sum(1 for x in items if x["level"] == "red")
    n = len(rows)
    summary = (f"🔎 Kite day · {n} order{'s' if n != 1 else ''} · {positions} position{'s' if positions != 1 else ''} · {opened} open · "
               f"targets {watched} watched · {hit} hit · checks: {'all clear' if not reds else f'{reds} red'}")
    res = {"at": now.isoformat(timespec="seconds"), "at_label": ampm(now), "day": day, "orders": n, "positions": positions, "open": opened,
           "targets_watched": watched, "targets_hit": hit,
           "items": items, "red": reds, "caps": caps, "summary": summary,
           "caps_text": f"entries {caps['entries']} of {caps['max_entries']} · realised {ko.rupees(caps['loss'], sign=True)} of the −{ko.rupees(caps['loss_cap'])} cap"}
    with get_db() as conn:
        set_setting(conn, DAY_KEY, json.dumps(res))
    try:                                               # 💡 the day's facts row (tips_day_facts), for future AI features
        from backend.tips import facts as fx
        res["facts"] = fx.day_facts(config, now)
    except Exception:
        logger.exception("Tips day facts failed")
    try:                                               # 🤖 the evening review of every closed Kite hint (tips.ai_review)
        from backend.tips import ai4
        res["reviews"] = ai4.review_day(config, now, notify=notify)
    except Exception:
        logger.exception("Tips evening reviews not queued")
    if notify:
        lines = [summary] + [f"⛔ {x['text']}" + (f" · {x['hand']}" if x.get("hand") else "") for x in items if x["level"] == "red"][:6]
        bk.push(config, f"tips|kite|day|{day}", "\n".join(lines))
    return res


def last_day() -> Optional[dict]:
    with get_db() as conn:
        raw = get_setting(conn, DAY_KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


# --- Home "Needs a look" -----------------------------------------------------------------------------------------------------

def attention_items(now: Optional[datetime] = None) -> list[tuple[str, str]]:
    """("warn", "⚡ Kite #6 · no stop resting → place the SL on Kite now") for every red item on a working Kite hint today."""
    now = now or _now()
    out = []
    for h in bk.rows("kite IS NOT NULL AND json_extract(kite, '$.check.red') > 0", order="id"):
        k = ko.kstate(h)
        if k.get("day") != now.date().isoformat():
            continue
        for x in (k.get("check") or {}).get("items") or []:
            if x["level"] == "red":
                out.append(("warn", f"⚡ Kite #{h['id']} · {x['text']}" + (f" → {x['hand']}" if x.get("hand") else "")))
    for h in bk.rows("kite IS NOT NULL AND json_extract(kite, '$.ai_fu_items') IS NOT NULL", order="id"):
        k = ko.kstate(h)
        if k.get("day") != now.date().isoformat() or k["state"] not in ko.WORKING:
            continue
        for x in (k.get("ai_fu_items") or [])[-1:]:          # 🤖 the AI second opinion differs: worth a look, never loud
            out.append(("info", f"⚡ {x['text']}"))
    try:                                              # 🤖 a pre-flight read that said "differs": worth a look, never loud
        from backend.tips import ai4
        out += ai4.attention_items(now)
    except Exception:
        logger.exception("Tips pre-flight Home items failed")
    d = last_day()
    if d and d.get("day") == now.date().isoformat():
        for x in d.get("items") or []:
            if x["level"] == "red" and not any(x["text"].split(" ", 1)[-1] in t for _, t in out):
                out.append(("warn", f"⚡ Kite day check · {x['text']}" + (f" → {x['hand']}" if x.get("hand") else "")))
    return out[:8]


# --- the AI second opinion on a channel follow-up (informational: never places, modifies or cancels anything) -----------------

FU_KIND = "tips_followup"
FU_PROMPT_V = "followup-v1"
FU_SYSTEM = ("You read one follow-up post from a Telegram trading channel about a trade that is open or armed, and compare it "
             "with what a trading app did. Reply with ONE line, plain text, no markdown: 'reads as: <book now | book part | "
             "move stop to N | exit | trail | nothing>' then ' · ' then 'agrees' or 'differs: <why in at most 20 words>'. "
             "Use only figures that appear in the input. A note that is conditional on the lots held applies to the lots given.")
READS = ("book now", "book part", "move stop to", "exit", "trail", "nothing")
_FU_RE = re.compile(r"reads\s*as\s*:?\s*(book now|book part|move stop to\s*\d[\d,]*(?:\.\d+)?|exit|trail|nothing)\b(.*)", re.I)


def fu_skip(text: str, act: Optional[dict]) -> Optional[str]:
    """Why a follow-up gets no AI read: a bare price ("476"), chatter, an add-on. None when it is read."""
    a = (act or {}).get("action")
    t = (text or "").strip()
    if a == "price" or re.fullmatch(r"[\d\s.,+\-/₹%()]*\W*", t):
        return "a bare price"
    if a == "add":
        return "an add-on (never averaged)"
    if not re.search(r"[A-Za-z]{3}", t):
        return "chatter"
    return None


def did_text(act: Optional[dict], line: Optional[str]) -> str:
    """What the app did with the follow-up, in a few words ('the app moved the stop to 440')."""
    a = (act or {}).get("action")
    ln = (line or "").lower()
    if ln.startswith("not followed") or "not modified" in ln or "not booked" in ln:
        return "the app did not follow it (" + (line or "")[:60] + ")"
    if a == "stop" and (act or {}).get("value") is not None:
        return f"the app moved the stop to {pl.fmt(act['value'])}"
    if a == "partial":
        if "one lot" in ln and "booked" in ln:
            return "the app booked the one lot at market"
        if "kept for the target" in ln:
            return "the app kept the one lot for the target" + (f" · rest stop {pl.fmt(act['rest_stop'])}" if act.get("rest_stop") is not None else "")
        return "the app booked part" + (f" · rest stop {pl.fmt(act['rest_stop'])}" if act.get("rest_stop") is not None else
                                        " · stop to cost" if act.get("cost") else "")
    if a in ("exit", "sl_hit"):
        return "the app exited at market"
    if a == "trail":
        return "the app trailed the stop to cost"
    if a == "avoid":
        return "the app cancelled the unfilled entry"
    if a == "active":
        return "the app sent the entry"
    if a == "targets":
        return "the app noted the targets"
    return "the app took no action"


def fu_input(h: dict, text: str, act: Optional[dict], line: Optional[str], ltp: Optional[float] = None) -> str:
    """The model's input: the post, the hint's one-line state (qty open, lots, entry, stop, targets, live price), the
    action the code took."""
    k = ko.kstate(h)
    plan = h.get("plan") or {}
    ltp = ltp if ltp is not None else bk.last_ltp(h)
    stop = k.get("stop_px") if k.get("stop_px") is not None else ((k["legs"].get("stop") or {}).get("trigger") or ko.premium_stop(h))
    tg = plan.get("targets") or []
    entry = k.get("entry_px") if k.get("entry_px") is not None else ((h.get("entry") or {}).get("lo") or (h.get("entry") or {}).get("hi"))
    state = (f"open {qty_text(h, k.get('qty_open'))}" if k["state"] == "entered" else f"armed {qty_text(h, k.get('qty'))} (not filled)")
    post = " / ".join(x.strip() for x in (text or "").split("\n") if x.strip())[:600]
    a = dict(act or {})
    code = a.get("action") or "note"
    if a.get("value") is not None:
        code += f" {pl.fmt(a['value'])}"
    if a.get("rest_stop") is not None:
        code += f" · rest stop {pl.fmt(a['rest_stop'])}"
    if a.get("cost"):
        code += " · cost"
    return "\n".join([
        f"Hint #{h['id']}: {bk.title(h)} · lot {h.get('lot_size') or '-'}",
        f"State: {state} · {int(k.get('lots') or 0)} lot{'s' if int(k.get('lots') or 0) != 1 else ''} · entry {pl.fmt(entry)} · stop {pl.fmt(stop)}"
        f" · targets {'/'.join(pl.fmt(t) for t in tg) or '-'} · live {pl.fmt(ltp)}",
        f"Post: {post}",
        f"The app read it as: {code}",
        f"The app did: {did_text(act, line)}" + (f" ({line[:120]})" if line else ""),
    ])


def fu_parse(raw: str, src: str) -> Optional[dict]:
    """{reads, agrees, why, line} from the model's one line, or None (no such line, or a figure not in the input)."""
    from backend.ai import notes
    allowed = notes.typed_tokens(src)
    for ln in (raw or "").split("\n"):
        t = re.sub(r"^\s*(?:[-*•]|\d[.)]|\[?AI\]?:?)\s*", "", ln).strip().strip("*").strip()
        m = _FU_RE.search(t)
        if not m:
            continue
        reads = re.sub(r"\s+", " ", m.group(1).lower()).replace(",", "")
        rest = m.group(2) or ""
        dm = re.search(r"\bdiffers?\b\s*:?\s*(.*)", rest, re.I)
        agrees = dm is None and bool(re.search(r"\bagrees?\b", rest, re.I))
        if dm is None and not agrees:
            return None
        why = (dm.group(1).strip(" .·-") if dm else "")
        why = " ".join(why.split()[:20])
        if not _figures_ok(t[:300], allowed):
            return None
        return {"reads": reads, "agrees": agrees, "why": why, "line": t[:240]}
    return None


def followup_read(config, hid: int, post_key: str, text: str, act: Optional[dict], line: Optional[str],
                  generate: Optional[Callable[[str, str], str]] = None, now: Optional[datetime] = None, notify: bool = True) -> dict:
    """One AI read of a follow-up (the queue's job, kind tips_followup). Agrees: "[AI] agrees" on the card, no push.
    Differs: ONE Telegram line + an amber item on the card and Home. A line with a figure not in the input is dropped.
    Never places, modifies or cancels anything."""
    now = now or _now()
    h = bk.load(hid)
    if not h:
        return {"skip": "no hint"}
    src = fu_input(h, text, act, line)
    tokens, model = None, getattr(config.ai, "day_model", None)
    if generate is None:
        from backend.ai import client
        r = client.generate(config.ai.day_model, src + "\n\nWrite the one line now, plain text:", system=FU_SYSTEM, json_mode=False,
                            timeout=float(config.ai.timeout_s), keep_alive=config.ai.keep_alive, options={"temperature": 0, "num_predict": 80})
        raw, tokens, model = r.get("text") or "", r.get("tokens"), r.get("model") or model
    else:
        raw = generate(FU_SYSTEM, src)
        model = "fake"
    res = fu_parse(raw, src)
    quote = (text or "").strip().replace("\n", " ")[:50]
    did = did_text(act, line)
    msg = None
    with ko._LOCK:
        h = bk.load(hid) or h
        k = ko.kstate(h)
        fu = dict(k.get("ai_fu") or {})
        done = list(fu.get("done") or [])
        if post_key not in done:
            done.append(post_key)
        fu["done"] = done[-40:]
        if res is None:
            fu["last"] = {"state": "dropped", "at": now.isoformat(timespec="seconds"), "post": quote}
            ko._log(k, now, f'[AI] second opinion on "{quote}" dropped (no usable line)')
        elif res["agrees"]:
            fu["last"] = {"state": "agrees", "reads": res["reads"], "at": now.isoformat(timespec="seconds"), "post": quote}
            ko._log(k, now, f'[AI] agrees · reads "{quote}" as {res["reads"]}')
        else:
            msg = f'[AI] #{hid} reads "{quote}" as {res["reads"]} · {did} · your call'
            fu["last"] = {"state": "differs", "reads": res["reads"], "why": res["why"], "at": now.isoformat(timespec="seconds"), "post": quote}
            items = list(k.get("ai_fu_items") or [])
            item = {"level": "amber", "text": msg + (f" ({res['why']})" if res["why"] else ""), "hand": None, "ai": True}
            if item["text"] not in [x["text"] for x in items]:
                items.append(item)
            k["ai_fu_items"] = items[-5:]
            chk = dict(k.get("check") or {"ok": True, "items": [], "red": 0, "at": now.isoformat(timespec="seconds")})
            chk["items"] = [x for x in (chk.get("items") or []) if not x.get("ai")] + k["ai_fu_items"]
            k["check"] = chk
            ko._log(k, now, msg + (f" · {res['why']}" if res["why"] else ""))
        k["ai_fu"] = fu
        ko.save_kite(h)
    if msg and notify:
        bk.push(config, f"tips|kite|aifu|{hid}|{post_key}", msg)
    with get_db() as conn:
        conn.execute("INSERT INTO ai_notes (created, day, kind, symbol, book, severity, title, text, data, model, prompt_version, ms) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (now_ist(), now.date().isoformat(), FU_KIND, h.get("tradingsymbol") or h.get("symbol"), "tips",
                      "warn" if msg else "info", f"Follow-up #{hid}",
                      (res or {}).get("line") or "[AI] nothing usable from the model (the app's reading stands)",
                      json.dumps({"hint_id": hid, "post": post_key, "raw": raw[:1000], "input": src[:2000], "result": res}),
                      model, FU_PROMPT_V, None))
    return {"result": res, "pushed": bool(msg and notify), "tokens": tokens, "note": f"tips follow-up #{hid}"}


def enqueue_followup(config, hid: int, post_key, text: str, act: Optional[dict], line: Optional[str], notify: bool = True) -> Optional[dict]:
    """Queue the second opinion after a follow-up linked to a hint with an open or armed Kite position (any mode but
    off): at most one per follow-up; never for a bare price or chatter. The AI guard / quiet hours hold it as any job."""
    if not getattr(config.ai, "enabled", False) or ko.mode(config) == "off":
        return None
    if fu_skip(text, act):
        return None
    h = bk.load(hid)
    if not h:
        return None
    k = ko.kstate(h)
    if k["state"] not in ko.WORKING or (k["state"] == "entered" and int(k.get("qty_open") or 0) <= 0):
        return None
    key = str(post_key)
    if key in ((k.get("ai_fu") or {}).get("done") or []):
        return None
    try:
        from backend.ai import queue as q
        return q.enqueue(config, FU_KIND, f"tips_followup:{hid}:{key}", lambda: followup_read(config, hid, key, text, act, line, notify=notify),
                         model=config.ai.day_model, note=f"Follow-up #{hid}")
    except Exception as exc:
        logger.info("Tips follow-up AI read not queued: %s", str(exc)[:120])
        return None
