"""🤖 Filing and deal notes (PR 2): the night model reads one exchange filing, or one day's deals on one stock, and
writes a short note for the stock's owner. Code decides everything that matters: which sources are read (the scope:
stocks held in any book, in the plan, on the watchlist), the numbers the model may quote (pulled from the text by regex
and handed over as "numbers found by code"), the severity shown (a mapping from the category, the direction and the
scope: a model mistake cannot hide a pledge), the discount of a block deal to the close, and whether a note is pushed.
The model writes words and picks categories, nothing else.

  enqueue_tonight(config, now)      one night job per filing / deal group in scope (held first, then plan, then
                                    watchlist; `notes_per_night` at most) through backend.ai.queue, kind "night"
  enqueue_new_filings(config, new)  the same for the filings the 15-minute job just stored (an evening results filing
                                    gets its note within the night window, not the next day)
  run_one(source, config)           prompt -> night model -> validate -> severity by code -> one `ai_notes` row;
                                    an invalid answer gets one retry ("JSON only"), then the job fails
  note_for(kind, key)               the note of one source (for the alert lines), `latest_for_symbol`, `list_notes`,
                                    `stats`, `tonight` (the Queue · tonight rows)
Every note stores the prompt version (`PROMPT_V`); a source is noted once per version (UNIQUE guard on the table).
"""

import json
import re
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from backend.ai import client
from backend.ai import queue as q
from backend.config import IST, AppConfig
from backend.database.models import find_instrument, get_db, now_ist
from backend.logging_config.logger import setup_logger

logger = setup_logger("ai")

PROMPT_V = "notes-v1"
TEXT_MAX = 1500                                   # chars of the filing text in the prompt
TITLE_MAX, LINE_MAX, ACT_MAX, ACTS_MAX = 60, 180, 60, 3
STALE_HOURS = 24                                  # a queued job older than this is skipped, not run
DEFAULT_EST_S = 12                                # the estimate for a job kind with no done job yet
CATEGORIES = ("results", "order", "deal", "pledge", "legal", "rating", "management", "exchange", "fund_raise",
              "dividend", "other")
SEVERITIES = ("high", "medium", "low", "info")
DIRECTIONS = ("positive", "negative", "neutral")
BOOKS = ("held", "plan", "watch")                 # the scope reasons, in the order the night reads them
BOOK_WHY = {"held": "you hold it", "plan": "in the plan", "watch": "on your watchlist"}
KEY_PREFIX = "note:"                              # ai_jobs.key = note:filing:<seq_id> / note:deal:<symbol>|<date>

# Brokers and trading platforms are never named on a screen or in an alert. The list is kept reversed so the names
# appear nowhere in the source; `_blocked_word` turns them back when it checks a note.
_BLOCKED_REVERSED = ("ahdorez", "nortedart", "seitiruces katok", "oen katok", "xotspu", "wworg", "eno legna", "enolegna",
                     "tcerid icici", "tceridicici", "seitiruces cfdh", "lawso lalitom", "nahkerahs", "asiap5", "yenom mtyap",
                     "tcerid sixa", "seitiruces lfii", "amavun", "ssiewlede", "tcennoc etik")
BLOCKED = tuple(w[::-1] for w in _BLOCKED_REVERSED)

SYSTEM_PROMPT = f"""You summarise one Indian stock-exchange filing (or one day's bulk, block or stake deals) for the
stock's owner, a private investor. You read and explain; you never advise, decide or predict. Reply with JSON only,
one object, no prose around it:
{{"category": one of {list(CATEGORIES)},
 "severity": one of {list(SEVERITIES)},
 "direction": one of {list(DIRECTIONS)},
 "title": "at most {TITLE_MAX} characters, the fact in plain words",
 "line": "at most {LINE_MAX} characters of plain text: what happened and what it means for the owner",
 "acts": ["at most {ACTS_MAX} short phrases of what the owner may want to check"],
 "confidence": a number from 0 to 1}}
Severity: high = a promoter pledge increase or invocation, a loss or a sharp fall in profit, legal or regulatory action
against the company, insolvency, fraud, default, a rating downgrade, an auditor resignation, a promoter selling;
medium = results that are fine, an order win, a fund raise, an acquisition, merger or buyback, a change of management;
low = routine news with some meaning (a pledge released, a rating upgrade, a dividend); info = nothing to act on. Direction is for the owner of the stock: positive, negative or neutral.
Numbers: quote only the numbers listed under "numbers found by code" or written in the filing text, exactly as given;
never compute, convert or estimate a number. Never name a broker or a trading platform. No markdown."""

RETRY_SUFFIX = "\nYour previous answer was not valid. Reply with the JSON object only, using only the listed values."


# --- the scope ------------------------------------------------------------------------------------------------------

def _futures_held() -> set:
    """Symbols with an open futures lot in any of the book's accounts (options legs are index spreads: skipped)."""
    try:
        from backend.futures import book
        st = book._state() or {}
        return {s for a in (st.get("accounts") or {}).values() for s in (a.get("positions") or {})}
    except Exception:
        return set()


