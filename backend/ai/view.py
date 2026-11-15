"""🤖 AI view: a rating beside every system-suggested stock. Code gathers the facts (the plan or scan payload, the
daily bars, the market filter, the industry, the results date, the AI's notes, the held position) and the candidate
levels; the model rates the name 0–100 with up to three short reasons and picks its support / resistance levels from
the candidates only. Code validates every answer, RECOMPUTES the verdict from the score (the model's own verdict is
kept as `verdict_model`), scores the outcome 5 / 10 / 20 sessions later, runs a shuffle test on ~1 in 5 views and
keeps the record. The model rates and explains; it never acts: the three switches that would let a score touch the
plan (rank ties, size, the exit-watch alert) stay locked until the record has beaten the plan for 4 weeks running, and
even then a score never sizes above 1.0×, never skips a name and never moves a stop.

  features(symbol, as_of, ...)   the facts the model gets, all computed by code
  levels(symbol, as_of, item)    up to 10 candidate levels {price, kind, label}
  build_prompt(...)              prompt view-v1: the facts, the candidates, the previous view, the calibration note
  validate(raw, ctx)             the JSON contract; ViewInvalid names the fault (one retry)
  verdict_for(...)               the verdict by code from the score (held: exit-watch on a broken fails_below)
  run_night(config, now)         the night job "view:<day>": rate in order within the cap, outcomes, calibration,
                                 the shuffle test, the exit-watch flags, the Telegram lines
  score_outcomes(config)         h5 / h10 / h20 {pct, hit} for the views whose sessions have passed
  record(weeks)                  by verdict vs the plan baseline, by week, shuffle, lean, worst bucket, the unlock rule
  attach(rows, source)           `ai_view` on plan / follow / screen / futures / position rows; never raises
  size_mult / ai_qty             the size-by-score math (never above 1.0×, never under one share)
Rows live in `ai_views` (never deleted; UNIQUE (day, symbol, source)); the prompt version is `PROMPT_V`.
"""

import json
import random
from datetime import date, datetime, timedelta
from math import floor
from typing import Callable, Optional

from backend.ai import client, notes
from backend.ai import queue as q
from backend.config import IST, AppConfig
from backend.database.models import find_instrument, get_db, get_setting, now_ist, set_setting
from backend.logging_config.logger import setup_logger

logger = setup_logger("ai")

PROMPT_V = "view-v1"
KEY_PREFIX = "view:"
CANDIDATE_VERDICTS = ("buy", "wait", "avoid")
HELD_VERDICTS = ("hold", "trim", "exit-watch")
VERDICTS = CANDIDATE_VERDICTS + HELD_VERDICTS
REASONS_MAX, REASON_LEN, CHANGE_LEN, WOULD_LEN = 3, 60, 100, 120
LEVEL_TOL = 0.002                                  # a picked price matches a candidate within 0.2%
MAX_CANDIDATES = 10
SWING_SESSIONS, PIVOT_BARS = 120, 2                # 5-bar pivots (2 bars each side) over the last 120 sessions
HORIZONS = (5, 10, 20)
HIT_PCT = 3.0                                      # a candidate "hit": +3% at the horizon (or the trail armed first)
SHUFFLE_SHARE = 0.2                                # ~1 in 5 views also rated with another stock's facts
UNLOCK_WEEKS = 4
MIN_BUCKET = 3                                     # a "where it is wrong" bucket needs this many scored buys
VIEW_DAYS = 10                                     # attach: a view older than this is not shown
TOP_PER_SCREEN = 5
SCREEN_STAGES = {"fresh": "breakout", "forming": "ready"}   # the screens' stages the view rates (breakout / ready)
LOCKED = ("view_rank_by_score", "view_size_by_score", "view_exit_watch_alert")
VIEW_KEYS = ("view_enabled", "view_rate_held", "view_rank_by_score", "view_size_by_score", "view_size_floor",
             "view_exit_watch_alert", "view_score_buy", "view_score_avoid", "view_max_per_night")
LOCKED_NOTE = "locked: the AI view record has not beaten the plan for 4 weeks running"
CALIB_KEY, LAST_NIGHT_KEY = "ai.view_calibration", "ai.view_last_night"
PLAN_KEYS = ("stage", "reason", "close", "change_pct", "turnover_cr", "base_label", "base_number", "base_weeks", "pivot",
             "dist_to_pivot_pct", "gain_from_pivot_pct", "breakout_date", "sessions_since_breakout", "volume_ratio", "volume_ok",
             "depth_pct", "rs", "rs_score", "industry_rank", "industry_of", "tt_passed", "tt_of", "pct_from_high52", "vcp", "score")
GEN_OPTIONS = {"temperature": 0.1, "num_predict": 360, "num_ctx": 3072, "top_p": 0.9}

SYSTEM_PROMPT = f"""You rate one Indian stock for a private investor whose own software suggested it (or who holds it).
You read the facts his software computed and give a rating; you never place, size or cancel an order, never move a stop
and never change a setting. Rate from the facts first. The previous view, when given, is context, not an anchor: say
in `change` why the score moved, or null. Never invent a number: every figure in your reasons must be written in the
facts. Pick levels ONLY from the candidate levels listed (copy the price exactly). `fails_below` is the support under
which the setup fails; it must be below the current close. For a candidate the verdict is buy, wait or avoid; for a
held name hold, trim or exit-watch. Never name a broker or a trading platform. Reply with JSON only, no markdown:
{{"score": 0-100, "verdict": "...", "confidence": 0.0-1.0, "reasons": ["at most {REASONS_MAX} phrases of at most
{REASON_LEN} characters"], "change": "at most {CHANGE_LEN} characters" or null, "levels": {{"support_1": price,
"support_2": price or null, "resistance": price or null, "fails_below": price}}, "would_change": "at most {WOULD_LEN}
characters: what would change the rating"}}"""

RETRY_SUFFIX = "\nYour previous answer was not valid: {why}. Reply with the JSON object only, levels copied from the candidates."


class ViewInvalid(ValueError):
    """The model's answer breaks the contract (the message names the fault)."""


# --- bars and small maths ---------------------------------------------------------------------------------------------

def _bars(symbol: str, as_of: str, n: int = 260) -> list[dict]:
    """The stock's last `n` daily bars up to `as_of`, ascending; [] without a token."""
    with get_db() as conn:
        inst = find_instrument(conn, symbol, "NSE")
        if not inst:
            return []
        rows = conn.execute("SELECT date, open, high, low, close, volume FROM candles_daily WHERE instrument_token = ? AND date <= ? "
                            "ORDER BY date DESC LIMIT ?", (inst["instrument_token"], as_of, n)).fetchall()
    return [dict(r) for r in reversed(rows)]


def _after(symbol: str, day: str, n: int) -> list[dict]:
    """The stock's first `n` daily bars after `day`, ascending."""
    with get_db() as conn:
        inst = find_instrument(conn, symbol, "NSE")
        if not inst:
            return []
        rows = conn.execute("SELECT date, high, low, close, volume FROM candles_daily WHERE instrument_token = ? AND date > ? "
                            "ORDER BY date LIMIT ?", (inst["instrument_token"], day, n)).fetchall()
    return [dict(r) for r in rows]


def _avg(xs) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _sma(closes: list, n: int) -> Optional[float]:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def _r(x, nd: int = 2):
    return round(x, nd) if isinstance(x, (int, float)) and not isinstance(x, bool) else x


def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return round((a / b - 1) * 100, 2) if a and b else None


def atr_pct(bars: list[dict], n: int = 14) -> Optional[float]:
    """ATR(n) as % of the last close (true range averaged over n sessions)."""
    if len(bars) < n + 1:
        return None
    tr = [max(b["high"] - b["low"], abs(b["high"] - p["close"]), abs(b["low"] - p["close"])) for p, b in zip(bars[-n - 1:-1], bars[-n:])]
    return round(sum(tr) / n / bars[-1]["close"] * 100, 2) if bars[-1]["close"] else None


