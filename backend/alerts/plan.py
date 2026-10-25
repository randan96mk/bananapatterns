"""Daily plan: rank tonight's Forming setups for the next session, keep the list, and judge the open.

Rank = VCP score (or 0.6 x RS when a name has no VCP score) - 2 per % under the pivot
       + 10 for an A setup + 8 when coiled + 4 when funds are buying (bulk / block deals, stake raises).
Coiled = the last bar is an NR7 (narrowest range of the last 7) or an inside day, on volume
         <= coiled_volume x the 50-day average, within coiled_within_pct under the pivot:
         the calm that often comes right before a breakout.
"""

import json
from typing import Optional

from backend.alerts.messages import SCREEN_PREF, setup_stop
from backend.config import AlertsConfig, VcpConfig
from backend.database.models import get_candles, get_db, now_ist
from backend.engine import market
from backend.ingest import events, holders

TOP_BONUS, COILED_BONUS, DIST_WEIGHT, FUND_BONUS = 10.0, 8.0, 2.0, 4.0


def coiled(candles: list[dict], dist_pct: Optional[float], cfg: AlertsConfig) -> Optional[dict]:
    """{'nr7', 'inside', 'volume_x'} when the last bar is coiled under the pivot, else None."""
    if len(candles) < 51 or dist_pct is None or dist_pct > cfg.coiled_within_pct:
        return None
    last, prev = candles[-1], candles[-2]
    ranges = [c["high"] - c["low"] for c in candles[-7:]]
    nr7 = ranges[-1] <= min(ranges)
    inside = last["high"] <= prev["high"] and last["low"] >= prev["low"]
    avg = sum(c["volume"] for c in candles[-51:-1]) / 50
    vol_x = last["volume"] / avg if avg else None
    if (nr7 or inside) and vol_x is not None and vol_x <= cfg.coiled_volume:
        return {"nr7": nr7, "inside": inside, "volume_x": round(vol_x, 2)}
    return None


def rank_score(p: dict, tier: Optional[str], coil: Optional[dict]) -> float:
    base = p["score"] if p.get("score") is not None else 0.6 * (p.get("rs") or 50)
    return round(base - DIST_WEIGHT * (p.get("dist_to_pivot_pct") or 0) + (TOP_BONUS if tier == "A" else 0)
                 + (COILED_BONUS if coil else 0), 1)


def candidates(as_of: str, screens: list[str]) -> list[dict]:
    """Forming names on the alert screens (VCP payload preferred when a name is on both)."""
    order = {s: SCREEN_PREF.get(s, 3) for s in screens}
    out = {}
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT screen, symbol, payload FROM scan_results WHERE as_of = ? AND stage = 'forming' AND screen IN "
            f"({','.join('?' * len(screens))})", (as_of, *screens)).fetchall()
    for r in sorted(rows, key=lambda r: order[r["screen"]]):
        out.setdefault(r["symbol"], dict(json.loads(r["payload"]), screen=r["screen"]))
    return list(out.values())


def soon(r: Optional[dict], cfg: AlertsConfig) -> Optional[dict]:
    """The results entry when it falls within results_warn_sessions, else None."""
    return r if r and r["sessions"] <= cfg.results_warn_sessions else None


def build_plan(as_of: str, cfg: AlertsConfig, vcp_cfg: VcpConfig, tokens: dict[str, int]) -> list[dict]:
    """Top plan_top_n setups for the session after `as_of`, best first."""
    with get_db() as conn:
        h = market.latest(conn, as_of)
    status = h["status"] if h else None
    items = []
    cands = candidates(as_of, cfg.screens)
    upcoming = events.upcoming_results(as_of, [p["symbol"] for p in cands]) if cfg.results_calendar else {}
    flags = holders.signals([p["symbol"] for p in cands], cfg, as_of)
    for p in cands:
        tok = tokens.get(p["symbol"])
        with get_db() as conn:
            cs = get_candles(conn, tok, limit=60) if tok is not None else []
        coil = coiled(cs, p.get("dist_to_pivot_pct"), cfg)
        t = market.tier(p, status, cfg) if cfg.tiering else None
        pivot = p["pivot"]
        stop = setup_stop(p) or round(pivot * (1 - vcp_cfg.max_stop), 2)
        items.append({"symbol": p["symbol"], "screen": p["screen"], "base_label": p.get("base_label"), "pivot": pivot,
                      "zone_top": round(pivot * (1 + cfg.buy_zone_pct), 2), "stop": stop, "close": p.get("close"),
                      "dist_to_pivot_pct": p.get("dist_to_pivot_pct"), "rs": p.get("rs"), "score": p.get("score"),
                      "tier": t, "coiled": coil, "payload": p, "results": soon(upcoming.get(p["symbol"]), cfg),
                      "flags": flags.get(p["symbol"]),
                      "rank": rank_score(p, t, coil) + (FUND_BONUS if (flags.get(p["symbol"]) or {}).get("buy")
                                                        else 0)})
    items.sort(key=lambda i: -i["rank"])
    return items[:cfg.plan_top_n]


def save_plan(as_of: str, items: list[dict]) -> None:
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO daily_plan (as_of, created_at, payload) VALUES (?, ?, ?)",
                     (as_of, now_ist(), json.dumps(items)))


def latest_plan(before: Optional[str] = None) -> tuple[Optional[str], list[dict]]:
    """(as_of, items) of the newest plan, optionally made before a date (the morning uses the previous evening's)."""
    q, args = "SELECT as_of, payload FROM daily_plan", []
    if before:
        q, args = q + " WHERE as_of < ?", [before]
    with get_db() as conn:
        row = conn.execute(q + " ORDER BY as_of DESC LIMIT 1", args).fetchone()
    return (row["as_of"], json.loads(row["payload"])) if row else (None, [])


def gap_verdict(open_px: float, item: dict) -> Optional[str]:
    """How the session opened for a plan name: chase / above / broken, or None (nothing to say)."""
    if open_px > item["zone_top"]:
        return "chase"
    if open_px > item["pivot"]:
        return "above"
    if open_px < item["stop"]:
        return "broken"
    return None
