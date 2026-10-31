"""Trend Breakout (swing, long only, Zerodha delivery) on today's Nifty 500 list from daily candles. A port of the
"breakout" strategy in randan96mk/Backtest swing.py (indicators, liquid, decide, simulate, delivery_charges, perf,
periods, evaluate, robust) with its baseline settings; keep the two identical. Read-only: never orders.

Decided on a day's close, filled at the next day's open (no look-ahead):
  calendar   NIFTY 50's sessions; the first warmup_sessions only warm the indicators up.
  market     no new buys unless NIFTY 50 closes over its 200-day average (positions keep their exits).
  universe   each day, the `top` stocks by 20-day average turnover with close >= min_price.
  entry      close > SMA50 > SMA150 > SMA200, SMA200 over its value 20 sessions earlier, close >= 0.75 x the 52-week
             high and >= 1.3 x the 52-week low, close > the previous hi_window-day high, volume >= vol_mult x the
             50-day average, not held. More signals than free slots: the highest 6-month return first.
  size       at most max_pos positions, qty = floor(min(equity / max_pos, cash) / (fill x 1.0012)), whole shares.
  stop       max(close - atr_mult x ATR14, close x (1 - stop_pct %)) at the signal, never moved; a day's low at or
             under it exits at min(open, stop), from the entry day on.
  exit       a close under SMA(exit_sma): out at the next open. No target. The end of the test closes at the last close.
  costs      STT 0.1% both sides, NSE 0.00297%, SEBI Rs 10 a crore, GST 18% on those two, stamp 0.015% on buys,
             DP Rs 15.93 a sale; slippage a fill (normal and stressed).
"""

import json
import threading
from dataclasses import asdict, replace
from datetime import date, timedelta
from math import floor
from typing import Optional

from backend.config import AppConfig
from backend.database.models import get_candles, get_db, get_setting, now_ist, set_setting
from backend.logging_config.logger import setup_logger

logger = setup_logger("trend_breakout")

INDEX = "NIFTY 50"
SPLITS = ("Development", "Validation", "Out-of-sample")
NEIGHBOURS = (("vol_mult", (1.3, 2.0)), ("stop_pct", (6.0, 10.0)), ("atr_mult", (1.5, 3.0)), ("hi_window", (10, 55)),
              ("exit_sma", (20,)), ("max_pos", (5, 15)), ("top", (0.5, 1.5)), ("regime", (False,)))
# "top" moves relative to the variant's own universe: A (200) -> 100 / 300, E (300) -> 150 / 450
LATE_NEIGHBOURS = (("late_at", (50.0, 150.0)), ("late_pct", (20.0, 30.0)))   # E+ only: 14 + 4 = 18
LABELS = {"late_at": "trail from +{:g}%", "late_pct": "trail {:g}%", "vol_mult": "volume ×{}", "stop_pct": "stop {:g}%", "atr_mult": "ATR ×{:g}", "hi_window": "breakout {} days",
          "exit_sma": "exit SMA {}", "max_pos": "max {} positions", "top": "universe top {}",
          "regime": "no market filter"}

_status = {"state": "idle", "message": "", "at": None}
_lock = threading.Lock()


def status() -> dict:
    return dict(_status)


# --- indicators on one stock's own candles ---------------------------------------------------------------

def sma(xs, n):
    out, s = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rolling(xs, n, fn):
    return [fn(xs[i - n + 1:i + 1]) if i >= n - 1 else None for i in range(len(xs))]


def indicators(rows):
    c = [r["close"] for r in rows]
    h, lo, v = [r["high"] for r in rows], [r["low"] for r in rows], [r["volume"] for r in rows]
    tr = [h[0] - lo[0]] + [max(h[i], c[i - 1]) - min(lo[i], c[i - 1]) for i in range(1, len(c))]
    turn = [c[i] * v[i] for i in range(len(c))]
    ind = {"sma50": sma(c, 50), "sma150": sma(c, 150), "sma200": sma(c, 200), "sma20": sma(c, 20),
           "atr": sma(tr, 14), "vol50": sma(v, 50), "turn20": sma(turn, 20),
           "hi252": rolling(h, 252, max), "lo252": rolling(lo, 252, min)}
    for n in (10, 20, 55):
        ind[f"hi{n}_prev"] = [None] + rolling(h, n, max)[:-1]
    ind["sma200_20"] = [None] * 20 + ind["sma200"][:-20]
    ind["ret126"] = [None] * 126 + [c[i] / c[i - 126] - 1 for i in range(126, len(c))]
    return ind


