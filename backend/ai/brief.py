"""🤖 The morning brief (PR 3 · C): at `ai.brief_time` on a trading day the night model reads the facts code gathered
(the Real morning plan, the futures plan and roll queue, the options book's state and last chain line, the night's
high notes, results due within 3 sessions on held names, VIX, event days) and writes at most 5 lines. Code decides
what is read and which numbers may be quoted (every figure in a line must appear in the facts, else it is stripped);
the model writes words. The brief is one Telegram message, one `ai_notes` row (kind "brief") and nothing else: no
action is taken.

  facts(config, now)            the facts, as a dict of sections and as the lines handed to the model
  validate(raw, source_text)    the JSON list checked by code: ≤ LINES_MAX lines of ≤ LINE_MAX chars, no broker
                                name, no figure that is not in the facts
  run(config, now, notifier)    facts -> night model (one call, one retry) -> the row -> the message
  enqueue(config, now, notifier)  one job a day through backend.ai.queue, kind "brief", key brief:<day>
  due(now, config, done, trading) the scheduler's question: ["ai_brief"] at brief_time on a trading day, once
  latest(day)                   {last_at, lines, day, id} for GET /api/ai
"""

import json
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from backend.ai import client, notes
from backend.ai import queue as q
from backend.config import IST, AppConfig
from backend.database.models import get_db, now_ist
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm

logger = setup_logger("ai")

PROMPT_V = "brief-v1"
LINES_MAX, LINE_MAX = 5, 160
RESULTS_SESSIONS = 3                      # results due within this many sessions on a held name make a fact
RESULTS_HORIZON_DAYS = 10
NOTES_MAX = 6                             # high notes of the night listed in the facts at most
KEY_PREFIX = "brief:"                     # ai_jobs.key = brief:<day>
JOB = "ai_brief"                          # the job_runs name
GEN_OPTIONS = {"temperature": 0.2, "num_predict": 400, "num_ctx": 4096, "top_p": 0.9}

SYSTEM_PROMPT = f"""You write the morning brief for a private investor from the facts below, which the app's own code
gathered from its plans, books and filings. You read and explain; you never advise, decide or predict. Reply with JSON
only, one object: {{"lines": ["…", "…"]}} with at most {LINES_MAX} strings, each at most {LINE_MAX} characters of plain
text, one fact per line, the most useful first (the orders going out, then a warning, then futures, options, notes). Quote only numbers written in
the facts, exactly as given; never compute, convert or estimate a number. Say nothing about a section with no facts.
Never name a broker or a trading platform. No markdown, no emoji, no prose around the list."""

RETRY_SUFFIX = ("\nYour previous answer was not valid: {why}. Reply with one JSON object {{\"lines\": [\"…\"]}} only, at most "
                f"{LINES_MAX} strings of at most {LINE_MAX} characters each.")


class BriefInvalid(ValueError):
    """The model's answer is not a usable brief (bad JSON, no line, a broker name)."""


# --- the facts ------------------------------------------------------------------------------------------------------

def day_label(iso: str) -> str:
    """'Mon 17 Nov' from '2026-11-17'."""
    d = date.fromisoformat(iso[:10])
    return f"{d:%a} {d.day} {d:%b}"


def _going(m: dict, action: str) -> list[str]:
    """The symbols of the morning plan's orders of `action` that go out (not skipped)."""
    return [o["symbol"] for o in m.get("orders") or [] if o["action"] == action and not o.get("skip") and o.get("status") != "skipped"]


def _real_facts(config: AppConfig, now: datetime) -> Optional[dict]:
    """The Real morning plan: EXITs, BUYs, cash, free slots, the skips with their reason."""
    from backend.engine import real
    if not real.has_account():
        return None
    m = real.morning(config, now)
    skipped = [f"{o['symbol']} ({(o['skip'] or {}).get('by') or 'skipped'})" for o in m.get("orders") or [] if o.get("skip") or o.get("status") == "skipped"]
    return {"exits": _going(m, "EXIT"), "buys": _going(m, "BUY"), "cash": m.get("cash"), "free": m.get("free"), "slots": m.get("slots"),
            "skipped": skipped, "send_at": ampm(config.real.send_time), "enabled": bool(config.real.enabled), "killed": real.killed()}


