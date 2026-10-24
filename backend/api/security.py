"""Optional dashboard password (HTTP Basic) for when the app is reachable beyond this Mac.

Set BP_DASHBOARD_PASSWORD in .env (any user name works). Unset = no password, as on
a Mac-only setup. The Kite callback is exempt: a Telegram login link proves itself
with its one-time nonce, and the callback checks the password itself otherwise.
"""

import base64
import binascii
import os
import secrets

from fastapi import Request
from fastapi.responses import PlainTextResponse

EXEMPT = ("/health", "/api/kite/callback")


def password() -> str:
    return os.getenv("BP_DASHBOARD_PASSWORD", "")


def authorised(request: Request) -> bool:
    pw = password()
    if not pw:
        return True
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return secrets.compare_digest(given.encode(), pw.encode())


def challenge() -> PlainTextResponse:
    return PlainTextResponse("Password required", 401, headers={"WWW-Authenticate": 'Basic realm="Banana Patterns"'})


async def require_password(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path in EXEMPT or authorised(request):
        return await call_next(request)
    return challenge()
