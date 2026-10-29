"""ORB + VWAP + relative volume: an intraday breakout of the 9:15-9:30 AM range, long and short, on the most liquid
Nifty 500 stocks. Backtested over history_days from 5-minute candles (fetched from Kite only for the days needed).
Read-only: Kite history only, never orders.

Universe   each session, the top_n Nifty 500 stocks by the median daily turnover (close x volume) of the previous
           turnover_days sessions, previous close over min_price (known before the open: no look-ahead).
Signal     on a completed 5-minute candle from range_end, closing by entry_end, all of:
             long   close > range high, close > VWAP, VWAP > VWAP vwap_slope_bars candles earlier,
                    volume >= rvol_min x the average of the previous rvol_bars candles (back into yesterday)
             short  the mirror (close < range low, < VWAP, VWAP falling)
           the first such candle per stock and side; bad or missing candles: no signal that day.
Trade      entry  the next candle's open, plus slippage (a short: minus).
           stop   the breakout candle's low - buffer (stop_buffer_pct of the price rounded up to the tick, at least one
                  tick); a short: its high + buffer. Over max_stop_pct from the entry, or past the entry: no trade.
           target entry + reward_r x risk. A candle touching both counts as the stop; a gap fills at the open.
           out    at the close of the candle ending at exit_time otherwise. Every exit fill pays slippage.
Account    qty = min(risk_per_trade / loss per share at the stop, what is left of `allocated`), cut until the loss at
           the stop plus the charges fits risk_per_trade; the reward at the target must beat the charges.
           At most max_entries_per_day entries and max_concurrent open; no entry once the day's realised loss plus
           the open risk plus this trade's risk would pass daily_loss_limit. Same-candle signals: the higher
           relative volume first.
Results    gross (price move), slippage, charges (costs.py) and net in rupees; development / validation /
           out-of-sample (the first 50% / next 25% / last 25% of the sessions), at normal and stressed slippage.
"""

import json
import threading
from collections import Counter
from datetime import date, timedelta
from math import floor
from statistics import median
from typing import Optional

from backend.config import AppConfig
from backend.database.models import get_candles, get_db, get_setting, now_ist, set_setting
from backend.engine import backtest as bt
from backend.engine import costs as fees
from backend.engine import intraday_ind as ind
from backend.logging_config.logger import setup_logger

logger = setup_logger("orb_vwap")

SCENARIOS = ("normal", "stress")
SPLITS = (("dev", "Development", 0.0, 0.5), ("val", "Validation", 0.5, 0.75), ("oos", "Out-of-sample", 0.75, 1.0))
AHEAD = 60                                             # sessions per Kite request (under its 100-day limit)

_status = {"state": "idle", "done": 0, "total": 0, "message": "", "at": None}
_lock = threading.Lock()
_memo: dict = {}


def status() -> dict:
    return dict(_status)


def slippage(cfg, scenario: str) -> float:
    return cfg.stress_slippage_pct if scenario == "stress" else cfg.slippage_pct


# --- universe -----------------------------------------------------------------------------------------

def universe(series: dict, cfg) -> dict[str, list[str]]:
    """{session: [symbols]}: each session's top_n by the median turnover of the turnover_days sessions before it."""
    by_day: dict = {}
    n = cfg.turnover_days
    for sym, candles in series.items():
        turn = [c["close"] * c["volume"] for c in candles]
        for t in range(n, len(candles)):
            if candles[t - 1]["close"] >= cfg.min_price:
                by_day.setdefault(candles[t]["date"], []).append((median(turn[t - n:t]), sym))
    return {d: [s for _, s in sorted(xs, reverse=True)[:cfg.top_n]] for d, xs in by_day.items()}


# --- one stock, one session ---------------------------------------------------------------------------

def stop_buffer(price: float, cfg) -> float:
    return max(cfg.tick, fees.tick_up(price * cfg.stop_buffer_pct / 100, cfg.tick))


