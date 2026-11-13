"""Backtest F: the Trend Breakout signals traded on stock futures, one lot a name, from the stored cash and continuous
futures candles (no Kite calls, never orders).

  signals    LONG = E+ (`trend_breakout.decide` on the CASH candles: the same trend template, breakout, volume and half
             market filter, the same top-N universe), kept to the underlyings with a stored future whose lot value is
             at most `futures.universe_lot_cap`. SHORT = E-: close under the previous 20-day low with a lower-high
             structure (the last 5-bar pivot high under the one before), close under SMA50 and SMA200, the market filter
             inverted (NIFTY 50 under its 50-day average); weakest 6-month return first; no short within
             `results_guard_sessions` of results when the calendar knows them.
  execution  decided on the close, filled at the next session's FUTURES open (continuous series). Stop = atr_mult x
             ATR(14, futures) under / over the entry, capped at `max_stop_pct` of it (a short's stop also never sits
             over its swing high); a day's low (high) through the stop exits at min(open, stop) (max(open, stop)).
             Longs: E+'s exit (close under SMA50 -> next open) and late trail on the futures closes. Shorts: the time
             stop after `SHORT_TIME_STOP` sessions, nothing else. One lot a name; eligibility through
             `eligibility.check` with the book's own held set and open lots (slots, small-lot third slot, lot cap,
             risk cap, notional 2.5x, cash floor, heat, margin 80%, ban list and results when the data exists).
             Margin = margin_pct of the notional, reserved from cash; equity marked to the close every day; a day's
             loss of `daily_loss_pct` blocks the next session's entries.
  rolls      `roll_days` sessions before each monthly expiry an open position rolls to the next month: an exit's and an
             entry's charges and slippage then, and the basis (the continuous series' jump after the expiry net of
             the cash move that night) when the position is still held after the expiry. A position whose exit is
             already due is not rolled (`roll_exit`). Never held into expiry day on the old contract.
  costs      per side on the notional: STT 0.02% on sells, exchange 0.00173%, SEBI 0.0001%, GST 18% on brokerage +
             exchange + SEBI, stamp 0.002% on buys; brokerage Rs 20 an order; slippage 0.05% a fill (stressed 0.15%).
  verdict    a side passes when it is profitable after costs in the development, validation and out-of-sample splits
             (stressed too), beats NIFTY 50's CAGR, keeps its drawdown within 15% (long) / 20% (short) and at least
             ROBUST_MIN of the ROBUST_COUNTED neighbours pass. The short verdict gates `futures.short_real`.
"""

import json
import threading
from dataclasses import asdict, replace
from datetime import date
from typing import Optional

from backend.config import AppConfig, FuturesConfig
from backend.database.models import get_db, now_ist
from backend.engine import trend_breakout as tb
from backend.futures import candles as fc
from backend.futures import eligibility, instruments
from backend.logging_config.logger import setup_logger

logger = setup_logger("futures")

SLIP_PCT = 0.05                     # a fill
STRESS_SLIP_PCT = 0.15              # stressed: +0.10%
BROKERAGE = 20.0                    # an order
SHORT_TIME_STOP = 10                # sessions
SHORT_RESULTS_GUARD = 2             # no short within this many sessions before results
PIVOT = 2                           # a 5-bar pivot high: higher than the 2 bars either side
DD_CAP = {"long": 15.0, "short": 20.0}
ROBUST_MIN = 5                      # neighbours that must pass ...
ROBUST_COUNTED = 7                  # ... of these (stop cap 4/6, lot cap 5/7 L, roll T-1/T-3, +0.3% slippage)
SKIP_KEYS = ("lot", "risk", "slots", "ban", "results", "notional", "cash", "heat", "oi", "held", "margin", "daily_loss",
             "no_bar")
EXIT_KEYS = ("stop", "trail", "time", "rule", "roll_exit", "end")
MIN_FUT_BARS = 20                   # a stock needs this many futures bars before it can be traded (ATR warm-up)

_status = {"state": "idle", "message": "", "at": None}
_lock = threading.Lock()


def status() -> dict:
    return dict(_status)


# --- costs -----------------------------------------------------------------------------------------------

def futures_charges(value: float, sell: bool, brokerage: float = BROKERAGE) -> float:
    """Charges of one futures order on `value` (price x lot x lots): STT 0.02% on a sell, exchange 0.00173%, SEBI
    0.0001%, GST 18% on brokerage + exchange + SEBI, stamp 0.002% on a buy, brokerage Rs 20."""
    stt = value * 0.02 / 100 if sell else 0.0
    exch = value * 0.00173 / 100
    sebi = value * 0.0001 / 100
    gst = (brokerage + exch + sebi) * 0.18
    stamp = 0.0 if sell else value * 0.002 / 100
    return stt + exch + sebi + gst + stamp + brokerage


def fill_price(px: float, buying: bool, slip: float) -> float:
    return px * (1 + slip / 100) if buying else px * (1 - slip / 100)


# --- indicators --------------------------------------------------------------------------------------------

def atr14(rows: list[dict]) -> list:
    if not rows:
        return []
    c, h, lo = [r["close"] for r in rows], [r["high"] for r in rows], [r["low"] for r in rows]
    tr = [h[0] - lo[0]] + [max(h[i], c[i - 1]) - min(lo[i], c[i - 1]) for i in range(1, len(c))]
    return tb.sma(tr, 14)


