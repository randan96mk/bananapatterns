"""📈 The candle model's sampled paths -> the numbers a read stores (pure, no torch: the worker and the tests use it).

`summarise(last, closes, highs, lows, levels, horizons)`: `closes[i][j]` is path i's close j minutes ahead (highs /
lows the same shape). For each horizon h (minutes, at most the path length):
  p_up          the share of paths whose close at h is above `last`
  exp_range     half the q10..q90 spread of the close at h (shown as ± points)
  paths_summary {q10, q50, q90}: the close quantiles at every step up to h
  touch         {level: share of paths where that level is the FIRST of `levels` crossed by h} (a path touches a level
                above `last` when its high reaches it, below when its low does; two levels crossed in the same minute:
                the one nearer `last` counts first). A single level: the share of paths that touch it at all.
  touch_any     {level: share of paths that touch it at all by h} (the entry timing's "x % of paths touch the zone top")"""

from typing import Iterable, Optional


def _q(vals: list[float], q: float) -> float:
    s = sorted(vals)
    if not s:
        return float("nan")
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def first_touch(last: float, highs: list[float], lows: list[float], levels: Iterable[float], steps: int) -> Optional[float]:
    """The first of `levels` one path crosses within `steps` minutes, or None."""
    lv = [float(x) for x in levels]
    for j in range(min(steps, len(highs))):
        hit = [x for x in lv if (x >= last and highs[j] >= x) or (x < last and lows[j] <= x)]
        if hit:
            return min(hit, key=lambda x: abs(x - last))
    return None


def level_key(x: float) -> str:
    return f"{float(x):g}"


def summarise(last: float, closes: list[list[float]], highs: Optional[list[list[float]]] = None,
              lows: Optional[list[list[float]]] = None, levels: Iterable[float] = (), horizons: Iterable[int] = ()) -> list[dict]:
    n = len(closes)
    if not n:
        return []
    highs = highs or closes
    lows = lows or closes
    steps = len(closes[0])
    lv = [float(x) for x in levels or []]
    out = []
    for h in sorted({int(x) for x in (horizons or [steps]) if 0 < int(x) <= steps}):
        end = [p[h - 1] for p in closes]
        up = sum(1 for v in end if v > last)
        summ = {k: [round(_q([p[j] for p in closes], q), 2) for j in range(h)] for k, q in (("q10", 0.1), ("q50", 0.5), ("q90", 0.9))}
        touch, anyt = {}, {}
        if lv:
            firsts = [first_touch(last, highs[i], lows[i], lv, h) for i in range(n)]
            touch = {level_key(x): round(sum(1 for f in firsts if f == x) / n, 3) for x in lv}
            anyt = {level_key(x): round(sum(1 for i in range(n) if first_touch(last, highs[i], lows[i], [x], h) is not None) / n, 3) for x in lv}
        out.append({"horizon": h, "p_up": round(up / n, 3), "exp_range": round((_q(end, 0.9) - _q(end, 0.1)) / 2, 2),
                    "paths_summary": summ, "touch": touch, "touch_any": anyt, "samples": n})
    return out
