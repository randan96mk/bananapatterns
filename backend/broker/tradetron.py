"""Tradetron webhook (💰 real trading): BUY with a quantity (and, with real.send_stop, its stop as sig_stop), EXIT
without one (Tradetron closes the whole position).
The payload is the user's own send(): key / value pairs with sig_action always last. The auth token comes from the
environment (TRADETRON_TOKEN in .env) and is never logged, stored or returned."""

import json
import os
import threading
import time
import urllib.error
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


PACE_S = 1.5                       # at least this long between two webhooks (Tradetron answers 429 to bursts)
RETRY_429 = (2, 4, 8)              # waits before the retries of a "429 Too Many Requests" (or Retry-After, if longer)
_pace_lock = threading.Lock()
_last_sent = [0.0]
last_retries = 0                   # how many 429 retries the latest send() needed (for the order log and Telegram)


def _pace() -> None:
    with _pace_lock:
        wait = _last_sent[0] + PACE_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_sent[0] = time.monotonic()


def send(symbol: str, action: str, qty=None, stop=None, opener=None) -> str:
    """One webhook; raises on a network or HTTP error, or when no token is configured. Webhooks are spaced PACE_S
    apart. Only a 429 "Too Many Requests" is retried (Tradetron refuses those before taking the order, so a retry
    can't place it twice); every other error is raised at once and never retried (the order may have gone in)."""
    global last_retries
    auth = token()
    if not auth:
        raise RuntimeError("TRADETRON_TOKEN is not set in .env")
    data = json.dumps(body(symbol, action, qty, auth, stop)).encode()
    opener = opener or urllib.request.urlopen
    last_retries = 0
    for attempt in range(len(RETRY_429) + 1):
        _pace()
        req = urllib.request.Request(URL, data, {"Content-Type": "application/json", "User-Agent": "tt-webhook/1.0"})
        try:
            return opener(req, timeout=15).read().decode()
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == len(RETRY_429):
                raise
            try:
                after = float(exc.headers.get("Retry-After") or 0) if exc.headers else 0.0
            except ValueError:
                after = 0.0
            last_retries = attempt + 1
            time.sleep(min(30.0, max(RETRY_429[attempt], after)))
