"""💡 The Tips card v2: the state badge, the story row, the fold counts and the footer, computed on the server by code
(pure over the hint, its Kite view and the card blocks already built; no quote call, no Kite call, never a model call).

  state_of     {kind live|wait|won|lost|off, word, why, group live|waiting|closed|not_taken}: "LIVE", "ARMED · RESTING",
               "WAITING · STOP MISSING", "WAITING · LEVEL", "WAITING · CHANNEL'S WORD", "PAUSED · AVOID", "CLOSED · TARGET 2",
               "CLOSED · STOP", "CLOSED · CHANNEL'S EXIT", "CLOSED · TIME STOP", "CLOSED · YOUR EXIT", "CANCELLED · AVOID",
               "NO FILL · TIME STOP", "SKIPPED BY YOU", "EXPIRED", "PAPER · OPEN", "PAPER · CLOSED" …
  story_of     five steps, always five: post · entry · stop · targets · result (closed / not taken) or now (open /
               waiting); each {step, t, head, sub, tone, done}; a missing figure shows "—".
  fold_counts  {legs, said_next, log, ai}: what each folded section holds.
  foot_of      the card's footer line (the text the page built in two places before), one place."""

import re
from datetime import datetime
from typing import Optional

from backend.config import IST
from backend.timefmt import ampm
from backend.tips import book as bk
from backend.tips import plan as pl

DASH = "—"
GROUPS = ("live", "waiting", "closed", "not_taken")
ENDED = {"t1": "TARGET 1", "t2": "TARGET 2", "t3": "TARGET 3", "stop": "STOP", "trail": "TRAILED STOP", "time": "TIME STOP",
         "expired": "EXPIRY", "channel_exit": "CHANNEL'S EXIT", "owner_exit": "YOUR EXIT", "no_fill": "NO FILL"}


def _st(kind: str, word: str, why: Optional[str], group: str, at: Optional[str] = None) -> dict:
    """`at`: when a closed card closed (the Closed group's order)."""
    return {"kind": kind, "word": word, "why": why or None, "group": group, "at": at}


def _k(h: dict) -> dict:
    return h.get("kite") if isinstance(h.get("kite"), dict) else {}


def _num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _dt(v) -> Optional[datetime]:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return d.astimezone(IST) if d.tzinfo else d.replace(tzinfo=IST)


def tstamp(iso, now: datetime) -> str:
    """'2:04' (12-hour, no AM/PM) today; 'Thu 2:04 PM' on another day; '' without a time."""
    d = _dt(iso)
    if d is None:
        return ""
    if d.date() == now.date():
        return ampm(d).rsplit(" ", 1)[0]
    return f"{d:%a} {ampm(d)}"


def _plural(n, w: str = "lot") -> str:
    n = int(n or 0)
    return f"{n} {w}{'' if n == 1 else 's'}"


def _pct(v: Optional[float]) -> str:
    if v is None:
        return DASH
    return f"{'+' if v >= 0 else '−'}{abs(v):.1f} %".replace(".0 %", " %")


# --- the state ---------------------------------------------------------------------------------------------------------------

def _closed_word(k: dict) -> str:
    """How a Kite position ended: TARGET n / STOP / CHANNEL'S EXIT / TIME STOP / YOUR EXIT / AUTO EXIT."""
    reason = str(k.get("reason") or "")
    why = str(k.get("exit_why") or "")
    by = k.get("exit_by")
    if by == "time_stop" or why.startswith("time stop") or reason.startswith("time stop"):
        return "TIME STOP"
    if why.startswith("the channel") or reason.startswith("the channel"):
        return "CHANNEL'S EXIT"
    m = re.match(r"target (\d)", reason)
    if m:
        return f"TARGET {m.group(1)}"
    if by == "target":
        n = max((int(x[1]) for x in ("t1", "t2", "t3") if ((k.get("legs") or {}).get(x) or {}).get("status") == "complete"), default=0)
        return f"TARGET {n}" if n else "TARGET"
    if by == "stop" or reason == "stop":
        return "STOP"
    if by == "you" or reason == "your exit":
        return "YOUR EXIT"
    if by == "auto":
        return "AUTO EXIT"
    return ""