def signals(prev: list[dict], bars: list[dict], cfg) -> list[dict]:
    """The first long and the first short signal of the session (0-2 dicts), from completed candles only."""
    last_entry = ind.plus(cfg.entry_end, -5)             # the signal candle starts by here (closes by entry_end)
    exit_bar = ind.plus(cfg.exit_time, -5)
    if not prev or ind.bad_session(bars, cfg.range_start, exit_bar):
        return []
    rng = ind.opening_range(bars, cfg.range_start, cfg.range_end)
    if rng is None:
        return []
    hi, lo = rng
    vw = ind.vwap(bars)
    vols = [b["volume"] for b in prev] + [b["volume"] for b in bars]
    out, seen = [], set()
    for i, b in enumerate(bars):
        if b["t"] < cfg.range_end or b["t"] > last_entry or i + 1 >= len(bars):
            continue
        slope = ind.vwap_slope(vw, i, cfg.vwap_slope_bars)
        rv = ind.rel_volume(vols, len(prev) + i, cfg.rvol_bars)
        if slope is None or rv is None or rv < cfg.rvol_min:
            continue
        side = ("long" if b["close"] > hi and b["close"] > vw[i] and slope > 0 else
                "short" if b["close"] < lo and b["close"] < vw[i] and slope < 0 else None)
        if side and side not in seen:
            seen.add(side)
            out.append({"side": side, "i": i, "signal_time": b["t"], "close": b["close"], "or_high": hi, "or_low": lo,
                        "vwap": round(vw[i], 2), "rvol": round(rv, 2)})
    return out


def outcome(bars: list[dict], sig: dict, cfg, slip_pct: float) -> dict:
    """The trade a signal makes at this slippage, per share: fills, stop, target, exit (independent of the size)."""
    long = sig["side"] == "long"
    b, nxt = bars[sig["i"]], bars[sig["i"] + 1]
    entry = fees.slip(nxt["open"], slip_pct, buying=long)
    stop = round(b["low"] - stop_buffer(b["low"], cfg), 2) if long else round(b["high"] + stop_buffer(b["high"], cfg), 2)
    risk = entry - stop if long else stop - entry
    row = {"entry_time": nxt["t"], "entry_px": nxt["open"], "entry_fill": round(entry, 4), "stop": stop}
    if risk <= 0:
        return {**row, "status": "invalid", "reason": "opened past the stop"}
    if risk / entry * 100 > cfg.max_stop_pct:
        return {**row, "status": "invalid", "reason": f"stop over {cfg.max_stop_pct:g}% away"}
    target = round(entry + cfg.reward_r * risk if long else entry - cfg.reward_r * risk, 2)
    exit_bar = ind.plus(cfg.exit_time, -5)
    px, why, t = None, None, None
    for j in range(sig["i"] + 1, len(bars)):
        c = bars[j]
        first = j == sig["i"] + 1
        if long:
            if c["low"] <= stop:
                px, why = (stop if first else min(stop, c["open"])), "stop"
            elif c["high"] >= target:
                px, why = (target if first else max(target, c["open"])), "target"
        else:
            if c["high"] >= stop:
                px, why = (stop if first else max(stop, c["open"])), "stop"
            elif c["low"] <= target:
                px, why = (target if first else min(target, c["open"])), "target"
        if px is None and c["t"] >= exit_bar:
            px, why = c["close"], "close"
        if px is not None:
            t = c["t"]
            break
    if px is None:                                     # the session's candles ran out (checked, so rare)
        px, why, t = bars[-1]["close"], "close", bars[-1]["t"]
    fill = fees.slip(px, slip_pct, buying=not long)
    stop_fill = fees.slip(stop, slip_pct, buying=not long)
    return {**row, "status": "signal", "reason": why, "target": target, "stop_fill": round(stop_fill, 4),
            "exit_time": t, "exit_px": px, "exit_fill": round(fill, 4)}


# --- the build ----------------------------------------------------------------------------------------

COLS = ("day", "symbol", "side", "scenario", "status", "reason", "rank", "signal_time", "close", "or_high", "or_low",
        "vwap", "rvol", "entry_time", "entry_px", "entry_fill", "stop", "stop_fill", "target", "exit_time", "exit_px",
        "exit_fill")
UPSERT = f"INSERT OR REPLACE INTO orb_signals ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})"


