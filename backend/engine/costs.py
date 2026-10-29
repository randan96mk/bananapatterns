"""Zerodha equity intraday charges (the schedule lives in OrbConfig, dated fees_as_of) and slippage. Gross, charges
and net are kept apart so a backtest can show each."""

from math import ceil


def charges(buy_value: float, sell_value: float, cfg) -> dict:
    """Rs for one round trip (one buy order and one sell order) of the given values."""
    brokerage = sum(min(cfg.brokerage_per_order, v * cfg.brokerage_pct / 100) for v in (buy_value, sell_value) if v > 0)
    stt = sell_value * cfg.stt_sell_pct / 100
    exchange = (buy_value + sell_value) * cfg.exchange_pct / 100
    sebi = (buy_value + sell_value) * cfg.sebi_per_crore / 1e7
    gst = (brokerage + exchange + sebi) * cfg.gst_pct / 100
    stamp = buy_value * cfg.stamp_buy_pct / 100
    total = brokerage + stt + exchange + sebi + gst + stamp
    return {k: round(v, 2) for k, v in (("brokerage", brokerage), ("stt", stt), ("exchange", exchange),
                                        ("sebi", sebi), ("gst", gst), ("stamp", stamp), ("total", total))}


def slip(price: float, pct: float, buying: bool) -> float:
    """The fill after slippage: a buy fills higher, a sale lower."""
    return price * (1 + pct / 100) if buying else price * (1 - pct / 100)


def tick_up(x: float, tick: float = 0.05) -> float:
    """x rounded up to the tick (a stop buffer is never smaller than asked)."""
    return round(ceil(round(x / tick, 6)) * tick, 2)
