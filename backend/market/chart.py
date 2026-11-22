"""📈 Chart facts by code: pure functions over 1-minute candles [{d 'YYYY-MM-DD', t 'HH:MM', o, h, l, c, v}] (oldest
first, several sessions). Nothing here reads the database, the model or the clock.

  trend     above / below VWAP (today's 1-minute bars; an index has no volume, so equal weights then), the 20 vs 50
            average on 5-minute closes, higher highs / lower lows over the last 12 five-minute bars, the opening range
            9:15–9:30 and the time it broke
  levels    previous session high / low / close, today's high / low, swing support / resistance from the last 2
            sessions' 5-minute pivots, round numbers
  patterns  on 5-minute bars: engulfing, pin bar, inside bar, 3 soldiers / crows, doji at a level, flag (3+ small inside
            bars after a breakout bar), each with the time it formed
  range     ATR(14) on 5 minutes; the expected range over h minutes = ATR × sqrt(h / 5)"""

import math
from typing import Optional

from backend.options.candles_util import resample as _resample
from backend.timefmt import ampm

OR_END = "09:30"
SWING_K = 2                       # a pivot high: higher than the 2 five-minute bars on each side
ROUND_STEP = {"NIFTY": 100, "BANKNIFTY": 500, "SENSEX": 500}


def days(bars: list[dict]) -> list[str]:
    return sorted({b["d"] for b in bars})


def of_day(bars: list[dict], day: str) -> list[dict]:
    return [b for b in bars if b["d"] == day]


def five(bars: list[dict]) -> list[dict]:
    """1-minute bars of several sessions -> 5-minute bars (per session, aligned to 9:15), each keeping its day."""
    out = []
    for d in days(bars):
        out += [{**b, "d": d} for b in _resample([dict(x) for x in of_day(bars, d)], 5)]
    return out


def sma(vals: list[float], n: int) -> Optional[float]:
    return round(sum(vals[-n:]) / n, 2) if len(vals) >= n else None


def vwap(day_bars: list[dict]) -> Optional[float]:
    """Typical price weighted by volume; with no volume (an index) every bar weighs the same."""
    if not day_bars:
        return None
    tp = [(b["h"] + b["l"] + b["c"]) / 3 for b in day_bars]
    vol = [float(b.get("v") or 0) for b in day_bars]
    if sum(vol) > 0:
        return round(sum(p * v for p, v in zip(tp, vol)) / sum(vol), 2)
    return round(sum(tp) / len(tp), 2)


def opening_range(day_bars: list[dict]) -> Optional[dict]:
    """{high, low, break_dir up|down|None, break_at 'HH:MM'|None}: 9:15–9:30, then the first 1-minute CLOSE outside."""
    orb = [b for b in day_bars if b["t"] < OR_END]
    if not orb:
        return None
    hi, lo = max(b["h"] for b in orb), min(b["l"] for b in orb)
    out = {"high": round(hi, 2), "low": round(lo, 2), "break_dir": None, "break_at": None, "complete": any(b["t"] >= OR_END for b in day_bars)}
    for b in day_bars:
        if b["t"] < OR_END:
            continue
        if b["c"] > hi:
            out.update(break_dir="up", break_at=b["t"])
            break
        if b["c"] < lo:
            out.update(break_dir="down", break_at=b["t"])
            break
    return out


def hh_ll(b5: list[dict], n: int = 12) -> Optional[dict]:
    """Higher highs / lower lows over the last n five-minute bars (3 blocks of n/3: each block's high and low above /
    below the previous block's). {kind 'higher highs'|'lower lows', since 'HH:MM'} or None."""
    if len(b5) < n:
        return None
    w = b5[-n:]
    k = n // 3
    blocks = [w[i * k:(i + 1) * k] for i in range(3)]
    hs = [max(b["h"] for b in x) for x in blocks]
    ls = [min(b["l"] for b in x) for x in blocks]
    if hs[0] < hs[1] < hs[2] and ls[0] < ls[1] < ls[2]:
        return {"kind": "higher highs", "since": w[0]["t"]}
    if hs[0] > hs[1] > hs[2] and ls[0] > ls[1] > ls[2]:
        return {"kind": "lower lows", "since": w[0]["t"]}
    return None


