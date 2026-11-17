"""The guard: is AI work allowed right now? `server()` reads the box (RAM, load, CPU from /proc, the AI's RAM from
/api/ps, the scheduler's last tick), `allowed(kind, now, config)` says ok / why not, and the breaker opens for
`breaker_minutes` after `breaker_failures` failures in a row (state in app_settings `ai.breaker`). The scheduler's
tick calls `record_tick(ms)` every time it runs; the full stop lives in app_settings `ai.stopped`."""

import json
import threading
from datetime import datetime, time, timedelta
import time as _time
from typing import Optional

from backend.ai import client
from backend.config import IST, AppConfig
from backend.database.models import get_db, get_setting, set_setting
from backend.timefmt import ampm_date

DAY_START, DAY_END = time(9, 0), time(15, 40)      # day answers (the day model only)
SUNDAY_FROM = time(20, 0)                          # 📖 the night window opens on Sunday evening too (the week's story)
BRIEF_END = time(9, 15)                            # 🤖 brief: kind "brief" may run past night_end, until here (before the 9:16 send)
EOD_DONE = ("ok", "partial", "failed", "no_session")  # the evening job ended for the day with one of these

_lock = threading.Lock()
_tick_ms: Optional[float] = None
_cpu_prev: Optional[tuple] = None                      # (busy, total) jiffies from the last /proc/stat read


# --- the scheduler's tick -------------------------------------------------------------------------------------------

TICK_FRESH_S = 120.0                      # a slow tick counts against the AI only this long (then the server is idle again)
_tick_at: Optional[float] = None


def record_tick(ms: float) -> None:
    """Called by jobs.Scheduler.tick with how long the tick took."""
    global _tick_ms, _tick_at
    _tick_ms = round(float(ms), 1)
    _tick_at = _time.monotonic()


def tick_fresh() -> bool:
    """True while the last recorded tick ended under TICK_FRESH_S ago: the evening job's long tick must not block
    the whole night once it is over."""
    return _tick_at is not None and (_time.monotonic() - _tick_at) < TICK_FRESH_S


def tick_ms() -> Optional[float]:
    return _tick_ms


# --- the box ---------------------------------------------------------------------------------------------------------

def _meminfo() -> dict:
    """{ram_total_gb, ram_free_gb} from /proc/meminfo (MemAvailable); None values off Linux."""
    out = {"ram_total_gb": None, "ram_free_gb": None}
    try:
        with open("/proc/meminfo") as f:
            kb = {ln.split(":")[0]: int(ln.split()[1]) for ln in f if ":" in ln}
        out["ram_total_gb"] = round(kb["MemTotal"] / 1e6, 1)
        out["ram_free_gb"] = round(kb["MemAvailable"] / 1e6, 1)
    except (OSError, KeyError, ValueError, IndexError):
        pass
    return out


def _load1() -> Optional[float]:
    try:
        with open("/proc/loadavg") as f:
            return round(float(f.read().split()[0]), 2)
    except (OSError, ValueError, IndexError):
        return None


def _cpu_pct() -> Optional[float]:
    """CPU busy % since the previous call (deltas of the /proc/stat totals); None on the first call and off Linux."""
    global _cpu_prev
    try:
        with open("/proc/stat") as f:
            parts = [int(x) for x in f.readline().split()[1:]]
    except (OSError, ValueError):
        return None
    total, idle = sum(parts), parts[3] + (parts[4] if len(parts) > 4 else 0)
    prev, _cpu_prev = _cpu_prev, (total - idle, total)
    if not prev or total == prev[1]:
        return None
    return round(100.0 * ((total - idle) - prev[0]) / (total - prev[1]), 1)


def _ai_ram_mb() -> Optional[int]:
    """The RAM the loaded models take (from /api/ps); None when the service does not answer."""
    try:
        return sum(m["ram_mb"] for m in client.ps())
    except client.AIError:
        return None


def server() -> dict:
    """{ram_total_gb, ram_free_gb, load1, cpu_pct, ai_ram_mb, app_tick_ms}: the gauge the guard and the page read."""
    out = _meminfo()
    out.update(load1=_load1(), cpu_pct=_cpu_pct(), ai_ram_mb=_ai_ram_mb(), app_tick_ms=_tick_ms)
    return out


# --- the full stop and the breaker ------------------------------------------------------------------------------------

def stopped() -> bool:
    with get_db() as conn:
        return get_setting(conn, "ai.stopped") == "1"


def set_stopped(on: bool) -> None:
    with get_db() as conn:
        set_setting(conn, "ai.stopped", "1" if on else "0")


def _breaker_state() -> dict:
    with get_db() as conn:
        raw = get_setting(conn, "ai.breaker")
    return json.loads(raw) if raw else {"failures": 0, "until": None}


def _save_breaker(st: dict) -> None:
    with get_db() as conn:
        set_setting(conn, "ai.breaker", json.dumps(st))


def breaker(now: Optional[datetime] = None) -> dict:
    """{open, until, failures}: open while `until` is in the future."""
    st = _breaker_state()
    now = now or datetime.now(IST)
    until = datetime.fromisoformat(st["until"]) if st.get("until") else None
    is_open = bool(until and until > now)
    return {"open": is_open, "until": st["until"] if is_open else None, "failures": int(st.get("failures") or 0)}


