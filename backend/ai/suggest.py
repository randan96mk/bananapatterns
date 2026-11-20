"""🤖 Suggestions (PR 3 · A): what the AI proposes for the next session, for the owner's review. Code decides
everything that matters: which suggestions are born (one rule per kind, from tonight's notes, the plans, the books and
the calendar), the evidence lines, the action an accept runs, the confidence, the expiry, the outcome five sessions
later and the scorecard. The model writes ONLY the one-line `why` (≤ 180 chars) from the evidence, validated like a
note: no figure that is not in the evidence, no broker name. Nothing is ever acted on by the model: an accept runs the
SAME code path as the hand action (the route's function, never HTTP) and only when the owner taps or replies.

  candidates(config, now)        the rules: skip, exit, flag, hold_options
  enqueue_tonight(config, now)   one night job "suggest:<day>" through the queue (after the notes, FIFO)
  run_night(config, now)         outcomes scored, candidates born (the model's why per candidate), the evening list
  expire_open(now)               every scheduler tick: past expires_at -> expired (no tap)
  accept / dismiss / later       the three decisions; accept runs the action once (a second accept is refused)
  scorecard(month)               per kind {made, accepted, dismissed, expired, helped_when_accepted, would_have_helped,
                                 muted} + totals {month, accepted_outcome_rs, dismissed_outcome_rs, best_kind}
  open_flags(day)                {symbol: text} of the open `flag` suggestions for `day` (real.morning's ai_flags)
Rows live in `ai_suggestions` (never deleted; UNIQUE (kind, symbol, day)); the prompt version is `PROMPT_V`.
"""

import json
import re
import threading
from datetime import date, datetime, timedelta
from typing import Optional

from backend.ai import client
from backend.ai import notes
from backend.ai import queue as q
from backend.config import IST, AppConfig
from backend.database.models import find_instrument, get_db, now_ist
from backend.logging_config.logger import setup_logger

logger = setup_logger("ai")

PROMPT_V = "sugg-v1"
WHY_MAX = 180
KINDS = ("skip", "exit", "flag", "hold_options")
ICON = {"skip": "⏸", "exit": "🚪", "flag": "🚩", "hold_options": "🧩"}
LABEL = {"skip": "skip", "exit": "exit", "flag": "flag", "hold_options": "hold options"}
CONFIDENCES = ("low", "medium", "high")
REASONS = ("wrong", "knew", "later", "none")
STATUSES = ("open", "accepted", "dismissed", "expired")
TWO_STEP = ("exit", "hold_options")              # Telegram: `accept N` answers with the confirm text, `yes N` within 10 min runs it
YES_MINUTES = 10
LATER_HOURS = 4
OUTCOME_SESSIONS = 5
DEAL_DISCOUNT = -3.0                             # a block / bulk deal this far under the close flags a planned name
RESULTS_SESSIONS = 3
KEY_PREFIX = "suggest:"
GEN_OPTIONS = {"temperature": 0.1, "num_predict": 120, "num_ctx": 1536, "top_p": 0.9}
HIGH_CATEGORIES = ("pledge", "legal", "rating")  # a high note of these kinds: confidence high on a skip
PENDING_KEY = "ai.sugg_pending"                  # app_settings: the Telegram `accept N` waiting for its `yes N`

_lock = threading.Lock()

SYSTEM_PROMPT = f"""You write one short line for a private investor who reviews a suggestion his own software made
about one of his stocks. You read and explain; you never advise, decide or predict. The suggestion and its evidence are
given; write WHY the evidence supports it, in plain words, at most {WHY_MAX} characters, one sentence. Quote only the
numbers written in the evidence, exactly as given; never compute, convert or estimate a number. Never name a broker or a
trading platform. Reply with JSON only: {{"why": "..."}}. No markdown."""

RETRY_SUFFIX = "\nYour previous answer was not valid: {why}. Reply with the JSON object only."


class SuggestInvalid(ValueError):
    """The model's `why` is not usable (bad JSON, empty, a broker name)."""


class SuggestionState(ValueError):
    """The decision does not fit the row's state (already decided, no second later, no such id)."""


# --- small helpers ----------------------------------------------------------------------------------------------------

def _at(day: str, hhmm: str) -> str:
    """ISO datetime of `hhmm` (HH:MM) on `day`, IST."""
    from backend.alerts.jobs import hm
    return datetime.combine(date.fromisoformat(day), hm(hhmm), tzinfo=IST).isoformat(timespec="seconds")


def muted_kinds(config: AppConfig) -> set:
    return {k.strip() for k in (config.ai.suggest_muted or "").split(",") if k.strip() in KINDS}


def set_muted(config: AppConfig, kinds) -> list[str]:
    """Save the kinds never born (`ai.suggest_muted`); unknown kinds are refused (ValueError)."""
    from backend.config import save_config
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise ValueError(f"unknown suggestion kind(s): {', '.join(bad)}")
    config.ai.suggest_muted = ",".join(k for k in KINDS if k in set(kinds))
    save_config(config)
    return sorted(set(kinds), key=KINDS.index)


