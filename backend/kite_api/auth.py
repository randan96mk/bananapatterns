"""Kite access-token lifecycle.

A Kite access token is valid until 06:00 IST the next morning (not midnight),
and only one token per app is active: logging in here also invalidates the
token AlgoTrade-v3 holds if both share the API key.
"""

from datetime import datetime, time, timedelta
from typing import Optional

from backend.config import IST
from backend.database.models import get_setting, set_setting

EXPIRY_TIME = time(6, 0)

KEY_TOKEN = "kite_access_token"
KEY_ISSUED = "kite_token_issued_at"


def _as_ist(dt: datetime) -> datetime:
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def last_expiry_boundary(now: Optional[datetime] = None) -> datetime:
    """Most recent 06:00 IST at or before `now`."""
    now = _as_ist(now or datetime.now(IST))
    boundary = datetime.combine(now.date(), EXPIRY_TIME, IST)
    return boundary if now >= boundary else boundary - timedelta(days=1)


def expiry_for(issued_at: datetime) -> datetime:
    """The 06:00 IST at which a token issued at `issued_at` dies."""
    return last_expiry_boundary(issued_at) + timedelta(days=1)


def is_fresh(issued_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    if issued_at is None:
        return False
    return _as_ist(issued_at) >= last_expiry_boundary(now)


def store_token(conn, token: str, issued_at: Optional[datetime] = None) -> datetime:
    issued_at = _as_ist(issued_at or datetime.now(IST))
    set_setting(conn, KEY_TOKEN, token)
    set_setting(conn, KEY_ISSUED, issued_at.isoformat(timespec="seconds"))
    return issued_at


def load_token(conn) -> tuple[Optional[str], Optional[datetime]]:
    token = get_setting(conn, KEY_TOKEN)
    issued = get_setting(conn, KEY_ISSUED)
    return token, (datetime.fromisoformat(issued) if issued else None)


def load_fresh_token(conn, now: Optional[datetime] = None) -> Optional[str]:
    token, issued = load_token(conn)
    return token if token and is_fresh(issued, now) else None
