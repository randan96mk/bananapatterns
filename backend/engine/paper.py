"""📝 Paper trading: the Trend Breakout E+ rules (backend/engine/trend_breakout.py) run forward, one session at a
time, from the day it is started, in one virtual account per variant (E+ is the only one since A was removed in Nov
2026; its account key is "E"). Simulated: never orders, never a Kite order call.

Each evening (after the daily sync), for every new session since the last one processed, exactly as simulate():
  1. yesterday's SELL orders fill at today's open (slippage, delivery charges);
  2. yesterday's BUY orders fill at today's open, best 6-month return first, equal weight, while the account has a
     free slot under the half filter (10, 5 or none):
     qty = floor(min(account value / max_pos, cash) / (fill x 1.0012)); skipped when no slot or under 1 share;
  3. a day's low at or under a position's stop sells it at min(open, stop); otherwise it is marked at the close;
  4. at the close: a close under SMA(exit_sma) queues a SELL; the day's breakouts queue BUYs (filter permitting).
replan (↻ Re-evaluate on the tab) runs step 4 again by hand from the same data and the account as it is now (after a
deposit or a max_pos change) and reports which breakouts fit and which wait, and why.
Fills, trades and the daily account are appended and never rewritten; a session already processed is skipped, so
running the job twice changes nothing. Gross (price move), charges and slippage are kept apart. Stops are checked on
the daily low after the close (no live intraday price), as in the backtest.
"""

import json
import threading
from math import floor
from typing import Optional

from backend.config import AppConfig
from backend.database.models import get_db, now_ist, set_setting
from backend.engine import trend_breakout as tb
from backend.logging_config.logger import setup_logger

logger = setup_logger("paper")

_lock = threading.Lock()


def variants(config: AppConfig) -> list[str]:
    """The enabled accounts that exist: a saved config still listing "A" gives ["E"]."""
    return [v for v in (config.paper.variants or ["E"]) if v in tb.MODES] or list(tb.MODES)


def _rules(config: AppConfig, key: str):
    return tb.rules(config, key)


def _states() -> dict:
    """Every existing account's state (an "A" row from before the migration is never read); one saved before the
    accounts had a variant (Oct 2026) gets its variant and name."""
    with get_db() as conn:
        states = {r["account"]: json.loads(r["payload"]) for r in conn.execute("SELECT account, payload FROM paper_state")
                  if r["account"] in tb.MODES}
    for k, st in states.items():
        st.setdefault("variant", k)
        st.setdefault("name", tb.variant_name(k, st.get("top", 300)))
    return states


def _save(key: str, st: dict, trades: list, fills: list, equity: list) -> None:
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO paper_state (account, payload) VALUES (?, ?)", (key, json.dumps(st)))
        conn.executemany("INSERT INTO paper_trades (account, payload) VALUES (?, ?)", [(key, json.dumps(t)) for t in trades])
        conn.executemany("INSERT INTO paper_fills (account, day, payload) VALUES (?, ?, ?)",
                         [(key, f["day"], json.dumps(f)) for f in fills])
        conn.executemany("INSERT OR REPLACE INTO paper_equity (account, day, equity, gross, nifty) VALUES (?, ?, ?, ?, ?)",
                         [(key, *e) for e in equity])


def _new(key: str, data: dict, i: int, cap: float, p) -> dict:
    st = {"variant": key, "name": tb.variant_name(key, p.top), "top": p.top, "started": data["cal"][i], "capital": cap, "cash": cap,
          "charges": 0.0, "slippage": 0.0, "as_of": data["cal"][i], "positions": {}, "orders": [],
          "started_at": now_ist(), "updated_at": now_ist()}
    if p.late_at:                                         # started as E+: nothing to migrate
        st["eplus_since"] = data["cal"][i]
    _decide(st, data, i, p)
    _save(key, st, [], [], [(data["cal"][i], cap, cap, data["index_close"][i])])
    return st


