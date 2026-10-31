"""Intraday indicators on bar lists ({o, h, l, c, v}); each returns the value at the last bar (None if too short)."""


def ema(values: list[float], n: int):
    if len(values) < n:
        return None
    k, e = 2 / (n + 1), sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes: list[float], n: int = 14):
    """Wilder's RSI."""
    if len(closes) <= n:
        return None
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    g, lo = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(gains)):
        g, lo = (g * (n - 1) + gains[i]) / n, (lo * (n - 1) + losses[i]) / n
    return 100.0 if lo == 0 else 100 - 100 / (1 + g / lo)


def vwap(today: list[dict]):
    """Session VWAP from today's bars (typical price × volume)."""
    pv = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in today)
    vol = sum(b["v"] for b in today)
    return pv / vol if vol else None


def rvol(bars: list[dict], n: int = 20):
    """The last bar's volume over the average of the n bars before it."""
    if len(bars) <= n:
        return None
    avg = sum(b["v"] for b in bars[-n - 1:-1]) / n
    return bars[-1]["v"] / avg if avg else None


def atr(bars: list[dict], n: int = 14):
    if len(bars) <= n:
        return None
    tr = [max(b["h"], p["c"]) - min(b["l"], p["c"]) for p, b in zip(bars, bars[1:])]
    a = sum(tr[:n]) / n
    for x in tr[n:]:
        a = (a * (n - 1) + x) / n
    return a


def supertrend_up(bars: list[dict], n: int = 10, mult: float = 3.0):
    """True when the Supertrend(n, mult) is in an up-trend at the last bar (Wilder ATR, one pass)."""
    if len(bars) <= n + 1:
        return None
    tr = [0.0] + [max(b["h"], p["c"]) - min(b["l"], p["c"]) for p, b in zip(bars, bars[1:])]
    a = sum(tr[1:n + 1]) / n
    up_trend, fu, fl = True, None, None
    for i in range(n, len(bars)):
        if i > n:
            a = (a * (n - 1) + tr[i]) / n
        mid = (bars[i]["h"] + bars[i]["l"]) / 2
        bu, bl = mid + mult * a, mid - mult * a
        fu = bu if fu is None or bu < fu or bars[i - 1]["c"] > fu else fu
        fl = bl if fl is None or bl > fl or bars[i - 1]["c"] < fl else fl
        up_trend = bars[i]["c"] > fu if not up_trend else bars[i]["c"] >= fl
    return up_trend
