"""🧩 The NIFTY weekly options book, paper only: one credit spread a day (two at most) on the opening-range bias,
confirmed by the option chain, out by 15:10 at the latest. The scheduler ticks it every minute 9:15-15:40 on trading
days (`due` -> options_tick -> `tick`); `steps` says what the minute does:

  first tick       contracts: the NIFTY weekly chain from the NFO dump (once a day), the near expiry, the lot size
  9:30             range: the index 9:15-9:30 high / low, the open's gap, the previous day's levels, the 9:30 chain
                   snapshot (the OI baseline)
  9:45-11:00       bias: on each 5-minute close the range / VWAP (futures) / previous-day test and a chain fetch
                   (snapshot); a bias -> the 0-3 chain score -> size -> strikes -> the paper entry at mid -/+ Rs 1 a leg
  every minute     while a spread is open: MTM from the quotes' mids, the stops (premium, VWAP on 5-minute closes,
                   breakeven after a 40% decay), the target, the daily loss limit; a snapshot of the legs +-3 strikes
  15:10            the time exit; 15:20 the hard exit (nothing is ever held overnight)
  15:30            the close snapshot (the next day's OI baseline)        15:35  the day summary -> options_days
Guards (no new entry): kill switch, enable switch off, hold-today, event day, gap over max_gap_pct, daily loss limit,
2 trades done, outside the window, no Kite login ("no data"). The Real switch cannot be turned on: no webhook yet.
Nothing here reads broker positions; Kite is used for quotes and candles only."""

import json
import threading
from datetime import date, datetime, time, timedelta
from typing import Optional

from backend.config import IST, AppConfig
from backend.database.models import get_candles, get_db, get_setting, now_ist, set_setting
from backend.intraday import store
from backend.logging_config.logger import setup_logger
from backend.options import bias as bs
from backend.options import chain as ch
from backend.options import instruments as oi
from backend.timefmt import ampm

logger = setup_logger("options")

_lock = threading.Lock()

TAG = "paper"
REAL_NOTE = "no webhook yet · stays off"
NOT_WIRED = "not wired"
TICK_START, TICK_END = time(9, 15), time(15, 40)
RANGE_AT, HARD_EXIT_AT, CLOSE_SNAP_AT, SUMMARY_AT = time(9, 30), time(15, 20), time(15, 30), time(15, 35)
MARKET_CLOSE = time(15, 30)
STEPS = ("contracts", "range", "bias", "mtm", "kill_exit", "time_exit", "hard_exit", "no_trade", "close_snap", "summary")
LIVE_SNAP_EXTRA = 3                        # the live snapshot: the legs' strikes +-3
EXIT_LABEL = {"premium stop": "premium stop", "vwap stop": "VWAP stop", "breakeven stop": "breakeven stop", "target": "target",
              "time exit": "time exit", "hard exit": "hard exit", "kill": "kill switch", "closed by you": "closed by you"}
RULES = [
    "NIFTY weekly options only · paper only · always a credit spread (sell one strike, buy one hedge_points further out)",
    "Bias 9:45-11:00 on 5-minute closes: above the 9:15-9:30 range high, above the futures VWAP and over the previous day's high = BULLISH (mirror = BEARISH)",
    "Chain score 0-3 at the bias: walls, OI change since 9:30 (else the previous close), PCR · 2-3 full size · 1 half · 0 no trade",
    "Sold strike: the farther of 1% OTM and |delta| <= 0.30, at the wall when it is within 1.5%, premium >= Rs 20 · hedge 250 points out (200 when under Rs 2)",
    "Size: 1 lot per Rs 1,50,000, 2 lots at most · VIX > 20 or a crowded PCR halves it",
    "Exits: sold premium +50% (+35% on expiry day) · a 5-minute close back through VWAP · breakeven after a 40% decay · target at 60% of the credit · 15:10 time exit · 15:20 hard exit",
    "No entry: event day, gap > 1%, daily loss 1.5% of capital, kill switch, hold-today, 2 trades done, outside the window",
    "Paper fills at the mid -/+ Rs 1 a leg, never at the last price · charges per order like a real account · nothing is held overnight",
]


# --- switches -------------------------------------------------------------------------------------------------

def killed() -> bool:
    with get_db() as conn:
        return get_setting(conn, "options.killed") == "1"


def set_killed(on: bool, config: AppConfig, client=None, now: Optional[datetime] = None) -> dict:
    """The kill switch: on, an open spread is closed at once (at the quotes when there are any, else at the last mids)."""
    with get_db() as conn:
        set_setting(conn, "options.killed", "1" if on else "0")
    closed = None
    if on:
        with _lock:
            st = _state()
            if st and st.get("open"):
                now = now or datetime.now(IST)
                closed = _exit(st, config, client, now, "kill", force=True)
                _note(st, now, "kill switch ON · spread closed")
                _save(st)
            elif st:
                _note(st, now or datetime.now(IST), "kill switch ON")
                _save(st)
    return {"killed": on, "closed": closed}


def switches(config: AppConfig) -> dict:
    c = config.options
    return {"paper": bool(c.paper), "real": False, "enabled": bool(c.enabled), "killed": killed(), "real_allowed": False, "real_note": REAL_NOTE}


def set_switches(config: AppConfig, **flags) -> dict:
    """paper / enabled; real=True is refused (ValueError "not wired"). Saved with the settings."""
    from backend.config import save_config
    if flags.get("real"):
        raise ValueError(NOT_WIRED)
    start(config)
    for k in ("paper", "enabled"):
        if flags.get(k) is not None:
            setattr(config.options, k, bool(flags[k]))
    config.options.real = False
    save_config(config)
    return switches(config)


# --- state ------------------------------------------------------------------------------------------------------

def _state() -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT payload FROM options_state WHERE id = 1").fetchone()
    return json.loads(r[0]) if r else None


def has_account() -> bool:
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM options_state WHERE id = 1").fetchone() is not None


def _save(st: dict) -> None:
    st["updated_at"] = now_ist()
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO options_state (id, payload) VALUES (1, ?)", (json.dumps(st),))


def _new(config: AppConfig, day: str) -> dict:
    cap = float(config.options.capital)
    return {"started": day, "started_at": now_ist(), "capital": cap, "cash": cap, "charges": 0.0, "deposits": [], "day": None,
            "open": None, "last_day": None, "notes": [], "alerts": [], "alerted": {}}


def _new_day(st: dict, today: str) -> dict:
    return {"date": today, "done": [], "checked": [], "checks": [], "timeline": [], "status": "waiting", "why": None, "range": None,
            "prev": None, "open_px": None, "gap_pct": None, "vwap": None, "vix": None, "bias": None, "bias_at": None, "biases": [],
            "steps": None, "chain": None, "score": None, "chain_line": None, "trades": 0, "realised": 0.0, "charges": 0.0, "hold": False,
            "no_data": False, "expiry": None, "sessions": None, "lot_size": None, "size_note": None, "day_start": round(_equity(st), 2),
            "skipped": [], "closed": []}


def reset() -> None:
    with _lock, get_db() as conn:
        conn.execute("DELETE FROM options_state")