def trend(bars: list[dict]) -> Optional[dict]:
    """{side up|down|flat, last, vwap, above_vwap, avg20, avg50, avg_cross '20 > 50'|'20 < 50'|None, hh_ll, or}."""
    if not bars:
        return None
    today = of_day(bars, bars[-1]["d"])
    b5 = five(bars)
    closes = [b["c"] for b in b5]
    last = bars[-1]["c"]
    vw = vwap(today)
    a20, a50 = sma(closes, 20), sma(closes, 50)
    hl = hh_ll([b for b in b5 if b["d"] == bars[-1]["d"]]) or hh_ll(b5)
    orr = opening_range(today)
    score = 0
    score += (1 if last > vw else -1) if vw is not None else 0
    score += (1 if a20 > a50 else -1) if a20 is not None and a50 is not None else 0
    score += (1 if hl["kind"] == "higher highs" else -1) if hl else 0
    side = "up" if score >= 2 else "down" if score <= -2 else "flat"
    return {"side": side, "score": score, "last": round(last, 2), "vwap": vw, "above_vwap": (last > vw) if vw is not None else None,
            "avg20": a20, "avg50": a50, "avg_cross": None if a20 is None or a50 is None else ("20 > 50" if a20 > a50 else "20 < 50"),
            "hh_ll": hl, "or": orr, "at": bars[-1]["t"], "day": bars[-1]["d"]}


def pivots(b5: list[dict], k: int = SWING_K) -> tuple[list[dict], list[dict]]:
    """(highs, lows): 5-minute pivot highs / lows (k bars each side)."""
    hi, lo = [], []
    for i in range(k, len(b5) - k):
        win = b5[i - k:i + k + 1]
        if b5[i]["h"] == max(b["h"] for b in win) and all(b5[i]["h"] > b["h"] for j, b in enumerate(win) if j != k):
            hi.append({"px": round(b5[i]["h"], 2), "d": b5[i]["d"], "t": b5[i]["t"]})
        if b5[i]["l"] == min(b["l"] for b in win) and all(b5[i]["l"] < b["l"] for j, b in enumerate(win) if j != k):
            lo.append({"px": round(b5[i]["l"], 2), "d": b5[i]["d"], "t": b5[i]["t"]})
    return hi, lo


def levels(bars: list[dict], symbol: Optional[str] = None) -> Optional[dict]:
    """{prev {h, l, c}, day {h, l}, support, resistance (nearest swing pivots of the last 2 sessions under / over the
    last price, else the previous session's low / high), swings, round {below, above}}."""
    if not bars:
        return None
    ds = days(bars)
    today = of_day(bars, ds[-1])
    last = bars[-1]["c"]
    prev = of_day(bars, ds[-2]) if len(ds) > 1 else []
    p = {"h": round(max(b["h"] for b in prev), 2), "l": round(min(b["l"] for b in prev), 2), "c": round(prev[-1]["c"], 2)} if prev else None
    b5 = five([b for b in bars if b["d"] in ds[-3:]])
    ph, plo = pivots(b5)
    res = sorted({x["px"] for x in ph if x["px"] > last})
    sup = sorted({x["px"] for x in plo if x["px"] < last}, reverse=True)
    if p:
        res = res or ([p["h"]] if p["h"] > last else [])
        sup = sup or ([p["l"]] if p["l"] < last else [])
    step = ROUND_STEP.get((symbol or "").upper()) or (100 if last < 40000 else 500)
    rb = math.floor(last / step) * step
    res = res or [float(rb + step)]                       # no swing / previous-session level above: the round number
    sup = sup or [float(rb if rb < last else rb - step)]
    return {"last": round(last, 2), "prev": p, "day": {"h": round(max(b["h"] for b in today), 2), "l": round(min(b["l"] for b in today), 2)},
            "support": sup[0] if sup else None, "resistance": res[0] if res else None, "supports": sup[:3], "resistances": res[:3],
            "round": {"below": rb if rb < last else rb - step, "above": rb + step, "step": step}}