def build(config: AppConfig, client, rebuild: bool = False) -> dict:
    """Every session after the last one done (all of history_days on the first run or a rebuild): the day's
    universe, its 5-minute candles (fetched once, stored), the signals and their trades at both slippages."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        from backend.ingest import universe as uni
        cfg = config.orb
        with get_db() as conn:
            done = None if rebuild else get_setting(conn, "orb.as_of")
            if rebuild:
                conn.execute("DELETE FROM orb_signals")
        _status.update(state="updating" if done else "building", done=0, total=0, message="", at=now_ist())
        stocks = uni.resolve_universe(config.ingest)["stocks"]
        with get_db() as conn:
            series = {s["symbol"]: (s["instrument_token"], get_candles(conn, s["instrument_token"])) for s in stocks}
        dates = [cs[-1]["date"] for _, cs in series.values() if cs]
        if not dates:
            _status.update(state="idle", message="no candles yet")
            return {"status": "no_data"}
        latest = max(dates)
        since = done or (date.fromisoformat(latest) - timedelta(days=cfg.history_days)).isoformat()
        top = {d: s for d, s in universe({k: v[1] for k, v in series.items()}, cfg).items() if since < d <= latest}
        days = sorted(top)
        members: dict = {}
        for d in days:
            for r, sym in enumerate(top[d], 1):
                members.setdefault(sym, []).append((d, r))
        _status["total"] = len(members)
        rows, calls, missing, scanned, bad = [], 0, None, 0, 0
        for k, sym in enumerate(sorted(members)):
            token, candles = series[sym]
            sessions = [c["date"] for c in candles]
            at = {d: i for i, d in enumerate(sessions)}
            five = bt.FiveMin(client, token, sym, sessions)
            for d, rank in members[sym]:
                i = at[d]
                prev = five.day(sessions[i - 1], ahead=AHEAD) if i > 0 else []    # first: its request covers d too
                bars = five.day(d, ahead=AHEAD)
                if bars is None or prev is None:
                    missing = d if missing is None else min(missing, d)
                    continue
                scanned += 1
                if ind.bad_session(bars, cfg.range_start, ind.plus(cfg.exit_time, -5)):
                    bad += 1
                    continue
                for sig in signals(prev, bars, cfg):
                    for sc in SCENARIOS:
                        r = {**sig, **outcome(bars, sig, cfg, slippage(cfg, sc)), "day": d, "symbol": sym,
                             "scenario": sc, "rank": rank}
                        rows.append(tuple(r.get(c) for c in COLS))
            calls += five.fetched
            _status.update(done=k + 1, message=f"{sym}: {calls} Kite requests so far")
            if len(rows) > 500:
                _write(rows)
                rows = []
        _write(rows)
        upto = latest if missing is None else max([d for d in days if d < missing], default=done)
        with get_db() as conn:
            if missing is not None:                    # re-run from the first session without candles next time
                conn.execute("DELETE FROM orb_signals WHERE day > ?", (upto or "",))
            set_setting(conn, "orb.built_at", get_setting(conn, "orb.built_at") or now_ist())
            set_setting(conn, "orb.updated_at", now_ist())
            if upto:
                set_setting(conn, "orb.as_of", upto)
            old = json.loads(get_setting(conn, "orb.days") or "[]") if done else []
            set_setting(conn, "orb.days", json.dumps(old + [d for d in days if not upto or d <= upto]))
            for key, v in (("scanned", scanned), ("bad", bad)):
                old = int(get_setting(conn, f"orb.{key}") or 0) if done else 0
                set_setting(conn, f"orb.{key}", str(old + v))
        msg = (f"{'updated' if done else 'built'}: {len(days)} sessions, {scanned} stock-days, {calls} Kite requests"
               + (f"; candles missing from {missing} (log in to Kite)" if missing else ""))
        logger.info("ORB + VWAP %s", msg)
        _status.update(state="idle", message=msg, at=now_ist())
        return {"status": "ok", "sessions": len(days), "scanned": scanned, "as_of": upto, "requests": calls,
                "missing_from": missing}
    except Exception as exc:
        logger.exception("ORB + VWAP build failed")
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


# --- the account ------------------------------------------------------------------------------------

def size(entry: float, stop_fill: float, target: float, side: str, room: float, cfg) -> tuple[int, Optional[str]]:
    """(qty, why not): the loss at the stop plus the round trip's charges within risk_per_trade, the position
    within `room` (what is left of the allocation), and the reward at the target over its charges."""
    loss = abs(entry - stop_fill)
    if loss <= 0:
        return 0, "no risk"
    qty = min(floor(cfg.risk_per_trade / loss), floor(room / entry))

    def legs(q, out):
        return (q * entry, q * out) if side == "long" else (q * out, q * entry)

    while qty > 0 and qty * loss + fees.charges(*legs(qty, stop_fill), cfg)["total"] > cfg.risk_per_trade:
        qty -= 1
    if qty <= 0:
        return 0, "too small for the risk or the allocation"
    if qty * abs(target - entry) <= fees.charges(*legs(qty, target), cfg)["total"]:
        return 0, "reward under the charges"
    return qty, None


def simulate(cands: list[dict], cfg) -> tuple[list[dict], Counter]:
    """Replays the signals (one scenario) day by day through the limits: (taken trades with qty, gross, slippage,
    charges, net, r), Counter of why the others were skipped."""
    taken, skipped = [], Counter()
    by_day: dict = {}
    for c in cands:
        by_day.setdefault(c["day"], []).append(c)
    for d in sorted(by_day):
        open_, realised, entries = [], 0.0, 0
        for c in sorted(by_day[d], key=lambda c: (c["entry_time"], -c["rvol"])):
            for p in [p for p in open_ if ind.plus(p["exit_time"], 5) <= c["entry_time"]]:
                open_.remove(p)
                realised += p["net"]
            if entries >= cfg.max_entries_per_day:
                skipped["entries a day"] += 1
                continue
            if len(open_) >= cfg.max_concurrent:
                skipped["open positions"] += 1
                continue
            if any(p["symbol"] == c["symbol"] for p in open_):
                skipped["already in the stock"] += 1
                continue
            at_risk = sum(p["risk"] for p in open_)
            if -realised + at_risk + cfg.risk_per_trade > cfg.daily_loss_limit:
                skipped["daily loss limit"] += 1
                continue
            room = cfg.allocated - sum(p["value"] for p in open_)
            qty, why = size(c["entry_fill"], c["stop_fill"], c["target"], c["side"], room, cfg)
            if not qty:
                skipped[why] += 1
                continue
            long = c["side"] == "long"
            sign = 1 if long else -1
            buy, sell = (c["entry_fill"], c["exit_fill"]) if long else (c["exit_fill"], c["entry_fill"])
            ch = fees.charges(qty * buy, qty * sell, cfg)
            gross = sign * (c["exit_px"] - c["entry_px"]) * qty
            net_px = sign * (c["exit_fill"] - c["entry_fill"]) * qty
            risk = qty * abs(c["entry_fill"] - c["stop_fill"])
            t = {**c, "qty": qty, "value": round(qty * c["entry_fill"], 2), "risk": round(risk, 2),
                 "gross": round(gross, 2), "slippage": round(gross - net_px, 2), "charges": ch["total"],
                 "fees": ch, "net": round(net_px - ch["total"], 2)}
            t["r"] = round(t["net"] / risk, 2) if risk else None
            open_.append(t)
            taken.append(t)
            entries += 1
    return taken, skipped


# --- reading ------------------------------------------------------------------------------------------

def _cands(scenario: str) -> list[dict]:
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM orb_signals WHERE scenario = ? AND status = 'signal' ORDER BY day, entry_time", (scenario,))]


def sessions() -> list[str]:
    """The sessions the backtest covered (stock-days scanned in each, signal or not)."""
    with get_db() as conn:
        return json.loads(get_setting(conn, "orb.days") or "[]")


def results(config: AppConfig) -> dict:
    """Both scenarios replayed through the account: {scenario: (trades, skipped)}, the sessions, the split edges.
    Memoised until the next build or a settings change."""
    cfg = config.orb
    with get_db() as conn:
        key = (get_setting(conn, "orb.updated_at"), repr(cfg))
    if _memo.get("key") != key:
        days = sessions()
        sims = {sc: simulate(_cands(sc), cfg) for sc in SCENARIOS}
        n = len(days)
        edges = [(k, name, days[int(a * n)] if n else None, days[min(n, int(b * n)) - 1] if n else None)
                 for k, name, a, b in SPLITS]
        _memo.clear()
        _memo.update(key=key, value={"days": days, "sims": sims, "edges": edges})
    return _memo["value"]


def split_of(day: str, edges) -> Optional[str]:
    return next((k for k, _, lo, hi in edges if lo and lo <= day <= hi), None)


def stats(trades: list[dict], capital: float) -> dict:
    won = [t for t in trades if t["net"] > 0]
    gain = sum(t["net"] for t in won)
    loss = -sum(t["net"] for t in trades if t["net"] <= 0)
    net = [t["net"] for t in trades]
    eq, peak, dd = capital, capital, 0.0
    for v in net:
        eq += v
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    n = len(trades)
    total = lambda k: round(sum(t[k] for t in trades), 2)
    return {"trades": n, "longs": sum(t["side"] == "long" for t in trades),
            "shorts": sum(t["side"] == "short" for t in trades),
            "win_rate": round(100 * len(won) / n, 1) if n else None,
            "gross": total("gross"), "slippage": total("slippage"), "charges": total("charges"), "net": total("net"),
            "profit_factor": round(gain / loss, 2) if loss > 0 else None,
            "expectancy": round(sum(net) / n, 2) if n else None,
            "expectancy_r": round(sum(t["r"] or 0 for t in trades) / n, 2) if n else None,
            "avg_cost": round((sum(t["charges"] + t["slippage"] for t in trades)) / n, 2) if n else None,
            "max_drawdown": round(dd, 2), "max_drawdown_pct": round(dd / capital * 100, 2),
            "return_pct": round(sum(net) / capital * 100, 2),
            "exits": dict(Counter(t["reason"] for t in trades))}


def summary(config: AppConfig) -> dict:
    """Splits x scenarios, longs vs shorts, the curves (account % by session), why signals were skipped."""
    cfg = config.orb
    res = results(config)
    edges, days = res["edges"], res["days"]
    out = {"splits": [], "sides": [], "total": {}, "skipped": {}, "curve": {"dates": days}}
    for sc in SCENARIOS:
        trades, skipped = res["sims"][sc]
        out["total"][sc] = stats(trades, cfg.capital)
        out["skipped"][sc] = dict(skipped.most_common())
        by = {}
        for t in trades:
            by[t["day"]] = by.get(t["day"], 0) + t["net"]
        acc, curve = 0.0, []
        for d in days:
            acc += by.get(d, 0)
            curve.append(round(acc / cfg.capital * 100, 2))
        out["curve"][sc] = curve
    for k, name, lo, hi in edges:
        row = {"split": k, "name": name, "from": lo, "to": hi}
        for sc in SCENARIOS:
            row[sc] = stats([t for t in res["sims"][sc][0] if split_of(t["day"], edges) == k], cfg.capital)
        out["splits"].append(row)
    for side in ("long", "short"):
        out["sides"].append({"side": side, **{sc: stats([t for t in res["sims"][sc][0] if t["side"] == side],
                                                        cfg.capital) for sc in SCENARIOS}})
    with get_db() as conn:
        out["signals"] = conn.execute("SELECT COUNT(*) FROM orb_signals WHERE scenario = 'normal'").fetchone()[0]
        out["scanned"] = int(get_setting(conn, "orb.scanned") or 0)
        out["bad_days"] = int(get_setting(conn, "orb.bad") or 0)
    return out


def trades(config: AppConfig, scenario: str = "normal", split: str = "all", side: str = "all") -> list[dict]:
    """Taken trades, newest first."""
    res = results(config)
    rs = res["sims"][scenario][0]
    if split != "all":
        rs = [t for t in rs if split_of(t["day"], res["edges"]) == split]
    if side != "all":
        rs = [t for t in rs if t["side"] == side]
    return sorted(rs, key=lambda t: (t["day"], t["entry_time"]), reverse=True)
