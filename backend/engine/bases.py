"""Base (consolidation box) detection, the B1…Bn boxes on the chart.

A base is a stretch of at least `min_base_bars` sessions in which no close
goes above the pivot and the drop from the pivot stays within
`max_base_depth`. The pivot is the highest close in the base by default
(`pivot_basis="close"`, which reproduces bananapatterns.com's pivots exactly
on NRL and ORIENTHOT, Oct 2026), or the highest high (`"high"`, where wicks
may lift it at most `max_pivot_drift` above the base's first high, otherwise
the stock is grinding up and it is a trend, not a base). The first close above the pivot is the breakout and
ends the base. Walking forward bar by bar makes this causal: at any bar the
state depends only on earlier bars, so a base is known exactly when a trader
could have known it. The only forward-looking field is `breakout.squat`,
which is an outcome (did it close back under the pivot afterwards) and is
None until enough bars exist.

Bases are counted B1, B2, … within an uptrend. The count restarts at B1
after a close below the `reset_ma` SMA, a base whose low undercuts the
previous base's low, or a decline deeper than `max_base_depth`.

Breakout here = any close above the pivot (the site marks low-volume
breakouts too). `volume_ok` tells whether it meets `bo_vol_mult`; the
screen's BREAKOUT stage (next step) requires it.
"""

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Optional

import numpy as np

from backend.config import BaseConfig
from backend.engine.indicators import sma, trailing_stat


@dataclass
class Breakout:
    date: str
    index: int
    close: float
    through_pct: float               # close / pivot - 1, in %
    volume: int
    volume_ratio: Optional[float]    # vs cfg.vol_baseline
    volume_ratio_sma50: Optional[float]
    volume_ratio_median20: Optional[float]
    volume_ok: bool
    close_in_range: Optional[float]  # (close - low) / (high - low); None when high == low
    weak_close: bool
    gain_1d_pct: Optional[float]
    turnover_cr: float               # close * volume in Rs crore
    squat: Optional[bool]            # closed back below pivot within squat_bars; None = not known yet
    tags: list = field(default_factory=list)


@dataclass
class Base:
    number: int
    status: str                      # "forming" (still active) | "broken_out"
    start_date: str
    end_date: str
    start_index: int
    end_index: int
    bars: int
    weeks: int                       # calendar weeks, rounded down (site convention: 24 days -> 3w)
    pivot: float
    pivot_date: str
    low: float
    low_date: str
    depth_pct: float
    vol_dryup: Optional[float]       # mean volume in base / mean volume of dryup_lookback bars before it
    dist_to_pivot_pct: Optional[float] = None   # forming only: (pivot - last close) / pivot, in %
    breakout: Optional[Breakout] = None

    @property
    def label(self) -> str:
        return f"B{self.number} · {self.weeks}w"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["label"] = self.label
        return d


