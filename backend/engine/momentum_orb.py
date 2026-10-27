"""Momentum ORB: an evening momentum screen on the Nifty 500, then an intraday breakout of the next session's first
15 minutes. Backtested over history_days from 5-minute candles (fetched from Kite only for the days needed);
each evening the new screen becomes tomorrow's list. Read-only: Kite history only, never orders.

Screen     on the day's candle (only that day and earlier): close > min_price, RSI(rsi_len) > rsi_min (RSI outside
           40-60, buy side), the day's volume > min_volume, close > EMA(ema_len), and a new 52-week high (the day's
           high is the highest of the last high_bars sessions).
Next day   opening range = the 5-minute candles from 9:15 AM for or_minutes: high H, low L, length H - L.
           buy     the first 5-minute candle from the end of the opening range, ending by entry_end, that CLOSES
                   over H; entry = that close.
           stop    L.
           target  entry + target_mult x (H - L): sell book_frac there; the stop moves to the entry.
           the rest  out on a 5-minute close under the highest high since the entry - atr_mult x ATR(atr_len) of
                   5-minute candles, or at the entry (hard stop).
           a candle touching both the stop and the target counts as the stop (the worse case).
           intraday only: whatever is left is sold at the close of the candle ending at exit_time.
Results    gross % and after costs (cost_pct round trip at intraday rates, slippage_pct on every fill); a trade is
           won / lost after costs. R = gross result / the risk to the stop.
"""

import json
import threading
from collections import Counter, deque
from datetime import date, datetime, time, timedelta
from typing import Optional

import numpy as np

from backend.config import AppConfig
from backend.database.models import get_candles, get_db, get_setting, now_ist, set_setting
from backend.engine import backtest as bt
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm

logger = setup_logger("momentum_orb")

FINAL = ("won", "lost", "no_entry")                   # these rows never change
PERIODS = {"1m": 31, "3m": 92, "6m": 183, "1y": 366, "2y": 731}
YEARS = ("y1", "y2")                                  # y1: the older year of the last two, y2: the last year
OR_BUCKETS = ((0.0, 1.0, "under 1%"), (1.0, 2.0, "1-2%"), (2.0, 1e9, "over 2%"))   # opening range, % of its high

_status = {"state": "idle", "done": 0, "total": 0, "message": "", "at": None}
_lock = threading.Lock()


def status() -> dict:
    return dict(_status)


def _r(x, nd=2):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def _plus(hhmm: str, minutes: int) -> str:
    t = datetime.combine(date.today(), time.fromisoformat(hhmm)) + timedelta(minutes=minutes)
    return t.strftime("%H:%M")


# --- the evening screen -------------------------------------------------------------------------------

def rsi(close: np.ndarray, n: int) -> np.ndarray:
    """Wilder's RSI; NaN until n + 1 closes exist."""
    out = np.full(len(close), np.nan)
    if len(close) <= n:
        return out
    d = np.diff(close)
    up, down = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = up[:n].mean(), down[:n].mean()
    for i in range(n, len(close)):
        if i > n:
            au = (au * (n - 1) + up[i - 1]) / n
            ad = (ad * (n - 1) + down[i - 1]) / n
        out[i] = 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    """Exponential average (alpha 2 / (n + 1)), seeded with the first n closes' mean; NaN before that."""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    a, v = 2 / (n + 1), float(np.mean(x[:n]))
    out[n - 1] = v
    for i in range(n, len(x)):
        v = a * x[i] + (1 - a) * v
        out[i] = v
    return out


def screen(candles: list[dict], cfg) -> list[dict]:
    """Every day the stock passed the screen: [{t, date, close, rsi, volume, ema}] (each uses that day and earlier)."""
    n = len(candles)
    if n < max(cfg.high_bars, cfg.ema_len, cfg.rsi_len + 1):
        return []
    c = np.array([x["close"] for x in candles], dtype=float)
    h = np.array([x["high"] for x in candles], dtype=float)
    v = np.array([x["volume"] for x in candles], dtype=float)
    r, e = rsi(c, cfg.rsi_len), ema(c, cfg.ema_len)
    top = np.full(n, np.nan)
    top[cfg.high_bars - 1:] = np.lib.stride_tricks.sliding_window_view(h, cfg.high_bars).max(axis=1)
    ok = (c > cfg.min_price) & (r > cfg.rsi_min) & (v > cfg.min_volume) & (c > e) & (h >= top)
    return [{"t": int(t), "date": candles[t]["date"], "close": _r(c[t]), "rsi": _r(r[t], 1), "volume": int(v[t]),
             "ema": _r(e[t])} for t in np.flatnonzero(np.nan_to_num(ok, nan=0).astype(bool))]


