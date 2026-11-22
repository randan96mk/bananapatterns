"""📈🤖 The local text model's sentences about the market read (kind "market" on the AI queue, prompt market-v1,
json_mode False; stored in tips_ai kind 'market', never deleted, archived after 180 days like every Tips AI row).

  card    4 labelled lines trend / levels / stop / verdict (≤ 30 words each) at ingest and once more on the entry fill.
          The verdict's ending ("supports the call" | "against the call" | "mixed") is decided by CODE (view.verdict:
          the trend with the call and, only when the gate shows the model, its p_up ≥ 55 % / ≤ 45 %): whatever ending
          the model wrote is replaced by it.
  entry   one "pullback:" line for a zone / level entry not filled yet (informational; auto is unchanged).
  panel   the "Market now" backdrop: "read:" and "for calls:" (the numbers line is code), refreshed with the reads when
          the code facts changed, at most once per PANEL_GAP_S per index.
  score   one "read:" line on the scorecard (Settings › market / ai), after the 6:40 PM scoring.

The input is text built by code; a line whose figures are not in it is dropped (ai4.parse: the figure check); in shadow
the model's numbers are not in the input, so they can never reach a card."""

import json
import re
from datetime import datetime
from typing import Callable, Optional

from backend.config import IST
from backend.database.models import get_db
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm

logger = setup_logger("market")

KIND = "market"
PROMPT_V = "market-v1"
QKIND = "market"
LABELS = {"card": ("trend", "levels", "stop", "verdict"), "entry": ("pullback",), "panel": ("read", "for calls"), "score": ("read",)}
WORDS = 30
PANEL_GAP_S = 300
NO_READ = "no usable read"
SYSTEM = {
    "card": ("A trading app computed chart facts about the index behind a Telegram channel's option call. Write exactly 4 lines, plain "
             "text, no markdown, each starting with its label: 'trend:', 'levels:', 'stop:', 'verdict:'. At most 30 words a line. Use "
             "only figures that appear in the input; never invent a number. The verdict line must end with the verdict the input "
             "gives. Describe; never advise buying or selling."),
    "entry": ("A trading app computed whether an option's price may pull back into the call's entry zone. Write ONE line, plain text, "
              "starting with 'pullback:', at most 30 words, from the input's figures only. Never advise buying or selling."),
    "panel": ("A trading app computed chart facts for one Indian index. Write exactly 2 lines, plain text, each starting with its label: "
              "'read:' (what the index is doing) and 'for calls:' (what that means for a call-buy and a put-buy). At most 30 words a "
              "line. Use only figures that appear in the input; never advise buying or selling."),
    "score": ("A trading app scored a candle model's past reads. Write ONE line, plain text, starting with 'read:', at most 30 words: "
              "where the model beats a coin flip and where it does not, from the input's figures only. Never advise trading."),
}
GENERATE: Optional[Callable[[str, str], str]] = None       # the tests' fake model: (system, user) -> text
VERDICT_RE = re.compile(r"[\s,;:.—–-]*\b(supports the call|against the call|mixed)\b[\s.]*$", re.I)


def _now() -> datetime:
    return datetime.now(IST)


# --- the store ----------------------------------------------------------------------------------------------------------

def insert(sub: str, hint_id: Optional[int], data: dict, now: datetime) -> int:
    with get_db() as conn:
        return conn.execute("INSERT INTO tips_ai (hint_id, kind, day, at, model, prompt_v, input_json, lines_json, ok, ms) VALUES (?,?,?,?,NULL,?,?,NULL,NULL,NULL)",
                            (hint_id, KIND, now.date().isoformat(), now.isoformat(timespec="seconds"), PROMPT_V, json.dumps({**data, "sub": sub}))).lastrowid


def rows(sub: str, hint_id: Optional[int] = None, symbol: Optional[str] = None) -> list[dict]:
    from backend.tips import ai4
    q, a = "SELECT * FROM tips_ai WHERE kind = ? AND json_extract(input_json, '$.sub') = ?", [KIND, sub]
    if hint_id is not None:
        q += " AND hint_id = ?"
        a.append(hint_id)
    if symbol is not None:
        q += " AND json_extract(input_json, '$.symbol') = ?"
        a.append(symbol)
    with get_db() as conn:
        return [ai4._row(r) for r in conn.execute(q + " ORDER BY id", a)]


def _generate(config, sub: str, src: str, generate=None) -> tuple[str, Optional[str], Optional[int]]:
    gen = generate or GENERATE
    if gen is not None:
        return gen(SYSTEM[sub], src), "fake", None
    from backend.ai import client
    c = config.ai
    n = len(LABELS[sub])
    r = client.generate(c.day_model, src + f"\n\nWrite the {n} labelled line{'s' if n > 1 else ''} now, plain text:", system=SYSTEM[sub], json_mode=False,
                        timeout=float(c.timeout_s), keep_alive=c.keep_alive, options={"temperature": 0, "num_predict": 90 * n})
    return r.get("text") or "", r.get("model") or c.day_model, r.get("tokens")


