"""₿ The 16 candle patterns at a pivot level (pure functions over the last 1–3 CLOSED candles; every threshold is a
crypto.* input). Candles are {t, o, h, l, c, v}, oldest first.

Bullish (a support S1–S3 touches)          Bearish (a resistance R1–R3 touches)
  hammer            1 candle                 shooting star     1 candle
  bullish engulfing 2                        bearish engulfing 2
  piercing line     2                        dark cloud cover  2
  morning star      3                        evening star      3
  bullish harami    2 (confirm)              bearish harami    2 (confirm)
  dragonfly doji    1 (confirm)              gravestone doji   1 (confirm)
  tweezer bottom    2                        tweezer top       2
  inverted hammer   1 (confirm)              hanging man       1 (confirm)

Rules (the patterns mock):
  hammer          lower wick ≥ wick_mult × body and ≥ wick_min of the range · upper wick ≤ opp_wick_max · body in the top
                  body_zone of the range · the level inside the range
  shooting star   upper wick ≥ wick_mult × body and ≥ wick_min · lower wick ≤ opp_wick_max · body in the bottom
                  1 − body_zone (40 %)
  engulfing       the second body covers the whole prior opposite body; bullish closes above the prior open
  piercing line   red, then green opening below the prior low and closing above the prior body's midpoint (under its open)
  dark cloud      green, then red opening above the prior high and closing below the prior body's midpoint (above its open)
  morning star    red · a small body (≤ star_body × the first) at or under the first close · green closing into the
                  first body; the level at the middle candle (evening star mirrored)
  harami          a small opposite body inside the prior body; the level at the prior candle; confirmation: the next
                  close beyond the harami's high (bullish) / low (bearish)
  dragonfly doji  body ≤ doji_body of the range · lower wick ≥ doji_wick (body at the top); gravestone mirrored
  tweezer bottom  two lows within eq_tol_pct % · the second green; tweezer top: equal highs, the second red
  inverted hammer upper wick ≥ wick_mult × body after a decline · body at the bottom; confirmation: a green next candle
  hanging man     the hammer shape after a rise · the level at the high; confirmation: a red next candle
Touch: the level inside the pattern's low–high or within touch_points of it. Suggested stop / 1R (information only):
bullish stop = pattern low − stop_pad, at most max_stop from the entry level (= the pattern high); 1R = entry + (entry −
stop). Bearish mirrored."""

from datetime import datetime, timezone
from typing import Optional

from backend.crypto.pivots import IST_OFFSET

BULLISH = ("hammer", "bullish_engulfing", "piercing_line", "morning_star", "bullish_harami", "dragonfly_doji", "tweezer_bottom",
           "inverted_hammer")
BEARISH = ("shooting_star", "bearish_engulfing", "dark_cloud_cover", "evening_star", "bearish_harami", "gravestone_doji",
           "tweezer_top", "hanging_man")
SIZE = {"hammer": 1, "shooting_star": 1, "dragonfly_doji": 1, "gravestone_doji": 1, "inverted_hammer": 1, "hanging_man": 1,
        "bullish_engulfing": 2, "bearish_engulfing": 2, "piercing_line": 2, "dark_cloud_cover": 2, "bullish_harami": 2,
        "bearish_harami": 2, "tweezer_bottom": 2, "tweezer_top": 2, "morning_star": 3, "evening_star": 3}
LABEL = {p: p.replace("_", " ") for p in SIZE}
FORMING = ("hammer", "shooting_star", "bullish_engulfing", "bearish_engulfing")   # the "forming" heads-up reads only these
DEFAULTS = {"wick_mult": 2.0, "wick_min": 0.5, "opp_wick_max": 0.3, "body_zone": 0.6, "doji_body": 0.1, "doji_wick": 0.7,
            "star_body": 0.3, "eq_tol_pct": 0.1, "touch_points": 20.0, "stop_pad": 10.0, "max_stop": 50.0}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def direction(p: str) -> str:
    return "long" if p in BULLISH else "short"


