"""Track record: every breakout the screens would have shown over the last year, the trade the swing rules
make from it, and the portfolio those trades add up to, against the Nifty 500.

Signal    a volume-confirmed close above a pivot that the screen showed that day, found by replaying the
          stored candles with the same detectors (they are causal: a breakout is known on its own bar).
          Gates as of the breakout date: liquidity and the trend line (ScreenConfig); VCP: the VCP rules on
          the base before the breakout; cup / flag: the pattern's RS and Trend Template minimums. RS ratings,
          the Trend Template, the industry rank and the market state are recomputed for that date.
          One signal per stock and day: VCP > Cup > Flag > Bases.
Trade     entry = the next session's open; no entry when it opens beyond pivot + buy_zone_pct (don't chase)
          or at / under the stop. stop = the pattern stop, never more than max_stop under the entry.
          Each session, in order:
            open under the stop (after the entry day)      -> exit at the open
            high reaches +book_half_pct                    -> sell half there (at the open if it gapped over)
            close under the stop                           -> exit at the close
            half sold and close under the exit_ma SMA      -> exit the rest at the close
            close >= +breakeven_pct                        -> stop to the entry from the next session
            max_hold sessions                              -> valued at that close ("3M mark")
          One open trade per stock: a breakout while one runs is "in_trade"; after the exit it is a re-entry.
Portfolio capital, risk_pct of equity per trade sized by the stop, max_positions open at once.

Built once (window_days back); each evening only new breakouts are added and pending / running trades are
re-run from their entry with the new candle (the same result as stepping a day). Closed rows never change.
"""

import json
import threading
from bisect import bisect_right
from datetime import date, timedelta
from math import floor
from typing import Optional

import numpy as np

from backend.config import AppConfig
from backend.database.models import get_candles, get_db, get_setting, now_ist, set_setting
from backend.engine import market
from backend.engine.bases import detect_bases
from backend.engine.cups import detect_cups
from backend.engine.flags import detect_flags
from backend.engine.indicators import sma
from backend.engine.rs import industry_ranks, rs_ratings, rs_score
from backend.engine.trend_template import trend_template
from backend.engine.vcp import evaluate
from backend.logging_config.logger import setup_logger

logger = setup_logger("track")

PATTERNS = ("vcp", "cup", "flag", "bases")                # preference when one day has several
FINAL = ("won", "lost", "no_entry", "in_trade")          # these rows never change
SNAPS = (("1w", 5), ("2w", 10), ("1m", 21), ("3m", 63))  # sessions from the entry (entry day = 1)

_status = {"state": "idle", "done": 0, "total": 0, "message": "", "at": None}
_lock = threading.Lock()


def status() -> dict:
    return dict(_status)


def _r(x, nd=2):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


# --- the trade rules ---------------------------------------------------------------------

class Bars:
    """One stock's candles as arrays, with the exit SMA."""

    def __init__(self, candles: list[dict], exit_ma: int):
        self.candles = candles
        self.dates = [c["date"] for c in candles]
        self.open = np.array([c["open"] for c in candles], dtype=float)
        self.high = np.array([c["high"] for c in candles], dtype=float)
        self.low = np.array([c["low"] for c in candles], dtype=float)
        self.close = np.array([c["close"] for c in candles], dtype=float)
        self.vol = np.array([c["volume"] for c in candles], dtype=float)
        self.ma = sma(self.close, exit_ma) if len(candles) else np.array([])

    def index(self, d: str) -> Optional[int]:
        i = bisect_right(self.dates, d) - 1
        return i if i >= 0 and self.dates[i] == d else None


