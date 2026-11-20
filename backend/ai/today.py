"""✨ The Today card (Home) and the Overnight panel's reader: NO model call. Code decides the phase by the IST clock,
the headline, the one thing that matters, the three columns (orders, watch, books), the counts and the actions; the
only model text on the card is today's morning brief (already written by `brief.py`), each line tagged `ai: true`.
The actions are labels the page turns into the existing buttons (skip through POST /api/real/skip after the same
confirm, open a page, scroll to the suggestions): nothing here sends, skips or changes anything.

  phase_for(now, config, trading, eod_done)   morning (to real.send_time) · session (to 3:40 PM) · close (to the
                                              evening job's end) · evening (after it, or a weekend / holiday)
  today_card(config, now)                     the payload of GET /api/today-card and the stream topic `today_card`
"""

import json
from datetime import datetime, time
from typing import Optional

from backend.config import IST, AppConfig
from backend.database.models import get_db
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm

logger = setup_logger("ai")

PHASES = ("morning", "session", "close", "evening")
SESSION_END = time(15, 40)                 # the day summary's time: the session view becomes the close view
RESULTS_SESSIONS = 2                       # results due within this many sessions on a held name go in the watch column
BOOK_ICON = {"real": "💰", "paper": "📝", "options": "🧩"}
BOOK_LABEL = {"real": "Real", "paper": "Paper", "options": "Options"}


def _hm(s: str) -> time:
    h, m = s.split(":")[:2]
    return time(int(h), int(m))


def _eod_done(day: str) -> bool:
    from backend.ai.guard import _eod_done
    return _eod_done(day)


def phase_for(now: datetime, config: AppConfig, trading: Optional[bool] = None, eod_done: Optional[bool] = None) -> str:
    """The card's phase by the clock (code): on a trading day `morning` until real.send_time (9:16), `session` until
    3:40 PM, `close` until the evening job has ended, then `evening`; a weekend or a holiday is `evening` all day."""
    if trading is None:
        from backend.ingest.holidays import is_trading_day
        trading = is_trading_day(now.date())
    if not trading:
        return "evening"
    t = now.time()
    if t < _hm(config.real.send_time):
        return "morning"
    if t < _hm(getattr(config.alerts, "day_summary_time", "15:40") or "15:40"):
        return "session"
    if eod_done is None:
        eod_done = _eod_done(now.date().isoformat())
    return "evening" if eod_done else "close"


# --- the pieces (each never raises: the card comes first) ------------------------------------------------------------------

def _safe(fn, default, *a):
    try:
        return fn(*a)
    except Exception:
        logger.exception("today card: %s failed", getattr(fn, "__name__", fn))
        return default


def _morning(config: AppConfig, now: datetime) -> Optional[dict]:
    from backend.engine import real
    return real.morning(config, now) if real.has_account() else None


def _scores(symbols) -> dict:
    from backend.ai import view
    return view.for_symbols(symbols, "plan")


def _order_item(o: dict, sv: Optional[dict]) -> dict:
    sk = o.get("skip")
    return {"symbol": o["symbol"], "action": o["action"], "qty": o.get("qty"), "value": o.get("value"), "close": o.get("close"),
            "status": o.get("status") or ("skipped" if sk else "pending"), "skip": bool(sk) or o.get("status") == "skipped",
            "skip_by": (sk or {}).get("by") if isinstance(sk, dict) else None, "price": o.get("price"),
            "at": ampm(datetime.fromisoformat(o["at"])) if o.get("at") else None,
            "score": (sv or {}).get("score"), "verdict": (sv or {}).get("verdict")}


def _orders_morning(m: Optional[dict]) -> list[dict]:
    """The dry run's orders (EXITs first, then BUYs) with the AI score joined (the BUYs carry it already)."""
    if not m:
        return []
    orders = m.get("orders") or []
    sc = _safe(_scores, {}, [o["symbol"] for o in orders])
    return [_order_item(o, o.get("ai_view") or sc.get(o["symbol"])) for o in orders]