def mute(config: AppConfig, kind: str, on: bool) -> list[str]:
    """One kind muted (on) or born again (off); the muted list after."""
    if kind not in KINDS:
        raise ValueError(f"unknown suggestion kind: {kind}")
    cur = muted_kinds(config)
    (cur.add if on else cur.discard)(kind)
    return set_muted(config, cur)


def apply_settings(config: AppConfig, flags: dict) -> dict:
    """PUT /api/ai/settings: pop and apply `suggest_muted` (comma list) and `max_suggestions_open`; the rest is
    returned for the switches. ValueError on a bad kind or cap."""
    if "suggest_muted" in flags:
        raw = flags.pop("suggest_muted") or ""
        set_muted(config, [k.strip() for k in raw.split(",") if k.strip()])
    if "max_suggestions_open" in flags:
        n = int(flags.pop("max_suggestions_open"))
        if not 1 <= n <= 50:
            raise ValueError("max_suggestions_open must be 1-50")
        config.ai.max_suggestions_open = n
    return flags


def _close_series(symbol: str, since: str, limit: int = 10) -> list[dict]:
    """The stock's daily bars from `since` (inclusive), ascending; [] without a token."""
    with get_db() as conn:
        inst = find_instrument(conn, symbol, "NSE")
        if not inst:
            return []
        rows = conn.execute("SELECT date, open, high, low, close FROM candles_daily WHERE instrument_token = ? AND date >= ? "
                            "ORDER BY date LIMIT ?", (inst["instrument_token"], since, limit)).fetchall()
    return [dict(r) for r in rows]


def _tonight_notes(day: str) -> list[dict]:
    """The night's filing / deal notes (the why notes are not sources)."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM ai_notes WHERE day = ? AND kind IN ('filing', 'deal') ORDER BY id DESC", (day,)).fetchall()
    return [notes._row(r) for r in rows]


def _note_line(n: dict) -> dict:
    return {"icon": "🤖", "text": f"{n['severity']} note · {n['title']}: {n['text']}", "url": n.get("url") or None, "note_id": n["id"]}


def _deal_discount(n: dict) -> Optional[float]:
    """The worst discount to the close among a deal note's rows (negative = under the close), from its numbers."""
    vals = [-float(x["value"]) for x in n.get("numbers") or [] if x.get("label") == "vs close"]
    return min(vals) if vals else None


def _negative_high(n: dict) -> bool:
    return n["severity"] == "high" and (n.get("direction") == "negative" or n.get("category") in HIGH_CATEGORIES
                                        or (n["kind"] == "deal" and (_deal_discount(n) or 0) <= DEAL_DISCOUNT))


# --- the scope: plans and holdings ---------------------------------------------------------------------------------------

def _real_plan() -> dict[str, dict]:
    """{symbol: order} of the real plan's BUYs not sent yet."""
    from backend.engine import real
    st = real._state() or {}
    return {o["symbol"]: o for o in st.get("orders", []) if o["action"] == "BUY" and not o.get("sent")}


def _paper_plan() -> dict[str, dict]:
    """{symbol: item} of the latest E+ plan (the paper book's next buys)."""
    from backend.alerts import plan as pl
    return {i["symbol"]: i for i in pl.latest_plan()[1]}


def _held() -> dict[str, dict]:
    """{symbol: {book, qty, last, entry}} of every held name: Real, Paper, the cash guard. Real wins."""
    out: dict[str, dict] = {}
    try:
        from backend.engine import real
        for s, p in ((real._state() or {}).get("positions") or {}).items():
            out[s] = {"book": "real", "qty": p["qty"], "last": p.get("last") or p["px"], "entry": p["px"]}
    except Exception:
        logger.exception("suggest: real positions")
    try:
        from backend.engine import paper
        for st in paper._states().values():
            for s, p in (st.get("positions") or {}).items():
                out.setdefault(s, {"book": "paper", "qty": p["qty"], "last": p.get("last") or p["px"], "entry": p["px"]})
    except Exception:
        logger.exception("suggest: paper positions")
    try:
        from backend.alerts import guard as aguard
        for p in aguard.positions():
            out.setdefault(p["symbol"], {"book": "paper", "qty": p.get("qty"), "last": p.get("last") or p.get("avg_price"), "entry": p.get("avg_price")})
    except Exception:
        logger.exception("suggest: guard positions")
    return out


# --- the rules: one per kind -----------------------------------------------------------------------------------------------

