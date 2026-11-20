"""🤖 Trade "why" notes (PR 3 · B): one line, "likely cause: …", under every trade closed today (Real, Paper,
Options) and under every held name that gapped more than 1% or hit a stop. Code assembles the evidence (the trade
row: entry, exit, how it ended, P&L; the stock's day move; the industry's average move when the universe table knows
the industry; NIFTY's move; the filings and deals of the last 3 days; for options the bias, the score and the exit
time), the model writes the one line (≤ 180 chars, numbers only from the evidence, no broker name), code keeps it as
an `ai_notes` row of kind "why" (severity info) and sends ONE evening message "🧾 Today's exits, explained" when at
least one trade closed (key {day}|ai-why). The model explains; it never grades a rule or proposes a change.

A stock closed the same day in several books (Real and Paper E+ on the same stop) is ONE item: one evidence block
(each book's trade line, then the stock's lines once), one model call, one row with source_key multi:<SYM>:<day> and
the books' trade keys in data.keys, so `attach()` finds the line for every trade row; the evening message lists the
stock once with the books' nets on one line.

  closed_today(day)              the normalised trade rows of the four books closed on `day`
  merge(items, day)              one item per stock closed in several books (options spreads stay apart)
  moved_today(day, config)       held names that gapped > 1% at the open or whose stop was hit today
  evidence(item, day, config)    the evidence lines for one item (trade or held name)
  run_night(config, now)         one job "why:<day>" through the queue (kind "night"): one model call per item
  attach(rows, book)             `ai_why` on trade rows (the trade tables' "🤖 why" line); never raises
"""

import json
from datetime import date, datetime, timedelta
from typing import Optional

from backend.ai import client, notes
from backend.ai import queue as q
from backend.config import IST, AppConfig
from backend.database.models import find_instrument, get_db, now_ist
from backend.logging_config.logger import setup_logger

logger = setup_logger("ai")

PROMPT_V = "why-v1"
LINE_MAX = 180
GAP_PCT = 1.0
FILING_DAYS = 3
KEY_PREFIX = "why:"
BOOKS = ("real", "paper", "options")
BOOK_ICON = {"real": "💰", "paper": "📝", "options": "🧩"}
GEN_OPTIONS = {"temperature": 0.1, "num_predict": 120, "num_ctx": 1536, "top_p": 0.9}

SYSTEM_PROMPT = f"""You explain to a private investor why one of his trades ended the way it did, from the evidence his
own software collected: the trade row, the stock's move, the sector's and the index's move, the filings and deals of the
last days. You read and explain; you never advise, grade the rule or propose a change. Write ONE line that starts with
"likely cause:" and is at most {LINE_MAX} characters. Quote only the numbers written in the evidence, exactly as given;
never compute, convert or estimate a number. Never name a broker or a trading platform. Reply with JSON only:
{{"why": "likely cause: ..."}}. No markdown."""

RETRY_SUFFIX = "\nYour previous answer was not valid: {why}. Reply with the JSON object only."


class WhyInvalid(ValueError):
    """The model's line is not usable."""


# --- what closed, what moved -----------------------------------------------------------------------------------------

def _rows(table: str, where: str = "", args: tuple = ()) -> list[dict]:
    with get_db() as conn:
        return [{**json.loads(r["payload"]), "id": r["id"]} for r in conn.execute(f"SELECT id, payload FROM {table}{where} ORDER BY id", args)]


