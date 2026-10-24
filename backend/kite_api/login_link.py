"""One-time Kite login links and the token handover to AlgoTrade-v3.

A link is the normal Kite login URL plus `redirect_params=n=<nonce>`; Kite hands
`n` back to /api/kite/callback with the request_token. A link works once, for
LINK_MINUTES, and a newer link from the same source supersedes older ones, so a
stale Telegram button shows "expired" instead of logging in.

Kite redirects to the URL registered on the developer console, so the link
works from a phone only when that URL is reachable from it: PUBLIC_BASE_URL
(Tailscale address or cloud domain); on the Mac alone it is 127.0.0.1.

Banana Patterns and AlgoTrade-v3 share one Kite app, and a new login replaces
the app's previous token. So after a login here the same token is handed to
AlgoTrade (ALGOTRADE_URL, its existing POST /api/kite/token): one login a day
serves both.
"""

import json
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote, urlsplit

import httpx

from backend.config import IST
from backend.database.models import get_setting, set_setting

LINK_MINUTES = 15
CALLBACK_PATH = "/api/kite/callback"
KEY_HANDOVER = "kite_handover"


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


def algotrade_url() -> str:
    return os.getenv("ALGOTRADE_URL", "").strip().rstrip("/")


def hand_over(conn, token: str, http: Optional[httpx.Client] = None) -> dict:
    """Send the token to AlgoTrade-v3. {configured, ok, message}; the result is kept for the status page."""
    url = algotrade_url()
    if not url:
        return {"configured": False, "ok": None, "message": "ALGOTRADE_URL not set"}
    try:
        r = (http or httpx).post(f"{url}/api/kite/token", json={"access_token": token}, timeout=10)
        ok = r.status_code == 200
        message = "token updated" if ok else f"HTTP {r.status_code}: {r.text[:200]}"
    except httpx.HTTPError as exc:
        ok, message = False, f"{type(exc).__name__}: {exc}"[:200]
    out = {"configured": True, "ok": ok, "message": message, "at": datetime.now(IST).isoformat(timespec="seconds")}
    set_setting(conn, KEY_HANDOVER, json.dumps(out))
    return out


def last_handover(conn) -> Optional[dict]:
    raw = get_setting(conn, KEY_HANDOVER)
    return json.loads(raw) if raw else None