def _body(b):
    return abs(b["c"] - b["o"])


def _rng(b):
    return max(b["h"] - b["l"], 1e-9)


def patterns(b5: list[dict], level_list: Optional[list[float]] = None, near_pct: float = 0.1) -> list[dict]:
    """Every pattern on the given 5-minute bars, oldest first: {name, dir up|down|none, at 'HH:MM' (formed), d, text}."""
    out = []
    lv = [float(x) for x in (level_list or []) if x is not None]
    for i, b in enumerate(b5):
        p = b5[i - 1] if i else None
        if p and p["d"] == b["d"]:
            if b["c"] > b["o"] and p["c"] < p["o"] and b["c"] >= p["o"] and b["o"] <= p["c"] and _body(b) > _body(p):
                out.append({"name": "bullish engulfing", "dir": "up", "at": b["t"], "d": b["d"]})
            elif b["c"] < b["o"] and p["c"] > p["o"] and b["c"] <= p["o"] and b["o"] >= p["c"] and _body(b) > _body(p):
                out.append({"name": "bearish engulfing", "dir": "down", "at": b["t"], "d": b["d"]})
            if b["h"] < p["h"] and b["l"] > p["l"]:
                out.append({"name": "inside bar", "dir": "none", "at": b["t"], "d": b["d"]})
        up_wick, lo_wick = b["h"] - max(b["o"], b["c"]), min(b["o"], b["c"]) - b["l"]
        if lo_wick >= 2 * _body(b) and up_wick <= _body(b) and lo_wick >= 0.6 * _rng(b):
            out.append({"name": "pin bar (bullish)", "dir": "up", "at": b["t"], "d": b["d"]})
        elif up_wick >= 2 * _body(b) and lo_wick <= _body(b) and up_wick >= 0.6 * _rng(b):
            out.append({"name": "pin bar (bearish)", "dir": "down", "at": b["t"], "d": b["d"]})
        if _body(b) <= 0.1 * _rng(b) and lv and any(abs(b["c"] - x) <= x * near_pct / 100 for x in lv):
            x = min(lv, key=lambda v: abs(v - b["c"]))
            out.append({"name": "doji at a level", "dir": "none", "at": b["t"], "d": b["d"], "level": x})
        if i >= 2 and all(x["d"] == b["d"] for x in b5[i - 2:i + 1]):
            t3 = b5[i - 2:i + 1]
            if all(x["c"] > x["o"] for x in t3) and t3[0]["c"] < t3[1]["c"] < t3[2]["c"] and all(_body(x) >= 0.5 * _rng(x) for x in t3):
                out.append({"name": "three soldiers", "dir": "up", "at": b["t"], "d": b["d"]})
            if all(x["c"] < x["o"] for x in t3) and t3[0]["c"] > t3[1]["c"] > t3[2]["c"] and all(_body(x) >= 0.5 * _rng(x) for x in t3):
                out.append({"name": "three crows", "dir": "down", "at": b["t"], "d": b["d"]})
    out += flags(b5)
    out.sort(key=lambda x: (x["d"], x["at"]))
    for x in out:
        x["text"] = pattern_text(x)
    return out


