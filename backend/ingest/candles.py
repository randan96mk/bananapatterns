"""Daily candle backfill and incremental sync.

One code path handles both: a symbol with no bars gets a full backfill
(`history_years` deep); a symbol with bars gets an incremental fetch that
re-reads the last `overlap_bars` bars. If those overlapping closes differ from
what is stored by more than `adjust_tolerance`, Kite has re-adjusted history
(split/bonus/etc.), so the symbol is wiped and refetched in full. This keeps
the store corporate-action adjusted without a separate corporate-actions feed.

Runs are resumable: each symbol commits on its own, and a re-run skips work
already done. Auth/permission errors abort the run; other per-symbol errors
are recorded in ingest_status and the run continues.
"""

import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional

from backend.config import IST, IngestConfig
from backend.database.models import (candle_stats, delete_candles, get_candles, get_db, start_run,
                                     update_ingest_status, update_run, upsert_candles, now_ist)
from backend.ingest.universe import resolve_universe
from backend.kite_api.client import KiteAuthError, KiteError, KitePermissionError
from backend.logging_config.logger import setup_logger

logger = setup_logger("candles")


def last_complete_session_date(cfg: IngestConfig, now: Optional[datetime] = None) -> date:
    """Latest date whose daily bar is final: today after eod_ready_time, else yesterday.

    Weekends/holidays need no special handling here: Kite simply returns no bar.
    """
    now = (now or datetime.now(IST)).astimezone(IST)
    hh, mm = (int(x) for x in cfg.eod_ready_time.split(":"))
    return now.date() if now.time() >= time(hh, mm) else now.date() - timedelta(days=1)


def _closes_differ(a: float, b: float, tol: float) -> bool:
    return abs(a - b) > tol * max(abs(b), 1e-9)


def _guard_not_empty(candles: list, stats: dict, symbol: str) -> None:
    """Never replace stored history with an empty response."""
    if not candles and stats["bars"]:
        raise RuntimeError(f"{symbol}: Kite returned no bars for a full refetch; kept {stats['bars']} stored bars")


def sync_symbol(client, cfg: IngestConfig, token: int, symbol: str, end: date,
                force_full: bool = False) -> dict:
    """Bring one instrument up to `end`. Returns {outcome, added, bars}.

    outcome: 'full' | 'refetched' | 'incremental' | 'uptodate' | 'empty'
    """
    with get_db() as conn:
        stats = candle_stats(conn, token)
        overlap = [] if force_full or not stats["bars"] else get_candles(conn, token, limit=cfg.overlap_bars)

    full_start = end - timedelta(days=int(cfg.history_years * 365.25))

    if not overlap:
        candles = client.historical_daily(token, full_start, end, label=symbol)
        _guard_not_empty(candles, stats, symbol)
        with get_db() as conn:
            delete_candles(conn, token)
            upsert_candles(conn, token, candles)
            update_ingest_status(conn, token, symbol, full=True)
        return {"outcome": "full" if candles else "empty", "added": len(candles), "bars": len(candles)}

    since = date.fromisoformat(overlap[0]["date"])
    if since > end:
        return {"outcome": "uptodate", "added": 0, "bars": stats["bars"]}
    fresh = client.historical_daily(token, since, end, label=symbol)
    fresh_by_date = {c["date"]: c for c in fresh}
    mismatch = [o["date"] for o in overlap
                if o["date"] in fresh_by_date
                and _closes_differ(fresh_by_date[o["date"]]["close"], o["close"], cfg.adjust_tolerance)]

    if mismatch:
        logger.warning("%s: stored closes differ from Kite on %s (corporate action?); full refetch",
                       symbol, mismatch)
        candles = client.historical_daily(token, full_start, end, label=symbol)
        _guard_not_empty(candles, stats, symbol)
        with get_db() as conn:
            delete_candles(conn, token)
            upsert_candles(conn, token, candles)
            update_ingest_status(conn, token, symbol, full=True)
        return {"outcome": "refetched", "added": len(candles), "bars": len(candles)}

    last_stored = overlap[-1]["date"]
    new = [c for c in fresh if c["date"] > last_stored]
    with get_db() as conn:
        upsert_candles(conn, token, fresh)  # also refreshes overlap bars (e.g. late volume corrections)
        update_ingest_status(conn, token, symbol, full=False)
        bars = candle_stats(conn, token)["bars"]
    return {"outcome": "incremental" if new else "uptodate", "added": len(new), "bars": bars}


