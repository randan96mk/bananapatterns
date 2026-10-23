"""Logging to rotating file + console, and WARNING+ to SQLite for the UI.

Only warnings and errors go to the DB: the ingest loop logs hundreds of INFO
lines per run and a SQLite write per line is wasteful.
"""

import logging
from logging.handlers import RotatingFileHandler

from backend.config import LOG_DIR


class DBHandler(logging.Handler):
    def emit(self, record):
        try:
            from backend.database.models import get_db, insert_log
            with get_db() as conn:
                insert_log(conn, record.levelname, record.name, record.getMessage(),
                           self.formatter.formatException(record.exc_info) if record.exc_info else None)
        except Exception:
            pass  # DB not ready yet; the file handler still has it


def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(f"bp.{name}")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(LOG_DIR / "bananapatterns.log", maxBytes=5 * 1024 * 1024, backupCount=3)
    ch = logging.StreamHandler()
    db = DBHandler(level=logging.WARNING)
    for h in (fh, ch, db):
        h.setFormatter(fmt)
        logger.addHandler(h)
    return logger
