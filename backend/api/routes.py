"""REST API: Kite auth, config, universe, ingest jobs, candles, logs."""

import json
import os
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from backend.config import IST, PERSISTED_SECTIONS, AppConfig, save_config
from backend.database.models import get_candles, get_db, get_logs, get_runs, latest_universe
from backend.engine.bases import detect_bases
from backend.engine.scan import run_scan
from backend.ingest import candles as cd
from backend.ingest import universe as uni
from backend.kite_api import auth
from backend.kite_api.client import KiteAuthError, KiteClient, KiteError
from backend.logging_config.logger import setup_logger

logger = setup_logger("api")
router = APIRouter(prefix="/api")

FRONTEND_BUILD = Path(__file__).resolve().parent.parent.parent / "frontend" / "build"


class AppState:
    def __init__(self, config: AppConfig, client: KiteClient):
        self.config = config
        self.client = client
        self.job = cd.JobState()
        self.worker: Optional[threading.Thread] = None
        # "db" = from browser login / POST token (expiry known), "env" = KITE_ACCESS_TOKEN, None = no token
        self.token_source: Optional[str] = None
        self.scan_lock = threading.Lock()
        self.scheduler = None                 # backend.alerts.jobs.Scheduler, set at startup

    def start_sync(self, force_full: bool = False, symbols: Optional[list[str]] = None) -> threading.Thread:
        """Background candle sync followed by a scan (the Sync button and the evening job)."""
        def work():
            try:
                snap = cd.run_sync(self.client, self.config.ingest, self.job, force_full=force_full, symbols=symbols)
                if snap["status"] in ("ok", "partial"):
                    self.scan()               # refresh the stage board on fresh data
            except Exception:
                logger.exception("Sync worker crashed")

        self.worker = threading.Thread(target=work, name="candle-sync", daemon=True)
        self.worker.start()
        return self.worker

    def run_sync_blocking(self) -> dict:
        self.start_sync().join()
        return self.job.snapshot()

    def scan(self) -> dict:
        """Run the stage scan over the resolved universe (serialised)."""
        with self.scan_lock:
            stocks = uni.resolve_universe(self.config.ingest)["stocks"]
            return run_scan(self.config.bases, self.config.screen, stocks, self.config.trend, self.config.vcp)

    def token_valid(self) -> bool:
        """False once a DB-stored token passes 06:00 IST (also clears it from the client)."""
        if not self.client.has_token:
            return False
        if self.token_source == "db":
            with get_db() as conn:
                if auth.load_fresh_token(conn) is None:
                    self.client.clear_access_token()
                    self.token_source = None
                    return False
        return True


_state: Optional[AppState] = None


def set_state(state: AppState) -> None:
    global _state
    _state = state


def S() -> AppState:
    if _state is None:
        raise HTTPException(503, "App not initialised")
    return _state


def _require_token() -> None:
    if not S().token_valid():
        raise HTTPException(401, "Kite login required (token missing or expired at 06:00 IST).")


# --- Kite auth ---

@router.get("/kite/login-url")
def kite_login_url():
    if not S().config.kite.is_configured:
        raise HTTPException(400, "KITE_API_KEY / KITE_API_SECRET not set in .env")
    return {"url": S().client.login_url()}


@router.get("/kite/callback")
def kite_callback(request_token: str = Query(None), status: str = Query(None)):
    if status == "failed" or not request_token:
        return HTMLResponse("<h2>Kite login failed or was cancelled.</h2><a href='/'>Back</a>", 400)
    try:
        data = S().client.generate_session(request_token)
    except KiteError as exc:
        return HTMLResponse(f"<h2>Kite authentication failed</h2><p>{exc}</p>"
                            "<p>Check KITE_API_KEY / KITE_API_SECRET in .env.</p><a href='/'>Back</a>", 400)
    with get_db() as conn:
        auth.store_token(conn, data["access_token"])
    S().token_source = "db"
    target = os.getenv("BP_FRONTEND_URL") or ("/" if FRONTEND_BUILD.exists() else "http://localhost:3000/")
    return RedirectResponse(f"{target}?kite=connected")


class TokenRequest(BaseModel):
    access_token: str


@router.post("/kite/token")
def kite_set_token(req: TokenRequest):
    S().client.set_access_token(req.access_token.strip())
    with get_db() as conn:
        issued = auth.store_token(conn, req.access_token.strip())
    S().token_source = "db"
    return {"status": "token_set", "expires_at": auth.expiry_for(issued).isoformat()}


