"""The webhook side's Developer API, READ-ONLY: which positions are really open there, so a BUY the app booked can be
confirmed as filled (backend/engine/real.py confirm_from_api). Nothing here can place, change or cancel an order.

  key        TRADETRON_API_KEY in .env (never logged, stored or returned; scrub() removes it from any error text)
  base URL   TRADETRON_API_URL (default DEFAULT_URL), header "Authorization: Bearer <key>"
  limit      12 calls a minute (the API's own limit): a 13th call inside a minute is refused here, not sent
  calls      deployments() and positions(sid), one HTTP call each

The exact endpoint paths and JSON shape are NOT known yet. The owner will confirm them: edit DEPLOYMENTS_PATH and
POSITIONS_PATH below (POSITIONS_PATH takes {sid}, the deployment id real.tt_sid). Until then the parser is tolerant:
it walks the whole JSON for objects with a symbol-like key (SYMBOL_KEYS) and a quantity key (QTY_KEYS) and returns
{SYMBOL: qty}; when a symbol shows up more than once the LARGEST quantity is kept (never a sum, so a summary repeated
next to the legs cannot over-count and confirm a fill that is not there)."""

import json
import os
import threading
import time
import urllib.request
from collections import deque
from typing import Optional

DEFAULT_URL = "https://api.tradetron.tech/api/v1"
# ⚠ To be confirmed by the owner against the Developer API docs (paths relative to TRADETRON_API_URL):
DEPLOYMENTS_PATH = "/deployments"                       # the account's deployments (find the id for real.tt_sid)
POSITIONS_PATH = "/deployments/{sid}/positions"         # the open positions of one deployment
SYMBOL_KEYS = ("instrument", "symbol", "tradingsymbol", "underlying")
QTY_KEYS = ("quantity", "qty", "net_qty", "open_qty")
RATE_PER_MIN = 12
TIMEOUT_S = 15

_calls: deque = deque()
_rate_lock = threading.Lock()


class RateLimited(RuntimeError):
    pass


def key() -> str:
    return os.getenv("TRADETRON_API_KEY", "").strip()


def base_url() -> str:
    return (os.getenv("TRADETRON_API_URL", "").strip() or DEFAULT_URL).rstrip("/")


def scrub(text: str) -> str:
    """Any error text with the key taken out (the key never reaches a log, the page or Telegram)."""
    k = key()
    return text.replace(k, "***") if k else text


def _take_slot(now: Optional[float] = None) -> None:
    """At most RATE_PER_MIN calls in any 60 s; a call over the limit is refused (RateLimited), never queued."""
    now = time.monotonic() if now is None else now
    with _rate_lock:
        while _calls and now - _calls[0] >= 60:
            _calls.popleft()
        if len(_calls) >= RATE_PER_MIN:
            raise RateLimited(f"the fill check is limited to {RATE_PER_MIN} calls a minute: try again in a minute")
        _calls.append(now)


def norm(symbol) -> str:
    """NSE:RELIANCE-EQ / reliance → RELIANCE."""
    s = str(symbol or "").strip().upper()
    s = s.split(":")[-1]
    for suffix in ("-EQ", "-BE"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_positions(doc) -> dict:
    """{SYMBOL: qty} from any JSON shape: every object with a symbol-like key and a quantity key is a position (its
    children are not walked again); a symbol seen twice keeps its largest quantity; zero / negative / unreadable
    quantities are left out."""
    out: dict = {}

    def walk(x):
        if isinstance(x, dict):
            low = {str(k).lower(): v for k, v in x.items()}
            sk = next((k for k in SYMBOL_KEYS if isinstance(low.get(k), str) and low.get(k).strip()), None)
            qk = next((k for k in QTY_KEYS if _num(low.get(k)) is not None), None)
            if sk and qk:
                q = _num(low[qk])
                if q and q > 0:
                    s = norm(low[sk])
                    out[s] = max(out.get(s, 0), int(q) if q == int(q) else q)
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(doc)
    return out


class Client:
    """One HTTP GET per call, Bearer key, the rate limit; `opener` is for the tests."""

    def __init__(self, opener=None):
        self.opener = opener or urllib.request.urlopen

    def _get(self, path: str):
        k = key()
        if not k:
            raise RuntimeError("no API key in .env")
        _take_slot()
        req = urllib.request.Request(base_url() + path, headers={"Authorization": f"Bearer {k}", "Accept": "application/json",
                                                                 "User-Agent": "bp-fillcheck/1.0"})
        try:
            raw = self.opener(req, timeout=TIMEOUT_S).read().decode()
        except Exception as exc:                        # the key never travels in the error
            raise RuntimeError(scrub(f"fill check call failed: {exc}")) from None
        try:
            return json.loads(raw)
        except ValueError:
            raise RuntimeError("fill check: the answer was not JSON") from None

    def deployments(self):
        """The raw deployments list (read-only), to find the id for real.tt_sid."""
        return self._get(DEPLOYMENTS_PATH)

    def positions(self, sid: int) -> dict:
        """{SYMBOL: qty} open in deployment `sid` (read-only)."""
        return parse_positions(self._get(POSITIONS_PATH.format(sid=int(sid))))
