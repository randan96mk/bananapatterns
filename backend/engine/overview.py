"""🏠 Home and 💼 Portfolio: every account the app runs, in one shape (read-only, built from the same data as the
Real, Paper and Intraday Flow pages). Accounts: Real (orders to Tradetron), Paper A and Paper E+ (swing, simulated),
Intraday (paper). Broker holdings are never read here.

A position / trade / order is {account, book, strategy, symbol, side, qty, ...}; book is real | paper_A | paper_E |
intraday, so the page can filter without asking again."""

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from backend.config import IST, AppConfig
from backend.database.models import get_db

NAMES = {"real": "Real", "paper_A": "Paper A", "paper_E": "Paper E+", "intraday": "Intraday"}
STRATEGY = {"A": "Trend Breakout A", "E": "Trend Breakout E+"}


def _pct(pnl, value):
    return round(pnl / value * 100, 2) if value else None


def _stop_dist(now, stop, side="long"):
    if not now or not stop:
        return None
    return round(((now - stop) if side == "long" else (stop - now)) / now * 100, 2)


def market_open(now: Optional[datetime] = None) -> bool:
    from datetime import time
    from backend.ingest import holidays
    now = now or datetime.now(IST)
    return holidays.is_trading_day(now.date()) and time(9, 15) <= now.time() <= time(15, 30)


def _mark_live(rows: list, quote) -> None:
    """In market hours the swing rows (Real, Paper) are marked at the last traded price from one Kite quote call
    (the stored price is the previous close, refreshed by the stop watch only every few minutes); each marked row
    gets live=True."""
    from backend.engine import trend_breakout as tb
    swing = [r for r in rows if r["book"] != "intraday"]
    if not swing or quote is None or not market_open():
        return
    try:
        q = quote([f"NSE:{r['symbol']}" for r in swing]) or {}
    except Exception:
        return
    for r in swing:
        ltp = (q.get(f"NSE:{r['symbol']}") or {}).get("last_price")
        if not ltp or not r.get("entry"):
            continue
        gross = (ltp - r["entry"]) * r["qty"]
        net = gross - tb.delivery_charges(ltp * r["qty"], True)
        r.update(now=round(ltp, 2), pnl=round(net, 2), pct=_pct(net, r["entry"] * r["qty"]), stop_dist=_stop_dist(ltp, r.get("stop")), live=True)


def positions(config: AppConfig, real_s: Optional[dict] = None, paper_s: Optional[dict] = None,
              live: Optional[dict] = None, quote=None) -> list:
    """Every open position, P&L after the costs an exit would take. quote: a Kite quote function (keys -> quotes) to
    mark the swing rows live in market hours."""
    from backend.engine import paper, real
    from backend.intraday import flow
    from backend.intraday import positions as ipos
    out = []
    real_s = real_s if real_s is not None else real.summary(config)
    for x in real_s.get("positions") or []:
        out.append({"book": "real", "account": NAMES["real"], "strategy": STRATEGY.get(x.get("variant"), x.get("variant") or ""),
                    "symbol": x["symbol"], "side": "long", "qty": x["qty"], "entry": x["buy"], "now": x["last"],
                    "pnl": x["net"], "pct": _pct(x["net"], x["buy"] * x["qty"]), "stop": x["stop"],
                    "stop_dist": _stop_dist(x["last"], x["stop"]), "opened": x["in"]})
    paper_s = paper_s if paper_s is not None else paper.summary(config)
    for k, a in (paper_s.get("accounts") or {}).items():
        for x in a.get("positions") or []:
            out.append({"book": f"paper_{k}", "account": NAMES.get(f"paper_{k}", f"Paper {k}"), "strategy": STRATEGY.get(k, k),
                        "symbol": x["symbol"], "side": "long", "qty": x["qty"], "entry": x["buy"], "now": x["last"],
                        "pnl": x["net"], "pct": _pct(x["net"], x["buy"] * x["qty"]), "stop": x["stop"],
                        "stop_dist": _stop_dist(x["last"], x["stop"]), "opened": x["in"]})
    live = live if live is not None else flow.live()
    today = datetime.now(IST).date().isoformat()
    by_sym = {p["symbol"]: p for p in ipos.load(today, ("open",))}
    for sym, r in (live.get("rows") or {}).items():
        p = by_sym.get(sym)
        if r.get("status") != "open" or not p:
            continue
        out.append({"book": "intraday", "account": "Intraday · signals (tracked)" if p.get("kind") == "signal" else NAMES["intraday"],
                    "strategy": "Intraday Flow", "symbol": sym,
                    "side": p["side"], "qty": p["open_qty"], "entry": p["entry"], "now": r.get("now"), "pnl": r.get("pnl"),
                    "pct": r.get("pct"), "stop": r.get("stop"), "stop_dist": _stop_dist(r.get("now"), r.get("stop"), p["side"]),
                    "opened": f"{today} {p.get('entry_t', '')}".strip()})
    _mark_live(out, quote)
    return sorted(out, key=lambda x: (x["book"] != "intraday", x["opened"]), reverse=False)