def _skip_candidates(config: AppConfig, day: str, tomorrow: str, tonight: list[dict]) -> list[dict]:
    """1 · `skip`: a planned name (Real BUY or E+ plan item) with a high negative note tonight, or a deal note at
    ≥ 3% under the close. Real plan names get the real skip; Paper-only plans a note-only suggestion."""
    real_plan, paper_plan = _real_plan(), _paper_plan()
    out = []
    for n in tonight:
        s = n["symbol"]
        if s not in real_plan and s not in paper_plan:
            continue
        neg_high = n["severity"] == "high" and (n.get("direction") == "negative" or n.get("category") in HIGH_CATEGORIES)
        deal_cut = n["kind"] == "deal" and (_deal_discount(n) or 0) <= DEAL_DISCOUNT
        if not (neg_high or deal_cut):
            continue
        o = real_plan.get(s)
        value = round(o["qty"] * o["close"], 2) if o and o.get("qty") and o.get("close") else None
        promoter = n["kind"] == "deal" and n["severity"] == "high" and "promoter" in (n["text"] or "").lower()
        conf = "high" if n.get("category") in ("pledge", "legal") or promoter else "medium"
        ev = [_note_line(n), {"icon": "📋", "text": f"{s} is in tomorrow's {'Real' if o else 'Paper'} plan" + (f": BUY {o['qty']} at ~{o['close']:,.2f}" if o else "")}]
        action = {"label": f"Skip {s} tomorrow", "path": "POST /api/real/skip", "args": {"symbol": s, "on": True},
                  "confirm_text": f"Skip {s} on tomorrow's send? The BUY stays out for the day; nothing else changes."} if o else None
        out.append({"kind": "skip", "symbol": s, "book": "real" if o else "paper", "title": f"Skip {s} tomorrow", "evidence": ev,
                    "action": action, "confidence": conf, "expires_at": _at(tomorrow, config.real.send_time), "note_id": n["id"],
                    "value": value, "for_day": tomorrow})
    return out


def _exit_candidates(config: AppConfig, day: str, tomorrow: str, tonight: list[dict]) -> list[dict]:
    """2 · `exit`: a held name (Real / Paper) with a high negative note tonight. The action navigates to the
    book's page (the exit is the row's own button; the AI never lowers a stop or sends an order)."""
    held = _held()
    route = {"real": "portfolio/real", "paper": "portfolio/paper"}
    out = []
    for n in tonight:
        s = n["symbol"]
        h = held.get(s)
        if not h or not _negative_high(n) or n.get("direction") == "positive":
            continue
        pos = f"held in {h['book'].title()}: {h['qty']} at {h['entry']:,.2f}" + (f", last {h['last']:,.2f}" if h.get("last") else "")
        ev = [_note_line(n), {"icon": "📦", "text": pos}]
        out.append({"kind": "exit", "symbol": s, "book": h["book"], "title": f"Consider exiting {s}", "evidence": ev,
                    "action": {"label": f"Open {h['book'].title()}", "path": "ui", "route": route[h["book"]], "hint": "Exit… on the row",
                               "confirm_text": f"Open the {h['book'].title()} page: the exit is the row's own button."},
                    "confidence": "medium", "expires_at": _at(tomorrow, config.real.send_time), "note_id": n["id"],
                    "value": round((h.get("qty") or 0) * (h.get("last") or 0), 2) or None, "for_day": tomorrow})
    return out


def _flag_candidates(config: AppConfig, day: str, tomorrow: str, tonight: list[dict]) -> list[dict]:
    """3 · `flag`: a planned name with a deal note at ≥ 3% under the close, or results within 3 sessions: a warning
    line on the 9:00 dry run (`real.morning` ai_flags). No action to run."""
    from backend.ingest import events
    real_plan, paper_plan = _real_plan(), _paper_plan()
    planned = set(real_plan) | set(paper_plan)
    out, seen = [], set()
    for n in tonight:
        s = n["symbol"]
        if s in planned and s not in seen and n["kind"] == "deal" and (_deal_discount(n) or 0) <= DEAL_DISCOUNT:
            seen.add(s)
            out.append(_flag(s, f"Deal {abs(_deal_discount(n)):.1f}% under the close", [_note_line(n)], n["id"], real_plan.get(s), tomorrow, config))
    try:
        res = events.upcoming_results(tomorrow, sorted(planned - seen)) if planned - seen else {}
    except Exception:
        res = {}
    for s, r in sorted(res.items()):
        if r["sessions"] <= RESULTS_SESSIONS:
            ev = [{"icon": "📅", "text": f"results {r['date']} (in {r['sessions']} session{'s' if r['sessions'] != 1 else ''})"}]
            out.append(_flag(s, f"Results in {r['sessions']} session{'s' if r['sessions'] != 1 else ''}", ev, None, real_plan.get(s), tomorrow, config))
    return out


def _flag(s: str, what: str, ev: list, note_id, o: Optional[dict], tomorrow: str, config: AppConfig) -> dict:
    ev = ev + [{"icon": "📋", "text": f"{s} is in tomorrow's plan" + (f": BUY {o['qty']} at ~{o['close']:,.2f}" if o else "")}]
    return {"kind": "flag", "symbol": s, "book": "real" if o else "paper", "title": f"Flag {s}: {what}", "evidence": ev, "action": None,
            "confidence": "high", "expires_at": _at(tomorrow, config.real.preview_time or "09:00"), "note_id": note_id,
            "value": round(o["qty"] * o["close"], 2) if o and o.get("qty") and o.get("close") else None, "for_day": tomorrow}


_EVENT_RE = re.compile(r"rbi\b.*polic|monetary policy|union budget|budget speech", re.I)


def event_tomorrow(tomorrow: str) -> Optional[str]:
    """A calendar entry (corp_events) or a filing of the last 3 days naming RBI policy / the Union Budget for
    `tomorrow`; the text found, or None."""
    since = (date.fromisoformat(tomorrow) - timedelta(days=3)).isoformat()
    with get_db() as conn:
        for r in conn.execute("SELECT purpose, descr FROM corp_events WHERE date = ?", (tomorrow,)):
            text = f"{r['purpose']} {r['descr']}"
            if _EVENT_RE.search(text):
                return text.strip()[:120]
        for r in conn.execute("SELECT category, text FROM filings WHERE dt >= ? ORDER BY dt DESC LIMIT 400", (since,)):
            text = f"{r['category']} {r['text'] or ''}"
            if _EVENT_RE.search(text) and tomorrow in text:
                return " ".join(text.split())[:120]
    return None


