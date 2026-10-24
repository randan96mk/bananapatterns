"""Sort score 0-100 for the VCP screen. Weights are dials (VcpConfig.score_weights);
they are untuned and should only be tuned by the backtester (later phase).

Each factor is scaled to 0..1:
  RS               rs / 99
  tightening       1 at <= 0.5, 0 at >= 1.5 (ATR ratio)
  dry-up           1 at <= 0.3, 0 at >= 1.5 (volume ratio)
  pivot proximity  1 at the pivot, 0 at 25% under it (breakouts count as at the pivot)
  52-week high     1 at the high, 0 at 25% below it
  industry RS      1 for the top industry, 0 for the bottom
"""

from typing import Optional


def _lin(x: Optional[float], best: float, worst: float) -> float:
    if x is None:
        return 0.0
    t = (worst - x) / (worst - best)
    return max(0.0, min(1.0, t))


def vcp_score(weights: list, rs, tightening, dryup, dist_to_pivot_pct, pct_from_high52,
              industry_rank, industry_of) -> float:
    ind = (industry_of - industry_rank) / (industry_of - 1) if industry_rank and industry_of and industry_of > 1 else 0.0
    parts = [
        (rs or 0) / 99,
        _lin(tightening, 0.5, 1.5),
        _lin(dryup, 0.3, 1.5),
        _lin(max(dist_to_pivot_pct or 0.0, 0.0), 0.0, 25.0),
        _lin(abs(pct_from_high52) if pct_from_high52 is not None else None, 0.0, 25.0),
        ind,
    ]
    total = sum(weights) or 1.0
    return round(100 * sum(w * p for w, p in zip(weights, parts)) / total, 1)