def _equity(table: str, account: Optional[str] = None) -> list:
    q = f"SELECT day, equity FROM {table}" + (" WHERE account = ?" if account else "") + " ORDER BY day"
    with get_db() as conn:
        return [(r[0], r[1]) for r in conn.execute(q, (account,) if account else ())]


def curves(config: AppConfig) -> dict:
    """{book: [[day, equity]]}: the swing accounts from their daily equity; intraday as capital + closed net a day."""
    from backend.engine import paper
    from backend.intraday import flow
    out = {"real": _equity("real_equity")}
    for k in paper.variants(config):
        out[f"paper_{k}"] = _equity("paper_equity", k)
    cap = flow.node_configs()["size"]["capital"]
    daily = defaultdict(float)
    with get_db() as conn:
        for r in conn.execute("SELECT day, payload FROM flow_positions WHERE status = 'closed' ORDER BY day"):
            daily[r[0]] += json.loads(r[1]).get("net") or 0
    eq, pts = cap, []
    for d in sorted(daily):
        eq += daily[d]
        pts.append((d, round(eq, 2)))
    out["intraday"] = pts
    return {k: v for k, v in out.items() if v}


def daily_pnl(cv: dict) -> dict:
    """{book: {day: P&L}} from the curves (the change from the session before)."""
    out = {}
    for k, pts in cv.items():
        out[k] = {d: round(e - pts[i - 1][1], 2) for i, (d, e) in enumerate(pts) if i}
    return out


def trades(config: AppConfig, days: int = 365) -> list:
    """Every closed trade of every account in the last `days` days, newest first."""
    since = (date.today() - timedelta(days=days)).isoformat()
    out = []
    with get_db() as conn:
        for (p,) in conn.execute("SELECT payload FROM real_trades"):
            t = json.loads(p)
            if t["out"] >= since:
                out.append({"book": "real", "account": NAMES["real"], "strategy": STRATEGY.get(t.get("variant"), t.get("variant") or ""),
                            "symbol": t["symbol"], "side": "long", "qty": t["qty"], "opened": t["in"], "closed": t["out"],
                            "entry": t["buy"], "exit": t["sell"], "gross": t["gross"], "costs": t["costs"], "net": t["net"],
                            "reason": t.get("why")})
        for acct, p in conn.execute("SELECT account, payload FROM paper_trades"):
            t = json.loads(p)
            if t["out"] >= since:
                out.append({"book": f"paper_{acct}", "account": NAMES.get(f"paper_{acct}", f"Paper {acct}"),
                            "strategy": STRATEGY.get(acct, acct), "symbol": t["symbol"], "side": "long", "qty": t["qty"],
                            "opened": t["in"], "closed": t["out"], "entry": t["buy"], "exit": t["sell"], "gross": t["gross"],
                            "costs": t["costs"], "net": t["net"], "reason": t.get("why")})
        for day, p in conn.execute("SELECT day, payload FROM flow_positions WHERE status = 'closed' AND day >= ?", (since,)):
            t = json.loads(p)
            sold = sum(e["price"] * e["qty"] for e in t.get("exits", []))
            out.append({"book": "intraday", "account": "Intraday · signals (tracked)" if t.get("kind") == "signal" else NAMES["intraday"],
                        "strategy": "Intraday Flow", "symbol": t["symbol"],
                        "side": t["side"], "qty": t["qty"], "opened": f"{day} {t.get('entry_t', '')}",
                        "closed": f"{day} {t.get('exit_t', '')}", "entry": t.get("entry"),
                        "exit": round(sold / max(t["qty"], 1), 2), "gross": t.get("gross"), "costs": t.get("costs"),
                        "net": t.get("net"), "reason": t.get("reason")})
    for t in out:
        t["pct"] = _pct(t["net"] or 0, (t["entry"] or 0) * t["qty"])
    return sorted(out, key=lambda t: t["closed"], reverse=True)


