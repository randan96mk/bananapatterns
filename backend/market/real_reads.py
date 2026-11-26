"""📈 The candle model on the Real book's OPEN positions: a nightly read on each held name's daily candles and a 9:20 AM
read on its 1-minute candles, scored like every other read, shown on Portfolio › Real only while the Real scorecard
earns it (or market.kronos_show = cards). Numbers come from code or from the candle model with its version; there is
no model sentence here.

  held names   the Real book's own records (real_state.positions in the app DB) and nothing else: no broker holdings or
               positions are ever read. A closed position is not read.
  nightly      after the evening update (the daily candles are fresh), and once at app start for a name whose latest
               stored daily candle has no read yet: the last DAILY_BARS daily candles, horizons 1 / 3 / 5 sessions,
               market.samples paths, touch levels = the book's stop (and target, when the book has one) -> p_up per
               horizon, the expected range, stop-first / target-first over 3 sessions. market_reads rows with
               interval "1d", hint_id NULL, book "real", paths_json.asof = the candle date (a name is read once per
               candle date).
  9:20 AM      while Kite is logged in: the held names' 1-minute candles (Kite historical, the same fetch helper as the
               indices; stored in candles_intraday under the name's token), read on 5-minute bars built from them up
               to 9:20 AM, horizon to 3:30 PM (H_920 minutes); the gap facts by code (gap % vs the previous close,
               filled or holding by 9:20 AM). Rows with interval "1m", book "real".
  scoring      the 6:40 PM market_score job scores the 9:20 rows from the day's 1-minute candles (the close and the
               level touched first by 3:30 PM); the nightly step scores the daily rows whose horizon has passed from
               the stored daily candles. Scorecard rows "Real · daily · 1 d / 3 d / 5 d" and "Real · 9:20 AM".
  the gate     the Real table's numbers show when Real · daily 3 d direction ≥ GATE_PCT over ≥ GATE_READS scored
               reads, or market.kronos_show = cards; otherwise the cells say "shadow" (every read is still stored and
               scored). market.kronos off: nothing is fetched, read or shown.

Never on the order path: the 9:16 AM orders and the stop check (backend/engine/real.py) do not import this module;
every read here runs on its own thread with the worker client's timeout. Nothing here places, modifies, cancels,
holds or blocks an order or changes a stop."""

import json
import threading
import time as _time
from datetime import date, datetime, time, timedelta
from typing import Optional

from backend.config import IST
from backend.database.models import find_instrument, get_candles, get_db, get_setting, set_setting
from backend.logging_config.logger import setup_logger
from backend.market import candles as cd
from backend.market import chart
from backend.market import paths as pth
from backend.market import reads as mr
from backend.timefmt import ampm

logger = setup_logger("market")

BOOK = "real"
DAILY_H = (1, 3, 5)                 # sessions ahead
TOUCH_H = 3                         # the table's "stop first · 3 d" / "up · 3 d"
DAILY_BARS = 120                    # daily candles handed to the model
DAILY_TIMEOUT_S = 20.0
AT_920, UNTIL_920 = time(9, 20), time(9, 45)   # the 9:20 read runs once in this window (a later login: none that day)
STEP_920 = 5                        # the model steps on 5-minute bars built from the 1-minute candles (74 steps to 3:30 PM)
H_920 = 370                         # minutes from 9:20 AM to 3:30 PM (the stored horizon)
TIMEOUT_920_S = 45.0
GATE_PCT, GATE_READS = 55.0, 100
NEAR_PCT = 50.0                     # stop-first ≥ this: red bold in the table and (cards only) a Home "Needs a look" item
GAP_MIN_PCT = 0.2                   # |gap| under this: "no gap"
DONE_KEY = "market.real920"         # app_settings: the IST day the 9:20 read ran
DAILY_MISSING_DAYS = 15             # a daily read whose candles never reach its horizon is scored "missing" after this
STOP_CHECK = "the stop check decides as always"

_night_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(IST)


def _pct(p: Optional[float]) -> Optional[int]:
    return None if p is None else int(round(100 * float(p)))


# --- the held names (the Real book's own records only) ----------------------------------------------------------------------