def dry_up(bars: list[dict]) -> Optional[float]:
    """The volume dry-up: the last 10 sessions' average volume over the last 50 sessions' − 1, in %."""
    if len(bars) < 50:
        return None
    a, b = _avg([x["volume"] for x in bars[-10:]]), _avg([x["volume"] for x in bars[-50:]])
    return round((a / b - 1) * 100, 1) if b else None


def sma_stack(close: float, s20, s50, s200) -> str:
    """'close > 20 > 50 > 200' / '50 > 200' / 'under the 200-day' / 'mixed' (or 'short history')."""
    if None in (s20, s50, s200):
        return "short history"
    if close > s20 > s50 > s200:
        return "close > 20 > 50 > 200"
    if close < s200:
        return "under the 200-day"
    return "50 > 200" if s50 > s200 else "mixed"


# --- the facts ------------------------------------------------------------------------------------------------------------

def _payload(item: Optional[dict]) -> dict:
    """The scan payload of a plan item (its `payload`) or the item itself (scan rows, orders)."""
    item = item or {}
    return {**item, **(item.get("payload") or {})} if isinstance(item.get("payload"), dict) else dict(item)


def _industry(symbol: str) -> Optional[str]:
    with get_db() as conn:
        r = conn.execute("SELECT industry FROM universe_snapshots WHERE symbol = ? AND industry IS NOT NULL AND industry != '' "
                         "ORDER BY snapshot_date DESC LIMIT 1", (symbol,)).fetchone()
    return r[0] if r else None


def industry_move(symbol: str, as_of: str, sessions: int = 5) -> Optional[dict]:
    """{industry, pct, n}: the average move of the industry's other stocks over the last `sessions` sessions."""
    ind = _industry(symbol)
    if not ind:
        return None
    with get_db() as conn:
        snap = conn.execute("SELECT MAX(snapshot_date) FROM universe_snapshots").fetchone()[0]
        peers = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM universe_snapshots WHERE snapshot_date = ? AND industry = ? "
                                            "AND symbol != ?", (snap, ind, symbol))]
    moves = []
    for p in peers[:60]:
        b = _bars(p, as_of, sessions + 1)
        if len(b) == sessions + 1 and b[0]["close"]:
            moves.append((b[-1]["close"] / b[0]["close"] - 1) * 100)
    return {"industry": ind, "pct": round(sum(moves) / len(moves), 2), "n": len(moves)} if moves else {"industry": ind, "pct": None, "n": 0}


def _market(as_of: str) -> Optional[str]:
    from backend.engine import market
    with get_db() as conn:
        h = market.latest(conn, as_of)
    return h["status"] if h else None


def _results(symbol: str, as_of: str) -> Optional[dict]:
    try:
        from backend.ingest import events
        return events.next_results(symbol, as_of)
    except Exception:
        return None


def _recent_notes(symbol: str, as_of: str, n: int = 3) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT day, kind, severity, title FROM ai_notes WHERE symbol = ? AND kind IN ('filing', 'deal') AND day <= ? "
                            "ORDER BY id DESC LIMIT ?", (symbol, as_of, n)).fetchall()
    return [dict(r) for r in rows]


def _held_facts(held: Optional[dict], close: Optional[float], as_of: str, late_at: float) -> Optional[dict]:
    """entry, stop, distance to the stop, days held, trail armed: the position facts of a held name."""
    if not held:
        return None
    entry, stop = held.get("entry"), held.get("stop")
    best = held.get("best") or close
    d = held.get("d") or held.get("in")
    days = held.get("days")
    if days is None and d:
        days = (date.fromisoformat(as_of) - date.fromisoformat(d[:10])).days
    return {"book": held.get("book"), "entry": _r(entry), "stop": _r(stop),
            "dist_to_stop_pct": round((close - stop) / close * 100, 2) if close and stop else None, "days_held": days,
            "trail_armed": bool(held.get("trail_active") or (entry and best and late_at and best >= entry * (1 + late_at / 100)))}


def features(symbol: str, as_of: str, item: Optional[dict] = None, held: Optional[dict] = None, source: str = "plan",
             late_at: float = 100.0) -> dict:
    """Every fact the model gets for one name, computed by code: the payload keys, the SMA 20/50/200 stack and the
    distance to the 50 / 200, ATR(14) %, the 20-day volume average, the last 5 sessions' volume ratio, the dry-up, the
    market filter, the industry and its 5-session move, the next results date, the last 3 AI notes, the option chain
    ("no chain": the stored chain is NIFTY's only) and, for a held name, the position facts."""
    p = _payload(item)
    bars = _bars(symbol, as_of)
    closes = [b["close"] for b in bars]
    close = closes[-1] if closes else p.get("close")
    f = {"symbol": symbol, "as_of": bars[-1]["date"] if bars else as_of, "source": source}
    f.update({k: _r(p[k]) for k in PLAN_KEYS if p.get(k) is not None})
    f["close"] = _r(close)
    s20, s50, s200 = _sma(closes, 20), _sma(closes, 50), _sma(closes, 200)
    f.update(sma20=_r(s20), sma50=_r(s50), sma200=_r(s200), sma_stack=sma_stack(close or 0, s20, s50, s200) if close else "short history",
             dist_sma50_pct=_pct(close, s50), dist_sma200_pct=_pct(close, s200), atr_pct=atr_pct(bars))
    vols = [b["volume"] for b in bars]
    v20 = _avg(vols[-20:]) if len(vols) >= 20 else None
    f["vol_avg20"] = round(v20) if v20 else None
    f["vol_ratio5"] = round(_avg(vols[-5:]) / v20, 2) if v20 else None
    f["dry_up_pct"] = dry_up(bars)
    if p.get("stop") is not None:
        f["stop"] = _r(p["stop"])
    f["market"] = _market(as_of)
    ind = industry_move(symbol, as_of)
    f["industry"] = ind["industry"] if ind else None
    f["industry_move5_pct"] = ind["pct"] if ind else None
    r = _results(symbol, as_of)
    f["results_date"], f["results_in"] = (r["date"], r["sessions"]) if r else (None, None)
    f["notes"] = [f"{n['day']} {n['kind']} ({n['severity']}): {n['title']}" for n in _recent_notes(symbol, as_of)]
    f["chain"] = "no chain"
    f["held"] = _held_facts(held, close, as_of, late_at)
    return f


# --- the candidate levels -----------------------------------------------------------------------------------------------------

def round_step(price: float) -> int:
    """The round-number step by price: 5 under 200, 10 under 1,000, 50 under 5,000, else 100."""
    return 5 if price < 200 else 10 if price < 1000 else 50 if price < 5000 else 100


def swings(bars: list[dict], k: int = PIVOT_BARS) -> tuple[list[dict], list[dict]]:
    """(swing lows, swing highs): bars whose low (high) is the lowest (highest) of the 2k+1 bars around them."""
    lows, highs = [], []
    for i in range(k, len(bars) - k):
        win = bars[i - k:i + k + 1]
        if bars[i]["low"] == min(b["low"] for b in win):
            lows.append(bars[i])
        if bars[i]["high"] == max(b["high"] for b in win):
            highs.append(bars[i])
    return lows, highs


def _vwap(symbol: str, day: str, bar: Optional[dict]) -> Optional[float]:
    """The breakout day's VWAP from the 1-minute store, else the day's typical price (H+L+C)/3."""
    try:
        from backend.intraday import store
        with get_db() as conn:
            inst = find_instrument(conn, symbol, "NSE")
        bars = store.load(inst["instrument_token"], day) if inst else None
        vol = sum(b.get("volume") or 0 for b in bars or [])
        if bars and vol:
            return sum((b["high"] + b["low"] + b["close"]) / 3 * (b.get("volume") or 0) for b in bars) / vol
    except Exception:
        pass
    return (bar["high"] + bar["low"] + bar["close"]) / 3 if bar else None