def pivot_highs(highs: list[float], k: int = PIVOT) -> tuple[list, list]:
    """(last confirmed pivot high as of each bar, the one before it). A pivot at j (higher than the k bars either
    side) is known at j + k, so neither list looks ahead."""
    last, prev = [None] * len(highs), [None] * len(highs)
    a = b = None
    for j in range(len(highs)):
        p = j - k
        if p >= k and all(highs[p] > highs[p - m] and highs[p] > highs[p + m] for m in range(1, k + 1)):
            a, b = highs[p], a
        last[j], prev[j] = a, b
    return last, prev


def short_indicators(rows: list[dict]) -> dict:
    lo = [r["low"] for r in rows]
    last, prev = pivot_highs([r["high"] for r in rows])
    return {"lo20_prev": [None] + tb.rolling(lo, 20, min)[:-1], "ph_last": last, "ph_prev": prev}


# --- data --------------------------------------------------------------------------------------------------

def load(config: AppConfig) -> dict:
    """The cash backtest's data (`trend_breakout.load`) plus, for every underlying with a stored future under the
    universe lot cap, its continuous futures rows on the same calendar, ATR(14), the day's OI, the expiry calendar
    with the roll sessions, the roll jumps and the ban / results history. {error} when nothing can run."""
    cfg = config.futures
    data = tb.load(config)
    if "error" in data:
        return data
    cal, S = data["cal"], data["S"]
    pos_of = {d: i for i, d in enumerate(cal)}
    notes: list[str] = []
    uni = instruments.universe(cfg.universe_lot_cap)
    if not uni:
        return {"error": "No stock futures contracts stored yet (run the futures data step)."}
    F, missing, first_fut = {}, [], None
    for u in uni:
        sym = u["symbol"]
        if sym not in S:
            missing.append(sym)
            continue
        rows = [r for r in fc.get(sym) if r["date"] in pos_of and r["close"] > 0]
        if len(rows) < MIN_FUT_BARS:
            missing.append(sym)
            continue
        F[sym] = {"rows": rows, "atr": atr14(rows), "at": {pos_of[r["date"]]: j for j, r in enumerate(rows)},
                  "lot_size": int(u["lot_size"] or 0), "contract": {"lot_size": int(u["lot_size"] or 0),
                                                                    "tradingsymbol": u["tradingsymbol"], "oi_rank": None},
                  "jumps": {j["expiry"]: j for j in fc.roll_jump(sym)}}
        first_fut = rows[0]["date"] if first_fut is None else min(first_fut, rows[0]["date"])
    if not F:
        return {"error": "No continuous futures candles stored for the eligible universe yet (run the futures data step)."}
    notes.append("Lot sizes: the current contract's lot size is used for the whole history (historical lot sizes are "
                 "not stored).")
    if missing:
        notes.append(f"{len(missing)} futures names without enough cash or futures candles were left out: "
                     + ", ".join(sorted(missing)[:12]) + (" ..." if len(missing) > 12 else ""))
    for sym in F:
        F[sym]["short"] = short_indicators(S[sym]["rows"])
    # the first tested session: after the cash warm-up and MIN_FUT_BARS futures bars
    first = max(config.breakout.warmup_sessions, pos_of[first_fut] + MIN_FUT_BARS)
    if first >= len(cal) - 60:
        return {"error": f"Only {max(0, len(cal) - first)} sessions with futures candles: need 60+."}
    exps = fc.expiry_days(cal[first - 10], cal[-1])
    expiries = []
    for x in exps:                                      # the expiry's session (the last session at or before it)
        k = max((i for i, d in enumerate(cal) if d <= x), default=None)
        if k is not None:
            expiries.append({"expiry": x, "i": k})
    with get_db() as conn:
        ban_rows = conn.execute("SELECT date, symbol FROM fo_ban WHERE date >= ? ORDER BY date", (cal[first],)).fetchall()
        res_rows = conn.execute("SELECT symbol, date FROM corp_events WHERE kind = 'results' AND date >= ? AND date <= ?",
                                (cal[first], cal[-1])).fetchall()
    ban: dict[str, set] = {}
    for r in ban_rows:
        ban.setdefault(r["date"], set()).add(r["symbol"])
    results: dict[str, list] = {}
    for r in res_rows:
        results.setdefault(r["symbol"], []).append(r["date"])
    if not ban:
        notes.append("Ban list: no stored MWPL lists for the tested range; the ban rule was skipped.")
    else:
        notes.append(f"Ban list: stored lists from {min(ban)} are applied (each list for the sessions up to the next).")
    if not results:
        notes.append("Results calendar: no results dates stored for the tested range; the results rules were skipped.")
    notes.append("OI rank: ranked each day on the stored futures open interest of the eligible universe.")
    notes.append("Rolls: charges at T-%d; the basis (the continuous series' jump after the expiry net of the cash move) "
                 "is booked when the position is still held after the expiry." % cfg.roll_days)
    notes.append("Next-month OI is not stored: the 'exit when the next month lacks OI' rule is not tested.")
    data.update({"F": F, "first": first, "expiries": expiries, "ban": ban, "ban_dates": sorted(ban), "results": results,
                 "notes": notes, "index_m50": tb.sma(data["index_close"], 50), "sig_cache": {}})
    return data


