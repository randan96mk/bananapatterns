"""⏱ Next up: when each scheduled job runs next (from the same settings and rules the scheduler uses), and when it
last ran. Read-only: it only describes the schedule (backend/alerts/jobs.py due_jobs and engine/real.py due)."""

from datetime import date, datetime, time, timedelta
from typing import Optional

from backend.config import IST, AppConfig
from backend.database.models import get_db

MARKET_OPEN, MARKET_CLOSE = time(9, 15), time(15, 30)


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _trading(d: date) -> bool:
    from backend.ingest import holidays as hol
    return hol.is_trading_day(d)


def _next_at(now: datetime, at: time, trading_only: bool = True, after: Optional[datetime] = None) -> Optional[datetime]:
    """The next datetime at `at` (today if still ahead), on a trading day if asked."""
    for k in range(0, 15):
        d = (now + timedelta(days=k)).date()
        when = datetime.combine(d, at, tzinfo=IST)
        if when > (after or now) and (not trading_only or _trading(d)):
            return when
    return None


def _next_every(now: datetime, start: time, end: time, minutes: int, last: Optional[datetime],
                trading_only: bool = True) -> Optional[datetime]:
    """An every-N-minutes job inside [start, end): the last run + N while inside the window, else the next opening."""
    minutes = max(1, minutes)
    if (not trading_only or _trading(now.date())) and start <= now.time() < end:
        if last:
            nxt = max(last + timedelta(minutes=minutes), now)
        else:                                         # not run yet in this process: the window's own clock steps
            first = datetime.combine(now.date(), start, tzinfo=IST)
            steps = -(-int((now - first).total_seconds()) // (minutes * 60))
            nxt = first + timedelta(minutes=steps * minutes)
        if nxt.time() < end and nxt.date() == now.date():
            return nxt
    return _next_at(now, start, trading_only)


def _last_runs() -> dict:
    with get_db() as conn:
        rows = conn.execute("SELECT job, MAX(finished_at) AS at, status FROM job_runs GROUP BY job").fetchall()
    return {r["job"]: {"at": r["at"], "status": r["status"]} for r in rows}


def market(now: datetime) -> dict:
    today = _trading(now.date())
    is_open = today and MARKET_OPEN <= now.time() < MARKET_CLOSE
    nxt_open = _next_at(now, MARKET_OPEN)
    return {"open": is_open, "trading_day": today,
            "closes_at": datetime.combine(now.date(), MARKET_CLOSE, tzinfo=IST).isoformat() if is_open else None,
            "opens_at": nxt_open.isoformat() if nxt_open and not is_open else None}


def jobs(config: AppConfig, now: datetime, scheduler=None, kite: bool = False) -> list[dict]:
    """[{key, icon, name, desc, at (ISO), every (min or None), real, skip (reason), last {at, status}}], soonest first."""
    a, r = config.alerts, config.real
    last = _last_runs()
    sch = scheduler
    from backend.engine import real
    real_on = real.has_account() and r.enabled and not real.killed()
    out = []

    def add(key, icon, name, desc, when, every=None, is_real=False, skip=None, job=None):
        if when is None:
            return
        out.append({"key": key, "icon": icon, "name": name, "desc": desc, "at": when.isoformat(), "every": every,
                    "real": is_real, "skip": skip, "last": last.get(job or key)})

    if real_on:
        add("real_stops", "🛡️", "Real stop check", f"every {r.stop_check_min} min · live price and day low",
            _next_every(now, _hm(r.send_time), MARKET_CLOSE, r.stop_check_min, getattr(sch, "last_real_stops", None)),
            r.stop_check_min, True)
        add("real_send", "🚀", "Real orders sent", "the planned EXITs, then BUYs → Tradetron", _next_at(now, _hm(r.send_time)),
            None, True)
    if a.intraday:
        add("intraday", "⚡", "Intraday scan", f"every {a.intraday_interval_min} min · watched stocks over their pivot",
            _next_every(now, _hm(a.intraday_start), _hm(a.intraday_end), a.intraday_interval_min,
                        getattr(sch, "last_intraday", None)), a.intraday_interval_min)
        if a.close_check:
            add("close_check", "🕒", "Close check", "breakouts still holding near the close", _next_at(now, _hm(a.close_check_time)))
    if config.flow.mode != "off":
        from backend.intraday import flow
        add("flow", "🔀", "Intraday Flow", f"{config.flow.mode} · live 1-minute candles, runs at each candle close",
            _next_at(now, time(9, 0)), skip="⏹ stopped" if flow.stopped() else None)
    if a.filings:
        add("filings", "📰", "Filings check", f"every {a.filings_interval_min} min · results, deals, red flags",
            _next_every(now, time(7, 0), time(23, 0), a.filings_interval_min, getattr(sch, "last_filings", None), False),
            a.filings_interval_min)
    if a.system:
        add("reminder", "🔔", "Kite login reminder", "only if not logged in", _next_at(now, _hm(a.reminder_time)),
            skip="logged in" if kite else None)
    if a.job_enabled:
        add("eod", "🌙", "Evening sync + scan", "daily candles (only missing days) → stages, Trend Breakout scan, "
            "paper A & E, real orders for tomorrow, backtest re-run", _next_at(now, _hm(a.job_time)))
    if a.top_setups or a.alert_style == "summary":
        add("top_setups", "📨", "Evening report", "one silent Telegram message", _next_at(now, _hm(a.top_setups_time)))
    if a.morning_plan:
        add("morning_plan", "🌅", "Morning plan", "top setups for the day", _next_at(now, _hm(a.morning_plan_time)))
    if a.gap_check:
        add("gap_check", "🧭", "Opening check", "gaps on plan stocks and holdings", _next_at(now, _hm(a.gap_check_time)))
    add("kite_expiry", "🔑", "Kite login expires", "log in again from 6:00 AM", _next_at(now, time(6, 0), False),
        skip=None if kite else "not logged in")
    out.sort(key=lambda j: j["at"])
    return out


def schedule(config: AppConfig, scheduler=None, kite: bool = False, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(IST)
    return {"now": now.isoformat(), "market": market(now), "jobs": jobs(config, now, scheduler, kite)}
