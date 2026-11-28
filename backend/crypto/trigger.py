"""₿ The trigger (crypto.trigger): "sweep" (default) or "touch" (the old rule: a candle pattern at a pivot level).

sweep · reclaim at a level L, long (short mirrored), on the closed candle at t:
  sweep     the candle's low ≤ L (within TOL = 0.02 %), the candle before it closed above L (it was on that side)
  reclaim   its close > L
  approach  within the last approach_candles before it the price was ≥ approach_atr × ATR(14) above L (it came from
            away); else "drift" (it hovered around L) or "no approach" (it came from the other side)
  ATR       ATR(14) ≥ atr_floor
  touches   only the first touches_per_session sweeps of (L, direction) in the session count (0 = any)
  session   the session's weight (session_asia / _london / _ny) is "alerts"
A detection that fails approach / ATR / touches / the session weight is stored "mark" with counted = 0 and its why
("drift · Asia"); the rest counted = 1. The candle shape (the 16 patterns) is a TAG (`shape`), never required.
Levels within MERGE = 0.05 % of each other merge into one detection naming all ("PDL 59,760 + S1 59,755").
Stop (stop_mode): "wick" → long stop = the sweep low − stop_wick_pad, capped at entry − stop_cap_atr × ATR; "fixed" →
the low − stop_pad, at most max_stop from the entry; entry = the signal candle's close; 1R mirrored. Pure functions."""

from typing import Optional

from backend.crypto import levels as lvm
from backend.crypto import patterns as pt

TOL = 0.0002
MERGE = 0.0005
PRIORITY = {f: i for i, f in enumerate(("pd", "session", "profile", "vwap", "pivot"))}


def atr14(cs: list[dict]) -> Optional[float]:
    from backend.market import chart
    return chart.atr([{"h": b["h"], "l": b["l"], "c": b["c"]} for b in cs[-60:]])


def swept(b: dict, lpx: float, side: str) -> dict:
    """{sweep, reclaim} of the level by the candle."""
    if side == "long":
        return {"sweep": b["l"] <= lpx * (1 + TOL), "reclaim": b["c"] > lpx}
    return {"sweep": b["h"] >= lpx * (1 - TOL), "reclaim": b["c"] < lpx}


def approach(before: list[dict], lpx: float, side: str, atr: Optional[float], k: float, n: int) -> dict:
    """{atr (the farthest distance in ATRs), n (candles before the signal candle), ok, word}: over the last n candles
    before the signal, the farthest the price went on the level's far side (the highs above for a long)."""
    win = before[-int(n):] if n else []
    if not win or not atr:
        return {"atr": None, "n": None, "px": None, "ok": False, "word": "ATR not ready" if not atr else "no approach"}
    dist = [(b["h"] - lpx) if side == "long" else (lpx - b["l"]) for b in win]
    i = max(range(len(dist)), key=lambda j: (dist[j], j))
    x = round(max(0.0, dist[i]) / atr, 1)
    ok = dist[i] >= k * atr
    word = None
    if not ok:
        other = [(lpx - b["c"]) if side == "long" else (b["c"] - lpx) for b in win]
        word = "no approach" if max(other) >= k * atr else "drift"
    return {"atr": x, "n": len(win) - i, "px": win[i]["h"] if side == "long" else win[i]["l"], "ok": bool(ok), "word": word}


def touches_before(cs: list[dict], lpx: float, side: str, since: int) -> int:
    """The sweeps · reclaims of the level in the session before the last candle."""
    n = 0
    for p, b in zip(cs[:-2], cs[1:-1]):
        if b["t"] < since:
            continue
        s = swept(b, lpx, side)
        if s["sweep"] and s["reclaim"] and (p["c"] > lpx if side == "long" else p["c"] < lpx):
            n += 1
    return n


def shape_of(cs: list[dict], side: str, cfg) -> Optional[str]:
    """The first of the 16 patterns (of the side) whose last candle is the signal candle: a tag only."""
    th = pt.th_of(cfg)
    i = len(cs) - 1
    for name in (pt.BULLISH if side == "long" else pt.BEARISH):
        n = pt.SIZE[name]
        if i - n + 1 < 0:
            continue
        if pt.shape(name, cs[i - n + 1: i + 1], th, cs[max(0, i - n - 2): i - n + 1]):
            return name
    return None