def _tl(st: dict, now: datetime, text: str, key: Optional[str] = None) -> None:
    """A line on today's timeline (with its 12-hour time), once per key."""
    d = st["day"]
    if key and any(x.get("key") == key for x in d["timeline"]):
        return
    d["timeline"].append({"t": ampm(now), "hm": now.strftime("%H:%M"), "text": text, "key": key})


def _alert(st: dict, now: datetime, kind: str, text: str, key: Optional[str] = None) -> None:
    """An alert the API lists (and step 3 sends): once per key a day."""
    day = now.date().isoformat()
    k = f"{day}|{key or kind}"
    if k in st.setdefault("alerted", {}):
        return
    st["alerted"][k] = now_ist()
    st.setdefault("alerts", []).append({"at": now.isoformat(timespec="seconds"), "t": ampm(now), "day": day, "kind": kind, "text": text, "tag": TAG})
    st["alerts"] = st["alerts"][-60:]
    if len(st["alerted"]) > 400:                              # the once-a-day keys: keep the last week's
        keep = (date.fromisoformat(day) - timedelta(days=7)).isoformat()
        st["alerted"] = {k: v for k, v in st["alerted"].items() if k[:10] >= keep}


def _note(st: dict, now: datetime, text: str) -> None:
    st.setdefault("notes", []).append({"at": now.isoformat(timespec="seconds"), "text": text})
    st["notes"] = st["notes"][-100:]


# --- money ------------------------------------------------------------------------------------------------------

def charges(premium: float, sell: bool) -> float:
    """Charges of one option order on `premium` rupees of premium (price x quantity): brokerage Rs 20, STT 0.1% on the
    sell side, exchange 0.0495%, SEBI Rs 10 a crore, GST 18% on brokerage + exchange, stamp 0.003% on the buy side."""
    brokerage = 20.0
    stt = 0.001 * premium if sell else 0.0
    exch = 0.000495 * premium
    sebi = premium / 1e7 * 10
    gst = 0.18 * (brokerage + exch)
    stamp = 0.00003 * premium if not sell else 0.0
    return round(brokerage + stt + exch + sebi + gst + stamp, 2)


def _qty(o: dict) -> int:
    return int(o["lots"]) * int(o["lot_size"])


def _open_pnl(o: Optional[dict]) -> float:
    if not o:
        return 0.0
    value = o.get("value") if o.get("value") is not None else o["credit"]
    return round((o["credit"] - value) * _qty(o) - o["charges_in"], 2)


def _equity(st: dict) -> float:
    """Cash less what it costs to close the open spread now (the credit is already in the cash)."""
    o = st.get("open")
    liability = (o.get("value") if o and o.get("value") is not None else (o["credit"] if o else 0.0)) * (_qty(o) if o else 0)
    return st["cash"] - liability


def _daily_loss(st: dict, cfg) -> dict:
    d = st.get("day") or {}
    used = -min(0.0, float(d.get("realised") or 0.0) + _open_pnl(st.get("open")))
    limit = round(cfg.daily_loss_pct / 100 * st["capital"], 2)
    return {"used": round(used, 2), "limit": limit, "hit": used >= limit > 0}


