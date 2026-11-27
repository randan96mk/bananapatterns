"""₿ What the Strategies › ₿ BTC patterns page reads (GET /api/crypto, /api/crypto/candles, stream topic "crypto").
Read-only views of the stored candles, levels and detections; every number from code; times as 12-hour IST labels."""

import time as _time
from dataclasses import asdict
from datetime import datetime
from typing import Optional

from backend.config import IST, CryptoConfig
from backend.crypto import feed
from backend.crypto import patterns as pt
from backend.crypto import pivots as pv
from backend.crypto import signals as sg
from backend.timefmt import ampm

STATE_WORD = {"sent": "sent", "quiet": "quiet", "mark": "mark only", "confirm_wait": "confirm wait", "dropped": "dropped"}
RANGES = {"1d": 86400, "3d": 3 * 86400, "2w": 14 * 86400}
MAX_CANDLES = 5000


def when(t: int) -> str:
    """'Tue 24 Nov 2:10 AM' (IST)."""
    d = datetime.fromtimestamp(int(t), IST)
    return f"{d:%a} {d.day} {d:%b} {ampm(d)}"


def _r(v) -> str:
    return f"{float(v):+.1f} R".replace("-", "−")


def outcome_view(r: dict, after_min: int = 60, now_ts: Optional[float] = None) -> dict:
    """{state, chip, r, mins, tone}: won / lost / neither once scored, else open with the minutes left."""
    o = r.get("outcome_json")
    if isinstance(o, dict) and o.get("state"):
        st, rr, mins = o["state"], o.get("r"), o.get("mins")
        if st == "won":
            chip, tone = f"✓ 1R hit · {mins} min · {_r(1)}", "up"
        elif st == "lost":
            chip, tone = f"✗ stop · {mins} min · {_r(-1)}", "down"
        else:
            chip, tone = f"neither · {_r(rr) if rr is not None else '—'} at {mins} min", "warn"
        return {"state": st, "chip": chip, "r": rr, "mins": mins, "tone": tone}
    now_ts = _time.time() if now_ts is None else now_ts
    left = int(r["t"]) + pv.TF_SECONDS.get(r.get("tf") or "5m", 300) + int(after_min) * 60 - now_ts
    chip = f"open · {max(1, -(-int(left) // 60))} min to score" if left > 0 else "open · waiting for the candles"
    return {"state": "open", "chip": chip, "r": None, "mins": None, "tone": "dim"}


def grade_view(r: dict) -> dict:
    gj = r.get("grade_json") if isinstance(r.get("grade_json"), dict) else {}
    if not gj:
        return {"g": None, "why": "not graded yet", "n": 0, "few": True, "model_step": None}
    return {"g": r.get("grade") or gj.get("g"), "why": gj.get("why"), "n": gj.get("n") or 0, "few": bool(gj.get("few")),
            "model_step": gj.get("model_step")}


def forecast_view(r: dict, times: bool = False) -> Optional[dict]:
    """The candle model's read for this row: {chip, p, dir, exp_px, exp_range, touch_stop, touch_r1, horizon_min, paths
    (+ times: the chart's x for each step, IST-shifted unix)} or None."""
    fc = r.get("fc_json") if isinstance(r.get("fc_json"), dict) else None
    if not fc or fc.get("p_up") is None:
        return None
    p_up = float(fc["p_up"])
    p = max(p_up, 1 - p_up)
    pc = round(100 * p)
    if abs(p_up - 0.5) < 0.02:
        chip = f"{pc} % · flat"
    else:
        chip = f"{pc} % {'up' if p_up > 0.5 else 'down'}"
        if r.get("kind") == "forecast" and fc.get("exp_range") is not None:
            chip += f" · ±{sg.px(round(float(fc['exp_range'])))}"
        elif fc.get("touch_r1") is not None:
            chip += f" · 1R first {round(100 * float(fc['touch_r1']))} %"
    out = {"chip": chip, "p": round(p, 3), "dir": "long" if p_up > 0.5 else "short", "exp_px": fc.get("exp_px"), "exp_range": fc.get("exp_range"),
           "touch_stop": fc.get("touch_stop"), "touch_r1": fc.get("touch_r1"), "horizon_min": fc.get("horizon_min"), "paths": fc.get("paths"),
           "n": len((fc.get("paths") or {}).get("median") or []), "right": fc.get("right")}
    if times:
        med = (fc.get("paths") or {}).get("median") or []
        s = pv.TF_SECONDS.get(r.get("tf") or "5m", 300)
        out["times"] = [int(r["t"]) + (j + 1) * s + pv.IST_OFFSET for j in range(len(med))]
        out["t0"] = int(r["t"]) + pv.IST_OFFSET
    return out


