"""🧪 Intraday Flow backtest (phase 3): replays saved 1-minute candles day by day through the same boxes and the same
paper-position code as live (next 1-minute open + slippage, intraday charges, stops / trails / square-off),
for the canvas or a saved setup, over 6 months / 1 year / 2 years. Only data up to each minute is used: daily
averages and turnover from the days before, warm-up from the saved sessions before, bars closed before the minute.
Missing stock-days are fetched from Kite once (then saved). Never orders, never touches the live paper positions."""

import json
import multiprocessing as mp
import os
import threading
import time as _time
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Optional

from backend.config import AppConfig
from backend.database.models import get_candles, get_db, get_setting, now_ist, set_setting
from backend.intraday import flow, store
from backend.intraday import indicators as ind
from backend.intraday import positions as pos
from backend.intraday.candles import resample
from backend.intraday.modules import REGISTRY
from backend.logging_config.logger import setup_logger

logger = setup_logger("flow_backtest")

PERIODS = {"6m": 6, "1y": 12, "2y": 24}
CANVAS = "On the canvas"
STRESS_EXTRA_PCT = 0.10                      # stressed: this much more slippage on every fill
_status = {"state": "idle", "message": "", "pct": 0, "at": None}
_lock = threading.Lock()
_cancel = threading.Event()
_job: Optional[threading.Thread] = None          # the run's thread (Kite fetch, then waits for the replay process)
_proc = None                                     # the replay's own process: the app stays responsive, ⏹ kills it


class Stopped(Exception):
    pass


def _set(**kw) -> None:
    """Progress, shared through the database (the replay runs in its own process)."""
    _status.update(kw)
    with get_db() as conn:
        set_setting(conn, "flow.bt_status", json.dumps(_status))


def status() -> dict:
    with get_db() as conn:
        st = json.loads(get_setting(conn, "flow.bt_status") or "null") or dict(_status)
    if st.get("state") == "running" and not (_job and _job.is_alive()) and not _lock.locked():
        st.update(state="idle", message="stopped: the app restarted during the run · run it again")
    return st


def cancel() -> dict:
    """⏹ Stops a running backtest now (the Kite fetch between stocks, the replay at once). Nothing is saved."""
    if not (_job and _job.is_alive()):
        return {"status": "idle"}
    _cancel.set()
    if _proc is not None and _proc.is_alive():
        _proc.terminate()
    return {"status": "stopping"}


# --- presets: well-known strategies, ready to load or test (read-only) ------------------------------------

_NO_FILTERS = {"trend": {"above_50": False, "above_200": False, "mirror": False},
               "filters": {"vwap": False, "rsi_on": False, "rvol_on": False, "ema_on": False, "st_on": False},
               "context": {"direction": "both", "follow": False, "ema_on": False, "vix_on": False, "breadth_on": False}}
PRESETS = {
    "📚 ORB 5m · stocks in play": {
        "desc": "Zarattini & Aziz (2023): only the ~20 stocks with unusual volume in the first 5 minutes; trade in the "
                "first 5-minute candle's direction when price breaks its high (green) or low (red); stop 10% of the daily "
                "ATR(14); no target, out at square-off. Entry here: a 5-minute close beyond the range, filled at the next open.",
        "nodes": {**_NO_FILTERS, "trigger": {"interval": 5, "start": "09:20", "end": "14:30"},
                  "universe": {"top": 500, "in_play": True, "in_play_top": 20, "in_play_min": 5},
                  "orb": {"minutes": 5, "with_open": True}, "score": {"top_k": 20}, "dedup": {"per_day": 20},
                  "stop": {"stop": "daily_atr", "daily_atr_frac": 0.1, "min_stop_pct": 0.05, "max_stop_pct": 2.5,
                           "target_r": 0, "partial_on": False, "trail": "off", "time_stop_min": 0, "squareoff": "15:15"},
                  "size": {"risk_pct": 0.5, "max_pos_pct": 50, "max_open": 4, "max_trades": 20, "daily_loss_pct": 2}}},
    "📚 ORB 30m · with the trend": {
        "desc": "Classic opening range breakout (Crabel style): the first 30 minutes' range, only with the daily trend "
                "(50 / 200-day) and NIFTY's side of its VWAP, above VWAP with RVOL ≥ 1.5; stop at the other side of the "
                "range; target 2R, stop to breakeven after 1R; at most 2 trades a day.",
        "nodes": {"trigger": {"interval": 5, "start": "09:45", "end": "13:30"}, "universe": {"top": 200},
                  "trend": {"above_50": True, "above_200": True, "mirror": True},
                  "filters": {"vwap": True, "rsi_on": False, "rvol_on": True, "rvol_min": 1.5, "ema_on": False, "st_on": False},
                  "context": {"direction": "both", "follow": True, "ema_on": False, "vix_on": False, "breadth_on": False},
                  "orb": {"minutes": 30, "with_open": False}, "score": {"top_k": 2}, "dedup": {"per_day": 2},
                  "stop": {"stop": "orb", "min_stop_pct": 0.2, "max_stop_pct": 2.0, "target_r": 2, "partial_on": False,
                           "trail": "breakeven", "trail_after_r": 1, "time_stop_min": 0, "squareoff": "15:15"},
                  "size": {"risk_pct": 0.5, "max_pos_pct": 30, "max_open": 2, "max_trades": 2, "daily_loss_pct": 1.5}}},
}