# --- the next session ---------------------------------------------------------------------------------

def net_pct(entry: float, sales: list[tuple], cfg) -> float:
    """Result after costs in %: every fill slips slippage_pct against you; each part sold pays the round trip."""
    s, cost = cfg.slippage_pct / 100, cfg.cost_pct / 100
    buy = entry * (1 + s)
    return round(sum(f * (px * (1 - s) / buy - 1 - cost) for f, px in sales) * 100, 2)


def trade(bars: list[dict], day: str, cfg) -> dict:
    """The intraday trade from one session's 5-minute candles (see the module docstring)."""
    if not bars:
        return {"status": "no_entry", "reason": "no 5-minute candles for the day"}
    or_end = _plus("09:15", cfg.or_minutes)
    opening = [x for x in bars if x["t"] < or_end]
    if not opening:
        return {"status": "no_entry", "reason": "no opening-range candles"}
    hi, lo = max(x["high"] for x in opening), min(x["low"] for x in opening)
    base = {"or_high": _r(hi), "or_low": _r(lo)}
    if hi <= lo:
        return {**base, "status": "no_entry", "reason": "flat opening range"}
    k = next((i for i, x in enumerate(bars) if x["t"] >= or_end and _plus(x["t"], 5) <= cfg.entry_end
              and x["close"] > hi), None)
    if k is None:
        return {**base, "status": "no_entry",
                "reason": f"no 5-minute close over the 15-minute high by {ampm(cfg.entry_end)}"}
    entry, t_in = bars[k]["close"], _plus(bars[k]["t"], 5)
    stop, target = lo, entry + cfg.target_mult * (hi - lo)
    events = [{"kind": "entry", "date": day, "time": t_in, "price": _r(entry)}]
    sales, left, booked, hh, trail, why = [], 1.0, False, entry, None, None
    trs: deque = deque(maxlen=cfg.atr_len)
    prev = None
    for x in bars[:k + 1]:                                    # the ATR starts from the day's candles so far
        if prev is not None:
            trs.append(max(x["high"] - x["low"], abs(x["high"] - prev), abs(x["low"] - prev)))
        prev = x["close"]

    def sell(px, t, f, kind, reason=None):
        nonlocal left
        sales.append((f, px))
        left -= f
        e = {"kind": kind, "date": day, "time": t, "price": _r(px)}
        if kind == "half":
            e["frac"] = f
        else:
            e["why"] = reason
        events.append(e)

    for x in bars[k + 1:]:
        o, h, lw, c, t = x["open"], x["high"], x["low"], x["close"], _plus(x["t"], 5)
        if lw <= stop:
            why = "breakeven stop" if booked else "stop"
            sell(o if o < stop else stop, t, left, "exit", why)
            break
        if booked and trail is not None and c < trail:
            why = "ATR trail"
            sell(c, t, left, "exit", why)
            break
        if not booked and h >= target:
            sell(max(o, target), t, cfg.book_frac, "half")
            booked, stop = True, max(stop, entry)
        hh = max(hh, h)
        trs.append(max(h - lw, abs(h - prev), abs(lw - prev)))
        prev = c
        if booked:
            trail = max(trail or 0.0, hh - cfg.atr_mult * sum(trs) / len(trs))
        if t >= cfg.exit_time:
            why = f"{ampm(cfg.exit_time)} square-off"
            sell(c, t, left, "exit", why)
            break
    if left > 1e-9:                                           # the data ended early (e.g. a short session)
        why = "last candle of the day"
        sell(bars[-1]["close"], _plus(bars[-1]["t"], 5), left, "exit", why)
    gross = sum(f * (px / entry - 1) for f, px in sales) * 100
    net = net_pct(entry, sales, cfg)
    risk = (entry - lo) / entry * 100
    return {**base, "status": "won" if net > 0 else "lost", "reason": why, "entry": _r(entry), "entry_time": t_in,
            "stop": _r(lo), "target": _r(target), "result_pct": _r(gross), "net_pct": net,
            "r": _r(gross / risk) if risk > 0 else None, "booked": booked, "events": events}


# --- build and the daily update -----------------------------------------------------------------------

