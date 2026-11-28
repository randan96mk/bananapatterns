"""REST API: Kite auth, config, universe, ingest jobs, candles, logs."""

import json
import os
import re
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Optional, Union

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from backend.timefmt import ampm, ampm_date
from backend.config import IST, PERSISTED_SECTIONS, AppConfig, save_config
from backend.database.models import get_candles, get_db, get_logs, get_runs, get_setting, now_ist, set_setting
from backend.engine.bases import detect_bases
from backend.engine.scan import run_scan
from backend.ingest import candles as cd
from backend.ingest import events as ev
from backend.ingest import filings as fl
from backend.ingest import holders as hd
from backend.ingest import universe as uni
from backend.api import security
from backend.kite_api import auth
from backend.kite_api import login_link as ll
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
        self._ticker = None
        self._live = None
        self.tips_tracker = None              # 💡 backend.tips.track.Tracker, set at startup
        self.tips_kite = None                 # ⚡ backend.tips.kite_orders.Syncer, set at startup
        self.market = None                    # 📈 backend.market.reads.MarketService, set at startup
        self.crypto = None                    # ₿ backend.crypto.feed.CryptoService, set at startup (alerts only)

    @property
    def ticker(self):
        """The index ticker bar (Nifty 50 / 500, Bank Nifty, India VIX, Sensex), created on first use."""
        if self._ticker is None:
            from backend.ingest.indices import IndexTicker
            self._ticker = IndexTicker(self.client)
        return self._ticker

    @property
    def live(self):
        """The Today page / live positions board (quotes, opening ranges, sparklines), created on first use."""
        if self._live is None:
            from backend.alerts.live import LiveBoard
            self._live = LiveBoard(self)
        return self._live

    def start_sync(self, force_full: bool = False, symbols: Optional[list[str]] = None) -> threading.Thread:
        """Background candle sync followed by a scan (the Sync button and the evening job)."""
        def work():
            try:
                snap = cd.run_sync(self.client, self.config.ingest, self.job, force_full=force_full, symbols=symbols)
                if snap["status"] in ("ok", "partial"):
                    try:                      # splits / bonuses in the fresh candles, before anything reads them
                        from backend.ingest import corp_actions
                        u = uni.resolve_universe(self.config.ingest)
                        n = corp_actions.refresh([s["instrument_token"] for s in u["stocks"]])
                        logger.info("Corporate actions on file: %d", n)
                    except Exception:
                        logger.exception("Corporate-action check failed")
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
            u = uni.resolve_universe(self.config.ingest)
            index = next((i["instrument_token"] for i in u["indices"] if i["symbol"] == uni.INDEX_NAME), None)
            out = run_scan(self.config.bases, self.config.screen, u["stocks"], self.config.trend, self.config.vcp,
                           self.config.market, index, self.config.flag)
            if out.get("as_of"):
                try:                                    # 📈 Follow steps after the scan; never fails the scan
                    from backend.engine import follow
                    follow.update(self.config, out["as_of"], u["stocks"])
                except Exception:
                    logger.exception("Follow update failed")
            return out

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
        raise HTTPException(401, "Kite login required (token missing or expired at 6:00 AM IST).")


# --- Kite auth ---

@router.get("/kite/login-url")
def kite_login_url():
    if not S().config.kite.is_configured:
        raise HTTPException(400, "KITE_API_KEY / KITE_API_SECRET not set in .env")
    return {"url": S().client.login_url()}


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    """Small phone-friendly page for the end of a login."""
    return HTMLResponse(
        "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title><body style='font:17px system-ui;background:#0f1115;color:#e6e6e6;"
        f"padding:28px;max-width:520px;margin:auto'><h2>{escape(title)}</h2>{body}</body>", status)


LINK_REFUSED = {"unknown": "isn't valid", "used": "was already used", "superseded": "was replaced by a newer link",
                "expired": "has expired (links work for 15 minutes)"}


@router.get("/kite/callback")
def kite_callback(request: Request, request_token: str = Query(None), status: str = Query(None),
                  n: Optional[str] = Query(None)):
    """Kite redirects here after login. With `n` (a Telegram link) the one-time nonce must be valid;
    without it (dashboard login) the dashboard password applies when one is set."""
    if n:
        with get_db() as conn:
            refused = ll.check_link(conn, n)
        if refused:
            return _page("Login link not usable", f"<p>This link {LINK_REFUSED[refused]}.</p>"
                         "<p>Use the newest link in Telegram, or send <b>/login</b> to the bot.</p>", 400)
    elif not security.authorised(request):
        return security.challenge()
    if status == "failed" or not request_token:
        return _page("Kite login failed or was cancelled", "<p>Try the link again.</p>", 400)
    try:
        data = S().client.generate_session(request_token)
    except KiteError as exc:
        return _page("Kite authentication failed", f"<p>{escape(str(exc))}</p>"
                     "<p>Check KITE_API_KEY / KITE_API_SECRET in .env.</p>", 400)
    token = data["access_token"]
    with get_db() as conn:
        issued = auth.store_token(conn, token)
        if n:
            ll.mark_used(conn, n)
    S().token_source = "db"
    from backend.alerts.dispatch import Notifier
    from backend.alerts.login import confirm_login
    try:
        confirm_login(Notifier(S().config.alerts), issued)
    except Exception:
        logger.exception("Login confirmation alert failed")
    if not n:
        target = os.getenv("BP_FRONTEND_URL") or ("/" if FRONTEND_BUILD.exists() else "http://localhost:3000/")
        return RedirectResponse(f"{target}?kite=connected")
    return _page("✅ Kite connected", f"<p>Valid until {ampm_date(auth.expiry_for(issued))} IST.</p>"
                 "<p>You can close this page.</p>")


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
        "redirect_url": ll.redirect_url(),
        "password_set": bool(security.password()),
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


def _truthy(v) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on") if isinstance(v, str) else bool(v)


@router.put("/config/{section}")
def put_config(section: str, values: dict):
    if section not in PERSISTED_SECTIONS:
        raise HTTPException(404, f"Unknown section; have {list(PERSISTED_SECTIONS)}")
    from dataclasses import asdict
    before = asdict(S().config.section(section))
    if section == "options" and _truthy(values.get("real")):
        raise HTTPException(400, "not wired")             # the options Real switch: no webhook yet, stays off
    if section == "ai":                                    # 🤖 AI view: the locked switches and the bounds
        from backend.ai import view as ai_view
        try:
            ai_view.check_lock(values, before)
            ai_view.check_bounds(values)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if section == "ai" and _truthy(values.get("enabled")) and not before.get("enabled"):
        from backend.ai import control as ai_control
        if not ai_control.can_enable(S().config):          # 🤖 the AI goes on only once both models are on disk
            raise HTTPException(400, ai_control.NEEDS_MODELS)
    try:
        out = S().config.update_section(section, values)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc))
    save_config(S().config)
    after = asdict(S().config.section(section))
    what = [f"{k}: {before[k]} → {after[k]}" for k in after if before.get(k) != after[k]]
    if what:                                       # the settings change log (System › Settings)
        with get_db() as conn:
            log = json.loads(get_setting(conn, "config.changes") or "[]")
            log.insert(0, {"at": now_ist(), "section": section, "what": what})
            set_setting(conn, "config.changes", json.dumps(log[:100]))
    if section == "ingest":
        S().client.hist_bucket.set_rate(S().config.ingest.hist_rate_per_sec)
    if section == "crypto" and getattr(S(), "crypto", None) is not None:
        S().crypto.poke()                          # ₿ a new symbol / timeframe: backfilled on the next pass
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
    from backend.alerts.preview import resolve_stock
    try:
        return resolve_stock(conn, symbol)
    except LookupError as exc:
        raise HTTPException(404, str(exc))


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


@router.get("/stock/{symbol}/alert-chart.png")
def stock_alert_chart(symbol: str, kind: str = Query("auto")):
    """The annotated decision chart a Telegram alert of `kind` would carry for this stock.
    auto = the kind matching the stock's stage in the latest scan (VCP screen first, then Bull flag, then Bases)."""
    from backend.alerts.dispatch import charts_available
    from backend.alerts.preview import alert_chart
    if not charts_available():
        raise HTTPException(503, "matplotlib is not installed: run .venv/bin/pip install -r requirements.txt")
    try:
        png, kind = alert_chart(S().config, symbol, kind)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    if not png:
        raise HTTPException(404, f"No base to annotate for {symbol}")
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store", "X-Alert-Kind": kind})


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
           "vcp": "VCP: bases with shrinking pullbacks, drying volume, RS >= 70 and an uptrend",
           "flag": "Bull flag: a 20%+ pole in 6 weeks, then a 1-5 week flag giving back <= 38% on drying volume"}
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


RESULTS_HORIZON = 15                                  # sessions: cards show results due within ~3 weeks


def _results_for(symbols: list[str]) -> dict[str, dict]:
    """{symbol: {date, sessions, label, warn}} for results due within RESULTS_HORIZON sessions."""
    cfg = S().config.alerts
    if not cfg.results_calendar or not symbols:
        return {}
    out = {}
    for sym, r in ev.upcoming_results(now_ist()[:10], symbols).items():
        if r["sessions"] <= RESULTS_HORIZON:
            out[sym] = dict(r, label=ev.results_label(r), warn=r["sessions"] <= cfg.results_warn_sessions)
    return out


def _news_for(symbols: list[str]) -> dict[str, dict]:
    """{symbol: newest important NSE filing in the last news_days}, for the 📰 chips."""
    cfg = S().config.alerts
    if not cfg.filings or not symbols:
        return {}
    since = (datetime.now(IST).date() - timedelta(days=cfg.news_days)).isoformat()
    return fl.latest_important(symbols, since)


@router.get("/filings")
def filings_list(symbol: Optional[str] = None, days: int = Query(30, ge=1, le=120)):
    """One stock's NSE filings (important ones and updates, newest first), or the fetch status."""
    since = (datetime.now(IST).date() - timedelta(days=days)).isoformat()
    if symbol:
        return {"symbol": symbol.upper(), "filings": fl.recent(symbol.upper(), since)}
    with get_db() as conn:
        n = conn.execute(f"SELECT COUNT(*) FROM filings WHERE dt >= ? AND kind IN "
                         f"({','.join('?' * len(fl.IMPORTANT))})",
                         (datetime.now(IST).date().isoformat(), *fl.IMPORTANT)).fetchone()[0]
    return {"last_fetch": fl.last_fetch(), "important_today": n}


@router.post("/filings/refresh")
def filings_refresh():
    """Fetch new NSE filings now (no alerts are sent from here; the scheduler does that)."""
    try:
        new = fl.refresh_from_nse()
    except Exception as exc:
        raise HTTPException(502, f"NSE filings fetch failed: {exc}")
    return {"new": len(new), "last_fetch": fl.last_fetch()}


@router.get("/news")
def news(days: Optional[int] = Query(None, ge=1, le=30)):
    """The News tab: filings, red flags, fund moves and upcoming results for the board stocks (every stock on the
    VCP, Bull flag and Bases screens, plus held / watched)."""
    from backend.ingest import news as nw
    return nw.board_news(S().config.alerts, days)