def all_setups() -> dict:
    """Presets (read-only) and the saved setups."""
    return {**{k: {**v, "preset": True} for k, v in PRESETS.items()}, **setups()}


# --- saved setups: a named copy of every box's settings -------------------------------------------------

def setups() -> dict:
    with get_db() as conn:
        return json.loads(get_setting(conn, "flow.setups") or "{}")


def _put(all_: dict) -> None:
    with get_db() as conn:
        set_setting(conn, "flow.setups", json.dumps(all_))


def save_setup(name: str) -> dict:
    name = name.strip()[:60]
    if not name or name == CANVAS or name in PRESETS:
        raise ValueError("a name is needed")
    all_ = setups()
    all_[name] = {"nodes": flow.node_configs(), "saved_at": now_ist()}
    _put(all_)
    return all_[name]


def delete_setup(name: str) -> None:
    if name in PRESETS:
        raise ValueError("a preset can't be deleted")
    all_ = setups()
    all_.pop(name, None)
    _put(all_)
    with get_db() as conn:
        conn.execute("DELETE FROM flow_backtests WHERE setup = ?", (name,))


def load_setup(name: str) -> None:
    """Puts a saved setup's settings on the canvas (the live flow uses them from the next candle)."""
    nodes = configs_of(name) if name in PRESETS else setups()[name]["nodes"]
    with get_db() as conn:
        set_setting(conn, "flow.nodes", json.dumps(nodes))
    from backend.intraday import strategy
    strategy.record("Every box", [f"setup \"{name}\" loaded on the canvas"])


def configs_of(name: str) -> dict:
    if name == CANVAS:
        return flow.node_configs()
    saved = all_setups()[name]["nodes"]
    return {nid: {**REGISTRY[kind].defaults(), **saved.get(nid, {})} for nid, kind, _, _ in flow.template()["nodes"]}


# --- data ---------------------------------------------------------------------------------------------

def sessions(months: int, warmup: int) -> tuple[list[str], list[str], dict]:
    """(warm-up days, test days, NIFTY 50 daily closes) from the saved NIFTY 50 daily candles."""
    with get_db() as conn:
        rows = get_candles(conn, flow.NIFTY_TOKEN)
    if not rows:
        return [], [], {}
    end = date.fromisoformat(rows[-1]["date"])
    start = (end - timedelta(days=int(months * 30.44))).isoformat()
    days = [r["date"] for r in rows if r["date"] > start]
    before = [r["date"] for r in rows if r["date"] <= start][-warmup:] if warmup else []
    return before, days, {r["date"]: r["close"] for r in rows}


def fetch_missing(client, tokens: list[int], days: list[str], progress=None) -> dict:
    """Fetches the stock-days not saved yet (one Kite request per stock per ~2 months). Returns counts."""
    todo: dict[int, list[str]] = defaultdict(list)
    for k in range(0, len(days), 400):
        for tok, d in store.missing(tokens, days[k:k + 400]):
            todo[tok].append(d)
    need = sum(len(v) for v in todo.values())
    if not need or client is None or not getattr(client, "has_token", False):
        return {"missing": need, "fetched": 0}
    got = 0
    for n, (tok, ds) in enumerate(todo.items(), 1):
        ds.sort()
        chunk = [ds[0]]
        for d in ds[1:] + [None]:                          # spans of up to ~55 calendar days, one request each
            if d and (date.fromisoformat(d) - date.fromisoformat(chunk[0])).days <= 55:
                chunk.append(d)
                continue
            try:
                got += store.fetch(client, tok, chunk)
            except Exception as exc:
                logger.warning("Backtest fetch %s %s..%s: %s", tok, chunk[0], chunk[-1], exc)
            _time.sleep(0.35)                              # Kite: 3 requests a second
            chunk = [d]
        if progress:
            progress(n, len(todo))
    return {"missing": need - got, "fetched": got}


