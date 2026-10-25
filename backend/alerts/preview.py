"""The decision chart for one stock, as an alert would carry it (dashboard preview, bot Chart button)."""

import json
from typing import Optional

from backend.alerts.messages import SCREEN_ORDER_SQL
from backend.config import AppConfig
from backend.database.models import get_candles, get_db, latest_universe

STAGE_KIND = {"fresh": "breakout", "fresh_low_volume": "breakout_low_volume", "climbing": "climbing",
              "below_pivot": "below_pivot", "played_out": "played_out", "forming": "forming"}
ALERT_CHART_KINDS = ("auto", "breakout", "breakout_low_volume", "near_pivot", "forming", "below_pivot",
                     "played_out", "climbing")


def resolve_stock(conn, symbol: str) -> tuple[dict, dict]:
    """(instrument row, universe metadata) for an NSE symbol (BE series -> SYMBOL-BE) or index name."""
    _, members = latest_universe(conn)
    meta = next((m for m in members if m["symbol"] == symbol), {})
    series = (meta.get("series") or "EQ").upper()
    candidates = [symbol] if series == "EQ" else [f"{symbol}-{series}", symbol]
    for cand in candidates:
        row = conn.execute(
            "SELECT instrument_token, segment FROM instruments WHERE tradingsymbol = ? "
            "AND segment IN ('NSE', 'INDICES') ORDER BY segment = 'NSE' DESC LIMIT 1", (cand,)
        ).fetchone()
        if row:
            return dict(row), meta
    raise LookupError(f"Unknown symbol {symbol}")


def alert_chart(cfg: AppConfig, symbol: str, kind: str = "auto") -> tuple[Optional[bytes], str]:
    """(PNG or None when there is no base to annotate, the kind used). Raises LookupError / ValueError."""
    from backend.alerts.dispatch import decision_for
    if kind not in ALERT_CHART_KINDS:
        raise ValueError(f"kind must be one of {list(ALERT_CHART_KINDS)}")
    with get_db() as conn:
        row, meta = resolve_stock(conn, symbol)
        candles = get_candles(conn, row["instrument_token"])
        scan = conn.execute("SELECT r.stage, r.payload FROM scan_results r WHERE r.symbol = ? AND r.as_of = "
                            "(SELECT MAX(as_of) FROM scan_runs) "
                            "ORDER BY " + SCREEN_ORDER_SQL.format(col="r.screen") + " LIMIT 1",
                            (symbol,)).fetchone()
        m = conn.execute("SELECT rs, tt_passed FROM stock_metrics WHERE symbol = ? ORDER BY as_of DESC LIMIT 1",
                         (symbol,)).fetchone()
    if not candles:
        raise LookupError(f"No candles stored for {symbol}")
    p = {"company": meta.get("company"), "industry": meta.get("industry"),
         "rs": m["rs"] if m else None, "tt_passed": m["tt_passed"] if m else 0}
    if scan:
        p.update(json.loads(scan["payload"]))
    p["symbol"] = symbol
    if kind == "auto":
        kind = STAGE_KIND.get(scan["stage"] if scan else "", "forming")
        if kind == "forming" and (p.get("dist_to_pivot_pct") or 99) <= cfg.alerts.near_pivot_pct * 100:
            kind = "near_pivot"
    png, _ = decision_for(candles, p, kind, cfg.alerts, cfg.bases, cfg.vcp, force_image=True)
    return png, kind
