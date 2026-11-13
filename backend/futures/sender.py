"""📐 The stock futures deployment's webhook sender (Tradetron, strategy "Stock Futures Webhook Long Short").
One signal per call: sig_symbol (the NSE symbol, sent exactly as named, e.g. M&M), sig_qty in LOTS (not for EXIT),
sig_action BUY | SELL | EXIT always last; `kill_switch` sends kill_switch=1 (closes everything, stops entries).
The token is its own: TRADETRON_FUTURES_TOKEN in .env, never logged, stored or returned (`public` strips it for
the order log). Webhooks are paced on the cash sender's clock (at least real.send_gap_s apart, default 10 s: the
deployment's own limits, 3/s and 30/min, are far above that). A 429 is retried after 2, 4, 8 s (or Retry-After);
any other HTTP error is raised at once; a timeout (or a connection lost after the request went out) raises
SendUnknown: the order MAY have gone in, so the book marks it `unknown · check` and never re-sends it; a reply
containing REJECTED_ALREADY_HELD raises AlreadyHeld. Outside 09:15–15:30 IST or on a non-trading day the sender
refuses (MarketClosed) before anything goes out: the deployment rejects (never queues) those anyway."""

import http.client
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, time as dtime
from typing import Optional
from urllib.parse import urlsplit

from backend.broker import tradetron as tt
from backend.config import IST
from backend.logging_config.logger import setup_logger

logger = setup_logger("futures.sender")

URL = os.getenv("TRADETRON_URL", "https://api.tradetron.tech/api?")
USER_AGENT = "tt-webhook/1.0"
ENV_TOKEN = "TRADETRON_FUTURES_TOKEN"
GAP_S = 10.0                       # the default floor between two futures webhooks (real.send_gap_s, via the book)
TIMEOUT_S = 15
RETRY_429 = (2, 4, 8)              # waits before the retries of a 429 (or Retry-After, if longer; 30 s at most)
OPEN, CLOSE = dtime(9, 15), dtime(15, 30)
ACTIONS = ("BUY", "SELL", "EXIT")

sent_kills: list[dict] = []        # every kill_switch=1 that went out (for the page and the tests)
last_retries = 0                   # how many 429 retries the latest call needed (for the order log)
_lock = threading.Lock()


class SendError(RuntimeError):
    """A webhook that did not go through (nothing was placed)."""


class SendUnknown(SendError):
    """The request went out but no answer came back (timeout, connection lost): the order MAY have gone in. The
    book marks it `unknown · check` and never re-sends it."""


class AlreadyHeld(SendError):
    """The deployment answered REJECTED_ALREADY_HELD: the stock is already held there (flip = EXIT first)."""


class MarketClosed(SendError):
    """Outside 09:15–15:30 IST or on a non-trading day: the deployment rejects, so nothing is sent."""


def token() -> str:
    return os.getenv(ENV_TOKEN, "").strip()


def body(symbol: str, action: str, lots=None, auth: str = "") -> dict:
    """The payload: key/value pairs in order, sig_action last. BUY/SELL carry sig_qty in lots; EXIT does not."""
    action = (action or "").strip().upper()
    symbol = (symbol or "").strip().upper()
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}, not {action!r}")
    if not symbol:
        raise ValueError("a symbol is required")
    pairs = [("sig_symbol", symbol)]
    if action != "EXIT":
        try:
            n = int(lots)
        except (TypeError, ValueError):
            raise ValueError(f"{action} {symbol}: lots must be a whole number >= 1, not {lots!r}") from None
        if n < 1:
            raise ValueError(f"{action} {symbol}: lots must be >= 1, not {n}")
        pairs.append(("sig_qty", str(n)))
    pairs.append(("sig_action", action))
    return _pairs(pairs, auth)


def kill_body(auth: str = "") -> dict:
    return _pairs([("kill_switch", "1")], auth)


def _pairs(pairs, auth: str) -> dict:
    out = {"auth-token": auth}
    for i, (k, v) in enumerate(pairs):
        s = "" if i == 0 else str(i)
        out["key" + s], out["value" + s] = k, v
    return out


def public(b: dict) -> dict:
    """The payload without the token (for the order log and the logger)."""
    return {k: v for k, v in b.items() if k != "auth-token"}


def market_open(now: Optional[datetime] = None) -> tuple[bool, str]:
    """(open?, state) on the NSE calendar the book uses: 'open' | 'closed' (outside 09:15–15:30 IST) | 'holiday'."""
    from backend.ingest import holidays as hol
    now = now or datetime.now(IST)
    if now.tzinfo is not None:
        now = now.astimezone(IST)
    if not hol.is_trading_day(now.date()):
        return False, "holiday"
    if OPEN <= now.time() < CLOSE:
        return True, "open"
    return False, "closed"


