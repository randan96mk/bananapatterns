"""🤖 Ask the app (PR 3 · D): a question in plain words -> the day model picks ONE tool from a fixed list (JSON
{tool, args}) -> code runs that read-only function -> the model writes the answer (≤ ANSWER_MAX chars) from the tool's
result only. Any other question gets the refusal text. The tools read the app's own tables and books; none of them
writes, orders or switches anything, and there is no HTTP in any of them. Every ask is a row in `ai_asks` (never
deleted). Allowed at any time (kind "ask") but only on the day model; the 7B model is never used here.

  TOOLS                         the whitelist: name -> {fn, args: {name: type}, help}
  pick(config, question)        the first model call, parsed and checked against the whitelist
  run_tool(config, tool, args)  the function, its arguments coerced by type and defaulted by code
  answer(config, question, ...) the second model call, validated: no broker name, every figure in the tool's result
  ask(config, question, who)    the whole flow through backend.ai.queue (kind "ask", key ask:<row id>), waits
                                `timeout_s`; the `ai_asks` row either way
  list_asks(limit), stats(now), failed_today(now)   the readers (the page, GET /api/ai, attention)
"""

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

from backend.ai import client, notes
from backend.ai import queue as q
from backend.config import IST, AppConfig
from backend.database.models import get_db, now_ist
from backend.logging_config.logger import setup_logger

logger = setup_logger("ai")

PROMPT_V = "ask-v1"
ANSWER_MAX = 400
QUESTION_MAX = 300
RESULT_MAX = 3500                                # chars of the tool's result in the answer prompt
TIMEOUT_S = 60                                   # how long the page / Telegram wait for the answer
KEY_PREFIX = "ask:"                              # ai_jobs.key = ask:<row id>
GEN_PICK = {"temperature": 0.0, "num_predict": 120, "num_ctx": 2048}
GEN_ANSWER = {"temperature": 0.1, "num_predict": 260, "num_ctx": 4096, "top_p": 0.9}
REFUSAL = "I can only answer from the app's own data: plans, positions, trades, filings, notes, backtests."
REJECTED = "The answer quoted a figure that is not in the app's data, so it is not shown. Ask again, more narrowly."
BOOKS = ("real", "paper", "options")
DEFAULTS = {"day": None, "days": 7, "limit": 10, "status": "open", "pct": 2.0, "book": "real", "symbol": ""}


class AskInvalid(ValueError):
    """The model's answer is not usable (bad JSON, a broker name, a figure that is not in the tool's result)."""


# --- the tools (read-only; every one a plain function of typed arguments) -----------------------------------------------

def _today() -> str:
    return datetime.now(IST).date().isoformat()


def _since(days: int) -> str:
    return (date.fromisoformat(_today()) - timedelta(days=int(days))).isoformat()


def _at(day: str, hhmm: str = "09:00") -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=IST).replace(hour=h, minute=m)


def _pick_keys(d: dict, keys: tuple) -> dict:
    return {k: d.get(k) for k in keys if k in d}


def _pos(p: dict) -> dict:
    return _pick_keys(p, ("symbol", "side", "qty", "lots", "buy", "entry", "last", "stop", "net", "days", "in", "book", "account",
                          "sessions_to_expiry", "roll_due", "ai_note"))


def _order(o: dict) -> dict:
    out = _pick_keys(o, ("symbol", "action", "side", "qty", "lots", "close", "stop", "why", "results_date", "results_in", "status", "sent"))
    if o.get("skip"):
        out["skip"] = _pick_keys(o["skip"], ("by", "note", "results_date", "results_in"))
    return out


def _recent(rows: list, days: int, keys: tuple = ("out", "day", "exit_day", "closed_at")) -> list:
    """The rows whose end day (the first of `keys` present) is within `days`."""
    since = _since(days)
    out = []
    for r in rows:
        d = next((str(r[k])[:10] for k in keys if r.get(k)), None)
        if d is None or d >= since:
            out.append(r)
    return out


def real_morning(config: AppConfig, day: str) -> dict:
    """The Real morning dry run for `day`: the orders as they go (went) out, the skips, cash and free slots."""
    from backend.engine import real
    if not real.has_account():
        return {"started": False}
    m = real.morning(config, _at(day))
    return {"day": m["today"], "phase": m["phase"], "send_at": m["send_at"][11:16], "cash": m["cash"], "free": m["free"], "slots": m["slots"],
            "going_out": m["going_out"], "skipped": m["skipped"], "waiting": m["waiting"], "orders": [_order(o) for o in m["orders"]]}