def _cancel_word(h: dict, reason: str) -> tuple[str, str]:
    """A Kite entry that ended unfilled: (word, kind)."""
    r = reason.lower()
    if r.startswith("the channel:"):
        quote = reason.split(":", 1)[1].strip().strip('"')
        a = pl.followup_action(quote).get("action")
        return {"avoid": "CANCELLED · AVOID", "sl_hit": "CANCELLED · CHANNEL'S SL", "exit": "CANCELLED · CHANNEL'S EXIT"}.get(a, "CANCELLED · THE CHANNEL"), "off"
    if r.startswith("time stop") and "never filled" in r:
        return "NO FILL · TIME STOP", "off"
    if r.startswith("entry window ended"):
        return "CANCELLED · WINDOW END", "off"
    if r == "kill switch":
        return "CANCELLED · KILL SWITCH", "off"
    if r in ("you cancelled it",):
        return "CANCELLED · BY YOU", "off"
    if r.startswith("a new day"):
        return "CANCELLED · NEW DAY", "off"
    if r.startswith("the post was edited"):
        return "CANCELLED · POST EDITED", "off"
    if "strike" in r:
        return "CANCELLED · STRIKE", "off"
    if r in ("entry rejected", "entry refused") or r.startswith("not sent"):
        return "NOT SENT · REFUSED" if r != "entry rejected" else "CANCELLED · REJECTED", "off"
    if r.startswith("the hint is"):
        return f"CANCELLED · HINT {r.split('the hint is', 1)[1].strip().upper()}", "off"
    return "CANCELLED", "off"


def _paused_why(h: dict) -> tuple[str, str]:
    for e in reversed(h.get("events") or []):
        t = str(e.get("text") or "")
        if t.startswith("paused by you"):
            return "PAUSED · BY YOU", "you paused it"
        if t.startswith("resumed"):
            break
    return "PAUSED · AVOID", "the channel said avoid · no entry until it says enter"


