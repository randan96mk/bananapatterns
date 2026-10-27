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
from backend.database.models import get_candles, get_db, get_logs, get_runs, latest_universe, now_ist
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
            return run_scan(self.config.bases, self.config.screen, u["stocks"], self.config.trend, self.config.vcp,
                            self.config.market, index, self.config.cup, self.config.flag)

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
    auto = the kind matching the stock's stage in the latest scan (VCP screen first, then Cup & handle, then Bases)."""
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
           "cup": "Cup & handle: U-shaped 12-33% cups of 7-65 weeks with a tight, quiet handle in the upper half",
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
    VCP, Cup & handle and Bases screens, plus held / watched)."""
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
    if screen in ("vcp", "cup", "flag"):
        items.sort(key=lambda p: -(p.get("score") or 0))          # best setups first
    else:
        items.sort(key=STAGE_SORT[stage], reverse=(stage == "played_out"))
    return {"as_of": as_of, "stage": stage, "rows": items}


@router.get("/screens/{screen}/sparks")
def screens_sparks(screen: str, stage: str = Query(...), bars: int = Query(120, ge=20, le=400)):
    """Last `bars` daily candles (+ 50-day line) for every stock in a stage, for the grid view's mini charts.
    On the cup / flag screens a card reaches back to just before the cup's left lip / the flag's pole (at most
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
            lip = (((p.get("cup") or {}).get("left_lip") or {}).get("date")
                   or ((p.get("flag") or {}).get("pole") or {}).get("start"))
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


# --- Track record ---

TRACK_RULES = ("swing", "i5m", "iday", "iday_s50", "iday_s75")
_track_memo: dict = {}          # (rule set, filters, build stamp, settings) -> its rows / stats / portfolio


def _rules(rules: str, nifty: str = "any") -> str:
    """The rule set key for a base rule set and a Nifty 500 filter (any / up / strong), e.g. 'iday_s50@up'."""
    from backend.engine import track_intraday as ti
    rules, nifty = rules.lower(), nifty.lower()
    if rules not in TRACK_RULES:
        raise HTTPException(422, f"rules must be one of {', '.join(TRACK_RULES)}")
    if nifty not in ti.FILTERS:
        raise HTTPException(422, f"nifty must be one of {', '.join(ti.FILTERS)}")
    return ti.key(rules, nifty)


def _track_rows(period: str, pattern: str, tier: str, status: str, rules: str = "swing") -> list[dict]:
    from backend.engine import track
    rs = track.rows(period.lower(), pattern.lower(), tier.upper(), rules)
    if status == "running":
        rs = [r for r in rs if r["status"] in ("running", "pending")]
    elif status == "closed":
        rs = [r for r in rs if r["status"] in ("won", "lost")]
    elif status == "no_entry":
        rs = [r for r in rs if r["status"] == "no_entry"]
    return rs


def _track_set(key: str, period: str, pattern: str, tier: str, status: str, stamp, cache: dict) -> dict:
    """Rows, stats, portfolio and the comparison line of one rule set, kept until the next build."""
    from backend.engine import track, track_intraday as ti
    cfg = S().config
    memo_key = (key, period.lower(), pattern.lower(), tier.upper(), status, stamp,
                repr((asdict(cfg.track), asdict(cfg.intraday))))
    if memo_key in _track_memo:
        return _track_memo[memo_key]
    rs = _track_rows(period, pattern, tier, status, key)
    st = track.stats(rs, cfg.track)
    pf = track.portfolio(rs, cfg, 0 if ti.parse(key)[0] == "swing" else cfg.intraday.max_new_per_day, cache)
    sv = (pf.get("compare") or {}).get("strategy") or {}
    dd = sv.get("drawdown")
    if len(_track_memo) > 60:
        _track_memo.clear()
    _track_memo[memo_key] = {
        "rules": key, "rows": rs, "stats": st, "portfolio": pf,
        "summary": {"rules": ti.parse(key)[0], "key": key, "name": ti.name(key), "entered": st["counts"]["entered"],
                    "closed": st["counts"]["closed"], "win_rate": st["win_rate"], "avg_win": st["avg_win"],
                    "avg_loss": st["avg_loss"], "expectancy_r": st["expectancy_r"],
                    "profit_factor": st["profit_factor"], "net_avg_pct": st["net_avg_pct"],
                    "return": sv.get("return"), "drawdown": dd,
                    "ratio": round(sv["return"] / -dd, 2) if sv.get("return") is not None and dd else None}}
    return _track_memo[memo_key]


@router.get("/track")
def track_summary(period: str = "1y", pattern: str = "all", tier: str = "all", status: str = "all", rules: str = "swing",
                  nifty: str = "any"):
    """Track record: status line, summary numbers, by pattern / market, and the portfolio curve vs Nifty 500.
    The period (1w ... 2y) filters by breakout date; the curve follows the same filters.
    rules: swing (the swing record) or an intraday rule set (i5m / iday / iday_s50 / iday_s75); nifty: the Nifty 500
    filter at the entry (any / up / strong). rule_sets compares every built rule set with the same filters (and,
    with a Nifty 500 filter, each one's return without it: return_any)."""
    from backend.engine import track, track_intraday as ti
    from backend.database.models import get_setting
    sel = _rules(rules, nifty)
    nifty = ti.parse(sel)[1]
    cfg = S().config
    with get_db() as conn:
        metas = {k: {m: get_setting(conn, f"{k}.{m}") for m in ("built_at", "updated_at", "as_of")}
                 for k in ("track", "track_intraday")}
    stale = ti.stale_reason(cfg)
    meta_of = lambda k: metas["track" if k == "swing" else "track_intraday"]  # noqa: E731
    ok = lambda k: bool(meta_of(k)["built_at"]) and (k == "swing" or not stale)  # noqa: E731
    sets, cache = [], {}
    for base in TRACK_RULES:
        k = ti.key(base, nifty)
        if not ok(k):
            continue
        one = _track_set(k, period, pattern, tier, status, meta_of(k)["updated_at"], cache)
        line = dict(one["summary"])
        if nifty != "any" and ok(base):
            line["return_any"] = _track_set(base, period, pattern, tier, status, meta_of(base)["updated_at"],
                                            cache)["summary"]["return"]
        sets.append((one, line))
    cur = next((one for one, _ in sets if one["rules"] == sel), None)
    rs = cur["rows"] if cur else []
    base, _ = ti.parse(sel)
    if base == "swing":
        shown = {k: getattr(cfg.track, k) for k in ("breakeven_pct", "book_half_pct", "exit_ma", "max_hold", "max_stop")}
        shown.update(nifty=nifty, entry_start=cfg.intraday.entry_start, swing_entry_end=cfg.intraday.swing_entry_end)
    else:
        shown = dict(asdict(cfg.intraday), min_stop_pct=ti.VARIANTS[base][1], trail=ti.VARIANTS[base][0], nifty=nifty)
    return {"built": ok(sel), **meta_of(sel), "rules_set": sel, "base": base, "nifty": nifty, "name": ti.name(sel),
            "stale": stale if sel != "swing" else None,
            "costs": {"cost_pct": cfg.track.cost_pct, "intraday_cost_pct": cfg.track.intraday_cost_pct,
                      "slippage_pct": cfg.track.slippage_pct},
            "progress": (track if sel == "swing" else ti).status(), "period": period.lower(), "rules": shown,
            "kite": bool(S().client.has_token),
            **(cur["stats"] if cur else track.stats(rs, cfg.track)), "by_pattern": track.split(rs, "pattern"),
            "by_market": track.split(rs, "market"),
            "portfolio": cur["portfolio"] if cur else track.portfolio([], cfg),
            "rule_sets": [line for _, line in sets] if len(sets) > 1 else []}


@router.get("/track/trades")
def track_trades(period: str = "1y", pattern: str = "all", tier: str = "all", status: str = "all", rules: str = "swing",
                 nifty: str = "any"):
    """Trades grouped by stock (newest first), each with its exit timeline and the closes for its sparkline
    (a few sessions before the entry to a few after the exit, or to today while it runs)."""
    from datetime import date as _date, timedelta as _td
    groups: dict[str, list] = {}
    for r in _track_rows(period, pattern, tier, status, _rules(rules, nifty)):
        groups.setdefault(r["symbol"], []).append(r)
    tokens = {s["symbol"]: s["instrument_token"] for s in uni.resolve_universe(S().config.ingest)["stocks"]}
    with get_db() as conn:
        for sym, ts in groups.items():
            if sym not in tokens:
                continue
            first = min(t["breakout_date"] for t in ts)
            cs = get_candles(conn, tokens[sym], start=(_date.fromisoformat(first) - _td(days=10)).isoformat())
            dates = [c["date"] for c in cs]
            for t in ts:
                if t["status"] in ("in_trade", "pending") or not t.get("entry_date") or t["entry_date"] not in dates:
                    continue
                i = dates.index(t["entry_date"])
                end = dates.index(t["exit_date"]) if t.get("exit_date") in dates else min(len(cs) - 1, i + 63)
                lo, hi = max(0, i - 3), min(len(cs), end + 4)
                t["spark"] = {"d": dates[lo:hi], "c": [c["close"] for c in cs[lo:hi]]}
    return {"stocks": [{"symbol": sym, "trades": ts} for sym, ts in groups.items()]}


@router.post("/track/rebuild")
def track_rebuild():
    """Rebuild the whole track record in the background (e.g. after changing a pattern setting)."""
    from backend.engine import track
    if track.status()["state"] != "idle":
        raise HTTPException(409, "The track record is already building")
    threading.Thread(target=track.build, args=(S().config, True), name="track-rebuild", daemon=True).start()
    return {"status": "started"}


@router.post("/track/intraday/build")
def track_intraday_build(rebuild: bool = False):
    """Run the intraday entry backtest in the background: the watchlist's pivot crosses over the last year, with
    the 5-minute candles fetched from Kite (needs today's login; about 3,000 requests a year of history,
    ~15-20 minutes; candles already stored are re-used). A build for older rule sets is re-run from scratch."""
    from backend.engine import track, track_intraday as ti
    st = S()
    if not track.built():
        raise HTTPException(409, "Build the swing track record first")
    if ti.status()["state"] != "idle":
        raise HTTPException(409, "The intraday backtest is already running")
    if not st.client.has_token:
        raise HTTPException(400, "Log in to Kite first: the backtest fetches 5-minute candles")
    threading.Thread(target=ti.build, args=(st.config, st.client, rebuild), name="track-intraday",
                     daemon=True).start()
    return {"status": "started"}


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