def start(config: AppConfig, capital: Optional[float] = None, at: Optional[str] = None) -> dict:
    """Start every enabled account with `capital` each (default: the paper capital setting) on the same day: the
    latest session's close (or `at`'s, for a replay) decides the first orders, which fill at the next open."""
    with _lock:
        if _states():
            return {"status": "started_already"}
        data = tb.load(config)
        if "error" in data:
            return {"status": "no_data", "error": data["error"]}
        cap = float(capital or config.paper.capital)
        if cap <= 0:
            return {"status": "bad_capital"}
        i = data["cal"].index(at) if at in data["cal"] else len(data["cal"]) - 1
        for key in variants(config):
            _new(key, data, i, cap, _rules(config, key))
        logger.info("Paper trading started %s with Rs %.0f per account (%s)", data["cal"][i], cap, ", ".join(variants(config)))
        return {"status": "ok", "started": data["cal"][i], "accounts": variants(config)}


def reset() -> None:
    with _lock, get_db() as conn:
        for t in ("paper_state", "paper_trades", "paper_fills", "paper_equity"):
            conn.execute(f"DELETE FROM {t}")


def first_capital(st: dict) -> float:
    """The capital the account started with: today's capital less the money added / withdrawn since."""
    return st["capital"] - sum(x["amount"] for x in st.get("deposits", []))


def apply_deposit(st: dict, capital: float) -> float:
    """A new capital on a running account: the difference is money added or withdrawn (cash moves, the profit so far
    stays) and is recorded in `deposits`, so the report never counts it as profit. Returns the delta. The state is
    changed in place and not saved (set_capital and the start-up migration save it)."""
    delta = float(capital) - st["capital"]
    if st["cash"] + delta < 0:
        raise ValueError(f"withdrawing {-delta:,.0f} is more than the cash ({st['cash']:,.0f}); "
                         "lower it after positions are sold")
    st["capital"] = float(capital)
    st["cash"] += delta
    st.setdefault("deposits", []).append({"at": now_ist(), "amount": round(delta, 2), "capital": float(capital)})
    st["updated_at"] = now_ist()
    return delta


def set_capital(config: AppConfig, capital: float) -> dict:
    """A new paper capital: before the start it is simply the setting; afterwards every account gets the difference
    as money added / withdrawn (like the real account), and the next BUYs are sized from the new value."""
    with _lock:
        states = _states()
        config.paper.capital = float(capital)
        if not states:
            return {"status": "ok", "capital": capital}
        deltas = {}
        for key, st in states.items():                    # check every account before changing any
            if st["cash"] + float(capital) - st["capital"] < 0:
                raise ValueError(f"withdrawing {st['capital'] - float(capital):,.0f} is more than the cash "
                                 f"({st['cash']:,.0f}); lower it after positions are sold")
        for key, st in states.items():
            deltas[key] = apply_deposit(st, capital)
            _save(key, st, [], [], [])
        logger.info("Paper capital set to Rs %.0f (%s)", float(capital),
                    ", ".join(f"{k} {d:+,.0f}" for k, d in deltas.items()))
        return {"status": "ok", "capital": capital, "delta": deltas[next(iter(deltas))], "deltas": deltas}