@dataclass
class JobState:
    """Progress of the current/last sync run, shared with the API thread."""
    running: bool = False
    run_id: Optional[int] = None
    kind: str = ""
    total: int = 0
    done: int = 0
    failed: int = 0
    refetched: int = 0
    current: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    status: str = "idle"
    message: str = ""
    errors: list = field(default_factory=list)
    outcomes: dict = field(default_factory=dict)
    cancel: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict:
        with self.lock:
            return {k: v for k, v in self.__dict__.items() if k not in ("lock", "cancel")} | \
                {"errors": list(self.errors[-25:]), "outcomes": dict(self.outcomes)}


def run_sync(client, cfg: IngestConfig, state: JobState, force_full: bool = False,
             symbols: Optional[list[str]] = None, now: Optional[datetime] = None) -> dict:
    """Sync all universe stocks + index instruments. Blocking; call from a worker thread."""
    resolved = resolve_universe(cfg)
    targets = [(i["instrument_token"], i["symbol"]) for i in resolved["indices"]] + \
              [(s["instrument_token"], s["symbol"]) for s in resolved["stocks"]]
    if symbols:
        wanted = {s.upper() for s in symbols}
        targets = [t for t in targets if t[1].upper() in wanted]
    end = last_complete_session_date(cfg, now)
    kind = "full" if force_full else "sync"

    with get_db() as conn:
        run_id = start_run(conn, kind, len(targets))
    with state.lock:
        state.__dict__.update(running=True, run_id=run_id, kind=kind, total=len(targets), done=0, failed=0,
                              refetched=0, current="", started_at=now_ist(), finished_at=None,
                              status="running", message=f"Syncing to {end}", errors=[], outcomes={},
                              cancel=False)
    logger.info("Run %d (%s): %d instruments, end date %s", run_id, kind, len(targets), end)

    status, message = "ok", ""
    try:
        for token, symbol in targets:
            if state.cancel:
                status, message = "aborted", "Cancelled by user"
                break
            with state.lock:
                state.current = symbol
            try:
                res = sync_symbol(client, cfg, token, symbol, end, force_full=force_full)
                with state.lock:
                    state.outcomes[res["outcome"]] = state.outcomes.get(res["outcome"], 0) + 1
                    if res["outcome"] == "refetched":
                        state.refetched += 1
                if 0 < res["bars"] < cfg.min_bars_warn:
                    logger.info("%s has only %d bars (young listing)", symbol, res["bars"])
            except (KiteAuthError, KitePermissionError):
                raise
            except Exception as exc:  # per-symbol failure: record loudly, continue
                logger.error("%s: sync failed: %s", symbol, exc)
                with get_db() as conn:
                    update_ingest_status(conn, token, symbol, error=str(exc)[:500])
                with state.lock:
                    state.failed += 1
                    state.errors.append({"symbol": symbol, "error": str(exc)[:300]})
            with state.lock:
                state.done += 1
            with get_db() as conn:
                update_run(conn, run_id, done=state.done, failed=state.failed, refetched=state.refetched)
    except KiteError as exc:
        status, message = "aborted", str(exc)
        logger.error("Run %d aborted: %s", run_id, exc)
    except Exception as exc:
        status, message = "failed", f"{type(exc).__name__}: {exc}"
        logger.exception("Run %d crashed", run_id)

    if status == "ok" and state.failed:
        status = "partial"
    if not message:
        message = f"{state.done - state.failed}/{state.total} ok, {state.failed} failed, " \
                  f"{state.refetched} refetched; data through {end}"
    finished = now_ist()
    with get_db() as conn:
        update_run(conn, run_id, status=status, finished_at=finished, message=message,
                   done=state.done, failed=state.failed, refetched=state.refetched)
    with state.lock:
        state.__dict__.update(running=False, status=status, message=message, finished_at=finished, current="")
    logger.info("Run %d finished: %s — %s", run_id, status, message)
    return state.snapshot()
