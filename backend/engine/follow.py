"""📈 Follow: what if every fresh breakout on the Research screens were bought blindly? One simulated book, no
screen accounts: a stock is one position at a time, tagged with every screen (bases / vcp / flag) that
flagged it. Read-only, never orders.

  entry   the first day a stock is a *fresh* breakout on any screen (the scan's own test: a volume-confirmed close
          over the base's pivot, within fresh_sessions, run not ended) -> bought at the NEXT session's open,
          floor(₹ per position / open) shares. Settings: follow.per_position (app_settings), the screens' dials.
  exit    the day the run ends, as the stage board defines it (a close under the played_out_ma line, or
          played_out_drop under the pivot) -> sold at the NEXT session's open, delivery charges both ways.
  backfill from the saved daily candles, causal: each breakout is judged on the candles up to its day only;
  daily   after the evening scan: the day's fresh rows open positions, every live position steps one candle.
Positions are kept in `screen_follow` ((symbol, breakout_date) -> payload)."""

import json
import threading
from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

import numpy as np

from backend.config import AppConfig
from backend.database.models import get_candles, get_db, get_setting, now_ist, set_setting
from backend.engine.bases import detect_bases
from backend.engine.flags import detect_flags
from backend.engine.indicators import sma
from backend.engine.rs import rs_ratings, rs_score
from backend.engine.stages import classify, classify_flag, classify_vcp
from backend.engine.trend_breakout import delivery_charges
from backend.engine.trend_template import trend_template
from backend.logging_config.logger import setup_logger

logger = setup_logger("follow")

SCREENS = ("bases", "vcp", "flag")
NAMES = {"bases": "Bases (all)", "vcp": "VCP", "flag": "Bull flag"}
DEFAULT_PER_POSITION = 100000.0
MONTHS = 12                                       # the backfill's reach
LIVE = ("pending", "open", "closing")
_status = {"state": "idle", "pct": 0, "message": "", "at": None}
_lock = threading.Lock()


# --- settings and storage ---------------------------------------------------------------------------------

def per_position() -> float:
    with get_db() as conn:
        v = get_setting(conn, "follow.per_position")
    return float(v) if v else DEFAULT_PER_POSITION


def set_per_position(v: float) -> float:
    v = max(1000.0, float(v))
    with get_db() as conn:
        set_setting(conn, "follow.per_position", str(v))
    return v


def _rows(conn, live_only: bool = False) -> list[dict]:
    q = "SELECT payload FROM screen_follow" + (" WHERE status IN ('pending', 'open', 'closing')" if live_only else "")
    return [json.loads(r[0]) for r in conn.execute(q)]


def _put(conn, p: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO screen_follow (symbol, breakout_date, status, payload) VALUES (?, ?, ?, ?)",
                 (p["symbol"], p["breakout_date"], p["status"], json.dumps(p)))


# --- one position, one candle at a time ---------------------------------------------------------------------

def new_position(symbol: str, token: int, breakout_date: str, signal_date: str, screens: list, pivot: float,
                 cfg) -> dict:
    return {"symbol": symbol, "token": token, "breakout_date": breakout_date, "signal_date": signal_date,
            "screens": sorted(set(screens), key=SCREENS.index), "pivot": round(pivot, 2),
            "stop": round(pivot * (1 - cfg.played_out_drop), 2), "ma": cfg.played_out_ma, "status": "pending",
            "entry_date": None, "entry": None, "qty": 0, "value": 0.0, "charges": 0.0, "exit_date": None, "exit": None,
            "reason": None, "gross": 0.0, "net": 0.0, "pct": None, "last_date": signal_date, "last": None, "days": 0}