def state_of(h: dict, kv: Optional[dict] = None, config=None) -> dict:
    """The card's state (see the module doc), by code from the hint, its Kite state, the paper position and the status."""
    from backend.tips import kite_orders as ko
    k = ko.kstate(dict(h)) if not isinstance(h.get("kite"), dict) else {**ko.new_kite(), **h["kite"]}
    ks = k["state"]
    status = h.get("status")
    pos = h.get("pos") if h.get("taken") else None
    dry = "DRY · " if k.get("dry") and ks != "none" else ""
    if ks == "entered":
        q = int(k.get("qty_open") or 0)
        by = (k.get("taken") or {}).get("by") if isinstance(k.get("taken"), dict) else None
        return _st("live", f"{dry}LIVE", f"{q} qty open" + (f" · taken by {by}" if by else ""), "live")
    if ks in ko.ARMED:
        arm = k.get("arm") or {}
        why = {"armed_watch": f"the app watches {arm.get('index') or ko.index_name(h)}",
               "armed_wait": "waiting for the channel's activation"}.get(ks)
        if why is None:
            kind = arm.get("kind")
            why = {"slm": "SL-M resting on Kite", "limit": "limit resting on Kite", "market": "market entry sent"}.get(kind, kind or "resting on Kite")
        return _st("wait", f"{dry}ARMED · RESTING", why, "waiting")
    if ks == "closed":
        r = float(k.get("realised") or 0.0)
        how = _closed_word(k) or ("WON" if r >= 0 else "LOST")
        return _st("won" if r >= 0 else "lost", f"{dry}CLOSED · {how}", k.get("reason"), "closed", k.get("closed_at"))
    if pos:                                              # a paper take (Kite has nothing on the hint)
        ps = pos.get("state")
        if ps == "open":
            return _st("live", "PAPER · OPEN", f"{pos.get('lots')} lot paper position", "live")
        if ps == "closed":
            r = float(pos.get("realised") or h.get("pnl") or 0.0)
            end = ENDED.get(str(pos.get("ended") or ""), str(pos.get("ended") or "").replace("_", " ").upper())
            return _st("won" if r >= 0 else "lost", "PAPER · CLOSED", end.lower() if end else None, "closed", (pos.get("run") or {}).get("exit_t"))
        return _st("wait", "PAPER · WAITING", "taken on paper · waiting for the entry", "waiting")
    if status == "skipped":
        return _st("off", "SKIPPED BY YOU", None, "not_taken")
    reason = str(k.get("reason") or "")
    if ks == "none" and reason and reason != "dry run cleared":
        word, kind = _cancel_word(h, reason)
        return _st(kind, word, reason, "not_taken")
    if status == "expired" or (status == "closed" and h.get("ended") == "expired"):
        return _st("off", "EXPIRED", h.get("untracked_why"), "not_taken")
    if not h.get("tracked") and status not in ("new", "waiting", "active", "paused") and status != "closed":
        return _st("off", "NOT TRACKED", h.get("untracked_why"), "not_taken")
    if status == "closed":
        end = str(h.get("ended") or "")
        if end == "no_fill":
            return _st("off", "NO FILL · TIME STOP", "the level never came", "not_taken")
        return _st("off", "NOT TAKEN", f"the record: {ENDED.get(end, end.replace('_', ' ').upper()).lower()}" if end else None, "not_taken")
    if status == "paused":
        word, why = _paused_why(h)
        return _st("wait", word, why, "waiting")
    plan = h.get("plan") or {}
    if not plan.get("ok"):
        why = plan.get("why") or "no plan"
        return _st("wait", f"WAITING · {why.upper()}", why, "waiting")
    if ko.needs_activation(h):
        return _st("wait", "WAITING · CHANNEL'S WORD", "the entry waits for the channel's activation", "waiting")
    if status == "active":                               # the record's entry came; you have not taken it
        return _st("wait", "LEVEL REACHED · NOT TAKEN", "the record filled · take it or let it go", "waiting")
    if not h.get("tracked") and h.get("untracked_why"):
        return _st("wait", "WAITING · LEVEL", h.get("untracked_why"), "waiting")
    return _st("wait", "WAITING · LEVEL", f"entry {pl.entry_text(ko.entry_of(h))}", "waiting")


# --- the story row -------------------------------------------------------------------------------------------------------------

def _step(step: str, t: str = "", head: str = DASH, sub: str = "", tone: str = "", done: bool = False, **kw) -> dict:
    return {"step": step, "t": t or "", "head": head or DASH, "sub": sub or "", "tone": tone, "done": bool(done), **kw}


def _stop_head(h: dict) -> str:
    st = h.get("stop") or {}
    src = pl.stop_src(st)
    if src is None:
        return "no SL"
    if st.get("kind") == "spot":
        return f"SL spot {pl.fmt(st.get('value'))}"
    if src == "provisional":
        return f"no SL · provisional {pl.fmt(st.get('value'))}"
    if src == "owner":
        first = next((x for x in (st.get("hist") or []) if x.get("src") == "channel" and x.get("value") is not None), None)
        return f"SL {pl.fmt(first['value'])}" if first else "no SL"
    return f"SL {pl.fmt(st.get('value'))}"