@router.get("/indices")
def indices():
    """The ticker bar: live quotes when logged in to Kite (cached ~25 s), else the last stored closes."""
    return S().ticker.snapshot(live=S().token_valid())


@router.get("/deals")
def deals_list(symbol: Optional[str] = None, days: int = Query(30, ge=1, le=120)):
    """One stock's bulk / block deals and holder disclosures with its signals, or the fetch status."""
    if symbol:
        sym = symbol.upper()
        since = (datetime.now(IST).date() - timedelta(days=days)).isoformat()
        return {"symbol": sym, "deals": hd.for_symbol(sym, since),
                "signals": hd.signals([sym], S().config.alerts).get(sym)}
    return {"last_fetch": hd.last_fetch()}


@router.post("/deals/refresh")
def deals_refresh():
    """Fetch the latest bulk / block deals and SAST disclosures now (alerts come from the scheduler)."""
    try:
        new = hd.refresh_from_nse()
    except Exception as exc:
        raise HTTPException(502, f"NSE deals fetch failed: {exc}")
    return {"new": len(new), "last_fetch": hd.last_fetch()}


@router.get("/events")
def events_list(symbol: Optional[str] = None):
    """The NSE event calendar: one stock's events (from 30 days ago), or the fetch status."""
    if symbol:
        since = (datetime.now(IST).date() - timedelta(days=30)).isoformat()
        return {"symbol": symbol, "events": ev.for_symbol(symbol.upper(), since)}
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM corp_events WHERE kind = 'results' AND date >= ?",
                         (now_ist()[:10],)).fetchone()[0]
    return {"last_fetch": ev.last_fetch(), "upcoming_results": n}


@router.post("/events/refresh")
def events_refresh():
    """Fetch the NSE results / board-meeting calendar now."""
    try:
        n = ev.refresh_from_nse()
    except Exception as exc:
        raise HTTPException(502, f"NSE event calendar fetch failed: {exc}")
    return {"stored": n, "last_fetch": ev.last_fetch()}


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
    from backend.engine import market
    with get_db() as conn:
        h = market.latest(conn, as_of) if as_of else None
    results = _results_for([p["symbol"] for p in items])
    news = _news_for([p["symbol"] for p in items])
    flags = hd.signals([p["symbol"] for p in items], S().config.alerts)
    for p in items:                                   # A/B badge, same rule as the alerts
        p["tier"] = market.tier(p, h["status"] if h else None, S().config.alerts)
        p["results"] = results.get(p["symbol"])
        p["news"] = news.get(p["symbol"])
        p["flags"] = flags.get(p["symbol"])
    if screen in ("vcp", "flag"):
        items.sort(key=lambda p: -(p.get("score") or 0))          # best setups first
    else:
        items.sort(key=STAGE_SORT[stage], reverse=(stage == "played_out"))
    from backend.ai import view                       # 🤖 AI view on the screen's rows
    return {"as_of": as_of, "stage": stage, "rows": view.attach(items, f"screen:{screen}")}


@router.get("/screens/{screen}/sparks")
def screens_sparks(screen: str, stage: str = Query(...), bars: int = Query(120, ge=20, le=400)):
    """Last `bars` daily candles (+ 50-day line) for every stock in a stage, for the grid view's mini charts.
    On the flag screen a card reaches back to just before the flag's pole (at most
    400 sessions)."""
    _check_screen(screen)
    if stage not in STAGE_SORT:
        raise HTTPException(422, f"stage must be one of {list(STAGE_SORT)}")
    tokens = {s["symbol"]: s["instrument_token"] for s in uni.resolve_universe(S().config.ingest)["stocks"]}
    out = {}
    with get_db() as conn:
        as_of = conn.execute("SELECT MAX(as_of) FROM scan_runs").fetchone()[0]
        rows = conn.execute("SELECT symbol, payload FROM scan_results WHERE as_of = ? AND screen = ? AND stage = ?",
                            (as_of, screen, stage)).fetchall()
        for row in rows:
            sym = row["symbol"]
            if sym not in tokens:
                continue
            p = json.loads(row["payload"])
            lip = ((p.get("flag") or {}).get("pole") or {}).get("start")
            cs = get_candles(conn, tokens[sym], limit=(400 if lip else bars) + 49)
            closes = [c["close"] for c in cs]
            ma = [round(sum(closes[i - 49:i + 1]) / 50, 2) if i >= 49 else None for i in range(len(cs))]
            n = bars
            if lip:
                n = min(400, max(bars, sum(1 for c in cs if c["date"] >= lip) + (20 if p.get("flag") else 10)))
            cs, ma = cs[-n:], ma[-n:]
            out[sym] = {"d": [c["date"] for c in cs], "o": [round(c["open"], 2) for c in cs],
                        "h": [round(c["high"], 2) for c in cs], "l": [round(c["low"], 2) for c in cs],
                        "c": [round(c["close"], 2) for c in cs], "ma50": ma}
    return {"as_of": as_of, "stage": stage, "bars": bars, "sparks": out}


@router.get("/market/health")
def market_health(days: int = Query(60, ge=1, le=500)):
    """Latest market status with its signals, and the daily status history."""
    from backend.engine import market
    with get_db() as conn:
        h = market.latest(conn)
        prev = market.before(conn, h["as_of"]) if h else None
        hist = market.history(conn, days)
    return {"health": h, "previous": prev, "history": hist, "config": asdict(S().config.market)}


class StopRequest(BaseModel):
    stop: float


@router.get("/positions")
def positions_list():
    """Positions the guard follows (synced from Kite holdings after the evening scan, or Run guard now)."""
    from backend.alerts import guard
    pos = guard.positions()
    syms = [p["symbol"] for p in pos]
    results, news, signals = _results_for(syms), _news_for(syms), hd.signals(syms, S().config.alerts)
    live = {}                                         # market hours: live price and guard state
    from backend.ingest.indices import market_status
    if pos and S().token_valid() and market_status(S().live.clock()) == "open":
        try:
            live = {p["symbol"]: p["live"] for p in S().live.snapshot(live=True)["positions"]}
        except Exception as exc:
            logger.info("Live positions: %s", exc)
    # `flags` stays the guard's alerts-sent record; red flags / fund moves go under `signals`
    return {"positions": [dict(p, results=results.get(p["symbol"]), news=news.get(p["symbol"]),
                               signals=signals.get(p["symbol"]), live=live.get(p["symbol"])) for p in pos]}


@router.get("/today")
def today_board():
    """The Today page: positions (live guard state), plan and near-pivot names with the entry checklist, market
    strip. Live quotes while there is a Kite login (one cached call), else the names only."""
    return S().live.snapshot(live=S().token_valid())


@router.get("/today/sparks")
def today_sparks():
    """Today's 5-minute closes for the Today page names (Kite history, cached 5 min)."""
    from backend.ingest.indices import market_status
    st = S()
    now = st.live.clock()
    if not st.token_valid() or market_status(now) == "holiday":
        return {"sparks": {}}
    positions, setups = st.live.names()
    return {"sparks": st.live.sparks([*positions, *setups], now)}


# --- Trend Breakout ---

@router.get("/breakout")
def breakout_summary():
    """Trend Breakout backtest: status, the settings, the latest run (without its trade lists) and the evening scan."""
    from dataclasses import asdict
    from backend.engine import trend_breakout as tb
    run = tb.saved(S().config)
    if run:
        run = {**run, "variants": {k: {f: x for f, x in v.items() if f not in ("trades", "stress_trades")}
                                   for k, v in run["variants"].items()}}
    scan = tb.saved_scan()
    if scan and scan.get("buys"):                     # 🤖 AI view on the morning's breakouts
        from backend.ai import view
        view.attach(scan["buys"], "plan")
    return {"built": bool(run), "progress": tb.status(), "kite": bool(S().client.has_token),
            "rules": asdict(S().config.breakout), "run": run, "scan": scan}


@router.get("/breakout/trades")
def breakout_trades(variant: str = "E", scenario: str = "normal", limit: int = 200, offset: int = 0):
    """The variant's trades, newest first: variant E (the only one; "A", removed in Nov 2026, is read as E), scenario
    normal / stress."""
    from backend.engine import trend_breakout as tb
    if scenario not in ("normal", "stress"):
        raise HTTPException(422, "scenario must be normal or stress")
    if variant == "A":
        variant = "E"
    if variant not in tb.MODES:
        raise HTTPException(422, f"variant must be one of {', '.join(tb.MODES)}")
    run = tb.saved() or {"variants": {}}
    rs = (run["variants"].get(variant) or {}).get("trades" if scenario == "normal" else "stress_trades", [])
    return {"total": len(rs), "rows": rs[offset:offset + max(1, min(limit, 1000))]}


@router.get("/attention")
def attention():
    """What needs a look on the Trend Breakout and Paper trading tabs (the blinking dots): level running / buy /
    sell / filter / warn per tab, with the reasons."""
    from backend.engine import attention as att
    return att.signals(S().config, bool(S().client.has_token))


@router.post("/breakout/run")
def breakout_run():
    """Re-run the Trend Breakout backtest and the evening scan in the background (stored daily candles only)."""
    from backend.engine import trend_breakout as tb
    st = S()
    if tb.status()["state"] != "idle":
        raise HTTPException(409, "The Trend Breakout backtest is already running")

    def go():
        tb.run(st.config)
        tb.run_scan(st.config, st.client)

    threading.Thread(target=go, name="breakout-run", daemon=True).start()
    return {"status": "started"}


@router.get("/breakout/scan")
def breakout_scan():
    """The latest evening scan: market filter, breakout buys, Kite holdings HOLD / SELL at open."""
    from backend.engine import trend_breakout as tb
    return tb.saved_scan() or {}


@router.get("/stock/{symbol}/everywhere")
def stock_everywhere(symbol: str):
    """One stock across the app: its stage on each screen (latest scan), open positions and past trades in every
    book, its Follow rows, the alerts sent about it."""
    from backend.engine import overview
    symbol = symbol.upper()
    cfg = S().config
    with get_db() as conn:
        as_of = conn.execute("SELECT MAX(as_of) FROM scan_runs").fetchone()[0]
        screens = {r[0]: {"stage": r[1], **{k: v for k, v in json.loads(r[2]).items()
                                            if k in ("pivot", "breakout_date", "dist_to_pivot_pct", "gain_from_pivot_pct", "rs", "score", "reason")}}
                   for r in conn.execute("SELECT screen, stage, payload FROM scan_results WHERE as_of = ? AND symbol = ?", (as_of, symbol))}
        follow = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM screen_follow WHERE symbol = ? ORDER BY breakout_date DESC", (symbol,))]
        alerts = [dict(r) for r in conn.execute("SELECT sent_at, kind, text, ok FROM sent_alerts WHERE symbol = ? ORDER BY sent_at DESC LIMIT 20", (symbol,))]
    try:                                            # "why not": the stage or the reason on each screen, live
        from backend.engine.scan import explain
        with get_db() as conn:
            row, _ = _resolve_stock(conn, symbol)
        for k, v in explain(symbol, row["instrument_token"], cfg.bases, cfg.screen, cfg.vcp, cfg.flag).items():
            screens[k] = {**screens.get(k, {}), "live_stage": v["stage"], "why": v["reason"], **({"pivot": v["pivot"]} if v.get("pivot") and "pivot" not in screens.get(k, {}) else {})}
    except HTTPException:
        pass
    return {"symbol": symbol, "as_of": as_of, "screens": screens,
            "positions": [p for p in overview.positions(cfg) if p["symbol"] == symbol],
            "trades": [t for t in overview.trades(cfg, 730) if t["symbol"] == symbol][:50],
            "follow": [{k: p.get(k) for k in ("breakout_date", "screens", "status", "entry_date", "entry", "qty", "stop", "pivot", "exit_date", "exit", "net", "pct", "reason")} for p in follow],
            "alerts": alerts}