def open_positions() -> dict:
    """{symbol: position} of the Real book's open positions (real_state in the app DB); {} when not started."""
    from backend.engine import real
    st = real._state() or {}
    return dict(st.get("positions") or {})


def token_of(symbol: str, pos: Optional[dict] = None) -> Optional[int]:
    with get_db() as conn:
        for s in dict.fromkeys([x for x in ((pos or {}).get("ts"), symbol) if x]):
            r = find_instrument(conn, s, "NSE")
            if r:
                return int(r["instrument_token"])
    return None


def levels_of(pos: dict) -> dict:
    """{stop, target}: the book's levels (target None: the E+ rules have no target)."""
    t = pos.get("target")
    return {"stop": round(float(pos["stop"]), 2) if pos.get("stop") is not None else None,
            "target": round(float(t), 2) if t is not None else None}


def daily_bars(token: int, n: int = DAILY_BARS, until: Optional[str] = None, after: Optional[str] = None) -> list[dict]:
    with get_db() as conn:
        if after:
            return [r for r in get_candles(conn, token, start=after) if r["date"] > after]
        rows = get_candles(conn, token)
    if until:
        rows = [r for r in rows if r["date"] <= until]
    return rows[-n:]


def _have(symbol: str, interval: str, key: str, val: str) -> bool:
    with get_db() as conn:
        return conn.execute(f"SELECT 1 FROM market_reads WHERE book = ? AND symbol = ? AND interval = ? AND json_extract(paths_json, '$.{key}') = ? "
                            "LIMIT 1", (BOOK, symbol, interval, val)).fetchone() is not None


# --- the nightly read on daily candles -----------------------------------------------------------------------------------

def read_daily(config, symbol: str, pos: dict, now: Optional[datetime] = None) -> list[int]:
    """One worker call (horizons 1 / 3 / 5 sessions) on the name's last DAILY_BARS daily candles; one row per horizon.
    Skipped (no call) when the latest candle date was read already or fewer than 30 candles are stored. Never raises."""
    now = now or _now()
    try:
        token = token_of(symbol, pos)
        if token is None:
            return []
        bars = daily_bars(token)
        if len(bars) < 30:
            return []
        asof = bars[-1]["date"]
        if _have(symbol, "1d", "asof", asof):
            return []
        lv = levels_of(pos)
        levels = [x for x in (lv["stop"], lv["target"]) if x is not None]
        payload = {"symbol": symbol, "interval": "1d", "horizon": max(DAILY_H), "horizons": list(DAILY_H), "samples": int(config.market.samples),
                   "candles": [{"at": b["date"], "o": b["open"], "h": b["high"], "l": b["low"], "c": b["close"], "v": b["volume"]} for b in bars],
                   "levels": levels}
        res = mr.call_worker(config, payload, DAILY_TIMEOUT_S)
        if not res:
            return []
        extra = {"basis": BOOK, "token": token, "asof": asof, "levels": lv, "why": "nightly"}
        ids = [mr.store(now, symbol, res, one, extra, None, BOOK, "1d") for one in (res.get("reads") or [res])
               if int(one.get("horizon") or 0) in DAILY_H]
        if ids:
            mr.LAST["read"] = {"at": now.isoformat(timespec="seconds"), "symbol": symbol, "ms": res.get("ms")}
        return ids
    except Exception:
        logger.exception("Real daily read of %s failed", symbol)
        return []


def nightly(config, now: Optional[datetime] = None) -> dict:
    """The daily rows whose horizon passed scored, then every open position read on its daily candles.
    {why, scored, read [symbols], skipped}. Off: nothing."""
    now = now or _now()
    if not mr.on(config):
        return {"why": "off", "scored": None, "read": [], "skipped": []}
    sc = score(now, daily_only=True)
    read, skipped = [], []
    for sym, pos in sorted(open_positions().items()):
        (read if read_daily(config, sym, pos, now) else skipped).append(sym)
    return {"why": None, "scored": sc, "read": read, "skipped": skipped}