def scope() -> dict[str, dict]:
    """{symbol: {book: held | plan | watch, why, rank}}: the stocks you hold (cash books via alerts.guard.positions,
    futures via the book's state), have in the plan (rank = its number) or watch. Held wins over plan over watch."""
    from backend.alerts import guard as aguard
    from backend.alerts import plan as pl
    out: dict[str, dict] = {}
    for p in aguard.positions():
        out[p["symbol"]] = {"book": "held", "why": BOOK_WHY["held"], "rank": None}
    for s in sorted(_futures_held()):
        out.setdefault(s, {"book": "held", "why": BOOK_WHY["held"], "rank": None})
    for n, i in enumerate(pl.latest_plan()[1], start=1):
        out.setdefault(i["symbol"], {"book": "plan", "why": BOOK_WHY["plan"], "rank": n})
    with get_db() as conn:
        for r in conn.execute("SELECT symbol FROM watchlist ORDER BY symbol"):
            out.setdefault(r["symbol"], {"book": "watch", "why": BOOK_WHY["watch"], "rank": None})
    return out


def night_day(now: datetime, config: AppConfig) -> str:
    """The night's day (ISO): the evening's date; before `night_end` it is still the previous day's night."""
    from backend.alerts.jobs import hm
    d = now.date() if now.time() >= hm(config.ai.night_end) else now.date() - timedelta(days=1)
    return d.isoformat()


# --- numbers found by code ------------------------------------------------------------------------------------------

_MONEY = r"(?:rs\.?|₹|inr)\s*([\d,]+(?:\.\d+)?)\s*(crore|crores|cr\.?|lakh|lakhs|million|mn|bn|billion)"
_METRICS = (("revenue", r"revenue(?: from operations)?|total income|turnover|sales"), ("PAT", r"net profit|profit after tax|pat\b"),
            ("EBITDA", r"ebitda|operating profit"), ("loss", r"net loss|loss after tax"))
_YOY = re.compile(r"(?:up|down|grew|growth of|declined|rose|fell|increase of|decrease of|higher|lower)\s*(?:by\s*)?([\d.]+)\s*%"
                  r"|([\d.]+)\s*%\s*(?:yoy|y-o-y|year[- ]on[- ]year|growth|increase|decline)", re.I)


def extract_numbers(text: str) -> list[dict]:
    """[{label, value, unit, raw}]: revenue / PAT / EBITDA / loss in ₹ with their unit, and YoY percentages, pulled
    from a results text by regex. These are the only numbers the model may quote; it may not compute any other."""
    out, low = [], (text or "").lower()
    for label, pat in _METRICS:
        for m in re.finditer(rf"(?:{pat})[^.;\d₹]{{0,60}}?{_MONEY}", low, re.I):
            out.append({"label": label, "value": m.group(1), "unit": m.group(2).rstrip("."), "raw": text[m.start():m.end()].strip()})
            if len(out) >= 8:
                break
    for m in _YOY.finditer(text or ""):
        v = m.group(1) or m.group(2)
        out.append({"label": "YoY", "value": v, "unit": "%", "raw": m.group(0).strip()})
        if sum(1 for x in out if x["label"] == "YoY") >= 4:
            break
    return out


def numbers_text(numbers: list[dict]) -> str:
    return "; ".join(f"{n['label']} {n['value']} {n['unit']}" for n in numbers) or "none"


# --- prompts ----------------------------------------------------------------------------------------------------------

def _trim(text: str, n: int = TEXT_MAX) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def filing_prompt(f: dict, sc: dict) -> tuple[str, list[dict]]:
    """(user prompt, numbers found by code) for one filing row (seq_id, symbol, dt, category, text, kind)."""
    from backend.timefmt import ampm_date
    numbers = extract_numbers(f.get("text") or "") if f.get("kind") == "results" else []
    d = datetime.fromisoformat(f["dt"])
    lines = [f"Stock: {f['symbol']} ({sc['why']})", f"Filing category (exchange): {f['category']}",
             f"Filed: {ampm_date(d)}", f"Numbers found by code: {numbers_text(numbers)}",
             "Filing text:", _trim(f.get("text") or f["category"])]
    return "\n".join(lines), numbers