def _futures_facts(config: AppConfig, now: datetime) -> Optional[dict]:
    """The futures plan for the open, the lots held, the roll day and the roll queue, results on held names."""
    from backend.futures import book
    if not book.has_account():
        return None
    st = book._state() or {}
    today = now.date().isoformat()
    plan = [o for s in ("long", "short") for o in (st.get("plan") or {}).get(s, []) if not o.get("sent")]
    held = sorted({f"{sym} {pos['side']} {pos['lots']} lot{'s' if pos['lots'] != 1 else ''}"
                   for a in (st.get("accounts") or {}).values() for sym, pos in (a.get("positions") or {}).items()})
    try:
        rq = book.roll_queue(st, config, today)["items"] if st.get("accounts") else []
    except Exception:
        rq = []
    roll = [f"{i['symbol']} {i['action']} ({i['why']})" for i in rq if i.get("due")]
    res = [f"{r['symbol']} {day_label(r['date'])} (in {r['sessions']} session{'s' if r['sessions'] != 1 else ''})"
           for r in (st.get("warnings") or {}).get("results") or []]
    return {"entries": [f"{o['symbol']} {o.get('side') or 'long'}" for o in plan if o["action"] != "EXIT"],
            "exits": [o["symbol"] for o in plan if o["action"] == "EXIT"], "held": held,
            "roll_day": day_label(st["roll_day"]) if st.get("roll_day") else None, "roll_queue": roll, "results": res}


def _options_facts(config: AppConfig, now: datetime) -> Optional[dict]:
    """The options book: the open spread, the last chain line (PCR, walls), VIX, an event day."""
    from backend.options import bias as bs
    from backend.options import book as obook
    if not obook.has_account():
        return None
    st = obook._state() or {}
    today = now.date().isoformat()
    o = st.get("open")
    chain = obook.chain_view(st, today) or {}
    last_day = (obook.days(1) or [{}])[0]
    ev = bs.event_days(config.options.event_days)
    soon = [day_label(d) for d in ev if today <= d <= (now.date() + timedelta(days=3)).isoformat()]
    spread = None
    if o:
        s = o["sold"]
        spread = f"{o['bias']} spread, sold {s['strike']:,.0f} {s['type']} at {s['entry']:.2f}, now {s['last']:.2f}, stop {o['stop_premium']:.2f}"
    walls = chain.get("walls") or {}
    return {"open": spread, "chain_at": chain.get("at"), "pcr": chain.get("pcr"), "support": walls.get("support"),
            "resistance": walls.get("resistance"), "line": chain.get("line"), "vix": chain.get("vix") or last_day.get("vix") or _vix_close(),
            "vix_half": config.options.vix_half, "event_day": bs.is_event_day(today, config.options.event_days), "event_days": soon,
            "last_day": {k: last_day.get(k) for k in ("day", "status", "bias", "pnl", "trades") if k in last_day}}


def _vix_close() -> Optional[float]:
    """India VIX's last stored daily close (the index ticker keeps it in candles_daily); None without one."""
    with get_db() as conn:
        r = conn.execute("SELECT c.close FROM candles_daily c JOIN instruments i ON i.instrument_token = c.instrument_token "
                         "WHERE i.tradingsymbol = 'INDIA VIX' ORDER BY c.date DESC LIMIT 1").fetchone()
    return round(float(r[0]), 2) if r else None


def _held_symbols() -> set:
    """Every stock held in a cash book or the futures book (the notes' scope, "held" only)."""
    return {s for s, v in notes.scope().items() if v["book"] == "held"}


def _results_facts(now: datetime) -> list[str]:
    """Results due within RESULTS_SESSIONS sessions on held names: 'SYM Tue 18 Nov (in 1 session)'."""
    from backend.ingest import events
    syms = sorted(_held_symbols())
    if not syms:
        return []
    try:
        res = events.upcoming_results(now.date().isoformat(), syms, horizon_days=RESULTS_HORIZON_DAYS)
    except Exception:
        return []
    return [f"{s} {day_label(r['date'])} (in {r['sessions']} session{'s' if r['sessions'] != 1 else ''})"
            for s, r in sorted(res.items()) if r["sessions"] <= RESULTS_SESSIONS]