def start_nightly(config, wait_ready: bool = False, wait_s: float = 300.0) -> Optional[threading.Thread]:
    """nightly() on its own thread (the evening job and the app start call this and return at once); one at a time.
    wait_ready: first wait (up to wait_s) for the worker to say ready (the app start: the worker is still loading)."""
    if not mr.on(config) or not open_positions():
        return None

    def run():
        if not _night_lock.acquire(blocking=False):
            return
        try:
            if wait_ready:
                end = _time.monotonic() + wait_s
                while _time.monotonic() < end and mr.health(config, 1.0).get("state") != "ready":
                    _time.sleep(5)
            out = nightly(config)
            logger.info("Real positions read on daily candles: %s", {k: out[k] for k in ("read", "skipped")})
        except Exception:
            logger.exception("Real nightly read failed")
        finally:
            _night_lock.release()
    t = threading.Thread(target=run, name="market-real-nightly", daemon=True)
    t.start()
    return t


# --- the 9:20 AM read on the day's 1-minute candles ------------------------------------------------------------------------

def due_920(config, client, now: datetime) -> bool:
    from backend.ingest.holidays import is_trading_day
    if not mr.on(config) or not cd.logged_in(client) or not is_trading_day(now.date()):
        return False
    if not (AT_920 <= now.time() < UNTIL_920):
        return False
    with get_db() as conn:
        if get_setting(conn, DONE_KEY) == now.date().isoformat():
            return False
    return bool(open_positions())


def fetch_minute(client, token: int, symbol: str, now: datetime) -> int:
    """The name's 1-minute candles: the 2 previous sessions (once) and today, stored in candles_intraday."""
    from backend.options.candles_util import from_kite
    have = set(cd.stored_days(token))
    today = now.date()
    prev = cd._prev_sessions(today, 2)
    start = prev[0] if any(d.isoformat() not in have for d in prev) else today
    rows = client.historical_minute(token, start, today, label=symbol)
    n = 0
    for d, bars in from_kite(rows).items():
        n += cd.store(token, d, bars, complete=d < today.isoformat())
    return n


def gap_fact(prev_close: Optional[float], today: list[dict], until: str = "09:20") -> Optional[dict]:
    """{pct, dir up|down|none, state filled|holding|none, text}: today's open vs the previous close, and whether the
    price came back to the previous close before `until` (gap up: a low at or under it; gap down: a high at or over)."""
    early = [b for b in today if b["t"] < until]
    if not prev_close or not early:
        return None
    pct = round((early[0]["o"] / prev_close - 1) * 100, 2)
    hm = ampm(until)
    if abs(pct) < GAP_MIN_PCT:
        return {"pct": pct, "dir": "none", "state": "none", "text": "no gap at the open"}
    up = pct > 0
    filled = min(b["l"] for b in early) <= prev_close if up else max(b["h"] for b in early) >= prev_close
    side = "up" if up else "down"
    return {"pct": pct, "dir": side, "state": "filled" if filled else "holding",
            "text": f"gap {side} {abs(pct):.1f} % " + (f"filled by {hm}" if filled else f"held at {hm}")}


def read_920(config, symbol: str, pos: dict, now: datetime) -> list[int]:
    """The 9:20 AM read of one held name (stored candles up to 9:20 AM). Never raises."""
    try:
        token = token_of(symbol, pos)
        if token is None:
            return []
        day = now.date().isoformat()
        bars = [b for b in cd.load(token, 3, day) if b["d"] < day or b["t"] < AT_920.strftime("%H:%M")]
        today = chart.of_day(bars, day)
        if not today:
            return []
        prev = [b for b in bars if b["d"] < day]
        prev_close = prev[-1]["c"] if prev else None
        if prev_close is None:
            d = daily_bars(token, 1, (now.date() - timedelta(days=1)).isoformat())
            prev_close = d[-1]["close"] if d else None
        b5 = chart.five(bars)
        if len(b5) < 30:
            return []
        lv = levels_of(pos)
        steps = H_920 // STEP_920
        payload = {"symbol": symbol, "interval": "5m", "horizon": steps, "horizons": [steps], "samples": int(config.market.samples),
                   "candles": cd.to_worker(b5), "levels": [x for x in (lv["stop"], lv["target"]) if x is not None]}
        res = mr.call_worker(config, payload, TIMEOUT_920_S)
        if not res:
            return []
        one = next((x for x in (res.get("reads") or [res]) if int(x.get("horizon") or 0) == steps), None)
        if one is None:
            return []
        at = datetime.combine(now.date(), AT_920, IST)
        extra = {"basis": BOOK, "token": token, "levels": lv, "gap": gap_fact(prev_close, today), "prev_close": prev_close,
                 "steps": f"{steps} five-minute steps from the 1-minute candles", "why": "9:20"}
        rid = mr.store(at, symbol, res, {**one, "horizon": H_920}, extra, None, BOOK, "1m")
        mr.LAST["read"] = {"at": now.isoformat(timespec="seconds"), "symbol": symbol, "ms": res.get("ms")}
        return [rid]
    except Exception:
        logger.exception("Real 9:20 read of %s failed", symbol)
        return []