def orders(config: AppConfig, limit: int = 200) -> list:
    """Real orders sent to Tradetron and the paper fills, newest first."""
    out = []
    with get_db() as conn:
        for r in conn.execute("SELECT created_at, symbol, action, qty, reason, status, price FROM real_orders "
                              "ORDER BY id DESC LIMIT ?", (limit,)):
            out.append({"book": "real", "account": NAMES["real"], "at": r[0], "symbol": r[1], "action": r[2], "qty": r[3],
                        "price": r[6], "status": r[5], "note": r[4]})
        for acct, day, p in conn.execute("SELECT account, day, payload FROM paper_fills ORDER BY id DESC LIMIT ?", (limit,)):
            f = json.loads(p)
            out.append({"book": f"paper_{acct}", "account": NAMES.get(f"paper_{acct}", f"Paper {acct}"), "at": day,
                        "symbol": f["symbol"], "action": f["action"], "qty": f["qty"], "price": f.get("price"),
                        "status": "filled (simulated)", "note": f.get("why") or ""})
    return sorted(out, key=lambda o: o["at"] or "", reverse=True)[:limit]


_today_curve: list = []                           # [(ISO time, today's P&L)] sampled while the market is open


def _sample_today(today_pnl: float, now: datetime) -> list:
    """Today's P&L sampled about once a minute in market hours (in memory; a restart starts again)."""
    day = now.date().isoformat()
    if _today_curve and not _today_curve[0][0].startswith(day):
        _today_curve.clear()
    if market_open(now) and (not _today_curve or (now - datetime.fromisoformat(_today_curve[-1][0])).total_seconds() >= 55):
        _today_curve.append((now.isoformat(timespec="seconds"), round(today_pnl, 2)))
        del _today_curve[:-400]
    return [list(x) for x in _today_curve]


def stats(config: AppConfig, dp: dict, today: str) -> dict:
    """The strip: win streak, best / worst day in 30 days, days since a loss (all books' daily P&L), signals today
    and the 30-day intraday hit rate, real orders this week and failures."""
    byday = defaultdict(float)
    for d in dp.values():
        for day, v in d.items():
            byday[day] += v
    days = sorted(byday)
    recent = [(d, byday[d]) for d in days if d >= (date.fromisoformat(today) - timedelta(days=30)).isoformat()]
    streak = 0
    for d in reversed(days):
        if byday[d] > 0:
            streak += 1
        else:
            break
    since_loss = next((i for i, d in enumerate(reversed(days)) if byday[d] < 0), len(days))
    best = max(recent, key=lambda x: x[1], default=None)
    worst = min(recent, key=lambda x: x[1], default=None)
    since30 = (date.fromisoformat(today) - timedelta(days=30)).isoformat()
    monday = (date.fromisoformat(today) - timedelta(days=date.fromisoformat(today).weekday())).isoformat()
    with get_db() as conn:
        n_sig = conn.execute("SELECT COUNT(*) FROM flow_signals WHERE day = ?", (today,)).fetchone()[0]
        closed = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM flow_positions WHERE status = 'closed' AND day >= ?", (since30,))]
        orders = conn.execute("SELECT status, COUNT(*) FROM real_orders WHERE day >= ? GROUP BY status", (monday,)).fetchall()
    won = sum(1 for p in closed if (p.get("net") or 0) > 0)
    o = {r[0]: r[1] for r in orders}
    return {"win_streak": streak, "best_day": {"day": best[0], "net": round(best[1], 2)} if best else None,
            "worst_day": {"day": worst[0], "net": round(worst[1], 2)} if worst else None, "days_since_loss": since_loss,
            "signals_today": n_sig, "hit_rate_30d": round(100 * won / len(closed), 1) if closed else None, "closed_30d": len(closed),
            "orders_week": sum(o.values()), "orders_failed_week": o.get("failed", 0)}