def deal_prompt(g: dict, sc: dict) -> tuple[str, list[dict]]:
    """(user prompt, numbers found by code) for one deal group (symbol, date, rows with the code's figures)."""
    from backend.ingest.filings import short_date
    numbers, lines = [], [f"Stock: {g['symbol']} ({sc['why']})", f"Deals disclosed on {short_date(g['date'])}:"]
    for r in g["rows"]:
        who = "promoter " if r["promoter"] else ""
        part = f"- {who}{r['client']} {r['side'].lower()} "
        if r["source"] == "sast":
            part += f"{r['pct']:.2f}% of the company" + (f" (holding after {r['after_pct']:.2f}%)" if r.get("after_pct") is not None else "")
            numbers.append({"label": "stake", "value": f"{r['pct']:.2f}", "unit": "%", "raw": part})
        else:
            part += f"{r['qty']:,.0f} shares at {r['price']:,.2f} ({r['source']} deal)"
            numbers += [{"label": "qty", "value": f"{r['qty']:,.0f}", "unit": "shares", "raw": part},
                        {"label": "price", "value": f"{r['price']:,.2f}", "unit": "₹", "raw": part}]
            if r.get("discount_pct") is not None:
                part += f", {abs(r['discount_pct']):.1f}% {'under' if r['discount_pct'] < 0 else 'over'} the day's close {g['close']:,.2f}"
                numbers.append({"label": "vs close", "value": f"{abs(r['discount_pct']):.1f}", "unit": "%", "raw": part})
        lines.append(part)
    lines += [f"Numbers found by code: {numbers_text(numbers)}",
              "Explain who bought or sold and what it may mean for the owner. Quote only the numbers above."]
    return "\n".join(lines), numbers


def build_prompt(source: dict) -> tuple[str, str, list[dict]]:
    """(system, user, numbers) for a source {kind: filing, filing, scope} or {kind: deal, group, scope}."""
    if source["kind"] == "filing":
        user, numbers = filing_prompt(source["filing"], source["scope"])
    else:
        user, numbers = deal_prompt(source["group"], source["scope"])
    return SYSTEM_PROMPT, user, numbers


# --- the answer ------------------------------------------------------------------------------------------------------------

class NoteInvalid(ValueError):
    """The model's answer is not a usable note (bad JSON, a value outside the lists, a broker name)."""


_DIGITS = re.compile(r"(?<![A-Za-z\d])\d[\d,]*(?:\.\d+)?")     # a figure; not the 2 of Q2 or the 27 of FY27


def _digit_tokens(text: str) -> set:
    return {t.replace(",", "") for t in _DIGITS.findall(text or "")}


def _blocked_word(text: str) -> Optional[str]:
    low = (text or "").lower()
    return next((w for w in BLOCKED if w in low), None)


