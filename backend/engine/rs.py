"""Relative strength (RS) rating, 1-99, ranked across the universe.

RS score = sum(w_k * ROC(n_k)) with the default weights 0.4/0.2/0.2/0.2 over
63/126/189/252 sessions (3/6/9/12 months). This is the common community
approximation of IBD's RS rating, not IBD's own formula. A stock with fewer
than 252 bars but at least `rs_min_bars` uses the periods it has, with the
weights renormalised, so young listings still get a rating.

The rating is the score's percentile among all stocks scored on the same
date: 99 = strongest, 1 = weakest. Because we rank against the Nifty 500
(the site ranks against ~5,000 liquid stocks) values differ from the site's.
"""

from statistics import median
from typing import Optional

from backend.config import TrendConfig


def rs_score(closes: list[float], cfg: TrendConfig) -> Optional[float]:
    """Weighted rate of change on the last bar, or None if too little history."""
    n = len(closes)
    if n <= cfg.rs_min_bars:
        return None
    total = wsum = 0.0
    for w, p in zip(cfg.rs_weights, cfg.rs_periods):
        if n > p and closes[-1 - p] > 0:
            total += w * (closes[-1] / closes[-1 - p] - 1)
            wsum += w
    return total / wsum if wsum else None


def rs_ratings(scores: dict[str, Optional[float]]) -> dict[str, Optional[int]]:
    """Percentile-rank scores to 1-99; ties share the same rating; None stays None."""
    valid = sorted((v, k) for k, v in scores.items() if v is not None)
    out: dict[str, Optional[int]] = {k: None for k in scores}
    m = len(valid)
    if m == 1:
        out[valid[0][1]] = 99
        return out
    i = 0
    while i < m:
        j = i
        while j + 1 < m and valid[j + 1][0] == valid[i][0]:
            j += 1
        pct = ((i + j) / 2) / (m - 1)                     # average rank of the tie group
        for k in range(i, j + 1):
            out[valid[k][1]] = int(round(1 + 98 * pct))
        i = j + 1
    return out


def industry_ranks(ratings: dict[str, Optional[int]], industry: dict[str, str]) -> dict[str, dict]:
    """Industries ranked by the median RS of their stocks: {industry: {rank, of, median_rs, stocks}}."""
    groups: dict[str, list[int]] = {}
    for sym, r in ratings.items():
        ind = industry.get(sym)
        if r is not None and ind:
            groups.setdefault(ind, []).append(r)
    med = {ind: median(v) for ind, v in groups.items()}
    order = sorted(med, key=lambda k: -med[k])
    return {ind: {"rank": i + 1, "of": len(order), "median_rs": med[ind], "stocks": len(groups[ind])}
            for i, ind in enumerate(order)}