def fail(config: AppConfig, now: Optional[datetime] = None) -> dict:
    """One more failure in a row; at `breaker_failures` the breaker opens for `breaker_minutes`."""
    with _lock:
        st = _breaker_state()
        now = now or datetime.now(IST)
        st["failures"] = int(st.get("failures") or 0) + 1
        if st["failures"] >= int(config.ai.breaker_failures):
            st["until"] = (now + timedelta(minutes=int(config.ai.breaker_minutes))).isoformat(timespec="seconds")
        _save_breaker(st)
    return breaker(now)


def ok() -> None:
    """A success closes the breaker and forgets the failures."""
    with _lock:
        _save_breaker({"failures": 0, "until": None})


def breaker_text(b: dict) -> str:
    """'open until 7:12 PM, 15 Nov' / 'closed'."""
    return f"open until {ampm_date(datetime.fromisoformat(b['until']))}" if b.get("open") else "closed"


# --- models and windows -------------------------------------------------------------------------------------------------

def models_present(config: AppConfig) -> dict:
    """{night: {name, present, size_gb}, day: {...}, error}: which of the two configured models are on disk."""
    try:
        have = {m["name"]: m for m in client.models()}
        err = None
    except client.AIError as exc:
        have, err = {}, str(exc)
    row = lambda name: {"name": name, "present": name in have, "size_gb": have[name]["size_gb"] if name in have else None}
    return {"night": row(config.ai.night_model), "day": row(config.ai.day_model), "error": err}


def _eod_done(today: str) -> bool:
    with get_db() as conn:
        return conn.execute(f"SELECT 1 FROM job_runs WHERE job = 'eod' AND run_date = ? AND status IN "
                            f"({','.join('?' * len(EOD_DONE))})", (today, *EOD_DONE)).fetchone() is not None


def night_window(now: datetime, config: AppConfig) -> bool:
    """Night work: from the evening job's end (at or after job_time, once the eod job is done) until night_end; on Sunday
    also from SUNDAY_FROM (8:00 PM), when the week's story is written."""
    from backend.alerts.jobs import hm
    t = now.time()
    if t < hm(config.ai.night_end):
        return True
    if now.weekday() == 6 and t >= SUNDAY_FROM:          # 📖 Sunday evening (no evening job): the week's story
        return True
    return t >= hm(config.alerts.job_time) and _eod_done(now.date().isoformat())


def day_window(now: datetime) -> bool:
    return DAY_START <= now.time() <= DAY_END


def brief_window(now: datetime, config: AppConfig) -> bool:
    """🤖 brief: the night window, extended to BRIEF_END (the brief is written at ai.brief_time, after night_end)."""
    return now.time() < BRIEF_END or night_window(now, config)


def allowed(kind: str, now: Optional[datetime] = None, config: Optional[AppConfig] = None, srv: Optional[dict] = None,
            models: Optional[dict] = None) -> tuple[bool, str]:
    """(ok, reason) for a job of `kind` ("night", "day", "test", ...): the switch, the full stop, the breaker, free
    RAM, the app's tick, the kind's window, the models. `srv` / `models` skip a fresh read when the caller has one."""
    from backend.timefmt import ampm
    config = config or AppConfig()
    c = config.ai
    now = now or datetime.now(IST)
    if not c.enabled:
        return False, "AI switched off"
    if stopped():
        return False, "full stop"
    b = breaker(now)
    if b["open"]:
        return False, f"breaker {breaker_text(b)}"
    srv = srv or server()
    if srv.get("ram_free_gb") is not None and srv["ram_free_gb"] < float(c.min_free_ram_gb):
        return False, f"free RAM {srv['ram_free_gb']:.1f} GB under {c.min_free_ram_gb:g} GB"
    if srv.get("app_tick_ms") is not None and srv["app_tick_ms"] > float(c.slow_tick_s) * 1000 and tick_fresh():
        return False, f"app tick {srv['app_tick_ms'] / 1000:.1f} s over {c.slow_tick_s:g} s"
    if kind == "night" and (not c.night_batch or not night_window(now, config)):
        return False, "night batch off" if not c.night_batch else f"outside the night window (evening job's end to {ampm(c.night_end)})"
    if kind == "day" and (not c.day_answers or not day_window(now)):
        return False, "day answers off" if not c.day_answers else f"outside {ampm(DAY_START)}-{ampm(DAY_END)}"
    if kind == "brief" and (not c.brief or not brief_window(now, config)):          # 🤖 brief
        return False, "morning brief off" if not c.brief else f"outside the brief window (evening job's end to {ampm(BRIEF_END)})"
    if kind == "ask" and (not c.day_answers or not c.ask):                          # 🤖 ask: any time, the day model only
        return False, "day answers off" if not c.day_answers else "ask off"
    m = models or models_present(config)
    if m.get("error"):
        return False, "service unreachable"
    if not (m["night"]["present"] and m["day"]["present"]):
        return False, "models missing"
    return True, "ok"