def _clip(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _strip_numbers(text: str, allowed: set) -> tuple[str, bool]:
    """Drop every number in `text` that is neither in the source text nor found by code; (text, all_ok)."""
    bad = [t for t in _DIGITS.findall(text) if len(t) > 1 and t.replace(",", "") not in allowed and not _partial(t, allowed)]
    if not bad:
        return text, True
    for t in bad:
        text = re.sub(r"(?:₹|rs\.?\s*)?" + re.escape(t) + r"\s*(?:%|crore|cr|lakh|million)?", "", text, count=1, flags=re.I)
    return re.sub(r"\s{2,}", " ", text).strip(" ,;·-"), False


def _partial(token: str, allowed: set) -> bool:
    """'1,234' quoted as '1234.5' or '1234' as '1,234.50': the same number written differently."""
    t = token.replace(",", "")
    return any(a.rstrip("0").rstrip(".") == t.rstrip("0").rstrip(".") for a in allowed if "." in a or "." in t)


def validate(raw: str, source_text: str, numbers: list[dict]) -> dict:
    """The JSON contract checked by code: the enums, the lengths (clipped), the acts (≤ 3 short phrases), the
    confidence (0–1), no broker name anywhere, and no figure in the line or the title that is not in the source text or
    the numbers found by code (such figures are stripped and `numbers_checked` is False; a single digit and the digits of
    a word such as Q2 or FY27 are not figures). Raises NoteInvalid."""
    try:
        d = json.loads(raw)
    except ValueError as exc:
        raise NoteInvalid(f"not JSON: {raw[:80]!r}") from exc
    if not isinstance(d, dict):
        raise NoteInvalid("not a JSON object")
    for key, values in (("category", CATEGORIES), ("severity", SEVERITIES), ("direction", DIRECTIONS)):
        v = str(d.get(key) or "").strip().lower()
        if v not in values:
            raise NoteInvalid(f"{key} {d.get(key)!r} is not one of {', '.join(values)}")
        d[key] = v
    acts = d.get("acts") if isinstance(d.get("acts"), list) else []
    acts = [_clip(a, ACT_MAX) for a in acts if str(a or "").strip()][:ACTS_MAX]
    title, line = _clip(d.get("title"), TITLE_MAX), _clip(d.get("line"), LINE_MAX)
    if not title or not line:
        raise NoteInvalid("title or line missing")
    for text in (title, line, *acts):
        w = _blocked_word(text)
        if w:
            raise NoteInvalid(f"names a broker ({w[:3]}…)")
    allowed = _digit_tokens(source_text) | {str(n["value"]).replace(",", "") for n in numbers}
    line, ok_line = _strip_numbers(line, allowed)
    title, ok_title = _strip_numbers(title, allowed)
    try:
        conf = min(1.0, max(0.0, float(d.get("confidence", 0.5))))
    except (TypeError, ValueError):
        conf = 0.5
    return {"category": d["category"], "severity_model": d["severity"], "direction": d["direction"], "title": title or "…",
            "line": line or "…", "acts": acts, "confidence": round(conf, 2), "numbers_checked": ok_line and ok_title}


# --- severity by code -----------------------------------------------------------------------------------------------------

def _mentions(text: str, *words: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in words)


def filing_severity(note: dict, f: dict, book: str) -> str:
    """The severity shown, from the category, the direction and the source text (never the model's own):
    high = pledge increase / invocation, results with a negative direction on a held name, legal (not in the company's
    favour), a rating downgrade, an auditor resignation; medium = results otherwise, orders, fund raise, management;
    low = deal / dividend / pledge release / rating upgrade; info = exchange queries and the rest."""
    cat, neg, text = note["category"], note["direction"] == "negative", f"{f.get('category', '')} {f.get('text', '')}"
    if cat == "pledge":
        return "high" if neg or _mentions(text, "increase", "creat", "invok", "invoc", "encumbr", "additional") else "low"
    if cat == "results":
        return "high" if neg and book == "held" else "medium"
    if cat == "legal":
        return "medium" if note["direction"] == "positive" or _mentions(text, "in favour", "in favor") else "high"
    if cat == "rating":
        return "high" if neg or _mentions(text, "downgrad") else "low"
    if cat == "management":
        return "high" if _mentions(text, "auditor") and _mentions(text, "resign") else "medium"
    if cat in ("order", "fund_raise"):
        return "medium"
    if cat in ("deal", "dividend"):
        return "low"
    return "info"


def deal_severity(g: dict, book: str) -> str:
    """Deal notes: a promoter selling, or a block / bulk deal of ≥ 1% at ≥ 3% under the close on a held or planned
    name = high; a promoter buying or any stake disclosure of ≥ 1% = medium; the rest low."""
    if g["promoter_sell"]:
        return "high"
    if book in ("held", "plan") and any((r.get("pct") or 0) >= 1.0 and (r.get("discount_pct") or 0) <= -3.0 for r in g["rows"]):
        return "high"
    if any(r["promoter"] for r in g["rows"]) or any((r.get("pct") or 0) >= 1.0 for r in g["rows"]):
        return "medium"
    return "low"


# --- deals: grouping and the discount to the close --------------------------------------------------------------------------

def _close_on(symbol: str, day: str) -> Optional[float]:
    """The stock's close on `day` (or the last session before it) from candles_daily; None without a token or bars."""
    with get_db() as conn:
        inst = find_instrument(conn, symbol, "NSE")
        if not inst:
            return None
        r = conn.execute("SELECT close FROM candles_daily WHERE instrument_token = ? AND date <= ? ORDER BY date DESC LIMIT 1",
                         (inst["instrument_token"], day)).fetchone()
    return float(r[0]) if r else None


def group_deals(rows: list[dict], close_fn: Callable[[str, str], Optional[float]] = None) -> list[dict]:
    """One group per (symbol, date): {symbol, date, rows, close, promoter_sell, max_pct, key}. Each row gains
    `discount_pct` = price vs the day's close (negative = under the close; None for stake disclosures without a price)."""
    close_fn = close_fn or _close_on
    groups: dict[tuple, dict] = {}
    for r in sorted(rows, key=lambda r: (r["symbol"], r["date"], r["client"])):
        g = groups.setdefault((r["symbol"], r["date"]), {"symbol": r["symbol"], "date": r["date"], "rows": [], "close": None,
                                                        "promoter_sell": False, "max_pct": 0.0, "key": f"{r['symbol']}|{r['date']}"})
        if g["close"] is None and r.get("price"):
            g["close"] = close_fn(r["symbol"], r["date"])
        row = dict(r)
        row["discount_pct"] = round((r["price"] / g["close"] - 1) * 100, 2) if r.get("price") and g["close"] else None
        g["rows"].append(row)
        g["promoter_sell"] = g["promoter_sell"] or bool(r.get("promoter") and r["side"] == "SELL")
        g["max_pct"] = max(g["max_pct"], r.get("pct") or 0.0)
    return list(groups.values())


# --- the sources of a night -------------------------------------------------------------------------------------------------

def _noted_keys(kind: str) -> set:
    with get_db() as conn:
        return {r[0] for r in conn.execute("SELECT source_key FROM ai_notes WHERE kind = ? AND prompt_version = ? AND source_key IS NOT NULL",
                                           (kind, PROMPT_V))}


def _failed_keys(since: str) -> set:
    """Sources whose job failed since `since`: not retried that night (the breaker already counted them)."""
    with get_db() as conn:
        return {r[0] for r in conn.execute("SELECT key FROM ai_jobs WHERE kind = 'night' AND key LIKE ? AND status = 'failed' AND created >= ?",
                                           (KEY_PREFIX + "%", since))}


def _wanted_filing(f: dict) -> bool:
    """Important kinds and updates, plus any filing about a pledge whatever its kind (NSE files them as updates)."""
    from backend.ingest.filings import IMPORTANT
    return f["kind"] in IMPORTANT or f["kind"] == "update" or _mentions(f"{f['category']} {f['text']}", "pledge", "encumb")


def filing_sources(sc: dict, since: str, symbols: Optional[set] = None) -> list[dict]:
    """[{kind: filing, key, filing, scope}] for the in-scope filings since `since` (ISO), not noted yet, newest first."""
    syms = sorted((symbols if symbols is not None else set(sc)) & set(sc))
    if not syms:
        return []
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT seq_id, symbol, dt, category, text, url, kind FROM filings WHERE dt >= ? AND symbol IN ({','.join('?' * len(syms))}) "
            "ORDER BY dt DESC", (since, *syms))]
    noted = _noted_keys("filing")
    return [{"kind": "filing", "key": r["seq_id"], "filing": r, "scope": sc[r["symbol"]]}
            for r in rows if r["seq_id"] not in noted and _wanted_filing(r)]