def run_920(config, client, now: Optional[datetime] = None) -> dict:
    """The held names' 1-minute candles fetched (one call each) and read; once a day (marked before it starts).
    Logged out / off / no position: nothing. {fetched, read}."""
    now = now or _now()
    if not mr.on(config) or not cd.logged_in(client):
        return {"fetched": {}, "read": []}
    with get_db() as conn:
        set_setting(conn, DONE_KEY, now.date().isoformat())
    fetched, read = {}, []
    for sym, pos in sorted(open_positions().items()):
        token = token_of(sym, pos)
        if token is None:
            fetched[sym] = "no instrument"
            continue
        try:
            fetched[sym] = fetch_minute(client, token, sym, now)
        except Exception as exc:
            fetched[sym] = f"error: {str(exc)[:80]}"
            continue
        if read_920(config, sym, pos, now):
            read.append(sym)
    return {"fetched": fetched, "read": read}


# --- scoring --------------------------------------------------------------------------------------------------------------

def _touch(r: dict, highs: list[float], lows: list[float], last: float) -> tuple[Optional[str], Optional[bool]]:
    touch = r.get("touch_json") or {}
    if not touch:
        return None, None
    f = pth.first_touch(last, highs, lows, [float(k) for k in touch], len(highs))
    first = pth.level_key(f) if f is not None else "none"
    probs = {**touch, "none": round(max(0.0, 1 - sum(touch.values())), 3)}
    return first, max(probs, key=lambda k: probs[k]) == first


def _graded(r: dict, close: float, highs: list[float], lows: list[float], n: int) -> Optional[dict]:
    last = (r.get("paths_json") or {}).get("last")
    if last is None:
        return None
    up = close > last
    pred = None if r.get("p_up") is None or r["p_up"] == 0.5 else r["p_up"] > 0.5
    first, touch_right = _touch(r, highs, lows, last)
    stop = ((r.get("paths_json") or {}).get("levels") or {}).get("stop")
    return {"up": up, "close": round(close, 2), "range": round(max(highs) - min(lows), 2), "first": first, "direction_right": None if pred is None else pred == up,
            "touch_right": touch_right, "stop_touched": None if stop is None else min(lows) <= stop, "bars": n}


def outcome_daily(r: dict, after: list[dict]) -> Optional[dict]:
    """A daily read against the next `horizon` stored sessions after its candle date, or None while they are not stored."""
    h = int(r["horizon"])
    if len(after) < h:
        return None
    w = after[:h]
    return _graded(r, w[-1]["close"], [b["high"] for b in w], [b["low"] for b in w], h)


def outcome_920(r: dict, day_bars: list[dict]) -> Optional[dict]:
    """A 9:20 read against the day's 1-minute candles 9:20 AM → the 3:29 PM bar (the close at 3:30 PM)."""
    start = datetime.fromisoformat(r["at"]).strftime("%H:%M")
    end = (datetime.fromisoformat(r["at"]) + timedelta(minutes=int(r["horizon"]) - 1)).strftime("%H:%M")
    w = [b for b in day_bars if start <= b["t"] <= end]
    if not w or w[-1]["t"] < end:
        return None
    return _graded(r, w[-1]["c"], [b["h"] for b in w], [b["l"] for b in w], len(w))


