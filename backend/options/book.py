"""🧩 The NIFTY weekly options book, paper only: a credit spread on the opening-range bias (three trades a day at
most), confirmed by the option chain, out by 15:10 at the latest. The scheduler ticks it every minute 9:15-15:40 on trading
days (`due` -> options_tick -> `tick`); `steps` says what the minute does:

  first tick       contracts: the NIFTY weekly chain from the NFO dump (once a day), the near expiry, the lot size
  9:30             range: the index 9:15-9:30 high / low, the open's gap, the previous day's levels, the 9:30 chain
                   snapshot (the OI baseline)
  9:45-11:00       bias: on each 5-minute close the range / VWAP (futures) / previous-day test and a chain fetch
                   (snapshot); a bias -> the 0-3 chain score -> size -> strikes -> the paper entry at mid -/+ Rs 1 a leg
  11:00-13:00      re-entries only (reentry_late): after a stop the same bias once more (reentry_same_bias: a 5-minute
                   close back over the range and VWAP, the chain still >= 1) or a flipped bias; first entries end at 11:00
  13:30            the afternoon pair (afternoon_sell): flat, the day's range <= 0.6% of spot, spot inside the range
                   +-0.25% -> a call spread above + a put spread below (kind "afternoon": two options_trades rows linked by
                   `pair`, one trade toward max_trades_day; premium stop / breakeven / target / time exit, no VWAP)
  every minute     while a spread is open: MTM from the quotes' mids, the stops (premium, VWAP on 5-minute closes,
                   breakeven after a 40% decay), the target, the daily loss limit; a snapshot of the legs +-3 strikes
  15:10            the time exit; 15:20 the hard exit (nothing is ever held overnight)
  15:30            the close snapshot (the next day's OI baseline)        15:35  the day summary -> options_days
Guards (no new entry): kill switch, enable switch off, hold-today, event day, gap over max_gap_pct, daily loss limit,
max_trades_day done, outside the window, no Kite login ("no data"). The Real switch cannot be turned on: no webhook yet.
Nothing here reads broker positions; Kite is used for quotes and candles only."""

import json
import threading
from datetime import date, datetime, time, timedelta
from typing import Optional

from backend.config import IST, AppConfig
from backend.database.models import get_candles, get_db, get_setting, now_ist, set_setting
from backend.logging_config.logger import setup_logger
from backend.options import bias as bs
from backend.options import candles_util
from backend.options import chain as ch
from backend.options import instruments as oi
from backend.options.bias import BEARISH, BULLISH
from backend.timefmt import ampm

logger = setup_logger("options")

_lock = threading.Lock()

TAG = "paper"
REAL_NOTE = "no webhook yet · stays off"
NOT_WIRED = "not wired"
TICK_START, TICK_END = time(9, 15), time(15, 40)
RANGE_AT, HARD_EXIT_AT, CLOSE_SNAP_AT, SUMMARY_AT = time(9, 30), time(15, 20), time(15, 30), time(15, 35)
MARKET_CLOSE = time(15, 30)
STEPS = ("contracts", "range", "bias", "mtm", "kill_exit", "time_exit", "hard_exit", "no_trade", "afternoon", "close_snap", "summary")
NEUTRAL = "NEUTRAL"                        # the afternoon pair's "bias": one spread each side
STOP_WHYS = ("premium stop", "vwap stop", "breakeven stop")      # a same-bias re-entry only after one of these
BAND_PCT = 0.25                            # the afternoon pair: spot within the opening range +- this %
KIND_WORD = {"first": "first entry", "flip": "re-entry · flipped bias", "same": "re-entry · same bias", "afternoon": "afternoon sell"}
LIVE_SNAP_EXTRA = 3                        # the live snapshot: the legs' strikes +-3
EXIT_LABEL = {"premium stop": "premium stop", "vwap stop": "VWAP stop", "breakeven stop": "breakeven stop", "target": "target",
              "time exit": "time exit", "hard exit": "hard exit", "kill": "kill switch", "closed by you": "closed by you"}
RULES = [
    "NIFTY weekly options only · paper only · always a credit spread (sell one strike, buy one hedge_points further out)",
    "Bias 9:45-11:00 on 5-minute closes: above the 9:15-9:30 range high, above the futures VWAP and over the previous day's high = BULLISH (mirror = BEARISH)",
    "Chain score 0-3 at the bias: walls, OI change since 9:30 (else the previous close), PCR · 2-3 full size · 1 half · 0 no trade",
    "Sold strike: the farther of 1% OTM and delta ≤ 0.30, at the wall when it is within 1.5%, premium ≥ ₹20 · hedge 250 points out (200 when under ₹2)",
    "Size: 1 lot per ₹1,50,000, 2 lots at most · VIX > 20 or a crowded PCR halves it",
    "Exits: sold premium 2× its entry (+100%; 1.7× = +70% on expiry day) · a 5-minute close back through VWAP (the vwap_exit switch) · breakeven after a 40% decay · target at 60% of the credit · 15:10 time exit · 15:20 hard exit",
    "No entry: event day, gap > 1%, daily loss 1.5% of capital, kill switch, hold-today, 3 trades done, outside the window",
    "Paper fills at the mid ∓ ₹1 a leg, never at the last price · charges per order like a real account · nothing is held overnight",
    "More entries: off · one re-entry on a flipped bias inside the window",
]


def exits_text(stop_pct: float, vwap: bool) -> str:
    """'exits: premium 2× · VWAP on' (the "n×" = 1 + stop_pct / 100): on the entry line, the timeline and the open footer."""
    return f"exits: premium {1 + float(stop_pct) / 100:g}× · VWAP {'on' if vwap else 'off'}"


def exits_text_afternoon(stop_pct: float, time_exit: str) -> str:
    """'exits: premium 2× · time 3:10 PM': the afternoon pair has no VWAP exit (it is non-directional)."""
    return f"exits: premium {1 + float(stop_pct) / 100:g}× · time {ampm(time_exit)}"


def late_on(c) -> bool:
    """Re-entries run past window_end (reentry_late on and reentry_until later than window_end)."""
    return bool(c.reentry_late) and bool(c.reentry_until) and bs.hm(c.reentry_until) > bs.hm(c.window_end)


def reentry_end(c) -> time:
    """The last minute a re-entry may be taken: reentry_until when late_on, else window_end (the old rule)."""
    return bs.hm(c.reentry_until) if late_on(c) else bs.hm(c.window_end)


def entries_text(c) -> str:
    """'entries: re-entry same bias · until 1:00 PM · afternoon sell 1:30 PM' (the modes that are on)."""
    bits = []
    if c.reentry_same_bias:
        bits.append("re-entry same bias")
    if late_on(c):
        bits.append(f"until {ampm(c.reentry_until)}" if bits else f"re-entry until {ampm(c.reentry_until)}")
    if c.afternoon_sell:
        bits.append(f"afternoon sell {ampm(c.afternoon_time)}")
    return "entries: " + (" · ".join(bits) if bits else "first + one re-entry on a flipped bias")


