"""Intraday indicators on bar lists ({o, h, l, c, v}); each returns the value at the last bar (None if too short)."""


def ema(values: list[float], n: int):
    if len(values) < n:
        return None
    k, e = 2 / (n + 1), sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi_state(new_closes: list[float], n: int = 14, state: tuple | None = None) -> tuple:
    """Wilder's RSI continued over `new_closes`, the closes after those `state` has seen: (value, state). The
    state (closes seen, average gain, average loss, last close) lets the backtest add one candle at a time and get
    the number rsi() gives over the whole list."""
    seen, g, lo, prev = state or (0, 0.0, 0.0, None)
    for c in new_closes:
        if prev is not None:
            d = c - prev
            up, dn = (d, 0.0) if d > 0 else (0.0, -d)
            if seen <= n:                               # the first n changes: a simple average
                g, lo = g + up / n, lo + dn / n
            else:
                g, lo = (g * (n - 1) + up) / n, (lo * (n - 1) + dn) / n
        prev, seen = c, seen + 1
    val = None if seen <= n else 100.0 if lo == 0 else 100 - 100 / (1 + g / lo)
    return val, (seen, g, lo, prev)


def rsi(closes: list[float], n: int = 14):
    """Wilder's RSI at the last close."""
    return rsi_state(closes, n)[0]


def vwap_state(new_bars: list[dict], state: tuple | None = None) -> tuple:
    """Session VWAP continued over the bars after those `state` (bars seen, Σ typical price × volume, Σ volume)
    has seen: (value, state)."""
    seen, pv, vol = state or (0, 0.0, 0.0)
    for b in new_bars:
        v = b["v"]
        pv += (b["h"] + b["l"] + b["c"]) * v
        vol += v
    return (pv / 3 / vol if vol else None), (seen + len(new_bars), pv, vol)


def vwap(today: list[dict]):
    """Session VWAP from today's bars (typical price × volume)."""
    return vwap_state(today)[0]


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
