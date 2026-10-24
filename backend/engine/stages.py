"""Pipeline stage of one stock on its last bar: forming / fresh / climbing / played_out.

Built on the bases from engine.bases. Only bars up to the last one are used,
so classifying candles[:k] gives the stage a trader would have seen on day k
(that is how the scan gets "yesterday" for the what-changed feed).

Precedence when several apply: fresh > forming > climbing > played_out.
A climber that starts its next base shows as forming (B7 under a new lid).

Stages (all thresholds in ScreenConfig):
  fresh       last breakout within `fresh_sessions`, still above its pivot, run not ended
              (with require_volume, a low-volume breakout is `fresh_low_volume` instead)
  forming     active base >= `min_forming_weeks`, close within `max_dist_to_pivot` under the pivot
  climbing    older breakout, still above its pivot, run not ended
  played_out  the last breakout's run ended within `played_out_window` sessions: a close below the
              `played_out_ma` SMA or `played_out_drop` below the pivot
  below_pivot run not ended but closed back under the pivot (feed only, no card)
Gates for every card: close above the `trend_ma` SMA and median turnover >= `min_turnover_cr`.
"""

from dataclasses import asdict, dataclass
from typing import Optional

import numpy as np

from backend.config import ScreenConfig
from backend.engine.bases import Base
from backend.engine.indicators import sma

CARD_STAGES = ("forming", "fresh", "climbing", "played_out")