def spreads_of(o: Optional[dict]) -> list[dict]:
    """The open spreads: the afternoon pair's two sides, else the one directional spread ([] when flat)."""
    if not o:
        return []
    return list(o["sides"]) if o.get("sides") is not None else [o]          # a side of the pair is a spread itself


def rules_text(c) -> list[str]:
    """RULES with the exits line, the trades a day and the more-entries line from the saved settings."""
    out = list(RULES)
    out[6] = out[6].replace("3 trades done", f"{c.max_trades_day} trades done")
    more = []
    if c.reentry_same_bias:
        more.append("after a stop the same bias once more on a 5-minute close back over the range and VWAP (the chain still ≥ 1)")
    if late_on(c):
        more.append(f"re-entries until {ampm(c.reentry_until)} (first entries to {ampm(c.window_end)})")
    if c.afternoon_sell:
        more.append(f"{ampm(c.afternoon_time)} on a quiet day (range ≤ {c.afternoon_max_range_pct:g}%, spot inside the range ± {BAND_PCT:g}%): a call spread above + "
                    f"a put spread below, premium / breakeven / target / time exits, no VWAP")
    if more:
        out[8] = "More entries: " + " · ".join(more) + f" · all count toward {c.max_trades_day} trades a day (the pair = 1)"
    out[5] = (f"Exits: sold premium {1 + c.stop_pct / 100:g}× its entry (+{c.stop_pct:g}%; +{c.stop_pct_expiry:g}% on expiry day) · "
              + ("a 5-minute close back through VWAP" if c.vwap_exit else "VWAP exit off") + f" · breakeven after a {c.be_after_pct:g}% decay · "
              f"target at {c.target_pct:g}% of the credit · {c.time_exit} time exit · 15:20 hard exit")
    return out


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
    return {"paper": bool(c.paper), "real": False, "enabled": bool(c.enabled), "vwap_exit": bool(c.vwap_exit), "reentry_same_bias": bool(c.reentry_same_bias),
            "reentry_late": bool(c.reentry_late), "afternoon_sell": bool(c.afternoon_sell), "killed": killed(), "real_allowed": False, "real_note": REAL_NOTE}


SWITCH_KEYS = ("paper", "enabled", "vwap_exit", "reentry_same_bias", "reentry_late", "afternoon_sell")


def set_switches(config: AppConfig, **flags) -> dict:
    """paper / enabled / vwap_exit / reentry_same_bias / reentry_late / afternoon_sell; real=True is refused (ValueError
    "not wired"). Saved with the settings."""
    from backend.config import save_config
    if flags.get("real"):
        raise ValueError(NOT_WIRED)
    start(config)
    for k in SWITCH_KEYS:
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
            "skipped": [], "closed": [], "same_used": [], "afternoon": None, "kinds": []}


def reset() -> None:
    with _lock, get_db() as conn:
        conn.execute("DELETE FROM options_state")


def _tl(st: dict, now: datetime, text: str, key: Optional[str] = None) -> None:
    """A line on today's timeline (with its 12-hour time), once per key."""
    d = st["day"]
    if key and any(x.get("key") == key for x in d["timeline"]):
        return
    d["timeline"].append({"t": ampm(now), "hm": now.strftime("%H:%M"), "text": text, "key": key})


def _alert(st: dict, now: datetime, kind: str, text: str, key: Optional[str] = None, data: Optional[dict] = None) -> None:
    """An alert the API lists and the scheduler sends to Telegram (alerts/jobs.py `run_options_tick`, once per key):
    `data` carries what the message builder needs (the spread, the trade, the day row); `key` is unique for the day."""
    day = now.date().isoformat()
    k = f"{day}|{key or kind}"
    if k in st.setdefault("alerted", {}):
        return
    st["alerted"][k] = now_ist()
    st.setdefault("alerts", []).append({"at": now.isoformat(timespec="seconds"), "t": ampm(now), "day": day, "kind": kind, "text": text, "tag": TAG,
                                        "key": key or kind, "data": data})
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


def _value(o: dict) -> float:
    return o.get("value") if o.get("value") is not None else o["credit"]


def _open_pnl(o: Optional[dict]) -> float:
    """The open MTM (the afternoon pair: both sides)."""
    if not o:
        return 0.0
    return round(sum((x["credit"] - _value(x)) * _qty(x) - x["charges_in"] for x in spreads_of(o)), 2)


def _equity(st: dict) -> float:
    """Cash less what it costs to close the open spread(s) now (the credit is already in the cash)."""
    return st["cash"] - sum(_value(x) * _qty(x) for x in spreads_of(st.get("open")))


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
    return candles_util.from_kite(rows).get(day, [])


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
    late = late_on(c) and bs.hm(c.window_end) < bs.hm(slot) <= reentry_end(c) and d["trades"] >= 1 and d["status"] == "closed" \
        and "afternoon" not in (d.get("kinds") or [])            # re-entries only, after a closed directional trade
    if ("range" in done or "range" in out) and (bs.in_window(bs.hm(slot), c.window_start, c.window_end) or late) and slot not in d["checked"] \
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
    if c.afternoon_sell and bs.hm(c.afternoon_time) <= t < bs.hm(c.time_exit) and "afternoon" not in done and "range" in done and not d["no_data"] \
            and not is_killed:
        out.append("afternoon")                                   # once a day, the first minute at / after afternoon_time
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
        if st["day"]["status"] == "closed" and now.time() > reentry_end(config.options):
            st["day"]["status"] = "done"                          # the (re-entry) window shut: no re-entry possible any more
        _save(st)
        return {"ran": ran, "status": st["day"]["status"], "open": bool(st.get("open")), "day": today}


def _no_data(st: dict, now: datetime, why: str) -> None:
    d = st["day"]
    d["no_data"], d["status"], d["why"] = True, "no_data", f"no data · {why}"
    _tl(st, now, f"no data · {why}", key="no_data")
    _alert(st, now, "no_data", f"no data · {why}", key="no_data", data={"why": why})


def _no_trade_data(d: dict, config: AppConfig, now: datetime) -> dict:
    """What the 11:00 "no trade today" message says: the range, where VWAP sits against it, the next look."""
    from backend.ingest.holidays import is_trading_day
    rng, vw = d.get("range") or {}, d.get("vwap")
    where = None
    if rng and vw:
        where = "in the middle" if rng["low"] <= vw <= rng["high"] else ("over the range" if vw > rng["high"] else "under the range")
    nxt = now.date() + timedelta(days=1)
    for _ in range(10):
        if is_trading_day(nxt):
            break
        nxt += timedelta(days=1)
    c = config.options
    return {"range": rng or None, "vwap": vw, "vwap_where": where, "window_end": c.window_end, "window_start": c.window_start,
            "next": nxt.isoformat(), "checks": len(d.get("checked") or []), "afternoon": c.afternoon_time if c.afternoon_sell else None}


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
        same = _same_bias_candidate(d, c, res, slot)
        if same and res["bias"] in (None, same):                  # the same bias again: range + VWAP only (the previous day not needed)
            res = {**res, "bias": same, "same": True}
        d["vwap"] = vw
        d["checks"].append({"t": slot, "close": bar["c"], "vwap": vw, "bias": res["bias"], **res["checks"], **({"same": True} if res.get("same") else {})})
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
        if o.get("kind") == "afternoon":                         # each side of the pair exits on its own; no VWAP exit
            for side, why in _mark_pair(st, config, client, now):
                _exit_spread(st, config, client, now, side, why)
            why = None
        else:
            why = _mark(st, config, client, now)
        if why:
            _exit(st, config, client, now, why)
        elif st.get("open") is not None and _daily_loss(st, config.options)["hit"]:
            dl = _daily_loss(st, config.options)
            _alert(st, now, "daily_loss", f"daily loss limit reached · {dl['used']:,.0f} of {dl['limit']:,.0f} · no new entries today · "
                   "the open spread keeps its stops", key="daily_loss", data=dl)

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
        c = config.options
        _tl(st, now, "no trade today · no bias by " + ampm(c.window_end) + (f" · afternoon sell looks at {ampm(c.afternoon_time)}" if c.afternoon_sell else ""), key="no_trade")
        _alert(st, now, "no_trade", f"no trade today · no bias by {ampm(config.options.window_end)}", key="no_trade", data=_no_trade_data(d, config, now))

    @staticmethod
    def afternoon(st, config, client, now):
        st["day"]["done"].append("afternoon")
        _afternoon(st, config, client, now)

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