def stop_for(c, side: str, b: dict, atr: Optional[float]) -> dict:
    """{entry, stop, r1, capped, pad, mode} (see the module doc)."""
    entry = float(b["c"])
    if c.stop_mode == "fixed":
        pad, cap = float(c.stop_pad), float(c.max_stop)
    else:
        pad, cap = float(c.stop_wick_pad), (float(c.stop_cap_atr) * atr if atr else None)
    if side == "long":
        stop = b["l"] - pad
        capped = cap is not None and entry - stop > cap
        if capped:
            stop = entry - cap
        r1 = entry + (entry - stop)
    else:
        stop = b["h"] + pad
        capped = cap is not None and stop - entry > cap
        if capped:
            stop = entry + cap
        r1 = entry - (stop - entry)
    return {"entry": round(entry, 2), "stop": round(stop, 2), "r1": round(r1, 2), "capped": bool(capped), "pad": pad, "mode": c.stop_mode}


def weight(c, session: str) -> str:
    return getattr(c, f"session_{session}", "alerts")


def trigger_levels(levels: list[dict], use_p: bool) -> list[dict]:
    return [x for x in levels if x["kind"] != "P" or use_p]


def merged_name(ls: list[dict]) -> str:
    return " + ".join(x["name"] for x in ls)


def merged_text(ls: list[dict]) -> str:
    """'PDL 59,760 + S1 59,755'."""
    return " + ".join(f"{x['name']} {lvm.px(x['px'])}" for x in ls)


def next_levels(levels: list[dict], entry: float, side: str, k: int = 3) -> list[dict]:
    """The k nearest levels beyond the entry in the trade's direction (above for a long), one per price."""
    ahead = sorted((x for x in levels if (x["px"] > entry if side == "long" else x["px"] < entry)),
                   key=lambda x: (abs(x["px"] - entry), PRIORITY.get(x["family"], 9)))
    out, seen = [], set()
    for x in ahead:
        if x["px"] in seen:
            continue
        seen.add(x["px"])
        out.append({"name": x["name"], "px": x["px"], "kind": x["kind"]})
        if len(out) == k:
            break
    return out


def _filters(cs: list[dict], c, side: str) -> dict:
    """The old rule's filters as facts (EMA, volume, day): only the day filter can drop a sweep."""
    closes = [b["c"] for b in cs]
    vols = [float(b.get("v") or 0) for b in cs]
    e_len, v_len = int(c.ema_len), int(c.vol_len)
    e = pt.ema(closes, e_len)[-1] if closes else None
    vavg = pt.sma(vols, v_len)[-1] if vols else None
    last = cs[-1]
    ema_ok = None if e is None else (last["c"] > e if side == "long" else last["c"] < e)
    vol_x = round(vols[-1] / vavg, 2) if vavg else None
    vol_ok = None if vol_x is None else vol_x >= float(c.vol_mult)
    day_ok = pt.ist_day(last["t"]) in [str(d).lower()[:3] for d in c.days]
    return {"ema": round(e, 2) if e is not None else None, "ema_len": e_len, "ema_ok": ema_ok, "vol_x": vol_x, "vol_ok": vol_ok, "day_ok": day_ok,
            "trend_ok": pt.prior_trend(cs[-4:-1]) == ("down" if side == "long" else "up")}


def judge(c, cs: list[dict], lpx: float, side: str, atr: Optional[float], session: dict) -> dict:
    """The count checks for one level: {approach, touch_n, nc (the reasons it does not count)}."""
    ap = approach(cs[:-1], lpx, side, atr, float(c.approach_atr), int(c.approach_candles))
    nc = []
    if not ap["ok"]:
        nc.append(ap["word"])
    if float(c.atr_floor) > 0 and (atr is None or atr < float(c.atr_floor)):
        nc.append(f"ATR {lvm.px(atr) if atr is not None else '—'} < {float(c.atr_floor):g}")
    tn = touches_before(cs, lpx, side, session["start"]) + 1
    cap = int(c.touches_per_session)
    if cap and tn > cap:
        nc.append(f"touch {tn} of the session (first {cap})")
    if weight(c, session["key"]) != "alerts":
        nc.append(session["label"])
    return {"approach": ap, "touch_n": tn, "nc": nc}


