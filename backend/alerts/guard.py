"""Position guard: follow what you hold (Kite holdings + open CNC positions, read-only) and alert on
the sell rules after the evening scan. No orders are ever placed.

Each new holding is matched to its setup: pivot and stop from the latest scan (VCP stop, else
pivot x (1 - max_stop)); without a setup, stop = average price x (1 - max_stop). The stop can be
changed on the dashboard. Rules (each alert once; the stop alert repeats daily while it holds):
  stop      close below the stop
  ma50      close below the 50-day line
  profit    gain from the average >= profit_zone_pct: consider selling part
  extended  close >= extended_pct above the 50-day line
  failed    back under the pivot within failed_days sessions of first holding it
"""

import json
from typing import Optional

from backend.config import AlertsConfig, VcpConfig
from backend.database.models import get_candles, get_db, now_ist
from backend.logging_config.logger import setup_logger

logger = setup_logger("guard")


def kite_holdings(client) -> dict[str, dict]:
    """{symbol: {qty, avg}} from holdings plus today's CNC buys (net positions)."""
    out: dict[str, dict] = {}
    for h in client.holdings() or []:
        qty = (h.get("quantity") or 0) + (h.get("t1_quantity") or 0)
        if qty > 0:
            out[h["tradingsymbol"]] = {"qty": qty, "avg": float(h.get("average_price") or 0)}
    for p in (client.positions() or {}).get("net", []):
        if p.get("product") == "CNC" and (p.get("quantity") or 0) > 0 and p["tradingsymbol"] not in out:
            out[p["tradingsymbol"]] = {"qty": p["quantity"], "avg": float(p.get("average_price") or 0)}
    return {k.removesuffix("-BE"): v for k, v in out.items()}


def _setup(conn, symbol: str, as_of: Optional[str]) -> Optional[dict]:
    row = conn.execute("SELECT payload FROM scan_results WHERE as_of = ? AND symbol = ? ORDER BY screen = 'vcp' DESC "
                       "LIMIT 1", (as_of, symbol)).fetchone() if as_of else None
    return json.loads(row["payload"]) if row else None


def _initial_stop(conn, symbol: str, avg: float, as_of: Optional[str], vcp_cfg: VcpConfig) -> tuple:
    """(pivot, stop, where the stop came from): VCP stop, else pivot - max_stop, else average - max_stop."""
    p = _setup(conn, symbol, as_of)
    pivot = p.get("pivot") if p else None
    if p and (p.get("vcp") or {}).get("stop"):
        return pivot, p["vcp"]["stop"], "setup (VCP stop)"
    if pivot:
        return pivot, round(pivot * (1 - vcp_cfg.max_stop), 2), f"pivot −{vcp_cfg.max_stop * 100:g}%"
    return None, round(avg * (1 - vcp_cfg.max_stop), 2), f"average −{vcp_cfg.max_stop * 100:g}%"