def score(now: Optional[datetime] = None, client=None, daily_only: bool = False) -> dict:
    """Every unscored Real row whose horizon has passed: its outcome stored. A 9:20 row whose day's candles do not reach
    3:29 PM fetches them once when `client` is logged in (read-only); still short after a day: "missing". A daily row
    waits for its sessions (missing after DAILY_MISSING_DAYS). {scored, missing, waiting}."""
    now = now or _now()
    out = {"scored": 0, "missing": 0, "waiting": 0}
    fetched: set = set()
    q = "scored_at IS NULL AND book = ?" + (" AND interval = '1d'" if daily_only else "")
    for r in mr.rows(q, (BOOK,)):
        p = r.get("paths_json") or {}
        token = p.get("token")
        at = datetime.fromisoformat(r["at"])
        at = at if at.tzinfo else at.replace(tzinfo=IST)
        o, late = None, False
        if r["interval"] == "1d":
            o = outcome_daily(r, daily_bars(int(token), after=p.get("asof"))) if token and p.get("asof") else None
            late = (now - at).days >= DAILY_MISSING_DAYS
        else:
            if at + timedelta(minutes=int(r["horizon"])) > now:
                out["waiting"] += 1
                continue
            day = r["at"][:10]
            bars = chart.of_day(cd.load(int(token), 1, day), day) if token else []
            o = outcome_920(r, bars)
            if o is None and token and cd.logged_in(client) and token not in fetched:
                fetched.add(token)
                try:
                    fetch_minute(client, int(token), r["symbol"], datetime.combine(date.fromisoformat(day), time(15, 35), IST))
                    o = outcome_920(r, chart.of_day(cd.load(int(token), 1, day), day))
                except Exception as exc:
                    logger.info("Real 9:20 scoring fetch failed for %s: %s", r["symbol"], str(exc)[:120])
            late = (now - at).total_seconds() >= 86400
        if o is None:
            if not late:
                out["waiting"] += 1
                continue
            o = {"missing": True, "direction_right": None, "touch_right": None}
            out["missing"] += 1
        else:
            out["scored"] += 1
        with get_db() as conn:
            conn.execute("UPDATE market_reads SET scored_at = ?, outcome_json = ? WHERE id = ?", (now.isoformat(timespec="seconds"), json.dumps(o), r["id"]))
    return out


# --- the scorecard and the gate ------------------------------------------------------------------------------------------

def scorecard() -> list[dict]:
    return mr.scorecard("book = ?", (BOOK,))


def gate(config, sc: Optional[list[dict]] = None) -> dict:
    """{show: the table's numbers show, earned, mode off|shadow|cards, why}: Real · daily 3 d direction ≥ GATE_PCT over
    ≥ GATE_READS scored reads, or market.kronos_show = cards."""
    sc = scorecard() if sc is None else sc
    x = mr.card_row(sc, mr.REAL_DAILY, TOUCH_H) or {"scored": 0, "direction_pct": None}
    earned = x["scored"] >= GATE_READS and (x["direction_pct"] or 0) >= GATE_PCT
    rule = f"the Real · daily scorecard passes {GATE_PCT:g} % over {GATE_READS} reads, or market.kronos_show = cards"
    if not mr.on(config):
        return {"show": False, "earned": earned, "mode": "off", "why": "the candle model is off (Settings › market · kronos)", "rule": rule}
    if earned:
        return {"show": True, "earned": True, "mode": "cards", "why": "shown because the Real · daily scorecard passed the gate", "rule": rule}
    if mr.show(config) == "cards":
        return {"show": True, "earned": False, "mode": "cards", "why": "shown because market.kronos_show = cards", "rule": rule}
    return {"show": False, "earned": False, "mode": "shadow", "why": f"hidden until {rule}", "rule": rule}


def hit(sc: list[dict]) -> Optional[dict]:
    x = mr.card_row(sc, mr.REAL_DAILY, TOUCH_H)
    return {"pct": x["direction_pct"], "n": x["scored"]} if x and x["scored"] else None


def chip(config, sc: Optional[list[dict]] = None) -> str:
    """'candle model · shadow · daily hit rate 57 % over 140 reads'."""
    if not mr.on(config):
        return "candle model · off"
    sc = scorecard() if sc is None else sc
    g, h = gate(config, sc), hit(sc)
    return f"candle model · {g['mode']} · " + (f"daily hit rate {round(h['pct'])} % over {h['n']} reads" if h else "daily not scored yet")


# --- what the table, Home and Telegram show ------------------------------------------------------------------------------

