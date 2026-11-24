"""The blinking signals on the 📈 Trend Breakout, 📝 Paper trading, 💰 Real and 🧩 Options tabs: what is
running or needs a look.

Levels, most urgent first: warn (something to fix: no Kite login, no scan, paper drifting from the backtest),
sell (a holding says SELL at open, or a paper stop / exit happened today), filter (a market filter changed at the
last close), buy (orders waiting for the next open), running (a backtest run in progress). `sig` changes whenever the
reasons do, so the browser can stop blinking once the tab was opened, until something new happens."""

import hashlib

from backend.engine import paper
from backend.engine import trend_breakout as tb

ORDER = ("warn", "sell", "filter", "buy", "running", "info")      # 🤖 ask: "info" = worth a look, never blinks loud


def _pack(items: list[tuple]) -> dict:
    """(level, text) or (level, text, meta): meta is a small second line under the item on Home."""
    levels = [x[0] for x in items]
    level = next((lvl for lvl in ORDER if lvl in levels), None)
    ranked = sorted(items, key=lambda x: ORDER.index(x[0]))
    text = [x[1] for x in ranked]
    return {"level": level, "items": [{"level": x[0], "text": x[1], **({"meta": x[2]} if len(x) > 2 and x[2] else {})} for x in ranked],
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
        out["options"] = _pack(_options(config, kite))
    except Exception:
        out["options"] = _pack([])
    try:
        out["ai"] = _pack(_ai(config))
    except Exception:
        out["ai"] = _pack([])
    try:                                                   # ⚡ Tips → Kite: a red order check names the hand action
        from backend.tips import kite_check as kc
        from backend.tips import vision as vs              # 📷 an open screenshot strike mismatch
        out["tips"] = _pack(vs.attention_items() + kc.attention_items())
    except Exception:
        out["tips"] = _pack([])
    try:                                                   # ₿ the BTC feed behind or failing (alerts only)
        from backend.crypto import feed as crypto_feed
        out["crypto"] = _pack(crypto_feed.attention_items(config))
    except Exception:
        out["crypto"] = _pack([])
    return out


def _ai(config, now=None) -> list:
    """🤖 The AI page: the breaker open (jobs are skipped until it closes), a model download that failed, and last
    night's high notes ("sell": a held name, "warn": a plan or watchlist name) as "🤖 SYMBOL · title"."""
    from datetime import datetime

    from backend.ai import control, guard, notes
    from backend.config import IST
    out = []
    b = guard.breaker()
    if b["open"]:
        out.append(("warn", f"AI breaker {guard.breaker_text(b)}: jobs are skipped until then"))
    p = control.pull_status()
    if p.get("error"):
        out.append(("warn", f"AI model download failed: {p['error']}"[:160]))
    for n in notes.high_notes(notes.night_day(now or datetime.now(IST), config)):
        out.append(("sell" if n["book"] == "held" else "warn", f"🤖 {n['symbol']} · {n['title']}"))
    try:                                                     # 🤖 suggestions: the open high-confidence ones
        from backend.ai import suggest
        out += suggest.attention_items(config)
    except Exception:
        pass
    try:                                                     # 🤖 ask: a question that got no answer today
        from backend.ai import ask
        for a in ask.failed_today(now):
            out.append(("info", f"🤖 ask not answered: {a['question'][:60]} ({a['error'][:60]})"))
    except Exception:
        pass
    return out


NEAR_STOP = 0.8          # 🧩 the sold premium has covered 80% of the way from its entry to the premium stop


def _options(config, kite: bool, now=None) -> list:
    """🧩 The options tab: the kill switch, an open spread near its premium stop (within 20% of the way left), the daily
    loss limit hit, no data today (Kite login missing on a trading day). Reads the book's state only: no Kite call."""
    from datetime import datetime

    from backend.config import IST
    from backend.ingest.holidays import is_trading_day
    from backend.options import book
    out = []
    st = book._state()
    if not st:
        return out
    now = now or datetime.now(IST)
    d = st.get("day") or {}
    if d.get("date") != now.date().isoformat():
        d = {}
    if book.killed():
        out.append(("warn", "Options kill switch ON: no new spreads" + (" · the open spread is closed at once" if st.get("open") else "")))
    o = st.get("open")
    for x in book.spreads_of(o):                              # the afternoon pair: each side
        s = x["sold"]
        way = x["stop_premium"] - s["entry"]
        if way > 0 and s.get("last") is not None and s["last"] >= s["entry"] + NEAR_STOP * way:
            out.append(("sell", f"Options spread near its stop: {s['strike']:,.0f} {s['type']} at {s['last']:.2f} vs stop {x['stop_premium']:.2f}"))
    if o:
        if not kite:
            out.append(("warn", "Kite not logged in: the open options spread is NOT marked (the 15:20 hard exit still closes it)"))
    if book._daily_loss(st, config.options)["hit"]:
        out.append(("sell", "Options daily loss limit reached: no new spreads today"))
    if d.get("no_data") and is_trading_day(now.date()):
        out.append(("warn", f"Options: {d.get('why') or 'no data today'}"))
    return out


def _real(config, kite: bool) -> list:
    from backend.database.models import get_db
    from backend.engine import real
    out = []
    s = real.summary(config)
    if s["killed"]:
        out.append(("warn", "Kill switch ON: nothing is sent"))
    if s["enabled"] and not s["token_set"]:
        out.append(("warn", "webhook token missing in .env: orders can't be sent"))
    if not s["enabled"]:
        return out
    if s.get("positions") and not kite:
        out.append(("warn", "Kite not logged in: stops are NOT watched"))
    from datetime import datetime

    from backend.config import IST
    today = datetime.now(IST).date().isoformat()          # the gate's day is an IST day (the server clock may be UTC)
    held = [p["symbol"] for p in s.get("positions") or [] if not p.get("confirmed") and (p.get("gate") or {}).get("day") == today]
    if held:                                              # the EXIT gate held an EXIT: red, until confirmed or removed
        out.append(("warn", f"Real · {', '.join(held)}: stop hit but the buy was never confirmed · no EXIT sent · "
                            "confirm (✓ filled) or remove (✗ never filled)"))
    waiting = [p["symbol"] for p in s.get("positions") or [] if not p.get("confirmed") and p["symbol"] not in held]
    if waiting and real.gate_on(config):                  # the gate off (the default): confirmation is not asked for
        out.append(("info", f"Real · fill not confirmed yet: {', '.join(waiting)} · no EXIT goes out until it is"))
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
    try:                                                  # 📈 the candle model on a held name (cards only; informational)
        from backend.market import real_reads as rr
        out += rr.attention_items(config)
    except Exception:
        pass
    return out