def _label_day(d: str) -> str:
    x = date.fromisoformat(d)
    return f"{x.day} {x.strftime('%b')}"


def levels(symbol: str, as_of: str, item: Optional[dict] = None) -> list[dict]:
    """Up to 10 candidate levels {price, kind, label} nearest the close, highest first: the swing lows / highs of the
    last 120 sessions (5-bar pivots), the base floor (lowest low since the base began) and the pivot, the 50 / 200-day
    averages, the nearest round number and the breakout day's VWAP."""
    p = _payload(item)
    bars = _bars(symbol, as_of, 260)
    if not bars:
        return []
    close = bars[-1]["close"]
    recent = bars[-SWING_SESSIONS:]
    lows, highs = swings(recent)
    out = [{"price": b["low"], "kind": "swing_low", "label": f"swing low {_label_day(b['date'])}"} for b in lows]
    out += [{"price": b["high"], "kind": "swing_high", "label": f"swing high {_label_day(b['date'])}"} for b in highs]
    if p.get("pivot"):
        out.append({"price": p["pivot"], "kind": "pivot", "label": "pivot"})
    if p.get("base_weeks"):
        span = bars[-int(p["base_weeks"] * 5) - 1:]
        out.append({"price": min(b["low"] for b in span), "kind": "base_floor", "label": "base floor"})
    closes = [b["close"] for b in bars]
    for n in (50, 200):
        s = _sma(closes, n)
        if s:
            out.append({"price": s, "kind": f"sma{n}", "label": f"{n}-day average"})
    step = round_step(close)
    out.append({"price": float(round(close / step) * step), "kind": "round", "label": "round number"})
    bd = p.get("breakout_date")
    if bd:
        bar = next((b for b in bars if b["date"] == bd), None)
        v = _vwap(symbol, bd, bar)
        if v:
            out.append({"price": v, "kind": "vwap", "label": "breakout-day VWAP"})
    seen, uniq = set(), []
    for c in sorted(out, key=lambda c: abs(c["price"] - close)):
        px = round(c["price"], 2)
        if px > 0 and px not in seen:
            seen.add(px)
            uniq.append({**c, "price": px})
    return sorted(uniq[:MAX_CANDIDATES], key=lambda c: -c["price"])


def snap(price, cands: list[dict]) -> Optional[dict]:
    """The candidate a picked price matches (within 0.2%), else None."""
    try:
        x = float(price)
    except (TypeError, ValueError):
        return None
    best = min(cands, key=lambda c: abs(c["price"] - x), default=None)
    return best if best and abs(best["price"] - x) <= best["price"] * LEVEL_TOL else None


# --- memory: the previous view and the calibration note ------------------------------------------------------------------

def previous(symbol: str, before_day: str) -> Optional[dict]:
    """The stock's last ok view before `before_day` (any source)."""
    with get_db() as conn:
        r = conn.execute("SELECT * FROM ai_views WHERE symbol = ? AND day < ? AND status = 'ok' ORDER BY day DESC, id DESC LIMIT 1",
                         (symbol, before_day)).fetchone()
    return _row(r) if r else None


def since(prev: dict, f: dict) -> dict:
    """What happened since the previous view: the close change %, whether support_1 held (no close under it), the
    volume ratio now vs then, the new notes."""
    pf = prev.get("features") or {}
    s1 = (prev.get("levels") or {}).get("support_1")
    closes = [b["close"] for b in _after(f["symbol"], prev["day"], 60) if b["date"] <= f["as_of"]]
    with get_db() as conn:
        new_notes = conn.execute("SELECT COUNT(*) FROM ai_notes WHERE symbol = ? AND kind IN ('filing', 'deal') AND day > ?",
                                 (f["symbol"], prev["day"])).fetchone()[0]
    return {"close_change_pct": _pct(f.get("close"), pf.get("close")),
            "support_1": s1, "support_1_held": (min(closes) >= s1) if s1 and closes else None,
            "vol_ratio_now": f.get("vol_ratio5"), "vol_ratio_then": pf.get("vol_ratio5"), "new_notes": new_notes}


def previous_block(prev: Optional[dict], f: dict) -> str:
    """The `previous view` lines of the prompt (empty without one)."""
    if not prev:
        return ""
    s = since(prev, f)
    held = {True: "held", False: "broke", None: "not tested"}[s["support_1_held"]]
    lines = ["Previous view (context, not an anchor):",
             f"- {prev['day']}: score {prev['score']} · {prev['verdict']} · " + "; ".join(prev.get("reasons") or []),
             f"- since then: close {s['close_change_pct']:+.2f}%" if s["close_change_pct"] is not None else "- since then: close change unknown"]
    if s["support_1"]:
        lines.append(f"- support 1 {s['support_1']} {held}")
    lines.append(f"- volume ratio now {s['vol_ratio_now']} vs {s['vol_ratio_then']} then · new notes {s['new_notes']}")
    return "\n".join(lines)


def calibration_note() -> Optional[str]:
    """The short paragraph the model gets about its own record (updated nightly); None before any outcome."""
    with get_db() as conn:
        return get_setting(conn, CALIB_KEY) or None


def build_calibration(rec: Optional[dict] = None) -> Optional[str]:
    """From the record: hit rates by verdict, in results weeks, by volume-ratio band; None before a scored outcome."""
    rec = rec or record()
    bv, base = rec["by_verdict"], rec["baseline"]
    if not any(v["hit20"] is not None for v in bv.values()):
        return None
    bits = [f"your {k} verdicts hit {v['hit20']}% at 20 sessions ({v['votes']} votes)" for k, v in bv.items() if v["hit20"] is not None]
    if base["hit20"] is not None:
        bits.append(f"every plan name hit {base['hit20']}%")
    for b in rec.get("buckets") or []:
        if b["hit20"] is not None and b["votes"] >= MIN_BUCKET:
            bits.append(f"your buy verdicts {b['name']} hit {b['hit20']}%")
    return "; ".join(bits)[:600] + "."


# --- the prompt and the contract ------------------------------------------------------------------------------------------------

def facts_text(f: dict) -> str:
    """The facts as prompt lines ("- key: value"); the figures here are the only ones a reason may quote."""
    skip = ("symbol", "source", "notes", "held")
    lines = [f"- {k.replace('_', ' ')}: {v}" for k, v in f.items() if k not in skip and v is not None]
    lines += [f"- note: {n}" for n in f.get("notes") or []]
    for k, v in (f.get("held") or {}).items():
        if v is not None:
            lines.append(f"- held {k.replace('_', ' ')}: {v}")
    return "\n".join(lines)


def cands_text(cands: list[dict]) -> str:
    return "\n".join(f"- {c['price']} · {c['label']}" for c in cands)


def build_prompt(f: dict, cands: list[dict], prev: Optional[dict], calib: Optional[str]) -> str:
    """The user prompt: what it is, the facts, the candidate levels, then the previous view (after the facts) and the
    calibration note."""
    held = bool(f.get("held"))
    kind = "held name (verdict hold / trim / exit-watch)" if held else "candidate (verdict buy / wait / avoid)"
    parts = [f"Stock: {f['symbol']} · {f['source']} · {kind}", "Facts:", facts_text(f),
             "Candidate levels (pick only from these prices):", cands_text(cands) or "- none"]
    pb = previous_block(prev, f)
    if pb:
        parts.append(pb)
    if calib:
        parts.append("Calibration (your record so far): " + calib)
    parts.append("Reply with the JSON object.")
    return "\n".join(parts)


def _text(v, n: int, what: str) -> str:
    s = " ".join(str(v or "").split())
    if len(s) > n:
        raise ViewInvalid(f"{what} longer than {n} characters")
    return s


def _num(t: str) -> Optional[float]:
    try:
        return float(t.replace(",", ""))
    except ValueError:
        return None