def risk(config: AppConfig, accts: dict, pos: list) -> dict:
    """Money in open positions across every book against the total capital, the worst case to the stops, the
    biggest position, today's real orders, the intraday loss limit."""
    in_use = sum((p.get("entry") or 0) * (p.get("qty") or 0) for p in pos)
    worst = 0.0
    for p in pos:
        if p.get("stop") and p.get("now") and p.get("qty"):
            loss = (p["stop"] - p["now"]) * p["qty"] if p.get("side") != "short" else (p["now"] - p["stop"]) * p["qty"]
            worst += min(loss, 0)
    big = max(pos, key=lambda p: (p.get("entry") or 0) * (p.get("qty") or 0), default=None)
    capital = (float(config.real.capital) if accts.get("real") else 0) + sum(a.get("value") or 0 for k, a in accts.items() if k.startswith("paper_")) \
        + float((accts.get("intraday") or {}).get("capital") or 0)
    today = datetime.now(IST).date().isoformat()
    with get_db() as conn:
        o = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM real_orders WHERE day = ? GROUP BY status", (today,))}
    near = [p["symbol"] for p in pos if p.get("stop_dist") is not None and 0 <= p["stop_dist"] < 2]
    it = accts.get("intraday") or {}
    return {"in_use": round(in_use, 2), "capital": round(capital, 2), "worst_case": round(worst, 2),
            "biggest": {"symbol": big["symbol"], "value": round(big["entry"] * big["qty"], 2)} if big else None,
            "orders_today": {"sent": o.get("sent", 0), "failed": o.get("failed", 0)}, "near_stop": near,
            "loss_used": round(max(0.0, -(it.get("realised") or 0)), 2), "loss_limit": it.get("loss_limit")}


def timeline(today: str) -> list:
    """What happened today, in order: real orders, intraday signals, intraday exits, real exits."""
    ev = []
    with get_db() as conn:
        for r in conn.execute("SELECT created_at, symbol, action, qty, status FROM real_orders WHERE day = ? ORDER BY id", (today,)):
            ev.append({"t": r[0][11:16], "kind": "order" if r[4] == "sent" else "failed", "text": f"{r[2]} {r[1]} {r[3] or ''} · {r[4]}"})
        for at, p in conn.execute("SELECT at, payload FROM flow_signals WHERE day = ? ORDER BY at", (today,)):
            s = json.loads(p)
            ev.append({"t": at[11:16] if len(at) > 10 else at, "kind": "signal", "text": f"{s.get('signal', 'BUY')} signal {s['symbol']}"})
        for (p,) in conn.execute("SELECT payload FROM flow_positions WHERE day = ? AND status = 'closed'", (today,)):
            x = json.loads(p)
            ev.append({"t": x.get("exit_t"), "kind": "exit", "text": f"{x['symbol']} out · {x.get('reason')} · {signed_rs(x.get('net'))}"})
        for (p,) in conn.execute("SELECT payload FROM real_trades WHERE json_extract(payload, '$.out') = ?", (today,)):
            x = json.loads(p)
            ev.append({"t": "09:16", "kind": "exit", "text": f"Real · {x['symbol']} sold · {x.get('why') or ''} · {signed_rs(x.get('net'))}"})
    return sorted([e for e in ev if e.get("t")], key=lambda e: e["t"])


def signed_rs(v) -> str:
    return "" if v is None else f"{'+' if v >= 0 else '−'}₹{abs(v):,.0f}"