@router.get("/kite/status")
def kite_status(verify: bool = False):
    st = S()
    cfg = st.config.kite
    valid = st.token_valid()
    with get_db() as conn:
        _, issued = auth.load_token(conn)
    from_db = st.token_source == "db"
    out = {
        "api_key_configured": cfg.is_configured,
        "has_token": valid,
        "token_source": st.token_source,
        "issued_at": issued.isoformat() if issued and from_db else None,
        "expires_at": auth.expiry_for(issued).isoformat() if issued and from_db else None,
        "needs_login": not valid,
        "login_url": st.client.login_url() if cfg.is_configured else None,
        "now": datetime.now(IST).isoformat(timespec="seconds"),
    }
    if verify and valid:
        try:
            out["user_id"] = st.client.profile().get("user_id")
            out["verified"] = True
        except KiteAuthError as exc:
            out.update(verified=False, needs_login=True, error=str(exc))
        except KiteError as exc:
            out.update(verified=False, error=str(exc))
    return out


# --- Config ---

@router.get("/config/{section}")
def get_config(section: str):
    if section not in PERSISTED_SECTIONS:
        raise HTTPException(404, f"Unknown section; have {list(PERSISTED_SECTIONS)}")
    return asdict(S().config.section(section))


@router.put("/config/{section}")
def put_config(section: str, values: dict):
    if section not in PERSISTED_SECTIONS:
        raise HTTPException(404, f"Unknown section; have {list(PERSISTED_SECTIONS)}")
    try:
        out = S().config.update_section(section, values)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc))
    save_config(S().config)
    if section == "ingest":
        S().client.hist_bucket.set_rate(S().config.ingest.hist_rate_per_sec)
    return out


# --- Universe ---

@router.post("/universe/refresh")
def universe_refresh():
    _require_token()
    try:
        return uni.refresh_universe(S().client, S().config.ingest)
    except KiteAuthError as exc:
        raise HTTPException(401, str(exc))
    except (KiteError, uni.UniverseError) as exc:
        raise HTTPException(502, str(exc))


@router.get("/universe")
def universe_list():
    resolved = uni.resolve_universe(S().config.ingest)
    rows = []
    with get_db() as conn:
        status = {r["instrument_token"]: dict(r) for r in conn.execute("SELECT * FROM ingest_status")}
        metrics = {r["symbol"]: dict(r) for r in conn.execute(
            "SELECT symbol, rs, tt_passed, tt_of, industry_rank, industry_of FROM stock_metrics "
            "WHERE as_of = (SELECT MAX(as_of) FROM stock_metrics)")}
        for item, kind in [(i, "index") for i in resolved["indices"]] + [(s, "stock") for s in resolved["stocks"]]:
            tok = item["instrument_token"]
            st = status.get(tok, {})
            last2 = get_candles(conn, tok, limit=2) if st.get("bars") else []
            close = last2[-1]["close"] if last2 else None
            prev = last2[0]["close"] if len(last2) == 2 else None
            rows.append({
                "kind": kind, "symbol": item["symbol"], "instrument_token": tok,
                "company": item.get("company"), "industry": item.get("industry"), "isin": item.get("isin"),
                "series": item.get("series"), "tradingsymbol": item.get("tradingsymbol", item["symbol"]),
                "bars": st.get("bars", 0), "first_date": st.get("first_date"), "last_date": st.get("last_date"),
                "close": close, "change_pct": round((close / prev - 1) * 100, 2) if close and prev else None,
                "last_error": st.get("last_error"),
                "rs": metrics.get(item["symbol"], {}).get("rs"),
                "tt_passed": metrics.get(item["symbol"], {}).get("tt_passed"),
                "tt_of": metrics.get(item["symbol"], {}).get("tt_of"),
            })
    return {"snapshot_date": resolved["snapshot_date"], "rows": rows,
            "unmatched": resolved["unmatched"], "excluded": resolved["excluded"],
            "missing_indices": resolved["missing_indices"]}


# --- Candles ---

def _resolve_stock(conn, symbol: str) -> tuple[dict, dict]:
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
    raise HTTPException(404, f"Unknown symbol {symbol}")


