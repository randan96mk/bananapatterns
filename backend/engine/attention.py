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
        bt.append(("warn", "Kite not logged in: candles not updated"))
    if not scan:
        bt.append(("warn", "No evening scan yet"))
    else:
        changed = [f"{k} {f['prev']} → {f['state']}" for k, f in (scan.get("filters") or {}).items() if f.get("changed")]
        if changed:
            bt.append(("filter", "Market filter changed: " + ", ".join(changed)))
            pp.append(("filter", "Market filter changed: " + ", ".join(changed)))
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
    out = {"breakout": _pack(bt), "paper": _pack(pp), "real": _pack(_real(config, kite))}
    try:
        out["futures"] = _pack(_futures(config, kite))
    except Exception:
        out["futures"] = _pack([])
    return out


def _futures(config, kite: bool) -> list:
    """📐 The futures tab: the plan for the next open, the morning dry run, a roll day, margin over 70%, the daily
    loss limit, the kill switch, held-name warnings, the short side's paper signals."""
    from datetime import datetime

    from backend.config import IST
    from backend.futures import book
    out = []
    if not book.has_account():
        return out
    cfg = config.futures
    s = book.summary(config)
    g = s.get("guards") or {}
    if g.get("kill"):
        out.append(("warn", "Futures kill switch ON: nothing is sent"))
    if any(s["sides"][x]["real"] for x in ("long", "short")) and s.get("positions") and not kite:
        out.append(("warn", "Kite not logged in: futures stops are NOT watched"))
    if g.get("margin", {}).get("warn"):
        out.append(("warn", f"Futures margin {g['margin']['pct']:.0f}% of capital (warn at {g['margin']['warn_at']:.0f}%)"))
    if g.get("daily_loss", {}).get("hit"):
        out.append(("sell", "Futures daily loss limit reached: no new lots today"))
    w = s.get("warnings") or {}
    if w.get("ban"):
        out.append(("warn", "MWPL ban on a held futures name: " + ", ".join(w["ban"])))
    if w.get("results"):
        out.append(("warn", "Results soon on a held futures name: " + ", ".join(r["symbol"] for r in w["results"])))
    if w.get("lot_change"):
        out.append(("warn", "Lot size change on a held futures name: " + ", ".join(c["symbol"] for c in w["lot_change"])))
    today = datetime.now(IST).date().isoformat()
    if s.get("roll_day") == today and (s.get("rolls") or {}).get("items"):
        out.append(("sell", f"Futures roll day (T-{cfg.roll_days}): {len(s['rolls']['items'])} lot(s) roll or exit at 9:20"))
    try:
        m = book.morning(config)
        if m.get("visible") and m.get("phase") != "after":
            out.append(("buy", f"Futures dry run: {m['going_out']['buys']} entr{'y' if m['going_out']['buys'] == 1 else 'ies'} · "
                               f"{m['going_out']['exits']} exit(s) · margin {m['going_out']['margin']:,.0f}"))
    except Exception:
        pass
    exits = [o for o in s.get("plan") or [] if o["action"] == "EXIT" and not o.get("sent")]
    entries = [o for o in s.get("plan") or [] if o["action"] != "EXIT" and not o.get("sent")]
    if exits:
        out.append(("sell", "Futures EXIT at the next open: " + ", ".join(o["symbol"] for o in exits)))
    if entries:
        out.append(("buy", f"{len(entries)} futures lot(s) for the next open: " + ", ".join(f"{o['symbol']} {o['side']}" for o in entries)))
    sp = s.get("short_paper") or {}
    if cfg.short_paper and sp.get("signals"):
        recent = [x for x in sp["signals"] if x["day"] == s.get("as_of")]
        if recent:
            out.append(("buy", f"paper · short: {len(recent)} E− signal(s) today"))
    if (s.get("filter") or {}).get("changed"):
        out.append(("filter", f"Filter {s['filter']['prev']} → {s['filter']['state']}"))
    return out


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
        bad = [r[0] for r in conn.execute("SELECT symbol FROM real_orders WHERE day = ? AND status != 'sent' AND action IN ('BUY', 'EXIT')", (today,))]
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
