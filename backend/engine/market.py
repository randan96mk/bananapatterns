"""Market health: one daily status (uptrend / under pressure / correction) from the Nifty 500
and its stocks, plus the A/B tier of a setup.

Signals (all from stored daily candles, computed with each scan):
  index       Nifty 500 vs its 50- and 200-day averages, 50-day rising over 10 sessions
  breadth     % of stocks above their own 50-day (and 200-day) average
  dist_days   sessions in the last dist_window where the index fell >= dist_drop_pct on higher
              volume than the day before. Kite index candles carry no volume, so the volume is
              the total of the stocks' volumes (a close substitute, not exact).
  breakouts   of breakouts in the last breakout_window sessions, the share still working
              (fresh or climbing) vs failed (back below the pivot or played out)
  highs/lows  stocks closing at a 52-week high / low

Distribution days are an IBD method, not an official NSE measure; thresholds are starting values.
"""

import json
from math import ceil
from typing import Optional

import numpy as np

from backend.config import AlertsConfig, MarketConfig
from backend.engine.indicators import sma

UPTREND, PRESSURE, CORRECTION = "uptrend", "pressure", "correction"
STATUS_TEXT = {UPTREND: "🟢 Uptrend", PRESSURE: "🟡 Under pressure", CORRECTION: "🔴 Correction"}
SIZE_FACTOR = {UPTREND: 1.0, PRESSURE: 0.5, CORRECTION: 0.25}
SIZE_NOTE = {UPTREND: "", PRESSURE: "half size: market under pressure", CORRECTION: "quarter size: market in correction"}


def _last(a) -> Optional[float]:
    return None if len(a) == 0 or np.isnan(a[-1]) else float(a[-1])


def index_signals(candles: list[dict]) -> dict:
    if len(candles) < 2:
        return {"close": None, "change_pct": None, "above_50": None, "above_200": None, "ma50_rising": None}
    c = np.array([x["close"] for x in candles], dtype=float)
    ma50, ma200 = sma(c, 50), sma(c, 200)
    m50, m200 = _last(ma50), _last(ma200)
    rising = None if len(ma50) < 11 or np.isnan(ma50[-11]) or m50 is None else bool(m50 > ma50[-11])
    return {"close": round(float(c[-1]), 2), "change_pct": round(float((c[-1] / c[-2] - 1) * 100), 2),
            "above_50": None if m50 is None else bool(c[-1] > m50),
            "above_200": None if m200 is None else bool(c[-1] > m200), "ma50_rising": rising}


def breadth(series: dict[str, list[dict]], as_of: str) -> dict:
    a50 = n50 = a200 = n200 = highs = lows = 0
    for cs in series.values():
        if not cs or cs[-1]["date"] != as_of:
            continue
        c = np.array([x["close"] for x in cs], dtype=float)
        if len(c) >= 50:
            n50 += 1
            a50 += c[-1] > c[-50:].mean()
        if len(c) >= 200:
            n200 += 1
            a200 += c[-1] > c[-200:].mean()
        if len(c) >= 252:
            highs += c[-1] >= c[-252:].max()
            lows += c[-1] <= c[-252:].min()
    pct = lambda a, n: round(100 * a / n, 1) if n else None  # noqa: E731
    return {"above_50_pct": pct(a50, n50), "above_200_pct": pct(a200, n200), "new_highs": int(highs),
            "new_lows": int(lows), "counted": n50}


def distribution_days(index: list[dict], series: dict[str, list[dict]], cfg: MarketConfig) -> Optional[int]:
    """Index down >= dist_drop_pct on higher total stock volume than the day before, last dist_window sessions."""
    if len(index) < cfg.dist_window + 1:
        return None
    vol: dict[str, float] = {}
    dates = {x["date"] for x in index[-(cfg.dist_window + 1):]}
    for cs in series.values():
        for x in cs[-(cfg.dist_window + 15):]:              # a little slack for missing days
            if x["date"] in dates:
                vol[x["date"]] = vol.get(x["date"], 0.0) + x["volume"]
    days = 0
    window = index[-(cfg.dist_window + 1):]
    for prev, cur in zip(window, window[1:]):
        drop = (cur["close"] / prev["close"] - 1) * 100
        if drop <= -cfg.dist_drop_pct and vol.get(cur["date"], 0) > vol.get(prev["date"], 0) > 0:
            days += 1
    return days


