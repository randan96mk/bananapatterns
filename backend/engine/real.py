"""💰 Real trading: the Trend Breakout orders sent to Tradetron by webhook and tracked like paper trading.

  evening (after the scan)  the account is marked at the day's close (the morning fills corrected to the day's open
                            from the daily candle), then tomorrow's orders are planned with the selected variant
                            (config.real.variant, E by default; it affects NEW buys only): EXIT a position that closed
                            under SMA(exit_sma); BUY the ranked breakouts into the free slots under that variant's
                            filter, sized at account value / max_pos (whole shares), within max_order_pct of the value and
                            max_buys_per_day.
  send_time .. send_until   EXITs, then BUYs, each sent once (one key per day, stock and action); a failure is not
                            retried, it raises a loud alert.
  market hours, every       live prices (Kite quote, read-only); a position trading at or under its stop gets an
  stop_check_min            EXIT at once (no stop-loss order is placed anywhere).
Orders go to Tradetron (which routes them to the broker, or keeps them offline); none ever goes to Kite. config.real
.enabled and the kill switch (app setting real.killed) both have to allow sending. Charges are Zerodha delivery
estimates; the broker's own fills are not visible here.
"""

import json
import threading
from datetime import datetime
from math import floor
from typing import Callable, Optional

from backend.broker import tradetron
from backend.config import AppConfig
from backend.database.models import get_db, get_setting, now_ist, set_setting
from backend.engine import trend_breakout as tb
from backend.logging_config.logger import setup_logger

logger = setup_logger("real")

_lock = threading.Lock()


def _rules(config: AppConfig, key: Optional[str] = None):
    return tb.rules(config, key or config.real.variant)


def killed() -> bool:
    with get_db() as conn:
        return get_setting(conn, "real.killed") == "1"


def set_killed(on: bool) -> None:
    with get_db() as conn:
        set_setting(conn, "real.killed", "1" if on else "0")


def active(config: AppConfig) -> bool:
    return bool(config.real.enabled) and not killed()


def _state() -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT payload FROM real_state WHERE id = 1").fetchone()
    return json.loads(r[0]) if r else None


def has_account() -> bool:
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM real_state WHERE id = 1").fetchone() is not None


def _save(st: dict) -> None:
    st["updated_at"] = now_ist()
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO real_state (id, payload) VALUES (1, ?)", (json.dumps(st),))


def _log(key: str, day: str, o: dict, status: str, reply: str = "", price: Optional[float] = None) -> None:
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO real_orders (key, day, created_at, symbol, action, qty, reason, status, reply, "
                     "payload, price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (key, day, now_ist(), o["symbol"], o["action"], o.get("qty"), o.get("why"), status, reply[:300],
                      json.dumps(tradetron.public(tradetron.body(o["ts"], o["action"], o.get("qty") if o["action"] == "BUY" else None))),
                      price))


def _sent(key: str) -> bool:
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM real_orders WHERE key = ?", (key,)).fetchone() is not None


def _value(st: dict) -> float:
    return st["cash"] + sum(x["qty"] * x["last"] for x in st["positions"].values())


# --- evening: mark, then plan tomorrow ---------------------------------------------------------------