def flags(b5: list[dict], min_inside: int = 3) -> list[dict]:
    """A flag: a breakout bar (range ≥ 1.5 × the average of the 10 bars before it) followed by 3+ small bars inside its
    range (each range ≤ 60 % of the breakout bar's); `at` = the last flag bar, `start` the first, `broke` the time a
    later close left the flag (up for a bull flag)."""
    out = []
    i = 10
    while i < len(b5):
        b = b5[i]
        avg = sum(_rng(x) for x in b5[i - 10:i]) / 10
        if _rng(b) >= 1.5 * avg and _body(b) >= 0.5 * _rng(b):
            j = i + 1
            while j < len(b5) and b5[j]["d"] == b["d"] and b5[j]["h"] <= b["h"] and b5[j]["l"] >= b["l"] and _rng(b5[j]) <= 0.6 * _rng(b):
                j += 1
            n = j - i - 1
            if n >= min_inside:
                up = b["c"] > b["o"]
                f = {"name": "bull flag" if up else "bear flag", "dir": "up" if up else "down", "at": b5[j - 1]["t"], "start": b5[i + 1]["t"],
                     "d": b["d"], "bars": n, "pole_at": b["t"], "broke": None}
                hi, lo = max(x["h"] for x in b5[i + 1:j]), min(x["l"] for x in b5[i + 1:j])
                for x in b5[j:]:
                    if x["d"] != b["d"]:
                        break
                    if (up and x["c"] > hi) or (not up and x["c"] < lo):
                        f["broke"] = x["t"]
                        break
                out.append(f)
                i = j
                continue
        i += 1
    return out


def pattern_text(p: dict) -> str:
    """The pattern's line, times 12-hour (IST)."""
    if p["name"].endswith("flag"):
        return (f"{p['bars']} small inside candles after the {'breakout' if p['dir'] == 'up' else 'breakdown'} at {ampm(p['pole_at'])}"
                + (f" · broke {'up' if p['dir'] == 'up' else 'down'} at {ampm(p['broke'])}" if p.get("broke") else " · not broken yet"))
    if p["name"] == "doji at a level":
        lv = float(p["level"])
        return f"a doji at {round(lv):,}" if abs(lv) >= 1000 else f"a doji at {lv:g}"
    return f"the 5-minute candle of {ampm(p['at'])}"


def atr(b5: list[dict], n: int = 14) -> Optional[float]:
    if len(b5) < 2:
        return None
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(b5, b5[1:])]
    w = trs[-n:]
    return round(sum(w) / len(w), 2) if len(w) >= min(n, 5) else None


def expected_range(atr5: Optional[float], horizon_min: int) -> Optional[float]:
    return None if atr5 is None else round(atr5 * math.sqrt(horizon_min / 5), 1)


def summary(bars: list[dict], symbol: Optional[str] = None) -> Optional[dict]:
    """Everything the tiles and the words need for one series: {trend, levels, patterns (today's, newest last), latest
    pattern, atr, exp_range {15, 30}, bars, day}."""
    if not bars:
        return None
    b5 = five(bars)
    lv = levels(bars, symbol) or {}
    tr = trend(bars)
    marks = [x for x in (lv.get("support"), lv.get("resistance"), (lv.get("prev") or {}).get("h"), (lv.get("prev") or {}).get("l"),
                         (tr.get("or") or {}).get("high"), (tr.get("or") or {}).get("low")) if x is not None]
    today = bars[-1]["d"]
    pats = [p for p in patterns([b for b in b5 if b["d"] in days(bars)[-2:]], marks) if p["d"] == today]
    a = atr(b5)
    return {"symbol": symbol, "trend": tr, "levels": lv, "patterns": pats[-5:], "pattern": pats[-1] if pats else None, "atr": a,
            "exp_range": {"15": expected_range(a, 15), "30": expected_range(a, 30)}, "bars": len(bars), "bars5": len(b5), "day": today}


# --- daily candles: the Real positions' code read (backend/market/real_reads.py) --------------------------------------------

# every threshold of the daily code read and its one-line verdict, in one place
DAILY = {
    "avg_n": 20,                  # the 20-day average of the closes (and of the volume, before today)
    "swing_n": 10,                # higher lows / lower highs and the range: the last 10 sessions (two blocks of 5)
    "atr_n": 10,                  # the 1-day range: the mean high − low of the last 10 sessions
    "gap_pct": 0.5,               # |today's open − the previous close| ≥ this % = a gap
    "vol_low": 0.7,               # today's volume ≤ this × its 20-day average: "low volume"
    "vol_high": 1.5,              # ≥ this ×: "high volume"
    "stop_days": 1.0,             # last − stop ≤ this × the 1-day range: "stop within 1-day range"
    "ratio": 3.0,                 # target-first ≥ this × stop-first: "target before stop 3:1" (only while the model shows)
    "coin": (45.0, 55.0),         # up · 3 d inside this band (%): "coin flip" (only while the model shows)
}