def _decide(st: dict, data: dict, i: int, p) -> None:
    """At the close of calendar day i: the orders for the next open (simulate's step 4) and the filter state."""
    S = data["S"]
    regime_on, cap = tb.slots(i, data["index_ok"], data["filters"], p)
    sells, buys, _, _ = tb.decide(i, S, {s: None for s in st["positions"] if s in S}, regime_on, p)
    value = st["cash"] + sum(x["qty"] * x["last"] for x in st["positions"].values())
    orders = [{"action": "SELL", "symbol": s, "qty": st["positions"][s]["qty"], "why": f"close under SMA{p.exit_sma}"}
              for s in sorted(sells)]
    for k, (s, stop) in enumerate(buys):
        close = S[s]["rows"][S[s]["at"][i]]["close"]
        qty = floor(min(value / p.max_pos, st["cash"]) / (close * (1 + p.slippage_pct / 100) * 1.0012))
        orders.append({"action": "BUY", "symbol": s, "stop": stop, "rank": k + 1, "close": close,   # unrounded stop
                       "qty_est": qty, "risk_est": round(qty * (close - stop), 2),
                       "ret126": round((tb.val(S[s], S[s]["at"][i], "ret126") or 0) * 100, 1)})
    st["orders"], st["cap"], st["allowed"], st["decided"] = orders, cap, bool(regime_on), data["cal"][i]
    closes = data["index_close"]
    now = tb.filter_day(p.regime_mode, i, closes)
    before = tb.filter_day(p.regime_mode, i - 1, closes) if i else now
    st["filter"] = {"name": st["name"], "top": p.top, "state": tb.filter_label(now, p.regime_mode),
                    "prev": tb.filter_label(before, p.regime_mode), "changed": now != before,
                    "slots": cap if regime_on else 0}


def _taken(st: dict, p) -> dict:
    """What the next open will do with st["orders"] (simulate's step 2, run ahead on the signal closes): the BUYs
    that fit the free slots and the cash, the ones waiting with their reason, the cash left idle."""
    cap = st.get("cap", p.max_pos) if st.get("allowed", True) else 0
    orders = st.get("orders", [])
    sells = [o for o in orders if o["action"] == "SELL" and o["symbol"] in st["positions"]]
    cash = st["cash"] + sum(st["positions"][o["symbol"]]["qty"] * st["positions"][o["symbol"]]["last"] for o in sells)
    value = st["cash"] + sum(x["qty"] * x["last"] for x in st["positions"].values())
    used, taken, waiting = len(st["positions"]) - len(sells), [], []
    for o in orders:
        if o["action"] != "BUY":
            continue
        px = o["close"] * (1 + p.slippage_pct / 100) * 1.0012
        qty = floor(min(value / p.max_pos, cash) / px)
        if not st.get("allowed", True):
            why = "filter OFF"
        elif used >= cap:
            why = f"no free slot ({used} of {cap})" + (" · filter HALF" if cap < p.max_pos else "")
        elif qty < 1:
            why = "cash" if cash < px else "under 1 share"
        else:
            cash -= qty * px
            used += 1
            taken.append({**o, "qty": qty, "capital": round(qty * o["close"], 2)})
            continue
        waiting.append({"symbol": o["symbol"], "rank": o.get("rank"), "close": round(o["close"], 2), "stop": round(o["stop"], 2),
                        "why": why})
    return {"taken": taken, "waiting": waiting, "idle_cash": round(cash, 2), "cap": cap, "slots_used": used,
            "value": round(value, 2), "size": round(value / p.max_pos, 2)}