def _day_bars(day: str) -> dict:
    with get_db() as conn:
        return {r[0]: store._unpack(r[1]) for r in conn.execute(
            "SELECT instrument_token, bars FROM candles_intraday WHERE day = ? AND interval = 1", (day,))}


class _Daily:
    """Each stock's 50 / 200-day averages and 20-day turnover from the daily candles BEFORE a day (prefix sums)."""

    def __init__(self, stocks: list[dict], start: str):
        self.s = {}
        with get_db() as conn:
            for st in stocks:
                rows = get_candles(conn, st["instrument_token"], start=start)
                c, t, tr = [0.0], [0.0], [0.0]
                for k, r in enumerate(rows):
                    c.append(c[-1] + r["close"])
                    t.append(t[-1] + r["close"] * r["volume"])
                    pc = rows[k - 1]["close"] if k else r["close"]
                    tr.append(tr[-1] + max(r["high"], pc) - min(r["low"], pc))
                self.s[st["symbol"]] = ([r["date"] for r in rows], c, t, tr, [(r["high"], r["low"]) for r in rows])

    def at(self, day: str) -> tuple[dict, dict]:
        daily, turn = {}, {}
        for sym, (dates, c, t, tr, hl) in self.s.items():
            i = bisect_left(dates, day)                    # rows before the day
            daily[sym] = {"sma50": (c[i] - c[i - 50]) / 50 if i >= 50 else None,
                          "sma200": (c[i] - c[i - 200]) / 200 if i >= 200 else None,
                          "atr14": (tr[i] - tr[i - 14]) / 14 if i >= 15 else None,
                          "prev_high": hl[i - 1][0] if i else None, "prev_low": hl[i - 1][1] if i else None}
            n = min(20, i)
            turn[sym] = (t[i] - t[i - n]) / n if n else 0
        return daily, turn


class _View:
    """What the live CandleBuilder offers the boxes, cut at a minute: bars that closed before `t`."""

    def __init__(self, all_bars: dict):
        self.all, self.t = all_bars, "09:15"
        self.ts = {k: [b["t"] for b in v] for k, v in all_bars.items()}

    def day(self, tok):
        v = self.all.get(tok)
        return v[:bisect_left(self.ts[tok], self.t)] if v else []

    @property
    def bars(self):
        return {k: self.day(k) for k in self.all}


class _Replay(flow.Ctx):
    """flow.Ctx with the day's paper positions kept in memory (nothing saved, no Telegram)."""

    def __init__(self, *a, book: list, signalled: set, **k):
        super().__init__(*a, **k)
        self.book, self.signalled = book, signalled

    def positions(self) -> dict:
        return pos.stats_of(self.book)

    def open_paper(self, it: dict, kind: str = "paper") -> None:
        p = pos.open_new(self.day, it, flow.candle_last_minute(it["signal_bar"]["t"], self.interval), it["plan"], save=False)
        p["setup"] = it.get("setup")
        self.book.append(p)

    def signalled_today(self) -> set:
        return self.signalled

    def _today(self, it) -> list:
        """Today's n-minute bars, built incrementally through the day (runs fall on candle closes, so every bucket
        before the run is complete)."""
        key = (it["token"], "today", self.interval)
        n, done = self.cache.get(key, (0, []))
        today = self.today_bars(it)
        if len(today) > n:
            done = done + resample(today[n:], self.interval)
            self.cache[key] = (len(today), done)
        return done

    def _hist(self, it) -> list:
        """The warm-up days' n-minute bars (resampled once a day)."""
        k = (it["token"], self.interval)
        if k not in self.cache:
            self.cache[k] = resample(self.history.get(it["token"], []), self.interval)
        return self.cache[k]

    def bars(self, it):
        return self._hist(it) + self._today(it)

    def last_closed(self, it):
        b = self._today(it)
        return b[-1] if b else None

    # the indicators continued from the candle before (the day's cache keeps each one's state): the same numbers
    # as over the whole list, without reading every bar again at every candle
    def _new_bars(self, it, seen: int) -> list:
        h, t = self._hist(it), self._today(it)
        return t[seen - len(h):] if seen >= len(h) else h[seen:] + t

    def rsi_of(self, it):
        key = (it["token"], self.interval, "rsi")
        st = self.cache.get(key)
        val, st = ind.rsi_state([b["c"] for b in self._new_bars(it, st[0] if st else 0)], state=st)
        self.cache[key] = st
        return val

    def vwap_of(self, it):
        key = (it["token"], "vwap")
        st = self.cache.get(key)
        val, st = ind.vwap_state(self.today_bars(it)[st[0] if st else 0:], st)
        self.cache[key] = st
        return val

    def rvol_of(self, it):
        h, t = self._hist(it), self._today(it)
        tail = t[-21:] if len(t) >= 21 else h[len(h) - 21 + len(t):] + t
        return ind.rvol(tail)

    def emit(self, it: dict, telegram: bool = True) -> None:
        self.signalled.add(it["symbol"])
        self.emitted.append(it["symbol"])


