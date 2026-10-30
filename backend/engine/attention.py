"""The blinking signals on the 📈 Trend Breakout and 📝 Paper trading tabs: what is running or needs a look.

Levels, most urgent first: warn (something to fix: no Kite login, no scan, paper drifting from the backtest),
sell (a holding says SELL at open, or a paper stop / exit happened today), filter (a market filter changed at the
last close), buy (orders waiting for the next open), running (a backtest run in progress). `sig` changes whenever the
reasons do, so the browser can stop blinking once the tab was opened, until something new happens."""

import hashlib

from backend.engine import paper
from backend.engine import trend_breakout as tb

ORDER = ("warn", "sell", "filter", "buy", "running")


def _pack(items: list[tuple[str, str]]) -> dict:
    levels = [lvl for lvl, _ in items]
    level = next((lvl for lvl in ORDER if lvl in levels), None)
    ranked = sorted(items, key=lambda x: ORDER.index(x[0]))
    text = [t for _, t in ranked]
    return {"level": level, "items": [{"level": lvl, "text": t} for lvl, t in ranked],
            "sig": hashlib.sha1("|".join(text).encode()).hexdigest()[:12] if text else ""}


def signals(config, kite: bool) -> dict:
    bt, pp = [], []
    if tb.status()["state"] != "idle":
        bt.append(("running", "Trend Breakout backtest running"))
    scan = tb.saved_scan()
    if not kite:
        bt.append(("warn", "Kite not logged in: holdings not checked, candles not updated"))
    if not scan:
        bt.append(("warn", "No evening scan yet"))
    else:
        changed = [f"{k} {f['prev']} → {f['state']}" for k, f in (scan.get("filters") or {}).items() if f.get("changed")]
        if changed:
            bt.append(("filter", "Market filter changed: " + ", ".join(changed)))
            pp.append(("filter", "Market filter changed: " + ", ".join(changed)))
        sells = [h["symbol"] for h in scan.get("holdings") or [] if h["action"] != "HOLD"]
        if sells:
            bt.append(("sell", f"SELL at open: {', '.join(sells[:6])}" + (f" +{len(sells) - 6}" if len(sells) > 6 else "")))
        if scan.get("buys"):
            bt.append(("buy", f"{len(scan['buys'])} breakout(s) for the next open"))
    rep = paper.latest_report()
    if rep:
        for k, r in rep["accounts"].items():
            out = [f for f in r["fills"] if f["action"] == "SELL"]
            if out:
                pp.append(("sell", f"{k}: closed today {', '.join(f['symbol'] for f in out)}"))
            buys = [o for o in r["orders"] if o["action"] == "BUY"]
            free = max(0, r["cap"] - r["account"]["positions"])
            if buys and free and r.get("allowed"):
                pp.append(("buy", f"{k}: buy at the next open ({min(free, len(buys))})"))
            if any(o["action"] == "SELL" for o in r["orders"]):
                pp.append(("sell", f"{k}: sell at the next open"))
        try:
            for k, a in paper.summary(config).get("accounts", {}).items():
                cmp = a.get("compare") or {}
                if cmp and abs(cmp.get("return", 0) - a["account"]["return"]) > 0.25:
                    pp.append(("warn", f"{k}: paper {a['account']['return']:+.1f}% vs backtest {cmp['return']:+.1f}% "
                                       "(data or rules drifted)"))
        except Exception:
            pass
    return {"breakout": _pack(bt), "paper": _pack(pp), "real": _pack(_real(config, kite))}


def _real(config, kite: bool) -> list:
    from datetime import date

    from backend.database.models import get_db
    from backend.engine import real
    out = []
    s = real.summary(config)
    if s["killed"]:
        out.append(("warn", "Kill switch ON: nothing is sent"))
    if s["enabled"] and not s["token_set"]:
        out.append(("warn", "TRADETRON_TOKEN missing in .env: orders can't be sent"))
    if not s["enabled"]:
        return out
    if s.get("positions") and not kite:
        out.append(("warn", "Kite not logged in: stops are NOT watched"))
    today = date.today().isoformat()
    with get_db() as conn:
        bad = [r[0] for r in conn.execute("SELECT symbol FROM real_orders WHERE day = ? AND status != 'sent'", (today,))]
        stops = [r[0] for r in conn.execute("SELECT symbol FROM real_orders WHERE day = ? AND status = 'sent' AND "
                                            "reason LIKE 'stop%'", (today,))]
    if bad:
        out.append(("warn", f"Not sent today: {', '.join(bad)}"))
    if stops:
        out.append(("sell", f"Stop hit, EXIT sent: {', '.join(stops)}"))
    if s.get("filter", {}) and s["filter"].get("changed"):
        out.append(("filter", f"Filter {s['filter']['prev']} → {s['filter']['state']}"))
    orders = [o for o in s.get("orders", []) if not o.get("sent")]
    if orders:
        out.append(("buy", f"{len(orders)} order(s) for the next open"))
    return out
