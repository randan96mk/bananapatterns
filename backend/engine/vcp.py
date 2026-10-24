"""VCP (volatility contraction pattern) metrics and qualification for one base.

Everything is measured on candles up to the evaluation bar (causal). For a
forming base that is the last bar; for a breakout it is the bar before the
breakout, so a breakout is judged on what its base looked like beforehand.

Metrics (our own definitions; the site's formulas are unpublished):
  tightening   ATR(atr_recent) now / mean true range over the whole base.
               < 1 means the latest swings are narrower than the base's average.
  dryup        mean volume of the last `dryup_recent` sessions / mean volume of
               the `dryup_lookback` sessions before the base. < 1 = volume fading.
  ud_ratio     volume on up days / volume on down days over `ud_window` sessions.
  contractions pullbacks inside the base from causal ZigZag swings: a swing
               high/low is confirmed only after price reverses by
               max(zz_min_pct, zz_atr_mult x ATR%). The current pullback (from
               the last swing high to the lowest low since) counts if it is at
               least that deep. depth = (swing high - swing low) / swing high.
  shrinking    the last contraction <= `shrink` x the largest earlier one.
  stop         last contraction low x (1 - stop_buffer), never more than
               `max_stop` below the pivot.
VCP qualifies (dials in VcpConfig): base >= min_weeks and depth <= max_depth;
min_contractions <= contractions <= max_contractions, last one <= max_final_contraction and (if
require_shrinking) shrinking; tightening <= max_tightening; dryup <= max_dryup;
RS >= min_rs; Trend Template checks >= min_trend_checks.
"""

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Optional

import numpy as np

from backend.config import VcpConfig
from backend.engine.bases import Base


@dataclass
class Vcp:
    qualifies: bool
    fails: list = field(default_factory=list)       # which rules failed (empty when it qualifies)
    tightening: Optional[float] = None
    dryup: Optional[float] = None
    ud_ratio: Optional[float] = None
    contractions: list = field(default_factory=list)  # [{high, high_date, low, low_date, depth_pct}]
    shrinking: Optional[bool] = None
    stop: Optional[float] = None
    stop_pct: Optional[float] = None                  # below the pivot, %
    eval_date: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _r(x, nd=2):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def true_range(high, low, close):
    prev = np.concatenate([[close[0]], close[:-1]])
    return np.maximum(high, prev) - np.minimum(low, prev)


def swings(high, low, start: int, end: int, threshold: float) -> list[tuple[str, int]]:
    """Causal ZigZag over [start, end]: confirmed ('H'|'L', index) points, alternating."""
    pts: list[tuple[str, int]] = []
    hi_i = lo_i = start
    direction = 0                                   # 0 unknown, 1 seeking high, -1 seeking low
    for t in range(start + 1, end + 1):
        if high[t] >= high[hi_i]:
            hi_i = t
        if low[t] <= low[lo_i]:
            lo_i = t
        if direction >= 0 and low[t] <= high[hi_i] * (1 - threshold) and hi_i < t:
            pts.append(("H", hi_i))                 # a high is known once price fell far enough
            direction, lo_i = -1, t
        elif direction <= 0 and high[t] >= low[lo_i] * (1 + threshold) and lo_i < t:
            pts.append(("L", lo_i))
            direction, hi_i = 1, t
    return pts, hi_i, lo_i, direction


def contractions_in(high, low, close, dates, start: int, end: int, cfg: VcpConfig) -> list[dict]:
    tr = true_range(high, low, close)
    a0 = max(start, end - cfg.atr_recent + 1)
    atr_pct = tr[a0:end + 1].mean() / close[end] if end >= start else 0.0
    thr = max(cfg.zz_min_pct, cfg.zz_atr_mult * atr_pct)
    pts, hi_i, lo_i, direction = swings(high, low, start, end, thr)
    out = []
    for (k1, i1), (k2, i2) in zip(pts, pts[1:]):
        if k1 == "H" and k2 == "L":
            out.append((i1, i2))
    # the current, still-open pullback from the last confirmed or running high
    if pts and pts[-1][0] == "H":
        h = pts[-1][1]
        l_ = h + int(np.argmin(low[h:end + 1]))
        if (high[h] - low[l_]) / high[h] >= thr and (not out or out[-1][0] != h):
            out.append((h, l_))
    elif direction > 0 and hi_i < end:
        l_ = hi_i + int(np.argmin(low[hi_i:end + 1]))
        if (high[hi_i] - low[l_]) / high[hi_i] >= thr:
            out.append((hi_i, l_))
    return [{"high": _r(high[i]), "high_date": dates[i], "low": _r(low[j]), "low_date": dates[j],
             "depth_pct": _r((high[i] - low[j]) / high[i] * 100)} for i, j in out]