def deal_sources(sc: dict, since: str, symbols: Optional[set] = None) -> list[dict]:
    """[{kind: deal, key, group, scope}] for the in-scope deals since `since` (ISO date), grouped per (symbol, date)."""
    syms = sorted((symbols if symbols is not None else set(sc)) & set(sc))
    if not syms:
        return []
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT date, symbol, client, side, qty, price, source, pct, after_pct, promoter, url FROM deals WHERE date >= ? "
            f"AND symbol IN ({','.join('?' * len(syms))})", (since[:10], *syms))]
    noted = _noted_keys("deal")
    groups = sorted(group_deals(rows), key=lambda g: g["date"], reverse=True)
    return [{"kind": "deal", "key": g["key"], "group": g, "scope": sc[g["symbol"]]} for g in groups if g["key"] not in noted]


def _job_key(s: dict) -> str:
    return f"{KEY_PREFIX}{s['kind']}:{s['key']}"


def _order(sources: list[dict]) -> list[dict]:
    """Held names first, then the plan, then the watchlist; newest first within each."""
    when = lambda s: s["filing"]["dt"] if s["kind"] == "filing" else s["group"]["date"]
    newest_first = sorted(sources, key=when, reverse=True)
    return sorted(newest_first, key=lambda s: BOOKS.index(s["scope"]["book"]))      # a stable sort keeps newest first


def _since(now: datetime, config: AppConfig) -> str:
    return (now - timedelta(days=int(config.ai.note_days))).replace(tzinfo=None).isoformat(timespec="seconds")


def _tonight_count(day: str) -> int:
    """Notes written this night plus note jobs still queued or running: what counts against the nightly cap."""
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM ai_notes WHERE day = ?", (day,)).fetchone()[0]
        n += conn.execute("SELECT COUNT(*) FROM ai_jobs WHERE kind = 'night' AND key LIKE ? AND status IN ('queued', 'running')",
                          (KEY_PREFIX + "%",)).fetchone()[0]
    return n


def enqueue_sources(config: AppConfig, sources: list[dict], now: datetime, notifier=None) -> dict:
    """Queue one night job per source, in scope order, up to the nightly cap: {queued, dedup, capped, keys}."""
    day = night_day(now, config)
    room = max(0, int(config.ai.notes_per_night) - _tonight_count(day))
    failed = _failed_keys(_since(now, config))
    out = {"queued": 0, "dedup": 0, "capped": 0, "keys": []}
    for s in _order([s for s in sources if _job_key(s) not in failed]):
        if out["queued"] >= room:
            out["capped"] += 1
            continue
        sym = s["filing"]["symbol"] if s["kind"] == "filing" else s["group"]["symbol"]
        try:
            job = q.enqueue(config, "night", _job_key(s), _runner(s, config, now, notifier), model=config.ai.night_model,
                            note=f"{sym} · {s['kind']} note")
        except q.QueueFull as exc:
            logger.warning("AI notes: %s", exc)
            break
        out["dedup" if job["dedup"] else "queued"] += 1
        if not job["dedup"]:
            out["keys"].append(_job_key(s))
    return out


def _runner(source: dict, config: AppConfig, queued_at: datetime, notifier) -> Callable[[], dict]:
    def work():
        if datetime.now(IST) - queued_at > timedelta(hours=STALE_HOURS):
            return {"skip": "stale"}
        return run_one(source, config, notifier=notifier)
    return work


def enqueue_tonight(config: AppConfig, now: Optional[datetime] = None, notifier=None) -> dict:
    """Tonight's notes: every in-scope filing and deal group of the last `note_days` days not noted yet, held names
    first, then the plan, then the watchlist, `notes_per_night` at most. The guard decides at run time whether a job may
    run; what it refuses is queued again by the next call (the filings job every 15 minutes, the evening job)."""
    now = now or datetime.now(IST)
    sc = scope()
    since = _since(now, config)
    sources = filing_sources(sc, since) + deal_sources(sc, since)
    out = enqueue_sources(config, sources, now, notifier)
    out.update(scope=len(sc), sources=len(sources), day=night_day(now, config))
    logger.info("AI notes tonight: %d source(s) in scope %d, %d queued, %d already queued, %d over the cap",
                len(sources), len(sc), out["queued"], out["dedup"], out["capped"])
    return out