def latest_daily(symbol: str) -> dict:
    """{horizon: row} of the name's newest daily read (one worker call = one `at`)."""
    with get_db() as conn:
        r = conn.execute("SELECT at FROM market_reads WHERE book = ? AND symbol = ? AND interval = '1d' ORDER BY at DESC, id DESC LIMIT 1",
                         (BOOK, symbol)).fetchone()
    if not r:
        return {}
    return {int(x["horizon"]): x for x in mr.rows("book = ? AND symbol = ? AND interval = '1d' AND at = ?", (BOOK, symbol, r[0]))}


def latest_920(symbol: str, day: str) -> Optional[dict]:
    rs = mr.rows("book = ? AND symbol = ? AND interval = '1m' AND at >= ? AND at < ?", (BOOK, symbol, day, day + "T99"), "id DESC")
    return rs[0] if rs else None


def _stop_first(r: Optional[dict], lv: dict) -> tuple[Optional[int], Optional[int]]:
    """(stop-first %, target-first %) from one row's touch odds."""
    t = (r or {}).get("touch_json") or {}
    s = t.get(pth.level_key(lv["stop"])) if lv.get("stop") is not None else None
    g = t.get(pth.level_key(lv["target"])) if lv.get("target") is not None else None
    return _pct(s), _pct(g)


def numbers(symbol: str, pos: dict, day: str) -> dict:
    """The model's numbers for one held name (whatever the gate says; the callers hide them in shadow)."""
    lv = levels_of(pos)
    d = latest_daily(symbol)
    r3 = d.get(TOUCH_H)
    sf, tf = _stop_first(r3, lv)
    r920 = latest_920(symbol, day)
    s920, t920 = _stop_first(r920, lv)
    return {"levels": lv, "stop_first": sf, "target_first": tf, "up": _pct((r3 or {}).get("p_up")),
            "exp_range": (r3 or {}).get("exp_range"), "at": (r3 or {}).get("at"), "asof": ((r3 or {}).get("paths_json") or {}).get("asof"),
            "version": (r3 or {}).get("version"), "samples": ((r3 or {}).get("paths_json") or {}).get("samples"),
            "by_h": {h: {"up": _pct(x.get("p_up")), "exp_range": x.get("exp_range")} for h, x in d.items()},
            "i920": {"stop_touch": s920, "target_touch": t920, "up": _pct(r920.get("p_up")), "at": r920["at"],
                     "gap": (r920.get("paths_json") or {}).get("gap")} if r920 else None}


def code_read(symbol: str, pos: dict, model: Optional[dict] = None) -> Optional[dict]:
    token = token_of(symbol, pos)
    if token is None:
        return None
    lv = levels_of(pos)
    return chart.daily_read(daily_bars(token, 40), lv["stop"], lv["target"], model)


def table(config, now: Optional[datetime] = None) -> dict:
    """Portfolio › Real: {on, mode, show, chip, why, rows {symbol: {...}}, order (stop-first desc while the numbers show,
    else None: the page keeps its own order), footer, samples, version}. In shadow the model's numbers are NOT in the
    payload (each row says shadow); the code read always is."""
    now = now or _now()
    day = now.date().isoformat()
    sc = scorecard()
    g = gate(config, sc)
    out = {"on": mr.on(config), "mode": g["mode"], "show": g["show"], "chip": chip(config, sc), "why": g["why"], "hit": hit(sc),
           "rows": {}, "order": None, "samples": int(config.market.samples), "version": mr.VERSION}
    if not out["on"]:
        out["footer"] = g["why"]
        return out
    for sym, pos in open_positions().items():
        nb = numbers(sym, pos, day)
        model = {"stop_first": nb["stop_first"], "target_first": nb["target_first"], "up": nb["up"]} if g["show"] and nb["at"] else None
        row = {"levels": nb["levels"], "code": code_read(sym, pos, model), "read": bool(nb["at"]), "asof": nb["asof"], "shadow": not g["show"]}
        if g["show"]:
            row.update({k: nb[k] for k in ("stop_first", "target_first", "up", "exp_range", "at", "version", "samples", "by_h", "i920")})
        else:
            row["gap"] = (nb["i920"] or {}).get("gap")         # a code fact (the 9:20 gap), not a model number
        out["rows"][sym] = row
    if g["show"]:
        out["order"] = sorted(out["rows"], key=lambda s: (-(out["rows"][s].get("stop_first") if out["rows"][s].get("stop_first") is not None else -1), s))
    n = out["samples"]
    out["footer"] = (f"stop first = share of {n} sampled 3-session paths that touch the stop (before the target where the book has one) · "
                     f"up = share closing above the last close after 3 sessions · from the stored daily candles (last {DAILY_BARS}) · "
                     f"Kronos-mini · {mr.VERSION} · {g['why']}")
    return out


