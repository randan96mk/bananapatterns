"""📝 Paper trading: the Trend Breakout rules (backend/engine/trend_breakout.py) run forward, one session at a time,
from the day it is started. Simulated: never orders, never a Kite order call.

Each evening (after the daily sync), for every new session since the last one processed, exactly as simulate():
  1. yesterday's SELL orders fill at today's open (slippage, delivery charges);
  2. yesterday's BUY orders fill at today's open, best 6-month return first, equal weight:
     qty = floor(min(account value / max_pos, cash) / (fill x 1.0012)); skipped when no slot or under 1 share;
  3. a day's low at or under a position's stop sells it at min(open, stop); otherwise it is marked at the close;
  4. at the close: a close under SMA(exit_sma) queues a SELL; the day's breakouts (market filter on) queue BUYs.
Fills, trades and the daily account are appended and never rewritten. Gross (price move), charges and slippage are
kept apart. Stops are checked on the daily low after the close (no live intraday price), as in the backtest.
"""

import json
import threading
from datetime import date
from math import floor
from typing import Optional

from backend.config import AppConfig
from backend.database.models import get_db, now_ist
from backend.engine import trend_breakout as tb
from backend.logging_config.logger import setup_logger

logger = setup_logger("paper")

_lock = threading.Lock()


def _state() -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT payload FROM paper_state WHERE id = 1").fetchone()
    return json.loads(r["payload"]) if r else None


def _save(st: dict, trades: list, fills: list, equity: list) -> None:
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO paper_state (id, payload) VALUES (1, ?)", (json.dumps(st),))
        conn.executemany("INSERT INTO paper_trades (payload) VALUES (?)", [(json.dumps(t),) for t in trades])
        conn.executemany("INSERT INTO paper_fills (day, payload) VALUES (?, ?)", [(f["day"], json.dumps(f)) for f in fills])
        conn.executemany("INSERT OR REPLACE INTO paper_equity (day, equity, gross, nifty) VALUES (?, ?, ?, ?)", equity)


def start(config: AppConfig, capital: Optional[float] = None, at: Optional[str] = None) -> dict:
    """Start with `capital` (default: the paper capital setting): the latest session's close (or `at`'s, for a
    replay) decides the first orders, which fill at the next open."""
    with _lock:
        if _state():
            return {"status": "started_already"}
        data = tb.load(config)
        if "error" in data:
            return {"status": "no_data", "error": data["error"]}
        cap = float(capital or config.paper.capital)
        if cap <= 0:
            return {"status": "bad_capital"}
        i = data["cal"].index(at) if at in data["cal"] else len(data["cal"]) - 1
        st = {"started": data["cal"][i], "capital": cap, "cash": cap, "charges": 0.0, "slippage": 0.0,
              "as_of": data["cal"][i], "positions": {}, "orders": [], "started_at": now_ist(),
              "updated_at": now_ist()}
        _decide(st, data, i, config.breakout)
        _save(st, [], [], [(data["cal"][i], cap, cap, data["index_close"][i])])
        logger.info("Paper trading started %s with Rs %.0f", st["started"], cap)
        return {"status": "ok", "started": st["started"], "orders": len(st["orders"])}


def reset() -> None:
    with _lock, get_db() as conn:
        for t in ("paper_state", "paper_trades", "paper_fills", "paper_equity"):
            conn.execute(f"DELETE FROM {t}")


def _decide(st: dict, data: dict, i: int, p) -> None:
    """At the close of calendar day i: the orders for the next open (same as simulate's step 4)."""
    S = data["S"]
    regime_on = data["index_ok"][i] or not p.regime
    sells, buys, _, _ = tb.decide(i, S, {s: None for s in st["positions"] if s in S}, regime_on, p)
    st["orders"] = ([{"action": "SELL", "symbol": s, "why": f"close under SMA{p.exit_sma}"} for s in sorted(sells)]
                    + [{"action": "BUY", "symbol": s, "stop": stop, "rank": k + 1,     # unrounded: as the backtest
                        "close": S[s]["rows"][S[s]["at"][i]]["close"],
                        "ret126": round((tb.val(S[s], S[s]["at"][i], "ret126") or 0) * 100, 1)}
                       for k, (s, stop) in enumerate(buys)])
    st["decided"] = data["cal"][i]
    st["market_on"] = bool(regime_on)