def _vix_close(day: str) -> Optional[float]:
    with get_db() as conn:
        r = conn.execute("SELECT payload FROM options_days WHERE day <= ? ORDER BY day DESC LIMIT 1", (day,)).fetchone()
    return (json.loads(r[0]).get("vix") if r else None)


def _hold_options_candidates(config: AppConfig, day: str, tomorrow: str) -> list[dict]:
    """4 · `hold_options`: an event day tomorrow (RBI policy / Union Budget) or a VIX close over options.vix_half:
    action POST /api/options/hold. Only while the options book exists."""
    from backend.options import book as obook
    if not obook.has_account():
        return []
    ev = []
    text = event_tomorrow(tomorrow)
    if text:
        ev.append({"icon": "📅", "text": f"event day tomorrow: {text}"})
    vix = _vix_close(day)
    if vix is not None and vix > float(config.options.vix_half):
        ev.append({"icon": "📈", "text": f"India VIX closed at {vix:.1f}, over the half-size line {config.options.vix_half:g}"})
    if not ev:
        return []
    return [{"kind": "hold_options", "symbol": "NIFTY", "book": "options", "title": "Hold the options book tomorrow", "evidence": ev,
             "action": {"label": "Hold options tomorrow", "path": "POST /api/options/hold", "args": {},
                        "confirm_text": "No new options spreads tomorrow? An open spread keeps its exits; the switch stays as it is."},
             "confidence": "medium", "expires_at": _at(tomorrow, config.options.window_end), "note_id": None, "value": None, "for_day": tomorrow}]


def candidates(config: AppConfig, now: Optional[datetime] = None) -> list[dict]:
    """Every candidate of the night in kind order, muted kinds left out, one per symbol (the first kind wins)."""
    from backend.engine import real
    now = now or datetime.now(IST)
    day = notes.night_day(now, config)
    tomorrow = real.next_session(day)
    tonight = _tonight_notes(day)
    muted = muted_kinds(config)
    out: list[dict] = []
    for kind, fn in (("skip", lambda: _skip_candidates(config, day, tomorrow, tonight)),
                     ("exit", lambda: _exit_candidates(config, day, tomorrow, tonight)),
                     ("flag", lambda: _flag_candidates(config, day, tomorrow, tonight)),
                     ("hold_options", lambda: _hold_options_candidates(config, day, tomorrow))):
        if kind in muted:
            continue
        try:
            out += fn()
        except Exception:
            logger.exception("suggest: the %s rule failed", kind)
    seen, uniq = set(), []
    for c in out:
        key = c["symbol"] or c["kind"]
        if key not in seen:
            seen.add(key)
            uniq.append({**c, "day": day})
    return uniq


# --- the model's why ---------------------------------------------------------------------------------------------------------

def why_prompt(c: dict) -> str:
    lines = [f"Suggestion: {c['title']} ({LABEL[c['kind']]})", "Evidence:"] + [f"- {e['text']}" for e in c["evidence"]]
    lines.append("Write why the evidence supports the suggestion. Quote only the numbers above.")
    return "\n".join(lines)


def evidence_text(c: dict) -> str:
    return " ".join(e["text"] for e in c["evidence"])


def validate_why(raw: str, evidence: str) -> dict:
    """{why, numbers_checked}: JSON with a `why`, clipped to WHY_MAX, no broker name (invalid), no figure that is not
    in the evidence (stripped; numbers_checked False). Raises SuggestInvalid."""
    try:
        d = json.loads(raw)
    except ValueError as exc:
        raise SuggestInvalid(f"not JSON: {raw[:80]!r}") from exc
    if not isinstance(d, dict):
        raise SuggestInvalid("not a JSON object")
    why = notes._clip(d.get("why"), WHY_MAX)
    if not why:
        raise SuggestInvalid("why missing")
    w = notes._blocked_word(why)
    if w:
        raise SuggestInvalid(f"names a broker ({w[:3]}…)")
    why, ok = notes._strip_numbers(why, notes.typed_tokens(evidence))
    return {"why": why or "…", "numbers_checked": ok}


def _generate(config: AppConfig, system: str, user: str) -> dict:
    c = config.ai
    return client.generate(c.night_model, user, system=system, json_mode=True, timeout=float(c.timeout_s), keep_alive=c.keep_alive,
                           options=GEN_OPTIONS)


def model_why(config: AppConfig, c: dict) -> tuple[dict, dict]:
    """(validated why, generate result); one retry on an invalid answer, then SuggestInvalid."""
    user, ev = why_prompt(c), evidence_text(c)
    r = _generate(config, SYSTEM_PROMPT, user)
    try:
        return validate_why(r["text"], ev), r
    except SuggestInvalid as exc:
        r2 = _generate(config, SYSTEM_PROMPT + RETRY_SUFFIX.format(why=str(exc)[:120]), user)
        r2["tokens"], r2["ms"] = r2["tokens"] + r["tokens"], r2["ms"] + r["ms"]
        return validate_why(r2["text"], ev), r2