def simulate(b: Bars, t: int, pivot: float, pattern_stop: float, cfg, buy_zone_pct: float) -> dict:
    """The trade from a breakout at bar t (see the module docstring)."""
    n, e = len(b.dates), t + 1
    if e >= n:
        return {"status": "pending", "reason": "entry at the next open"}
    op = float(b.open[e])
    zone_top = pivot * (1 + buy_zone_pct)
    stop0 = max(pattern_stop, op * (1 - cfg.max_stop))
    if op > zone_top or op <= stop0:
        why = (f"opened {(op / pivot - 1) * 100:+.1f}% over the pivot, beyond the buy zone" if op > zone_top
               else "opened at or under the stop")
        last = min(n - 1, e + cfg.max_hold - 1)
        return {"status": "no_entry", "reason": why, "entry_date": b.dates[e], "entry": _r(op),
                "raw_pct": _r((b.close[last] / op - 1) * 100)}
    stop, half, be = stop0, False, False
    realized, remaining = 0.0, 1.0
    events = [{"kind": "entry", "date": b.dates[e], "price": _r(op)}]
    marks, exit_i, reason = {}, None, None
    target = op * (1 + cfg.book_half_pct / 100)
    for j in range(e, min(n, e + cfg.max_hold)):
        if j > e and b.open[j] < stop:                                      # gapped under the stop
            px = float(b.open[j])
            realized += remaining * (px / op - 1)
            remaining, exit_i, reason = 0.0, j, ("gap under breakeven" if be else "gap under the stop")
            events.append({"kind": "exit", "date": b.dates[j], "price": _r(px), "why": reason})
            marks[j] = realized
            break
        if not half and b.high[j] >= target:                                # book half
            px = target if (j == e or b.open[j] < target) else float(b.open[j])
            realized += 0.5 * (px / op - 1)
            remaining, half = 0.5, True
            events.append({"kind": "half", "date": b.dates[j], "price": _r(px)})
        c = float(b.close[j])
        if c < stop:
            realized += remaining * (c / op - 1)
            remaining, exit_i, reason = 0.0, j, ("breakeven stop" if be else "stop")
            events.append({"kind": "exit", "date": b.dates[j], "price": _r(c), "why": reason})
            marks[j] = realized
            break
        if half and not np.isnan(b.ma[j]) and c < b.ma[j]:
            realized += remaining * (c / op - 1)
            remaining, exit_i, reason = 0.0, j, f"close under the {cfg.exit_ma}-day"
            events.append({"kind": "exit", "date": b.dates[j], "price": _r(c), "why": reason})
            marks[j] = realized
            break
        if not be and c >= op * (1 + cfg.breakeven_pct / 100):
            be, stop = True, max(stop, op)
            events.append({"kind": "breakeven", "date": b.dates[j], "price": _r(op)})
        marks[j] = realized + remaining * (c / op - 1)
        if j - e + 1 >= cfg.max_hold:
            realized += remaining * (c / op - 1)
            remaining, exit_i, reason = 0.0, j, "3M mark"
            events.append({"kind": "exit", "date": b.dates[j], "price": _r(c), "why": reason})
            break
    last = exit_i if exit_i is not None else n - 1
    result = marks[last]
    snaps = {}
    for name, k in SNAPS:
        idx = e + k - 1
        if exit_i is not None and idx >= exit_i:
            snaps[name] = _r(result * 100)
        elif idx < n and idx in marks:
            snaps[name] = _r(marks[idx] * 100)
    risk = (op - stop0) / op
    return {"status": "running" if exit_i is None else ("won" if result > 0 else "lost"), "reason": reason,
            "entry_date": b.dates[e], "entry": _r(op), "stop": _r(stop0), "current_stop": _r(stop),
            "exit_date": b.dates[exit_i] if exit_i is not None else None, "result_pct": _r(result * 100),
            "r": _r(result / risk) if risk > 0 else None, "days": last - e + 1,
            "mfe": _r((b.high[e:last + 1].max() / op - 1) * 100), "mae": _r((b.low[e:last + 1].min() / op - 1) * 100),
            "snaps": snaps, "events": events, "half": half}


# --- signals: replay the detectors, gate as of the breakout date ----------------------------------

def rs_scores(close: np.ndarray, cfg) -> list[Optional[float]]:
    """rs_score() for every bar at once (same weights, periods and minimum history)."""
    out = []
    for i in range(len(close)):
        n = i + 1
        if n <= cfg.rs_min_bars:
            out.append(None)
            continue
        total = wsum = 0.0
        for w, per in zip(cfg.rs_weights, cfg.rs_periods):
            if n > per and close[i - per] > 0:
                total += w * (close[i] / close[i - per] - 1)
                wsum += w
        out.append(total / wsum if wsum else None)
    return out


