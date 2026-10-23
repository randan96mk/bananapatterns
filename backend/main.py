"""FastAPI application: lifespan, CORS, routes, optional static frontend."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=False)

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    set_state(build_state())
    logger.info("Banana Patterns backend ready")
    yield
    logger.info("Banana Patterns backend stopped")


app = FastAPI(title="Banana Patterns (personal VCP screener)", version="0.1.0", lifespan=lifespan)
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