def closed_today(day: str) -> list[dict]:
    """The trades closed on `day` in every book, normalised: {book, key, symbol, side, qty, entry, exit, net, why,
    label, in, out, days, stop, extra}."""
    out = []
    for t in _rows("real_trades"):
        if t.get("out") == day and not t.get("void"):
            out.append(_cash(t, "real"))
    for t in _rows("paper_trades"):
        if t.get("out") == day:
            out.append(_cash(t, "paper"))
    for t in _rows("options_trades", " WHERE day = ?", (day,)):
        sold = t.get("sold") or {}
        out.append({"book": "options", "key": f"options:{t['id']}", "symbol": "NIFTY", "side": t.get("bias"), "qty": t.get("qty"),
                    "entry": t.get("credit"), "exit": t.get("cost"), "net": t.get("net"), "why": t.get("why"), "label": t.get("label") or t.get("why"),
                    "in": t.get("in"), "out": t.get("out"), "days": 0, "stop": None,
                    "extra": {"bias": t.get("bias"), "score": t.get("score"), "strike": sold.get("strike"), "type": sold.get("type"),
                              "spot_in": t.get("spot_in"), "spot_out": t.get("spot_out"), "chain_line": t.get("chain_line"), "be_t": t.get("be_t")}})
    return out


def _cash(t: dict, book: str) -> dict:
    return {"book": book, "key": f"{book}:{t['id']}", "symbol": t["symbol"], "side": "long", "qty": t.get("qty"), "entry": t.get("buy"),
            "exit": t.get("sell"), "net": t.get("net"), "why": t.get("why"), "label": t.get("why"), "in": t.get("in"), "out": t.get("out"),
            "days": t.get("days"), "stop": t.get("stop"), "extra": {"best": t.get("best")}}


def merge(items: list[dict], day: str) -> list[dict]:
    """One item per stock closed in several books on `day` (order kept): {book (the first book's), key
    multi:<SYM>:<day>, symbol, label, members: the books' trade items, ...the first item's fields}. A stock closed in one
    book and the options spreads (all NIFTY) pass through unchanged."""
    groups: dict[str, list] = {}
    for t in items:
        if t["book"] != "options":
            groups.setdefault(t["symbol"], []).append(t)
    out, seen = [], set()
    for t in items:
        g = groups.get(t["symbol"]) if t["book"] != "options" else None
        if not g or len(g) == 1:
            out.append(t)
        elif t["symbol"] not in seen:
            seen.add(t["symbol"])
            label = " / ".join(dict.fromkeys(str(m["label"]) for m in g if m.get("label")))
            out.append({**g[0], "key": f"multi:{t['symbol']}:{day}", "label": label or g[0]["label"], "members": g})
    return out


def _bars(symbol: str, day: str, n: int = 2) -> list[dict]:
    """The last `n` daily bars up to `day` (ascending); [] without a token."""
    with get_db() as conn:
        inst = find_instrument(conn, symbol, "NSE")
        if not inst:
            return []
        rows = conn.execute("SELECT date, open, high, low, close FROM candles_daily WHERE instrument_token = ? AND date <= ? ORDER BY date DESC LIMIT ?",
                            (inst["instrument_token"], day, n)).fetchall()
    return [dict(r) for r in reversed(rows)]


def day_move(symbol: str, day: str) -> Optional[dict]:
    """{close_pct, gap_pct, open, close, prev} of `day` vs the previous session; None without two bars on `day`."""
    b = _bars(symbol, day)
    if len(b) < 2 or b[-1]["date"] != day or not b[-2]["close"]:
        return None
    prev = b[-2]["close"]
    return {"close_pct": round((b[-1]["close"] / prev - 1) * 100, 2), "gap_pct": round((b[-1]["open"] / prev - 1) * 100, 2),
            "open": b[-1]["open"], "close": b[-1]["close"], "prev": prev}


def _stopped_today(day: str) -> set:
    """Symbols whose stop sent an EXIT today (the real order log)."""
    with get_db() as conn:
        return {r[0] for r in conn.execute("SELECT symbol FROM real_orders WHERE day = ? AND status = 'sent' AND reason LIKE 'stop%'", (day,))}