def _same_bias_candidate(d: dict, c, res: dict, slot: str) -> Optional[str]:
    """reentry_same_bias: the bias to re-enter on the same side, or None. Only after a directional trade stopped out
    (premium / VWAP / breakeven), once per bias a day, on a LATER 5-minute close back over the range and the VWAP
    (bullish: close > range high and > VWAP; bearish: the mirror)."""
    if not c.reentry_same_bias or not d.get("biases") or not d.get("closed"):
        return None
    last, lastx = d["biases"][-1], d["closed"][-1]
    if lastx.get("kind") == "afternoon" or lastx["why"] not in STOP_WHYS or last in (d.get("same_used") or []):
        return None
    if lastx.get("hm") and slot <= lastx["hm"]:
        return None
    sign = 1 if last == BULLISH else -1
    return last if res["checks"]["range"] == sign and res["checks"]["vwap"] == sign else None


def _guard_reasons(st: dict, config: AppConfig, now: datetime, bias: str, same: bool = False) -> list[str]:
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
    if d["trades"] >= 1 and d["biases"] and d["biases"][-1] == bias and not same:
        lastx = (d.get("closed") or [{}])[-1]
        if not c.reentry_same_bias:
            out.append("re-entry only on a flipped bias")
        elif bias in (d.get("same_used") or []):
            out.append("same-bias re-entry used")
        elif lastx.get("why") and lastx["why"] not in STOP_WHYS:
            out.append(f"same bias · the last exit was a {EXIT_LABEL.get(lastx['why'], lastx['why'])}, not a stop")
        else:
            out.append("re-entry only on a flipped bias")
    if "afternoon" in (d.get("kinds") or []):
        out.append("the afternoon pair is done")
    t = now.time()
    if d["trades"] == 0 and t > bs.hm(c.window_end):
        out.append(f"first entries end at {ampm(c.window_end)}")
    elif d["trades"] >= 1 and t > reentry_end(c):
        out.append(f"re-entries end at {ampm(reentry_end(c).strftime('%H:%M'))}")
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
    same = bool(res.get("same"))
    if same:
        steps_[2] = {**steps_[2], "ok": res["checks"]["prev"] == (1 if bias == bs.BULLISH else -1),
                     "text": steps_[2]["text"] + " · not needed for a same-bias re-entry"}
    if bias != d.get("bias"):
        _tl(st, now, f"{bias} · close {bar['c']:,.0f} · VWAP {vw:,.0f}" if vw else f"{bias} · close {bar['c']:,.0f}", key=f"bias|{bias}|{slot}")
    if bias != d.get("bias") or not d["biases"] or d["biases"][-1] != bias or (same and not st.get("open")):   # the record freezes once this bias was traded
        d["bias"], d["bias_at"], d["steps"] = bias, ampm(now), steps_
        d["score"], d["chain_line"] = (sc["score"] if sc else None), (sc["line"] if sc else None)
    reasons = _guard_reasons(st, config, now, bias, same)
    if reasons:
        if "a spread is open" not in reasons:
            key = "skip|" + "|".join(reasons)
            if key not in d["skipped"]:
                d["skipped"].append(key)
                _tl(st, now, f"{bias} · no entry · {' · '.join(reasons)}", key=key)
                loud = [r for r in reasons if not r.startswith(("re-entry only", f"{c.max_trades_day} trades", "same-bias", "same bias", "the afternoon pair",
                                                                "first entries end", "re-entries end"))]
                if loud:                                           # the day's own limits are not news; the guards are
                    kind = "event_day" if "event day" in reasons else "skipped"
                    _alert(st, now, kind, f"{bias} found · no entry · {' · '.join(reasons)}", key=key, data={"bias": bias, "reasons": reasons})
        return
    if not q or not sc:
        _tl(st, now, f"{bias} · chain not available · no entry", key=f"nochain|{slot}")
        return
    if sc["score"] == 0:
        _tl(st, now, f"{bias} · chain disagrees ({sc['line']})", key=f"disagree|{bias}")
        _alert(st, now, "chain_disagrees", f"bias found · chain disagrees · {bias} · {sc['line']}", key=f"disagree|{bias}", data={"bias": bias, "line": sc["line"], "score": 0})
        return
    lots, notes = lots_for(c, st["capital"], q.get("vix"), sc["score"], sc["crowded"])
    d["size_note"] = " · ".join(notes) if notes else None
    for n in notes:
        if n.startswith("VIX"):
            _alert(st, now, "vix_half", n, key="vix_half", data={"vix": q.get("vix"), "half_at": c.vix_half, "lots": lots})
    if lots < 1:
        _tl(st, now, f"{bias} · no trade · " + (" · ".join(notes) or "0 lots"), key=f"nolots|{bias}")
        _alert(st, now, "no_trade_size", f"{bias} · no trade · " + (" · ".join(notes) or "0 lots"), key=f"nolots|{bias}", data={"bias": bias, "notes": notes})
        return
    sel = ch.select_strikes(bias, q["rows"], q["spot"], sc["walls"], c.otm_pct, c.max_delta, c.min_premium, c.hedge_points)
    if not sel["ok"]:
        _tl(st, now, f"{bias} · no trade · {sel['why']}", key=f"nostrike|{bias}")
        _alert(st, now, "no_trade_strikes", f"{bias} · no trade · {sel['why']}", key=f"nostrike|{bias}", data={"bias": bias, "why": sel["why"]})
        return
    kind = "first" if d["trades"] == 0 else ("same" if same else "flip")
    why = None
    if kind == "same":
        last = d["closed"][-1]
        side = "over" if bias == bs.BULLISH else "under"
        why = (f"after the {EXIT_LABEL.get(last['why'], last['why'])} at {last['t']}: close {bar['c']:,.0f} back {side} the range "
               f"{'high' if bias == bs.BULLISH else 'low'} {(d['range']['high'] if bias == bs.BULLISH else d['range']['low']):,.0f}" + (f" and VWAP {vw:,.0f}" if vw else ""))
    _enter(st, config, now, bias, q, sc, sel, lots, kind=kind, why=why)