def _post_step(h: dict, cb: dict, now: datetime) -> dict:
    tg = h.get("targets") or []
    tgt = ("tgt " + "/".join(pl.fmt(x) for x in tg) + ("+" if h.get("open_ended") else "")) if tg else "no tgt"
    head = f"{pl.entry_text(h.get('entry') or {})} · {_stop_head(h)} · {tgt}"
    sz = cb.get("sizing") or {}
    sub = ""
    if sz:
        sub = str(sz.get("line2") or "").split(" · premium")[0]
        m = int(sz.get("mult") or 1)
        if m > 1 and f"(lots ×{m})" not in sub:
            sub += f" (lots ×{m})"
    k = _k(h)
    n = int(k.get("lots") or 0) if k.get("state") not in (None, "none") else 0
    if n and sub and f"→ {_plural(n)}" not in sub and not sub.split(" (lots")[0].endswith(_plural(n)) and f"says {_plural(n)}" not in sub:
        sub += f" → {_plural(n)} on Kite"
    return _step("post", tstamp(h.get("posted_at"), now), head, sub, "", True)


def _entry_level_text(h: dict) -> str:
    from backend.tips import kite_orders as ko
    if ko.needs_activation(h):
        return "waiting for the channel's word"
    lvl = pl.entry_level(h)
    e = ko.entry_of(h)
    if lvl is not None:
        return f"waiting for {pl.fmt(lvl)}"
    if (e.get("mode") or "market") == "market":
        return "at market"
    return f"waiting for {pl.entry_text(e)}"


def _entry_step(h: dict, k: dict, state: dict, cb: dict, now: datetime) -> dict:
    ks = k.get("state")
    f = cb.get("trade_facts") or {}
    lot = int(h.get("lot_size") or 0) or 1
    if ks in ("entered", "closed") and k.get("entry_px") is not None:
        lots = int(k.get("qty") or 0) // lot or int(k.get("lots") or 0)
        bits = []
        if f.get("post_to_fill_s") is not None:
            from backend.tips import card as cd
            bits.append(f"post → fill {cd.dur(f['post_to_fill_s'])}")
        s = f.get("slippage_entry")
        if s and s.get("rs") is not None:
            bits.append(f"slippage {'+' if s['rs'] >= 0 else '−'}{pl.fmt(abs(s['rs']))}")
        if f.get("spread_entry") is not None:
            bits.append(f"spread {pl.fmt(f['spread_entry'])}")
        return _step("entry", tstamp(k.get("entry_at"), now), f"filled {pl.fmt(k['entry_px'])} · {_plural(lots)}", " · ".join(bits), "", True)
    if ks in ("armed", "armed_watch", "armed_wait"):
        arm = k.get("arm") or {}
        kind = arm.get("kind")
        if ks == "armed" and kind == "slm":
            head = f"resting on Kite · SL trigger {pl.fmt(arm.get('trigger'))}"
        elif ks == "armed" and kind == "limit":
            head = f"resting on Kite · limit {pl.fmt(arm.get('price'))}"
        elif ks == "armed_watch":
            head = f"armed · the app watches {arm.get('index') or 'the index'}" + (f" {pl.fmt(arm.get('level'))}" if arm.get("level") is not None else "")
        elif ks == "armed_wait":
            head = "armed · waiting for the channel's word"
        else:
            head = "sent to Kite"
        return _step("entry", tstamp(arm.get("at"), now), head, (cb.get("now") or {}).get("sub") or "", "warn", False)
    pos = h.get("pos") if h.get("taken") else None
    if pos and pos.get("entry_px") is not None:
        r = pos.get("run") or {}
        return _step("entry", tstamp(r.get("entry_t"), now), f"filled {pl.fmt(pos['entry_px'])} · {pos.get('lots')} lot · paper", "paper book", "", True)
    if state["group"] in ("not_taken", "closed"):
        return _step("entry", "", "not taken", state.get("why") or "", "", False)
    return _step("entry", "", _entry_level_text(h), (cb.get("now") or {}).get("sub") or "", "warn" if state["kind"] == "wait" else "", False)