def _rolling(c: np.ndarray, w: int, op) -> np.ndarray:
    """Max / min of the last w values at each bar (fewer at the start), like c[max(0, i-w+1):i+1]."""
    out = op.accumulate(c) if len(c) else c.copy()
    if len(c) >= w:
        win = np.lib.stride_tricks.sliding_window_view(c, w)
        out[w - 1:] = win.max(axis=1) if op is np.maximum else win.min(axis=1)
    return out


class Universe:
    """Every stock's candles plus the per-date cross-sections the gates need (computed once per stock,
    looked up per date)."""

    def __init__(self, config: AppConfig, stocks: list[dict], index_token: Optional[int]):
        self.cfg = config
        with get_db() as conn:
            series = {s["symbol"]: get_candles(conn, s["instrument_token"]) for s in stocks}
            self.index = get_candles(conn, index_token) if index_token is not None else []
        self.series = {k: v for k, v in series.items() if v}
        self.dates = {k: [c["date"] for c in v] for k, v in self.series.items()}
        self.at = {k: {d: i for i, d in enumerate(v)} for k, v in self.dates.items()}
        self.industry = {s["symbol"]: s.get("industry") for s in stocks}
        self._pre: dict[str, dict] = {}
        self._vol: Optional[dict] = None
        self._rs: dict[str, tuple] = {}
        self._mkt: dict[str, Optional[str]] = {}

    def pre(self, sym: str) -> dict:
        """Per-stock arrays: RS score per bar, 50 / 200-day SMAs, 252-bar high and low of the close."""
        if sym not in self._pre:
            c = np.array([x["close"] for x in self.series[sym]], dtype=float)
            hi, lo = _rolling(c, 252, np.maximum), _rolling(c, 252, np.minimum)
            self._pre[sym] = {"close": c, "rs": rs_scores(c, self.cfg.trend), "sma50": sma(c, 50),
                              "sma200": sma(c, 200), "hi252": hi, "lo252": lo}
        return self._pre[sym]

    def ratings(self, d: str) -> tuple[dict, dict]:
        """(RS rating, industry rank) of every stock with a bar on date d (as rs_ratings / industry_ranks)."""
        if d not in self._rs:
            scores = {sym: self.pre(sym)["rs"][self.at[sym][d]] for sym in self.series if d in self.at[sym]}
            r = rs_ratings(scores)
            self._rs[d] = (r, industry_ranks(r, {s: self.industry.get(s) for s in r}))
        return self._rs[d]

    def _breadth(self, d: str) -> dict:
        """market.breadth() on date d."""
        a50 = n50 = a200 = n200 = highs = lows = 0
        for sym in self.series:
            k = self.at[sym].get(d)
            if k is None:
                continue
            p = self.pre(sym)
            c = p["close"][k]
            if k + 1 >= 50:
                n50 += 1
                a50 += c > p["sma50"][k]
            if k + 1 >= 200:
                n200 += 1
                a200 += c > p["sma200"][k]
            if k + 1 >= 252:
                highs += c >= p["hi252"][k]
                lows += c <= p["lo252"][k]
        pct = lambda a, n: round(100 * a / n, 1) if n else None  # noqa: E731
        return {"above_50_pct": pct(a50, n50), "above_200_pct": pct(a200, n200), "new_highs": int(highs),
                "new_lows": int(lows), "counted": n50}

    def _dist_days(self, idx: list[dict]) -> Optional[int]:
        """market.distribution_days() on the index cut at a date (total stock volume per date, precomputed)."""
        cfg = self.cfg.market
        if len(idx) < cfg.dist_window + 1:
            return None
        if self._vol is None:
            self._vol = {}
            for cs in self.series.values():
                for x in cs:
                    self._vol[x["date"]] = self._vol.get(x["date"], 0.0) + x["volume"]
        days, window = 0, idx[-(cfg.dist_window + 1):]
        for prev, cur in zip(window, window[1:]):
            drop = (cur["close"] / prev["close"] - 1) * 100
            if drop <= -cfg.dist_drop_pct and self._vol.get(cur["date"], 0) > self._vol.get(prev["date"], 0) > 0:
                days += 1
        return days

    def market(self, d: str) -> Optional[str]:
        """Market state on date d from the Nifty 500, breadth and distribution days (the breakout-success
        signal isn't replayed, and market.classify skips it)."""
        if d not in self._mkt:
            idx = self.index[:bisect_right([c["date"] for c in self.index], d)]
            if not idx:
                self._mkt[d] = None
            else:
                sig = {"as_of": d, "index": market.index_signals(idx), "breadth": self._breadth(d),
                       "dist_days": self._dist_days(idx), "breakouts": {"rate": None}}
                self._mkt[d] = market.classify(sig, self.cfg.market)
        return self._mkt[d]


