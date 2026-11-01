"""1-minute candles from Kite ticks, and resampling to longer bars. A bar is {t: 'HH:MM', o, h, l, c, v}."""

from datetime import datetime


class CandleBuilder:
    """Builds each instrument's 1-minute bars from ticks (last price + the day's cumulative volume). A bar is
    complete once a tick of a later minute arrives, or when close_until() is called at a candle close."""

    def __init__(self):
        self.bars: dict[int, list[dict]] = {}       # token -> completed bars of the day
        self.cur: dict[int, dict] = {}               # token -> the forming bar
        self.cum: dict[int, int] = {}                # token -> last cumulative volume

    def add(self, token: int, price: float, cum_volume: int, at: datetime) -> None:
        t = at.strftime("%H:%M")
        vol = max(0, cum_volume - self.cum.get(token, cum_volume)) if token in self.cum else 0
        self.cum[token] = cum_volume
        bar = self.cur.get(token)
        if bar is not None and bar["t"] != t:
            self.bars.setdefault(token, []).append(bar)
            bar = None
        if bar is None:
            self.cur[token] = {"t": t, "o": price, "h": price, "l": price, "c": price, "v": vol}
        else:
            bar.update(h=max(bar["h"], price), l=min(bar["l"], price), c=price, v=bar["v"] + vol)

    def close_until(self, hhmm: str) -> None:
        """Completes every forming bar that started before hhmm (the candle close has passed)."""
        for token, bar in list(self.cur.items()):
            if bar["t"] < hhmm:
                self.bars.setdefault(token, []).append(bar)
                del self.cur[token]

    def day(self, token: int) -> list[dict]:
        return list(self.bars.get(token, []))


def _minutes(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def resample(bars: list[dict], n: int, start: str = "09:15") -> list[dict]:
    """1-minute bars -> n-minute bars aligned to `start` (9:15, 9:20 … for n = 5)."""
    if n <= 1:
        return list(bars)
    out, s, last = [], _minutes(start), None
    for b in bars:
        t = b["t"]
        slot = s + (int(t[:2]) * 60 + int(t[3:5]) - s) // n * n
        if slot == last:
            o = out[-1]
            if b["h"] > o["h"]:
                o["h"] = b["h"]
            if b["l"] < o["l"]:
                o["l"] = b["l"]
            o["c"] = b["c"]
            o["v"] += b["v"]
        else:
            out.append({**b, "t": f"{slot // 60:02d}:{slot % 60:02d}"})
            last = slot
    return out