MINUTES = [f"{m // 60:02d}:{m % 60:02d}" for m in range(9 * 60 + 16, 15 * 60 + 32)]   # each 1-minute close
TIME_BUCKETS = [("09:35", "9:15–9:35"), ("10:00", "9:35–10:00"), ("10:30", "10:00–10:30"), ("12:00", "10:30–12:00"),
                ("99:99", "after 12:00")]
SHOWN = 300                                  # trades kept in a report for the page (the CSV has every trade)


def replay_day(day: str, bars: dict, history: dict, daily: dict, turnover: dict, stocks: list, cfgs: dict,
               cache: Optional[dict] = None) -> list:
    """One session through the flow at every candle close; positions managed every minute. Returns its positions.
    cache: the warm-up days' bars already resampled, {(token, interval): bars} (else resampled here)."""
    tr = cfgs["trigger"]
    view, book, signalled, cache = _View(bars), [], set(), dict(cache or {})
    items = [{"symbol": s["symbol"], "token": s["instrument_token"]} for s in stocks if s["instrument_token"] in bars]
    for t in MINUTES:
        view.t = t
        for p in book:
            if p["status"] in ("pending", "open"):
                pos.advance(p, bars.get(p["token"], []), t)
        mins = int(t[:2]) * 60 + int(t[3:]) - (9 * 60 + 15)
        if tr["start"] <= t <= tr["end"] and not mins % tr["interval"]:
            ctx = _Replay(day, tr["interval"], view, history, daily, turnover, None, "paper", t, book=book, signalled=signalled)
            ctx.cache = cache
            flow.run_once(ctx, items, cfgs=cfgs, live=False)
    for p in book:                                         # still open after the last bar: out at the last close
        if p["status"] == "open":
            last = bars.get(p["token"]) or [{"t": "15:30", "c": p["entry"]}]
            pos._exit(p, last[-1]["c"], last[-1]["t"], "end of day")
    return [p for p in book if p["status"] == "closed"]


# --- the report -----------------------------------------------------------------------------------------

def _agg(ts: list) -> dict:
    won = [t for t in ts if t["net"] > 0]
    loss = -sum(t["net"] for t in ts if t["net"] <= 0)
    return {"trades": len(ts), "won": len(won), "win_rate": round(100 * len(won) / len(ts), 1) if ts else None,
            "net": round(sum(t["net"] for t in ts), 2),
            "profit_factor": round(sum(t["net"] for t in won) / loss, 2) if loss else None,
            "avg_r": round(sum(t.get("r") or 0 for t in ts) / len(ts), 2) if ts else None}


def _curve_stats(nets: list, capital: float, n_days: int) -> dict:
    eq, peak, dd, curve = capital, capital, 0.0, []
    for x in nets:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100)
        curve.append(round(eq, 2))
    ret = eq / capital - 1
    cagr = ((1 + ret) ** (248 / max(n_days, 1)) - 1) * 100 if ret > -1 else -100.0
    return {"net": round(eq - capital, 2), "return": round(ret * 100, 2), "cagr": round(cagr, 2), "drawdown": round(dd, 2),
            "curve": curve}


def _charges(trades: list, plain_curve: dict) -> dict:
    """Plain profit (no slippage, no charges) -> slippage -> charges -> after costs, in total and per trade."""
    n = max(len(trades), 1)
    tot = {k: round(sum(t.get(k) or 0 for t in trades), 2) for k in ("plain", "slip", "brk", "gov", "costs", "net")}
    won_p = sum(t["plain"] > 0 for t in trades)
    loss_p = -sum(t["plain"] for t in trades if t["plain"] <= 0)
    return {**tot, "per_trade": {k: round(v / n, 2) for k, v in tot.items()},
            "win_rate_plain": round(100 * won_p / n, 1) if trades else None,
            "pf_plain": round(sum(t["plain"] for t in trades if t["plain"] > 0) / loss_p, 2) if loss_p else None,
            "cagr_plain": plain_curve["cagr"], "share": round(100 * (tot["slip"] + tot["costs"]) / tot["plain"], 1)
            if tot["plain"] > 0 else None}


