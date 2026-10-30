"""Tradetron webhook (💰 real trading): BUY with a quantity (and, with real.send_stop, its stop as sig_stop), EXIT
without one (Tradetron closes the whole position).
The payload is the user's own send(): key / value pairs with sig_action always last. The auth token comes from the
environment (TRADETRON_TOKEN in .env) and is never logged, stored or returned."""

import json
import os
import urllib.request

URL = os.getenv("TRADETRON_URL", "https://api.tradetron.tech/api?")


def token() -> str:
    return os.getenv("TRADETRON_TOKEN", "").strip()


def body(symbol: str, action: str, qty=None, auth: str = "", stop=None) -> dict:
    pairs = [("sig_symbol", symbol)]
    if qty:
        pairs.append(("sig_qty", str(qty)))
    if stop:
        pairs.append(("sig_stop", f"{stop:.2f}"))     # optional: Tradetron exits on its own at this price
    pairs.append(("sig_action", action))                # "BUY" or "EXIT", always last
    out = {"auth-token": auth}
    for i, (k, v) in enumerate(pairs):
        s = "" if i == 0 else str(i)
        out["key" + s], out["value" + s] = k, v
    return out


def public(b: dict) -> dict:
    """The payload without the token (for the order log)."""
    return {k: v for k, v in b.items() if k != "auth-token"}


def send(symbol: str, action: str, qty=None, stop=None) -> str:
    """One webhook; raises on a network or HTTP error, or when no token is configured."""
    auth = token()
    if not auth:
        raise RuntimeError("TRADETRON_TOKEN is not set in .env")
    req = urllib.request.Request(URL, json.dumps(body(symbol, action, qty, auth, stop)).encode(),
                                 {"Content-Type": "application/json", "User-Agent": "tt-webhook/1.0"})
    return urllib.request.urlopen(req, timeout=15).read().decode()