def oi_ranks(data: dict, i: int) -> dict:
    """{symbol: rank} by the day's futures open interest (1 = most), cached per day."""
    cache = data.setdefault("oi_cache", {})
    if i not in cache:
        xs = sorted(((F["rows"][F["at"][i]]["oi"], sym) for sym, F in data["F"].items() if i in F["at"]), reverse=True)
        cache[i] = {sym: k + 1 for k, (_, sym) in enumerate(xs)}
    return cache[i]


def ban_for(data: dict, d: str) -> Optional[set]:
    """The ban list in force on `d`: the latest stored list at most 7 days old; None when the rule cannot be applied."""
    dates = data["ban_dates"]
    if not dates:
        return None
    use = None
    for x in dates:
        if x <= d:
            use = x
        else:
            break
    if use is None or (date.fromisoformat(d) - date.fromisoformat(use)).days > 7:
        return None
    return data["ban"][use]


def results_ahead(data: dict, sym: str, d: str, sessions: int, cal: list, i: int) -> Optional[dict]:
    """{date, sessions} of the stock's results within `sessions` sessions after day i (False when the calendar has
    none for the range, so the rule is skipped)."""
    if not data["results"]:
        return False
    horizon = cal[min(i + sessions, len(cal) - 1)]
    for x in data["results"].get(sym, []):
        if d <= x <= horizon:
            n = sum(1 for k in range(i + 1, len(cal)) if cal[k] <= x)
            return {"date": x, "sessions": n}
    return False


# --- the signals: decided at the close of calendar day i ----------------------------------------------------

def long_signals(data: dict, i: int, p) -> list[tuple]:
    """E+'s ranked buys among the futures names, [(symbol, cash_stop)], from the full cash universe (same pool rule),
    cached per day; the caller drops held names and gates by the market filter."""
    key = ("L", i)
    if key not in data["sig_cache"]:
        _, ranked, _, _ = tb.decide(i, data["S"], set(), True, p)
        data["sig_cache"][key] = [(sym, stop) for sym, stop in ranked if sym in data["F"]]
    return data["sig_cache"][key]


def short_signals(data: dict, i: int, p) -> list[tuple]:
    """E-'s ranked shorts among the futures names, [(symbol, swing_high)], weakest 6-month return first."""
    key = ("S", i)
    if key in data["sig_cache"]:
        return data["sig_cache"][key]
    S = data["S"]
    cands = []
    for sym in tb.liquid(S, i, p.top, p.min_price):
        if sym not in data["F"]:
            continue
        s, x = S[sym], data["F"][sym]["short"]
        j = s["at"][i]
        c = s["rows"][j]["close"]
        lo20, m50, m200 = x["lo20_prev"][j], s["ind"]["sma50"][j], s["ind"]["sma200"][j]
        ph, ph_prev = x["ph_last"][j], x["ph_prev"][j]
        if None in (lo20, m50, m200, ph, ph_prev):
            continue
        if c < lo20 and c < m50 and c < m200 and ph < ph_prev and ph > c:
            cands.append((s["ind"]["ret126"][j] or 0, sym, ph))
    out = [(sym, ph) for _, sym, ph in sorted(cands)]
    data["sig_cache"][key] = out
    return out


def long_exit(S: dict, sym: str, i: int, p) -> bool:
    s = S[sym]
    j = s["at"].get(i)
    if j is None:
        return False
    m = s["ind"][f"sma{p.exit_sma}"][j]
    return bool(m) and s["rows"][j]["close"] < m