def report(trades: list, days: list, capital: float, nifty: dict) -> dict:
    by_day, by_day_s, by_day_p = defaultdict(float), defaultdict(float), defaultdict(float)
    for t in trades:
        by_day[t["day"]] += t["net"]
        by_day_s[t["day"]] += t["net_stress"]
        by_day_p[t["day"]] += t["plain"]
    nets, nets_s = [by_day[d] for d in days], [by_day_s[d] for d in days]
    main, stress = _curve_stats(nets, capital, len(days)), _curve_stats(nets_s, capital, len(days))
    plain = _curve_stats([by_day_p[d] for d in days], capital, len(days))
    splits, n = [], len(days)
    for name, a, b in (("Development", 0, n // 2), ("Validation", n // 2, n * 3 // 4), ("Out-of-sample", n * 3 // 4, n)):
        base = capital + sum(nets[:a])
        base_s = capital + sum(nets_s[:a])
        splits.append({"name": name, "from": days[a] if a < n else None, "to": days[b - 1] if b else None,
                       "net": round(sum(nets[a:b]) / base * 100, 2), "stress": round(sum(nets_s[a:b]) / base_s * 100, 2)})
    def bucket(t):
        for end, name in TIME_BUCKETS:
            if t < end:
                return name
        return TIME_BUCKETS[-1][1]

    def group(key, order=None):
        keys = [k for k in order if any(key(t) == k for t in trades)] if order else sorted({key(t) for t in trades})
        return {k: _agg([t for t in trades if key(t) == k]) for k in keys}
    nf = [nifty.get(d) for d in days if nifty.get(d)]
    return {"from": days[0], "to": days[-1], "sessions": n, "capital": capital,
            **{k: main[k] for k in ("net", "return", "cagr", "drawdown")}, **_agg(trades),
            "gross": round(sum(t["gross"] for t in trades), 2), "costs": round(sum(t["costs"] for t in trades), 2),
            "stress": {k: stress[k] for k in ("net", "return", "cagr", "drawdown")},
            "nifty": round((nf[-1] / nf[0] - 1) * 100, 2) if len(nf) > 1 else None,
            "charges": _charges(trades, plain),
            "curve": {"dates": days, "net": main["curve"], "stress": stress["curve"], "plain": plain["curve"],
                      "nifty": [round(nifty[d] / nf[0] * capital, 2) if nifty.get(d) and nf else None for d in days]},
            "splits": splits, "ok_periods": all(s["net"] > 0 and s["stress"] > 0 for s in splits),
            "by_side": group(lambda t: t["side"]), "by_time": group(lambda t: bucket(t["entry_t"]), [n for _, n in TIME_BUCKETS]),
            "by_month": group(lambda t: t["day"][:7]), "by_setup": group(lambda t: t.get("setup") or "—"),
            "exits": dict(Counter(t["reason"] for t in trades).most_common()),
            "rows": [{k: v for k, v in t.items() if k != "orders"}
                     for t in sorted(trades, key=lambda t: (t["day"], t["entry_t"]), reverse=True)[:SHOWN]]}


def _trade(p: dict) -> dict:
    sold = sum(e["price"] * e["qty"] for e in p["exits"])
    value = p["entry"] * p["qty"] + sold
    extra = STRESS_EXTRA_PCT / 100 * value
    slip = pos.SLIPPAGE_PCT / 100 * value                 # the slippage already inside the fills
    long = p["side"] == "long"
    return {"day": p["day"], "symbol": p["symbol"], "side": p["side"], "setup": p.get("setup"), "qty": p["qty"],
            "entry_t": p["entry_t"], "entry": p["entry"], "exit_t": p["exit_t"],
            "exit": round(sold / max(p["qty"], 1), 2), "reason": p["reason"], "gross": p["gross"], "costs": p["costs"],
            "net": p["net"], "net_stress": round(p["net"] - extra, 2), "r": p.get("r"),
            "slip": round(slip, 2), "plain": round(p["gross"] + slip, 2), "risk": p.get("risk"),
            "signal_t": p.get("signal_t"), "signal_price": p.get("signal_price"), "sl": p.get("stop0"),
            "target": p.get("target"), "exits": [{k: e.get(k) for k in ("t", "qty", "price", "why")} for e in p["exits"]],
            "orders": [[round(p["entry"] * p["qty"], 2), long]] + [[round(e["price"] * e["qty"], 2), not long] for e in p["exits"]]}


# --- the cost model: what the report counts (slippage, brokerage % / ₹ per order, government charges) ------

DEFAULT_COSTS = {"slip": True, "brk_pct": 0.03, "brk_rs": 20.0, "gov": True}     # default: the lower of 0.03% and ₹20 an order


def brokerage(value: float, m: dict) -> float:
    """One order: % and ₹ both set -> the lower ; only one set -> that one; none -> free."""
    pct, rs = (m.get("brk_pct") or 0) / 100 * value, m.get("brk_rs") or 0
    return min(pct, rs) if pct and rs else pct or rs


def order_costs(orders: list, m: dict) -> tuple[float, float]:
    """(brokerage + its GST, government charges: STT on the sell, exchange, SEBI, GST on them, stamp on the buy)."""
    brk = sum(brokerage(v, m) for v, _ in orders)
    total = sum(v for v, _ in orders)
    exch, sebi = total * 0.0000297, total * 10 / 1e7
    stt = sum(v for v, buy in orders if not buy) * 0.00025
    stamp = sum(v for v, buy in orders if buy) * 0.00003
    return brk * 1.18, stt + exch + sebi + 0.18 * (exch + sebi) + stamp


def apply_costs(trades: list, m: dict, run_capital: Optional[float] = None) -> list:
    """The same trades under another cost model and capital. Fills and exits do not change, only each trade's
    profit; another capital scales every quantity (sizes are % of capital: risk % and max position %), whole
    shares, so it matches a run at that capital to within a share's rounding. A trade under 1 share is left out."""
    out = []
    f = (m.get("capital") or run_capital) / run_capital if run_capital and m.get("capital") else 1.0
    for t in trades:
        if "orders" not in t:
            out.append(t)
            continue
        if f != 1.0:
            q = int(t["qty"] * f)
            if q < 1:
                continue
            k = q / t["qty"]
            t = {**t, "qty": q, "plain": t["plain"] * k, "slip": t["slip"] * k, "gross": t["gross"] * k,
                 "risk": (t.get("risk") or 0) * k or None, "orders": [[v * k, b] for v, b in t["orders"]],
                 "exits": [{**e, "qty": round(e["qty"] * k)} for e in t.get("exits", [])]}
        brk, gov = order_costs(t["orders"], m)
        costs = brk + (gov if m.get("gov", True) else 0)
        slip = t["slip"] if m.get("slip", True) else 0.0
        extra = STRESS_EXTRA_PCT / 100 * sum(v for v, _ in t["orders"])
        net = t["plain"] - slip - costs
        out.append({**t, "plain": round(t["plain"], 2), "gross": round(t["gross"], 2), "costs": round(costs, 2), "brk": round(brk, 2), "gov": round(gov if m.get("gov", True) else 0, 2),
                    "slip": round(slip, 2), "net": round(net, 2), "net_stress": round(t["plain"] - t["slip"] - extra - costs, 2),
                    "r": round(net / t["risk"], 2) if t.get("risk") else t.get("r")})
    return out


# --- a run ----------------------------------------------------------------------------------------------

def _universe(config: AppConfig, period: str, stocks: Optional[list] = None):
    from backend.ingest import universe as uni
    warm, days, nifty = sessions(PERIODS[period], config.flow.warmup_days)
    if not days:
        raise RuntimeError("No NIFTY 50 daily candles saved yet (run the daily sync).")
    stocks = stocks if stocks is not None else uni.resolve_universe(config.ingest)["stocks"]
    return warm, days, nifty, stocks, [s["instrument_token"] for s in stocks] + [flow.NIFTY_TOKEN, flow.VIX_TOKEN]


def _replay(config: AppConfig, names: list[str], period: str, got: dict, stocks: list) -> dict:
    """The replay of every session and the saved results (in the replay process when started from the tab)."""
    warm, days, nifty, stocks, tokens = _universe(config, period, stocks)
    daily = _Daily(stocks, (date.fromisoformat(days[0]) - timedelta(days=330)).isoformat())
    cfgs = {name: configs_of(name) for name in names}
    trades = {name: [] for name in names}
    ivs = {c["trigger"]["interval"] for c in cfgs.values()}
    rs = lambda d: {(tok, iv): resample(b, iv) for tok, b in d.items() for iv in ivs}   # a day resampled once
    prev: list[dict] = [_day_bars(d) for d in warm]
    prev_rs = [rs(d) for d in prev]
    keep = config.flow.warmup_days
    covered = 0
    for k, day in enumerate(days):
        _set(pct=40 + round(60 * k / len(days)), message=f"replaying {day} ({k + 1} of {len(days)} sessions)")
        bars = _day_bars(day)
        if len([s for s in stocks if s["instrument_token"] in bars]) >= len(stocks) // 2:   # else: left out
            covered += 1
            history = {tok: [b for d in prev for b in d.get(tok, [])] for tok in tokens}
            cache = {(tok, iv): [b for d in prev_rs for b in d.get((tok, iv), [])] for tok in tokens for iv in ivs}
            dctx, turn = daily.at(day)
            for name in names:
                trades[name] += [_trade(p) for p in replay_day(day, bars, history, dctx, turn, stocks, cfgs[name], cache)]
        prev, prev_rs = (prev + [bars])[-keep:] if keep else [], (prev_rs + [rs(bars)])[-keep:] if keep else []
    out = {}
    with get_db() as conn:
        for name in names:
            rep = report(apply_costs(trades[name], DEFAULT_COSTS), days, float(cfgs[name]["size"]["capital"]), nifty)
            rep.update(setup=name, period=period, covered=covered, missing=got["missing"], fetched=got["fetched"],
                       settings=cfgs[name], ran_at=now_ist(), raw=trades[name],
                       nifty_px={d: nifty[d] for d in days if d in nifty})
            conn.execute("DELETE FROM flow_backtests WHERE setup = ? AND period = ?", (name, period))
            conn.execute("INSERT INTO flow_backtests (setup, period, created, payload) VALUES (?, ?, ?, ?)",
                         (name, period, rep["ran_at"], json.dumps(rep)))
            out[name] = {"trades": rep["trades"], "net": rep["net"]}
    msg = " · ".join(f"{n}: {x['trades']} trades, net {x['net']:+,.0f}" for n, x in out.items())
    logger.info("Flow backtest %s (%d of %d sessions with data): %s", period, covered, len(days), msg)
    _set(state="idle", pct=100, message=f"done · {msg}", at=now_ist())
    return {"status": "ok", "covered": covered, **out}


def _child(config: AppConfig, names: list[str], period: str, got: dict, stocks: list) -> None:
    """The replay process: lower priority, so the app and the live flow always come first."""
    try:
        os.nice(10)
    except OSError:
        pass
    with get_db() as conn:
        _status.update(json.loads(get_setting(conn, "flow.bt_status") or "{}"))
    try:
        _replay(config, names, period, got, stocks)
    except Exception as exc:
        logger.exception("Flow backtest replay failed")
        _set(state="idle", message=f"failed: {exc}")


def run(config: AppConfig, client, names: list[str], period: str, own_process: bool = False) -> dict:
    """Each setup in `names` over the period; saved (the latest per setup and period) for the tab. own_process:
    the replay runs in a separate process (from the tab), which ⏹ can stop at once."""
    global _proc
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        _set(state="running", pct=0, message="preparing", at=now_ist(), period=period, setups=names)

        def progress(k, n):
            if _cancel.is_set():
                raise Stopped()
            _set(pct=round(40 * k / n), message=f"1-minute history from Kite: {k} of {n} stocks (only missing days)")
        warm, days, _, stocks, tokens = _universe(config, period)
        got = fetch_missing(client, tokens, warm + days, progress)
        if _cancel.is_set():
            raise Stopped()
        if not own_process:
            return _replay(config, names, period, got, stocks)
        _proc = mp.get_context("spawn").Process(target=_child, args=(config, names, period, got, stocks), name="flow-replay", daemon=True)
        _proc.start()
        _proc.join()
        if _proc.exitcode == 0:                          # finished (and saved), even if ⏹ came just after
            return {"status": "ok"}
        if _cancel.is_set():
            raise Stopped()
        if _proc.exitcode:
            raise RuntimeError(f"the replay process ended with code {_proc.exitcode}")
        return {"status": "ok"}
    except Stopped:
        _set(state="idle", pct=0, message=f"⏹ stopped by you at {now_ist()[11:16]} · nothing saved", at=now_ist())
        return {"status": "stopped"}
    except Exception as exc:
        logger.exception("Flow backtest failed")
        _set(state="idle", message=f"failed: {exc}")
        return {"status": "failed", "error": str(exc)}
    finally:
        _cancel.clear()
        _lock.release()


def start(config: AppConfig, client, names: list[str], period: str) -> dict:
    global _job
    if _lock.locked() or (_job and _job.is_alive()):
        return {"status": "busy"}
    known = set(all_setups()) | {CANVAS}
    names = [n for n in names if n in known][:6]
    if not names or period not in PERIODS:
        return {"status": "bad_request"}
    _job = threading.Thread(target=run, args=(config, client, names, period, True), name="flow-backtest", daemon=True)
    _job.start()
    return {"status": "started", "setups": names, "period": period}


CSV_COLS = ["date", "stock", "side", "setup", "signal_time", "signal_price", "entry_time", "entry_price", "qty",
            "entry_value", "stop", "target", "risk_rs", "partial_time", "partial_qty", "partial_price", "partial_type",
            "remaining_qty", "exit_time", "exit_qty", "exit_price", "exit_type", "avg_exit_price", "exit_reason",
            "minutes_held", "plain_profit", "slippage", "brokerage_with_gst", "govt_charges", "total_costs", "net_profit",
            "r_multiple", "result"]


def _r2(v):
    return round(v, 2) if isinstance(v, (int, float)) else ""


def csv_rows(setup: str, period: str, costs: Optional[dict] = None) -> tuple[list, dict]:
    """Every trade of a saved result (not only the latest shown on the page), one row each, under the cost model:
    the partial exit(s), what stayed open after them, and the final exit, with each cost apart."""
    with get_db() as conn:
        r = conn.execute("SELECT payload FROM flow_backtests WHERE setup = ? AND period = ?", (setup, period)).fetchone()
    if not r:
        return [], {}
    rep = json.loads(r[0])
    m = {**DEFAULT_COSTS, **(costs or {})}
    trades = apply_costs(rep.get("raw") or [], m, rep["capital"])
    mins = lambda t: int(t[:2]) * 60 + int(t[3:5])
    rows = []
    for t in sorted(trades, key=lambda t: (t["day"], t["entry_t"])):
        ex = t.get("exits") or []
        parts, last = ex[:-1], (ex[-1] if ex else {})
        sold = sum(e["qty"] for e in parts)
        rows.append([t["day"], t["symbol"], t["side"], t.get("setup") or "", t.get("signal_t") or "", _r2(t.get("signal_price")),
                     t["entry_t"], t["entry"], t["qty"], round(t["entry"] * t["qty"], 2), _r2(t.get("sl")), _r2(t.get("target")),
                     round(t["risk"], 2) if t.get("risk") else "",
                     " | ".join(e["t"] for e in parts), " | ".join(str(e["qty"]) for e in parts),
                     " | ".join(str(e["price"]) for e in parts), " | ".join(e.get("why") or "" for e in parts),
                     t["qty"] - sold, last.get("t", t["exit_t"]), last.get("qty", t["qty"] - sold), last.get("price", t["exit"]),
                     last.get("why", t["reason"]), t["exit"], t["reason"], mins(t["exit_t"]) - mins(t["entry_t"]),
                     t["plain"], t["slip"], t.get("brk", 0), t.get("gov", 0), round(t["slip"] + t["costs"], 2), t["net"],
                     t.get("r") if t.get("r") is not None else "", "win" if t["net"] > 0 else "loss"])
    return rows, {"capital": m.get("capital") or rep["capital"], **{k: m[k] for k in ("slip", "brk_pct", "brk_rs", "gov")}}


KEEP = ("setup", "period", "covered", "missing", "fetched", "settings", "ran_at")


def saved() -> dict:
    """{setup: [period, ...]} of the saved results, and the period of the latest run (cheap: no payloads read)."""
    with get_db() as conn:
        rows = conn.execute("SELECT setup, period, created FROM flow_backtests ORDER BY created DESC").fetchall()
    out: dict[str, list] = defaultdict(list)
    for s, p, _ in rows:
        out[s].append(p)
    return {"periods": dict(out), "latest": rows[0][1] if rows else None}


def results(period: Optional[str] = None, costs: Optional[dict] = None) -> list:
    """Saved results; with `costs`, each report is rebuilt from its saved trades under that cost model."""
    q, args = "SELECT payload FROM flow_backtests", []
    if period:
        q, args = q + " WHERE period = ?", [period]
    with get_db() as conn:
        rows = [json.loads(r[0]) for r in conn.execute(q + " ORDER BY created DESC", args)]
    out = []
    for r in rows:
        raw = r.pop("raw", None)
        px = r.pop("nifty_px", None)
        if costs is not None and raw is not None:
            cap = costs.get("capital") or r["capital"]
            r = {**report(apply_costs(raw, costs, r["capital"]), r["curve"]["dates"], cap, px or {}),
                 **{k: r.get(k) for k in KEEP}, "run_capital": r["capital"]}
        r["costs_model"] = costs or DEFAULT_COSTS
        r["split_costs"] = raw is not None
        out.append(r)
    return out
