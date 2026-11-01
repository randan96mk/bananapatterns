"""🧪 Intraday Flow backtest (phase 3): replays saved 1-minute candles day by day through the same boxes and the same
paper-position code as live (next 1-minute open + slippage, Zerodha intraday charges, stops / trails / square-off),
for the canvas or a saved setup, over 6 months / 1 year / 2 years. Only data up to each minute is used: daily
averages and turnover from the days before, warm-up from the saved sessions before, bars closed before the minute.
Missing stock-days are fetched from Kite once (then saved). Never orders, never touches the live paper positions."""

import json
import threading
import time as _time
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Optional

from backend.config import AppConfig
from backend.database.models import get_candles, get_db, get_setting, now_ist, set_setting
from backend.intraday import flow, store
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


def status() -> dict:
    return dict(_status)


# --- saved setups: a named copy of every box's settings -------------------------------------------------

def setups() -> dict:
    with get_db() as conn:
        return json.loads(get_setting(conn, "flow.setups") or "{}")


def _put(all_: dict) -> None:
    with get_db() as conn:
        set_setting(conn, "flow.setups", json.dumps(all_))


def save_setup(name: str) -> dict:
    name = name.strip()[:60]
    if not name or name == CANVAS:
        raise ValueError("a name is needed")
    all_ = setups()
    all_[name] = {"nodes": flow.node_configs(), "saved_at": now_ist()}
    _put(all_)
    return all_[name]


def delete_setup(name: str) -> None:
    all_ = setups()
    all_.pop(name, None)
    _put(all_)
    with get_db() as conn:
        conn.execute("DELETE FROM flow_backtests WHERE setup = ?", (name,))


def load_setup(name: str) -> None:
    """Puts a saved setup's settings on the canvas (the live flow uses them from the next candle)."""
    nodes = setups()[name]["nodes"]
    with get_db() as conn:
        set_setting(conn, "flow.nodes", json.dumps(nodes))


def configs_of(name: str) -> dict:
    if name == CANVAS:
        return flow.node_configs()
    saved = setups()[name]["nodes"]
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
                c, t = [0.0], [0.0]
                for r in rows:
                    c.append(c[-1] + r["close"])
                    t.append(t[-1] + r["close"] * r["volume"])
                self.s[st["symbol"]] = ([r["date"] for r in rows], c, t)

    def at(self, day: str) -> tuple[dict, dict]:
        daily, turn = {}, {}
        for sym, (dates, c, t) in self.s.items():
            i = bisect_left(dates, day)                    # rows before the day
            daily[sym] = {"sma50": (c[i] - c[i - 50]) / 50 if i >= 50 else None,
                          "sma200": (c[i] - c[i - 200]) / 200 if i >= 200 else None}
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

    def open_paper(self, it: dict) -> None:
        p = pos.open_new(self.day, it, it["signal_bar"]["t"], it["plan"], save=False)
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

    def bars(self, it):
        k = (it["token"], self.interval)
        if k not in self.cache:
            self.cache[k] = resample(self.history.get(it["token"], []), self.interval)
        return self.cache[k] + self._today(it)

    def last_closed(self, it):
        b = self._today(it)
        return b[-1] if b else None

    def emit(self, it: dict, telegram: bool = True) -> None:
        self.signalled.add(it["symbol"])
        self.emitted.append(it["symbol"])


MINUTES = [f"{m // 60:02d}:{m % 60:02d}" for m in range(9 * 60 + 16, 15 * 60 + 32)]   # each 1-minute close


def replay_day(day: str, bars: dict, history: dict, daily: dict, turnover: dict, stocks: list, cfgs: dict) -> list:
    """One session through the flow at every candle close; positions managed every minute. Returns its positions."""
    tr = cfgs["trigger"]
    view, book, signalled, cache = _View(bars), [], set(), {}
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


