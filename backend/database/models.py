"""SQLite (WAL) schema and data-access helpers."""

import json
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

-- Summary alert style: the evening's alerts held for the evening report (batch = the scan date); the full text is
-- kept for the report's Details button and the chart captions. meta: tier, change, stop, distance ... for its lines.
CREATE TABLE IF NOT EXISTS alert_outbox (
    batch TEXT NOT NULL,
    key TEXT NOT NULL,
    kind TEXT NOT NULL,
    symbol TEXT,
    html TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (batch, key)
);

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

-- Daily market health (status + signals), written by each scan.
CREATE TABLE IF NOT EXISTS market_health (
    as_of TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);

-- Tonight's ranked setups for the next session (Top N message, morning plan, gap check).
CREATE TABLE IF NOT EXISTS daily_plan (
    as_of TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

-- Positions the guard follows (from Kite holdings / positions; read-only, never orders).
CREATE TABLE IF NOT EXISTS guard_positions (
    symbol TEXT PRIMARY KEY,
    qty INTEGER NOT NULL,
    avg_price REAL NOT NULL,
    first_seen TEXT NOT NULL,
    pivot REAL,
    stop REAL NOT NULL,
    stop_source TEXT NOT NULL,
    flags TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

-- Bot buttons: watched names always alert; muted names get no buy-side alerts until `until`.
CREATE TABLE IF NOT EXISTS watchlist (
    symbol TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS muted (
    symbol TEXT PRIMARY KEY,
    until TEXT NOT NULL
);

-- NSE event calendar: board meetings for results / dividends / fund raising (backend/ingest/events.py).
CREATE TABLE IF NOT EXISTS corp_events (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    kind TEXT NOT NULL,
    purpose TEXT NOT NULL,
    descr TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, date, purpose)
);
CREATE INDEX IF NOT EXISTS idx_corp_events_kind_date ON corp_events (kind, date);

-- NSE corporate announcements (backend/ingest/filings.py), kept KEEP_DAYS.
CREATE TABLE IF NOT EXISTS filings (
    seq_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    dt TEXT NOT NULL,
    category TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filings_symbol_dt ON filings (symbol, dt);

-- Bulk / block deals and SAST Reg 29 holder disclosures (backend/ingest/holders.py), kept KEEP_DAYS.
CREATE TABLE IF NOT EXISTS deals (
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    client TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    source TEXT NOT NULL,
    pct REAL,
    after_pct REAL,
    promoter INTEGER NOT NULL DEFAULT 0,
    url TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (date, symbol, client, side, qty, price, source)
);
CREATE INDEX IF NOT EXISTS idx_deals_symbol_date ON deals (symbol, date);

-- 📝 Paper trading (backend/engine/paper.py): the account state (cash, positions, the next open's orders) and the
-- append-only record (fills, closed trades, the account each session); never rewritten.
-- One account per variant; E+ is the only one since Nov 2026 (A rows are deleted on start by _migrate_drop_paper_a).
CREATE TABLE IF NOT EXISTS paper_state (account TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL DEFAULT 'E',
                                         payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_fills (id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL DEFAULT 'E',
                                        day TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_equity (account TEXT NOT NULL DEFAULT 'E', day TEXT NOT NULL, equity REAL NOT NULL,
                                         gross REAL NOT NULL, nifty REAL, PRIMARY KEY (account, day));

-- 💰 Real trading (backend/engine/real.py): the account, every webhook sent to Tradetron (never deleted; the token
-- is never stored), closed trades and the account each session.
CREATE TABLE IF NOT EXISTS real_state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS real_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,              -- day|symbol|action: each order is sent once
    day TEXT NOT NULL, created_at TEXT NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL, qty INTEGER,
    reason TEXT, status TEXT NOT NULL,     -- sent / failed / skipped
    reply TEXT, payload TEXT, price REAL
);
CREATE TABLE IF NOT EXISTS real_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS real_equity (day TEXT PRIMARY KEY, equity REAL NOT NULL, gross REAL NOT NULL, nifty REAL);

-- ⚡ Intraday Flow (backend/intraday): 1-minute bars, one compressed row per stock per day (shrunk to 5-minute bars
-- after the retention); every run (each node's in / out / drop reasons) and every signal.
CREATE TABLE IF NOT EXISTS corp_actions (
    instrument_token INTEGER NOT NULL, date TEXT NOT NULL, ratio REAL NOT NULL, PRIMARY KEY (instrument_token, date));
CREATE TABLE IF NOT EXISTS candles_intraday (
    instrument_token INTEGER NOT NULL, day TEXT NOT NULL, interval INTEGER NOT NULL DEFAULT 1, bars BLOB NOT NULL,
    n INTEGER NOT NULL, source TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (instrument_token, day));
CREATE TABLE IF NOT EXISTS flow_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS flow_positions (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, symbol TEXT NOT NULL,
    status TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS flow_backtests (id INTEGER PRIMARY KEY AUTOINCREMENT, setup TEXT NOT NULL, period TEXT NOT NULL,
    created TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS flow_signals (day TEXT NOT NULL, symbol TEXT NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (day, symbol));

-- Trend Breakout backtest (backend/engine/trend_breakout.py): the latest run (summary, splits, years, robustness,
-- curves, trades) as one JSON document.
CREATE TABLE IF NOT EXISTS screen_follow (
    symbol TEXT NOT NULL, breakout_date TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (symbol, breakout_date));
CREATE TABLE IF NOT EXISTS breakout_runs (
    id INTEGER PRIMARY KEY,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

-- Stock futures data layer (backend/futures): NFO contracts per underlying and day (history kept: the latest as_of
-- wins), continuous daily futures candles with OI (never deleted), the MWPL F&O ban list per day.
CREATE TABLE IF NOT EXISTS futures_contracts (
    underlying TEXT NOT NULL, month TEXT NOT NULL, tradingsymbol TEXT NOT NULL, token INTEGER NOT NULL,
    expiry TEXT NOT NULL, lot_size INTEGER NOT NULL, lot_value REAL, oi INTEGER, oi_rank INTEGER, as_of TEXT NOT NULL,
    PRIMARY KEY (as_of, underlying, month));
CREATE TABLE IF NOT EXISTS candles_fut_daily (
    underlying TEXT NOT NULL, date TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
    close REAL NOT NULL, volume INTEGER NOT NULL DEFAULT 0, oi INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (underlying, date)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fo_ban (date TEXT NOT NULL, symbol TEXT NOT NULL, PRIMARY KEY (date, symbol));
-- Backtest F runs (backend/futures/backtest.py): the latest row is the one shown.
CREATE TABLE IF NOT EXISTS futures_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL, ran_at TEXT NOT NULL);
-- The futures book (backend/futures/book.py): one state (four accounts: paper / real x long / short), every order
-- (sent, failed, skipped, filled on paper, rolls, hand corrections; never deleted), closed trades, equity per account.
CREATE TABLE IF NOT EXISTS futures_state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS futures_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,              -- day|account|symbol|action: each order once
    day TEXT NOT NULL, created_at TEXT NOT NULL, account TEXT NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL,
    lots INTEGER, contract TEXT, reason TEXT, status TEXT NOT NULL, reply TEXT, price REAL
);
CREATE TABLE IF NOT EXISTS futures_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS futures_equity (account TEXT NOT NULL, day TEXT NOT NULL, equity REAL NOT NULL, gross REAL NOT NULL,
                                           nifty REAL, PRIMARY KEY (account, day));

-- 🧩 The NIFTY options book (backend/options): the weekly contracts per day (history kept: the latest as_of wins), the
-- option-chain snapshots (9:30, every 5 min in the bias window, every minute while a spread is open, 15:30), one row
-- per trading day, the closed spreads, the book state, the equity per day. Nothing here is ever deleted.
CREATE TABLE IF NOT EXISTS options_contracts (
    as_of TEXT NOT NULL, expiry TEXT NOT NULL, strike REAL NOT NULL, type TEXT NOT NULL, token INTEGER NOT NULL,
    tradingsymbol TEXT NOT NULL, lot_size INTEGER NOT NULL, PRIMARY KEY (as_of, tradingsymbol));
CREATE TABLE IF NOT EXISTS options_chain_snap (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL, expiry TEXT NOT NULL,
    strike REAL NOT NULL, type TEXT NOT NULL, ltp REAL, bid REAL, ask REAL, oi INTEGER, iv REAL, delta REAL, spot REAL);
CREATE INDEX IF NOT EXISTS idx_opt_snap_day ON options_chain_snap(day, kind);
CREATE TABLE IF NOT EXISTS options_days (day TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS options_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS options_state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS options_equity (day TEXT PRIMARY KEY, equity REAL NOT NULL, gross REAL NOT NULL, nifty REAL);

-- 💡 Tips (backend/tips): a Telegram channel's posts (every one, with its kind), the hints decoded from them, the quotes
-- sampled while a hint is open or waiting (thinned to one a minute after it closes), the Tips paper book's fills and its
-- equity per day, the option / MCX contracts the hints resolve to. Standalone: no other book reads these. Never deleted.
CREATE TABLE IF NOT EXISTS tips_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, post_id INTEGER NOT NULL, posted_at TEXT NOT NULL,
    edited_at TEXT, deleted_at TEXT, text TEXT NOT NULL DEFAULT '', has_media INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL CHECK(kind IN ('chatter', 'promo', 'image', 'hint', 'follow_up')), hint_id INTEGER,
    classified_by TEXT NOT NULL DEFAULT 'code', model TEXT, ms INTEGER, source TEXT NOT NULL DEFAULT 'live',
    created TEXT NOT NULL, UNIQUE (channel, post_id));
CREATE INDEX IF NOT EXISTS idx_tips_posts_at ON tips_posts(posted_at);
CREATE TABLE IF NOT EXISTS tips_hints (
    id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, post_id INTEGER NOT NULL, created TEXT NOT NULL,
    posted_at TEXT NOT NULL, raw TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'live',
    symbol TEXT, side TEXT, instrument TEXT NOT NULL DEFAULT 'unknown'
        CHECK(instrument IN ('option', 'future', 'cash', 'commodity', 'crypto', 'unknown')),
    strike REAL, opt_type TEXT, expiry TEXT, expiry_src TEXT, tradingsymbol TEXT, exchange TEXT, lot_size INTEGER,
    entry TEXT, stop TEXT, targets TEXT, open_ended INTEGER NOT NULL DEFAULT 0, size_hint TEXT, decoded TEXT, plan TEXT,
    status TEXT NOT NULL DEFAULT 'new'
        CHECK(status IN ('new', 'waiting', 'active', 'paused', 'closed', 'skipped', 'expired')),
    ended TEXT, taken INTEGER NOT NULL DEFAULT 0, lots REAL, entry_px REAL, exit_px REAL, pnl REAL,
    events TEXT NOT NULL DEFAULT '[]', tracked INTEGER NOT NULL DEFAULT 0, untracked_why TEXT,
    run TEXT, pos TEXT, score TEXT, updated TEXT, kite TEXT, UNIQUE (channel, post_id));
CREATE INDEX IF NOT EXISTS idx_tips_hints_status ON tips_hints(status);
CREATE TABLE IF NOT EXISTS tips_quotes (hint_id INTEGER NOT NULL, t TEXT NOT NULL, ltp REAL, spot REAL, PRIMARY KEY (hint_id, t));
CREATE TABLE IF NOT EXISTS tips_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, hint_id INTEGER NOT NULL, opened_at TEXT, closed_at TEXT NOT NULL, side TEXT,
    symbol TEXT, lots REAL, qty INTEGER, entry_px REAL, exit_px REAL, gross REAL, charges REAL, net REAL, why TEXT);
CREATE TABLE IF NOT EXISTS tips_equity (day TEXT PRIMARY KEY, equity REAL NOT NULL, realised REAL, open_pnl REAL);
CREATE TABLE IF NOT EXISTS tips_instruments (
    as_of TEXT NOT NULL, exchange TEXT NOT NULL, tradingsymbol TEXT NOT NULL, name TEXT NOT NULL, expiry TEXT,
    strike REAL, type TEXT, token INTEGER, lot_size INTEGER, PRIMARY KEY (as_of, exchange, tradingsymbol));
CREATE INDEX IF NOT EXISTS idx_tips_instr_name ON tips_instruments(name, as_of);
-- ⚡ Tips → Kite orders (backend/tips/kite_orders.py): every order the app sent to Kite for a hint (only ours: tag
-- bptips), its leg, its status from the last sync and its fill. Never deleted.
CREATE TABLE IF NOT EXISTS tips_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, hint_id INTEGER NOT NULL,
    leg TEXT NOT NULL CHECK(leg IN ('entry', 'stop', 't1', 't2', 'exit')), side TEXT, qty INTEGER, order_type TEXT,
    price REAL, "trigger" REAL, status TEXT NOT NULL DEFAULT 'placed'
        CHECK(status IN ('placed', 'open', 'complete', 'rejected', 'cancelled', 'failed')),
    order_id TEXT, filled_px REAL, filled_qty INTEGER, placed_at TEXT, updated_at TEXT,
    "by" TEXT CHECK("by" IN ('click', 'app', 'auto')), note TEXT, day TEXT);
CREATE INDEX IF NOT EXISTS idx_tips_orders_day ON tips_orders(day);
CREATE INDEX IF NOT EXISTS idx_tips_orders_hint ON tips_orders(hint_id);

-- 🤖 Local AI (backend/ai): every job the queue ran (queued / running / done / failed / skipped) and the notes the
-- model wrote (filled from PR 2). Nothing here is ever deleted.
CREATE TABLE IF NOT EXISTS ai_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, started TEXT, finished TEXT, kind TEXT NOT NULL,
    key TEXT NOT NULL, model TEXT, status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued', 'running', 'done', 'failed', 'skipped')),
    ms INTEGER, tokens INTEGER, error TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS idx_ai_jobs_status ON ai_jobs(status);
CREATE TABLE IF NOT EXISTS ai_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL, symbol TEXT,
    book TEXT, severity TEXT, title TEXT, text TEXT, data TEXT, model TEXT, prompt_version TEXT, ms INTEGER);
CREATE INDEX IF NOT EXISTS idx_ai_notes_day ON ai_notes(day, kind);
-- 🤖 suggestions (PR 3): what the AI proposes for the next session; code decides the kind, the evidence, the expiry and
-- the outcome, the model writes the one-line why. Never deleted; one per kind, symbol and day.
CREATE TABLE IF NOT EXISTS ai_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, day TEXT NOT NULL, for_day TEXT, kind TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT '', book TEXT, title TEXT NOT NULL, why TEXT, evidence TEXT, action TEXT, confidence TEXT,
    expires_at TEXT, status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'accepted', 'dismissed', 'expired')),
    decided_at TEXT, reason TEXT, outcome TEXT, note_id INTEGER, model TEXT, prompt_version TEXT, data TEXT,
    UNIQUE (kind, symbol, day));
CREATE INDEX IF NOT EXISTS idx_ai_sugg_status ON ai_suggestions(status, expires_at);
-- 🤖 ask: every question put to the app (page or Telegram), the tool the model picked, the answer; never deleted
CREATE TABLE IF NOT EXISTS ai_asks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, who TEXT NOT NULL, question TEXT NOT NULL, tool TEXT,
    args TEXT, answer TEXT, ms INTEGER, model TEXT, ok INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE INDEX IF NOT EXISTS idx_ai_asks_created ON ai_asks(created);
-- 🤖 AI view: a rating beside every system-suggested stock (score, verdict by code, reasons, levels picked from the
-- candidates code found, the facts it got, the outcome 5 / 10 / 20 sessions later, the shuffle score). Never deleted.
CREATE TABLE IF NOT EXISTS ai_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, day TEXT NOT NULL, symbol TEXT NOT NULL, source TEXT NOT NULL,
    book TEXT, rank_in_source INTEGER, score INTEGER, verdict TEXT, verdict_model TEXT, confidence REAL, reasons TEXT, change TEXT,
    would_change TEXT, levels TEXT, candidates TEXT, features TEXT, previous_id INTEGER, model TEXT, prompt_version TEXT, ms INTEGER,
    status TEXT NOT NULL DEFAULT 'ok' CHECK(status IN ('ok', 'failed')), outcome TEXT, shuffle_score INTEGER,
    UNIQUE (day, symbol, source));
CREATE INDEX IF NOT EXISTS idx_ai_views_symbol ON ai_views(symbol, day);

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
        dropped = _migrate_orb(conn)
        _migrate_paper(conn)
        conn.executescript(SCHEMA)
        _migrate_drop_paper_a(conn)
        _migrate_capital_300k(conn)
        _migrate_real_confirm(conn)
        _add_column(conn, "guard_positions", "source", "TEXT NOT NULL DEFAULT 'sync'")   # sync / manual (kite: removed)
        _migrate_ai_notes(conn)
        _drop_track_record(conn)
        n = conn.execute("DELETE FROM guard_positions WHERE source = 'kite'").rowcount   # Kite holdings: never again
        if n:
            print(f"Removed {n} guard position(s) that came from Kite holdings (the app no longer reads them)")
    if dropped:                                         # give the ~400 MB of 5-minute candles back to the disk
        conn = sqlite3.connect(str(p), timeout=30, isolation_level=None)
        try:
            conn.execute("VACUUM")
        except sqlite3.Error as exc:
            print(f"VACUUM after dropping the ORB tables failed: {exc}")
        finally:
            conn.close()


def _migrate_paper(conn) -> None:
    """Paper trading became one account per variant (Oct 2026): the single account becomes account A (which
    _migrate_drop_paper_a then deletes: A, the 200-day filter, was removed in Nov 2026)."""
    cols = lambda t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
    if "id" in cols("paper_state"):
        conn.execute("ALTER TABLE paper_state RENAME TO paper_state_old")
        conn.execute("CREATE TABLE paper_state (account TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        conn.execute("INSERT INTO paper_state (account, payload) SELECT 'A', payload FROM paper_state_old")
        conn.execute("DROP TABLE paper_state_old")
    for t in ("paper_trades", "paper_fills"):
        if cols(t) and "account" not in cols(t):
            conn.execute(f"ALTER TABLE {t} ADD COLUMN account TEXT NOT NULL DEFAULT 'A'")
    if cols("paper_equity") and "account" not in cols("paper_equity"):
        conn.execute("ALTER TABLE paper_equity RENAME TO paper_equity_old")
        conn.execute("CREATE TABLE paper_equity (account TEXT NOT NULL DEFAULT 'A', day TEXT NOT NULL, "
                     "equity REAL NOT NULL, gross REAL NOT NULL, nifty REAL, PRIMARY KEY (account, day))")
        conn.execute("INSERT INTO paper_equity (account, day, equity, gross, nifty) "
                     "SELECT 'A', day, equity, gross, nifty FROM paper_equity_old")
        conn.execute("DROP TABLE paper_equity_old")


PAPER_TABLES = ("paper_state", "paper_trades", "paper_fills", "paper_equity")


def _migrate_drop_paper_a(conn) -> None:
    """Trend Breakout A (the 200-day filter) was removed in Nov 2026: E+ is the only swing variant. Once, on the
    first start after the upgrade: Paper A's rows (account = 'A') are deleted from the paper tables and the A
    variant is dropped from the saved backtest run. Nothing else is touched (E's rows, candles, settings, any other
    table). Idempotent: running it again finds nothing to do and is silent."""
    removed = {}
    for t in PAPER_TABLES:
        n = conn.execute(f"DELETE FROM {t} WHERE account = 'A'").rowcount
        if n:
            removed[t] = n
    for rid, raw in conn.execute("SELECT id, payload FROM breakout_runs").fetchall():
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        variants = payload.get("variants")
        if isinstance(variants, dict) and "A" in variants:
            del variants["A"]
            payload.pop("stale", None)
            conn.execute("UPDATE breakout_runs SET payload = ? WHERE id = ?", (json.dumps(payload), rid))
            removed["breakout_runs.variants"] = 1
    if removed:
        print("Trend Breakout A removed (E+ is the only variant): deleted "
              + ", ".join(f"{n} {t} row(s)" if not t.startswith("breakout_runs") else "the A variant from the saved backtest run"
                          for t, n in removed.items()))


OLD_CAPITAL, NEW_CAPITAL = 200_000.0, 300_000.0
CAPITAL_DONE_KEY = "capital.300k.done"


def _migrate_capital_300k(conn) -> list[str]:
    """The capital became Rs 3,00,000 everywhere (Nov 2026). Once, guarded by the app_settings key
    `capital.300k.done`: every saved setting still at the old default is set to the new one (`breakout.capital`,
    `paper.capital` and `real.capital` at 2,00,000; the intraday Size & risk box and its saved setups at 1,00,000 or
    2,00,000), and a running Real / Paper account that started with 2,00,000 gets +1,00,000 through the same deposit
    path as the capital box (cash moves, the deposit is recorded, never counted as profit). A value the owner already
    changed is left alone; nothing else (candles, trades, curves) is touched. Returns what changed, also printed."""
    if get_setting(conn, CAPITAL_DONE_KEY):
        return []
    changed = []
    for name in ("breakout", "paper", "real"):           # the saved config sections
        raw = get_setting(conn, f"config.{name}")
        if not raw:
            continue
        try:
            sec = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(sec, dict) and sec.get("capital") == OLD_CAPITAL:
            sec["capital"] = NEW_CAPITAL
            set_setting(conn, f"config.{name}", json.dumps(sec))
            changed.append(f"{name}.capital")
    old_flow = (100_000.0, OLD_CAPITAL)

    def bump_nodes(nodes: dict) -> bool:
        size = nodes.get("size") if isinstance(nodes, dict) else None
        if isinstance(size, dict) and size.get("capital") in old_flow:
            size["capital"] = NEW_CAPITAL
            return True
        return False

    try:
        nodes = json.loads(get_setting(conn, "flow.nodes") or "{}")
    except (TypeError, ValueError):
        nodes = {}
    if bump_nodes(nodes):
        set_setting(conn, "flow.nodes", json.dumps(nodes))
        changed.append("flow Size & risk capital")
    try:
        setups = json.loads(get_setting(conn, "flow.setups") or "{}")
    except (TypeError, ValueError):
        setups = {}
    bumped = [k for k, v in setups.items() if isinstance(v, dict) and bump_nodes(v.get("nodes"))]
    if bumped:
        set_setting(conn, "flow.setups", json.dumps(setups))
        changed.append("flow setup(s) " + ", ".join(bumped))
    # running accounts: +1,00,000 as a deposit (the engines' own helpers, saved on this connection)
    from backend.engine import paper, real
    r = conn.execute("SELECT payload FROM real_state WHERE id = 1").fetchone()
    if r:
        st = json.loads(r[0])
        if st.get("capital") == OLD_CAPITAL:
            real.apply_deposit(st, NEW_CAPITAL, _config_from(conn))
            st["updated_at"] = now_ist()
            conn.execute("UPDATE real_state SET payload = ? WHERE id = 1", (json.dumps(st),))
            changed.append("Real account +1,00,000 deposit")
    for acct, raw in conn.execute("SELECT account, payload FROM paper_state").fetchall():
        st = json.loads(raw)
        if st.get("capital") == OLD_CAPITAL:
            paper.apply_deposit(st, NEW_CAPITAL)
            conn.execute("UPDATE paper_state SET payload = ? WHERE account = ?", (json.dumps(st), acct))
            changed.append(f"Paper {acct} +1,00,000 deposit")
    set_setting(conn, CAPITAL_DONE_KEY, now_ist())
    if changed:
        print("Capital is Rs 3,00,000 everywhere now: " + "; ".join(changed))
    return changed


REAL_CONFIRM_DONE_KEY = "real.confirm.migrated"


def _migrate_real_confirm(conn) -> list[str]:
    """💰 Fill confirmation (after the 18 Nov 2026 incident): every Real position booked from now on starts unconfirmed
    and gets no EXIT until its fill is confirmed. Once, guarded by `real.confirm.migrated`: the positions open before
    this update (reconciled by hand by the owner that day) are marked confirmed, by "migration". A position that
    already carries the flag is left alone. Returns the symbols marked."""
    if get_setting(conn, REAL_CONFIRM_DONE_KEY):
        return []
    marked = []
    r = conn.execute("SELECT payload FROM real_state WHERE id = 1").fetchone()
    if r:
        st = json.loads(r[0])
        for sym, pos in (st.get("positions") or {}).items():
            if "confirmed" not in pos:
                pos.update(confirmed=True, confirmed_at=now_ist(), confirmed_by="migration", unconfirmed_since=None)
                marked.append(sym)
        if marked:
            conn.execute("UPDATE real_state SET payload = ? WHERE id = 1", (json.dumps(st),))
    set_setting(conn, REAL_CONFIRM_DONE_KEY, now_ist())
    if marked:
        print(f"Real fill confirmation: {len(marked)} open position(s) marked confirmed (by migration): {', '.join(sorted(marked))}")
    return marked


def _config_from(conn):
    """The saved config read on this connection (load_config opens its own, which may be another file)."""
    from dataclasses import fields
    from backend.config import AppConfig
    config = AppConfig()
    for name in ("breakout", "real"):
        raw = get_setting(conn, f"config.{name}")
        if not raw:
            continue
        sec = config.section(name)
        known = {f.name for f in fields(sec)}
        for key, value in json.loads(raw).items():
            if key in known:
                setattr(sec, key, value)
    return config


def _migrate_orb(conn) -> bool:
    """The ORB backtests (Oct 2026) were replaced by the daily Trend Breakout: their results, saved settings and the
    5-minute candles (only they used them) are dropped. True when something was dropped (worth a VACUUM).
    Runs on every start, so it is one cheap sqlite_master lookup and nothing else once the tables are gone."""
    names = ("orb_trades", "orb_signals", "candles_5m", "candles_5m_days")
    old = [r[0] for r in conn.execute(f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
                                      f"({','.join('?' * len(names))})", names)]
    if not old:
        return False
    for t in old:
        conn.execute(f"DROP TABLE {t}")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_settings'").fetchone():
        conn.execute("DELETE FROM app_settings WHERE key LIKE 'orb.%' OR key = 'config.orb'")
    return True


def _drop_track_record(conn) -> None:
    """The Track record tab was removed (Oct 2026): drop its result tables and settings. The 5-minute candles stay."""
    conn.execute("DROP TABLE IF EXISTS track_signals")
    conn.execute("DROP TABLE IF EXISTS track_intraday")
    conn.execute("DELETE FROM app_settings WHERE key LIKE 'track.%' OR key LIKE 'track\\_intraday.%' ESCAPE '\\' "
                 "OR key IN ('config.track', 'config.intraday')")


def _add_column(conn, table: str, column: str, decl: str) -> None:
    if column not in [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate_ai_notes(conn) -> None:
    """🤖 PR 2: `source_key` (the filing's seq_id / the deal group's symbol|date) and the guard that notes a source once
    per prompt version. Added with ALTER TABLE behind a PRAGMA check; nothing is dropped."""
    _add_column(conn, "ai_notes", "source_key", "TEXT")
    _add_column(conn, "tips_posts", "reply_to", "INTEGER")        # 💡 the post a reply quotes (Telegram's reply-to id)
    _add_column(conn, "tips_hints", "kite", "TEXT")               # ⚡ the hint's Kite legs (JSON; Tips → Kite orders)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_notes_source ON ai_notes(kind, source_key, prompt_version) "
                 "WHERE source_key IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_notes_symbol ON ai_notes(symbol, created)")


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


def get_candles(conn, token: int, start: Optional[str] = None, limit: Optional[int] = None,
                adjusted: bool = True) -> list[dict]:
    """Candles in ascending date order; `limit` keeps the most recent N. adjusted: prices before each split / bonus
    in `corp_actions` are scaled by its ratio (volume the other way), so a 1:2 split is not a 50% crash."""
    q, params = "SELECT date, open, high, low, close, volume FROM candles_daily WHERE instrument_token = ?", [token]
    if start:
        q += " AND date >= ?"
        params.append(start)
    if limit:
        q = f"SELECT * FROM ({q} ORDER BY date DESC LIMIT ?) ORDER BY date"
        params.append(limit)
    else:
        q += " ORDER BY date"
    rows = [dict(r) for r in conn.execute(q, params)]
    if adjusted and rows:
        acts = conn.execute("SELECT date, ratio FROM corp_actions WHERE instrument_token = ? AND date > ? ORDER BY date DESC",
                            (token, rows[0]["date"])).fetchall()
        if acts:
            f, k = 1.0, 0
            for r in reversed(rows):
                while k < len(acts) and acts[k][0] > r["date"]:
                    f *= acts[k][1]
                    k += 1
                if f != 1.0:
                    for key in ("open", "high", "low", "close"):
                        r[key] = round(r[key] * f, 2)
                    r["volume"] = int(round(r["volume"] / f))
    return rows


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
