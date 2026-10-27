"""Intraday entry backtest: the swing watchlist bought on the breakout day from 5-minute candles, with a tight
stop, part booked at a small target and the rest on an ATR trail. Two rule sets differ only in the trail:
i5m (5-minute ATR) and iday (daily ATR); iday_s50 / iday_s75 are iday with the stop at least 0.5% / 0.75% under the
entry (a closer one is moved down to that). The results sit next to the swing rules on the Track record tab.

Watchlist  each evening (day k-1): setups still intact and within today_near_pct under their pivot. Box bases
           as detect_bases knew them that evening; cups / flags ending that evening (find_cup / find_flag at
           k-1). Same gates as the swing record, as of k-1: liquidity, trend line, RS, Trend Template, the VCP
           rules, market state, tier. Day k is a candidate when its high crosses the pivot, *including* days
           that closed back under it: a failed breakout costs a stop and has to count.
Entry      day k from 5-minute candles (fetched from Kite for these days only):
             opened more than gap_max_pct over the last close         -> no entry
             a candle starting at entry_start or later and ending by entry_end, that closes over the pivot,
             at most max_ext_pct over it, over VWAP, with volume on pace for volume_pace x the 50-day average,
             while the Nifty 500 is over its opening price (indices have no volume, so no VWAP)
           entry = that candle's close (when the alert would fire).
           stop  = the closer of the opening-range low (first or_minutes) and the candle's low; more than
                   max_stop_pct under the entry -> that candle is passed over (a later one may qualify).
Trade      before the target: a low at / under the stop -> out at the stop (at the open if it opened under).
           Stop and target in one candle -> the stop (we can't tell which came first; assume the worse).
           high >= +target_pct -> sell book_frac there; the stop moves to the entry.
           The rest: out at the entry (hard stop), or on a close under the trail, highest high since the entry
           - atr_mult x ATR(atr_len):
             i5m   5-minute candles, checked on every candle's close
             iday  daily candles, checked at each day's close (5-minute candles no longer needed)
           After max_hold sessions it is valued at that close ("3M mark").
           One trade per stock at a time; a pivot that was traded is not traded again.
"""

import threading
from collections import Counter, deque
from datetime import date, datetime, time, timedelta
from typing import Optional

from backend.alerts.intraday import day_share
from backend.config import IST, AppConfig
from backend.database.models import get_db, get_setting, now_ist, set_setting
from backend.engine import market, track
from backend.engine.bases import detect_bases
from backend.engine.cups import _as_base as cup_base, find_cup
from backend.engine.flags import _as_base as flag_base, find_flag
from backend.engine.trend_template import trend_template
from backend.engine.vcp import evaluate
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm

logger = setup_logger("track_intraday")

VARIANTS = {"i5m": ("i5m", 0.0), "iday": ("iday", 0.0), "iday_s50": ("iday", 0.5), "iday_s75": ("iday", 0.75)}
RULES = tuple(VARIANTS)                                  # rule set -> (trail, minimum stop distance %)
NAMES = {"swing": "Swing · 8% stop", "i5m": "Intraday · 5-min ATR trail", "iday": "Intraday · daily ATR trail",
         "iday_s50": "Intraday · daily trail · stop ≥ 0.5%", "iday_s75": "Intraday · daily trail · stop ≥ 0.75%"}
VERSION = "2"                                            # the rule sets above; another version re-runs the backtest
PATTERNS = track.PATTERNS
FINAL = track.FINAL

_status = {"state": "idle", "done": 0, "total": 0, "message": "", "at": None}
_lock = threading.Lock()


def status() -> dict:
    return dict(_status)


def _r(x, nd=2):
    return track._r(x, nd)


def _plus(hhmm: str, minutes: int) -> str:
    t = datetime.combine(date.today(), time.fromisoformat(hhmm)) + timedelta(minutes=minutes)
    return t.strftime("%H:%M")


# --- watchlist: the pivot crosses -------------------------------------------------------------------