def _figures_ok(text: str, allowed: set) -> bool:
    """Every figure (more than one digit) in `text` is a figure of the facts, written any way (490 = 490.0 = 490.00)."""
    nums = {_num(a) for a in allowed}
    return all(len(t) < 2 or _num(t) in nums for t in notes._DIGITS.findall(text))


def validate(raw: str, ctx: dict) -> dict:
    """The contract checked by code (ctx: facts, cands, close, held, has_prev): JSON object; score 0–100; the verdict
    in the right set; confidence 0–1; 1–3 reasons ≤ 60 chars whose figures are all in the facts; change ≤ 100 (null
    without a previous view); would_change ≤ 120; support_1 and fails_below (and support_2 / resistance when given) at
    a candidate's price, fails_below ≤ the close; no broker name. Prices are snapped to the candidate. Raises ViewInvalid."""
    try:
        d = json.loads(raw)
    except ValueError as exc:
        raise ViewInvalid(f"not JSON: {str(raw)[:60]!r}") from exc
    if not isinstance(d, dict):
        raise ViewInvalid("not a JSON object")
    try:
        score = float(d.get("score"))
    except (TypeError, ValueError):
        raise ViewInvalid("score missing")
    if not 0 <= score <= 100:
        raise ViewInvalid("score must be 0-100")
    allowed_v = HELD_VERDICTS if ctx["held"] else CANDIDATE_VERDICTS
    vm = str(d.get("verdict") or "").strip().lower().replace(" ", "-")
    if vm not in allowed_v:
        raise ViewInvalid(f"verdict must be one of {', '.join(allowed_v)}")
    try:
        conf = float(d.get("confidence"))
    except (TypeError, ValueError):
        raise ViewInvalid("confidence missing")
    if not 0 <= conf <= 1:
        raise ViewInvalid("confidence must be 0-1")
    reasons = d.get("reasons")
    if not isinstance(reasons, list) or not reasons or len(reasons) > REASONS_MAX:
        raise ViewInvalid(f"reasons must be 1-{REASONS_MAX} phrases")
    allowed = notes._digit_tokens(ctx["facts"] + " " + cands_text(ctx["cands"]))
    reasons = [_text(r, REASON_LEN, "a reason") for r in reasons]
    change = _text(d.get("change"), CHANGE_LEN, "change") or None if ctx.get("has_prev") else None
    would = _text(d.get("would_change"), WOULD_LEN, "would_change")
    for t in [*reasons, change or "", would]:
        if not _figures_ok(t, allowed):
            raise ViewInvalid("a figure that is not in the facts")
        w = notes._blocked_word(t)
        if w:
            raise ViewInvalid(f"names a broker ({w[:3]}…)")
    lv = d.get("levels") if isinstance(d.get("levels"), dict) else {}
    out_lv = {}
    for k, need in (("support_1", True), ("support_2", False), ("resistance", False), ("fails_below", True)):
        v = lv.get(k)
        if v in (None, "", "null"):
            if need:
                raise ViewInvalid(f"levels.{k} missing")
            out_lv[k] = None
            continue
        c = snap(v, ctx["cands"])
        if not c:
            raise ViewInvalid(f"levels.{k} {v} is not one of the candidate prices")
        out_lv[k] = c["price"]
    if ctx.get("close") and out_lv["fails_below"] > ctx["close"]:
        raise ViewInvalid("fails_below must be below the close")
    return {"score": int(round(score)), "verdict_model": vm, "confidence": round(conf, 2), "reasons": reasons, "change": change,
            "would_change": would, "levels": out_lv}


def verdict_for(score: int, held: bool, config: AppConfig, close: Optional[float] = None, fails_below: Optional[float] = None) -> str:
    """The verdict by code: candidates buy ≥ view_score_buy, avoid < view_score_avoid, else wait; held names hold ≥
    buy, exit-watch < avoid OR a close under fails_below, else trim."""
    hi, lo = config.ai.view_score_buy, config.ai.view_score_avoid
    if held:
        if score < lo or (close is not None and fails_below is not None and close < fails_below):
            return "exit-watch"
        return "hold" if score >= hi else "trim"
    return "buy" if score >= hi else "avoid" if score < lo else "wait"


# --- one rating -------------------------------------------------------------------------------------------------------------------

def _generate(config: AppConfig, system: str, user: str) -> dict:
    c = config.ai
    return client.generate(c.night_model, user, system=system, json_mode=True, timeout=float(c.timeout_s), keep_alive=c.keep_alive,
                           options=GEN_OPTIONS)


def ask_model(config: AppConfig, user: str, ctx: dict) -> tuple[dict, dict]:
    """(validated answer, generate result); one retry naming the fault, then ViewInvalid."""
    r = _generate(config, SYSTEM_PROMPT, user)
    try:
        return validate(r["text"], ctx), r
    except ViewInvalid as exc:
        r2 = _generate(config, SYSTEM_PROMPT + RETRY_SUFFIX.format(why=str(exc)[:120]), user)
        r2["tokens"], r2["ms"] = r2["tokens"] + r["tokens"], r2["ms"] + r["ms"]
        return validate(r2["text"], ctx), r2


def _late_at(config: AppConfig) -> float:
    return float(getattr(config.breakout, "late_at", 100.0) or 100.0)


def prepare(it: dict, day: str, config: AppConfig) -> dict:
    """{f, cands, prev, user, ctx}: everything one rating needs (code only)."""
    f = features(it["symbol"], day, it.get("item"), it.get("held"), it["source"], _late_at(config))
    cands = levels(it["symbol"], day, it.get("item"))
    prev = previous(it["symbol"], day)
    user = build_prompt(f, cands, prev, calibration_note())
    ctx = {"facts": facts_text(f) + "\n" + previous_block(prev, f), "cands": cands, "close": f.get("close"), "held": bool(f.get("held")), "has_prev": bool(prev)}
    return {"f": f, "cands": cands, "prev": prev, "user": user, "ctx": ctx}


def rate(config: AppConfig, it: dict, day: str) -> dict:
    """One name rated: the model's answer validated, the verdict by code, one row (status ok, or failed after the
    retry: no rating shown, the record counts it). Returns the row."""
    p = prepare(it, day, config)
    base = {"day": day, "symbol": it["symbol"], "source": it["source"], "book": it.get("book"), "rank_in_source": it.get("rank"),
            "candidates": p["cands"], "features": p["f"], "previous_id": (p["prev"] or {}).get("id")}
    try:
        a, r = ask_model(config, p["user"], p["ctx"])
    except (ViewInvalid, client.AIError) as exc:
        logger.warning("view: no usable rating for %s (%s)", it["symbol"], exc)
        return save({**base, "status": "failed", "error": str(exc)[:200], "model": config.ai.night_model, "ms": None})
    v = verdict_for(a["score"], p["ctx"]["held"], config, p["f"].get("close"), a["levels"]["fails_below"])
    return save({**base, **a, "verdict": v, "status": "ok", "model": r["model"], "ms": r["ms"], "tokens": r["tokens"]})


# --- rows -----------------------------------------------------------------------------------------------------------------------------

COLS = ("created", "day", "symbol", "source", "book", "rank_in_source", "score", "verdict", "verdict_model", "confidence", "reasons",
        "change", "would_change", "levels", "candidates", "features", "previous_id", "model", "prompt_version", "ms", "status")
JSON_COLS = ("reasons", "levels", "candidates", "features")