def sync(held: dict[str, dict], vcp_cfg: VcpConfig) -> dict:
    """Update the followed positions from Kite. Returns {added: [...], closed: [...]}"""
    added, closed, now = [], [], now_ist()
    with get_db() as conn:
        as_of = conn.execute("SELECT MAX(as_of) FROM scan_runs").fetchone()[0]
        rows = conn.execute("SELECT symbol, source FROM guard_positions").fetchall()
        known = {r["symbol"] for r in rows}
        from_kite = {r["symbol"] for r in rows if r["source"] == "kite"}
        for sym, h in held.items():
            if sym in known:                               # a manual ("I bought") entry becomes Kite-backed
                conn.execute("UPDATE guard_positions SET qty = ?, avg_price = ?, source = 'kite', updated_at = ? "
                             "WHERE symbol = ?", (h["qty"], h["avg"], now, sym))
                continue
            pivot, stop, src = _initial_stop(conn, sym, h["avg"], as_of, vcp_cfg)
            conn.execute("INSERT INTO guard_positions (symbol, qty, avg_price, first_seen, pivot, stop, stop_source, "
                         "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (sym, h["qty"], h["avg"], as_of or now[:10], pivot, stop, src, now))
            added.append(sym)
        for sym in from_kite - set(held):                  # sold; manual entries stay until removed
            conn.execute("DELETE FROM guard_positions WHERE symbol = ?", (sym,))
            closed.append(sym)
    return {"added": added, "closed": closed}


def add_manual(symbol: str, price: float, vcp_cfg: VcpConfig) -> dict:
    """Follow a position from the bot's "I bought" button (qty 0 until Kite holdings show it)."""
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM guard_positions WHERE symbol = ?", (symbol,)).fetchone():
            as_of = conn.execute("SELECT MAX(as_of) FROM scan_runs").fetchone()[0]
            pivot, stop, src = _initial_stop(conn, symbol, price, as_of, vcp_cfg)
            conn.execute("INSERT INTO guard_positions (symbol, qty, avg_price, first_seen, pivot, stop, stop_source, "
                         "updated_at, source) VALUES (?, 0, ?, ?, ?, ?, ?, ?, 'manual')",
                         (symbol, price, as_of or now_ist()[:10], pivot, stop, src, now_ist()))
    return next(p for p in positions() if p["symbol"] == symbol)


def positions() -> list[dict]:
    with get_db() as conn:
        return [dict(r, flags=json.loads(r["flags"])) for r in conn.execute(
            "SELECT * FROM guard_positions ORDER BY symbol")]


def set_stop(symbol: str, stop: float) -> bool:
    with get_db() as conn:
        cur = conn.execute("UPDATE guard_positions SET stop = ?, stop_source = 'manual', updated_at = ? "
                           "WHERE symbol = ?", (stop, now_ist(), symbol))
    return cur.rowcount == 1


def evaluate(pos: dict, candles: list[dict], cfg: AlertsConfig) -> dict:
    """Today's numbers and which rules fire (before de-duplication)."""
    c = candles[-1]["close"]
    closes = [x["close"] for x in candles]
    ma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
    days = sum(1 for x in candles if x["date"] > pos["first_seen"])
    risk = pos["avg_price"] - pos["stop"]
    gain = (c / pos["avg_price"] - 1) * 100 if pos["avg_price"] else 0.0
    fire = {
        "stop": c < pos["stop"],
        "ma50": ma50 is not None and c < ma50,
        "profit": gain >= cfg.profit_zone_pct,
        "extended": ma50 is not None and c >= ma50 * (1 + cfg.extended_pct / 100),
        "failed": pos.get("pivot") is not None and days <= cfg.failed_days and c < pos["pivot"],
    }
    return {"close": c, "ma50": ma50, "gain_pct": round(gain, 2), "r": round((c - pos["avg_price"]) / risk, 2)
            if risk > 0 else None, "days": days, "fire": fire, "date": candles[-1]["date"]}


def run(as_of: str, tokens: dict[str, int], cfg: AlertsConfig, notifier, decorate=lambda h: h) -> dict:
    """Evening checks for every followed position; sends rule alerts and one summary."""
    from backend.alerts import messages as msg
    rows, sent = [], 0
    for pos in positions():
        tok = tokens.get(pos["symbol"])
        with get_db() as conn:
            cs = get_candles(conn, tok, limit=60) if tok is not None else []
        if not cs:
            continue
        ev = evaluate(pos, cs, cfg)
        rows.append((pos, ev))
        flags = dict(pos["flags"])
        for rule, on in ev["fire"].items():
            if not on:
                continue
            key = f"{as_of}|guard|{pos['symbol']}|stop" if rule == "stop" else f"guard|{pos['symbol']}|{rule}"
            if rule != "stop" and flags.get(rule):
                continue                                      # once per position
            if notifier.send(key, "guard", decorate(msg.guard_alert(rule, pos, ev, cfg)), pos["symbol"]):
                sent += 1
                flags[rule] = ev["date"]
        if flags != pos["flags"]:
            with get_db() as conn:
                conn.execute("UPDATE guard_positions SET flags = ? WHERE symbol = ?", (json.dumps(flags), pos["symbol"]))
    if rows and notifier.send(f"{as_of}|guard-summary", "guard", decorate(msg.guard_summary(rows, as_of))):
        sent += 1
    return {"positions": len(rows), "sent": sent}
