"""💡🤖 AI on Tips: the evening review, the call grade, the pre-flight read and the weekly channel profile.

The rules (the same as every AI feature here): every number comes from code; the local model writes sentences only and
every sentence it wrote carries the [AI] tag; nothing leaves the server (the local model behind backend/ai/queue.py);
model output is never deleted (tips_ai rows; the archive job MOVES rows older than 180 days); and nothing here places,
modifies, cancels, holds or blocks an order: the pre-flight read is queued and the arm goes on at once.

  review     (tips.ai_review) at the 3:35 PM day check, and at the 6:35 PM evening run for a hint closed after that:
             every hint that had a Kite position today and is closed (a dry run labelled; paper-only hints skipped).
             The input is code: the trade facts, the ticks summary, the order rows, the follow-ups with the action and
             the AI second opinion, the channel's own result, the plan. 5 labelled lines (entry / management / exit /
             channel / one thing). One Telegram message a day after the last review of the day (hidden when nothing
             was taken).
  grade      (tips.ai_grade) at ingest of a new call, before any arming; again ONCE when the plan changes before the
             entry. The grade A–D (or "–") is computed by code from the channel's record for calls like this (same
             symbol + side + hour band over 60 days, falling back to the symbol, then all), the shape (reward / risk
             from the plan) and the wording (Btst / carry, "if comes", stop missing); the model words one "read:" line.
             Informational: auto ignores it.
  preflight  (tips.ai_preflight, default off) from kite_orders.place_entry once every guard passed: the raw post, the
             decoded plan and the contract are queued for a 2-line read; the arm is NOT held. "differs" → one Telegram
             line + an amber Home item; a flag → an amber line on the card only.
  weekly     (tips.ai_weekly) Sunday 7:05 PM per channel with ≥ 1 call in the week just ended (and the Track tab's
             "Write now", one per hour): the week's numbers by code (the four boxes and their bars) and 4 [AI] lines.

Every read goes through the AI queue with json_mode False; the reply is split into lines, each must start with a known
label and pass the figure check against the input (typed tokens); failing lines are dropped; zero good lines → "[AI] no
usable read" and the raw reply stays in lines_json."""

import json
import re
import statistics
from datetime import date, datetime, time, timedelta
from typing import Callable, Optional

from backend.config import IST
from backend.database.models import get_db, get_setting, set_setting
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm
from backend.tips import book as bk
from backend.tips import plan as pl

logger = setup_logger("tips")

KINDS = ("review", "grade", "preflight", "weekly")
PROMPT_V = {"review": "tips-review-v1", "grade": "tips-grade-v1", "preflight": "tips-preflight-v1", "weekly": "tips-weekly-v1"}
LABELS = {"review": ("entry", "management", "exit", "channel", "one thing"), "grade": ("read",),
          "preflight": ("plan vs post", "flag"), "weekly": ("works", "fails", "your exits", "follow-ups")}
WORDS = {"review": 30, "grade": 30, "preflight": 40, "weekly": 35}
MIN_WORDS = 3                                       # a line is a sentence, not a bare value ("none", "1", "3 calls out of 9")
MIN_CONTENT = 2                                     # … with at least two content words (not numbers, labels or fillers)
FILLER = {"calls", "call", "none", "out", "the", "and", "total", "agreement", "fill", "with", "for", "from", "that", "this",
          "not", "yes", "n/a", "nil"}
SHORT_OK = {("preflight", "flag"): ("none",)}        # the one legal short answer


def _is_sentence(text: str) -> bool:
    words = text.split()
    content = [w for w in (re.sub(r"[^A-Za-z-]", "", x).lower() for x in words) if len(w) >= 3 and w not in FILLER]
    return len(words) >= MIN_WORDS and len(content) >= MIN_CONTENT
NIGHT = ("review", "weekly")                       # the night model; grade and preflight use the day model
SWITCH = {"review": "ai_review", "grade": "ai_grade", "preflight": "ai_preflight", "weekly": "ai_weekly"}
QKIND = "tips_ai"
NO_READ = "no usable read"

EVENING_JOB, EVENING_TIME = "tips_ai_evening", time(18, 35)
WEEKLY_JOB, WEEKLY_TIME = "tips_ai_weekly", time(19, 5)
WEEKLY_NOW_KEY, WEEKLY_NOW_GAP_S = "tips.ai.weekly_now", 3600
REVIEW_TG_KEY = "tips.ai.review_tg."               # + day: the evening Telegram went out

# the grade, by code: thresholds in one place
GRADE = {"days": 60, "min_calls": 3, "A": {"win": 60.0, "rr": 1.5}, "B": {"win": 50.0, "rr": 1.2}}
GRADE_TONE = {"A": "ok", "B": "ok", "C": "w", "D": "bad", "–": ""}
BANDS = (("b1", "9:15–10:30", time(10, 30)), ("b2", "10:30–12", time(12, 0)), ("b3", "12–2 PM", time(14, 0)), ("b4", "after 2 PM", time(23, 59, 59)))
WORDING = (("carry", re.compile(r"\b(btst|stbt|carry|positional|overnight)\b", re.I), "the post says carry / Btst"),
           ("if_comes", re.compile(r"\b(if|when)\s+(it\s+)?comes?\b", re.I), 'the post says "if comes"'))

SYSTEM = {
    "review": ("You review one finished trade: a Telegram channel's call that a trading app followed. Write exactly 5 lines, plain "
               "text, no markdown, each line starting with its label: 'entry:', 'management:', 'exit:', 'channel:', 'one thing:'. "
               "At most 30 words a line. Use only figures that appear in the input; never invent a number. Describe what "
               "happened and why; never advise buying or selling anything."),
    "grade": ("A trading app graded a Telegram channel's new call by code. Write ONE line, plain text, starting with 'read:', at "
              "most 30 words, that explains the grade from the numbers given. Use only figures that appear in the input. The "
              "grade is final; never advise buying or selling."),
    "preflight": ("Compare a Telegram channel's trade post with the plan a trading app decoded from it, before the app arms it. "
                  "Write exactly 2 lines, plain text: 'plan vs post: <what the plan is> agrees' or 'plan vs post: <what the plan "
                  "is> differs: <why>', then 'flag: <one thing worth a look>' or 'flag: none'. At most 40 words a line. Use "
                  "only figures that appear in the input."),
    "weekly": ("You describe one week of a Telegram trading channel's calls from numbers a trading app computed. Write exactly 4 "
               "lines, plain text, no markdown, each starting with its label: 'works:', 'fails:', 'your exits:', 'follow-ups:'. "
               "At most 35 words a line. Use only figures that appear in the input; never invent a number; never advise."),
}

GENERATE: Optional[Callable[[str, str], str]] = None   # the tests' fake model: (system, user) -> text


def _now() -> datetime:
    return datetime.now(IST)


def on(config, kind: str) -> bool:
    return bool(getattr(config.tips, SWITCH[kind], False))


def ai_on(config) -> bool:
    return bool(getattr(config.ai, "enabled", False))


def _dt(iso) -> Optional[datetime]:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=IST)


def t12(iso) -> str:
    d = _dt(iso)
    return ampm(d) if d else ""


def _num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _pct(v) -> str:
    return "—" if v is None else f"{'+' if v >= 0 else '−'}{abs(v):.1f} %"


def _r(v) -> str:
    return "—" if v is None else f"{'+' if v >= 0 else '−'}{abs(v):.1f} R"


def short(h: dict) -> str:
    t = bk.title(h)
    return t.split(" ", 1)[1] if t.split(" ", 1)[0] in ("BUY", "SELL") and " " in t else t


# --- the store (tips_ai: never deleted) ------------------------------------------------------------------------------