def _chip_of(chip: Optional[str]) -> str:
    """'candle model · shadow · BTC hit rate 52 % over 38' -> 'shadow · BTC hit rate 52 % over 38'."""
    chip = chip or "candle model · off"
    return chip[len("candle model · "):] if chip.startswith("candle model · ") else chip


def forecast_now(config, minutes: int = 30, now_ts: Optional[float] = None, model_chip: Optional[str] = None) -> dict:
    """GET /api/crypto/forecast_now: the latest per-candle read kept by backend/crypto/forecast.py (LATEST; this never
    calls the worker) + model_chip, next_at_label (the next candle close), stale (older than 2 candles) and the candle's
    label; {"worker": "down", ...} when the last read failed with nothing kept, {"empty": true, ...} when nothing is cached."""
    from backend.crypto import forecast
    from backend.crypto import outcomes
    c = config.crypto
    now_ts = _time.time() if now_ts is None else now_ts
    s = pv.TF_SECONDS.get(c.tf, 300)
    nxt = (int(now_ts) // s + 1) * s
    if model_chip is None:
        try:
            model_chip = outcomes.model_chip(config)
        except Exception:
            model_chip = None
    base = {"horizon_min": int(minutes), "tf": c.tf, "model_chip": _chip_of(model_chip), "next_at": nxt,
            "next_at_label": ampm(datetime.fromtimestamp(nxt, IST))}
    r = forecast.LATEST.get(int(minutes))
    if not r or r.get("symbol") != c.symbol or r.get("tf") != c.tf:
        return {**base, "empty": True, "worker": None, "stale": False}
    t = int(r["t"])
    stale = now_ts - (t + s) > 2 * s
    return {**r, **base, "empty": False, "stale": bool(stale), "candle_label": ampm(datetime.fromtimestamp(t, IST)), "when": when(t)}


def row_view(r: dict, after_min: int = 60, now_ts: Optional[float] = None, times: bool = False) -> dict:
    f = r.get("facts_json") or {}
    aj = r.get("ai_json") or {}
    model = aj.get("model") or {}
    return {"id": r["id"], "t": r["t"], "when": when(r["t"]), "pattern": r["pattern"], "label": pt.LABEL.get(r["pattern"], r["pattern"]),
            "dir": r["dir"], "tf": r["tf"], "level": r["level_name"], "level_px": r["level_px"], "entry": r["entry"], "stop": r["stop"], "r1": r["r1"],
            "capped": bool(f.get("capped")), "ema_ok": None if r["ema_ok"] is None else bool(r["ema_ok"]), "vol_ok": None if r["vol_ok"] is None else bool(r["vol_ok"]),
            "vol_x": f.get("vol_x"), "ema_len": f.get("ema_len"), "state": r["state"], "state_word": STATE_WORD.get(r["state"], r["state"]),
            "why": r.get("why"), "sent_at": r.get("sent_at"), "sent_label": ampm(datetime.fromisoformat(r["sent_at"])) if r.get("sent_at") else None,
            "png": bool(r.get("png_path")), "t0": f.get("t0") or r["t"],
            "ai": {"line": aj.get("line"), "verdict": aj.get("verdict"), "state": aj.get("state"), "facts": aj.get("facts"),
                   "model": {"p_up": model.get("p_up"), "stop_first": model.get("stop_first"), "r1_first": model.get("r1_first")} if model else None},
            "text": sg.line(r), "kind": r.get("kind") or "pattern", "grade": grade_view(r), "forecast": forecast_view(r, times),
            "outcome": outcome_view(r, after_min, now_ts)}


def summary(config, service=None, now_ts: Optional[float] = None) -> dict:
    """The page: state, feed health, the pivots in force, the alert hours, today's counts, the AI, the detections
    (newest first, 300), the settings and the defaults."""
    c = config.crypto
    now_ts = _time.time() if now_ts is None else now_ts
    now = datetime.fromtimestamp(now_ts, IST)
    sv = feed.saved_health()
    h = feed.health(c.symbol, c.tf, now_ts, errors=int(getattr(service, "errors", 0) or sv.get("errors") or 0),
                    last_error=getattr(service, "last_error", None) or sv.get("last_error"))
    lv = sg.levels_for(config, int(now_ts) - int(now_ts) % pv.TF_SECONDS[c.tf])
    start = datetime(now.year, now.month, now.day, tzinfo=IST).timestamp()
    rs = sg.rows("symbol = ?", (c.symbol,), limit=300)
    today = [r for r in rs if r["t"] >= start]
    counts = {k: sum(1 for r in today if r["state"] == k) for k in STATE_WORD}
    q = sg.quiet_until(now)
    from backend.market import reads as rd
    last_ai = next((r for r in rs if (r.get("ai_json") or {}).get("line")), None)
    sc = _scores(config, 30, now_ts)
    return {"enabled": bool(c.enabled), "running": bool(service and getattr(service, "running", False)), "symbol": c.symbol, "tf": c.tf,
            "source": feed.SOURCE, "health": h, "now_label": ampm(now),
            "pivots": {"type": c.pivot_type, "period": pv.auto_period(c.tf, c.pivot_tf), "pivot_tf": c.pivot_tf, "boundary": c.day_boundary,
                       "from": (lv or {}).get("from"), "levels": (lv or {}).get("levels")},
            "hours": {"from": c.alert_from, "until": c.alert_until, "from12": ampm(c.alert_from), "until12": ampm(c.alert_until),
                      "quiet_until": q.isoformat(timespec="seconds") if q else None, "quiet_label": ampm(q) if q else None,
                      "inside": sg.in_hours(config, now), "text": sg.hours_text(config, now)},
            "today": counts, "today_total": len(today),
            "ai": {"read": bool(c.ai_read), "local_ai": bool(getattr(config.ai, "enabled", False)), "model_chip": rd.chip(config) if rd.on(config) else "candle model · off",
                   "last": {"id": last_ai["id"], "when": when(last_ai["t"]), "line": last_ai["ai_json"]["line"]} if last_ai else None},
            "signals": [row_view(r, int(c.score_after_min), now_ts) for r in rs], "scores": sc,
            "forecast_now": forecast_now(config, 30, now_ts, (sc or {}).get("model_chip")), "settings": asdict(c), "defaults": asdict(CryptoConfig()),
            "patterns": [{"key": p, "label": pt.LABEL[p], "dir": pt.direction(p), "confirm": f"confirm_{p}" in asdict(c)} for p in (*pt.BULLISH, *pt.BEARISH)]}


def candles(config, tf: Optional[str] = None, start: Optional[int] = None, end: Optional[int] = None, rng: str = "3d") -> dict:
    """The chart: candles (time = unix + the IST offset, so the axis reads IST), the EMA, the pivot levels per period
    (segments), the detections in range. At most MAX_CANDLES (the newest)."""
    c = config.crypto
    tf = tf if tf in pv.TF_SECONDS and tf != "1d" else c.tf
    end = int(end or _time.time())
    start = int(start or end - RANGES.get(rng, RANGES["3d"]))
    warm = int(c.ema_len) * 3 * pv.TF_SECONDS[tf]
    allc = feed.load(c.symbol, tf, start - warm, end)
    e = pt.ema([b["c"] for b in allc], int(c.ema_len))
    keep = [i for i, b in enumerate(allc) if b["t"] >= start][-MAX_CANDLES:]
    off = pv.IST_OFFSET
    cs = [allc[i] for i in keep]
    out_c = [{"time": b["t"] + off, "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "volume": b.get("v")} for b in cs]
    out_e = [{"time": allc[i]["t"] + off, "value": round(e[i], 2)} for i in keep if e[i] is not None]
    segs = []
    if cs:
        period = pv.auto_period(c.tf, c.pivot_tf)
        seen = set()
        for b in cs:
            k = pv.period_start(b["t"], period, c.day_boundary)
            if k in seen:
                continue
            seen.add(k)
            lv = sg.levels_for(config, b["t"])
            if lv:
                segs.append({"start": max(k, cs[0]["t"]) + off, "levels": lv["levels"], "from": lv["from"]})
        for i, s in enumerate(segs):
            s["end"] = (segs[i + 1]["start"] if i + 1 < len(segs) else cs[-1]["t"] + off)
    rs = sg.rows("symbol = ? AND t >= ? AND t <= ?", (c.symbol, start, end), limit=500) if cs else []
    return {"symbol": c.symbol, "tf": tf, "start": start, "end": end, "offset": off, "candles": out_c, "ema": out_e, "ema_len": int(c.ema_len),
            "pivots": segs, "signals": [row_view(r, int(c.score_after_min), times=True) for r in rs if r["tf"] == tf]}


def _scores(config, days: int, now_ts: Optional[float] = None) -> Optional[dict]:
    from backend.crypto import outcomes
    try:
        return outcomes.scores(config, days, now_ts)
    except Exception:
        return None


def scores(config, days: int = 30, now_ts: Optional[float] = None) -> dict:
    """GET /api/crypto/scores?days=1|7|30: the tiles' numbers (outcomes.scores)."""
    from backend.crypto import outcomes
    return outcomes.scores(config, days, now_ts)