def watch_signals(u: track.Universe, sym: str, since: str) -> list[dict]:
    """Days on or after `since` whose high crossed the pivot of a setup on the evening watchlist (one per day:
    VCP > Cup > Flag > Bases), gated as of the evening before."""
    cfg, candles = u.cfg, u.series[sym]
    b = track.Bars(candles, cfg.track.exit_ma)
    dates, high, low, close, vol = b.dates, b.high, b.low, b.close, b.vol
    states: dict = {}
    detect_bases(candles, cfg.bases, states=states)
    near = cfg.alerts.today_near_pct / 100
    hc, fm = cfg.cup.handle_min_bars, cfg.flag.flag_min_bars
    out = []
    for k in range(1, len(candles)):
        if dates[k] < since:
            continue
        found = []
        st = states.get(k)
        if st and st[2] and high[k] > st[1]:
            base = next((x for x in reversed(detect_bases(candles[:k], cfg.bases)) if x.status == "forming"), None)
            if base is not None:
                found.append(("bases", base))
        if k > hc and high[k] > high[k - hc:k].max():
            c = find_cup(high, low, vol, k - 1, cfg.cup, close)
            if c is not None and high[k] > c["pivot"]:
                found.append(("cup", cup_base(c, dates, "forming", cfg.vcp.max_stop, cfg.cup)))
        if k > fm and high[k] > high[k - fm:k].max():
            f = find_flag(high, low, close, vol, k - 1, cfg.flag)
            if f is not None and high[k] > f["pivot"]:
                found.append(("flag", flag_base(f, dates, "forming", cfg.flag)))
        found = [(kind, base) for kind, base in found if close[k - 1] >= base.pivot * (1 - near)]
        if not found or not track._gates_ok(b, k - 1, cfg.screen):
            continue
        y = dates[k - 1]
        ratings, ind = u.ratings(y)
        rs = ratings.get(sym)
        tt = trend_template(candles[:k], rs, cfg.trend)
        best = None
        for kind, base in found:
            cands = []
            if kind == "bases":
                cands.append(("bases", base.pivot * (1 - cfg.vcp.max_stop)))
                v = evaluate(candles[:k], base, rs, tt["passed"], cfg.vcp, end=base.end_index)
                if v.qualifies and v.stop:
                    cands.append(("vcp", v.stop))
            else:
                pcfg = cfg.cup if kind == "cup" else cfg.flag
                if rs is not None and rs >= pcfg.min_rs and tt["passed"] >= pcfg.min_trend_checks:
                    cands.append((kind, (base.shape or {}).get("stop") or base.pivot * (1 - cfg.vcp.max_stop)))
            for pattern, stop in cands:
                if best is None or PATTERNS.index(pattern) < PATTERNS.index(best[0]):
                    best = (pattern, stop, base)
        if best is None:
            continue
        pattern, stop, base = best
        ir = ind.get(u.industry.get(sym) or "", {})
        mkt = u.market(y)
        p = {"vcp": pattern == "vcp", "cup": pattern == "cup", "flag": pattern == "flag", "rs": rs,
             "tt_passed": tt["passed"], "industry_rank": ir.get("rank"), "industry_of": ir.get("of")}
        out.append({"symbol": sym, "breakout_date": dates[k], "t": k, "pattern": pattern, "pivot": _r(base.pivot),
                    "pattern_stop": _r(stop), "rs": rs, "tt_passed": tt["passed"], "base_label": base.label,
                    "volume_ratio": None, "market": mkt, "tier": market.tier(p, mkt, cfg.alerts)})
    return out


# --- 5-minute candles ----------------------------------------------------------------------------