def th_of(cfg) -> dict:
    """The thresholds from a CryptoConfig (or a dict), the defaults for what is missing."""
    get = cfg.get if isinstance(cfg, dict) else (lambda k, d=None: getattr(cfg, k, d))
    return {k: float(get(k, v)) for k, v in DEFAULTS.items()}


# --- candle geometry -------------------------------------------------------------------------------------------------------

def body(b) -> float:
    return abs(b["c"] - b["o"])


def rng(b) -> float:
    return b["h"] - b["l"]


def upper(b) -> float:
    return b["h"] - max(b["o"], b["c"])


def lower(b) -> float:
    return min(b["o"], b["c"]) - b["l"]


def green(b) -> bool:
    return b["c"] > b["o"]


def red(b) -> bool:
    return b["c"] < b["o"]


def mid(b) -> float:
    return (b["o"] + b["c"]) / 2


def _hammer_shape(b, th) -> bool:
    r = rng(b)
    return (r > 0 and lower(b) >= th["wick_mult"] * body(b) and lower(b) >= th["wick_min"] * r and upper(b) <= th["opp_wick_max"] * r
            and min(b["o"], b["c"]) >= b["h"] - th["body_zone"] * r)


def _star_shape(b, th) -> bool:
    r = rng(b)
    return (r > 0 and upper(b) >= th["wick_mult"] * body(b) and upper(b) >= th["wick_min"] * r and lower(b) <= th["opp_wick_max"] * r
            and max(b["o"], b["c"]) <= b["l"] + (1 - th["body_zone"]) * r)


def prior_trend(before: list[dict]) -> Optional[str]:
    """'down' / 'up' over the 3 candles before the pattern (the first close vs the last), None with fewer than 3."""
    if len(before) < 3:
        return None
    a, z = before[-3]["c"], before[-1]["c"]
    return "down" if z < a else "up" if z > a else None