def real_summary(config: AppConfig) -> dict:
    """The Real book: the account, the open positions, the orders for the next open, the filter, the trade tally."""
    from backend.engine import real
    s = real.summary(config)
    if not s.get("started"):
        return {"started": False, "enabled": s["enabled"]}
    return {**_pick_keys(s, ("started", "enabled", "killed", "as_of", "account", "waiting", "waiting_why", "won", "closed", "profit_factor", "drawdown")),
            "filter": (s.get("filter") or {}).get("state"), "positions": [_pos(p) for p in s.get("positions") or []],
            "orders": [_order(o) for o in s.get("orders") or []]}


def paper_summary(config: AppConfig) -> dict:
    """The Paper book per account: the account, the positions, the orders and skips, the trade tally."""
    from backend.engine import paper
    s = paper.summary(config)
    if not s.get("started"):
        return {"started": False}
    return {"started": True, "accounts": {k: {**_pick_keys(a, ("as_of", "account", "won", "closed", "profit_factor", "drawdown", "skipped")),
                                             "filter": (a.get("filter") or {}).get("state"), "positions": [_pos(p) for p in a.get("positions") or []],
                                             "orders": [_order(o) for o in a.get("orders") or []]} for k, a in s["accounts"].items()}}


def options_summary(config: AppConfig) -> dict:
    """The options book: today's state, the open spread, the guards (VIX, event day, daily loss), the chain line."""
    from backend.options import book
    s = book.summary(config)
    t, o, g, c = s.get("today") or {}, s.get("open"), s.get("guards") or {}, s.get("chain") or {}
    return {"started": s.get("started"), "equity": s.get("equity"), "net": s.get("net"), "mtm_today": s.get("mtm_today"),
            "today": _pick_keys(t, ("date", "status", "why", "gap_pct", "vix", "trades", "realised", "hold", "no_data")),
            "bias": _pick_keys(s.get("bias") or {}, ("state", "at", "score", "line")),
            "open": _pick_keys(o, ("bias", "expiry", "lots", "credit", "credit_rs", "value", "value_rs", "pnl", "stop_premium", "target_value", "opened_at", "near_stop",
                                   "legs")) if o else None,
            "guards": {k: g.get(k) for k in ("kill", "enabled", "hold", "event_day", "daily_loss", "vix", "window", "trades")},
            "chain": _pick_keys(c, ("at", "spot", "pcr", "walls", "line", "score", "vix"))}


def options_days(config: AppConfig, limit: int) -> dict:
    """The last `limit` options days: date, status, bias, trades, P&L, why."""
    from backend.options import book
    rows = [_pick_keys(d, ("date", "day", "status", "bias", "trades", "pnl", "realised", "why", "vix", "hold", "no_data")) for d in book.days(limit)]
    return {"days": rows}


def trades(config: AppConfig, book: str, days: int) -> dict:
    """The trades closed in the last `days` days in one book (real | paper | options)."""
    keys = ("symbol", "side", "qty", "lots", "in", "out", "buy", "sell", "entry", "exit", "stop", "days", "gross", "costs", "net", "why", "account",
            "day", "bias", "pnl", "credit", "exit_why", "opened_at", "closed_at")
    if book == "real":
        with get_db() as conn:
            rows = [t for t in (json.loads(r[0]) for r in conn.execute("SELECT payload FROM real_trades ORDER BY id DESC LIMIT 200"))
                    if not t.get("void")]                 # a trade marked as never filled is not counted
    elif book == "paper":
        from backend.engine import paper
        s = paper.summary(config)
        rows = [t for a in (s.get("accounts") or {}).values() for t in a.get("trades") or []] if s.get("started") else []
    else:
        from backend.options import book as obook
        rows = obook.trades(100)
    return {"book": book, "days": days, "trades": [_pick_keys(t, keys) for t in _recent(rows, days)][:40]}


def filings(config: AppConfig, symbol: str, days: int) -> dict:
    """The exchange filings and the bulk / block / stake deals of one stock in the last `days` days."""
    since = _since(days)
    sym = symbol.upper()
    with get_db() as conn:
        fs = [{"at": r["dt"], "category": r["category"], "kind": r["kind"], "text": notes._trim(r["text"], 240)} for r in conn.execute(
            "SELECT dt, category, kind, text FROM filings WHERE symbol = ? AND dt >= ? ORDER BY dt DESC LIMIT 20", (sym, since))]
        ds = [dict(r) for r in conn.execute("SELECT date, client, side, qty, price, source, pct, promoter FROM deals WHERE symbol = ? AND date >= ? "
                                            "ORDER BY date DESC LIMIT 20", (sym, since))]
    return {"symbol": sym, "days": days, "filings": fs, "deals": ds}