class FiveMin:
    """One instrument's 5-minute candles by day, from the database, fetching missing days from Kite (a day
    plus the next `ahead - 1` sessions per request). day() is None when a day can't be had (no Kite), []
    when Kite had no candles for it (the daily candle stands in)."""

    def __init__(self, client, token: int, label: str, sessions: list[str]):
        self.client, self.token, self.label, self.sessions = client, token, label, sessions
        self.cache: dict[str, list] = {}
        with get_db() as conn:
            self.have = {r[0] for r in conn.execute("SELECT day FROM candles_5m_days WHERE instrument_token = ?",
                                                    (token,))}
        self.fetched = 0

    def _load(self, days: list[str]) -> None:
        with get_db() as conn:
            for d in days:
                rows = conn.execute("SELECT ts, open, high, low, close, volume FROM candles_5m WHERE instrument_token = ? "
                                    "AND ts >= ? AND ts < ? ORDER BY ts", (self.token, d, d + "~")).fetchall()
                self.cache[d] = [dict(r, t=r["ts"][11:]) for r in rows]

    def day(self, d: str, ahead: int = 1) -> Optional[list]:
        if d in self.cache:
            return self.cache[d]
        if d not in self.have:
            if self.client is None or not getattr(self.client, "has_token", False):
                return None
            i = self.sessions.index(d) if d in self.sessions else None
            want = self.sessions[i:i + ahead] if i is not None else [d]
            want = [x for x in want if x not in self.have] or [d]
            try:
                rows = self.client.historical_bars(self.token, date.fromisoformat(want[0]),
                                                   date.fromisoformat(want[-1]), label=self.label)
            except Exception as exc:
                logger.info("5-minute candles for %s %s: %s", self.label, d, exc)
                return None
            self.fetched += 1
            with get_db() as conn:
                conn.executemany("INSERT OR REPLACE INTO candles_5m (instrument_token, ts, open, high, low, close, volume) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 [(self.token, r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"])
                                  for r in rows])
                conn.executemany("INSERT OR IGNORE INTO candles_5m_days (instrument_token, day) VALUES (?, ?)",
                                 [(self.token, x) for x in want])
            self.have.update(want)
        self._load([d])
        return self.cache[d]


# --- entry and trade --------------------------------------------------------------------------------

WHY = {"ext": "more than {ext:g}% over the pivot", "vwap": "under VWAP", "volume": "volume pace under {pace:g}×",
       "index": "Nifty 500 under its open", "stop": "stop more than {stop:g}% away"}


def find_entry(bars: list[dict], idx: Optional[list], pivot: float, prev_close: float, day_open: float,
               avg_vol: Optional[float], day: str, cfg) -> tuple[Optional[dict], Optional[str]]:
    """The first 5-minute candle that triggers the entry (see the module docstring), or (None, why not)."""
    if not bars:
        return None, "no 5-minute candles for the day"
    gap = (day_open / prev_close - 1) * 100 if prev_close else 0.0
    if gap > cfg.gap_max_pct:
        return None, f"opened {gap:+.1f}% over the last close (gap over {cfg.gap_max_pct:g}%)"
    or_end = _plus("09:15", cfg.or_minutes)
    or_lows = [x["low"] for x in bars if x["t"] < or_end]
    or_low = min(or_lows) if or_lows else None
    idx_open = idx[0]["open"] if idx else None
    idx_at = {x["t"]: x["close"] for x in idx or []}
    cum_v = cum_pv = 0.0
    fails: Counter = Counter()
    for i, x in enumerate(bars):
        cum_v += x["volume"]
        cum_pv += (x["high"] + x["low"] + x["close"]) / 3 * x["volume"]
        end = _plus(x["t"], 5)
        if x["t"] < cfg.entry_start:
            continue
        if end > cfg.entry_end:
            break
        c = x["close"]
        if c <= pivot:
            continue
        if c > pivot * (1 + cfg.max_ext_pct / 100):
            fails["ext"] += 1
            continue
        vwap = cum_pv / cum_v if cum_v else None
        if vwap is not None and c <= vwap:
            fails["vwap"] += 1
            continue
        share = day_share(datetime.combine(date.fromisoformat(day), time.fromisoformat(end), IST))
        pace = cum_v / share / avg_vol if avg_vol and share > 0 else None
        if pace is None or pace < cfg.volume_pace:
            fails["volume"] += 1
            continue
        if idx_open is not None and x["t"] in idx_at and idx_at[x["t"]] < idx_open:
            fails["index"] += 1
            continue
        lows = [v for v in (or_low, x["low"]) if v is not None and v < c]
        stop = max(lows) if lows else None
        if stop is None or (c - stop) / c * 100 > cfg.max_stop_pct:
            fails["stop"] += 1
            continue
        return {"i": i, "time": end, "price": c, "stop": stop, "pace": _r(pace), "vwap": _r(vwap)}, None
    if not fails:
        return None, f"no 5-minute close over the pivot by {ampm(cfg.entry_end)}"
    top = fails.most_common(1)[0][0]
    return None, "crossed the pivot, but " + WHY[top].format(ext=cfg.max_ext_pct, pace=cfg.volume_pace,
                                                             stop=cfg.max_stop_pct)


