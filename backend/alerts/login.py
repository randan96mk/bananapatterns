"""Kite login alerts: send a one-time login link to Telegram, and verify the current session."""

from datetime import datetime
from typing import Optional

from backend.alerts import messages as msg
from backend.alerts.dispatch import Notifier
from backend.config import IST
from backend.database.models import get_db
from backend.kite_api import auth
from backend.kite_api import login_link as ll
from backend.kite_api.client import KiteAuthError
from backend.logging_config.logger import setup_logger

logger = setup_logger("login")


def send_login_link(state, notifier: Notifier, reason: str, key: str, now: Optional[datetime] = None) -> bool:
    """Telegram message with a 'Log in to Kite' button carrying a fresh one-time link."""
    if not notifier.ready or not state.config.kite.is_configured or notifier.already_sent(key):
        return False
    with get_db() as conn:
        url, expires = ll.create_link(conn, state.client.login_url(), "telegram", now)
    return notifier.send(key, "login", msg.login_link_alert(reason, expires), buttons=[("🔑 Log in to Kite", url)])


def session_ok(state) -> bool:
    """True when a token is held and Kite accepts it. A rejected token is dropped (so the app shows
    'login needed'); a network error counts as OK, so a flaky connection doesn't trigger links."""
    if not state.token_valid():
        return False
    try:
        state.client.profile()
        return True
    except KiteAuthError as exc:
        logger.warning("Kite rejected the session: %s", exc)
        state.client.clear_access_token()
        state.token_source = None
        with get_db() as conn:
            auth.clear_token(conn)
        return False
    except Exception as exc:                                       # network / Kite down: don't cry wolf
        logger.info("Session check inconclusive: %s", exc)
        return True


def confirm_login(notifier: Notifier, issued_at: datetime) -> bool:
    expires = auth.expiry_for(issued_at)
    return notifier.send(f"login-ok|{issued_at.isoformat(timespec='seconds')}", "login",
                         msg.login_ok_alert(expires.astimezone(IST)))