def evaluate(candles: list[dict], base: Base, rs: Optional[int], trend_passed: int, cfg: VcpConfig,
             end: Optional[int] = None) -> Vcp:
    """VCP metrics for `base` measured at bar `end` (default: the base's last bar)."""
    end = base.end_index if end is None else end
    high = np.array([c["high"] for c in candles[:end + 1]], dtype=float)
    low = np.array([c["low"] for c in candles[:end + 1]], dtype=float)
    close = np.array([c["close"] for c in candles[:end + 1]], dtype=float)
    vol = np.array([c["volume"] for c in candles[:end + 1]], dtype=float)
    dates = [c["date"] for c in candles[:end + 1]]
    s = base.start_index

    tr = true_range(high, low, close)
    base_tr = tr[s:end + 1].mean()
    recent_tr = tr[max(s, end - cfg.atr_recent + 1):end + 1].mean()
    tightening = recent_tr / base_tr if base_tr > 0 else None

    before = vol[max(0, s - cfg.dryup_lookback):s]
    recent = vol[max(s, end - cfg.dryup_recent + 1):end + 1]
    dryup = recent.mean() / before.mean() if len(before) and before.mean() > 0 else None

    w = min(cfg.ud_window, end)
    chg = np.diff(close[end - w:end + 1])
    v = vol[end - w + 1:end + 1]
    up, down = v[chg > 0].sum(), v[chg < 0].sum()
    ud = up / down if down > 0 else None

    cons = contractions_in(high, low, close, dates, s, end, cfg)
    depths = [c["depth_pct"] for c in cons]
    shrinking = depths[-1] <= cfg.shrink * max(depths[:-1]) if len(depths) >= 2 else None

    pivot = base.pivot
    stop = None
    if cons:
        stop = cons[-1]["low"] * (1 - cfg.stop_buffer)
        stop = max(stop, pivot * (1 - cfg.max_stop))
    else:
        stop = pivot * (1 - cfg.max_stop)

    fails = []
    weeks = max(1, (date.fromisoformat(dates[end]) - date.fromisoformat(dates[s])).days // 7)
    if weeks < cfg.min_weeks:
        fails.append(f"base {weeks}w < {cfg.min_weeks}w")
    if base.depth_pct > cfg.max_depth * 100:
        fails.append(f"base {base.depth_pct:.1f}% deep")
    if tightening is None or tightening > cfg.max_tightening:
        fails.append("not tightening")
    if dryup is None or dryup > cfg.max_dryup:
        fails.append("volume not drying up")
    if rs is None or rs < cfg.min_rs:
        fails.append(f"RS {rs if rs is not None else 'n/a'} < {cfg.min_rs}")
    if trend_passed < cfg.min_trend_checks:
        fails.append(f"trend {trend_passed}/8 < {cfg.min_trend_checks}")
    if len(cons) < cfg.min_contractions:
        fails.append(f"{len(cons)} contractions < {cfg.min_contractions}")
    if len(cons) > cfg.max_contractions:
        fails.append(f"{len(cons)} contractions > {cfg.max_contractions}")
    if depths and depths[-1] > cfg.max_final_contraction * 100:
        fails.append(f"last pullback {depths[-1]:.1f}% > {cfg.max_final_contraction * 100:g}%")
    if cfg.require_shrinking and shrinking is not True and len(depths) >= 2:
        fails.append("pullbacks not shrinking")

    return Vcp(qualifies=not fails, fails=fails, tightening=_r(tightening), dryup=_r(dryup), ud_ratio=_r(ud),
               contractions=cons, shrinking=shrinking, stop=_r(stop), stop_pct=_r((1 - stop / pivot) * 100),
               eval_date=dates[end])
