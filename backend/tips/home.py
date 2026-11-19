"""🏠 Home · "💡 Tips · Kite": the block on Home (GET /api/home/tips, stream topic `home_tips`).

Two cards (Tips · Kite · today: realised + open Kite P&L; Channel · 30 d), the strip (mode, kill switch, caps, window,
the reader), one row per working Kite hint (open / armed, with the buttons the Tips page offers), today's calls as chips
with their outcome, the order-check lines, the channel's 30-day record and the Tips times for Next up.

Read-only and cheap: the stored hints, their kite JSON, tips_orders and the stored quotes only. No quote call, no Kite
call (the Home page polls it every 5 s while anything is armed or open, else every 30 s)."""

import re
from datetime import datetime
from typing import Optional

from backend.config import IST
from backend.timefmt import ampm
from backend.tips import book as bk
from backend.tips import kite_orders as ko
from backend.tips import plan as pl

DAY_CHECK = "15:35"

REASON = {"stop": "stop", "target 1": "T1", "target 2": "T2", "your exit": "exit"}


def _num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _lots(n: int) -> str:
    return f"{n} lot{'s' if n != 1 else ''}"


def _short(h: dict) -> str:
    """'NIFTY 25400 CE' (the title without the side)."""
    t = bk.title(h)
    return t.split(" ", 1)[1] if t.split(" ", 1)[0] in ("BUY", "SELL") and " " in t else t


def _strip_now(t: Optional[str]) -> str:
    """'now 214 · stop 193 is −9.8 %' -> 'stop 193 is −9.8 %'."""
    t = t or ""
    return t.split(" · ", 1)[1] if t.startswith("now ") and " · " in t else ("" if t.startswith("now ") else t)


def open_pnl(h: dict) -> Optional[float]:
    """Today's Kite P&L of an entered hint: the open quantity at the last stored quote + what is already booked."""
    k = ko.kstate(h)
    ltp, epx = _num(bk.last_ltp(h)), _num(k.get("entry_px"))
    q = int(k.get("qty_open") or 0)
    if ltp is None or epx is None:
        return round(float(k.get("realised") or 0.0), 2) if k.get("realised") else None
    return round((ltp - epx) * q * ko._sign(h) + float(k.get("realised") or 0.0), 2)


def _short_why(t: str, n: int = 38) -> str:
    """'waits: 3 Kite positions open at most (kite_auto_max_open)' -> 'waits: 3 Kite positions open at most'."""
    t = re.sub(r"\s*\([^)]*\)?", "", str(t or "")).strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0].rstrip(" ·:,") + "…"


