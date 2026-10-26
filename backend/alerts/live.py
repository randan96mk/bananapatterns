"""Market-hours helpers (read-only: Kite quotes and history, never orders).

  entry checklist  five checks on a name trading above its pivot, and one verdict:
                     zone     within the buy zone (pivot .. +buy_zone_pct)
                     vwap     above VWAP (Kite's average traded price)
                     volume   on pace for >= intraday_volume_mult x the 50-day average (NSE volume curve)
                     leader   up more than the Nifty 500 today
                     range    over the first opening_range_min minutes' high
                   valid = all five; skip = beyond the buy zone, or under VWAP on weak volume (pace < 1x);
                   wait = the rest (a check without data yet counts as not passed)
  live guard       under_stop: trading below your stop · heavy: down live_drop_pct+ today on
                   live_drop_volume x normal volume for the time of day, still above the stop
  size             qty for capital x risk_pct x the market size factor (as on the decision charts)
  LiveBoard        the Today page: one cached quote call for positions, plan and near-pivot names, plus the
                   indices; opening-range highs and 5-minute sparklines from Kite history, cached
"""

import threading
import time as _time
from datetime import date, datetime, time, timedelta
from math import floor
from typing import Optional

from backend.alerts import intraday as itd
from backend.config import IST, AlertsConfig
from backend.database.models import get_candles, get_db
from backend.logging_config.logger import setup_logger

logger = setup_logger("live")

SESSION_OPEN = time(9, 15)
CHECKS = ("zone", "vwap", "volume", "leader", "range")


# --- entry checklist -----------------------------------------------------------------

def checklist(ltp: float, pivot: float, cfg: AlertsConfig, vwap: Optional[float], pace: Optional[float],
              stock_pct: Optional[float], index_pct: Optional[float], or_high: Optional[float]) -> dict:
    """{checks: [(id, ok True/False/None, text)], passed, verdict valid/wait/skip, reason} for a name above its
    pivot (verdict None when it isn't)."""
    zone_top = pivot * (1 + cfg.buy_zone_pct)
    ext = (ltp / pivot - 1) * 100
    need = cfg.intraday_volume_mult
    checks = [
        ("zone", ltp <= zone_top, f"In the buy zone: {ext:+.1f}% over the pivot (≤ {cfg.buy_zone_pct * 100:g}%)"
         if ltp <= zone_top else f"Extended: {ext:+.1f}% over the pivot (zone ends {_rs(zone_top)})"),
        ("vwap", None if not vwap else ltp > vwap,
         f"{'Above' if vwap and ltp > vwap else 'Under'} VWAP {_rs(vwap)}" if vwap else "VWAP: no data yet"),
        ("volume", None if pace is None else pace >= need,
         f"Volume pace {pace:.1f}×" + ("" if pace >= need else f" (needs {need:g}×)") if pace is not None
         else "Volume pace: too early to tell"),
        ("leader", None if stock_pct is None or index_pct is None else stock_pct > index_pct,
         f"{'Stronger' if stock_pct > index_pct else 'Weaker'} than Nifty 500: {stock_pct:+.1f}% vs {index_pct:+.1f}%"
         if stock_pct is not None and index_pct is not None else "Nifty 500: no data"),
        ("range", None if not or_high else ltp > or_high,
         f"{'Over' if or_high and ltp > or_high else 'Under'} the first-{cfg.opening_range_min}-min high {_rs(or_high)}"
         if or_high else f"First {cfg.opening_range_min} min: not over yet"),
    ]
    passed = sum(ok is True for _, ok, _ in checks)
    if ltp <= pivot:
        verdict, reason = None, ""
    elif ltp > zone_top:
        verdict, reason = "skip", f"extended {ext:+.1f}% over the pivot: don't chase"
    elif vwap and ltp < vwap and pace is not None and pace < 1.0:
        verdict, reason = "skip", "under VWAP on weak volume"
    elif passed == len(checks):
        verdict, reason = "valid", "all five checks pass"
    else:
        missing = [cid for cid, ok, _ in checks if ok is not True]
        verdict, reason = "wait", "waiting on " + ", ".join(missing)
    return {"checks": checks, "passed": passed, "of": len(checks), "verdict": verdict, "reason": reason,
            "zone_top": round(zone_top, 2), "ext_pct": round(ext, 2)}