def save(v: dict) -> dict:
    """Insert the view (UNIQUE (day, symbol, source)); a failed row of the same key is overwritten in place, an ok
    row is kept as it is. Returns the stored row."""
    vals = {**{k: v.get(k) for k in COLS}, "created": now_ist(), "prompt_version": PROMPT_V}
    for k in JSON_COLS:
        vals[k] = json.dumps(v.get(k)) if v.get(k) is not None else None
    if v.get("error"):
        vals["features"] = json.dumps({**(v.get("features") or {}), "error": v["error"]})
    with get_db() as conn:
        old = conn.execute("SELECT id, status FROM ai_views WHERE day = ? AND symbol = ? AND source = ?", (v["day"], v["symbol"], v["source"])).fetchone()
        if old and old["status"] == "ok":
            vid = old["id"]
        elif old:
            sets = ", ".join(f"{k} = ?" for k in vals)
            conn.execute(f"UPDATE ai_views SET {sets} WHERE id = ?", (*vals.values(), old["id"]))
            vid = old["id"]
        else:
            vid = conn.execute(f"INSERT INTO ai_views ({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})", tuple(vals.values())).lastrowid
    return {**get(vid), "tokens": v.get("tokens") or 0}


def _row(r) -> dict:
    d = dict(r)
    for k in (*JSON_COLS, "outcome"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    return d


def get(vid: int) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM ai_views WHERE id = ?", (vid,)).fetchone()
    return _row(r) if r else None


def rows(where: str = "", args: tuple = ()) -> list[dict]:
    with get_db() as conn:
        return [_row(r) for r in conn.execute(f"SELECT * FROM ai_views{where} ORDER BY day, id", args)]


# --- the night: scope, order, cap -----------------------------------------------------------------------------------------------------

def _plan_items() -> list[dict]:
    """Tomorrow's plan: the evening plan's items, then the Real BUYs, then the Paper BUYs not in either."""
    from backend.alerts import plan as pl
    out = [{"symbol": i["symbol"], "source": "plan", "book": None, "item": i} for i in pl.latest_plan()[1]]
    try:
        from backend.engine import real
        out += [{"symbol": o["symbol"], "source": "plan", "book": "real", "item": o}
                for o in (real._state() or {}).get("orders", []) if o["action"] == "BUY" and not o.get("sent")]
    except Exception:
        logger.exception("view: real plan")
    try:
        from backend.engine import paper
        for st in paper._states().values():
            out += [{"symbol": o["symbol"], "source": "plan", "book": "paper", "item": o} for o in st.get("orders", []) if o.get("action") == "BUY"]
    except Exception:
        logger.exception("view: paper plan")
    return out


def _held_items() -> list[dict]:
    """Every held name (Real, Paper, Futures), with the position facts."""
    out = []
    try:
        from backend.engine import real
        for s, x in ((real._state() or {}).get("positions") or {}).items():
            out.append({"symbol": s, "source": "held", "book": "real", "held": {"book": "real", "entry": x["px"], "stop": x.get("stop"), "best": x.get("best"),
                                                                                "d": x.get("d"), "days": x.get("days")}})
    except Exception:
        logger.exception("view: real positions")
    try:
        from backend.engine import paper
        for st in paper._states().values():
            for s, x in (st.get("positions") or {}).items():
                out.append({"symbol": s, "source": "held", "book": "paper", "held": {"book": "paper", "entry": x["px"], "stop": x.get("stop"),
                                                                                     "best": x.get("best"), "d": x.get("d"), "days": x.get("days")}})
    except Exception:
        logger.exception("view: paper positions")
    try:
        from backend.futures import book
        for a in ((book._state() or {}).get("accounts") or {}).values():
            for s, x in (a.get("positions") or {}).items():
                out.append({"symbol": s, "source": "held", "book": "futures", "held": {"book": "futures", "entry": x.get("entry"), "stop": x.get("stop"),
                                                                                       "best": x.get("best"), "d": x.get("d"), "days": x.get("days")}})
    except Exception:
        logger.exception("view: futures positions")
    return out


def _futures_items() -> list[dict]:
    try:
        from backend.futures import book
        plan = (book._state() or {}).get("plan") or {}
        return [{"symbol": o["symbol"], "source": "futures", "book": "futures", "item": o} for s in ("long", "short") for o in plan.get(s, [])]
    except Exception:
        logger.exception("view: futures plan")
        return []


def _scan_rows(day: str) -> tuple[Optional[str], list[tuple[str, str, dict]]]:
    with get_db() as conn:
        as_of = conn.execute("SELECT MAX(as_of) FROM scan_runs WHERE as_of <= ?", (day,)).fetchone()[0]
        rs = conn.execute("SELECT screen, stage, payload FROM scan_results WHERE as_of = ? AND stage IN ('fresh', 'forming')",
                          (as_of,)).fetchall() if as_of else []
    return as_of, [(r["screen"], r["stage"], json.loads(r["payload"])) for r in rs]


def _follow_items(day: str) -> list[dict]:
    """The day's Follow breakouts: fresh rows whose breakout is on the scan's day."""
    as_of, rs = _scan_rows(day)
    return [{"symbol": p["symbol"], "source": "follow", "book": None, "item": {**p, "stage": "breakout"}}
            for scr, st, p in rs if st == "fresh" and p.get("breakout_date") == as_of]


def _screen_items(day: str) -> list[dict]:
    """Each screen's evening results, stage breakout (fresh) then ready (forming), top TOP_PER_SCREEN by score."""
    _, rs = _scan_rows(day)
    out = []
    for scr in sorted({r[0] for r in rs}):
        for st in SCREEN_STAGES:
            ps = sorted((p for s, g, p in rs if s == scr and g == st), key=lambda p: -(p.get("score") or 0))[:TOP_PER_SCREEN]
            out += [{"symbol": p["symbol"], "source": f"screen:{scr}", "book": None, "item": {**p, "stage": SCREEN_STAGES[st]}} for p in ps]
    return out


def scope(config: AppConfig, day: str) -> list[dict]:
    """The night's names in order: plan, held (when view_rate_held), futures plan, Follow breakouts, Screens; one per
    symbol (the first source wins), rank_in_source numbered within each source."""
    groups: list[Callable[[], list]] = [_plan_items]
    if config.ai.view_rate_held:
        groups.append(_held_items)
    groups += [_futures_items, lambda: _follow_items(day), lambda: _screen_items(day)]
    seen, out, rank = set(), [], {}
    for g in groups:
        try:
            items = g()
        except Exception:
            logger.exception("view: a scope source failed")
            continue
        for it in items:
            if it["symbol"] in seen:
                continue
            seen.add(it["symbol"])
            rank[it["source"]] = rank.get(it["source"], 0) + 1
            out.append({**it, "rank": rank[it["source"]]})
    return out


def _done(day: str) -> set:
    with get_db() as conn:
        return {(r[0], r[1]) for r in conn.execute("SELECT symbol, source FROM ai_views WHERE day = ? AND status = 'ok'", (day,))}


def rate_night(config: AppConfig, day: str) -> dict:
    """Rate tonight's names in order within view_max_per_night: {rated: [rows], failed, capped, tokens, ms_total}."""
    done = _done(day)
    todo = [it for it in scope(config, day) if (it["symbol"], it["source"]) not in done]
    cap = max(0, int(config.ai.view_max_per_night))
    out = {"rated": [], "failed": 0, "capped": max(0, len(todo) - cap), "tokens": 0, "ms_total": 0}
    for it in todo[:cap]:
        row = rate(config, it, day)
        out["tokens"] += row.get("tokens") or 0
        out["ms_total"] += row.get("ms") or 0
        if row["status"] == "ok":
            out["rated"].append(row)
        else:
            out["failed"] += 1
    return out


# --- outcomes --------------------------------------------------------------------------------------------------------------------------

def outcome_for(v: dict, after: list[dict], late_at: float = 100.0) -> dict:
    """{h5, h10, h20: {pct, hit}} for the horizons whose sessions have passed (pure). pct = close(day+h)/close(day) − 1
    in %; a candidate hits at +3% or when the trail armed (a close late_at % over) before a close under its stop; a held
    name hits when it did not close under fails_below within h."""
    f, lv = v.get("features") or {}, v.get("levels") or {}
    base = f.get("close")
    out = dict(v.get("outcome") or {})
    if not base:
        return out
    held = v["source"] == "held"
    stop = f.get("stop") or lv.get("fails_below")
    fb = lv.get("fails_below")
    for h in HORIZONS:
        if f"h{h}" in out or len(after) < h:
            continue
        win = after[:h]
        pct = round((win[-1]["close"] / base - 1) * 100, 2)
        if held:
            hit = fb is None or all(b["close"] >= fb for b in win)
        else:
            hit = pct >= HIT_PCT
            for b in win:
                if hit:
                    break
                if stop and b["close"] < stop:
                    break
                if late_at and b["close"] >= base * (1 + late_at / 100):
                    hit = True
        out[f"h{h}"] = {"pct": pct, "hit": bool(hit)}
    return out


def score_outcomes(config: AppConfig) -> int:
    """Every ok view missing a horizon whose sessions have passed gets it (scored_at stamped). How many changed."""
    n = 0
    for v in rows(" WHERE status = 'ok' AND (outcome IS NULL OR outcome NOT LIKE '%\"h20\"%')"):
        o = outcome_for(v, _after(v["symbol"], v["day"], max(HORIZONS)), _late_at(config))
        if o != (v.get("outcome") or {}):
            o["scored_at"] = now_ist()
            with get_db() as conn:
                conn.execute("UPDATE ai_views SET outcome = ? WHERE id = ?", (json.dumps(o), v["id"]))
            n += 1
    return n


# --- the shuffle test ---------------------------------------------------------------------------------------------------------------------

def shuffle_test(config: AppConfig, views: list[dict], rng: Optional[random.Random] = None) -> int:
    """~1 in 5 of tonight's views rated again with ANOTHER stock's facts swapped in (same prompt, same name): the score
    is kept as shuffle_score. A model that rates the name, not the facts, scores both alike. How many stored."""
    rng = rng or random.Random()
    pool = [v for v in views if v.get("status") == "ok" and v.get("features")]
    if len(pool) < 2:
        return 0
    n = 0
    for v in pool:
        if rng.random() >= SHUFFLE_SHARE:
            continue
        other = rng.choice([o for o in pool if o["id"] != v["id"]])
        f = {**other["features"], "symbol": v["symbol"], "source": v["source"]}
        user = build_prompt(f, other.get("candidates") or [], None, None)
        try:
            r = _generate(config, SYSTEM_PROMPT, user)
            s = float(json.loads(r["text"]).get("score"))
        except (client.AIError, ValueError, TypeError, AttributeError):
            continue
        if 0 <= s <= 100:
            with get_db() as conn:
                conn.execute("UPDATE ai_views SET shuffle_score = ? WHERE id = ?", (int(round(s)), v["id"]))
            n += 1
    return n


# --- the record and the unlock rule -------------------------------------------------------------------------------------------------------

def _rate(vs: list[dict], h: int = 20) -> Optional[float]:
    hs = [v["outcome"][f"h{h}"]["hit"] for v in vs if (v.get("outcome") or {}).get(f"h{h}")]
    return round(sum(hs) / len(hs) * 100, 1) if hs else None


def _avg_move(vs: list[dict], h: int = 20) -> Optional[float]:
    ps = [v["outcome"][f"h{h}"]["pct"] for v in vs if (v.get("outcome") or {}).get(f"h{h}")]
    return round(sum(ps) / len(ps), 2) if ps else None


def _stats(vs: list[dict]) -> dict:
    return {"votes": len(vs), "hit5": _rate(vs, 5), "hit20": _rate(vs, 20), "avg20": _avg_move(vs, 20)}


def week_of(day: str) -> str:
    y, w, _ = date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


def by_week(vs: list[dict]) -> list[dict]:
    """[{week, from, votes, buy_hit20, avoid_hit20, baseline20, beat}] oldest first; beat = buy > baseline and avoid <
    baseline (None while a figure is missing)."""
    weeks: dict[str, list] = {}
    for v in vs:
        weeks.setdefault(week_of(v["day"]), []).append(v)
    out = []
    for w in sorted(weeks):
        g = weeks[w]
        b, a = _rate([v for v in g if v["verdict"] == "buy"]), _rate([v for v in g if v["verdict"] == "avoid"])
        base = _rate([v for v in g if v["source"] == "plan"])
        monday = date.fromisocalendar(int(w[:4]), int(w[6:]), 1).isoformat()
        beat = None if None in (b, a, base) else (b > base and a < base)
        out.append({"week": w, "from": monday, "votes": len(g), "buy_hit20": b, "avoid_hit20": a, "baseline20": base, "beat": beat})
    return out


def unlock_state(weeks: list[dict]) -> dict:
    """{unlocked, streak, need, text}: the most recent scored weeks in a row that beat the baseline."""
    streak = 0
    for w in reversed([w for w in weeks if w["beat"] is not None]):
        if not w["beat"]:
            break
        streak += 1
    unlocked = streak >= UNLOCK_WEEKS
    text = (f"unlocked: {streak} weeks in a row with buy hit@20 over the plan and avoid under it" if unlocked else
            f"locked: {streak} of {UNLOCK_WEEKS} weeks in a row with buy hit@20 over the plan baseline and avoid hit@20 under it")
    return {"unlocked": unlocked, "streak": streak, "need": UNLOCK_WEEKS, "text": text}


BUCKETS = (("in results weeks", lambda f: f.get("results_in") is not None and f["results_in"] <= 5),
           ("with volume ratio under 1.5", lambda f: (f.get("volume_ratio") or f.get("vol_ratio5") or 0) < 1.5),
           ("with volume ratio 1.5 and over", lambda f: (f.get("volume_ratio") or f.get("vol_ratio5") or 0) >= 1.5),
           ("in a weak market", lambda f: (f.get("market") or "").lower() not in ("", "confirmed uptrend", "uptrend", "full")),
           ("over 5% past the pivot", lambda f: (f.get("gain_from_pivot_pct") or 0) > 5))


def buckets(buys: list[dict]) -> list[dict]:
    return [{"name": name, **_stats([v for v in buys if fn(v.get("features") or {})])} for name, fn in BUCKETS]


LEAN_FEATURES = (("dry_up_pct", "the volume dry-up"), ("rs", "RS"), ("volume_ratio", "the volume ratio"))


def leans(vs: list[dict]) -> dict:
    """{feature, label, by_verdict: {verdict: {dry_up_pct, rs, volume_ratio}}}: the mean of each feature by verdict; the
    feature it leans on most is the one whose buy and avoid means differ most (relative to its overall mean)."""
    byv = {}
    for k in VERDICTS:
        g = [v["features"] or {} for v in vs if v["verdict"] == k]
        if g:
            byv[k] = {f: _r(_avg([x.get(f) for x in g])) for f, _ in LEAN_FEATURES}
    best, gap = None, 0.0
    for f, label in LEAN_FEATURES:
        b, a = (byv.get("buy") or {}).get(f), (byv.get("avoid") or {}).get(f)
        allm = _avg([(v["features"] or {}).get(f) for v in vs])
        if b is None or a is None or not allm:
            continue
        g = abs(b - a) / abs(allm)
        if g > gap:
            best, gap = (f, label), g
    return {"feature": best[0] if best else None, "label": best[1] if best else None, "by_verdict": byv}


def record(weeks: Optional[int] = None, today: Optional[str] = None, score_buy: int = 60) -> dict:
    """The record: per verdict {votes, hit5, hit20, avg20} vs the plan baseline (every plan name rated), by week, the
    shuffle (real vs shuffled buy hit@20), the feature it leans on, the worst bucket, the unlock state, failed / total."""
    allv = rows()
    since_day = (date.fromisoformat(today or now_ist()[:10]) - timedelta(weeks=weeks)).isoformat() if weeks else None
    vs = [v for v in allv if v["status"] == "ok" and (not since_day or v["day"] >= since_day)]
    failed = sum(1 for v in allv if v["status"] == "failed" and (not since_day or v["day"] >= since_day))
    bv = {k: _stats([v for v in vs if v["verdict"] == k]) for k in VERDICTS}
    wk = by_week(vs)
    unlock = unlock_state(by_week([v for v in allv if v["status"] == "ok"]))
    sh = [v for v in vs if v.get("shuffle_score") is not None and (v.get("outcome") or {}).get("h20")]
    hi = score_buy
    real_hit, shuf_hit = _rate([v for v in sh if v["score"] >= hi]), _rate([v for v in sh if v["shuffle_score"] >= hi])
    shuffle = {"n": len(sh), "real_hit20": real_hit, "shuffled_hit20": shuf_hit,
               "mean_gap": _r(_avg([abs(v["score"] - v["shuffle_score"]) for v in sh])),
               "ok": None if real_hit is None or shuf_hit is None else real_hit > shuf_hit}
    bk = buckets([v for v in vs if v["verdict"] == "buy"])
    scored = [b for b in bk if b["hit20"] is not None and b["votes"] >= MIN_BUCKET]
    w = min(scored, key=lambda b: b["hit20"], default=None)
    worst = {"bucket": f"buys {w['name']}", "votes": w["votes"], "hit20": w["hit20"],
             "text": f"buys {w['name']} hit {w['hit20']}% at 20 sessions ({w['votes']} votes)"} if w else None
    return {"weeks": weeks, "by_verdict": bv, "baseline": _stats([v for v in vs if v["source"] == "plan"]), "by_week": wk,
            "shuffle": shuffle, "leans": leans(vs), "worst": worst, "buckets": bk, "unlocked": unlock["unlocked"], "unlock": unlock,
            "failed": failed, "total": len(vs) + failed}


def unlocked() -> bool:
    return unlock_state(by_week(rows(" WHERE status = 'ok'")))["unlocked"]


def record_summary(rec: Optional[dict] = None) -> dict:
    rec = rec or record()
    b, a, base = rec["by_verdict"]["buy"]["hit20"], rec["by_verdict"]["avoid"]["hit20"], rec["baseline"]["hit20"]
    return {"buy_hit20": b, "avoid_hit20": a, "baseline20": base, "edge": round(b - base, 1) if None not in (b, base) else None,
            "unlocked": rec["unlocked"], "shuffle_ok": rec["shuffle"]["ok"]}


# --- the switches -------------------------------------------------------------------------------------------------------------------------

def _truthy(v) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on") if isinstance(v, str) else bool(v)


def check_lock(values: dict, before: dict, is_unlocked: Optional[Callable[[], bool]] = None) -> None:
    """A locked switch (rank ties, size, exit-watch alert) may only go on once the record unlocked it: ValueError."""
    turning_on = [k for k in LOCKED if k in values and _truthy(values[k]) and not before.get(k)]
    if turning_on and not (is_unlocked or unlocked)():
        raise ValueError(f"{', '.join(turning_on)} {LOCKED_NOTE}")


def check_bounds(values: dict) -> None:
    """view_size_floor 0.5–1.0, view_score_avoid < view_score_buy within 0–100, view_max_per_night 1–200: ValueError."""
    if "view_size_floor" in values and not 0.5 <= float(values["view_size_floor"]) <= 1.0:
        raise ValueError("view_size_floor must be 0.5-1.0")
    for k in ("view_score_buy", "view_score_avoid"):
        if k in values and not 0 <= int(values[k]) <= 100:
            raise ValueError(f"{k} must be 0-100")
    if "view_max_per_night" in values and not 1 <= int(values["view_max_per_night"]) <= 200:
        raise ValueError("view_max_per_night must be 1-200")


def apply_settings(config: AppConfig, flags: dict) -> dict:
    """PUT /api/ai/settings: pop and apply the view_ keys (the lock rule and the bounds first); the rest returned."""
    vals = {k: flags.pop(k) for k in list(flags) if k in VIEW_KEYS}
    if not vals:
        return flags
    check_lock(vals, {k: getattr(config.ai, k) for k in LOCKED})
    check_bounds(vals)
    lo, hi = int(vals.get("view_score_avoid", config.ai.view_score_avoid)), int(vals.get("view_score_buy", config.ai.view_score_buy))
    if lo >= hi:
        raise ValueError("view_score_avoid must be under view_score_buy")
    for k, v in vals.items():
        cur = getattr(config.ai, k)
        setattr(config.ai, k, _truthy(v) if isinstance(cur, bool) else type(cur)(v))
    return flags


def switches(config: AppConfig) -> dict:
    c = config.ai
    return {**{k[5:]: getattr(c, k) for k in VIEW_KEYS}, "locked": not unlocked(), "locked_keys": [k[5:] for k in LOCKED],
            "unlock_text": f"{UNLOCK_WEEKS} weeks in a row with buy hit@20 over the plan baseline and avoid hit@20 under it"}


# --- the locked switches' wiring (built now, off) ------------------------------------------------------------------------------------------

def size_mult(score: Optional[float], floor_: float = 0.5) -> float:
    """clip(0.5 + 0.5 × (score − 40) / 40, floor, 1.0): 80+ → 1.0×, 40 → 0.5×; no score → 1.0×. Never above 1.0×."""
    if score is None:
        return 1.0
    m = min(1.0, max(float(floor_), 0.5 + 0.5 * (float(score) - 40) / 40))
    assert m <= 1.0
    return round(m, 3)


def ai_qty(qty: int, score: Optional[float], floor_: float = 0.5) -> int:
    """The BUY quantity by score: the rule quantity × size_mult, whole shares, at least one (never a skip), never more
    than the rule quantity."""
    if qty < 1:
        return qty
    out = max(1, floor(qty * size_mult(score, floor_)))
    assert 1 <= out <= qty
    return out


def score_for(symbol: str, before_day: str) -> Optional[dict]:
    """The latest ok view of a name made before `before_day` (the plan source first): {score, verdict, reasons, ...}."""
    with get_db() as conn:
        r = conn.execute("SELECT * FROM ai_views WHERE symbol = ? AND day < ? AND status = 'ok' ORDER BY day DESC, source != 'plan', id DESC LIMIT 1",
                         (symbol, before_day)).fetchone()
    return _row(r) if r else None


def order_ties(items: list, key: Callable, symbol: Callable, scores: dict) -> list:
    """Re-order only the runs of items with the same rank key by AI score (highest first, unscored last); the order
    between different keys never changes."""
    out, i = [], 0
    while i < len(items):
        j = i
        while j < len(items) and key(items[j]) == key(items[i]):
            j += 1
        run = items[i:j]
        out += sorted(run, key=lambda x: -(scores.get(symbol(x)) if scores.get(symbol(x)) is not None else -1)) if len(run) > 1 else run
        i = j
    return out


def rank_ties(config: AppConfig, items: list, key: Callable, symbol: Callable, day: str) -> list:
    """view_rank_by_score: ties (the same breakout rank) ordered by the AI score; off → unchanged."""
    if not config.ai.view_rank_by_score:
        return items
    try:
        scores = {symbol(x): (score_for(symbol(x), day) or {}).get("score") for x in items}
    except Exception:
        return items
    return order_ties(items, key, symbol, scores)


def exit_watch(config: AppConfig, day: str, notifier=None) -> list[dict]:
    """view_exit_watch_alert: a held name that CLOSED under its previous view's fails_below today sends one alert (a
    flag only: nothing is sold, no stop moves). Off → []."""
    if not config.ai.view_exit_watch_alert:
        return []
    from backend.alerts import messages as msg
    from backend.alerts.dispatch import Notifier
    out = []
    for it in _held_items():
        prev = previous(it["symbol"], day)
        fb = ((prev or {}).get("levels") or {}).get("fails_below")
        b = _bars(it["symbol"], day, 1)
        if not fb or not b or b[-1]["date"] != day or b[-1]["close"] >= fb:
            continue
        x = {"symbol": it["symbol"], "book": it["book"], "close": b[-1]["close"], "fails_below": fb, "day": day, "score": prev.get("score")}
        try:
            (notifier or Notifier(config.alerts)).send(f"{day}|ai-exit-watch|{it['symbol']}", "ai", msg.ai_view_exit_watch(x))
        except Exception:
            logger.exception("view: the exit-watch alert failed")
        out.append(x)
    return out


# --- the night job --------------------------------------------------------------------------------------------------------------------------

def _week_sent(week: str) -> bool:
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM sent_alerts WHERE key = ?", (f"{week}|ai-view-week",)).fetchone() is not None


def last_night() -> Optional[dict]:
    with get_db() as conn:
        raw = get_setting(conn, LAST_NIGHT_KEY)
    return json.loads(raw) if raw else None


def run_night(config: AppConfig, now: Optional[datetime] = None, notifier=None, rng: Optional[random.Random] = None) -> dict:
    """The night job: rate in order within the cap, score the outcomes, rebuild the calibration note, run the shuffle
    test, the exit-watch flags (when on), the nightly count line and, once a week, the record line."""
    from backend.alerts import messages as msg
    from backend.alerts.dispatch import Notifier
    now = now or datetime.now(IST)
    day = notes.night_day(now, config)
    res = rate_night(config, day)
    outcomes = score_outcomes(config)
    rec = record(score_buy=config.ai.view_score_buy)
    calib = build_calibration(rec)
    shuffled = shuffle_test(config, res["rated"], rng)
    by = {}
    for v in res["rated"]:
        by[v["verdict"]] = by.get(v["verdict"], 0) + 1
    night = {"day": day, "n": len(res["rated"]), "by_verdict": by, "failed": res["failed"], "capped": res["capped"], "ms_total": res["ms_total"],
             "outcomes": outcomes, "shuffled": shuffled, "at": now_ist()}
    with get_db() as conn:
        set_setting(conn, CALIB_KEY, calib or "")
        set_setting(conn, LAST_NIGHT_KEY, json.dumps(night))
    flagged = exit_watch(config, day, notifier)
    try:
        notifier = notifier or Notifier(config.alerts)
        if night["n"] or night["failed"]:
            notifier.send(f"{day}|ai-view", "ai", msg.ai_view_night(night))
        week = week_of(day)
        last = [w for w in rec["by_week"] if w["week"] < week and w["beat"] is not None]
        if last and not _week_sent(week):
            notifier.send(f"{week}|ai-view-week", "ai", msg.ai_view_week(last[-1], rec))
    except Exception:
        logger.exception("view: the night message failed")
    return {"tokens": res["tokens"], "note": f"{night['n']} rated · {res['failed']} failed · {res['capped']} over the cap · {outcomes} outcome(s) · "
                                             f"{shuffled} shuffled · {len(flagged)} exit-watch"}


def running() -> bool:
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM ai_jobs WHERE kind = 'night' AND key LIKE ? AND status IN ('queued', 'running') LIMIT 1",
                            (KEY_PREFIX + "%",)).fetchone() is not None