@router.get("/stock/{symbol}/candles")
def stock_candles(symbol: str, limit: int = Query(750, ge=1, le=5000)):
    with get_db() as conn:
        row, meta = _resolve_stock(conn, symbol)
        candles = get_candles(conn, row["instrument_token"], limit=limit)
    return {"symbol": symbol, "instrument_token": row["instrument_token"], "segment": row["segment"],
            "company": meta.get("company"), "industry": meta.get("industry"), "series": meta.get("series"),
            "candles": candles}


@router.get("/stock/{symbol}/metrics")
def stock_metrics(symbol: str):
    """Latest RS rating, industry rank and Trend Template checks (from the last scan)."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM stock_metrics WHERE symbol = ? ORDER BY as_of DESC LIMIT 1",
                           (symbol,)).fetchone()
    if not row:
        return {"symbol": symbol, "as_of": None}
    payload = json.loads(row["payload"])
    return {"symbol": symbol, "as_of": row["as_of"], "rs": row["rs"], "rs_score": row["rs_score"],
            "industry": row["industry"], "industry_rank": row["industry_rank"], "industry_of": row["industry_of"],
            "trend": payload["trend"]}


@router.get("/stock/{symbol}/vcp")
def stock_vcp(symbol: str):
    """VCP metrics for the stock's latest base (a breakout is judged on the bar before it),
    using the latest scan's RS and trend count."""
    from backend.engine.vcp import evaluate
    with get_db() as conn:
        row, _ = _resolve_stock(conn, symbol)
        candles = get_candles(conn, row["instrument_token"])
        m = conn.execute("SELECT rs, tt_passed FROM stock_metrics WHERE symbol = ? ORDER BY as_of DESC LIMIT 1",
                         (symbol,)).fetchone()
    bases = detect_bases(candles, S().config.bases)
    if not bases:
        return {"symbol": symbol, "vcp": None}
    base = bases[-1]
    end = len(candles) - 1 if base.status == "forming" else base.end_index
    v = evaluate(candles, base, m["rs"] if m else None, m["tt_passed"] if m else 0, S().config.vcp, end=end)
    return {"symbol": symbol, "base_label": base.label, "pivot": base.pivot, "vcp": v.to_dict()}


@router.get("/stock/{symbol}/bases")
def stock_bases(symbol: str, since: Optional[str] = None):
    """Bases over the full stored history (so numbering is right); `since` drops older ones."""
    with get_db() as conn:
        row, _ = _resolve_stock(conn, symbol)
        candles = get_candles(conn, row["instrument_token"])
    bases = [b.to_dict() for b in detect_bases(candles, S().config.bases)]
    if since:
        bases = [b for b in bases if b["end_date"] >= since]
    latest = bases[-1] if bases else None
    return {"symbol": symbol, "as_of": candles[-1]["date"] if candles else None, "bases": bases,
            "latest": latest, "config": asdict(S().config.bases)}


# --- Ingest jobs ---

class SyncRequest(BaseModel):
    force_full: bool = False
    symbols: Optional[list[str]] = None


@router.post("/ingest/sync")
def ingest_sync(req: SyncRequest = SyncRequest()):
    st = S()
    if st.worker and st.worker.is_alive():
        raise HTTPException(409, "A sync is already running")
    _require_token()
    reason = uni.universe_ready(st.config.ingest)
    if reason:
        raise HTTPException(400, reason)

    st.start_sync(force_full=req.force_full, symbols=req.symbols)
    return {"status": "started"}


@router.post("/ingest/cancel")
def ingest_cancel():
    S().job.cancel = True
    return {"status": "cancelling"}


@router.get("/ingest/status")
def ingest_status():
    with get_db() as conn:
        runs = get_runs(conn, 10)
    return {"job": S().job.snapshot(), "runs": runs}


# --- Screens (stage board) ---

SCREENS = {"bases": "Bases: every base the engine finds",
           "vcp": "VCP: bases with shrinking pullbacks, drying volume, RS >= 70 and an uptrend"}
STAGE_SORT = {
    "forming": lambda p: (p["dist_to_pivot_pct"] or 0),
    "fresh": lambda p: (p["sessions_since_breakout"] or 0, -(p["volume_ratio"] or 0)),   # newest first
    "fresh_low_volume": lambda p: (p["sessions_since_breakout"] or 0, -(p["volume_ratio"] or 0)),
    "climbing": lambda p: -(p["gain_from_pivot_pct"] or 0),
    "played_out": lambda p: (p["ended_date"] or ""),
}


