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


def _track_if_missing(state: AppState) -> None:
    """Build the track record once (about a minute, in the background); later the evening job adds to it."""
    from backend.engine import track
    try:
        if not track.built() or track.stale(state.config):
            track.build(state.config)
    except Exception:
        logger.exception("Track record build failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = build_state()
    set_state(state)
    threading.Thread(target=_scan_if_behind, args=(state,), name="startup-scan", daemon=True).start()
    if os.getenv("BP_NO_SCHEDULER") != "1":
        threading.Thread(target=_track_if_missing, args=(state,), name="track-build", daemon=True).start()
    state.scheduler = Scheduler(state)
    bot = BotCommands(state)
    if os.getenv("BP_NO_SCHEDULER") != "1":
        state.scheduler.start()           # evening job, reminders, login links, intraday heads-up, weekly refresh
        bot.start()                       # Telegram /login and /status
    if security.password():
        logger.info("Dashboard password on (BP_DASHBOARD_PASSWORD)")
    logger.info("Banana Patterns backend ready")
    yield
    state.scheduler.stop()
    bot.stop()
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

if FRONTEND_BUILD.exists() and os.getenv("BP_NO_STATIC") != "1":
    app.mount("/", StaticFiles(directory=str(FRONTEND_BUILD), html=True), name="frontend")