def evening(config: AppConfig, data: Optional[dict] = None) -> dict:
    """Mark the account at the day's close and plan the next open's orders. Starts the account (capital from the
    settings) the first evening real trading is on; a day already processed is not processed again."""
    with _lock:
        st = _state()
        if not st and not config.real.enabled:
            return {"status": "off"}
        data = data or tb.load(config)
        if "error" in data:
            return {"status": "no_data", "error": data["error"]}
        cal, S = data["cal"], data["S"]
        i = len(cal) - 1
        day = cal[i]
        if not st:
            st = {"started": day, "capital": float(config.real.capital), "cash": float(config.real.capital),
                  "charges": 0.0, "as_of": None, "positions": {}, "orders": [], "started_at": now_ist()}
        if st.get("as_of") == day:
            return {"status": "done", "orders": st.get("orders", [])}
        missed = [o for o in st.get("orders", []) if not o.get("sent")]
        bar = lambda sym: S[sym]["rows"][S[sym]["at"][i]] if sym in S and i in S[sym]["at"] else None
        for sym, pos in st["positions"].items():          # morning fills -> the day's open; mark at the close
            b = bar(sym)
            if not b:
                continue
            if pos.get("est") and pos["d"] == day:
                _rebook(st, pos, b["open"])
            pos["last"] = b["close"]
            pos["days"] = pos.get("days", 0) + 1
        with get_db() as conn:                            # morning exits -> the day's open too
            for r in conn.execute("SELECT id, payload FROM real_trades WHERE json_extract(payload, '$.out') = ?", (day,)):
                t = json.loads(r[1])
                b = bar(t["symbol"])
                if t.get("est") and b:
                    diff = (b["open"] - t["sell"]) * t["qty"]
                    st["cash"] += diff
                    t.update(sell=round(b["open"], 2), gross=round(t["gross"] + diff, 2), net=round(t["net"] + diff, 2), est=False)
                    conn.execute("UPDATE real_trades SET payload = ? WHERE id = ?", (json.dumps(t), r[0]))
        _plan(st, data, i, config)
        st["as_of"] = day
        eq = _value(st)
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO real_equity (day, equity, gross, nifty) VALUES (?, ?, ?, ?)",
                         (day, round(eq, 2), round(eq + st["charges"], 2), data["index_close"][i]))
        _save(st)
        return {"status": "ok", "orders": st["orders"], "missed": missed, "as_of": day, "top": st.get("top")}


def _rebook(st: dict, pos: dict, px: float) -> None:
    """A morning BUY booked at the price seen when it was sent, corrected to the day's open."""
    diff = (px - pos["px"]) * pos["qty"]
    new_ch = tb.delivery_charges(pos["qty"] * px, False)
    st["cash"] -= diff + (new_ch - pos["charges"])
    st["charges"] += new_ch - pos["charges"]
    pos.update(px=px, charges=new_ch, est=False)


def limits(st: dict, config: AppConfig) -> dict:
    """What the configured capital means right now: account value, size per stock, the largest order."""
    value = _value(st)
    return {"value": round(value, 2), "size": round(value / config.breakout.max_pos, 2),
            "max_order": round(value * config.real.max_order_pct / 100, 2)}


def _size(st: dict, orders: list, config: AppConfig) -> list:
    """BUY quantities from the account value: min(value / max_pos, cash left, max_order_pct of the value) at the
    signal close (+0.12% for charges), whole shares; a BUY under one share is dropped. EXITs free their cash first."""
    lim = limits(st, config)
    cash = st["cash"] + sum(st["positions"][o["symbol"]]["qty"] * st["positions"][o["symbol"]]["last"]
                            for o in orders if o["action"] == "EXIT" and o["symbol"] in st["positions"])
    out = []
    for o in orders:
        if o["action"] == "BUY" and not o.get("sent"):
            qty = floor(min(lim["size"], cash, lim["max_order"]) / (o["close"] * 1.0012))
            if qty < 1:
                continue
            cash -= qty * o["close"] * 1.0012
            o = {**o, "qty": qty, "risk": round(qty * (o["close"] - o["stop"]), 2)}
        out.append(o)
    return out


def set_capital(config: AppConfig, capital: float) -> dict:
    """A new capital: before the start it is simply used; afterwards the difference is money added or withdrawn
    (cash moves, the profit so far stays), and tomorrow's unsent BUYs are re-sized."""
    with _lock:
        st = _state()
        if not st:
            config.real.capital = float(capital)
            return {"status": "ok", "capital": capital}
        delta = float(capital) - st["capital"]
        if st["cash"] + delta < 0:
            raise ValueError(f"withdrawing {-delta:,.0f} is more than the cash ({st['cash']:,.0f}); "
                             "lower it after positions are sold")
        config.real.capital = float(capital)
        st["capital"] = float(capital)
        st["cash"] += delta
        st.setdefault("deposits", []).append({"at": now_ist(), "amount": round(delta, 2), "capital": float(capital)})
        st["orders"] = _size(st, st.get("orders", []), config)
        _save(st)
        return {"status": "ok", "capital": capital, "delta": delta, **limits(st, config)}