def pulse(config: AppConfig, live: dict) -> dict:
    """The market in one row: the regime from the last close (market health), and the live NIFTY vs VWAP, breadth
    above VWAP and advancing / declining while the intraday feed runs."""
    from backend.engine import market
    out = {}
    with get_db() as conn:
        h = market.latest(conn)
    if h:
        out.update(regime=h.get("status"), as_of=h.get("as_of"), breadth_50=(h.get("breadth") or {}).get("above_50_pct"),
                   dist_days=h.get("dist_days"), breakouts=(h.get("breakouts") or {}))
    rows = (live or {}).get("rows") or {}
    if live and live.get("feed") == "live":
        try:
            from backend.intraday import feed, flow
            r = feed.RUNNER
            if r and r.feed:
                up = dn = above = n = 0
                for tok, bars in r.feed.builder.bars.items():
                    if tok in (flow.NIFTY_TOKEN, flow.VIX_TOKEN) or not bars:
                        continue
                    n += 1
                    up += bars[-1]["c"] > bars[0]["o"]
                    dn += bars[-1]["c"] < bars[0]["o"]
                    v = sum(b["v"] for b in bars)
                    vw = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in bars) / v if v else None
                    above += bool(vw and bars[-1]["c"] > vw)
                nb = r.feed.builder.bars.get(flow.NIFTY_TOKEN) or []
                if nb:
                    out["nifty_vs_vwap"] = "above" if nb[-1]["c"] > sum(b["c"] for b in nb) / len(nb) else "below"
                if n:
                    out.update(advancing=up, declining=dn, above_vwap_pct=round(100 * above / n, 1), live=True)
        except Exception:
            pass
    out["signals_live"] = sum(1 for x in rows.values() if x.get("status") in ("open", "pending"))
    return out


def facts(config: AppConfig, accts: dict, pos: list, curve: list, n_signals: int) -> dict:
    """One fact per account card (the bottom-left of the card): today's high / low, Real's capital in use and
    slots, each paper book vs NIFTY and its filter state, the intraday loss limit and signals used, Follow's open
    book and the breakouts it buys at the next open."""
    out = {}
    if curve:
        hi = max(curve, key=lambda x: x[1])
        lo = min(curve, key=lambda x: x[1])
        out["today"] = {"high": hi[1], "high_at": hi[0][11:16], "low": lo[1], "low_at": lo[0][11:16]}
    real = [p for p in pos if p["book"] == "real"]
    if accts.get("real"):
        out["real"] = {"in_use": round(sum((p.get("entry") or 0) * (p.get("qty") or 0) for p in real), 2),
                       "slots": len(real), "max_pos": config.breakout.max_pos, "capital": float(config.real.capital)}
    try:
        from backend.engine import trend_breakout as tb
        filters = (tb.saved_scan() or {}).get("filters") or {}
    except Exception:
        filters = {}
    for k in ("A", "E"):
        a = accts.get(f"paper_{k}")
        if a:
            out[f"paper_{k}"] = {"vs_nifty": round((a.get("return") or 0) - (a.get("nifty") or 0), 2) if a.get("nifty") is not None else None,
                                 "filter": (filters.get(k) or {}).get("state"), "open": a.get("positions")}
    it = accts.get("intraday") or {}
    try:
        from backend.intraday import flow
        per_day = flow.node_configs()["dedup"].get("per_day")
    except Exception:
        per_day = None
    out["intraday"] = {"loss_used": round(max(0.0, -(it.get("realised") or 0)), 2), "loss_limit": it.get("loss_limit"),
                       "signals": n_signals, "per_day": per_day}
    f = accts.get("follow")
    if f:
        out["follow"] = {"open": f.get("open"), "open_pnl": f.get("open_pnl"), "pending": f.get("pending")}
    return out