def _stop_step(h: dict, k: dict, tile: Optional[dict], cb: dict, now: datetime) -> dict:
    from backend.tips import kite_orders as ko
    tile = tile or {}
    extra = {"settable": bool(tile.get("settable")), "rev": tile.get("rev"), "src": tile.get("src"), "cls": tile.get("cls") or "",
             "chip": tile.get("chip"), "chip_tone": tile.get("chip_tone"), "tile": tile or None}
    ks = k.get("state")
    if ks in ("entered", "closed") and k.get("entry_px") is not None:
        cur = _num(k.get("stop_px"))
        moves = k.get("stop_moves") or []
        first = _num(moves[0].get("from")) if moves and moves[0].get("from") is not None else ko.premium_stop(h)
        head = pl.fmt(cur) if cur is not None else "no SL"
        t = ""
        if moves and first is not None and cur is not None and first != cur:
            note = ""
            if cur == _num(k.get("entry_px")):
                note = " (cost after T1)" if any("T1" in str(m.get("who") or "") or "cost" in str(m.get("who") or "") for m in moves) else " (cost)"
            head = f"{pl.fmt(first)} → {pl.fmt(cur)}{note}"
            t = tstamp(moves[-1].get("t") or moves[-1].get("at"), now)
        sl = (k.get("legs") or {}).get("stop") or {}
        f = cb.get("trade_facts") or {}
        if ks == "closed":
            if sl.get("status") == "complete":
                sub, tone = f"hit · sold {pl.fmt(sl.get('filled_px'))}", "down"
                t = tstamp(sl.get("at") or k.get("closed_at"), now)
            else:
                near = f.get("stop_nearest_pct")
                sub = (f"never closer than {near:.1f} %".replace(".0 %", " %") + " · cancelled at the exit") if near is not None else "cancelled at the exit"
                tone = ""
            return _step("stop", t, head, sub, tone, True, **extra)
        d = (cb.get("dist") or {}).get("stop") or {}
        live = sl.get("status") in ko.LIVE
        sub = ("resting" if live else "not resting") + (f" · {d.get('pct_text')} away" if d.get("pct_text") and d.get("pct_text") != DASH else "")
        return _step("stop", t, head, sub, "down" if d.get("tone") == "down" else "", live, **extra)
    st = h.get("stop") or {}
    if tile.get("value_text"):
        return _step("stop", tstamp(st.get("at"), now) if st.get("at") else "", tile["value_text"], tile.get("sub") or "",
                     "warn" if tile.get("src") == "provisional" else "", False, **extra)
    if st.get("kind") == "spot":
        return _step("stop", "", pl.stop_text(st), "watched by the app · exit by hand", "", False, **extra)
    return _step("stop", "", "no SL", "stop missing · set one to plan the trade", "warn", False, **extra)


