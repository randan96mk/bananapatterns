"""FastAPI application: lifespan, CORS, routes, optional static frontend."""

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=False)

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from backend.alerts.commands import BotCommands  # noqa: E402
from backend.alerts.jobs import Scheduler  # noqa: E402
from backend.api import security  # noqa: E402
from backend.api.routes import AppState, router, set_state  # noqa: E402
from backend.config import AppConfig, load_config  # noqa: E402
from backend.database.models import get_db, init_db  # noqa: E402
from backend.kite_api import auth  # noqa: E402
from backend.kite_api.client import KiteClient  # noqa: E402
from backend.logging_config.logger import setup_logger  # noqa: E402

logger = setup_logger("app")
FRONTEND_BUILD = Path(__file__).resolve().parent.parent / "frontend" / "build"


def build_state() -> AppState:
    init_db()
    config = load_config(AppConfig())
    client = KiteClient(config.kite, config.ingest)
    with get_db() as conn:
        token = auth.load_fresh_token(conn)
    state = AppState(config, client)
    if token:
        client.set_access_token(token)
        state.token_source = "db"
        logger.info("Restored today's Kite token from DB")
    elif config.kite.access_token:
        state.token_source = "env"
        logger.info("Using KITE_ACCESS_TOKEN from .env (issue time unknown; verify via /api/kite/status?verify=1)")
    else:
        client.clear_access_token()
        logger.info("No valid Kite token: log in via the dashboard")
    return state


def _scan_if_behind(state: AppState) -> None:
    """Refresh the stage board on startup when candles are newer than the last scan."""
    try:
        with get_db() as conn:
            latest_bar = conn.execute("SELECT MAX(last_date) FROM ingest_status").fetchone()[0]
            last_scan = conn.execute("SELECT MAX(as_of) FROM scan_runs").fetchone()[0]
        if latest_bar and (last_scan is None or latest_bar > last_scan):
            logger.info("Stage board behind data (%s < %s): scanning", last_scan, latest_bar)
            state.scan()
    except Exception:
        logger.exception("Startup scan failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = build_state()
    set_state(state)
    threading.Thread(target=_scan_if_behind, args=(state,), name="startup-scan", daemon=True).start()
    state.scheduler = Scheduler(state)
    bot = BotCommands(state)
    from backend.tips import reader as tips_reader
    from backend.tips import track as tips_track
    state.tips_tracker = tips_track.Tracker(state)
    from backend.tips import kite_orders as tips_kite
    state.tips_kite = tips_kite.Syncer(state)
    try:                                  # ⚡ once: the close facts of Kite hints closed before the card's P&L strip
        tips_kite.backfill_close_facts()
    except Exception:
        logger.exception("Tips Kite close-facts backfill failed")
    try:                                  # ⚡ once: the Kite ledger rows of closed Kite hints that lack one (append-only)
        from backend.tips import kite_book as tips_kite_book
        tips_kite_book.backfill(state.config)
    except Exception:
        logger.exception("Tips Kite ledger backfill failed")
    try:                                  # ₿ BTC pivot patterns: idle until crypto.enabled (alerts only, its own thread)
        from backend.crypto import feed as crypto_feed
        state.crypto = crypto_feed.CryptoService(state)
        if os.getenv("BP_NO_CRYPTO") != "1":
            state.crypto.start()
    except Exception:
        logger.exception("Crypto feed start failed")
    if os.getenv("BP_NO_SCHEDULER") != "1":
        state.scheduler.start()           # evening job, reminders, login links, intraday heads-up, weekly refresh
        try:                              # 🤖 a deploy at night: the skipped night jobs are queued again
            state.scheduler.rerun_if_skipped()
        except Exception:
            logger.exception("AI rerun after start failed")
        bot.start()                       # Telegram /login and /status
        state.tips_tracker.start()        # 💡 Tips: quotes every 5 s while a hint is open or waiting
        state.tips_kite.start()           # ⚡ Tips → Kite orders: the order sync every 10 s while an order of ours works
        try:                              # 📈 the market read: the candle model worker (its own process) + the index loop
            from backend.market import reads as market_reads
            state.market = market_reads.MarketService(state)
            state.market.start()
        except Exception:
            logger.exception("Market read start failed")
        try:                              # 💡 Tips: the reader starts only when tips.enabled and a session exist
            tips_reader.ensure(state)
        except Exception:
            logger.exception("Tips reader start failed")
    if security.password():
        logger.info("Dashboard password on (BP_DASHBOARD_PASSWORD)")
    logger.info("Banana Patterns backend ready")
    yield
    state.scheduler.stop()
    bot.stop()
    state.tips_tracker.stop()
    state.tips_kite.stop()
    if getattr(state, "market", None) is not None:
        state.market.stop()
    if getattr(state, "crypto", None) is not None:
        state.crypto.stop()
    if tips_reader.READER is not None:
        tips_reader.READER.stop()
    logger.info("Banana Patterns backend stopped")


app = FastAPI(title="Banana Patterns (personal VCP screener)", version="0.1.0", lifespan=lifespan)
app.middleware("http")(security.require_password)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "bananapatterns"}


app.include_router(router)



class _Static(StaticFiles):
    """The build's hashed assets may be cached for a year; index.html never (a stale index after a deploy would ask
    for chunk files that no longer exist)."""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if path.startswith("assets/"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-cache"
        return resp


if FRONTEND_BUILD.exists() and os.getenv("BP_NO_STATIC") != "1":
    app.mount("/", _Static(directory=str(FRONTEND_BUILD), html=True), name="frontend")