def _when(iso: Optional[str], now: datetime) -> str:
    """'2:41 PM' today, 'Thu 2:41 PM' another day."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso)).astimezone(IST)
    except ValueError:
        return str(iso)
    return ampm(dt) if dt.date() == now.date() else f"{dt:%a} {ampm(dt)}"


def _by(k: dict) -> str:
    b = k.get("by")
    return "auto" if b == "auto" else "dry run" if b == "dry" or k.get("dry") else "your click" if b == "click" else (b or "")


def _ai_line(k: dict) -> Optional[str]:
    lines = [x for x in ((k.get("ai") or {}).get("lines") or []) if isinstance(x, str) and x.strip()]
    return lines[0] if lines else None


def row(config, h: dict, now: datetime) -> dict:
    """One working Kite hint: {id, title, state, tag, tag_c, pnl, facts, detail, ai, buttons, kite (for the confirms)}."""
    v = ko.hint_kite(config, h)
    k = ko.kstate(h)
    legs = {x["leg"]: x for x in v["legs"]}
    st = k["state"]
    ltp = _num(bk.last_ltp(h))
    lots = int(v["lots"] or 0)
    dry = "DRY · " if k.get("dry") else ""
    arm = k.get("arm") or {}
    acts = v["actions"]
    until = ampm(config.tips.kite_entry_until)
    out = {"id": h["id"], "title": bk.title(h), "name": _short(h), "state": st, "pnl": None, "ai": _ai_line(k),
           "tradingsymbol": h.get("tradingsymbol"), "exchange": h.get("exchange"), "ltp": ltp, "kite": v,
           "product": config.tips.kite_product}
    if st == "entered":
        out["tag"], out["tag_c"] = f"{dry}open · {_lots(lots)}", "ok"
        out["pnl"] = open_pnl(h)
        nxt = next((legs[x] for x in ("t1", "t2") if x in legs and legs[x]["status"] != "complete"), None)
        parts = [f"now {pl.fmt(ltp)}"] if ltp is not None else []
        st_leg = legs.get("stop") or {}
        if st_leg.get("trigger") is not None:
            parts.append(_strip_now(st_leg.get("now_text")) or f"stop {pl.fmt(st_leg['trigger'])}")
        elif st_leg:
            parts.append("spot stop · watched by the app")
        if nxt and nxt.get("price") is not None:
            parts.append(_strip_now(nxt.get("now_text")) or f"target {pl.fmt(nxt['price'])}")
        out["facts"] = " · ".join(p for p in parts if p)
        resting = [ko.LEG_NAMES[x] if x == "stop" else x.upper() for x in ("stop", "t1", "t2")
                   if (k["legs"].get(x) or {}).get("status") in ko.LIVE]
        rest = (" and ".join([r.lower() if r == "Stop" else r for r in resting]) + (" resting in the dry run" if k.get("dry") else " resting on Kite")) \
            if resting else ("no stop resting" if ko.premium_stop(h) is not None else "spot stop: exit by hand")
        out["detail"] = " · ".join(x for x in (f"entry {pl.fmt(k.get('entry_px'))}", _when(k.get("entry_at"), now), _by(k), rest) if x)
        out["buttons"] = [b for b, ok in (("trail", "modify_stop" in acts), ("exit", "exit" in acts)) if ok]
    else:
        kind = arm.get("kind")
        if st == "armed" and kind in ("slm", "limit"):
            lvl = arm.get("trigger") if kind == "slm" else arm.get("price")
            out["tag"] = f"{dry}armed · {arm.get('order_type') or 'SL-M'} {pl.fmt(lvl)} resting"
        elif st == "armed_watch":
            out["tag"] = f"{dry}armed · watching {arm.get('index') or ko.index_name(h)}"
        elif st == "armed_wait":
            out["tag"] = f"{dry}armed · waiting for activation"
        else:
            out["tag"] = f"{dry}armed · sent"
        out["tag_c"] = "w"
        e = legs.get("entry") or {}
        facts = e.get("now_text") or (f"now {pl.fmt(ltp)}" if ltp is not None else e.get("type_text") or "")
        out["facts"] = " · ".join(x for x in (facts, _lots(lots)) if x)
        lvl = arm.get("level")
        what = ("the level is the trigger" if kind in ("slm", "limit") else
                f"the app watches {arm.get('index') or ko.index_name(h)}" if kind == "watch" else
                "waiting for the channel's activation" if st == "armed_wait" else "sent at market")
        words = " · wait words logged" if ko.entry_of(h).get("wait_words") and st != "armed_wait" else ""
        cut = (f"cancels at {until} if it never trades {pl.fmt(lvl)}" if lvl is not None and st != "armed_wait" else
               f"cancels at {until} if never activated" if st == "armed_wait" else "")
        out["detail"] = " · ".join(x for x in (_when(arm.get("at"), now), _by(k), what + words, cut) if x)
        out["buttons"] = ["send_now"] + (["cancel"] if "cancel:entry" in acts else ["exit"] if "exit" in acts else [])
        out["send_ok"] = "send_now" in acts
    return out


def _last_cancel_at(k: dict) -> str:
    for x in reversed(k.get("log") or []):
        if "cancel" in (x.get("text") or "").lower():
            return ko.t12(x.get("t"))
    return ""


def outcome(h: dict) -> tuple[str, str]:
    """A today's call's chip: (text, colour) · no entry · cancelled 2:30 PM | closed +₹x · T1 | armed | armed · resting |
    skipped · <why> | open | waiting | paper only."""
    k = ko.kstate(h)
    st = k["state"]
    if st == "entered":
        return ("DRY · open" if k.get("dry") else "open"), "ok"
    if st == "armed":
        return "armed · resting", "w"
    if st in ("armed_watch", "armed_wait"):
        return "armed", "w"
    if st == "closed":
        r = float(k.get("realised") or 0.0)
        why = REASON.get(k.get("reason") or "", "exit")
        return f"closed {bk.inr(r, sign=True)} · {why}", "ok" if r > 0 else "bad" if r < 0 else ""
    reason = str(k.get("reason") or "")
    if reason and reason != "dry run cleared":
        if "cancel" in reason or k.get("arm") is None and any((x.get("text") or "").startswith("Kite: the unfilled entry cancelled") for x in k.get("log") or []):
            at = _last_cancel_at(k)
            return f"no entry · cancelled{f' {at}' if at else ''}", ""
        return f"no entry · {_short_why(reason)}", ""
    why = (k.get("auto") or {}).get("why")
    if why:
        return f"skipped · {_short_why(why)}", "a"
    if h.get("taken"):
        return "paper only", ""
    s = h.get("status")
    if s in bk.OPEN_STATUSES:
        return "waiting", ""
    if s == "skipped":
        return "skipped", "a"
    return "no entry", ""


def checks(rows: list[dict], now: datetime) -> dict:
    """The order check across today's Kite hints: {clear: [ids], at, at_label, items: [{id, level, text, hand}]} plus an
    open screenshot strike mismatch (📷) on a today's hint."""
    from backend.tips import vision as vs
    clear, items, at = [], [], None
    for h in rows:
        c = ko.kstate(h).get("check")
        if not c:
            continue
        at = max(at or "", c.get("at") or "") or at
        if not c.get("items"):
            clear.append(h["id"])
        for x in c.get("items") or []:
            items.append({"id": h["id"], "level": x.get("level"), "text": x.get("text"), "hand": x.get("hand")})
    for h in rows:
        mm = (h.get("shot") or {}).get("mismatch")
        if mm and not mm.get("resolved") and h.get("status") in bk.OPEN_STATUSES:
            items.append({"id": h["id"], "level": "red", "text": vs.mismatch_line(h), "hand": "pick the strike on the Tips page"})
    items.sort(key=lambda x: (x["level"] != "red", x["id"]))
    return {"clear": sorted(clear, reverse=True), "at": at, "at_label": ko.t12(at) if at else None, "items": items}