# --- costs -----------------------------------------------------------------------------------------------

def delivery_charges(value, sell):
    stt = value * 0.1 / 100
    exch = value * 0.00297 / 100
    sebi = value * 10 / 1e7
    gst = (exch + sebi) * 0.18
    return stt + exch + sebi + gst + (15.93 if sell else value * 0.015 / 100)


# --- the rules: decided at the close of calendar day i ---------------------------------------------------

def ranked_by_turnover(S, cal_i, min_price):
    """The day's stocks priced at min_price or more, most traded first (20-day average turnover, data up to the day)."""
    xs = [(s["ind"]["turn20"][j], sym) for sym, s in S.items()
          if (j := s["at"].get(cal_i)) is not None and s["ind"]["turn20"][j] and s["rows"][j]["close"] >= min_price]
    return [sym for _, sym in sorted(xs, reverse=True)]


def liquid(S, cal_i, top, min_price=100.0):
    """The universe of a day: its `top` most traded stocks. Every strategy picks its stocks through this, with its
    own top (A 200, E 300)."""
    return set(ranked_by_turnover(S, cal_i, min_price)[:top])


def val(s, j, k):
    return s["ind"][k][j]


def stop_of(c, atr, p):
    return max(c - p.atr_mult * atr, c * (1 - p.stop_pct / 100))


def late_stop(entry: float, best: float, p) -> Optional[float]:
    """E+'s late trailing stop after a close (swing.py late_stop, "pct"): None below +late_at %, else late_pct %
    under the best close since entry. The caller only ever raises the stop with it, from the next session."""
    if not p.late_at or best < entry * (1 + p.late_at / 100):
        return None
    return best * (1 - p.late_pct / 100)


def raise_late(pos: dict, close: float, p) -> Optional[float]:
    """Today's close into pos (px = entry, best, stop, trailed): best close, then the late trail. Returns the new
    stop when it went up (used from the next session), else None. Shared by the backtest, paper and real."""
    pos["best"] = max(pos.get("best") or pos["px"], close)
    new = late_stop(pos["px"], pos["best"], p)
    if new is not None and new > (pos["stop"] or 0):
        pos["stop"], pos["trailed"] = new, True
        return new
    return None


def decide(i, S, held, regime_on, p):
    """(symbols to sell at the next open, ranked [(symbol, stop)] to buy at the next open, the day's signals and
    pool size). Signals are counted on market-filter-off days too, but not bought."""
    sells = set()
    for sym in held:
        s = S[sym]
        j = s["at"].get(i)
        if j is None:
            continue
        c = s["rows"][j]["close"]
        ex = f"sma{p.exit_sma}"
        if val(s, j, ex) and c < val(s, j, ex):
            sells.add(sym)
    cands = []
    hi = f"hi{p.hi_window}_prev"
    pool = liquid(S, i, p.top, p.min_price)
    for sym in pool:
        if sym in held:
            continue
        s = S[sym]
        j = s["at"][i]
        g = lambda k: val(s, j, k)
        c, v = s["rows"][j]["close"], s["rows"][j]["volume"]
        if None in (g("sma50"), g("sma150"), g("sma200"), g("sma200_20"), g("hi252"), g(hi), g("atr")):
            continue
        if (c > g("sma50") > g("sma150") > g("sma200") and g("sma200") > g("sma200_20")
                and c >= 0.75 * g("hi252") and c >= 1.3 * g("lo252") and c > g(hi)
                and g("vol50") and v >= p.vol_mult * g("vol50")):
            cands.append((g("ret126") or 0, sym, stop_of(c, g("atr"), p)))
    ranked = [(sym, stop) for _, sym, stop in sorted(cands, reverse=True)]
    return sells, (ranked if regime_on else []), len(ranked), len(pool)


# --- market filters: A = NIFTY over its 200-day SMA; E ("half") = 10 slots over the 200-day, 5 over the 50-day ----