def _plan(st: dict, data: dict, i: int, config: AppConfig) -> None:
    S, p, rc = data["S"], _rules(config), config.real
    regime_on, cap = tb.slots(i, data["index_ok"], data["filters"], p)
    sells, buys, _, _ = tb.decide(i, S, {s: None for s in st["positions"] if s in S}, regime_on, p)
    ts = data.get("tradingsymbol", {})
    turn_rank = {sym: k + 1 for k, sym in enumerate(tb.ranked_by_turnover(S, i, p.min_price))}
    orders = [{"action": "EXIT", "symbol": s, "ts": ts.get(s, s), "qty": st["positions"][s]["qty"],
               "why": f"close under SMA{p.exit_sma}"} for s in sorted(sells)]
    free = max(0, cap - (len(st["positions"]) - len(sells))) if regime_on else 0
    for k, (s, stop) in enumerate(buys):
        if sum(o["action"] == "BUY" for o in orders) >= min(free, rc.max_buys_per_day):
            break
        close = S[s]["rows"][S[s]["at"][i]]["close"]
        orders.append({"action": "BUY", "symbol": s, "ts": ts.get(s, s), "qty": 0, "stop": stop, "close": close,
                       "rank": k + 1, "variant": config.real.variant, "top": p.top, "turnover_rank": turn_rank.get(s),
                       "why": f"breakout #{k + 1} · {config.real.variant} · top {p.top} · turnover rank {turn_rank.get(s)}"})
    orders = _size(st, orders, config)
    closes = data["index_close"]
    now = tb.filter_day(p.regime_mode, i, closes)
    before = tb.filter_day(p.regime_mode, i - 1, closes) if i else now
    st.update(orders=orders, decided=data["cal"][i], variant=config.real.variant, cap=cap, allowed=bool(regime_on),
              top=p.top, filter={"name": tb.variant_name(config.real.variant, p.top), "top": p.top, "state": tb.filter_label(now, p.regime_mode),
                      "prev": tb.filter_label(before, p.regime_mode), "changed": now != before,
                      "slots": cap if regime_on else 0},
              waiting=len(buys) - sum(o["action"] == "BUY" for o in orders))


# --- sending ---------------------------------------------------------------------------------------

def _quotes(client, symbols: list[str]) -> dict:
    """{tradingsymbol: last price} from one Kite quote call (read-only); {} without a login."""
    if not symbols or client is None or not getattr(client, "has_token", False):
        return {}
    try:
        q = client.quote([f"NSE:{s}" for s in symbols])
    except Exception as exc:
        logger.warning("Real trading quote failed: %s", exc)
        return {}
    return {k.split(":", 1)[1]: v.get("last_price") for k, v in q.items() if v.get("last_price")}


def _close(st: dict, sym: str, px: float, day: str, why: str, est: bool) -> dict:
    pos = st["positions"].pop(sym)
    ch = tb.delivery_charges(pos["qty"] * px, True)
    st["cash"] += pos["qty"] * px - ch
    st["charges"] += ch
    gross = (px - pos["px"]) * pos["qty"]
    t = {"symbol": sym, "qty": pos["qty"], "in": pos["d"], "out": day, "buy": round(pos["px"], 2), "sell": round(px, 2),
         "stop": round(pos["stop"], 2), "days": pos.get("days", 0), "variant": pos.get("variant"), "gross": round(gross, 2),
         "costs": round(pos["charges"] + ch, 2), "net": round(gross - pos["charges"] - ch, 2), "why": why, "est": est}
    with get_db() as conn:
        conn.execute("INSERT INTO real_trades (payload) VALUES (?)", (json.dumps(t),))
    return t


