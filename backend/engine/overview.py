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


def overview(config: AppConfig, quote=None) -> dict:
    """Home: each account's figures, today's P&L across them, every open position, the latest activity."""
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
                         "on": bool(real_s.get("enabled") and not real_s.get("killed")), "variant": real_s.get("variant")}
    for k, a in (paper_s.get("accounts") or {}).items():
        b = f"paper_{k}"
        last = cv.get(b, [])[-1][0] if cv.get(b) else None
        accts[b] = {"name": NAMES.get(b, b), "value": a["account"]["value"], "net": a["account"]["net"],
                    "return": a["account"]["return"], "nifty": a["account"].get("nifty"), "last_day": last,
                    "day_pnl": dp.get(b, {}).get(last, 0.0) if last else 0.0, "positions": len(a.get("positions") or [])}
    st = live.get("stats") or {}
    accts["intraday"] = {"name": "Intraday · paper", "day_pnl": round((st.get("realised") or 0) + (st.get("unrealised") or 0), 2),
                         "realised": st.get("realised"), "open_pnl": st.get("unrealised"), "trades": st.get("trades"),
                         "open": st.get("open"), "at_risk": st.get("at_risk"), "loss_limit": st.get("loss_limit"),
                         "capital": st.get("capital"), "mode": config.flow.mode}
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
    return {"at": datetime.now(IST).isoformat(timespec="seconds"), "accounts": accts,
            "today": round(sum(a.get("day_pnl") or 0 for a in accts.values()), 2),
            "positions": positions(config, real_s, paper_s, live, quote), "activity": act[:10],
            "feed": live.get("feed")}


def portfolio(config: AppConfig, days: int = 365, quote=None) -> dict:
    """Portfolio: positions, trades, orders, the daily P&L and the curves of every account (filtered on the page)."""
    cv = curves(config)
    return {"positions": positions(config, quote=quote), "trades": trades(config, days), "orders": orders(config),
            "daily": daily_pnl(cv), "curves": cv, "names": NAMES}
