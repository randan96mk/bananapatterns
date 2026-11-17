"""The job queue: ONE worker thread, FIFO. Every job is a row in `ai_jobs` (never deleted): queued -> running ->
done / failed, or skipped (the guard said no, or a full stop emptied the queue). `enqueue(config, kind, key, fn)`
dedupes on (kind, key) while a job is queued or running and refuses beyond `max_queue`. `fn()` does the work (one
or more client calls) and returns a dict whose `tokens` / `note` are recorded (or `{"skip": reason}` when the work
turned out not to be needed, e.g. a stale note: the job ends skipped); a success closes the breaker, any failure counts
toward opening it."""

import queue as _q
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from backend.ai import client, guard
from backend.config import IST, AppConfig
from backend.database.models import get_db, now_ist
from backend.logging_config.logger import setup_logger

logger = setup_logger("ai")

_items: "_q.Queue[tuple]" = _q.Queue()
_lock = threading.Lock()
_worker: Optional[threading.Thread] = None
_running: Optional[dict] = None                     # the job the worker is on right now (a row + "since")


class QueueFull(ValueError):
    """More than `max_queue` jobs are waiting."""


# --- rows ------------------------------------------------------------------------------------------------------------

def _row(job_id: int) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM ai_jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(r) if r else None


def _set(job_id: int, **cols) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)
    with get_db() as conn:
        conn.execute(f"UPDATE ai_jobs SET {sets} WHERE id = ?", (*cols.values(), job_id))


def _pending(kind: str, key: str) -> Optional[int]:
    with get_db() as conn:
        r = conn.execute("SELECT id FROM ai_jobs WHERE kind = ? AND key = ? AND status IN ('queued', 'running') "
                         "ORDER BY id LIMIT 1", (kind, key)).fetchone()
    return r[0] if r else None


def _count(status: str) -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM ai_jobs WHERE status = ?", (status,)).fetchone()[0]


# --- enqueue -----------------------------------------------------------------------------------------------------------

def enqueue(config: AppConfig, kind: str, key: str, fn: Callable[[], Optional[dict]], model: Optional[str] = None,
            note: Optional[str] = None) -> dict:
    """Queue one job; {id, dedup}. The same (kind, key) already queued or running -> that job's id, dedup True."""
    with _lock:
        existing = _pending(kind, key)
        if existing:
            return {"id": existing, "dedup": True}
        if _count("queued") >= int(config.ai.max_queue):
            raise QueueFull(f"queue full ({config.ai.max_queue} waiting)")
        with get_db() as conn:
            cur = conn.execute("INSERT INTO ai_jobs (created, kind, key, model, status, note) VALUES (?, ?, ?, ?, 'queued', ?)",
                               (now_ist(), kind, key, model, note))
            job_id = cur.lastrowid
    _items.put((job_id, config, fn))
    _ensure_worker()
    return {"id": job_id, "dedup": False}


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_loop, name="ai-worker", daemon=True)
            _worker.start()


def _loop() -> None:
    while True:
        job_id, config, fn = _items.get()
        try:
            _run(job_id, config, fn)
        except Exception:                                   # the worker never dies on a job
            logger.exception("AI job %s crashed the worker loop", job_id)
        finally:
            _items.task_done()


_timers: list = []
RETRY_KINDS = ("night", "brief")                 # batch work waits for its moment; an ask or a test answers at once
RETRY_S = 60.0                                      # a transient guard refusal is looked at again after this
STALE_H = 24                                        # a job queued longer than this is skipped as stale
PERMANENT = ("AI switched off", "full stop", "night batch off", "day answers off", "ask off", "morning brief off", "model", "models", "service unreachable")


def transient(why: str) -> bool:
    """A guard refusal that can clear on its own (busy app, low RAM, breaker, a window not open yet)."""
    return not any(why.startswith(p) or p in why for p in PERMANENT)


