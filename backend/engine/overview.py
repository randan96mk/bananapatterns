"""🏠 Home and 💼 Portfolio: every account the app runs, in one shape (read-only, built from the same data as the
Real, Paper and Intraday Flow pages). Accounts: Real (orders to Tradetron), Paper E+ (swing, simulated), Intraday
(paper). Broker holdings are never read here.

A position / trade / order is {account, book, strategy, symbol, side, qty, ...}; book is real | paper_E | intraday,
so the page can filter without asking again."""

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from backend.config import IST, AppConfig
from backend.database.models import get_db, get_setting

NAMES = {"real": "Real", "paper_E": "Paper E+", "intraday": "Intraday", "futures": "Futures"}
STRATEGY = {"E": "Trend Breakout E+"}
FUT_STRATEGY = {"long": "Futures E+ long", "short": "Futures E− short"}


def _futures_summary(config: AppConfig) -> Optional[dict]:
    """The futures book's summary (None when it has not started or fails: Home never blanks for it)."""
    try:
        from backend.futures import book
        s = book.summary(config)
        return s if s.get("started") else None
    except Exception:
        return None


def _futures_rows(fs: Optional[dict]) -> list:
    """The futures book's open lots as Home position rows (book 'futures', account 'Futures · paper' / 'Futures · real')."""
    out = []
    for x in (fs or {}).get("positions") or []:
        now, entry, qty = x["last"], x["entry"], x["qty"]
        out.append({"book": "futures", "account": f"Futures · {x['book']}", "strategy": FUT_STRATEGY.get(x["side"], "Futures"),
                    "symbol": x["symbol"], "side": x["side"], "qty": qty, "lots": x["lots"], "entry": entry, "now": now, "pnl": x["net"],
                    "pct": _pct(x["net"], entry * qty), "stop": x["stop"], "stop_dist": _stop_dist(now, x["stop"], x["side"]), "opened": x["in"],
                    "contract": x.get("contract"), "expiry": x.get("expiry"), "live": bool(x.get("live"))})
    return out


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
    swing = [r for r in rows if r["book"] not in ("intraday", "futures")]
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
              live: Optional[dict] = None, quote=None, fut_s: Optional[dict] = None) -> list:
    """Every open position, P&L after the costs an exit would take. quote: a Kite quote function (keys -> quotes) to
    mark the swing rows live in market hours. The futures lots come marked by their own stop watch."""
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
    _add_trail(config, out, real_s, paper_s)
    out += _futures_rows(fut_s if fut_s is not None else _futures_summary(config))
    _add_results(out, today)
    return sorted(out, key=lambda x: (x["book"] != "intraday", x["opened"]), reverse=False)


RESULTS_DAYS = 7                                  # a results date this close shows on the position and the strip


def _trail(x: dict, p) -> Optional[dict]:
    """The E+ late trail of one swing row (real.summary / paper._one: buy, best, trailed): active once the best close
    is +late_at % over the entry; pct_to_arm = the % still needed (0 when active); raised = the trail set the stop."""
    if not p or not p.late_at or not x.get("buy"):
        return None
    best = x.get("best") or max(x["buy"], x.get("last") or x["buy"])
    gain = (best / x["buy"] - 1) * 100
    active = bool(x.get("trail_active")) or best >= x["buy"] * (1 + p.late_at / 100)
    return {"active": active, "pct_to_arm": 0.0 if active else round(max(0.0, p.late_at - gain), 2),
            "best": round(best, 2) if x.get("best") else None, "raised": bool(x.get("trailed"))}


def _add_trail(config: AppConfig, rows: list, real_s: dict, paper_s: dict) -> None:
    """trail on the real and paper rows (null for intraday); a failure leaves the key null."""
    from backend.engine import trend_breakout as tb
    for r in rows:
        r["trail"] = None
    try:
        src = {("real", x["symbol"]): (x, tb.rules(config, "E")) for x in real_s.get("positions") or []}
        for k, a in (paper_s.get("accounts") or {}).items():
            try:
                p = tb.rules(config, k)
            except KeyError:
                continue
            src.update({(f"paper_{k}", x["symbol"]): (x, p) for x in a.get("positions") or []})
        for r in rows:
            hit = src.get((r["book"], r["symbol"]))
            if hit:
                r["trail"] = _trail(*hit)
    except Exception:
        pass


def _results(symbols: set, today: str) -> dict:
    """{symbol: {date, sessions}}: the next results (board meeting) within RESULTS_DAYS calendar days, from the
    stored NSE events calendar."""
    from backend.ingest import events
    if not symbols:
        return {}
    return events.upcoming_results(today, sorted(symbols), horizon_days=RESULTS_DAYS)