MODES = {"A": "sma200", "E": "half"}


def rules(config, key: str):
    """A variant's full rule set: the shared settings with its own market filter and universe (A: 200-day filter,
    top `top`, no late trail; E+: half filter, top `top_e`, the late trail late_at_e / late_pct_e)."""
    b = config.breakout
    if key == "A":
        return replace(b, regime_mode=MODES[key], top=b.top, late_at=0.0)
    return replace(b, regime_mode=MODES[key], top=b.top_e, late_at=b.late_at_e, late_pct=b.late_pct_e)


EPLUS_SINCE = "2026-11-01"                               # E became E+ (the late trail) from this update


def variant_name(key: str, top: int, late: bool = True) -> str:
    if key == "A":
        return f"A · 200-day filter, top {top}"
    return f"E+ · half filter, top {top}, late trail" if late else f"E · half filter, top {top}"


def filter_day(mode, i, closes):
    """Day i's filter from NIFTY 50 closes up to day i's close only: 1 = all slots, 0.5 = half, 0 = no new buys.
    (swing.py filter_day, the modes used here.)"""
    c = closes[:i + 1]
    avg = lambda n, back=0: sum(c[len(c) - n - back:len(c) - back]) / n if len(c) >= n + back else None
    m50, m200 = avg(50), avg(200)
    above200 = m200 is not None and c[-1] > m200
    above50 = m50 is not None and c[-1] > m50
    if mode == "sma200":
        return 1.0 if above200 else 0.0
    if mode == "half":
        return 1.0 if above200 else 0.5 if above50 else 0.0
    raise ValueError(mode)


def market_filters(closes) -> dict:
    return {m: [filter_day(m, i, closes) for i in range(len(closes))] for m in ("half",)}


