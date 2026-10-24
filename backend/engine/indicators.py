"""Causal indicators over numpy arrays (value at i uses bars <= i only)."""

import numpy as np


def sma(x: np.ndarray, n: int) -> np.ndarray:
    """Simple moving average; NaN until n bars exist."""
    out = np.full(len(x), np.nan)
    if n <= 0 or len(x) < n:
        return out
    c = np.cumsum(np.insert(np.asarray(x, dtype=float), 0, 0.0))
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def trailing_stat(x: np.ndarray, i: int, n: int, fn) -> float:
    """fn over the n bars *before* i (excludes bar i). NaN if fewer than n bars."""
    if i < n:
        return float("nan")
    return float(fn(x[i - n:i]))
