"""A/B priority and the market header for alerts.

Buy-side alerts (a new setup or entry) are tiered: A goes out at once, tagged 🅰️; B follows
b_alerts (digest only / push tagged 🅱️ / off). In a correction, correction_mode "mute_buys"
holds all buy-side alerts; "caution" adds a warning line. Sell-side alerts (below pivot,
played out, failed breakout, didn't confirm) and system alerts are never held back.
"""

from dataclasses import dataclass
from typing import Optional

from backend.config import AlertsConfig
from backend.database.models import get_db
from backend.engine import market

BUY_KINDS = {"breakout", "breakout_low_volume", "forming", "near_pivot", "intraday", "close_check"}
TAG = {"A": "🅰️ ", "B": "🅱️ "}


@dataclass
class MarketContext:
    health: Optional[dict]
    status: Optional[str]
    line: Optional[str]                 # HTML line for the top of alerts (None when off / no data)
    size_factor: float
    size_note: str
    watched: frozenset = frozenset()    # bot 📌 Watch: always alert
    muted: frozenset = frozenset()      # bot 🔕 Mute: no buy-side alerts

    @classmethod
    def load(cls, cfg: AlertsConfig, on_or_before: Optional[str] = None,
             index_today: Optional[float] = None) -> "MarketContext":
        from backend.alerts import messages as msg
        with get_db() as conn:
            h = market.latest(conn, on_or_before)
            watched = frozenset(r[0] for r in conn.execute("SELECT symbol FROM watchlist"))
            muted = frozenset(r[0] for r in conn.execute("SELECT symbol FROM muted WHERE until >= date('now')"))
        status = h["status"] if h else None
        sizing = cfg.market_sizing and status is not None
        return cls(h, status, msg.market_line(h, index_today) if (h and cfg.market_line) else None,
                   market.SIZE_FACTOR[status] if sizing else 1.0, market.SIZE_NOTE[status] if sizing else "",
                   watched, muted)


def gate(kind: str, p: dict, mc: MarketContext, cfg: AlertsConfig) -> tuple[bool, Optional[str]]:
    """(send individually?, tier A/B or None when not tiered)."""
    if kind not in BUY_KINDS:
        return True, None
    sym = p.get("symbol")
    if sym in mc.muted:
        return False, None
    if mc.status == market.CORRECTION and cfg.correction_mode == "mute_buys":
        return False, None
    if sym in mc.watched:
        return True, (market.tier(p, mc.status, cfg) if cfg.tiering else None)
    if not cfg.tiering:
        return True, None
    t = market.tier(p, mc.status, cfg)
    return (t == "A" or cfg.b_alerts == "push"), t


def decorate(html: str, kind: str, t: Optional[str], mc: MarketContext) -> str:
    """Market line on top, A/B tag on the first line, correction caution for buy-side alerts."""
    out = TAG.get(t, "") + html
    if kind in BUY_KINDS and mc.status == market.CORRECTION:
        out += "\n🔴 <b>Market in correction</b>: smaller size, or skip."
    return f"{mc.line}\n{out}" if mc.line else out