# --- birth -----------------------------------------------------------------------------------------------------------------------

def open_count() -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM ai_suggestions WHERE status = 'open'").fetchone()[0]


def save(c: dict, why: Optional[dict], model: Optional[str]) -> Optional[int]:
    """One row (None when (kind, symbol, day) exists already). `why` None = the model gave nothing usable: the first
    evidence line stands in and `data.why_source` says "code"."""
    data = {"value": c.get("value"), "why_source": "model" if why else "code", "numbers_checked": (why or {}).get("numbers_checked"),
            "later": False}
    text = why["why"] if why else c["evidence"][0]["text"][:WHY_MAX]
    with get_db() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO ai_suggestions (created, day, for_day, kind, symbol, book, title, why, evidence, action, "
                           "confidence, expires_at, status, note_id, model, prompt_version, data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?)",
                           (now_ist(), c["day"], c.get("for_day"), c["kind"], c["symbol"], c.get("book"), c["title"], text,
                            json.dumps(c["evidence"]), json.dumps(c["action"]) if c.get("action") else None, c["confidence"],
                            c.get("expires_at"), c.get("note_id"), model, PROMPT_V, json.dumps(data)))
    return cur.lastrowid if cur.rowcount else None


def born_today(day: str) -> set:
    with get_db() as conn:
        return {(r[0], r[1]) for r in conn.execute("SELECT kind, symbol FROM ai_suggestions WHERE day = ?", (day,))}


def birth(config: AppConfig, now: Optional[datetime] = None) -> dict:
    """Score the due outcomes, then give birth to tonight's candidates (the model's why each, one call per
    suggestion) up to the open cap: {born: [ids], capped, tokens, outcomes}."""
    now = now or datetime.now(IST)
    outcomes = score_outcomes(now)
    cands = candidates(config, now)
    room = max(0, int(config.ai.max_suggestions_open) - open_count())
    done = born_today(cands[0]["day"]) if cands else set()
    out = {"born": [], "capped": 0, "tokens": 0, "outcomes": outcomes, "failed": 0}
    for c in cands:
        if (c["kind"], c["symbol"]) in done:
            continue
        if len(out["born"]) >= room:
            out["capped"] += 1
            continue
        why, model = None, None
        try:
            why, r = model_why(config, c)
            model, out["tokens"] = r["model"], out["tokens"] + r["tokens"]
        except (SuggestInvalid, client.AIError) as exc:
            out["failed"] += 1
            logger.warning("suggest: no usable why for %s %s (%s); the evidence line stands in", c["kind"], c["symbol"], exc)
        sid = save(c, why, model)
        if sid:
            out["born"].append(sid)
    return out


def run_night(config: AppConfig, now: Optional[datetime] = None, notifier=None) -> dict:
    """The night job: outcomes, births, the evening list on Telegram (once per day), the scorecard line on the first
    night of a month. Returns the queue's {tokens, note}."""
    from backend.alerts import messages as msg
    from backend.alerts.dispatch import Notifier
    from backend.engine import real
    now = now or datetime.now(IST)
    res = birth(config, now)
    day = notes.night_day(now, config)
    n = len(res["born"])
    try:
        notifier = notifier or Notifier(config.alerts, ai=config.ai)
        if n:
            rows = [get(i) for i in res["born"]]
            notifier.send(f"{day}|ai-sugg", "ai", msg.ai_suggestions(rows, real.next_session(day)))
        month = day[:7]                                   # the scorecard line: once, on the first night batch of a month
        if not _scorecard_sent(month):
            prev = (date.fromisoformat(day).replace(day=1) - timedelta(days=1)).isoformat()[:7]
            if scorecard(prev)["totals"]["made"]:
                notifier.send(f"{month}|ai-scorecard", "ai", msg.ai_scorecard(scorecard(prev), prev))
    except Exception:
        logger.exception("suggest: the evening message failed")
    return {"tokens": res["tokens"], "note": f"{n} suggestion(s) born · {res['capped']} over the cap · {res['outcomes']} outcome(s) scored"}


def _scorecard_sent(month: str) -> bool:
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM sent_alerts WHERE key = ?", (f"{month}|ai-scorecard",)).fetchone() is not None


def running() -> bool:
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM ai_jobs WHERE kind = 'night' AND key LIKE ? AND status IN ('queued', 'running') LIMIT 1",
                            (KEY_PREFIX + "%",)).fetchone() is not None


def enqueue_tonight(config: AppConfig, now: Optional[datetime] = None, notifier=None) -> dict:
    """One night job "suggest:<day>" (FIFO after the notes the evening job queued); {id, dedup, key}."""
    now = now or datetime.now(IST)
    key = f"{KEY_PREFIX}{notes.night_day(now, config)}"
    job = q.enqueue(config, "night", key, lambda: run_night(config, datetime.now(IST), notifier), model=config.ai.night_model,
                    note="suggestions for the next session")
    return {**job, "key": key}


# --- expiry and the decisions ------------------------------------------------------------------------------------------------------

