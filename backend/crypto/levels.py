"""₿ The levels that matter, by code from our own stored candles (pure functions; the store is read only by `levels_at`):

  pd       PDH / PDL / PDC: the previous day's high, low, close (crypto.day_boundary: the UTC day, as Delta, or IST)
  session  SES_H / SES_L: the running high / low of the session in progress and the finished sessions' (the last
           Asia, London and New York sessions, IST: Asia 5:30 AM–1:30 PM · London 1:30–6:30 PM · New York 6:30 PM–3:30 AM;
           the quiet 3:30–5:30 AM gap belongs to New York)
  vwap     VWAP / VWAP+1 / VWAP-1: the day's VWAP (typical price × volume, cumulative from the day open) ±1σ (volume-weighted)
  profile  POC / VAH / VAL: the volume profile of the last 24 h in BIN-point bins (each candle's volume spread evenly
           over the bins its range covers), the value area = 70 % of the volume around the POC
  pivot    P, R1…R5, S1…S5: the previous period's pivots (backend/crypto/pivots.py, crypto.pivot_type), as before
Every level is computed from the candles BEFORE the candle at `t` (a candle never makes its own level).
A level = {kind, name, px, label ("PDL 59,760 · prev day low"), family, session (SES_*), start, end (its time span, unix)}."""

import bisect
import math
from typing import Optional

from backend.crypto import pivots as pv
from backend.timefmt import ampm

IST_OFFSET = pv.IST_OFFSET
BIN = 20.0                       # the volume profile's bin (points)
VALUE_AREA = 0.70
FAMILIES = ("pd", "session", "vwap", "profile", "pivot")
FAMILY_KEY = {"pd": "levels_pd", "session": "levels_sessions", "vwap": "levels_vwap", "profile": "levels_profile", "pivot": "levels_pivots"}
FAMILY_WORD = {"pd": "PDH/PDL/PDC", "session": "session H/L", "vwap": "VWAP", "profile": "POC/VA", "pivot": "pivots"}
# (key, label, short, start minute of the IST day, end minute; New York runs past midnight to 3:30 AM)
SESSIONS = (("asia", "Asia", "Asia", 330, 810), ("london", "London", "London", 810, 1110), ("ny", "New York", "NY", 1110, 1650))
SESSION_LABEL = {k: lab for k, lab, _, _, _ in SESSIONS}
SESSION_SHORT = {k: sh for k, _, sh, _, _ in SESSIONS}
PD_WORD = {"PDH": "prev day high", "PDL": "prev day low", "PDC": "prev day close"}


def px(v) -> str:
    return f"{float(v):,.2f}".rstrip("0").rstrip(".")


# --- the sessions -------------------------------------------------------------------------------------------------------------

def _ist_minute(ts: float) -> float:
    return ((int(ts) + IST_OFFSET) % 86400) / 60


def _ist_day_start(ts: float) -> int:
    """Unix of 00:00 IST of the IST date holding ts."""
    return int(ts) - (int(ts) + IST_OFFSET) % 86400


def session_of(ts: float) -> str:
    """'asia' (5:30 AM–1:30 PM IST) | 'london' (1:30–6:30 PM) | 'ny' (6:30 PM–5:30 AM: the session to 3:30 AM and the gap)."""
    m = _ist_minute(ts)
    if 330 <= m < 810:
        return "asia"
    if 810 <= m < 1110:
        return "london"
    return "ny"


def session_span(ts: float) -> dict:
    """The session instance holding ts: {key, label, start, end (3:30 AM for New York), until (the next session's start)}."""
    key = session_of(ts)
    day = _ist_day_start(ts)
    m = _ist_minute(ts)
    if key == "ny" and m < 330:
        day -= 86400
    _, lab, _, a, b = next(s for s in SESSIONS if s[0] == key)
    start = day + a * 60
    until = start + {"asia": 8 * 3600, "london": 5 * 3600, "ny": 11 * 3600}[key]
    return {"key": key, "label": lab, "start": start, "end": day + b * 60, "until": until}


def instances(start: int, end: int) -> list[dict]:
    """The session instances overlapping [start, end], oldest first."""
    out, ts = [], int(start)
    while ts <= end:
        s = session_span(ts)
        out.append(s)
        ts = s["until"]
    return out


def windows_text() -> str:
    return "IST: Asia 5:30 AM–1:30 PM · London 1:30–6:30 PM · NY 6:30 PM–3:30 AM"