def shape(name: str, cs: list[dict], th: dict, before: Optional[list[dict]] = None) -> Optional[dict]:
    """The pattern's candles `cs` (its SIZE, oldest first) against its rule; {focus: the candles the level must touch,
    facts} or None. `before` = the candles before the pattern (the decline / rise the inverted hammer and the
    hanging man need)."""
    th = {**DEFAULTS, **(th or {})}
    if len(cs) != SIZE[name]:
        return None
    b = cs[-1]
    r = rng(b)
    tr = prior_trend(before or [])
    if name == "hammer" and _hammer_shape(b, th):
        return {"focus": [b], "facts": {"wick_x": round(lower(b) / body(b), 1) if body(b) else None, "wick_pct": round(100 * lower(b) / r)}}
    if name == "hanging_man" and _hammer_shape(b, th) and tr == "up":
        return {"focus": [b], "facts": {"wick_x": round(lower(b) / body(b), 1) if body(b) else None, "wick_pct": round(100 * lower(b) / r)}}
    if name == "shooting_star" and _star_shape(b, th):
        return {"focus": [b], "facts": {"wick_x": round(upper(b) / body(b), 1) if body(b) else None, "wick_pct": round(100 * upper(b) / r)}}
    if name == "inverted_hammer" and r > 0 and upper(b) >= th["wick_mult"] * body(b) and max(b["o"], b["c"]) <= b["l"] + (1 - th["body_zone"]) * r and tr == "down":
        return {"focus": [b], "facts": {"wick_x": round(upper(b) / body(b), 1) if body(b) else None, "wick_pct": round(100 * upper(b) / r)}}
    if name == "dragonfly_doji" and r > 0 and body(b) <= th["doji_body"] * r and lower(b) >= th["doji_wick"] * r:
        return {"focus": [b], "facts": {"body_pct": round(100 * body(b) / r), "wick_pct": round(100 * lower(b) / r)}}
    if name == "gravestone_doji" and r > 0 and body(b) <= th["doji_body"] * r and upper(b) >= th["doji_wick"] * r:
        return {"focus": [b], "facts": {"body_pct": round(100 * body(b) / r), "wick_pct": round(100 * upper(b) / r)}}
    if len(cs) >= 2:
        p = cs[-2]
        if name == "bullish_engulfing" and red(p) and green(b) and b["o"] <= p["c"] and b["c"] >= p["o"] and b["c"] > p["o"] and body(b) > body(p):
            return {"focus": [p, b], "facts": {"cover_x": round(body(b) / body(p), 1) if body(p) else None}}
        if name == "bearish_engulfing" and green(p) and red(b) and b["o"] >= p["c"] and b["c"] <= p["o"] and b["c"] < p["o"] and body(b) > body(p):
            return {"focus": [p, b], "facts": {"cover_x": round(body(b) / body(p), 1) if body(p) else None}}
        if name == "piercing_line" and red(p) and green(b) and b["o"] < p["l"] and mid(p) < b["c"] < p["o"]:
            return {"focus": [b], "facts": {}}
        if name == "dark_cloud_cover" and green(p) and red(b) and b["o"] > p["h"] and p["o"] < b["c"] < mid(p):
            return {"focus": [b], "facts": {}}
        if name == "bullish_harami" and red(p) and green(b) and body(b) < body(p) and b["o"] >= p["c"] and b["c"] <= p["o"]:
            return {"focus": [p], "facts": {}}
        if name == "bearish_harami" and green(p) and red(b) and body(b) < body(p) and b["o"] <= p["c"] and b["c"] >= p["o"]:
            return {"focus": [p], "facts": {}}
        tol = th["eq_tol_pct"] / 100 * b["c"]
        if name == "tweezer_bottom" and green(b) and abs(p["l"] - b["l"]) <= tol:
            return {"focus": [p, b], "facts": {"diff": round(abs(p["l"] - b["l"]), 2)}}
        if name == "tweezer_top" and red(b) and abs(p["h"] - b["h"]) <= tol:
            return {"focus": [p, b], "facts": {"diff": round(abs(p["h"] - b["h"]), 2)}}
    if len(cs) == 3:
        a, m = cs[0], cs[1]
        if name == "morning_star" and red(a) and body(a) > 0 and body(m) <= th["star_body"] * body(a) and max(m["o"], m["c"]) <= a["c"] \
                and green(b) and b["c"] > a["c"]:
            return {"focus": [m], "facts": {"star_pct": round(100 * body(m) / body(a))}}
        if name == "evening_star" and green(a) and body(a) > 0 and body(m) <= th["star_body"] * body(a) and min(m["o"], m["c"]) >= a["c"] \
                and red(b) and b["c"] < a["c"]:
            return {"focus": [m], "facts": {"star_pct": round(100 * body(m) / body(a))}}
    return None


def touch(focus: list[dict], levels: list[tuple], touch_points: float, side: str) -> Optional[tuple]:
    """The (name, px) level that touches the focus candles' low–high (or within touch_points), nearest to the low
    (long) / high (short); None when none does."""
    lo, hi = min(b["l"] for b in focus), max(b["h"] for b in focus)
    hits = [(n, px) for n, px in levels if lo - touch_points <= px <= hi + touch_points]
    if not hits:
        return None
    ref = lo if side == "long" else hi
    return min(hits, key=lambda x: abs(x[1] - ref))


def stop_r1(cs: list[dict], side: str, stop_pad: float, max_stop: float) -> dict:
    """The suggested entry level, stop and 1R (information only)."""
    lo, hi = min(b["l"] for b in cs), max(b["h"] for b in cs)
    if side == "long":
        entry = hi
        stop = lo - stop_pad
        capped = entry - stop > max_stop
        if capped:
            stop = entry - max_stop
        r1 = entry + (entry - stop)
    else:
        entry = lo
        stop = hi + stop_pad
        capped = stop - entry > max_stop
        if capped:
            stop = entry + max_stop
        r1 = entry - (stop - entry)
    return {"entry": round(entry, 2), "stop": round(stop, 2), "r1": round(r1, 2), "capped": capped}


# --- filters ------------------------------------------------------------------------------------------------------------------

def ema(vals: list[float], n: int) -> list[Optional[float]]:
    """TradingView's ta.ema: seeded with the SMA of the first n values; None before."""
    out: list[Optional[float]] = []
    a, e = 2 / (n + 1), None
    for i, v in enumerate(vals):
        if i < n - 1:
            out.append(None)
            continue
        e = sum(vals[:n]) / n if e is None else e + a * (v - e)
        out.append(e)
    return out