def replan(config: AppConfig, data: Optional[dict] = None) -> dict:
    """↻ Re-evaluate: every account's next-open orders decided again from the evening's data (the stored candles, no
    Kite call) and the account as it is NOW (capital, cash, positions, max_pos). Simulated as ever: nothing is sent.
    Returns the first account's report (before / after, added / removed, the waiting breakouts with reasons, the idle
    cash, the max_pos suggestion), with every account's under "accounts"."""
    from backend.engine.real import suggest
    with _lock:
        states = _states()
        if not states:
            return {"status": "not_started"}
        data = data or tb.load(config)
        if "error" in data:
            return {"status": "no_data", "error": data["error"]}
        cal, reports = data["cal"], {}
        for key, st in sorted(states.items()):
            p = _rules(config, key)
            i = cal.index(st["decided"]) if st.get("decided") in cal else cal.index(st["as_of"]) if st.get("as_of") in cal else len(cal) - 1
            before = list(st.get("orders", []))
            _decide(st, data, i, p)
            st["updated_at"] = now_ist()
            _save(key, st, [], [], [])
            t = _taken(st, p)
            k = lambda o: (o["symbol"], o["action"])
            was, now = {k(o) for o in before}, {k(o) for o in st["orders"]}
            sug = suggest(t["value"], t["idle_cash"], p.max_pos, t["cap"], t["waiting"], t["value"], len(t["waiting"]),
                          price_mult=(1 + p.slippage_pct / 100) * 1.0012) if st.get("allowed", True) else None
            reports[key] = {"status": "ok", "account": key, "at": now_ist(), "before": before, "after": st["orders"],
                            "added": [o for o in st["orders"] if k(o) not in was], "removed": [o for o in before if k(o) not in now],
                            "taken": t["taken"], "waiting": t["waiting"], "idle_cash": t["idle_cash"], "size": t["size"],
                            "max_pos": p.max_pos, "slots_used": t["slots_used"], "slots": t["cap"], "capital": st["capital"],
                            "cash": round(st["cash"], 2), "value": t["value"], "decided": st["decided"], "filter": st.get("filter"),
                            "bt_max_pos": ((tb.saved() or {}).get("settings") or {}).get("max_pos"), "suggestion": sug}
    first = reports[next(iter(reports))]
    return {**first, "accounts": reports}


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
             "net": round(gross - charges - slippage, 2), "why": why,
             "best": round(pos.get("best") or pos["px"], 2)}
        trades.append(t)
        fills.append({"day": d, "action": "SELL", "symbol": sym, "qty": pos["qty"], "price": round(px, 2), "why": why,
                      "gross": t["gross"], "costs": t["costs"], "net": t["net"], "days": pos["days"]})

    orders, st["orders"], st["skipped"] = st.get("orders", []), [], []
    cap = st.get("cap", p.max_pos)
    for o in orders:                                      # 1) sells at the open
        b = bar(o["symbol"])
        if o["action"] == "SELL" and o["symbol"] in pos_all and b:
            close_pos(o["symbol"], b["open"], "exit")
    equity = st["cash"] + sum(x["qty"] * x["last"] for x in pos_all.values())
    buys = [o for o in orders if o["action"] == "BUY"]
    for k, o in enumerate(buys):                          # 2) buys at the open, equal weight, within the slot cap
        sym = o["symbol"]
        if len(pos_all) >= cap:
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
                        "best": b["open"],
                        "charges": ch, "slippage": sl, "days": 0}
        fills.append({"day": d, "action": "BUY", "symbol": sym, "qty": qty, "price": round(b["open"], 2),
                      "stop": round(o["stop"], 2), "value": round(qty * f, 2), "charges": round(ch, 2),
                      "slippage": round(sl, 2)})
    for sym in list(pos_all):                             # 3) stops during the day, then the close
        b = bar(sym)
        if not b:
            continue
        pos = pos_all[sym]
        pos["days"] += 1
        if pos["stop"] and b["low"] <= pos["stop"]:
            close_pos(sym, min(b["open"], pos["stop"]), "trail" if pos.get("trailed") else "stop")
            continue
        pos["prev"], pos["last"] = pos["last"], b["close"]
        if p.late_at:                                     # E+: the late trail from today's close, used tomorrow
            before = pos.get("stop")
            new = tb.raise_late(pos, b["close"], p)
            if new is not None:
                st.setdefault("raised", []).append(_raise_note(sym, pos, d, before))
    return trades, fills


def _raise_note(sym: str, pos: dict, d: str, before: Optional[float] = None) -> dict:
    return {"day": d, "symbol": sym, "stop": round(pos["stop"], 2), "best": round(pos["best"], 2), "entry": round(pos["px"], 2),
            "from": round(before, 2) if before else None}


def best_since(S: dict, sym: str, since: str, until: str) -> Optional[float]:
    """The highest stored daily close of sym from `since` (the entry day) to `until`, both included."""
    s = S.get(sym)
    if not s:
        return None
    xs = [r["close"] for r in s["rows"] if since <= r["date"] <= until]
    return max(xs) if xs else None