def _spread(c, d: dict, now: datetime, bias: str, q: dict, sel: dict, lots: int, vwap_exit: bool) -> tuple[dict, float]:
    """One paper credit spread from a strike selection: sell at mid - 1, buy the hedge at mid + 1. (spread, entry charges)."""
    lot = int(d["lot_size"] or 0)
    qty = lots * lot
    sold_px = round(sel["sold"]["mid"] - 1.0, 2)
    hedge_px = round(sel["hedge"]["mid"] + 1.0, 2)
    credit = round(sold_px - hedge_px, 2)
    ch_in = charges(sold_px * qty, sell=True) + charges(hedge_px * qty, sell=False)
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
         "value": credit, "pnl": round(-ch_in, 2), "spot": q["spot"], "vwap_checked": [], "n": d["trades"] + 1, "vwap_exit": bool(vwap_exit),
         "exits": exits_text(stop_pct, vwap_exit)}
    return o, ch_in


def _book_in(st: dict, o: dict, ch_in: float) -> None:
    """The credit into the cash, the entry charges out."""
    st["cash"] += o["credit"] * o["qty"] - ch_in
    st["charges"] += ch_in
    st["day"]["charges"] += ch_in


def _enter(st: dict, config: AppConfig, now: datetime, bias: str, q: dict, sc: dict, sel: dict, lots: int, kind: str = "first",
           why: Optional[str] = None) -> dict:
    """The paper entry: sell the sold leg at mid - 1, buy the hedge at mid + 1; charges per order; the state's open spread.
    `kind` first / flip (a re-entry on the flipped bias) / same (a same-bias re-entry after a stop; `why` says what brought it)."""
    c, d = config.options, st["day"]
    o, ch_in = _spread(c, d, now, bias, q, sel, lots, c.vwap_exit)
    _book_in(st, o, ch_in)
    late = now.time() > bs.hm(c.window_end)
    o.update({"score": sc["score"], "chain_line": sc["line"], "kind": kind, "entry_why": why, "late": late, "max_trades": c.max_trades_day})
    st["open"] = o
    d["biases"].append(bias)
    if kind == "same":
        d.setdefault("same_used", []).append(bias)
    d.setdefault("kinds", []).append(kind)
    d["trades"] += 1
    d["status"] = "open"
    s, h, qty, credit = o["sold"], o["hedge"], o["qty"], o["credit"]
    head = {"first": "", "flip": "re-entry · flipped bias · ", "same": f"re-entry · same bias · {why} · " if why else "re-entry · same bias · "}[kind]
    if late:
        head += f"after {ampm(c.window_end)} (re-entries to {ampm(c.reentry_until)}) · "
    _tl(st, now, f"{head}sold {s['strike']:,.0f} {s['type']} @ {s['entry']:.2f} · bought {h['strike']:,.0f} {h['type']} @ {h['entry']:.2f} · "
        f"credit {credit:.2f} x {qty} = ₹{credit * qty:,.0f} · {lots} lot{'s' if lots != 1 else ''} · stop {o['stop_premium']:.2f} · target {o['target_value']:.2f} · {o['exits']}",
        key=f"entry|{o['n']}")
    _alert(st, now, "entry", f"{bias} · {KIND_WORD[kind] + ' · ' if kind != 'first' else ''}sold {s['strike']:,.0f} {s['type']} / bought {h['strike']:,.0f} {h['type']} · "
           f"credit ₹{credit * qty:,.0f} · stop {o['stop_premium']:.2f} · target {o['target_value']:.2f} · worst case ₹{o['worst_case']:,.0f} · {sc['line']}", key=f"entry|{o['n']}",
           data={**o, "range": d.get("range"), "prev": d.get("prev"), "vix": d.get("vix"), "close": (d.get("checks") or [{}])[-1].get("close") if d.get("checks") else None,
                 "window": {"start": c.window_start, "end": c.window_end, "reentry_until": c.reentry_until if late_on(c) else None}, "target_pct": c.target_pct})
    return o


# --- the afternoon pair: a call spread above + a put spread below on a quiet day ------------------------------------

def _afternoon_guards(st: dict, config: AppConfig, now: datetime) -> list[str]:
    c, d = config.options, st["day"]
    out = []
    if killed():
        out.append("kill switch")
    if not c.enabled:
        out.append("switched off")
    if not c.paper:
        out.append("paper off")
    if d.get("hold"):
        out.append("hold today")
    if bs.is_event_day(now.date().isoformat(), c.event_days):
        out.append("event day")
    if d.get("gap_pct") is not None and abs(d["gap_pct"]) > c.max_gap_pct:
        out.append(f"gap {d['gap_pct']:+.2f}% > {c.max_gap_pct:g}%")
    if _daily_loss(st, c)["hit"]:
        out.append("daily loss limit")
    if d["trades"] >= c.max_trades_day:
        out.append(f"{c.max_trades_day} trades done")
    if st.get("open"):
        out.append("a spread is open")
    return out


