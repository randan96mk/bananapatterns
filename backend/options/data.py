"""The options data layer's one step and the Data page's summary: refresh the NIFTY weekly contracts from the NFO dump
(once a day; the book's first tick does it too) and describe what is stored (contracts, expiries, the last chain
snapshot). Nothing here deletes anything."""

from datetime import date
from typing import Optional

from backend.options import chain, instruments
from backend.logging_config.logger import setup_logger

logger = setup_logger("options")


def run(client, today: Optional[date] = None, force: bool = False) -> dict:
    """{status: ok|failed, contracts, error}."""
    try:
        return {"status": "ok", "contracts": instruments.refresh(client, today, force=force), "error": None}
    except Exception as exc:
        logger.exception("Options contracts refresh failed")
        return {"status": "failed", "contracts": None, "error": str(exc)[:200]}


def summary() -> dict:
    """{contracts: {as_of, contracts, expiries, future, lot_size}, snapshot: {ts, day, kind, expiry, rows}}."""
    snap = chain.last_snapshot()
    return {"contracts": instruments.status(),
            "snapshot": {"ts": snap["ts"], "day": snap["day"], "kind": snap["kind"], "expiry": snap["expiry"], "rows": len(snap["rows"])} if snap else None}