def _orders_session(day: str) -> list[dict]:
    """The day's order log (sent / failed / skipped, stops included) with the AI score."""
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT symbol, action, qty, status, created_at, price, reason FROM real_orders WHERE day = ? "
                                              "AND status IN ('sent', 'failed', 'skipped') ORDER BY id", (day,))]
    sc = _safe(_scores, {}, [r["symbol"] for r in rows])
    out = []
    for r in rows:
        stop = r["action"] != "BUY" and str(r.get("reason") or "").lower().startswith("stop")
        it = _order_item({"symbol": r["symbol"], "action": "STOP" if stop else r["action"], "qty": r["qty"], "status": r["status"],
                          "price": r["price"], "at": r["created_at"]}, sc.get(r["symbol"]))
        it["status"] = "filled" if r["status"] == "sent" else r["status"]
        out.append(it)
    return out


def _held() -> set:
    from backend.ai import notes
    return {s for s, v in notes.scope().items() if v["book"] == "held"}


def _open_suggestions(now: datetime) -> list[dict]:
    """Open suggestions not past their expiry, newest first."""
    stamp = now.isoformat(timespec="seconds")
    with get_db() as conn:
        rows = conn.execute("SELECT id, kind, symbol, title, confidence, expires_at FROM ai_suggestions WHERE status = 'open' "
                            "AND (expires_at IS NULL OR expires_at > ?) ORDER BY id DESC", (stamp,)).fetchall()
    return [dict(r) for r in rows]


def _until(expires: Optional[str]) -> Optional[str]:
    """'9:16 AM' (today) / 'Wed 9:16 AM' (another day) from an ISO stamp; None without one."""
    if not expires:
        return None
    try:
        d = datetime.fromisoformat(expires)
    except ValueError:
        return None
    return ampm(d)


def _exit_watch(held: set) -> list[dict]:
    """Held names whose latest ok view says exit-watch: {symbol, score, close, fails_below, under}."""
    if not held:
        return []
    marks = ",".join("?" * len(held))
    with get_db() as conn:
        rows = conn.execute(f"SELECT symbol, score, verdict, levels, features, day FROM ai_views WHERE status = 'ok' AND symbol IN ({marks}) "
                            "ORDER BY day, id", tuple(sorted(held))).fetchall()
    latest: dict = {}
    for r in rows:
        latest[r["symbol"]] = r
    out = []
    for s, r in sorted(latest.items()):
        if r["verdict"] != "exit-watch":
            continue
        lv, f = json.loads(r["levels"] or "{}") or {}, json.loads(r["features"] or "{}") or {}
        fb, close = lv.get("fails_below"), f.get("close")
        out.append({"symbol": s, "score": r["score"], "close": close, "fails_below": fb, "day": r["day"],
                    "under": bool(fb and close is not None and close < fb)})
    return out


def _results_due(day: str, held: set) -> list[dict]:
    from backend.ingest import events
    if not held:
        return []
    res = events.upcoming_results(day, sorted(held), horizon_days=10)
    return [{"symbol": s, "date": r["date"], "sessions": r["sessions"]} for s, r in sorted(res.items()) if r["sessions"] <= RESULTS_SESSIONS]


def _deal_notes(day: str) -> list[dict]:
    """The AI's deal notes of the last session that had any (on or before `day`)."""
    with get_db() as conn:
        last = conn.execute("SELECT MAX(day) FROM ai_notes WHERE kind = 'deal' AND day <= ?", (day,)).fetchone()[0]
        if not last:
            return []
        rows = conn.execute("SELECT id, symbol, title, severity FROM ai_notes WHERE kind = 'deal' AND day = ? ORDER BY id", (last,)).fetchall()
    return [dict(r, day=last) for r in rows]


def _day_label(iso: str) -> str:
    from datetime import date
    d = date.fromisoformat(iso[:10])
    return f"{d:%a} {d.day} {d:%b}"


def _watch(now: datetime, day: str, sugg: list, ew: list, held: set) -> list[dict]:
    """The watch column: open suggestions (title + until), exit-watch held names, results due within 2 sessions on held
    names, the AI's deal notes of the last session."""
    out = [{"kind": "suggestion", "symbol": s["symbol"] or None, "text": s["title"], "until": _until(s["expires_at"]), "id": s["id"],
            "confidence": s["confidence"]} for s in sugg]
    for x in ew:
        text = f"{x['symbol']} closed under its support" if x["under"] else f"{x['symbol']} is on exit-watch (score {x['score']})"
        out.append({"kind": "exit_watch", "symbol": x["symbol"], "text": text, "until": None})
    for r in _safe(_results_due, [], day, held):
        when = {0: "today", 1: "next session"}.get(r["sessions"], f"in {r['sessions']} sessions")
        out.append({"kind": "results", "symbol": r["symbol"], "text": f"{r['symbol']} results {_day_label(r['date'])} ({when})", "until": None})
    for n in _safe(_deal_notes, [], day):
        out.append({"kind": "deal", "symbol": n["symbol"], "text": f"{n['symbol']} · {n['title']}", "until": None, "id": n["id"]})
    return out