def breakout_success(stages: list, cfg: MarketConfig) -> dict:
    """stages: StageResult-like objects (the Bases screen). Working = fresh/climbing; failed = below_pivot/played_out."""
    working = failed = 0
    for r in stages:
        if r.sessions_since_breakout is None or r.sessions_since_breakout > cfg.breakout_window:
            continue
        if r.stage in ("fresh", "climbing"):
            working += 1
        elif r.stage in ("below_pivot", "played_out"):
            failed += 1
    total = working + failed
    return {"working": working, "total": total, "rate": round(working / total, 2) if total else None}


def classify(sig: dict, cfg: MarketConfig) -> str:
    """Correction first, then uptrend; unknown signals (None) are skipped, except that very weak breadth
    counts as a correction unless the index is known to be above its 50-day."""
    ix, br, dd, bo = sig["index"], sig["breadth"]["above_50_pct"], sig["dist_days"], sig["breakouts"]["rate"]
    if (ix["above_200"] is False or (ix["above_50"] is not True and br is not None and br < cfg.breadth_correction)
            or (dd is not None and dd >= cfg.dist_days_correction)):
        return CORRECTION
    ok = [ix["above_50"] is not False, br is None or br >= cfg.breadth_up,
          dd is None or dd <= cfg.dist_days_max, bo is None or bo >= cfg.breakout_rate_min]
    if ix["above_50"] is None and br is None:
        return PRESSURE                                   # nothing to go on
    return UPTREND if all(ok) else PRESSURE


def compute(index: list[dict], series: dict[str, list[dict]], stages: list, as_of: str, cfg: MarketConfig) -> dict:
    sig = {"as_of": as_of, "index": index_signals(index), "breadth": breadth(series, as_of),
           "dist_days": distribution_days(index, series, cfg), "breakouts": breakout_success(stages, cfg)}
    sig["status"] = classify(sig, cfg)
    sig["windows"] = {"dist": cfg.dist_window, "breakouts": cfg.breakout_window}
    return sig


# --- storage ------------------------------------------------------------------------

def save(conn, h: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO market_health (as_of, status, payload) VALUES (?, ?, ?)",
                 (h["as_of"], h["status"], json.dumps(h)))


def latest(conn, on_or_before: Optional[str] = None) -> Optional[dict]:
    q, args = "SELECT payload FROM market_health", []
    if on_or_before:
        q, args = q + " WHERE as_of <= ?", [on_or_before]
    row = conn.execute(q + " ORDER BY as_of DESC LIMIT 1", args).fetchone()
    return json.loads(row["payload"]) if row else None


def before(conn, as_of: str) -> Optional[dict]:
    row = conn.execute("SELECT payload FROM market_health WHERE as_of < ? ORDER BY as_of DESC LIMIT 1",
                       (as_of,)).fetchone()
    return json.loads(row["payload"]) if row else None


def history(conn, limit: int = 60) -> list[dict]:
    rows = conn.execute("SELECT as_of, status FROM market_health ORDER BY as_of DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]


# --- A/B tier ---------------------------------------------------------------------------

def tier(p: dict, status: Optional[str], cfg: AlertsConfig) -> str:
    """A: VCP or cup with handle qualifies, RS >= a_min_rs, trend >= a_min_trend, industry in the top a_top_industry_frac,
    market not in correction, no red flag (promoter selling, legal action, auditor exit, downgrade). Otherwise B."""
    vcp_ok = bool(p.get("vcp")) or bool(p.get("vcp_tag")) or bool(p.get("cup")) or bool(p.get("cup_tag"))
    rank, of = p.get("industry_rank"), p.get("industry_of")
    top_industry = rank is not None and of and rank <= max(1, ceil(of * cfg.a_top_industry_frac))
    ok = (vcp_ok and (p.get("rs") or 0) >= cfg.a_min_rs and (p.get("tt_passed") or 0) >= cfg.a_min_trend
          and top_industry and status != CORRECTION)
    if ok and p.get("symbol"):
        from backend.ingest import holders
        ok = not holders.has_red(p["symbol"], cfg)
    return "A" if ok else "B"
