"""📖 The week's story: ONE night-model call a week. Code builds the facts (the Week page's payload as text and one line
per closed trade: symbol, book, entry, exit, net, the day it ended, days held, results within 3 sessions of the entry,
the AI score at entry); the model writes at most 3 observations (≤ 160 chars each, every figure from the facts, each
anchored to a day Mon–Fri or "week"). Code validates them like the brief (a figure not in the facts is stripped; a line
with no claim left is dropped; a broker name is refused) and keeps ONE `ai_notes` row of kind "week" (text = the JSON
lines; data {week, model, ms, prompt_version, ...}; UNIQUE on week:<iso>). Telegram "📖 [AI] Week 46 · the story" (kind
"ai", held in the quiet hours). The model reads and explains; it never acts.

  facts(week_iso, today)         (payload, lines): the page payload and the lines handed to the model
  validate(raw, source_text)     {observations: [{text, ai, checked, day}], numbers_checked}; WeekInvalid
  run(config, week_iso, ...)     facts -> model (one call, one retry) -> the row -> the message
  enqueue(config, week_iso, ...) one job (queue kind "night", key week:<iso>)
  latest(week_iso)               {id, observations, at, at_label, model, rows_checked} | None
  due(now, config, done)         ["ai_week"] on Sunday from WEEK_TIME (8:00 PM), once
"""

import json
import re
from datetime import date, datetime, time, timedelta
from typing import Optional

from backend.ai import client, notes
from backend.ai import queue as q
from backend.config import IST, AppConfig
from backend.database.models import get_db, now_ist
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm

logger = setup_logger("ai")

PROMPT_V = "week-v1"
OBS_MAX, OBS_LEN = 3, 160
KEY_PREFIX = "week:"
JOB = "ai_week"
WEEK_TIME = time(20, 0)                           # Sunday evening: the week's story is queued from here
RESULTS_SESSIONS = 3
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
MIN_WORDS = 3                                     # a line with fewer words left after the numbers check says nothing
GEN_OPTIONS = {"temperature": 0.2, "num_predict": 360, "num_ctx": 4096, "top_p": 0.9}

SYSTEM_PROMPT = f"""You write the week's story for a private investor from the facts below, which his own software
computed from his trade books. You read and explain; you never advise, decide, predict or propose a change. Reply with
JSON only, one object: {{"observations": [{{"day": "Mon|Tue|Wed|Thu|Fri|week", "text": "..."}}]}} with at most
{OBS_MAX} observations, each at most {OBS_LEN} characters of plain text, the most telling first, each anchored to the
day it is about (or "week"). Quote only numbers written in the facts, exactly as given; never compute, convert or
estimate a number. Quote a figure only with the word the facts attach to it (net, won, trades, NIFTY, held) and
never move a figure from one thing to another; a percent stays a percent and a net stays a net. Never name a broker or a
trading platform. No markdown, no emoji."""

RETRY_SUFFIX = ("\nYour previous answer was not valid: {why}. Reply with one JSON object {{\"observations\": [{{\"day\": \"...\", "
                "\"text\": \"...\"}}]}} only, at most " + str(OBS_MAX) + " observations of at most " + str(OBS_LEN) + " characters each.")


class WeekInvalid(ValueError):
    """The model's answer is not a usable set of observations."""


# --- the facts ---------------------------------------------------------------------------------------------------------------

