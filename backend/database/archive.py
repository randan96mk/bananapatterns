"""🧹 Archive and vacuum: old rows of the fast-growing log tables MOVE to data/archive.db (next to the live DB);
nothing is ever deleted without its copy in the archive first. The one exception (the owner's rule): the Tips
screenshot files under data/tips_media are deleted after 2 days (backend/tips/vision.py sweep, daily and here); their
OCR text stays on the post forever.

`run(config, now)` ATTACHes the archive file, creates the same table shapes there (the live CREATE TABLE text) and,
per table in TABLES, moves the rows older than `maintenance.archive_days`:
  1. copy  (one transaction, batches of BATCH rows): INSERT OR IGNORE into the archive
  2. delete (one transaction, batches of BATCH rows): only live rows whose exact copy (key + time column) is now in
     the archive. Copy and delete are separate commits on purpose: SQLite does not make a commit across two ATTACHed
     files atomic in WAL mode, so a crash between them leaves at worst a row in both files (the next run finishes the
     move), never a row in neither.
Then `PRAGMA wal_checkpoint(TRUNCATE)` and, with `maintenance.vacuum`, VACUUM the live DB.

Kept in the live DB whatever their age: the latest job_runs row per job, ai_jobs still queued/running, sent_alerts not
sent yet (ok = 0, retried by the notifier) and the sent_alerts dedup keys that are not dated (e.g. near|... once per
base: archiving them could send the alert again). Read the archive back with:
    ATTACH 'data/archive.db' AS a;  SELECT * FROM a.options_chain_snap WHERE day = '2026-03-02';
"""

import json
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from backend.config import IST, WEEKDAYS, AppConfig, db_path
from backend.database.models import get_db, get_setting, now_ist, set_setting
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm

logger = setup_logger("archive")

JOB = "archive"
BATCH = 20000
LAST_KEY = "maintenance.last"           # app_settings: the last run's summary (GET /api/maintenance)

# table -> (time column, key columns that identify a row, SQL of rows kept whatever their age or None)
TABLES = {
    "options_chain_snap": ("day", ("id",), None),
    "tips_quotes": ("t", ("hint_id", "t"), None),
    "tips_ticks": ("at", ("hint_id", "at"), None),
    "tips_ai": ("at", ("id",), None),
    "market_reads": ("at", ("id",), None),
    "ai_jobs": ("created", ("id",), "status IN ('queued', 'running')"),
    "job_runs": ("run_date", ("id",), "id IN (SELECT MAX(id) FROM main.job_runs GROUP BY job)"),
    "sent_alerts": ("sent_at", ("key",), "ok = 0 OR key NOT GLOB '[0-9][0-9][0-9][0-9]*'"),
    "crypto_candles": ("t", ("symbol", "tf", "t"), None),
    "crypto_signals": ("t", ("id",), "state = 'confirm_wait'"),
}
# ₿ tables whose time column is unix seconds (an INTEGER always sorts before TEXT in SQLite: the cutoff must be a number)
UNIX_TIME = ("crypto_candles", "crypto_signals")

# a table kept longer than maintenance.archive_days in the live DB (days): the Tips card's tick store and the Tips AI
# reads (tips_ai), 180 days
TABLE_DAYS = {"tips_ticks": 180, "tips_ai": 180, "market_reads": 180}
# 📈 an archive copy made before a live CHECK widened (tips_ai kind 'market'): rebuilt in the archive with the live shape
# (renamed, created, copied, the old copy dropped only once every row is across) so the move never skips a row
WIDENED = {"tips_ai": "'market'"}

_lock = threading.Lock()


class Busy(RuntimeError):
    """An archive run is already going (POST /api/maintenance/archive answers 409)."""


def archive_path() -> Path:
    return db_path().parent / "archive.db"


def running() -> bool:
    return _lock.locked()


def _mb(p: Path) -> float:
    size = sum(f.stat().st_size for f in (p, p.with_name(p.name + "-wal")) if f.exists())
    return round(size / 1e6, 1)


def _cutoff(config: AppConfig, now: datetime, table: Optional[str] = None):
    if table in UNIX_TIME:                                 # ₿ crypto.archive_days (365), as unix seconds
        day = now.date() - timedelta(days=int(getattr(getattr(config, "crypto", None), "archive_days", 365)))
        return int(datetime(day.year, day.month, day.day, tzinfo=IST).timestamp())
    days = TABLE_DAYS.get(table or "", int(config.maintenance.archive_days))
    return (now.date() - timedelta(days=int(days))).isoformat()