def _gap_text(nb: dict) -> str:
    g = (nb.get("i920") or {}).get("gap")
    return g["text"] if g else ""


def attention_items(config, now: Optional[datetime] = None) -> list[tuple]:
    """Home "Needs a look" (cards only): an open name whose stop-first · 3 d ≥ NEAR_PCT or whose 9:20 read touches the
    stop in ≥ NEAR_PCT of its paths. ("info", text, meta). Informational: the stop check decides as always."""
    now = now or _now()
    if not mr.on(config) or not gate(config)["show"]:
        return []
    out = []
    for sym, pos in sorted(open_positions().items()):
        nb = numbers(sym, pos, now.date().isoformat())
        stop, first = nb["levels"]["stop"], " first" if nb["levels"]["target"] is not None else ""
        gap = _gap_text(nb)
        if nb["stop_first"] is not None and nb["stop_first"] >= NEAR_PCT:
            text = f"{sym} · candle model: stop {chart._px(stop)} touched{first} in {nb['stop_first']} % of 3-day paths"
        elif nb["i920"] and nb["i920"]["stop_touch"] is not None and nb["i920"]["stop_touch"] >= NEAR_PCT:
            text = f"{sym} · candle model: stop {chart._px(stop)} touched{first} by 3:30 PM in {nb['i920']['stop_touch']} % of 9:20 AM paths"
        else:
            continue
        out.append(("info", text + (f" · {gap}" if gap else ""), f"informational · {STOP_CHECK} · Portfolio › Real"))
    return out


def brief_line(config, now: Optional[datetime] = None) -> Optional[str]:
    """The 9:00 AM brief's line: 'candle model (shadow): 1 of 2 held names lean down · nearest to its stop: DEF (52 % odds
    of the stop first)'. Numbers only; None when off or no held name has a read."""
    now = now or _now()
    if not mr.on(config):
        return None
    got = {}
    for sym, pos in open_positions().items():
        nb = numbers(sym, pos, now.date().isoformat())
        if nb["up"] is not None:
            got[sym] = nb
    if not got:
        return None
    mode = gate(config)["mode"]
    down = sum(1 for x in got.values() if x["up"] < 50)
    names = "held names lean" if len(got) > 1 else "held name leans"
    head = f"candle model ({mode}): {down} of {len(got)} {names} down"
    sf = {s: x["stop_first"] for s, x in got.items() if x["stop_first"] is not None}
    if not sf:
        return head
    w = max(sf, key=lambda s: (sf[s], s))
    return f"{head} · nearest to its stop: {w} ({sf[w]} % odds of the stop first)"


def day_line(config, now: Optional[datetime] = None) -> Optional[str]:
    """The 3:40 PM summary's line: 'candle model: 9:20 AM read was right on 3 of 5 · scorecard 57 % (140)' (the
    direction of today's 9:20 reads, scored from the day's candles; the scorecard = Real · daily 3 d). None when off
    or nothing to say."""
    now = now or _now()
    if not mr.on(config):
        return None
    day = now.date().isoformat()
    rs = mr.rows("book = ? AND interval = '1m' AND at >= ? AND at < ?", (BOOK, day, day + "T99"))
    h = hit(scorecard())
    if not rs and (not h or not open_positions()):          # nothing read today and nothing scored yet: no line
        return None
    graded = [(r.get("outcome_json") or {}).get("direction_right") for r in rs]
    graded = [x for x in graded if x is not None]
    first = (f"9:20 AM read was right on {sum(graded)} of {len(graded)}" if graded
             else "9:20 AM read not scored yet" if rs else "no 9:20 AM read today")
    return f"candle model: {first} · " + (f"scorecard {round(h['pct'])} % ({h['n']})" if h else "scorecard not scored yet")