def _books(config: AppConfig, day: str) -> list[dict]:
    """The day summary's parts shortened: {book, icon, label, value, today} per book (paper: one per account)."""
    from backend.alerts import daysum
    parts = daysum.parts(config, day)
    out = []
    for key in ("real", "paper", "options"):
        p = parts.get(key)
        for x in (p if isinstance(p, list) else [p] if p else []):
            label = BOOK_LABEL[key] + (f" {x['name']}" if key == "paper" and x.get("name") else "")
            out.append({"book": key, "icon": BOOK_ICON[key], "label": label, "value": x.get("value"), "today": x.get("today")})
    return out


def _brief_lines(day: str) -> list[dict]:
    from backend.ai import brief
    b = brief.latest(day)
    return [{"text": ln, "ai": True} for ln in (b or {}).get("lines") or [] if ln]


def _close_line(config: AppConfig, day: str) -> Optional[str]:
    """The day summary's first book line as plain text (no HTML)."""
    import re
    from html import unescape
    from backend.alerts import daysum, messages as msg
    html = msg.day_summary(daysum.parts(config, day), day)
    if not html:
        return None
    lines = html.split("\n")
    line = lines[1] if len(lines) > 1 else lines[0]
    return unescape(re.sub(r"<[^>]+>", "", line)).strip()


def _tonight(config: AppConfig, eod_done: bool) -> str:
    """'tonight: notes, why, suggestions, ratings after 6:30 PM' (or the evening job's end)."""
    after = ampm(config.alerts.job_time)
    if not config.ai.enabled:
        return f"tonight: the evening job at {after}" if not eod_done else "the evening job is done · see you at the open"
    parts = ["notes"] + (["why"] if config.ai.why else []) + ["suggestions"] + (["ratings"] if config.ai.view_enabled else [])
    return f"tonight: {', '.join(parts)} after {after}" if not eod_done else f"tonight's queue: {', '.join(parts)}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def headline(phase: str, config: AppConfig, orders: list, stops: int, close_line: Optional[str], eod_done: bool) -> str:
    """By code: morning "3 orders go out at 9:16 AM" · session "2 of 3 orders filled · 1 stop hit" · close the day
    summary's first line · evening "tonight: notes, why, suggestions, ratings after 6:30 PM"."""
    send = ampm(config.real.send_time)
    if phase == "morning":
        going = [o for o in orders if not o["skip"]]
        return f"{_plural(len(going), 'order')} go{'es' if len(going) == 1 else ''} out at {send}" if going else f"no orders go out at {send}"
    if phase == "session":
        planned = [o for o in orders if o["action"] in ("BUY", "EXIT") and not o["skip"] and o["status"] != "skipped"]
        filled = sum(1 for o in planned if o["status"] == "filled")
        head = f"{filled} of {_plural(len(planned), 'order')} filled" if planned else "no orders today"
        return head + (f" · {_plural(stops, 'stop')} hit" if stops else "")
    if phase == "close":
        return close_line or "the market closed at 3:30 PM · the day summary follows"
    return _tonight(config, eod_done)


def the_one_thing(sugg: list, ew: list, morning_orders: list, config: AppConfig) -> Optional[str]:
    """The one line that matters most (code, by priority): an open high-confidence suggestion before its expiry →
    "decide on GLENMARK before 9:16 AM"; else a held name in exit-watch → "ITC closed under its support"; else the
    biggest pending order; else None (the line hides)."""
    hi = [s for s in sugg if (s.get("confidence") or "").lower() == "high"]
    if hi:
        s = hi[0]
        who = s["symbol"] or s["title"]
        until = _until(s["expires_at"])
        return f"decide on {who} before {until}" if until else f"decide on {who}"
    if ew:
        x = next((x for x in ew if x["under"]), ew[0])
        return f"{x['symbol']} closed under its support" if x["under"] else f"{x['symbol']} is on exit-watch (score {x['score']})"
    pend = [o for o in morning_orders if o["status"] == "pending" and not o["skip"]]
    if pend:
        o = max(pend, key=lambda o: o.get("value") or 0)
        from backend.alerts.messages import inr
        val = f" ({inr(o['value'])})" if o.get("value") else ""
        return f"{o['action']} {o['symbol']} · {o['qty']} shares{val} goes out at {ampm(config.real.send_time)}"
    return None