def _gates_ok(b: Bars, t: int, screen_cfg) -> bool:
    """Liquidity and the trend line on the breakout bar (as classify() checks them)."""
    tw = min(screen_cfg.turnover_window, t + 1)
    turnover = float(np.median(b.close[t + 1 - tw:t + 1] * b.vol[t + 1 - tw:t + 1])) / 1e7
    if turnover < screen_cfg.min_turnover_cr:
        return False
    if screen_cfg.trend_ma > 0:
        ma = sma(b.close[:t + 1], screen_cfg.trend_ma)[t]
        if np.isnan(ma) or b.close[t] <= ma:
            return False
    return True


def find_signals(u: Universe, sym: str, since: str) -> list[dict]:
    """Breakouts on or after `since` that the screens would have shown, one per day (VCP > Cup > Flag > Bases)."""
    cfg, candles = u.cfg, u.series[sym]
    b = Bars(candles, cfg.track.exit_ma)
    found = {"bases": detect_bases(candles, cfg.bases),
             "cup": detect_cups(candles, cfg.cup, cfg.bases, cfg.vcp.max_stop),
             "flag": detect_flags(candles, cfg.flag, cfg.bases)}
    by_day: dict[str, dict] = {}
    for kind, items in (("bases", found["bases"]), ("cup", found["cup"]), ("flag", found["flag"])):
        for base in items:
            bo = base.breakout
            if bo is None or bo.date < since or (cfg.screen.require_volume and not bo.volume_ok):
                continue
            t = bo.index
            if not _gates_ok(b, t, cfg.screen):
                continue
            ratings, ind = u.ratings(bo.date)
            rs = ratings.get(sym)
            tt = trend_template(candles[:t + 1], rs, cfg.trend)
            candidates = []
            if kind == "bases":
                candidates.append(("bases", base.pivot * (1 - cfg.vcp.max_stop)))
                v = evaluate(candles, base, rs, tt["passed"], cfg.vcp, end=base.end_index)
                if v.qualifies and v.stop:
                    candidates.append(("vcp", v.stop))
            else:
                pcfg = cfg.cup if kind == "cup" else cfg.flag
                if rs is not None and rs >= pcfg.min_rs and tt["passed"] >= pcfg.min_trend_checks:
                    candidates.append((kind, (base.shape or {}).get("stop") or base.pivot * (1 - cfg.vcp.max_stop)))
            ir = ind.get(u.industry.get(sym) or "", {})
            for pattern, stop in candidates:
                row = {"symbol": sym, "breakout_date": bo.date, "t": t, "pattern": pattern, "pivot": _r(base.pivot),
                       "pattern_stop": _r(stop), "rs": rs, "tt_passed": tt["passed"], "base_label": base.label,
                       "volume_ratio": bo.volume_ratio, "industry_rank": ir.get("rank"), "industry_of": ir.get("of")}
                cur = by_day.get(bo.date)
                if cur is None or PATTERNS.index(pattern) < PATTERNS.index(cur["pattern"]):
                    by_day[bo.date] = row
    out = []
    for d in sorted(by_day):
        row = by_day[d]
        mkt = u.market(d)
        p = {"vcp": row["pattern"] == "vcp", "cup": row["pattern"] == "cup", "flag": row["pattern"] == "flag",
             "rs": row["rs"], "tt_passed": row["tt_passed"], "industry_rank": row["industry_rank"],
             "industry_of": row["industry_of"]}                     # no symbol: today's red flags don't apply
        row["market"], row["tier"] = mkt, market.tier(p, mkt, cfg.alerts)
        out.append(row)
    return out


# --- build and the daily update -----------------------------------------------------------