def enqueue_new_filings(config: AppConfig, new: list[dict], now: Optional[datetime] = None, notifier=None) -> dict:
    """The 15-minute filings job's hook: notes for the filings it just stored, on stocks in scope."""
    now = now or datetime.now(IST)
    syms = {f["symbol"] for f in new}
    if not syms:
        return {"queued": 0, "dedup": 0, "capped": 0, "keys": []}
    sc = scope()
    seqs = {str(f["seq_id"]) for f in new}
    sources = [s for s in filing_sources(sc, _since(now, config), syms) if s["key"] in seqs]
    return enqueue_sources(config, sources, now, notifier)


# --- one note ---------------------------------------------------------------------------------------------------------------

def _generate(config: AppConfig, system: str, user: str) -> dict:
    c = config.ai
    return client.generate(c.night_model, user, system=system, json_mode=True, timeout=float(c.timeout_s), keep_alive=c.keep_alive)


def ask(config: AppConfig, system: str, user: str, source_text: str, numbers: list[dict]) -> tuple[dict, dict]:
    """(note, generate result): the model's answer validated; one retry with "JSON only" when it is not usable."""
    r = _generate(config, system, user)
    try:
        return validate(r["text"], source_text, numbers), r
    except NoteInvalid as exc:
        logger.info("AI note answer invalid (%s); one retry", exc)
        r2 = _generate(config, system + RETRY_SUFFIX, user)
        r2["tokens"] = r2["tokens"] + r["tokens"]
        r2["ms"] = r2["ms"] + r["ms"]
        return validate(r2["text"], source_text, numbers), r2


def _source_text(source: dict) -> str:
    if source["kind"] == "filing":
        return source["filing"].get("text") or ""
    return build_prompt(source)[1]                      # the deal lines carry the code's figures


def _source_meta(source: dict) -> dict:
    if source["kind"] == "filing":
        f = source["filing"]
        return {"seq_id": f["seq_id"], "url": f.get("url") or "", "dt": f["dt"], "category": f["category"], "kind": f["kind"]}
    g = source["group"]
    return {"symbol": g["symbol"], "date": g["date"], "url": next((r["url"] for r in g["rows"] if r.get("url")), ""),
            "dt": g["date"], "category": "deals", "n": len(g["rows"]), "close": g["close"]}


def save_note(source: dict, note: dict, severity: str, numbers: list[dict], model: str, ms: int, day: str) -> Optional[int]:
    """One `ai_notes` row (None when the source was noted meanwhile: the UNIQUE guard)."""
    sym = source["filing"]["symbol"] if source["kind"] == "filing" else source["group"]["symbol"]
    data = {"category": note["category"], "direction": note["direction"], "acts": note["acts"], "confidence": note["confidence"],
            "severity_model": note["severity_model"], "numbers": numbers, "numbers_checked": note["numbers_checked"],
            "source": _source_meta(source), "scope": source["scope"], "alerted": False}
    with get_db() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO ai_notes (created, day, kind, symbol, book, severity, title, text, data, model, "
                           "prompt_version, ms, source_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (now_ist(), day, source["kind"], sym, source["scope"]["book"], severity, note["title"], note["line"],
                            json.dumps(data), model, PROMPT_V, ms, source["key"]))
    return cur.lastrowid if cur.rowcount else None


def run_one(source: dict, config: AppConfig, now: Optional[datetime] = None, notifier=None) -> dict:
    """Prompt, model, validation, severity by code, the row, the high-severity alert: {id, tokens, note}. Raises
    NoteInvalid (the job fails) when the second answer is not usable either."""
    now = now or datetime.now(IST)
    system, user, numbers = build_prompt(source)
    note, r = ask(config, system, user, _source_text(source), numbers)
    book = source["scope"]["book"]
    severity = filing_severity(note, source["filing"], book) if source["kind"] == "filing" else deal_severity(source["group"], book)
    note_id = save_note(source, note, severity, numbers, r["model"], r["ms"], night_day(now, config))
    sym = source["filing"]["symbol"] if source["kind"] == "filing" else source["group"]["symbol"]
    if note_id and severity == "high":
        try:
            alert_high(get_note(note_id), config, notifier)
        except Exception:
            logger.exception("AI note alert failed for %s", sym)
    return {"id": note_id, "tokens": r["tokens"], "note": f"{sym} · {severity} · {note['title']}"}


# --- the alert for a high note -------------------------------------------------------------------------------------------------

