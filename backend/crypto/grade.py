"""₿ The grade, by code from the record (never by a model on its own):

  record   the last 30 days of scored rows like this one: the same pattern family (hammer / inverted hammer / dragonfly
           and their bearish twins · engulfing / tweezer / piercing / dark cloud · the stars · harami · forecast), the
           same level kind (R any, S any, P), the same direction, the same session bucket (IST 00–08, 08–16, 16–24):
           n, won, pct, avg_r
  context  ema_ok · volume ≥ 1.2× its average · room to the next pivot ≥ 0.6 × ATR (in the trade's direction)
  grade    A ≥ 60 % won and avg R > 0 and context 3/3 · B ≥ 50 % and 2/3 · C ≥ 40 % or 1/3 · D else;
           n < MIN_N: no letter ("too few like this (n)")
  model    the candle model's touch_r1 − touch_stop ≥ +0.15 → +1, ≤ −0.15 → −1: applied (one step) only once its BTC
           scorecard passes the gate (outcomes.model_card), else shown as "shadow +1" in the why.
grade_json = {g, why, n, won, pct, avg_r, few, ctx {ema, vol, room}, ctx_n, model_step, model_applied, family, kind, bucket}."""

from datetime import datetime
from typing import Optional

from backend.config import IST
from backend.crypto import patterns as pt
from backend.timefmt import ampm

DAYS = 30
MIN_N = 10
VOL_X, ROOM_ATR, STEP = 1.2, 0.6, 0.15
LETTERS = ("D", "C", "B", "A")
FAMILY = {"hammer": "wick", "inverted_hammer": "wick", "dragonfly_doji": "wick", "shooting_star": "wick", "hanging_man": "wick",
          "gravestone_doji": "wick", "bullish_engulfing": "engulf", "bearish_engulfing": "engulf", "tweezer_bottom": "engulf",
          "tweezer_top": "engulf", "piercing_line": "engulf", "dark_cloud_cover": "engulf", "morning_star": "star",
          "evening_star": "star", "bullish_harami": "harami", "bearish_harami": "harami"}
FAMILY_WORD = {("wick", "long"): "hammer/dragonfly", ("wick", "short"): "shooting star/gravestone", ("engulf", "long"): "tweezer/engulfing",
               ("engulf", "short"): "tweezer/engulfing", ("star", "long"): "morning star", ("star", "short"): "evening star",
               ("harami", "long"): "harami", ("harami", "short"): "harami", ("forecast", "long"): "forecast", ("forecast", "short"): "forecast"}
BUCKETS = ((0, 8), (8, 16), (16, 24))


def family(pattern: str, kind: str = "pattern") -> str:
    if kind == "forecast" or str(pattern).startswith("forecast"):
        return "forecast"
    return FAMILY.get(pattern, pattern)


def level_kind(name: Optional[str]) -> str:
    n = str(name or "").replace("near ", "")
    return "R" if n.startswith("R") else "S" if n.startswith("S") else "P"


def bucket(t: int) -> int:
    h = datetime.fromtimestamp(int(t), IST).hour
    return next(i for i, (a, b) in enumerate(BUCKETS) if a <= h < b)


def bucket_word(i: int) -> str:
    a, b = BUCKETS[i]
    return f"{ampm(f'{a:02d}:00')}–{ampm(f'{b % 24:02d}:00')}"


def model_step(fc: Optional[dict]) -> Optional[int]:
    if not fc or fc.get("touch_r1") is None or fc.get("touch_stop") is None:
        return None
    d = float(fc["touch_r1"]) - float(fc["touch_stop"])
    return 1 if d >= STEP else -1 if d <= -STEP else 0


def context(det: dict, levels: Optional[dict], atr: Optional[float]) -> dict:
    """{ema, vol, room}: each True / False (unknown counts as not met)."""
    vol_x = det.get("vol_x")
    if vol_x is None:
        vol_x = (det.get("facts_json") or {}).get("vol_x")
    entry, long_ = det.get("entry"), det["dir"] == "long"
    room = None
    if entry is not None and levels:
        ahead = [v for v in levels.values() if (v > entry if long_ else v < entry)]
        room = (min(abs(v - entry) for v in ahead) if ahead else None)
    room_ok = bool(atr) and (room is None or room >= ROOM_ATR * atr) if entry is not None else False
    return {"ema": bool(det.get("ema_ok")), "vol": vol_x is not None and float(vol_x) >= VOL_X, "room": bool(room_ok)}


def letter(pct: float, avg_r: float, ctx_n: int) -> str:
    if pct >= 60 and avg_r > 0 and ctx_n == 3:
        return "A"
    if pct >= 50 and ctx_n >= 2:
        return "B"
    if pct >= 40 or ctx_n >= 1:
        return "C"
    return "D"


def shift(g: Optional[str], step: int) -> Optional[str]:
    if g is None or not step:
        return g
    return LETTERS[max(0, min(3, LETTERS.index(g) + step))]