def sma(vals: list[float], n: int) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def ist_day(t: int) -> str:
    return DAYS[datetime.fromtimestamp(int(t) + IST_OFFSET, timezone.utc).weekday()]


def confirm(det: dict, nxt: dict) -> bool:
    """The next candle closes in the direction: long = green and above the pattern's last close (the harami: above its
    high); short mirrored."""
    last = det["candles"][-1]
    if det["dir"] == "long":
        return green(nxt) and nxt["c"] > (last["h"] if det["pattern"] == "bullish_harami" else last["c"])
    return red(nxt) and nxt["c"] < (last["l"] if det["pattern"] == "bearish_harami" else last["c"])


def detect(candles: list[dict], levels: dict, cfg, i: Optional[int] = None, names: Optional[tuple] = None) -> list[dict]:
    """Every enabled pattern whose last candle is candles[i] (default the last), at a touching level, with the filters
    evaluated: [{pattern, dir, t, t0, candles, level_name, level_px, entry, stop, r1, capped, ema, ema_ok, vol_x,
    vol_ok, trend_ok, day_ok, why, facts}]. `levels` = pivots.pattern_levels(). A failing filter leaves `why` set (the
    row is stored "dropped: why"); the state (off / mark / alert) is the caller's."""
    get = cfg.get if isinstance(cfg, dict) else (lambda k, d=None: getattr(cfg, k, d))
    th = th_of(cfg)
    i = len(candles) - 1 if i is None else i
    closes = [b["c"] for b in candles[: i + 1]]
    vols = [float(b.get("v") or 0) for b in candles[: i + 1]]
    e_len, v_len = int(get("ema_len", 20)), int(get("vol_len", 50))
    e = ema(closes, e_len)[-1] if closes else None
    vavg = sma(vols, v_len)[-1] if vols else None
    out = []
    for name in names or (*BULLISH, *BEARISH):
        st = get(name, "off")
        if st == "off":
            continue
        n = SIZE[name]
        if i - n + 1 < 0:
            continue
        cs = candles[i - n + 1: i + 1]
        before = candles[max(0, i - n - 2): i - n + 1]
        sh = shape(name, cs, th, before)
        if not sh:
            continue
        side = direction(name)
        lv = touch(sh["focus"], levels.get(side, []), th["touch_points"], side)
        if not lv:
            continue
        last = cs[-1]
        ema_ok = None if e is None else (last["c"] > e if side == "long" else last["c"] < e)
        vol_x = round(vols[-1] / vavg, 2) if vavg else None
        vol_ok = None if vol_x is None else vol_x >= float(get("vol_mult", 1.0))
        tr = prior_trend(before)
        trend_ok = tr == ("down" if side == "long" else "up")
        day_ok = ist_day(last["t"]) in [str(d).lower()[:3] for d in get("days", DAYS)]
        why = []
        if get("ema_on", True) and not ema_ok:
            why.append(f"close {'under' if side == 'long' else 'over'} EMA {e_len}" if e is not None else f"EMA {e_len} not ready")
        if get("vol_on", False) and not vol_ok:
            why.append(f"volume {vol_x if vol_x is not None else '—'}× under {float(get('vol_mult', 1.0)):g}×")
        if get("trend_on", False) and not trend_ok:
            why.append("no prior 3-candle " + ("decline" if side == "long" else "rise"))
        if not day_ok:
            why.append(f"{ist_day(last['t'])} is off")
        sr = stop_r1(cs, side, th["stop_pad"], th["max_stop"])
        out.append({"pattern": name, "dir": side, "t": last["t"], "t0": cs[0]["t"], "candles": cs, "level_name": lv[0], "level_px": lv[1],
                    **sr, "ema": round(e, 2) if e is not None else None, "ema_len": e_len, "ema_ok": ema_ok, "vol_x": vol_x, "vol_ok": vol_ok,
                    "trend_ok": trend_ok, "day_ok": day_ok, "why": "; ".join(why) or None, "facts": sh["facts"], "state_set": st})
    return out