def expire_open(now: Optional[datetime] = None) -> int:
    """Every open suggestion past its expires_at -> expired (no tap). How many."""
    now = now or datetime.now(IST)
    with get_db() as conn:
        return conn.execute("UPDATE ai_suggestions SET status = 'expired', decided_at = ? WHERE status = 'open' AND expires_at IS NOT NULL "
                            "AND expires_at <= ?", (now_ist(), now.isoformat(timespec="seconds"))).rowcount


def _row(r) -> dict:
    d = json.loads(r["data"] or "{}")
    return {"id": r["id"], "created": r["created"], "day": r["day"], "for_day": r["for_day"], "kind": r["kind"], "icon": ICON.get(r["kind"], "🤖"),
            "symbol": r["symbol"], "book": r["book"], "title": r["title"], "why": r["why"], "evidence": json.loads(r["evidence"] or "[]"),
            "action": json.loads(r["action"]) if r["action"] else None, "confidence": r["confidence"], "expires_at": r["expires_at"],
            "status": r["status"], "decided_at": r["decided_at"], "reason": r["reason"],
            "outcome": json.loads(r["outcome"]) if r["outcome"] else None, "note_id": r["note_id"], "model": r["model"],
            "prompt_version": r["prompt_version"], "value": d.get("value"), "why_source": d.get("why_source"),
            "numbers_checked": d.get("numbers_checked"), "later": bool(d.get("later")), "result": d.get("result")}


def get(sid: int) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM ai_suggestions WHERE id = ?", (sid,)).fetchone()
    return _row(r) if r else None


def _decide(sid: int, status: str, reason: Optional[str] = None, extra: Optional[dict] = None) -> dict:
    """Move an OPEN row to `status` (accepted / dismissed) under the lock; SuggestionState otherwise."""
    with _lock:
        s = get(sid)
        if not s:
            raise SuggestionState("no such suggestion")
        if s["status"] != "open":
            raise SuggestionState(f"already {s['status']}")
        with get_db() as conn:
            d = json.loads(conn.execute("SELECT data FROM ai_suggestions WHERE id = ?", (sid,)).fetchone()[0] or "{}")
            d.update(extra or {})
            conn.execute("UPDATE ai_suggestions SET status = ?, decided_at = ?, reason = ?, data = ? WHERE id = ?",
                         (status, now_ist(), reason, json.dumps(d), sid))
    return get(sid)


def run_action(s: dict, config: AppConfig, now: Optional[datetime] = None) -> dict:
    """The accept's action through the SAME function the hand action calls: real.skip_order for the real skip,
    options.book.hold_today for the options hold; a "ui" or missing action runs nothing. {ran, path, result}."""
    a = s.get("action") or {}
    path = a.get("path")
    if path == "POST /api/real/skip":
        from backend.engine import real
        m = real.skip_order(config, a["args"]["symbol"], True, f"AI suggestion #{s['id']}", now)
        return {"ran": True, "path": path, "result": {"skipped": m.get("skipped")}}
    if path == "POST /api/options/hold":
        from backend.options import book as obook
        r = obook.hold_today(config, now, f"AI suggestion #{s['id']}")
        return {"ran": True, "path": path, "result": r}
    return {"ran": False, "path": path, "result": None}


def accept(sid: int, config: AppConfig, now: Optional[datetime] = None, who: str = "page") -> dict:
    """Accept: the row moves to accepted FIRST (so the action can never run twice), then the action runs; a failing
    action is recorded on the row (data.result.error) and re-raised as ValueError."""
    s = _decide(sid, "accepted", None, {"who": who})
    try:
        res = run_action(s, config, now)
    except ValueError as exc:
        _patch(sid, {"result": {"ran": False, "error": str(exc)[:200]}})
        raise ValueError(f"accepted, but the action was refused: {exc}") from exc
    _patch(sid, {"result": res})
    return {**get(sid), "action_result": res}


def _patch(sid: int, extra: dict) -> None:
    with get_db() as conn:
        d = json.loads(conn.execute("SELECT data FROM ai_suggestions WHERE id = ?", (sid,)).fetchone()[0] or "{}")
        d.update(extra)
        conn.execute("UPDATE ai_suggestions SET data = ? WHERE id = ?", (json.dumps(d), sid))


def dismiss(sid: int, reason: str = "none", who: str = "page") -> dict:
    """Dismiss with a reason (wrong | knew | later | none; anything else -> ValueError)."""
    reason = (reason or "none").strip().lower()
    if reason not in REASONS:
        raise ValueError(f"reason must be one of {', '.join(REASONS)}")
    return _decide(sid, "dismissed", reason, {"who": who})


def later(sid: int, now: Optional[datetime] = None) -> dict:
    """Push expires_at by 4 hours, once (a second later -> SuggestionState); a row without an expiry too."""
    now = now or datetime.now(IST)
    with _lock:
        s = get(sid)
        if not s:
            raise SuggestionState("no such suggestion")
        if s["status"] != "open":
            raise SuggestionState(f"already {s['status']}")
        if s["later"]:
            raise SuggestionState("already pushed once")
        base = datetime.fromisoformat(s["expires_at"]) if s["expires_at"] else now
        exp = (max(base, now) + timedelta(hours=LATER_HOURS)).isoformat(timespec="seconds")
        with get_db() as conn:
            d = json.loads(conn.execute("SELECT data FROM ai_suggestions WHERE id = ?", (sid,)).fetchone()[0] or "{}")
            d["later"] = True
            conn.execute("UPDATE ai_suggestions SET expires_at = ?, data = ? WHERE id = ?", (exp, json.dumps(d), sid))
    return get(sid)