def migrate_eplus(st: dict, data: dict, p) -> list:
    """Once, when E becomes E+: each open position's best close is backfilled from the stored daily candles (entry
    day to the last processed session) and the late trail is applied from that close, for the next session. Past
    days are not re-simulated; a stop is never lowered. Returns the raises."""
    if not p.late_at or st.get("eplus_since"):
        return []
    raised = []
    for sym, pos in st["positions"].items():
        best = best_since(data["S"], sym, pos["d"], st["as_of"])
        if best is None:
            best = max(pos["px"], pos["last"])
        pos["best"] = best
        before = pos.get("stop")
        if tb.raise_late(pos, best, p) is not None:
            raised.append(_raise_note(sym, pos, st["as_of"], before))
    st["eplus_since"] = st["as_of"]
    st["name"] = tb.variant_name(st.get("variant", "E"), p.top)
    st.setdefault("notes", []).append({"day": st["as_of"], "text": f"strategy updated to E+ on {st['as_of']}: late "
                                       f"trail ({p.late_pct:g}% under the highest close once +{p.late_at:g}%); best "
                                       f"closes backfilled for {len(st['positions'])} open position(s)"})
    return raised


def _advance(key: str, st: dict, data: dict, p) -> int:
    """Every session after st["as_of"]; returns how many were processed (0 when already up to date)."""
    cal = data["cal"]
    if st.get("top", p.top) != p.top:                     # the universe changed: noted in the log; positions stay
        st.setdefault("notes", []).append({"day": st["as_of"], "text": f"universe top {st.get('top')} → top {p.top} "
                                                                       f"for new buys from the next session"})
        st["top"], st["name"] = p.top, tb.variant_name(key, p.top)
    st["raised"] = migrate_eplus(st, data, p)            # once; then the trail runs in _step
    new = [i for i, d in enumerate(cal) if d > st["as_of"]]
    all_trades, all_fills, equity = [], [], []
    for i in new:
        trades, fills = _step(st, data, i, p)
        all_trades += trades
        all_fills += fills
        st["as_of"] = cal[i]
        eq = st["cash"] + sum(x["qty"] * x["last"] for x in st["positions"].values())
        equity.append((cal[i], round(eq, 2), round(eq + st["charges"] + st["slippage"], 2), data["index_close"][i]))
        _decide(st, data, i, p)
    st["compare"] = _compare(st, data, p)
    st["updated_at"] = now_ist()
    _save(key, st, all_trades, all_fills, equity)
    if new:                                               # the last run's raises, for Home's timeline
        with get_db() as conn:
            set_setting(conn, "paper.trail_raised", json.dumps({"day": st["as_of"], "account": key, "raised": st.get("raised", [])}))
    return len(new)


def process(config: AppConfig, data: Optional[dict] = None) -> dict:
    """Every account's new sessions; an enabled account that is missing starts on the same day as the others,
    replayed from the stored candles. Returns the day's report, also kept for the evening report."""
    with _lock:
        states = _states()
        if not states:
            return {"status": "not_started"}
        data = data or tb.load(config)
        if "error" in data:
            return {"status": "no_data", "error": data["error"]}
        first = min(states.values(), key=lambda s: s["started"])
        for key in variants(config):
            if key not in states and first["started"] in data["cal"]:
                states[key] = _new(key, data, data["cal"].index(first["started"]), first["capital"], _rules(config, key))
        sessions = {key: _advance(key, st, data, _rules(config, key)) for key, st in states.items() if key in tb.MODES}
        return {"status": "ok", "sessions": max(sessions.values(), default=0), "report": report(_states())}