UPSERT = ("INSERT OR REPLACE INTO orb_trades (symbol, signal_date, trade_date, status, reason, close, rsi, volume, ema, "
          "or_high, or_low, entry, entry_time, stop, target, result_pct, net_pct, r, booked, events, updated_at) "
          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")


def _values(sym: str, sig: dict, trade_date: Optional[str], tr: dict) -> tuple:
    return (sym, sig["date"], trade_date, tr["status"], tr.get("reason"), sig["close"], sig["rsi"], sig["volume"],
            sig["ema"], tr.get("or_high"), tr.get("or_low"), tr.get("entry"), tr.get("entry_time"), tr.get("stop"),
            tr.get("target"), tr.get("result_pct"), tr.get("net_pct"), tr.get("r"), int(bool(tr.get("booked"))),
            json.dumps(tr.get("events") or []), now_ist())


def build(config: AppConfig, client, rebuild: bool = False) -> dict:
    """First build (or a rebuild): every screen day in the last history_days and its next-session trade, fetching
    the 5-minute candles needed (a few sessions per request; stored candles are re-used). Otherwise the daily
    update: new screen days, and pending rows re-run. The last day's screen is tomorrow's list (pending)."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        from backend.ingest import universe as uni
        cfg = config.orb
        with get_db() as conn:
            built = get_setting(conn, "orb.built_at")
            if rebuild:
                conn.execute("DELETE FROM orb_trades")
                built = None
            stored = {(r["symbol"], r["signal_date"]): r["status"] for r in conn.execute(
                "SELECT symbol, signal_date, status FROM orb_trades")}
        _status.update(state="building" if not built else "updating", done=0, total=0, message="", at=now_ist())
        stocks = uni.resolve_universe(config.ingest)["stocks"]
        _status["total"] = len(stocks)
        latest, rows, added, pending, calls = None, [], 0, 0, 0
        with get_db() as conn:
            series = {s["symbol"]: (s["instrument_token"], get_candles(conn, s["instrument_token"])) for s in stocks}
        dates = [cs[-1]["date"] for _, cs in series.values() if cs]
        if not dates:
            _status.update(state="idle", message="no candles yet")
            return {"status": "no_data"}
        latest = max(dates)
        since = (date.fromisoformat(latest) - timedelta(days=cfg.history_days)).isoformat()
        for i, sym in enumerate(sorted(series)):
            token, candles = series[sym]
            sessions = [x["date"] for x in candles]
            five = bt.FiveMin(client, token, sym, sessions)
            for sig in screen(candles, cfg):
                if sig["date"] < since or stored.get((sym, sig["date"])) in FINAL:
                    continue
                added += (sym, sig["date"]) not in stored
                t = sig["t"]
                if t + 1 >= len(candles):
                    tr, d = {"status": "pending", "reason": "on the list for the next session"}, None
                else:
                    d = sessions[t + 1]
                    bars = five.day(d, ahead=5)
                    tr = trade(bars, d, cfg) if bars is not None else \
                        {"status": "pending", "reason": "5-minute candles not fetched yet (needs a Kite login)"}
                pending += tr["status"] == "pending"
                rows.append(_values(sym, sig, d, tr))
            calls += five.fetched
            _status.update(done=i + 1, message=f"{sym}: {added} screen days so far, {calls} Kite requests")
            if len(rows) > 300:
                _write(rows)
                rows = []
        _write(rows)
        with get_db() as conn:
            set_setting(conn, "orb.built_at", built or now_ist())
            set_setting(conn, "orb.updated_at", now_ist())
            set_setting(conn, "orb.as_of", latest)
        msg = f"{'updated' if built else 'built'}: {added} new screen days (data to {latest})"
        logger.info("Momentum ORB %s", msg)
        _status.update(state="idle", message=msg, at=now_ist())
        return {"status": "ok", "added": added, "pending": pending, "as_of": latest, "requests": calls}
    except Exception as exc:
        logger.exception("Momentum ORB build failed")
        _status.update(state="idle", message=f"failed: {exc}")
        return {"status": "failed", "error": str(exc)}
    finally:
        _lock.release()


def _write(rows: list[tuple]) -> None:
    if rows:
        with get_db() as conn:
            conn.executemany(UPSERT, rows)


def built() -> bool:
    with get_db() as conn:
        return bool(get_setting(conn, "orb.built_at"))


# --- reading ------------------------------------------------------------------------------------------

def rows(period: str = "1y") -> list[dict]:
    """Rows by signal date: the last N days (1m ... 2y), or y1 / y2 (the older / the latest of the last two years)."""
    with get_db() as conn:
        as_of = get_setting(conn, "orb.as_of")
        if not as_of:
            return []
        end = date.fromisoformat(as_of)
        if period == "y1":
            lo, hi = end - timedelta(days=730), end - timedelta(days=365)
        elif period == "y2":
            lo, hi = end - timedelta(days=365), end
        else:
            lo, hi = end - timedelta(days=PERIODS.get(period, 366)), end
        out = []
        for r in conn.execute("SELECT * FROM orb_trades WHERE signal_date > ? AND signal_date <= ? "
                              "ORDER BY signal_date DESC, symbol", (lo.isoformat(), hi.isoformat())):
            d = dict(r)
            d["events"] = json.loads(d["events"])
            d["or_pct"] = _r((d["or_high"] - d["or_low"]) / d["or_high"] * 100) if d["or_high"] else None
            out.append(d)
    return out


def _avg(xs) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def stats(rs: list[dict]) -> dict:
    """Counts and per-trade numbers (won / lost after costs)."""
    closed = [r for r in rs if r["status"] in ("won", "lost")]
    won = [r for r in closed if r["status"] == "won"]
    lost = [r for r in closed if r["status"] == "lost"]
    gain = sum(r["net_pct"] for r in won)
    loss = -sum(r["net_pct"] for r in lost)
    exits = Counter(r["reason"] for r in closed)
    return {"counts": {"screened": len(rs), "entered": len(closed), "won": len(won), "lost": len(lost),
                       "no_entry": sum(r["status"] == "no_entry" for r in rs),
                       "pending": sum(r["status"] == "pending" for r in rs)},
            "win_rate": round(100 * len(won) / len(closed), 1) if closed else None,
            "target_rate": round(100 * sum(r["booked"] for r in closed) / len(closed), 1) if closed else None,
            "avg_win": _avg([r["net_pct"] for r in won]), "avg_loss": _avg([r["net_pct"] for r in lost]),
            "avg_gross": _avg([r["result_pct"] for r in closed]), "avg_net": _avg([r["net_pct"] for r in closed]),
            "expectancy_r": _avg([r["r"] for r in closed]),
            "profit_factor": round(gain / loss, 2) if loss > 0 else None,
            "exits": [{"reason": k, "trades": v, "pct": round(100 * v / len(closed), 1)} for k, v in exits.most_common()]}


def split_or(rs: list[dict]) -> list[dict]:
    """Per opening-range size (% of its high): shows whether very narrow or very wide ranges behave differently."""
    out = []
    for lo, hi, label in OR_BUCKETS:
        sub = [r for r in rs if r.get("or_pct") is not None and lo <= r["or_pct"] < hi]
        s = stats(sub)
        out.append({"bucket": label, "entered": s["counts"]["entered"], "win_rate": s["win_rate"],
                    "target_rate": s["target_rate"], "avg_net": s["avg_net"]})
    return out


def portfolio(rs: list[dict], config: AppConfig) -> dict:
    """The account (capital, risk_pct sized by the stop, position_cap, max_positions, costs) vs Nifty 500 held."""
    from backend.ingest import universe as uni
    cfg = config.orb
    trades = [r for r in rs if r["status"] in ("won", "lost")]
    if not rs:
        return {"dates": [], "strategy": [], "nifty": []}
    start = min(r["trade_date"] or r["signal_date"] for r in rs)
    u = uni.resolve_universe(config.ingest)
    index_token = next((i["instrument_token"] for i in u["indices"] if i["symbol"] == uni.INDEX_NAME), None)
    with get_db() as conn:
        index = get_candles(conn, index_token, start=start) if index_token is not None else []
    days = [c["date"] for c in index] or sorted({r["trade_date"] for r in trades})
    by_day: dict = {}
    for r in trades:
        for ev in r["events"]:
            by_day.setdefault(ev["date"], []).append((ev, {"symbol": r["symbol"], "breakout_date": r["signal_date"],
                                                           "stop": r["stop"], "priority": -(r["rsi"] or 0)}))
    costs = bt.costs(cfg.slippage_pct, cfg.cost_pct, cfg.cost_pct)    # every sale is the same day
    curve, idle, taken, skipped = bt.book(days, by_day, {}, cfg.capital, cfg.max_positions, cfg.risk_pct,
                                          cfg.position_cap, 0, costs)
    nifty = [round((c["close"] / index[0]["close"] - 1) * 100, 2) for c in index] if index else []
    up, months = bt.months_up(days, curve)
    return {"dates": days, "strategy": curve, "nifty": nifty, "taken": taken, "skipped": skipped,
            "return": curve[-1] if curve else None, "value": round(cfg.capital * (1 + (curve[-1] if curve else 0) / 100)),
            "drawdown": bt.drawdown(curve), "months_up": [up, months],
            "nifty_return": nifty[-1] if nifty else None, "nifty_drawdown": bt.drawdown(nifty) if nifty else None,
            "settings": {"capital": cfg.capital, "risk_pct": cfg.risk_pct, "position_cap": cfg.position_cap,
                         "max_positions": cfg.max_positions, "cost_pct": cfg.cost_pct,
                         "slippage_pct": cfg.slippage_pct}}