def moved_today(day: str, config: AppConfig) -> list[dict]:
    """Held names (every book, via the notes' scope) that gapped more than 1% at the open or hit a stop today, as
    items {book: held, key, symbol, move, stopped}; a name that also closed today is left to its trade row."""
    held = [s for s, v in notes.scope().items() if v["book"] == "held"]
    stopped = _stopped_today(day)
    out = []
    for s in sorted(held):
        m = day_move(s, day)
        hit = s in stopped
        if (m and abs(m["gap_pct"]) > GAP_PCT) or hit:
            out.append({"book": "held", "key": f"held:{s}:{day}", "symbol": s, "side": None, "qty": None, "entry": None, "exit": None, "net": None,
                        "why": "stop hit" if hit else f"gap {m['gap_pct']:+.1f}%", "label": "stop hit" if hit else f"gapped {m['gap_pct']:+.1f}%",
                        "in": None, "out": day, "days": None, "stop": None, "extra": {"move": m, "stopped": hit}})
    return out


# --- the evidence ------------------------------------------------------------------------------------------------------

def _industry_move(symbol: str, day: str) -> Optional[dict]:
    """{industry, pct, n}: the average close move of the universe's stocks in the same industry on `day` (needs the
    `industry` column of universe_snapshots and bars for them); None when unknown."""
    with get_db() as conn:
        r = conn.execute("SELECT industry FROM universe_snapshots WHERE symbol = ? AND industry IS NOT NULL AND industry != '' "
                         "ORDER BY snapshot_date DESC LIMIT 1", (symbol,)).fetchone()
        if not r:
            return None
        ind = r[0]
        snap = conn.execute("SELECT MAX(snapshot_date) FROM universe_snapshots").fetchone()[0]
        peers = [x[0] for x in conn.execute("SELECT DISTINCT symbol FROM universe_snapshots WHERE snapshot_date = ? AND industry = ? AND symbol != ?",
                                            (snap, ind, symbol))]
    moves = [m["close_pct"] for m in (day_move(p, day) for p in peers[:60]) if m]
    if not moves:
        return None
    return {"industry": ind, "pct": round(sum(moves) / len(moves), 2), "n": len(moves)}


def _nifty_move(day: str) -> Optional[float]:
    from backend.options import instruments as oi
    with get_db() as conn:
        rows = conn.execute("SELECT date, close FROM candles_daily WHERE instrument_token = ? AND date <= ? ORDER BY date DESC LIMIT 2",
                            (oi.INDEX_TOKEN, day)).fetchall()
    if len(rows) < 2 or rows[0]["date"] != day or not rows[1]["close"]:
        return None
    return round((rows[0]["close"] / rows[1]["close"] - 1) * 100, 2)


def _recent_filings(symbol: str, day: str) -> list[dict]:
    """Filings and deals of the last 3 days on the stock, as evidence lines (the AI's note when one exists)."""
    since = (date.fromisoformat(day) - timedelta(days=FILING_DAYS)).isoformat()
    out = []
    with get_db() as conn:
        for r in conn.execute("SELECT seq_id, dt, category, text, url FROM filings WHERE symbol = ? AND dt >= ? ORDER BY dt DESC LIMIT 4", (symbol, since)):
            n = notes.note_for("filing", r["seq_id"])
            text = f"filing {r['dt'][:10]} · {r['category']}" + (f": {n['title']}" if n else f": {' '.join((r['text'] or '').split())[:100]}")
            out.append({"icon": "📰", "text": text, "url": r["url"] or None})
        for r in conn.execute("SELECT date, client, side, qty, price, source, pct, promoter FROM deals WHERE symbol = ? AND date >= ? ORDER BY date DESC LIMIT 4",
                              (symbol, since)):
            who = "promoter " if r["promoter"] else ""
            what = f"{r['pct']:.2f}% stake" if r["source"] == "sast" and r["pct"] is not None else f"{(r['qty'] or 0):,.0f} shares at {(r['price'] or 0):,.2f}"
            out.append({"icon": "🤝", "text": f"deal {r['date']} · {who}{r['client']} {str(r['side']).lower()} {what} ({r['source']})"})
    return out