def run_row(config, rid: int, generate=None, now: Optional[datetime] = None) -> dict:
    """The queue's job: the labelled lines kept (figure check), the card's verdict ending set by code."""
    import time as _t
    from backend.tips import ai4
    row = ai4.load(rid)
    if not row:
        return {"skip": "no row"}
    if row["ok"] is not None:
        return {"skip": "already written"}
    data = row["input_json"] or {}
    sub, src = data.get("sub") or "card", data.get("text") or ""
    t0 = _t.monotonic()
    raw, model, tokens = _generate(config, sub, src, generate)
    ms = round((_t.monotonic() - t0) * 1000)
    out = ai4.parse("review", raw, src, labels=LABELS[sub], words=WORDS)
    if sub == "card" and data.get("verdict"):
        for x in out["lines"]:
            if x["label"] == "verdict":
                body = VERDICT_RE.sub("", x["text"]).rstrip(" ,;:.—–-")
                x["text"] = (body + "; " if body else "") + data["verdict"]
                x["verdict"] = data["verdict"]
    out["raw"] = (raw or "")[:3000]
    ai4._finish(rid, out, model, ms, now or _now())
    return {"lines": out["lines"], "dropped": len(out["dropped"]), "tokens": tokens, "note": f"market {sub} #{row.get('hint_id') or rid}"}


def enqueue(config, rid: int, key: str, note: str) -> Optional[dict]:
    """Queue the row (deduped on the key while queued). Never raises; the AI guard decides when it runs."""
    if not getattr(config.ai, "enabled", False):
        return None
    try:
        from backend.ai import queue as q
        return q.enqueue(config, QKIND, key, lambda: run_row(config, rid), model=config.ai.day_model, note=note)
    except Exception as exc:
        logger.info("Market words not queued: %s", str(exc)[:120])
        return None


def _view(rs: list[dict]) -> dict:
    from backend.tips import ai4
    r = rs[-1] if rs else None
    v = ai4.lines_view(r)
    if r and r["ok"] is None:
        v["text"] = "the words wait for the local model"
    return v


def card_view(hid: int) -> dict:
    return _view(rows("card", hid))


def entry_view(hid: int) -> dict:
    return _view(rows("entry", hid))


def panel_view(symbol: str) -> dict:
    return _view(rows("panel", symbol=symbol))


def score_view() -> dict:
    return _view(rows("score"))


# --- the inputs (code) ----------------------------------------------------------------------------------------------------

def card_input(config, h: dict, now: datetime) -> Optional[dict]:
    """The card's input text: the index facts, levels vs the strike, the pattern, the stop vs the expected range, and the
    model's numbers only when the gate shows them; the verdict by code."""
    from backend.market import chart
    from backend.market import candles as cd
    from backend.market import view as vw
    from backend.tips import book as bk
    cf = vw.card_facts(config, h, now)
    if not cf or not cf["facts"]:
        return None
    f, tr, lv = cf["facts"], cf["facts"]["trend"], cf["facts"]["levels"]
    o = tr.get("or") or {}
    pat = f.get("pattern")
    plan = h.get("plan") or {}
    prem = chart.five(cd.premium_bars(h["id"]))
    p_atr = chart.atr(prem)
    p_er = chart.expected_range(p_atr, 30)
    stop = cf["levels"].get("stop")
    lines = [f"Call #{h['id']}: {bk.title(h)} · the call wants {cf['index']} {cf['want'] or '-'}",
             f"Plan: entry {plan.get('entry_text') or '-'} · {plan.get('stop_text') or '-'} · targets {plan.get('targets_text') or '-'}",
             f"{cf['index']} now {vw.fmt(tr['last'])} at {vw.hm(tr['at'])}: trend {tr['side']} · {'above' if tr.get('above_vwap') else 'below'} VWAP {vw.fmt(tr.get('vwap'))}"
             f" · {vw.trend_text(tr)}",
             f"Levels: support {vw.fmt(lv.get('support'))} · resistance {vw.fmt(lv.get('resistance'))} · previous close {vw.fmt((lv.get('prev') or {}).get('c'))}"
             + (f" · opening range {vw.fmt(o.get('low'))}–{vw.fmt(o.get('high'))}" if o.get("high") is not None else "")
             + (f" · strike {vw.fmt(h.get('strike'))}" if h.get("strike") is not None else ""),
             "Pattern on 5 minutes: " + (f"{pat['name']} at {vw.hm(pat['at'])} ({pat['text']})" if pat else "none today"),
             f"Index expected 30-minute range ±{vw.fmt(f['exp_range']['30'], False)} points (ATR 5 min {vw.fmt(f['atr'], False)})"]
    if stop is not None:
        lines.append(f"Stop {vw.fmt(stop, False)}" + (f" · the premium's expected 30-minute range ±{vw.fmt(p_er, False)}" if p_er is not None else " · the premium's range: too few ticks yet"))
    r = cf["read"]
    if cf["gate"]["cards"] and r is not None:
        st, t1 = vw._touch(r, stop), vw._touch(r, cf["levels"].get("t1"))
        lines.append(f"Candle model ({r['version']}, {r['horizon']} min): {vw.pct(r['p_up'])} of paths up"
                     + (f" · stop touched first {vw.pct(st)}" if st is not None else "") + (f" · T1 first {vw.pct(t1)}" if t1 is not None else ""))
    lines.append(f"Verdict by code: {cf['verdict']}")
    return {"text": "\n".join(lines), "verdict": cf["verdict"], "align": cf["align"], "index": cf["index"]}


