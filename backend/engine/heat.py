"""🔥 Live heat on the Plan (Research › Plan and Today): NO model call, display only. For every item of the latest
plan, code takes the price (a live quote from the market-data client the breakout opening check uses, only while the
market is open; otherwise, or without a quote, the last close from candles_daily with `live: false`), the levels
(fails_below / support_1 / resistance from the name's latest AI view, pivot / stop / zone_top from the plan item) and
puts the name in one state: skipped (in the morning's skips) · broken (under fails_below or the stop) · above (at or
over the pivot) · testing (within 0.5% of the pivot or of support 1) · quiet. It never reads holdings, positions or
orders from a broker, places nothing and moves no stop.

  plan_heat(config, now, quote)    {at, live, items: [...], counts: {above, testing, broken, quiet, skipped}}
  state_for(...) / pos_for(...)    the two rules (pure)
"""

from datetime import datetime
from typing import Callable, Optional

from backend.config import IST, AppConfig
from backend.database.models import find_instrument, get_db
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm

logger = setup_logger("engine")

TEST_PCT = 0.5                                  # within this % of the pivot or of support 1: testing
STATES = ("broken", "testing", "above", "quiet", "skipped")      # the tiles' order
SORT = {s: i for i, s in enumerate(STATES)}


def state_for(price: Optional[float], pivot: Optional[float], stop: Optional[float], fails_below: Optional[float],
              support_1: Optional[float], skipped: bool = False) -> str:
    """skipped · broken (price < fails_below, or < stop) · above (price ≥ pivot) · testing (within 0.5% of the pivot or
    of support 1) · quiet."""
    if skipped:
        return "skipped"
    if price is None:
        return "quiet"
    if (fails_below and price < fails_below) or (stop and price < stop):
        return "broken"
    if pivot and price >= pivot:
        return "above"
    for lv in (pivot, support_1):
        if lv and abs(price / lv - 1) * 100 <= TEST_PCT:
            return "testing"
    return "quiet"


def pos_for(price: Optional[float], stop: Optional[float], zone_top: Optional[float]) -> Optional[float]:
    """Where the price sits between the stop (0) and the buy zone's top (1), clipped to 0–1; None without a range."""
    if price is None or stop is None or zone_top is None or zone_top <= stop:
        return None
    return round(min(1.0, max(0.0, (price - stop) / (zone_top - stop))), 3)


def _last_closes(symbol: str, n: int = 2) -> tuple[Optional[str], list[dict]]:
    """(tradingsymbol, the last n daily bars, newest last) from candles_daily."""
    with get_db() as conn:
        inst = find_instrument(conn, symbol, "NSE")
        if not inst:
            return None, []
        rows = conn.execute("SELECT date, close FROM candles_daily WHERE instrument_token = ? ORDER BY date DESC LIMIT ?",
                            (inst["instrument_token"], n)).fetchall()
    return inst["tradingsymbol"], [dict(r) for r in reversed(rows)]


def _skips(day: str) -> set:
    """The symbols skipped in this morning's Real plan (the app's own state)."""
    try:
        from backend.engine import real
        st = real._state() or {}
        return set(real._skips(st, day)) | {o["symbol"] for o in st.get("orders") or [] if o.get("sent") == "skipped" and st.get("decided") == day}
    except Exception:
        logger.exception("heat: the morning skips failed")
        return set()


def _views(symbols: list[str], day: str) -> dict:
    try:
        from backend.ai import view
        return view.for_symbols(symbols, "plan", day)
    except Exception:
        logger.exception("heat: the AI view join failed")
        return {}


def _quotes(quote: Optional[Callable], keys: list[str]) -> dict:
    if not quote or not keys:
        return {}
    try:
        return quote(keys) or {}
    except Exception as exc:
        logger.warning("heat: the quote failed: %s", exc)
        return {}


def plan_heat(config: AppConfig, now: Optional[datetime] = None, quote: Optional[Callable] = None) -> dict:
    """{at, at_label, live, market_open, as_of, items: [{symbol, rank, price, change_pct, state, pos, stop, fails_below,
    pivot, zone_top, support_1, resistance, score, verdict, reasons: [{text, ai}], skipped, live}], counts}. `quote` is
    the market-data client's quote(["NSE:SYM", ...]); it is called only while the market is open."""
    from backend.alerts import plan as pl
    from backend.engine.overview import market_open
    now = now or datetime.now(IST)
    day = now.date().isoformat()
    as_of, plan = pl.latest_plan()
    plan = [i for i in plan if isinstance(i, dict) and i.get("symbol")]
    is_open = market_open(now)
    bars, ts = {}, {}
    for i in plan:
        ts[i["symbol"]], bars[i["symbol"]] = _last_closes(i["symbol"])
    keys = {f"NSE:{ts[s] or s}": s for s in bars}
    quotes = _quotes(quote, list(keys)) if is_open else {}
    live_px = {}
    for k, s in keys.items():
        q = quotes.get(k) or {}
        if q.get("last_price"):
            live_px[s] = (float(q["last_price"]), (q.get("ohlc") or {}).get("close"))
    views = _views([i["symbol"] for i in plan], day)
    skips = _skips(day)
    items = []
    for n, i in enumerate(plan, start=1):
        s = i["symbol"]
        b = bars.get(s) or []
        last = b[-1]["close"] if b else i.get("close")
        if s in live_px:
            price, prev = live_px[s][0], live_px[s][1] or (b[-1]["close"] if b and b[-1]["date"] < day else (b[-2]["close"] if len(b) > 1 else None))
            live = True
        else:
            price, prev, live = last, (b[-2]["close"] if len(b) > 1 else None), False
        v = views.get(s) or {}
        lv = v.get("levels") or {}
        skipped = s in skips
        st = state_for(price, i.get("pivot"), i.get("stop"), lv.get("fails_below"), lv.get("support_1"), skipped)
        items.append({"symbol": s, "rank": n, "price": round(price, 2) if price else None,
                      "change_pct": round((price / prev - 1) * 100, 2) if price and prev else None, "state": st,
                      "pos": pos_for(price, i.get("stop"), i.get("zone_top")), "stop": i.get("stop"), "fails_below": lv.get("fails_below"),
                      "pivot": i.get("pivot"), "zone_top": i.get("zone_top"), "support_1": lv.get("support_1"), "resistance": lv.get("resistance"),
                      "score": v.get("score"), "verdict": v.get("verdict"), "reasons": [{"text": r, "ai": True} for r in v.get("reasons") or []],
                      "skipped": skipped, "live": live})
    items.sort(key=lambda x: (SORT[x["state"]], x["rank"]))
    counts = {k: sum(1 for x in items if x["state"] == k) for k in ("above", "testing", "broken", "quiet", "skipped")}
    live = any(x["live"] for x in items)
    return {"at": now.isoformat(timespec="seconds"), "at_label": f"{ampm(now)} · live" if live else "last close · market closed" if not is_open else "last close",
            "live": live, "market_open": is_open, "as_of": as_of, "items": items, "counts": counts}