def _universe(config: AppConfig) -> Universe:
    from backend.ingest import universe as uni
    u = uni.resolve_universe(config.ingest)
    index = next((i["instrument_token"] for i in u["indices"] if i["symbol"] == uni.INDEX_NAME), None)
    return Universe(config, u["stocks"], index)


def _row_values(sig: dict, trade: dict, trade_no: Optional[int]) -> tuple:
    return (sig["symbol"], sig["breakout_date"], sig["pattern"], sig["pivot"], sig["pattern_stop"], sig.get("tier"),
            sig.get("market"), sig.get("rs"), sig.get("tt_passed"), sig.get("base_label"), sig.get("volume_ratio"),
            trade["status"], trade.get("reason"), trade_no, trade.get("entry_date"), trade.get("entry"),
            trade.get("stop"), trade.get("exit_date"), trade.get("result_pct"), trade.get("r"), trade.get("days"),
            trade.get("mfe"), trade.get("mae"), json.dumps(trade.get("snaps") or {}),
            json.dumps(trade.get("events") or ([] if "raw_pct" not in trade else [{"kind": "raw", "pct": trade["raw_pct"]}])),
            now_ist())


UPSERT = ("INSERT OR REPLACE INTO track_signals (symbol, breakout_date, pattern, pivot, pattern_stop, tier, market, "
          "rs, tt_passed, base_label, volume_ratio, status, reason, trade_no, entry_date, entry, stop, exit_date, "
          "result_pct, r, days, mfe, mae, snaps, events, updated_at) VALUES "
          "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")


def _replay_symbol(u: Universe, sym: str, stored: list[dict], new: list[dict]) -> list[tuple]:
    """Re-run the open trades and place the new signals for one stock; closed rows are kept as they are."""
    cfg = u.cfg
    b = Bars(u.series[sym], cfg.track.exit_ma)
    rows = sorted([dict(r, _stored=True) for r in stored] + new, key=lambda r: r["breakout_date"])
    busy_until, trade_no, out = None, 0, []
    for r in rows:
        if r.get("_stored") and r["status"] in FINAL:
            if r["status"] in ("won", "lost"):
                trade_no = max(trade_no, r.get("trade_no") or 0)
                busy_until = r.get("exit_date")
            continue
        t = b.index(r["breakout_date"])
        if t is None:
            continue
        if busy_until is not None and (busy_until == "open" or busy_until >= r["breakout_date"]):
            out.append(_row_values(r, {"status": "in_trade", "reason": "breakout while a trade was open"}, None))
            continue
        trade = simulate(b, t, r["pivot"], r["pattern_stop"], cfg.track, cfg.alerts.buy_zone_pct)
        no = None
        if trade["status"] in ("running", "won", "lost"):
            trade_no += 1
            no = trade_no
            busy_until = trade["exit_date"] or "open"
        out.append(_row_values(r, trade, no))
    return out