def _targets_step(h: dict, k: dict, kv: Optional[dict], cb: dict, now: datetime) -> dict:
    tg = (h.get("plan") or {}).get("targets") or h.get("targets") or []
    if not tg:
        return _step("targets", "", "no target", "", "", False)
    legs = {x.get("leg"): x for x in ((kv or {}).get("legs") or [])}
    run = ((h.get("pos") or {}).get("run") if h.get("taken") and h.get("pos") else None) or h.get("run") or {}
    hits = run.get("hits") or []
    bits, t, done = [], "", False
    use_legs = k.get("state") not in (None, "none") and any(x in legs for x in ("t1", "t2", "t3"))
    for i, px in enumerate(tg[:3]):
        key = f"t{i + 1}"
        word = ""
        if use_legs and key in legs:
            m = re.match(r"watched by the app · ([^·]+)", str(legs[key].get("detail") or ""))
            word = m.group(1).strip() if m else ""
            word = "runner" if legs[key].get("runner") else word
            word = "rest" if word.startswith("rest") else word
            if legs[key].get("status") == "complete":
                done, word = True, (word + " ✓").strip()
                t = t or (legs[key].get("at") or "")
        elif use_legs:
            continue                                     # not a Kite leg: in the sub as "paper only"
        elif i == 2 and (len(tg) > 3 or h.get("open_ended")):
            word = "(runner)"
        if not use_legs and any(x.get("k") == i + 1 for x in hits):
            done, word = True, (word + " ✓").strip()
        plus = "+" if h.get("open_ended") and i == len(tg) - 1 else ""
        bits.append(f"T{i + 1} {pl.fmt(px)}{plus}" + (f" {word}" if word else ""))
    head = " · ".join(bits)
    sub = []
    if use_legs:
        for i, px in enumerate(tg):
            if f"t{i + 1}" not in legs:
                sub.append(f"T{i + 1} {pl.fmt(px)}{'+' if h.get('open_ended') and i == len(tg) - 1 else ''} paper only")
    elif len(tg) > 3:
        sub.append("past T3: " + " / ".join(pl.fmt(x) for x in tg[3:]))
    ch = (cb.get("pnl_strip") or {}).get("channel") or {}
    if ch.get("text"):
        sub.append(f"channel: {ch['text']}" + (f" · {ch['note']}" if ch.get("note") else ""))
    if not t and done:
        first = next((x for x in hits if x.get("t")), None)
        t = tstamp(first.get("t"), now) if first else ""
    elif t:
        t = _leg_t(t, k, now)
    return _step("targets", t, head, " · ".join(sub), "up" if done else "", done)


def _today_leg(k: dict, now: datetime) -> bool:
    return k.get("day") == now.date().isoformat()


def _leg_t(t: str, k: dict, now: datetime) -> str:
    """A leg's '10:20:05 AM' (ko.t12 with seconds) in the story's form."""
    m = re.match(r"(\d{1,2}):(\d{2})(?::\d{2})? (AM|PM)", t)
    if not m:
        return t
    if _today_leg(k, now) or not k.get("day"):
        return f"{m.group(1)}:{m.group(2)}"
    try:
        d = datetime.fromisoformat(str(k["day"]))
        return f"{d:%a} {m.group(1)}:{m.group(2)} {m.group(3)}"
    except ValueError:
        return f"{m.group(1)}:{m.group(2)} {m.group(3)}"


def _index_move(h: dict, f: dict) -> str:
    sf, se = _num(f.get("spot_at_fill")), _num(f.get("spot_at_exit"))
    if not sf or se is None:
        return ""
    return f"{h.get('symbol') or 'the index'} {_pct((se - sf) / sf * 100)} meanwhile"


