"""Automatic jobs, run from a background thread while the app is open (all times IST).

  reminder  trading days, from reminder_time until job_time: Kite login missing -> one alert
  eod       trading days, from job_time: sync candles -> scan -> alerts. If the login is
            missing it alerts once and retries every 15 min until 23:00; if Kite has no bar
            for today yet it retries every 30 min until 22:30, then records "no session".
  intraday  trading days, intraday_start..intraday_end, every intraday_interval_min:
            one Kite quote call for the Forming names; alert once per stock per day when
            the price trades above the pivot (unconfirmed until the close)
  weekly    universe_refresh_weekday from 10:00: NSE holiday list + Nifty 500 refresh,
            alert on index additions/removals
A sleeping Mac or a closed app runs nothing; the eod job catches up when the app is
opened later the same evening.
"""

import json
import threading
from datetime import date, datetime, time, timedelta
from typing import Callable, Optional

from backend.alerts import messages as msg
from backend.alerts.dispatch import Notifier, dispatch_scan
from backend.config import IST, AlertsConfig
from backend.database.models import get_candles, get_db, latest_universe, now_ist
from backend.ingest import holidays as hol
from backend.ingest import universe as uni
from backend.logging_config.logger import setup_logger

logger = setup_logger("jobs")

DONE = ("ok", "partial", "failed", "no_session")      # statuses that end a job for the day
SESSION_START, SESSION_MINUTES = time(9, 15), 375       # 09:15-15:30


def hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def due_jobs(now: datetime, cfg: AlertsConfig, done_today: set, trading: bool,
             last_intraday: Optional[datetime] = None, not_before: Optional[dict] = None) -> list[str]:
    """Which jobs should run at `now` (pure; used by the loop and the tests)."""
    t, nb = now.time(), not_before or {}
    jobs = []

    def ready(job):
        return job not in done_today and (job not in nb or now >= nb[job])

    if trading and cfg.system and ready("reminder") and hm(cfg.reminder_time) <= t < hm(cfg.job_time):
        jobs.append("reminder")
    if trading and cfg.job_enabled and ready("eod") and t >= hm(cfg.job_time):
        jobs.append("eod")
    if (trading and cfg.intraday and hm(cfg.intraday_start) <= t <= hm(cfg.intraday_end)
            and (last_intraday is None or now - last_intraday >= timedelta(minutes=cfg.intraday_interval_min))):
        jobs.append("intraday")
    if now.weekday() == cfg.universe_refresh_weekday and ready("weekly") and t >= time(10, 0):
        jobs.append("weekly")
    return jobs


def volume_pace(vol_so_far: float, avg_vol: float, now: datetime) -> Optional[float]:
    """Projected full-day volume as a multiple of the average, from the share of the session elapsed."""
    start = datetime.combine(now.date(), SESSION_START, now.tzinfo)
    frac = (now - start).total_seconds() / 60 / SESSION_MINUTES
    if not avg_vol or frac <= 0.02:
        return None
    return vol_so_far / min(frac, 1.0) / avg_vol