def _ensure_table(conn, table: str) -> list[str]:
    """The archive copy of `table` (same CREATE TABLE text, plus an index on the time column); columns added to the
    live table since are added to the archive too. Returns the live column names."""
    sql = conn.execute("SELECT sql FROM main.sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    if not sql:
        return []
    create = re.sub(r"^\s*CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?[\"`\[]?\w+[\"`\]]?",
                    f"CREATE TABLE IF NOT EXISTS a.{table}", sql[0], count=1, flags=re.I)
    old = conn.execute("SELECT sql FROM a.sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    mark = WIDENED.get(table)
    if old and mark and mark in sql[0] and mark not in (old[0] or ""):
        cols = [r[1] for r in conn.execute(f"PRAGMA a.table_info({table})")]
        conn.execute(f"DROP INDEX IF EXISTS a.idx_arch_{table}_{TABLES[table][0]}")
        conn.execute(f"ALTER TABLE a.{table} RENAME TO {table}_pre_widen")
        conn.execute(create)
        names = ", ".join(f'"{c}"' for c in cols)
        conn.execute(f"INSERT INTO a.{table} ({names}) SELECT {names} FROM a.{table}_pre_widen")
        if conn.execute(f"SELECT COUNT(*) FROM a.{table}").fetchone()[0] >= conn.execute(f"SELECT COUNT(*) FROM a.{table}_pre_widen").fetchone()[0]:
            conn.execute(f"DROP TABLE a.{table}_pre_widen")
    conn.execute(create)
    col = TABLES[table][0]
    conn.execute(f"CREATE INDEX IF NOT EXISTS a.idx_arch_{table}_{col} ON {table}({col})")
    live = [r[1] for r in conn.execute(f"PRAGMA main.table_info({table})")]
    have = {r[1] for r in conn.execute(f"PRAGMA a.table_info({table})")}
    for r in conn.execute(f"PRAGMA main.table_info({table})").fetchall():
        if r[1] not in have:
            conn.execute(f"ALTER TABLE a.{table} ADD COLUMN \"{r[1]}\" {r[2] or ''}")
    return live


def _move(conn, table: str, cutoff: str) -> int:
    """Copy then delete the old rows of one table (see the module doc). Returns the rows removed from the live DB."""
    col, keys, keep = TABLES[table]
    cols = _ensure_table(conn, table)
    conn.commit()
    if not cols:
        return 0
    old = f"{col} < ?" + (f" AND NOT ({keep})" if keep else "")
    names = ", ".join(f'"{c}"' for c in cols)
    last = 0
    conn.execute("BEGIN IMMEDIATE")                      # 1. copy, one transaction, BATCH rows at a time
    try:
        while True:
            hi = conn.execute(f"SELECT MAX(r) FROM (SELECT rowid AS r FROM main.{table} WHERE {old} AND rowid > ? "
                              f"ORDER BY rowid LIMIT {BATCH})", (cutoff, last)).fetchone()[0]
            if hi is None:
                break
            conn.execute(f"INSERT OR IGNORE INTO a.{table} ({names}) SELECT {names} FROM main.{table} "
                         f"WHERE {old} AND rowid > ? AND rowid <= ?", (cutoff, last, hi))
            last = hi
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    same = " AND ".join(f"x.{k} IS m.{k}" for k in dict.fromkeys((*keys, col)))
    moved = 0
    conn.execute("BEGIN IMMEDIATE")                      # 2. delete only what the archive now holds, one transaction
    try:
        while True:
            n = conn.execute(f"DELETE FROM main.{table} WHERE rowid IN (SELECT m.rowid FROM main.{table} m WHERE m.{old} "
                             f"AND EXISTS (SELECT 1 FROM a.{table} x WHERE {same}) LIMIT {BATCH})", (cutoff,)).rowcount
            moved += n
            if n < BATCH:
                break
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return moved


def run(config: AppConfig, now: Optional[datetime] = None) -> dict:
    """Move the old rows, checkpoint, VACUUM; records a job_runs row and the summary. Raises Busy while one runs."""
    if not _lock.acquire(blocking=False):
        raise Busy("an archive run is already going")
    try:
        return _run(config, now or datetime.now(IST))
    finally:
        _lock.release()


def _run(config: AppConfig, now: datetime) -> dict:
    started, live, arch = now_ist(), db_path(), archive_path()
    cutoff = _cutoff(config, now)
    moved: dict[str, int] = {}
    conn = sqlite3.connect(str(live), timeout=30, isolation_level=None)   # explicit BEGIN / COMMIT below
    error, vacuumed = None, False
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("ATTACH DATABASE ? AS a", (str(arch),))
        for table in TABLES:
            try:
                if table == "crypto_signals":             # ₿ the snapshot PNGs of the rows about to move go with them
                    snaps = [r[0] for r in conn.execute("SELECT png_path FROM main.crypto_signals WHERE t < ? AND state != 'confirm_wait' "
                                                        "AND png_path IS NOT NULL", (_cutoff(config, now, table),))]
                moved[table] = _move(conn, table, _cutoff(config, now, table))
                if table == "crypto_signals" and moved[table]:
                    _move_snaps(snaps)
            except sqlite3.Error as exc:                  # one table failing leaves the others (and its rows) alone
                logger.exception("Archive of %s failed", table)
                error = f"{table}: {exc}"
                moved[table] = 0
        conn.execute("DETACH DATABASE a")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if config.maintenance.vacuum:
            try:
                conn.execute("VACUUM")
                vacuumed = True
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error as exc:
                logger.warning("VACUUM after the archive failed: %s", exc)
                error = error or f"vacuum: {exc}"
    finally:
        conn.close()
    total = sum(moved.values())
    try:                                                   # 📷 the one deletion of downloaded data: screenshots after 2 days
        from backend.tips import vision as vs
        media = vs.sweep(now)
    except Exception:
        logger.exception("Tips media sweep in the archive run failed")
        media = {"removed": 0}
    out = {"tips_media_removed": media.get("removed", 0), "at": now.isoformat(timespec="seconds"), "cutoff": cutoff, "moved_by_table": moved, "moved": total,
           "live_mb": _mb(live), "archive_mb": _mb(arch), "vacuumed": vacuumed,
           "archive_file": str(arch), "error": error}
    message = (f"moved {total} rows older than {cutoff} (" + ", ".join(f"{t} {n}" for t, n in moved.items())
               + f") · live {out['live_mb']} MB · archive {out['archive_mb']} MB" + (f" · {error}" if error else ""))
    with get_db() as c:
        c.execute("INSERT INTO job_runs (job, run_date, started_at, finished_at, status, message) VALUES (?, ?, ?, ?, ?, ?)",
                  (JOB, now.date().isoformat(), started, now_ist(), "partial" if error else "ok", message))
        set_setting(c, LAST_KEY, json.dumps(out))
    logger.info("Archive: %s", message)
    return out


def _move_snaps(paths: list) -> int:
    """₿ Move (never delete) archived detections' snapshot PNGs into data/crypto/snaps/archive/."""
    n = 0
    for p in paths:
        src = Path(p)
        if not src.is_file():
            continue
        dst = src.parent / "archive" / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dst)
        n += 1
    return n


# --- schedule ------------------------------------------------------------------------------------------------------------

def _at(config: AppConfig) -> tuple[int, int, int]:
    m = config.maintenance
    h, _, mi = str(m.time).partition(":")
    return WEEKDAYS.index(str(m.day).lower()), int(h), int(mi)


def due(now: datetime, config: AppConfig, done: set) -> list[str]:
    """["archive"] on `maintenance.day` from `maintenance.time` until midnight, once that day (pure)."""
    m = config.maintenance
    if not m.enabled or JOB in done:
        return []
    wd, h, mi = _at(config)
    return [JOB] if now.weekday() == wd and (now.hour, now.minute) >= (h, mi) else []


def next_at(config: AppConfig, now: datetime, last: Optional[dict] = None) -> Optional[str]:
    """The next scheduled run (ISO, IST): this week's slot when still ahead or due and not yet run today, else next week."""
    if not config.maintenance.enabled:
        return None
    wd, h, mi = _at(config)
    day = now.date() + timedelta(days=(wd - now.weekday()) % 7)
    slot = datetime(day.year, day.month, day.day, h, mi, tzinfo=IST)
    ran_today = bool(last and str(last.get("at", ""))[:10] == now.date().isoformat())
    if slot < now and (day != now.date() or ran_today):
        slot += timedelta(days=7)
    return slot.isoformat(timespec="seconds")


# --- reading -------------------------------------------------------------------------------------------------------------

def last_run() -> Optional[dict]:
    with get_db() as conn:
        raw = get_setting(conn, LAST_KEY)
    return json.loads(raw) if raw else None


def status(config: AppConfig, now: Optional[datetime] = None) -> dict:
    """GET /api/maintenance: {last_run, moved_by_table, live_mb, archive_mb, next_at, running, settings}."""
    from dataclasses import asdict
    now = now or datetime.now(IST)
    last = last_run()
    label = None
    if last:
        try:
            t = datetime.fromisoformat(last["at"])
            label = f"{t:%a} {ampm(t)}"
        except (KeyError, ValueError):
            pass
    return {"last_run": last, "last_label": label, "moved_by_table": (last or {}).get("moved_by_table", {}),
            "moved": (last or {}).get("moved", 0), "live_mb": _mb(db_path()), "archive_mb": _mb(archive_path()),
            "next_at": next_at(config, now, last), "running": running(), "archive_file": str(archive_path()),
            "settings": asdict(config.maintenance)}


def health_row(config: AppConfig, today: Optional[date] = None) -> dict:
    """🩺 archive: green when it ran in the last 10 days (or never yet / switched off), amber when the last run is older."""
    today = today or datetime.now(IST).date()
    last = last_run()
    if not config.maintenance.enabled:
        return {"key": "archive", "name": "Archive", "ok": True, "warn": False, "state": "off",
                "detail": "weekly move of old log rows to data/archive.db", "at": last.get("at") if last else None}
    if not last:
        return {"key": "archive", "name": "Archive", "ok": True, "warn": False, "state": "not run yet",
                "detail": f"weekly, {config.maintenance.day.title()} {ampm(config.maintenance.time)}", "at": None}
    age = (today - date.fromisoformat(last["at"][:10])).days
    return {"key": "archive", "name": "Archive", "ok": True, "warn": age > 10,
            "state": f"{age} d ago · {last.get('moved', 0)} rows moved" if age else f"today · {last.get('moved', 0)} rows moved",
            "detail": f"live {last.get('live_mb')} MB · archive {last.get('archive_mb')} MB", "at": last["at"]}
