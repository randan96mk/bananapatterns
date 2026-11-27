"""₿ Outcomes by code: every signal (any state, any kind) is scored once, score_after_min (60) minutes after its candle
closed, from the stored candles after it (never rescored, never deleted).

  won      1R touched first (+1 R)
  lost     the stop touched first (−1 R); both inside one candle counts as lost (conservative)
  neither  neither by the end: the R at the last candle's close
outcome_json = {state, r, mins (from the signal candle's close to the deciding candle's close), px_at, scored_at}.

The candle model's read stored with a signal (fc_json) is scored here too, by code: its direction (p_up > 0.5 = up)
against the close `horizon_min` after the signal candle -> fc_json.right (bool), fc_json.actual_px. That is the BTC-only
scorecard the grade's model step and the model chip read (`model_card`). The forecast rows' own gate is `forecast_card`.
R and points only, no money; alerts only: nothing here places an order."""

import json
import time as _time
from datetime import datetime
from typing import Optional

from backend.config import FORECAST_GATE_N, FORECAST_GATE_PCT, IST
from backend.crypto import feed
from backend.crypto import pivots as pv
from backend.database.models import get_db
from backend.logging_config.logger import setup_logger

logger = setup_logger("crypto")

MODEL_GATE_N, MODEL_GATE_PCT = FORECAST_GATE_N, FORECAST_GATE_PCT       # the candle model's BTC scorecard gate
BUCKETS = ((58, 62), (62, 66), (66, 70), (70, 101))


def _risk(r: dict) -> Optional[float]:
    if r.get("entry") is None or r.get("stop") is None:
        return None
    risk = abs(float(r["entry"]) - float(r["stop"]))
    return risk or None


def _r_at(r: dict, px: float) -> Optional[float]:
    risk = _risk(r)
    if risk is None:
        return None
    move = (px - float(r["entry"])) if r["dir"] == "long" else (float(r["entry"]) - px)
    return round(move / risk, 2)


def score_one(r: dict, candles: list[dict], tf_s: int, after_min: int, now_ts: Optional[float] = None) -> Optional[dict]:
    """The outcome of one signal from the candles that follow it, or None while not due / no candles cover the window."""
    if _risk(r) is None or r.get("r1") is None:
        return None
    t_close = int(r["t"]) + tf_s
    end = t_close + int(after_min) * 60
    if now_ts is not None and now_ts < end:
        return None
    win = [b for b in candles if int(r["t"]) < b["t"] and b["t"] + tf_s <= end]
    if not win:
        return None
    long_ = r["dir"] == "long"
    stop, r1 = float(r["stop"]), float(r["r1"])
    at = datetime.now(IST).isoformat(timespec="seconds")
    for b in win:
        hit_stop = b["l"] <= stop if long_ else b["h"] >= stop
        hit_r1 = b["h"] >= r1 if long_ else b["l"] <= r1
        mins = (b["t"] + tf_s - t_close) // 60
        if hit_stop:                                         # both in one candle: lost
            return {"state": "lost", "r": -1.0, "mins": int(mins), "px_at": stop, "scored_at": at}
        if hit_r1:
            return {"state": "won", "r": 1.0, "mins": int(mins), "px_at": r1, "scored_at": at}
    last = win[-1]
    if last["t"] + tf_s < end and (now_ts is None or now_ts < end + 86400):   # the end of the window not stored yet: wait (a day at most)
        return None
    return {"state": "neither", "r": _r_at(r, last["c"]), "mins": int(after_min), "px_at": last["c"], "scored_at": at}


