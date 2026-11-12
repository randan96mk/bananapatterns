"""📐 The stock futures book: the Trend Breakout signals traded on the near-month future, one lot a name, in four
virtual accounts that share one capital setting (`futures.capital`): paper_long, real_long, paper_short, real_short.
Paper and Real of a side run from the SAME plan; a side's Paper switch fills it from the daily futures candles, its
Real switch sends it through backend/futures/sender.py (the futures deployment's webhook) and books the fill like the cash Real.

  evening (after the futures data step)  each account is marked at the futures close (paper fills the plan at the
                      day's open from candles_fut_daily, checks stops on the day's low / high, counts the short time
                      stop; real corrects its morning fills to the open), the E+ late trail raises long stops, then
                      the next open's plan is made per side: long = `trend_breakout.decide` on the cash candles under
                      the half market filter, short = Backtest F's E- signals under the inverted filter; every
                      candidate goes through `eligibility.check` (lot cap, risk cap, OI rank, ban, results, held in
                      cash / futures, slots, notional, cash floor, margin, heat) and the book's own guards (daily loss,
                      kill, filter); a rejected signal is kept in `waiting` with its reasons.
  preview_time        the morning dry run (same shapes as real.morning): the plan with per-order skips (you, the
                      results guard, Telegram `fut skip SYM` / `fut hold`), EXITs cannot be skipped.
  send_time           the real sides' orders go through the sender once each (a failure is logged, never retried; a
                      timeout is logged `unknown · check`: the order may have gone in, it is never re-sent).
  every 5 min         stops on the futures quote (NFO:<contract>): a long at or under its stop (or the day's low), a
                      short at or over it (or the day's high) exits; paper closes at the price, real sends EXIT.
  roll day (T-2)      9:20: EXIT the near month, then (after the exit is booked) the entry on the next month, logged as
                      one roll with its cost (charges + basis); a position whose exit rule fired is exited instead;
                      never held into expiry (a forced exit at T-1 when the roll failed).
Guards: daily loss limit (no new entries), notional 2.5x, cash floor, heat, margin 70% warn / 80% block, the enable
switch (`futures.enabled`) and the kill switch (`futures.killed`: when flipped on with a real side on, the sender is
asked for kill_switch=1 once). `futures.short_real` cannot be on while the short backtest verdict is False.
Nothing here reads broker positions; Kite is used for quotes only."""

import json
import re
import threading
import uuid
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from backend.config import AppConfig
from backend.database.models import get_db, get_setting, now_ist, set_setting
from backend.engine import trend_breakout as tb
from backend.futures import backtest as fb
from backend.futures import ban as fo_ban
from backend.futures import candles as fc
from backend.futures import eligibility, instruments, sender
from backend.logging_config.logger import setup_logger

logger = setup_logger("futures")

_lock = threading.Lock()

SIDES = ("long", "short")
MODES = ("paper", "real")
ACCOUNTS = ("paper_long", "real_long", "paper_short", "real_short")
TAG = {"paper_long": "paper · long", "real_long": "real · long", "paper_short": "paper · short", "real_short": "real · short"}
ENTRY = {"long": "BUY", "short": "SELL"}          # the entry action per side (EXIT closes either)
MARGIN_WARN, MARGIN_BLOCK = 70.0, 80.0
ROLL_TIME = "09:20"
HAND = {"EDIT": "edited", "ADD": "added", "REMOVE": "removed"}
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def acct(side: str, mode: str) -> str:
    return f"{mode}_{side}"


def side_of(account: str) -> str:
    return account.split("_", 1)[1]


def mode_of(account: str) -> str:
    return account.split("_", 1)[0]


# --- switches, kill, enable -------------------------------------------------------------------------------

def killed() -> bool:
    with get_db() as conn:
        return get_setting(conn, "futures.killed") == "1"


def enabled() -> bool:
    with get_db() as conn:
        return get_setting(conn, "futures.enabled", "1") != "0"


def set_enabled(on: bool) -> None:
    with get_db() as conn:
        set_setting(conn, "futures.enabled", "1" if on else "0")


def switch(config: AppConfig, side: str, mode: str) -> bool:
    return bool(getattr(config.futures, f"{side}_{mode}"))


def real_live(config: AppConfig, side: str) -> bool:
    """May the side's real orders go out: its Real switch, the enable switch and the kill switch."""
    return switch(config, side, "real") and enabled() and not killed()


def primary(config: AppConfig, side: str) -> str:
    """The account a side's plan and guards are measured on: real when the side is real, else paper."""
    return acct(side, "real" if switch(config, side, "real") else "paper")


def set_killed(on: bool, config: AppConfig, kill_sender: Optional[Callable] = None) -> dict:
    """The kill switch. Flipped on while a side's Real switch is on: the sender is asked for kill_switch=1 once and
    the book marks itself killed (the record is kept in the state)."""
    kill_sender = kill_sender or sender.kill_switch
    was = killed()
    with get_db() as conn:
        set_setting(conn, "futures.killed", "1" if on else "0")
    out = {"killed": on, "sent": False}
    if on and not was and any(switch(config, s, "real") for s in SIDES):
        try:
            reply = kill_sender("futures kill switch")
            out.update(sent=True, reply=str(reply)[:120])
        except Exception as exc:
            out.update(sent=False, error=str(exc)[:200])
        with _lock:
            st = _state()
            if st:
                st["killed_at"] = now_ist()
                st.setdefault("notes", []).append({"day": now_ist()[:10], "text": f"kill switch ON · sender {out.get('reply') or out.get('error')}"})
                _save(st)
    return out


def set_switches(config: AppConfig, **flags) -> dict:
    """long_paper / long_real / short_paper / short_real / enabled. short_real cannot go on while the short
    backtest verdict is False (ValueError). Saved with the settings."""
    from backend.config import save_config
    if flags.get("short_real") and not config.futures.short_real and not fb.short_passed():
        raise ValueError("short side backtest has not passed")
    for k in ("long_paper", "long_real", "short_paper", "short_real"):
        if flags.get(k) is not None:
            setattr(config.futures, k, bool(flags[k]))
    if flags.get("enabled") is not None:
        set_enabled(bool(flags["enabled"]))
    save_config(config)
    return switches(config)


def switches(config: AppConfig) -> dict:
    return {"long_paper": config.futures.long_paper, "long_real": config.futures.long_real,
            "short_paper": config.futures.short_paper, "short_real": config.futures.short_real,
            "enabled": enabled(), "killed": killed(), "short_real_allowed": fb.short_passed()}


# --- state -----------------------------------------------------------------------------------------------

def _state() -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT payload FROM futures_state WHERE id = 1").fetchone()
    return json.loads(r[0]) if r else None


def has_account() -> bool:
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM futures_state WHERE id = 1").fetchone() is not None


def _save(st: dict) -> None:
    st["updated_at"] = now_ist()
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO futures_state (id, payload) VALUES (1, ?)", (json.dumps(st),))


def _new(config: AppConfig, day: str) -> dict:
    cap = float(config.futures.capital)
    return {"started": day, "started_at": now_ist(), "capital": cap, "deposits": [], "as_of": None, "decided": None,
            "accounts": {k: {"cash": cap, "charges": 0.0, "positions": {}, "day_start": cap} for k in ACCOUNTS},
            "plan": {"long": [], "short": []}, "waiting": {"long": [], "short": []}, "skips": {}, "unskipped": {},
            "rolls": [], "notes": [], "short_signals": [], "warnings": {}}