def _step(st: dict, data: dict, i: int, p) -> tuple[list, list]:
    """One session (simulate's steps 1-3): returns (closed trades, fills)."""
    S, d, slip = data["S"], data["cal"][i], p.slippage_pct
    bar = lambda sym: S[sym]["rows"][S[sym]["at"][i]] if sym in S and i in S[sym]["at"] else None
    fill = lambda px, buying: px * (1 + slip / 100) if buying else px * (1 - slip / 100)
    pos_all, trades, fills = st["positions"], [], []

    def close_pos(sym, px, why):
        pos = pos_all.pop(sym)
        f = fill(px, False)
        ch = tb.delivery_charges(pos["qty"] * f, True)
        sl = (px - f) * pos["qty"]
        st["cash"] += pos["qty"] * f - ch
        st["charges"] += ch
        st["slippage"] += sl
        gross = (px - pos["px"]) * pos["qty"]
        charges, slippage = pos["charges"] + ch, pos["slippage"] + sl
        t = {"symbol": sym, "qty": pos["qty"], "in": pos["d"], "out": d, "buy": round(pos["px"], 2),
             "sell": round(px, 2), "stop": round(pos["stop"], 2), "days": pos["days"], "gross": round(gross, 2),
             "charges": round(charges, 2), "slippage": round(slippage, 2), "costs": round(charges + slippage, 2),
             "net": round(gross - charges - slippage, 2), "why": why}
        trades.append(t)
        fills.append({"day": d, "action": "SELL", "symbol": sym, "qty": pos["qty"], "price": round(px, 2), "why": why,
                      "gross": t["gross"], "costs": t["costs"], "net": t["net"], "days": pos["days"]})

    orders, st["orders"], st["skipped"] = st.get("orders", []), [], []
    for o in orders:                                      # 1) sells at the open
        b = bar(o["symbol"])
        if o["action"] == "SELL" and o["symbol"] in pos_all and b:
            close_pos(o["symbol"], b["open"], "exit")
    equity = st["cash"] + sum(x["qty"] * x["last"] for x in pos_all.values())
    buys = [o for o in orders if o["action"] == "BUY"]
    for k, o in enumerate(buys):                          # 2) buys at the open, equal weight
        sym = o["symbol"]
        if len(pos_all) >= p.max_pos:
            st["skipped"] += [{**x, "why": "no free slot"} for x in buys[k:]]
            break
        b = bar(sym)
        if not b or sym in pos_all:
            continue
        f = fill(b["open"], True)
        qty = floor(min(equity / p.max_pos, st["cash"]) / (f * (1 + 0.0012)))
        if qty < 1:
            st["skipped"].append({**o, "why": "too small (under 1 share)"})
            continue
        ch = tb.delivery_charges(qty * f, False)
        sl = (f - b["open"]) * qty
        st["cash"] -= qty * f + ch
        st["charges"] += ch
        st["slippage"] += sl
        pos_all[sym] = {"qty": qty, "px": b["open"], "d": d, "stop": o["stop"], "last": b["open"], "prev": b["open"],
                        "charges": ch, "slippage": sl, "days": 0}
        fills.append({"day": d, "action": "BUY", "symbol": sym, "qty": qty, "price": round(b["open"], 2),
                      "stop": round(o["stop"], 2), "value": round(qty * f, 2), "charges": round(ch, 2), "slippage": round(sl, 2)})
    for sym in list(pos_all):                             # 3) stops during the day, then the close
        b = bar(sym)
        if not b:
            continue
        pos = pos_all[sym]
        pos["days"] += 1
        if pos["stop"] and b["low"] <= pos["stop"]:
            close_pos(sym, min(b["open"], pos["stop"]), "stop")
            continue
        pos["prev"], pos["last"] = pos["last"], b["close"]
    return trades, fills


def process(config: AppConfig, data: Optional[dict] = None) -> dict:
    """Every new session since the last one processed. Returns the day's report (fills, closed trades, orders for the
    next open, the account), also kept for the evening report."""
    with _lock:
        st = _state()
        if not st:
            return {"status": "not_started"}
        data = data or tb.load(config)
        if "error" in data:
            return {"status": "no_data", "error": data["error"]}
        p, cal = config.breakout, data["cal"]
        new = [i for i, d in enumerate(cal) if d > st["as_of"]]
        all_trades, all_fills, equity = [], [], []
        for i in new:
            trades, fills = _step(st, data, i, p)
            all_trades += trades
            all_fills += fills
            st["as_of"] = cal[i]
            eq = st["cash"] + sum(x["qty"] * x["last"] for x in st["positions"].values())
            equity.append((cal[i], round(eq, 2), round(eq + st["charges"] + st["slippage"], 2),
                           data["index_close"][i]))
            _decide(st, data, i, p)
        st["compare"] = _compare(st, data, p)
        st["updated_at"] = now_ist()
        _save(st, all_trades, all_fills, equity)
        return {"status": "ok", "sessions": len(new), "report": report(st, data)}