def notes_tool(config: AppConfig, symbol: str, days: int) -> dict:
    """The AI's notes (filing / deal / why) on one stock in the last `days` days, or the newest notes of every stock."""
    since = _since(days)
    rows = notes.list_notes(symbol or None, limit=100)["rows"]
    out = [{"day": n["day"], "kind": n["kind"], "symbol": n["symbol"], "severity": n["severity"], "title": n["title"], "text": n["text"],
            "direction": n.get("direction")} for n in rows if n["created"][:10] >= since and n["kind"] != "brief"]
    return {"symbol": symbol.upper() or None, "days": days, "notes": out[:20]}


def suggestions(config: AppConfig, status: str) -> dict:
    """The AI's suggestions with `status` (open | accepted | dismissed | expired); [] before the table exists."""
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ai_suggestions'").fetchone():
            return {"status": status, "suggestions": []}
        rows = [dict(r) for r in conn.execute("SELECT id, day, kind, symbol, book, title, why, confidence, expires_at, status, reason FROM ai_suggestions "
                                              "WHERE status = ? ORDER BY id DESC LIMIT 20", (status,))]
    return {"status": status, "suggestions": rows}


def plan(config: AppConfig, day: str) -> dict:
    """The evening plan made for the session of `day` (the newest plan before it), ranked."""
    from backend.alerts import plan as pl
    nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    as_of, items = pl.latest_plan(before=nxt)
    rows = [{"rank": n, **_pick_keys(i, ("symbol", "screen", "pivot", "zone_top", "stop", "close", "dist_to_pivot_pct", "rs", "tier", "results"))}
            for n, i in enumerate(items, start=1)]
    return {"day": day, "as_of": as_of, "items": rows}


def why_skipped(config: AppConfig, symbol: str, day: str) -> dict:
    """Why one stock did not go out on `day`: the Real skip (by whom, the note), the results guard, the waiting list,
    the plan's rank, the paper skips."""
    from backend.alerts import plan as pl
    from backend.engine import real
    from backend.ingest import events
    sym = symbol.upper()
    st = real._state() or {}
    skips = (st.get("skips") or {}).get(day) or {}
    waiting = [w for w in st.get("waiting_why") or [] if (w.get("symbol") if isinstance(w, dict) else str(w)) == sym or sym in str(w)]
    as_of, items = pl.latest_plan(before=(date.fromisoformat(day) + timedelta(days=1)).isoformat())
    rank = next((n for n, i in enumerate(items, start=1) if i["symbol"] == sym), None)
    with get_db() as conn:
        log = [dict(r) for r in conn.execute("SELECT action, status, reason, reply FROM real_orders WHERE day = ? AND symbol = ? ORDER BY id", (day, sym))]
    return {"symbol": sym, "day": day, "real_skip": skips.get(sym), "unskipped": sym in ((st.get("unskipped") or {}).get(day) or []),
            "waiting": waiting, "plan_rank": rank, "plan_as_of": as_of, "results": events.next_results(sym, day),
            "order_log": log, "in_real_orders": any(o["symbol"] == sym for o in st.get("orders") or [])}


def positions_near_stop(config: AppConfig, pct: float) -> dict:
    """Open positions in every book within `pct` percent of their stop: {book, symbol, last, stop, distance_pct}."""
    from backend.engine import paper, real
    out = []

    def add(book, rows):
        for p in rows:
            last, stop = p.get("last"), p.get("stop")
            if not last or not stop:
                continue
            sign = -1 if p.get("side") == "short" else 1
            dist = round(sign * (last - stop) / last * 100, 2)
            if dist <= pct:
                out.append({"book": book, "symbol": p["symbol"], "side": p.get("side") or "long", "last": last, "stop": stop, "distance_pct": dist})
    try:
        add("real", real.summary(config).get("positions") or [])
    except Exception:
        pass
    try:
        for k, a in (paper.summary(config).get("accounts") or {}).items():
            add(f"paper {k}", a.get("positions") or [])
    except Exception:
        pass
    return {"pct": pct, "positions": sorted(out, key=lambda x: x["distance_pct"])}


