"""REST API: Kite auth, config, universe, ingest jobs, candles, logs."""

import json
import os
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from backend.timefmt import ampm_date
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


@router.put("/config/{section}")
def put_config(section: str, values: dict):
    if section not in PERSISTED_SECTIONS:
        raise HTTPException(404, f"Unknown section; have {list(PERSISTED_SECTIONS)}")
    from dataclasses import asdict
    before = asdict(S().config.section(section))
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
    return {"as_of": as_of, "stage": stage, "rows": items}


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
    return {"built": bool(run), "progress": tb.status(), "kite": bool(S().client.has_token),
            "rules": asdict(S().config.breakout), "run": run, "scan": tb.saved_scan()}


@router.get("/breakout/trades")
def breakout_trades(variant: str = "A", scenario: str = "normal", limit: int = 200, offset: int = 0):
    """A variant's trades, newest first: variant A / E, scenario normal / stress."""
    from backend.engine import trend_breakout as tb
    if scenario not in ("normal", "stress"):
        raise HTTPException(422, "scenario must be normal or stress")
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


# --- ⚡ Intraday Flow (signals only) ---

class FlowMode(BaseModel):
    mode: str


@router.get("/flow")
def flow_view():
    """The canvas: every box (settings, schema, live state), today's signals, the feed."""
    from backend.intraday import flow
    return flow.state(S().config)


@router.put("/flow/node/{nid}")
def flow_node(nid: str, values: dict):
    """Change one box's settings (applies from the next candle)."""
    from backend.intraday import flow
    try:
        return flow.set_node(nid, values)
    except KeyError:
        raise HTTPException(404, f"No box {nid}")


@router.post("/flow/mode")
def flow_mode(req: FlowMode):
    if req.mode not in ("off", "signals", "paper"):
        raise HTTPException(422, "mode must be off, signals or paper")
    st = S()
    st.config.flow.mode = req.mode
    save_config(st.config)
    return {"mode": req.mode}


def _flow_runner():
    from backend.intraday import feed
    if feed.RUNNER is None:
        feed.RUNNER = feed.Runner(S())
    return feed.RUNNER


def _flow_notify():
    sch = S().scheduler
    return sch._flow_alert if sch else None


@router.post("/flow/stop")
def flow_stop():
    """⏹ Stop all intraday activity: paper positions squared off at the last price, live feed closed, no runs or
    signals until ▶ start (kept across restarts and days)."""
    return _flow_runner().stop_all(datetime.now(IST), _flow_notify())


@router.post("/flow/start")
def flow_start():
    _flow_runner().start_all(_flow_notify())
    return {"stopped": False}


def _costs(slip, gov, brk_pct, brk_rs, capital):
    if brk_pct is None and brk_rs is None and not capital and slip and gov:
        return None
    return {"slip": slip, "gov": gov, "brk_pct": max(0.0, brk_pct or 0), "brk_rs": max(0.0, brk_rs or 0),
            "capital": capital if capital and capital >= 1000 else None}


@router.get("/flow/backtest/csv")
def flow_backtest_csv(setup: str, period: str, slip: bool = True, gov: bool = True, brk_pct: Optional[float] = None,
                      brk_rs: Optional[float] = None, capital: Optional[float] = None):
    """⬇ Every trade of a result as CSV (one row a trade, partial and final exits, each cost apart)."""
    import csv
    import io
    from fastapi.responses import Response
    from backend.intraday import backtest as bt
    rows, m = bt.csv_rows(setup, period, _costs(slip, gov, brk_pct, brk_rs, capital))
    if not m:
        raise HTTPException(404, "No saved result for that setup and period (re-run it)")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"# {setup} · {period} · capital {m['capital']:.0f} · slippage {'on' if m['slip'] else 'off'} · brokerage "
                f"{m['brk_pct']}% / Rs {m['brk_rs']} per order · govt charges {'on' if m['gov'] else 'off'}"])
    w.writerow(bt.CSV_COLS)
    w.writerows(rows)
    name = "".join(ch if ch.isalnum() else "_" for ch in setup).strip("_")[:40] or "setup"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="backtest_{name}_{period}.csv"'})


@router.get("/flow/backtest")
def flow_backtest(period: Optional[str] = None, slip: bool = True, gov: bool = True, brk_pct: Optional[float] = None,
                  brk_rs: Optional[float] = None, capital: Optional[float] = None):
    """🧪 Saved setups, the latest result of each (setup, period) under the chosen costs (slippage on / off,
    brokerage % and / or ₹ per order, government charges on / off), and the running job's progress."""
    from backend.intraday import backtest as bt
    costs = _costs(slip, gov, brk_pct, brk_rs, capital)
    return {"status": bt.status(), "setups": {bt.CANVAS: None, **bt.all_setups()}, "periods": list(bt.PERIODS),
            "results": bt.results(period, costs), "saved": bt.saved(), "default_costs": bt.DEFAULT_COSTS}


@router.post("/flow/backtest/stop")
def flow_backtest_stop():
    """⏹ Stop the running backtest now (nothing is saved)."""
    from backend.intraday import backtest as bt
    return bt.cancel()


@router.post("/flow/backtest")
def flow_backtest_run(req: dict):
    from backend.intraday import backtest as bt
    st = S()
    return bt.start(st.config, st.client, list(req.get("setups") or []), str(req.get("period") or "1y"))