# --- Telegram's two-step accept ---------------------------------------------------------------------------------------------------

def _pending() -> dict:
    from backend.database.models import get_setting
    with get_db() as conn:
        raw = get_setting(conn, PENDING_KEY)
    return json.loads(raw) if raw else {}


def _set_pending(d: Optional[dict]) -> None:
    from backend.database.models import set_setting
    with get_db() as conn:
        set_setting(conn, PENDING_KEY, json.dumps(d or {}))


def telegram_accept(sid: int, config: AppConfig, now: Optional[datetime] = None) -> tuple[str, Optional[dict]]:
    """`accept N`: a `skip` (reversible) and the note-only kinds go straight through; `exit` / `hold_options` answer
    with the confirm text and wait for `yes N` within 10 minutes. ("step", row) where step is done | confirm."""
    now = now or datetime.now(IST)
    s = get(sid)
    if not s:
        raise SuggestionState("no such suggestion")
    if s["status"] != "open":
        raise SuggestionState(f"already {s['status']}")
    if s["kind"] in TWO_STEP:
        _set_pending({"id": sid, "until": (now + timedelta(minutes=YES_MINUTES)).isoformat(timespec="seconds")})
        return "confirm", s
    return "done", accept(sid, config, now, who="telegram")


def telegram_yes(sid: int, config: AppConfig, now: Optional[datetime] = None) -> dict:
    """`yes N`: runs the accept the pending `accept N` asked about; refused without one, for another id, or after
    10 minutes (SuggestionState)."""
    now = now or datetime.now(IST)
    p = _pending()
    if not p or int(p.get("id") or 0) != int(sid):
        raise SuggestionState(f"nothing is waiting for a yes on #{sid}: reply accept {sid} first")
    if now.isoformat(timespec="seconds") > p["until"]:
        _set_pending(None)
        raise SuggestionState(f"the {YES_MINUTES}-minute window for #{sid} is over: reply accept {sid} again")
    _set_pending(None)
    return accept(sid, config, now, who="telegram")


# --- outcomes -------------------------------------------------------------------------------------------------------------------------

def _nifty_range(day: str) -> Optional[float]:
    """NIFTY's day range (high − low, % of the previous close) on `day`; None without bars."""
    from backend.options import instruments as oi
    with get_db() as conn:
        rows = conn.execute("SELECT date, high, low, close FROM candles_daily WHERE instrument_token = ? AND date <= ? ORDER BY date DESC LIMIT 2",
                            (oi.INDEX_TOKEN, day)).fetchall()
    if len(rows) < 2 or rows[0]["date"] != day or not rows[1]["close"]:
        return None
    return round((rows[0]["high"] - rows[0]["low"]) / rows[1]["close"] * 100, 2)


def outcome_for(s: dict, today: Optional[str] = None) -> Optional[dict]:
    """{at, pct_move, helped, rs, note} five sessions after `day`, or None while the bars are not there yet.
    pct_move = close(day + 5 sessions) / close(day) − 1. helped: skip / exit / flag when the stock fell ≥ 1%;
    hold_options when NIFTY's range on the target day was > 1.5%; a kind no longer made (the futures roll_or_exit and
    backtest): no market outcome. rs = the move × the planned / held value, signed so that a positive rupee figure means the
    suggestion would have saved money."""
    kind, sym = s["kind"], s["symbol"]
    if kind not in KINDS:
        return {"at": now_ist(), "pct_move": None, "helped": None, "rs": None, "note": "a settings idea: no market outcome"}
    if kind == "hold_options":
        rng = _nifty_range(s["for_day"] or s["day"])
        if rng is None:
            return None
        return {"at": now_ist(), "pct_move": rng, "helped": rng > 1.5, "rs": None, "note": f"NIFTY range {rng:.2f}% on the day"}
    bars = _close_series(sym, s["day"], OUTCOME_SESSIONS + 1)
    if len(bars) < OUTCOME_SESSIONS + 1 or bars[0]["date"] != s["day"]:
        return None
    pct = round((bars[OUTCOME_SESSIONS]["close"] / bars[0]["close"] - 1) * 100, 2)
    value = s.get("value") or 0.0
    return {"at": now_ist(), "pct_move": pct, "helped": pct <= -1.0, "rs": round(-pct / 100 * value, 2) if value else None,
            "note": f"{sym} moved {pct:+.2f}% in {OUTCOME_SESSIONS} sessions"}


def score_outcomes(now: Optional[datetime] = None) -> int:
    """Fill `outcome` on every decided or expired suggestion old enough (5 sessions of bars after its day); how many."""
    now = now or datetime.now(IST)
    cutoff = (now.date() - timedelta(days=OUTCOME_SESSIONS)).isoformat()
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM ai_suggestions WHERE outcome IS NULL AND day <= ? ORDER BY id", (cutoff,)).fetchall()
    n = 0
    for r in rows:
        s = _row(r)
        try:
            o = outcome_for(s)
        except Exception:
            logger.exception("suggest: outcome for #%s", s["id"])
            continue
        if o is None:
            continue
        with get_db() as conn:
            conn.execute("UPDATE ai_suggestions SET outcome = ? WHERE id = ?", (json.dumps(o), s["id"]))
        n += 1
    return n