def levels(config: AppConfig, symbol: str) -> dict:
    """🤖 AI view: the candidate levels code found for a stock and the ones its latest view picked."""
    from backend.ai import view
    return view.levels_tool(config, symbol)


TOOLS: dict[str, dict] = {
    "real_morning": {"fn": real_morning, "args": {"day": str}, "help": "the Real morning plan of a day: orders, skips, cash, free slots"},
    "real_summary": {"fn": real_summary, "args": {}, "help": "the Real book: account, positions, next orders, trade tally"},
    "paper_summary": {"fn": paper_summary, "args": {}, "help": "the Paper book: account, positions, next orders"},
    "options_summary": {"fn": options_summary, "args": {}, "help": "the options book: today's state, the open spread, VIX, the chain line"},
    "options_days": {"fn": options_days, "args": {"limit": int}, "help": "the last N options days: status, bias, trades, P&L"},
    "trades": {"fn": trades, "args": {"book": str, "days": int}, "help": "trades closed in the last N days in a book: real, paper or options"},
    "filings": {"fn": filings, "args": {"symbol": str, "days": int}, "help": "a stock's exchange filings and deals of the last N days"},
    "notes": {"fn": notes_tool, "args": {"symbol": str, "days": int}, "help": "the AI's notes on a stock (filing, deal, why) of the last N days"},
    "suggestions": {"fn": suggestions, "args": {"status": str}, "help": "the AI's suggestions by status: open, accepted, dismissed, expired"},
    "plan": {"fn": plan, "args": {"day": str}, "help": "the evening plan for a day's session, ranked"},
    "why_skipped": {"fn": why_skipped, "args": {"symbol": str, "day": str}, "help": "why a stock did not go out on a day: skips, results guard, waiting list"},
    "positions_near_stop": {"fn": positions_near_stop, "args": {"pct": float}, "help": "open positions within N percent of their stop, every book"},
    "levels": {"fn": levels, "args": {"symbol": str}, "help": "a stock's support / resistance levels: the candidates and the AI view's picks"},
}


def tool_list_text() -> str:
    """'real_morning(day: str) — …' one per line, for the pick prompt and the page."""
    return "\n".join(f"{name}({', '.join(f'{a}: {t.__name__}' for a, t in t_['args'].items())}) — {t_['help']}" for name, t_ in TOOLS.items())


# --- arguments by code ------------------------------------------------------------------------------------------------------------

def _iso_day(v: Any) -> str:
    """An ISO date, today when the value is not one."""
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except (TypeError, ValueError):
        return _today()


def coerce_args(tool: str, args: Any) -> dict:
    """The tool's arguments by its typed spec: unknown ones dropped, missing ones defaulted, strings cleaned,
    numbers bounded (days 1–90, limit 1–30, pct 0.1–20), book one of BOOKS, day an ISO date."""
    spec = TOOLS[tool]["args"]
    args = args if isinstance(args, dict) else {}
    out = {}
    for name, typ in spec.items():
        v = args.get(name, DEFAULTS.get(name))
        if name == "day":
            v = _iso_day(v)
        elif typ is int:
            try:
                v = int(float(v))
            except (TypeError, ValueError):
                v = DEFAULTS[name]
            v = max(1, min(v, 90 if name == "days" else 30))
        elif typ is float:
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = DEFAULTS[name]
            v = max(0.1, min(v, 20.0))
        else:
            v = re.sub(r"[^A-Za-z0-9 &._-]", "", str(v or DEFAULTS.get(name) or "")).strip()[:40]
            if name == "book":
                v = v.lower() if v.lower() in BOOKS else DEFAULTS["book"]
            if name == "status":
                v = v.lower() if v.lower() in ("open", "accepted", "dismissed", "expired") else DEFAULTS["status"]
        out[name] = v
    return out


# --- the two model calls --------------------------------------------------------------------------------------------------------------

PICK_SYSTEM = f"""You route a question about a private investor's trading app to ONE of these read-only tools. Reply with
JSON only: {{"tool": "<name>", "args": {{...}}}}. Use the exact tool names and argument names below; dates are ISO
(YYYY-MM-DD); when the question gives no value, leave the argument out. If no tool can answer the question, reply
{{"tool": "none"}}. Never invent a tool.
Tools:
{tool_list_text()}"""

ANSWER_SYSTEM = f"""You answer a private investor's question about their own trading app from the tool result below and
nothing else. You read and explain; you never advise, decide or predict. Reply with JSON only: {{"answer": "..."}}, at most
{ANSWER_MAX} characters of plain text. Quote only numbers that appear in the tool result, exactly as written; never
compute, convert or estimate a number. If the result does not answer the question, say so in one sentence. Never name
a broker or a trading platform. No markdown."""