def _last_step(h: dict, k: dict, state: dict, cb: dict, now: datetime) -> dict:
    from backend.tips import card as cd
    ks = k.get("state")
    f = cb.get("trade_facts") or {}
    strip = cb.get("pnl_strip") or {}
    if ks == "closed" and k.get("entry_px") is not None:
        pnl = _num(k.get("realised")) or 0.0
        net = strip.get("net") if strip.get("net") is not None else f.get("net_est")
        head = f"{bk.inr(pnl, sign=True)}" + (f" · net {bk.inr(net, sign=True)}" if net is not None else "")
        best, worst = (f.get("mfe") or {}).get("pct"), (f.get("mae") or {}).get("pct")
        sub = " · ".join(x for x in ((f"best {cd.pct_text(best)}" if best is not None else ""), (f"worst {cd.pct_text(worst)}" if worst is not None else ""),
                                     _index_move(h, f)) if x)
        return _step("result", tstamp(k.get("closed_at"), now), head, sub, "up" if pnl > 0 else "down" if pnl < 0 else "", True)
    ltp = _num(bk.last_ltp(h)) if h.get("id") else None
    if ks == "entered" and k.get("entry_px") is not None:
        epx = float(k["entry_px"])
        sign = -1 if h.get("side") == "SELL" else 1
        head = f"{pl.fmt(ltp)} · {_pct((ltp - epx) / epx * 100 * sign)} from the fill" if ltp is not None and epx else DASH
        d = cb.get("dist") or {}
        sub = []
        if d.get("stop"):
            sub.append(f"stop {d['stop'].get('pct_text')} away")
        if d.get("target"):
            sub.append(f"{d['target'].get('name') or 'target'} {d['target'].get('pct_text')} away")
        ts = (h.get("plan") or {}).get("time_stop")
        if ts and not (h.get("plan") or {}).get("hold_to_expiry"):
            sub.append(f"time stop {ampm(pl.hm(ts))}")
        mv = (ltp - epx) * sign if ltp is not None else 0
        return _step("now", tstamp(now.isoformat(), now), head, " · ".join(sub), "live" if not mv else "up" if mv > 0 else "down", False)
    pos = h.get("pos") if h.get("taken") else None
    if pos and pos.get("state") == "closed":
        pnl = _num(h.get("pnl")) if h.get("pnl") is not None else _num(pos.get("realised"))
        r = pos.get("run") or {}
        return _step("result", tstamp(r.get("exit_t"), now), f"{bk.inr(pnl, sign=True)} · paper", state.get("why") or "",
                     "up" if (pnl or 0) > 0 else "down" if (pnl or 0) < 0 else "", True)
    if pos and pos.get("state") == "open":
        epx = _num(pos.get("entry_px"))
        sign = -1 if h.get("side") == "SELL" else 1
        head = f"{pl.fmt(ltp)} · {_pct((ltp - epx) / epx * 100 * sign)} from the fill" if ltp is not None and epx else DASH
        mv = (ltp - epx) * sign if ltp is not None and epx else 0
        return _step("now", tstamp(now.isoformat(), now), head, f"paper P&L {bk.inr(h.get('pnl'), sign=True)}" if h.get("pnl") is not None else "",
                     "live" if not mv else "up" if mv > 0 else "down", False)
    if state["group"] == "not_taken":
        run = h.get("run") or {}
        end = str(run.get("end") or h.get("ended") or "")
        head = f"the record: {ENDED.get(end, end.replace('_', ' ').upper()).lower()}" if end else "not taken"
        sub = f"{bk.inr(h.get('pnl'), sign=True)} a lot at the plan" if h.get("pnl") is not None else ""
        return _step("result", tstamp(run.get("exit_t"), now), head, sub, "", False)
    # waiting / armed: where the price sits against the level
    lvl = pl.entry_level(h)
    if ltp is not None and lvl:
        head = f"{pl.fmt(ltp)} · {_pct((ltp - lvl) / lvl * 100)} from the level"
    elif ltp is not None:
        head = f"{pl.fmt(ltp)}" + (f" · {(cb.get('now') or {}).get('sub')}" if (cb.get("now") or {}).get("sub") else "")
    else:
        head = DASH
    ts = (h.get("plan") or {}).get("time_stop")
    sub = f"skipped at {ampm(pl.hm(ts))} if it never comes" if ts and (h.get("plan") or {}).get("wait") else (f"time stop {ampm(pl.hm(ts))}" if ts else "")
    return _step("now", tstamp(now.isoformat(), now) if ltp is not None else "", head, sub, "", False)


def story_of(h: dict, kv: Optional[dict], cb: dict, now: Optional[datetime] = None, config=None, state: Optional[dict] = None,
             tile: Optional[dict] = None) -> list[dict]:
    """The five steps (see the module doc). `cb`: the card blocks hint_view built (pnl_strip, trade_facts, dist, sizing,
    now); `tile`: the stop tile already in the payload."""
    now = now or datetime.now(IST)
    k = _k(h)
    state = state or state_of(h, kv, config)
    if tile is None:
        from backend.tips import track as tr
        tile = tr.stop_tile(h)
    return [_post_step(h, cb, now), _entry_step(h, k, state, cb, now), _stop_step(h, k, tile, cb, now),
            _targets_step(h, k, kv, cb, now), _last_step(h, k, state, cb, now)]