def actions(phase: str, morning_orders: list, n_sugg: int, has_real: bool) -> list[dict]:
    """Labels only (the page asks first and calls the existing endpoints; nothing here fires): "Skip SYM" for each BUY
    still to go in the morning, "Open the dry run", "Suggestions (n)"."""
    out = []
    if phase == "morning":
        out += [{"label": f"Skip {o['symbol']}", "kind": "skip", "arg": o["symbol"]}
                for o in morning_orders if o["action"] == "BUY" and o["status"] == "pending" and not o["skip"]]
    if has_real:
        out.append({"label": "Open the dry run", "kind": "go", "arg": "portfolio/real"})
    if n_sugg:
        out.append({"label": f"Suggestions ({n_sugg})", "kind": "open", "arg": "suggestions"})
    return out


def today_card(config: AppConfig, now: Optional[datetime] = None) -> dict:
    """{phase, day, at, at_label, headline, the_one_thing, columns: {orders, watch, books}, brief_lines: [{text, ai}],
    actions: [{label, kind, arg}], counts, phases}. Read-only."""
    now = now or datetime.now(IST)
    day = now.date().isoformat()
    from backend.ingest.holidays import is_trading_day
    trading = is_trading_day(now.date())
    eod = _safe(_eod_done, False, day)
    phase = phase_for(now, config, trading, eod)
    m = _safe(_morning, None, config, now)
    morning_orders = _safe(_orders_morning, [], m)
    if phase == "morning":
        orders = morning_orders
    elif phase == "session":
        log = _safe(_orders_session, [], day)
        planned = {(o["symbol"], o["action"]) for o in log}
        orders = log + [{**o, "status": o["status"]} for o in morning_orders
                        if (o["symbol"], o["action"]) not in planned and o["status"] in ("pending", "skipped")]
    else:
        orders = []
    stops = sum(1 for o in (orders if phase == "session" else []) if o["action"] == "STOP")
    held = _safe(_held, set())
    sugg = _safe(_open_suggestions, [], now)
    ew = _safe(_exit_watch, [], held)
    watch = _watch(now, day, sugg, ew, held)
    books = _safe(_books, [], config, day)
    close_line = _safe(_close_line, None, config, day) if phase == "close" else None
    lines = _safe(_brief_lines, [], day)
    acts = actions(phase, morning_orders, len(sugg), bool(m and m.get("started")))
    counts = {"orders": len(orders), "pending": sum(1 for o in orders if o["status"] == "pending" and not o["skip"]),
              "filled": sum(1 for o in orders if o["status"] == "filled"), "failed": sum(1 for o in orders if o["status"] == "failed"),
              "skipped": sum(1 for o in orders if o["skip"]), "stops": stops, "watch": len(watch), "suggestions": len(sugg),
              "exit_watch": len(ew), "results": sum(1 for w in watch if w["kind"] == "results"),
              "deals": sum(1 for w in watch if w["kind"] == "deal"), "books": len(books), "brief": len(lines)}
    return {"phase": phase, "day": day, "at": now.isoformat(timespec="seconds"), "at_label": ampm(now), "trading": trading,
            "headline": headline(phase, config, orders, stops, close_line, eod),
            "the_one_thing": the_one_thing(sugg, ew, morning_orders if phase == "morning" else [], config),
            "columns": {"orders": orders, "watch": watch, "books": books}, "brief_lines": lines, "actions": acts, "counts": counts,
            "steps": [{"key": "brief", "label": f"{ampm(config.ai.brief_time)} brief"},
                      {"key": "orders", "label": f"{ampm(config.real.send_time)} orders"},
                      {"key": "summary", "label": f"{ampm(getattr(config.alerts, 'day_summary_time', '15:40'))} summary"}]}