def sweeps(config, cs: list[dict], levels: list[dict], atr: Optional[float] = None) -> list[dict]:
    """The sweep · reclaim detections of the last candle of `cs` (oldest first) at `levels` (levels_all), merged,
    as signals.insert dicts (pattern 'sweep', trigger 'sweep', shape, levels, session, approach_atr, approach_n,
    counted, nc_why, next, sweep facts)."""
    c = config.crypto
    if not cs:
        return []
    b = cs[-1]
    atr = atr if atr is not None else atr14(cs[:-1])
    sess = lvm.session_span(b["t"])
    hits = {"long": [], "short": []}
    prev = cs[-2]["c"] if len(cs) > 1 else None
    for lv in trigger_levels(levels, c.use_p):
        for side in ("long", "short"):
            s = swept(b, lv["px"], side)
            was = prev is not None and (prev > lv["px"] if side == "long" else prev < lv["px"])
            if s["sweep"] and s["reclaim"] and was:
                hits[side].append({**lv, **judge(c, cs, lv["px"], side, atr, sess)})
    out = []
    for side, hs in hits.items():
        if not hs:
            continue
        hs.sort(key=lambda x: x["px"])
        groups, cur = [], [hs[0]]
        for h in hs[1:]:
            if h["px"] - cur[0]["px"] <= MERGE * cur[0]["px"]:
                cur.append(h)
            else:
                groups.append(cur)
                cur = [h]
        groups.append(cur)
        for g in groups:
            g.sort(key=lambda x: (bool(x["nc"]), PRIORITY.get(x["family"], 9)))
            out.append(_det(config, cs, side, g, atr, sess, levels))
    return out


def _det(config, cs: list[dict], side: str, group: list[dict], atr: Optional[float], sess: dict, levels: list[dict]) -> dict:
    c = config.crypto
    b = cs[-1]
    head = group[0]
    ls = [{k: x[k] for k in ("kind", "name", "px", "label", "family", *(("session",) if "session" in x else ()))} for x in group]
    sr = stop_for(c, side, b, atr)
    f = _filters(cs, c, side)
    wick = b["l"] if side == "long" else b["h"]
    beyond = round(abs(head["px"] - wick), 2)
    shp = shape_of(cs, side, c)
    nc = head["nc"]
    why = None if f["day_ok"] else f"{pt.ist_day(b['t'])} is off"
    return {"pattern": "sweep", "dir": side, "t": b["t"], "t0": b["t"], "candles": [b], "level_name": merged_name(ls), "level_px": head["px"],
            **{k: sr[k] for k in ("entry", "stop", "r1", "capped")}, **f, "why": why,
            "facts": {"wick": wick, "beyond": beyond, "pad": sr["pad"], "stop_mode": sr["mode"], "atr": atr, "touch_n": head["touch_n"],
                      "shape": shp},
            "state_set": "mark" if nc else "alert", "trigger": "sweep", "shape": shp, "levels": ls, "session": sess["key"],
            "approach_atr": head["approach"]["atr"], "approach_n": head["approach"]["n"], "approach_px": head["approach"]["px"], "counted": 0 if nc else 1,
            "nc_why": " · ".join(nc) or None, "next": next_levels(levels, sr["entry"], side), "family": head["family"]}


def touch_judge(config, cs: list[dict], det: dict, atr: Optional[float], sweep_mode: bool) -> dict:
    """The new columns for an old-rule (touch) detection: the shape tag, the session, the approach (information), and
    counted: in touch mode only the session weight decides; in sweep mode a touch is never counted (its why: the
    approach word, "no sweep", the session)."""
    c = config.crypto
    sess = lvm.session_span(det["t"])
    ap = approach(cs[:-1], det["level_px"], det["dir"], atr, float(c.approach_atr), int(c.approach_candles))
    nc = []
    if sweep_mode:
        nc.append(ap["word"] or "no sweep")
    if weight(c, sess["key"]) != "alerts":
        nc.append(sess["label"])
    return {"trigger": "touch", "shape": det["pattern"], "session": sess["key"], "approach_atr": ap["atr"], "approach_n": ap["n"], "approach_px": ap["px"],
            "counted": 0 if nc else 1, "nc_why": " · ".join(nc) or None,
            "levels": [{"kind": det["level_name"], "name": det["level_name"], "px": det["level_px"],
                        "label": f"{det['level_name']} {lvm.px(det['level_px'])} · pivot", "family": "pivot"}]}