def sessions_info(now_ts: float) -> dict:
    """The three IST windows + the current one (the summary's `sessions`)."""
    cur = session_span(now_ts)
    wins = [{"key": k, "label": lab, "short": sh, "from": ampm(f"{a // 60 % 24:02d}:{a % 60:02d}"), "until": ampm(f"{b // 60 % 24:02d}:{b % 60:02d}")}
            for k, lab, sh, a, b in SESSIONS]
    return {"windows": wins, "current": cur["key"], "current_label": cur["label"], "start": cur["start"], "end": cur["end"],
            "text": windows_text()}


# --- the pieces -------------------------------------------------------------------------------------------------------------

def _slice(candles: list[dict], lo: int, hi: int) -> list[dict]:
    """candles with lo <= t < hi (candles oldest first)."""
    ts = [b["t"] for b in candles]
    return candles[bisect.bisect_left(ts, lo): bisect.bisect_left(ts, hi)]


def _mk(kind: str, name: str, v: float, word: str, family: str, start: int, end: int, session: Optional[str] = None) -> dict:
    d = {"kind": kind, "name": name, "px": round(float(v), 2), "label": f"{name} {px(v)} · {word}", "family": family, "start": int(start),
         "end": int(end)}
    if session:
        d["session"] = session
    return d


def prev_day(candles: list[dict], t: int, boundary: str = "utc", daily: Optional[list[dict]] = None) -> Optional[dict]:
    """{h, l, c, start (the day of t), prev_start} of the day before t's day, from the candles (else the stored 1d candles
    on the UTC boundary)."""
    start = pv.period_start(t, "daily", boundary)
    prev = start - 86400
    bars = _slice(candles, prev, start)
    if bars:
        return {"h": max(b["h"] for b in bars), "l": min(b["l"] for b in bars), "c": bars[-1]["c"], "start": start, "prev_start": prev}
    if daily and boundary == "utc":
        d = next((b for b in daily if b["t"] == prev), None)
        if d:
            return {"h": d["h"], "l": d["l"], "c": d["c"], "start": start, "prev_start": prev}
    return None


def vwap(bars: list[dict]) -> Optional[dict]:
    """{vwap, sd} over the bars (typical price × volume; σ volume-weighted), None without volume."""
    vol = sum(float(b.get("v") or 0) for b in bars)
    if vol <= 0:
        return None
    tp = [((b["h"] + b["l"] + b["c"]) / 3, float(b.get("v") or 0)) for b in bars]
    vw = sum(p * v for p, v in tp) / vol
    sd = math.sqrt(max(0.0, sum(v * (p - vw) ** 2 for p, v in tp) / vol))
    return {"vwap": round(vw, 2), "sd": round(sd, 2)}


def vwap_series(candles: list[dict], start: int, end: int) -> list[dict]:
    """[{t, vwap, up, dn}] after each candle of [start, end) (the chart's VWAP ±1σ lines)."""
    out, sv, spv, spv2 = [], 0.0, 0.0, 0.0
    for b in _slice(candles, start, end):
        v = float(b.get("v") or 0)
        p = (b["h"] + b["l"] + b["c"]) / 3
        sv, spv, spv2 = sv + v, spv + p * v, spv2 + p * p * v
        if sv <= 0:
            continue
        vw = spv / sv
        sd = math.sqrt(max(0.0, spv2 / sv - vw * vw))
        out.append({"t": b["t"], "vwap": round(vw, 2), "up": round(vw + sd, 2), "dn": round(vw - sd, 2)})
    return out


def profile(bars: list[dict], bin_size: float = BIN) -> Optional[dict]:
    """{poc, vah, val}: each candle's volume spread evenly over the bins its range covers; the value area grows from the
    POC bin, one bin at a time to the side with more volume, until it holds VALUE_AREA of the volume."""
    vol: dict[int, float] = {}
    for b in bars:
        v = float(b.get("v") or 0)
        if v <= 0:
            continue
        lo, hi = int(math.floor(b["l"] / bin_size)), int(math.floor(b["h"] / bin_size))
        share = v / (hi - lo + 1)
        for i in range(lo, hi + 1):
            vol[i] = vol.get(i, 0.0) + share
    if not vol:
        return None
    total = sum(vol.values())
    poc = max(sorted(vol), key=lambda i: vol[i])
    lo = hi = poc
    have = vol[poc]
    while have < VALUE_AREA * total - 1e-9:
        up, dn = vol.get(hi + 1), vol.get(lo - 1)
        if up is None and dn is None:
            break
        if dn is None or (up is not None and up >= dn):
            hi += 1
            have += up
        else:
            lo -= 1
            have += dn
    return {"poc": round((poc + 0.5) * bin_size, 2), "vah": round((hi + 1) * bin_size, 2), "val": round(lo * bin_size, 2)}