@router.get("/export/trades.csv")
def export_trades(days: int = Query(3650, ge=1)):
    """⬇ Every closed trade of every book (Real, Paper E+) as one CSV."""
    import csv
    import io
    from backend.engine import overview
    rows = overview.trades(S().config, days)
    cols = ["book", "account", "strategy", "symbol", "side", "qty", "opened", "closed", "entry", "exit", "gross", "costs", "net", "pct", "reason"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerows([[t.get(c) for c in cols] for t in rows])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="trades_all_books.csv"'})


@router.get("/config-changes")
def config_changes():
    """The last 100 settings changes: when, which section, what went from what to what."""
    with get_db() as conn:
        return {"changes": json.loads(get_setting(conn, "config.changes") or "[]")}


@router.get("/backups")
def backups_list():
    from backend.engine import backup
    return {"backups": backup.listing(), "keep": backup.KEEP, "folder": str(backup.folder())}


@router.post("/backups/run")
def backups_run():
    from backend.engine import backup
    return backup.run()


@router.get("/maintenance")
def maintenance_status():
    """🧹 Archive and vacuum: {last_run, moved_by_table, live_mb, archive_mb, next_at, running, settings}."""
    from backend.database import archive
    return archive.status(S().config)


@router.post("/maintenance/archive")
def maintenance_archive():
    """🧹 Run the archive now (moves old log rows to data/archive.db, then VACUUM); 409 while one is running."""
    from backend.database import archive
    try:
        return archive.run(S().config)
    except archive.Busy as exc:
        raise HTTPException(409, str(exc))


@router.get("/real/reconcile")
def real_reconcile(days: int = Query(30, ge=1, le=365)):
    """💰 vs 📝: every paper fill of the real variant's account in the last `days` against the real order sent that
    day (sent / failed / missing), with the price difference and the retries."""
    from backend.engine import real
    return real.reconcile(S().config, days)


@router.get("/health")
def health():
    """🩺 Data freshness and failures, one check per source (the strip on Home and the Data page)."""
    from backend.database.models import get_setting
    from backend.engine import health as hl
    st = S()
    with get_db() as conn:
        follow_as_of = get_setting(conn, "follow.as_of")
    cs = hl.checks(st.config, st.token_valid(), follow_as_of)
    return {"at": now_ist(), "checks": cs, "red": sum(not c["ok"] for c in cs), "amber": sum(c["warn"] and c["ok"] for c in cs)}


# --- 🧩 the options book (paper only) ----------------------------------------------------------------------------

class OptionsSettings(BaseModel):
    paper: Optional[bool] = None
    real: Optional[bool] = None
    enabled: Optional[bool] = None
    vwap_exit: Optional[bool] = None
    reentry_same_bias: Optional[bool] = None          # 🧩 more entries: the same bias again after a stop
    reentry_late: Optional[bool] = None               # ... re-entries until reentry_until
    afternoon_sell: Optional[bool] = None             # ... the afternoon pair


class OptionsCapital(BaseModel):
    capital: float


@router.get("/options")
def options_summary():
    """🧩 The options book: switches, today's timeline, guards, margin, cards, the open spread, the bias steps, the chain,
    the rules, recent days, trades, alerts."""
    from backend.options import book as obook
    st = S()
    obook.start(st.config)                            # the page opened once: the book exists and ticks from now on
    threading.Thread(target=obook.refresh_view, args=(st.config, st.client), name="options-view", daemon=True).start()
    return obook.summary(st.config)


@router.put("/options/settings")
def options_settings(req: OptionsSettings):
    """paper / enabled / vwap_exit / reentry_same_bias / reentry_late / afternoon_sell; real=true is refused (400 "not
    wired": no webhook yet)."""
    from backend.options import book as obook
    try:
        return obook.set_switches(S().config, **req.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/options/kill")
def options_kill(on: bool = True):
    """The options kill switch: on, an open spread is closed at once."""
    from backend.options import book as obook
    st = S()
    return obook.set_killed(on, st.config, st.client)


@router.post("/options/close")
def options_close():
    """Close the open spread now (the page confirms first)."""
    from backend.options import book as obook
    st = S()
    try:
        return obook.close_now(st.config, st.client)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/options/hold")
def options_hold():
    """No new entries today (an open spread keeps its exits)."""
    from backend.options import book as obook
    return obook.hold_today(S().config)


@router.post("/options/capital")
def options_capital(req: OptionsCapital):
    """The options capital; after the start the change is money added / withdrawn."""
    from backend.options import book as obook
    if req.capital < 10_000:
        raise HTTPException(422, "capital must be at least Rs 10,000")
    try:
        return obook.set_capital(S().config, req.capital)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.get("/options/days")
def options_days(limit: int = Query(30, le=1000)):
    from backend.options import book as obook
    return {"rows": obook.days(limit)}


@router.get("/options/trades")
def options_trades(limit: int = Query(100, le=1000)):
    from backend.options import book as obook
    return {"rows": obook.trades(limit)}


@router.get("/options/asif")
def options_asif(days: int = Query(60, ge=1, le=1000)):
    """Report as if · exit rules: the closed spreads of the last `days` days replayed on their days' chain snapshots
    under the saved rule, 2× only, 2× + VWAP and 1.5× only (paper; VWAP exits kept at their real time, never invented)."""
    from backend.options import asif
    return asif.report(S().config, days)


@router.get("/options/chain")
def options_chain():
    """Today's option chain block (else the last snapshot read back)."""
    from backend.options import book as obook
    return obook.chain_view()


# --- 🤖 the local AI (infrastructure: switches, server, models, queue, test) -----------------------------------------

class AiSettings(BaseModel):
    enabled: Optional[bool] = None
    night_batch: Optional[bool] = None
    day_answers: Optional[bool] = None
    suggest_muted: Optional[str] = None          # 🤖 suggestions: comma list of kinds never born
    max_suggestions_open: Optional[int] = None   # 🤖 suggestions: the open cap (1-50)
    why: Optional[bool] = None                   # 🤖 why: the nightly lines under today's exits
    view_enabled: Optional[bool] = None          # 🤖 AI view (the three locked ones: 400 until the record unlocks them)
    view_rate_held: Optional[bool] = None
    view_rank_by_score: Optional[bool] = None
    view_size_by_score: Optional[bool] = None
    view_size_floor: Optional[float] = None
    view_exit_watch_alert: Optional[bool] = None
    view_score_buy: Optional[int] = None
    view_score_avoid: Optional[int] = None
    view_max_per_night: Optional[int] = None
    week: Optional[bool] = None                  # 📖 the Sunday observations about the closed week


@router.get("/ai")
def ai_summary():
    """🤖 The AI page: switches, server gauge, models (and a download's progress), breaker, queue, recent jobs, the
    last test, the settings, the service's limits and version."""
    from backend.ai import control
    return control.summary(S().config)


@router.put("/ai/settings")
def ai_settings(req: AiSettings):
    """enabled / night_batch / day_answers; enabled=true without both models is refused (400)."""
    from backend.ai import control
    flags = req.model_dump(exclude_none=True)
    try:
        from backend.ai import suggest                                      # 🤖 suggestions: its own keys first
        flags = suggest.apply_settings(S().config, flags)
        from backend.ai import view                                         # 🤖 AI view: the view_ keys (the lock rule)
        flags = view.apply_settings(S().config, flags)
        if "why" in flags:                                                  # 🤖 why
            S().config.ai.why = bool(flags.pop("why"))
        if "week" in flags:                                                 # 📖 the week's story
            S().config.ai.week = bool(flags.pop("week"))
        return control.set_switches(S().config, **flags)                   # the switches' shape; the rest is in GET /api/ai settings
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/ai/stop")
def ai_stop():
    """The full stop: every model unloaded, every queued job skipped, nothing runs until /ai/start."""
    from backend.ai import control
    return control.full_stop()


@router.post("/ai/start")
def ai_start():
    from backend.ai import control
    return control.start()


@router.post("/ai/pull")
def ai_pull():
    """Download the missing models in the background; the progress is in GET /api/ai models.pull."""
    from backend.ai import control
    try:
        return control.pull_models(S().config)
    except ValueError as exc:
        raise HTTPException(502, str(exc))


@router.post("/ai/test")
def ai_test():
    """A tiny JSON prompt on the day model through the queue; the reply and the time it took."""
    from backend.ai import control
    return control.test_prompt(S().config)


@router.get("/ai/jobs")
def ai_jobs(limit: int = Query(50, le=500)):
    from backend.ai import queue as aiq
    return {"rows": aiq.recent(limit)}


@router.get("/ai/notes")
def ai_notes(symbol: Optional[str] = None, kind: Optional[str] = None, severity: Optional[str] = None,
             limit: int = Query(50, le=500), offset: int = Query(0, ge=0)):
    """🤖 The AI's filing / deal notes, newest first: {rows, total}; filters symbol / kind (filing | deal) / severity."""
    from backend.ai import notes
    return notes.list_notes(symbol, kind, severity, limit, offset)


@router.get("/ai/notes/{note_id}")
def ai_note(note_id: int):
    from backend.ai import notes
    n = notes.get_note(note_id)
    if not n:
        raise HTTPException(404, "no such note")
    return n


@router.post("/ai/rerun")
def ai_rerun():
    """🤖 Queue tonight's whole batch again (after a fix or a deploy): what is already done is not repeated; the guard
    decides at run time. 409 while the AI is switched off."""
    if not S().config.ai.enabled:
        raise HTTPException(409, "AI is switched off")
    sch = S().scheduler
    if sch is None:
        raise HTTPException(409, "no scheduler")
    return sch.rerun_night(datetime.now(IST))


@router.post("/ai/notes/run")
def ai_notes_run():
    """🤖 Queue tonight's notes now (the guard decides whether they run); 409 while note jobs are queued or running."""
    from backend.ai import notes
    if notes.running():
        raise HTTPException(409, "notes are running")
    return notes.enqueue_tonight(S().config)


# 🤖 AI view

@router.get("/ai/view")
def ai_view_get(symbols: str = Query(...)):
    """🤖 The latest AI view per symbol (comma list): {views: {SYM: row with levels, candidates, change, previous}}."""
    from backend.ai import view
    syms = [x for x in symbols.split(",") if x.strip()][:200]
    return {"views": view.latest(syms)}


@router.get("/ai/view/record")
def ai_view_record(weeks: Optional[int] = Query(None, ge=1, le=520)):
    """🤖 The AI view record: by verdict vs the plan baseline, by week, the shuffle, the lean, the worst bucket, the
    unlock state; `weeks` back from today (every week by default)."""
    from backend.ai import view
    return view.record(weeks, score_buy=S().config.ai.view_score_buy)


@router.post("/ai/view/run")
def ai_view_run():
    """🤖 Queue tonight's AI view job now (the guard decides whether it runs); 409 while one is queued or running."""
    from backend.ai import view
    if view.running():
        raise HTTPException(409, "the AI view job is running")
    return view.enqueue_tonight(S().config)


@router.get("/ai/overnight")
def ai_overnight(day: Optional[str] = None):
    """✨ Overnight: the views of `day` (default: the latest day with ok views) that have a previous view, what moved
    and why: {day, rows: [{symbol, source, book, prev, now, delta, change, level, moved}], unchanged}. Code only."""
    from backend.ai import view
    if day and not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        raise HTTPException(400, "day must be YYYY-MM-DD")
    return view.overnight(day)


# 🤖 ask

class AskReq(BaseModel):
    question: str


@router.post("/ai/ask")
def ai_ask(req: AskReq):
    """🤖 Ask the app: the day model picks one read-only tool, code runs it, the model answers from its result; waits
    up to 60 s. {answer, tool, args, ms, id, ok, status, read}. 400 on an empty question or while ask is off."""
    from backend.ai import ask as aask
    c = S().config.ai
    if not (c.enabled and c.ask):
        raise HTTPException(400, "ask is off" if c.enabled else "AI switched off")
    try:
        return aask.ask(S().config, req.question, "page")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/ai/asks")
def ai_asks(limit: int = Query(10, le=200)):
    """🤖 The last asks, newest first: {rows}."""
    from backend.ai import ask as aask
    return {"rows": aask.list_asks(limit)}


# 🤖 suggestions

class SuggDismiss(BaseModel):
    reason: str = "none"


class SuggMute(BaseModel):
    kind: str
    muted: bool = True


@router.get("/ai/suggestions")
def ai_suggestions(status: Optional[str] = None, kind: Optional[str] = None, limit: int = Query(50, le=500)):
    """🤖 The suggestions, open first then newest: {rows, open, by_kind}. Filters status (open | accepted | dismissed |
    expired) and kind."""
    from backend.ai import suggest
    if status and status not in suggest.STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(suggest.STATUSES)}")
    rows = suggest.list_suggestions(status, limit, kind)
    sm = suggest.summary(S().config)
    return {"rows": rows, "open": sm["open"], "by_kind": sm["by_kind"]}


@router.get("/ai/suggestions/scorecard")
def ai_suggestions_scorecard(month: Optional[str] = None):
    """🤖 Per kind {made, open, accepted, dismissed, expired, helped_when_accepted, would_have_helped, scored, muted} and
    the totals {month, made, accepted, dismissed, expired, accepted_outcome_rs, dismissed_outcome_rs, best_kind}; `month`
    YYYY-MM (this month by default)."""
    from backend.ai import suggest
    if month and not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(400, "month must be YYYY-MM")
    return suggest.scorecard(month, S().config)


@router.post("/ai/suggestions/run")
def ai_suggestions_run():
    """🤖 Queue tonight's suggestion job now (the guard decides whether it runs); 409 while one is queued or running."""
    from backend.ai import suggest
    if suggest.running():
        raise HTTPException(409, "the suggestion job is running")
    return suggest.enqueue_tonight(S().config)


@router.post("/ai/suggestions/mute")
def ai_suggestions_mute(req: SuggMute):
    """🤖 Mute (or unmute) one kind: it is never born while muted. {muted: [kinds]}; 400 on an unknown kind."""
    from backend.ai import suggest
    try:
        return {"muted": suggest.mute(S().config, req.kind, req.muted)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/ai/suggestions/{sid}/accept")
def ai_suggestion_accept(sid: int):
    """🤖 Accept: the action runs through the same function as the hand action, once; the row is accepted. 404 when
    missing, 409 when already decided or expired, 400 when the action itself was refused (the row stays accepted)."""
    from backend.ai import suggest
    if not suggest.get(sid):
        raise HTTPException(404, "no such suggestion")
    try:
        return suggest.accept(sid, S().config, who="page")
    except suggest.SuggestionState as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/ai/suggestions/{sid}/dismiss")
def ai_suggestion_dismiss(sid: int, req: SuggDismiss = SuggDismiss()):
    """🤖 Dismiss with a reason (wrong | knew | later | none). 404 / 409 / 400 like accept."""
    from backend.ai import suggest
    if not suggest.get(sid):
        raise HTTPException(404, "no such suggestion")
    try:
        return suggest.dismiss(sid, req.reason, who="page")
    except suggest.SuggestionState as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/ai/suggestions/{sid}/later")
def ai_suggestion_later(sid: int):
    """🤖 Later: the expiry moves 4 hours on, once (409 the second time or once decided)."""
    from backend.ai import suggest
    if not suggest.get(sid):
        raise HTTPException(404, "no such suggestion")
    try:
        return suggest.later(sid)
    except suggest.SuggestionState as exc:
        raise HTTPException(409, str(exc))


@router.get("/ai/suggestions/{sid}")
def ai_suggestion(sid: int):
    from backend.ai import suggest
    s = suggest.get(sid)
    if not s:
        raise HTTPException(404, "no such suggestion")
    return s


# 🤖 why

@router.post("/ai/why/run")
def ai_why_run():
    """🤖 Queue tonight's "why" lines now (the guard decides whether they run); 409 while the job is queued or running."""
    from backend.ai import why
    if why.running():
        raise HTTPException(409, "the why job is running")
    return why.enqueue_tonight(S().config)


@router.post("/ai/testset/run")
def ai_testset_run():
    """🤖 Run the 30-filing test set on the night model in the background; 409 while a run is going."""
    from backend.ai import quality
    try:
        return quality.start(S().config)
    except quality.TestsetRunning as exc:
        raise HTTPException(409, str(exc))


@router.get("/corp-actions")
def corp_actions_list():
    """Splits / bonuses found in the daily candles (prices before each are adjusted everywhere)."""
    from backend.ingest import corp_actions
    syms = {s["instrument_token"]: s["symbol"] for s in uni.resolve_universe(S().config.ingest)["stocks"]}
    return {"actions": corp_actions.listing(syms)}


@router.get("/follow")
def follow_report():
    """📈 Follow: every fresh breakout bought blindly: totals, by screen, every position (one book, screen tags)."""
    from backend.engine import follow
    st = S()
    u = uni.resolve_universe(st.config.ingest)
    out = follow.report(next((i["instrument_token"] for i in u["indices"] if i["symbol"] == uni.INDEX_NAME), None))
    from backend.ai import view                       # 🤖 AI view on the Follow rows
    view.attach(out.get("rows") or [], "follow")
    return out


@router.post("/follow/rebuild")
def follow_rebuild():
    """Rebuild the book from the saved daily candles (background; the page shows the progress)."""
    from backend.engine import follow
    st = S()
    return follow.start_rebuild(st.config, uni.resolve_universe(st.config.ingest)["stocks"])


@router.put("/follow/settings")
def follow_settings(req: dict):
    """₹ per position (new entries from now; Rebuild to apply it to the whole history), and / or the saved
    "Try it as" selection (`defaults`: the page opens on it next time; {} clears it)."""
    from backend.engine import follow
    out = {}
    if "defaults" in req:
        if not isinstance(req["defaults"], dict):
            raise HTTPException(400, "defaults must be an object")
        out["defaults"] = follow.set_defaults(req["defaults"])
    if "per_position" in req:
        try:
            out["per_position"] = follow.set_per_position(float(req.get("per_position")))
        except (TypeError, ValueError):
            raise HTTPException(400, "per_position must be a number")
    return out


# --- ⏱ schedule and the status bar ---

@router.get("/schedule")
def schedule_view():
    """The status bar and ⏱ Next up: now (IST), the market, every scheduled job's next run and last run, Kite and
    real trading state."""
    from backend.engine import real
    from backend.engine import schedule as sc
    st = S()
    kite = bool(st.client.has_token)
    out = sc.schedule(st.config, st.scheduler, kite)
    out["kite"] = kite
    out["real"] = {"enabled": bool(st.config.real.enabled), "killed": real.killed(), "variant": st.config.real.variant,
                   "started": real.has_account()}
    return out


# --- 💰 Real trading (orders to Tradetron by webhook) ---

class RealSettings(BaseModel):
    enabled: Optional[bool] = None
    variant: Optional[str] = None


@router.get("/real")
def real_summary():
    """💰 Real trading: switches, the account, tomorrow's orders, positions, trades, the order log."""
    from backend.engine import real
    return real.summary(S().config)


@router.get("/real/kronos")
def real_kronos():
    """📈 Portfolio › Real: the candle model on the open positions (stop first · 3 d, up · 3 d while the gate shows them,
    else "shadow"), the code read on daily candles, the chip and the footer. Read-only; never acts on an order."""
    from backend.market import real_reads as rr
    return rr.table(S().config)


@router.post("/real/settings")
def real_settings(req: RealSettings):
    """Turn real trading on / off; saved with the settings. `variant` is kept for compatibility: only "E" (E+, the
    one variant) is accepted."""
    from backend.engine import trend_breakout as tb
    st = S()
    if req.variant is not None and req.variant not in tb.MODES:
        raise HTTPException(422, f"variant must be one of {', '.join(tb.MODES)}")
    if req.enabled is not None:
        st.config.real.enabled = req.enabled
    if req.variant is not None:
        st.config.real.variant = req.variant
    save_config(st.config)
    return {"enabled": st.config.real.enabled, "variant": st.config.real.variant}


class RealCapital(BaseModel):
    capital: float


@router.post("/real/capital")
def real_capital(req: RealCapital):
    """Set the real-trading capital. After the start, the change is money added / withdrawn: cash moves, the profit
    so far stays, and tomorrow's BUYs are re-sized (size per stock = account value / 10)."""
    from backend.engine import real
    if req.capital < 10_000:
        raise HTTPException(422, "capital must be at least Rs 10,000")
    st = S()
    try:
        res = real.set_capital(st.config, req.capital)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    save_config(st.config)
    return {**res, "replan": real.replan(st.config)}      # the new money re-planned at once (nothing is sent)


class Replan(BaseModel):
    max_pos: Optional[int] = None


def _set_max_pos(n: Optional[int]) -> None:
    """breakout.max_pos saved through the settings path (Settings shows it; the change is logged)."""
    if n is None:
        return
    if not 1 <= n <= 50:
        raise HTTPException(422, "max_pos must be between 1 and 50")
    st = S()
    before = st.config.breakout.max_pos
    st.config.update_section("breakout", {"max_pos": n})
    save_config(st.config)
    if before != n:
        with get_db() as conn:
            log = json.loads(get_setting(conn, "config.changes") or "[]")
            log.insert(0, {"at": now_ist(), "section": "breakout", "what": [f"max_pos: {before} → {n}"]})
            set_setting(conn, "config.changes", json.dumps(log[:100]))


@router.post("/real/replan")
def real_replan(req: Optional[Replan] = None):
    """↻ Re-evaluate the next open's real orders from the evening's data and the account as it is now; with
    `max_pos`, breakout.max_pos is saved first. Nothing is sent; sent orders are kept."""
    from backend.engine import real
    _set_max_pos(req.max_pos if req else None)
    return real.replan(S().config)


@router.post("/real/retry")
def real_retry(req: dict):
    """↻ Send a failed order of today's plan again (by hand, once per press; real trading on, market open)."""
    from backend.engine import real
    st = S()
    res = real.retry_order(st.config, st.client, datetime.now(IST).date().isoformat(), str(req.get("symbol", "")),
                           str(req.get("action", "BUY")))
    if res["status"] == "sent" and st.scheduler:
        try:
            from backend.alerts import messages as msg
            st.scheduler.notifier().send(f"{res['symbol']}|retry|{now_ist()}", "real",
                                         f"{msg.REAL} · ↻ MANUAL RETRY SENT · {res['action']} <b>{res['symbol']}</b> "
                                         f"{res['qty'] or ''} · ~₹{res['price']:,.2f}")
        except Exception:
            pass
    return res


# --- the morning dry run: today's plan with a per-order skip ---

class Skip(BaseModel):
    symbol: str
    on: bool = True
    note: str = ""


@router.get("/real/morning")
def real_morning():
    """☀️ The morning dry run: today's orders as they go out at send_time, each with its skip, results date and sent
    state, the clock to the send, the account facts. Read-only."""
    from backend.engine import real
    return real.morning(S().config)


@router.post("/real/skip")
def real_skip(req: Skip):
    """Skip one planned BUY for today (on) or send it after all (off). EXITs cannot be skipped (400). Nothing is sent;
    a skipped BUY is logged at the send as `skipped` and is not retried that day."""
    from backend.engine import real
    try:
        return real.skip_order(S().config, req.symbol, req.on, req.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/real/hold")
def real_hold():
    """Hold every planned BUY today (the EXITs still go out). The kill switch and the enable switch are not touched."""
    from backend.engine import real
    try:
        return real.hold_today(S().config)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/real/send-now")
def real_send_now():
    """Run the morning send at once (the confirm is on the page). Refused when off / killed, outside market hours or
    after send_until; otherwise exactly the scheduled send, recorded and alerted like it."""
    from backend.alerts import messages as msg
    from backend.alerts.jobs import Scheduler
    from backend.engine import real
    st = S()
    now = datetime.now(IST)
    started = now_ist()
    res = real.send_now(st.config, st.client, now)
    if res["status"] == "error":
        raise HTTPException(400, res["error"])
    Scheduler.record("real_send", now.date(), "ok", f"{res['status']}: sent {len(res['sent'])}, failed {len(res['failed'])}, "
                     f"skipped {len(res.get('skipped') or [])} · by hand (Send now)", started)
    if (res["sent"] or res["failed"]) and st.scheduler:
        try:
            st.scheduler.notifier().send(f"{now.date()}|real-sent", "real", msg.real_sent(res, ampm(now.strftime("%H:%M"))))
        except Exception:
            pass
    if res.get("gated") and st.scheduler:                 # the EXIT gate held an EXIT: the once-a-day alert
        try:
            st.scheduler._gate_alerts(now.date().isoformat(), res["gated"])
        except Exception:
            pass
    return {**res, "morning": real.morning(st.config)}


@router.post("/real/kill")
def real_kill(on: bool = True):
    """The kill switch: nothing is sent while it is on."""
    from backend.engine import real
    real.set_killed(on)
    return {"killed": real.killed()}


@router.post("/real/reset")
def real_reset():
    """Clear the real account's tracking (positions, trades, curve); the order log is kept. Nothing is sent."""
    from backend.engine import real
    real.reset()
    return {"status": "ok"}


# Corrections by hand: a fill the webhook could only estimate, a trade placed by hand, an order that never filled.
# None of these touches the enable switch or the kill switch; nothing is sent to the broker.

class PositionEdit(BaseModel):
    qty: Optional[float] = None
    entry: Optional[float] = None
    stop: Optional[float] = None
    note: str = ""


class PositionRemove(BaseModel):
    note: str = ""


class PositionAdd(BaseModel):
    symbol: str
    qty: float
    entry: float
    day: str
    stop: Optional[float] = None
    note: str = ""


class TradeEdit(BaseModel):
    exit: Optional[float] = None
    qty: Optional[float] = None
    note: str = ""


@router.post("/real/position/{symbol}")
def real_position_edit(symbol: str, req: PositionEdit, dry: bool = False):
    """✎ Correct an open position's qty / entry / stop; `?dry=1` returns the effects without saving."""
    from backend.engine import real
    try:
        return real.edit_position(S().config, symbol.upper(), req.qty, req.entry, req.stop, req.note, dry=dry)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/real/position/{symbol}")
def real_position_remove(symbol: str, req: Optional[PositionRemove] = None):
    """Drop a position from tracking without any order (a webhook BUY the broker never filled)."""
    from backend.engine import real
    try:
        return real.remove_position(S().config, symbol.upper(), (req.note if req else ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/real/position")
def real_position_add(req: PositionAdd):
    """Track a trade placed by hand (exited by the app's rules); the stop defaults to the strategy's rule."""
    from backend.engine import real
    try:
        return real.add_position(S().config, req.symbol.strip().upper(), req.qty, req.entry, req.day, req.stop, req.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/real/trade/{trade_id}")
def real_trade_edit(trade_id: int, req: TradeEdit, dry: bool = False):
    """✎ Correct a closed trade's exit price / qty; `?dry=1` returns the effects without saving."""
    from backend.engine import real
    try:
        return real.edit_trade(S().config, trade_id, req.exit, req.qty, req.note, dry=dry)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# 💰 Fill confirmation (after the 18 Nov incident) and the hand tools: none of these ever sends an order.

def _real_activity(text: str) -> None:
    """The Telegram activity line of a hand correction (never held; a failure never fails the request)."""
    st = S()
    if not st.scheduler:
        return
    try:
        from backend.alerts import messages as msg
        st.scheduler.notifier().send(f"real-hand|{now_ist()}|{text[:40]}", "real", msg.real_hand(text))
    except Exception:
        pass


class FillNote(BaseModel):
    note: str = ""


class PositionClose(BaseModel):
    price: float
    qty: Optional[float] = None
    at: Optional[str] = None
    note: str = ""


class TradeAdd(BaseModel):
    symbol: str
    qty: float
    entry: float
    entry_day: str
    exit: float
    exit_day: str
    note: str = ""


@router.post("/real/position/{symbol}/confirm")
def real_position_confirm(symbol: str, req: Optional[FillNote] = None):
    """✓ filled: you saw this BUY filled on the broker's side; the app then watches its stop and may send an EXIT."""
    from backend.engine import real
    try:
        res = real.confirm_position(S().config, symbol.upper(), "manual", req.note if req else "")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _real_activity(f"{symbol.upper()} confirmed filled by you")
    return res


@router.post("/real/confirm")
def real_confirm_now():
    """Check fills now: the read-only fill check (one call; skipped quietly without a key). Never sends an order."""
    from backend.engine import real
    return real.confirm_from_api(S().config)


@router.post("/real/position/{symbol}/close")
def real_position_close(symbol: str, req: PositionClose):
    """Sold outside the app: the position (or `qty` of it) closed at `price` with the usual charges, by hand."""
    from backend.engine import real
    try:
        res = real.close_position(S().config, symbol.upper(), req.price, req.qty, req.at, req.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _real_activity(res["text"])
    return res


@router.post("/real/trade")
def real_trade_add(req: TradeAdd):
    """A closed trade the app never saw (costs computed, by hand); the cash and the realised follow."""
    from backend.engine import real
    try:
        res = real.add_trade(S().config, req.symbol, req.qty, req.entry, req.entry_day, req.exit, req.exit_day, req.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _real_activity(res["text"])
    return res


@router.post("/real/trade/{trade_id}/void")
def real_trade_void(trade_id: int, req: Optional[FillNote] = None):
    """Mark a trade as never filled: kept, tagged void, out of the P&L, the win rate, the curves and the day summary."""
    from backend.engine import real
    try:
        res = real.void_trade(S().config, trade_id, req.note if req else "")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _real_activity(res["text"])
    return res


# --- Paper trading (simulated: never orders) ---

class PaperStart(BaseModel):
    capital: Optional[float] = None


@router.get("/paper")
def paper_summary():
    """📝 Paper trading: the account (before / after costs), curve, orders for the next open, positions, trades,
    paper vs backtest."""
    from backend.engine import paper
    return paper.summary(S().config)


@router.post("/paper/start")
def paper_start(req: PaperStart):
    """Start paper trading with `capital` (default: the paper capital setting)."""
    from backend.engine import paper
    if req.capital is not None and req.capital <= 0:
        raise HTTPException(422, "capital must be positive")
    r = paper.start(S().config, req.capital)
    if r["status"] == "started_already":
        raise HTTPException(409, "Paper trading is already running: reset it first")
    if r["status"] != "ok":
        raise HTTPException(400, r.get("error") or r["status"])
    return r


class PaperCapital(BaseModel):
    capital: float


@router.post("/paper/capital")
def paper_capital(req: PaperCapital):
    """Set the paper capital. After the start, the change is money added / withdrawn on every paper account: cash
    moves, the profit so far stays, and the next BUYs are sized from the new value."""
    from backend.engine import paper
    if req.capital < 10_000:
        raise HTTPException(422, "capital must be at least Rs 10,000")
    st = S()
    try:
        res = paper.set_capital(st.config, req.capital)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    save_config(st.config)
    return {**res, "replan": paper.replan(st.config)}


@router.post("/paper/replan")
def paper_replan(req: Optional[Replan] = None):
    """↻ Re-evaluate the paper account's next-open orders from the evening's data and the account as it is now; with
    `max_pos`, breakout.max_pos is saved first. Simulated: nothing is sent."""
    from backend.engine import paper
    _set_max_pos(req.max_pos if req else None)
    return paper.replan(S().config)


@router.post("/paper/reset")
def paper_reset():
    """Clear the paper account and its record."""
    from backend.engine import paper
    paper.reset()
    return {"status": "ok"}


@router.put("/positions/{symbol}")
def positions_set_stop(symbol: str, req: StopRequest):
    from backend.alerts import guard
    if req.stop <= 0:
        raise HTTPException(422, "stop must be positive")
    if not guard.set_stop(symbol, req.stop):
        raise HTTPException(404, f"Not following {symbol}")
    return {"symbol": symbol, "stop": req.stop}


@router.get("/plan")
def daily_plan():
    """The newest saved plan (tonight's ranked setups)."""
    from backend.alerts import plan as pl
    as_of, items = pl.latest_plan()
    from backend.ai import view                       # 🤖 AI view: the rating beside every plan name
    return {"as_of": as_of, "items": view.attach(items, "plan")}


def _heat_quote():
    """The market-data client's quote (the breakout opening check's), or None without a session. Never holdings,
    positions or orders."""
    st = S()
    return st.client.quote if st.client.has_token else None


@router.get("/plan/heat")
def plan_heat_view():
    """🔥 Live heat on the plan: one tile per plan name (state, price, the range bar's position, the AI levels and
    reasons); live quotes only while the market is open, else the last close (`live: false`). Display only."""
    from backend.engine import heat
    return heat.plan_heat(S().config, quote=_heat_quote())


@router.get("/today-card")
def today_card_view(at: Optional[str] = None):
    """✨ The Today card on Home: the phase by the clock, the headline, the one thing, orders / watch / books, the
    morning brief's lines (the only model text, tagged), the actions (labels only: nothing fires by itself), and the
    phase-aware tiles (`panel`). `at` (an ISO time) is a test clock, honoured only without the scheduler."""
    from backend.ai import today
    return today.today_card(S().config, today.clock(at) if at else None)


@router.get("/week")
def week_view(week: Optional[str] = None):
    """📖 The Week page: one ISO week (default the current one), Mon–Fri, by code from the trade and equity tables, with
    the AI's observations when written."""
    from backend.engine import week as wk
    try:
        return wk.week(week)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/week/write")
def week_write(week: str = Query(...)):
    """📖 "Write now": queue the week's story for that week (the guard decides whether it runs); 409 while it is queued
    or running or once the week has its story; 400 for a week that is not over yet."""
    from backend.ai import week_story
    from backend.engine import week as wk
    try:
        y, w = wk.parse(week)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    iso = f"{y}-W{w:02d}"
    if iso > week_story.closed_week(datetime.now(IST)):
        raise HTTPException(400, "the week is not over yet (it closes on Friday at 3:30 PM)")
    if week_story.running(iso):
        raise HTTPException(409, "the week's story is being written")
    if week_story.latest(iso):
        raise HTTPException(409, "this week's story is written")
    if not S().config.ai.week:
        raise HTTPException(400, "the week's story is off (Settings: ai.week)")
    return week_story.enqueue(S().config, iso, telegram=False)


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
    from backend.alerts.charts import CHARTS_AVAILABLE
    return {"bot_configured": n.client.configured, "chat_id": n.chat_id, "ready": n.ready,
            "charts_available": CHARTS_AVAILABLE,
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


@router.post("/alerts/login-link")
def alerts_login_link():
    """Send a one-time Kite login link to Telegram now."""
    from backend.alerts.dispatch import Notifier
    from backend.alerts.login import send_login_link
    if not S().config.kite.is_configured:
        raise HTTPException(400, "KITE_API_KEY / KITE_API_SECRET not set in .env")
    notifier = Notifier(S().config.alerts)
    if not notifier.ready:
        raise HTTPException(400, "Telegram not ready: set the bot token and chat ID first")
    if not send_login_link(S(), notifier, "requested", f"manual-login|{uuid.uuid4().hex}"):
        raise HTTPException(502, "Telegram send failed: see the Sent-alerts log")
    return {"status": "sent", "redirect_url": ll.redirect_url()}


@router.get("/alerts/log")
def alerts_log(limit: int = Query(100, le=500), kind: Optional[str] = None, symbol: Optional[str] = None,
               q: Optional[str] = None, days: int = Query(0, ge=0, le=365), failed: bool = False):
    """Every alert the app sent (all kinds: screens, intraday, guard, real orders, system), newest first, with
    filters: kind, symbol, free text, the last N days, failed only."""
    where, args = [], []
    if kind:
        where.append("kind = ?"); args.append(kind)
    if symbol:
        where.append("symbol = ?"); args.append(symbol.upper())
    if q:
        where.append("text LIKE ?"); args.append(f"%{q}%")
    if days:
        where.append("sent_at >= ?"); args.append((datetime.now(IST) - timedelta(days=days)).isoformat(timespec="seconds"))
    if failed:
        where.append("ok = 0")
    sql = "SELECT key, sent_at, kind, symbol, text, ok, error FROM sent_alerts" + (" WHERE " + " AND ".join(where) if where else "")
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY sent_at DESC LIMIT ?", (*args, limit))]
        kinds = [r[0] for r in conn.execute("SELECT DISTINCT kind FROM sent_alerts ORDER BY kind")]
    return {"rows": rows, "kinds": kinds}


@router.get("/jobs")
def jobs_list(limit: int = Query(50, le=500)):
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,))]
    return {"runs": rows, "config": {k: getattr(S().config.alerts, k) for k in
                                     ("job_enabled", "job_time", "reminder_time", "intraday", "intraday_start",
                                      "intraday_end", "intraday_interval_min", "close_check", "close_check_time",
                                      "universe_refresh_weekday",
                                      "login_links", "login_times", "login_heads_up", "login_heads_up_time")}}


@router.post("/jobs/{job}/run")
def jobs_run(job: str):
    sch = S().scheduler
    if sch is None or job not in ("eod", "intraday", "close_check", "weekly", "reminder", "login_check", "top_setups",
                                    "morning_plan", "gap_check", "guard", "options_tick"):
        raise HTTPException(404, "Unknown job or scheduler not running")
    now = datetime.now(IST)
    forced = {"eod": lambda: sch.run_eod(now, force=True), "top_setups": lambda: sch.run_top_setups(now, force=True),
              "guard": lambda: sch.run_guard()}
    fn = forced.get(job) or (lambda: getattr(sch, f"run_{job}")(now))

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


@router.get("/overview")
def overview_view():
    """🏠 Home: every account's figures, today's P&L across them, every open position, the latest activity."""
    from backend.engine import overview as ov
    st = S()
    return ov.overview(st.config, st.live.quotes if st.client.has_token else None)


@router.get("/portfolio")
def portfolio_view(days: int = 365):
    """💼 Portfolio: positions, trades, orders, daily P&L and curves of every account (the app's own; no broker holdings)."""
    from backend.engine import overview as ov
    st = S()
    return ov.portfolio(st.config, max(1, min(days, 3650)), st.live.quotes if st.client.has_token else None)


@router.get("/home/tips")
def home_tips_view():
    """🏠 Home · 💡 Tips · Kite: the two cards, the strip, the working Kite rows, today's calls, the checks, the record,
    the Tips times for Next up. Stored hints / kite JSON / tips_orders only: no quote call, no Kite call."""
    from backend.tips import home as th
    return th.home_tips(S().config)


# --- 💡 Tips: a Telegram channel's calls, decoded, tracked and scored (standalone; backend/tips) ---------------------

class TipsTake(BaseModel):
    lots: Optional[float] = None
    rupees: Optional[float] = None
    half: Optional[bool] = False


class TipsTrail(BaseModel):
    to: float


class TipsStop(BaseModel):
    value: float
    rev: Optional[int] = None                  # the stop's change counter as the page saw it (a stale "Set" is refused)


class TipsSymbol(BaseModel):
    symbol: str


class TipsStrike(BaseModel):
    """📷 the screenshot's strike ("Use screenshot") or keep: true ("Keep" the text's)."""
    strike: Optional[float] = None
    opt_type: Optional[str] = None
    symbol: Optional[str] = None
    keep: bool = False


class TipsLogin(BaseModel):
    phone: str


class TipsCode(BaseModel):
    code: str
    password: Optional[str] = None


class TipsTestPost(BaseModel):
    text: str
    channel: Optional[str] = None


def _tips_err(exc):
    raise HTTPException(exc.code, str(exc))


@router.get("/tips")
def tips_summary():
    """💡 The Tips page: reader, cards, hints (open + today + the last 20), feed (last 50 posts), record (30 d), the
    paper book, the settings."""
    from backend.tips import track as tt
    return tt.summary(S().config)


@router.get("/tips/hints")
def tips_hints(status: Optional[str] = None, days: Optional[int] = Query(None, ge=1, le=3650), channel: Optional[str] = None):
    from backend.tips import track as tt
    return {"rows": tt.hints_list(status, days, channel)}


@router.get("/tips/hints/{hid}")
def tips_hint(hid: int):
    from backend.tips import book as tb
    from backend.tips import track as tt
    h = tb.load(hid)
    if not h:
        raise HTTPException(404, f"no hint #{hid}")
    v = tt.hint_view(h, config=S().config)
    with get_db() as conn:
        v["quotes"] = [dict(r) for r in conn.execute("SELECT t, ltp, spot FROM tips_quotes WHERE hint_id = ? ORDER BY t", (hid,))]
        v["posts"] = [dict(r) for r in conn.execute("SELECT post_id, posted_at, kind, text FROM tips_posts WHERE hint_id = ? ORDER BY posted_at", (hid,))]
    return v


def _tips_act(fn, hid: int, *a, **kw):
    from backend.tips import book as tb
    from backend.tips import track as tt
    try:
        return tt.hint_view(fn(S().config, hid, *a, **kw), config=S().config)
    except tb.TipsError as exc:
        _tips_err(exc)


@router.post("/tips/hints/{hid}/take")
def tips_take(hid: int, req: Optional[TipsTake] = None):
    """Take a hint into the Tips paper book: {lots?|rupees?, half?}. 404 / 409 (taken, over, not tracked) / 400 (no plan,
    bad size)."""
    from backend.tips import book as tb
    req = req or TipsTake()
    return _tips_act(tb.take, hid, lots=req.lots, rupees=req.rupees, half=bool(req.half), who="page")


@router.post("/tips/hints/{hid}/skip")
def tips_skip(hid: int):
    from backend.tips import book as tb
    return _tips_act(tb.skip, hid, who="page")


@router.post("/tips/hints/{hid}/exit")
def tips_exit(hid: int):
    from backend.tips import book as tb
    return _tips_act(tb.exit_now, hid, who="page")


@router.post("/tips/hints/{hid}/symbol")
def tips_symbol(hid: int, req: TipsSymbol):
    """You name the index of a hint whose post did not: the contract is resolved and tracking starts (before a take)."""
    from backend.tips import book as tb
    return _tips_act(tb.set_symbol, hid, req.symbol, who="page")


@router.post("/tips/hints/{hid}/strike")
def tips_strike(hid: int, req: TipsStrike):
    """📷 A screenshot mismatch resolved: {strike, opt_type?, symbol?} uses the screenshot's contract (re-resolved, the
    plan rebuilt, logged); {keep: true} keeps the text's. 404 / 409 (no open mismatch, taken, filled on Kite) / 400."""
    from backend.tips import book as tb
    if req.keep:
        return _tips_act(tb.keep_strike, hid, who="page")
    return _tips_act(tb.set_strike, hid, req.strike, req.opt_type, req.symbol, who="page")


@router.post("/tips/hints/{hid}/trail")
def tips_trail(hid: int, req: TipsTrail):
    from backend.tips import book as tb
    return _tips_act(tb.trail, hid, req.to, who="page")


@router.post("/tips/hints/{hid}/stop")
def tips_set_stop(hid: int, req: TipsStop):
    """🛑 "Set stop…": your premium stop {value, rev?}. Before the fill it rewrites the plan (and an armed Kite entry's
    stop); after the fill it modifies the SL order on Kite (one modify, its guards). The channel's SL replaces it when
    it comes. 400 bad price / wrong side · 404 · 409 closed, a guard, Kite's refusal, or stale `rev`."""
    from backend.tips import book as tb
    from backend.tips import kite_orders as ko
    st = S()
    h0 = tb.load(hid)
    cl = ko.client_for(st.config, st, h0) if h0 else None
    return _tips_act(tb.set_stop, hid, req.value, who="page", rev=req.rev, client=cl)


@router.post("/tips/hints/{hid}/pause")
def tips_pause(hid: int):
    from backend.tips import book as tb
    return _tips_act(tb.pause, hid, True, who="page")


@router.post("/tips/hints/{hid}/resume")
def tips_resume(hid: int):
    from backend.tips import book as tb
    return _tips_act(tb.pause, hid, False, who="page")


# --- ⚡ Tips → Kite orders (by your click, each through a confirm; backend/tips/kite_orders.py) ---

class TipsKiteKill(BaseModel):
    on: bool


class TipsKiteAct(BaseModel):
    lots: Optional[int] = None
    leg: Optional[str] = None
    to: Optional[Union[float, str]] = None


KITE_ACTIONS = ("entry", "stop", "all", "rest", "exit", "cancel", "modify_stop", "send_now", "add")      # targets: watched by the app, never placed


@router.get("/tips/kite")
def tips_kite():
    """Today's Kite orders of the Tips (ours only), the caps, the guards, the mode and the kill switch."""
    from backend.tips import kite_orders as ko
    st = S()
    return ko.today(st.config, st.client)


@router.post("/tips/kite/kill")
def tips_kite_kill(req: TipsKiteKill):
    """The kill switch: on = every working order of ours cancelled and every open quantity exited at market."""
    from backend.tips import kite_orders as ko
    st = S()
    return {**ko.set_kill(st.config, st.client, req.on), **ko.today(st.config, st.client)}


@router.post("/tips/kite/resync")
def tips_kite_resync():
    """Read the Kite order book now (ours only) and apply what changed."""
    from backend.tips import kite_orders as ko
    st = S()
    try:
        r = ko.sync_all(st.config, st)
    except Exception as exc:
        raise HTTPException(502, f"Kite order book: {str(exc)[:160]}")
    return {**r, **ko.today(st.config, st.client)}


@router.get("/tips/kite/book")
def tips_kite_book(days: Optional[int] = Query(None, ge=1, le=3650)):
    """⚡ The Kite ledger (the mode's book: real, or the dry run while kite_mode is dry)."""
    from backend.tips import kite_book as kb
    return kb.view(S().config, days)


@router.post("/tips/kite/book/backfill")
def tips_kite_book_backfill():
    """⚡ The Kite ledger: closed Kite hints without their row get one (never updates or deletes a row)."""
    from backend.tips import kite_book as kb
    st = S()
    added = kb.backfill(st.config)
    return {"added": added, **kb.view(st.config)}


@router.post("/tips/kite/dry/clear")
def tips_kite_dry_clear():
    """"Clear dry run": today's dry rows voided (never deleted), the simulator's book emptied, the dry hints' legs reset."""
    from backend.tips import kite_orders as ko
    st = S()
    return {**ko.clear_dry(st.config), **ko.today(st.config, st.client)}


@router.post("/tips/kite/dry/rehearse")
def tips_kite_dry_rehearse():
    """Dry run only: a made-up NIFTY CE hint in channel "rehearsal" to press Place all on. Nothing reaches Kite."""
    from backend.tips import kite_orders as ko
    st = S()
    return ko.rehearsal_hint(st.config, st.client, st)


@router.post("/tips/kite/daycheck")
def tips_kite_daycheck():
    """The Kite day check now (it runs by itself at 3:35 PM on trading days): today's orders against the hints."""
    from backend.tips import kite_check as kc
    from backend.tips import kite_orders as ko
    st = S()
    res = kc.day_check(st.config, client=st.client)
    return {"result": res, **ko.today(st.config, st.client)}


@router.post("/tips/kite/test")
def tips_kite_test():
    """The connection test on the real Kite account: one LIMIT buy of a far OTM NIFTY PE at half its price, cancelled
    5 s later (session, static IP, order permission). 409: mode off, no login, or a test in the last 5 minutes."""
    from backend.tips import book as tb
    from backend.tips import kite_orders as ko
    st = S()
    try:
        res = ko.connection_test(st.config, st.client)
    except tb.TipsError as exc:
        _tips_err(exc)
    return {"result": res, **ko.today(st.config, st.client)}


@router.post("/tips/hints/{hid}/kite/{action}")
def tips_kite_action(hid: int, action: str, req: Optional[TipsKiteAct] = None):
    """entry {lots?} · all {lots?} (entry now, the stop on the fill, the targets watched by the app) · rest (the stop when
    missing + the watch) · stop · exit · cancel {leg} ·
    modify_stop {to: price | "cost"}. 404 no hint · 409 a guard (its text) or Kite's refusal · 400 bad input."""
    from backend.tips import book as tb
    from backend.tips import kite_orders as ko
    from backend.tips import track as tt
    if action not in KITE_ACTIONS:
        raise HTTPException(404, f"no Kite action {action}")
    req = req or TipsKiteAct()
    st = S()
    c = st.config
    cl = ko.client_for(c, st, tb.load(hid))          # the simulator in a dry run (or for a dry position), else Kite
    try:
        if action == "entry":
            h = ko.place_entry(c, cl, hid, req.lots)
        elif action == "all":
            h = ko.place_all(c, cl, hid, req.lots)
        elif action == "rest":
            h = ko.place_rest(c, cl, hid)
        elif action == "stop":
            h = ko.place_stop(c, cl, hid)
        elif action == "exit":
            h = ko.exit_now(c, cl, hid)
        elif action == "cancel":
            h = ko.cancel(c, cl, hid, req.leg or "")
        elif action == "send_now":
            h = ko.send_now(c, cl, hid)
        elif action == "add":                        # 📏 the channel's one add (tips.kite_follow_adds on), your click
            h = ko.place_add(c, cl, hid)
        else:
            if req.to is None:
                raise HTTPException(400, 'modify_stop needs {to: price | "cost"}')
            h = ko.modify_stop(c, cl, hid, req.to)
        if ko.is_dry(cl):                            # a rehearsal fills at once from the last quote: show it now
            ko.sync(c, cl)
            h = tb.load(hid) or h
    except tb.TipsError as exc:
        _tips_err(exc)
    return tt.hint_view(h, config=c)


@router.get("/tips/ai/weekly")
def tips_ai_weekly(week: Optional[str] = None, channel: Optional[str] = None):
    """🤖 Research › Tips › Track: the weekly channel profile (the week picker: the last 8 weeks)."""
    from backend.tips import ai4
    return ai4.weekly_page(S().config, week, channel)


@router.get("/market/now")
def market_now(index: str = "NIFTY"):
    """📈 Research › Tips (Calls): the "Market now" panel for one index (code tiles, the candle model gated, the [AI]
    backdrop). Read-only; never acts on an order."""
    from backend.market import view as mv
    st = S()
    return mv.panel(st.config, index, client=st.client)


@router.get("/market/scorecard")
def market_scorecard():
    """📈 Settings › market / ai: the candle model's scorecard, the cards gate, the switches and the [AI] read."""
    from dataclasses import asdict
    from backend.market import reads as mr
    from backend.market import words as mw
    st = S()
    sc = mr.scorecard()
    return {"rows": sc, "gate": mr.gate(st.config, sc), "settings": {k: v for k, v in asdict(st.config.market).items()
            if k in ("kronos", "kronos_show", "interval_s", "horizon_min", "samples")}, "ai": mw.score_view(),
            "worker": mr.health(st.config, 0.3) if mr.on(st.config) else {"state": "off"}, "version": mr.VERSION,
            "gate_rule": f"cards only when NIFTY and BANKNIFTY 30-min direction ≥ {mr.GATE_PCT:g} % over {mr.GATE_READS}+ scored reads"}


@router.get("/market/status")
def market_status_view():
    """📈 System › AI · Candle model: the worker's state, the last read, today's counts, the mode and the gate, the
    settings (read-only). Cheap: in-memory state, one count query, /health with a 1 s timeout cached 30 s."""
    from backend.market import reads as mr
    st = S()
    return mr.model_status(st.config, getattr(st, "market", None))


class MarketReadNow(BaseModel):
    symbol: str = "NIFTY"


@router.post("/market/read")
def market_read_now(req: MarketReadNow):
    """📈 System › AI "Read now": one index read through the market read service (one a minute; 409 while the model
    is off, the worker is not ready or a pass runs). Reads stored candles and stores a market_reads row; it never
    touches an order."""
    from backend.market import reads as mr
    st = S()
    svc = getattr(st, "market", None)
    if svc is None:
        if not mr.on(st.config):
            raise HTTPException(409, "the candle model is off (Settings › market · kronos)")
        raise HTTPException(409, "the market read service is not running")
    out = svc.read_now((req.symbol or "NIFTY").upper())
    if not out.get("ok"):
        raise HTTPException(out.get("code") or 409, out.get("why") or "not read")
    return {**out, "status": mr.model_status(st.config, svc)}


class TipsWeeklyNow(BaseModel):
    week: Optional[str] = None


@router.post("/tips/ai/weekly/write")
def tips_ai_weekly_write(req: Optional[TipsWeeklyNow] = None):
    """🤖 "Write now": queue the week's profile for every channel (one per hour; 429 before that). Never acts on an order."""
    from datetime import date as _date
    from backend.tips import ai4
    st = S()
    try:
        mon = ai4.week_of(_date.fromisoformat(req.week)) if req and req.week else None
    except ValueError:
        raise HTTPException(422, "week: YYYY-MM-DD")
    if not st.config.tips.ai_weekly:
        raise HTTPException(409, "the weekly profile is off (Settings › tips · ai_weekly)")
    if not st.config.ai.enabled:
        raise HTTPException(409, "the local AI is off")
    try:
        return ai4.weekly_now(st.config, mon)
    except ValueError as exc:
        raise HTTPException(429, str(exc))


@router.get("/tips/feed")
def tips_feed(kind: Optional[str] = None, limit: int = Query(50, ge=1, le=1000), channel: Optional[str] = None):
    from backend.tips import track as tt
    return {"rows": tt.feed_rows(kind, limit, channel)}


@router.get("/tips/record")
def tips_record(days: int = Query(30, ge=1, le=3650), channel: Optional[str] = None):
    from backend.tips import track as tt
    return tt.record(days, channel)


@router.put("/tips/settings")
def tips_settings(values: dict):
    """TipsConfig keys (422 on an unknown key or a bad value). The reader restarts when enabled / channels change."""
    from backend.tips import reader as rd
    st = S()
    before = asdict(st.config.tips)
    if "channels" in values:
        chans = rd.split_channels(values["channels"])
        if not chans or any(not re.fullmatch(r"[A-Za-z0-9_ ,.&'()+-]{3,80}", c) for c in chans):
            raise HTTPException(422, "channels: one per line · a channel's username, or a private channel's title or numeric id")
        values["channels"] = ";".join(chans)
    try:
        out = st.config.update_section("tips", values)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc))
    save_config(st.config)
    what = [f"{k}: {before[k]} → {out[k]}" for k in out if before.get(k) != out[k]]
    if what:
        with get_db() as conn:
            log = json.loads(get_setting(conn, "config.changes") or "[]")
            log.insert(0, {"at": now_ist(), "section": "tips", "what": what})
            set_setting(conn, "config.changes", json.dumps(log[:100]))
    if before["enabled"] != out["enabled"] or before["channels"] != out["channels"]:
        try:
            if rd.READER is not None and rd.READER.running and before["channels"] != out["channels"] and out["enabled"]:
                rd.READER.restart()
            else:
                rd.ensure(st)
        except Exception:
            logger.exception("Tips reader restart failed")
    return out


@router.post("/tips/login")
def tips_login(req: TipsLogin):
    """Step 1: Telegram sends a code to your app. 400 without TIPS_TG_API_ID / TIPS_TG_API_HASH or a bad phone."""
    from backend.tips import reader as rd
    try:
        return rd.LOGIN.start(req.phone)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, rd._reason(exc))


@router.post("/tips/login/code")
def tips_login_code(req: TipsCode):
    """Step 2: the code (and the 2-step password when asked). The session string goes to .env; the reader starts when
    tips.enabled is on."""
    from backend.tips import reader as rd
    try:
        r = rd.LOGIN.code(req.code, req.password)
    except LookupError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if r.get("ok"):
        try:
            rd.ensure(S())
        except Exception:
            logger.exception("Tips reader start after login failed")
    return r


def _multipart_files(body: bytes, ctype: str) -> list[tuple[str, bytes]]:
    """The file parts of a multipart/form-data body (the stdlib email parser; no extra dependency)."""
    from email.parser import BytesParser
    from email.policy import HTTP
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body)
    out = []
    for part in msg.iter_parts():
        name = part.get_filename() or part.get_param("name", header="content-disposition") or "file"
        out.append((str(name), part.get_payload(decode=True) or b""))
    return out


@router.post("/tips/import")
async def tips_import(request: Request, channel: Optional[str] = None):
    """The Telegram HTML export (multipart files messages.html, messages2.html, ...; or one HTML body): every post with
    its kind, hints decoded and planned (old ones expire untracked), follow-ups linked. Never pushes alerts."""
    from starlette.concurrency import run_in_threadpool
    from backend.tips import reader as rd
    ctype = request.headers.get("content-type") or ""
    body = await request.body()
    if not body:
        raise HTTPException(400, "no file")
    files = _multipart_files(body, ctype) if ctype.startswith("multipart/") else [("messages.html", body)]
    files = [(n, b) for n, b in files if b and b"message" in b]
    if not files:
        raise HTTPException(400, "no Telegram HTML export in the upload")
    try:
        return await run_in_threadpool(rd.import_export, S().config, files, channel)
    except Exception as exc:
        logger.exception("Tips import failed")
        raise HTTPException(400, f"import failed: {str(exc)[:160]}")


@router.post("/tips/test-post")
def tips_test_post(req: TipsTestPost):
    """The page's "try a post" box: a dry sort + decode + plan (no save, no model, no alert)."""
    from backend.tips import decode as dc
    from backend.tips import filter as flt
    from backend.tips import plan as pl
    from backend.tips import track as tt
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "paste a post")
    now = datetime.now(IST)
    kind = flt.code_kind(text, False)
    d = dc.decode(text, now)
    is_hint = kind is None and dc.looks_like_hint(d, text)
    out = {"kind": kind or ("hint" if is_hint else "follow_up?" if flt.FOLLOW_CODE.search(text) else "chatter"),
           "by": "code", "decoded": None, "plan": None, "followup": None}
    if is_hint:
        h = {k: d.get(k) for k in ("symbol", "side", "instrument", "strike", "opt_type", "expiry", "expiry_src", "entry", "stop",
                                   "targets", "open_ended", "size_hint", "decoded")}
        h["exchange"] = tt.exchange_of(h)
        day, _ = pl.plan_day(now, h["exchange"])
        tt.resolve(h, None, day)
        h["plan"] = pl.build(h, S().config.tips, now)
        h["id"] = 0
        from backend.alerts import messages as msg
        out.update(decoded={**h, "title": dc.title(h), "entry_text": pl.entry_text(h["entry"]), "stop_text": pl.stop_text(h["stop"]),
                            "targets_text": pl.targets_text(h["targets"], bool(h["open_ended"]))},
                   plan=h["plan"], telegram=re.sub(r"<[^>]+>", "", msg.tips_hint_body(h)))
    else:
        out["followup"] = pl.followup_action(text)
    return out


@router.get("/today-pnl")
def today_pnl_view():
    """₹ Today (the status bar chip): Kite (live) + real in the total; paper and a dry run shown, not counted."""
    from backend.engine.today_pnl import today_pnl
    return today_pnl(S().config)


# --- ₿ BTC pivot patterns (alerts only: nothing here places, sizes or books an order) ---------------------------------

def crypto_summary():
    from backend.crypto import view as cv
    return cv.summary(S().config, S().crypto)


@router.get("/crypto")
def crypto_view():
    """₿ The Strategies › ₿ BTC patterns page: the feed, the pivots, the alert hours, the detections, the settings."""
    return crypto_summary()


@router.get("/crypto/candles")
def crypto_candles(tf: Optional[str] = None, start: Optional[int] = Query(None, alias="from"), end: Optional[int] = Query(None, alias="to"),
                   range: str = "3d"):
    """₿ The chart's candles from the store (time shifted to IST), the EMA, the pivot segments, the detections."""
    from backend.crypto import view as cv
    return cv.candles(S().config, tf, start, end, range)


@router.get("/crypto/scores")
def crypto_scores(days: int = Query(30), counted: int = Query(1)):
    """₿ The tiles' numbers for the last 1 | 7 | 30 days: direction, by grade, the forecast rows and their gate, by pattern
    and level, the candle model's BTC chip (the same object as GET /api/crypto's "scores")."""
    from backend.crypto import view as cv
    if days not in (1, 7, 30):
        raise HTTPException(400, "days must be 1, 7 or 30")
    return cv.scores(S().config, days, counted_only=bool(counted))


@router.get("/crypto/forecast_now")
def crypto_forecast_now(horizon: int = Query(30)):
    """₿ "Forecast now": the candle model's latest per-candle read (30 | 60 min) kept in memory by the service thread,
    the BTC scorecard chip, the next candle close, stale. Reads the cache only: the worker is never called from here."""
    from backend.crypto import view as cv
    if horizon not in (30, 60):
        raise HTTPException(400, "horizon must be 30 or 60")
    return cv.forecast_now(S().config, horizon)


@router.get("/crypto/signals")
def crypto_signals(limit: int = Query(300, ge=1, le=5000)):
    from backend.crypto import signals as sg
    from backend.crypto import view as cv
    c = S().config.crypto
    return {"signals": [cv.row_view(r, int(c.score_after_min)) for r in sg.rows("symbol = ?", (c.symbol,), limit=limit)]}


@router.get("/crypto/snap/{sid}.png")
def crypto_snap(sid: int):
    """₿ A detection's chart snapshot (the PNG sent with the alert; an archived one from snaps/archive)."""
    from backend.crypto import signals as sg
    r = sg.load(sid)
    p = Path(r["png_path"]) if r and r.get("png_path") else None
    if p and not p.is_file():
        p = p.parent / "archive" / p.name
    if not p or not p.is_file():
        raise HTTPException(404, "no snapshot for this detection")
    return Response(p.read_bytes(), media_type="image/png", headers={"Cache-Control": "no-cache"})


class CryptoQuiet(BaseModel):
    hours: Optional[float] = None          # None / 0 = resume


@router.post("/crypto/quiet")
def crypto_quiet(req: CryptoQuiet):
    """₿ Pause the alerts for N hours (detections are stored "quiet" and listed in the digest), or resume."""
    from backend.crypto import signals as sg
    if req.hours is not None and not (0 <= float(req.hours) <= 72):
        raise HTTPException(400, "hours must be 0 … 72")
    q = sg.set_quiet(req.hours or None)
    return {"quiet_until": q.isoformat(timespec="seconds") if q else None, "quiet_label": ampm(q) if q else None,
            "text": sg.hours_text(S().config)}


@router.post("/crypto/scan")
def crypto_scan():
    """₿ Mark the patterns in the stored candles of the last crypto.scan_days on the chart (nothing is sent)."""
    from backend.crypto import signals as sg
    st = S()
    if not st.config.crypto.enabled:
        return {"ok": False, "why": "the feed is off (crypto.enabled)"}
    r = sg.scan_history(st.config)
    return {"ok": True, **r}


@router.post("/crypto/refresh")
def crypto_refresh():
    """₿ Wake the feed now (it fetches the last candles on its next pass); off: says so."""
    st = S()
    if not st.config.crypto.enabled:
        return {"ok": False, "why": "the feed is off (crypto.enabled)"}
    if st.crypto is not None:
        st.crypto.poke()
    return {"ok": True}


# --- 📡 one live connection: the page subscribes to topics; each is sent when it changes ---------------------

def _stream_topics() -> dict:
    """topic -> (producer, seconds until the next look given the last value). Read-only, the same data as the
    matching GET endpoints."""
    return {
        "schedule": (schedule_view, lambda d: 20),
        "attention": (attention, lambda d: 30),
        "kite": (lambda: kite_status(False), lambda d: 60),
        "overview": (overview_view, lambda d: 30),
        "real_morning": (lambda: __import__("backend.engine.real", fromlist=["morning"]).morning(S().config),
                         lambda d: 30 if (d or {}).get("visible") and (d or {}).get("phase") != "after" else 120),
        "options": (options_summary, lambda d: 15 if (d or {}).get("open") or __import__("backend.engine.overview", fromlist=["market_open"]).market_open() else 120),
        "today_card": (today_card_view, lambda d: 60),
        "plan_heat": (plan_heat_view, lambda d: 60 if (d or {}).get("market_open") else 600),
        "tips": (tips_summary, lambda d: 5 if (d or {}).get("any_open") else 60),
        "home_tips": (home_tips_view, lambda d: 5 if (d or {}).get("live") else 30),
        "today_pnl": (today_pnl_view, lambda d: 5 if (d or {}).get("open") else 60),
        "crypto": (crypto_summary, lambda d: 10 if (d or {}).get("enabled") else 60),
        "ai": (ai_summary, lambda d: 5 if ((d or {}).get("models") or {}).get("pull", {}).get("running") or ((d or {}).get("queue") or {}).get("job") else 60),
    }


@router.get("/stream")
async def stream(request: Request, topics: str = "schedule,attention,kite", once: bool = False):
    """Server-sent events: `event: <topic>` with its JSON, whenever it changes (checked on each topic's own
    interval: faster while a book is open). One connection replaces the pages' timers. once=1:
    every topic once, then the stream ends (tests)."""
    import asyncio
    import time as _t
    from fastapi.responses import StreamingResponse
    from starlette.concurrency import run_in_threadpool
    prod = _stream_topics()
    wanted = [t for t in dict.fromkeys(topics.split(",")) if t in prod]

    async def gen():
        last, due, beat = {}, {t: 0.0 for t in wanted}, _t.monotonic()
        yield "retry: 3000\n\n"
        while True:
            now = _t.monotonic()
            for t in wanted:
                if now < due[t]:
                    continue
                try:
                    data = await run_in_threadpool(prod[t][0])
                except Exception as exc:                       # a topic failing never closes the stream
                    data = {"error": str(exc)[:200]}
                s = json.dumps(data, default=str)
                if s != last.get(t):
                    last[t] = s
                    yield f"event: {t}\ndata: {s}\n\n"
                due[t] = now + prod[t][1](data)
            if once or await request.is_disconnected():
                return
            if now - beat > 15:                                 # keeps proxies from closing a quiet stream
                beat = now
                yield ": beat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