def _afternoon(st: dict, config: AppConfig, client, now: datetime) -> Optional[dict]:
    """At afternoon_time (once a day): flat, the day's range so far (the 5-minute bars since 9:15) at most
    afternoon_max_range_pct % of spot, spot inside the opening range +- BAND_PCT % -> sell a call spread above and a
    put spread below (the usual strike rules per side, 1 lot each side at the default capital, VIX halves as today)."""
    c, d = config.options, st["day"]
    today, slot = now.date().isoformat(), bs.slot_of(now.time())
    look = {"at": ampm(now), "ok": False, "why": None, "range_pct": None, "spot": None, "day_high": None, "day_low": None,
            "band": [round(d["range"]["low"] * (1 - BAND_PCT / 100), 2), round(d["range"]["high"] * (1 + BAND_PCT / 100), 2)] if d.get("range") else None,
            "max_pct": c.afternoon_max_range_pct}
    d["afternoon"] = look

    def skip(why: str, loud: bool = False) -> None:
        look["why"] = why
        _tl(st, now, f"afternoon sell · no entry · {why}", key="afternoon")
        if loud:
            _alert(st, now, "event_day" if "event day" in why else "skipped", f"afternoon sell · no entry · {why}", key="afternoon_skip",
                   data={"bias": "afternoon sell", "reasons": why.split(" · ")})
    reasons = _afternoon_guards(st, config, now)
    if reasons:
        loud = [r for r in reasons if not r.startswith((f"{c.max_trades_day} trades", "a spread is open"))]
        return skip(" · ".join(reasons), loud=bool(loud))
    if not _has_token(client) or not d.get("range"):
        return skip("no data")
    try:
        idx = _bars(client, oi.INDEX_TOKEN, today)
    except Exception as exc:
        logger.warning("Options index bars failed: %s", exc)
        return skip("no index candles")
    dr = bs.day_range(idx, until=slot)
    if not dr or not idx:
        return skip("no index candles")
    spot = idx[-1]["c"]
    pct = round((dr["high"] - dr["low"]) / spot * 100, 2)
    look.update({"range_pct": pct, "spot": spot, "day_high": dr["high"], "day_low": dr["low"]})
    bad = []
    if pct > c.afternoon_max_range_pct:
        bad.append(f"day range {pct:.2f}% > {c.afternoon_max_range_pct:g}%")
    if not bs.in_band(spot, d["range"], BAND_PCT):
        bad.append(f"spot {spot:,.0f} outside {look['band'][0]:,.0f}–{look['band'][1]:,.0f}")
    if bad:
        return skip(" · ".join(bad))
    q = _fetch_chain(client, st, now, spot, with_vix=True)
    if not q:
        return skip("chain not available")
    ch.snapshot(today, "bias", d["expiry"], q["rows"], q["spot"], now.isoformat(timespec="seconds"))
    d["vix"] = q.get("vix") or d.get("vix")
    w = ch.walls(q["rows"], q["spot"])
    base, notes = lots_for(c, st["capital"], q.get("vix"), 2, False)
    lots = max(1, base // 2) if base else 0                    # two spreads: half the directional size each side (1 lot at least)
    if lots < 1:
        return skip("0 lots")
    for n_ in notes:
        if n_.startswith("VIX"):
            _alert(st, now, "vix_half", n_, key="vix_half", data={"vix": q.get("vix"), "half_at": c.vix_half, "lots": lots})
    ce = ch.select_strikes(BEARISH, q["rows"], q["spot"], w, c.otm_pct, c.max_delta, c.min_premium, c.hedge_points)
    pe = ch.select_strikes(BULLISH, q["rows"], q["spot"], w, c.otm_pct, c.max_delta, c.min_premium, c.hedge_points)
    if not ce["ok"] or not pe["ok"]:
        why = " · ".join(x["why"] for x in (ce, pe) if not x["ok"])
        _alert(st, now, "no_trade_strikes", f"afternoon sell · no trade · {why}", key="afternoon_nostrike", data={"bias": "afternoon sell", "why": why})
        return skip(why)
    look["ok"] = True
    d["size_note"] = " · ".join(notes) if notes else d.get("size_note")
    return _enter_pair(st, config, now, q, ce, pe, lots, pct)


def _enter_pair(st: dict, config: AppConfig, now: datetime, q: dict, ce_sel: dict, pe_sel: dict, lots: int, range_pct: float) -> dict:
    """The afternoon pair: two paper credit spreads (CE above, PE below) in one container st['open'] = {kind: afternoon,
    pair, sides: [CE spread, PE spread], ...}; each side exits on its own; one trade toward max_trades_day."""
    c, d = config.options, st["day"]
    n = d["trades"] + 1
    pair = f"{d['date']}|{n}"
    sides = []
    for sel in (ce_sel, pe_sel):
        o, ch_in = _spread(c, d, now, NEUTRAL, q, sel, lots, False)
        _book_in(st, o, ch_in)
        o.update({"kind": "afternoon", "pair": pair, "side": o["sold"]["type"], "score": None, "chain_line": None,
                  "exits": exits_text_afternoon(o["stop_pct"], c.time_exit)})
        sides.append(o)
    ce, pe = sides
    cont = {"kind": "afternoon", "pair": pair, "n": n, "bias": NEUTRAL, "sides": sides, "expiry": d["expiry"], "sessions": d.get("sessions"),
            "expiry_day": ce["expiry_day"], "lots": lots, "lot_size": ce["lot_size"], "qty": ce["qty"], "opened_at": ce["opened_at"], "opened_t": ce["opened_t"],
            "range_pct": range_pct, "spot": q["spot"], "spot_at_entry": q["spot"], "credit_rs": round(ce["credit_rs"] + pe["credit_rs"], 2),
            "worst_case": max(ce["worst_case"], pe["worst_case"]), "margin_est": round(ce["margin_est"] + pe["margin_est"], 2),
            "exits": ce["exits"], "stop_pct": ce["stop_pct"]}
    st["open"] = cont
    d.setdefault("kinds", []).append("afternoon")
    d["trades"] += 1
    d["status"] = "open"
    legs = lambda o: f"{o['sold']['strike']:,.0f} {o['sold']['type']} @ {o['sold']['entry']:.2f} / {o['hedge']['strike']:,.0f} @ {o['hedge']['entry']:.2f}"
    _tl(st, now, f"afternoon sell · day range {range_pct:.2f}% · sold {legs(ce)} · sold {legs(pe)} · credit ₹{cont['credit_rs']:,.0f} · "
        f"{lots} lot{'s' if lots != 1 else ''} each side · stops {ce['stop_premium']:.2f} / {pe['stop_premium']:.2f} · {cont['exits']}", key=f"entry|{n}")
    _alert(st, now, "afternoon", f"afternoon sell · {ce['sold']['strike']:,.0f} CE / {pe['sold']['strike']:,.0f} PE · credit ₹{cont['credit_rs']:,.0f} · "
           f"range {range_pct:.1f}% · {cont['exits']}", key=f"entry|{n}",
           data={**{k: v for k, v in cont.items() if k != "sides"}, "sides": sides, "max_trades": c.max_trades_day, "time_exit": c.time_exit,
                 "range": d.get("range"), "vix": d.get("vix"), "max_pct": c.afternoon_max_range_pct})
    return cont


# --- the open spread: MTM, stops, exits -----------------------------------------------------------------------------

def _leg_quotes(client, st: dict, now: datetime, o: Optional[dict] = None) -> Optional[dict]:
    """The legs' quotes (+-3 strikes of the sold type) in one call; snapshot 'live'. {tradingsymbol: row, spot}. `o` is
    one spread or the afternoon pair (both sides in the one call); the open position by default."""
    o = o or st["open"]
    strikes, types = set(), set()
    for x in spreads_of(o):
        lo, hi = min(x["sold"]["strike"], x["hedge"]["strike"]), max(x["sold"]["strike"], x["hedge"]["strike"])
        strikes |= {lo + i * ch.STRIKE_STEP for i in range(-LIVE_SNAP_EXTRA, int((hi - lo) / ch.STRIKE_STEP) + LIVE_SNAP_EXTRA + 1)}
        types.add(x["sold"]["type"])
    res = _fetch_chain(client, st, now, o.get("spot"), strikes=sorted(strikes))
    if not res:
        return None
    rows = [r for r in res["rows"] if r["type"] in types]
    ch.snapshot(now.date().isoformat(), "live", o["expiry"], rows, res["spot"], now.isoformat(timespec="seconds"))
    return {"rows": {r["tradingsymbol"]: r for r in rows}, "spot": res["spot"]}


def _mark_spread(st: dict, c, o: dict, q: dict, now: datetime) -> tuple[bool, Optional[str]]:
    """MTM one spread from the quotes' mids: (marked, the price exit that fires: premium stop / breakeven stop / target)."""
    s, h = q["rows"].get(o["sold"]["tradingsymbol"]), q["rows"].get(o["hedge"]["tradingsymbol"])
    if not s or not h or not s.get("mid") or h.get("mid") is None:
        return False, None
    o["sold"]["last"], o["hedge"]["last"] = s["mid"], h["mid"]
    o["sold"]["iv"], o["sold"]["delta"], o["hedge"]["iv"], o["hedge"]["delta"] = s["iv"], s["delta"], h["iv"], h["delta"]
    o["value"] = round(s["mid"] - h["mid"], 2)
    o["spot"] = q["spot"]
    o["pnl"] = _open_pnl(o)
    o["marked_at"] = now.isoformat(timespec="seconds")
    pair = o.get("kind") == "afternoon"
    if s["mid"] <= o["be_at"] and not o["be_active"]:
        o["be_active"], o["be_t"] = True, ampm(now)
        _tl(st, now, f"{'afternoon ' + o['side'] + ' · ' if pair else ''}sold premium {s['mid']:.2f} · decayed {c.be_after_pct:g}% · stop now breakeven",
            key=f"be|{o['n']}" + (f"|{o['side']}" if pair else ""))
    if s["mid"] >= o["stop_premium"]:
        return True, "premium stop"
    if o["be_active"] and o["value"] >= o["credit"]:
        return True, "breakeven stop"
    if o["value"] <= o["target_value"]:
        return True, "target"
    return True, None


def _mark_pair(st: dict, config: AppConfig, client, now: datetime) -> list[tuple[dict, str]]:
    """MTM the afternoon pair (one quote call for both sides): [(side, exit reason)] for the sides whose exit fired."""
    cont = st["open"]
    q = _leg_quotes(client, st, now, cont) if _has_token(client) else None
    if not q:
        return []
    out = []
    for side in cont["sides"]:
        marked, why = _mark_spread(st, config.options, side, q, now)
        if why:
            out.append((side, why))
    cont["spot"], cont["marked_at"] = q["spot"], now.isoformat(timespec="seconds")
    return out


def _mark(st: dict, config: AppConfig, client, now: datetime) -> Optional[str]:
    """MTM the open directional spread from the quotes' mids and return the exit reason when one fires (None otherwise)."""
    o, c = st["open"], config.options
    q = _leg_quotes(client, st, now) if _has_token(client) else None
    if not q:
        return None
    marked, why = _mark_spread(st, c, o, q, now)
    if not marked or why:
        return why
    t = now.time()
    slot = bs.slot_of(t)
    if c.vwap_exit and bs.is_5m_close(t) and slot not in o["vwap_checked"] and t < bs.hm(c.time_exit):     # the vwap_exit switch
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
    overnight). The afternoon pair: every side still open. Returns the (first) trade row or None."""
    o = st.get("open")
    if not o:
        return None
    rows = [_exit_spread(st, config, client, now, x, why, force) for x in spreads_of(o)]
    return next((r for r in rows if r), None)


def _exit_spread(st: dict, config: AppConfig, client, now: datetime, o: dict, why: str, force: bool = False) -> Optional[dict]:
    """Close one spread (the directional one, or one side of the afternoon pair): a row in options_trades. The pair's
    sides share `n` and `pair`; the position goes flat once no side is left."""
    d = st["day"]
    pair = o.get("kind") == "afternoon"
    sfx = f"|{o['side']}" if pair else ""
    q = _leg_quotes(client, st, now, o) if _has_token(client) else None
    s = q["rows"].get(o["sold"]["tradingsymbol"]) if q else None
    h = q["rows"].get(o["hedge"]["tradingsymbol"]) if q else None
    est = not (s and h and s.get("mid") and h.get("mid") is not None)
    if est and not force:
        _tl(st, now, f"{EXIT_LABEL.get(why, why)} · no quote · retrying", key=f"noquote|{why}|{now.strftime('%H:%M')}{sfx}")
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
         "label": EXIT_LABEL.get(why, why), "est": est, "score": o["score"], "chain_line": o["chain_line"], "be_active": o["be_active"], "be_t": o.get("be_t"),
         "spot_in": o.get("spot_at_entry"), "spot_out": q["spot"] if q else o.get("spot"), "book": TAG, "kind": o.get("kind") or "first",
         "entry_why": o.get("entry_why"), "pair": o.get("pair"), "side": o.get("side") if pair else None, "vwap_exit": bool(o.get("vwap_exit", True))}
    with get_db() as conn:
        conn.execute("INSERT INTO options_trades (day, payload) VALUES (?, ?)", (d["date"], json.dumps(t)))
    d["realised"] = round(d["realised"] + net, 2)
    d["closed"].append({"n": o["n"], "why": why, "net": net, "t": ampm(now), "hm": now.strftime("%H:%M"), "kind": o.get("kind") or "first",
                        **({"side": o["side"]} if pair else {})})
    if pair:
        cont = st["open"]
        cont["sides"] = [x for x in cont["sides"] if x is not o and x["sold"]["tradingsymbol"] != o["sold"]["tradingsymbol"]]
        if not cont["sides"]:
            st["open"] = None
    else:
        st["open"] = None
    c = config.options
    if st["open"] is None:
        d["status"] = "done" if d["trades"] >= c.max_trades_day or now.time() > reentry_end(c) or pair else "closed"
    label = EXIT_LABEL.get(why, why)
    d["why"] = f"afternoon {o['side']} · {label}" if pair else label
    who = f"afternoon {o['side']} · " if pair else ""
    _tl(st, now, f"{who}{label} · bought back {o['sold']['strike']:,.0f} {o['sold']['type']} @ {sold_px:.2f} · sold hedge @ {hedge_px:.2f} · "
        f"net {net:+,.0f}" + (" · at the last known mid" if est else ""), key=f"exit|{o['n']}{sfx}")
    _alert(st, now, "exit", f"{who}{label} · {o['bias'] if not pair else 'afternoon'} {o['sold']['strike']:,.0f} {o['sold']['type']} spread · net ₹{net:+,.0f}"
           + (" · closed at the last known mid (no quote)" if est else ""), key=f"exit|{o['n']}{sfx}", data=t)
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
    mate = next((t for t in trades[1:] if first and first.get("pair") and t.get("pair") == first["pair"]), None)
    row = {"day": d["date"], "status": d["status"], "why": d["why"], "range": d["range"], "gap_pct": d["gap_pct"], "prev": d["prev"], "vwap": d["vwap"],
           "vix": d["vix"], "bias": d["bias"], "bias_at": d["bias_at"], "score": d["score"], "chain_line": d["chain_line"], "expiry": d["expiry"],
           "sessions": d["sessions"], "trades": d["trades"], "pnl": round(d["realised"], 2), "charges": round(d["charges"], 2), "equity": eq,
           "mtm": round(eq - (d.get("day_start") or eq), 2), "size_note": d["size_note"],
           "strikes": (f"{first['sold']['strike']:,.0f} {first['sold']['type']} / {mate['sold']['strike']:,.0f} {mate['sold']['type']}" if mate else
                       f"{first['sold']['strike']:,.0f} {first['sold']['type']} / {first['hedge']['strike']:,.0f} {first['hedge']['type']}" if first else None),
           "credit": round(first["credit"] + (mate["credit"] if mate else 0.0), 2) if first else None, "lots": first["lots"] if first else None,
           "result": _result(trades) if trades else d["why"], "notes": d["timeline"], "closed": d["closed"], "no_data": d["no_data"],
           "kinds": d.get("kinds") or [], "rows": len(trades), "afternoon": d.get("afternoon"), "entries": entries_text(config.options)}
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO options_days (day, payload) VALUES (?, ?)", (d["date"], json.dumps(row)))
        conn.execute("INSERT OR REPLACE INTO options_equity (day, equity, gross, nifty) VALUES (?, ?, ?, ?)",
                     (d["date"], eq, round(eq + st["charges"], 2), (d.get("chain") or {}).get("spot")))
    st["last_day"] = row
    _tl(st, now, f"day summary · {row['result']} · P&L {row['pnl']:+,.0f}", key="summary")
    dl = days(10000)
    traded = [x for x in dl if x.get("trades")]
    _alert(st, now, "summary", f"day summary · {row['result']} · P&L ₹{row['pnl']:+,.0f} · equity ₹{eq:,.0f}", key="summary",
           data={"row": row, "book": {"equity": eq, "capital": st["capital"], "started": st.get("started"), "days": len(dl), "traded": len(traded),
                                      "won": sum(1 for x in traded if (x.get("pnl") or 0) > 0), "return": round((eq - st["capital"]) / st["capital"] * 100, 2) if st["capital"] else 0.0}})
    return row


def _result(trades: list[dict]) -> str:
    """The day's result line: each trade's exit label; the afternoon pair as one ('afternoon CE target / PE time exit');
    a re-entry marked ('re-entry premium stop')."""
    out, seen = [], set()
    for t in trades:
        if t.get("pair"):
            if t["pair"] in seen:
                continue
            seen.add(t["pair"])
            sides = [x for x in trades if x.get("pair") == t["pair"]]
            out.append("afternoon " + " / ".join(f"{x['sold']['type']} {x['label']}" for x in sides))
        else:
            out.append(("re-entry " if t.get("kind") in ("flip", "same") else "") + t["label"])
    return out[0] if len(out) == 1 else " · ".join(out)


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


def _ai_why(rows: list) -> list:
    """🤖 why: the AI's "likely cause" line on the closed spreads (ai_why: {id, text, at} | None)."""
    from backend.ai import why
    return why.attach(rows, "options")


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