@router.post("/flow/setups")
def flow_setup_save(req: dict):
    from backend.intraday import backtest as bt
    try:
        return bt.save_setup(str(req.get("name") or ""))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/flow/setups/load")
def flow_setup_load(req: dict):
    from backend.intraday import backtest as bt
    try:
        bt.load_setup(str(req.get("name")))
    except KeyError:
        raise HTTPException(404, "No such setup")
    return {"loaded": req.get("name")}


@router.post("/flow/setups/delete")
def flow_setup_delete(req: dict):
    from backend.intraday import backtest as bt
    try:
        bt.delete_setup(str(req.get("name")))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"deleted": req.get("name")}


@router.get("/flow/live")
def flow_live():
    """Today's signals with the live price and the paper trade's P&L (polled every few seconds)."""
    from backend.intraday import flow
    return flow.live()


@router.get("/flow/strategy")
def flow_strategy():
    """📜 The strategy running now in plain words (one text to copy), and the last changes to the boxes."""
    from backend.intraday import strategy
    mode = S().config.flow.mode
    return {"text": strategy.text(mode), "mode": mode, "changes": strategy.changes()}
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
    """⬇ Every closed trade of every book (Real, Paper A / E+, Intraday) as one CSV."""
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
    return follow.report(next((i["instrument_token"] for i in u["indices"] if i["symbol"] == uni.INDEX_NAME), None))


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


@router.get("/flow/journey")
def flow_journey():
    """🧭 The latest candle's run explained: each box's in → out and reasons, why (no) trade, the stocks closest to the
    trigger, and the rules in plain words."""
    from backend.intraday import journey
    return journey.summary()


@router.get("/flow/journey/node/{nid}")
def flow_journey_node(nid: str):
    """Who came into a box, who passed (values, where each is now), who dropped and why."""
    from backend.intraday import journey
    return journey.node_stocks(nid)


@router.post("/flow/journey/rerun/{nid}")
def flow_journey_rerun(nid: str):
    """↻ Re-run a box and everything after it on the latest candle with the current settings (dry run: no alert, no
    paper trade)."""
    from backend.intraday import journey
    res = journey.rerun(nid)
    if res.get("error") == "no such box":
        raise HTTPException(404, res["error"])
    return res


@router.get("/flow/journey/check")
def flow_journey_check(symbol: str):
    """One stock through every box on the latest candle."""
    from backend.intraday import journey
    return journey.check(symbol)


@router.post("/flow/reset")
def flow_reset_nodes():
    """↺ Every box back to its default settings (applies from the next candle)."""
    from backend.intraday import flow
    flow.reset_nodes()
    return {"reset": True}


@router.put("/flow/layout")
def flow_layout(pos: dict):
    """Save box positions dragged on the canvas: {node: [x, y]}; an empty body resets to the template's layout."""
    from backend.intraday import flow
    if not pos:
        flow.reset_layout()
        return {}
    return flow.set_layout(pos)


@router.get("/flow/journal")
def flow_journal(days: int = 30):
    """Closed paper trades: totals, by side / setup / time of day, the daily curve."""
    from backend.intraday import flow
    return flow.journal(max(1, min(days, 365)))


@router.post("/flow/run")
def flow_run():
    """Run the flow once now on whatever bars there are (live feed if running), e.g. to try settings."""
    from backend.intraday import feed, flow
    r = feed.RUNNER
    return flow.run_now(S().config, r.feed if r else None)


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


@router.post("/real/settings")
def real_settings(req: RealSettings):
    """Turn real trading on / off, or pick the strategy for new buys (A / E); saved with the settings."""
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
    return res


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
    return {"as_of": as_of, "items": items}


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
                                    "morning_plan", "gap_check", "guard"):
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


# --- 📡 one live connection: the page subscribes to topics; each is sent when it changes ---------------------

def _stream_topics() -> dict:
    """topic -> (producer, seconds until the next look given the last value). Read-only, the same data as the
    matching GET endpoints."""
    from backend.intraday import backtest as bt
    from backend.intraday import flow
    live_feed = lambda d: (d or {}).get("feed", {}).get("state") == "live" if isinstance((d or {}).get("feed"), dict) \
        else (d or {}).get("feed") == "live"
    return {
        "schedule": (schedule_view, lambda d: 20),
        "attention": (attention, lambda d: 30),
        "kite": (lambda: kite_status(False), lambda d: 60),
        "flow": (lambda: flow.state(S().config), lambda d: 3 if live_feed(d) or (d or {}).get("running") else 15),
        "journey": (lambda: __import__("backend.intraday.journey", fromlist=["summary"]).summary(),
                    lambda d: 10 if (d or {}).get("at") else 30),
        "flow_live": (flow.live, lambda d: 3 if live_feed(d) else 30),
        "overview": (overview_view, lambda d: 5 if (d or {}).get("feed") == "live" else 30),
        "backtest": (bt.status, lambda d: 2 if (d or {}).get("state") == "running" else 5),
    }


@router.get("/stream")
async def stream(request: Request, topics: str = "schedule,attention,kite", once: bool = False):
    """Server-sent events: `event: <topic>` with its JSON, whenever it changes (checked on each topic's own
    interval: faster while the live feed or a backtest runs). One connection replaces the pages' timers. once=1:
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