def _atr_daily(b: track.Bars, j: int, n: int) -> Optional[float]:
    if j < 1:
        return None
    lo = max(1, j - n + 1)
    trs = [max(b.high[i] - b.low[i], abs(b.high[i] - b.close[i - 1]), abs(b.low[i] - b.close[i - 1]))
           for i in range(lo, j + 1)]
    return sum(trs) / len(trs) if trs else None


def simulate(b: track.Bars, k: int, e: dict, five: FiveMin, cfg, variant: str) -> dict:
    """The trade from entry `e` (find_entry) on daily bar k, under rule set `variant` (i5m / iday)."""
    entry, stop0 = e["price"], e["stop"]
    target = entry * (1 + cfg.target_pct / 100)
    stop, booked, realized, remaining = stop0, False, 0.0, 1.0
    events = [{"kind": "entry", "date": b.dates[k], "time": e["time"], "price": _r(entry)}]
    hh, hi_seen, lo_seen = entry, entry, entry
    trs: deque = deque(maxlen=cfg.atr_len)
    trail5 = trail_d = None
    prev_c = None
    marks, exit_j, reason = {}, None, None
    n = len(b.dates)

    def out(j, t, px, why):
        nonlocal realized, remaining, exit_j, reason
        realized += remaining * (px / entry - 1)
        remaining, exit_j, reason = 0.0, j, why
        events.append({"kind": "exit", "date": b.dates[j], "time": t, "price": _r(px), "why": why})

    for j in range(k, min(n, k + cfg.max_hold)):
        d = b.dates[j]
        if not booked or variant == "i5m":
            bars = five.day(d, ahead=cfg.fetch_sessions)
            if bars is None:
                return {"status": "pending", "reason": f"5-minute candles for {d} not fetched yet"}
            if not bars:                                   # Kite had none: the daily candle stands in
                bars = [{"t": "09:15", "open": b.open[j], "high": b.high[j], "low": b.low[j], "close": b.close[j]}]
            if j == k:
                for x in bars[:e["i"] + 1]:                # the ATR starts from the day's candles before the entry
                    if prev_c is not None:
                        trs.append(max(x["high"] - x["low"], abs(x["high"] - prev_c), abs(x["low"] - prev_c)))
                    prev_c = x["close"]
                bars = bars[e["i"] + 1:]
            for x in bars:
                o, h, lo, c, t = x["open"], x["high"], x["low"], x["close"], _plus(x["t"], 5)
                if lo <= stop:
                    gapped = o < stop
                    why = ("gap under breakeven" if booked else "gap under the stop") if gapped and x["t"] == "09:15" \
                        else ("breakeven stop" if booked else "stop")
                    lo_seen = min(lo_seen, lo)
                    out(j, x["t"] if gapped else t, o if gapped else stop, why)
                    break
                hi_seen, lo_seen = max(hi_seen, h), min(lo_seen, lo)
                if booked and variant == "i5m" and trail5 is not None and c < trail5:
                    out(j, t, c, "ATR trail (5-min)")
                    break
                if not booked and h >= target:
                    px = o if o > target else target
                    realized += cfg.book_frac * (px / entry - 1)
                    remaining, booked, stop = 1 - cfg.book_frac, True, max(stop, entry)
                    events.append({"kind": "half", "date": d, "time": t, "price": _r(px), "frac": cfg.book_frac})
                hh = max(hh, h)
                if prev_c is not None:
                    trs.append(max(h - lo, abs(h - prev_c), abs(lo - prev_c)))
                prev_c = c
                if booked and variant == "i5m" and trs:
                    trail5 = max(trail5 or 0.0, hh - cfg.atr_mult * sum(trs) / len(trs))
            if exit_j is not None:
                break
        else:                                              # iday runner on a later day: the daily candle
            o, h, lo, c = b.open[j], b.high[j], b.low[j], b.close[j]
            hi_seen, lo_seen = max(hi_seen, h), min(lo_seen, lo)
            if o <= stop:
                out(j, "09:15", o, "gap under breakeven")
                break
            if lo <= stop:
                out(j, "15:30", stop, "breakeven stop")
                break
            hh = max(hh, h)
        if booked and variant == "iday":                   # the daily trail, at the close
            if trail_d is not None and b.close[j] < trail_d:
                out(j, "15:30", float(b.close[j]), "ATR trail (daily)")
                break
            atr = _atr_daily(b, j, cfg.atr_len)
            if atr:
                trail_d = max(trail_d or 0.0, max(hh, b.high[j]) - cfg.atr_mult * atr)
        marks[j] = realized + remaining * (b.close[j] / entry - 1)
        if j - k + 1 >= cfg.max_hold:
            out(j, "15:30", float(b.close[j]), "3M mark")
            break
    if exit_j is not None:
        marks[exit_j] = realized
    last = exit_j if exit_j is not None else (max(marks) if marks else k)
    result = marks.get(last, 0.0)
    snaps = {}
    for name, s in track.SNAPS:
        idx = k + s - 1
        if exit_j is not None and idx >= exit_j:
            snaps[name] = _r(result * 100)
        elif idx in marks:
            snaps[name] = _r(marks[idx] * 100)
    risk = (entry - stop0) / entry
    return {"status": "running" if exit_j is None else ("won" if result > 0 else "lost"), "reason": reason,
            "entry_date": b.dates[k], "entry": _r(entry), "stop": _r(stop0), "current_stop": _r(stop),
            "exit_date": b.dates[exit_j] if exit_j is not None else None, "result_pct": _r(result * 100),
            "r": _r(result / risk) if risk > 0 else None, "days": last - k + 1,
            "mfe": _r((hi_seen / entry - 1) * 100), "mae": _r((lo_seen / entry - 1) * 100),
            "snaps": snaps, "events": events, "half": booked}