def where_text(symbol: str, config: AppConfig, sc: Optional[dict] = None) -> tuple[str, Optional[str]]:
    """("held in Real (+4.2%)" / "in tomorrow's plan at #3" / "on your watchlist", the skip hint or None): the part of
    the note alert that comes from code. The skip hint exists only for a plan name while real trading is on."""
    from backend.engine import real
    from backend.timefmt import ampm
    sc = sc if sc is not None else scope().get(symbol) or {}
    held = []
    try:
        for x in real.summary(config).get("positions") or []:
            if x["symbol"] == symbol and x.get("buy"):
                held.append(f"held in Real ({(x['last'] / x['buy'] - 1) * 100:+.1f}%)")
    except Exception:
        pass
    if not held and sc.get("book") == "held":
        held.append("held in Futures" if symbol in _futures_held() else "held")
    if held:
        return held[0], None
    if sc.get("book") == "plan":
        hint = f"reply skip {symbol} before {ampm(config.real.send_time)} to leave it out" if real.has_account() and config.real.enabled else None
        return f"in tomorrow's plan at #{sc.get('rank') or '?'}", hint
    return "on your watchlist", None


def alert_high(note: dict, config: AppConfig, notifier=None) -> bool:
    """The separate message for a high note that no filing / deal alert carried: key {day}|ai-note-{id}, channel
    "filing"; once. Builds a Notifier from the alerts config when none is given."""
    from backend.alerts import messages as msg
    from backend.alerts.dispatch import Notifier
    if note.get("alerted") or note["severity"] != "high":
        return False
    notifier = notifier or Notifier(config.alerts)
    where, hint = where_text(note["symbol"], config, note.get("scope"))
    sent = notifier.send(f"{note['day']}|ai-note-{note['id']}", "filing", msg.note_alert(note, where, hint), note["symbol"])
    if sent:
        mark_alerted(note["id"])
    return sent


def mark_alerted(note_id: int) -> None:
    """The note was part of a sent message (its own, or the AI line on a filing / deal alert)."""
    with get_db() as conn:
        r = conn.execute("SELECT data FROM ai_notes WHERE id = ?", (note_id,)).fetchone()
        if r:
            d = json.loads(r[0] or "{}")
            d["alerted"] = True
            conn.execute("UPDATE ai_notes SET data = ? WHERE id = ?", (json.dumps(d), note_id))


# --- reading the notes -----------------------------------------------------------------------------------------------------------

def _row(r) -> dict:
    """A table row flattened with its data JSON: the page's note shape."""
    d = json.loads(r["data"] or "{}")
    src = d.get("source") or {}
    return {"id": r["id"], "created": r["created"], "day": r["day"], "kind": r["kind"], "symbol": r["symbol"], "book": r["book"],
            "severity": r["severity"], "title": r["title"], "text": r["text"], "model": r["model"], "prompt_version": r["prompt_version"],
            "ms": r["ms"], "source_key": r["source_key"], "category": d.get("category"), "direction": d.get("direction"),
            "acts": d.get("acts") or [], "confidence": d.get("confidence"), "severity_model": d.get("severity_model"),
            "numbers": d.get("numbers") or [], "numbers_checked": d.get("numbers_checked"), "source": src, "url": src.get("url") or "",
            "source_at": src.get("dt"), "scope": d.get("scope"), "alerted": bool(d.get("alerted"))}


def get_note(note_id: int) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM ai_notes WHERE id = ?", (note_id,)).fetchone()
    return _row(r) if r else None


def note_for(kind: str, key: str) -> Optional[dict]:
    """The note of one source (a filing's seq_id, a deal group's symbol|date), the latest prompt version first."""
    with get_db() as conn:
        r = conn.execute("SELECT * FROM ai_notes WHERE kind = ? AND source_key = ? ORDER BY id DESC LIMIT 1", (kind, str(key))).fetchone()
    return _row(r) if r else None


def latest_for_symbol(symbol: str, days: int = 7, today: Optional[str] = None) -> Optional[dict]:
    """{title, severity, at, kind, id} of the stock's newest note within `days`; None without one."""
    return latest_for_symbols([symbol], days, today).get(symbol)


def latest_for_symbols(symbols, days: int = 7, today: Optional[str] = None) -> dict[str, dict]:
    syms = sorted(set(symbols))
    if not syms:
        return {}
    since = (date.fromisoformat(today or now_ist()[:10]) - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute(f"SELECT id, symbol, title, severity, created, kind FROM ai_notes WHERE created >= ? AND symbol IN "
                            f"({','.join('?' * len(syms))}) ORDER BY id", (since, *syms)).fetchall()
    return {r["symbol"]: {"id": r["id"], "title": r["title"], "severity": r["severity"], "at": r["created"], "kind": r["kind"]} for r in rows}


def list_notes(symbol: Optional[str] = None, kind: Optional[str] = None, severity: Optional[str] = None, limit: int = 50,
               offset: int = 0) -> dict:
    """{rows, total}: the notes newest first, filtered by symbol / kind / severity."""
    where, args = [], []
    for col, v in (("symbol", (symbol or "").upper()), ("kind", kind), ("severity", severity)):
        if v:
            where.append(f"{col} = ?")
            args.append(v)
    sql = (" WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM ai_notes{sql}", args).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM ai_notes{sql} ORDER BY id DESC LIMIT ? OFFSET ?",
                            (*args, max(1, min(int(limit), 500)), max(0, int(offset)))).fetchall()
    return {"rows": [_row(r) for r in rows], "total": total}


