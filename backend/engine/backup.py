"""💾 Nightly copy of the SQLite database (sqlite's own backup API, safe while the app runs) into data/backups,
the last KEEP kept. Run after the evening job and from the Data page."""

import sqlite3
from datetime import datetime

from backend.config import IST, db_path
from backend.logging_config.logger import setup_logger

logger = setup_logger("backup")
KEEP = 7


def folder():
    f = db_path().parent / "backups"
    f.mkdir(parents=True, exist_ok=True)
    return f


def run() -> dict:
    """One backup named by the day (a second run the same day replaces it); older ones beyond KEEP are deleted."""
    name = f"bananapatterns-{datetime.now(IST).strftime('%Y-%m-%d')}.db"
    dest = folder() / name
    src = sqlite3.connect(str(db_path()))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    files = sorted(folder().glob("bananapatterns-*.db"))
    for old in files[:-KEEP]:
        old.unlink()
    logger.info("Backup written: %s (%d kept)", name, min(len(files), KEEP))
    return {"name": name, "bytes": dest.stat().st_size, "kept": min(len(files), KEEP)}


def listing() -> list[dict]:
    return [{"name": f.name, "bytes": f.stat().st_size,
             "at": datetime.fromtimestamp(f.stat().st_mtime, IST).isoformat(timespec="seconds")}
            for f in sorted(folder().glob("bananapatterns-*.db"), reverse=True)]