class Scheduler:
    def __init__(self, state, clock: Callable[[], datetime] = lambda: datetime.now(IST),
                 notifier_factory: Optional[Callable[[AlertsConfig], Notifier]] = None):
        self.state = state
        self.clock = clock
        self.notifier_factory = notifier_factory or Notifier
        self.last_intraday: Optional[datetime] = None
        self.not_before: dict[str, datetime] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

    # --- loop ---

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="alerts-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Scheduler tick failed")
            self._stop.wait(30)

    def tick(self, now: Optional[datetime] = None) -> list[str]:
        now = now or self.clock()
        today = now.date()
        jobs = due_jobs(now, self.state.config.alerts, self.done_today(today), hol.is_trading_day(today),
                        self.last_intraday, self.not_before)
        for job in jobs:
            getattr(self, f"run_{job}")(now)
        return jobs

    # --- bookkeeping ---

    @staticmethod
    def done_today(today: date) -> set:
        with get_db() as conn:
            rows = conn.execute(f"SELECT DISTINCT job FROM job_runs WHERE run_date = ? AND status IN "
                                f"({','.join('?' * len(DONE))})", (today.isoformat(), *DONE)).fetchall()
        return {r["job"] for r in rows}

    @staticmethod
    def record(job: str, today: date, status: str, message: str, started: str) -> None:
        with get_db() as conn:
            conn.execute("INSERT INTO job_runs (job, run_date, started_at, finished_at, status, message) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (job, today.isoformat(), started, now_ist(), status, message))
        logger.info("Job %s %s: %s — %s", job, today, status, message)

    def notifier(self) -> Notifier:
        return self.notifier_factory(self.state.config.alerts)

    def _tokens(self) -> dict:
        return {s["symbol"]: s["instrument_token"] for s in uni.resolve_universe(self.state.config.ingest)["stocks"]}

    # --- jobs ---

    def run_reminder(self, now: datetime) -> None:
        today, started = now.date(), now_ist()
        if self.state.token_valid():
            self.record("reminder", today, "ok", "Kite login present", started)
            return
        self.notifier().system(f"{today}|login-reminder",
                               f"Kite login needed for today's {self.state.config.alerts.job_time} evening scan "
                               "(tokens expire 06:00 IST). Open the dashboard and log in.")
        self.record("reminder", today, "ok", "reminder sent", started)

    def run_eod(self, now: datetime, force: bool = False) -> dict:
        with self.lock:
            return self._run_eod(now, force)

    def _run_eod(self, now: datetime, force: bool) -> dict:
        today, started, st = now.date(), now_ist(), self.state
        cutoff_login, cutoff_data = datetime.combine(today, time(23, 0), now.tzinfo), \
            datetime.combine(today, time(22, 30), now.tzinfo)
        notifier = self.notifier()
        if not st.token_valid():
            notifier.system(f"{today}|eod-login", "Evening job waiting: Kite login missing. Log in and it runs "
                                                  "within 15 minutes (or press Run evening job now).")
            if now >= cutoff_login and not force:
                self.record("eod", today, "failed", "no Kite login by 23:00", started)
            else:
                self.not_before["eod"] = now + timedelta(minutes=15)
                self.record("eod", today, "waiting", "Kite login missing", started)
            return {"status": "waiting_login"}
        if st.worker and st.worker.is_alive():
            self.not_before["eod"] = now + timedelta(minutes=5)
            return {"status": "busy"}

        snap = st.run_sync_blocking()                      # sync, then scan (same path as the Sync button)
        if snap["status"] not in ("ok", "partial"):
            notifier.system(f"{today}|eod-sync-{snap['status']}", f"Evening sync {snap['status']}: {snap['message']}")
            self.record("eod", today, "failed", f"sync {snap['status']}: {snap['message']}", started)
            return {"status": "sync_failed", "sync": snap}
        if snap["status"] == "partial":
            failed = ", ".join(e["symbol"] for e in snap["errors"][:10])
            notifier.system(f"{today}|eod-partial", f"Evening sync partial: {snap['failed']} symbols failed ({failed}).")

        with get_db() as conn:
            as_of = conn.execute("SELECT MAX(as_of) FROM scan_runs").fetchone()[0]
        if as_of != today.isoformat() and not force:
            if now >= cutoff_data:
                self.record("eod", today, "no_session", f"no {today} bar from Kite (holiday or no data)", started)
                notifier.system(f"{today}|eod-nodata", f"No {today} bar from Kite by 22:30: no stage alerts today.")
            else:
                self.not_before["eod"] = now + timedelta(minutes=30)
                self.record("eod", today, "retry", f"latest bar {as_of}; retrying in 30 min", started)
            return {"status": "no_bar", "as_of": as_of}

        sent = dispatch_scan(as_of, st.config.alerts, st.config.bases, notifier, self._tokens(),
                             st.config.screen.fresh_sessions)
        status = "partial" if snap["status"] == "partial" else "ok"
        self.record("eod", today, status, f"scan {as_of}; alerts {sent}", started)
        return {"status": status, "as_of": as_of, "alerts": sent}

    def run_intraday(self, now: datetime) -> dict:
        self.last_intraday = now
        st, cfg, today = self.state, self.state.config.alerts, now.date()
        if not st.token_valid():
            return {"status": "no_login"}
        stocks = {s["symbol"]: s for s in uni.resolve_universe(st.config.ingest)["stocks"]}
        with get_db() as conn:
            as_of = conn.execute("SELECT MAX(as_of) FROM scan_runs").fetchone()[0]
            rows = conn.execute(
                f"SELECT symbol, payload FROM scan_results WHERE as_of = ? AND stage = 'forming' AND screen IN "
                f"({','.join('?' * len(cfg.screens))})", (as_of, *cfg.screens)).fetchall()
        cands = {}
        for r in rows:
            if r["symbol"] in stocks and r["symbol"] not in cands:
                cands[r["symbol"]] = json.loads(r["payload"])
        if not cands:
            return {"status": "no_candidates"}
        keys = {f"NSE:{stocks[s].get('tradingsymbol', s)}": s for s in cands}
        try:
            quotes = st.client.quote(list(keys))
        except Exception as exc:
            logger.warning("Intraday quote failed: %s", exc)
            return {"status": "quote_failed", "error": str(exc)}
        notifier, sent = self.notifier(), 0
        for key, sym in keys.items():
            q, p = quotes.get(key) or {}, cands[sym]
            ltp = q.get("last_price")
            if not ltp or ltp <= p["pivot"]:
                continue
            vol = q.get("volume") or q.get("volume_traded")
            with get_db() as conn:
                hist = get_candles(conn, stocks[sym]["instrument_token"], limit=50)
            avg = sum(c["volume"] for c in hist) / len(hist) if hist else None
            pace = volume_pace(vol, avg, now) if vol and avg else None
            if notifier.send(f"{today}|intraday|{sym}", "intraday", msg.intraday_alert(p, ltp, vol, pace, avg), sym):
                sent += 1
        return {"status": "ok", "checked": len(keys), "sent": sent}

    def run_weekly(self, now: datetime) -> dict:
        today, started, st = now.date(), now_ist(), self.state
        notes = []
        try:
            hol.refresh_from_nse()
            notes.append("holidays refreshed")
        except Exception as exc:
            notes.append(f"holiday refresh failed ({exc}); kept the file")
        if not st.token_valid():
            self.not_before["weekly"] = now + timedelta(hours=1)
            self.record("weekly", today, "waiting", "; ".join(notes + ["Kite login needed for the universe refresh"]),
                        started)
            return {"status": "waiting_login"}
        with get_db() as conn:
            _, before = latest_universe(conn)
        try:
            uni.refresh_universe(st.client, st.config.ingest)
        except Exception as exc:
            self.record("weekly", today, "failed", "; ".join(notes + [f"universe refresh failed: {exc}"]), started)
            return {"status": "failed"}
        with get_db() as conn:
            _, after = latest_universe(conn)
        old, new = {m["symbol"] for m in before}, {m["symbol"] for m in after}
        added, removed = sorted(new - old), sorted(old - new)
        if before and (added or removed):
            self.notifier().system(f"{today}|index-changes",
                                   "Nifty 500 changes: " + (f"added {', '.join(added)}" if added else "")
                                   + ("; " if added and removed else "")
                                   + (f"removed {', '.join(removed)}" if removed else ""))
        self.record("weekly", today, "ok", "; ".join(notes + [f"universe +{len(added)} −{len(removed)}"]), started)
        return {"status": "ok", "added": added, "removed": removed}
