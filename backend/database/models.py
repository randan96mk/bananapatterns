"""SQLite (WAL) schema and data-access helpers."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from backend.config import IST, db_path


def now_ist() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def get_connection(path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_db(path: Optional[str] = None):
    conn = get_connection(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    level TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT
);
CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON system_logs(timestamp);

-- Kite instruments dump (NSE EQ + indices). Refreshed daily; tokens can change.
CREATE TABLE IF NOT EXISTS instruments (
    instrument_token INTEGER PRIMARY KEY,
    exchange_token INTEGER,
    tradingsymbol TEXT NOT NULL,
    name TEXT,
    exchange TEXT NOT NULL,
    segment TEXT NOT NULL,
    instrument_type TEXT,
    tick_size REAL,
    lot_size INTEGER,
    fetched_on TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_instr_symbol ON instruments(segment, tradingsymbol);

-- Dated index membership snapshots (avoid survivorship bias in backtests).
CREATE TABLE IF NOT EXISTS universe_snapshots (
    snapshot_date TEXT NOT NULL,
    index_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    company TEXT,
    industry TEXT,
    series TEXT,
    isin TEXT,
    PRIMARY KEY (snapshot_date, index_name, symbol)
);

-- Adjusted daily OHLCV from Kite. date = trading date (YYYY-MM-DD, IST).
CREATE TABLE IF NOT EXISTS candles_daily (
    instrument_token INTEGER NOT NULL,
    date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    PRIMARY KEY (instrument_token, date)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS ingest_status (
    instrument_token INTEGER PRIMARY KEY,
    tradingsymbol TEXT NOT NULL,
    first_date TEXT,
    last_date TEXT,
    bars INTEGER NOT NULL DEFAULT 0,
    last_full_fetch TEXT,
    last_incremental TEXT,
    last_error TEXT,
    last_error_at TEXT,
    updated_at TEXT NOT NULL
);

-- Stage of each stock per as-of date (rows only for stocks in some stage).
CREATE TABLE IF NOT EXISTS scan_results (
    as_of TEXT NOT NULL,
    screen TEXT NOT NULL,
    symbol TEXT NOT NULL,
    stage TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (as_of, screen, symbol)
);
CREATE INDEX IF NOT EXISTS idx_scan_stage ON scan_results(as_of, screen, stage);

-- What changed since the previous close (stage moves), per as-of date.
CREATE TABLE IF NOT EXISTS scan_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    screen TEXT NOT NULL,
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    change_pct REAL,
    close REAL
);
CREATE INDEX IF NOT EXISTS idx_changes_asof ON scan_changes(as_of, screen);

-- Per-stock metrics on each as-of date (every scanned stock, not only staged ones).
CREATE TABLE IF NOT EXISTS stock_metrics (
    as_of TEXT NOT NULL,
    symbol TEXT NOT NULL,
    rs INTEGER,
    rs_score REAL,
    industry TEXT,
    industry_rank INTEGER,
    industry_of INTEGER,
    tt_passed INTEGER NOT NULL,
    tt_of INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (as_of, symbol)
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    ran_at TEXT NOT NULL,
    scanned INTEGER NOT NULL,
    stale INTEGER NOT NULL,
    counts TEXT NOT NULL,
    config TEXT NOT NULL
);

-- Telegram alerts already sent (dedup key) and what was sent.
CREATE TABLE IF NOT EXISTS sent_alerts (
    key TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    symbol TEXT,
    text TEXT NOT NULL,
    ok INTEGER NOT NULL DEFAULT 1,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_sent_at ON sent_alerts(sent_at);

-- Automatic jobs (evening job, reminders, intraday checks, weekly refresh).
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,
    run_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs ON job_runs(job, run_date);

-- One-time Kite login links (Telegram button / bot /login). A newer link from the same source supersedes older ones.
CREATE TABLE IF NOT EXISTS login_links (
    nonce TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    superseded INTEGER NOT NULL DEFAULT 0
);

-- Intraday events per stock per day: crossed the pivot, failed, 15:15 verdict (close_holding/_weak/_extended).
CREATE TABLE IF NOT EXISTS intraday_flags (
    day TEXT NOT NULL,
    symbol TEXT NOT NULL,
    flag TEXT NOT NULL,
    at TEXT NOT NULL,
    detail TEXT,
    PRIMARY KEY (day, symbol, flag)
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK(status IN ('running', 'ok', 'partial', 'failed', 'aborted')),
    total INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    refetched INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""


def init_db(path: Optional[str] = None) -> None:
    p = Path(path or db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    with get_db(str(p)) as conn:
        _migrate_scan_tables(conn)
        conn.executescript(SCHEMA)


def _migrate_scan_tables(conn) -> None:
    """Scan tables are derived data: if they predate the `screen` column, drop them so
    they are recreated and the next scan (startup scan when behind) refills them."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(scan_results)")]
    if cols and "screen" not in cols:
        conn.executescript("DROP TABLE IF EXISTS scan_results; DROP TABLE IF EXISTS scan_changes; "
                           "DROP TABLE IF EXISTS scan_runs;")


# --- Settings ---

def get_setting(conn, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, now_ist()),
    )


# --- Logs ---

def insert_log(conn, level: str, source: str, message: str, details: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO system_logs (timestamp, level, source, message, details) VALUES (?, ?, ?, ?, ?)",
        (now_ist(), level, source, message, details),
    )