def lots_for(cfg, capital: float, vix: Optional[float], score: int, crowded: bool) -> tuple[int, list[str]]:
    """The size: 1 lot per capital_per_lot up to max_lots; VIX over vix_half halves (1 at least); score 1 halves (may
    reach 0 = no trade); a crowded PCR halves (1 at least). (lots, notes)."""
    notes = []
    lots = min(int(cfg.max_lots), int(capital // cfg.capital_per_lot))
    if vix is not None and vix > cfg.vix_half:
        lots = max(1, lots // 2)
        notes.append(f"VIX {vix:.1f} > {cfg.vix_half:g} · half size")
    if score == 1:
        lots //= 2
        notes.append("chain score 1 · half size")
    if crowded:
        lots = max(1, lots // 2)
        notes.append("PCR crowded · half size")
    return lots, notes


# --- data (Kite: quotes and 1-minute candles only) ---------------------------------------------------------------

def _has_token(client) -> bool:
    return client is not None and bool(getattr(client, "has_token", False))


def _bars(client, token: int, day: str) -> list[dict]:
    """Today's 1-minute bars of a token from Kite's minute history ({t, o, h, l, c, v})."""
    d = date.fromisoformat(day)
    rows = client.historical_minute(token, d, d)
    return store.from_kite(rows).get(day, [])


def _daily_nifty() -> list[dict]:
    with get_db() as conn:
        return get_candles(conn, oi.INDEX_TOKEN, limit=10)


def _contracts(st: dict) -> list[dict]:
    exp = st["day"].get("expiry")
    return oi.chain(exp) if exp else []


def _fetch_chain(client, st: dict, now: datetime, spot_hint: Optional[float], with_vix: bool = False, strikes: Optional[list] = None) -> Optional[dict]:
    """One quote call for the chain rows around the spot hint (or the given strikes); None without a login / quotes."""
    if not _has_token(client):
        return None
    d = st["day"]
    rows = ch.pick_rows(_contracts(st), spot_hint or 0, strikes=strikes)
    if not rows:
        return None
    try:
        res = ch.fetch(client, rows, now.time(), int(d.get("sessions") or 0), spot_hint, with_vix)
    except Exception as exc:
        logger.warning("Options chain quote failed: %s", exc)
        return None
    return res if res["rows"] else None


def _chain_block(res: dict, base: Optional[dict], sc: Optional[dict], expiry: str) -> dict:
    """The chain as the page shows it: per strike CE / PE {oi, oi_chg, iv, ltp, delta}, the walls, PCR, IV sides."""
    spot = res["spot"]
    chg = ch.per_strike_change(res["rows"], base["rows"] if base else None)
    by = {}
    for r in res["rows"]:
        k = by.setdefault(r["strike"], {"strike": r["strike"]})
        k[r["type"].lower()] = {"oi": r["oi"], "oi_chg": chg.get((r["strike"], r["type"])), "iv": round(r["iv"] * 100, 1) if r["iv"] else None,
                                "ltp": r["ltp"], "mid": r["mid"], "delta": r["delta"]}
    w = (sc or {}).get("walls") or ch.walls(res["rows"], spot)
    return {"at": res["at"], "expiry": expiry, "spot": spot, "vix": res.get("vix"), "atm": ch.grid(spot),
            "strikes": [by[k] for k in sorted(by)], "walls": {"support": w["support"], "resistance": w["resistance"]},
            "pcr": (sc or {}).get("pcr") if sc else ch.pcr(res["rows"], spot), "score": (sc or {}).get("score"), "line": (sc or {}).get("line"),
            "oi_chg": (sc or {}).get("oi_chg") if sc else ch.oi_change(res["rows"], base["rows"] if base else None, spot),
            "iv": ch.iv_sides(res["rows"], spot), "baseline": base["kind"] if base else None, "crowded": (sc or {}).get("crowded")}


# --- the steps ---------------------------------------------------------------------------------------------------

def steps(now: datetime, config: AppConfig, st: dict, is_killed: bool = False) -> list[str]:
    """Which steps this minute runs (pure): see the module docstring."""
    c, d, t, out = config.options, st["day"], now.time(), []
    done, o = d["done"], st.get("open")
    if is_killed and o:
        out.append("kill_exit")
    if "contracts" not in done and t < MARKET_CLOSE:
        out.append("contracts")
    if "range" not in done and RANGE_AT <= t < MARKET_CLOSE and not d["no_data"]:
        out.append("range")
    slot = bs.slot_of(t)
    if ("range" in done or "range" in out) and bs.in_window(bs.hm(slot), c.window_start, c.window_end) and slot not in d["checked"] \
            and not d["no_data"] and d["status"] not in ("done",):
        out.append("bias")
    if o and not is_killed:
        out.append("mtm")
        if t >= bs.hm(c.time_exit) and "time_exit" not in done:
            out.append("time_exit")
        if t >= HARD_EXIT_AT:
            out.append("hard_exit")
    if t > bs.hm(c.window_end) and d["status"] == "waiting" and "no_trade" not in done:
        out.append("no_trade")
    if t >= CLOSE_SNAP_AT and "close_snap" not in done:
        out.append("close_snap")
    if t >= SUMMARY_AT and "summary" not in done and not o:
        out.append("summary")
    return out


def tick(config: AppConfig, client, now: Optional[datetime] = None) -> dict:
    """The minute's work: starts the book on the first tick, rolls the day, runs `steps`. {ran, status, open}."""
    now = now or datetime.now(IST)
    today = now.date().isoformat()
    with _lock:
        st = _state() or _new(config, today)
        if st.get("day") is None or st["day"]["date"] != today:
            if st.get("day") and "summary" not in st["day"]["done"]:
                _summarise(st, config, datetime.combine(date.fromisoformat(st["day"]["date"]), SUMMARY_AT, tzinfo=IST))
            st["day"] = _new_day(st, today)
        ran = steps(now, config, st, killed())
        for s in ran:
            try:
                getattr(_Steps, s)(st, config, client, now)
            except Exception as exc:
                logger.exception("Options step %s failed", s)
                _tl(st, now, f"{s} failed · {str(exc)[:80]}", key=f"fail|{s}|{now.strftime('%H:%M')}")
        if st["day"]["status"] == "closed" and now.time() > bs.hm(config.options.window_end):
            st["day"]["status"] = "done"                          # the window shut: no re-entry possible any more
        _save(st)
        return {"ran": ran, "status": st["day"]["status"], "open": bool(st.get("open")), "day": today}


def _no_data(st: dict, now: datetime, why: str) -> None:
    d = st["day"]
    d["no_data"], d["status"], d["why"] = True, "no_data", f"no data · {why}"
    _tl(st, now, f"no data · {why}", key="no_data")
    _alert(st, now, "no_data", f"no data · {why}", key="no_data")


class _Steps:
    """One static method per step; each takes (st, config, client, now) and records itself in st['day']['done']."""

    @staticmethod
    def contracts(st, config, client, now):
        d = st["day"]
        today = now.date()
        if _has_token(client):
            try:
                oi.refresh(client, today)
            except Exception as exc:
                logger.warning("Options contracts refresh failed: %s", exc)
        exp = oi.near_expiry(today.isoformat())
        if not exp:
            if not _has_token(client):
                _no_data(st, now, "Kite login missing")
            else:
                _no_data(st, now, "no NIFTY option contracts")
            d["done"].append("contracts")
            return
        d["expiry"], d["lot_size"] = exp, oi.lot_size(exp)
        d["sessions"] = oi.sessions_to_expiry(exp, today)
        d["done"].append("contracts")
        _tl(st, now, f"expiry {exp} · {d['sessions']} session{'s' if d['sessions'] != 1 else ''} to expiry · lot {d['lot_size']}", key="contracts")

    @staticmethod
    def range(st, config, client, now):
        d = st["day"]
        today = now.date().isoformat()
        if not _has_token(client):
            _no_data(st, now, "Kite login missing")
            return
        try:
            idx = _bars(client, oi.INDEX_TOKEN, today)
        except Exception as exc:
            logger.warning("Options index bars failed: %s", exc)
            idx = []
        rng = bs.opening_range(idx)
        if not rng:
            if now.time() >= time(9, 45):
                _no_data(st, now, "no index candles")
            return                                              # try again next minute
        d["range"] = rng
        d["open_px"] = idx[0]["o"]
        d["prev"] = bs.prev_day_levels(_daily_nifty(), today)
        d["gap_pct"] = bs.gap_pct(d["open_px"], d["prev"]["close"]) if d["prev"] else None
        d["done"].append("range")
        _tl(st, now, f"range {rng['low']:,.0f}-{rng['high']:,.0f}" + (f" · gap {d['gap_pct']:+.2f}%" if d["gap_pct"] is not None else "")
            + (f" · prev day {d['prev']['low']:,.0f}-{d['prev']['high']:,.0f}" if d["prev"] else " · no previous-day candle"), key="range")
        spot = idx[-1]["c"]
        res = _fetch_chain(client, st, now, spot, with_vix=True)
        if res and d.get("expiry"):
            ch.snapshot(today, "open", d["expiry"], res["rows"], res["spot"], now.isoformat(timespec="seconds"))
            d["vix"] = res.get("vix")
            d["chain"] = _chain_block(res, None, None, d["expiry"])
            _tl(st, now, f"9:30 chain snapshot · spot {res['spot']:,.0f}" + (f" · VIX {res['vix']:.1f}" if res.get("vix") else ""), key="open_snap")

    @staticmethod
    def bias(st, config, client, now):
        c, d = config.options, st["day"]
        today, slot = now.date().isoformat(), bs.slot_of(now.time())
        d["checked"].append(slot)
        if not _has_token(client):
            _no_data(st, now, "Kite login missing")
            return
        fut = oi.future()
        try:
            idx = _bars(client, oi.INDEX_TOKEN, today)
            futb = _bars(client, fut["token"], today) if fut else []
        except Exception as exc:
            logger.warning("Options minute bars failed: %s", exc)
            return
        bar = bs.last_closed_5m(idx, slot)
        if not bar or not d["range"]:
            return
        vw = bs.vwap(futb, until=slot)
        res = bs.test_bias(bar["c"], d["range"], vw, d["prev"])
        d["vwap"] = vw
        d["checks"].append({"t": slot, "close": bar["c"], "vwap": vw, "bias": res["bias"], **res["checks"]})
        d["checks"] = d["checks"][-20:]
        spot = idx[-1]["c"]
        q = _fetch_chain(client, st, now, spot, with_vix=True)
        base = ch.baseline(today, d["expiry"]) if d.get("expiry") else None
        sc = None
        if q:
            ch.snapshot(today, "bias", d["expiry"], q["rows"], q["spot"], now.isoformat(timespec="seconds"))
            d["vix"] = q.get("vix") or d.get("vix")
            if res["bias"]:
                sc = ch.score(res["bias"], q["rows"], q["spot"], base["rows"] if base else None, c.pcr_bull, c.pcr_bear)
            d["chain"] = _chain_block(q, base, sc, d["expiry"])
        if not res["bias"]:
            return
        _bias_found(st, config, client, now, slot, res, bar, vw, q, sc)

    @staticmethod
    def mtm(st, config, client, now):
        o = st["open"]
        why = _mark(st, config, client, now)
        if why:
            _exit(st, config, client, now, why)
        elif o is not None and _daily_loss(st, config.options)["hit"]:
            _alert(st, now, "daily_loss", f"daily loss limit reached · {_daily_loss(st, config.options)['used']:,.0f} of "
                   f"{_daily_loss(st, config.options)['limit']:,.0f} · no new entries today · the open spread keeps its stops", key="daily_loss")

    @staticmethod
    def kill_exit(st, config, client, now):
        _exit(st, config, client, now, "kill", force=True)

    @staticmethod
    def time_exit(st, config, client, now):
        st["day"]["done"].append("time_exit")
        _exit(st, config, client, now, "time exit")

    @staticmethod
    def hard_exit(st, config, client, now):
        _exit(st, config, client, now, "hard exit", force=True)

    @staticmethod
    def no_trade(st, config, client, now):
        d = st["day"]
        d["done"].append("no_trade")
        d["status"], d["why"] = "no_trade", f"no break by {ampm(config.options.window_end)}"
        _tl(st, now, "no trade today · no bias by " + ampm(config.options.window_end), key="no_trade")
        _alert(st, now, "no_trade", f"no trade today · no bias by {ampm(config.options.window_end)}", key="no_trade")

    @staticmethod
    def close_snap(st, config, client, now):
        d = st["day"]
        d["done"].append("close_snap")
        if not _has_token(client) or not d.get("expiry"):
            return
        spot = (d.get("chain") or {}).get("spot")
        res = _fetch_chain(client, st, now, spot, with_vix=True)
        if res:
            ch.snapshot(now.date().isoformat(), "close", d["expiry"], res["rows"], res["spot"], now.isoformat(timespec="seconds"))
            d["chain"] = _chain_block(res, ch.baseline(now.date().isoformat(), d["expiry"]), None, d["expiry"])
            _tl(st, now, f"close snapshot · spot {res['spot']:,.0f}", key="close_snap")

    @staticmethod
    def summary(st, config, client, now):
        _summarise(st, config, now)


# --- the bias found: score, size, strikes, entry ------------------------------------------------------------------

def _guard_reasons(st: dict, config: AppConfig, now: datetime, bias: str) -> list[str]:
    c, d = config.options, st["day"]
    today = now.date().isoformat()
    out = []
    if killed():
        out.append("kill switch")
    if not c.enabled:
        out.append("switched off")
    if not c.paper:
        out.append("paper off")
    if d.get("hold"):
        out.append("hold today")
    if bs.is_event_day(today, c.event_days):
        out.append("event day")
    if d.get("gap_pct") is not None and abs(d["gap_pct"]) > c.max_gap_pct:
        out.append(f"gap {d['gap_pct']:+.2f}% > {c.max_gap_pct:g}%")
    if _daily_loss(st, c)["hit"]:
        out.append("daily loss limit")
    if d["trades"] >= c.max_trades_day:
        out.append(f"{c.max_trades_day} trades done")
    if d["trades"] >= 1 and d["biases"] and d["biases"][-1] == bias:
        out.append("re-entry only on a flipped bias")
    if st.get("open"):
        out.append("a spread is open")
    return out


def _bias_found(st, config, client, now, slot, res, bar, vw, q, sc) -> None:
    c, d = config.options, st["day"]
    bias = res["bias"]
    prev = d["prev"] or {}
    level = prev.get("high") if bias == bs.BULLISH else prev.get("low")
    word = "over" if bias == bs.BULLISH else "under"
    steps_ = [{"n": 1, "name": "Opening range", "ok": True, "text": f"5-min close {bar['c']:,.2f} {word} the range {'high' if bias == bs.BULLISH else 'low'} "
               f"{(d['range']['high'] if bias == bs.BULLISH else d['range']['low']):,.0f}", "close": bar["c"], "range": d["range"]},
              {"n": 2, "name": "VWAP (futures)", "ok": True, "text": f"close {bar['c']:,.2f} {word} VWAP {vw:,.2f}" if vw else "VWAP unknown", "vwap": vw},
              {"n": 3, "name": "Previous day", "ok": True, "text": f"close {bar['c']:,.2f} {'≥' if bias == bs.BULLISH else '≤'} prev {'high' if bias == bs.BULLISH else 'low'} {level:,.0f}" if level else "no previous-day level", "level": level},
              {"n": 4, "name": "Option chain", "ok": bool(sc and sc["score"] >= 1), "text": sc["line"] if sc else "chain not available", "score": sc["score"] if sc else None}]
    if bias != d.get("bias"):
        _tl(st, now, f"{bias} · close {bar['c']:,.0f} · VWAP {vw:,.0f}" if vw else f"{bias} · close {bar['c']:,.0f}", key=f"bias|{bias}|{slot}")
    if bias != d.get("bias") or not d["biases"] or d["biases"][-1] != bias:     # the record freezes once this bias was traded
        d["bias"], d["bias_at"], d["steps"] = bias, ampm(now), steps_
        d["score"], d["chain_line"] = (sc["score"] if sc else None), (sc["line"] if sc else None)
    reasons = _guard_reasons(st, config, now, bias)
    if reasons:
        if "a spread is open" not in reasons:
            key = "skip|" + "|".join(reasons)
            if key not in d["skipped"]:
                d["skipped"].append(key)
                _tl(st, now, f"{bias} · no entry · {' · '.join(reasons)}", key=key)
                loud = [r for r in reasons if not r.startswith(("re-entry only", f"{c.max_trades_day} trades"))]
                if loud:                                           # the day's own limits are not news; the guards are
                    kind = "event_day" if "event day" in reasons else "skipped"
                    _alert(st, now, kind, f"{bias} found · no entry · {' · '.join(reasons)}", key=key)
        return
    if not q or not sc:
        _tl(st, now, f"{bias} · chain not available · no entry", key=f"nochain|{slot}")
        return
    if sc["score"] == 0:
        _tl(st, now, f"{bias} · chain disagrees ({sc['line']})", key=f"disagree|{bias}")
        _alert(st, now, "chain_disagrees", f"bias found · chain disagrees · {bias} · {sc['line']}", key=f"disagree|{bias}")
        return
    lots, notes = lots_for(c, st["capital"], q.get("vix"), sc["score"], sc["crowded"])
    d["size_note"] = " · ".join(notes) if notes else None
    for n in notes:
        if n.startswith("VIX"):
            _alert(st, now, "vix_half", n, key="vix_half")
    if lots < 1:
        _tl(st, now, f"{bias} · no trade · " + (" · ".join(notes) or "0 lots"), key=f"nolots|{bias}")
        _alert(st, now, "no_trade_size", f"{bias} · no trade · " + (" · ".join(notes) or "0 lots"), key=f"nolots|{bias}")
        return
    sel = ch.select_strikes(bias, q["rows"], q["spot"], sc["walls"], c.otm_pct, c.max_delta, c.min_premium, c.hedge_points)
    if not sel["ok"]:
        _tl(st, now, f"{bias} · no trade · {sel['why']}", key=f"nostrike|{bias}")
        _alert(st, now, "no_trade_strikes", f"{bias} · no trade · {sel['why']}", key=f"nostrike|{bias}")
        return
    _enter(st, config, now, bias, q, sc, sel, lots)


def _enter(st: dict, config: AppConfig, now: datetime, bias: str, q: dict, sc: dict, sel: dict, lots: int) -> dict:
    """The paper entry: sell the sold leg at mid - 1, buy the hedge at mid + 1; charges per order; the state's open spread."""
    c, d = config.options, st["day"]
    lot = int(d["lot_size"] or 0)
    qty = lots * lot
    sold_px = round(sel["sold"]["mid"] - 1.0, 2)
    hedge_px = round(sel["hedge"]["mid"] + 1.0, 2)
    credit = round(sold_px - hedge_px, 2)
    ch_in = charges(sold_px * qty, sell=True) + charges(hedge_px * qty, sell=False)
    st["cash"] += credit * qty - ch_in
    st["charges"] += ch_in
    d["charges"] += ch_in
    expiry_day = (d.get("sessions") or 0) == 0
    stop_pct = c.stop_pct_expiry if expiry_day else c.stop_pct
    leg = lambda r, px, side: {"strike": r["strike"], "type": r["type"], "tradingsymbol": r["tradingsymbol"], "token": r["token"], "side": side,
                               "entry": px, "mid_at_entry": r["mid"], "last": r["mid"], "iv": r["iv"], "delta": r["delta"]}
    o = {"bias": bias, "expiry": d["expiry"], "sessions": d.get("sessions"), "expiry_day": expiry_day, "lots": lots, "lot_size": lot, "qty": qty,
         "sold": leg(sel["sold"], sold_px, "SELL"), "hedge": leg(sel["hedge"], hedge_px, "BUY"), "credit": credit, "credit_rs": round(credit * qty, 2),
         "width": sel["width"], "stop_pct": stop_pct, "stop_premium": round(sold_px * (1 + stop_pct / 100), 2),
         "be_at": round(sold_px * (1 - c.be_after_pct / 100), 2), "be_active": False, "target_value": round(credit * (1 - c.target_pct / 100), 2),
         "worst_case": round(sel["width"] * qty - credit * qty, 2), "margin_est": round(c.margin_per_lot * lots, 2), "charges_in": round(ch_in, 2),
         "opened_at": now.isoformat(timespec="seconds"), "opened_t": ampm(now), "spot_at_entry": q["spot"], "vwap_at_entry": d.get("vwap"),
         "score": sc["score"], "chain_line": sc["line"], "value": credit, "pnl": round(-ch_in, 2), "spot": q["spot"], "vwap_checked": [], "n": d["trades"] + 1}
    st["open"] = o
    d["biases"].append(bias)
    d["trades"] += 1
    d["status"] = "open"
    s, h = o["sold"], o["hedge"]
    _tl(st, now, f"{'re-entry · ' if o['n'] > 1 else ''}sold {s['strike']:,.0f} {s['type']} @ {s['entry']:.2f} · bought {h['strike']:,.0f} {h['type']} @ {h['entry']:.2f} · "
        f"credit {credit:.2f} x {qty} = ₹{credit * qty:,.0f} · {lots} lot{'s' if lots != 1 else ''} · stop {o['stop_premium']:.2f} · target {o['target_value']:.2f}", key=f"entry|{o['n']}")
    _alert(st, now, "entry", f"{bias} · sold {s['strike']:,.0f} {s['type']} / bought {h['strike']:,.0f} {h['type']} · credit ₹{credit * qty:,.0f} · "
           f"stop {o['stop_premium']:.2f} · target {o['target_value']:.2f} · worst case ₹{o['worst_case']:,.0f} · {sc['line']}", key=f"entry|{o['n']}")
    return o


# --- the open spread: MTM, stops, exits -----------------------------------------------------------------------------

def _leg_quotes(client, st: dict, now: datetime) -> Optional[dict]:
    """The legs' quotes (+-3 strikes of the sold type) in one call; snapshot 'live'. {tradingsymbol: row, spot}."""
    o = st["open"]
    lo, hi = min(o["sold"]["strike"], o["hedge"]["strike"]), max(o["sold"]["strike"], o["hedge"]["strike"])
    strikes = [k for k in [lo + i * ch.STRIKE_STEP for i in range(-LIVE_SNAP_EXTRA, int((hi - lo) / ch.STRIKE_STEP) + LIVE_SNAP_EXTRA + 1)]]
    res = _fetch_chain(client, st, now, o.get("spot"), strikes=strikes)
    if not res:
        return None
    rows = [r for r in res["rows"] if r["type"] == o["sold"]["type"]]
    ch.snapshot(now.date().isoformat(), "live", o["expiry"], rows, res["spot"], now.isoformat(timespec="seconds"))
    return {"rows": {r["tradingsymbol"]: r for r in rows}, "spot": res["spot"]}


def _mark(st: dict, config: AppConfig, client, now: datetime) -> Optional[str]:
    """MTM the open spread from the quotes' mids and return the exit reason when one fires (None otherwise)."""
    o, c = st["open"], config.options
    q = _leg_quotes(client, st, now) if _has_token(client) else None
    if not q:
        return None
    s, h = q["rows"].get(o["sold"]["tradingsymbol"]), q["rows"].get(o["hedge"]["tradingsymbol"])
    if not s or not h or not s.get("mid") or h.get("mid") is None:
        return None
    o["sold"]["last"], o["hedge"]["last"] = s["mid"], h["mid"]
    o["sold"]["iv"], o["sold"]["delta"], o["hedge"]["iv"], o["hedge"]["delta"] = s["iv"], s["delta"], h["iv"], h["delta"]
    o["value"] = round(s["mid"] - h["mid"], 2)
    o["spot"] = q["spot"]
    o["pnl"] = _open_pnl(o)
    o["marked_at"] = now.isoformat(timespec="seconds")
    if s["mid"] <= o["be_at"] and not o["be_active"]:
        o["be_active"] = True
        _tl(st, now, f"sold premium {s['mid']:.2f} · decayed {c.be_after_pct:g}% · stop now breakeven", key=f"be|{o['n']}")
    if s["mid"] >= o["stop_premium"]:
        return "premium stop"
    if o["be_active"] and o["value"] >= o["credit"]:
        return "breakeven stop"
    if o["value"] <= o["target_value"]:
        return "target"
    t = now.time()
    slot = bs.slot_of(t)
    if bs.is_5m_close(t) and slot not in o["vwap_checked"] and t < bs.hm(c.time_exit):
        o["vwap_checked"].append(slot)
        try:
            today = now.date().isoformat()
            fut = oi.future()
            idx = _bars(client, oi.INDEX_TOKEN, today)
            futb = _bars(client, fut["token"], today) if fut else []
        except Exception as exc:
            logger.warning("Options VWAP check failed: %s", exc)
            return None
        bar = bs.last_closed_5m(idx, slot)
        vw = bs.vwap(futb, until=slot)
        if bar and vw:
            st["day"]["vwap"] = vw
            o["vwap"] = vw
            if (o["bias"] == bs.BULLISH and bar["c"] < vw) or (o["bias"] == bs.BEARISH and bar["c"] > vw):
                return "vwap stop"
    return None


def _exit(st: dict, config: AppConfig, client, now: datetime, why: str, force: bool = False) -> Optional[dict]:
    """Close the open spread at the quotes (buy the sold leg back at mid + 1, sell the hedge at mid - 1). Without quotes
    the exit waits (the hard exit at 15:20 and the kill switch close at the last known mids instead: nothing is held
    overnight). Returns the trade row or None."""
    o = st.get("open")
    if not o:
        return None
    d = st["day"]
    q = _leg_quotes(client, st, now) if _has_token(client) else None
    s = q["rows"].get(o["sold"]["tradingsymbol"]) if q else None
    h = q["rows"].get(o["hedge"]["tradingsymbol"]) if q else None
    est = not (s and h and s.get("mid") and h.get("mid") is not None)
    if est and not force:
        _tl(st, now, f"{EXIT_LABEL.get(why, why)} · no quote · retrying", key=f"noquote|{why}|{now.strftime('%H:%M')}")
        return None
    sold_mid = s["mid"] if not est else o["sold"]["last"]
    hedge_mid = h["mid"] if not est else o["hedge"]["last"]
    sold_px, hedge_px = round(sold_mid + 1.0, 2), round(max(0.05, hedge_mid - 1.0), 2)
    qty = _qty(o)
    cost = round(sold_px - hedge_px, 2)
    ch_out = charges(sold_px * qty, sell=False) + charges(hedge_px * qty, sell=True)
    st["cash"] -= cost * qty + ch_out
    st["charges"] += ch_out
    d["charges"] += ch_out
    net = round((o["credit"] - cost) * qty - o["charges_in"] - ch_out, 2)
    t = {"day": d["date"], "n": o["n"], "bias": o["bias"], "expiry": o["expiry"], "sessions": o.get("sessions"), "lots": o["lots"], "lot_size": o["lot_size"], "qty": qty,
         "sold": {**o["sold"], "exit": sold_px}, "hedge": {**o["hedge"], "exit": hedge_px}, "credit": o["credit"], "cost": cost,
         "credit_rs": o["credit_rs"], "gross": round((o["credit"] - cost) * qty, 2), "charges": round(o["charges_in"] + ch_out, 2), "net": net,
         "in": o["opened_t"], "out": ampm(now), "opened_at": o["opened_at"], "closed_at": now.isoformat(timespec="seconds"), "why": why,
         "label": EXIT_LABEL.get(why, why), "est": est, "score": o["score"], "chain_line": o["chain_line"], "be_active": o["be_active"],
         "spot_in": o.get("spot_at_entry"), "spot_out": q["spot"] if q else o.get("spot"), "book": TAG}
    with get_db() as conn:
        conn.execute("INSERT INTO options_trades (day, payload) VALUES (?, ?)", (d["date"], json.dumps(t)))
    d["realised"] = round(d["realised"] + net, 2)
    d["closed"].append({"n": o["n"], "why": why, "net": net, "t": ampm(now)})
    st["open"] = None
    d["status"] = "done" if d["trades"] >= config.options.max_trades_day or now.time() > bs.hm(config.options.window_end) else "closed"
    d["why"] = EXIT_LABEL.get(why, why)
    _tl(st, now, f"{EXIT_LABEL.get(why, why)} · bought back {o['sold']['strike']:,.0f} {o['sold']['type']} @ {sold_px:.2f} · sold hedge @ {hedge_px:.2f} · "
        f"net {net:+,.0f}" + (" · at the last known mid" if est else ""), key=f"exit|{o['n']}")
    _alert(st, now, "exit", f"{EXIT_LABEL.get(why, why)} · {o['bias']} {o['sold']['strike']:,.0f} {o['sold']['type']} spread · net ₹{net:+,.0f}"
           + (" · closed at the last known mid (no quote)" if est else ""), key=f"exit|{o['n']}")
    return t


def _summarise(st: dict, config: AppConfig, now: datetime) -> dict:
    """The 15:35 day summary: one row in options_days (rewritten if the day is summarised again), the equity row."""
    d = st["day"]
    if "summary" in d["done"]:
        return d
    d["done"].append("summary")
    if d["status"] == "waiting":
        d["status"], d["why"] = "no_trade", d["why"] or "no bias"
    eq = round(_equity(st), 2)
    trades = _trades_of(d["date"])
    first = trades[0] if trades else None
    row = {"day": d["date"], "status": d["status"], "why": d["why"], "range": d["range"], "gap_pct": d["gap_pct"], "prev": d["prev"], "vwap": d["vwap"],
           "vix": d["vix"], "bias": d["bias"], "bias_at": d["bias_at"], "score": d["score"], "chain_line": d["chain_line"], "expiry": d["expiry"],
           "sessions": d["sessions"], "trades": d["trades"], "pnl": round(d["realised"], 2), "charges": round(d["charges"], 2), "equity": eq,
           "mtm": round(eq - (d.get("day_start") or eq), 2), "size_note": d["size_note"],
           "strikes": (f"{first['sold']['strike']:,.0f} {first['sold']['type']} / {first['hedge']['strike']:,.0f} {first['hedge']['type']}" if first else None),
           "credit": first["credit"] if first else None, "lots": first["lots"] if first else None, "result": (first["label"] if len(trades) == 1 else
           " · ".join(t["label"] for t in trades)) if trades else d["why"], "notes": d["timeline"], "closed": d["closed"], "no_data": d["no_data"]}
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO options_days (day, payload) VALUES (?, ?)", (d["date"], json.dumps(row)))
        conn.execute("INSERT OR REPLACE INTO options_equity (day, equity, gross, nifty) VALUES (?, ?, ?, ?)",
                     (d["date"], eq, round(eq + st["charges"], 2), (d.get("chain") or {}).get("spot")))
    st["last_day"] = row
    _tl(st, now, f"day summary · {row['result']} · P&L {row['pnl']:+,.0f}", key="summary")
    _alert(st, now, "summary", f"day summary · {row['result']} · P&L ₹{row['pnl']:+,.0f} · equity ₹{eq:,.0f}", key="summary")
    return row


# --- by hand: close, hold, capital ---------------------------------------------------------------------------------

def close_now(config: AppConfig, client, now: Optional[datetime] = None) -> dict:
    """Close the open spread now (the UI confirms first). ValueError when nothing is open."""
    now = now or datetime.now(IST)
    with _lock:
        st = _state()
        if not st or not st.get("open"):
            raise ValueError("no spread is open")
        t = _exit(st, config, client, now, "closed by you", force=True)
        _save(st)
        return {"status": "ok", "trade": t}


def hold_today(config: AppConfig, now: Optional[datetime] = None, note: str = "hold today") -> dict:
    """No entries today (an open spread keeps its exits)."""
    now = now or datetime.now(IST)
    today = now.date().isoformat()
    with _lock:
        st = _state() or _new(config, today)
        if st.get("day") is None or st["day"]["date"] != today:
            st["day"] = _new_day(st, today)
        st["day"]["hold"] = True
        _tl(st, now, f"hold today · {note}", key="hold")
        _save(st)
    return {"status": "ok", "hold": True, "day": today}


def set_capital(config: AppConfig, capital: float) -> dict:
    """A new capital: before the start it is the setting; afterwards the difference is money added / withdrawn."""
    from backend.config import save_config
    capital = float(capital)
    with _lock:
        st = _state()
        config.options.capital = capital
        delta = None
        if st and st.get("cash") == st["capital"] and not st.get("day") and not st.get("deposits"):
            st["capital"] = st["cash"] = capital                  # untouched book: the setting simply changes
            _save(st)
        elif st:
            delta = round(capital - st["capital"], 2)
            if st["cash"] + delta < 0:
                raise ValueError(f"withdrawing {-delta:,.0f} is more than the cash ({st['cash']:,.0f})")
            st["cash"] += delta
            st["capital"] = capital
            if st.get("day"):
                st["day"]["day_start"] = (st["day"].get("day_start") or capital) + delta
            st.setdefault("deposits", []).append({"at": now_ist(), "amount": delta, "capital": capital})
            _save(st)
        save_config(config)
        return {"status": "ok", "capital": capital, "delta": delta}


# --- views -----------------------------------------------------------------------------------------------------------

def _trades_of(day: str) -> list[dict]:
    with get_db() as conn:
        return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM options_trades WHERE day = ? ORDER BY id", (day,))]


def trades(limit: int = 100) -> list[dict]:
    with get_db() as conn:
        return [{**json.loads(r[1]), "id": r[0]} for r in conn.execute("SELECT id, payload FROM options_trades ORDER BY id DESC LIMIT ?", (limit,))]


def days(limit: int = 30) -> list[dict]:
    with get_db() as conn:
        return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM options_days ORDER BY day DESC LIMIT ?", (limit,))]


def _curve() -> dict:
    with get_db() as conn:
        rows = conn.execute("SELECT day, equity FROM options_equity ORDER BY day").fetchall()
    return {"dates": [r[0] for r in rows], "net": [r[1] for r in rows]}


def chain_view(st: Optional[dict] = None, today: Optional[str] = None) -> dict:
    """GET /api/options/chain: today's chain block (else the last snapshot, read back)."""
    st = st or _state()
    today = today or now_ist()[:10]
    d = (st or {}).get("day") or {}
    if d.get("chain") and d.get("date") == today:
        return d["chain"]
    snap = ch.last_snapshot()
    if not snap:
        return {"strikes": [], "at": None}
    spot = snap["spot"] or 0
    by = {}
    for r in snap["rows"]:
        k = by.setdefault(r["strike"], {"strike": r["strike"]})
        k[r["type"].lower()] = {"oi": r["oi"], "oi_chg": None, "iv": round(r["iv"] * 100, 1) if r["iv"] else None, "ltp": r["ltp"], "mid": None, "delta": r["delta"]}
    w = ch.walls(snap["rows"], spot) if spot else {"support": None, "resistance": None}
    return {"at": snap["ts"], "expiry": snap["expiry"], "spot": spot, "vix": None, "atm": ch.grid(spot) if spot else None, "strikes": [by[k] for k in sorted(by)],
            "walls": {"support": w["support"], "resistance": w["resistance"]}, "pcr": ch.pcr(snap["rows"], spot) if spot else None, "score": None, "line": None,
            "oi_chg": None, "iv": ch.iv_sides(snap["rows"], spot) if spot else {"put": None, "call": None}, "baseline": None, "kind": snap["kind"]}


def _open_view(o: Optional[dict], c) -> Optional[dict]:
    if not o:
        return None
    legs = []
    for k in ("sold", "hedge"):
        leg = o[k]
        legs.append({"leg": k, "side": leg["side"], "strike": leg["strike"], "type": leg["type"], "tradingsymbol": leg["tradingsymbol"], "entry": leg["entry"],
                     "last": leg["last"], "iv": round(leg["iv"] * 100, 1) if leg.get("iv") else None, "delta": leg.get("delta"), "qty": o["qty"],
                     "pnl": round((leg["entry"] - leg["last"]) * o["qty"] if k == "sold" else (leg["last"] - leg["entry"]) * o["qty"], 2)})
    value = o.get("value") if o.get("value") is not None else o["credit"]
    near_stop = o["sold"]["last"] >= o["stop_premium"] * 0.9
    return {**{k: o[k] for k in ("bias", "expiry", "sessions", "expiry_day", "lots", "lot_size", "qty", "credit", "credit_rs", "width", "stop_pct", "stop_premium",
                                 "be_at", "be_active", "target_value", "worst_case", "margin_est", "charges_in", "opened_at", "opened_t", "score", "chain_line", "n")},
            "legs": legs, "value": value, "value_rs": round(value * o["qty"], 2), "pnl": _open_pnl(o), "spot": o.get("spot"), "vwap": o.get("vwap") or o.get("vwap_at_entry"),
            "marked_at": o.get("marked_at"), "near_stop": near_stop, "decay_pct": round((1 - o["sold"]["last"] / o["sold"]["entry"]) * 100, 1) if o["sold"]["entry"] else None,
            "footer": f"credit ₹{o['credit_rs']:,.0f} · value ₹{value * o['qty']:,.0f} · stop {o['stop_premium']:.2f} ({o['stop_pct']:g}%)"
                      f"{' · breakeven stop on' if o['be_active'] else ''} · target {o['target_value']:.2f} · time exit {ampm(c.time_exit)}"}


def _cards(st: Optional[dict], cfg, dl: list, eq: float, open_pnl: float) -> list[dict]:
    cap = st["capital"] if st else float(cfg.capital)
    traded = [d for d in dl if d.get("trades")]
    won = [d for d in traded if (d.get("pnl") or 0) > 0]
    today = (st or {}).get("day") or {}
    mtm = round(eq - (today.get("day_start") or cap), 2) if st else 0.0
    return [{"key": "capital", "label": "Capital", "value": cap, "sub": f"{len((st or {}).get('deposits') or [])} deposits" if st else "not started", "fmt": "rs"},
            {"key": "equity", "label": "Equity", "value": round(eq, 2), "sub": f"net {eq - cap:+,.0f} · {((eq - cap) / cap * 100 if cap else 0):+.2f}%", "fmt": "rs"},
            {"key": "today", "label": "Today", "value": mtm, "sub": f"realised {today.get('realised', 0.0):+,.0f} · open {open_pnl:+,.0f}", "fmt": "rs_signed"},
            {"key": "win", "label": "Win rate", "value": round(len(won) / len(traded) * 100, 0) if traded else None, "sub": f"{len(won)} of {len(traded)} days traded", "fmt": "pct"},
            {"key": "days", "label": "Days", "value": len(dl), "sub": f"{sum(1 for d in dl if d.get('status') == 'no_trade')} no trade · {sum(1 for d in dl if d.get('no_data'))} no data", "fmt": "n"},
            {"key": "charges", "label": "Charges", "value": round((st or {}).get("charges") or 0.0, 2), "sub": "paper · per order like a real account", "fmt": "rs"}]


def _bias_steps(d: dict, c) -> dict:
    steps_ = d.get("steps")
    if not steps_:
        rng = d.get("range")
        prev = d.get("prev") or {}
        last = (d.get("checks") or [{}])[-1] if d.get("checks") else {}
        steps_ = [{"n": 1, "name": "Opening range", "ok": None, "text": f"range {rng['low']:,.0f}-{rng['high']:,.0f}" + (f" · last 5-min close {last['close']:,.0f}" if last.get("close") else "") if rng else "waiting for 9:30"},
                  {"n": 2, "name": "VWAP (futures)", "ok": None, "text": f"VWAP {last['vwap']:,.0f}" if last.get("vwap") else "from the first 5-minute close"},
                  {"n": 3, "name": "Previous day", "ok": None, "text": f"prev day {prev['low']:,.0f}-{prev['high']:,.0f}" if prev else "no previous-day candle"},
                  {"n": 4, "name": "Option chain", "ok": None, "text": (d.get("chain") or {}).get("line") or "scored when a bias is found"}]
    return {"state": d.get("bias"), "at": d.get("bias_at"), "score": d.get("score"), "line": d.get("chain_line"), "steps": steps_, "checks": d.get("checks") or [],
            "window": {"start": ampm(c.window_start), "end": ampm(c.window_end)}}


def summary(config: AppConfig, now: Optional[datetime] = None) -> dict:
    """Everything the Options page needs: switches, today's timeline, guards, margin, cards, the open spread, the bias
    steps, the chain block, the rules, recent days, trades, alerts."""
    c = config.options
    now = now or datetime.now(IST)
    today = now.date().isoformat()
    st = _state()
    d = (st or {}).get("day") or {}
    if d and d.get("date") != today:
        d = {}
    o = (st or {}).get("open")
    eq = _equity(st) if st else float(c.capital)
    cap = st["capital"] if st else float(c.capital)
    dl = days(30)
    dloss = _daily_loss(st, c) if st else {"used": 0.0, "limit": round(c.daily_loss_pct / 100 * c.capital, 2), "hit": False}
    from backend.ingest.holidays import is_trading_day
    exp = d.get("expiry") or oi.near_expiry(today)
    sessions = d.get("sessions") if d.get("sessions") is not None else (oi.sessions_to_expiry(exp, now.date()) if exp else None)
    lot = d.get("lot_size") or oi.lot_size()
    lots_plan = min(int(c.max_lots), int(cap // c.capital_per_lot)) if c.capital_per_lot else 0
    open_pnl = _open_pnl(o)
    t = now.time()
    guards = {"kill": killed(), "enabled": bool(c.enabled), "paper": bool(c.paper), "hold": bool(d.get("hold")), "event_day": bs.is_event_day(today, c.event_days),
              "event_days": bs.event_days(c.event_days), "gap": {"pct": d.get("gap_pct"), "max": c.max_gap_pct, "ok": d.get("gap_pct") is None or abs(d["gap_pct"]) <= c.max_gap_pct},
              "daily_loss": dloss, "window": {"start": ampm(c.window_start), "end": ampm(c.window_end), "open": bs.in_window(t, c.window_start, c.window_end)},
              "trades": {"used": d.get("trades", 0), "max": c.max_trades_day}, "no_data": bool(d.get("no_data")), "trading_day": is_trading_day(now.date()),
              "vix": {"value": d.get("vix"), "half_at": c.vix_half, "half": bool(d.get("vix") and d["vix"] > c.vix_half)}}
    margin = {"per_lot": c.margin_per_lot, "lots": o["lots"] if o else lots_plan, "est": round(c.margin_per_lot * (o["lots"] if o else lots_plan), 2),
              "worst_case": o["worst_case"] if o else (round(c.hedge_points * (lot or 0) * lots_plan, 2) if lot else None), "expiry": exp, "sessions_to_expiry": sessions,
              "expiry_day": sessions == 0 if sessions is not None else None, "lot_size": lot, "capital_per_lot": c.capital_per_lot, "max_lots": c.max_lots}
    base = {"started": bool(st), "start_date": (st or {}).get("started"), "capital": cap, "cash": round((st or {}).get("cash", cap), 2), "equity": round(eq, 2),
            "net": round(eq - cap, 2), "return": round((eq - cap) / cap * 100, 2) if cap else 0.0, "mtm_today": round(eq - (d.get("day_start") or cap), 2) if st and d else 0.0,
            "realised_today": round(d.get("realised", 0.0), 2), "open_pnl": open_pnl, "charges": round((st or {}).get("charges", 0.0), 2),
            "switches": switches(config), "settings": {f: getattr(c, f) for f in c.__dataclass_fields__}, "tag": TAG,
            "today": {"date": today, "status": d.get("status") or ("waiting" if is_trading_day(now.date()) else "holiday"), "why": d.get("why"), "timeline": d.get("timeline") or [],
                      "range": d.get("range"), "prev": d.get("prev"), "open_px": d.get("open_px"), "gap_pct": d.get("gap_pct"), "vwap": d.get("vwap"), "vix": d.get("vix"),
                      "trades": d.get("trades", 0), "realised": round(d.get("realised", 0.0), 2), "hold": bool(d.get("hold")), "no_data": bool(d.get("no_data")),
                      "size_note": d.get("size_note"), "closed": d.get("closed") or [], "done": d.get("done") or [],
                      "schedule": [{"t": ampm("09:30"), "text": "range + 9:30 chain snapshot"}, {"t": f"{ampm(c.window_start)}–{ampm(c.window_end)}", "text": "bias on 5-minute closes"},
                                   {"t": ampm(c.time_exit), "text": "time exit"}, {"t": ampm("15:20"), "text": "hard exit"}, {"t": ampm("15:30"), "text": "close snapshot"},
                                   {"t": ampm("15:35"), "text": "day summary"}]},
            "guards": guards, "margin": margin, "cards": _cards(st, c, dl, eq, open_pnl), "open": _open_view(o, c), "bias": _bias_steps(d, c),
            "chain": chain_view(st, today), "rules": RULES, "days": dl, "trades": trades(50), "alerts": list(reversed((st or {}).get("alerts") or []))[:30],
            "deposits": (st or {}).get("deposits") or [], "curve": _curve(), "notes": (st or {}).get("notes") or [], "last_day": (st or {}).get("last_day"),
            "contracts": oi.status(), "updated_at": (st or {}).get("updated_at"), "now": now.isoformat(timespec="seconds")}
    return base


# --- the scheduler's clock ---------------------------------------------------------------------------------------

def start(config: AppConfig, now: Optional[datetime] = None) -> dict:
    """Make sure the book exists (the Options page, a switch, a deposit or a hold starts it); from then on it ticks."""
    with _lock:
        st = _state()
        if not st:
            st = _new(config, (now or datetime.now(IST)).date().isoformat())
            _save(st)
        return st


def active(config: AppConfig) -> bool:
    """Does the book tick: it exists, and the paper switch is on or a spread is still open (it must exit)."""
    st = _state()
    return bool(st) and (bool(config.options.paper) or bool(st.get("open")))


def due(now: datetime, config: AppConfig, done: set, trading: bool, last_tick: Optional[datetime]) -> list[str]:
    """['options_tick'] once a minute 9:15-15:40 on trading days while the book is active (pure)."""
    if not trading or not (TICK_START <= now.time() <= TICK_END) or not active(config):
        return []
    if last_tick is not None and last_tick.strftime("%Y-%m-%d %H:%M") == now.strftime("%Y-%m-%d %H:%M"):
        return []
    return ["options_tick"]