def overview(config: AppConfig, quote=None) -> dict:
    """Home: each account's figures, today's P&L across them, every open position, the latest activity, plus the
    sparklines, the stats strip, risk, today's timeline, the live P&L curve, the market pulse and a fact per card."""
    from backend.engine import paper, real
    from backend.intraday import flow
    real_s, paper_s, live = real.summary(config), paper.summary(config), flow.live()
    cv = curves(config)
    dp = daily_pnl(cv)
    accts = {}
    if real_s.get("started"):
        a = real_s["account"]
        last = cv.get("real", [])[-1][0] if cv.get("real") else None
        accts["real"] = {"name": "Real · Tradetron", "value": a["value"], "net": a["net"], "return": a["return"],
                         "last_day": last, "day_pnl": dp.get("real", {}).get(last, 0.0) if last else 0.0,
                         "positions": len(real_s.get("positions") or []),
                         "on": bool(real_s.get("enabled") and not real_s.get("killed")), "variant": real_s.get("variant"),
                         "spark": [e for _, e in cv.get("real", [])[-30:]]}
    for k, a in (paper_s.get("accounts") or {}).items():
        b = f"paper_{k}"
        last = cv.get(b, [])[-1][0] if cv.get(b) else None
        accts[b] = {"name": NAMES.get(b, b), "value": a["account"]["value"], "net": a["account"]["net"],
                    "return": a["account"]["return"], "nifty": a["account"].get("nifty"), "last_day": last,
                    "day_pnl": dp.get(b, {}).get(last, 0.0) if last else 0.0, "positions": len(a.get("positions") or []),
                    "spark": [e for _, e in cv.get(b, [])[-30:]]}
    st = live.get("stats") or {}
    accts["intraday"] = {"name": "Intraday · paper", "day_pnl": round((st.get("realised") or 0) + (st.get("unrealised") or 0), 2),
                         "realised": st.get("realised"), "open_pnl": st.get("unrealised"), "trades": st.get("trades"),
                         "open": st.get("open"), "at_risk": st.get("at_risk"), "loss_limit": st.get("loss_limit"),
                         "capital": st.get("capital"), "mode": config.flow.mode, "spark": [e for _, e in cv.get("intraday", [])[-30:]]}
    try:                                                    # 📈 Follow: every breakout bought blindly (read-only)
        from backend.engine import follow
        accts["follow"] = follow.summary()
    except Exception:
        pass
    act = [{"at": o["at"], "account": o["account"], "text": f"{o['action']} {o['symbol']} {o['qty'] or ''} · {o['status']}"}
           for o in orders(config, 12)]
    with get_db() as conn:
        today = datetime.now(IST).date().isoformat()
        for at, p in conn.execute("SELECT at, payload FROM flow_signals WHERE day = ? ORDER BY at DESC LIMIT 8", (today,)):
            s = json.loads(p)
            act.append({"at": at, "account": "Intraday", "text": f"{s.get('signal', 'BUY')} signal {s['symbol']}"})
    act.sort(key=lambda x: x["at"] or "", reverse=True)
    now = datetime.now(IST)
    pos = positions(config, real_s, paper_s, live, quote)
    today_pnl = round(sum(a.get("day_pnl") or 0 for a in accts.values()), 2)
    extra = {}
    for name, fn in (("stats", lambda: stats(config, dp, today)), ("risk", lambda: risk(config, accts, pos)),
                     ("timeline", lambda: timeline(today)), ("pulse", lambda: pulse(config, live)),
                     ("today_curve", lambda: _sample_today(today_pnl, now))):
        try:
            extra[name] = fn()
        except Exception as exc:                      # one broken panel never blanks Home
            extra[name] = {"error": str(exc)[:120]} if name != "timeline" and name != "today_curve" else []
    try:
        extra["facts"] = facts(config, accts, pos, extra.get("today_curve") or [], (extra.get("stats") or {}).get("signals_today") or 0)
    except Exception as exc:
        extra["facts"] = {"error": str(exc)[:120]}
    return {"at": now.isoformat(timespec="seconds"), "accounts": accts, "today": today_pnl, "positions": pos,
            "activity": act[:10], "feed": live.get("feed"), **extra}


def portfolio(config: AppConfig, days: int = 365, quote=None) -> dict:
    """Portfolio: positions, trades, orders, the daily P&L and the curves of every account (filtered on the page)."""
    cv = curves(config)
    return {"positions": positions(config, quote=quote), "trades": trades(config, days), "orders": orders(config),
            "daily": daily_pnl(cv), "curves": cv, "names": NAMES}