def size(entry: float, stop: Optional[float], cfg: AlertsConfig, factor: float = 1.0) -> Optional[dict]:
    """{qty, risk_rs, value} risking capital x risk_pct x factor between entry and stop (None when sizing is off)."""
    if not stop or cfg.capital <= 0 or cfg.risk_pct <= 0 or entry <= stop:
        return None
    per_share = entry - stop
    qty = floor(cfg.capital * cfg.risk_pct * factor / per_share)
    return {"qty": qty, "risk_rs": round(qty * per_share), "value": round(qty * entry), "factor": factor}


def opening_high(rows: list[dict], minutes: int, now: Optional[datetime] = None) -> Optional[float]:
    """High of the 5-minute bars in the first `minutes` after 09:15 (None until that window has closed)."""
    end = (datetime.combine(date.today(), SESSION_OPEN) + timedelta(minutes=minutes)).time()
    if now is not None and now.time() < end:
        return None
    highs = [r["high"] for r in rows if r.get("high") is not None and r["time"] < end.strftime("%H:%M")]
    return max(highs) if highs else None


# --- live position guard -------------------------------------------------------------------

def guard_rules(pos: dict, ltp: float, day_pct: Optional[float], pace: Optional[float],
                cfg: AlertsConfig) -> list[str]:
    """Which live rules fire: under_stop, heavy (heavy selling while still above the stop)."""
    out = []
    if pos.get("stop") and ltp < pos["stop"]:
        out.append("under_stop")
    elif day_pct is not None and day_pct <= -cfg.live_drop_pct and pace is not None and pace >= cfg.live_drop_volume:
        out.append("heavy")
    return out


def r_multiple(pos: dict, ltp: float) -> Optional[float]:
    risk = (pos.get("avg_price") or 0) - (pos.get("stop") or 0)
    return round((ltp - pos["avg_price"]) / risk, 2) if risk > 0 and pos.get("avg_price") else None


def avg_volume(token: Optional[int]) -> Optional[float]:
    """50-day average daily volume from stored candles (None for stocks we don't have)."""
    if token is None:
        return None
    with get_db() as conn:
        cs = get_candles(conn, token, limit=50)
    return sum(c["volume"] for c in cs) / len(cs) if cs else None


def day_pct(q: dict) -> Optional[float]:
    prev = (q.get("ohlc") or {}).get("close")
    return round((q["last_price"] / prev - 1) * 100, 2) if q.get("last_price") and prev else None


def _rs(v) -> str:
    return "—" if v is None else f"₹{v:,.2f}"


# --- the Today page --------------------------------------------------------------------