def _notes_facts(config: AppConfig, now: datetime) -> list[dict]:
    """The night's high notes: {symbol, category, book, title, text}."""
    return [{"symbol": n["symbol"], "category": n.get("category") or n["kind"], "book": n["book"], "title": n["title"], "text": n["text"],
             "direction": n.get("direction")} for n in notes.high_notes(notes.night_day(now, config), NOTES_MAX)]


def _safe(fn: Callable, *a):
    """A section that fails is left out: the brief never dies on one book."""
    try:
        return fn(*a)
    except Exception:
        logger.exception("AI brief: %s failed", getattr(fn, "__name__", fn))
        return None


def facts(config: AppConfig, now: Optional[datetime] = None) -> dict:
    """{day, at, real, futures, options, notes, results, lines, empty}: the sections and the lines handed to the model."""
    now = now or datetime.now(IST)
    f = {"day": now.date().isoformat(), "at": now.strftime("%H:%M"), "real": _safe(_real_facts, config, now),
         "futures": _safe(_futures_facts, config, now), "options": _safe(_options_facts, config, now),
         "notes": _safe(_notes_facts, config, now) or [], "results": _safe(_results_facts, now) or []}
    f["lines"] = fact_lines(f)
    f["empty"] = not any((f["real"], f["futures"], f["options"], f["notes"], f["results"]))
    return f


def _join(xs: list, none: str = "none") -> str:
    return ", ".join(str(x) for x in xs) if xs else none


def fact_lines(f: dict) -> list[str]:
    """The facts as plain lines (the prompt's body and the source text of the numbers check)."""
    out = [f"Day: {day_label(f['day'])}, {ampm(f['at'])}"]
    r = f.get("real")
    if r:
        n = len(r["exits"]) + len(r["buys"])
        state = "" if r["enabled"] and not r["killed"] else " (real sending is OFF: a dry run only)"
        cash = f" Cash after {r['cash']:,.0f}; {r['free']} free slot{'s' if r['free'] != 1 else ''} of {r['slots']}." if r["cash"] is not None else ""
        out.append(f"Real: {n} order{'s' if n != 1 else ''} at {r['send_at']}{state}: EXIT {_join(r['exits'])}; BUY {_join(r['buys'])}.{cash} "
                   f"Skipped: {_join(r['skipped'])}.")
    fu = f.get("futures")
    if fu:
        out.append(f"Futures: held {_join(fu['held'])}; entries at the open {_join(fu['entries'])}; exits {_join(fu['exits'])}; "
                   f"roll day {fu['roll_day'] or 'none'}; roll queue {_join(fu['roll_queue'])}; results on held names {_join(fu['results'])}.")
    o = f.get("options")
    if o:
        chain = (f"PCR {o['pcr']}, put wall {o['support']:,.0f}, call wall {o['resistance']:,.0f}"
                 if o.get("pcr") is not None and o.get("support") and o.get("resistance") else "no chain stored")
        if o.get("line"):
            chain += f" ({o['line']})"
        vix = f"VIX {o['vix']}" + (" (over the half-size line)" if o.get("vix") and o["vix"] > o["vix_half"] else "") if o.get("vix") else "VIX unknown"
        ev = "today is an event day (no options entry)" if o["event_day"] else (f"event days soon: {_join(o['event_days'])}" if o["event_days"] else "no event day soon")
        out.append(f"Options: open spread {o['open'] or 'none'}; last chain {chain}; {vix}; {ev}.")
    for n in f.get("notes") or []:
        out.append(f"Note (high, {n['book']}): {n['symbol']} {n['category']}: {n['title']}. {n['text']}")
    if f.get("results"):
        out.append(f"Results within {RESULTS_SESSIONS} sessions on held names: {_join(f['results'])}.")
    return out


# --- the answer --------------------------------------------------------------------------------------------------------