def _row(r) -> dict:
    d = dict(r)
    for k in ("input_json", "lines_json"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else None
        except (TypeError, ValueError):
            d[k] = None
    return d


def insert(kind: str, hint_id: Optional[int], day: str, data: dict, now: datetime, model: Optional[str] = None) -> int:
    """A pending row (ok NULL) holding the numbers code gives the model."""
    with get_db() as conn:
        cur = conn.execute("INSERT INTO tips_ai (hint_id, kind, day, at, model, prompt_v, input_json, lines_json, ok, ms) "
                           "VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL)",
                           (hint_id, kind, day, now.isoformat(timespec="seconds"), model, PROMPT_V[kind], json.dumps(data)))
        return cur.lastrowid


def load(rid: int) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM tips_ai WHERE id = ?", (rid,)).fetchone()
    return _row(r) if r else None


def rows(kind: str, hint_id: Optional[int] = None, day: Optional[str] = None) -> list[dict]:
    q, a = "SELECT * FROM tips_ai WHERE kind = ?", [kind]
    if hint_id is not None:
        q += " AND hint_id = ?"
        a.append(hint_id)
    if day is not None:
        q += " AND day = ?"
        a.append(day)
    with get_db() as conn:
        return [_row(r) for r in conn.execute(q + " ORDER BY id", a)]


def latest(kind: str, hint_id: int) -> Optional[dict]:
    rs = rows(kind, hint_id)
    return rs[-1] if rs else None


def _finish(rid: int, out: dict, model: Optional[str], ms: Optional[int], now: datetime) -> None:
    with get_db() as conn:
        conn.execute("UPDATE tips_ai SET lines_json = ?, ok = ?, model = ?, ms = ?, at = ? WHERE id = ?",
                     (json.dumps(out), 1 if out["lines"] else 0, model, ms, now.isoformat(timespec="seconds"), rid))


# --- the model's reply: labelled lines, figure check ------------------------------------------------------------------

def _label_re(label: str) -> str:
    return r"[\s-]?".join(re.escape(w) for w in re.split(r"[\s-]+", label))


def parse(kind: str, raw: str, src: str, labels: Optional[tuple] = None, words: Optional[int] = None) -> dict:
    """{lines [{label, text}], dropped [text]}: a line must start with one of the kind's labels and every figure in it
    must be in `src` (typed: a % stays a %, a ₹ a ₹); the words are capped; a broker's name drops the line. `labels` /
    `words` override the kind's own (📈 the market read's card, panel and entry-timing lines)."""
    from backend.ai import notes
    from backend.tips import kite_check as kc
    allowed = notes.typed_tokens(src)
    order = tuple(labels or LABELS[kind])
    cap = int(words or WORDS[kind])
    labels = sorted(order, key=len, reverse=True)
    pat = re.compile(r"^\s*(?:[-*•]|\d+[.)])?\s*(?:\[?AI\]?\s*:?\s*)?\**\s*(" + "|".join(_label_re(x) for x in labels) +
                     r")\s*\**\s*[:\-–—]\s*(.+)$", re.I)
    got: dict = {}
    dropped = []
    for ln in (raw or "").split("\n"):
        t = ln.strip()
        if not t or not re.search(r"[A-Za-z]{3}", t):
            continue
        m = pat.match(t)
        if not m:
            dropped.append(t[:240])
            continue
        lab = next(x for x in labels if re.fullmatch(_label_re(x), m.group(1).strip(), re.I))
        text = notes.normalise_currency(m.group(2).strip().strip("*").strip())
        words = text.split()
        if len(words) > cap:
            text = " ".join(words[:cap]).rstrip(",;:") + "…"
        short = not _is_sentence(text) and text.lower().strip(".") not in SHORT_OK.get((kind, lab), ())
        if lab in got or not text or short or not kc._figures_ok(text, allowed) or notes._blocked_word(text):
            dropped.append(t[:240])
            continue
        got[lab] = text[:400]
    return {"lines": [{"label": x, "text": got[x]} for x in order if x in got], "dropped": dropped}


def _generate(config, kind: str, src: str, generate=None) -> tuple[str, Optional[str], Optional[int]]:
    """(raw, model, tokens): the night model for review / weekly, the day model for grade / preflight."""
    gen = generate or GENERATE
    if gen is not None:
        return gen(SYSTEM[kind], src), "fake", None
    from backend.ai import client
    c = config.ai
    model = c.night_model if kind in NIGHT else c.day_model
    r = client.generate(model, src + f"\n\nWrite the {len(LABELS[kind])} labelled line{'s' if len(LABELS[kind]) > 1 else ''} now, plain text:",
                        system=SYSTEM[kind], json_mode=False, timeout=float(c.timeout_s), keep_alive=c.keep_alive,
                        options={"temperature": 0, "num_predict": 90 * len(LABELS[kind])})
    return r.get("text") or "", r.get("model") or model, r.get("tokens")


def run_row(config, rid: int, generate=None, now: Optional[datetime] = None, notify: bool = True) -> dict:
    """The queue's job: the model reads the row's input text; the labelled lines are kept, the rest dropped; then the
    kind's follow-on (preflight differs → Telegram + Home; review → the day's Telegram; weekly → Telegram)."""
    import time as _t
    row = load(rid)
    if not row:
        return {"skip": "no row"}
    if row["ok"] is not None:
        return {"skip": "already written"}
    data = row["input_json"] or {}
    src = data.get("text") or ""
    t0 = _t.monotonic()
    raw, model, tokens = _generate(config, row["kind"], src, generate)
    ms = round((_t.monotonic() - t0) * 1000)
    out = parse(row["kind"], raw, src)
    out["raw"] = (raw or "")[:3000]
    if row["kind"] == "preflight":
        out.update(verdict(out["lines"]))
    now = now or _now()
    _finish(rid, out, model, ms, now)
    try:
        if row["kind"] == "preflight":
            _after_preflight(config, load(rid), notify)
        elif row["kind"] == "review" and notify:
            send_review_day(config, row["day"], now)
        elif row["kind"] == "weekly" and notify:
            send_weekly(config, load(rid))
    except Exception:
        logger.exception("Tips AI follow-on of row %s failed", rid)
    return {"lines": out["lines"], "dropped": len(out["dropped"]), "tokens": tokens, "note": f"tips {row['kind']} #{row.get('hint_id') or rid}"}


def enqueue(config, rid: int, kind: str, key: str, note: str, notify: bool = True) -> Optional[dict]:
    """Queue the row's read (deduped on the key while queued). Never raises; the AI guard decides when it runs."""
    if not ai_on(config):
        return None
    try:
        from backend.ai import queue as q
        model = config.ai.night_model if kind in NIGHT else config.ai.day_model
        return q.enqueue(config, f"{QKIND}_{kind}", key, lambda: run_row(config, rid, notify=notify), model=model, note=note)
    except Exception as exc:
        logger.info("Tips AI %s not queued: %s", kind, str(exc)[:120])
        return None


def lines_view(row: Optional[dict]) -> dict:
    """{state pending|done|empty|off, lines [{label, text}], at_label, ms, model, text (the dim one when no line)}."""
    if not row:
        return {"state": "none", "lines": []}
    lj = row.get("lines_json") or {}
    st = "pending" if row["ok"] is None else "done" if row["ok"] else "empty"
    return {"state": st, "lines": lj.get("lines") or [], "at": row["at"], "at_label": t12(row["at"]), "ms": row.get("ms"),
            "model": row.get("model"), "prompt_v": row.get("prompt_v"), "id": row["id"],
            "text": NO_READ if st == "empty" else "the read waits for the local model" if st == "pending" else None}


# --- 1. the evening review ------------------------------------------------------------------------------------------------

def reviewable(h: dict, day: str) -> bool:
    """A hint that had a Kite position today (live or dry) and is closed; paper-only hints never."""
    from backend.tips import kite_orders as ko
    k = ko.kstate(h)
    if k.get("entry_px") is None or k["state"] != "closed":
        return False
    closed = _dt(k.get("closed_at"))
    return (closed.date().isoformat() if closed else k.get("day")) == day


def _ticks_after(ts: list, start: Optional[datetime], end: Optional[datetime]) -> tuple[Optional[float], Optional[float]]:
    vals = [float(t["ltp"]) for t in ts if t.get("ltp") is not None and _dt(t.get("at")) and (start is None or _dt(t["at"]) >= start)
            and (end is None or _dt(t["at"]) <= end)]
    return (min(vals), max(vals)) if vals else (None, None)


def review_input(h: dict, now: datetime) -> dict:
    """The review's input text and the dim numbers line, every figure from code."""
    from backend.tips import facts as fx
    from backend.tips import kite_check as kc
    from backend.tips import kite_orders as ko
    from backend.tips import card as cd
    k = ko.kstate(h)
    f = fx.for_hint(h, now) or {}
    plan = h.get("plan") or {}
    ts = fx.ticks(h["id"])
    entry_at, exit_at = _dt(k.get("entry_at")), _dt(k.get("closed_at"))
    dry = bool(k.get("dry"))
    by = cd.EXIT_BY.get(k.get("exit_by") or "", k.get("exit_by") or k.get("reason") or "closed")
    lines = [f"Hint #{h['id']}: {bk.title(h)}" + (" · DRY RUN (a rehearsal, nothing reached the broker)" if dry else ""),
             "Post: " + " / ".join(x.strip() for x in (h.get("raw") or "").split("\n") if x.strip())[:400],
             f"Plan: entry {plan.get('entry_text') or '-'} · {plan.get('stop_text') or '-'} · {plan.get('targets_text') or '-'}"
             + (f" · time stop {ampm(plan['time_stop'])}" if plan.get("time_stop") else ""),
             f"Trade: {kc.qty_text(h, k.get('qty'))} · entry {pl.fmt(k.get('entry_px'))} at {t12(k.get('entry_at'))}"
             + (f" · post to fill {f['post_to_fill_s']} s" if f.get("post_to_fill_s") is not None else "")
             + f" · exit {pl.fmt(k.get('exit_px'))} at {t12(k.get('closed_at'))} by {by} · realised {bk.inr(float(k.get('realised') or 0), sign=True)}"]
    mfe, mae = f.get("mfe") or {}, f.get("mae") or {}
    tk = [f"best {_pct(mfe.get('pct'))} ({bk.inr(mfe.get('rs'), sign=True)}) at {t12(mfe.get('at'))}" if mfe else "best -",
          f"worst {_pct(mae.get('pct'))} at {t12(mae.get('at'))}" if mae else "worst -"]
    if f.get("stop_nearest_pct") is not None:
        tk.append(f"nearest the stop {f['stop_nearest_pct']:.1f} %")
    if f.get("stop") is not None:
        tk.append(f"stop {pl.fmt(f['stop'])}")
    lo, hi = _ticks_after(ts, entry_at, exit_at)
    if lo is not None:
        tk.append(f"in the trade low {pl.fmt(lo)} high {pl.fmt(hi)}")
    lines.append("Ticks: " + " · ".join(tk))
    for r in [r for r in ko.rows_for(h["id"]) if r["status"] != "voided" and r["leg"] != "test"]:
        lines.append(f"Order {t12(r.get('placed_at'))} {r['leg']} {r.get('side')} {kc.qty_text(h, r.get('qty'))} "
                     f"{ko._type_text(r.get('order_type') or '', r.get('price'), r.get('trigger'))} {r['status']}"
                     + (f" at {pl.fmt(r['filled_px'])}" if r.get("filled_px") is not None else ""))
    ops = f.get("ai_opinions") or []
    for x in f.get("followups") or []:
        at = _dt(x.get("at"))
        flo, fhi = _ticks_after(ts, at, None)
        op = next((o for o in ops if (_dt(o.get("at")) or at or now) >= (at or now)), None)
        lines.append(f'Follow-up {t12(x.get("at"))} "{x.get("text")}" · the app: {x.get("did") or x.get("action") or "noted"}'
                     + (f" · the day's low {pl.fmt(flo)} high {pl.fmt(fhi)} after it" if flo is not None else "")
                     + (f" · AI second opinion: {'agrees' if op['agrees'] else 'differs'} ({op['reads']})" if op else ""))
    mkt = market_at_entry(h, entry_at)
    if mkt:
        lines.append(f"Market at entry: {mkt}")
    ch = f.get("channel_result")
    lines.append("Channel's result: " + (f"{ch['outcome']} at {pl.fmt(ch.get('exit_px'))}" + (f" ({t12(ch.get('at'))})" if ch.get("at") else "") if ch else "none posted"))
    nums = [f"entry {pl.fmt(k.get('entry_px'))}"]
    q = int(k.get("qty") or 0)
    if mfe and q and k.get("entry_px") is not None and mfe.get("rs") is not None:
        best = float(k["entry_px"]) + float(mfe["rs"]) / q * (-1 if h.get("side") == "SELL" else 1)
        nums.append(f"best {pl.fmt(round(best, 2))} ({_pct(mfe.get('pct'))}) at {t12(mfe.get('at')).rsplit(' ', 1)[0]}")
    if f.get("stop") is not None:
        nums.append(f"stop {pl.fmt(f['stop'])}")
    nums.append(f"{'filled' if k.get('exit_by') in ('stop', None) else 'exit'} {pl.fmt(k.get('exit_px'))} at {t12(k.get('closed_at')).rsplit(' ', 1)[0]}")
    if ch:
        nums.append(f"channel {pl.fmt(ch.get('exit_px'))}" if ch.get("exit_px") is not None else f"channel {ch['outcome']}")
    return {"text": "\n".join(lines), "numbers": " · ".join(nums) + " · all from code", "dry": dry,
            "pnl": round(float(k.get("realised") or 0.0), 2), "exit_by": by, "title": short(h)}


def market_at_entry(h: dict, entry_at: Optional[datetime]) -> Optional[str]:
    """📈 The index trend at the entry (code, from the stored 1-minute candles up to the fill) and, when the cards gate
    shows the model, its read at the fill with the version. None off the three indices / without candles."""
    if entry_at is None:
        return None
    try:
        from backend.market import candles as mcd
        from backend.market import chart as mch
        from backend.market import reads as mrd
        from backend.market import view as mv
        idx = mrd.hint_index(h)
        if not idx:
            return None
        hm = entry_at.astimezone(IST).strftime("%H:%M")
        bars = [b for b in mcd.index_bars(idx, 3, entry_at.date().isoformat()) if b["d"] < entry_at.date().isoformat() or b["t"] <= hm]
        f = mch.summary(bars, idx) if bars else None
        if not f:
            return None
        tr = f["trend"]
        out = f"{idx} {tr['side']} · {'above' if tr.get('above_vwap') else 'below'} VWAP · {mv.align(tr['side'], mv.call_dir(h)) or 'flat'} the call"
        cfg = None
        try:
            from backend.api import routes
            cfg = routes._state.config if routes._state is not None else None
        except Exception:
            cfg = None
        if cfg is not None and mrd.gate(cfg)["cards"]:
            r = next((x for x in reversed(mrd.hint_reads(h["id"])) if (x.get("paths_json") or {}).get("why") == "fill"), None)
            if r:
                out += f" · candle model {mv.pct(r['p_up'])} up over {r['horizon']} min ({r['version']})"
        return out
    except Exception:
        logger.exception("Market at entry failed")
        return None


def review_hint(config, h: dict, now: Optional[datetime] = None, notify: bool = True) -> Optional[int]:
    """The review row of a closed hint today (at most one a day: a pending one is queued again); the row id or None."""
    now = now or _now()
    day = now.date().isoformat()
    if not on(config, "review") or not ai_on(config) or not reviewable(h, day):
        return None
    have = rows("review", h["id"], day)
    if have and have[-1]["ok"] is not None:
        return None                                     # written today already
    rid = have[-1]["id"] if have else insert("review", h["id"], day, review_input(h, now), now)
    enqueue(config, rid, "review", f"tips_ai_review:{h['id']}:{day}", f"Evening review #{h['id']}", notify)
    return rid


def review_day(config, now: Optional[datetime] = None, notify: bool = True) -> list[int]:
    """Every closed Kite hint of today without a review: queued (the 3:35 PM day check and the 6:35 PM run)."""
    now = now or _now()
    if not on(config, "review"):
        return []
    out = []
    for h in bk.rows("kite IS NOT NULL AND json_extract(kite, '$.entry_px') IS NOT NULL", order="id"):
        try:
            rid = review_hint(config, h, now, notify)
            if rid:
                out.append(rid)
        except Exception:
            logger.exception("Tips review #%s failed", h.get("id"))
    return out


def review_message(day: str, now: Optional[datetime] = None) -> Optional[str]:
    """The evening Telegram (kind tips), or None when nothing was taken today."""
    from backend.tips import kite_orders as ko
    hs = [h for h in bk.rows("kite IS NOT NULL AND json_extract(kite, '$.entry_px') IS NOT NULL", order="id") if reviewable(h, day)]
    if not hs:
        return None
    d = date.fromisoformat(day)
    out = [f"💡 Tips · evening review · {d:%a} {d.day} {d:%b}"]
    total = 0.0
    for h in hs:
        k = ko.kstate(h)
        r = round(float(k.get("realised") or 0.0), 2)
        total += r
        from backend.tips import card as cd
        by = cd.EXIT_BY.get(k.get("exit_by") or "", k.get("exit_by") or "closed")
        out.append(f"#{h['id']}{' DRY' if k.get('dry') else ''} {short(h)} · {bk.inr(r, sign=True)} · {by}")
        rv = rows("review", h["id"], day)
        ls = ((rv[-1].get("lines_json") or {}).get("lines") or []) if rv else []
        one = next((x["text"] for x in ls if x["label"] == "one thing"), None)
        out.append(f"[AI] {one}" if one else f"[AI] {NO_READ}")
    nxt = d + timedelta(days=1)
    calls = bk.rows("posted_at >= ? AND posted_at < ? AND channel != 'rehearsal'", (day, nxt.isoformat()), order="id")
    all1 = round(sum(((h.get("score") or {}).get("pnl_lot") or 0.0) for h in calls if not (h.get("score") or {}).get("open")), 2)
    out.append(f"day: {len(hs)} taken · {bk.inr(round(total, 2), sign=True)} · all calls 1 lot {bk.inr(all1, sign=True)}")
    gl, skipped = [], {}
    for h in calls:
        g = latest("grade", h["id"])
        if not g:
            continue
        gr = (g.get("input_json") or {}).get("grade") or "–"
        if ko.kstate(h).get("entry_px") is not None:
            gl.append(f"#{h['id']} {gr}")              # the taken calls, then the skipped ones counted by grade
        else:
            skipped[gr] = skipped.get(gr, 0) + 1
    if skipped:
        gl.append(f"{sum(skipped.values())} skipped (grades " + " · ".join(f"{g}×{n}" for g, n in sorted(skipped.items(), key=lambda x: ("ABCD–".find(x[0]) % 9, x[0]))) + ")")
    if gl:
        out.append("grades today: " + " · ".join(gl))
    return "\n".join(out)


def send_review_day(config, day: str, now: Optional[datetime] = None, force: bool = False) -> bool:
    """After the last review of the day: once a day, only when every closed Kite hint of the day has its review."""
    hs = [h for h in bk.rows("kite IS NOT NULL AND json_extract(kite, '$.entry_px') IS NOT NULL", order="id") if reviewable(h, day)]
    if not hs:
        return False
    for h in hs:
        rv = rows("review", h["id"], day)
        if not rv or rv[-1]["ok"] is None:
            return False                                # another review of the day is still to come
    with get_db() as conn:
        if get_setting(conn, REVIEW_TG_KEY + day) and not force:
            return False
    msg = review_message(day, now)
    if not msg:
        return False
    bk.push(config, f"tips|ai|review|{day}", msg)
    with get_db() as conn:
        set_setting(conn, REVIEW_TG_KEY + day, (now or _now()).isoformat(timespec="seconds"))
    return True


# --- 2. the call grade (code) -------------------------------------------------------------------------------------------

def band_of(posted: datetime) -> tuple[str, str]:
    t = posted.time()
    for key, label, until in BANDS:
        if t < until:
            return key, label
    return BANDS[-1][0], BANDS[-1][1]


def _closed_score(h: dict) -> Optional[dict]:
    s = h.get("score") or {}
    if s.get("open") or not s.get("first"):
        return None
    return s


def record_like(h: dict, now: Optional[datetime] = None) -> dict:
    """The channel's record for calls like this one: same symbol + side + hour band over GRADE days, else the symbol,
    else all; {level, label, calls, won, win_pct, t1_first, stop_first, no_fill, avg_r, enough}."""
    posted = _dt(h.get("posted_at")) or now or _now()
    since = (posted - timedelta(days=GRADE["days"])).isoformat(timespec="seconds")
    hs = [x for x in bk.rows("channel = ? AND posted_at >= ? AND posted_at < ? AND id != ?",
                             (h.get("channel"), since, posted.isoformat(timespec="seconds"), h["id"]), order="id") if _closed_score(x)]
    bkey, blabel = band_of(posted)
    side = (h.get("side") or "?").lower()
    sym = h.get("symbol") or "?"
    levels = (("like", f"{sym} {side}s posted {blabel}", lambda x: x.get("symbol") == sym and x.get("side") == h.get("side")
               and band_of(_dt(x["posted_at"]))[0] == bkey),
              ("symbol", f"{sym} calls (any side, any hour)", lambda x: x.get("symbol") == sym),
              ("all", "all the channel's calls", lambda x: True))
    pick = None
    for lvl, label, keep in levels:
        g = [x for x in hs if keep(x)]
        if len(g) >= GRADE["min_calls"]:
            pick = (lvl, label, g)
            break
    if pick is None:
        lvl, label, keep = levels[-1]
        pick = (lvl, label, [x for x in hs if keep(x)])
    lvl, label, g = pick
    sc = [x["score"] for x in g]
    filled = [s for s in sc if s.get("first") != "no_fill"]
    rs = [s["r"] for s in filled if s.get("r") is not None]
    won = sum(1 for s in filled if (s.get("pnl_lot") or s.get("pnl_pts") or 0) > 0)
    return {"level": lvl, "label": label, "calls": len(g), "filled": len(filled), "won": won,
            "win_pct": round(100.0 * won / len(filled), 1) if filled else None,
            "t1_first": sum(1 for s in sc if s.get("first") == "t1"), "stop_first": sum(1 for s in sc if s.get("first") in ("stop", "trail")),
            "no_fill": sum(1 for s in sc if s.get("first") == "no_fill"), "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
            "enough": len(g) >= GRADE["min_calls"], "days": GRADE["days"]}


def shape(h: dict) -> dict:
    """Reward / risk from the plan: (target 1 − entry) / (entry − stop), the entry at the zone's middle."""
    plan = h.get("plan") or {}
    e = plan.get("entry") or h.get("entry") or {}
    st = plan.get("stop") or h.get("stop") or {}
    tg = [float(x) for x in (plan.get("targets") or h.get("targets") or []) if _num(x)]
    lo, hi = _num(e.get("lo")), _num(e.get("hi") if e.get("hi") is not None else e.get("lo"))
    entry = (lo + hi) / 2 if lo is not None and hi is not None else None
    stop = _num(st.get("value")) if st.get("kind") == "premium" else None
    sign = -1 if h.get("side") == "SELL" else 1
    out = {"entry": round(entry, 2) if entry is not None else None, "stop": stop, "target": tg[0] if tg else None, "rr": None,
           "stop_pct": None, "target_pct": None, "stop_missing": (st.get("kind") or "none") == "none" or st.get("value") is None,
           "stop_src": st.get("src")}
    if entry and stop is not None:
        out["stop_pct"] = round((stop - entry) / entry * 100, 1)
    if entry and tg:
        out["target_pct"] = round((tg[0] - entry) / entry * 100, 1)
    if entry is not None and stop is not None and tg:
        risk, reward = (entry - stop) * sign, (tg[0] - entry) * sign
        if risk > 0:
            out["rr"] = round(reward / risk, 2)
    return out


def wording(h: dict) -> list[dict]:
    raw = h.get("raw") or ""
    out = [{"key": k, "text": t} for k, rx, t in WORDING if rx.search(raw)]
    plan = h.get("plan") or {}
    st = plan.get("stop") or h.get("stop") or {}
    if (st.get("kind") or "none") == "none" or st.get("value") is None or st.get("src") == "provisional":
        out.append({"key": "stop_missing", "text": "no stop in the post"})   # 🛑 a provisional stop is the app's, not the call's
    return out


def grade_of(rec: dict, shp: dict, words: list[dict]) -> tuple[str, str]:
    """(grade, why) by the thresholds in GRADE: D = stop missing or the record negative (avg R < 0); – = fewer than
    min_calls calls like this; A = win ≥ 60 % and R/R ≥ 1.5; B = win ≥ 50 % or R/R ≥ 1.2; C = otherwise."""
    if any(w["key"] == "stop_missing" for w in words) or shp.get("stop_missing"):
        return "D", "stop missing"
    if not rec.get("enough"):
        return "–", "too few calls like this"
    if rec.get("avg_r") is not None and rec["avg_r"] < 0:
        return "D", "the record is negative (avg R under 0)"
    win, rr = rec.get("win_pct") or 0.0, shp.get("rr") or 0.0
    if win >= GRADE["A"]["win"] and rr >= GRADE["A"]["rr"]:
        return "A", f"win {win:g} % ≥ {GRADE['A']['win']:g} % and R/R {rr:g} ≥ {GRADE['A']['rr']:g}"
    if win >= GRADE["B"]["win"] or rr >= GRADE["B"]["rr"]:
        return "B", f"win {win:g} % ≥ {GRADE['B']['win']:g} %" if win >= GRADE["B"]["win"] else f"R/R {rr:g} ≥ {GRADE['B']['rr']:g}"
    return "C", "under the B line on both the win rate and the shape"


def plan_sig(h: dict) -> str:
    plan = h.get("plan") or {}
    e = plan.get("entry") or {}
    return json.dumps([e.get("mode"), e.get("lo"), e.get("hi"), (plan.get("stop") or {}).get("value"), plan.get("targets")])


GRADE_STEPS = ("D", "C", "B", "A")


def market_input(config, h: dict, now: datetime) -> Optional[dict]:
    """📈 with / against the market, by code (the market read's verdict: the index trend with the call and, only when
    the cards gate shows the model, its vote); `applied` only when market.kronos_show is "cards" (and the gate passes):
    in shadow it is stored, the grade is unchanged. None for a hint off the three indices or with no candles."""
    try:
        from backend.market import view as mv
        cf = mv.card_facts(config, h, now)
    except Exception:
        logger.exception("Market input of the grade failed")
        return None
    if not cf or not cf["facts"]:
        return None
    tr = cf["facts"]["trend"]
    al = {"supports the call": "with", "against the call": "against"}.get(cf["verdict"])
    applied = bool(cf["gate"]["cards"]) and al is not None
    return {"align": al, "verdict": cf["verdict"], "applied": applied, "mode": cf["gate"]["mode"],
            "text": f"{cf['index']} {tr['side']} · {'above' if tr.get('above_vwap') else 'below'} VWAP · {cf['verdict']}"}


def market_step(g: str, why: str, mk: Optional[dict]) -> tuple[str, str]:
    """One step up (with) or down (against) at most, only when applied; never on "–" or a missing stop."""
    if not mk or not mk.get("applied") or g not in GRADE_STEPS or why == "stop missing":
        return g, why
    i = GRADE_STEPS.index(g) + (1 if mk["align"] == "with" else -1)
    g2 = GRADE_STEPS[max(0, min(len(GRADE_STEPS) - 1, i))]
    return g2, why + (f" · {'one step up: with' if mk['align'] == 'with' else 'one step down: against'} the market" if g2 != g else "")


def grade_input(h: dict, now: datetime, config=None) -> dict:
    rec, shp, words = record_like(h, now), shape(h), wording(h)
    g, why = grade_of(rec, shp, words)
    mk = market_input(config, h, now) if config is not None else None
    g, why = market_step(g, why, mk)
    rtxt = (f"{rec['label']}: {rec['calls']} call{'s' if rec['calls'] != 1 else ''} · T1 first {rec['t1_first']} · stop first {rec['stop_first']}"
            f" · no fill {rec['no_fill']} · avg {_r(rec['avg_r'])}" + (f" · won {rec['win_pct']:g} %" if rec.get("win_pct") is not None else "")
            + ("" if rec["enough"] else " · too few calls like this"))
    bits = []
    if shp.get("stop_pct") is not None:
        bits.append(f"stop {_pct(shp['stop_pct'])}" + (" (provisional)" if shp.get("stop_src") == "provisional" else ""))
    if shp.get("target_pct") is not None:
        bits.append(f"target {_pct(shp['target_pct'])}")
    stxt = " and ".join(bits) + (f": R/R {shp['rr']:g}" if shp.get("rr") is not None else (": no R/R (stop missing or not a premium stop)" if bits or shp.get("stop_missing") else "no R/R"))
    if shp.get("rr") is not None:
        stxt += " · risk larger than reward" if shp["rr"] < 1 else " · reward larger than risk"
    if words:
        stxt += " · " + " · ".join(w["text"] for w in words)
    plan = h.get("plan") or {}
    text = "\n".join([f"Call #{h['id']}: {bk.title(h)} · posted {t12(h.get('posted_at'))}",
                      "Post: " + " / ".join(x.strip() for x in (h.get("raw") or "").split("\n") if x.strip())[:300],
                      f"Plan: entry {plan.get('entry_text') or '-'} · {plan.get('stop_text') or '-'} · {plan.get('targets_text') or '-'}"
                      + (f" · time stop {ampm(plan['time_stop'])}" if plan.get("time_stop") else ""),
                      f"Record ({rec['days']} days, {rec['level']}): {rtxt}", f"Shape: {stxt}"]
                     + ([f"Market (code): {mk['text']}" + ("" if mk["applied"] else " · not counted in the grade")] if mk else [])
                     + [f"Grade by code: {g} ({why})"])
    return {"text": text, "grade": g, "why": why, "record": rec, "shape": shp, "wording": words, "record_text": rtxt,
            "shape_text": stxt, "sig": plan_sig(h), "numbers": rtxt + " · " + stxt, "market": mk}


def grade_hint(config, h: dict, now: Optional[datetime] = None, notify: bool = True) -> Optional[int]:
    """The grade at ingest, and once more when the plan changed before the entry. The code grade is stored at once; the
    model's read line is queued (AI on). Never touches the hint's Kite state: auto ignores the grade."""
    from backend.tips import kite_orders as ko
    now = now or _now()
    if not on(config, "grade") or not h or not h.get("plan"):
        return None
    have = rows("grade", h["id"])
    if have:
        if len(have) >= 2 or ko.kstate(h).get("entry_px") is not None or ko.kstate(h)["state"] in ("entered", "closed"):
            return None                                 # re-graded once at most, never after the entry
        if (have[-1].get("input_json") or {}).get("sig") == plan_sig(h):
            return None                                 # the plan did not change
    data = grade_input(h, now, config)
    rid = insert("grade", h["id"], now.date().isoformat(), data, now)
    enqueue(config, rid, "grade", f"tips_ai_grade:{h['id']}:{rid}", f"Call grade #{h['id']}", notify)
    return rid


def grade_letter(hid: int) -> Optional[str]:
    g = latest("grade", hid)
    return (g.get("input_json") or {}).get("grade") if g else None


def on_post(config, out: dict, now: Optional[datetime] = None) -> None:
    """After a live post is stored (reader.ingest): a new call is graded; a follow-up that changed the plan before the
    entry re-grades it once. Never raises."""
    try:
        if not out.get("new") or not out.get("hint_id") or out.get("kind") not in ("hint", "follow_up"):
            return
        h = bk.load(out["hint_id"])
        if h:
            grade_hint(config, h, now or _dt(out.get("at")) or _now())
    except Exception:
        logger.exception("Tips grade failed")


# --- 3. the pre-flight read --------------------------------------------------------------------------------------------

def preflight_input(h: dict, ep: dict, lots: int, qty: int) -> dict:
    plan = h.get("plan") or {}
    e = plan.get("entry") or {}
    text = "\n".join([
        f"Hint #{h['id']}",
        "Post: " + " / ".join(x.strip() for x in (h.get("raw") or "").split("\n") if x.strip())[:500],
        f"Decoded: {h.get('side') or '-'} {h.get('symbol') or '-'} strike {pl.fmt(h.get('strike')) if h.get('strike') is not None else '-'} "
        f"{h.get('opt_type') or ''} · expiry {h.get('expiry') or '-'} ({h.get('expiry_src') or '-'})",
        f"Plan: entry {e.get('mode') or 'market'} {plan.get('entry_text') or '-'} · {plan.get('stop_text') or '-'} · {plan.get('targets_text') or '-'}"
        + (f" · time stop {ampm(plan['time_stop'])}" if plan.get("time_stop") else " · no time stop"),
        f"Size: {lots} lot{'s' if lots != 1 else ''} · {qty} qty (lot {h.get('lot_size') or '-'})",
        f"Contract: {h.get('tradingsymbol') or '-'} on {h.get('exchange') or '-'}",
        f"Order: {ep.get('text') or ep.get('order_type') or 'MARKET'}"])
    return {"text": text, "contract": h.get("tradingsymbol"), "order": ep.get("text"), "lots": lots, "qty": qty}


def verdict(lines: list[dict]) -> dict:
    """{agrees: True / False / None, why, flag}: from the 'plan vs post' and 'flag' lines."""
    pv = next((x["text"] for x in lines if x["label"] == "plan vs post"), "")
    fl = next((x["text"] for x in lines if x["label"] == "flag"), "")
    m = re.search(r"\bdiffers?\b\s*:?\s*(.*)$", pv, re.I)
    agrees = False if m else (True if re.search(r"\bagrees?\b", pv, re.I) else None)
    flag = None if not fl or re.fullmatch(r"\W*none\W*", fl, re.I) else fl
    return {"agrees": agrees, "why": (m.group(1).strip(" .·-") if m else "")[:160], "flag": flag}


def preflight(config, h: dict, ep: dict, lots: int, qty: int, now: Optional[datetime] = None) -> Optional[int]:
    """Called by kite_orders.place_entry once the guards passed, BEFORE the order: queue the read and return at once
    (the arm is never held, never blocked). Off (the default) → nothing. Never raises."""
    try:
        now = now or _now()
        if not on(config, "preflight") or not ai_on(config):
            return None
        arm = now.isoformat(timespec="seconds")
        if any((r.get("input_json") or {}).get("arm") == arm for r in rows("preflight", h["id"])):
            return None                                 # one read per arm
        data = {**preflight_input(h, ep, lots, qty), "arm": arm}
        rid = insert("preflight", h["id"], now.date().isoformat(), data, now)
        enqueue(config, rid, "preflight", f"tips_ai_preflight:{h['id']}:{arm}", f"Pre-flight #{h['id']}")
        return rid
    except Exception:
        logger.exception("Tips pre-flight not queued")
        return None


def _after_preflight(config, row: Optional[dict], notify: bool = True) -> Optional[str]:
    """differs → one Telegram line (kind tips) and an amber Home item; a flag stays on the card."""
    if not row:
        return None
    lj = row.get("lines_json") or {}
    if lj.get("agrees") is not False:
        return None
    why = lj.get("why") or "see the card"
    msg = f"[AI] #{row['hint_id']} pre-flight: plan differs from the post · {why} · armed anyway · check"
    if notify:
        bk.push(config, f"tips|ai|preflight|{row['id']}", msg)
    return msg


def attention_items(now: Optional[datetime] = None) -> list[tuple[str, str]]:
    """("info", "[AI] #14 pre-flight: plan differs …") for today's reads that said differs (amber: worth a look)."""
    now = now or _now()
    out = []
    for r in rows("preflight", day=now.date().isoformat()):
        lj = r.get("lines_json") or {}
        if r["ok"] and lj.get("agrees") is False:
            out.append(("info", f"💡 [AI] #{r['hint_id']} pre-flight: plan differs from the post · {lj.get('why') or 'see the card'} · armed anyway · check"))
    return out[-3:]


# --- 4. the weekly channel profile ------------------------------------------------------------------------------------------

def week_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def last_week(now: datetime) -> date:
    """The Monday of the last Mon–Fri that has ended (on Saturday / Sunday: this week)."""
    mon = week_of(now.date())
    return mon if now.weekday() >= 5 else mon - timedelta(days=7)


def week_label(mon: date) -> str:
    fri = mon + timedelta(days=4)
    span = f"{mon.day}–{fri.day} {fri:%b}" if mon.month == fri.month else f"{mon.day} {mon:%b}–{fri.day} {fri:%b}"
    return f"week {mon.isocalendar()[1]} · {span}"


def _week_hints(channel: Optional[str], mon: date) -> list[dict]:
    a, b = mon.isoformat(), (mon + timedelta(days=5)).isoformat()
    q = "posted_at >= ? AND posted_at < ? AND channel != 'rehearsal'" + (" AND channel = ?" if channel else "")
    return bk.rows(q, (a, b, channel) if channel else (a, b), order="id")


def channels_in(mon: date) -> list[str]:
    return sorted({h["channel"] for h in _week_hints(None, mon)})


def _bars(groups: dict, order: Optional[list] = None) -> list[dict]:
    """[{label, n, won, w (CSS width %, the largest = 100), tone ok|bad}]."""
    keys = order or sorted(groups, key=lambda k: (-groups[k]["n"], k))
    mx = max([groups[k]["n"] for k in keys if k in groups] or [0])
    out = []
    for k in keys:
        g = groups.get(k)
        if not g:
            continue
        out.append({"label": g.get("label", k), "n": g["n"], "won": g.get("won"), "w": round(100.0 * g["n"] / mx) if mx else 0,
                    "tone": "ok" if g.get("won") is None or g["won"] * 2 >= g["n"] else "bad"})
    return out


def _won(h: dict) -> bool:
    s = _closed_score(h) or {}
    return s.get("first") != "no_fill" and ((s.get("pnl_lot") or s.get("pnl_pts") or 0) > 0)


def week_numbers(channel: str, mon: date) -> dict:
    """The four boxes: this week, by index, by hour band, how calls end; your exits vs the app; follow-ups. Code only."""
    from backend.tips import facts as fx
    from backend.tips import kite_orders as ko
    from backend.tips import track as tr
    hs = _week_hints(channel, mon)
    agg = tr._agg(hs)
    kite = [h for h in hs if ko.kstate(h).get("entry_px") is not None]
    yours = round(sum(float(ko.kstate(h).get("realised") or 0.0) for h in kite if ko.kstate(h)["state"] == "closed"), 2)
    four = [h for w in range(4) for h in _week_hints(channel, mon - timedelta(days=7 * w))]
    a4 = tr._agg(four)
    t1p = round(100.0 * a4["t1_first"] / a4["hints"]) if a4["hints"] else None
    idx: dict = {}
    for h in hs:
        g = idx.setdefault(h.get("symbol") or "?", {"label": h.get("symbol") or "?", "n": 0, "won": 0})
        g["n"] += 1
        g["won"] += _won(h)
    hrs: dict = {k: {"label": lbl, "n": 0, "won": 0} for k, lbl, _ in BANDS}
    for h in hs:
        k = band_of(_dt(h["posted_at"]))[0]
        hrs[k]["n"] += 1
        hrs[k]["won"] += _won(h)
    ends = {"target": {"label": "target", "n": 0}, "early": {"label": "booked early", "n": 0}, "stop": {"label": "stop", "n": 0},
            "time": {"label": "time stop", "n": 0}}
    to_t1, to_stop = [], []
    for h in hs:
        s = _closed_score(h)
        if not s or s.get("first") == "no_fill":
            continue
        end = str(s.get("end") or "")
        k = "target" if re.fullmatch(r"t\d+", end) else "early" if end in ("channel_exit", "owner_exit") else \
            "stop" if end in ("stop", "trail") else "time" if end in ("time", "expired") else None
        if k:
            ends[k]["n"] += 1
        if s.get("minutes_to_first") is not None:
            (to_t1 if s.get("first") == "t1" else to_stop if s.get("first") in ("stop", "trail") else []).append(s["minutes_to_first"])
    hand = []
    for h in kite:
        k = ko.kstate(h)
        s = _closed_score(h)
        if k["state"] != "closed" or k.get("exit_by") != "you" or not s:
            continue
        sign = -1 if h.get("side") == "SELL" else 1
        app = round(float(s.get("pnl_pts") or 0.0) * int(k.get("qty") or 0), 2)
        got = round(float(k.get("realised") or 0.0), 2)
        hand.append({"id": h["id"], "exit_px": k.get("exit_px"), "at": k.get("closed_at"), "yours": got, "watch": app,
                     "diff": round(app - got, 2), "sign": sign})
    fu = agree = opin = 0
    for h in hs:
        fu += sum(1 for e in h.get("events") or [] if e.get("src") == "channel")
        for o in fx.ai_opinions(ko.kstate(h)):
            opin += 1
            agree += bool(o.get("agrees"))
    return {"channel": channel, "week": mon.isoformat(), "label": week_label(mon),
            "this_week": {"calls": agg["hints"], "t1_first": agg["t1_first"], "stop_first": agg["stop_first"], "no_fill": agg["no_fill"],
                          "avg_r": agg["avg_r"], "took": len(kite), "dry": sum(1 for h in kite if ko.kstate(h).get("dry")),
                          "yours": yours, "all_1lot": agg["pnl_lot"]},
            "four_weeks": {"calls": a4["hints"], "t1_pct": t1p, "avg_r": a4["avg_r"]},
            "by_index": _bars(idx), "by_hour": _bars(hrs, [k for k, _, _ in BANDS if hrs[k]["n"]]),
            "ends": _bars(ends, ["target", "early", "stop", "time"]) if any(v["n"] for v in ends.values()) else [],
            "median_t1_min": statistics.median(to_t1) if to_t1 else None, "median_stop_min": statistics.median(to_stop) if to_stop else None,
            "hand_exits": hand, "hand_left": round(sum(x["diff"] for x in hand), 2),
            "followups": {"count": fu, "read": opin, "agree": agree}}


def week_text(n: dict) -> str:
    """The model's input: every figure from week_numbers."""
    tw, f4 = n["this_week"], n["four_weeks"]
    ls = [f"Channel {n['channel']} · {n['label']}",
          f"This week: {tw['calls']} calls · T1 first {tw['t1_first']} · stop first {tw['stop_first']} · no fill {tw['no_fill']} · avg {_r(tw['avg_r'])}",
          f"You took {tw['took']} · yours {bk.inr(tw['yours'], sign=True)} · all calls 1 lot {bk.inr(tw['all_1lot'], sign=True)}",
          f"4 weeks: {f4['calls']} calls · T1 first {f4['t1_pct'] if f4['t1_pct'] is not None else '-'} % · avg {_r(f4['avg_r'])}",
          "By index: " + (" · ".join(f"{b['label']} {b['n']} calls {b['won']} won" for b in n["by_index"]) or "none"),
          "By hour posted: " + (" · ".join(f"{b['label']} {b['n']} calls {b['won']} won" for b in n["by_hour"]) or "none"),
          "How calls end: " + (" · ".join(f"{b['label']} {b['n']}" for b in n["ends"]) or "none")
          + (f" · median minutes to T1 {n['median_t1_min']:g}" if n.get("median_t1_min") is not None else "")
          + (f" · median minutes to the stop {n['median_stop_min']:g}" if n.get("median_stop_min") is not None else "")]
    if n["hand_exits"]:
        ls.append("Your hand exits: " + " · ".join(f"#{x['id']} at {pl.fmt(x['exit_px'])} ({t12(x['at'])}) yours {bk.inr(x['yours'], sign=True)}, "
                                                   f"the watch would have given {bk.inr(x['watch'], sign=True)}" for x in n["hand_exits"])
                  + f" · together the watch would have added {bk.inr(n['hand_left'], sign=True)}")
    else:
        ls.append("Your hand exits: none")
    fu = n["followups"]
    ls.append(f"Follow-ups: {fu['count']} · AI second opinions {fu['read']} · agreed with the app {fu['agree']}")
    return "\n".join(ls)


def week_view(channel: str, mon: date) -> dict:
    """The Track tab's panel for one channel and week: the stored row (newest) or the code numbers with no lines."""
    have = [r for r in rows("weekly", day=mon.isoformat()) if (r.get("input_json") or {}).get("channel") == channel]
    r = have[-1] if have else None
    n = (r.get("input_json") or {}).get("numbers") if r else None
    n = n or week_numbers(channel, mon)
    tw = n["this_week"]
    v = lines_view(r) if r else {"state": "none", "lines": [], "text": "not written yet · Sunday 7:05 PM or Write now"}
    head = f"{tw['calls']} calls · T1 first {tw['t1_first']} · stop first {tw['stop_first']} · avg {_r(tw['avg_r'])}"
    model = None
    try:                                                  # 📈 the candle model's hit rate on this channel's calls (a score, not a read)
        from backend.market import reads as mrd
        hr = mrd.channel_hit_rate(channel, mon.isoformat(), (mon + timedelta(days=7)).isoformat())
        model = f"candle model on this channel's calls: {hr['pct']:g} % over {hr['n']} reads" if hr else None
    except Exception:
        logger.exception("Weekly model hit rate failed")
    return {"channel": channel, "week": mon.isoformat(), "label": n["label"], "numbers": n, "ai": v, "headline": head, "model": model,
            "written": f"written {(_dt(r['at']) or _now()):%a} {t12(r['at'])}" if r and r["ok"] is not None else None}


def weekly_write(config, channel: str, mon: date, now: Optional[datetime] = None, force: bool = False, notify: bool = True) -> Optional[int]:
    """One row for (channel, week): the Sunday job writes once a week (a pending row is queued again); Write now adds a
    new row (the older ones are kept)."""
    now = now or _now()
    if not on(config, "weekly") or not ai_on(config):
        return None
    have = [r for r in rows("weekly", day=mon.isoformat()) if (r.get("input_json") or {}).get("channel") == channel]
    if have and not force:
        if have[-1]["ok"] is not None:
            return None
        rid = have[-1]["id"]
    else:
        n = week_numbers(channel, mon)
        if not n["this_week"]["calls"]:
            return None
        rid = insert("weekly", None, mon.isoformat(), {"channel": channel, "numbers": n, "text": week_text(n)}, now)
    enqueue(config, rid, "weekly", f"tips_ai_weekly:{channel}:{mon.isoformat()}:{rid}", f"Channel profile {channel}", notify)
    return rid


def _configured(config, ch: str) -> bool:
    """Is `ch` (a hint's channel key) one of tips.channels today? Usernames and titles compared loosely."""
    from backend.tips import reader as rd
    want = [rd.norm_title(x) if hasattr(rd, "norm_title") else re.sub(r"[^a-z0-9]+", "", x.lower()) for x in rd.split_channels(getattr(config.tips, "channels", "") or "")]
    key = re.sub(r"[^a-z0-9]+", "", (ch or "").lower())
    return any(key and (key == w or key in w or w in key) for w in want if w)


def weekly_channels_due(config, mon: date) -> list[str]:
    """The channels that get a Sunday message: still in tips.channels, or 2+ calls in the week (an old channel with one
    stray call is written for the Track page but not pushed)."""
    out = []
    for ch in channels_in(mon):
        n = week_numbers(ch, mon)["this_week"]["calls"]
        if _configured(config, ch) or n >= 2:
            out.append(ch)
    return out


def weekly_due(config, now: Optional[datetime] = None, notify: bool = True) -> list[int]:
    """Sunday 7:05 PM: every channel with a call in the Mon–Fri just ended is written; only the configured ones (or those
    with 2+ calls) are pushed to Telegram."""
    now = now or _now()
    mon = last_week(now)
    out = []
    push = set(weekly_channels_due(config, mon))
    for ch in channels_in(mon):
        rid = weekly_write(config, ch, mon, now, notify=notify and ch in push)
        if rid:
            out.append(rid)
    return out


def weekly_now(config, mon: Optional[date] = None, now: Optional[datetime] = None) -> dict:
    """The Track tab's "Write now": one per hour; {rows, week} or ValueError with the time to wait."""
    now = now or _now()
    with get_db() as conn:
        last = _dt(get_setting(conn, WEEKLY_NOW_KEY))
        if last and (now - last).total_seconds() < WEEKLY_NOW_GAP_S:
            nxt = last + timedelta(seconds=WEEKLY_NOW_GAP_S)
            raise ValueError(f"one Write now per hour · next at {ampm(nxt)}")
        set_setting(conn, WEEKLY_NOW_KEY, now.isoformat(timespec="seconds"))
    mon = mon or last_week(now)
    ids = [rid for ch in channels_in(mon) for rid in [weekly_write(config, ch, mon, now, force=True)] if rid]
    return {"rows": ids, "week": mon.isoformat()}


def weekly_message(row: dict) -> str:
    d = row.get("input_json") or {}
    n = d.get("numbers") or {}
    tw = n.get("this_week") or {}
    mon = date.fromisoformat(n.get("week") or row["day"])
    ls = [f"📊 Tips · channel profile · week {mon.isocalendar()[1]}",
          f"{tw.get('calls', 0)} calls · T1 first {tw.get('t1_first', 0)} · stop first {tw.get('stop_first', 0)} · avg {_r(tw.get('avg_r'))}"]
    lines = (row.get("lines_json") or {}).get("lines") or []
    ls += [f"[AI] {x['text']}" for x in lines]                   # nothing usable → numbers only, no [AI] line
    ls.append(f"you: {tw.get('took', 0)} of {tw.get('calls', 0)} · {bk.inr(tw.get('yours') or 0, sign=True)} · all: {bk.inr(tw.get('all_1lot') or 0, sign=True)}")
    ls.append("full profile: Research › Tips › Track")
    return "\n".join(ls)


def send_weekly(config, row: Optional[dict]) -> bool:
    if not row:
        return False
    d = row.get("input_json") or {}
    bk.push(config, f"tips|ai|weekly|{d.get('channel')}|{row['day']}|{row['id']}", weekly_message(row))
    return True


def weekly_page(config, week: Optional[str] = None, channel: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """GET /api/tips/ai/weekly: the week picker (last 8 weeks) and one panel per channel."""
    now = now or _now()
    last, this = last_week(now), week_of(now.date())
    weeks = [this - timedelta(days=7 * i) for i in range(8)]          # the last 8 weeks (on a weekday: this one so far)
    default = this if this != last and any(rows("weekly", day=this.isoformat())) else last
    try:
        mon = week_of(date.fromisoformat(week)) if week else default
    except ValueError:
        mon = default
    chans = [channel] if channel else (channels_in(mon) or [])
    with get_db() as conn:
        lw = _dt(get_setting(conn, WEEKLY_NOW_KEY))
    return {"week": mon.isoformat(), "weeks": [{"week": w.isoformat(), "label": week_label(w) + (" · so far" if w > last else "")} for w in weeks],
            "panels": [week_view(c, mon) for c in chans], "on": on(config, "weekly"), "ai": ai_on(config),
            "write_next": ampm(lw + timedelta(seconds=WEEKLY_NOW_GAP_S)) if lw and (now - lw).total_seconds() < WEEKLY_NOW_GAP_S else None}


# --- the card --------------------------------------------------------------------------------------------------------------

def card_view(config, h: dict) -> dict:
    """The hint card's AI blocks: {grade, review, preflight} (each None when there is nothing to show)."""
    out: dict = {"grade": None, "review": None, "preflight": None}
    g = latest("grade", h["id"]) if on(config, "grade") else None
    if g:
        d = g.get("input_json") or {}
        gr = d.get("grade") or "–"
        v = lines_view(g)
        if not ai_on(config) and g["ok"] is None:
            v["text"] = "the local model is off · the grade stands by code"
        out["grade"] = {"grade": gr, "tone": GRADE_TONE.get(gr, ""), "why": d.get("why"), "at_label": t12(d.get("at") or g["at"]),
                        "record": d.get("record_text"), "shape": d.get("shape_text"), "level": (d.get("record") or {}).get("level"),
                        "read": v, "regraded": len(rows("grade", h["id"])) > 1,
                        "market": (f"market: {(d.get('market') or {}).get('align') or 'mixed'} · " + ("counted (one step at most)" if (d.get("market") or {}).get("applied") else "stored, not counted (shadow)")) if d.get("market") else None}
    r = latest("review", h["id"]) if on(config, "review") else None
    if r:
        d = r.get("input_json") or {}
        out["review"] = {**lines_view(r), "numbers": d.get("numbers"), "dry": d.get("dry")}
    p = latest("preflight", h["id"]) if on(config, "preflight") else None
    if p:
        lj = p.get("lines_json") or {}
        out["preflight"] = {**lines_view(p), "agrees": lj.get("agrees"), "why": lj.get("why"), "flag": lj.get("flag"),
                            "switch": "on · Settings › tips · ai_preflight"}
    return out


# --- the scheduler ------------------------------------------------------------------------------------------------------------

def due(now: datetime, config, done: set, trading: bool) -> list[str]:
    """tips_ai_evening at 6:35 PM on trading days (the reviews of hints closed after 3:35 PM, and the day's Telegram);
    tips_ai_weekly Sunday from 7:05 PM."""
    out = []
    t = now.time()
    if trading and on(config, "review") and EVENING_JOB not in done and EVENING_TIME <= t < time(23, 0):
        out.append(EVENING_JOB)
    if now.weekday() == 6 and on(config, "weekly") and WEEKLY_JOB not in done and WEEKLY_TIME <= t < time(23, 0):
        out.append(WEEKLY_JOB)
    return out