def _r(x, nd=2):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def detect_bases(candles: list[dict], cfg: BaseConfig) -> list[Base]:
    """candles: ascending [{date, open, high, low, close, volume}]. Returns bases oldest first."""
    n = len(candles)
    if n < 2:
        return []
    dates = [c["date"] for c in candles]
    high = np.array([c["high"] for c in candles], dtype=float)
    by_close = cfg.pivot_basis != "high"
    low = np.array([c["low"] for c in candles], dtype=float)
    close = np.array([c["close"] for c in candles], dtype=float)
    vol = np.array([c["volume"] for c in candles], dtype=float)
    ma = sma(close, cfg.reset_ma) if cfg.reset_ma > 0 else np.full(n, np.nan)

    bases: list[Base] = []
    count = 0
    reset_pending = True        # the first base found is B1
    prev_low: Optional[float] = None

    ref = np.array([c["close"] for c in candles], dtype=float) if by_close else high  # pivot source
    # current candidate base: starts at s; H0 = its first pivot value, H = running pivot, L = running low
    s, H0, H, h_i, L, l_i = 0, ref[0], ref[0], 0, low[0], 0

    def qualifies(start: int) -> bool:
        lo = low[max(0, start - cfg.prior_advance_bars):start + 1].min()
        return high[start] >= lo * (1 + cfg.min_prior_advance)

    def make_base(end: int, status: str) -> Base:
        nonlocal count, reset_pending, prev_low
        if reset_pending or (cfg.reset_on_undercut and prev_low is not None and L < prev_low):
            count = 1
        else:
            count += 1
        reset_pending = False
        prev_low = L
        bars = end - s + 1
        days = (date.fromisoformat(dates[end]) - date.fromisoformat(dates[s])).days
        before = vol[max(0, s - cfg.dryup_lookback):s]
        dry = vol[s:end + 1].mean() / before.mean() if len(before) and before.mean() > 0 else None
        return Base(number=count, status=status, start_date=dates[s], end_date=dates[end],
                    start_index=s, end_index=end, bars=bars, weeks=max(1, days // 7),
                    pivot=_r(H), pivot_date=dates[h_i], low=_r(L), low_date=dates[l_i],
                    depth_pct=_r((H - L) / H * 100), vol_dryup=_r(dry))

    def breakout_at(t: int, base: Base) -> Breakout:
        pivot = H
        sma50 = trailing_stat(vol, t, 50, np.mean)
        med20 = trailing_stat(vol, t, 20, np.median)
        r50 = vol[t] / sma50 if sma50 and not np.isnan(sma50) else None
        r20 = vol[t] / med20 if med20 and not np.isnan(med20) else None
        ratio = r20 if cfg.vol_baseline == "median20" else r50
        rng = high[t] - low[t]
        cir = (close[t] - low[t]) / rng if rng > 0 else None
        after = close[t + 1:t + 1 + cfg.squat_bars]
        if bool((after < pivot).any()):
            squat = True
        elif len(after) >= cfg.squat_bars:
            squat = False
        else:
            squat = None
        vol_ok = bool(ratio is not None and ratio >= cfg.bo_vol_mult)   # plain bools: JSON-serialisable
        weak = bool(cir is not None and cir < cfg.weak_close_cir)
        tags = [t_ for t_, on in (("low volume", not vol_ok), ("weak close", weak), ("squat", squat)) if on]
        return Breakout(date=dates[t], index=t, close=_r(close[t]),
                        through_pct=_r((close[t] / pivot - 1) * 100),
                        volume=int(vol[t]), volume_ratio=_r(ratio), volume_ratio_sma50=_r(r50),
                        volume_ratio_median20=_r(r20), volume_ok=vol_ok, close_in_range=_r(cir),
                        weak_close=weak, gain_1d_pct=_r((close[t] / close[t - 1] - 1) * 100),
                        turnover_cr=_r(close[t] * vol[t] / 1e7), squat=squat, tags=tags)

    for t in range(1, n):
        if not np.isnan(ma[t]) and close[t] < ma[t]:
            reset_pending = True

        if close[t] > H:                                   # close above the pivot
            if t - s >= cfg.min_base_bars and qualifies(s):
                base = make_base(t - 1, "broken_out")
                base.breakout = breakout_at(t, base)
                bases.append(base)
            s, H0, H, h_i, L, l_i = t, ref[t], ref[t], t, low[t], t     # next candidate starts here
            continue

        if not by_close:
            if high[t] > H0 * (1 + cfg.max_pivot_drift):   # grinding higher: a trend, not a base
                s, H0, H, h_i, L, l_i = t, ref[t], ref[t], t, low[t], t
                continue
            if high[t] > H:                                # wick above, close below: pivot rises a little
                H, h_i = high[t], t
        if low[t] < L:
            L, l_i = low[t], t
        if (H - L) / H > cfg.max_base_depth or (t - s + 1) > cfg.max_base_bars:
            if (H - L) / H > cfg.max_base_depth:
                reset_pending = True                       # a correction, not a base: recount
            s, H0, H, h_i, L, l_i = t, ref[t], ref[t], t, low[t], t

    if n - s >= cfg.min_base_bars and qualifies(s):        # still-active base at the last bar
        base = make_base(n - 1, "forming")
        base.dist_to_pivot_pct = _r((H - close[-1]) / H * 100)
        bases.append(base)
    return bases
