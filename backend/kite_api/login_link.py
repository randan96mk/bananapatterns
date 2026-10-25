"""One-time Kite login links.

A link is the normal Kite login URL plus `redirect_params=n=<nonce>`; Kite hands
`n` back to /api/kite/callback with the request_token. A link works once, for
LINK_MINUTES, and a newer link from the same source supersedes older ones, so a
stale Telegram button shows "expired" instead of logging in.

Kite redirects to the URL registered on the developer console, so the link
works from a phone only when that URL is reachable from it: PUBLIC_BASE_URL
(Tailscale address or cloud domain); on the Mac alone it is 127.0.0.1.
"""

import os
import secrets
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote, urlsplit

from backend.config import IST

LINK_MINUTES = 15
CALLBACK_PATH = "/api/kite/callback"


def public_base_url() -> str:
    """Where Kite sends the browser back to: PUBLIC_BASE_URL, else the origin of KITE_REDIRECT_URL."""
    base = os.getenv("PUBLIC_BASE_URL", "").strip()
    if not base:
        parts = urlsplit(os.getenv("KITE_REDIRECT_URL", "") or "http://127.0.0.1:8000")
        base = f"{parts.scheme}://{parts.netloc}"
    return base.rstrip("/")


def redirect_url() -> str:
    """The exact URL to register as the redirect URL on the Kite developer console."""
    return public_base_url() + CALLBACK_PATH


def create_link(conn, login_url: str, source: str = "telegram", now: Optional[datetime] = None) -> tuple[str, datetime]:
    """(Kite login URL carrying a fresh one-time nonce, expiry). Supersedes older links of `source`."""
    now = now or datetime.now(IST)
    nonce, expires = secrets.token_urlsafe(16), now + timedelta(minutes=LINK_MINUTES)
    conn.execute("UPDATE login_links SET superseded = 1 WHERE source = ? AND used_at IS NULL", (source,))
    conn.execute("INSERT INTO login_links (nonce, source, created_at, expires_at) VALUES (?, ?, ?, ?)",
                 (nonce, source, now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")))
    return f"{login_url}&redirect_params={quote(f'n={nonce}', safe='')}", expires


def check_link(conn, nonce: str, now: Optional[datetime] = None) -> Optional[str]:
    """None when the link may log in, else why not: unknown / used / superseded / expired."""
    row = conn.execute("SELECT expires_at, used_at, superseded FROM login_links WHERE nonce = ?", (nonce,)).fetchone()
    if row is None:
        return "unknown"
    if row["used_at"]:
        return "used"
    if row["superseded"]:
        return "superseded"
    if (now or datetime.now(IST)) > datetime.fromisoformat(row["expires_at"]):
        return "expired"
    return None


def mark_used(conn, nonce: str, now: Optional[datetime] = None) -> None:
    conn.execute("UPDATE login_links SET used_at = ? WHERE nonce = ?",
                 ((now or datetime.now(IST)).isoformat(timespec="seconds"), nonce))