def get_logs(conn, level: Optional[str] = None, limit: int = 200) -> list[dict]:
    q, params = "SELECT * FROM system_logs", []
    if level:
        q += " WHERE level = ?"
        params.append(level)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(q, params)]


# --- Instruments ---

def replace_instruments(conn, rows: Iterable[dict]) -> int:
    rows = list(rows)
    fetched = now_ist()
    conn.execute("DELETE FROM instruments")
    conn.executemany(
        "INSERT OR REPLACE INTO instruments (instrument_token, exchange_token, tradingsymbol, name, exchange, "
        "segment, instrument_type, tick_size, lot_size, fetched_on) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(r["instrument_token"], r.get("exchange_token"), r["tradingsymbol"], r.get("name"), r["exchange"],
          r["segment"], r.get("instrument_type"), r.get("tick_size"), r.get("lot_size"), fetched) for r in rows],
    )
    return len(rows)


def find_instrument(conn, tradingsymbol: str, segment: str = "NSE") -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM instruments WHERE segment = ? AND tradingsymbol = ?", (segment, tradingsymbol)
    ).fetchone()
    return dict(row) if row else None


# --- Universe ---

def save_universe_snapshot(conn, snapshot_date: str, index_name: str, members: list[dict]) -> int:
    conn.execute("DELETE FROM universe_snapshots WHERE snapshot_date = ? AND index_name = ?",
                 (snapshot_date, index_name))
    conn.executemany(
        "INSERT INTO universe_snapshots (snapshot_date, index_name, symbol, company, industry, series, isin) "
        "VALUES (?,?,?,?,?,?,?)",
        [(snapshot_date, index_name, m["symbol"], m.get("company"), m.get("industry"), m.get("series"),
          m.get("isin")) for m in members],
    )
    return len(members)


def latest_universe(conn, index_name: str = "NIFTY 500") -> tuple[Optional[str], list[dict]]:
    row = conn.execute("SELECT MAX(snapshot_date) AS d FROM universe_snapshots WHERE index_name = ?",
                       (index_name,)).fetchone()
    if not row or not row["d"]:
        return None, []
    members = conn.execute(
        "SELECT * FROM universe_snapshots WHERE snapshot_date = ? AND index_name = ? ORDER BY symbol",
        (row["d"], index_name),
    ).fetchall()
    return row["d"], [dict(m) for m in members]


# --- Candles ---

def upsert_candles(conn, token: int, candles: Iterable[dict]) -> int:
    rows = [(token, c["date"], c["open"], c["high"], c["low"], c["close"], int(c["volume"])) for c in candles]
    conn.executemany(
        "INSERT INTO candles_daily (instrument_token, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(instrument_token, date) DO UPDATE SET open=excluded.open, high=excluded.high, "
        "low=excluded.low, close=excluded.close, volume=excluded.volume",
        rows,
    )
    return len(rows)


def delete_candles(conn, token: int) -> None:
    conn.execute("DELETE FROM candles_daily WHERE instrument_token = ?", (token,))


def get_candles(conn, token: int, start: Optional[str] = None, limit: Optional[int] = None) -> list[dict]:
    """Candles in ascending date order; `limit` keeps the most recent N."""
    q, params = "SELECT date, open, high, low, close, volume FROM candles_daily WHERE instrument_token = ?", [token]
    if start:
        q += " AND date >= ?"
        params.append(start)
    if limit:
        q = f"SELECT * FROM ({q} ORDER BY date DESC LIMIT ?) ORDER BY date"
        params.append(limit)
    else:
        q += " ORDER BY date"
    return [dict(r) for r in conn.execute(q, params)]


def candle_stats(conn, token: int) -> dict:
    row = conn.execute(
        "SELECT MIN(date) AS first_date, MAX(date) AS last_date, COUNT(*) AS bars "
        "FROM candles_daily WHERE instrument_token = ?", (token,)
    ).fetchone()
    return dict(row)


# --- Ingest status / runs ---

def update_ingest_status(conn, token: int, symbol: str, *, full: bool = False, error: Optional[str] = None) -> None:
    stats = candle_stats(conn, token)
    now = now_ist()
    conn.execute(
        """INSERT INTO ingest_status (instrument_token, tradingsymbol, first_date, last_date, bars,
               last_full_fetch, last_incremental, last_error, last_error_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(instrument_token) DO UPDATE SET
               tradingsymbol = excluded.tradingsymbol,
               first_date = excluded.first_date, last_date = excluded.last_date, bars = excluded.bars,
               last_full_fetch = COALESCE(excluded.last_full_fetch, ingest_status.last_full_fetch),
               last_incremental = COALESCE(excluded.last_incremental, ingest_status.last_incremental),
               last_error = excluded.last_error,
               last_error_at = COALESCE(excluded.last_error_at, ingest_status.last_error_at),
               updated_at = excluded.updated_at""",
        (token, symbol, stats["first_date"], stats["last_date"], stats["bars"],
         now if full and not error else None, now if not full and not error else None,
         error, now if error else None, now),
    )


def get_ingest_status(conn, token: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM ingest_status WHERE instrument_token = ?", (token,)).fetchone()
    return dict(row) if row else None


def start_run(conn, kind: str, total: int) -> int:
    cur = conn.execute("INSERT INTO ingest_runs (kind, started_at, total) VALUES (?, ?, ?)",
                       (kind, now_ist(), total))
    return cur.lastrowid


def update_run(conn, run_id: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE ingest_runs SET {sets} WHERE id = ?", [*fields.values(), run_id])


def get_runs(conn, limit: int = 10) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT ?", (limit,))]