def high_notes(day: str, limit: int = 8) -> list[dict]:
    """The night's high notes (for attention and the alerts), newest first."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM ai_notes WHERE day = ? AND severity = 'high' ORDER BY id DESC LIMIT ?", (day, limit)).fetchall()
    return [_row(r) for r in rows]


def stats(now: datetime, config: AppConfig) -> dict:
    """{today, high, last_night: {day, n, by_kind, ms_total, at}}: notes written today (calendar day), how many high,
    and the newest night that has notes."""
    today = now.date().isoformat()
    with get_db() as conn:
        n_today = conn.execute("SELECT COUNT(*) FROM ai_notes WHERE created LIKE ?", (today + "%",)).fetchone()[0]
        n_high = conn.execute("SELECT COUNT(*) FROM ai_notes WHERE created LIKE ? AND severity = 'high'", (today + "%",)).fetchone()[0]
        last_day = conn.execute("SELECT MAX(day) FROM ai_notes WHERE day <= ?", (night_day(now, config),)).fetchone()[0]
        last = None
        if last_day:
            by_kind = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM ai_notes WHERE day = ? GROUP BY kind", (last_day,))}
            ms, at = conn.execute("SELECT COALESCE(SUM(ms), 0), MAX(created) FROM ai_notes WHERE day = ?", (last_day,)).fetchone()
            high = conn.execute("SELECT COUNT(*) FROM ai_notes WHERE day = ? AND severity = 'high'", (last_day,)).fetchone()[0]
            last = {"day": last_day, "n": sum(by_kind.values()), "by_kind": by_kind, "ms_total": int(ms), "at": at, "high": high}
    return {"today": n_today, "high": n_high, "last_night": last}


def running() -> bool:
    """A note job is queued or running right now."""
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM ai_jobs WHERE kind = 'night' AND key LIKE ? AND status IN ('queued', 'running') LIMIT 1",
                            (KEY_PREFIX + "%",)).fetchone() is not None


def _avg_ms(kind: str) -> Optional[float]:
    with get_db() as conn:
        r = conn.execute("SELECT AVG(ms) FROM ai_jobs WHERE kind = 'night' AND key LIKE ? AND status = 'done'",
                         (f"{KEY_PREFIX}{kind}:%",)).fetchone()
    return float(r[0]) if r and r[0] else None


def tonight(config: AppConfig, now: Optional[datetime] = None) -> list[dict]:
    """The Queue · tonight rows: [{kind, label, n, queued, est_s}] per note kind, n = the jobs queued now plus the
    sources the next enqueue would add (within the cap); est_s from the average ms of that kind's done jobs (12 s default)."""
    now = now or datetime.now(IST)
    sc = scope()
    since = _since(now, config)
    out, room = [], max(0, int(config.ai.notes_per_night) - _tonight_count(night_day(now, config)))
    with get_db() as conn:
        queued = {r[0]: r[1] for r in conn.execute(
            "SELECT substr(key, 6, instr(substr(key, 6), ':') - 1), COUNT(*) FROM ai_jobs WHERE kind = 'night' AND key LIKE ? "
            "AND status IN ('queued', 'running') GROUP BY 1", (KEY_PREFIX + "%",))}
    for kind, label, fn in (("filing", "Filing notes", filing_sources), ("deal", "Deal notes", deal_sources)):
        pending = [s for s in fn(sc, since) if not q._pending("night", _job_key(s))]
        add = min(room, len(pending))
        room -= add
        n = queued.get(kind, 0) + add
        avg = _avg_ms(kind) or DEFAULT_EST_S * 1000
        out.append({"kind": kind, "label": label, "n": n, "queued": queued.get(kind, 0), "est_s": round(n * avg / 1000)})
    return out


def attach(rows: list, days: int = 7, today: Optional[str] = None) -> list:
    """Give every position row (dicts with a "symbol") an `ai_note: {title, severity, at} | None` from the stock's
    newest note within `days`. Used by the Real, Paper and Futures summaries and the overview. Never raises."""
    for r in rows:
        if isinstance(r, dict):
            r.setdefault("ai_note", None)
    try:
        syms = {r["symbol"] for r in rows if isinstance(r, dict) and r.get("symbol")}
        if not syms:
            return rows
        latest = latest_for_symbols(syms, days, today or now_ist()[:10])
        for r in rows:
            n = latest.get(r.get("symbol")) if isinstance(r, dict) else None
            if n:
                r["ai_note"] = {"title": n["title"], "severity": n["severity"], "at": n["at"]}
    except Exception:                                   # a note is decoration: the summary never fails on it
        logger.exception("AI notes attach failed")
    return rows