def _add_results(rows: list, today: str) -> None:
    for r in rows:
        r["results_date"] = r["results_in"] = None
    try:
        res = _results({r["symbol"] for r in rows if r["book"] != "intraday"}, today)
        for r in rows:
            x = res.get(r["symbol"]) if r["book"] != "intraday" else None
            if x:
                r["results_date"], r["results_in"] = x["date"], x["sessions"]
    except Exception:
        pass


def results_held(pos: list) -> list:
    """stats.results_held: [{symbol, date}] of the held swing names (real + paper E+, unique) reporting within
    RESULTS_DAYS."""
    seen, out = set(), []
    for p in pos:
        if p.get("book") in ("real", "paper_E") and p.get("results_date") and p["symbol"] not in seen:
            seen.add(p["symbol"])
            out.append({"symbol": p["symbol"], "date": p["results_date"]})
    return sorted(out, key=lambda x: (x["date"], x["symbol"]))


def _equity(table: str, account: Optional[str] = None) -> list:
    q = f"SELECT day, equity FROM {table}" + (" WHERE account = ?" if account else "") + " ORDER BY day"
    with get_db() as conn:
        return [(r[0], r[1]) for r in conn.execute(q, (account,) if account else ())]


def spark_nifty(table: str, account: Optional[str], capital: float, n: int = 30) -> list:
    """NIFTY 50 rebased to the account's starting capital (the first stored session = capital), the last n sessions:
    the same days as the account's spark, so both fit one scale. [] without a NIFTY column."""
    q = f"SELECT day, nifty FROM {table}" + (" WHERE account = ?" if account else "") + " ORDER BY day"
    with get_db() as conn:
        rows = [(r[0], r[1]) for r in conn.execute(q, (account,) if account else ())]
    base = next((v for _, v in rows if v), None)
    if not base or not capital:
        return []
    return [round(v / base * capital, 2) if v else None for _, v in rows[-n:]]


def drift(pos: list, cv: dict, n: int = 30) -> dict:
    """Real vs Paper E+ over the names held in both books today, and the last n sessions of real equity minus paper
    equity (on the days both have)."""
    real = {p["symbol"]: p for p in pos if p.get("book") == "real"}
    paper = {p["symbol"]: p for p in pos if p.get("book") == "paper_E"}
    both = sorted(set(real) & set(paper))
    rp = round(float(sum(real[s].get("pnl") or 0 for s in both)), 2)
    pp = round(float(sum(paper[s].get("pnl") or 0 for s in both)), 2)
    diff = round(rp - pp, 2)
    spark = []
    if cv.get("real") and cv.get("paper_E"):
        pe = dict(cv["paper_E"])
        spark = [round(e - pe[d], 2) for d, e in cv["real"] if d in pe][-n:]
    return {"names": len(both), "symbols": both, "real_pnl": rp, "paper_pnl": pp, "diff": diff,
            "pct_of_paper": round(diff / pp * 100, 2) if pp else None, "spark": spark}


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
    try:                                                    # 📐 the futures book: its long side's primary account
        from backend.futures import book
        if book.has_account():
            out["futures"] = _equity("futures_equity", book.primary(config, "long"))
    except Exception:
        pass
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
        for acct, p in conn.execute("SELECT account, payload FROM futures_trades"):
            t = json.loads(p)
            if t["out"] >= since:
                out.append({"book": "futures", "account": f"Futures · {acct.split('_')[0]}", "strategy": FUT_STRATEGY.get(t["side"], "Futures"),
                            "symbol": t["symbol"], "side": t["side"], "qty": t["qty"], "opened": t["in"], "closed": t["out"], "entry": t["entry"],
                            "exit": t["exit"], "gross": t["gross"], "costs": t["costs"], "net": t["net"], "reason": t.get("why")})
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
        for r in conn.execute("SELECT created_at, account, symbol, action, lots, reason, status, price FROM futures_orders "
                              "WHERE action IN ('BUY', 'SELL', 'EXIT', 'ROLL') ORDER BY id DESC LIMIT ?", (limit,)):
            out.append({"book": "futures", "account": f"Futures · {r[1].replace('_', ' · ')}", "at": r[0], "symbol": r[2], "action": r[3],
                        "qty": r[4], "price": r[7], "status": r[6], "note": r[5] or ""})
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


