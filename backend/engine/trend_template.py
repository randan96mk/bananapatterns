"""Minervini Trend Template: eight checks on the last bar, each reported separately.

  1. close > 150-day and 200-day SMA       5. close > 50-day SMA
  2. 150-day SMA > 200-day SMA             6. close >= 30% above the 52-week low
  3. 200-day SMA rising for ~1 month       7. close within 25% of the 52-week high
  4. 50-day SMA > 150-day and 200-day      8. RS rating >= 70
Thresholds are TrendConfig dials. A check with too little history fails with
value None, so young listings don't pass by accident.
"""

from typing import Optional

import numpy as np

from backend.config import TrendConfig
from backend.engine.indicators import sma


def _r(x, nd=2):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def trend_template(candles: list[dict], rs: Optional[int], cfg: TrendConfig) -> dict:
    close = np.array([c["close"] for c in candles], dtype=float)
    high = np.array([c["high"] for c in candles], dtype=float)
    low = np.array([c["low"] for c in candles], dtype=float)
    i = len(close) - 1
    f, m, s = (sma(close, n)[i] for n in (cfg.ma_fast, cfg.ma_mid, cfg.ma_slow))
    slow = sma(close, cfg.ma_slow)
    s_prev = slow[i - cfg.slow_rising_bars] if i >= cfg.slow_rising_bars else np.nan
    w = min(252, len(close))
    hi52, lo52 = high[-w:].max(), low[-w:].min()
    c = close[i]

    def ok(cond, *vals):
        return bool(cond) if all(not np.isnan(v) for v in vals) else False

    checks = [
        ("above_mid_slow", f"Price above {cfg.ma_mid}- and {cfg.ma_slow}-day", ok(c > m and c > s, m, s),
         f"₹{c:.2f} vs {_r(m)} / {_r(s)}"),
        ("mid_above_slow", f"{cfg.ma_mid}-day above {cfg.ma_slow}-day", ok(m > s, m, s), f"{_r(m)} vs {_r(s)}"),
        ("slow_rising", f"{cfg.ma_slow}-day rising over {cfg.slow_rising_bars} sessions", ok(s > s_prev, s, s_prev),
         f"{_r(s)} vs {_r(s_prev)}"),
        ("fast_above", f"{cfg.ma_fast}-day above {cfg.ma_mid}- and {cfg.ma_slow}-day", ok(f > m and f > s, f, m, s),
         f"{_r(f)} vs {_r(m)} / {_r(s)}"),
        ("above_fast", f"Price above {cfg.ma_fast}-day", ok(c > f, f), f"₹{c:.2f} vs {_r(f)}"),
        ("above_low52", f"≥ {cfg.min_above_low52:.0%} above 52-week low", bool(c >= lo52 * (1 + cfg.min_above_low52)),
         f"{(c / lo52 - 1) * 100:+.1f}% vs low ₹{lo52:.2f}"),
        ("near_high52", f"Within {cfg.max_below_high52:.0%} of 52-week high", bool(c >= hi52 * (1 - cfg.max_below_high52)),
         f"{(c / hi52 - 1) * 100:+.1f}% vs high ₹{hi52:.2f}"),
        ("rs", f"RS rating ≥ {cfg.min_rs}", rs is not None and rs >= cfg.min_rs, "n/a" if rs is None else str(rs)),
    ]
    passed = sum(1 for _, _, p, _ in checks if p)
    return {"passed_all": passed == len(checks), "passed": passed, "of": len(checks),
            "checks": [{"key": k, "label": lab, "pass": p, "value": v} for k, lab, p, v in checks],
            "high52": _r(hi52), "low52": _r(lo52),
            "pct_from_high52": _r((c / hi52 - 1) * 100), "pct_from_low52": _r((c / lo52 - 1) * 100)}