def _pair_view(o: dict, c) -> dict:
    """The afternoon pair as the page shows it: four legs (each sold leg with its own stop), the sums, the side nearest
    its stop for the one stop / near-stop fields the Home rows and the status reply read."""
    sides = o["sides"]
    legs = []
    for x in sides:
        for k in ("sold", "hedge"):
            leg = x[k]
            legs.append({"leg": k, "side": leg["side"], "strike": leg["strike"], "type": leg["type"], "tradingsymbol": leg["tradingsymbol"], "entry": leg["entry"],
                         "last": leg["last"], "iv": round(leg["iv"] * 100, 1) if leg.get("iv") else None, "delta": leg.get("delta"), "qty": x["qty"], "spread": x["side"],
                         "stop": x["stop_premium"] if k == "sold" else None, "near_stop": leg["last"] >= x["stop_premium"] * 0.9 if k == "sold" else None,
                         "be_active": x["be_active"] if k == "sold" else None,
                         "pnl": round((leg["entry"] - leg["last"]) * x["qty"] if k == "sold" else (leg["last"] - leg["entry"]) * x["qty"], 2)})
    worst = max(sides, key=lambda x: x["sold"]["last"] / x["stop_premium"] if x["stop_premium"] else 0)
    credit = round(sum(x["credit"] for x in sides), 2)
    value = round(sum(_value(x) for x in sides), 2)
    target = round(sum(x["target_value"] for x in sides), 2)
    names = " / ".join(f"{x['sold']['strike']:,.0f} {x['side']}" for x in sides)
    stops = " / ".join(f"{x['stop_premium']:.2f}" for x in sides)
    return {"kind": "afternoon", "pair": o["pair"], "bias": NEUTRAL, "expiry": o["expiry"], "sessions": o.get("sessions"), "expiry_day": o.get("expiry_day"),
            "lots": o["lots"], "lot_size": o["lot_size"], "qty": o["qty"], "credit": credit, "credit_rs": round(sum(x["credit_rs"] for x in sides), 2),
            "width": max(x["width"] for x in sides), "stop_pct": worst["stop_pct"], "stop_premium": worst["stop_premium"], "be_at": worst["be_at"],
            "be_active": any(x["be_active"] for x in sides), "target_value": target, "worst_case": max(x["worst_case"] for x in sides),
            "margin_est": round(sum(x["margin_est"] for x in sides), 2), "charges_in": round(sum(x["charges_in"] for x in sides), 2), "opened_at": o["opened_at"],
            "opened_t": o["opened_t"], "score": None, "chain_line": None, "n": o["n"], "legs": legs, "value": value, "value_rs": round(sum(_value(x) * x["qty"] for x in sides), 2),
            "pnl": _open_pnl(o), "spot": o.get("spot"), "vwap": None, "marked_at": o.get("marked_at"), "near_stop": any(leg["near_stop"] for leg in legs if leg["leg"] == "sold"),
            "decay_pct": round((1 - worst["sold"]["last"] / worst["sold"]["entry"]) * 100, 1) if worst["sold"]["entry"] else None, "range_pct": o.get("range_pct"),
            "sides": [{"side": x["side"], "credit": x["credit"], "value": _value(x), "stop_premium": x["stop_premium"], "be_active": x["be_active"],
                       "target_value": x["target_value"], "pnl": _open_pnl(x)} for x in sides],
            "footer": f"afternoon pair {names} · credit ₹{sum(x['credit_rs'] for x in sides):,.0f} · value ₹{sum(_value(x) * x['qty'] for x in sides):,.0f} · "
                      f"stops {stops} ({worst['stop_pct']:g}%) · day range {o.get('range_pct') or 0:.2f}% · "
                      f"{exits_text_afternoon(worst['stop_pct'], c.time_exit)}"}


