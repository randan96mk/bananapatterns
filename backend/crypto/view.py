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


def row_view(r: dict) -> dict:
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
            "text": sg.line(r)}


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
            "signals": [row_view(r) for r in rs], "settings": asdict(c), "defaults": asdict(CryptoConfig()),
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
            "pivots": segs, "signals": [row_view(r) for r in rs if r["tf"] == tf]}