@dataclass
class StageResult:
    stage: Optional[str]            # one of CARD_STAGES, "fresh_low_volume", "below_pivot", or None
    reason: str                     # why (esp. why None): "illiquid", "below 200-day", "no base", ...
    date: str
    close: float
    change_pct: Optional[float]
    turnover_cr: Optional[float]    # median daily turnover over turnover_window
    base_label: Optional[str] = None
    base_number: Optional[int] = None
    base_weeks: Optional[int] = None
    pivot: Optional[float] = None
    dist_to_pivot_pct: Optional[float] = None     # forming: (pivot - close) / pivot
    gain_from_pivot_pct: Optional[float] = None   # breakout stages: close / pivot - 1
    breakout_date: Optional[str] = None
    sessions_since_breakout: Optional[int] = None
    volume_ratio: Optional[float] = None
    volume_ok: Optional[bool] = None
    depth_pct: Optional[float] = None
    ended_date: Optional[str] = None
    ended_reason: Optional[str] = None
    peak_gain_pct: Optional[float] = None         # best close since breakout vs pivot
    base_start: Optional[str] = None              # base box, for the grid view's mini chart
    base_end: Optional[str] = None
    base_low: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _r(x, nd=2):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def classify(candles: list[dict], bases: list[Base], cfg: ScreenConfig) -> StageResult:
    n = len(candles)
    close = np.array([c["close"] for c in candles], dtype=float)
    i = n - 1
    vol = np.array([c["volume"] for c in candles], dtype=float)
    tw = min(cfg.turnover_window, n)
    turnover = float(np.median(close[-tw:] * vol[-tw:])) / 1e7
    res = StageResult(stage=None, reason="", date=candles[i]["date"], close=_r(close[i]),
                      change_pct=_r((close[i] / close[i - 1] - 1) * 100) if n > 1 else None,
                      turnover_cr=_r(turnover))

    if turnover < cfg.min_turnover_cr:
        res.reason = f"illiquid (median turnover Rs {turnover:.1f} cr)"
        return res
    if cfg.trend_ma > 0:
        tma = sma(close, cfg.trend_ma)[i]
        if np.isnan(tma):
            res.reason = f"under {cfg.trend_ma} sessions of history"
            return res
        if close[i] <= tma:
            res.reason = f"below its {cfg.trend_ma}-day line"
            return res

    last_bo = next((b for b in reversed(bases) if b.breakout is not None), None)
    latest = bases[-1] if bases else None

    run_ended = None   # (index, reason, gain at end)
    if last_bo is not None:
        bo, pivot = last_bo.breakout, last_bo.pivot
        ma = sma(close, cfg.played_out_ma) if cfg.played_out_ma > 0 else np.full(n, np.nan)
        for j in range(bo.index + 1, n):
            if not np.isnan(ma[j]) and close[j] < ma[j]:
                run_ended = (j, f"closed below its {cfg.played_out_ma}-day line")
                break
            if close[j] <= pivot * (1 - cfg.played_out_drop):
                run_ended = (j, f"fell {cfg.played_out_drop * 100:.0f}% below the pivot")
                break
        res.base_label, res.base_number, res.base_weeks = last_bo.label, last_bo.number, last_bo.weeks
        res.pivot, res.depth_pct = last_bo.pivot, last_bo.depth_pct
        res.base_start, res.base_end, res.base_low = last_bo.start_date, last_bo.end_date, _r(last_bo.low)
        res.breakout_date, res.sessions_since_breakout = bo.date, i - bo.index
        res.volume_ratio, res.volume_ok = bo.volume_ratio, bo.volume_ok
        res.gain_from_pivot_pct = _r((close[i] / pivot - 1) * 100)
        end = run_ended[0] if run_ended else i
        res.peak_gain_pct = _r((close[bo.index:end + 1].max() / pivot - 1) * 100)
        if run_ended:
            res.ended_date = candles[run_ended[0]]["date"]
            res.ended_reason = run_ended[1]
            res.gain_from_pivot_pct = _r((close[run_ended[0]] / pivot - 1) * 100)

    def set_forming():
        res.stage, res.reason = "forming", "pausing under its lid"
        res.base_label, res.base_number, res.base_weeks = latest.label, latest.number, latest.weeks
        res.pivot, res.depth_pct = latest.pivot, latest.depth_pct
        res.base_start, res.base_end, res.base_low = latest.start_date, latest.end_date, _r(latest.low)
        res.dist_to_pivot_pct = latest.dist_to_pivot_pct
        res.breakout_date = res.sessions_since_breakout = res.volume_ratio = res.volume_ok = None
        res.gain_from_pivot_pct = res.ended_date = res.ended_reason = res.peak_gain_pct = None
        return res

    alive = last_bo is not None and run_ended is None and close[i] > last_bo.pivot
    counts = last_bo is not None and (last_bo.breakout.volume_ok or not cfg.require_volume)

    if alive and res.sessions_since_breakout < cfg.fresh_sessions:
        res.stage = "fresh" if counts else "fresh_low_volume"
        res.reason = "cleared its pivot" + ("" if counts else " on low volume")
        return res
    forming_ok = (latest is not None and latest.status == "forming" and latest.weeks >= cfg.min_forming_weeks
                  and latest.dist_to_pivot_pct is not None
                  and latest.dist_to_pivot_pct <= cfg.max_dist_to_pivot * 100)
    if forming_ok:
        return set_forming()
    if alive and counts:
        res.stage, res.reason = "climbing", "breakout holding"
        return res
    if run_ended and i - run_ended[0] < cfg.played_out_window and counts:
        res.stage, res.reason = "played_out", run_ended[1]
        return res
    if last_bo is not None and run_ended is None and close[i] <= last_bo.pivot and counts:
        res.stage, res.reason = "below_pivot", "back below the pivot it broke out of"
        return res

    if latest is not None and latest.status == "forming":
        if latest.weeks < cfg.min_forming_weeks:
            res.reason = f"base only {latest.weeks}w old"
        else:
            res.reason = f"{latest.dist_to_pivot_pct:.1f}% under its pivot (too far)"
    else:
        res.reason = "no active base"
    return res


def classify_vcp(candles: list[dict], bases: list[Base], rs: Optional[int], trend_passed: int,
                 screen_cfg: ScreenConfig, vcp_cfg) -> tuple[StageResult, Optional[dict]]:
    """Stage on the VCP screen: the base stage, kept only if the relevant base is a VCP.

    Forming uses the VCP screen's own distance rule (vcp_cfg.max_dist_to_pivot) and
    minimum length; breakout stages judge the base as it was on the bar before the
    breakout. Returns (result, vcp dict or None).
    """
    from dataclasses import replace

    from backend.engine.vcp import evaluate

    cfg = replace(screen_cfg, max_dist_to_pivot=vcp_cfg.max_dist_to_pivot, min_forming_weeks=vcp_cfg.min_weeks)
    res = classify(candles, bases, cfg)
    if res.stage is None:
        return res, None
    if res.stage == "forming":
        base, end = bases[-1], len(candles) - 1
    else:
        base = next(b for b in reversed(bases) if b.breakout is not None)
        end = base.end_index
    v = evaluate(candles, base, rs, trend_passed, vcp_cfg, end=end)
    if not v.qualifies:
        res.stage, res.reason = None, "not a VCP: " + "; ".join(v.fails)
    return res, v.to_dict()