def _trade_line(t: dict) -> str:
    if t["book"] == "options":
        x = t["extra"]
        return (f"options spread: {x.get('bias')} bias, score {x.get('score')}, sold {x.get('strike'):,.0f} {x.get('type')}; credit {t['entry']}, "
                f"bought back at {t['exit']}; ended: {t['label']} at {t['out']}; net {t['net']:+,.0f}"
                + (f"; spot {x['spot_in']:,.0f} -> {x['spot_out']:,.0f}" if x.get("spot_in") and x.get("spot_out") else "")
                + (f"; breakeven stop active since {x['be_t']}" if x.get("be_t") else ""))
    if t["book"] == "held":
        return f"held name; today: {t['label']}"
    side = f" {t['side']}" if t.get("side") and t["side"] != "long" else ""
    return (f"{t['book']} trade{side}: {t['qty']} from {t['entry']:,.2f} ({t['in']}) to {t['exit']:,.2f} ({t['out']}), stop {t['stop']:,.2f}, "
            f"{t['days']} sessions; ended: {t['why']}; net {t['net']:+,.0f}")


def evidence(t: dict, day: str, config: Optional[AppConfig] = None) -> list[dict]:
    """The evidence lines for one item: the trade row (one per book for a merged stock), the stock's day move (gap and
    close), the industry's average move, NIFTY's move, the filings and deals of the last 3 days."""
    ev = [{"icon": BOOK_ICON.get(m["book"], "📦"), "text": _trade_line(m)} for m in t.get("members") or [t]]
    sym = t["symbol"]
    if t["book"] != "options":
        m = t["extra"].get("move") if t["book"] == "held" else day_move(sym, day)
        if m:
            ev.append({"icon": "📈", "text": f"{sym} today: opened {m['gap_pct']:+.2f}% vs the previous close, closed {m['close_pct']:+.2f}%"})
        ind = _industry_move(sym, day)
        if ind:
            ev.append({"icon": "🏭", "text": f"{ind['industry']} ({ind['n']} stocks) averaged {ind['pct']:+.2f}% today"})
    nifty = _nifty_move(day)
    if nifty is not None:
        ev.append({"icon": "🇮🇳", "text": f"NIFTY closed {nifty:+.2f}% today"})
    if t["book"] != "options":
        ev += _recent_filings(sym, day)
    return ev


# --- the model's line --------------------------------------------------------------------------------------------------------

def prompt(t: dict, ev: list[dict]) -> str:
    books = " + ".join(m["book"] for m in t.get("members") or [t])
    lines = [f"Trade: {t['symbol']} · {books} · {t['label']}", "Evidence:"] + [f"- {e['text']}" for e in ev]
    lines.append('Write the one line "likely cause: ...". Quote only the numbers above.')
    return "\n".join(lines)


def validate(raw: str, evidence_text: str) -> dict:
    """{line, numbers_checked}: JSON with `why`, starts with "likely cause:" (added when missing), ≤ 180 chars, no
    broker name (invalid), no figure that is not in the evidence (stripped). Raises WhyInvalid."""
    try:
        d = json.loads(raw)
    except ValueError as exc:
        raise WhyInvalid(f"not JSON: {raw[:80]!r}") from exc
    if not isinstance(d, dict):
        raise WhyInvalid("not a JSON object")
    line = " ".join(str(d.get("why") or d.get("line") or "").split())
    if not line:
        raise WhyInvalid("why missing")
    if not line.lower().startswith("likely cause"):
        line = "likely cause: " + line[0].lower() + line[1:]
    w = notes._blocked_word(line)
    if w:
        raise WhyInvalid(f"names a broker ({w[:3]}…)")
    line, ok = notes._strip_numbers(notes._clip(line, LINE_MAX), notes.typed_tokens(evidence_text))
    return {"line": line, "numbers_checked": ok}


def _generate(config: AppConfig, system: str, user: str) -> dict:
    c = config.ai
    return client.generate(c.night_model, user, system=system, json_mode=True, timeout=float(c.timeout_s), keep_alive=c.keep_alive,
                           options=GEN_OPTIONS)