def enqueue_tonight(config: AppConfig, now: Optional[datetime] = None, notifier=None) -> dict:
    """One night job "view:<day>" (FIFO after the suggestions); {id, dedup, key}. Off (`view_enabled` False): {status: off}."""
    now = now or datetime.now(IST)
    if not config.ai.view_enabled:
        return {"status": "off"}
    key = f"{KEY_PREFIX}{notes.night_day(now, config)}"
    job = q.enqueue(config, "night", key, lambda: run_night(config, datetime.now(IST), notifier), model=config.ai.night_model,
                    note="AI view: a rating beside every suggested stock")
    return {**job, "key": key}


# --- reading: the API, the joins, the page ------------------------------------------------------------------------------------------------

def latest(symbols, today: Optional[str] = None) -> dict[str, dict]:
    """{SYM: the latest ok view (every column) + previous: {id, day, score, verdict, reasons} | None}."""
    syms = sorted({s.strip().upper() for s in symbols if s and s.strip()})
    if not syms:
        return {}
    with get_db() as conn:
        rs = conn.execute(f"SELECT * FROM ai_views WHERE status = 'ok' AND symbol IN ({','.join('?' * len(syms))}) ORDER BY day, id", syms).fetchall()
    out: dict[str, dict] = {}
    for r in rs:
        out[r["symbol"]] = _row(r)
    for s, v in out.items():
        p = get(v["previous_id"]) if v.get("previous_id") else None
        v["previous"] = {k: p[k] for k in ("id", "day", "score", "verdict", "reasons")} if p else None
    return out


