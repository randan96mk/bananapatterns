"""⚡ Tips → Kite orders · the dry run's simulator (tips.kite_mode = "dry"). The same surface the engine uses on the real
client (place_order / modify_order / cancel_order / orders / profile, has_token), so every button, leg and follow-on
order runs exactly as for real; nothing reaches Kite and no login is needed.

  fills     from live quotes (the hint's last LTP: tips_quotes, or what the sync passes to mark()):
              MARKET  COMPLETE at once at the last known LTP (also outside market hours: the owner can rehearse in the
                      evening); with no quote yet it waits OPEN for the first one
              LIMIT   sell COMPLETE at the price when ltp >= price · buy when ltp <= price
              SL-M    sell COMPLETE at the trigger when ltp <= trigger · buy when ltp >= trigger
  orders()  Kite-shaped dicts: order_id ("DRY-<n>"), status OPEN / COMPLETE / CANCELLED / REJECTED, average_price,
            filled_quantity, quantity, tag, tradingsymbol, transaction_type, order_type, price, trigger_price,
            status_message.
  storage   app_settings "tips.kite.sim" (JSON {n, orders}), so the book survives a restart. clear() empties the book
            (the counter keeps counting, so an order id is never reused)."""

import json
import threading
from typing import Callable, Optional

from backend.database.models import get_db, get_setting, set_setting

SIM_KEY = "tips.kite.sim"
_LOCK = threading.RLock()


class DryRunError(Exception):
    """A request the simulator refuses (as Kite would): an unknown order, a change to an order that is not open."""


def quote_ltp(tradingsymbol: str) -> Optional[float]:
    """The last stored quote of any hint on this contract (tips_quotes, written by the Tips quote poll)."""
    with get_db() as conn:
        r = conn.execute("SELECT q.ltp FROM tips_quotes q JOIN tips_hints h ON h.id = q.hint_id WHERE h.tradingsymbol = ? "
                         "AND q.ltp IS NOT NULL ORDER BY q.t DESC LIMIT 1", (tradingsymbol,)).fetchone()
    return float(r[0]) if r else None


def _crossed(o: dict, ltp: float) -> Optional[float]:
    """The fill price when `ltp` fills open order `o`, else None."""
    typ, buy = o.get("order_type"), o.get("transaction_type") == "BUY"
    if typ == "MARKET":
        return float(ltp)
    if typ == "LIMIT" and o.get("price") is not None:
        p = float(o["price"])
        return p if ((buy and ltp <= p) or (not buy and ltp >= p)) else None
    if typ in ("SL-M", "SL") and o.get("trigger_price") is not None:
        t = float(o["trigger_price"])
        return t if ((buy and ltp >= t) or (not buy and ltp <= t)) else None
    return None


class SimClient:
    """The dry run's broker. `ltp_for(tradingsymbol)` gives the last known price (default: the stored Tips quotes)."""

    dry = True
    has_token = True                       # no Kite login needed for a rehearsal

    def __init__(self, ltp_for: Optional[Callable[[str], Optional[float]]] = None):
        self.ltp_for = ltp_for or quote_ltp

    # --- the stored book ---

    def _load(self) -> dict:
        with get_db() as conn:
            raw = get_setting(conn, SIM_KEY)
        try:
            d = json.loads(raw) if raw else {}
        except ValueError:
            d = {}
        d.setdefault("n", 0)
        d.setdefault("orders", [])
        return d

    def _save(self, d: dict) -> None:
        with get_db() as conn:
            set_setting(conn, SIM_KEY, json.dumps(d))

    def _find(self, d: dict, order_id: str) -> dict:
        o = next((x for x in d["orders"] if x["order_id"] == str(order_id)), None)
        if o is None:
            raise DryRunError(f"dry run: no order {order_id}")
        return o

    # --- the client surface ---

    def profile(self) -> dict:
        return {"user_id": "DRY", "user_name": "dry run"}

    def place_order(self, exchange: str = "", tradingsymbol: str = "", transaction_type: str = "BUY", quantity: int = 0,
                    product: str = "NRML", order_type: str = "MARKET", price: Optional[float] = None,
                    trigger_price: Optional[float] = None, tag: str = "", market_protection: Optional[float] = None,
                    validity: str = "DAY", **_ignored) -> str:
        with _LOCK:
            d = self._load()
            d["n"] += 1
            o = {"order_id": f"DRY-{d['n']}", "status": "OPEN", "average_price": 0.0, "filled_quantity": 0, "quantity": int(quantity),
                 "tag": tag, "exchange": exchange, "tradingsymbol": tradingsymbol, "transaction_type": transaction_type,
                 "order_type": order_type, "product": product, "price": price, "trigger_price": trigger_price,
                 "status_message": None, "dry": True}
            if int(quantity) <= 0:
                o.update(status="REJECTED", status_message="dry run: quantity must be positive")
            elif order_type == "MARKET":
                ltp = self.ltp_for(tradingsymbol)
                if ltp is not None:
                    o.update(status="COMPLETE", average_price=float(ltp), filled_quantity=int(quantity))
            d["orders"].append(o)
            self._save(d)
            return o["order_id"]

    def modify_order(self, order_id: str, **kw) -> str:
        with _LOCK:
            d = self._load()
            o = self._find(d, order_id)
            if o["status"] != "OPEN":
                raise DryRunError(f"dry run: order {order_id} is {o['status'].lower()}, it cannot be modified")
            for k in ("quantity", "price", "trigger_price", "order_type"):
                if kw.get(k) is not None:
                    o[k] = int(kw[k]) if k == "quantity" else kw[k]
            self._save(d)
            return str(order_id)

    def cancel_order(self, order_id: str) -> str:
        with _LOCK:
            d = self._load()
            o = self._find(d, order_id)
            if o["status"] != "OPEN":
                raise DryRunError(f"dry run: order {order_id} is {o['status'].lower()}, it cannot be cancelled")
            o["status"] = "CANCELLED"
            self._save(d)
            return str(order_id)

    def mark(self, ltps: dict) -> int:
        """Fill the open orders the prices {tradingsymbol: ltp} cross. The number filled."""
        n = 0
        with _LOCK:
            d = self._load()
            for o in d["orders"]:
                if o["status"] != "OPEN":
                    continue
                ltp = ltps.get(o["tradingsymbol"])
                if ltp is None:
                    ltp = self.ltp_for(o["tradingsymbol"])
                if ltp is None:
                    continue
                px = _crossed(o, float(ltp))
                if px is not None:
                    o.update(status="COMPLETE", average_price=round(px, 2), filled_quantity=int(o["quantity"]))
                    n += 1
            if n:
                self._save(d)
        return n

    def orders(self) -> list[dict]:
        return [dict(o) for o in self._load()["orders"]]

    def clear(self) -> int:
        """Empty the book (the dry run is cleared). The number of orders dropped from the simulator."""
        with _LOCK:
            d = self._load()
            n = len(d["orders"])
            d["orders"] = []
            self._save(d)
            return n