def card(config, hid: int, stage: str, now: Optional[datetime] = None, read_ids=None) -> Optional[int]:
    """At ingest, and once more on the fill: one card row (plus the entry-timing row for an unfilled zone entry)."""
    from backend.tips import book as bk
    from backend.tips import kite_orders as ko
    now = now or _now()
    h = bk.load(hid)
    if not h:
        return None
    have = rows("card", hid)
    if any((r.get("input_json") or {}).get("stage") == stage for r in have):
        return None
    data = card_input(config, h, now)
    if not data:
        return None
    rid = insert("card", hid, {**data, "stage": stage, "reads": read_ids or []}, now)
    enqueue(config, rid, f"market:card:{hid}:{stage}", f"Market read #{hid}")
    k = ko.kstate(h)
    if stage == "ingest" and k.get("entry_px") is None:
        from backend.market import view as vw
        cf = vw.card_facts(config, h, now)
        et = vw.entry_timing(config, h, cf, k) if cf else None
        if et:
            eid = insert("entry", hid, {"text": f"Call #{hid}: entry {et['zone']} · now {vw.fmt(et['now'], False) if et['now'] is not None else '-'}\n{et['code']}"}, now)
            enqueue(config, eid, f"market:entry:{hid}", f"Entry timing #{hid}")
    return rid


def _sig(f: dict) -> str:
    tr, lv = f["trend"], f["levels"]
    return json.dumps([tr["side"], tr.get("above_vwap"), (tr.get("or") or {}).get("break_at"), lv.get("support"), lv.get("resistance"),
                       (f.get("pattern") or {}).get("name")])


def panel(config, symbol: str, now: Optional[datetime] = None) -> Optional[int]:
    """The panel's backdrop for one index: a new row when the code facts changed and PANEL_GAP_S passed."""
    from backend.market import view as vw
    now = now or _now()
    if not getattr(config.ai, "enabled", False):
        return None
    f = vw.index_facts(symbol, now)
    if not f:
        return None
    have = rows("panel", symbol=symbol)
    sig = _sig(f)
    if have:
        last = have[-1]
        age = (now - datetime.fromisoformat(last["at"])).total_seconds()
        if (last.get("input_json") or {}).get("sig") == sig or age < PANEL_GAP_S:
            return None
    tr, lv = f["trend"], f["levels"]
    o = tr.get("or") or {}
    text = "\n".join([f"{symbol} now {vw.fmt(tr['last'])} at {vw.hm(tr['at'])}: trend {tr['side']} · {'above' if tr.get('above_vwap') else 'below'} VWAP {vw.fmt(tr.get('vwap'))}",
                      f"Swings: {vw.trend_text(tr)}",
                      f"Levels: support {vw.fmt(lv.get('support'))} · resistance {vw.fmt(lv.get('resistance'))} · previous close {vw.fmt((lv.get('prev') or {}).get('c'))}"
                      + (f" · opening range high {vw.fmt(o['high'])}" if o.get("high") is not None else ""),
                      "Pattern on 5 minutes: " + (f"{f['pattern']['name']} at {vw.hm(f['pattern']['at'])}" if f.get("pattern") else "none today"),
                      f"Expected 30-minute range ±{vw.fmt(f['exp_range']['30'], False)} points"])
    rid = insert("panel", None, {"text": text, "symbol": symbol, "sig": sig}, now)
    enqueue(config, rid, f"market:panel:{symbol}:{rid}", f"Market now {symbol}")
    return rid


def score_words(config, sc: list[dict], now: Optional[datetime] = None) -> Optional[int]:
    now = now or _now()
    if not sc or not getattr(config.ai, "enabled", False):
        return None
    ls = [f"{x['name']} {x['horizon']} min: {x['scored']} scored reads · direction right {x['direction_pct'] if x['direction_pct'] is not None else '-'} %"
          + (f" · first-level calls right {x['touch_pct']} %" if x.get("touch_pct") is not None else "") for x in sc]
    rid = insert("score", None, {"text": "A coin flip is 50 %.\n" + "\n".join(ls)}, now)
    enqueue(config, rid, f"market:score:{now.date().isoformat()}", "Scorecard read")
    return rid


def at12(iso: Optional[str]) -> str:
    try:
        return ampm(datetime.fromisoformat(iso)) if iso else ""
    except ValueError:
        return ""