def _open_view(o: Optional[dict], c) -> Optional[dict]:
    if not o:
        return None
    if o.get("kind") == "afternoon":
        return _pair_view(o, c)
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
            "kind": o.get("kind") or "first", "entry_why": o.get("entry_why"),
            "legs": legs, "value": value, "value_rs": round(value * o["qty"], 2), "pnl": _open_pnl(o), "spot": o.get("spot"), "vwap": o.get("vwap") or o.get("vwap_at_entry"),
            "marked_at": o.get("marked_at"), "near_stop": near_stop, "decay_pct": round((1 - o["sold"]["last"] / o["sold"]["entry"]) * 100, 1) if o["sold"]["entry"] else None,
            "footer": f"credit ₹{o['credit_rs']:,.0f} · value ₹{value * o['qty']:,.0f} · stop {o['stop_premium']:.2f} ({o['stop_pct']:g}%)"
                      f"{' · breakeven stop on' if o['be_active'] else ''} · target {o['target_value']:.2f} · time exit {ampm(c.time_exit)} · {exits_text(o['stop_pct'], c.vwap_exit)}"}


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


def _entry_hint(d: dict, c, now: datetime, o: Optional[dict]) -> Optional[str]:
    """What can still open today, in one line ('re-entry possible until 1:00 PM · same bias after a stop ... ·
    afternoon sell looks at 1:30 PM'); None when nothing can."""
    if not d or o or d.get("no_data") or d.get("hold"):
        return None
    t, bits = now.time(), []
    used = d.get("trades") or 0
    if used and used < c.max_trades_day and d.get("status") == "closed" and t <= reentry_end(c) and "afternoon" not in (d.get("kinds") or []):
        b = f"re-entry possible until {ampm(reentry_end(c).strftime('%H:%M'))}"
        lastx = (d.get("closed") or [{}])[-1]
        last = (d.get("biases") or [None])[-1]
        if c.reentry_same_bias and last and last not in (d.get("same_used") or []) and lastx.get("why") in STOP_WHYS:
            b += f" · {last.lower()} again on a close back {'over' if last == BULLISH else 'under'} the range and VWAP"
        if last:
            b += f" · or a {'bearish' if last == BULLISH else 'bullish'} flip"
        bits.append(b)
    if c.afternoon_sell and used < c.max_trades_day and "afternoon" not in (d.get("done") or []) and t < bs.hm(c.afternoon_time):
        bits.append(f"afternoon sell looks at {ampm(c.afternoon_time)} (day range ≤ {c.afternoon_max_range_pct:g}%)")
    return " · ".join(bits) or None


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
              "daily_loss": dloss, "window": {"start": ampm(c.window_start), "end": ampm(c.window_end), "open": bs.in_window(t, c.window_start, c.window_end),
                                              "reentry_until": ampm(c.reentry_until) if late_on(c) else None,
                                              "late_open": late_on(c) and bs.hm(c.window_end) < t <= reentry_end(c)},
              "entries": {"same_bias": bool(c.reentry_same_bias), "late": late_on(c), "until": ampm(c.reentry_until), "afternoon": bool(c.afternoon_sell),
                          "afternoon_time": ampm(c.afternoon_time), "afternoon_max_range_pct": c.afternoon_max_range_pct, "text": entries_text(c)},
              "trades": {"used": d.get("trades", 0), "max": c.max_trades_day}, "no_data": bool(d.get("no_data")), "trading_day": is_trading_day(now.date()),
              "vix": {"value": d.get("vix"), "half_at": c.vix_half, "half": bool(d.get("vix") and d["vix"] > c.vix_half)}}
    margin = {"per_lot": c.margin_per_lot, "lots": o["lots"] if o else lots_plan,
              "est": o["margin_est"] if o and o.get("kind") == "afternoon" else round(c.margin_per_lot * (o["lots"] if o else lots_plan), 2),
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
                      "kinds": d.get("kinds") or [], "afternoon": d.get("afternoon"), "entries": entries_text(c), "hint": _entry_hint(d, c, now, o),
                      "schedule": _schedule(c)},
            "guards": guards, "margin": margin, "cards": _cards(st, c, dl, eq, open_pnl), "open": _open_view(o, c), "bias": _bias_steps(d, c),
            "chain": chain_view(st, today), "rules": rules_text(c), "entries": entries_text(c), "days": dl, "trades": _ai_why(trades(50)), "alerts": [{k: v for k, v in a.items() if k != "data"} for a in reversed((st or {}).get("alerts") or [])][:30],
            "deposits": (st or {}).get("deposits") or [], "curve": _curve(), "notes": (st or {}).get("notes") or [], "last_day": (st or {}).get("last_day"),
            "contracts": oi.status(), "updated_at": (st or {}).get("updated_at"), "now": now.isoformat(timespec="seconds")}
    return base


