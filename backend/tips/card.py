"""💡 The Tips hint card's Kite blocks, computed on the server (the page only renders them). Read-only: stored hints,
their kite JSON, tips_orders, the stored quotes and ticks; no quote call, no Kite call, no AI call.

  pnl_strip  under the card header while Kite has / had a position on the hint (paper-only hints keep their look):
             open "open P&L · 1 lot · 65 qty" + signed ₹ and % from the fill, "≈ net" after the estimated charges, time
             in the trade and to the time stop, the best / worst move; closed "closed · your exit 10:51 AM", held, the
             channel's own call against your exit ("left ₹x" / "beat the channel by ₹x"); the day across the Tips on the
             right (DRY in a dry run). P&L: open = (ltp − fill) × open qty (+ the booked parts); closed = the realised.
  now_tile   the big NOW tile (live with a pulse while open, "at exit" once closed, grey while armed).
  dist       stop ◀──●──▶ target: the dot is the price (the exit once closed) mapped linearly from the stop to the
             last target (clamped), a marker at the fill, ticks at the targets before the last one.
  said_next  the chips: every linked follow-up (and the action the app took), post edits, your exit, the channel's
             record, the AI second opinions already read; the newest 7 and "+n" when there are more than 8.
  facts      the trade facts row (6 cells) from backend/tips/facts.py."""

from datetime import datetime
from typing import Optional

from backend.timefmt import ampm
from backend.tips import book as bk
from backend.tips import facts as fx
from backend.tips import plan as pl

ZONE_STOP, ZONE_TARGET = 16, 84            # the bar's red zone 0–16 %, green zone 84–100 %
ALMOST_PCT, CLOSE_STOP_PCT, NEAR_PCT = 1.0, 3.0, 3.0
MAX_CHIPS = 8
EXIT_BY = {"auto": "auto exit", "you": "your exit", "time_stop": "time stop", "target": "target", "stop": "stop"}


def _num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def pct_text(v: Optional[float]) -> str:
    """'+17.8 %' / '−1.7 %'."""
    if v is None:
        return "—"
    return f"{'+' if v >= 0 else '−'}{abs(v):.1f} %".replace(".0 %", " %")


def t12(iso) -> str:
    d = fx._dt(iso)
    return ampm(d) if d else ""


def hm(iso) -> str:
    """'9:58' (the hour and minute of a 12-hour time, for the compact "fill 9:58 → exit 10:51")."""
    return t12(iso).rsplit(" ", 1)[0]