class LiveBoard:
    """Snapshot for the Today page and live positions. Quotes are cached QUOTE_TTL seconds, opening-range
    highs for the day, 5-minute sparklines SPARK_TTL seconds."""

    QUOTE_TTL, SPARK_TTL, MAX_NAMES, OR_FETCH_PER_CALL = 25, 300, 60, 6
    INDEX_KEYS = {"nifty500": "NSE:NIFTY 500", "vix": "NSE:INDIA VIX"}

    def __init__(self, state, clock=lambda: datetime.now(IST)):
        self.state, self.clock = state, clock
        self.lock = threading.Lock()
        self._quotes: dict = {}
        self._quoted_at = 0.0
        self._quote_keys: tuple = ()
        self._or: dict[tuple, Optional[float]] = {}           # (day, symbol) -> opening-range high
        self._spark: dict[str, dict] = {}                     # symbol -> {day, at, points}

    # data ---------------------------------------------------------------------------

    def quotes(self, keys: list[str]) -> dict:
        """One Kite quote call for `keys`, reused for QUOTE_TTL seconds when the keys are the same."""
        now = _time.time()
        if self._quotes and tuple(sorted(keys)) == self._quote_keys and now - self._quoted_at < self.QUOTE_TTL:
            return self._quotes
        self._quotes = self.state.client.quote(keys) or {}
        self._quote_keys, self._quoted_at = tuple(sorted(keys)), now
        return self._quotes

    def or_high(self, symbol: str, token: Optional[int], now: datetime) -> Optional[float]:
        """Opening-range high (one Kite history call per stock per day, after the window closes)."""
        cfg = self.state.config.alerts
        key = (now.date().isoformat(), symbol)
        if key in self._or:
            return self._or[key]
        end = (datetime.combine(now.date(), SESSION_OPEN) + timedelta(minutes=cfg.opening_range_min)).time()
        if token is None or now.time() < end:
            return None
        try:
            rows = self.state.client.historical_intraday(token, now.date(), label=symbol)
        except Exception as exc:
            logger.info("Opening range for %s: %s", symbol, exc)
            return None
        self._or[key] = opening_high(rows, cfg.opening_range_min)
        return self._or[key]

    def sparks(self, items: list[dict], now: datetime) -> dict:
        """{symbol: [5-minute closes]} for today, refreshed every SPARK_TTL seconds while open."""
        out, day, ts = {}, now.date(), _time.time()
        for it in items:
            if it.get("token") is None:
                continue
            s = self._spark.get(it["symbol"])
            if not s or s["day"] != day.isoformat() or ts - s["at"] >= self.SPARK_TTL:
                try:
                    rows = self.state.client.historical_intraday(it["token"], day, label=it["symbol"])
                except Exception as exc:
                    logger.info("Sparkline for %s: %s", it["symbol"], exc)
                    continue
                s = {"day": day.isoformat(), "at": ts, "points": [r["close"] for r in rows]}
                self._spark[it["symbol"]] = s
                hi = opening_high(rows, self.state.config.alerts.opening_range_min, now)
                if hi is not None:
                    self._or[(day.isoformat(), it["symbol"])] = hi
            out[it["symbol"]] = s["points"]
        return out

    # names --------------------------------------------------------------------------

    def names(self) -> tuple[list[dict], list[dict]]:
        """(positions, setups): the followed positions, then the plan and Forming names near their pivot."""
        import json
        from backend.alerts import guard
        from backend.alerts import plan as pl
        from backend.alerts.messages import SCREEN_ORDER_SQL
        from backend.ingest import universe as uni
        st, cfg = self.state, self.state.config.alerts
        stocks = {s["symbol"]: s for s in uni.resolve_universe(st.config.ingest)["stocks"]}
        positions = [dict(p, token=(stocks.get(p["symbol"]) or {}).get("instrument_token"),
                          key=f"NSE:{(stocks.get(p['symbol']) or {}).get('tradingsymbol', p['symbol'])}")
                     for p in guard.positions()]
        setups: dict[str, dict] = {}
        _, plan = pl.latest_plan()
        for i in plan:
            setups[i["symbol"]] = {"symbol": i["symbol"], "pivot": i["pivot"], "stop": i.get("stop"),
                                   "tier": i.get("tier"), "results": i.get("results"), "source": "plan"}
        with get_db() as conn:
            as_of = conn.execute("SELECT MAX(as_of) FROM scan_runs").fetchone()[0]
            rows = conn.execute(
                f"SELECT symbol, payload FROM scan_results WHERE as_of = ? AND stage = 'forming' AND screen IN "
                f"({','.join('?' * len(cfg.screens))}) ORDER BY {SCREEN_ORDER_SQL.format(col='screen')}",
                (as_of, *cfg.screens)).fetchall() if as_of else []
        for r in rows:
            p = json.loads(r["payload"])
            if r["symbol"] in setups or (p.get("dist_to_pivot_pct") or 99) > cfg.today_near_pct:
                continue
            stop = ((p.get("vcp") or {}).get("stop") or (p.get("cup") or {}).get("stop") or (p.get("flag") or {}).get("stop")
                    or round(p["pivot"] * (1 - st.config.vcp.max_stop), 2))
            setups[r["symbol"]] = {"symbol": r["symbol"], "pivot": p["pivot"], "stop": stop, "tier": p.get("tier"),
                                   "results": p.get("results"), "source": "near"}
        out = []
        for s in list(setups.values())[: self.MAX_NAMES]:
            row = stocks.get(s["symbol"])
            if row:
                out.append(dict(s, token=row["instrument_token"], key=f"NSE:{row.get('tradingsymbol', s['symbol'])}"))
        return positions, out

    # snapshot -------------------------------------------------------------------------

    def snapshot(self, live: bool) -> dict:
        """Today page / live positions. live=False (no Kite login, or outside market hours) -> the names only."""
        from backend.alerts.priority import MarketContext
        from backend.ingest.indices import market_status
        now = self.clock()
        status = market_status(now)
        cfg = self.state.config.alerts
        mc = MarketContext.load(cfg)
        positions, setups = self.names()
        base = {"status": status, "live": False, "as_of": now.isoformat(timespec="seconds"),
                "market": {"health": mc.status, "size_note": mc.size_note}, "capital_set": cfg.capital > 0}
        if not live or status not in ("open", "pre_open", "closed") or not (positions or setups):
            return dict(base, positions=[dict(_pos_row(p), live=None) for p in positions],
                        setups=[dict(s, live=None) for s in setups])
        with self.lock:
            keys = sorted({p["key"] for p in positions} | {s["key"] for s in setups} | set(self.INDEX_KEYS.values()))
            try:
                q = self.quotes(keys)
            except Exception as exc:
                logger.warning("Today quotes failed: %s", exc)
                return dict(base, error=str(exc)[:200], positions=[dict(_pos_row(p), live=None) for p in positions],
                            setups=[dict(s, live=None) for s in setups])
            index_pct = day_pct(q.get(self.INDEX_KEYS["nifty500"]) or {})
            vix_pct = day_pct(q.get(self.INDEX_KEYS["vix"]) or {})
            fetched = 0
            prows, srows = [], []
            for p in positions:
                d = q.get(p["key"]) or {}
                prows.append(dict(_pos_row(p), live=self._pos_live(p, d, now, cfg) if d.get("last_price") else None))
            for s in setups:
                d = q.get(s["key"]) or {}
                if not d.get("last_price"):
                    srows.append(dict(s, live=None))
                    continue
                orh = self._or.get((now.date().isoformat(), s["symbol"]))
                if orh is None and d["last_price"] > s["pivot"] and fetched < self.OR_FETCH_PER_CALL:
                    fetched += 1
                    orh = self.or_high(s["symbol"], s["token"], now)
                srows.append(dict(s, live=self._setup_live(s, d, now, cfg, index_pct, orh, mc.size_factor)))
        rank = {"valid": 0, "wait": 1, "skip": 2, None: 3}
        srows.sort(key=lambda r: (rank[(r["live"] or {}).get("verdict")],
                                  -((r["live"] or {}).get("vs_pivot_pct") or -99)))
        return dict(base, live=True, market=dict(base["market"], nifty500_pct=index_pct, vix_pct=vix_pct),
                    positions=prows, setups=srows)

    @staticmethod
    def _pos_live(p: dict, d: dict, now: datetime, cfg: AlertsConfig) -> dict:
        ltp, dp = d["last_price"], day_pct(d)
        pace = itd.volume_pace(d.get("volume") or d.get("volume_traded"), avg_volume(p.get("token")), now)
        with get_db() as conn:
            fl = itd.flags(conn, now.date().isoformat(), p["symbol"]).get(p["symbol"], set())
        rules = guard_rules(p, ltp, dp, pace, cfg)
        state = ("under_stop" if "under_stop" in rules else "recovered" if "g_under" in fl or "g_gap" in fl
                 else "heavy" if "heavy" in rules else None)
        return {"ltp": ltp, "day_pct": dp, "pace": round(pace, 2) if pace else None, "r": r_multiple(p, ltp),
                "stop_dist_pct": round((ltp / p["stop"] - 1) * 100, 2) if p.get("stop") else None,
                "gain_pct": round((ltp / p["avg_price"] - 1) * 100, 2) if p.get("avg_price") else None,
                "state": state}

    @staticmethod
    def _setup_live(s: dict, d: dict, now: datetime, cfg: AlertsConfig, index_pct: Optional[float],
                    orh: Optional[float], factor: float) -> dict:
        ltp, dp = d["last_price"], day_pct(d)
        pace = itd.volume_pace(d.get("volume") or d.get("volume_traded"), avg_volume(s.get("token")), now)
        ck = checklist(ltp, s["pivot"], cfg, d.get("average_price"), pace, dp, index_pct, orh)
        sz = size(ltp, s.get("stop"), cfg, factor) if ck["verdict"] == "valid" else None
        return {"ltp": ltp, "day_pct": dp, "pace": round(pace, 2) if pace else None, "vwap": d.get("average_price"),
                "vs_pivot_pct": round((ltp / s["pivot"] - 1) * 100, 2), "verdict": ck["verdict"],
                "reason": ck["reason"], "passed": ck["passed"], "zone_top": ck["zone_top"],
                "checks": [{"id": c, "ok": ok, "text": t} for c, ok, t in ck["checks"]],
                "vs_nifty": round(dp - index_pct, 2) if dp is not None and index_pct is not None else None,
                "size": sz}


def _pos_row(p: dict) -> dict:
    return {k: p.get(k) for k in ("symbol", "qty", "avg_price", "stop", "stop_source", "pivot", "first_seen", "token")}