# --- the fold and the footer -------------------------------------------------------------------------------------------------

def fold_counts(h: dict, kv: Optional[dict], cb: dict) -> dict:
    """{legs, said_next, log, ai}: the Kite legs, the chips ("+n" counted as n), the log lines (the hint's events and the
    Kite log), the AI / market blocks with something to show."""
    chips = cb.get("said_next") or []
    said = sum(int(c.get("n") or 0) if c.get("kind") == "more" else 1 for c in chips)
    ai4 = cb.get("ai4") or {}
    ai = sum(1 for x in (ai4.get("review"), ai4.get("grade"), ai4.get("preflight"), cb.get("market")) if x)
    return {"legs": len((kv or {}).get("legs") or []), "said_next": said,
            "log": len(h.get("events") or []) + len((kv or {}).get("log") or []), "ai": ai}


def foot_of(h: dict, kv: Optional[dict], state: dict) -> str:
    """The card footer, by state: "closed on Kite · target 2 · +₹2,243 · taken by auto on Kite" / "65 open on Kite · only
    the stop rests · targets watched by the app" / the paper close / the plan while it waits / the record only."""
    kv = kv or {}
    k = _k(h)
    ks = k.get("state")
    footer = kv.get("footer") if "footer" in kv else bk.kite_taken_text(h)
    if ks == "closed":
        return " · ".join(x for x in (f"closed {'in the dry run' if k.get('dry') else 'on Kite'}", k.get("reason"),
                                      bk.inr(float(k.get("realised") or 0.0), sign=True), footer) if x)
    if ks == "entered":
        return " · ".join(x for x in (f"{int(k.get('qty_open') or 0)} open {'in the dry run' if k.get('dry') else 'on Kite'}",
                                      "only the stop rests · targets watched by the app", footer) if x)
    pos = h.get("pos") if h.get("taken") else None
    if pos and pos.get("state") == "closed":
        return f"{bk.inr(h.get('pnl'), sign=True)} · closed" + (f" at {pl.fmt(h['exit_px'])}" if h.get("exit_px") is not None else "") + " · paper"
    if pos and pos.get("state") == "open":
        return f"paper position open · {bk.inr(h.get('pnl'), sign=True)}"
    if state["group"] == "waiting":
        return (h.get("plan") or {}).get("text") or state.get("why") or ""
    if footer:
        return footer
    return "scored for the channel's record only · you did not take it"


def card(h: dict, kv: dict, cb: dict, now: datetime, config=None, tile: Optional[dict] = None) -> dict:
    """Everything hint_view adds: {state, story, fold_counts, foot}."""
    s = state_of(h, kv, config)
    return {"state": s, "story": story_of(h, kv, cb, now, config, s, tile), "fold_counts": fold_counts(h, kv, cb), "foot": foot_of(h, kv, s)}


def groups(views: list[dict]) -> dict:
    """GET /api/tips `groups`: {live, waiting, closed, not_taken} of hint ids: live newest first, waiting by posted time,
    closed newest first (by the close), not taken newest first."""
    out: dict = {g: [] for g in GROUPS}
    by: dict = {g: [] for g in GROUPS}
    for v in views:
        g = ((v.get("state") or {}).get("group")) or "not_taken"
        by.setdefault(g, []).append(v)
    key = lambda v: (str(v.get("posted_at") or ""), int(v.get("id") or 0))      # noqa: E731

    def closed_key(v):
        return (str((v.get("state") or {}).get("at") or v.get("posted_at") or ""), int(v.get("id") or 0))
    out["live"] = [v["id"] for v in sorted(by["live"], key=key, reverse=True)]
    out["waiting"] = [v["id"] for v in sorted(by["waiting"], key=key)]
    out["closed"] = [v["id"] for v in sorted(by["closed"], key=closed_key, reverse=True)]
    out["not_taken"] = [v["id"] for v in sorted(by["not_taken"], key=key, reverse=True)]
    return out