def slots(i, index_ok, filters, p):
    """(new buys allowed, open-position cap) decided at day i's close."""
    regime_on, cap = index_ok[i] or not p.regime, p.max_pos
    if p.regime and p.regime_mode != "sma200":
        frac = filters[p.regime_mode][i]
        regime_on, cap = frac > 0, (p.max_pos if frac >= 1 else p.max_pos // 2)
    return regime_on, cap


def filter_label(frac: float, mode: str) -> str:
    if mode == "sma200":
        return "ON" if frac >= 1 else "OFF"
    return "FULL" if frac >= 1 else "HALF" if frac > 0 else "OFF"


# --- the account -----------------------------------------------------------------------------------------

def simulate(cal, S, index_ok, p, slip, first, capital=None, close_end=True, filters=None):
    """Daily portfolio from calendar index `first`: (equity by day, equity before costs by day, trades, funnel).
    capital: instead of p.capital (paper trading); close_end=False leaves the last day's positions open;
    filters: market_filters() for a regime_mode other than sma200 (E: half the slots between the averages)."""
    cash, held, trades = (capital or p.capital), {}, []
    pend_sell, pend_buy, pend_cap = set(), [], p.max_pos
    eq, eq_gross, costs_cum = [], [], 0.0
    funnel = {"stock_days": 0, "signals": 0, "no_slot": 0, "market_off": 0, "too_small": 0}
    fill = lambda px, buying: px * (1 + slip / 100) if buying else px * (1 - slip / 100)

    def close_pos(sym, px, d, why):
        nonlocal cash, costs_cum
        pos = held.pop(sym)
        f = fill(px, False)
        ch = delivery_charges(pos["qty"] * f, True)
        cash += pos["qty"] * f - ch
        gross = (px - pos["px"]) * pos["qty"]
        cost = pos["cost"] + ch + (px - f) * pos["qty"]
        costs_cum += ch + (px - f) * pos["qty"]
        trades.append({"sym": sym, "in": pos["d"], "out": d, "qty": pos["qty"], "buy": round(pos["px"], 2),
                       "sell": round(px, 2), "stop": round(pos["stop"], 2), "days": len(pos["days"]),
                       "best": round(pos.get("best") or pos["px"], 2),
                       "gross": gross, "costs": cost, "net": gross - cost, "why": why})

    for i in range(first, len(cal)):
        d = cal[i]
        bar = lambda sym: S[sym]["rows"][S[sym]["at"][i]] if i in S[sym]["at"] else None
        for sym in list(pend_sell):                      # 1) sells at the open
            b = bar(sym)
            if sym in held and b:
                close_pos(sym, b["open"], d, "exit")
                pend_sell.discard(sym)
        equity = cash + sum(pos["qty"] * pos["last"] for pos in held.values())
        for k, (sym, stop) in enumerate(pend_buy):       # 2) buys at the open, equal weight
            if len(held) >= pend_cap:
                funnel["no_slot"] += len(pend_buy) - k
                break
            b = bar(sym)
            if not b or sym in held:
                continue
            f = fill(b["open"], True)
            qty = floor(min(equity / p.max_pos, cash) / (f * (1 + 0.0012)))
            if qty < 1:
                funnel["too_small"] += 1
                continue
            ch = delivery_charges(qty * f, False)
            cash -= qty * f + ch
            costs_cum += ch + (f - b["open"]) * qty
            held[sym] = {"qty": qty, "px": b["open"], "d": d, "i": i, "stop": stop, "last": b["open"],
                         "best": b["open"], "cost": ch + (f - b["open"]) * qty, "days": {d}}
        pend_sell, pend_buy = set(), []
        for sym in list(held):                           # 3) stops during the day, then the close
            b = bar(sym)
            if not b:
                continue
            pos = held[sym]
            pos["days"].add(d)
            if pos["stop"] and b["low"] <= pos["stop"]:
                close_pos(sym, min(b["open"], pos["stop"]), d, "trail" if pos.get("trailed") else "stop")
                continue
            pos["last"] = b["close"]
            if p.late_at:                                # E+: the late trail from today's close, used tomorrow
                raise_late(pos, b["close"], p)
        e = cash + sum(pos["qty"] * pos["last"] for pos in held.values())
        eq.append(e)
        eq_gross.append(e + costs_cum)
        if i + 1 < len(cal):                             # 4) decide at the close
            regime_on, pend_cap = slots(i, index_ok, filters, p)
            pend_sell, pend_buy, n_sig, n_pool = decide(i, S, held, regime_on, p)
            funnel["stock_days"] += n_pool
            funnel["signals"] += n_sig
            if not regime_on:
                funnel["market_off"] += n_sig
    if not close_end:
        return eq, eq_gross, trades, funnel
    for sym in list(held):
        close_pos(sym, held[sym]["last"], cal[-1], "end")
    eq[-1] = cash
    eq_gross[-1] = cash + costs_cum
    return eq, eq_gross, trades, funnel


# --- performance -----------------------------------------------------------------------------------------

def perf(eq, start_eq, days):
    if not eq:
        return 0.0, 0.0, 0.0
    ret = eq[-1] / start_eq - 1
    years = max(days / 248, 1e-9)
    cagr = (1 + ret) ** (1 / years) - 1 if ret > -1 else -1.0
    peak, dd = start_eq, 0.0
    for e in eq:
        peak = max(peak, e)
        dd = max(dd, (peak - e) / peak)
    return ret * 100, cagr * 100, dd * 100


def periods(n):
    return [("Development", 0, int(n * .5)), ("Validation", int(n * .5), int(n * .75)),
            ("Out-of-sample", int(n * .75), n)]


def period_returns(eq, capital, n):
    return [eq[b - 1] / (eq[a - 1] if a else capital) - 1 for _, a, b in periods(n)]


def evaluate(q, cal, S, index_ok, first, nifty_cagr, filters=None):
    """The settings q at normal and stressed slippage: the numbers the robustness block shows."""
    n = len(cal) - first
    eq, _, trades, _ = simulate(cal, S, index_ok, q, q.slippage_pct, first, filters=filters)
    st = simulate(cal, S, index_ok, q, q.stress_slippage_pct, first, filters=filters)[0]
    _, cagr, dd = perf(eq, q.capital, n)
    pn, ps = period_returns(eq, q.capital, n), period_returns(st, q.capital, n)
    won = [t["net"] for t in trades if t["net"] > 0]
    lost = -sum(t["net"] for t in trades if t["net"] <= 0)
    return {"trades": len(trades), "cagr": round(cagr, 2), "stress_cagr": round(perf(st, q.capital, n)[1], 2),
            "drawdown": round(dd, 2), "periods": [round(x * 100, 2) for x in pn],
            "stress_periods": [round(x * 100, 2) for x in ps],
            "ok": all(x > 0 for x in pn + ps) and cagr > nifty_cagr,
            "win_rate": round(100 * len(won) / max(len(trades), 1), 1), "profit_factor": round(sum(won) / lost, 2) if lost else None}


def neighbours(p):
    out = []
    for key, vals in NEIGHBOURS:
        for v in vals:
            if key == "top":                          # half and 1.5x the variant's own universe (swing.py)
                v = p.top // 2 if v < 1 else p.top * 3 // 2
            out.append((LABELS[key].format(v), replace(p, **{key: v})))
    if p.late_at:
        out += [(LABELS[key].format(v), replace(p, **{key: v})) for key, vals in LATE_NEIGHBOURS for v in vals]
    return out


# --- data ------------------------------------------------------------------------------------------------

def load(config: AppConfig) -> dict:
    """Stored daily candles (no Kite calls): the NIFTY 50 calendar, each stock's rows on it with indicators."""
    from backend.ingest import universe as uni
    p = config.breakout
    u = uni.resolve_universe(config.ingest)
    index_token = next((i["instrument_token"] for i in u["indices"] if i["symbol"] == INDEX), None)
    with get_db() as conn:
        idx_all = get_candles(conn, index_token) if index_token is not None else []
        if not idx_all:
            return {"error": "No NIFTY 50 daily candles stored yet (run the daily sync)."}
        end = date.fromisoformat(idx_all[-1]["date"])
        start = (end - timedelta(days=int((p.test_years + p.warmup_years) * 365))).isoformat()
        idx = [r for r in idx_all if r["date"] >= start]
        missing = []
        if len(idx) < p.warmup_sessions + 100:
            return {"error": f"Only {len(idx)} NIFTY 50 sessions stored: need {p.warmup_sessions + 100}+ "
                             "(the daily backfill fetches the missing years)."}
        cal = [r["date"] for r in idx]
        pos_of = {d: i for i, d in enumerate(cal)}
        S = {}
        for s in u["stocks"]:
            rows = [r for r in get_candles(conn, s["instrument_token"], start=start)
                    if r["date"] in pos_of and r["close"] > 0]
            if len(rows) >= 60:
                S[s["symbol"]] = {"rows": rows, "ind": indicators(rows), "token": s["instrument_token"],
                                  "at": {pos_of[r["date"]]: j for j, r in enumerate(rows)}}
            else:                                     # never skipped silently: logged and shown on the tab
                missing.append(s["symbol"])
    if missing:
        logger.warning("Trend Breakout: %d Nifty 500 stocks without enough daily candles (under 60 sessions): %s",
                       len(missing), ", ".join(sorted(missing)[:40]) + (" ..." if len(missing) > 40 else ""))
    close = [r["close"] for r in idx]
    m200 = sma(close, 200)
    return {"cal": cal, "S": S, "missing": sorted(missing), "index_close": close, "index_m200": m200,
            "index_ok": [m is not None and c > m for c, m in zip(close, m200)], "filters": market_filters(close),
            "tradingsymbol": {s["symbol"]: s.get("tradingsymbol", s["symbol"]) for s in u["stocks"]}}


# --- the run --------------------------------------------------------------------------------------------

def _stats(eq, eqg, trades, capital, n):
    won = [t for t in trades if t["net"] > 0]
    lost = -sum(t["net"] for t in trades if t["net"] <= 0)
    _, cagr, dd = perf(eq, capital, n)
    nt = max(len(trades), 1)
    return {"trades": len(trades), "gross": round(eqg[-1] - capital, 2), "costs": round(eqg[-1] - eq[-1], 2),
            "net": round(eq[-1] - capital, 2), "cagr": round(cagr, 2), "drawdown": round(dd, 2),
            "win_rate": round(100 * len(won) / nt, 1),
            "profit_factor": round(sum(t["net"] for t in won) / lost, 2) if lost else None,
            "avg_hold": round(sum(t["days"] for t in trades) / nt, 1),
            "cost_per_trade": round((eqg[-1] - eq[-1]) / nt, 2),
            "exits": {k: sum(t["why"] == k for t in trades) for k in ("stop", "trail", "exit", "end")},
            "avg_win": round(sum(t["net"] for t in won) / len(won), 2) if won else None,
            "avg_loss": round(-lost / (len(trades) - len(won)), 2) if len(trades) > len(won) else None,
            "best_trade_pct": round(max(((t["sell"] / t["buy"] - 1) * 100 for t in trades), default=0), 1)}


def _variant(data: dict, p, key: str, first: int, nifty: list, nifty_cagr: float) -> dict:
    """One variant (A or E): both slippages, periods, years, curve, robustness, trades."""
    cal, S, index_ok, close, filters = data["cal"], data["S"], data["index_ok"], data["index_close"], data["filters"]
    n = len(cal) - first
    eq, eqg, trades, funnel = simulate(cal, S, index_ok, p, p.slippage_pct, first, filters=filters)
    seq, seqg, strades, sfunnel = simulate(cal, S, index_ok, p, p.stress_slippage_pct, first, filters=filters)
    out = {"name": variant_name(key, p.top, bool(p.late_at)), "mode": p.regime_mode, "top": p.top, "funnel": funnel,
           "late_at": p.late_at, "late_pct": p.late_pct if p.late_at else None,
           "updated_note": f"Updated on {EPLUS_SINCE}: added late trail ({p.late_pct:g}% under the highest close once "
                           f"+{p.late_at:g}%)" if p.late_at else None,
           "normal": _stats(eq, eqg, trades, p.capital, n), "stress": _stats(seq, seqg, strades, p.capital, n)}
    out["normal"]["skipped"] = {k: funnel[k] for k in ("no_slot", "market_off", "too_small")}
    out["stress"]["skipped"] = {k: sfunnel[k] for k in ("no_slot", "market_off", "too_small")}
    allowed = [slots(i, index_ok, filters, p)[0] for i in range(first, len(cal))]
    out["allowed_pct"] = round(100 * sum(allowed) / max(n, 1), 1)
    splits = []
    for name, a, b in periods(n):
        base = lambda xs: xs[a - 1] if a else p.capital
        splits.append({"name": name, "from": cal[first + a], "to": cal[first + b - 1],
                       "gross": round((eqg[b - 1] / base(eqg) - 1) * 100, 2),
                       "net": round((eq[b - 1] / base(eq) - 1) * 100, 2),
                       "stress": round((seq[b - 1] / base(seq) - 1) * 100, 2),
                       "nifty": round((close[first + b - 1] / close[first + a - 1] - 1) * 100, 2)})
        splits[-1]["costs"] = round(splits[-1]["gross"] - splits[-1]["net"], 2)
    out["splits"] = splits
    years = {}
    for i in range(first, len(cal)):
        years.setdefault(cal[i][:4], []).append(i)
    out["years"] = [{"year": y, "net": round((eq[ix[-1] - first] / (eq[ix[0] - first - 1] if ix[0] > first
                                                                   else p.capital) - 1) * 100, 2),
                     "nifty": round((close[ix[-1]] / close[ix[0] - 1] - 1) * 100, 2)} for y, ix in years.items()]
    out["ok_periods"] = all(x["net"] > 0 and x["stress"] > 0 for x in splits)
    out["beats_nifty"] = out["normal"]["cagr"] > nifty_cagr
    out["verdict"] = out["ok_periods"] and out["beats_nifty"]
    step = max(1, n // 400)                              # the curves, thinned to ~400 points
    ix = list(range(0, n, step)) + ([n - 1] if (n - 1) % step else [])
    out["curve"] = {"normal": [round(eq[k]) for k in ix], "stress": [round(seq[k]) for k in ix]}
    rob = []
    nb = neighbours(p)
    for k, (label, q) in enumerate(nb, 1):
        _status["message"] = f"{key}: robustness {k} of {len(nb)}: {label}"
        rob.append({"label": label, **evaluate(q, cal, S, index_ok, first, nifty_cagr, filters)})
    out["robust"] = rob
    out["robust_pass"] = sum(r["ok"] for r in rob)
    tidy = lambda ts: [{**t, "gross": round(t["gross"], 2), "costs": round(t["costs"], 2), "net": round(t["net"], 2)}
                       for t in sorted(ts, key=lambda t: (t["in"], t["sym"]), reverse=True)]
    out["trades"], out["stress_trades"] = tidy(trades), tidy(strades)
    out["stocks_traded"] = len({t["sym"] for t in trades})
    return out


def run(config: AppConfig) -> dict:
    """Both variants (A: 200-day filter, E: half filter) at normal and stressed slippage, the splits, years,
    robustness; saved for the tab. A is exactly the original rules."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        base = config.breakout
        _status.update(state="running", message="loading the daily candles", at=now_ist())
        data = load(config)
        if "error" in data:
            _status.update(state="idle", message=data["error"])
            return {"status": "no_data", "error": data["error"]}
        cal, close = data["cal"], data["index_close"]
        first = base.warmup_sessions
        n = len(cal) - first
        nifty = [close[i] / close[first - 1] * base.capital for i in range(first, len(cal))]
        nifty_cagr = perf(nifty, base.capital, n)[1]
        step = max(1, n // 400)
        ix = list(range(0, n, step)) + ([n - 1] if (n - 1) % step else [])
        out = {"from": cal[first], "to": cal[-1], "sessions": n, "stocks": len(data["S"]),
               "nifty": {"cagr": round(nifty_cagr, 2), "drawdown": round(perf(nifty, base.capital, n)[2], 2),
                         "return": round((nifty[-1] / base.capital - 1) * 100, 2)},
               "curve": {"dates": [cal[first + k] for k in ix], "nifty": [round(nifty[k]) for k in ix]},
               "variants": {}, "settings": asdict(base)}
        out["missing"] = data["missing"]
        for key in MODES:
            p = rules(config, key)
            _status["message"] = f"{variant_name(key, p.top)}: simulating {n} sessions, {len(data['S'])} stocks"
            out["variants"][key] = _variant(data, p, key, first, nifty, nifty_cagr)
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO breakout_runs (id, updated_at, payload) VALUES (1, ?, ?)",
                         (now_ist(), json.dumps(out)))
        v = out["variants"]
        msg = " · ".join(f"{k}: {x['normal']['trades']} trades, CAGR {x['normal']['cagr']:.1f}%, "
                         f"{x['robust_pass']}/{len(x['robust'])} robust" for k, x in v.items())
        logger.info("Trend Breakout: %s (NIFTY 50 %.1f%%)", msg, nifty_cagr)
        _status.update(state="idle", message=msg, at=now_ist())
        return {"status": "ok", **{k: {"trades": x["normal"]["trades"], "cagr": x["normal"]["cagr"]} for k, x in v.items()}}
    except Exception as exc:
        logger.exception("Trend Breakout run failed")
        _status.update(state="idle", message=f"failed: {exc}")
        return {"status": "failed", "error": str(exc)}
    finally:
        _lock.release()


def saved(config: Optional[AppConfig] = None) -> Optional[dict]:
    """The latest run (None before the first run, or for a run from before the A / E variants). With `config`,
    "stale" lists the variants whose universe changed since that run (their numbers must not be trusted)."""
    with get_db() as conn:
        r = conn.execute("SELECT updated_at, payload FROM breakout_runs WHERE id = 1").fetchone()
    if not r:
        return None
    payload = json.loads(r["payload"])
    if "variants" not in payload:
        return None
    if config is not None:
        def stale(k, v):                                 # universe or late trail changed: the numbers are not these rules'
            p = rules(config, k)
            return v.get("top") != p.top or (v.get("late_at") or 0) != p.late_at or \
                (p.late_at and v.get("late_pct") != p.late_pct)
        payload["stale"] = [k for k, v in payload["variants"].items() if stale(k, v)]
    return {**payload, "updated_at": r["updated_at"]}


# --- the evening scan -----------------------------------------------------------------------------------

def scan(config: AppConfig, holdings: Optional[list] = None) -> dict:
    """After the close: the market filter, the day's breakout buys (ranked, sized at capital / max_pos) and, for
    each Kite holding, HOLD or SELL at open (close under SMA(exit_sma), or sell_pct under the average price)."""
    p = config.breakout
    data = load(config)
    if "error" in data:
        return {"error": data["error"]}
    cal, S = data["cal"], data["S"]
    i = len(cal) - 1
    c, m = data["index_close"][i], data["index_m200"][i]
    market_on = bool(data["index_ok"][i])
    closes = data["index_close"]
    fstate = {}
    for key, mode in MODES.items():
        now = filter_day(mode, i, closes)
        before = filter_day(mode, i - 1, closes) if i else now
        fstate[key] = {"name": variant_name(key, rules(config, key).top), "top": rules(config, key).top,
                       "state": filter_label(now, mode),
                       "prev": filter_label(before, mode), "changed": now != before,
                       "slots": p.max_pos if now >= 1 else p.max_pos // 2 if now > 0 else 0}
    pa, pe = rules(config, "A"), rules(config, "E")
    wide = pa if pa.top >= pe.top else pe                 # the larger universe; each breakout is tagged with who can buy it
    turn_rank = {sym: k + 1 for k, sym in enumerate(ranked_by_turnover(S, i, p.min_price))}
    _, ranked, _, _ = decide(i, S, {}, True, wide)
    buys = []
    for sym, stop in ranked:
        r = turn_rank.get(sym)
        who = [k for k, q in (("A", pa), ("E", pe)) if r and r <= q.top]
        s = S[sym]
        close = s["rows"][s["at"][i]]["close"]
        qty = floor(p.capital / p.max_pos / close)
        buys.append({"symbol": sym, "close": round(close, 2), "stop": round(stop, 2),
                     "stop_pct": round((1 - stop / close) * 100, 2), "qty": qty, "value": round(qty * close, 2),
                     "risk": round(qty * (close - stop), 2),
                     "ret126": round((val(s, s["at"][i], "ret126") or 0) * 100, 1), "turnover_rank": r, "for": who})
    by_ts = {ts: sym for sym, ts in data["tradingsymbol"].items()}
    held = []
    for h in holdings or []:
        qty = (h.get("quantity") or 0) + (h.get("t1_quantity") or 0)
        if qty <= 0:
            continue
        ts = h.get("tradingsymbol", "")
        sym = by_ts.get(ts, ts)
        avg = h.get("average_price") or 0
        s = S.get(sym)
        j = s["at"].get(i) if s else None
        close = s["rows"][j]["close"] if j is not None else (h.get("close_price") or h.get("last_price") or 0)
        ma = val(s, j, f"sma{p.exit_sma}") if j is not None else None
        why = []
        if ma and close < ma:
            why.append(f"close under SMA{p.exit_sma}")
        if avg and close <= avg * (1 - p.sell_pct / 100):
            why.append(f"{(1 - close / avg) * 100:.1f}% under the average price")
        held.append({"symbol": ts, "qty": qty, "avg": round(avg, 2), "close": round(close, 2),
                     "sma": round(ma, 2) if ma else None, "action": "SELL at open" if why else "HOLD",
                     "why": ", ".join(why) or ("trend intact" if ma else f"not in the Nifty 500 data: SMA{p.exit_sma} unknown")})
    m50 = sma(closes, 50)[i]
    return {"as_of": cal[i], "market_on": market_on, "nifty": round(c, 2), "nifty_sma200": round(m, 2) if m else None,
            "nifty_sma50": round(m50, 2) if m50 else None, "filters": fstate,
            "buys": buys, "holdings": held, "computed_at": now_ist()}


def run_scan(config: AppConfig, client=None) -> dict:
    """The evening scan with the Kite holdings (read-only) when logged in; saved for the tab and the report."""
    holdings = None
    if client is not None and getattr(client, "has_token", False):
        try:
            holdings = client.holdings()
        except Exception as exc:
            logger.warning("Kite holdings for the Trend Breakout scan: %s", exc)
    res = scan(config, holdings)
    if "error" not in res:
        res["holdings_read"] = holdings is not None
        with get_db() as conn:
            set_setting(conn, "breakout.scan", json.dumps(res))
    return res


def saved_scan() -> Optional[dict]:
    with get_db() as conn:
        raw = get_setting(conn, "breakout.scan")
    return json.loads(raw) if raw else None