def next_times(config, now: datetime) -> list[dict]:
    """Next up: the entry window's close, the time stop, the day check: on a trading day, Kite mode not off, still ahead."""
    if ko.mode(config) == "off" or not pl.trading_day(now.date()):
        return []
    out = []
    for key, icon, name, hm in (("tips_window", "⏱", "Tips entry window closes", config.tips.kite_entry_until),
                                ("tips_time_stop", "🛑", "Tips time stop · open Kite positions exit", config.tips.intraday_time_stop),
                                ("tips_day_check", "🔎", "Kite day check", DAY_CHECK)):
        t = pl.hm(hm)
        at = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if at > now:
            out.append({"key": key, "icon": icon, "name": name, "at": at.isoformat(timespec="seconds"), "label": ampm(at), "tips": True})
    return sorted(out, key=lambda x: x["at"])


def home_tips(config, now: Optional[datetime] = None) -> dict:
    """GET /api/home/tips (see the module doc)."""
    from backend.tips import kite_check as kc
    from backend.tips import reader as rd
    from backend.tips import track as tr
    now = now or datetime.now(IST)
    day = now.date().isoformat()
    m = ko.mode(config)
    dry = m == "dry"
    lite = ko.lite(config, now)
    caps = lite["caps"]
    st = rd.status()
    last = st.get("last_post_at")
    rec = tr.record(30, None, now)
    tot, yva = rec["totals"], rec["you_vs_all"]
    working = bk.rows("kite IS NOT NULL AND json_extract(kite, '$.state') IN ('armed', 'armed_watch', 'armed_wait', 'entered')", order="id DESC")
    today_h = bk.rows("posted_at >= ? AND channel != 'rehearsal'", (day,), order="id")
    rows = [row(config, h, now) for h in working] if m != "off" else []
    kite_today = {h["id"]: h for h in bk.rows("kite IS NOT NULL AND json_extract(kite, '$.day') = ?", (day,), order="id")}
    # today's Kite P&L: what the hints closed today booked + every open one at its last stored quote (with its booked part)
    realised = sum(float(ko.kstate(h).get("realised") or 0.0) for h in kite_today.values() if ko.kstate(h)["state"] == "closed")
    open_sum = sum(r["pnl"] or 0.0 for r in rows if r["state"] == "entered")
    total = round(realised + open_sum, 2) if m != "off" else None
    n_open = sum(1 for r in rows if r["state"] == "entered")
    n_armed = sum(1 for r in rows if r["state"] != "entered")
    for h in today_h:
        if (h.get("shot") or {}).get("mismatch"):
            kite_today.setdefault(h["id"], h)
    dc = kc.last_day()
    dc = dc if dc and dc.get("day") == day else None
    yours = round(float(yva.get("you") or 0.0), 2)
    return {
        "enabled": bool(config.tips.enabled), "mode": m, "dry": dry, "kill": lite["kill"], "window": lite["window"], "product": lite["product"],
        "caps": {**caps, "loss_used": abs(caps.get("used_loss") or 0.0)},
        "reader": {"state": st.get("state"), "last_post_at": last, "last_post_label": ko.t12(last) if last else None},
        "card": {"pnl": total, "open": n_open, "armed": n_armed, "entries": caps["entries"], "max_entries": caps["max_entries"],
                 "line": ("DRY · " if dry else "") + f"{n_open} open · {n_armed} armed · {caps['entries']} of {caps['max_entries']} entries"
                 if m != "off" else "paper book only"},
        "channel30": {k: tot.get(k) for k in ("hints", "t1_first", "stop_first", "no_fill", "avg_r")},
        "rows": rows,
        "today": [{"id": h["id"], "name": _short(h), "text": o[0], "c": o[1]} for h in today_h for o in [outcome(h)]],
        "checks": checks(list(kite_today.values()), now) if m != "off" else {"clear": [], "at": None, "at_label": None, "items": []},
        "record": {**{k: tot.get(k) for k in ("hints", "t1_first", "stop_first", "no_fill", "avg_r")}, "took": yva.get("took"), "of": yva.get("of"),
                   "you": yours, "all": yva.get("all"),
                   "day_check": {"at_label": dc.get("at_label"), "summary": dc.get("summary"), "red": dc.get("red", 0)} if dc else None,
                   "day_check_at": ampm(DAY_CHECK)},
        "next": next_times(config, now),
        "live": bool(rows),
        "at": now.isoformat(timespec="seconds"),
    }
