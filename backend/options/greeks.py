"""Black-Scholes for the NIFTY chain: the implied volatility of a quote (bisection: no SciPy here), the delta, the time
to expiry in years from the sessions left plus today's remaining fraction. Pure functions. Kite gives no greeks, so the
chain's IV and delta come from here, off the mid price (r = 6.5%)."""

import math
from datetime import time
from typing import Optional

RATE = 0.065                       # the risk-free rate used everywhere in the module
SESSIONS_A_YEAR = 252
SESSION_MINUTES = 375              # 9:15 to 15:30
MIN_T = 0.25 / SESSION_MINUTES / SESSIONS_A_YEAR     # a quarter of a minute: never zero time


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1(S: float, K: float, T: float, sigma: float, r: float = RATE) -> float:
    return (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))


def price(S: float, K: float, T: float, sigma: float, kind: str, r: float = RATE) -> float:
    """The Black-Scholes price of a CE / PE on spot S, strike K, T years, volatility sigma."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if kind == "CE" else max(0.0, K - S)
    d1 = _d1(S, K, T, sigma, r)
    d2 = d1 - sigma * math.sqrt(T)
    if kind == "CE":
        return S * _cdf(d1) - K * math.exp(-r * T) * _cdf(d2)
    return K * math.exp(-r * T) * _cdf(-d2) - S * _cdf(-d1)


def delta(S: float, K: float, T: float, sigma: float, kind: str, r: float = RATE) -> float:
    """CE delta in (0, 1), PE delta in (-1, 0)."""
    if T <= 0 or sigma <= 0:
        itm = S > K if kind == "CE" else S < K
        return (1.0 if itm else 0.0) if kind == "CE" else (-1.0 if itm else 0.0)
    d1 = _d1(S, K, T, sigma, r)
    return _cdf(d1) if kind == "CE" else _cdf(d1) - 1.0


def implied_vol(p: float, S: float, K: float, T: float, kind: str, r: float = RATE, lo: float = 0.01, hi: float = 5.0) -> Optional[float]:
    """The volatility whose Black-Scholes price is `p`, by bisection; None when the price is outside what any
    volatility gives (under the intrinsic value, or over the spot / strike)."""
    if p is None or p <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, S - K * math.exp(-r * T)) if kind == "CE" else max(0.0, K * math.exp(-r * T) - S)
    if p < intrinsic or price(S, K, T, lo, kind, r) > p or price(S, K, T, hi, kind, r) < p:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if price(S, K, T, mid, kind, r) > p:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-6:
            break
    return round((lo + hi) / 2, 6)


def time_to_expiry(sessions_left: int, now: Optional[time] = None) -> float:
    """Years to expiry: the full sessions after today plus the part of today still to run (to 15:30), over 252.
    sessions_left is 0 on expiry day."""
    frac = 1.0
    if now is not None:
        left = (15 * 60 + 30) - (now.hour * 60 + now.minute)
        frac = min(1.0, max(0.0, left / SESSION_MINUTES))
    return max(MIN_T, (max(0, sessions_left) + frac) / SESSIONS_A_YEAR)


def iv_and_delta(mid: Optional[float], S: float, K: float, T: float, kind: str) -> tuple[Optional[float], Optional[float]]:
    """(iv, delta) of one quote from its mid price; (None, None) when no IV fits."""
    iv = implied_vol(mid, S, K, T, kind) if mid else None
    if iv is None:
        return None, None
    return iv, round(delta(S, K, T, iv, kind), 4)