def trail_events(today: str, config: Optional[AppConfig] = None) -> list:
    """The stops the E+ late trail raised at the last evening run, when that run was today (real.evening and
    paper._advance keep the last run's list in app_settings real.trail_raised / paper.trail_raised)."""
    t = (config.alerts.job_time if config else "18:30") or "18:30"
    ev = []
    with get_db() as conn:
        saved = {k: get_setting(conn, f"{k}.trail_raised") for k in ("real", "paper")}
    for k, raw in saved.items():
        x = json.loads(raw) if raw else {}
        if x.get("day") != today:
            continue
        for r in x.get("raised") or []:
            arrow = f"{r['from']:,.0f} → {r['stop']:,.0f}" if r.get("from") else f"→ {r['stop']:,.0f}"
            ev.append({"t": t, "kind": "trail", "text": ("" if k == "real" else "Paper E+ · ") + f"stop raised {r['symbol']} {arrow}"})
    return ev


def timeline(today: str, config: Optional[AppConfig] = None) -> list:
    """What happened today, in order: real orders, intraday signals, intraday exits, real exits, raised stops."""
    ev = []
    try:
        ev += trail_events(today, config)
    except Exception:
        pass
    with get_db() as conn:
        for r in conn.execute("SELECT created_at, symbol, action, qty, status, reason FROM real_orders WHERE day = ? ORDER BY id", (today,)):
            if r[2] in ("BUY", "EXIT"):
                ev.append({"t": r[0][11:16], "kind": "order" if r[4] == "sent" else "failed", "text": f"{r[2]} {r[1]} {r[3] or ''} · {r[4]}"})
            else:                                          # a correction by hand (edited / added / removed)
                ev.append({"t": r[0][11:16], "kind": "edit", "text": r[5] or f"{r[4]} {r[1]}"})
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


def next_open(config: AppConfig, pos: list) -> Optional[dict]:
    """Tomorrow's open from the latest saved evening scan (trend_breakout.run_scan): the E+ filter, the free real
    slots (max_pos minus the open real positions), every buy in scan order and how many fit. None without a scan."""
    from backend.engine import trend_breakout as tb
    sc = tb.saved_scan()
    if not sc:
        return None
    held = sum(1 for p in pos if p.get("book") == "real")
    free = max(0, config.breakout.max_pos - held)
    buys = [{"symbol": b["symbol"], "qty": b.get("qty"), "value": b.get("value"), "stop_pct": b.get("stop_pct"),
             "risk": b.get("risk")} for b in sc.get("buys") or []]
    return {"as_of": sc.get("as_of"), "filter": ((sc.get("filters") or {}).get("E") or {}).get("state"),
            "slots_free": free, "buys": buys, "total_value": round(sum(b["value"] or 0 for b in buys), 2),
            "fits": min(len(buys), free)}


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
    if not out.get("live"):
        out["live"] = False
        try:
            out.update(close_pulse(config))
        except Exception:
            pass
    out["signals_live"] = sum(1 for x in rows.values() if x.get("status") in ("open", "pending"))
    return out


def close_pulse(config: AppConfig) -> dict:
    """The pulse from the last session's daily candles while the intraday feed is off: advancing / declining =
    close vs the previous close over the Nifty 500, above_vwap_pct with the day's typical price (high + low + close)
    / 3 standing in for VWAP (proxy: true), NIFTY 50 vs its typical price; as_of_close: true."""
    from backend.database.models import get_candles
    from backend.engine import trend_breakout as tb
    from backend.ingest import universe as uni
    u = uni.resolve_universe(config.ingest)
    tokens = {s["instrument_token"]: s["symbol"] for s in u["stocks"]}
    idx = next((i["instrument_token"] for i in u["indices"] if i["symbol"] == tb.INDEX), None)
    if not tokens:
        return {}
    with get_db() as conn:
        last = conn.execute("SELECT MAX(date) FROM candles_daily WHERE instrument_token IN (%s)" % ",".join("?" * len(tokens)),
                            list(tokens)).fetchone()[0]
        if not last:
            return {}
        since = (date.fromisoformat(last) - timedelta(days=10)).isoformat()
        rows = conn.execute("SELECT instrument_token, date, high, low, close FROM candles_daily WHERE instrument_token IN (%s) "
                            "AND date >= ? ORDER BY instrument_token, date" % ",".join("?" * len(tokens)), list(tokens) + [since]).fetchall()
        nb = get_candles(conn, idx, limit=1) if idx is not None else []
    by = defaultdict(list)
    for r in rows:
        by[r[0]].append(r)
    up = dn = above = n = 0
    for tok, cs in by.items():
        if cs[-1][1] != last or len(cs) < 2:
            continue
        n += 1
        c, prev = cs[-1][4], cs[-2][4]
        up += c > prev
        dn += c < prev
        above += c > (cs[-1][2] + cs[-1][3] + cs[-1][4]) / 3
    out = {"as_of_close": True, "proxy": True, "close_date": last}
    if n:
        out.update(advancing=up, declining=dn, above_vwap_pct=round(100 * above / n, 1))
    if nb:
        b = nb[-1]
        out["nifty_vs_vwap"] = "above" if b["close"] > (b["high"] + b["low"] + b["close"]) / 3 else "below"
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
    for k in STRATEGY:
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
    fu = accts.get("futures")
    if fu:
        fut = [p for p in pos if p["book"] == "futures"]
        out["futures"] = {"lots": sum(p.get("lots") or 0 for p in fut), "max_pos": config.futures.max_pos, "margin_pct": fu.get("margin_pct"),
                          "mode": fu.get("mode"), "killed": fu.get("killed")}
    return out


