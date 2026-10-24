"""Universe scan: stage of every stock today and yesterday, plus the what-changed feed.

"Yesterday" is the stage computed on the same candles minus the last bar, so
the feed never depends on an earlier scan having run. Results are stored per
as-of date in scan_results / scan_changes (history for Phase 4).
"""

import json
from typing import Optional

from backend.config import BaseConfig, ScreenConfig, TrendConfig, VcpConfig
from backend.database.models import get_candles, get_db, now_ist
from backend.engine.bases import detect_bases
from backend.engine.rs import industry_ranks, rs_ratings, rs_score
from backend.engine.scoring import vcp_score
from backend.engine.stages import CARD_STAGES, StageResult, classify, classify_vcp
from backend.engine.trend_template import trend_template
from backend.logging_config.logger import setup_logger

logger = setup_logger("scan")

LISTED = set(CARD_STAGES) | {"fresh_low_volume"}


def _rs(v) -> str:
    return f"₹{v:,.2f}"


def change_line(symbol: str, y: Optional[StageResult], t: StageResult) -> Optional[tuple[str, str]]:
    """(kind, sentence) for a stage move worth reporting, else None."""
    ys = y.stage if y else None
    ts = t.stage
    if ts == ys:
        return None
    if ts in ("fresh", "fresh_low_volume") and ys not in ("fresh", "fresh_low_volume"):
        vol = f"{t.volume_ratio:.1f}x volume" if t.volume_ratio is not None else "volume n/a"
        low = " (below the volume rule)" if ts == "fresh_low_volume" else ""
        return ("breakout", f"{symbol} broke out of {t.base_label}: closed {_rs(t.close)}, "
                            f"{t.gain_from_pivot_pct:+.1f}% over the {_rs(t.pivot)} pivot on {vol}{low}.")
    if ts == "played_out":
        return ("played_out", f"{symbol} played out. Closed {_rs(t.close)}, {t.ended_reason}, "
                              f"{t.gain_from_pivot_pct:+.1f}% from the {_rs(t.pivot)} pivot "
                              f"(peak {t.peak_gain_pct:+.1f}%).")
    if ts == "below_pivot" and ys in ("fresh", "climbing", "fresh_low_volume"):
        return ("below_pivot", f"{symbol} closed {_rs(t.close)}, back below the {_rs(t.pivot)} pivot it broke out of.")
    if ts == "forming":
        return ("forming", f"{symbol} is newly forming {t.base_label}: {t.dist_to_pivot_pct:.1f}% under the "
                           f"{_rs(t.pivot)} pivot.")
    if ts == "climbing" and ys == "below_pivot":
        return ("climbing", f"{symbol} reclaimed the {_rs(t.pivot)} pivot; breakout holding again.")
    if ys == "forming" and ts is None:
        return ("left", f"{symbol} left Forming: {t.reason}.")
    if ys in ("fresh", "climbing") and ts is None:
        return ("left", f"{symbol} left {'Fresh breakouts' if ys == 'fresh' else 'Climbing'}: {t.reason}.")
    return None


SCREENS = ("bases", "vcp")