RETRY_SUFFIX = "\nYour previous answer was not valid: {why}. Reply with the JSON object only, quoting only figures from the tool result."


def _generate(config: AppConfig, system: str, user: str, options: dict) -> dict:
    c = config.ai
    return client.generate(c.day_model, user, system=system, json_mode=True, timeout=float(c.timeout_s), keep_alive=c.keep_alive, options=options)


def parse_pick(raw: str) -> tuple[Optional[str], dict]:
    """(tool, args) from the model's JSON; (None, {}) when it named no tool or one outside the whitelist."""
    try:
        d = json.loads(raw)
    except ValueError:
        return None, {}
    if not isinstance(d, dict):
        return None, {}
    name = str(d.get("tool") or d.get("name") or "").strip().lower().replace("-", "_")
    if name not in TOOLS:
        return None, {}
    return name, coerce_args(name, d.get("args") or d.get("arguments") or {})


def pick(config: AppConfig, question: str) -> tuple[Optional[str], dict, dict]:
    """The first call: (tool, args, generate result)."""
    r = _generate(config, PICK_SYSTEM, f"Today is {_today()}.\nQuestion: {question}", GEN_PICK)
    tool, args = parse_pick(r["text"])
    return tool, args, r


def run_tool(config: AppConfig, tool: str, args: dict) -> Any:
    """The whitelisted function with its coerced arguments (no model involved)."""
    return TOOLS[tool]["fn"](config, **coerce_args(tool, args))


def result_text(result: Any) -> str:
    """The tool's result as compact JSON, cut to RESULT_MAX chars (the source text of the numbers check)."""
    s = json.dumps(result, default=str, separators=(",", ":"))
    return s if len(s) <= RESULT_MAX else s[:RESULT_MAX - 1] + "…"


def validate_answer(raw: str, source_text: str, question: str = "") -> str:
    """The answer checked by code: a JSON object with "answer" (a bare string is taken too), clipped to ANSWER_MAX, no
    broker name, and no figure that is not in the tool's result or the question. Raises AskInvalid."""
    try:
        d = json.loads(raw)
    except ValueError as exc:
        raise AskInvalid(f"not JSON: {raw[:80]!r}") from exc
    text = d.get("answer") if isinstance(d, dict) else d
    text = notes._clip(text, ANSWER_MAX)
    if not text:
        raise AskInvalid("answer missing")
    w = notes._blocked_word(text)
    if w:
        raise AskInvalid(f"names a broker ({w[:3]}…)")
    allowed = notes._digit_tokens(source_text) | notes._digit_tokens(question)
    _, ok = notes._strip_numbers(text, allowed)
    if not ok:
        raise AskInvalid("a figure is not in the tool result")
    return text


def answer(config: AppConfig, question: str, tool: str, args: dict, result: Any) -> tuple[str, dict]:
    """The second call: (answer, generate result); one retry naming the fault, then AskInvalid."""
    src = result_text(result)
    user = f"Question: {question}\nTool: {tool}({json.dumps(args)})\nTool result (JSON):\n{src}"
    r = _generate(config, ANSWER_SYSTEM, user, GEN_ANSWER)
    try:
        return validate_answer(r["text"], src, question), r
    except AskInvalid as exc:
        logger.info("AI ask answer invalid (%s); one retry", exc)
        r2 = _generate(config, ANSWER_SYSTEM + RETRY_SUFFIX.format(why=str(exc)[:120]), user, GEN_ANSWER)
        r2["tokens"] += r["tokens"]
        r2["ms"] += r["ms"]
        return validate_answer(r2["text"], src, question), r2


# --- the rows -----------------------------------------------------------------------------------------------------------------------------

def _insert(who: str, question: str, model: str) -> int:
    with get_db() as conn:
        cur = conn.execute("INSERT INTO ai_asks (created, who, question, model) VALUES (?, ?, ?, ?)", (now_ist(), who, question, model))
    return cur.lastrowid


def _update(row_id: int, **cols) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)
    with get_db() as conn:
        conn.execute(f"UPDATE ai_asks SET {sets} WHERE id = ?", (*cols.values(), row_id))


def get_ask(row_id: int) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM ai_asks WHERE id = ?", (row_id,)).fetchone()
    return _row(r) if r else None


