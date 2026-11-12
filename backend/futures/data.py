"""The evening job's "futures data" step and the Data page's summary: refresh the NFO contracts, sync the continuous
futures candles for the eligible universe (lot value <= futures.universe_lot_cap), fetch the ban list. Each part
tolerates the others' failure; the result is one dict for the job record and the API."""

from datetime import date
from typing import Optional

from backend.config import AppConfig
from backend.database.models import get_db
from backend.futures import ban, candles, instruments
from backend.logging_config.logger import setup_logger

logger = setup_logger("futures")


def run(client, config: AppConfig, today: Optional[date] = None, force: bool = False) -> dict:
    """Returns {status: ok|partial|failed, contracts, candles, ban, errors: [...]}."""
    cfg, errors, out = config.futures, [], {"status": "ok", "contracts": None, "candles": None, "ban": None}
    try:
        out["contracts"] = instruments.refresh(client, today, force=force)
    except Exception as exc:
        logger.exception("Futures contracts refresh failed")
        errors.append(f"contracts: {exc}")
    try:
        names = [u["symbol"] for u in instruments.universe(cfg.universe_lot_cap)]
        out["candles"] = candles.sync(client, names, years=cfg.history_years, end=today)
        if out["candles"]["failed"]:
            errors.append(f"candles: {len(out['candles']['failed'])} failed")
    except Exception as exc:
        logger.exception("Futures candles sync failed")
        errors.append(f"candles: {exc}")
    out["ban"] = ban.refresh(today=today.isoformat() if today else None)
    if not out["ban"]["ok"]:
        errors.append(f"ban list: {out['ban']['error']}")
    out["errors"] = errors
    if errors:
        out["status"] = "failed" if out["contracts"] is None and out["candles"] is None else "partial"
    return out


def summary(config: AppConfig) -> dict:
    """GET /api/futures/data: {contracts, as_of, candles: {underlyings, to}, ban: {date, symbols, stale_days},
    universe: [{symbol, lot_size, lot_value, expiry, oi_rank}]}."""
    with get_db() as conn:
        as_of = instruments.latest_as_of(conn)
        n = conn.execute("SELECT COUNT(*) FROM futures_contracts WHERE as_of = ?", (as_of,)).fetchone()[0] if as_of else 0
    c = candles.status()
    uni = [{"symbol": u["symbol"], "lot_size": u["lot_size"], "lot_value": u["lot_value"], "expiry": u["expiry"],
            "oi_rank": u["oi_rank"]} for u in instruments.universe(config.futures.universe_lot_cap)]
    return {"contracts": n, "as_of": as_of, "candles": {"underlyings": c["underlyings"], "to": c["to"], "bars": c["bars"]},
            "ban": ban.summary(), "universe": uni, "universe_lot_cap": config.futures.universe_lot_cap}