def _log(account: str, day: str, symbol: str, action: str, status: str, lots=None, contract: Optional[str] = None,
         reason: str = "", reply: str = "", price: Optional[float] = None, key: Optional[str] = None) -> None:
    key = key or f"{day}|{account}|{symbol}|{action}"
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO futures_orders (key, day, created_at, account, symbol, action, lots, contract, reason, "
                     "status, reply, price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (key, day, now_ist(), account, symbol, action, lots, contract, reason[:200], status, (reply or "")[:300], price))


def _logged(key: str) -> bool:
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM futures_orders WHERE key = ?", (key,)).fetchone() is not None


def _sign(side: str) -> int:
    return 1 if side == "long" else -1


def _qty(pos: dict) -> int:
    return int(pos["lots"]) * int(pos["lot_size"])


def _last(pos: dict) -> float:
    return pos.get("live") or pos["last"]


def _unrealised(pos: dict) -> float:
    return _sign(pos["side"]) * (_last(pos) - pos["entry"]) * _qty(pos)


def _value(a: dict) -> float:
    return a["cash"] + sum(p["margin_est"] + _unrealised(p) for p in a["positions"].values())


def _margin_used(a: dict) -> float:
    return sum(p["margin_est"] for p in a["positions"].values())


def _risk(pos: dict) -> float:
    return max(0.0, _sign(pos["side"]) * (_last(pos) - pos["stop"])) * _qty(pos)


def _open_lots(a: dict, skip: tuple = ()) -> list:
    return [{"symbol": s, "lot_value": p["entry"] * _qty(p), "margin": p["margin_est"], "risk": abs(p["entry"] - p["stop"]) * _qty(p)}
            for s, p in a["positions"].items() if s not in skip]


def _fut_bars(sym: str, day: Optional[str] = None, n: int = 40) -> list:
    """The last n stored futures bars on or before `day` (today without it): a replay never sees later bars."""
    day = day or now_ist()[:10]
    start = (date.fromisoformat(day) - timedelta(days=n * 2 + 30)).isoformat()
    return [b for b in fc.get(sym, start=start) if b["date"] <= day][-n:]


def _bar_on(bars: list, day: str) -> Optional[dict]:
    return next((b for b in bars if b["date"] == day), None)


def _contract_for(sym: str) -> Optional[dict]:
    return instruments.near(sym)


# --- the account's bookkeeping -------------------------------------------------------------------------------

def _open(st: dict, account: str, o: dict, px: float, day: str, est: bool) -> dict:
    """An entry booked: margin and charges leave the cash; the position holds the plan's contract and stop."""
    a = st["accounts"][account]
    qty = int(o["lots"]) * int(o["lot_size"])
    ch = fb.futures_charges(px * qty, sell=(o["side"] == "short"))
    margin = eligibility.margin_estimate(_cfg(st), px * qty)
    a["cash"] -= margin + ch
    a["charges"] += ch
    pos = {"symbol": o["symbol"], "side": o["side"], "lots": int(o["lots"]), "lot_size": int(o["lot_size"]),
           "contract": o.get("contract"), "token": o.get("token"), "expiry": o.get("expiry"), "entry": px, "stop": o["stop"],
           "best": px, "last": px, "margin_est": margin, "est": est, "manual_stop": False, "edits": [], "d": day, "days": 0,
           "charges": ch, "rolls": 0, "trailed": False}
    a["positions"][o["symbol"]] = pos
    return pos


def _close(st: dict, account: str, sym: str, px: float, day: str, why: str, est: bool = False) -> dict:
    a = st["accounts"][account]
    pos = a["positions"].pop(sym)
    qty = _qty(pos)
    ch = fb.futures_charges(px * qty, sell=(pos["side"] == "long"))
    gross = _sign(pos["side"]) * (px - pos["entry"]) * qty
    a["cash"] += pos["margin_est"] + gross - ch
    a["charges"] += ch
    t = {"symbol": sym, "side": pos["side"], "account": account, "book": mode_of(account), "lots": pos["lots"], "lot_size": pos["lot_size"],
         "qty": qty, "in": pos["d"], "out": day, "entry": round(pos["entry"], 2), "exit": round(px, 2), "stop": round(pos["stop"], 2),
         "days": pos.get("days", 0), "best": round(pos.get("best") or pos["entry"], 2), "rolls": pos.get("rolls", 0),
         "contract": pos.get("contract"), "gross": round(gross, 2), "costs": round(pos["charges"] + ch, 2),
         "net": round(gross - pos["charges"] - ch, 2), "why": why, "est": est}
    with get_db() as conn:
        conn.execute("INSERT INTO futures_trades (account, payload) VALUES (?, ?)", (account, json.dumps(t)))
    return t


_CFG: dict = {}


def _cfg(st: dict):
    """The futures config the book was last run with (set by every entry point)."""
    return _CFG.get("cfg") or AppConfig().futures


def _use(config: AppConfig) -> None:
    _CFG["cfg"] = config.futures


# --- evening: mark the day, then plan the next open ---------------------------------------------------------

def evening(config: AppConfig, data: Optional[dict] = None, today: Optional[str] = None) -> dict:
    """Mark every account at the futures close (paper fills the morning's plan at the open first), raise the long
    trail, note held-name warnings, plan the next open per side. Starts the book the first evening any switch is on;
    a day already processed is not processed again."""
    _use(config)
    with _lock:
        st = _state()
        if not st and not any(switch(config, s, m) for s in SIDES for m in MODES):
            return {"status": "off"}
        data = data or tb.load(config)
        if "error" in data:
            return {"status": "no_data", "error": data["error"]}
        cal = data["cal"]
        i = len(cal) - 1
        day = today or cal[i]
        if day in cal:
            i = cal.index(day)
        st = st or _new(config, day)
        if st.get("as_of") == day:
            return {"status": "done", "plan": st["plan"], "waiting": st["waiting"]}
        missed = [o for s in SIDES for o in st["plan"].get(s, []) if not o.get("sent") and switch(config, s, "real")]
        fills, closed = _mark(st, config, day)
        raised = _late_trail(st, data, day, tb.rules(config, "E"))
        warnings = _held_warnings(st, config, day)
        st["skips"], st["unskipped"] = {}, {}
        plan = _plan(st, data, i, config)
        st["as_of"] = day
        with get_db() as conn:
            for k, a in st["accounts"].items():
                eq = _value(a)
                a["day_start"] = round(eq, 2)
                conn.execute("INSERT OR REPLACE INTO futures_equity (account, day, equity, gross, nifty) VALUES (?, ?, ?, ?, ?)",
                             (k, day, round(eq, 2), round(eq + a["charges"], 2), data["index_close"][i]))
            set_setting(conn, "futures.trail_raised", json.dumps({"day": day, "raised": raised}))
        _save(st)
        return {"status": "ok", "as_of": day, "plan": st["plan"], "waiting": st["waiting"], "fills": fills, "closed": closed,
                "missed": missed, "raised": raised, "warnings": warnings, "short_signals": plan["short_signals"],
                "roll_day": st.get("roll_day")}


def _mark(st: dict, config: AppConfig, day: str) -> tuple:
    """Each account's day from the futures candles: paper fills the plan at the open, stops on the low / high,
    the close as the mark; real corrects estimated fills and exits to the open. Returns (fills, closed trades)."""
    cfg = config.futures
    fills, closed = [], []
    bars = {}
    syms = {s for a in st["accounts"].values() for s in a["positions"]} | {o["symbol"] for s in SIDES for o in st["plan"].get(s, [])}
    for s in syms:
        bars[s] = _bar_on(_fut_bars(s, day, 5), day)
    skips = (st.get("skips") or {}).get(day) or {}
    for k, a in st["accounts"].items():
        side, mode = side_of(k), mode_of(k)
        if mode == "paper" and switch(config, side, "paper") and st.get("decided") and st["decided"] < day:
            for o in sorted(st["plan"].get(side, []), key=lambda o: o["action"] != "EXIT"):
                b = bars.get(o["symbol"])
                if not b:
                    continue
                if o["action"] == "EXIT" and o["symbol"] in a["positions"]:
                    t = _close(st, k, o["symbol"], b["open"], day, o.get("why_key", "exit"))
                    closed.append({**t, "account": k})
                    _log(k, day, o["symbol"], "EXIT", "filled", o.get("lots"), o.get("contract"), o.get("why", ""), "paper fill at the open", b["open"])
                elif o["action"] == ENTRY[side] and o["symbol"] not in a["positions"]:
                    if o["symbol"] in skips:
                        _log(k, day, o["symbol"], o["action"], "skipped", o["lots"], o.get("contract"), o.get("why", ""),
                             f"skipped today · by {skips[o['symbol']]['by']}")
                        continue
                    if not _fits(st, k, o, cfg, b["open"]):
                        _log(k, day, o["symbol"], o["action"], "skipped", o["lots"], o.get("contract"), o.get("why", ""), "no room at the open")
                        continue
                    pos = _open(st, k, o, b["open"], day, est=False)
                    fills.append({"account": k, "symbol": o["symbol"], "side": side, "lots": pos["lots"], "price": round(b["open"], 2),
                                  "stop": round(pos["stop"], 2), "margin": pos["margin_est"]})
                    _log(k, day, o["symbol"], o["action"], "filled", o["lots"], o.get("contract"), o.get("why", ""), "paper fill at the open", b["open"])
        for sym, pos in list(a["positions"].items()):
            b = bars.get(sym)
            if not b:
                continue
            if mode == "real" and pos.get("est") and pos["d"] == day:
                _rebook(st, k, pos, b["open"])
            pos["days"] = pos.get("days", 0) + 1
            if side == "long" and b["low"] <= pos["stop"] and not (mode == "real"):
                t = _close(st, k, sym, min(b["open"], pos["stop"]), day, "trail" if pos.get("trailed") else "stop")
                closed.append({**t, "account": k})
                _log(k, day, sym, "EXIT", "filled", pos["lots"], pos.get("contract"), f"stop {pos['stop']:.2f} hit (day low {b['low']:.2f})", "paper", t["exit"])
                continue
            if side == "short" and b["high"] >= pos["stop"] and not (mode == "real"):
                t = _close(st, k, sym, max(b["open"], pos["stop"]), day, "stop")
                closed.append({**t, "account": k})
                _log(k, day, sym, "EXIT", "filled", pos["lots"], pos.get("contract"), f"stop {pos['stop']:.2f} hit (day high {b['high']:.2f})", "paper", t["exit"])
                continue
            pos["last"] = b["close"]
            pos.pop("live", None)
            if side == "short":
                pos["best"] = min(pos.get("best") or pos["entry"], b["close"])
        with get_db() as conn:                            # real morning exits booked at the price seen -> the day's open
            for r in conn.execute("SELECT id, payload FROM futures_trades WHERE account = ? AND json_extract(payload, '$.out') = ?", (k, day)):
                t = json.loads(r[1])
                b = bars.get(t["symbol"])
                if t.get("est") and b:
                    diff = _sign(t["side"]) * (b["open"] - t["exit"]) * t["qty"]
                    a["cash"] += diff
                    t.update(exit=round(b["open"], 2), gross=round(t["gross"] + diff, 2), net=round(t["net"] + diff, 2), est=False)
                    conn.execute("UPDATE futures_trades SET payload = ? WHERE id = ?", (json.dumps(t), r[0]))
    return fills, closed


def _fits(st: dict, account: str, o: dict, cfg, px: Optional[float] = None) -> bool:
    """At the fill: the slot, notional, cash floor, margin and heat guards against the account as it is then."""
    a = st["accounts"][account]
    chk = eligibility.check(cfg, o["symbol"], o["side"], px or o["entry"], o["stop"], set(), set(), _open_lots(a), cash=a["cash"],
                            contract={"lot_size": o["lot_size"], "tradingsymbol": o.get("contract"), "oi_rank": None},
                            banned=False, results=False, lots=o["lots"])
    hard = [r for r in chk["reasons"] if not r.startswith(("held", "risk", "lot too big"))]
    return not hard


def _rebook(st: dict, account: str, pos: dict, px: float) -> None:
    """A morning entry booked at the price seen when sending, corrected to the day's open."""
    a = st["accounts"][account]
    qty = _qty(pos)
    new_ch = fb.futures_charges(px * qty, sell=(pos["side"] == "short"))
    new_margin = eligibility.margin_estimate(_cfg(st), px * qty)
    a["cash"] -= (new_ch - pos["charges"]) + (new_margin - pos["margin_est"])
    a["charges"] += new_ch - pos["charges"]
    pos.update(entry=px, charges=new_ch, margin_est=new_margin, est=False, best=px if pos["side"] == "long" else min(pos.get("best") or px, px))


def _late_trail(st: dict, data: dict, day: str, p) -> list:
    """E+ on the futures price: each long's best close since entry (from the stored futures candles) and the late
    trail from it; never lowered; a hand-set stop is only ever raised too."""
    raised = []
    if not p.late_at:
        return raised
    for k, a in st["accounts"].items():
        for sym, pos in a["positions"].items():
            if pos["side"] != "long":
                continue
            xs = [b["close"] for b in fc.get(sym, start=pos["d"]) if pos["d"] <= b["date"] <= day]
            pos["best"] = max(pos.get("best") or pos["entry"], max(xs) if xs else pos["entry"])
            before = pos["stop"]
            new = tb.late_stop(pos["entry"], pos["best"], p)
            if new and new > pos["stop"]:                      # only ever raised (a hand-set stop too)
                pos["stop"], pos["trailed"] = new, True
                raised.append({"day": day, "account": k, "symbol": sym, "stop": round(new, 2), "best": round(pos["best"], 2),
                               "entry": round(pos["entry"], 2), "from": round(before, 2)})
    return raised


def _held_warnings(st: dict, config: AppConfig, day: str) -> dict:
    """On the held names: an MWPL ban, results within results_guard_sessions, a lot size change (the contract table's
    current lot against the position's). Kept in the state for the page and the alerts."""
    cfg = config.futures
    out = {"day": day, "ban": [], "results": [], "lot_change": []}
    seen = set()
    for k, a in st["accounts"].items():
        for sym, pos in a["positions"].items():
            if sym in seen:
                continue
            seen.add(sym)
            try:
                if fo_ban.banned(sym, day):
                    out["ban"].append(sym)
            except Exception:
                pass
            try:
                r = eligibility.results_within(sym, cfg.results_guard_sessions, day)
                if r:
                    out["results"].append({"symbol": sym, "date": r["date"], "sessions": r["sessions"]})
            except Exception:
                pass
            c = _contract_for(sym)
            if c and int(c["lot_size"] or 0) and int(c["lot_size"]) != int(pos["lot_size"]):
                out["lot_change"].append({"symbol": sym, "from": int(pos["lot_size"]), "to": int(c["lot_size"])})
    st["warnings"] = out
    return out


def _short_data(data: dict, syms: set) -> dict:
    """What Backtest F's short_signals needs: the cash rows with the short indicators for the futures names."""
    S = data["S"]
    F = {sym: {"short": fb.short_indicators(S[sym]["rows"])} for sym in syms if sym in S}
    return {**data, "F": F, "sig_cache": {}, "index_m50": tb.sma(data["index_close"], 50)}


def _held_cash() -> set:
    """Names held in the cash Real and Paper E+ books (the eligibility rule 'held in cash')."""
    out = set()
    try:
        with get_db() as conn:
            r = conn.execute("SELECT payload FROM real_state WHERE id = 1").fetchone()
            if r:
                out |= set(json.loads(r[0]).get("positions", {}))
            for (p,) in conn.execute("SELECT payload FROM paper_state"):
                out |= set(json.loads(p).get("positions", {}))
    except Exception:
        pass
    return out


def _entry_estimate(sym: str, day: str, S: dict, i: int) -> tuple:
    """(futures entry estimate, ATR14 in futures points, the futures / cash ratio) from the stored futures candles;
    the cash close stands in without them."""
    bars = _fut_bars(sym, day, 40)
    cash_close = S[sym]["rows"][S[sym]["at"][i]]["close"] if sym in S and i in S[sym]["at"] else None
    if not bars:
        return cash_close, None, 1.0
    px = bars[-1]["close"]
    atr = fb.atr14(bars)[-1] if len(bars) >= 15 else None
    return px, atr, (px / cash_close if cash_close else 1.0)


def _plan(st: dict, data: dict, i: int, config: AppConfig, keep: dict = None) -> dict:
    """The next open's orders per side from day i's close and the accounts as they are now. EXITs: a long that
    closed under SMA(exit_sma) on cash, a short past its time stop. Entries: every signal through the eligibility
    rules and the guards; one lot each; the rest wait with their reasons. `keep` = today's sent orders per side."""
    cfg, p, S, cal = config.futures, tb.rules(config, "E"), data["S"], data["cal"]
    day = cal[i]
    keep = keep or {}
    held_cash = _held_cash()
    held_fut = {s for a in st["accounts"].values() for s in a["positions"]}
    regime_on, cap = tb.slots(i, data["index_ok"], data["filters"], p)
    uni = {u["symbol"] for u in instruments.universe(cfg.universe_lot_cap)}
    sd = _short_data(data, uni)
    short_on, _ = fb.market(sd, i, "short", p, cfg)
    plan, waiting, short_sigs = {}, {}, []
    for side in SIDES:
        pk = primary(config, side)
        a = st["accounts"][pk]
        orders = list(keep.get(side, []))
        done = {(o["symbol"], o["action"]) for o in orders}
        exits = []
        for sym, pos in a["positions"].items():
            if pos["side"] != side or (sym, "EXIT") in done:
                continue
            if side == "long":
                sells, _, _, _ = tb.decide(i, S, {sym: None} if sym in S else {}, True, p)
                if sym in sells:
                    exits.append(sym)
                    orders.append({"action": "EXIT", "symbol": sym, "side": side, "lots": pos["lots"], "lot_size": pos["lot_size"],
                                   "contract": pos.get("contract"), "token": pos.get("token"), "expiry": pos.get("expiry"),
                                   "why": f"close under SMA{p.exit_sma}", "why_key": "rule"})
            elif pos.get("days", 0) >= fb.SHORT_TIME_STOP:
                exits.append(sym)
                orders.append({"action": "EXIT", "symbol": sym, "side": side, "lots": pos["lots"], "lot_size": pos["lot_size"],
                               "contract": pos.get("contract"), "token": pos.get("token"), "expiry": pos.get("expiry"),
                               "why": f"time stop ({fb.SHORT_TIME_STOP} sessions)", "why_key": "time"})
        if side == "long":
            _, ranked, _, _ = tb.decide(i, S, {s: None for s in a["positions"] if s in S}, True, p)
            sigs = [(sym, stop) for sym, stop in ranked if sym in uni]
            on = regime_on
            blocked_why = f"filter {tb.filter_label(0.0, p.regime_mode)}" if not on else None
        else:
            sigs = fb.short_signals(sd, i, p)
            short_sigs = [{"day": day, "symbol": sym, "hint": round(h, 2)} for sym, h in sigs]
            on = short_on
            blocked_why = "filter: NIFTY over its 50-day" if not on else None
        loss = _daily_loss(st, pk, cfg)
        wait = []
        open_lots = _open_lots(a, tuple(exits))
        cash = a["cash"]
        for k, (sym, hint) in enumerate(sigs):
            if (sym, ENTRY[side]) in done or sym in a["positions"]:
                continue
            entry, atr, ratio = _entry_estimate(sym, day, S, i)
            if not entry:
                wait.append({"symbol": sym, "side": side, "rank": k + 1, "reasons": ["no futures candles"]})
                continue
            stop = fb.stop_for(side, entry, atr, hint * ratio if hint else None, p, cfg)
            chk = eligibility.check(cfg, sym, side, entry, stop, held_cash, held_fut - set(a["positions"]), open_lots, today=day, cash=cash)
            reasons = list(chk["reasons"])
            if blocked_why:
                reasons.insert(0, blocked_why)
            if loss["hit"]:
                reasons.insert(0, "daily loss limit")
            if killed():
                reasons.append("kill switch ON")
            row = {"action": ENTRY[side], "symbol": sym, "side": side, "lots": 1, "lot_size": chk["lot_size"], "rank": k + 1,
                   "contract": (chk["contract"] or {}).get("tradingsymbol"), "token": (chk["contract"] or {}).get("token"),
                   "expiry": (chk["contract"] or {}).get("expiry"), "entry": round(entry, 2), "stop": round(stop, 2),
                   "stop_pct": round(abs(entry - stop) / entry * 100, 2), "lot_value": chk["lot_value"], "risk": chk["risk"],
                   "risk_pct": chk["risk_pct"], "margin_est": chk["margin_est"], "atr": round(atr, 2) if atr else None,
                   "why": f"{'E+ breakout' if side == 'long' else 'E− breakdown'} #{k + 1} · 1 lot ({chk['lot_size']})"}
            if reasons:
                wait.append({**row, "reasons": reasons})
                continue
            orders.append(row)
            open_lots.append({"symbol": sym, "lot_value": chk["lot_value"], "margin": chk["margin_est"], "risk": chk["risk"]})
            cash -= chk["margin_est"]
        plan[side], waiting[side] = orders, wait
    closes = data["index_close"]
    now_f = tb.filter_day(p.regime_mode, i, closes)
    before = tb.filter_day(p.regime_mode, i - 1, closes) if i else now_f
    st.update(plan=plan, waiting=waiting, decided=day, allowed={"long": bool(regime_on), "short": bool(short_on)}, cap=cap,
              filter={"state": tb.filter_label(now_f, p.regime_mode), "prev": tb.filter_label(before, p.regime_mode),
                      "changed": now_f != before, "slots": cap if regime_on else 0, "short": "ON" if short_on else "OFF"})
    recent = [s for s in st.get("short_signals", []) if s["day"] >= (date.fromisoformat(day) - timedelta(days=31)).isoformat() and s["day"] != day]
    st["short_signals"] = (recent + short_sigs)[-200:]
    st["roll_day"] = _roll_day(st, config, day)
    _results_guard(st, config, next_session(day))
    return {"plan": plan, "waiting": waiting, "short_signals": short_sigs}


def _daily_loss(st: dict, account: str, cfg) -> dict:
    a = st["accounts"][account]
    limit = round(cfg.daily_loss_pct / 100 * st["capital"], 2)
    used = round(max(0.0, (a.get("day_start") or st["capital"]) - _value(a)), 2)
    return {"used": used, "limit": limit, "hit": bool(limit) and used >= limit}


def next_session(day: str) -> str:
    from backend.ingest.holidays import is_trading_day
    d = date.fromisoformat(day)
    for _ in range(14):
        d += timedelta(days=1)
        if is_trading_day(d):
            return d.isoformat()
    return d.isoformat()


# --- replan -------------------------------------------------------------------------------------------------

def replan(config: AppConfig, data: Optional[dict] = None) -> dict:
    """↻ Re-evaluate: the next open's plan rebuilt from the evening's data and the accounts as they are now.
    Nothing is sent; sent orders are kept. The same report shape as real.replan (`suggestion` is null: the futures
    book always takes one lot a name)."""
    _use(config)
    with _lock:
        st = _state()
        if not st:
            return {"status": "not_started"}
        data = data or tb.load(config)
        if "error" in data:
            return {"status": "no_data", "error": data["error"]}
        cal = data["cal"]
        i = cal.index(st["decided"]) if st.get("decided") in cal else len(cal) - 1
        before = [o for s in SIDES for o in st["plan"].get(s, [])]
        keep = {s: [o for o in st["plan"].get(s, []) if o.get("sent")] for s in SIDES}
        _plan(st, data, i, config, keep)
        _save(st)
    after = [o for s in SIDES for o in st["plan"].get(s, [])]
    key = lambda o: (o["symbol"], o["action"])
    was, now = {key(o) for o in before}, {key(o) for o in after}
    pk = primary(config, "long")
    a = st["accounts"][pk]
    waiting = [w for s in SIDES for w in st["waiting"].get(s, [])]
    return {"status": "ok", "at": now_ist(), "before": before, "after": after, "added": [o for o in after if key(o) not in was],
            "removed": [o for o in before if key(o) not in now], "waiting": [{**w, "why": " · ".join(w["reasons"])} for w in waiting],
            "idle_cash": round(a["cash"] - sum(o.get("margin_est") or 0 for o in after if o["action"] != "EXIT"), 2),
            "size": None, "max_order": None, "max_pos": config.futures.max_pos, "slots_used": len(a["positions"]),
            "slots": config.futures.max_pos if st.get("allowed", {}).get("long") else 0, "capital": st["capital"], "cash": round(a["cash"], 2),
            "value": round(_value(a), 2), "decided": st["decided"], "filter": st.get("filter"), "max_buys_per_day": None,
            "send_time": config.real.send_time, "preview_time": config.real.preview_time, "bt_max_pos": None, "suggestion": None}


# --- the morning: dry run, skips, the send ------------------------------------------------------------------

def _skips(st: dict, day: str) -> dict:
    return (st.get("skips") or {}).get(day) or {}


def _results_guard(st: dict, config: AppConfig, day: str) -> list:
    """A planned entry whose results are within results_guard_sessions is skipped for `day` by the results guard
    (unless you switched it back on). Changes the state in place."""
    n = int(config.futures.results_guard_sessions or 0)
    if n <= 0:
        return []
    skips = st.setdefault("skips", {}).setdefault(day, {})
    free = set((st.get("unskipped") or {}).get(day) or [])
    out = []
    for s in SIDES:
        for o in st["plan"].get(s, []):
            if o["action"] == "EXIT" or o.get("sent") or o["symbol"] in skips or o["symbol"] in free:
                continue
            r = eligibility.results_within(o["symbol"], n, day)
            if r:
                skips[o["symbol"]] = {"by": "results guard", "at": now_ist(), "note": f"results {r['date']} (in {r['sessions']} session"
                                      f"{'s' if r['sessions'] != 1 else ''})", "results_date": r["date"], "results_in": r["sessions"]}
                out.append(o["symbol"])
    return out


def guard_today(config: AppConfig, now: Optional[datetime] = None) -> list:
    with _lock:
        st = _state()
        if not st:
            return []
        out = _results_guard(st, config, _day(now))
        if out:
            _save(st)
        return out


def _day(now: Optional[datetime]) -> str:
    from backend.config import IST
    return (now or datetime.now(IST)).date().isoformat()


def _find_order(st: dict, symbol: str) -> Optional[dict]:
    return next((o for s in SIDES for o in st["plan"].get(s, []) if o["symbol"] == symbol), None)


def skip_order(config: AppConfig, symbol: str, on: bool, note: str = "", now: Optional[datetime] = None) -> dict:
    """Skip (on) or take after all (off) one planned entry for today: both the paper fill and the real send of that
    side honour it. EXITs, unknown orders and orders already sent / logged today are refused (ValueError)."""
    symbol = (symbol or "").strip().upper()
    today = _day(now)
    with _lock:
        st = _state()
        if not st:
            raise ValueError("the futures book has not started yet")
        o = _find_order(st, symbol)
        if not o:
            raise ValueError(f"{symbol} is not in today's futures plan")
        if o["action"] == "EXIT":
            raise ValueError("EXITs cannot be skipped: an exit always goes out")
        if o.get("sent"):
            raise ValueError(f"{symbol} is already {o['sent']} today")
        skips = st.setdefault("skips", {}).setdefault(today, {})
        free = st.setdefault("unskipped", {}).setdefault(today, [])
        if on:
            skips[symbol] = {"by": "you", "at": now_ist(), "note": (note or "").strip()[:120]}
            if symbol in free:
                free.remove(symbol)
        else:
            skips.pop(symbol, None)
            if symbol not in free:
                free.append(symbol)
        _save(st)
    return morning(config, now)


def hold_today(config: AppConfig, note: str = "hold all", now: Optional[datetime] = None) -> dict:
    """Skip every planned entry today (the EXITs still go)."""
    today = _day(now)
    with _lock:
        st = _state()
        if not st:
            raise ValueError("the futures book has not started yet")
        skips = st.setdefault("skips", {}).setdefault(today, {})
        for s in SIDES:
            for o in st["plan"].get(s, []):
                if o["action"] != "EXIT" and not o.get("sent") and o["symbol"] not in skips:
                    skips[o["symbol"]] = {"by": "you", "at": now_ist(), "note": note}
        st.setdefault("unskipped", {})[today] = []
        _save(st)
    return morning(config, now)


def morning(config: AppConfig, now: Optional[datetime] = None) -> dict:
    """The morning dry run, read-only: the plan of both sides as it goes out at real.send_time (the real sides) and
    fills on paper, each order's skip / results / sent state, the clock, the account facts. Same keys as
    real.morning plus side, lots, margin, contract and the side's books."""
    from backend.alerts.jobs import hm
    from backend.config import IST
    from backend.ingest.holidays import is_trading_day
    _use(config)
    now = now or datetime.now(IST)
    rc, cfg = config.real, config.futures
    today = now.date().isoformat()
    send_at = datetime.combine(now.date(), hm(rc.send_time), tzinfo=IST)
    send_until = datetime.combine(now.date(), hm(rc.send_until), tzinfo=IST)
    preview_at = datetime.combine(now.date(), hm(rc.preview_time), tzinfo=IST) if rc.preview_time else send_at
    st = _state() or {}
    with get_db() as conn:
        log = {(r["symbol"], r["action"]): dict(r) for r in conn.execute(
            "SELECT symbol, action, status, created_at, reply, price FROM futures_orders WHERE day = ? AND account LIKE 'real_%' "
            "AND action IN ('BUY', 'SELL', 'EXIT') ORDER BY id", (today,))}
    skips = _skips(st, today)
    orders = []
    for s in SIDES:
        books = [m for m in MODES if switch(config, s, m)]
        for o in sorted(st.get("plan", {}).get(s, []), key=lambda o: o["action"] != "EXIT"):
            row = log.get((o["symbol"], o["action"]))
            status = (row or {}).get("status") or (o.get("sent") if o.get("sent") in ("sent", "failed", "skipped", "unknown") else None)
            sk = skips.get(o["symbol"]) if o["action"] != "EXIT" else None
            r = eligibility.results_within(o["symbol"], 10, today) if o["action"] != "EXIT" else None
            pk = primary(config, s)
            pos = (st.get("accounts", {}).get(pk, {}).get("positions") or {}).get(o["symbol"]) or {}
            close = o.get("entry") if o["action"] != "EXIT" else pos.get("last")
            orders.append({"symbol": o["symbol"], "action": o["action"], "side": s, "books": books, "lots": o.get("lots"),
                           "qty": (o.get("lots") or 0) * (o.get("lot_size") or 0), "lot_size": o.get("lot_size"), "contract": o.get("contract"),
                           "expiry": o.get("expiry"), "close": round(close, 2) if close else None,
                           "value": round((o.get("lots") or 0) * (o.get("lot_size") or 0) * close, 2) if close else None,
                           "margin": o.get("margin_est") or pos.get("margin_est"), "stop": round(o["stop"], 2) if o.get("stop") else None,
                           "risk": o.get("risk"), "why": o.get("why"), "results_date": r["date"] if r else None, "results_in": r["sessions"] if r else None,
                           "skip": sk, "status": status, "sent": status == "sent", "failed": status == "failed",
                           "at": (row or {}).get("created_at"), "price": (row or {}).get("price"), "reply": (row or {}).get("reply")})
    pending = [o for o in orders if not o["status"] and not o["skip"]]
    phase = "before" if now < send_at else "sending" if now < send_until and pending else "after"
    going = [o for o in orders if not o["skip"] and o["status"] != "skipped"]
    exits = [o for o in going if o["action"] == "EXIT"]
    entries = [o for o in going if o["action"] != "EXIT"]
    skipped = [o["symbol"] for o in orders if o["skip"] or o["status"] == "skipped"]
    sent_at = [o["at"] for o in orders if o["at"]]
    pk = primary(config, "long")
    a = (st.get("accounts") or {}).get(pk)
    gap = min(30.0, max(1.0, float(rc.send_gap_s or 10)))
    real_sides = [s for s in SIDES if switch(config, s, "real")]
    return {"now": now.isoformat(timespec="seconds"), "today": today, "preview_at": preview_at.isoformat(timespec="seconds"),
            "send_at": send_at.isoformat(timespec="seconds"), "send_until": send_until.isoformat(timespec="seconds"),
            "seconds_to_send": int((send_at - now).total_seconds()),
            "seconds_to_open": int((datetime.combine(now.date(), hm("09:15"), tzinfo=IST) - now).total_seconds()),
            "phase": phase, "visible": bool(st) and now >= preview_at and bool(orders) and is_trading_day(now.date()),
            "started": bool(st), "capital": st.get("capital"), "cash": round(a["cash"], 2) if a else None,
            "margin_used": round(_margin_used(a), 2) if a else None,
            "slots_used": len(a["positions"]) if a else 0, "slots": cfg.max_pos, "free": max(0, cfg.max_pos - (len(a["positions"]) if a else 0)),
            "filter": (st.get("filter") or {}).get("state"), "filter_name": "E+ · half filter", "short_filter": (st.get("filter") or {}).get("short"),
            "gap_s": gap, "est_total_s": round(gap * len([o for o in going if any(m == "real" for m in o["books"])])),
            "guard": {"enabled": enabled(), "killed": killed(), "results_guard_sessions": int(cfg.results_guard_sessions or 0),
                      "real_sides": real_sides, "long_real": switch(config, "long", "real"), "short_real": switch(config, "short", "real")},
            "orders": orders, "going_out": {"exits": len(exits), "buys": len(entries), "value": round(sum(o["value"] or 0 for o in entries), 2),
                                            "margin": round(sum(o["margin"] or 0 for o in entries), 2)},
            "skipped": skipped, "waiting": sum(len(st.get("waiting", {}).get(s, [])) for s in SIDES),
            "counts": {"sent": sum(o["sent"] for o in orders), "failed": sum(o["failed"] for o in orders),
                       "skipped": sum(o["status"] == "skipped" for o in orders)},
            "first_sent": min(sent_at) if sent_at else None, "last_sent": max(sent_at) if sent_at else None}


def _quotes(client, contracts: list[str]) -> dict:
    """{tradingsymbol: (last, low, high)} from one Kite quote call on the NFO contracts; {} without a login."""
    if not contracts or client is None or not getattr(client, "has_token", False):
        return {}
    try:
        q = client.quote([f"NFO:{c}" for c in contracts])
    except Exception as exc:
        logger.warning("Futures quote failed: %s", exc)
        return {}
    out = {}
    for k, v in (q or {}).items():
        if v.get("last_price"):
            o = v.get("ohlc") or {}
            out[k.split(":", 1)[1]] = (v["last_price"], o.get("low") or v["last_price"], o.get("high") or v["last_price"])
    return out


def _gap(config: AppConfig) -> float:
    try:
        return min(30.0, max(1.0, float(getattr(config.real, "send_gap_s", 10.0) or 10.0)))
    except (TypeError, ValueError):
        return 10.0


UNKNOWN = "unknown · check"           # the order log status of a webhook that got no answer: it may have gone in


def _send_fail(exc: Exception) -> tuple[str, str]:
    """(status, text) of a sender exception for the order log. A timeout (sender.SendUnknown) is `unknown · check`:
    the signal may have gone in, so the order is logged under its key (never re-sent) and left for the owner to
    check at the deployment and correct by hand. REJECTED_ALREADY_HELD (sender.AlreadyHeld) is logged as "already
    held at the deployment": the plan stays unfilled, nothing is re-sent. Anything else is a failed order."""
    if isinstance(exc, sender.SendUnknown):
        return UNKNOWN, f"{UNKNOWN}: {exc}"
    if isinstance(exc, sender.AlreadyHeld):
        return "failed", f"already held at the deployment: {exc}"
    return "failed", str(exc)


def send_orders(config: AppConfig, client, today: str, send: Optional[Callable] = None) -> dict:
    """The real sides' planned orders (EXITs, then entries), each once, through the sender (a failure is a failed
    order, never retried; a timeout is logged `unknown · check` and never re-sent either). Booked at the quote (or the plan's estimate), corrected to the open in the
    evening. Returns {status, sent, failed, skipped}."""
    _use(config)
    send = send or (lambda sym, action, lots=None: sender.send(sym, action, lots, gap=_gap(config)))
    with _lock:
        st = _state()
        if not st:
            return {"status": "nothing", "sent": [], "failed": [], "skipped": []}
        sides = [s for s in SIDES if real_live(config, s)]
        if not sides:
            return {"status": "off", "sent": [], "failed": [], "skipped": []}
        sent, failed, skipped = [], [], []
        skips = _skips(st, today)
        for side in sides:
            k = acct(side, "real")
            a = st["accounts"][k]
            orders = sorted(st["plan"].get(side, []), key=lambda o: o["action"] != "EXIT")
            px = _quotes(client, [o.get("contract") for o in orders if o.get("contract")])
            for o in orders:
                key = f"{today}|{k}|{o['symbol']}|{o['action']}"
                if o.get("sent") or _logged(key):
                    continue
                if o["action"] == "EXIT" and o["symbol"] not in a["positions"]:
                    continue
                if o["action"] != "EXIT" and o["symbol"] in skips:
                    sk = skips[o["symbol"]]
                    _log(k, today, o["symbol"], o["action"], "skipped", o["lots"], o.get("contract"), o.get("why", ""),
                         f"skipped today · by {sk['by']}" + (f" · {sk['note']}" if sk.get("note") else ""))
                    o["sent"] = "skipped"
                    skipped.append({**o, "account": k, "skip": sk})
                    continue
                price = (px.get(o.get("contract")) or (None,))[0] or (a["positions"][o["symbol"]]["last"] if o["action"] == "EXIT" else o["entry"])
                if o["action"] != "EXIT" and not _fits(st, k, o, config.futures, price):
                    _log(k, today, o["symbol"], o["action"], "skipped", o["lots"], o.get("contract"), o.get("why", ""), "no room (guards)")
                    failed.append({**o, "account": k, "error": "no room (guards)"})
                    continue
                try:
                    reply = send(o["symbol"], o["action"], None if o["action"] == "EXIT" else o["lots"])
                except Exception as exc:
                    status, text = _send_fail(exc)
                    _log(k, today, o["symbol"], o["action"], status, o["lots"], o.get("contract"), o.get("why", ""), text)
                    failed.append({**o, "account": k, "error": text[:120], "unknown": status == UNKNOWN})
                    o["sent"] = "unknown" if status == UNKNOWN else "failed"
                    continue
                _log(k, today, o["symbol"], o["action"], "sent", o["lots"], o.get("contract"), o.get("why", ""), str(reply), price)
                o["sent"] = "sent"
                if o["action"] == "EXIT":
                    _close(st, k, o["symbol"], price, today, o.get("why_key", "exit"), est=True)
                else:
                    _open(st, k, o, price, today, est=True)
                sent.append({**o, "account": k, "price": price, "reply": str(reply)[:120]})
        _save(st)
        return {"status": "ok", "sent": sent, "failed": failed, "skipped": skipped}


def send_now(config: AppConfig, client, now: Optional[datetime] = None, send: Optional[Callable] = None) -> dict:
    from backend.alerts.jobs import hm
    from backend.config import IST
    from backend.ingest.holidays import is_trading_day
    now = now or datetime.now(IST)
    if not any(real_live(config, s) for s in SIDES):
        return {"status": "error", "error": "no side is real, the futures book is off, or the kill switch is on"}
    if not is_trading_day(now.date()):
        return {"status": "error", "error": "not a trading day"}
    if not ("09:15" <= now.strftime("%H:%M") < "15:30"):
        return {"status": "error", "error": "the market is closed (9:15 AM – 3:30 PM)"}
    if now.time() >= hm(config.real.send_until):
        return {"status": "error", "error": f"the send window closed at {config.real.send_until}: nothing goes out by hand"}
    return send_orders(config, client, now.date().isoformat(), send)


# --- stops every few minutes on the futures quote ------------------------------------------------------------

def watch_stops(config: AppConfig, client, today: str, send: Optional[Callable] = None) -> dict:
    """Live futures quotes of every open lot: a long at or under its stop (today's low counts too), a short at or
    over it (today's high), exits once per account, stock and day. Paper closes at the price; real sends EXIT
    through the sender (a failure is logged, the position stays). Returns {status, hits}."""
    _use(config)
    send = send or (lambda sym, action, lots=None: sender.send(sym, action, lots, gap=_gap(config)))
    with _lock:
        st = _state()
        if not st or not any(a["positions"] for a in st["accounts"].values()):
            return {"status": "nothing", "hits": []}
        if client is None or not getattr(client, "has_token", False):
            return {"status": "no_login", "hits": []}
        contracts = {p.get("contract") or s for a in st["accounts"].values() for s, p in a["positions"].items()}
        px = _quotes(client, sorted(contracts))
        hits = []
        for k, a in st["accounts"].items():
            side, mode = side_of(k), mode_of(k)
            for sym, pos in list(a["positions"].items()):
                c = pos.get("contract") or sym
                if c not in px:
                    continue
                ltp, low, high = px[c]
                pos["live"] = ltp
                if pos["d"] == today and pos.get("est"):
                    low = high = ltp
                hit = (min(ltp, low) <= pos["stop"]) if side == "long" else (max(ltp, high) >= pos["stop"])
                if not hit:
                    continue
                key = f"{today}|{k}|{sym}|EXIT"
                if _logged(key):
                    continue
                why = f"stop {pos['stop']:.2f} hit" + ("" if (ltp <= pos["stop"] if side == "long" else ltp >= pos["stop"]) else
                                                      (f" (day low {low:.2f})" if side == "long" else f" (day high {high:.2f})"))
                h = {"account": k, "tag": TAG[k], "symbol": sym, "side": side, "lots": pos["lots"], "contract": c, "price": ltp, "why": why,
                     "stop": round(pos["stop"], 2)}
                if mode == "real":
                    if not real_live(config, side):
                        continue
                    try:
                        reply = send(sym, "EXIT", None)
                    except Exception as exc:
                        status, text = _send_fail(exc)
                        _log(k, today, sym, "EXIT", status, pos["lots"], c, why, text, ltp)
                        if status == UNKNOWN:
                            pos["check"] = f"{today}: EXIT {UNKNOWN}"          # the lot stays until the owner checks
                        hits.append({**h, "error": text[:120], "unknown": status == UNKNOWN})
                        continue
                    _log(k, today, sym, "EXIT", "sent", pos["lots"], c, why, str(reply), ltp)
                else:
                    _log(k, today, sym, "EXIT", "filled", pos["lots"], c, why, "paper", ltp)
                t = _close(st, k, sym, ltp, today, "trail" if pos.get("trailed") else "stop")
                hits.append({**h, "net": t["net"]})
        _save(st)
        return {"status": "ok", "hits": hits}


# --- rolls at T-roll_days ------------------------------------------------------------------------------------

def _sessions_left(pos: dict, today: str) -> Optional[int]:
    try:
        return instruments.sessions_to_expiry(pos["expiry"], today) if pos.get("expiry") else None
    except Exception:
        return None


def _roll_day(st: dict, config: AppConfig, today: str) -> Optional[str]:
    """The next roll session: `roll_days` sessions before the nearest held expiry (or the near month's expiry)."""
    from backend.ingest.holidays import is_trading_day
    exps = [p["expiry"] for a in st["accounts"].values() for p in a["positions"].values() if p.get("expiry")]
    if not exps:
        cal = instruments.expiry_calendar()
        exps = [x["expiry"] for x in cal if x["expiry"] >= today]
    if not exps:
        return None
    d = date.fromisoformat(min(exps))
    n = 0
    while n < config.futures.roll_days:
        d -= timedelta(days=1)
        n += is_trading_day(d)
    return d.isoformat()


def is_roll_day(config: AppConfig, today: str) -> bool:
    st = _state()
    if not st:
        return False
    return any((_sessions_left(p, today) or 99) <= config.futures.roll_days for a in st["accounts"].values() for p in a["positions"].values())


def roll_queue(st: dict, config: AppConfig, today: str) -> dict:
    """What the roll day does with each open lot: roll (EXIT near + entry next) or exit (the rule fired, or no
    next month), with the estimated cost. {day, items}."""
    items = []
    for k, a in st["accounts"].items():
        for sym, pos in a["positions"].items():
            nxt = instruments.contracts(sym).get("next")
            pending = any(o["action"] == "EXIT" and o["symbol"] == sym for o in st["plan"].get(pos["side"], []))
            qty = _qty(pos)
            px = _last(pos)
            cost = fb.futures_charges(px * qty, True) + fb.futures_charges(px * qty, False)
            left = _sessions_left(pos, today)
            items.append({"account": k, "book": mode_of(k), "symbol": sym, "side": pos["side"], "contract": pos.get("contract"),
                          "expiry": pos.get("expiry"), "sessions": left, "next": (nxt or {}).get("tradingsymbol"), "next_expiry": (nxt or {}).get("expiry"),
                          "action": "exit" if pending or not nxt or (nxt.get("expiry") or "") <= (pos.get("expiry") or "") else "roll",
                          "why": "rule fired" if pending else "no next month" if not nxt else "rule holds", "est_cost": round(cost, 2),
                          "due": left is not None and left <= config.futures.roll_days})
    return {"day": st.get("roll_day"), "items": items}


def roll(config: AppConfig, client, today: str, send: Optional[Callable] = None, force: bool = False) -> dict:
    """The roll at T-roll_days (9:20): every lot within roll_days of its expiry. Exit rule pending or no next month:
    EXIT. Else EXIT the near month, then (once the exit is booked) the entry on the next month at the same price,
    logged as one roll with its cost (charges + the basis between the two quotes). At T-1 a lot that could not roll is
    exited (never held into expiry). Real lots go through the sender (an entry that fails leaves the lot closed and
    is reported)."""
    _use(config)
    cfg = config.futures
    send = send or (lambda sym, action, lots=None: sender.send(sym, action, lots, gap=_gap(config)))
    with _lock:
        st = _state()
        if not st:
            return {"status": "nothing", "rolls": [], "exits": [], "failed": []}
        rolls, exits, failed = [], [], []
        for k, a in st["accounts"].items():
            side, mode = side_of(k), mode_of(k)
            for sym, pos in list(a["positions"].items()):
                left = _sessions_left(pos, today)
                if left is None or (left > cfg.roll_days and not force):
                    continue
                key = f"{today}|{k}|{sym}|ROLL"
                if _logged(key):
                    continue
                nxt = instruments.contracts(sym).get("next")
                pending = any(o["action"] == "EXIT" and o["symbol"] == sym for o in st["plan"].get(side, []))
                c = pos.get("contract") or sym
                q = _quotes(client, [c] + ([nxt["tradingsymbol"]] if nxt else []))
                px = (q.get(c) or (None,))[0] or _last(pos)
                px_next = (q.get(nxt["tradingsymbol"]) or (None,))[0] if nxt else None
                can_roll = nxt and (nxt.get("expiry") or "") > (pos.get("expiry") or "") and not pending
                if mode == "real" and not real_live(config, side):
                    continue
                if not can_roll:
                    if not pending and nxt is None and left > 1 and not force:
                        pos["roll_failed"] = f"{today}: no next month"      # try again; forced out at T-1
                        continue
                    why = "roll_exit" if pending else "expiry"
                    if mode == "real":
                        try:
                            reply = send(sym, "EXIT", None)
                        except Exception as exc:
                            status, text = _send_fail(exc)
                            _log(k, today, sym, "EXIT", status, pos["lots"], c, f"roll: {why}", text, px, key=key)
                            if status == UNKNOWN:
                                pos["check"] = f"{today}: EXIT {UNKNOWN}"
                            failed.append({"account": k, "tag": TAG[k], "symbol": sym, "error": text[:120], "step": "exit", "unknown": status == UNKNOWN})
                            continue
                        _log(k, today, sym, "EXIT", "sent", pos["lots"], c, f"roll: {why}", str(reply), px, key=key)
                    else:
                        _log(k, today, sym, "EXIT", "filled", pos["lots"], c, f"roll: {why}", "paper", px, key=key)
                    t = _close(st, k, sym, px, today, why)
                    exits.append({"account": k, "tag": TAG[k], "symbol": sym, "side": side, "why": why, "price": px, "net": t["net"]})
                    continue
                qty = _qty(pos)
                basis = ((px_next or px) - px) * qty * _sign(side) if px_next else 0.0
                if mode == "real":
                    try:
                        reply = send(sym, "EXIT", None)
                    except Exception as exc:
                        status, text = _send_fail(exc)
                        _log(k, today, sym, "ROLL", status, pos["lots"], c, f"roll → {nxt['tradingsymbol']}: exit refused", text, px, key=key)
                        if status == UNKNOWN:
                            pos["check"] = f"{today}: roll EXIT {UNKNOWN}"
                        failed.append({"account": k, "tag": TAG[k], "symbol": sym, "error": text[:120], "step": "exit", "unknown": status == UNKNOWN})
                        continue
                t = _close(st, k, sym, px, today, "roll", est=False)      # the exit is booked first
                entry = px_next or px
                o = {"symbol": sym, "side": side, "lots": pos["lots"], "lot_size": pos["lot_size"], "contract": nxt["tradingsymbol"],
                     "token": nxt["token"], "expiry": nxt["expiry"], "stop": pos["stop"]}
                if mode == "real":
                    try:
                        reply2 = send(sym, ENTRY[side], pos["lots"])
                    except Exception as exc:
                        status, text = _send_fail(exc)
                        _log(k, today, sym, "ROLL", status, pos["lots"], c, f"roll → {nxt['tradingsymbol']}: exit sent, entry refused", text, px, key=key)
                        failed.append({"account": k, "tag": TAG[k], "symbol": sym, "error": text[:120], "step": "entry", "exited": True,
                                       "unknown": status == UNKNOWN})
                        continue
                    reply = f"{reply} / {reply2}"
                else:
                    reply = "paper"
                new = _open(st, k, o, entry, today, est=(mode == "real"))      # the next month, the old stop and history kept
                new.update(d=pos["d"], days=pos.get("days", 0), best=pos.get("best") or entry, trailed=pos.get("trailed"),
                           manual_stop=pos.get("manual_stop"), edits=pos.get("edits", []), rolls=pos.get("rolls", 0) + 1)
                # the roll's cost: the exit's and the entry's charges plus the basis paid (next month over the near)
                cost = round(fb.futures_charges(px * qty, side == "long") + fb.futures_charges(entry * qty, side == "short") + basis, 2)
                r = {"day": today, "account": k, "tag": TAG[k], "book": mode, "symbol": sym, "side": side, "lots": pos["lots"], "from": c,
                     "to": nxt["tradingsymbol"], "expiry": nxt["expiry"], "px": round(px, 2), "px_next": round(entry, 2),
                     "basis": round(basis, 2), "basis_pct": round((entry / px - 1) * 100, 3) if px else None, "cost": cost}
                st.setdefault("rolls", []).append(r)
                st["rolls"] = st["rolls"][-100:]
                _log(k, today, sym, "ROLL", "sent" if mode == "real" else "filled", pos["lots"], c,
                     f"roll {c} → {nxt['tradingsymbol']} · cost ₹{cost:,.0f}", str(reply), px, key=key)
                rolls.append(r)
        st["roll_day"] = _roll_day(st, config, today)
        _save(st)
        return {"status": "ok", "rolls": rolls, "exits": exits, "failed": failed}


# --- capital, corrections by hand ---------------------------------------------------------------------------

def apply_deposit(st: dict, capital: float) -> float:
    delta = float(capital) - st["capital"]
    for k, a in st["accounts"].items():
        if a["cash"] + delta < 0:
            raise ValueError(f"withdrawing {-delta:,.0f} is more than the cash of {TAG[k]} ({a['cash']:,.0f}); lower it after lots are closed")
    for a in st["accounts"].values():
        a["cash"] += delta
        a["day_start"] = (a.get("day_start") or st["capital"]) + delta
    st["capital"] = float(capital)
    st.setdefault("deposits", []).append({"at": now_ist(), "amount": round(delta, 2), "capital": float(capital)})
    return delta


def set_capital(config: AppConfig, capital: float) -> dict:
    """A new capital: before the start it is simply the setting; afterwards the difference is money added or
    withdrawn on every account (cash moves, the profit so far stays)."""
    with _lock:
        st = _state()
        config.futures.capital = float(capital)
        if not st:
            return {"status": "ok", "capital": capital}
        delta = apply_deposit(st, capital)
        _save(st)
        return {"status": "ok", "capital": capital, "delta": delta}


def _note(account: str, symbol: str, action: str, text: str, lots=None, price=None, contract=None) -> None:
    at = now_ist()
    _log(account, at[:10], symbol, action, HAND[action], lots, contract, text, "", price, key=f"{at}|{account}|{symbol}|{action}|{uuid.uuid4().hex[:8]}")


def _num(x, name: str, whole: bool = False):
    if x is None or x == "":
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if not v > 0:
        raise ValueError(f"{name} must be more than 0")
    if whole and v != int(v):
        raise ValueError(f"{name} must be a whole number of lots")
    return int(v) if whole else v


def _holder(st: dict, symbol: str, account: Optional[str] = None) -> Optional[str]:
    """The account holding `symbol` (a given one, else the first in ACCOUNTS order)."""
    if account:
        return account if symbol in st["accounts"].get(account, {}).get("positions", {}) else None
    return next((k for k in ACCOUNTS if symbol in st["accounts"][k]["positions"]), None)


def _effects(st: dict, account: str, pos: dict, before: dict) -> dict:
    last = _last(pos)
    qty = _qty(pos)
    exit_ch = fb.futures_charges(last * qty, sell=(pos["side"] == "long"))
    return {"pnl_before": round(before["net"], 2), "pnl_after": round(_unrealised(pos) - pos["charges"] - exit_ch, 2),
            "cash": round(st["accounts"][account]["cash"], 2), "exit_qty": pos["lots"], "margin": round(pos["margin_est"], 2),
            "stop_pct": round(_sign(pos["side"]) * (last - pos["stop"]) / last * 100, 2), "charges": round(pos["charges"], 2),
            "est_cleared": bool(before.get("est")) and not pos.get("est"), "trail_arms_at": round(pos["entry"] * 2, 2) if pos["side"] == "long" else None}


def edit_position(config: AppConfig, symbol: str, lots=None, entry=None, stop=None, note: str = "", dry: bool = False,
                  account: Optional[str] = None) -> dict:
    """Correct an open lot by hand: lots, entry and / or stop. Cash, margin and charges move like the evening's
    correction; `est` is cleared; an edited stop is marked manual_stop (the trail may still raise a long's, never
    lower it). Logged. dry=True: the effects only."""
    _use(config)
    lots, entry, stop = _num(lots, "lots", whole=True), _num(entry, "entry"), _num(stop, "stop")
    symbol = (symbol or "").upper()
    with _lock:
        st = _state()
        k = _holder(st, symbol, account) if st else None
        if not k:
            raise ValueError(f"no open futures position in {symbol}")
        a, pos = st["accounts"][k], st["accounts"][k]["positions"][symbol]
        before = _pos_row(k, pos, tb.rules(config, "E"), config)
        last = _last(pos)
        if stop is not None and (stop >= last if pos["side"] == "long" else stop <= last):
            raise ValueError(f"the stop ({stop:,.2f}) must be {'below' if pos['side'] == 'long' else 'above'} the last known price ({last:,.2f})")
        if lots is None and entry is None and stop is None:
            raise ValueError("nothing to change")
        at, edits = now_ist(), []
        if lots is not None or entry is not None:
            new_l, new_px = lots if lots is not None else pos["lots"], entry if entry is not None else pos["entry"]
            qty = new_l * pos["lot_size"]
            new_ch = fb.futures_charges(new_px * qty, sell=(pos["side"] == "short"))
            new_margin = eligibility.margin_estimate(config.futures, new_px * qty)
            a["cash"] -= (new_ch - pos["charges"]) + (new_margin - pos["margin_est"])
            a["charges"] += new_ch - pos["charges"]
            if lots is not None and lots != pos["lots"]:
                edits.append({"at": at, "field": "lots", "from": pos["lots"], "to": lots, "note": note})
                for o in st["plan"].get(pos["side"], []):
                    if o["symbol"] == symbol and o["action"] == "EXIT" and not o.get("sent"):
                        o["lots"] = lots
            if entry is not None and round(entry, 2) != round(pos["entry"], 2):
                edits.append({"at": at, "field": "entry", "from": round(pos["entry"], 2), "to": round(entry, 2), "note": note})
            pos.setdefault("px_orig", round(pos["entry"], 2))
            pos.update(lots=new_l, entry=new_px, charges=new_ch, margin_est=new_margin, est=False)
            pos["best"] = max(pos.get("best") or new_px, new_px) if pos["side"] == "long" else min(pos.get("best") or new_px, new_px)
        if stop is not None and round(stop, 2) != round(pos["stop"], 2):
            edits.append({"at": at, "field": "stop", "from": round(pos["stop"], 2), "to": round(stop, 2), "note": note})
            pos.update(stop=stop, manual_stop=True)
        pos.setdefault("edits", []).extend(edits)
        if not dry:
            _save(st)
            for e in edits:
                _note(k, symbol, "EDIT", f"edited {symbol}: {e['field']} {e['from']:,.2f} → {e['to']:,.2f}" + (f" · {note}" if note else "") + " · by you",
                      lots=pos["lots"], price=pos["entry"], contract=pos.get("contract"))
        return {"status": "ok", "dry": dry, "account": k, "position": _pos_row(k, pos, tb.rules(config, "E"), config),
                "effects": _effects(st, k, pos, before), "edits": edits}


def remove_position(config: AppConfig, symbol: str, note: str = "", account: Optional[str] = None) -> dict:
    """Drop a lot from tracking without any order (an entry the deployment never filled): the margin and charges
    come back as if never opened. Logged."""
    _use(config)
    symbol = (symbol or "").upper()
    with _lock:
        st = _state()
        k = _holder(st, symbol, account) if st else None
        if not k:
            raise ValueError(f"no open futures position in {symbol}")
        a = st["accounts"][k]
        pos = a["positions"].pop(symbol)
        a["cash"] += pos["margin_est"] + pos["charges"]
        a["charges"] -= pos["charges"]
        st["plan"][pos["side"]] = [o for o in st["plan"].get(pos["side"], []) if not (o["symbol"] == symbol and o["action"] == "EXIT" and not o.get("sent"))]
        _save(st)
        _note(k, symbol, "REMOVE", f"removed from tracking: {note or 'never filled'} · {pos['lots']} lot @ {pos['entry']:,.2f} · by you",
              lots=pos["lots"], price=pos["entry"], contract=pos.get("contract"))
        return {"status": "ok", "account": k, "symbol": symbol, "cash": round(a["cash"], 2), "removed": {"lots": pos["lots"], "entry": round(pos["entry"], 2)}}


def add_position(config: AppConfig, symbol: str, lots, entry, day: str, side: str = "long", book: str = "paper", stop=None,
                 note: str = "") -> dict:
    """A lot opened by hand that the book should track and exit by its rules: the near contract from the stored
    contracts (lot size, expiry), the stop from the ATR rule unless given, margin from the estimate. Refused without
    a contract, a free slot or the cash. Nothing is sent."""
    _use(config)
    cfg = config.futures
    lots, entry, stop = _num(lots, "lots", whole=True), _num(entry, "entry"), _num(stop, "stop")
    symbol = (symbol or "").strip().upper()
    if lots is None or entry is None:
        raise ValueError("lots and entry are needed")
    if side not in SIDES or book not in MODES:
        raise ValueError("side must be long or short; book paper or real")
    if not day or not _ISO_DAY.match(day):
        raise ValueError("the day must be YYYY-MM-DD")
    c = _contract_for(symbol)
    if not c:
        raise ValueError(f"{symbol} has no stored futures contract")
    k = acct(side, book)
    with _lock:
        st = _state()
        if not st:
            raise ValueError("the futures book has not started yet (the first evening run starts it)")
        a = st["accounts"][k]
        if symbol in a["positions"]:
            raise ValueError(f"{symbol} is already an open lot in {TAG[k]}: edit it instead")
        if len(a["positions"]) >= cfg.max_pos:
            raise ValueError(f"no free slot ({len(a['positions'])} of {cfg.max_pos} open)")
        qty = lots * int(c["lot_size"])
        margin = eligibility.margin_estimate(cfg, entry * qty)
        ch = fb.futures_charges(entry * qty, sell=(side == "short"))
        if margin + ch > a["cash"]:
            raise ValueError(f"not enough cash: {margin + ch:,.0f} needed for the margin, {a['cash']:,.0f} free")
        bars = _fut_bars(symbol, None, 40)
        last = bars[-1]["close"] if bars else entry
        given = stop is not None
        if not given:
            atr = fb.atr14(bars)[-1] if len(bars) >= 15 else None
            stop = fb.stop_for(side, entry, atr, None, tb.rules(config, "E"), cfg)
        if (stop >= last) if side == "long" else (stop <= last):
            raise ValueError(f"the stop ({stop:,.2f}) must be {'below' if side == 'long' else 'above'} the last close ({last:,.2f})")
        o = {"symbol": symbol, "side": side, "lots": lots, "lot_size": int(c["lot_size"]), "contract": c["tradingsymbol"], "token": c["token"],
             "expiry": c["expiry"], "stop": stop}
        pos = _open(st, k, o, entry, day, est=False)
        xs = [b["close"] for b in bars if b["date"] >= day]
        pos.update(last=last, days=len(xs), manual=True, manual_stop=given,
                   best=(max([entry] + xs) if side == "long" else min([entry] + xs)),
                   edits=[{"at": now_ist(), "field": "added", "from": None, "to": f"{lots} lot @ {entry:,.2f}", "note": note}])
        _save(st)
        _note(k, symbol, "ADD", f"added {symbol}: {lots} lot @ {entry:,.2f} opened {day} · stop {stop:,.2f}" + (f" · {note}" if note else "") + " · by you",
              lots=lots, price=entry, contract=c["tradingsymbol"])
        return {"status": "ok", "account": k, "position": _pos_row(k, pos, tb.rules(config, "E"), config), "cash": round(a["cash"], 2)}


# --- reading -----------------------------------------------------------------------------------------------

def reset() -> None:
    with _lock, get_db() as conn:
        for t in ("futures_state", "futures_trades", "futures_equity"):
            conn.execute(f"DELETE FROM {t}")


def _pos_row(account: str, x: dict, pe, config: AppConfig) -> dict:
    last = _last(x)
    qty = _qty(x)
    exit_ch = fb.futures_charges(last * qty, sell=(x["side"] == "long"))
    gross = _unrealised(x)
    left = _sessions_left(x, now_ist()[:10])
    active = x["side"] == "long" and bool(pe.late_at and x.get("best") and x["best"] >= x["entry"] * (1 + pe.late_at / 100))
    return {"account": account, "book": mode_of(account), "tag": TAG[account], "symbol": x["symbol"], "side": x["side"], "lots": x["lots"],
            "lot_size": x["lot_size"], "qty": qty, "contract": x.get("contract"), "token": x.get("token"), "expiry": x.get("expiry"),
            "sessions_to_expiry": left, "roll_due": left is not None and left <= config.futures.roll_days, "in": x["d"],
            "entry": round(x["entry"], 2), "buy": round(x["entry"], 2), "last": round(last, 2), "live": bool(x.get("live")),
            "stop": round(x["stop"], 2), "stop_pct": round(_sign(x["side"]) * (last - x["stop"]) / last * 100, 2) if last else None,
            "days": x.get("days", 0), "gross": round(gross, 2), "costs": round(x["charges"] + exit_ch, 2),
            "net": round(gross - x["charges"] - exit_ch, 2), "mtm": round(gross, 2), "margin_est": round(x["margin_est"], 2),
            "lot_value": round(x["entry"] * qty, 2), "risk": round(_risk(x), 2), "est": bool(x.get("est")),
            "best": round(x["best"], 2) if x.get("best") else None, "trailed": bool(x.get("trailed")), "trail_active": active,
            "edits": x.get("edits", []), "manual_stop": bool(x.get("manual_stop")), "manual": bool(x.get("manual")),
            "px_orig": x.get("px_orig"), "rolls": x.get("rolls", 0), "roll_failed": x.get("roll_failed")}


def guards(st: dict, config: AppConfig, account: str) -> dict:
    cfg = config.futures
    a = st["accounts"][account]
    cap = st["capital"]
    used_m = _margin_used(a)
    notional = sum(p["entry"] * _qty(p) for p in a["positions"].values())
    heat = sum(_risk(p) for p in a["positions"].values())
    mp = round(used_m / cap * 100, 1) if cap else 0.0
    return {"kill": killed(), "enabled": enabled(), "daily_loss": _daily_loss(st, account, cfg),
            "notional": {"used": round(notional, 2), "cap": round(cfg.max_notional_x * cap, 2)},
            "cash_floor": {"free": round(a["cash"], 2), "floor": cfg.cash_floor, "ok": a["cash"] >= cfg.cash_floor},
            "heat": {"pct": round(heat / cap * 100, 1) if cap else 0.0, "rs": round(heat, 2), "cap": cfg.heat_pct},
            "margin": {"used": round(used_m, 2), "pct": mp, "warn": mp >= MARGIN_WARN, "block": mp >= MARGIN_BLOCK,
                       "warn_at": MARGIN_WARN, "block_at": MARGIN_BLOCK}}


def _equity(account: str) -> list:
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT day, equity, gross, nifty FROM futures_equity WHERE account = ? ORDER BY day", (account,))]


def _trades(account: Optional[str] = None) -> list:
    with get_db() as conn:
        q = "SELECT id, account, payload FROM futures_trades" + (" WHERE account = ?" if account else "") + " ORDER BY id DESC"
        return [{**json.loads(r[2]), "id": r[0], "account": r[1], "tag": TAG.get(r[1], r[1])} for r in conn.execute(q, (account,) if account else ())]


def orders_log(limit: int = 200) -> list:
    with get_db() as conn:
        return [{**dict(r), "tag": TAG.get(r["account"], r["account"])} for r in conn.execute(
            "SELECT created_at, day, account, symbol, action, lots, contract, reason, status, reply, price FROM futures_orders "
            "ORDER BY id DESC LIMIT ?", (limit,))]


def account_view(st: dict, config: AppConfig, k: str) -> dict:
    a = st["accounts"][k]
    value = _value(a)
    cap = st["capital"]
    trades = _trades(k)
    realised = sum(t["net"] for t in trades)
    won = [t for t in trades if t["net"] > 0]
    loss = -sum(t["net"] for t in trades if t["net"] <= 0)
    eq = _equity(k)
    deposits = st.get("deposits", [])
    first_cap = cap - sum(x["amount"] for x in deposits)
    for e in eq:
        e["equity"] -= sum(x["amount"] for x in deposits if x["at"][:10] <= e["day"])
    peak, dd = first_cap, 0.0
    for e in eq:
        peak = max(peak, e["equity"])
        dd = max(dd, (peak - e["equity"]) / peak * 100 if peak else 0.0)
    return {"account": k, "tag": TAG[k], "side": side_of(k), "book": mode_of(k), "on": switch(config, side_of(k), mode_of(k)),
            "value": round(value, 2), "cash": round(a["cash"], 2), "margin_used": round(_margin_used(a), 2), "charges": round(a["charges"], 2),
            "net": round(value - cap, 2), "return": round((value - cap) / cap * 100, 2) if cap else 0.0, "realised": round(realised, 2),
            "unrealised": round(value - cap - realised, 2), "mtm_today": round(value - (a.get("day_start") or cap), 2),
            "positions": len(a["positions"]), "closed": len(trades), "won": len(won),
            "profit_factor": round(sum(t["net"] for t in won) / loss, 2) if loss else None, "drawdown": round(dd, 2),
            "curve": {"dates": [e["day"] for e in eq], "net": [e["equity"] for e in eq]}, "guards": guards(st, config, k)}


def summary(config: AppConfig) -> dict:
    """Everything the Futures page needs (the headline figures are the long side's primary account: real when the
    long side is real, else paper; the short paper book has its own block)."""
    _use(config)
    cfg = config.futures
    st = _state()
    sw = switches(config)
    today = now_ist()[:10]
    bt = fb.saved()
    verdict = (bt or {}).get("verdict") or {}
    robust = {s: {"pass": ((bt or {}).get(s) or {}).get("robust_pass"), "of": ((bt or {}).get(s) or {}).get("robust_of")} for s in SIDES}
    sides = {s: {"paper": sw[f"{s}_paper"], "real": sw[f"{s}_real"], "verdict": verdict.get(s), "robust": robust[s],
                 "real_allowed": True if s == "long" else sw["short_real_allowed"], "tag": {m: TAG[acct(s, m)] for m in MODES}} for s in SIDES}
    uni = instruments.universe(cfg.universe_lot_cap)
    exp = [x for x in instruments.expiry_calendar() if x["expiry"] >= today]
    base = {"started": bool(st), "capital": st["capital"] if st else float(cfg.capital), "sides": sides, "switches": sw,
            "settings": {k: getattr(cfg, k) for k in ("capital", "max_pos", "small_lot_value", "max_lot_value", "max_risk_pct", "max_stop_pct",
                                                      "top_oi", "margin_pct", "max_notional_x", "cash_floor", "daily_loss_pct", "heat_pct",
                                                      "roll_days", "results_guard_sessions")},
            "send": {"preview_time": config.real.preview_time, "send_time": config.real.send_time, "send_until": config.real.send_until,
                     "stop_check_min": config.real.stop_check_min, "gap_s": _gap(config), "roll_time": ROLL_TIME},
            "universe_count": len(instruments.universe()), "eligible_count": len(uni),
            "next_expiry": exp[0]["expiry"] if exp else None, "backtest": {"built": bool(bt), "verdict": verdict, "ran_at": (bt or {}).get("ran_at")},
            "log": orders_log(120), "kills": list(sender.sent_kills), "sender": sender.check(gap=_gap(config))}
    if not st:
        return {**base, "cash": float(cfg.capital), "margin_used": 0.0, "margin_pct": 0.0, "mtm_today": 0.0, "value": float(cfg.capital), "net": 0.0,
                "return": 0.0, "guards": {"kill": sw["killed"], "enabled": sw["enabled"], "daily_loss": {"used": 0.0, "limit": round(cfg.daily_loss_pct / 100 * cfg.capital, 2), "hit": False},
                                          "notional": {"used": 0.0, "cap": round(cfg.max_notional_x * cfg.capital, 2)},
                                          "cash_floor": {"free": float(cfg.capital), "floor": cfg.cash_floor, "ok": True},
                                          "heat": {"pct": 0.0, "rs": 0.0, "cap": cfg.heat_pct},
                                          "margin": {"used": 0.0, "pct": 0.0, "warn": False, "block": False, "warn_at": MARGIN_WARN, "block_at": MARGIN_BLOCK}},
                "positions": [], "waiting": [], "plan": [], "rolls": {"day": None, "items": []}, "roll_day": None,
                "short_paper": {"open": 0, "signals_month": 0, "net": 0.0, "trades": []}, "deposits": [], "accounts": {}, "trades": [], "curve": None}
    pk = primary(config, "long")
    pe = tb.rules(config, "E")
    acc = {k: account_view(st, config, k) for k in ACCOUNTS}
    positions = [_pos_row(k, x, pe, config) for k in ACCOUNTS if switch(config, side_of(k), mode_of(k)) or st["accounts"][k]["positions"]
                 for x in sorted(st["accounts"][k]["positions"].values(), key=lambda p: p["d"], reverse=True)]
    waiting = [{**w, "why": " · ".join(w["reasons"])} for s in SIDES for w in st.get("waiting", {}).get(s, [])]
    plan = [o for s in SIDES for o in st.get("plan", {}).get(s, [])]
    sp = acc["paper_short"]
    month = (date.fromisoformat(today) - timedelta(days=31)).isoformat()
    short_trades = _trades("paper_short")[:12]
    main = acc[pk]
    return {**base, "as_of": st.get("as_of"), "decided": st.get("decided"), "updated_at": st.get("updated_at"), "start_date": st["started"],
            "primary": pk, "cash": main["cash"], "margin_used": main["margin_used"], "margin_pct": main["guards"]["margin"]["pct"],
            "mtm_today": main["mtm_today"], "value": main["value"], "net": main["net"], "return": main["return"], "realised": main["realised"],
            "unrealised": main["unrealised"], "won": main["won"], "closed": main["closed"], "profit_factor": main["profit_factor"],
            "drawdown": main["drawdown"], "curve": main["curve"], "guards": main["guards"], "positions": positions, "waiting": waiting, "plan": plan,
            "allowed": st.get("allowed"), "filter": st.get("filter"), "rolls": roll_queue(st, config, today), "roll_day": st.get("roll_day"),
            "roll_log": list(reversed(st.get("rolls", [])))[:20], "warnings": st.get("warnings") or {},
            "short_paper": {"open": sp["positions"], "net": sp["net"], "return": sp["return"], "value": sp["value"],
                            "signals_month": sum(1 for s in st.get("short_signals", []) if s["day"] >= month), "signals": st.get("short_signals", [])[-10:],
                            "trades": short_trades, "verdict": verdict.get("short"), "robust": robust["short"]},
            "deposits": st.get("deposits", []), "accounts": acc, "trades": _trades()[:100], "notes": st.get("notes", []),
            "risk": round(sum(p["risk"] for p in positions if p["account"] == pk), 2)}


def due(now: datetime, config: AppConfig, done: set, trading: bool, last_stop: Optional[datetime]) -> list[str]:
    """Which futures jobs run at `now` (pure): the preview at real.preview_time, the send at real.send_time (real
    sides only), stops every stop_check_min in market hours, the roll on a roll day from 9:20."""
    from backend.alerts.jobs import MARKET_CLOSE, hm
    rc, t, jobs = config.real, now.time(), []
    if not trading:
        return jobs
    if rc.preview_time and "futures_preview" not in done and hm(rc.preview_time) <= t < hm(rc.send_time):
        jobs.append("futures_preview")
    if "futures_send" not in done and hm(rc.send_time) <= t < hm(rc.send_until) and any(real_live(config, s) for s in SIDES):
        jobs.append("futures_send")
    if hm("09:15") <= t < MARKET_CLOSE and (last_stop is None or (now - last_stop).total_seconds() >= rc.stop_check_min * 60):
        jobs.append("futures_stops")
    if "futures_roll" not in done and hm(ROLL_TIME) <= t < MARKET_CLOSE and is_roll_day(config, now.date().isoformat()):
        jobs.append("futures_roll")
    return jobs