def _compare(st: dict, data: dict, p) -> dict:
    """The backtest over the same sessions with the same capital and rules (positions left open at the end), for
    paper vs backtest: a gap means the data or the rules drifted."""
    cal = data["cal"]
    if st["started"] not in cal:
        return {}
    s = cal.index(st["started"])
    eq, eqg, trades, _ = tb.simulate(cal, data["S"], data["index_ok"], p, p.slippage_pct, s,
                                     capital=st["capital"], close_end=False, filters=data["filters"])
    c = st["capital"]
    return {"return": round((eq[-1] / c - 1) * 100, 2), "gross": round((eqg[-1] / c - 1) * 100, 2),
            "costs": round(eqg[-1] - eq[-1], 2), "trades": len(trades)}


def _fills(key: str, day: str) -> list:
    with get_db() as conn:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM paper_fills WHERE account = ? AND day = ? ORDER BY id", (key, day))]


def report(states: dict) -> Optional[dict]:
    """The latest session of every account, for the evening report and Telegram."""
    if not states:
        return None
    as_of = max(st["as_of"] for st in states.values())
    accounts = {k: {"name": st["name"], "fills": _fills(k, st["as_of"]), "orders": st.get("orders", []),
                    "raised": st.get("raised", []),
                    "account": account(k, st), "cap": st.get("cap", 10), "allowed": st.get("allowed")}
                for k, st in sorted(states.items())}
    first = next(iter(accounts.values()))["account"]
    return {"as_of": as_of, "accounts": accounts, "filters": {k: st.get("filter") for k, st in sorted(states.items())
                                                             if st.get("filter")}, "nifty": first["nifty"]}


def account(key: str, st: dict) -> dict:
    """Value, cash, before / after costs, realised / unrealised, NIFTY 50 over the same days."""
    held = st["positions"]
    value = st["cash"] + sum(x["qty"] * x["last"] for x in held.values())
    costs = st["charges"] + st["slippage"]
    with get_db() as conn:
        rows = conn.execute("SELECT nifty FROM paper_equity WHERE account = ? ORDER BY day", (key,)).fetchall()
        realised = sum(json.loads(r[0])["net"] for r in conn.execute(
            "SELECT payload FROM paper_trades WHERE account = ?", (key,)))
    nifty = round((rows[-1][0] / rows[0][0] - 1) * 100, 2) if len(rows) > 1 and rows[0][0] else 0.0
    cap = st["capital"]
    deposits = st.get("deposits", [])
    return {"capital": cap, "first_capital": round(first_capital(st), 2), "deposits": deposits,
            "deposited": round(sum(x["amount"] for x in deposits), 2),
            "value": round(value, 2), "cash": round(st["cash"], 2), "positions": len(held),
            "gross": round(value + costs - cap, 2), "charges": round(st["charges"], 2),
            "slippage": round(st["slippage"], 2), "costs": round(costs, 2), "net": round(value - cap, 2),
            "realised": round(realised, 2), "unrealised": round(value - cap - realised, 2),
            "return_gross": round((value + costs - cap) / cap * 100, 2), "return": round((value - cap) / cap * 100, 2),
            "nifty": nifty}