def _as_lines(d):
    """The model's answer as a list of strings, whatever JSON shape it chose under JSON mode: {"lines": [...]}, any list
    value, a dict of numbered string values, a list of {"text"|"line": ...}, or one string with newlines."""
    if isinstance(d, str):
        return [x.strip(" -•") for x in d.splitlines() if x.strip(" -•")]
    if isinstance(d, dict):
        for k in ("lines", "brief", "items"):
            if isinstance(d.get(k), list):
                return _as_lines(d[k])
        lst = next((v for v in d.values() if isinstance(v, list)), None)
        if lst is not None:
            return _as_lines(lst)
        strs = [v for v in d.values() if isinstance(v, (str, int, float)) and str(v).strip()]
        if strs:
            return _as_lines([str(v) for v in strs]) if len(strs) > 1 else _as_lines(str(strs[0]))
        return None
    if isinstance(d, list):
        out = []
        for x in d:
            if isinstance(x, dict):
                v = x.get("text") or x.get("line") or next((v for v in x.values() if isinstance(v, str)), None)
                if v:
                    out.append(v)
            elif isinstance(x, (str, int, float)):
                out.append(x)
        return out
    return None


def validate(raw: str, source_text: str) -> dict:
    """{lines, numbers_checked}: a JSON list (or {"lines": [...]}) of strings, at most LINES_MAX, each clipped to
    LINE_MAX; a broker name anywhere is invalid; a figure not in the facts is stripped (numbers_checked False).
    Raises BriefInvalid."""
    try:
        d = json.loads(raw)
    except ValueError as exc:
        raise BriefInvalid(f"not JSON: {raw[:80]!r}") from exc
    d = _as_lines(d)
    if not isinstance(d, list):
        raise BriefInvalid(f"not a JSON list of strings: {raw[:60]!r}")
    lines = [notes._clip(x, LINE_MAX) for x in d if isinstance(x, (str, int, float)) and str(x).strip()][:LINES_MAX]
    if not lines:
        raise BriefInvalid("no line")
    for text in lines:
        w = notes._blocked_word(text)
        if w:
            raise BriefInvalid(f"names a broker ({w[:3]}…)")
    allowed = notes._digit_tokens(source_text)
    ok, out = True, []
    for text in lines:
        t, good = notes._strip_numbers(text, allowed)
        ok = ok and good
        if t:
            out.append(t)
    if not out:
        raise BriefInvalid("nothing left after the numbers check")
    return {"lines": out, "numbers_checked": ok}


def _generate(config: AppConfig, system: str, user: str) -> dict:
    c = config.ai
    return client.generate(c.night_model, user, system=system, json_mode=True, timeout=float(c.timeout_s), keep_alive=c.keep_alive,
                           options=GEN_OPTIONS)


def ask_model(config: AppConfig, lines: list[str]) -> tuple[dict, dict]:
    """(brief, generate result): the night model's list validated; one retry naming the fault."""
    user = "Facts:\n" + "\n".join(f"- {ln}" for ln in lines)
    source = "\n".join(lines)
    r = _generate(config, SYSTEM_PROMPT, user)
    try:
        return validate(r["text"], source), r
    except BriefInvalid as exc:
        logger.info("AI brief answer invalid (%s); one retry", exc)
        r2 = _generate(config, SYSTEM_PROMPT + RETRY_SUFFIX.format(why=str(exc)[:120]), user)
        r2["tokens"] += r["tokens"]
        r2["ms"] += r["ms"]
        return validate(r2["text"], source), r2


# --- the row, the message, the job --------------------------------------------------------------------------------------

def save(f: dict, brief: dict, model: str, ms: int) -> Optional[int]:
    """One `ai_notes` row of kind "brief" (None when the day's brief exists: the UNIQUE guard on brief:<day>)."""
    data = {"lines": brief["lines"], "numbers_checked": brief["numbers_checked"], "at": f["at"], "alerted": False,
            "facts": {k: f[k] for k in ("real", "futures", "options", "notes", "results", "lines")}}
    with get_db() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO ai_notes (created, day, kind, symbol, book, severity, title, text, data, model, "
                           "prompt_version, ms, source_key) VALUES (?,?,'brief',NULL,NULL,'info',?,?,?,?,?,?,?)",
                           (now_ist(), f["day"], f"Morning brief · {day_label(f['day'])}", "\n".join(brief["lines"]), json.dumps(data),
                            model, PROMPT_V, ms, KEY_PREFIX + f["day"]))
    return cur.lastrowid if cur.rowcount else None