def record(config, fam: str, lk: str, direction: str, bk: int, now_ts: float, exclude_id: Optional[int] = None) -> dict:
    """The scored rows like this one in the DAYS before now_ts: {n, won, pct, avg_r}."""
    from backend.crypto import pivots as pv
    from backend.crypto import signals as sg
    known = int(now_ts) - pv.TF_SECONDS.get(config.crypto.tf, 300) - int(config.crypto.score_after_min) * 60   # decided by now_ts
    rs = sg.rows("symbol = ? AND dir = ? AND outcome_json IS NOT NULL AND t >= ? AND t <= ?",
                 (config.crypto.symbol, direction, int(now_ts) - DAYS * 86400, known), order="t")
    xs = [r for r in rs if r["id"] != exclude_id and isinstance(r.get("outcome_json"), dict)
          and family(r["pattern"], r.get("kind") or "pattern") == fam and level_kind(r.get("level_name")) == lk and bucket(r["t"]) == bk]
    n = len(xs)
    won = sum(1 for r in xs if r["outcome_json"]["state"] == "won")
    rr = [float(r["outcome_json"]["r"]) for r in xs if r["outcome_json"].get("r") is not None]
    return {"n": n, "won": won, "pct": round(100 * won / n) if n else None, "avg_r": round(sum(rr) / len(rr), 2) if rr else None}


def _r(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:+.1f} R".replace("-", "−")


def grade_for(config, det: dict, now_ts: float, levels: Optional[dict] = None, atr: Optional[float] = None, fc: Optional[dict] = None,
              gate_ok: Optional[bool] = None, exclude_id: Optional[int] = None) -> dict:
    """The grade of a detection (a detect() dict or a stored row) at now_ts: grade_json (see the module doc)."""
    kind = det.get("kind") or ("forecast" if str(det.get("pattern", "")).startswith("forecast") else "pattern")
    fam = family(det["pattern"], kind)
    lk, bk = level_kind(det.get("level_name")), bucket(det["t"])
    rec = record(config, fam, lk, det["dir"], bk, now_ts, exclude_id)
    ctx = context(det, levels, atr)
    ctx_n = sum(ctx.values())
    step = model_step(fc)
    if gate_ok is None and step:
        from backend.crypto import outcomes
        gate_ok = outcomes.fc_gate_ok(config)
    side = "longs" if det["dir"] == "long" else "shorts"
    lw = {"R": "R levels", "S": "S levels", "P": "P"}[lk]
    head = f"{FAMILY_WORD.get((fam, det['dir']), fam)} {side} at {lw} {bucket_word(bk)}"
    out = {"n": rec["n"], "won": rec["won"], "pct": rec["pct"], "avg_r": rec["avg_r"], "ctx": ctx, "ctx_n": ctx_n, "model_step": step,
           "model_applied": False, "family": fam, "kind": lk, "bucket": bk}
    if rec["n"] < MIN_N:
        return {**out, "g": None, "few": True, "why": f"too few like this ({rec['n']})"}
    g = letter(rec["pct"] or 0, rec["avg_r"] or 0, ctx_n)
    why = f"{head} · {rec['won']} of {rec['n']} won · {_r(rec['avg_r'])} avg · context {ctx_n}/3"
    if step:
        if gate_ok:
            g = shift(g, step)
            out["model_applied"] = True
            why += f" · model {step:+d}"
        else:
            why += f" · shadow {step:+d}"
    return {**out, "g": g, "few": False, "why": why}


def below(g: Optional[str], minimum: str) -> bool:
    """A grade below the minimum (all: never; no letter: never)."""
    if minimum == "all" or g is None:
        return False
    return LETTERS.index(g) < LETTERS.index(minimum)


def label(r: dict) -> str:
    return pt.LABEL.get(r["pattern"], r["pattern"])


def backfill(config, limit: int = 5000) -> int:
    """Grade the stored rows without a grade (the detections from before the grade, the history scan) from the record
    before each row's time, oldest first; the context from the stored facts and candles. Returns how many."""
    from backend.crypto import feed
    from backend.crypto import signals as sg
    c = config.crypto
    rs = sg.rows("symbol = ? AND tf = ? AND grade_json IS NULL", (c.symbol, c.tf), order="t", limit=limit)
    n = 0
    for r in rs:
        f = r.get("facts_json") or {}
        cs = feed.load(c.symbol, c.tf, end=r["t"], limit=60)
        atr = sg._atr(cs) if cs else None
        det = {**r, "vol_x": f.get("vol_x"), "kind": r.get("kind") or "pattern"}
        gj = grade_for(config, det, r["t"], levels=f.get("levels"), atr=atr, fc=r.get("fc_json"), exclude_id=r["id"])
        sg.update(r["id"], grade=gj.get("g"), grade_json=gj)
        n += 1
    return n