def _unknown(exc: BaseException) -> bool:
    """Did the request go out without an answer? (A timeout, or the connection dropped after the send.)"""
    if isinstance(exc, (TimeoutError, socket.timeout, http.client.RemoteDisconnected, http.client.IncompleteRead,
                        ConnectionResetError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        r = getattr(exc, "reason", None)
        return _unknown(r) if isinstance(r, BaseException) else "timed out" in str(r).lower() or "timeout" in str(r).lower()
    return False


def _post(b: dict, opener=None, gap: Optional[float] = None, floor: float = GAP_S) -> str:
    """One webhook, paced on the shared Tradetron clock (at least `floor`, or `gap` when longer); a 429 is
    retried, everything else raised once."""
    global last_retries
    data = json.dumps(b).encode()
    opener = opener or urllib.request.urlopen
    last_retries = 0
    for attempt in range(len(RETRY_429) + 1):
        tt._pace(max(float(gap or 0), floor))
        req = urllib.request.Request(URL, data, {"Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            text = opener(req, timeout=TIMEOUT_S).read().decode()
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == len(RETRY_429):
                raise _already_held(exc) or exc
            try:
                after = float(exc.headers.get("Retry-After") or 0) if exc.headers else 0.0
            except ValueError:
                after = 0.0
            last_retries = attempt + 1
            time.sleep(min(30.0, max(RETRY_429[attempt], after)))
            continue
        except Exception as exc:
            if _unknown(exc):
                raise SendUnknown(f"no answer from Tradetron ({exc.__class__.__name__}): the signal may have gone in · check") from exc
            raise
        if "REJECTED_ALREADY_HELD" in text:
            raise AlreadyHeld(text.strip()[:200])
        return text
    raise RuntimeError("unreachable")


def _already_held(exc: urllib.error.HTTPError) -> Optional[AlreadyHeld]:
    try:
        text = exc.read().decode(errors="replace")
    except Exception:
        return None
    return AlreadyHeld(text.strip()[:200]) if "REJECTED_ALREADY_HELD" in text else None


def send(symbol: str, action: str, lots=None, opener=None, gap: Optional[float] = None,
         now: Optional[datetime] = None) -> str:
    """One signal to the futures deployment; returns Tradetron's reply text. Raises RuntimeError without a token,
    MarketClosed outside hours, ValueError on a bad action / lots, SendUnknown on a timeout (the order may have gone
    in: never re-send), AlreadyHeld on REJECTED_ALREADY_HELD, urllib.error.HTTPError on any other HTTP error
    (a 429 is retried first). `gap` is the floor between two webhooks (at least GAP_S)."""
    auth = token()
    if not auth:
        raise RuntimeError(f"{ENV_TOKEN} is not set in .env: nothing was sent")
    b = body(symbol, action, lots, auth)
    ok, state = market_open(now)
    if not ok:
        when = "not a trading day" if state == "holiday" else "outside market hours (09:15–15:30 IST)"
        raise MarketClosed(f"{when}: the deployment would reject {action} {symbol}, nothing was sent")
    with _lock:
        logger.info("futures webhook %s", json.dumps(public(b)))
        text = _post(b, opener, gap)
    logger.info("futures webhook reply %s %s: %s", action, b["value"], text.strip()[:120])
    return text


def kill_switch(note: str = "", opener=None) -> str:
    """kill_switch=1: closes everything at the deployment and stops its entries. Sent once per call, at any hour
    (the deployment decides); recorded in `sent_kills`. Raises without a token or on an HTTP / network error."""
    from backend.database.models import now_ist
    auth = token()
    if not auth:
        raise RuntimeError(f"{ENV_TOKEN} is not set in .env: the kill switch was not sent")
    with _lock:
        logger.warning("futures kill switch %s", json.dumps(public(kill_body())))
        text = _post(kill_body(auth), opener, 0.0, floor=tt.PACE_S)      # urgent: only the shared 1.5 s floor
    sent_kills.append({"at": now_ist(), "note": note, "reply": text.strip()[:120]})
    return text


def check(gap: Optional[float] = None, now: Optional[datetime] = None) -> dict:
    """What the page shows: is a token configured (never the token), the host, the floor between webhooks and
    the market-hours state."""
    ok, state = market_open(now)
    return {"token_set": bool(token()), "url_host": urlsplit(URL).netloc or URL, "gap_s": max(float(gap or 0), GAP_S),
            "window": {"open": OPEN.strftime("%H:%M"), "close": CLOSE.strftime("%H:%M"), "state": state, "ok": ok},
            "user_agent": USER_AGENT}