def step(p: dict, c: dict, ma: Optional[float], per_pos: float) -> None:
    """The position through one later session's candle: the pending buy / sell fills at its open, then the close
    is checked against the exit levels."""
    if c["date"] <= p["last_date"]:
        return
    if p["status"] == "pending":
        qty = int(per_pos // c["open"]) if c["open"] else 0
        if qty < 1:
            p.update(status="closed", reason=f"one share costs more than ₹{per_pos:,.0f}", last_date=c["date"])
            return
        value = qty * c["open"]
        p.update(status="open", entry_date=c["date"], entry=round(c["open"], 2), qty=qty, value=round(value, 2),
                 charges=round(delivery_charges(value, False), 2))
    elif p["status"] == "closing":
        sold = p["qty"] * c["open"]
        gross = sold - p["qty"] * p["entry"]
        charges = p["charges"] + delivery_charges(sold, True)
        p.update(status="closed", exit_date=c["date"], exit=round(c["open"], 2), charges=round(charges, 2),
                 gross=round(gross, 2), net=round(gross - charges, 2), pct=round(gross / p["value"] * 100, 2),
                 last_date=c["date"], last=round(c["open"], 2))
        return
    p["last_date"], p["last"] = c["date"], round(c["close"], 2)
    if p["status"] == "open":
        p["days"] += 1
        p["gross"] = round((c["close"] - p["entry"]) * p["qty"], 2)
        p["net"] = round(p["gross"] - p["charges"] - delivery_charges(c["close"] * p["qty"], True), 2)
        p["pct"] = round(p["gross"] / p["value"] * 100, 2)
        if ma is not None and p["ma"] and c["close"] < ma:
            p.update(status="closing", reason=f"closed below its {p['ma']}-day line")
        elif c["close"] <= p["stop"]:
            p.update(status="closing", reason=f"fell {round((1 - p['stop'] / p['pivot']) * 100)}% below the pivot")


def _ma_at(closes: list, n: int) -> Optional[float]:
    return float(np.mean(closes[-n:])) if n and len(closes) >= n else None


# --- the backfill: every breakout of the last MONTHS, judged on the candles up to its day ---------------------

class _Ratings:
    """RS ratings of the whole universe as of a date (each symbol's closes up to that date), computed when asked."""

    def __init__(self, series: dict, trend_cfg):
        self.series, self.cfg, self.cache = series, trend_cfg, {}
        self.dates = {s: [c["date"] for c in cs] for s, cs in series.items()}

    def at(self, day: str) -> dict:
        if day not in self.cache:
            scores = {}
            for s, cs in self.series.items():
                k = bisect_right(self.dates[s], day)
                scores[s] = rs_score([c["close"] for c in cs[:k]], self.cfg) if k else None
            self.cache[day] = rs_ratings(scores)
        return self.cache[day]


def _fresh_screens(prefix: list, rs: Optional[int], config: AppConfig) -> list:
    """The screens on which the stock is a fresh breakout on the last bar of `prefix` (the scan's classifiers)."""
    tt = trend_template(prefix, rs, config.trend)["passed"]
    out = []
    if classify(prefix, detect_bases(prefix, config.bases), config.screen).stage == "fresh":
        out.append("bases")
    if classify_vcp(prefix, detect_bases(prefix, config.bases), rs, tt, config.screen, config.vcp)[0].stage == "fresh":
        out.append("vcp")
    if classify_flag(prefix, detect_flags(prefix, config.flag, config.bases), rs, tt, config.screen, config.flag)[0].stage == "fresh":
        out.append("flag")
    return out


def _signals(symbol: str, candles: list, start: str, rs_at, config: AppConfig) -> list[tuple[int, list]]:
    """[(bar index, screens)] of the breakouts on or after `start` that were fresh on their own day."""
    idx = set()
    for b in detect_bases(candles, config.bases):
        if b.breakout and b.breakout.date >= start:
            idx.add(b.breakout.index)
    for b in detect_flags(candles, config.flag, config.bases):
        if b.breakout and b.breakout.date >= start:
            idx.add(b.breakout.index)
    out = []
    for t in sorted(idx):
        prefix = candles[:t + 1]
        scr = _fresh_screens(prefix, rs_at(prefix[-1]["date"]).get(symbol), config)
        if scr:
            out.append((t, scr))
    return out


def _simulate(symbol: str, token: int, candles: list, signals: list, config: AppConfig, per_pos: float) -> list:
    """The stock's positions from its signals through its candles, one at a time."""
    closes = [c["close"] for c in candles]
    ma = sma(np.array(closes, dtype=float), config.screen.played_out_ma) if config.screen.played_out_ma else None
    by_bar = {t: scr for t, scr in signals}
    out, p = [], None
    for j in range(min(by_bar), len(candles)) if by_bar else []:
        if p is not None and p["status"] != "closed":
            step(p, candles[j], float(ma[j]) if ma is not None and not np.isnan(ma[j]) else None, per_pos)
            if j in by_bar:                              # flagged again while held: merge the tags
                p["screens"] = sorted(set(p["screens"]) | set(by_bar[j]), key=SCREENS.index)
            continue
        if j in by_bar:
            pivot = _pivot_at(candles[:j + 1], config)
            if pivot:
                p = new_position(symbol, token, candles[j]["date"], candles[j]["date"], by_bar[j], pivot, config.screen)
                out.append(p)
    return out


def _pivot_at(prefix: list, config: AppConfig) -> Optional[float]:
    for find in (lambda: detect_bases(prefix, config.bases), lambda: detect_flags(prefix, config.flag, config.bases)):
        last = next((b for b in reversed(find()) if b.breakout is not None), None)
        if last is not None and last.breakout.index == len(prefix) - 1:
            return last.pivot
    return None


def rebuild(config: AppConfig, symbols: list[dict]) -> dict:
    """Everything again from the saved daily candles (the last MONTHS). Replaces the book."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        _status.update(state="running", pct=0, message="reading candles", at=now_ist())
        with get_db() as conn:
            series = {s["symbol"]: get_candles(conn, s["instrument_token"]) for s in symbols}
        tokens = {s["symbol"]: s["instrument_token"] for s in symbols}
        as_of = max((c[-1]["date"] for c in series.values() if c), default=None)
        if not as_of:
            _status.update(state="idle", message="no daily candles saved yet")
            return {"status": "empty"}
        start = (date.fromisoformat(as_of) - timedelta(days=int(MONTHS * 30.44))).isoformat()
        ratings = _Ratings(series, config.trend)
        per_pos = per_position()
        book, n = [], 0
        for k, (sym, candles) in enumerate(series.items()):
            if k % 10 == 0:
                _status.update(pct=round(95 * k / max(len(series), 1)), message=f"{sym}: {k} of {len(series)} stocks · {n} positions")
            if len(candles) < 60 or candles[-1]["date"] < start:
                continue
            sig = _signals(sym, candles, start, ratings.at, config)
            if sig:
                ps = _simulate(sym, tokens[sym], candles, sig, config, per_pos)
                book += ps
                n += len(ps)
        with get_db() as conn:
            conn.execute("DELETE FROM screen_follow")
            for p in book:
                _put(conn, p)
            set_setting(conn, "follow.as_of", as_of)
            set_setting(conn, "follow.rebuilt", now_ist())
        _status.update(state="idle", pct=100, message=f"done · {n} positions from {start} to {as_of}", at=now_ist())
        return {"status": "ok", "positions": n, "as_of": as_of}
    except Exception as exc:
        logger.exception("Follow rebuild failed")
        _status.update(state="idle", message=f"failed: {exc}")
        return {"status": "failed", "error": str(exc)}
    finally:
        _lock.release()


def start_rebuild(config: AppConfig, symbols: list[dict]) -> dict:
    if _lock.locked():
        return {"status": "busy"}
    threading.Thread(target=rebuild, args=(config, symbols), name="follow-rebuild", daemon=True).start()
    return {"status": "started"}


def status() -> dict:
    return dict(_status)


# --- the daily step after the evening scan --------------------------------------------------------------------

def update(config: AppConfig, as_of: str, symbols: list[dict]) -> dict:
    """The day's fresh rows (from scan_results) open positions; every live position steps through its candles
    after the last one it saw (so a missed day heals). Safe to run twice."""
    tokens = {s["symbol"]: s["instrument_token"] for s in symbols}
    per_pos = per_position()
    with get_db() as conn:
        fresh = defaultdict(dict)                          # symbol -> {breakout_date, pivot, screens}
        for scr, payload in conn.execute("SELECT screen, payload FROM scan_results WHERE as_of = ? AND stage = 'fresh'", (as_of,)):
            r = json.loads(payload)
            if scr in SCREENS and r.get("pivot") and r.get("breakout_date"):
                f = fresh[r["symbol"]]
                f.setdefault("screens", []).append(scr)
                f.update(breakout_date=r["breakout_date"], pivot=r["pivot"])
        live = {p["symbol"]: p for p in _rows(conn, live_only=True)}
        seen = {(r[0], r[1]) for r in conn.execute("SELECT symbol, breakout_date FROM screen_follow")}
        opened = 0
        for sym, f in fresh.items():
            if sym in live:
                p = live[sym]
                p["screens"] = sorted(set(p["screens"]) | set(f["screens"]), key=SCREENS.index)
            elif (sym, f["breakout_date"]) not in seen and sym in tokens:
                live[sym] = new_position(sym, tokens[sym], f["breakout_date"], as_of, f["screens"], f["pivot"], config.screen)
                opened += 1
        for sym, p in live.items():
            if p["last_date"] < as_of:
                cs = get_candles(conn, p["token"], limit=max(config.screen.played_out_ma, 1) + 40)
                closes = [c["close"] for c in cs]
                for i, c in enumerate(cs):
                    if c["date"] > p["last_date"] and p["status"] != "closed":
                        step(p, c, _ma_at(closes[:i + 1], config.screen.played_out_ma), per_pos)
            _put(conn, p)
        set_setting(conn, "follow.as_of", as_of)
    return {"as_of": as_of, "opened": opened, "live": len(live)}


# --- the report -----------------------------------------------------------------------------------------------

def _agg(ps: list) -> dict:
    closed = [p for p in ps if p["status"] == "closed" and p["qty"]]
    won = [p for p in closed if p["net"] > 0]
    lost = [p for p in closed if p["net"] <= 0]
    return {"breakouts": len([p for p in ps if p["qty"]]), "open": len([p for p in ps if p["status"] in LIVE]),
            "closed": len(closed), "won": len(won), "win_rate": round(100 * len(won) / len(closed), 1) if closed else None,
            "avg_win": round(sum(p["pct"] for p in won) / len(won), 2) if won else None,
            "avg_loss": round(sum(p["pct"] for p in lost) / len(lost), 2) if lost else None,
            "net": round(sum(p["net"] for p in ps if p["qty"]), 2),
            "open_net": round(sum(p["net"] for p in ps if p["status"] in LIVE and p["qty"]), 2),
            "value": round(sum(p["value"] for p in ps if p["qty"]), 2)}


def _peak(ps: list) -> dict:
    """The most money in open positions at once (nothing is skipped, so this is what following all of it needs)."""
    ev = []
    for p in ps:
        if p["entry_date"]:
            ev.append((p["entry_date"], 1, p["value"]))
            if p["exit_date"]:
                ev.append((p["exit_date"], 0, -p["value"]))
    ev.sort()
    cur, n, best = 0.0, 0, {"value": 0.0, "positions": 0, "date": None}
    for d, kind, v in ev:
        cur += v
        n += 1 if kind else -1
        if cur > best["value"]:
            best = {"value": round(cur, 2), "positions": n, "date": d}
    return best


def _drawdown(ps: list) -> float:
    """Worst fall of the cumulative closed net, in ₹."""
    eq = peak = dd = 0.0
    for p in sorted((p for p in ps if p["status"] == "closed" and p["qty"]), key=lambda p: p["exit_date"]):
        eq += p["net"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return round(dd, 2)


def _path(conn, p: dict, as_of: Optional[str]) -> list:
    """[[date, open, close]] for every session from the entry day to the exit day (or today): the page re-runs the
    exits on it (a tighter stop, a time stop: both can only sell earlier), sums P&L day by day for the growth chart
    and the drawdown. charges_in: the buy's delivery charges, so the page can cost any exit."""
    if not p.get("entry_date"):
        return []
    end = p["exit_date"] or as_of or p["last_date"]
    return [[c["date"], c["open"], c["close"]] for c in get_candles(conn, p["token"], start=p["entry_date"]) if c["date"] <= end]


def report(index_token: Optional[int] = None) -> dict:
    """Everything the page shows; index_token: NIFTY 50's daily closes over the same dates for the growth chart."""
    with get_db() as conn:
        ps = _rows(conn)
        as_of, rebuilt = get_setting(conn, "follow.as_of"), get_setting(conn, "follow.rebuilt")
        for p in ps:
            p["path"] = _path(conn, p, as_of)
            p["charges_in"] = round(delivery_charges(p["value"], False), 2) if p.get("qty") else 0.0
        first = min((p["entry_date"] for p in ps if p.get("entry_date")), default=None)
        nifty = [[c["date"], c["close"]] for c in get_candles(conn, index_token, start=first)
                 if not as_of or c["date"] <= as_of] if index_token and first else []
    ps.sort(key=lambda p: (p["signal_date"], p["symbol"]), reverse=True)
    by_screen = {k: _agg([p for p in ps if k in p["screens"]]) for k in SCREENS}
    by_screen["vcp+bases"] = _agg([p for p in ps if "vcp" in p["screens"] and "bases" in p["screens"]])
    return {"status": status(), "per_position": per_position(), "as_of": as_of, "rebuilt": rebuilt, "months": MONTHS,
            "total": {**_agg(ps), "peak": _peak(ps), "drawdown": _drawdown(ps)}, "by_screen": by_screen,
            "names": {**NAMES, "vcp+bases": "VCP + Bases together"}, "rows": ps, "nifty": nifty}