def send_orders(config: AppConfig, client, today: str, sender: Optional[Callable] = None) -> dict:
    """The planned orders (EXITs, then BUYs), each sent once. Returns {status, sent, failed}."""
    sender = sender or tradetron.send
    with _lock:
        st = _state()
        if not st or not st.get("orders"):
            return {"status": "nothing", "sent": [], "failed": []}
        if not active(config):
            return {"status": "off", "sent": [], "failed": []}
        orders = sorted(st["orders"], key=lambda o: o["action"] != "EXIT")
        px = _quotes(client, [o["ts"] for o in orders])
        sent, failed = [], []
        for o in orders:
            key = f"{today}|{o['symbol']}|{o['action']}"
            if o.get("sent") or _sent(key):
                continue
            if o["action"] == "EXIT" and o["symbol"] not in st["positions"]:
                continue
            price = px.get(o["ts"]) or (st["positions"][o["symbol"]]["last"] if o["action"] == "EXIT" else o["close"])
            if o["action"] == "BUY" and o["qty"] * price * 1.0012 > st["cash"]:
                _log(key, today, o, "skipped", "not enough cash")
                failed.append({**o, "error": "not enough cash"})
                continue
            try:
                reply = sender(o["ts"], o["action"], o["qty"] if o["action"] == "BUY" else None)
            except Exception as exc:
                _log(key, today, o, "failed", str(exc))
                failed.append({**o, "error": str(exc)[:120]})
                o["sent"] = "failed"
                continue
            _log(key, today, o, "sent", reply, price)
            o["sent"] = "sent"
            if o["action"] == "EXIT":
                _close(st, o["symbol"], price, today, "exit", est=True)
            else:
                ch = tb.delivery_charges(o["qty"] * price, False)
                st["cash"] -= o["qty"] * price + ch
                st["charges"] += ch
                st["positions"][o["symbol"]] = {"qty": o["qty"], "px": price, "d": today, "stop": o["stop"], "last": price,
                                                "charges": ch, "days": 0, "ts": o["ts"], "variant": st.get("variant"),
                                                "est": True}
            sent.append({**o, "price": price, "reply": reply[:120]})
        _save(st)
        return {"status": "ok", "sent": sent, "failed": failed}


def watch_stops(config: AppConfig, client, today: str, sender: Optional[Callable] = None) -> dict:
    """Live prices of the open positions: an EXIT the moment one trades at or under its stop (once per stock)."""
    sender = sender or tradetron.send
    with _lock:
        st = _state()
        if not st or not st["positions"]:
            return {"status": "nothing", "hits": []}
        if not active(config):
            return {"status": "off", "hits": []}
        if client is None or not getattr(client, "has_token", False):
            return {"status": "no_login", "hits": []}
        px = _quotes(client, [p.get("ts", s) for s, p in st["positions"].items()])
        hits = []
        for sym, pos in list(st["positions"].items()):
            ltp = px.get(pos.get("ts", sym))
            if ltp is None:
                continue
            pos["live"] = ltp
            if ltp > pos["stop"]:
                continue
            key = f"{today}|{sym}|EXIT"
            if _sent(key):
                continue
            o = {"action": "EXIT", "symbol": sym, "ts": pos.get("ts", sym), "qty": pos["qty"], "why": f"stop {pos['stop']:.2f} hit"}
            try:
                reply = sender(o["ts"], "EXIT")
            except Exception as exc:
                _log(key, today, o, "failed", str(exc))
                hits.append({**o, "price": ltp, "error": str(exc)[:120]})
                continue
            _log(key, today, o, "sent", reply, ltp)
            t = _close(st, sym, ltp, today, "stop", est=False)
            hits.append({**o, "price": ltp, "net": t["net"]})
        _save(st)
        return {"status": "ok", "hits": hits}


# --- reading -------------------------------------------------------------------------------------

def reset() -> None:
    with _lock, get_db() as conn:
        for t in ("real_state", "real_trades", "real_equity"):
            conn.execute(f"DELETE FROM {t}")


