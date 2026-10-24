"""Universe scan: stage of every stock today and yesterday, plus the what-changed feed.

"Yesterday" is the stage computed on the same candles minus the last bar, so
the feed never depends on an earlier scan having run. Results are stored per
as-of date in scan_results / scan_changes (history for Phase 4).
"""

import json
from typing import Optional

from backend.config import BaseConfig, ScreenConfig
from backend.database.models import get_candles, get_db, now_ist
from backend.engine.bases import detect_bases
from backend.engine.stages import CARD_STAGES, StageResult, classify
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


def run_scan(bases_cfg: BaseConfig, screen_cfg: ScreenConfig, symbols: list[dict]) -> dict:
    """symbols: [{symbol, instrument_token, company?, industry?}], e.g. resolve_universe(...)["stocks"]."""
    with get_db() as conn:
        series = {s["symbol"]: get_candles(conn, s["instrument_token"]) for s in symbols}
    as_of = max((c[-1]["date"] for c in series.values() if c), default=None)
    if as_of is None:
        return {"as_of": None, "counts": {}, "changes": 0, "scanned": 0}

    rows, changes, scanned, stale = [], [], 0, 0
    for s in symbols:
        candles = series[s["symbol"]]
        if len(candles) < 3 or candles[-1]["date"] != as_of:
            stale += 1
            continue
        scanned += 1
        today = classify(candles, detect_bases(candles, bases_cfg), screen_cfg)
        prev = candles[:-1]
        yday = classify(prev, detect_bases(prev, bases_cfg), screen_cfg)
        rows.append((s, today))
        line = change_line(s["symbol"], yday, today)
        if line:
            changes.append((s, line[0], line[1], today))

    counts = {k: 0 for k in (*CARD_STAGES, "fresh_low_volume")}
    for _, r in rows:
        if r.stage in counts:
            counts[r.stage] += 1
    with get_db() as conn:
        conn.execute("DELETE FROM scan_results WHERE as_of = ?", (as_of,))
        conn.execute("DELETE FROM scan_changes WHERE as_of = ?", (as_of,))
        conn.executemany(
            "INSERT INTO scan_results (as_of, symbol, stage, payload) VALUES (?, ?, ?, ?)",
            [(as_of, s["symbol"], r.stage, json.dumps({**r.to_dict(), "symbol": s["symbol"],
                                                        "company": s.get("company"), "industry": s.get("industry")}))
             for s, r in rows if r.stage is not None],
        )
        conn.executemany(
            "INSERT INTO scan_changes (as_of, symbol, kind, text, change_pct, close) VALUES (?, ?, ?, ?, ?, ?)",
            [(as_of, s["symbol"], kind, text, r.change_pct, r.close) for s, kind, text, r in changes],
        )
        conn.execute(
            "INSERT INTO scan_runs (as_of, ran_at, scanned, stale, counts, config) VALUES (?, ?, ?, ?, ?, ?)",
            (as_of, now_ist(), scanned, stale, json.dumps(counts),
             json.dumps({"bases": bases_cfg.__dict__, "screen": screen_cfg.__dict__})),
        )
    logger.info("Scan %s: %d scanned (%d stale), %s, %d changes", as_of, scanned, stale, counts, len(changes))
    return {"as_of": as_of, "counts": counts, "changes": len(changes), "scanned": scanned, "stale": stale}
