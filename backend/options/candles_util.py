"""1-minute bar helpers for the options book: Kite historical minute rows -> {day: bars} and n-minute resampling
(moved here from the removed backend/intraday package)."""


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


def from_kite(rows: list[dict]) -> dict[str, list[dict]]:
    """Kite historical minute rows -> {day: bars}."""
    out: dict[str, list[dict]] = {}
    for r in rows:
        dt = r["date"]
        d, t = dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        out.setdefault(d, []).append({"t": t, "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"],
                                      "v": int(r.get("volume") or 0)})
    return out