def run_scan(bases_cfg: BaseConfig, screen_cfg: ScreenConfig, symbols: list[dict],
             trend_cfg: Optional[TrendConfig] = None, vcp_cfg: Optional[VcpConfig] = None) -> dict:
    """symbols: [{symbol, instrument_token, company?, industry?}], e.g. resolve_universe(...)["stocks"].

    Produces both screens: "bases" (every base) and "vcp" (bases that pass the VCP rules).
    RS and the Trend Template use today's values for "yesterday" too, so the feed only
    reports moves caused by price and volume.
    """
    trend_cfg = trend_cfg or TrendConfig()
    vcp_cfg = vcp_cfg or VcpConfig()
    with get_db() as conn:
        series = {s["symbol"]: get_candles(conn, s["instrument_token"]) for s in symbols}
    as_of = max((c[-1]["date"] for c in series.values() if c), default=None)
    if as_of is None:
        return {"as_of": None, "counts": {}, "changes": {}, "scanned": 0}

    current = [s for s in symbols if len(series[s["symbol"]]) >= 3 and series[s["symbol"]][-1]["date"] == as_of]
    stale = len(symbols) - len(current)
    scores = {s["symbol"]: rs_score([c["close"] for c in series[s["symbol"]]], trend_cfg) for s in current}
    ratings = rs_ratings(scores)
    ind_of = {s["symbol"]: s.get("industry") for s in current}
    ind_rank = industry_ranks(ratings, ind_of)

    rows = {k: [] for k in SCREENS}            # screen -> [(symbol info, StageResult, extra)]
    changes = {k: [] for k in SCREENS}         # screen -> [(symbol info, kind, text, StageResult)]
    metrics = []
    for s in current:
        candles = series[s["symbol"]]
        sym = s["symbol"]
        tt = trend_template(candles, ratings[sym], trend_cfg)
        ir = ind_rank.get(ind_of[sym] or "", {})
        extra = {"rs": ratings[sym], "rs_score": scores[sym], "industry_rank": ir.get("rank"),
                 "industry_of": ir.get("of"), "tt_passed": tt["passed"], "tt_of": tt["of"],
                 "tt_all": tt["passed_all"], "pct_from_high52": tt["pct_from_high52"]}
        metrics.append((sym, extra, tt))
        prev = candles[:-1]
        bases_today, bases_yday = detect_bases(candles, bases_cfg), detect_bases(prev, bases_cfg)

        today = classify(candles, bases_today, screen_cfg)
        yday = classify(prev, bases_yday, screen_cfg)
        rows["bases"].append((s, today, extra))
        line = change_line(sym, yday, today)
        if line:
            changes["bases"].append((s, line[0], line[1], today))

        vt, vcp = classify_vcp(candles, bases_today, ratings[sym], tt["passed"], screen_cfg, vcp_cfg)
        vy, _ = classify_vcp(prev, bases_yday, ratings[sym], tt["passed"], screen_cfg, vcp_cfg)
        vextra = dict(extra)
        if vcp is not None:
            vextra["vcp"] = vcp
            vextra["score"] = vcp_score(vcp_cfg.score_weights, ratings[sym], vcp["tightening"], vcp["dryup"],
                                        vt.dist_to_pivot_pct, tt["pct_from_high52"], ir.get("rank"), ir.get("of"))
            extra["vcp_tag"] = vt.stage is not None   # shown as a "VCP" tag on the Bases screen
        rows["vcp"].append((s, vt, vextra))
        line = change_line(sym, vy, vt)
        if line:
            changes["vcp"].append((s, line[0], line[1], vt))

    counts = {k: {st: 0 for st in (*CARD_STAGES, "fresh_low_volume")} for k in SCREENS}
    for k in SCREENS:
        for _, r, _ in rows[k]:
            if r.stage in counts[k]:
                counts[k][r.stage] += 1
    with get_db() as conn:
        conn.execute("DELETE FROM scan_results WHERE as_of = ?", (as_of,))
        conn.execute("DELETE FROM scan_changes WHERE as_of = ?", (as_of,))
        for k in SCREENS:
            conn.executemany(
                "INSERT INTO scan_results (as_of, screen, symbol, stage, payload) VALUES (?, ?, ?, ?, ?)",
                [(as_of, k, s["symbol"], r.stage, json.dumps({**r.to_dict(), **x, "symbol": s["symbol"],
                                                              "company": s.get("company"),
                                                              "industry": s.get("industry")}))
                 for s, r, x in rows[k] if r.stage is not None],
            )
            conn.executemany(
                "INSERT INTO scan_changes (as_of, screen, symbol, kind, text, change_pct, close) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(as_of, k, s["symbol"], kind, text, r.change_pct, r.close) for s, kind, text, r in changes[k]],
            )
        conn.execute("DELETE FROM stock_metrics WHERE as_of = ?", (as_of,))
        conn.executemany(
            "INSERT INTO stock_metrics (as_of, symbol, rs, rs_score, industry, industry_rank, industry_of, "
            "tt_passed, tt_of, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(as_of, sym, x["rs"], x["rs_score"], ind_of[sym], x["industry_rank"], x["industry_of"],
              x["tt_passed"], x["tt_of"], json.dumps({**x, "trend": tt})) for sym, x, tt in metrics],
        )
        conn.execute(
            "INSERT INTO scan_runs (as_of, ran_at, scanned, stale, counts, config) VALUES (?, ?, ?, ?, ?, ?)",
            (as_of, now_ist(), len(current), stale, json.dumps(counts),
             json.dumps({"bases": bases_cfg.__dict__, "screen": screen_cfg.__dict__, "trend": trend_cfg.__dict__,
                         "vcp": vcp_cfg.__dict__})),
        )
    n_changes = {k: len(v) for k, v in changes.items()}
    logger.info("Scan %s: %d scanned (%d stale), %s, changes %s", as_of, len(current), stale, counts, n_changes)
    return {"as_of": as_of, "counts": counts, "changes": n_changes, "scanned": len(current), "stale": stale}