def dur(s: Optional[float]) -> str:
    """'46 min' / '4 h 30 min' / '0 min 32 s'."""
    if s is None:
        return "—"
    s = max(0, int(s))
    if s < 60:
        return f"0 min {s} s"
    m = s // 60
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min" if m % 60 else f"{m // 60} h"


def _sign(h: dict) -> int:
    return -1 if h.get("side") == "SELL" else 1


def _k(h: dict) -> dict:
    return h.get("kite") if isinstance(h.get("kite"), dict) else {}


def has_position(h: dict) -> bool:
    k = _k(h)
    return k.get("state") in ("entered", "closed") and k.get("entry_px") is not None


def exit_text(k: dict) -> str:
    return EXIT_BY.get(k.get("exit_by") or "", "exit" if not k.get("reason") else str(k["reason"]))


# --- the day across the Tips -----------------------------------------------------------------------------------------------

def day_line(config, now: datetime) -> dict:
    """{pnl, room, entries, max_entries, dry, text}: today's Kite P&L across the Tips (closed realised + open at the last
    quote), the loss cap's room, the entries against the cap."""
    from backend.tips import kite_orders as ko
    day = now.date().isoformat()
    dry = ko.mode(config) == "dry"
    total = 0.0
    for h in bk.rows("kite IS NOT NULL AND json_extract(kite, '$.day') = ?", (day,)):
        k = ko.kstate(h)
        if bool(k.get("dry")) != dry:
            continue
        if k["state"] == "closed":
            total += float(k.get("realised") or 0.0)
        elif k["state"] == "entered":
            total += open_pnl(h) or 0.0
    lite = ko.lite(config, now)
    c = lite["caps"]
    room = max(0.0, float(c["loss_cap"]) + float(c.get("used_loss") or 0.0))
    text = (f"{'DRY · ' if dry else ''}today across Tips {bk.inr(round(total, 2), sign=True)} · loss cap room {bk.inr(room)} · "
            f"entries {c['entries']} of {c['max_entries']}")
    return {"pnl": round(total, 2), "room": round(room, 2), "entries": c["entries"], "max_entries": c["max_entries"], "dry": dry, "text": text}


def open_pnl(h: dict, ltp: Optional[float] = None) -> Optional[float]:
    """An entered hint's Kite P&L: (ltp − fill) × open qty × side + what is already booked (a sell hint mirrored)."""
    k = _k(h)
    epx = _num(k.get("entry_px"))
    ltp = _num(ltp if ltp is not None else bk.last_ltp(h))
    if epx is None:
        return None
    if ltp is None:
        return round(float(k.get("realised") or 0.0), 2) if k.get("realised") else None
    return round((ltp - epx) * int(k.get("qty_open") or 0) * _sign(h) + float(k.get("realised") or 0.0), 2)


# --- the P&L strip ----------------------------------------------------------------------------------------------------------

def _move(x: Optional[dict]) -> Optional[dict]:
    if not x:
        return None
    return {"pct": x.get("pct"), "text": pct_text(x.get("pct")), "at": t12(x.get("at")), "rs": x.get("rs")}


def pnl_strip(config, h: dict, f: Optional[dict], now: datetime, day: Optional[dict] = None) -> Optional[dict]:
    """The strip (see the module doc), or None when Kite never had a position on the hint."""
    if not has_position(h):
        return None
    k = _k(h)
    sign = _sign(h)
    epx = float(k["entry_px"])
    lot = int(h.get("lot_size") or 0) or 1
    qty = int(k.get("qty") or 0)
    day = day if day is not None else day_line(config, now)
    out = {"dry": bool(k.get("dry")), "best": _move((f or {}).get("mfe")), "worst": _move((f or {}).get("mae")), "day": day,
           "charges": (f or {}).get("charges_est")}
    if k["state"] == "entered":
        ltp = _num(bk.last_ltp(h))
        q_open = int(k.get("qty_open") or 0)
        pnl = open_pnl(h, ltp)
        pct = (ltp - epx) / epx * 100 * sign if ltp is not None and epx else None
        lots = q_open // lot if lot else q_open
        ent = fx._dt(k.get("entry_at"))
        ts = (h.get("plan") or {}).get("time_stop")
        left = ""
        if ts and not (h.get("plan") or {}).get("hold_to_expiry"):
            t = pl.hm(ts)
            at = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            left = f"{dur((at - now).total_seconds())} to {ampm(ts)}" if at > now else f"time stop {ampm(ts)} passed"
        out.update(state="open", label=f"open P&L · {lots} lot{'s' if lots != 1 else ''} · {q_open} qty", pnl=pnl, open=pnl,
                   pnl_text=bk.inr(pnl, sign=True) if pnl is not None else "—", pct=round(pct, 1) if pct is not None else None,
                   pct_text=pct_text(pct), time_k="in trade", time_v=dur((now - ent).total_seconds()) if ent else "—", time_sub=left)
    else:
        pnl = round(float(k.get("realised") or 0.0), 2)
        pct = pnl / (epx * qty) * 100 if epx and qty else None
        ent, cl = fx._dt(k.get("entry_at")), fx._dt(k.get("closed_at"))
        when = f" {t12(k.get('closed_at'))}" if cl else ""
        out.update(state="closed", label=f"closed · {exit_text(k)}{when}", pnl=pnl, open=None,
                   pnl_text=bk.inr(pnl, sign=True), pct=round(pct, 1) if pct is not None else None, pct_text=pct_text(pct),
                   time_k="held", time_v=dur((cl - ent).total_seconds()) if cl and ent else "—",
                   time_sub=f"fill {hm(k.get('entry_at'))} → exit {hm(k.get('closed_at'))}" if cl and ent else "")
        out["channel"] = channel_vs_you(h, f)
    net = (f or {}).get("net_est")
    if net is None and pnl is not None:
        net = pnl
    out["net"] = net
    out["net_text"] = f"≈ {bk.inr(net, sign=True)}" if net is not None else "—"
    out["tone"] = "up" if (pnl or 0) > 0 else "down" if (pnl or 0) < 0 else ""
    return out


def channel_vs_you(h: dict, f: Optional[dict]) -> Optional[dict]:
    """The channel's own call against your exit: "T1 at 271.05 · left ₹1,527" when the record closed better, "beat the
    channel by ₹x" when you did. None while the record has no result."""
    k = _k(h)
    cr = fx.channel_result(h)
    if not cr:
        return None
    if cr.get("exit_px") is None:                     # the record closed without a price (no fill / expired)
        return {"text": str(cr["outcome"]).replace("_", " "), "note": "", "tone": ""} if cr.get("closed") else None
    what = f"{cr['outcome']} at {pl.fmt(cr['exit_px'])}"
    mine = _num(k.get("exit_px"))
    if mine is None:                                  # the record has its result; your exit price is not stored
        return {"text": what, "note": "", "tone": ""}
    qty = int(k.get("qty") or 0)
    diff = round((float(cr["exit_px"]) - mine) * qty * _sign(h), 2)
    if diff > 0:
        return {"text": what, "left": diff, "note": f"left {bk.inr(diff)}", "tone": "down"}
    if diff < 0:
        return {"text": what, "beat": -diff, "note": f"beat the channel by {bk.inr(-diff)}", "tone": "up"}
    return {"text": what, "note": "same as the channel", "tone": ""}


# --- the NOW tile ----------------------------------------------------------------------------------------------------------------

def _prev_quotes(h: dict) -> list[float]:
    from backend.database.models import get_db
    with get_db() as conn:
        return [r[0] for r in conn.execute("SELECT ltp FROM tips_quotes WHERE hint_id = ? AND ltp IS NOT NULL ORDER BY t DESC LIMIT 2", (h["id"],))]


def now_tile(h: dict) -> Optional[dict]:
    """The big NOW tile: live (open on Kite: green / red against the fill, the tick's change, a pulse), at exit (closed:
    the exit fill, now the last price), or grey (armed / not entered: where the price sits against the entry). None when
    the hint has nothing on Kite (the plain NOW cell stays)."""
    from backend.tips import kite_orders as ko
    k = _k(h)
    st = k.get("state")
    if st in (None, "none"):
        return None
    ltp = _num(bk.last_ltp(h))
    if st == "closed" and k.get("entry_px") is not None:
        ex = _num(k.get("exit_px"))
        if ex is None:                                # no exit fill stored: the last price, never called the exit fill
            return {"state": "exit", "label": "now · after exit", "value": ltp, "value_text": pl.fmt(ltp), "tone": "", "pulse": False,
                    "sub": "last price · exit fill not stored"} if ltp is not None else None
        return {"state": "exit", "label": "now · at exit", "value": ex, "value_text": pl.fmt(ex), "tone": "", "pulse": False,
                "sub": f"exit fill{f' · now {pl.fmt(ltp)}' if ltp is not None else ''}"}
    if ltp is None:
        return None
    if st == "entered" and k.get("entry_px") is not None:
        epx = float(k["entry_px"])
        mv = (ltp - epx) * _sign(h)
        qs = _prev_quotes(h)
        delta = round(qs[0] - qs[1], 2) if len(qs) == 2 else None
        arrow = "" if delta is None else ("▲" if delta > 0 else "▼" if delta < 0 else "•")
        dtext = "" if delta is None else f"{arrow} {'+' if delta >= 0 else '−'}{pl.fmt(abs(delta))} · "
        return {"state": "live", "label": "now · live", "value": ltp, "value_text": pl.fmt(ltp), "pulse": True,
                "tone": "up" if mv > 0 else "down" if mv < 0 else "", "delta": delta,
                "sub": f"{dtext}{pct_text(mv / epx * 100 if epx else None)} from fill"}
    cell = ko.now_cell(h) or {}
    return {"state": "flat", "label": "now", "value": ltp, "value_text": pl.fmt(ltp), "tone": "", "pulse": False,
            "sub": cell.get("sub") or ""}


# --- the distance bar ------------------------------------------------------------------------------------------------------------

def _clamp(v: float) -> float:
    return round(max(0.0, min(100.0, v)), 1)


def dist(h: dict, f: Optional[dict] = None) -> Optional[dict]:
    """stop ◀──●──▶ target (see the module doc). None without a Kite position, a premium stop or a target."""
    if not has_position(h):
        return None
    k = _k(h)
    sign = _sign(h)
    stop = fx._stop_of(h, k)
    tg = [float(x) for x in ((h.get("plan") or {}).get("targets") or h.get("targets") or []) if _num(x)]
    tg = [x for x in tg if x > 0]
    if stop is None or not tg:
        return None
    last = tg[-1]
    span = last - stop
    if not span:
        return None
    closed = k["state"] == "closed"
    epx = float(k["entry_px"])
    dot = _num(k.get("exit_px")) if closed else _num(bk.last_ltp(h))
    if dot is None:
        dot = epx
    pos = lambda v: _clamp((v - stop) / span * 100)      # noqa: E731
    legs = k.get("legs") or {}
    nxt = next((i for i in range(len(tg)) if i >= 2 or (legs.get(f"t{i + 1}") or {}).get("status") != "complete"), len(tg) - 1)
    tpx = tg[min(nxt, len(tg) - 1)]
    s_pct = (stop - dot) / dot * 100 if dot else None
    t_pct = (tpx - dot) / dot * 100 if dot else None
    near_stop = s_pct is not None and abs(s_pct) <= CLOSE_STOP_PCT
    stop_d = {"px": stop, "pct": round(s_pct, 1) if s_pct is not None else None, "pct_text": pct_text(s_pct),
              "note": "close to the stop" if near_stop and not closed else "away", "tone": "down" if near_stop and not closed else ""}
    passed = t_pct is not None and t_pct * sign <= 0
    if closed:
        note, tone = target_after_exit(h, tg.index(tpx) + 1, k)
    elif t_pct is not None and (abs(t_pct) <= ALMOST_PCT or (passed and abs(t_pct) <= CLOSE_STOP_PCT)):
        note, tone = "almost there", "up"
    elif passed:
        note, tone = "past the target", "up"
    else:
        note, tone = "", ""
    tgt_d = {"px": tpx, "pct": round(t_pct, 1) if t_pct is not None else None, "pct_text": pct_text(t_pct), "note": note, "tone": tone,
             "name": f"T{tg.index(tpx) + 1}" if len(tg) > 1 else "target"}
    return {"stop": stop_d, "target": tgt_d, "fill": {"px": epx, "pos": pos(epx)}, "dot": pos(dot), "dot_px": dot,
            "ticks": [{"pos": pos(x), "label": f"T{i + 1}", "px": x} for i, x in enumerate(tg[:-1])],
            "zones": [ZONE_STOP, ZONE_TARGET], "closed": closed, "last": last, "sell": sign < 0,
            "danger": bool(near_stop and not closed)}


def target_after_exit(h: dict, n: int, k: dict) -> tuple[str, str]:
    """A closed position's target label: "hit at 11:57 after your exit" (the record reached it later) / "hit" / "not
    reached"."""
    if (k.get("legs") or {}).get(f"t{n}", {}).get("status") == "complete":
        return "hit", "up"
    cl = fx._dt(k.get("closed_at"))
    for x in (h.get("run") or {}).get("hits") or []:
        at = fx._dt(x.get("t"))
        if x.get("k") == n and at is not None and (cl is None or at >= cl):
            return f"hit at {ampm(at)} after your exit", "up"
    return "not reached", ""


# --- the legs ----------------------------------------------------------------------------------------------------------------------

def adjust_legs(h: dict, legs: list[dict], f: Optional[dict]) -> list[dict]:
    """The legs' "now" lines coloured (green within 3 % of a target, red within 3 % of the stop) and a closed position's
    words: the stop "never within x % of the stop" (no Place SL), a target the record reached after your exit."""
    k = _k(h)
    ltp = _num(bk.last_ltp(h))
    closed = k.get("state") == "closed"
    stop = fx._stop_of(h, k)
    near_t = danger = False
    for d in legs:
        if ltp is None:
            break
        if d["leg"] in ("t1", "t2") and d.get("price") is not None and d.get("status") != "complete":
            p = abs((float(d["price"]) - ltp) / ltp * 100)
            if p <= NEAR_PCT or (float(d["price"]) - ltp) * _sign(h) <= 0:
                d["tone"] = "near"
                near_t = True
        if d["leg"] == "stop" and stop is not None and abs((stop - ltp) / ltp * 100) <= NEAR_PCT:
            d["tone"] = "danger"
            danger = True
    for d in legs:
        if d["leg"] == "entry" and d.get("status") == "complete" and not closed:
            d["tone"] = "danger" if danger else "near" if near_t else ""
    if closed:
        for d in legs:
            d.pop("tone", None)
            if d["leg"] == "stop":
                d["closed"] = True
                if d.get("status") != "complete" and (f or {}).get("stop_nearest_pct") is not None:
                    d["detail"] = f"never within {f['stop_nearest_pct']:.1f} % of the stop".replace(".0 %", " %")
            elif d["leg"] in ("t1", "t2") and d.get("status") != "complete":
                n = int(d["leg"][1])
                note, _ = target_after_exit(h, n, k)
                if note.startswith("hit at"):
                    at = next((fx._dt(x.get("t")) for x in (h.get("run") or {}).get("hits") or [] if x.get("k") == n), None)
                    cl = fx._dt(k.get("closed_at"))
                    mins = f" · {int((at - cl).total_seconds() // 60)} min after your exit" if at and cl else ""
                    d["detail"] = f"watched by the app · hit {ampm(at)}{mins}" if at else d["detail"]
            elif d["leg"] == "entry" and (f or {}).get("slippage_entry"):
                s = f["slippage_entry"]
                d["detail"] = f"{d.get('detail') or ''} · slippage {'+' if s['rs'] >= 0 else '−'}{pl.fmt(abs(s['rs']))} vs quote".lstrip(" ·")
    return legs


# --- "channel said next" -------------------------------------------------------------------------------------------------------------

ACT = {"partial": "book part", "exit": "exit", "sl_hit": "SL hit", "stop": "stop change", "trail": "trail", "add": "add-on",
       "active": "active", "avoid": "avoid", "targets": "target done", "price": "price", "note": "note", "edit": "post edited",
       "deleted": "post deleted"}


def _short(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip(" ·,") + "…"


def said_next(h: dict, f: Optional[dict] = None) -> list[dict]:
    """The chips (see the module doc): [{kind fu|edit|exit|record|ai|more, at, at_label, text, tone}], oldest first."""
    k = _k(h)
    if k.get("state") in (None, "none") and not k.get("log"):
        return []
    chips = []
    for x in fx.followups(h):
        if x["action"] == "edit":
            text = "post edited"
            kind = "edit"
        elif x["action"] == "deleted":
            text, kind = "post deleted", "edit"
        else:
            did = _short(str(x.get("did") or "").replace("followed: ", "").split(" · ")[0], 26)
            text = f"{_short(x['text'], 30)}{f' → {did}' if did else ''}"
            kind = "fu"
        chips.append({"kind": kind, "at": x["at"], "text": text, "action": ACT.get(x["action"], x["action"])})
    if k.get("state") == "closed" and k.get("closed_at"):
        chips.append({"kind": "exit", "at": k["closed_at"], "text": f"{exit_text(k)} · {pl.fmt(k.get('exit_px'))}"})
    cr = fx.channel_result(h)
    if cr and cr.get("at") and cr.get("exit_px") is not None and has_position(h):
        chips.append({"kind": "record", "at": cr["at"], "text": f"{cr['outcome']} {pl.fmt(cr['exit_px'])} · record"})
    for a in fx.ai_opinions(k):
        chips.append({"kind": "ai", "at": a["at"], "text": f"[AI] {t12(a['at'])} · reads as {a['reads']} · {'agrees' if a['agrees'] else 'differs'}",
                      "tone": "ai"})
    chips.sort(key=lambda c: str(c.get("at") or ""))
    for c in chips:
        c["at_label"] = t12(c.get("at"))
    if len(chips) > MAX_CHIPS:
        more = len(chips) - (MAX_CHIPS - 1)
        chips = [{"kind": "more", "text": f"+{more}", "n": more, "at": None, "at_label": ""}] + chips[-(MAX_CHIPS - 1):]
    return chips


# --- the trade facts row -------------------------------------------------------------------------------------------------------------

def facts_row(h: dict, f: Optional[dict]) -> Optional[list[dict]]:
    """The six cells: post → fill · slippage at entry · spread at entry · index move in trade · stop nearest · exit by."""
    if not f:
        return None
    s = f.get("slippage_entry")
    slip = f"{'+' if s['rs'] >= 0 else '−'}{pl.fmt(abs(s['rs']))} · {abs(s['pct'] or 0):.1f} %" if s else "—"
    sf, se = f.get("spot_at_fill"), f.get("spot_at_exit")
    idx = f"{h.get('symbol') or 'index'} {pct_text((se - sf) / sf * 100)}" if sf and se else "—"
    sn = f.get("stop_nearest_pct")
    near = "—" if sn is None else (f"{sn:.1f} % · never near" if sn > fx.NEAR_STOP_PCT else f"{sn:.1f} % away")
    ex = "—"
    if f.get("exit_by"):
        who = {"time_stop": "time stop"}.get(f["exit_by"], f["exit_by"])
        ex = f"{who} · {t12(f.get('exit_at'))}" if f.get("exit_at") else who
    return [{"k": "post → fill", "v": dur(f.get("post_to_fill_s")) if f.get("post_to_fill_s") is not None else "—"},
            {"k": "slippage at entry", "v": slip},
            {"k": "spread at entry", "v": pl.fmt(f["spread_entry"]) if f.get("spread_entry") is not None else "—"},
            {"k": "index move in trade", "v": idx},
            {"k": "stop nearest", "v": near, "tone": "down" if sn is not None and sn <= CLOSE_STOP_PCT else ""},
            {"k": "exit by", "v": ex}]


# --- the event log's default fold --------------------------------------------------------------------------------------------------

LOG_OPEN_STATUS = ("new", "waiting", "active", "paused")
LOG_OPEN_KITE = ("armed", "armed_watch", "armed_wait", "entered")


def log_open(h: dict, now: datetime) -> bool:
    """The card's event log open by default: only while the hint is working today (armed / open on Kite, or a paper take
    still open). Closed, skipped, record-only and older hints start folded to their last lines (the page remembers a
    card you opened or folded)."""
    k = _k(h)
    posted = fx._dt(h.get("posted_at"))
    today = (posted is not None and posted.date() == now.date()) or k.get("day") == now.date().isoformat()
    if not today:
        return False
    return k.get("state") in LOG_OPEN_KITE or (bool(h.get("taken")) and h.get("status") in LOG_OPEN_STATUS)


def blocks(config, h: dict, now: datetime) -> dict:
    """Every block for hint_view: {pnl_strip, now_tile, dist, said_next, facts, trade_facts, log_open}."""
    f = fx.for_hint(h, now) if has_position(h) else None
    return {"pnl_strip": pnl_strip(config, h, f, now), "now_tile": now_tile(h), "dist": dist(h, f), "said_next": said_next(h, f),
            "facts": facts_row(h, f), "trade_facts": f, "log_open": log_open(h, now)}