def _schedule(c) -> list[dict]:
    """Today's clock for the page's strip (12-hour), in time order, with the entry modes that are on."""
    out = [(bs.hm("09:30"), {"t": ampm("09:30"), "text": "range + 9:30 chain snapshot"}),
           (bs.hm(c.window_start), {"t": f"{ampm(c.window_start)}–{ampm(c.window_end)}", "text": "bias on 5-minute closes"}),
           (bs.hm(c.time_exit), {"t": ampm(c.time_exit), "text": "time exit"}), (bs.hm("15:20"), {"t": ampm("15:20"), "text": "hard exit"}),
           (bs.hm("15:30"), {"t": ampm("15:30"), "text": "close snapshot"}), (bs.hm("15:35"), {"t": ampm("15:35"), "text": "day summary"})]
    if late_on(c):
        out.append((bs.hm(c.window_end), {"t": f"{ampm(c.window_end)}–{ampm(c.reentry_until)}", "text": "re-entry after a stop"}))
    if c.afternoon_sell:
        out.append((bs.hm(c.afternoon_time), {"t": ampm(c.afternoon_time), "text": f"afternoon sell (range ≤ {c.afternoon_max_range_pct:g}%)"}))
    return [x for _, x in sorted(out, key=lambda p: p[0])]


# --- the page's own reading (any day, holidays included) --------------------------------------------------

VIEW_EVERY_S = 600                      # one "view" chain snapshot per 10 minutes at most when the page is open
_view: dict = {"at": None}


def _ts(ts: str) -> datetime:
    t = datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=IST)


def refresh_view(config: AppConfig, client, now: Optional[datetime] = None) -> bool:
    """Opening the Options page reads the week's contracts and takes one "view" snapshot of the chain (OI, IV, walls,
    PCR) whenever Kite is logged in, on holidays and weekends too, so the page always shows the current week. Throttled
    to VIEW_EVERY_S; on a trading day the tick's own snapshots make this a no-op. Returns True when a snapshot was taken."""
    now = now or datetime.now(IST)
    if not _has_token(client):
        return False
    last = _view["at"]
    if last and (now - last).total_seconds() < VIEW_EVERY_S:
        return False
    _view["at"] = now
    today = now.date()
    try:
        oi.refresh(client, today)
    except Exception as exc:
        logger.warning("Options contracts refresh failed: %s", exc)
    exp = oi.near_expiry(today.isoformat())
    if not exp:
        return False
    snap = ch.last_snapshot()
    if snap and snap.get("ts") and (now - _ts(snap["ts"])).total_seconds() < VIEW_EVERY_S:
        return False
    spot = (snap or {}).get("spot")
    if not spot:
        try:
            spot = ((client.quote([ch.INDEX_KEY]) or {}).get(ch.INDEX_KEY) or {}).get("last_price")
        except Exception as exc:
            logger.warning("Options index quote failed: %s", exc)
            return False
    rows = ch.pick_rows(oi.chain(exp), spot or 0)
    if not rows:
        return False
    try:
        res = ch.fetch(client, rows, now.time(), int(oi.sessions_to_expiry(exp, today) or 0), spot, with_vix=True)
    except Exception as exc:
        logger.warning("Options chain quote failed: %s", exc)
        return False
    if not res["rows"]:
        return False
    ch.snapshot(today.isoformat(), "view", exp, res["rows"], res["spot"], now.isoformat(timespec="seconds"))
    logger.info("Options chain view snapshot %s: %d rows, spot %s", exp, len(res["rows"]), res["spot"])
    return True


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