# --- reading -----------------------------------------------------------------------------------------------------------------------------

def list_suggestions(status: Optional[str] = None, limit: int = 50, kind: Optional[str] = None) -> list[dict]:
    """Newest first; open ones first when no status is asked."""
    where, args = [], []
    if status:
        where.append("status = ?")
        args.append(status)
    if kind:
        where.append("kind = ?")
        args.append(kind)
    sql = (" WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        rows = conn.execute(f"SELECT * FROM ai_suggestions{sql} ORDER BY status != 'open', id DESC LIMIT ?", (*args, max(1, min(int(limit), 500)))).fetchall()
    return [_row(r) for r in rows]


def open_suggestions() -> list[dict]:
    """The open suggestions of the kinds still made (an old futures roll_or_exit / backtest row stays as history)."""
    return [s for s in list_suggestions("open", 100) if s["kind"] in KINDS]


def open_flags(day: str) -> dict[str, str]:
    """{symbol: text} of the open `flag` suggestions for the session `day` (the 9:00 dry run prints them)."""
    with get_db() as conn:
        rows = conn.execute("SELECT symbol, title, why FROM ai_suggestions WHERE kind = 'flag' AND status = 'open' AND for_day = ? ORDER BY id",
                            (day,)).fetchall()
    return {r["symbol"]: f"{r['title']} — {r['why']}" for r in rows}


def summary(config: AppConfig, month: Optional[str] = None) -> dict:
    """GET /api/ai `suggestions`: {open, by_kind, scorecard_month, muted}."""
    rows = open_suggestions()
    by: dict[str, int] = {}
    for s in rows:
        by[s["kind"]] = by.get(s["kind"], 0) + 1
    return {"open": len(rows), "by_kind": by, "scorecard_month": scorecard(month)["totals"], "muted": sorted(muted_kinds(config), key=KINDS.index),
            "max_open": int(config.ai.max_suggestions_open)}


def scorecard(month: Optional[str] = None, config: Optional[AppConfig] = None) -> dict:
    """{month, kinds: {kind: {made, open, accepted, dismissed, expired, helped_when_accepted, would_have_helped, scored,
    muted}}, totals: {month, made, accepted, dismissed, expired, accepted_outcome_rs, dismissed_outcome_rs, best_kind}}.
    `month` YYYY-MM (the current month by default); helped_when_accepted counts accepted rows whose outcome helped,
    would_have_helped the dismissed / expired ones that would have."""
    month = month or now_ist()[:7]
    muted = muted_kinds(config) if config else set()
    with get_db() as conn:
        rows = [_row(r) for r in conn.execute("SELECT * FROM ai_suggestions WHERE day LIKE ? ORDER BY id", (month + "%",))]
    kinds = {k: {"made": 0, "open": 0, "accepted": 0, "dismissed": 0, "expired": 0, "helped_when_accepted": 0, "would_have_helped": 0,
                 "scored": 0, "muted": k in muted} for k in KINDS}
    tot = {"month": month, "made": 0, "accepted": 0, "dismissed": 0, "expired": 0, "accepted_outcome_rs": 0.0, "dismissed_outcome_rs": 0.0,
           "best_kind": None}
    for s in rows:
        if s["kind"] not in kinds:                 # a kind no longer made (futures roll_or_exit / backtest)
            continue
        k = kinds[s["kind"]]
        k["made"] += 1
        k[s["status"]] += 1
        tot["made"] += 1
        if s["status"] != "open":
            tot[s["status"]] += 1
        o = s.get("outcome") or {}
        if o.get("helped") is not None:
            k["scored"] += 1
            if s["status"] == "accepted":
                k["helped_when_accepted"] += int(bool(o["helped"]))
                tot["accepted_outcome_rs"] += o.get("rs") or 0.0
            else:
                k["would_have_helped"] += int(bool(o["helped"]))
                if s["status"] == "dismissed":
                    tot["dismissed_outcome_rs"] += o.get("rs") or 0.0
    rates = {k: (v["helped_when_accepted"] + v["would_have_helped"]) / v["scored"] for k, v in kinds.items() if v["scored"]}
    tot["best_kind"] = max(rates, key=lambda k: (rates[k], kinds[k]["scored"])) if rates else None
    tot["accepted_outcome_rs"], tot["dismissed_outcome_rs"] = round(tot["accepted_outcome_rs"], 2), round(tot["dismissed_outcome_rs"], 2)
    return {"month": month, "kinds": kinds, "totals": tot}


def attention_items(config: AppConfig) -> list[tuple[str, str]]:
    """The "ai" tab: every open high-confidence suggestion as a warn line."""
    return [("warn", f"🤖 #{s['id']} {LABEL.get(s['kind'], s['kind'])} {s['symbol']} · {s['title']}".replace("  ", " ")) for s in open_suggestions()
            if s["confidence"] == "high"]