def build(config: AppConfig, rebuild: bool = False) -> dict:
    """First build (or a deliberate rebuild): every breakout in the last window_days. Otherwise the daily update."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        with get_db() as conn:
            built = get_setting(conn, "track.built_at")
            if rebuild:
                conn.execute("DELETE FROM track_signals")
                built = None
        _status.update(state="building" if not built else "updating", done=0, message="", at=now_ist())
        u = _universe(config)
        if not u.series:
            _status.update(state="idle", message="no candles yet")
            return {"status": "no_data"}
        latest = max(d[-1] for d in u.dates.values())
        since = (date.fromisoformat(latest) - timedelta(days=config.track.window_days)).isoformat()
        with get_db() as conn:
            stored = {}
            for r in conn.execute("SELECT * FROM track_signals"):
                stored.setdefault(r["symbol"], []).append(dict(r))
            last = conn.execute("SELECT MAX(breakout_date) FROM track_signals").fetchone()[0]
        start = since if not built or not last else max(since, last)
        _status["total"] = len(u.series)
        rows, added = [], 0
        for i, sym in enumerate(sorted(u.series)):
            mine = stored.get(sym, [])
            have = {r["breakout_date"] for r in mine}
            try:
                new = [s for s in find_signals(u, sym, start) if s["breakout_date"] not in have]
            except Exception:
                logger.exception("Track record: signals for %s failed", sym)
                new = []
            added += len(new)
            if new or any(r["status"] not in FINAL for r in mine):
                rows += _replay_symbol(u, sym, mine, new)
            _status["done"] = i + 1
        with get_db() as conn:
            conn.executemany(UPSERT, rows)
            set_setting(conn, "track.built_at", built or now_ist())
            set_setting(conn, "track.updated_at", now_ist())
            set_setting(conn, "track.as_of", latest)
        msg = f"{'updated' if built else 'built'}: {added} new breakouts, {len(rows)} rows written (data to {latest})"
        logger.info("Track record %s", msg)
        _status.update(state="idle", message=msg, at=now_ist())
        return {"status": "ok", "added": added, "written": len(rows), "as_of": latest, "built": not built}
    except Exception as exc:
        logger.exception("Track record build failed")
        _status.update(state="idle", message=f"failed: {exc}")
        return {"status": "failed", "error": str(exc)}
    finally:
        _lock.release()


def built() -> bool:
    with get_db() as conn:
        return bool(get_setting(conn, "track.built_at"))


# --- reading: summary, portfolio curve, trades ---------------------------------------------

PERIODS = {"1w": 7, "1m": 31, "3m": 92, "6m": 183, "1y": 366}


def rows(period: str = "1y", pattern: str = "all", tier: str = "all", rules: str = "swing") -> list[dict]:
    """Rows of the swing record, or of an intraday rule set (track_intraday)."""
    with get_db() as conn:
        as_of = get_setting(conn, "track.as_of" if rules == "swing" else "track_intraday.as_of")
        if not as_of:
            return []
        since = (date.fromisoformat(as_of) - timedelta(days=PERIODS.get(period, 366))).isoformat()
        if rules == "swing":
            q, args = "SELECT * FROM track_signals WHERE breakout_date >= ?", [since]
        else:
            q, args = "SELECT * FROM track_intraday WHERE rules = ? AND breakout_date >= ?", [rules, since]
        if pattern in PATTERNS:
            q, args = q + " AND pattern = ?", args + [pattern]
        if tier in ("A", "B"):
            q, args = q + " AND tier = ?", args + [tier]
        out = []
        for r in conn.execute(q + " ORDER BY breakout_date DESC, symbol", args):
            d = dict(r)
            d["snaps"], d["events"] = json.loads(d["snaps"]), json.loads(d["events"])
            out.append(d)
    return out


def _avg(xs) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def stats(rs: list[dict]) -> dict:
    """Status line and the summary numbers for a set of rows."""
    by = {k: [r for r in rs if r["status"] == k] for k in ("pending", "running", "won", "lost", "no_entry", "in_trade")}
    closed, entered = by["won"] + by["lost"], by["won"] + by["lost"] + by["running"]
    win_r = sum(r["r"] for r in by["won"] if r["r"] is not None)
    loss_r = -sum(r["r"] for r in by["lost"] if r["r"] is not None)
    return {"counts": {"breakouts": len(rs) - len(by["in_trade"]), "entered": len(entered), "no_entry": len(by["no_entry"]),
                       "pending": len(by["pending"]), "closed": len(closed), "won": len(by["won"]), "lost": len(by["lost"]),
                       "running": len(by["running"]), "in_trade": len(by["in_trade"])},
            "win_rate": round(100 * len(by["won"]) / len(closed), 1) if closed else None,
            "avg_win": _avg([r["result_pct"] for r in by["won"]]), "avg_loss": _avg([r["result_pct"] for r in by["lost"]]),
            "expectancy_r": _avg([r["r"] for r in closed]),
            "snaps": {k: _avg([r["snaps"].get(k) for r in entered]) for k, _ in SNAPS},
            "hold_days": {"all": _avg([r["days"] for r in closed]), "won": _avg([r["days"] for r in by["won"]]),
                          "lost": _avg([r["days"] for r in by["lost"]])},
            "profit_factor": round(win_r / loss_r, 2) if loss_r > 0 else None,
            "stopped_pct": round(100 * sum(1 for r in closed if (r["reason"] or "").startswith(("stop", "gap under the")))
                                 / len(closed), 1) if closed else None}


def split(rs: list[dict], key: str) -> list[dict]:
    """Stats per pattern or per market state."""
    keys = PATTERNS if key == "pattern" else ("uptrend", "pressure", "correction")
    out = []
    for k in keys:
        sub = [r for r in rs if r.get(key) == k]
        if sub:
            s = stats(sub)
            out.append({key: k, "signals": s["counts"]["breakouts"], "entered": s["counts"]["entered"],
                        "win_rate": s["win_rate"], "snaps": s["snaps"], "stopped_pct": s["stopped_pct"],
                        "expectancy_r": s["expectancy_r"]})
    return out


SIZING = (("1% risk, cash-limited", 0.01, 0.0), ("0.75% risk", 0.0075, 0.0),
          ("1% risk, max 10% per position", 0.01, 0.10), ("1% risk, max 20% per position", 0.01, 0.20))


def _book(days, by_day, closes, capital, max_positions, risk_pct, position_cap, max_new_per_day=0):
    """Replay the trade events day by day on one account. Each entry risks `risk_pct` of equity to the stop, capped
    at `position_cap` of equity per position (0 = no cap) and by the cash left; at most max_new_per_day entries a
    day (0 = any). Events with a time (intraday) run in time order. -> (curve %, cash share, taken, skipped)"""
    order = {"exit": 0, "half": 1, "breakeven": 2, "entry": 3}           # sell before buying on the same day
    tier_rank = {"A": 0, "B": 1}
    cash, equity, pos = capital, capital, {}
    taken = skipped = 0
    strategy, idle, last_px = [], [], {}
    for d in days:
        evs = sorted(by_day.get(d, []), key=lambda x: (x[0].get("time", ""), order[x[0]["kind"]],
                                                        tier_rank.get(x[1].get("tier"), 2), PATTERNS.index(x[1]["pattern"])))
        new_today = 0
        for ev, r in evs:
            key = (r["symbol"], r["breakout_date"])
            if ev["kind"] == "entry":
                if len(pos) >= max_positions or not r.get("stop") or ev["price"] <= r["stop"] or (
                        max_new_per_day and new_today >= max_new_per_day):
                    skipped += 1
                    continue
                qty = floor(equity * risk_pct / (ev["price"] - r["stop"]))
                if position_cap > 0:
                    qty = min(qty, floor(equity * position_cap / ev["price"]))
                qty = min(qty, floor(cash / ev["price"]))
                if qty <= 0:
                    skipped += 1
                    continue
                cash -= qty * ev["price"]
                pos[key] = qty
                taken += 1
                new_today += 1
            elif key in pos and ev["kind"] in ("half", "exit"):
                q = pos[key] // 2 if ev["kind"] == "half" else pos[key]
                cash += q * ev["price"]
                pos[key] -= q
                if pos[key] <= 0 or ev["kind"] == "exit":
                    pos.pop(key, None)
        value = 0.0
        for (sym, _), q in pos.items():
            px = closes.get(sym, {}).get(d)
            if px is not None:
                last_px[sym] = px
            value += q * last_px.get(sym, 0.0)
        equity = cash + value
        strategy.append(round((equity / capital - 1) * 100, 2))
        idle.append(cash / equity if equity else 1.0)
    return strategy, idle, taken, skipped


def portfolio(rs: list[dict], config: AppConfig, max_new_per_day: int = 0) -> dict:
    """Daily account value if every signal in `rs` was taken under the portfolio rules, vs Nifty 500 buy and hold."""
    from backend.ingest import universe as uni
    cfg = config.track
    trades = [r for r in rs if r["status"] in ("running", "won", "lost")]
    if not rs:
        return {"dates": [], "strategy": [], "nifty": []}
    start = min(r["breakout_date"] for r in rs)
    u = uni.resolve_universe(config.ingest)
    tokens = {s["symbol"]: s["instrument_token"] for s in u["stocks"]}
    index_token = next((i["instrument_token"] for i in u["indices"] if i["symbol"] == uni.INDEX_NAME), None)
    with get_db() as conn:
        index = get_candles(conn, index_token, start=start) if index_token is not None else []
        closes = {}
        for sym in {r["symbol"] for r in trades}:
            if sym in tokens:
                closes[sym] = {c["date"]: c["close"] for c in get_candles(conn, tokens[sym], start=start)}
    days = [c["date"] for c in index] or sorted({d for m in closes.values() for d in m})
    by_day: dict[str, list] = {}
    for r in trades:
        for ev in r["events"]:
            by_day.setdefault(ev["date"], []).append((ev, r))
    strategy, idle, taken, skipped = _book(days, by_day, closes, cfg.capital, cfg.max_positions, cfg.risk_pct,
                                           cfg.position_cap, max_new_per_day)
    nifty = [round((c["close"] / index[0]["close"] - 1) * 100, 2) for c in index] if index else []

    def drawdown(curve):
        peak, worst = 0.0, 0.0
        for v in curve:
            peak = max(peak, v)
            worst = min(worst, (100 + v) / (100 + peak) * 100 - 100)
        return round(worst, 2)

    def months_up(curve):
        ends, prev, up, total = {}, None, 0, 0
        for d, v in zip(days, curve):
            ends[d[:7]] = v
        for v in ends.values():
            if prev is not None:
                total += 1
                up += v > prev
            prev = v
        return up, total

    sizing = []
    for label, risk, cap in SIZING:
        current = (risk, cap) == (cfg.risk_pct, cfg.position_cap)
        curve, _, _, sk = ((strategy, idle, taken, skipped) if current else
                           _book(days, by_day, closes, cfg.capital, cfg.max_positions, risk, cap, max_new_per_day))
        ret, dd = (curve[-1] if curve else None), drawdown(curve)
        sizing.append({"label": label, "risk_pct": risk, "position_cap": cap, "return": ret, "drawdown": dd,
                       "ratio": round(ret / -dd, 2) if ret is not None and dd < 0 else None, "skipped": sk,
                       "current": current})
    su, st = months_up(strategy)
    nu, nt = months_up(nifty) if nifty else (None, None)
    return {"dates": days, "strategy": strategy, "nifty": nifty, "taken": taken, "skipped": skipped,
            "compare": {"strategy": {"return": strategy[-1] if strategy else None,
                                     "value": round(cfg.capital * (1 + (strategy[-1] if strategy else 0) / 100)),
                                     "drawdown": drawdown(strategy), "months_up": [su, st],
                                     "cash_idle": round(100 * sum(idle) / len(idle), 1) if idle else None},
                        "nifty": {"return": nifty[-1] if nifty else None,
                                  "value": round(cfg.capital * (1 + nifty[-1] / 100)) if nifty else None,
                                  "drawdown": drawdown(nifty) if nifty else None, "months_up": [nu, nt],
                                  "cash_idle": 0.0}},
            "sizing": sizing,
            "settings": {"capital": cfg.capital, "risk_pct": cfg.risk_pct, "max_positions": cfg.max_positions,
                         "position_cap": cfg.position_cap, "max_new_per_day": max_new_per_day}}


def line(pattern: str, tier: Optional[str], mkt: Optional[str], min_trades: int = 10) -> Optional[str]:
    """'📈 Track record (1Y): VCP · tier A · uptrend → 61% won, +0.71R avg, 38 trades' (the closest group with
    enough closed trades), or a 'too few' note. None before the first build."""
    if not built():
        return None
    names = {"vcp": "VCP", "cup": "Cup", "flag": "Flag", "bases": "Bases"}
    all_rows = [r for r in rows("1y") if r["status"] in ("won", "lost")]
    for label, keep in ((f"{names.get(pattern, pattern)} · tier {tier} · {mkt}",
                         lambda r: r["pattern"] == pattern and r["tier"] == tier and r["market"] == mkt),
                        (f"{names.get(pattern, pattern)} · tier {tier}", lambda r: r["pattern"] == pattern and r["tier"] == tier),
                        (names.get(pattern, pattern), lambda r: r["pattern"] == pattern)):
        if tier is None and "tier" in label:
            continue
        sub = [r for r in all_rows if keep(r)]
        if len(sub) >= min_trades:
            won = sum(r["status"] == "won" for r in sub)
            exp = _avg([r["r"] for r in sub])
            return (f"📈 Track record (1Y): {label} → {round(100 * won / len(sub))}% won, "
                    f"{exp:+.2f}R avg, {len(sub)} trades")
    return f"📈 Track record (1Y): too few past {names.get(pattern, pattern)} trades to judge"