def report(trades: list, days: list, capital: float, nifty: dict) -> dict:
    by_day = defaultdict(float)
    by_day_s = defaultdict(float)
    for t in trades:
        by_day[t["day"]] += t["net"]
        by_day_s[t["day"]] += t["net_stress"]
    nets, nets_s = [by_day[d] for d in days], [by_day_s[d] for d in days]
    main, stress = _curve_stats(nets, capital, len(days)), _curve_stats(nets_s, capital, len(days))
    splits, n = [], len(days)
    for name, a, b in (("Development", 0, n // 2), ("Validation", n // 2, n * 3 // 4), ("Out-of-sample", n * 3 // 4, n)):
        base = capital + sum(nets[:a])
        base_s = capital + sum(nets_s[:a])
        splits.append({"name": name, "from": days[a] if a < n else None, "to": days[b - 1] if b else None,
                       "net": round(sum(nets[a:b]) / base * 100, 2), "stress": round(sum(nets_s[a:b]) / base_s * 100, 2)})
    bucket = lambda t: "9:15–10:30" if t < "10:30" else "10:30–12:00" if t < "12:00" else "after 12:00"
    group = lambda key: {k: _agg([t for t in trades if key(t) == k]) for k in sorted({key(t) for t in trades})}
    nf = [nifty.get(d) for d in days if nifty.get(d)]
    return {"from": days[0], "to": days[-1], "sessions": n, "capital": capital,
            **{k: main[k] for k in ("net", "return", "cagr", "drawdown")}, **_agg(trades),
            "gross": round(sum(t["gross"] for t in trades), 2), "costs": round(sum(t["costs"] for t in trades), 2),
            "stress": {k: stress[k] for k in ("net", "return", "cagr", "drawdown")},
            "nifty": round((nf[-1] / nf[0] - 1) * 100, 2) if len(nf) > 1 else None,
            "curve": {"dates": days, "net": main["curve"], "stress": stress["curve"],
                      "nifty": [round(nifty[d] / nf[0] * capital, 2) if nifty.get(d) and nf else None for d in days]},
            "splits": splits, "ok_periods": all(s["net"] > 0 and s["stress"] > 0 for s in splits),
            "by_side": group(lambda t: t["side"]), "by_time": group(lambda t: bucket(t["entry_t"])),
            "by_month": group(lambda t: t["day"][:7]), "by_setup": group(lambda t: t.get("setup") or "—"),
            "exits": dict(Counter(t["reason"] for t in trades).most_common()),
            "rows": sorted(trades, key=lambda t: (t["day"], t["entry_t"]), reverse=True)[:1500]}


def _trade(p: dict) -> dict:
    sold = sum(e["price"] * e["qty"] for e in p["exits"])
    extra = STRESS_EXTRA_PCT / 100 * (p["entry"] * p["qty"] + sold)
    return {"day": p["day"], "symbol": p["symbol"], "side": p["side"], "setup": p.get("setup"), "qty": p["qty"],
            "entry_t": p["entry_t"], "entry": p["entry"], "exit_t": p["exit_t"],
            "exit": round(sold / max(p["qty"], 1), 2), "reason": p["reason"], "gross": p["gross"], "costs": p["costs"],
            "net": p["net"], "net_stress": round(p["net"] - extra, 2), "r": p.get("r")}


# --- a run ----------------------------------------------------------------------------------------------

def run(config: AppConfig, client, names: list[str], period: str) -> dict:
    """Each setup in `names` over the period; saved (the latest per setup and period) for the tab."""
    from backend.ingest import universe as uni
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        months = PERIODS[period]
        _status.update(state="running", pct=0, message="preparing", at=now_ist())
        warm, days, nifty = sessions(months, config.flow.warmup_days)
        if not days:
            raise RuntimeError("No NIFTY 50 daily candles saved yet (run the daily sync).")
        stocks = uni.resolve_universe(config.ingest)["stocks"]
        tokens = [s["instrument_token"] for s in stocks] + [flow.NIFTY_TOKEN, flow.VIX_TOKEN]
        got = fetch_missing(client, tokens, warm + days, lambda k, n: _status.update(
            pct=round(40 * k / n), message=f"1-minute history from Kite: {k} of {n} stocks (only missing days)"))
        daily = _Daily(stocks, (date.fromisoformat(days[0]) - timedelta(days=330)).isoformat())
        cfgs = {name: configs_of(name) for name in names}
        trades = {name: [] for name in names}
        prev: list[dict] = [_day_bars(d) for d in warm]
        covered = 0
        for k, day in enumerate(days):
            _status.update(pct=40 + round(60 * k / len(days)), message=f"replaying {day} ({k + 1} of {len(days)} sessions)")
            bars = _day_bars(day)
            if len([s for s in stocks if s["instrument_token"] in bars]) < len(stocks) // 2:
                prev = (prev + [bars])[-config.flow.warmup_days:] if config.flow.warmup_days else []
                continue                                    # under half the stocks saved: the day is left out
            covered += 1
            history = {tok: [b for d in prev for b in d.get(tok, [])] for tok in tokens}
            dctx, turn = daily.at(day)
            for name in names:
                trades[name] += [_trade(p) for p in replay_day(day, bars, history, dctx, turn, stocks, cfgs[name])]
            prev = (prev + [bars])[-config.flow.warmup_days:] if config.flow.warmup_days else []
        out = {}
        with get_db() as conn:
            for name in names:
                rep = report(trades[name], days, float(cfgs[name]["size"]["capital"]), nifty)
                rep.update(setup=name, period=period, covered=covered, missing=got["missing"], fetched=got["fetched"],
                           settings=cfgs[name], ran_at=now_ist())
                conn.execute("DELETE FROM flow_backtests WHERE setup = ? AND period = ?", (name, period))
                conn.execute("INSERT INTO flow_backtests (setup, period, created, payload) VALUES (?, ?, ?, ?)",
                             (name, period, rep["ran_at"], json.dumps(rep)))
                out[name] = {"trades": rep["trades"], "net": rep["net"]}
        msg = " · ".join(f"{n}: {x['trades']} trades, net {x['net']:+,.0f}" for n, x in out.items())
        logger.info("Flow backtest %s (%d of %d sessions with data): %s", period, covered, len(days), msg)
        _status.update(state="idle", pct=100, message=f"done · {msg}", at=now_ist())
        return {"status": "ok", "covered": covered, **out}
    except Exception as exc:
        logger.exception("Flow backtest failed")
        _status.update(state="idle", message=f"failed: {exc}")
        return {"status": "failed", "error": str(exc)}
    finally:
        _lock.release()


def start(config: AppConfig, client, names: list[str], period: str) -> dict:
    if _lock.locked():
        return {"status": "busy"}
    known = set(setups()) | {CANVAS}
    names = [n for n in names if n in known][:6]
    if not names or period not in PERIODS:
        return {"status": "bad_request"}
    threading.Thread(target=run, args=(config, client, names, period), name="flow-backtest", daemon=True).start()
    return {"status": "started", "setups": names, "period": period}


def results(period: Optional[str] = None) -> list:
    q, args = "SELECT payload FROM flow_backtests", []
    if period:
        q, args = q + " WHERE period = ?", [period]
    with get_db() as conn:
        return [json.loads(r[0]) for r in conn.execute(q + " ORDER BY created DESC", args)]