def _row(r) -> dict:
    d = dict(r)
    d["ok"] = bool(d.get("ok"))
    try:
        d["args"] = json.loads(d["args"]) if d.get("args") else {}
    except ValueError:
        d["args"] = {}
    d["read"] = f"{d['tool']}({', '.join(f'{k}={v}' for k, v in d['args'].items())})" if d.get("tool") else None
    return d


# --- the flow ------------------------------------------------------------------------------------------------------------------------------

def work(config: AppConfig, row_id: int, question: str) -> dict:
    """Pick -> run -> answer, writing the row as it goes: {tokens, note}. An unusable second answer leaves the row
    with ok 0 and the REJECTED text (the job still ends done: the user got a reply). Exceptions fail the job (the row
    keeps the error)."""
    try:
        tool, args, r1 = pick(config, question)
        if not tool:
            _update(row_id, tool=None, args=None, answer=REFUSAL, ms=r1["ms"], ok=1)
            return {"tokens": r1["tokens"], "note": "refused: no tool"}
        _update(row_id, tool=tool, args=json.dumps(args))
        result = run_tool(config, tool, args)
        try:
            text, r2 = answer(config, question, tool, args, result)
        except AskInvalid as exc:
            _update(row_id, answer=REJECTED, ms=r1["ms"], ok=0, error=str(exc)[:200])
            return {"tokens": r1["tokens"], "note": f"{tool} · rejected"}
        _update(row_id, answer=text, ms=r1["ms"] + r2["ms"], ok=1)
        return {"tokens": r1["tokens"] + r2["tokens"], "note": f"{tool} · answered"}
    except Exception as exc:
        _update(row_id, ok=0, error=f"{exc.__class__.__name__}: {str(exc)[:200]}", answer=None)
        raise


def ask(config: AppConfig, question: str, who: str = "page", timeout_s: float = TIMEOUT_S) -> dict:
    """The whole flow for one question through the queue (kind "ask"); waits `timeout_s` for the row. The row either
    way: {id, question, tool, args, answer, ms, ok, error, status, read}. Raises ValueError on an empty question."""
    question = " ".join((question or "").split())[:QUESTION_MAX]
    if not question:
        raise ValueError("ask what? the question is empty")
    row_id = _insert(who, question, config.ai.day_model)
    try:
        job = q.enqueue(config, "ask", f"{KEY_PREFIX}{row_id}", lambda: work(config, row_id, question), model=config.ai.day_model,
                        note=f"ask · {question[:40]}")
    except q.QueueFull as exc:
        _update(row_id, ok=0, error=str(exc), answer=f"Not now: {exc}.")
        return {**get_ask(row_id), "status": "failed"}
    jr = q.wait(job["id"], timeout_s) or {}
    row = get_ask(row_id)
    status = jr.get("status")
    if status == "skipped":
        _update(row_id, ok=0, error=jr.get("error"), answer=f"AI not available: {jr.get('error')}.")
    elif status in ("queued", "running", None) and not row.get("answer"):
        _update(row_id, ok=0, error=f"no answer within {timeout_s:g} s", answer=f"No answer within {timeout_s:g} s; the page keeps it if it arrives.")
    elif status == "failed" and not row.get("answer"):
        _update(row_id, answer=f"The model could not answer: {jr.get('error') or 'error'}.")
    return {**get_ask(row_id), "status": status or "queued"}


# --- reading -------------------------------------------------------------------------------------------------------------------------------

def list_asks(limit: int = 10) -> list[dict]:
    with get_db() as conn:
        return [_row(r) for r in conn.execute("SELECT * FROM ai_asks ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 200)),))]


def stats(now: Optional[datetime] = None) -> dict:
    """GET /api/ai `asks`: {today, last}: how many asks today and the newest row."""
    now = now or datetime.now(IST)
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM ai_asks WHERE created LIKE ?", (now.date().isoformat() + "%",)).fetchone()[0]
    last = list_asks(1)
    return {"today": n, "last": last[0] if last else None}


def failed_today(now: Optional[datetime] = None) -> list[dict]:
    """Today's asks that ended without an answer (ok 0 with an error): the attention item."""
    now = now or datetime.now(IST)
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM ai_asks WHERE created LIKE ? AND ok = 0 AND error IS NOT NULL ORDER BY id DESC LIMIT 3",
                            (now.date().isoformat() + "%",)).fetchall()
    return [_row(r) for r in rows]


TOOL_NAMES: tuple = tuple(TOOLS)          # the whitelist as the tests and the page read it