def _compare(st: dict, data: dict, p) -> dict:
    """The backtest over the same sessions with the same capital (positions left open at the end), for paper vs
    backtest: a gap means the data or the rules drifted."""
    cal = data["cal"]
    if st["started"] not in cal:
        return {}
    s = cal.index(st["started"])
    eq, eqg, trades, _ = tb.simulate(cal, data["S"], data["index_ok"], p, p.slippage_pct, s,
                                     capital=st["capital"], close_end=False)
    c = st["capital"]
    return {"return": round((eq[-1] / c - 1) * 100, 2), "gross": round((eqg[-1] / c - 1) * 100, 2),
            "costs": round(eqg[-1] - eq[-1], 2), "trades": len(trades),
            "won": round(100 * sum(t["net"] > 0 for t in trades) / len(trades), 1) if trades else None}


def report(st: dict, data: Optional[dict] = None) -> dict:
    """The latest session for the evening report and Telegram."""
    with get_db() as conn:
        fills = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM paper_fills WHERE day = ? ORDER BY rowid",
                                                        (st["as_of"],))]
    return {"as_of": st["as_of"], "fills": fills, "orders": st.get("orders", []), "account": account(st),
            "market_on": st.get("market_on")}


def account(st: dict) -> dict:
    """Value, cash, before / after costs, NIFTY 50 over the same days."""
    held = st["positions"]
    value = st["cash"] + sum(x["qty"] * x["last"] for x in held.values())
    costs = st["charges"] + st["slippage"]
    with get_db() as conn:
        rows = conn.execute("SELECT nifty FROM paper_equity ORDER BY day").fetchall()
    nifty = round((rows[-1][0] / rows[0][0] - 1) * 100, 2) if len(rows) > 1 and rows[0][0] else 0.0
    cap = st["capital"]
    return {"capital": cap, "value": round(value, 2), "cash": round(st["cash"], 2), "positions": len(held),
            "gross": round(value + costs - cap, 2), "charges": round(st["charges"], 2),
            "slippage": round(st["slippage"], 2), "costs": round(costs, 2), "net": round(value - cap, 2),
            "return_gross": round((value + costs - cap) / cap * 100, 2), "return": round((value - cap) / cap * 100, 2),
            "nifty": nifty}


def summary(config: AppConfig) -> dict:
    """Everything the 📝 Paper trading tab shows."""
    st = _state()
    if not st:
        return {"started": False, "capital": config.paper.capital}
    p = config.breakout
    with get_db() as conn:
        trades = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM paper_trades ORDER BY rowid DESC")]
        eq = [dict(r) for r in conn.execute("SELECT day, equity, gross, nifty FROM paper_equity ORDER BY day")]
    peak, dd = st["capital"], 0.0
    for e in eq:
        peak = max(peak, e["equity"])
        dd = max(dd, (peak - e["equity"]) / peak * 100)
    positions = []
    for sym, x in sorted(st["positions"].items(), key=lambda kv: kv[1]["d"], reverse=True):
        exit_fill = x["last"] * (1 - p.slippage_pct / 100)
        exit_ch = tb.delivery_charges(x["qty"] * exit_fill, True)
        gross = (x["last"] - x["px"]) * x["qty"]
        costs = x["charges"] + x["slippage"] + exit_ch + (x["last"] - exit_fill) * x["qty"]
        jump = x.get("prev") and abs(x["last"] / x["prev"] - 1) > 0.35
        positions.append({"symbol": sym, "qty": x["qty"], "in": x["d"], "buy": round(x["px"], 2),
                          "last": round(x["last"], 2), "stop": round(x["stop"], 2), "days": x["days"], "gross": round(gross, 2),
                          "costs_so_far": round(x["charges"] + x["slippage"], 2), "costs": round(costs, 2),
                          "net": round(gross - costs, 2), "warning": "big overnight change: split or bonus?" if jump else None})
    won = [t for t in trades if t["net"] > 0]
    nifty0 = eq[0]["nifty"] if eq else None
    return {"started": True, "start_date": st["started"], "started_at": st.get("started_at"), "as_of": st["as_of"],
            "account": account(st), "drawdown": round(dd, 2), "positions": positions, "orders": st.get("orders", []),
            "skipped": st.get("skipped", []), "market_on": st.get("market_on"), "decided": st.get("decided"),
            "trades": trades, "won": len(won), "closed": len(trades),
            "trade_totals": {k: round(sum(t[k] for t in trades), 2) for k in ("gross", "charges", "slippage", "costs", "net")},
            "curve": {"dates": [e["day"] for e in eq], "net": [e["equity"] for e in eq], "gross": [e["gross"] for e in eq],
                      "nifty": [round(e["nifty"] / nifty0 * st["capital"], 2) if nifty0 else None for e in eq]},
            "compare": st.get("compare") or {}, "max_pos": p.max_pos, "updated_at": st.get("updated_at"),
            "slippage_pct": p.slippage_pct}


def latest_report() -> Optional[dict]:
    st = _state()
    return report(st) if st else None