def _join(v: dict, prev_score: Optional[int]) -> dict:
    return {"score": v["score"], "verdict": v["verdict"], "reasons": v["reasons"] or [], "change": v["change"], "levels": v["levels"],
            "at": v["created"], "day": v["day"], "source": v["source"], "prev_score": prev_score}


def for_symbols(symbols, source: str, today: Optional[str] = None) -> dict[str, dict]:
    """{SYM: the ai_view join}: the latest day's view of the matching source (screen rows: that screen), else any
    source that day; views older than VIEW_DAYS are not shown."""
    syms = sorted({s for s in symbols if s})
    if not syms:
        return {}
    since_day = (date.fromisoformat(today or now_ist()[:10]) - timedelta(days=VIEW_DAYS)).isoformat()
    with get_db() as conn:
        rs = [_row(r) for r in conn.execute(f"SELECT * FROM ai_views WHERE status = 'ok' AND day >= ? AND symbol IN ({','.join('?' * len(syms))}) "
                                            "ORDER BY day, id", (since_day, *syms))]
        prev = {r["id"]: r["score"] for r in conn.execute("SELECT id, score FROM ai_views WHERE id IN (" + ",".join(
            str(int(v["previous_id"])) for v in rs if v.get("previous_id")) + ")")} if any(v.get("previous_id") for v in rs) else {}
    best: dict[str, dict] = {}
    for v in rs:
        cur = best.get(v["symbol"])
        if not cur or v["day"] > cur["day"] or (v["day"] == cur["day"] and v["source"] == source and cur["source"] != source):
            best[v["symbol"]] = v
    return {s: _join(v, prev.get(v.get("previous_id"))) for s, v in best.items()}