def overview(config: AppConfig, quote=None) -> dict:
    """Home: each account's figures, today's P&L across them, every open position, the latest activity, plus the
    sparklines, the stats strip, risk, today's timeline, the live P&L curve, the market pulse and a fact per card."""
    from backend.engine import paper, real
    from backend.intraday import flow
    real_s, paper_s, live = real.summary(config), paper.summary(config), flow.live()
    fut_s = _futures_summary(config)
    cv = curves(config)
    dp = daily_pnl(cv)
    accts = {}
    if real_s.get("started"):
        a = real_s["account"]
        last = cv.get("real", [])[-1][0] if cv.get("real") else None
        accts["real"] = {"name": "Real", "value": a["value"], "net": a["net"], "return": a["return"],
                         "last_day": last, "day_pnl": dp.get("real", {}).get(last, 0.0) if last else 0.0,
                         "positions": len(real_s.get("positions") or []),
                         "on": bool(real_s.get("enabled") and not real_s.get("killed")), "variant": real_s.get("variant"),
                         "spark": [e for _, e in cv.get("real", [])[-30:]], "spark_nifty": []}
        try:                                                # NIFTY 50 on the Real card's scale (starting capital)
            first_cap = a["capital"] - sum(x["amount"] for x in real_s.get("deposits") or [])
            accts["real"]["spark_nifty"] = spark_nifty("real_equity", None, first_cap)
        except Exception:
            pass
    for k, a in (paper_s.get("accounts") or {}).items():
        b = f"paper_{k}"
        last = cv.get(b, [])[-1][0] if cv.get(b) else None
        accts[b] = {"name": NAMES.get(b, b), "value": a["account"]["value"], "net": a["account"]["net"],
                    "return": a["account"]["return"], "nifty": a["account"].get("nifty"), "last_day": last,
                    "day_pnl": dp.get(b, {}).get(last, 0.0) if last else 0.0, "positions": len(a.get("positions") or []),
                    "spark": [e for _, e in cv.get(b, [])[-30:]], "spark_nifty": []}
        try:
            accts[b]["spark_nifty"] = spark_nifty("paper_equity", k, a["account"].get("capital") or 0)
        except Exception:
            pass
    if fut_s:                                               # 📐 the futures book (its long side's primary account)
        last = cv.get("futures", [])[-1][0] if cv.get("futures") else None
        accts["futures"] = {"name": "Futures", "value": fut_s["value"], "net": fut_s["net"], "return": fut_s["return"], "last_day": last,
                            "day_pnl": fut_s.get("mtm_today") or (dp.get("futures", {}).get(last, 0.0) if last else 0.0),
                            "positions": len(fut_s.get("positions") or []), "margin_pct": fut_s.get("margin_pct"),
                            "mode": "real" if fut_s["sides"]["long"]["real"] else "paper", "sides": fut_s["sides"], "killed": fut_s["guards"]["kill"],
                            "spark": [e for _, e in cv.get("futures", [])[-30:]], "spark_nifty": []}
        try:
            first_cap = fut_s["capital"] - sum(x["amount"] for x in fut_s.get("deposits") or [])
            accts["futures"]["spark_nifty"] = spark_nifty("futures_equity", fut_s.get("primary"), first_cap)
        except Exception:
            pass
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
    pos = positions(config, real_s, paper_s, live, quote, fut_s)
    today_pnl = round(sum(a.get("day_pnl") or 0 for a in accts.values()), 2)
    extra = {}
    for name, fn in (("stats", lambda: stats(config, dp, today)), ("risk", lambda: risk(config, accts, pos)),
                     ("timeline", lambda: timeline(today, config)), ("pulse", lambda: pulse(config, live)),
                     ("today_curve", lambda: _sample_today(today_pnl, now)), ("drift", lambda: drift(pos, cv)),
                     ("next_open", lambda: next_open(config, pos))):
        try:
            extra[name] = fn()
        except Exception as exc:                      # one broken panel never blanks Home
            extra[name] = {"error": str(exc)[:120]} if name not in ("timeline", "today_curve", "next_open") else ([] if name != "next_open" else None)
    try:
        if isinstance(extra.get("stats"), dict):
            extra["stats"]["results_held"] = results_held(pos)
    except Exception:
        pass
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