def score_model(r: dict, candles: list[dict], tf_s: int, now_ts: Optional[float] = None) -> Optional[dict]:
    """fc_json + {right, actual_px} once the close `horizon_min` after the signal candle is stored, else None."""
    fc = r.get("fc_json") or {}
    if not fc or "right" in fc or fc.get("p_up") is None:
        return None
    steps = max(1, int(fc.get("horizon_min") or 30) * 60 // tf_s)
    t_h = int(r["t"]) + steps * tf_s
    if now_ts is not None and now_ts < t_h + tf_s:          # that candle has not closed yet
        return None
    b = next((x for x in candles if x["t"] == t_h), None)
    if b is None:
        return None
    last = fc.get("last")
    if last is None:
        last = ((r.get("facts_json") or {}).get("close"))
    if last is None or float(fc["p_up"]) == 0.5:
        return {**fc, "right": None, "actual_px": b["c"]}
    up = float(fc["p_up"]) > 0.5
    return {**fc, "right": (b["c"] > float(last)) == up if b["c"] != float(last) else False, "actual_px": b["c"]}


def score_due(config, now_ts: Optional[float] = None, limit: int = 5000) -> dict:
    """Score every due signal without an outcome (and every model read without `right`). Runs from the CryptoService
    loop each candle and once at start (the backfill of the older rows). {scored, model}."""
    from backend.crypto import signals as sg
    c = config.crypto
    now_ts = _time.time() if now_ts is None else now_ts
    tf_s = pv.TF_SECONDS.get(c.tf, 300)
    after = int(c.score_after_min)
    cutoff = int(now_ts) - tf_s - after * 60
    due = sg.rows("symbol = ? AND tf = ? AND outcome_json IS NULL AND t <= ?", (c.symbol, c.tf, cutoff), order="t", limit=limit)
    mdue = sg.rows("symbol = ? AND tf = ? AND fc_json IS NOT NULL AND fc_json NOT LIKE '%\"right\"%' AND t <= ?",
                   (c.symbol, c.tf, int(now_ts) - tf_s), order="t", limit=limit)
    if not due and not mdue:
        return {"scored": 0, "model": 0}
    t_lo = min(r["t"] for r in (due + mdue))
    t_hi = max(r["t"] for r in (due + mdue)) + tf_s + max(after, 120) * 60
    cs = feed.load(c.symbol, c.tf, t_lo, t_hi)
    n = m = 0
    for r in due:
        out = score_one(r, cs, tf_s, after, now_ts)
        if out:
            with get_db() as conn:
                conn.execute("UPDATE crypto_signals SET outcome_json = ? WHERE id = ? AND outcome_json IS NULL", (json.dumps(out), r["id"]))
            n += 1
    for r in mdue:
        fc = score_model(r, cs, tf_s, now_ts)
        if fc:
            sg.update(r["id"], fc_json=fc)
            m += 1
    return {"scored": n, "model": m}


# --- the scorecards ---------------------------------------------------------------------------------------------------------

def _pct(won: int, n: int) -> Optional[int]:
    return round(100 * won / n) if n else None


def model_card(config) -> dict:
    """The candle model's BTC-only scorecard: {n, hit, pct, ok} (ok = ≥ MODEL_GATE_N scored reads and ≥ MODEL_GATE_PCT %)."""
    with get_db() as conn:
        rs = conn.execute("SELECT fc_json FROM crypto_signals WHERE symbol = ? AND fc_json LIKE '%\"right\"%'", (config.crypto.symbol,)).fetchall()
    n = hit = 0
    for (raw,) in rs:
        try:
            v = json.loads(raw).get("right")
        except (TypeError, ValueError, AttributeError):
            continue
        if v is None:
            continue
        n += 1
        hit += bool(v)
    pct = _pct(hit, n)
    return {"n": n, "hit": hit, "pct": pct, "ok": n >= MODEL_GATE_N and (pct or 0) >= MODEL_GATE_PCT}


def fc_gate_ok(config) -> bool:
    return model_card(config)["ok"]


def model_chip(config, card: Optional[dict] = None) -> str:
    """'candle model · shadow · BTC hit rate 52 % over 38'."""
    from backend.market import reads as rd
    if not rd.on(config):
        return "candle model · off"
    card = card or model_card(config)
    word = "in the grade" if card["ok"] else "shadow"
    if not card["n"]:
        return f"candle model · {word} · no BTC reads scored yet"
    return f"candle model · {word} · BTC hit rate {card['pct']} % over {card['n']}"


def _scored(config, where: str = "1", args: tuple = ()) -> list[dict]:
    from backend.crypto import signals as sg
    return [r for r in sg.rows(f"symbol = ? AND outcome_json IS NOT NULL AND {where}", (config.crypto.symbol, *args), order="t")
            if isinstance(r.get("outcome_json"), dict)]


def forecast_card(config) -> dict:
    """The forecast rows' gate (all scored forecast rows): {n, won, pct, ok, why}."""
    rs = _scored(config, "kind = 'forecast'")
    n, won = len(rs), sum(1 for r in rs if r["outcome_json"]["state"] == "won")
    pct = _pct(won, n)
    ok = n >= FORECAST_GATE_N and (pct or 0) >= FORECAST_GATE_PCT
    why = (f"gate passed: {n} scored · {pct} % won" if ok else f"held in shadow: needs {FORECAST_GATE_N} scored and {FORECAST_GATE_PCT} %")
    return {"n": n, "won": won, "pct": pct, "ok": ok, "why": why}


def scores(config, days: int = 30, now_ts: Optional[float] = None) -> dict:
    """The page's tiles: direction, by grade, the forecast rows (buckets + the gate), by pattern and level, the model chip."""
    now_ts = _time.time() if now_ts is None else now_ts
    days = int(days) if int(days) in (1, 7, 30) else 30
    rs = _scored(config, "t >= ?", (int(now_ts) - days * 86400,))
    pat = [r for r in rs if (r.get("kind") or "pattern") == "pattern"]
    fcs = [r for r in rs if r.get("kind") == "forecast"]

    def won(r):
        return r["outcome_json"]["state"] == "won"

    def rsum(xs):
        return round(sum(float(r["outcome_json"].get("r") or 0) for r in xs), 1)

    direction = {d: {"n": len(xs), "won": sum(map(won, xs)), "r": rsum(xs)}
                 for d in ("long", "short") for xs in [[r for r in pat if r["dir"] == d]]}
    by_grade = {}
    for g in ("A", "B", "C", "D"):
        xs = [r for r in pat if r.get("grade") == g]
        by_grade[g] = {"n": len(xs), "won": sum(map(won, xs)), "pct": _pct(sum(map(won, xs)), len(xs))}
    buckets = []
    for lo, hi in BUCKETS:
        xs = [r for r in fcs if lo <= round(100 * float(((r.get("fc_json") or {}).get("p") or 0))) < hi]
        buckets.append({"lo": lo, "hi": min(hi, 100), "n": len(xs), "pct": _pct(sum(map(won, xs)), len(xs))})
    fwon = sum(map(won, fcs))
    groups: dict = {}
    for r in pat:
        groups.setdefault((r["pattern"], r["dir"]), []).append(r)
    by_pattern = []
    for (p, d), xs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        at = {}
        for name in sorted({x.get("level_name") or "—" for x in xs}):
            ys = [x for x in xs if (x.get("level_name") or "—") == name]
            at[name] = {"n": len(ys), "pct": _pct(sum(map(won, ys)), len(ys))}
        rr = [float(x["outcome_json"]["r"]) for x in xs if x["outcome_json"].get("r") is not None]
        by_pattern.append({"pattern": p, "dir": d, "n": len(xs), "won": sum(map(won, xs)), "pct": _pct(sum(map(won, xs)), len(xs)),
                           "avg_r": round(sum(rr) / len(rr), 2) if rr else None, "at": at})
    card = forecast_card(config)
    mc = model_card(config)
    return {"days": days, "direction": direction, "by_grade": by_grade,
            "forecast": {"scored": len(fcs), "won": fwon, "pct": _pct(fwon, len(fcs)), "buckets": buckets,
                         "gate": {"ok": card["ok"], "why": card["why"], "n": card["n"], "pct": card["pct"]}},
            "by_pattern": by_pattern, "model": mc, "model_chip": model_chip(config, mc)}