def _one(key: str, st: dict, config: AppConfig) -> dict:
    p = _rules(config, key)
    with get_db() as conn:
        trades = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM paper_trades WHERE account = ? ORDER BY id DESC", (key,))]
        eq = [dict(r) for r in conn.execute(
            "SELECT day, equity, gross, nifty FROM paper_equity WHERE account = ? ORDER BY day", (key,))]
    deposits, first_cap = st.get("deposits", []), first_capital(st)
    for e in eq:                                          # the curve without the money added / withdrawn
        added = sum(x["amount"] for x in deposits if x["at"][:10] <= e["day"])
        e["equity"] -= added
        e["gross"] -= added
    peak, dd = first_cap, 0.0
    for e in eq:
        peak = max(peak, e["equity"])
        dd = max(dd, (peak - e["equity"]) / peak * 100)
    data_sma = st.get("sma", {})
    positions = []
    for sym, x in sorted(st["positions"].items(), key=lambda kv: kv[1]["d"], reverse=True):
        exit_fill = x["last"] * (1 - p.slippage_pct / 100)
        exit_ch = tb.delivery_charges(x["qty"] * exit_fill, True)
        gross = (x["last"] - x["px"]) * x["qty"]
        costs = x["charges"] + x["slippage"] + exit_ch + (x["last"] - exit_fill) * x["qty"]
        jump = x.get("prev") and abs(x["last"] / x["prev"] - 1) > 0.35
        positions.append({"symbol": sym, "qty": x["qty"], "in": x["d"], "buy": round(x["px"], 2),
                          "last": round(x["last"], 2), "stop": round(x["stop"], 2), "sma": data_sma.get(sym),
                          "days": x["days"], "gross": round(gross, 2), "costs": round(costs, 2),
                          "best": round(x["best"], 2) if x.get("best") else None, "trailed": bool(x.get("trailed")),
                          "trail_active": bool(p.late_at and x.get("best") and x["best"] >= x["px"] * (1 + p.late_at / 100)),
                          "net": round(gross - costs, 2),
                          "warning": "big overnight change: split or bonus?" if jump else None})
    won = [t for t in trades if t["net"] > 0]
    gain, loss = sum(t["net"] for t in won), -sum(t["net"] for t in trades if t["net"] <= 0)
    return {"name": st["name"], "start_date": st["started"], "as_of": st["as_of"], "updated_at": st.get("updated_at"),
            "account": account(key, st), "deposits": deposits, "drawdown": round(dd, 2), "positions": positions,
            "orders": st.get("orders", []), "skipped": st.get("skipped", []), "cap": st.get("cap", p.max_pos),
            "allowed": st.get("allowed"), "filter": st.get("filter"), "decided": st.get("decided"),
            "trades": trades, "won": len(won), "closed": len(trades), "notes": st.get("notes", []), "top": st.get("top"),
            "profit_factor": round(gain / loss, 2) if loss else None,
            "trade_totals": {k: round(sum(t[k] for t in trades), 2) for k in ("gross", "charges", "slippage", "costs", "net")},
            "curve": {"dates": [e["day"] for e in eq], "net": [e["equity"] for e in eq], "gross": [e["gross"] for e in eq],
                      "nifty": [round(e["nifty"] / eq[0]["nifty"] * first_cap, 2) if eq and eq[0]["nifty"] else None
                                for e in eq]},
            "compare": st.get("compare") or {}}


def summary(config: AppConfig) -> dict:
    """Everything the 📝 Paper trading tab shows: the accounts ({"E": ...})."""
    states = _states()
    base = {"capital": config.paper.capital, "variants": variants(config), "max_pos": config.breakout.max_pos,
            "slippage_pct": config.breakout.slippage_pct}
    if not states:
        return {"started": False, **base}
    try:
        sma = _sma_now(config, {s for st in states.values() for s in st["positions"]})
    except Exception:
        sma = {}
    for st in states.values():
        st["sma"] = sma
    return {"started": True, **base, "accounts": {k: _one(k, st, config) for k, st in sorted(states.items())}}


def _sma_now(config: AppConfig, symbols: set) -> dict:
    """The latest SMA(exit_sma) of the held stocks, from the stored candles (for the positions table)."""
    if not symbols:
        return {}
    from backend.database.models import get_candles
    from backend.ingest import universe as uni
    n = config.breakout.exit_sma
    tokens = {s["symbol"]: s["instrument_token"] for s in uni.resolve_universe(config.ingest)["stocks"]}
    out = {}
    with get_db() as conn:
        for sym in symbols:
            if sym in tokens:
                cs = get_candles(conn, tokens[sym], limit=n)
                if len(cs) == n:
                    out[sym] = round(sum(c["close"] for c in cs) / n, 2)
    return out


def latest_report() -> Optional[dict]:
    return report(_states())