def summary(config: AppConfig) -> dict:
    st = _state()
    base = {"enabled": bool(config.real.enabled), "killed": killed(), "variant": config.real.variant,
            "settings": {k: getattr(config.real, k) for k in ("capital", "send_time", "send_until", "stop_check_min",
                                                               "max_buys_per_day", "max_order_pct")},
            "max_pos": config.breakout.max_pos, "token_set": bool(tradetron.token()),
            "tops": {k: tb.rules(config, k).top for k in tb.MODES}}
    with get_db() as conn:
        log = [dict(r) for r in conn.execute("SELECT created_at, day, symbol, action, qty, reason, status, reply, price "
                                             "FROM real_orders ORDER BY id DESC LIMIT 200")]
    if not st:
        cap = float(config.real.capital)
        return {"started": False, **base, "log": log,
                "limits": {"value": cap, "size": round(cap / config.breakout.max_pos, 2),
                           "max_order": round(cap * config.real.max_order_pct / 100, 2)}}
    with get_db() as conn:
        trades = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM real_trades ORDER BY id DESC")]
        eq = [dict(r) for r in conn.execute("SELECT day, equity, gross, nifty FROM real_equity ORDER BY day")]
    value = _value(st)
    cap = st["capital"]
    deposits = st.get("deposits", [])
    first_cap = cap - sum(x["amount"] for x in deposits)
    for e in eq:                                          # the curve without the money added / withdrawn
        e["equity"] -= sum(x["amount"] for x in deposits if x["at"][:10] <= e["day"])
    realised = sum(t["net"] for t in trades)
    peak, dd = first_cap, 0.0
    for e in eq:
        peak = max(peak, e["equity"])
        dd = max(dd, (peak - e["equity"]) / peak * 100)
    positions = []
    for sym, x in sorted(st["positions"].items(), key=lambda kv: kv[1]["d"], reverse=True):
        last = x.get("live") or x["last"]
        exit_ch = tb.delivery_charges(x["qty"] * last, True)
        gross = (last - x["px"]) * x["qty"]
        positions.append({"symbol": sym, "qty": x["qty"], "in": x["d"], "buy": round(x["px"], 2), "last": round(last, 2),
                          "stop": round(x["stop"], 2), "variant": x.get("variant"), "days": x.get("days", 0),
                          "gross": round(gross, 2), "costs": round(x["charges"] + exit_ch, 2),
                          "net": round(gross - x["charges"] - exit_ch, 2), "est": x.get("est", False)})
    won = [t for t in trades if t["net"] > 0]
    loss = -sum(t["net"] for t in trades if t["net"] <= 0)
    nifty = round((eq[-1]["nifty"] / eq[0]["nifty"] - 1) * 100, 2) if len(eq) > 1 and eq[0]["nifty"] else 0.0
    return {"started": True, **base, "log": log, "start_date": st["started"], "as_of": st.get("as_of"),
            "updated_at": st.get("updated_at"), "orders": st.get("orders", []), "decided": st.get("decided"),
            "filter": st.get("filter"), "cap": st.get("cap"), "allowed": st.get("allowed"), "waiting": st.get("waiting", 0),
            "limits": limits(st, config), "deposits": deposits,
            "account": {"capital": cap, "value": round(value, 2), "cash": round(st["cash"], 2), "positions": len(positions),
                        "gross": round(value + st["charges"] - cap, 2), "costs": round(st["charges"], 2),
                        "net": round(value - cap, 2), "return": round((value - cap) / cap * 100, 2),
                        "realised": round(realised, 2), "unrealised": round(value - cap - realised, 2), "nifty": nifty},
            "positions": positions, "trades": trades, "won": len(won), "closed": len(trades),
            "profit_factor": round(sum(t["net"] for t in won) / loss, 2) if loss else None, "drawdown": round(dd, 2),
            "curve": {"dates": [e["day"] for e in eq], "net": [e["equity"] for e in eq],
                      "nifty": [round(e["nifty"] / eq[0]["nifty"] * first_cap, 2) if eq and eq[0]["nifty"] else None for e in eq]}}


def due(now: datetime, config: AppConfig, done: set, trading: bool, last_stop: Optional[datetime]) -> list[str]:
    """Which real-trading jobs run at `now` (pure: the scheduler and the tests)."""
    from backend.alerts.jobs import MARKET_CLOSE, hm
    rc, t, jobs = config.real, now.time(), []
    if not trading:
        return jobs
    if "real_send" not in done and hm(rc.send_time) <= t < hm(rc.send_until):
        jobs.append("real_send")
    if hm(rc.send_time) <= t < MARKET_CLOSE and (last_stop is None or (now - last_stop).total_seconds() >= rc.stop_check_min * 60):
        jobs.append("real_stops")
    return jobs