def _dl(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return f"{d:%a} {d.day} {d:%b}"


def _rs(v: Optional[float]) -> str:
    from backend.alerts.messages import inr
    return inr(v, sign=True) if v is not None else "—"


def _px(v) -> str:
    if v is None:
        return "—"
    v = float(v)
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def _pct(v) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def _results_near(symbol: Optional[str], entry_day: Optional[str]) -> Optional[bool]:
    if not symbol or not entry_day or symbol == "NIFTY":
        return None
    try:
        from backend.ingest import events
        r = events.next_results(symbol, str(entry_day)[:10])
        return bool(r and r["sessions"] <= RESULTS_SESSIONS)
    except Exception:
        return None


def facts(week_iso: str, today: Optional[str] = None) -> tuple[dict, list[str]]:
    """(the page payload, the lines handed to the model)."""
    from backend.engine import week as wk
    p = wk.week(week_iso, today)
    t = p["totals"]
    lines = [f"Week {p['week'].split('-W')[-1].lstrip('0')}: {_dl(p['from'])} to {_dl(p['to'])}",
             f"Total: net {_rs(t['net'])}; {t['trades']} trades; {t['won']} won" +
             (f"; the books {_pct(p['curve'][-1]['all_books'])} vs NIFTY {_pct(p['curve'][-1]['nifty'])} ({t['vs_nifty_pts']:+.1f} pts)"
              if p["curve"] and t["vs_nifty_pts"] is not None else "")]
    for d, k in zip(p["by_day"], DAYS):
        lines.append(f"{k} {_dl(d['day'])[4:]}: net {_rs(d['net'])}; {d['trades']} trades; {d['won']} won; NIFTY {_pct(d['nifty_pct'])}")
    for b in p["by_book"]:
        lines.append(f"Book {b['book']}: net {_rs(b['net'])}; {b['trades']} trades; {b['won']} won")
    for k in ("best", "worst"):
        x = p.get(k)
        if x:
            sc = f"; AI score at entry {x['score']}" if x.get("score") is not None else ""
            lines.append(f"{k.title()} trade: {x['symbol']} ({x['book']}) net {_rs(x['net'])}, ended {_dl(x['ended'])}{sc}")
    s = p["suggestions"]
    lines.append(f"AI suggestions: {s['made']} made, {s['accepted']} accepted, {s['helped']} helped")
    a = p["ai_view"]
    if a.get("buy_hit") is not None or a.get("baseline") is not None:
        lines.append(f"AI view: buy hit {a['buy_hit']}% at 20 sessions vs the plan baseline {a['baseline']}%")
    from backend.engine.week import _score_at
    for tr in wk.trades(p["from"], p["to"]):
        rn = _results_near(tr["symbol"], tr.get("in"))
        sc = _score_at(tr["symbol"], tr.get("in"))
        lines.append(f"Trade: {tr['symbol']} · {tr['book']} · entry {_px(tr.get('entry'))} · exit {_px(tr.get('exit'))} · net {_rs(tr['net'])}"
                     f" · ended {_dl(tr['out'])}" + (f" ({tr['why']})" if tr.get("why") else "") +
                     (f" · held {tr['days']} days" if tr.get("days") is not None else "") +
                     (f" · results within {RESULTS_SESSIONS} sessions: {'yes' if rn else 'no'}" if rn is not None else "") +
                     (f" · AI score at entry {sc}" if sc is not None else ""))
    return p, lines


# --- the answer ------------------------------------------------------------------------------------------------------------

def _items(d) -> Optional[list]:
    if isinstance(d, dict):
        for k in ("observations", "lines", "items", "story"):
            if isinstance(d.get(k), list):
                return d[k]
        lst = next((v for v in d.values() if isinstance(v, list)), None)
        return lst if lst is not None else ([d] if isinstance(d.get("text"), str) else None)
    return d if isinstance(d, list) else None


def _day_of(v) -> str:
    s = str(v or "").strip()[:3].title()
    return s if s in DAYS else "week"


def validate(raw: str, source_text: str) -> dict:
    """{observations: [{text, ai, checked, day}], numbers_checked}: at most 3, each clipped to 160 chars; a broker name
    anywhere is invalid; a figure not in the facts is stripped (checked stays True for what is left: every remaining
    figure is in the facts; numbers_checked False tells something was stripped); a line with no claim left (under 3
    words) is dropped. Raises WeekInvalid."""
    try:
        d = json.loads(raw)
    except ValueError as exc:
        raise WeekInvalid(f"not JSON: {raw[:80]!r}") from exc
    items = _items(d)
    if not isinstance(items, list):
        raise WeekInvalid(f"not a list of observations: {raw[:60]!r}")
    obs = []
    for x in items:
        if isinstance(x, dict):
            text, day = x.get("text") or x.get("line") or x.get("observation"), x.get("day")
        else:
            text, day = x, None
        if isinstance(text, (str, int, float)) and str(text).strip():
            obs.append((notes._clip(text, OBS_LEN), _day_of(day)))
    if not obs:
        raise WeekInvalid("no observation")
    for text, _ in obs:
        w = notes._blocked_word(text)
        if w:
            raise WeekInvalid(f"names a broker ({w[:3]}…)")
    allowed = notes.typed_tokens(source_text)
    ok, out = True, []
    for text, day in obs:
        t, good = notes._strip_numbers(text, allowed)
        ok = ok and good
        if len(re.findall(r"[A-Za-z]{2,}", t)) >= MIN_WORDS:
            out.append({"text": t, "ai": True, "checked": True, "day": day})
        if len(out) == OBS_MAX:
            break
    if not out:
        raise WeekInvalid("nothing left after the numbers check")
    return {"observations": out, "numbers_checked": ok}


def _generate(config: AppConfig, system: str, user: str) -> dict:
    c = config.ai
    return client.generate(c.night_model, user, system=system, json_mode=True, timeout=float(c.timeout_s), keep_alive=c.keep_alive,
                           options=GEN_OPTIONS)


def ask_model(config: AppConfig, lines: list[str]) -> tuple[dict, dict]:
    """(observations, generate result): validated; one retry naming the fault."""
    user = "Facts:\n" + "\n".join(f"- {ln}" for ln in lines)
    source = "\n".join(lines)
    r = _generate(config, SYSTEM_PROMPT, user)
    try:
        return validate(r["text"], source), r
    except WeekInvalid as exc:
        logger.info("AI week answer invalid (%s); one retry", exc)
        r2 = _generate(config, SYSTEM_PROMPT + RETRY_SUFFIX.format(why=str(exc)[:120]), user)
        r2["tokens"] += r["tokens"]
        r2["ms"] += r["ms"]
        return validate(r2["text"], source), r2


# --- the row, the message, the job ------------------------------------------------------------------------------------------

def _day_iso(week_iso: str, day: str) -> Optional[str]:
    from backend.engine.week import days_of
    return days_of(week_iso)[DAYS.index(day)] if day in DAYS else None


def save(week_iso: str, to: str, res: dict, model: str, ms: int, rows_checked: int) -> Optional[int]:
    """ONE `ai_notes` row of kind "week" (None when the week has one: the UNIQUE guard on week:<iso>)."""
    obs = [{**o, "day_iso": _day_iso(week_iso, o["day"])} for o in res["observations"]]
    n = week_iso.split("-W")[-1].lstrip("0")
    data = {"week": week_iso, "model": model, "ms": ms, "prompt_version": PROMPT_V, "observations": obs,
            "numbers_checked": res["numbers_checked"], "rows_checked": rows_checked, "alerted": False}
    with get_db() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO ai_notes (created, day, kind, symbol, book, severity, title, text, data, model, "
                           "prompt_version, ms, source_key) VALUES (?,?,'week',NULL,NULL,'info',?,?,?,?,?,?,?)",
                           (now_ist(), to, f"Week {n} · the story", "\n".join(o["text"] for o in obs), json.dumps(data, ensure_ascii=False),
                            model, PROMPT_V, ms, KEY_PREFIX + week_iso))
    return cur.lastrowid if cur.rowcount else None


def send(note_id: int, week_iso: str, lines: list[str], config: AppConfig, notifier=None, rows_checked: Optional[int] = None) -> bool:
    """📖 The Telegram message once per week (key {week}|ai-week, kind "ai": held in the quiet hours)."""
    from backend.alerts import messages as msg
    from backend.alerts.dispatch import Notifier
    notifier = notifier or Notifier(config.alerts, ai=config.ai)
    sent = notifier.send(f"{week_iso}|ai-week", "ai", msg.ai_week_story(week_iso, lines, ampm(datetime.now(IST)), rows_checked))
    if sent:
        notes.mark_alerted(note_id)
    return bool(sent)


def run(config: AppConfig, week_iso: str, notifier=None, today: Optional[str] = None, telegram: bool = True) -> dict:
    """Facts -> model -> validation -> the row -> the message: {id, tokens, note}, or {"skip": why} when the week has
    its story already or had no closed trade. Raises WeekInvalid when the second answer is not usable either."""
    if latest(week_iso):
        return {"skip": "this week's story exists"}
    p, lines = facts(week_iso, today)
    if not p["totals"]["trades"]:
        return {"skip": "no closed trade this week"}
    res, r = ask_model(config, lines)
    note_id = save(week_iso, p["to"], res, r["model"], r["ms"], p["totals"]["trades"])
    if not note_id:
        return {"skip": "this week's story exists"}
    if telegram:
        try:
            send(note_id, week_iso, [o["text"] for o in res["observations"]], config, notifier, p["totals"]["trades"])
        except Exception:
            logger.exception("AI week message failed")
    return {"id": note_id, "tokens": r["tokens"], "note": f"week {week_iso} · {len(res['observations'])} observation(s)"}


def running(week_iso: Optional[str] = None) -> bool:
    with get_db() as conn:
        if week_iso:
            return conn.execute("SELECT 1 FROM ai_jobs WHERE kind = 'night' AND key = ? AND status IN ('queued', 'running') LIMIT 1",
                                (KEY_PREFIX + week_iso,)).fetchone() is not None
        return conn.execute("SELECT 1 FROM ai_jobs WHERE kind = 'night' AND key LIKE ? AND status IN ('queued', 'running') LIMIT 1",
                            (KEY_PREFIX + "%",)).fetchone() is not None


def enqueue(config: AppConfig, week_iso: str, notifier=None, telegram: bool = True) -> dict:
    """The week's story as one queue job (kind "night", key week:<iso>); {id, dedup, key}. Off (`ai.week` False): {status: off}."""
    if not getattr(config.ai, "week", True):
        return {"status": "off"}
    key = KEY_PREFIX + week_iso
    job = q.enqueue(config, "night", key, lambda: run(config, week_iso, notifier, telegram=telegram), model=config.ai.night_model,
                    note=f"the week's story · {week_iso}")
    return {**job, "key": key}


def closed_week(now: datetime) -> str:
    """The last ISO week whose Friday is over at `now`: this week from Friday 3:30 PM on (Sat / Sun included), else last week."""
    from backend.engine.week import iso_week
    d = now.date()
    if d.weekday() > 4 or (d.weekday() == 4 and now.time() >= time(15, 30)):
        return iso_week(d)
    return iso_week(d - timedelta(days=7))


def enqueue_due(config: AppConfig, now: Optional[datetime] = None, notifier=None) -> dict:
    """The night batch's step (after the ratings): the last closed week's story when it has none yet (the Sunday job, or
    the first night batch after the week closes when Sunday was missed). Older weeks are never back-filled."""
    now = now or datetime.now(IST)
    if not (config.ai.enabled and getattr(config.ai, "week", True)):
        return {"status": "off"}
    wk = closed_week(now)
    if latest(wk):
        return {"status": "exists", "week": wk}
    return {**enqueue(config, wk, notifier), "week": wk}


def due(now: datetime, config: AppConfig, done: set) -> list[str]:
    """["ai_week"] on Sunday from WEEK_TIME (8:00 PM) until 11:00 PM, once (pure)."""
    c = config.ai
    if not (c.enabled and getattr(c, "week", True)) or JOB in done or now.weekday() != 6:
        return []
    return [JOB] if WEEK_TIME <= now.time() < time(23, 0) else []


# --- reading ---------------------------------------------------------------------------------------------------------------------

def latest(week_iso: str) -> Optional[dict]:
    """{id, week, observations: [{text, ai, checked, day, day_iso}], at, at_label, model, rows_checked, numbers_checked}."""
    with get_db() as conn:
        r = conn.execute("SELECT * FROM ai_notes WHERE kind = 'week' AND source_key = ? ORDER BY id DESC LIMIT 1", (KEY_PREFIX + week_iso,)).fetchone()
    if not r:
        return None
    d = json.loads(r["data"] or "{}")
    obs = d.get("observations")
    if obs is None:
        raw = r["text"] or ""
        try:
            lines = json.loads(raw) if raw.startswith("[") else raw.split("\n")
        except ValueError:
            lines = raw.split("\n")
        obs = [{"text": t, "ai": True, "checked": True, "day": "week", "day_iso": None} for t in lines if t]
    at = r["created"]
    try:
        dt = datetime.fromisoformat(at)
        label = f"{dt:%a} {ampm(dt)}"
    except (TypeError, ValueError):
        label = None
    return {"id": r["id"], "week": week_iso, "observations": obs, "at": at, "at_label": label, "model": r["model"],
            "rows_checked": d.get("rows_checked"), "numbers_checked": d.get("numbers_checked")}