def _px(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.2f}".rstrip("0").rstrip(".")


def daily_read(candles: list[dict], stop: Optional[float] = None, target: Optional[float] = None,
               model: Optional[dict] = None, th: Optional[dict] = None) -> Optional[dict]:
    """The code read of one held name on its daily candles [{date, open, high, low, close, volume}] (oldest first):
    trend (above / below the 20-day average, higher lows / lower highs over 10 sessions), the 10-day range, today's
    gap, volume vs its 20-day average, the 1-day range, a short head ("up · higher lows") and ONE verdict from the
    thresholds in DAILY. `model` {stop_first, target_first, up} (%, only while the model's numbers show) adds the two
    model verdicts ("target before stop 3:1", "coin flip"); without it the verdict is code only. Pure."""
    th = {**DAILY, **(th or {})}
    if len(candles) < 2:
        return None
    c = [float(x["close"]) for x in candles]
    last, prev = c[-1], c[-2]
    n = int(th["avg_n"])
    avg = round(sum(c[-n:]) / n, 2) if len(c) >= n else None
    w = candles[-int(th["swing_n"]):]
    half = len(w) // 2
    a, b = w[:half], w[half:]
    hl = bool(a and b and min(x["low"] for x in b) > min(x["low"] for x in a))
    lh = bool(a and b and max(x["high"] for x in b) < max(x["high"] for x in a))
    above = None if avg is None else last > avg
    side = "up" if above and hl else "down" if above is False and lh else "sideways"
    lo, hi = min(x["low"] for x in w), max(x["high"] for x in w)
    before = candles[-int(th["swing_n"]) - 1:-1] or candles[:-1]
    plo, phi = min(x["low"] for x in before), max(x["high"] for x in before)
    gap = round((float(candles[-1]["open"]) / prev - 1) * 100, 2) if prev else None
    vols = [float(x.get("volume") or 0) for x in candles[-n - 1:-1]]
    vavg = sum(vols) / len(vols) if vols else 0
    vol_x = round(float(candles[-1].get("volume") or 0) / vavg, 2) if vavg > 0 else None
    rng = candles[-int(th["atr_n"]):]
    atr1 = round(sum(x["high"] - x["low"] for x in rng) / len(rng), 2)
    bits = [side]
    if side == "sideways":
        bits.append(f"{_px(lo)}–{_px(hi)}")
    elif side == "up":
        bits.append("higher lows" if hl else "above 20-day")
    else:
        bits.append("lower highs" if lh else "below 20-day")
    if gap is not None and abs(gap) >= th["gap_pct"]:
        bits.append(f"gap {'up' if gap > 0 else 'down'} {abs(gap):.1f} %")
    elif vol_x is not None and vol_x <= th["vol_low"]:
        bits.append("low volume")
    elif vol_x is not None and vol_x >= th["vol_high"]:
        bits.append("high volume")
    verdict = None
    if stop is not None and last - float(stop) <= th["stop_days"] * atr1:
        verdict = "stop within 1-day range"
    elif model:
        sf, tf, up = model.get("stop_first"), model.get("target_first"), model.get("up")
        if target is not None and sf and tf is not None and tf >= th["ratio"] * sf:
            verdict = f"target before stop {int(tf // sf)}:1"
        elif up is not None and th["coin"][0] <= up <= th["coin"][1]:
            verdict = "coin flip"
    if verdict is None:
        verdict = "above the 10-day range" if last > phi else "below the 10-day range" if last < plo else "inside the range"
    return {"asof": str(candles[-1]["date"])[:10], "last": round(last, 2), "side": side, "avg20": avg, "above_avg20": above,
            "higher_lows": hl, "lower_highs": lh, "range": {"lo": round(lo, 2), "hi": round(hi, 2)}, "gap_pct": gap, "vol_x": vol_x,
            "atr1": atr1, "head": " · ".join(bits), "verdict": verdict}