def stale(row: dict, now: datetime) -> bool:
    try:
        created = datetime.fromisoformat(row["created"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=IST)
    except Exception:
        return False
    return (now - created).total_seconds() > STALE_H * 3600


# --- one job -------------------------------------------------------------------------------------------------------------

def _run(job_id: int, config: AppConfig, fn: Callable[[], Optional[dict]]) -> None:
    """Guard, run, record: skipped with the guard's reason, else done / failed with ms and tokens."""
    global _running
    row = _row(job_id)
    if not row or row["status"] != "queued":                 # a full stop marked it skipped meanwhile
        return
    now = datetime.now(IST)
    ok, why = guard.allowed(row["kind"], now, config)
    if not ok:
        if row["kind"] in RETRY_KINDS and transient(why) and not stale(row, now):
            # the server is busy, the breaker is open or the window is shut for now: keep the job queued and look
            # again in RETRY_S (the evening job's long tick, a RAM dip or an early enqueue must not cost the night)
            _set(job_id, note=f"waiting · {why}"[:300])
            t = threading.Timer(RETRY_S, lambda: _items.put((job_id, config, fn)))
            t.daemon = True
            _timers.append(t)
            t.start()
            return
        _set(job_id, status="skipped", finished=now_ist(), error=("stale · " + why) if stale(row, now) else why)
        return
    _set(job_id, status="running", started=now_ist())
    _running = {**row, "status": "running", "since": time.monotonic()}
    t0 = time.monotonic()
    try:
        res = fn() or {}
    except Exception as exc:
        ms = round((time.monotonic() - t0) * 1000)
        kind = "AI" if isinstance(exc, client.AIError) else exc.__class__.__name__
        _set(job_id, status="failed", finished=now_ist(), ms=ms, error=f"{kind}: {str(exc)[:300]}")
        guard.fail(config, now)
        logger.warning("AI job %s (%s %s) failed after %d ms: %s", job_id, row["kind"], row["key"], ms, exc)
    else:
        ms = round((time.monotonic() - t0) * 1000)
        if res.get("skip"):                                  # the work decided it was not needed (no breaker count)
            _set(job_id, status="skipped", finished=now_ist(), ms=ms, error=str(res["skip"])[:300])
        else:
            _set(job_id, status="done", finished=now_ist(), ms=ms, tokens=res.get("tokens"), note=res.get("note") or row.get("note"))
            guard.ok()
    finally:
        _running = None


def wait(job_id: int, timeout_s: float = 120) -> Optional[dict]:
    """Block until the job has ended (done / failed / skipped) or `timeout_s` passed; the row either way."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        row = _row(job_id)
        if not row or row["status"] not in ("queued", "running"):
            return row
        time.sleep(0.05)
    return _row(job_id)


# --- the full stop and the views ----------------------------------------------------------------------------------------

def clear(reason: str = "full stop") -> int:
    """Mark every queued job skipped with `reason` and drop them from the worker's line; how many."""
    for t in _timers:
        t.cancel()
    _timers.clear()
    with _lock:
        with get_db() as conn:
            n = conn.execute("UPDATE ai_jobs SET status = 'skipped', finished = ?, error = ? WHERE status = 'queued'",
                             (now_ist(), reason)).rowcount
        while True:                                           # the worker re-reads the row and skips what is left
            try:
                _items.get_nowait()
                _items.task_done()
            except _q.Empty:
                break
    return n


def status(today: Optional[str] = None) -> dict:
    """{queued, running, done_today, failed_today, job}: the counts and the job running right now."""
    today = today or now_ist()[:10]
    with get_db() as conn:
        one = lambda q, *a: conn.execute(q, a).fetchone()[0]
        out = {"queued": one("SELECT COUNT(*) FROM ai_jobs WHERE status = 'queued'"),
               "running": one("SELECT COUNT(*) FROM ai_jobs WHERE status = 'running'"),
               "done_today": one("SELECT COUNT(*) FROM ai_jobs WHERE status = 'done' AND finished LIKE ?", today + "%"),
               "failed_today": one("SELECT COUNT(*) FROM ai_jobs WHERE status = 'failed' AND finished LIKE ?", today + "%")}
    r = _running
    out["job"] = {k: r[k] for k in ("id", "kind", "key", "model", "started")} | {"s": round(time.monotonic() - r["since"])} if r else None
    return out


def progress(text: str) -> None:
    """The running job's own progress line ("12 of 32 rated · ~9 min left"), written to its row's `note` so the page
    and `status()` show it; a no-op outside a job."""
    r = _running
    if r:
        try:
            _set(r["id"], note=str(text)[:300])
        except Exception:
            pass


def recent(n: int = 20) -> list[dict]:
    """The latest rows, newest first; a queued row carries `ahead` (how many queued rows are before it), a running one
    `elapsed_s`."""
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM ai_jobs ORDER BY id DESC LIMIT ?", (max(1, min(int(n), 500)),))]
        queued_ids = [r[0] for r in conn.execute("SELECT id FROM ai_jobs WHERE status = 'queued' ORDER BY id")]
    r = _running
    for row in rows:
        if row["status"] == "queued" and row["id"] in queued_ids:
            row["ahead"] = queued_ids.index(row["id"]) + (1 if r else 0)
        if r and row["id"] == r["id"]:
            row["elapsed_s"] = round(time.monotonic() - r["since"])
    return rows
