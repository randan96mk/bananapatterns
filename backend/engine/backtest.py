"""Backtest building blocks shared by the strategy backtests: 5-minute candles fetched from Kite only for the days
a backtest needs (stored in candles_5m), the daily closes in one query, the portfolio replay (sizing by the stop, a
cap per position, costs) and the curve statistics. Read-only: Kite history only, never orders."""

from datetime import date
from math import floor
from typing import Optional

from backend.database.models import get_db
from backend.logging_config.logger import setup_logger

logger = setup_logger("backtest")


def costs(slippage_pct: float, overnight_pct: float, same_day_pct: float) -> tuple:
    """(slippage per fill, round-trip charges held overnight, round-trip charges bought and sold the same day), as
    fractions, for book(). Zerodha charges a same-day buy and sell as intraday (STT 0.025% on the sale only, no DP
    charge): about 0.06% a round trip, against about 0.25% for delivery."""
    return slippage_pct / 100, overnight_pct / 100, same_day_pct / 100


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


def closes(conn, tokens: dict, syms, start: str, cache: dict) -> dict:
    """{symbol: {date: close}} from `start`, read in one query; `cache` keeps them for later calls (several
    replays in one request read each stock once)."""
    need = [s for s in syms if s in tokens and (s not in cache or cache[s][0] > start)]
    if need:
        by_token = {tokens[s]: s for s in need}
        got: dict = {s: {} for s in need}
        q = (f"SELECT instrument_token, date, close FROM candles_daily WHERE date >= ? "
             f"AND instrument_token IN ({','.join('?' * len(by_token))})")
        for tok, d, c in conn.execute(q, [start, *by_token]):
            got[by_token[tok]][d] = c
        for s in need:
            cache[s] = (start, got[s])
    return {s: cache[s][1] for s in syms if s in cache}


def book(days, by_day, closes, capital, max_positions, risk_pct, position_cap, max_new_per_day=0,
          costs=(0.0, 0.0, 0.0)):
    """Replay the trade events day by day on one account. by_day: {date: [(event, trade)]}, event = {kind: entry /
    half / exit (/ breakeven), date, time?, price, frac? (half)}, trade = {symbol, breakout_date, stop, tier?,
    priority?}; closes: {symbol: {date: close}} to value open positions. Each entry risks `risk_pct` of equity to the stop, capped
    at `position_cap` of equity per position (0 = no cap) and by the cash left; at most max_new_per_day entries a
    day (0 = any). costs = _costs(): slippage on every fill, round-trip charges on each sale (intraday rates on
    the entry day). Events with a time (intraday) run in time order; same time: sales first, then tier, then
    priority. -> (curve %, cash share, taken, skipped)"""
    slip, overnight, same_day = costs
    bought: dict = {}
    order = {"exit": 0, "half": 1, "breakeven": 2, "entry": 3}           # sell before buying on the same day
    tier_rank = {"A": 0, "B": 1}
    cash, equity, pos = capital, capital, {}
    taken = skipped = 0
    strategy, idle, last_px = [], [], {}
    for d in days:
        evs = sorted(by_day.get(d, []), key=lambda x: (x[0].get("time", ""), order[x[0]["kind"]],
                                                        tier_rank.get(x[1].get("tier"), 2), x[1].get("priority", 0)))
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
                qty = min(qty, floor(cash / (ev["price"] * (1 + slip))))
                if qty <= 0:
                    skipped += 1
                    continue
                cash -= qty * ev["price"] * (1 + slip)
                pos[key], bought[key] = qty, d
                taken += 1
                new_today += 1
            elif key in pos and ev["kind"] in ("half", "exit"):
                q = int(pos[key] * ev.get("frac", 0.5)) if ev["kind"] == "half" else pos[key]
                cash += q * ev["price"] * (1 - slip - (same_day if bought.get(key) == d else overnight))
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


def drawdown(curve: list[float]) -> float:
    """Worst fall from a peak of a curve in % (0 = start), as a negative %."""
    peak, worst = 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        worst = min(worst, (100 + v) / (100 + peak) * 100 - 100)
    return round(worst, 2)


def months_up(days: list[str], curve: list[float]) -> tuple[int, int]:
    """(months that ended higher than the month before, months compared)."""
    ends, prev, up, total = {}, None, 0, 0
    for d, v in zip(days, curve):
        ends[d[:7]] = v
    for v in ends.values():
        if prev is not None:
            total += 1
            up += v > prev
        prev = v
    return up, total