def send(note_id: int, f: dict, lines: list[str], config: AppConfig, notifier=None) -> bool:
    """The Telegram message, once per day (key {day}|ai-brief, channel "system": never held), then the AI messages held
    in the quiet hours (`Notifier.flush_quiet`: with the brief or at quiet_to, whichever comes first)."""
    from backend.alerts import messages as msg
    from backend.alerts.dispatch import Notifier
    notifier = notifier or Notifier(config.alerts, ai=config.ai)
    sent = notifier.send(f"{f['day']}|ai-brief", "system", msg.ai_brief(f["day"], f["at"], lines))
    if sent:
        notes.mark_alerted(note_id)
    flush = getattr(notifier, "flush_quiet", None)          # 🤖 quiet hours: the night's held AI messages follow the brief
    if callable(flush):
        try:
            flush()
        except Exception:
            logger.exception("brief: the quiet-hours flush failed")
    return sent


def run(config: AppConfig, now: Optional[datetime] = None, notifier=None) -> dict:
    """Facts -> model -> validation -> the row -> the message: {id, tokens, note}, or {"skip": why} when there is
    nothing to brief or today's brief exists. Raises BriefInvalid when the second answer is not usable either."""
    now = now or datetime.now(IST)
    f = facts(config, now)
    if f["empty"]:
        return {"skip": "nothing to brief"}
    if latest(f["day"]):
        return {"skip": "today's brief exists"}
    brief, r = ask_model(config, f["lines"])
    note_id = save(f, brief, r["model"], r["ms"])
    if not note_id:
        return {"skip": "today's brief exists"}
    try:
        send(note_id, f, brief["lines"], config, notifier)
    except Exception:
        logger.exception("AI brief message failed")
    return {"id": note_id, "tokens": r["tokens"], "note": f"brief · {len(brief['lines'])} line(s)"}


def enqueue(config: AppConfig, now: Optional[datetime] = None, notifier=None) -> dict:
    """Today's brief as one queue job (kind "brief", key brief:<day>); {id, dedup}. The guard decides at run time."""
    now = now or datetime.now(IST)
    day = now.date().isoformat()
    return q.enqueue(config, "brief", KEY_PREFIX + day, lambda: run(config, None, notifier), model=config.ai.night_model, note="morning brief")


def due(now: datetime, config: AppConfig, done: set, trading: bool) -> list[str]:
    """["ai_brief"] from ai.brief_time until the brief window's end on a trading day, once (pure)."""
    from backend.ai.guard import BRIEF_END
    from backend.alerts.jobs import hm
    c = config.ai
    if not (trading and c.enabled and c.brief) or JOB in done:
        return []
    return [JOB] if hm(c.brief_time) <= now.time() < BRIEF_END else []


# --- reading ---------------------------------------------------------------------------------------------------------------

def latest(day: Optional[str] = None) -> Optional[dict]:
    """{id, day, last_at, lines, title, numbers_checked, alerted}: the newest brief (of `day` when given)."""
    sql, args = "SELECT * FROM ai_notes WHERE kind = 'brief'", []
    if day:
        sql, args = sql + " AND day = ?", [day]
    with get_db() as conn:
        r = conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
    if not r:
        return None
    d = json.loads(r["data"] or "{}")
    return {"id": r["id"], "day": r["day"], "last_at": r["created"], "lines": d.get("lines") or (r["text"] or "").split("\n"),
            "title": r["title"], "numbers_checked": d.get("numbers_checked"), "alerted": bool(d.get("alerted")), "model": r["model"]}


def status() -> dict:
    """GET /api/ai `brief`: {last_at, lines, day, id} of the newest brief, or empty values."""
    b = latest()
    return {"last_at": b["last_at"], "lines": b["lines"], "day": b["day"], "id": b["id"]} if b else {"last_at": None, "lines": [], "day": None, "id": None}