def _check_screen(screen: str) -> None:
    if screen not in SCREENS:
        raise HTTPException(404, f"Unknown screen; have {list(SCREENS)}")


@router.post("/screens/scan")
def screens_scan():
    st = S()
    if st.worker and st.worker.is_alive():
        raise HTTPException(409, "A candle sync is running; the scan runs automatically when it finishes")
    return st.scan()


@router.get("/screens")
def screens_list():
    return [{"id": k, "description": v} for k, v in SCREENS.items()]


@router.get("/screens/{screen}/summary")
def screens_summary(screen: str):
    _check_screen(screen)
    with get_db() as conn:
        run = conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        latest_bar = conn.execute("SELECT MAX(last_date) FROM ingest_status").fetchone()[0]
        n_changes = conn.execute("SELECT COUNT(*) FROM scan_changes WHERE as_of = ? AND screen = ?",
                                 (run["as_of"], screen)).fetchone()[0] if run else 0
    if not run:
        return {"as_of": None, "counts": {}, "changes": 0, "latest_bar": latest_bar, "stale": bool(latest_bar)}
    return {"as_of": run["as_of"], "ran_at": run["ran_at"], "scanned": run["scanned"],
            "counts": json.loads(run["counts"]).get(screen, {}), "changes": n_changes, "latest_bar": latest_bar,
            "stale": bool(latest_bar and latest_bar > run["as_of"]),
            "fresh_sessions": S().config.screen.fresh_sessions}


@router.get("/screens/{screen}/results")
def screens_results(screen: str, stage: str = Query(...)):
    _check_screen(screen)
    if stage not in STAGE_SORT:
        raise HTTPException(422, f"stage must be one of {list(STAGE_SORT)}")
    with get_db() as conn:
        as_of = conn.execute("SELECT MAX(as_of) FROM scan_runs").fetchone()[0]
        rows = conn.execute("SELECT payload FROM scan_results WHERE as_of = ? AND screen = ? AND stage = ?",
                            (as_of, screen, stage)).fetchall()
    items = [json.loads(r["payload"]) for r in rows]
    if screen == "vcp":
        items.sort(key=lambda p: -(p.get("score") or 0))          # best setups first
    else:
        items.sort(key=STAGE_SORT[stage], reverse=(stage == "played_out"))
    return {"as_of": as_of, "stage": stage, "rows": items}


@router.get("/changes")
def changes(screen: str = "bases", limit: int = Query(200, le=1000)):
    _check_screen(screen)
    with get_db() as conn:
        as_of = conn.execute("SELECT MAX(as_of) FROM scan_runs").fetchone()[0]
        prev = conn.execute("SELECT MAX(as_of) FROM scan_runs WHERE as_of < ?", (as_of,)).fetchone()[0] \
            if as_of else None
        rows = conn.execute("SELECT symbol, kind, text, change_pct, close FROM scan_changes "
                            "WHERE as_of = ? AND screen = ? "
                            "ORDER BY CASE kind WHEN 'breakout' THEN 0 WHEN 'below_pivot' THEN 1 "
                            "WHEN 'played_out' THEN 2 WHEN 'forming' THEN 3 ELSE 4 END, symbol LIMIT ?",
                            (as_of, screen, limit)).fetchall()
    return {"as_of": as_of, "screen": screen, "previous_scan": prev, "rows": [dict(r) for r in rows]}


# --- Alerts (Telegram) and automatic jobs ---

@router.get("/alerts/status")
def alerts_status():
    from backend.alerts.dispatch import Notifier
    from backend.ingest import holidays as hol
    n = Notifier(S().config.alerts)
    today = datetime.now(IST).date()
    hols = hol.load_holidays()
    upcoming = sorted((d, name) for d, name in hols.items() if d >= today.isoformat())[:3]
    return {"bot_configured": n.client.configured, "chat_id": n.chat_id, "ready": n.ready,
            "enabled": S().config.alerts.enabled, "scheduler_running": bool(S().scheduler and S().scheduler._thread),
            "today": today.isoformat(), "trading_day": hol.is_trading_day(today),
            "holiday_today": hols.get(today.isoformat()),
            "upcoming_holidays": [{"date": d, "name": nm} for d, nm in upcoming]}