def session_levels(candles: list[dict], t: int) -> list[dict]:
    """SES_H / SES_L of the session in progress (running, before t) and of the last finished Asia / London / New York."""
    out, seen = [], set()
    s = session_span(t)
    for _ in range(4):
        if s["key"] not in seen:
            seen.add(s["key"])
            bars = _slice(candles, s["start"], min(s["until"], t))
            if bars:
                run = s["until"] > t
                tail = " (running)" if run else ""
                short = SESSION_SHORT[s["key"]]
                end = t if run else s["until"]
                out.append(_mk("SES_H", f"{short} H", max(b["h"] for b in bars), f"{s['label']} high{tail}", "session", s["start"], end, s["key"]))
                out.append(_mk("SES_L", f"{short} L", min(b["l"] for b in bars), f"{s['label']} low{tail}", "session", s["start"], end, s["key"]))
        s = session_span(s["start"] - 1)
    return out


def on(config, family: str) -> bool:
    return bool(getattr(config.crypto, FAMILY_KEY[family], True))


def levels_all(config, candles: list[dict], t: int, daily: Optional[list[dict]] = None, pivots: Optional[dict] = None) -> list[dict]:
    """Every level in force for the candle opening at `t` (the families switched on), from `candles` (the timeframe's,
    oldest first, reaching back to the previous day) and the pivots `pivots` ({period, from, start, levels} as
    signals.levels_for; read from the store when None)."""
    c = config.crypto
    t = int(t)
    out: list[dict] = []
    day_start = pv.period_start(t, "daily", c.day_boundary)
    if on(config, "pd"):
        pd = prev_day(candles, t, c.day_boundary, daily)
        if pd:
            for kind, v in (("PDH", pd["h"]), ("PDL", pd["l"]), ("PDC", pd["c"])):
                out.append(_mk(kind, kind, v, PD_WORD[kind], "pd", day_start, day_start + 86400))
    if on(config, "session"):
        out += session_levels(candles, t)
    if on(config, "vwap"):
        vw = vwap(_slice(candles, day_start, t))
        if vw:
            out.append(_mk("VWAP", "VWAP", vw["vwap"], "daily VWAP", "vwap", day_start, t))
            if vw["sd"] > 0:
                out.append(_mk("VWAP+1", "VWAP +1σ", vw["vwap"] + vw["sd"], "VWAP + 1σ", "vwap", day_start, t))
                out.append(_mk("VWAP-1", "VWAP −1σ", vw["vwap"] - vw["sd"], "VWAP − 1σ", "vwap", day_start, t))
    if on(config, "profile"):
        pr = profile(_slice(candles, t - 86400, t))
        if pr:
            out.append(_mk("POC", "POC", pr["poc"], "24 h point of control", "profile", t - 86400, t))
            out.append(_mk("VAH", "VAH", pr["vah"], "24 h value area high", "profile", t - 86400, t))
            out.append(_mk("VAL", "VAL", pr["val"], "24 h value area low", "profile", t - 86400, t))
    if on(config, "pivot"):
        if pivots is None:
            from backend.crypto import signals as sg
            pivots = sg.levels_for(config, t)
        if pivots and pivots.get("levels"):
            period = pivots.get("period") or "daily"
            p0 = int(pivots.get("start") or day_start)
            p1 = pv.period_start(p0 + 40 * 86400, period, c.day_boundary) if period == "monthly" else p0 + (7 if period == "weekly" else 1) * 86400
            for name in pv.NAMES:
                if name in pivots["levels"]:
                    out.append(_mk(name, name, pivots["levels"][name], f"{period} pivot {name}", "pivot", p0, p1))
    return out


def levels_at(config, t: int, candles: Optional[list[dict]] = None, pivots: Optional[dict] = None) -> list[dict]:
    """levels_all for the candle opening at `t`, reading the stored candles (two days back) and the 1d candles."""
    from backend.crypto import feed
    c = config.crypto
    lo = pv.period_start(int(t), "daily", c.day_boundary) - 86400
    lo = min(lo, int(t) - 86400)
    cs = candles if candles is not None else feed.load(c.symbol, c.tf, lo, int(t))
    daily = feed.load(c.symbol, "1d", lo - 86400, int(t)) if c.day_boundary == "utc" else None
    return levels_all(config, cs, t, daily, pivots)