def attach(rows_: list, source: str, today: Optional[str] = None) -> list:
    """`ai_view: {score, verdict, reasons, change, levels, at, day, source, prev_score} | None` on every row with a
    "symbol". Never raises (a view is decoration)."""
    for r in rows_:
        if isinstance(r, dict):
            r.setdefault("ai_view", None)
    try:
        found = for_symbols([r.get("symbol") for r in rows_ if isinstance(r, dict)], source, today)
        for r in rows_:
            if isinstance(r, dict) and r.get("symbol") in found:
                r["ai_view"] = found[r["symbol"]]
    except Exception:
        logger.exception("view attach failed")
    return rows_


def summary(config: AppConfig) -> dict:
    """GET /api/ai `view`: {on, last_night: {n, by_verdict, ms_total, at, ...}, record_summary, switches}."""
    try:
        rec = record(score_buy=config.ai.view_score_buy)
        return {"on": bool(config.ai.view_enabled), "last_night": last_night(), "record_summary": record_summary(rec), "switches": switches(config)}
    except Exception:
        logger.exception("view summary failed")
        return {"on": bool(config.ai.view_enabled), "last_night": None, "record_summary": None, "switches": None}


def levels_tool(config: AppConfig, symbol: str) -> dict:
    """🤖 ask `levels(symbol)`: the candidate levels code found and the levels the latest view picked."""
    sym = symbol.upper()
    with get_db() as conn:
        row = conn.execute("SELECT MAX(date) FROM candles_daily").fetchone()
    as_of = (row[0] if row and row[0] else now_ist()[:10])
    v = latest([sym]).get(sym)
    return {"symbol": sym, "as_of": as_of, "candidates": levels(sym, as_of), "picked": (v or {}).get("levels"),
            "view": {k: v[k] for k in ("day", "score", "verdict", "reasons")} if v else None}