def ask(config: AppConfig, t: dict, ev: list[dict]) -> tuple[dict, dict]:
    """(validated line, generate result); one retry, then WhyInvalid."""
    user, text = prompt(t, ev), " ".join(e["text"] for e in ev)
    r = _generate(config, SYSTEM_PROMPT, user)
    try:
        return validate(r["text"], text), r
    except WhyInvalid as exc:
        r2 = _generate(config, SYSTEM_PROMPT + RETRY_SUFFIX.format(why=str(exc)[:120]), user)
        r2["tokens"], r2["ms"] = r2["tokens"] + r["tokens"], r2["ms"] + r["ms"]
        return validate(r2["text"], text), r2


# --- rows ------------------------------------------------------------------------------------------------------------------------

def _plain(t: dict) -> dict:
    """The trade row as stored and shown (no `extra`, no `members`)."""
    return {k: v for k, v in t.items() if k not in ("extra", "members")}


def save(t: dict, line: dict, ev: list[dict], day: str, model: str, ms: int) -> Optional[int]:
    """One `ai_notes` row of kind "why" (None when the item has its line already: the UNIQUE guard on source_key). A
    merged stock keeps its books' trade rows in data.trades and their keys in data.keys (what `attach()` reads)."""
    data = {"trade": _plain(t), "extra": t["extra"], "evidence": ev, "numbers_checked": line["numbers_checked"],
            "source": {"dt": day, "category": "why", "url": next((e["url"] for e in ev if e.get("url")), "")}, "alerted": False}
    if t.get("members"):
        data.update(trades=[_plain(m) for m in t["members"]], keys=[m["key"] for m in t["members"]], books=[m["book"] for m in t["members"]])
    with get_db() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO ai_notes (created, day, kind, symbol, book, severity, title, text, data, model, prompt_version, ms, "
                           "source_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (now_ist(), day, "why", t["symbol"], t["book"], "info", f"{t['symbol']} · {t['label']}", line["line"], json.dumps(data),
                            model, PROMPT_V, ms, t["key"]))
    return cur.lastrowid if cur.rowcount else None


def _done_keys(day: str) -> set:
    """Every item key with a line already: the rows' source keys and the trade keys a merged row covers."""
    with get_db() as conn:
        done = {r[0] for r in conn.execute("SELECT source_key FROM ai_notes WHERE kind = 'why' AND prompt_version = ?", (PROMPT_V,))}
        done |= {r[0] for r in conn.execute("SELECT j.value FROM ai_notes n, json_each(n.data, '$.keys') j WHERE n.kind = 'why' "
                                            "AND n.source_key LIKE 'multi:%' AND n.prompt_version = ?", (PROMPT_V,))}
    return done


def items_for(day: str, config: AppConfig) -> list[dict]:
    """Tonight's items: the trades closed today (a stock closed in several books merged into one), then the held names
    that moved; the ones noted already left out."""
    done = _done_keys(day)
    every = closed_today(day)
    closed = [t for t in every if t["key"] not in done]
    closed_syms = {t["symbol"] for t in every}
    moved = [m for m in moved_today(day, config) if m["symbol"] not in closed_syms]
    return [t for t in merge(closed, day) + moved if t["key"] not in done]