# --- build and the daily update -------------------------------------------------------------------

UPSERT = ("INSERT OR REPLACE INTO track_intraday (rules, symbol, breakout_date, pattern, pivot, pattern_stop, tier, "
          "market, rs, tt_passed, base_label, volume_ratio, status, reason, trade_no, entry_date, entry, stop, "
          "exit_date, result_pct, r, days, mfe, mae, snaps, events, updated_at) VALUES "
          "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")


def _replay(u: track.Universe, sym: str, rules: str, stored: list[dict], new: list[dict], five: FiveMin,
            idx: Optional[FiveMin]) -> list[tuple]:
    """Re-run the open and pending trades and place the new crosses for one stock and rule set."""
    cfg, icfg = u.cfg, u.cfg.intraday
    b = track.Bars(u.series[sym], cfg.track.exit_ma)
    rows = sorted([dict(r, _stored=True) for r in stored] + new, key=lambda r: r["breakout_date"])
    busy_until, trade_no, traded, out = None, 0, set(), []
    for r in rows:
        if r.get("_stored") and r["status"] in FINAL:
            if r["status"] in ("won", "lost"):
                trade_no = max(trade_no, r.get("trade_no") or 0)
                busy_until = r.get("exit_date")
                traded.add(r["pivot"])
            continue
        k = b.index(r["breakout_date"])
        if k is None or k < 1:
            continue
        if busy_until is not None and (busy_until == "open" or busy_until >= r["breakout_date"]):
            out.append((rules, *track._row_values(r, {"status": "in_trade", "reason": "crossed while a trade was open"},
                                                 None)))
            continue
        if r["pivot"] in traded:                           # one attempt per pivot
            continue
        d = r["breakout_date"]
        bars = five.day(d)
        if bars is None:
            trade = {"status": "pending", "reason": "5-minute candles not fetched yet (needs a Kite login)"}
        else:
            ib = idx.day(d, ahead=60) if idx is not None else None
            avg = float(b.vol[max(0, k - 50):k].mean()) if k else None
            e, why = find_entry(bars, ib, r["pivot"], float(b.close[k - 1]), float(b.open[k]), avg, d, icfg)
            trail, min_stop = VARIANTS[rules]
            if e and min_stop and e["stop"] > e["price"] * (1 - min_stop / 100):
                e = dict(e, stop=e["price"] * (1 - min_stop / 100))     # give the trade room: move a close stop down
            trade = simulate(b, k, e, five, icfg, trail) if e else {"status": "no_entry", "reason": why}
        no = None
        if trade["status"] in ("running", "won", "lost"):
            trade_no += 1
            no = trade_no
            busy_until = trade["exit_date"] or "open"
            traded.add(r["pivot"])
        out.append((rules, *track._row_values(r, trade, no)))
    return out