@router.post("/alerts/detect-chat")
def alerts_detect_chat():
    from backend.alerts.telegram import TelegramClient, TelegramError
    try:
        chats = TelegramClient().detect_chats()
    except TelegramError as exc:
        raise HTTPException(400, str(exc))
    if len(chats) == 1:
        S().config.update_section("alerts", {"chat_id": chats[0]["chat_id"]})
        save_config(S().config)
    return {"chats": chats, "saved": chats[0]["chat_id"] if len(chats) == 1 else None,
            "hint": None if chats else "Send any message to your bot in Telegram, then try again."}


@router.post("/alerts/test")
def alerts_test():
    from backend.alerts.dispatch import Notifier
    from backend.alerts.telegram import TelegramError
    n = Notifier(S().config.alerts)
    if not n.client.configured:
        raise HTTPException(400, "TELEGRAM_BOT_TOKEN is not set in .env (restart the backend after adding it)")
    if not n.chat_id:
        raise HTTPException(400, "No chat ID: press Detect chat ID first")
    try:
        n.client.send_message(n.chat_id, "✅ <b>Banana Patterns</b> alerts are connected.\n"
                                         "You'll get stage alerts after the evening scan.")
    except TelegramError as exc:
        raise HTTPException(502, str(exc))
    return {"status": "sent", "chat_id": n.chat_id}


@router.get("/alerts/bot")
def alerts_bot():
    """Check the bot token with Telegram (getMe) without sending anything."""
    from backend.alerts.dispatch import Notifier
    from backend.alerts.telegram import TelegramError
    n = Notifier(S().config.alerts)
    if not n.client.configured:
        return {"configured": False, "ok": False, "error": "TELEGRAM_BOT_TOKEN is not set in .env"}
    try:
        me = n.client.get_me()
    except TelegramError as exc:
        return {"configured": True, "ok": False, "error": str(exc)}
    return {"configured": True, "ok": True, "username": me.get("username"), "name": me.get("first_name"),
            "chat_id": n.chat_id}


class SampleRequest(BaseModel):
    kind: str = "all"


@router.post("/alerts/sample")
def alerts_sample(req: SampleRequest = SampleRequest()):
    """Send sample alerts (marked TEST) built from the latest scan; works any day, no Kite needed."""
    from backend.alerts.dispatch import Notifier
    from backend.alerts.samples import SAMPLE_KINDS, send_samples
    if req.kind != "all" and req.kind not in SAMPLE_KINDS:
        raise HTTPException(422, f"kind must be 'all' or one of {SAMPLE_KINDS}")
    n = Notifier(S().config.alerts)
    if not n.client.configured:
        raise HTTPException(400, "TELEGRAM_BOT_TOKEN is not set in .env (restart the backend after adding it)")
    if not n.chat_id:
        raise HTTPException(400, "No chat ID: press Detect chat ID first")
    tokens = {s["symbol"]: s["instrument_token"] for s in uni.resolve_universe(S().config.ingest)["stocks"]}
    results = send_samples(req.kind, S().config, tokens, n.chat_id, n.client)
    return {"chat_id": n.chat_id, "results": results, "sent": sum(r["ok"] for r in results),
            "failed": sum(not r["ok"] for r in results)}


@router.get("/alerts/log")
def alerts_log(limit: int = Query(100, le=500)):
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT key, sent_at, kind, symbol, text, ok, error FROM sent_alerts ORDER BY sent_at DESC LIMIT ?",
            (limit,))]


@router.get("/jobs")
def jobs_list(limit: int = Query(50, le=500)):
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,))]
    return {"runs": rows, "config": {k: getattr(S().config.alerts, k) for k in
                                     ("job_enabled", "job_time", "reminder_time", "intraday", "intraday_start",
                                      "intraday_end", "intraday_interval_min", "universe_refresh_weekday")}}


@router.post("/jobs/{job}/run")
def jobs_run(job: str):
    sch = S().scheduler
    if sch is None or job not in ("eod", "intraday", "weekly", "reminder"):
        raise HTTPException(404, "Unknown job or scheduler not running")
    now = datetime.now(IST)
    fn = (lambda: sch.run_eod(now, force=True)) if job == "eod" else (lambda: getattr(sch, f"run_{job}")(now))

    def work():
        try:
            fn()
        except Exception:
            logger.exception("Manual job %s failed", job)

    threading.Thread(target=work, name=f"job-{job}", daemon=True).start()
    return {"status": "started", "job": job}


# --- Logs ---

@router.get("/logs")
def logs(level: Optional[str] = None, limit: int = Query(200, le=1000)):
    with get_db() as conn:
        return get_logs(conn, level, limit)