def run_night(config: AppConfig, now: Optional[datetime] = None, notifier=None) -> dict:
    """The job: one model call per item, one row each, then the evening message once when ≥ 1 trade closed today.
    An item whose line is unusable twice is skipped (the next night's run does not retry a closed trade: its day is
    over). Returns the queue's {tokens, note} (or {skip} with nothing to explain)."""
    from backend.alerts import messages as msg
    from backend.alerts.dispatch import Notifier
    now = now or datetime.now(IST)
    day = _trade_day(now, config)
    items = items_for(day, config)
    if not items:
        return {"skip": f"nothing closed or moved on {day}"}
    tokens, written, failed = 0, [], 0
    for t in items:
        ev = evidence(t, day, config)
        try:
            line, r = ask(config, t, ev)
        except (WhyInvalid, client.AIError) as exc:
            failed += 1
            logger.warning("why: no usable line for %s (%s)", t["key"], exc)
            continue
        tokens += r["tokens"]
        nid = save(t, line, ev, day, r["model"], r["ms"])
        if nid:
            row = {**notes.get_note(nid), "trade": _plain(t)}
            if t.get("members"):
                row["trades"] = [_plain(m) for m in t["members"]]
            written.append(row)
    closed = [n for n in written if n["book"] in BOOKS]
    if closed and config.ai.why:
        try:
            (notifier or Notifier(config.alerts, ai=config.ai)).send(f"{day}|ai-why", "ai", msg.ai_why_evening(closed, day))
            for n in closed:
                notes.mark_alerted(n["id"])
        except Exception:
            logger.exception("why: the evening message failed")
    return {"tokens": tokens, "note": f"{len(written)} why line(s) · {failed} failed"}


def _trade_day(now: datetime, config: AppConfig) -> str:
    """The session the lines are about: the night's day (the evening's date)."""
    return notes.night_day(now, config)


def running() -> bool:
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM ai_jobs WHERE kind = 'night' AND key LIKE ? AND status IN ('queued', 'running') LIMIT 1",
                            (KEY_PREFIX + "%",)).fetchone() is not None


def enqueue_tonight(config: AppConfig, now: Optional[datetime] = None, notifier=None) -> dict:
    """One night job "why:<day>" through the queue (after the notes: FIFO); {id, dedup, key}. Off (`ai.why` False):
    {status: off}."""
    now = now or datetime.now(IST)
    if not config.ai.why:
        return {"status": "off"}
    key = f"{KEY_PREFIX}{_trade_day(now, config)}"
    job = q.enqueue(config, "night", key, lambda: run_night(config, datetime.now(IST), notifier), model=config.ai.night_model,
                    note="why lines for today's exits")
    return {**job, "key": key}


# --- reading ---------------------------------------------------------------------------------------------------------------------

def for_keys(keys) -> dict[str, dict]:
    """{trade key: {id, text, at}} of the why lines for trade keys such as "real:12": the row keyed by the trade, or
    the merged row (multi:<SYM>:<day>) whose data.keys lists it."""
    keys = sorted(set(keys))
    if not keys:
        return {}
    marks = ",".join("?" * len(keys))
    with get_db() as conn:
        rows = conn.execute(f"SELECT id, source_key, text, created FROM ai_notes WHERE kind = 'why' AND source_key IN ({marks}) "
                            "ORDER BY id", keys).fetchall()
        merged = conn.execute(f"SELECT n.id, j.value AS source_key, n.text, n.created FROM ai_notes n, json_each(n.data, '$.keys') j "
                              f"WHERE n.kind = 'why' AND n.source_key LIKE 'multi:%' AND j.value IN ({marks}) ORDER BY n.id", keys).fetchall()
    return {r["source_key"]: {"id": r["id"], "text": r["text"], "at": r["created"]} for r in [*merged, *rows]}


def attach(rows: list, book: str) -> list:
    """`ai_why: {id, text, at} | None` on every trade row (dicts with an "id") of `book`. Never raises."""
    for r in rows:
        if isinstance(r, dict):
            r.setdefault("ai_why", None)
    try:
        found = for_keys(f"{book}:{r['id']}" for r in rows if isinstance(r, dict) and r.get("id") is not None)
        for r in rows:
            if isinstance(r, dict) and r.get("id") is not None:
                r["ai_why"] = found.get(f"{book}:{r['id']}")
    except Exception:
        logger.exception("why attach failed")
    return rows


def today_lines(day: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM ai_notes WHERE kind = 'why' AND day = ? ORDER BY id", (day,)).fetchall()
    return [notes._row(r) for r in rows]