def build(config: AppConfig, client, rebuild: bool = False) -> dict:
    """First build (or a deliberate rebuild): every pivot cross in the last history_days, every rule set, fetching
    the 5-minute candles it needs (a few per cross; Kite allows about 3 requests a second; stored candles are
    re-used). Otherwise the daily update: new crosses, and pending / running trades re-run. A build for other
    rule sets (VERSION) or another history_days is re-run from scratch."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        from backend.ingest import universe as uni
        with get_db() as conn:
            built = get_setting(conn, "track_intraday.built_at")
            if built and stale_reason(config):
                rebuild = True
            if rebuild:
                conn.execute("DELETE FROM track_intraday")
                built = None
        _status.update(state="building" if not built else "updating", done=0, total=0, message="", at=now_ist())
        res = uni.resolve_universe(config.ingest)
        tokens = {s["symbol"]: s["instrument_token"] for s in res["stocks"]}
        index_token = next((i["instrument_token"] for i in res["indices"] if i["symbol"] == uni.INDEX_NAME), None)
        u = track.Universe(config, res["stocks"], index_token)
        if not u.series:
            _status.update(state="idle", message="no candles yet")
            return {"status": "no_data"}
        latest = max(d[-1] for d in u.dates.values())
        since = (date.fromisoformat(latest) - timedelta(days=config.track.history_days)).isoformat()
        with get_db() as conn:
            stored: dict = {}
            for r in conn.execute("SELECT * FROM track_intraday"):
                stored.setdefault((r["rules"], r["symbol"]), []).append(dict(r))
            last = conn.execute("SELECT MAX(breakout_date) FROM track_intraday").fetchone()[0]
        start = since if not built or not last else max(since, last)
        index_dates = [c["date"] for c in u.index]
        idx = FiveMin(client, index_token, uni.INDEX_NAME, index_dates) if index_token is not None else None
        _status["total"] = len(u.series)
        rows, added, pending = [], 0, 0
        for i, sym in enumerate(sorted(u.series)):
            try:
                crosses = watch_signals(u, sym, start)
            except Exception:
                logger.exception("Intraday backtest: watchlist for %s failed", sym)
                crosses = []
            five = FiveMin(client, tokens[sym], sym, u.dates[sym])
            for rules in RULES:
                mine = stored.get((rules, sym), [])
                have = {r["breakout_date"] for r in mine}
                new = [s for s in crosses if s["breakout_date"] not in have]
                if rules == RULES[0]:
                    added += len(new)
                if new or any(r["status"] not in FINAL for r in mine):
                    got = _replay(u, sym, rules, mine, new, five, idx)
                    pending += sum(1 for g in got if g[12] == "pending")   # status, after rules
                    rows += got
            _status["done"] = i + 1
            _status["message"] = f"{sym}: {added} pivot crosses so far"
            if len(rows) > 500:
                _write(rows)
                rows = []
        _write(rows)
        with get_db() as conn:
            set_setting(conn, "track_intraday.built_at", built or now_ist())
            set_setting(conn, "track_intraday.updated_at", now_ist())
            set_setting(conn, "track_intraday.as_of", latest)
            set_setting(conn, "track_intraday.version", f"{VERSION}/{config.track.history_days}")
        msg = (f"{'updated' if built else 'built'}: {added} new pivot crosses (data to {latest})"
               + (f"; {pending} waiting for 5-minute candles" if pending else ""))
        logger.info("Intraday backtest %s", msg)
        _status.update(state="idle", message=msg, at=now_ist())
        return {"status": "ok", "added": added, "pending": pending, "as_of": latest}
    except Exception as exc:
        logger.exception("Intraday backtest failed")
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
        return bool(get_setting(conn, "track_intraday.built_at"))


def stale_reason(config: AppConfig) -> Optional[str]:
    """Why a finished build no longer matches the rule sets or the history window (None when it does)."""
    with get_db() as conn:
        if not get_setting(conn, "track_intraday.built_at"):
            return None
        v = get_setting(conn, "track_intraday.version") or "1/365"
    ver, days = (v.split("/") + ["365"])[:2]
    if ver != VERSION:
        return "new rule sets were added (the stop-minimum variants and costs)"
    if days != str(config.track.history_days):
        return f"the history changed to {config.track.history_days} days"
    return None