def market(data: dict, i: int, side: str, p, cfg: FuturesConfig) -> tuple[bool, int]:
    """(new entries allowed, open-position cap) at day i's close. Long: E+'s half filter (half the slots between the
    averages, none under the 50-day). Short: the filter inverted, NIFTY 50 under its 50-day average."""
    if side == "long":
        on, cap = tb.slots(i, data["index_ok"], data["filters"], p)
        frac = data["filters"][p.regime_mode][i] if p.regime and p.regime_mode != "sma200" else (1.0 if on else 0.0)
        return on, (cfg.max_pos if frac >= 1 else max(1, cfg.max_pos // 2)) if on else 0
    m50 = data["index_m50"][i]
    on = m50 is not None and data["index_close"][i] < m50
    return on, cfg.max_pos if on else 0


def stop_for(side: str, entry: float, atr: Optional[float], hint: Optional[float], p, cfg: FuturesConfig) -> float:
    """The futures stop: atr_mult x ATR(14, futures) from the entry, within max_stop_pct of it; a short's stop also
    never sits over its swing high (`hint`)."""
    cap = entry * cfg.max_stop_pct / 100
    dist = min(p.atr_mult * atr, cap) if atr else cap
    if side == "long":
        return entry - dist
    stop = entry + dist
    return min(stop, hint) if hint and hint > entry else stop


# --- the account -------------------------------------------------------------------------------------------

def _reason_key(reason: str) -> str:
    r = reason.lower()
    for key, start in (("lot", "lot too big"), ("risk", "risk"), ("oi", "oi rank"), ("ban", "in ban"), ("results", "results"),
                       ("held", "held"), ("slots", "slots"), ("notional", "notional"), ("cash", "cash"), ("margin", "margin"),
                       ("heat", "heat")):
        if r.startswith(start):
            return key
    return "other"


def simulate(data: dict, cfg: FuturesConfig, p, side: str, slip: float, first: Optional[int] = None,
             lots: int = 1, close_end: bool = True) -> dict:
    """One side's futures book from calendar index `first`: {eq, eq_gross, trades, rolls, skipped, no_slot_days,
    margin_worst_pct, cash_floor_held, loss_days}. p: the breakout rules (atr_mult, exit_sma, late trail, top...);
    cfg: the futures rules (capital, slots, caps, roll_days...)."""
    cal, S, F = data["cal"], data["S"], data["F"]
    first = data["first"] if first is None else first
    sign = 1 if side == "long" else -1
    cash, held, trades, rolls = cfg.capital, {}, [], []
    pend_exit, pend_entry, cap_today, blocked = {}, [], cfg.max_pos, False
    eq, eq_gross, costs_cum = [], [], 0.0
    skipped = {k: 0 for k in SKIP_KEYS}
    no_slot_days, loss_days, margin_worst, floor_held = 0, 0, 0.0, True
    roll_at = {x["i"] - cfg.roll_days: x for x in data["expiries"]}          # roll session -> expiry
    jump_at = {x["i"] + 1: x for x in data["expiries"]}                      # the first session after the expiry
    bar = lambda sym, i: F[sym]["rows"][F[sym]["at"][i]] if i in F[sym]["at"] else None

    def unrealised(pos):
        return sign * (pos["last"] - pos["px"]) * pos["qty"]

    def equity():
        return cash + sum(pos["margin"] + unrealised(pos) for pos in held.values())

    def open_lots():
        return [{"symbol": s, "lot_value": pos["px"] * pos["qty"], "margin": pos["margin"],
                 "risk": abs(pos["px"] - pos["stop"]) * pos["qty"]} for s, pos in held.items()]

    def close_pos(sym, px, d, why):
        nonlocal cash, costs_cum
        pos = held.pop(sym)
        f = fill_price(px, side == "short", slip)                           # a long sells, a short buys back
        ch = futures_charges(f * pos["qty"], sell=(side == "long"))
        slip_cost = abs(px - f) * pos["qty"]
        gross = sign * (px - pos["px"]) * pos["qty"]
        cost = pos["cost"] + ch + slip_cost
        cash += pos["margin"] + gross - ch - slip_cost
        costs_cum += ch + slip_cost
        trades.append({"sym": sym, "side": side, "in": pos["d"], "out": d, "lots": lots, "lot_size": pos["lot_size"],
                       "qty": pos["qty"], "entry": round(pos["px"], 2), "exit": round(px, 2), "stop": round(pos["stop"], 2),
                       "days": len(pos["days"]), "best": round(pos["best"], 2), "rolls": pos["rolls"],
                       "contract": pos["contract"], "gross": round(gross, 2), "costs": round(cost, 2),
                       "net": round(gross - cost, 2), "why": why})

    for i in range(first, len(cal)):
        d = cal[i]
        for sym, why in list(pend_exit.items()):                               # 1) exits at the open
            b = bar(sym, i)
            if sym in held and b:
                close_pos(sym, b["open"], d, why)
        pend_exit = {}
        slots_hit = False
        for k, (sym, hint) in enumerate(pend_entry):                          # 2) entries at the open, one lot each
            if sym in held:
                continue
            b = bar(sym, i)
            j = F[sym]["at"].get(i)
            if not b or j is None:
                skipped["no_bar"] += 1
                continue
            if cap_today < cfg.max_pos and len(held) >= cap_today:   # the half filter's cap (check() knows max_pos)
                skipped["slots"] += 1
                slots_hit = True
                continue
            atr = F[sym]["atr"][j - 1] if j >= 1 else None
            stop = stop_for(side, b["open"], atr, hint, p, cfg)
            contract = {**F[sym]["contract"], "oi_rank": oi_ranks(data, i - 1).get(sym)}
            banlist = ban_for(data, d)
            res = results_ahead(data, sym, d, cfg.results_guard_sessions, cal, i)
            chk = eligibility.check(cfg, sym, side, b["open"], stop, set(), set(held), open_lots(), today=d, cash=cash,
                                    contract=contract, banned=False if banlist is None else sym in banlist,
                                    results=res, lots=lots)
            if not chk["ok"]:
                key = _reason_key(chk["reasons"][0])
                skipped[key] = skipped.get(key, 0) + 1
                if key == "slots":
                    slots_hit = True
                continue
            qty = chk["lot_size"]
            f = fill_price(b["open"], side == "long", slip)
            ch = futures_charges(f * qty, sell=(side == "short"))
            slip_cost = abs(f - b["open"]) * qty
            cash -= chk["margin_est"] + ch + slip_cost
            costs_cum += ch + slip_cost
            # entered from the roll session on: the next month already (no roll for that expiry)
            on_next = next((x["expiry"] for x in data["expiries"] if x["i"] - cfg.roll_days <= i <= x["i"]), None)
            held[sym] = {"qty": qty, "lot_size": F[sym]["lot_size"], "px": b["open"], "d": d, "i": i, "stop": stop,
                         "last": b["open"], "best": b["open"], "margin": chk["margin_est"], "cost": ch + slip_cost,
                         "days": {d}, "rolls": 0, "skip_roll": on_next, "contract": "next" if on_next else "near"}
        if slots_hit:
            no_slot_days += 1
        pend_entry = []
        if i in jump_at:                                                       # 3a) the basis after an expiry
            x = jump_at[i]
            for sym, pos in held.items():
                jmp = F[sym]["jumps"].get(x["expiry"])
                cb = S[sym]["at"].get(i)
                if not jmp or cb is None or cb == 0:
                    continue
                cprev, cnow = S[sym]["rows"][cb - 1], S[sym]["rows"][cb]
                cash_pct = (cnow["open"] / cprev["close"] - 1) * 100 if cprev["close"] else 0.0
                basis = jmp["prev_close"] * ((jmp["jump_pct"] or 0) - cash_pct) / 100 * pos["qty"] * sign
                pos["cost"] += basis                                           # the continuous series' spurious jump
                cash -= basis
                costs_cum += basis
                rolls.append({"sym": sym, "date": d, "expiry": x["expiry"], "kind": "basis", "cost": round(basis, 2)})
        for sym in list(held):                                                 # 3b) stops during the day, then the close
            b = bar(sym, i)
            if not b:
                continue
            pos = held[sym]
            pos["days"].add(d)
            if side == "long" and b["low"] <= pos["stop"]:
                close_pos(sym, min(b["open"], pos["stop"]), d, "trail" if pos.get("trailed") else "stop")
                continue
            if side == "short" and b["high"] >= pos["stop"]:
                close_pos(sym, max(b["open"], pos["stop"]), d, "stop")
                continue
            pos["last"] = b["close"]
            if side == "long":
                if p.late_at:
                    tb.raise_late(pos, b["close"], p)
                if i + 1 < len(cal) and long_exit(S, sym, i, p):
                    pend_exit[sym] = "rule"
            else:
                pos["best"] = min(pos["best"], b["close"])
                if i + 1 < len(cal) and len(pos["days"]) >= SHORT_TIME_STOP:
                    pend_exit[sym] = "time"
        if i in roll_at and i + 1 < len(cal):                                   # 3c) the roll at T-roll_days
            x = roll_at[i]
            for sym, pos in held.items():
                if pos["skip_roll"] == x["expiry"]:
                    continue
                if sym in pend_exit:
                    pend_exit[sym] = "roll_exit"
                    continue
                px = pos["last"]
                winning = (px - pos["px"]) * sign > 0
                if cfg.roll_mode == "never" or (cfg.roll_mode == "winners" and not winning):
                    pend_exit[sym] = "roll_exit"                               # close at T-N instead of paying another month
                    continue
                f_out, f_in = fill_price(px, side == "short", slip), fill_price(px, side == "long", slip)
                ch = futures_charges(f_out * pos["qty"], sell=(side == "long")) + futures_charges(f_in * pos["qty"], sell=(side == "short"))
                slip_cost = (abs(px - f_out) + abs(f_in - px)) * pos["qty"]
                cost = ch + slip_cost
                cash -= cost
                costs_cum += cost
                pos["cost"] += cost
                pos["rolls"] += 1
                rolls.append({"sym": sym, "date": d, "expiry": x["expiry"], "kind": "roll", "cost": round(cost, 2)})
        e = equity()
        eq.append(e)
        eq_gross.append(e + costs_cum)
        margin_used = sum(pos["margin"] for pos in held.values())
        margin_worst = max(margin_worst, margin_used / cfg.capital * 100 if cfg.capital else 0.0)
        if cash < cfg.cash_floor:
            floor_held = False
        prev = eq[-2] if len(eq) > 1 else cfg.capital
        blocked = (prev - e) >= cfg.daily_loss_pct / 100 * cfg.capital
        if blocked:
            loss_days += 1
        if i + 1 < len(cal):                                                   # 4) decide at the close
            on, cap_today = market(data, i, side, p, cfg)
            sigs = long_signals(data, i, p) if side == "long" else short_signals(data, i, p)
            sigs = [(sym, h) for sym, h in sigs if sym not in held]
            if side == "short" and data["results"]:
                keep = []
                for sym, h in sigs:
                    if results_ahead(data, sym, d, SHORT_RESULTS_GUARD, cal, i):
                        skipped["results"] += 1
                    else:
                        keep.append((sym, h))
                sigs = keep
            if not on:
                pend_entry = []
            elif blocked:
                skipped["daily_loss"] += len(sigs)
                pend_entry = []
            else:
                pend_entry = sigs
    if close_end:
        for sym in list(held):
            close_pos(sym, held[sym]["last"], cal[-1], "end")
        eq[-1] = cash
        eq_gross[-1] = cash + costs_cum
    return {"eq": eq, "eq_gross": eq_gross, "trades": trades, "rolls": rolls, "skipped": skipped, "no_slot_days": no_slot_days,
            "margin_worst_pct": round(margin_worst, 1), "cash_floor_held": floor_held, "loss_days": loss_days}


# --- performance ---------------------------------------------------------------------------------------------

def worst_month(eq: list, dates: list, capital: float) -> Optional[float]:
    """The worst calendar month's return, %."""
    if not eq:
        return None
    months, last = {}, capital
    for e, d in zip(eq, dates):
        months.setdefault(d[:7], []).append(e)
    out = []
    for m in sorted(months):
        out.append((months[m][-1] / last - 1) * 100 if last else 0.0)
        last = months[m][-1]
    return round(min(out), 2) if out else None


def _side_stats(sim: dict, capital: float, n: int, dates: list) -> dict:
    eq, eqg, trades = sim["eq"], sim["eq_gross"], sim["trades"]
    won = [t for t in trades if t["net"] > 0]
    lost = -sum(t["net"] for t in trades if t["net"] <= 0)
    _, cagr, dd = tb.perf(eq, capital, n)
    nt = max(len(trades), 1)
    return {"cagr": round(cagr, 2), "net": round(eq[-1] - capital, 2), "gross": round(eqg[-1] - capital, 2),
            "costs": round(eqg[-1] - eq[-1], 2),
            "rolls": {"n": sum(r["kind"] == "roll" for r in sim["rolls"]), "cost": round(sum(r["cost"] for r in sim["rolls"]), 2),
                      "basis": round(sum(r["cost"] for r in sim["rolls"] if r["kind"] == "basis"), 2)},
            "trades": len(trades), "win_rate": round(100 * len(won) / nt, 1),
            "avg_win": round(sum(t["net"] for t in won) / len(won), 2) if won else None,
            "avg_loss": round(-lost / (len(trades) - len(won)), 2) if len(trades) > len(won) else None,
            "profit_factor": round(sum(t["net"] for t in won) / lost, 2) if lost else None,
            "drawdown": round(dd, 2), "worst_month": worst_month(eq, dates, capital),
            "margin_worst_pct": sim["margin_worst_pct"], "cash_floor_held": sim["cash_floor_held"],
            "skipped": sim["skipped"], "no_slot_days": sim["no_slot_days"], "loss_days": sim["loss_days"],
            "exits": {k: sum(t["why"] == k for t in trades) for k in EXIT_KEYS},
            "avg_hold": round(sum(t["days"] for t in trades) / nt, 1)}


def _splits(eq, seq, close, cal, first, n, capital):
    out = []
    for name, a, b in tb.periods(n):
        base = lambda xs: xs[a - 1] if a else capital
        out.append({"name": name, "from": cal[first + a], "to": cal[first + b - 1],
                    "net": round((eq[b - 1] / base(eq) - 1) * 100, 2), "stress": round((seq[b - 1] / base(seq) - 1) * 100, 2),
                    "nifty": round((close[first + b - 1] / close[first + a - 1] - 1) * 100, 2)})
    return out


def _years(eq, close, cal, first, capital):
    years = {}
    for i in range(first, len(cal)):
        years.setdefault(cal[i][:4], []).append(i)
    return [{"year": y, "net": round((eq[ix[-1] - first] / (eq[ix[0] - first - 1] if ix[0] > first else capital) - 1) * 100, 2),
             "nifty": round((close[ix[-1]] / close[ix[0] - 1] - 1) * 100, 2)} for y, ix in years.items()]


def evaluate(data: dict, cfg: FuturesConfig, p, side: str, nifty_cagr: float, slip: float = SLIP_PCT,
             stress: float = STRESS_SLIP_PCT, lots: int = 1) -> dict:
    """One neighbour at normal and stressed slippage: ok when every split is profitable both ways and the CAGR beats
    NIFTY's (the cash backtest's rule)."""
    n = len(data["cal"]) - data["first"]
    a = simulate(data, cfg, p, side, slip, lots=lots)
    b = simulate(data, cfg, p, side, stress, lots=lots)
    _, cagr, dd = tb.perf(a["eq"], cfg.capital, n)
    pn, ps = tb.period_returns(a["eq"], cfg.capital, n), tb.period_returns(b["eq"], cfg.capital, n)
    return {"trades": len(a["trades"]), "cagr": round(cagr, 2), "stress_cagr": round(tb.perf(b["eq"], cfg.capital, n)[1], 2),
            "drawdown": round(dd, 2), "periods": [round(x * 100, 2) for x in pn], "stress_periods": [round(x * 100, 2) for x in ps],
            "ok": all(x > 0 for x in pn + ps) and cagr > nifty_cagr}


def neighbours(cfg: FuturesConfig) -> list[tuple]:
    """(label, cfg, slip, lots, counted): the counted neighbours first (ROBUST_COUNTED of them), then the ones shown
    for information: 3 lots, no roll (every position closes at T-N), roll winners only (a position below its entry
    closes at T-N, one above it rolls); walk-forward is computed from the base run."""
    out = [(f"stop cap {v:g}%", replace(cfg, max_stop_pct=v), None, 1, True) for v in (4.0, 6.0)]
    out += [(f"lot cap {eligibility.lakh(v, True)}", replace(cfg, max_lot_value=v), None, 1, True) for v in (500000.0, 700000.0)]
    out += [(f"roll T-{v}", replace(cfg, roll_days=v), None, 1, True) for v in (1, 3)]
    out += [("slippage +0.3%", cfg, SLIP_PCT + 0.3, 1, True)]
    out += [("3 lots", cfg, None, 3, False)]
    out += [("no roll (close at T-%d)" % cfg.roll_days, replace(cfg, roll_mode="never"), None, 1, False)]
    out += [("roll winners only", replace(cfg, roll_mode="winners"), None, 1, False)]
    return out


def _side(data: dict, cfg: FuturesConfig, p, side: str, nifty_cagr: float) -> dict:
    cal, close, first = data["cal"], data["index_close"], data["first"]
    n = len(cal) - first
    dates = cal[first:]
    _status["message"] = f"{side}: simulating {n} sessions, {len(data['F'])} futures names"
    a = simulate(data, cfg, p, side, SLIP_PCT)
    b = simulate(data, cfg, p, side, STRESS_SLIP_PCT)
    out = _side_stats(a, cfg.capital, n, dates)
    out["stress"] = {k: v for k, v in _side_stats(b, cfg.capital, n, dates).items() if k in ("cagr", "net", "costs", "drawdown", "trades")}
    step = max(1, n // 400)
    ix = list(range(0, n, step)) + ([n - 1] if (n - 1) % step else [])
    out["curve"] = {"dates": [dates[k] for k in ix], "net": [round(a["eq"][k]) for k in ix], "stress": [round(b["eq"][k]) for k in ix]}
    out["splits"] = _splits(a["eq"], b["eq"], close, cal, first, n, cfg.capital)
    out["years"] = _years(a["eq"], close, cal, first, cfg.capital)
    rob = []
    nb = neighbours(cfg)
    for k, (label, q, slip, lots, counted) in enumerate(nb, 1):
        _status["message"] = f"{side}: robustness {k} of {len(nb) + 1}: {label}"
        ev = evaluate(data, q, p, side, nifty_cagr, slip=slip or SLIP_PCT, stress=(slip or SLIP_PCT) + 0.10, lots=lots)
        rob.append({"label": label, "counted": counted, **ev})
    pos_years = sum(y["net"] > 0 for y in out["years"])
    rob.append({"label": "walk-forward (years)", "counted": False, "cagr": out["cagr"], "trades": out["trades"],
                "drawdown": out["drawdown"], "ok": bool(out["years"]) and pos_years * 2 > len(out["years"]),
                "detail": f"{pos_years} of {len(out['years'])} years positive"})
    out["robust"] = rob
    out["robust_pass"] = sum(r["ok"] for r in rob if r["counted"])
    out["robust_of"] = sum(r["counted"] for r in rob)
    out["ok_splits"] = all(x["net"] > 0 and x["stress"] > 0 for x in out["splits"])
    out["beats_nifty"] = out["cagr"] > nifty_cagr
    reasons = []
    if not out["ok_splits"]:
        reasons.append("not profitable after costs in every split (normal and stressed)")
    if not out["beats_nifty"]:
        reasons.append(f"CAGR {out['cagr']:.1f}% does not beat NIFTY 50 {nifty_cagr:.1f}%")
    if out["drawdown"] > DD_CAP[side]:
        reasons.append(f"drawdown {out['drawdown']:.1f}% over {DD_CAP[side]:g}%")
    if out["robust_pass"] < ROBUST_MIN:
        reasons.append(f"robustness {out['robust_pass']} of {out['robust_of']} (need {ROBUST_MIN})")
    out["verdict"], out["reasons"] = not reasons, reasons
    out["trades_rows"] = sorted(a["trades"], key=lambda t: (t["in"], t["sym"]), reverse=True)
    out["roll_rows"] = a["rolls"]
    return out


def cash_same_signals(data: dict, config: AppConfig, cfg: FuturesConfig) -> dict:
    """E+ in the cash book at the futures capital on the futures names (the comparison column)."""
    cal, first = data["cal"], data["first"]
    n = len(cal) - first
    p = replace(tb.rules(config, "E"), capital=cfg.capital)
    S_sub = {sym: data["S"][sym] for sym in data["F"]}
    q = replace(p, top=max(p.top, len(S_sub)))
    eq, eqg, trades, funnel = tb.simulate(cal, S_sub, data["index_ok"], q, q.slippage_pct, first, filters=data["filters"])
    _, cagr, dd = tb.perf(eq, cfg.capital, n)
    return {"cagr": round(cagr, 2), "drawdown": round(dd, 2), "trades": len(trades), "net": round(eq[-1] - cfg.capital, 2),
            "costs": round(eqg[-1] - eq[-1], 2), "worst_month": worst_month(eq, cal[first:], cfg.capital),
            "no_slot_days": funnel["no_slot"],
            "note": "E+ at the futures capital on the futures names (max_pos %d, delivery charges)" % p.max_pos}


# --- the run ---------------------------------------------------------------------------------------------------

def build(config: AppConfig, data: Optional[dict] = None) -> dict:
    """The whole report (no persistence): {run, long, short, cash_same_signals, nifty, verdict} or {error}."""
    data = data or load(config)
    if "error" in data:
        return data
    cfg, p = config.futures, tb.rules(config, "E")
    cal, close, first = data["cal"], data["index_close"], data["first"]
    n = len(cal) - first
    nifty = [close[i] / close[first - 1] * cfg.capital for i in range(first, len(cal))]
    nifty_cagr = tb.perf(nifty, cfg.capital, n)[1]
    step = max(1, n // 400)
    ix = list(range(0, n, step)) + ([n - 1] if (n - 1) % step else [])
    out = {"run": {"from": cal[first], "to": cal[-1], "sessions": n, "capital": cfg.capital, "names": len(data["F"]),
                   "settings": {"futures": asdict(cfg), "breakout": {k: getattr(p, k) for k in ("atr_mult", "exit_sma", "hi_window",
                                                                                               "vol_mult", "top", "late_at", "late_pct")},
                                "slippage_pct": SLIP_PCT, "stress_slippage_pct": STRESS_SLIP_PCT, "brokerage": BROKERAGE,
                                "short_time_stop": SHORT_TIME_STOP, "short_results_guard": SHORT_RESULTS_GUARD,
                                "dd_cap": DD_CAP, "robust_min": ROBUST_MIN},
                   "notes": data["notes"], "missing": data.get("missing", [])},
           "nifty": {"cagr": round(nifty_cagr, 2), "drawdown": round(tb.perf(nifty, cfg.capital, n)[2], 2),
                     "return": round((nifty[-1] / cfg.capital - 1) * 100, 2), "curve": [round(nifty[k]) for k in ix]}}
    out["long"] = _side(data, cfg, p, "long", nifty_cagr)
    out["short"] = _side(data, cfg, p, "short", nifty_cagr)
    _status["message"] = "the same signals in the cash book"
    out["cash_same_signals"] = cash_same_signals(data, config, cfg)
    out["verdict"] = {"long": out["long"]["verdict"], "short": out["short"]["verdict"],
                      "reasons": {"long": out["long"]["reasons"], "short": out["short"]["reasons"]}}
    return out


def run(config: AppConfig) -> dict:
    """Backtest F end to end, saved as the latest `futures_runs` row. {status: ok|busy|no_data|failed, ...}."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        _status.update(state="running", message="loading the cash and futures candles", at=now_ist())
        out = build(config)
        if "error" in out:
            _status.update(state="idle", message=out["error"])
            return {"status": "no_data", "error": out["error"]}
        with get_db() as conn:
            conn.execute("INSERT INTO futures_runs (payload, ran_at) VALUES (?, ?)", (json.dumps(out), now_ist()))
        msg = " · ".join(f"{s}: {out[s]['trades']} trades, CAGR {out[s]['cagr']:.1f}%, {out[s]['robust_pass']}/"
                         f"{out[s]['robust_of']} robust, {'PASS' if out[s]['verdict'] else 'FAIL'}" for s in ("long", "short"))
        logger.info("Backtest F: %s (NIFTY 50 %.1f%%)", msg, out["nifty"]["cagr"])
        _status.update(state="idle", message=msg, at=now_ist())
        return {"status": "ok", "long": {"trades": out["long"]["trades"], "cagr": out["long"]["cagr"], "verdict": out["long"]["verdict"]},
                "short": {"trades": out["short"]["trades"], "cagr": out["short"]["cagr"], "verdict": out["short"]["verdict"]}}
    except Exception as exc:
        logger.exception("Backtest F run failed")
        _status.update(state="idle", message=f"failed: {exc}")
        return {"status": "failed", "error": str(exc)}
    finally:
        _lock.release()


def saved() -> Optional[dict]:
    """The latest run with its `ran_at` (None before the first)."""
    with get_db() as conn:
        r = conn.execute("SELECT payload, ran_at FROM futures_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    return {**json.loads(r["payload"]), "ran_at": r["ran_at"]}


def short_passed() -> bool:
    """Does the latest run's short verdict allow `futures.short_real`?"""
    s = saved()
    return bool(s and (s.get("verdict") or {}).get("short") is True)


def summary(config: AppConfig) -> dict:
    """GET /api/futures/backtest: the latest run without its trade lists, the progress and the rules."""
    s = saved()
    if s:
        s = {**s, "long": {k: v for k, v in s["long"].items() if k not in ("trades_rows", "roll_rows")},
             "short": {k: v for k, v in s["short"].items() if k not in ("trades_rows", "roll_rows")}}
    return {"built": bool(s), "progress": status(), "run": s,
            "rules": {"futures": asdict(config.futures), "slippage_pct": SLIP_PCT, "stress_slippage_pct": STRESS_SLIP_PCT,
                      "brokerage": BROKERAGE, "short_time_stop": SHORT_TIME_STOP, "short_results_guard": SHORT_RESULTS_GUARD,
                      "dd_cap": DD_CAP, "robust_min": ROBUST_MIN, "robust_of": ROBUST_COUNTED}}


def trades(side: str = "long") -> list[dict]:
    s = saved()
    return (s or {}).get(side, {}).get("trades_rows", []) if s else []


CSV_COLUMNS = ("sym", "side", "in", "out", "lots", "lot_size", "qty", "entry", "exit", "stop", "days", "best", "rolls",
               "contract", "gross", "costs", "net", "why")


def csv_rows(side: str = "long") -> list[list]:
    return [[t.get(k) for k in CSV_COLUMNS] for t in trades(side)]

