"""Automatic jobs, run from a background thread while the app is open (all times IST).

  reminder  trading days, from reminder_time until job_time: Kite login missing -> one alert
  eod       trading days, from job_time: sync candles -> scan -> alerts. If the login is
            missing it alerts once and retries every 15 min until 23:00; if Kite has no bar
            for today yet it retries every 30 min until 22:30, then records "no session".
  intraday  trading days, intraday_start..intraday_end, every intraday_interval_min:
            one Kite quote call for the Forming names; alert once per stock per day when
            the price trades above the pivot (unconfirmed until the close), with a volume
            verdict; a crossed name falling failed_buffer_pct under the pivot -> failed alert
  close_check  trading days at close_check_time (15:15): verdict for each name above its
            pivot (holding / weak / extended) so you can act before 15:30; the 18:30 alerts
            refer back to it
  weekly    universe_refresh_weekday from 10:00: NSE holiday list + Nifty 500 refresh,
            alert on index additions/removals
  top_setups  trading days from top_setups_time (19:00), after the evening scan: rank the Forming
            setups, save the plan, send the Top N with charts
  morning_plan  trading days morning_plan_time (08:50) until noon: the saved plan's triggers
  gap_check  trading days gap_check_time (09:10) until 10:00: how the plan names opened
  login     each of login_times (06:01, 08:45): if Kite doesn't accept a session, send a
            one-time login link (missed slots catch up as one link when the app wakes)
  login_headsup  optional, login_heads_up_time (05:45) while a token is active: "ends at 06:00"
  login_check    every login_check_min from 06:00 to 23:00: a rejected session -> link at once
A sleeping Mac or a closed app runs nothing; the eod job catches up when the app is
opened later the same evening.
"""

import json
import threading
from datetime import date, datetime, time, timedelta
from typing import Callable, Optional

from backend.alerts import guard
from backend.alerts import intraday as itd
from backend.alerts import live as lv
from backend.alerts import plan as pl
from backend.alerts import messages as msg
from backend.alerts.dispatch import Notifier, decision_for, dispatch_scan
from backend.alerts.login import send_login_link, session_ok
from backend.alerts.priority import MarketContext, decorate, gate
from backend.config import IST, AlertsConfig
from backend.timefmt import ampm
from backend.database.models import get_candles, get_db, latest_universe, now_ist
from backend.ingest import events
from backend.ingest import filings
from backend.ingest import holders
from backend.ingest import holidays as hol
from backend.ingest import universe as uni
from backend.kite_api.auth import EXPIRY_TIME
from backend.logging_config.logger import setup_logger

logger = setup_logger("jobs")

DONE = ("ok", "partial", "failed", "no_session")      # statuses that end a job for the day
MARKET_CLOSE = time(15, 30)
volume_pace = itd.volume_pace                          # re-exported (projection with the NSE volume curve)


FILINGS_START, FILINGS_END = time(7, 0), time(23, 0)


def filings_window_open(now: datetime, cfg) -> bool:
    """The filings check rests on the weekend: never on Saturday, on Sunday only from `filings_weekend_from` (the
    Monday plan needs Sunday evening's filings; nothing before that can be acted on)."""
    wd = now.weekday()
    if wd == 5:
        return False
    if wd == 6:
        return now.time() >= hm(getattr(cfg, "filings_weekend_from", "18:00") or "18:00")
    return True


def hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def login_slots_due(now: datetime, cfg: AlertsConfig, done_today: set, day_ok: bool) -> list[str]:
    """Login-link times that have passed today and weren't handled yet (earliest first)."""
    if not (cfg.login_links and day_ok):
        return []
    return sorted((s for s in cfg.login_times if hm(s) <= now.time() and f"login@{s}" not in done_today), key=hm)


def due_jobs(now: datetime, cfg: AlertsConfig, done_today: set, trading: bool,
             last_intraday: Optional[datetime] = None, not_before: Optional[dict] = None,
             last_login_check: Optional[datetime] = None, last_filings: Optional[datetime] = None) -> list[str]:
    """Which jobs should run at `now` (pure; used by the loop and the tests)."""
    t, nb = now.time(), not_before or {}
    jobs = []
    login_day = trading or cfg.login_every_day

    def ready(job):
        return job not in done_today and (job not in nb or now >= nb[job])

    if trading and cfg.system and ready("reminder") and hm(cfg.reminder_time) <= t < hm(cfg.job_time):
        jobs.append("reminder")
    if trading and cfg.job_enabled and ready("eod") and t >= hm(cfg.job_time):
        jobs.append("eod")
    if (trading and cfg.intraday and hm(cfg.intraday_start) <= t <= hm(cfg.intraday_end)
            and (last_intraday is None or now - last_intraday >= timedelta(minutes=cfg.intraday_interval_min))):
        jobs.append("intraday")
    if (trading and cfg.intraday and cfg.close_check and ready("close_check")
            and hm(cfg.close_check_time) <= t < MARKET_CLOSE):
        jobs.append("close_check")
    if trading and cfg.day_summary and ready("day_summary") and hm(cfg.day_summary_time) <= t < time(23, 0):
        jobs.append("day_summary")                         # 🌙 one close-of-day message across the books
    if (trading and (cfg.top_setups or cfg.alert_style == "summary") and ready("top_setups")
            and hm(cfg.top_setups_time) <= t < time(23, 0)):             # summary style: the evening report
        jobs.append("top_setups")
    if trading and cfg.morning_plan and ready("morning_plan") and hm(cfg.morning_plan_time) <= t < time(12, 0):
        jobs.append("morning_plan")
    if trading and cfg.gap_check and ready("gap_check") and hm(cfg.gap_check_time) <= t < time(10, 0):
        jobs.append("gap_check")
    if now.weekday() == cfg.universe_refresh_weekday and ready("weekly") and t >= time(10, 0):
        jobs.append("weekly")
    if (cfg.filings and FILINGS_START <= t < FILINGS_END and filings_window_open(now, cfg)
            and (last_filings is None or now - last_filings >= timedelta(minutes=cfg.filings_interval_min))):
        jobs.append("filings")                             # weekdays, and Sunday evening on: results come out in the evening
    if login_slots_due(now, cfg, done_today, login_day):
        jobs.append("login")
    if (cfg.login_links and cfg.login_heads_up and login_day and ready("login_headsup")
            and hm(cfg.login_heads_up_time) <= t < EXPIRY_TIME):
        jobs.append("login_headsup")
    if (cfg.login_links and login_day and EXPIRY_TIME <= t < time(23, 0) and "login" not in jobs
            and (last_login_check is None or now - last_login_check >= timedelta(minutes=cfg.login_check_min))):
        jobs.append("login_check")
    return jobs


class Scheduler:
    def __init__(self, state, clock: Callable[[], datetime] = lambda: datetime.now(IST),
                 notifier_factory: Optional[Callable[[AlertsConfig], Notifier]] = None):
        self.state = state
        self.clock = clock
        self.notifier_factory = notifier_factory or Notifier
        self.last_intraday: Optional[datetime] = None
        self.last_login_check: Optional[datetime] = None
        self.last_filings: Optional[datetime] = None
        self.not_before: dict[str, datetime] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()
        self.hold: Optional[str] = None                   # summary style: the batch the evening job is holding
        self.last_real_stops: Optional[datetime] = None   # 💰 real trading: the last stop watch
        self.last_options_tick: Optional[datetime] = None   # 🧩 options: the last minute the options book ticked
        self.options_sent: set = set()                      # 🧩 options: the alert keys already handed to the notifier

    # --- loop ---

    def start(self) -> None:
        try:                                              # rules changed since the saved run (E -> E+): re-run now
            from backend.engine import trend_breakout as tb
            run = tb.saved(self.state.config)
            if run and run.get("stale"):
                threading.Thread(target=tb.run, args=(self.state.config,), name="breakout-stale", daemon=True).start()
        except Exception:
            logger.exception("Stale Trend Breakout check failed")
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
        """One pass: every due job runs; how long it took goes to the AI guard (`ai.guard.record_tick`)."""
        import time as _t
        t0 = _t.monotonic()
        try:
            return self._tick(now)
        finally:
            try:
                from backend.ai import guard as ai_guard
                ai_guard.record_tick((_t.monotonic() - t0) * 1000)
            except Exception:                               # the gauge never breaks the scheduler
                pass

    def _tick(self, now: Optional[datetime] = None) -> list[str]:
        now = now or self.clock()
        today = now.date()
        jobs = due_jobs(now, self.state.config.alerts, self.done_today(today), hol.is_trading_day(today),
                        self.last_intraday, self.not_before, self.last_login_check, self.last_filings)
        from backend.engine import real
        if real.has_account():                             # 💰 only once real trading has started
            jobs += real.due(now, self.state.config, self.done_today(today), hol.is_trading_day(today), self.last_real_stops)
        try:                                               # 🧩 the options book: one tick a minute 9:15-15:40
            from backend.options import book as obook
            jobs += obook.due(now, self.state.config, self.done_today(today), hol.is_trading_day(today), self.last_options_tick)
        except Exception:
            logger.exception("Options due check failed")
        try:                                               # 🤖 brief: the morning brief at ai.brief_time
            from backend.ai import brief
            jobs += brief.due(now, self.state.config, self.done_today(today), hol.is_trading_day(today))
        except Exception:
            logger.exception("AI brief due check failed")
        try:                                               # 📖 the week's story: Sunday evening
            from backend.ai import week_story
            jobs += week_story.due(now, self.state.config, self.done_today(today))
        except Exception:
            logger.exception("AI week due check failed")
        try:                                               # 🧹 archive and vacuum: once a week (maintenance.day / time)
            from backend.database import archive
            jobs += archive.due(now, self.state.config, self.done_today(today))
        except Exception:
            logger.exception("Archive due check failed")
        try:                                               # 📷 Tips screenshots older than 2 days: once a day
            from backend.tips import vision as vs
            jobs += vs.due(now, self.done_today(today))
        except Exception:
            logger.exception("Tips media due check failed")
        try:                                               # ⚡ Tips → Kite: the 3:35 PM day check (trading days)
            from backend.tips import kite_check as kc
            jobs += kc.due(now, self.state.config, self.done_today(today), hol.is_trading_day(today))
        except Exception:
            logger.exception("Tips Kite day check due failed")
        try:                                               # 🤖 AI on Tips: the 6:35 PM reviews, Sunday 7:05 PM weekly profile
            from backend.tips import ai4
            jobs += ai4.due(now, self.state.config, self.done_today(today), hol.is_trading_day(today))
        except Exception:
            logger.exception("Tips AI due check failed")
        try:                                               # 🤖 suggestions: an open one past its time expires (no tap)
            from backend.ai import suggest
            suggest.expire_open(now)
        except Exception:
            logger.exception("AI suggestion expiry failed")
        try:                                               # 🤖 quiet hours over: the held AI messages go out
            self.flush_quiet(now)
        except Exception:
            logger.exception("AI quiet-hours flush failed")
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
        n = self.notifier_factory(self.state.config.alerts)
        n.hold = self.hold                                  # summary style: the evening job holds its alerts
        n.ai = self.state.config.ai                         # 🤖 the AI quiet hours
        return n

    def flush_quiet(self, now: datetime) -> int:
        """🤖 Past the AI quiet hours (quiet_to), the AI messages held overnight go out in order (Notifier.flush_quiet;
        the morning brief may have sent them already). Nothing to do inside the window or with nothing held."""
        from backend.alerts.dispatch import in_quiet
        if in_quiet(now, self.state.config.ai):
            return 0
        flush = getattr(self.notifier(), "flush_quiet", None)
        return flush() if callable(flush) else 0

    def summary(self) -> bool:
        return self.state.config.alerts.alert_style == "summary"

    def _tokens(self) -> dict:
        return {s["symbol"]: s["instrument_token"] for s in uni.resolve_universe(self.state.config.ingest)["stocks"]}

    # --- jobs ---

    def run_reminder(self, now: datetime) -> None:
        today, started = now.date(), now_ist()
        if self.state.token_valid():
            self.record("reminder", today, "ok", "Kite login present", started)
            return
        self._login_needed(f"{today}|login-reminder", now,
                           f"Kite login needed for today's {ampm(self.state.config.alerts.job_time)} evening scan "
                           "(tokens expire 6:00 AM IST). Open the dashboard and log in.")
        self.record("reminder", today, "ok", "reminder sent", started)

    def _login_needed(self, key: str, now: datetime, text: str, notifier: Optional[Notifier] = None) -> None:
        """Evening-scan login nudge: a login link when possible, else the plain system alert."""
        notifier = notifier or self.notifier()
        if not (self.state.config.alerts.login_links and send_login_link(self.state, notifier, "evening", key, now)):
            notifier.system(key, text)

    def run_login(self, now: datetime) -> dict:
        """Login-link slots: one link for all slots due now, unless Kite accepts the current session."""
        today, started, cfg = now.date(), now_ist(), self.state.config.alerts
        slots = login_slots_due(now, cfg, self.done_today(today), True)
        if not slots:
            return {"status": "none"}
        self.last_login_check = now
        if session_ok(self.state):
            sent, note = False, "Kite session valid"
        else:
            first = min(cfg.login_times, key=hm)
            sent = send_login_link(self.state, self.notifier(), "new_day" if first in slots else "still_needed",
                                   f"{today}|login|{slots[-1]}", now)
            note = "login link sent" if sent else "login needed; link not sent (Telegram or Kite keys not set)"
        for s in slots:
            self.record(f"login@{s}", today, "ok", note, started)
        return {"status": "ok", "slots": slots, "sent": sent}

    def run_login_headsup(self, now: datetime) -> dict:
        today, started, cfg = now.date(), now_ist(), self.state.config.alerts
        sent = False
        if self.state.token_valid() and cfg.login_times:
            sent = self.notifier().send(f"{today}|login-headsup", "login",
                                        msg.login_heads_up(min(cfg.login_times, key=hm)))
        self.record("login_headsup", today, "ok", "heads-up sent" if sent else "no active session", started)
        return {"status": "ok", "sent": sent}

    def run_login_check(self, now: datetime) -> dict:
        """A held session that Kite now rejects (logged out / revoked) gets a link straight away."""
        self.last_login_check = now
        if not self.state.token_valid() or session_ok(self.state):
            return {"status": "ok", "sent": False}
        sent = send_login_link(self.state, self.notifier(), "revoked", f"{now.date()}|login|revoked|{now:%H:%M}", now)
        return {"status": "revoked", "sent": sent}

    def run_eod(self, now: datetime, force: bool = False) -> dict:
        with self.lock:
            out = self._run_eod(now, force)
        if out.get("status") in ("ok", "partial"):       # 🤖 the night's filing / deal notes, once the eod record is in
            out["ai_notes"] = self.run_ai_notes(now)
            out["ai_why"] = self.run_ai_why(now)          # 🤖 why: the lines under today's exits (FIFO: after the notes)
            out["ai_suggest"] = self.run_ai_suggest(now)  # 🤖 suggestions: for the next session (FIFO: after the notes)
            out["ai_view"] = self.run_ai_view(now)        # 🤖 AI view: a rating beside every suggested stock (after the suggestions)
            out["ai_week_story"] = self.run_ai_week_story(now)   # 📖 the last closed week's story when Sunday's was missed (after the ratings)
        return out

    def rerun_night(self, now: datetime) -> dict:
        """🤖 Queue the night batch again (warm-up, notes, why, suggestions, ratings, the week's story): the AI page's
        "Re-run tonight" button, Telegram `ai rerun`, and the start after a deploy when the night's jobs had been
        skipped. Each step queues only what is still missing (a filing already noted, a name already rated, a job already
        queued is not repeated); the guard decides at run time."""
        return {"ai_notes": self.run_ai_notes(now), "ai_why": self.run_ai_why(now), "ai_suggest": self.run_ai_suggest(now),
                "ai_view": self.run_ai_view(now), "ai_week_story": self.run_ai_week_story(now)}

    def rerun_if_skipped(self, now: Optional[datetime] = None) -> dict:
        """At start-up (a deploy restarts the app): when the night window is open and today's night jobs were skipped
        for a passing reason (a busy app, low RAM, the breaker — not a switch or a full stop), queue them again."""
        now = now or datetime.now(IST)
        try:
            from backend.ai import guard, queue as aiq
            if not self.state.config.ai.enabled or not guard.night_window(now, self.state.config):
                return {"status": "not now"}
            day = now.date().isoformat()
            with get_db() as conn:
                rows = conn.execute("SELECT error FROM ai_jobs WHERE kind = 'night' AND status = 'skipped' AND created >= ? "
                                    "AND created < ?", (f"{day}T00:00", f"{day}T23:59:59")).fetchall()
            why = [r[0] or "" for r in rows if aiq.transient(r[0] or "") and not (r[0] or "").startswith("stale")]
            if not why:
                return {"status": "nothing skipped"}
            logger.info("AI: %d night jobs were skipped today (%s); queuing the night batch again", len(why), why[0][:60])
            return {"status": "requeued", "skipped": len(why), **self.rerun_night(now)}
        except Exception as exc:
            logger.exception("AI rerun after start failed")
            return {"status": "failed", "error": str(exc)[:200]}

    def run_ai_why(self, now: datetime) -> dict:
        """🤖 why: queue tonight's "why" lines for the trades closed today (one job; the guard decides at run time)."""
        if not self.state.config.ai.enabled:
            return {"status": "off"}
        try:
            from backend.ai import why
            return why.enqueue_tonight(self.state.config, now, self.notifier())
        except Exception as exc:
            logger.exception("AI why enqueue failed")
            return {"status": "failed", "error": str(exc)[:200]}

    def run_ai_suggest(self, now: datetime) -> dict:
        """🤖 suggestions: queue tonight's suggestion job (outcomes scored, candidates born, the evening list)."""
        if not self.state.config.ai.enabled:
            return {"status": "off"}
        try:
            from backend.ai import suggest
            return suggest.enqueue_tonight(self.state.config, now, self.notifier())
        except Exception as exc:
            logger.exception("AI suggest enqueue failed")
            return {"status": "failed", "error": str(exc)[:200]}

    def run_ai_view(self, now: datetime) -> dict:
        """🤖 AI view: queue tonight's rating job (the plan, held, Follow and Screens names; FIFO after the
        suggestions). Never raises."""
        if not self.state.config.ai.enabled:
            return {"status": "off"}
        try:
            from backend.ai import view
            return view.enqueue_tonight(self.state.config, now, self.notifier())
        except Exception as exc:
            logger.exception("AI view enqueue failed")
            return {"status": "failed", "error": str(exc)[:200]}

    def run_ai_week_story(self, now: datetime) -> dict:
        """📖 queue the last closed week's story when it has none (FIFO after the ratings). Never raises."""
        if not self.state.config.ai.enabled:
            return {"status": "off"}
        try:
            from backend.ai import week_story
            return week_story.enqueue_due(self.state.config, now, self.notifier())
        except Exception as exc:
            logger.exception("AI week story enqueue failed")
            return {"status": "failed", "error": str(exc)[:200]}

    def run_archive(self, now: datetime) -> dict:
        """🧹 Move the old log rows to data/archive.db, checkpoint and VACUUM (archive.run records the job_runs row).
        Never raises; a run already going (the Data page's Run now) is left alone and retried on the next tick."""
        from backend.database import archive
        try:
            return {"status": "ok", **archive.run(self.state.config, now)}
        except archive.Busy:
            return {"status": "busy"}
        except Exception as exc:
            logger.exception("Archive job failed")
            self.record(archive.JOB, now.date(), "failed", str(exc)[:200], now_ist())
            return {"status": "failed", "error": str(exc)[:200]}

    def run_ai_week(self, now: datetime) -> dict:
        """📖 Sunday evening: queue the week's story (kind "night", key week:<iso>) and record the job so it is queued once.
        Never raises."""
        from backend.ai import week_story
        today, started = now.date(), now_ist()
        try:
            job = week_story.enqueue_due(self.state.config, now, self.notifier())
        except Exception as exc:
            logger.exception("AI week enqueue failed")
            self.record(week_story.JOB, today, "failed", str(exc)[:200], started)
            return {"status": "failed", "error": str(exc)[:200]}
        msg_ = (f"queued as AI job {job['id']}" + (" (already queued)" if job.get("dedup") else "")) if job.get("id") else \
            f"{job.get('status')} ({job.get('week', '')})"
        self.record(week_story.JOB, today, "ok", msg_, started)
        return {"status": "ok", **job}

    def _ai_warm(self, now: datetime) -> None:
        """🤖 Load the night model once at the start of the night batch (a "warm" job through the queue, so the guard
        still decides): the first real job then starts with the model in RAM instead of spending its timeout loading."""
        try:
            from backend.ai import client, guard, queue as aiq
            c = self.state.config.ai
            if not guard.allowed("night", now, self.state.config)[0]:
                return
            aiq.enqueue(self.state.config, "night", f"warm:{now.date().isoformat()}",
                        lambda: client.warm(c.night_model, c.keep_alive), model=c.night_model, note="model warm-up")
        except Exception:
            logger.exception("AI warm-up enqueue failed")

    def run_ai_notes(self, now: datetime) -> dict:
        """🤖 Queue tonight's AI notes (the guard decides at run time; nothing runs while the AI is off). Never raises."""
        from backend.ai import notes
        if not self.state.config.ai.enabled:
            return {"status": "off"}
        self._ai_warm(now)
        try:
            return notes.enqueue_tonight(self.state.config, now, self.notifier())
        except Exception as exc:
            logger.exception("AI notes enqueue failed")
            return {"status": "failed", "error": str(exc)[:200]}

    def run_tips_media(self, now: datetime) -> dict:
        """📷 The daily sweep: Tips screenshot files older than 2 days are deleted (the OCR text stays on the post)."""
        from backend.tips import vision as vs
        today, started = now.date(), now_ist()
        res = vs.sweep(now)
        self.record(vs.JOB, today, "ok", f"{res['removed']} image file(s) older than 2 days removed · {res['kept']} kept"
                    + (f" · {res['errors']} not removable" if res["errors"] else ""), started)
        return {"status": "ok", **res}

    def run_tips_kite_day(self, now: datetime) -> dict:
        """⚡ 3:35 PM: every Tips order of today against the hints, one Telegram summary (backend/tips/kite_check.py)."""
        from backend.tips import kite_check as kc
        today, started = now.date(), now_ist()
        try:
            res = kc.day_check(self.state.config, now, client=getattr(self.state, "client", None))
        except Exception as exc:
            logger.exception("Tips Kite day check failed")
            self.record(kc.DAY_JOB, today, "failed", str(exc)[:200], started)
            return {"status": "failed", "error": str(exc)[:200]}
        self.record(kc.DAY_JOB, today, "ok", res["summary"], started)
        return {"status": "ok", **res}

    def run_tips_ai_evening(self, now: datetime) -> dict:
        """🤖 6:35 PM: the evening review of every Kite hint closed after the 3:35 PM day check, and the day's Telegram
        once every review is written (backend/tips/ai4.py). Never raises; never acts on an order."""
        from backend.tips import ai4
        today, started = now.date(), now_ist()
        try:
            ids = ai4.review_day(self.state.config, now)
            sent = ai4.send_review_day(self.state.config, today.isoformat(), now)
        except Exception as exc:
            logger.exception("Tips AI evening run failed")
            self.record(ai4.EVENING_JOB, today, "failed", str(exc)[:200], started)
            return {"status": "failed", "error": str(exc)[:200]}
        self.record(ai4.EVENING_JOB, today, "ok", f"{len(ids)} review(s) queued" + (" · Telegram sent" if sent else ""), started)
        return {"status": "ok", "queued": ids, "sent": sent}

    def run_tips_ai_weekly(self, now: datetime) -> dict:
        """🤖 Sunday 7:05 PM: the weekly channel profile per channel with a call in the week just ended. Never raises."""
        from backend.tips import ai4
        today, started = now.date(), now_ist()
        try:
            ids = ai4.weekly_due(self.state.config, now)
        except Exception as exc:
            logger.exception("Tips AI weekly failed")
            self.record(ai4.WEEKLY_JOB, today, "failed", str(exc)[:200], started)
            return {"status": "failed", "error": str(exc)[:200]}
        self.record(ai4.WEEKLY_JOB, today, "ok", f"{len(ids)} channel profile(s) queued", started)
        return {"status": "ok", "queued": ids}

    def run_ai_brief(self, now: datetime) -> dict:
        """🤖 brief: queue today's morning brief (kind "brief"; the guard decides at run time) and record the job so it
        is queued once a day. Never raises."""
        from backend.ai import brief
        today, started = now.date(), now_ist()
        try:
            job = brief.enqueue(self.state.config, now, self.notifier())
        except Exception as exc:
            logger.exception("AI brief enqueue failed")
            self.record(brief.JOB, today, "failed", str(exc)[:200], started)
            return {"status": "failed", "error": str(exc)[:200]}
        self.record(brief.JOB, today, "ok", f"queued as AI job {job['id']}" + (" (already queued)" if job["dedup"] else ""), started)
        return {"status": "ok", "job": job["id"], "dedup": job["dedup"]}

    def _run_eod(self, now: datetime, force: bool) -> dict:
        today, started, st = now.date(), now_ist(), self.state
        cutoff_login, cutoff_data = datetime.combine(today, time(23, 0), now.tzinfo), \
            datetime.combine(today, time(22, 30), now.tzinfo)
        notifier = self.notifier()
        if not st.token_valid():
            self._login_needed(f"{today}|eod-login", now, "Evening job waiting: Kite login missing. Log in and it "
                                                          "runs within 15 minutes (or press Run evening job now).",
                               notifier)
            if now >= cutoff_login and not force:
                self.record("eod", today, "failed", "no Kite login by 11:00 PM", started)
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
                notifier.system(f"{today}|eod-nodata", f"No {today} bar from Kite by 10:30 PM: no stage alerts today.")
            else:
                self.not_before["eod"] = now + timedelta(minutes=30)
                self.record("eod", today, "retry", f"latest bar {as_of}; retrying in 30 min", started)
            return {"status": "no_bar", "as_of": as_of}

        if st.config.alerts.results_calendar:
            self.refresh_events()
        self.hold = as_of if self.summary() else None      # summary style: held for the evening report
        try:
            notifier.hold = self.hold
            sent = dispatch_scan(as_of, st.config.alerts, st.config.bases, notifier, self._tokens(),
                                 st.config.screen.fresh_sessions, vcp_cfg=st.config.vcp)
            sent["guard"] = self.run_guard(as_of)      # the positions you added with "I bought" (never Kite holdings)
        finally:
            self.hold = None
            notifier.hold = None
        try:                                       # Trend Breakout: the evening scan (signals only, never orders)
            sent["breakout"] = self.run_breakout_scan(as_of)
        except Exception:
            logger.exception("Trend Breakout scan failed")
        try:                                       # 📝 paper trading: today's fills, tomorrow's orders (simulated)
            sent["paper"] = self.run_paper()
        except Exception:
            logger.exception("Paper trading update failed")
        try:                                       # 💰 real trading: mark the account, plan tomorrow's orders
            sent["real"] = self.run_real_evening()
        except Exception:
            logger.exception("Real trading evening update failed")
        try:                                       # 📈 Follow: the blind book's buys and sells at the next open
            from backend.engine import follow
            line = follow.evening_line(as_of)
            if line:
                notifier.send(f"{as_of}|follow", "follow", line)
        except Exception:
            logger.exception("Follow evening line failed")
        try:                                       # 💾 the nightly database backup
            from backend.engine import backup
            backup.run()
        except Exception:
            logger.exception("Backup failed")
        self._rerun_breakout()                     # the backtest to the latest day, in the background
        status = "partial" if snap["status"] == "partial" else "ok"
        self.record("eod", today, status, f"scan {as_of}; alerts {sent}", started)
        return {"status": status, "as_of": as_of, "alerts": sent}

    def run_filings(self, now: datetime) -> dict:
        """Fetch new NSE filings; push the important ones on stocks you watch, hold or have in the plan."""
        self.last_filings = now
        cfg = self.state.config.alerts
        deals = []
        if cfg.red_flags or cfg.deals:
            try:
                deals = holders.refresh_from_nse()
            except Exception as exc:
                logger.warning("Deals fetch failed (%s); keeping the stored ones", exc)
        try:
            new = filings.refresh_from_nse()
        except Exception as exc:
            logger.warning("Filings fetch failed (%s); keeping the stored ones", exc)
            new = None
        sent = 0
        if cfg.filings_alerts:
            sent = self.alert_filings(new or [], now) + self.alert_deals(deals)
        if new and self.state.config.ai.enabled:          # 🤖 notes for the new in-scope filings (within the night window)
            try:
                from backend.ai import notes
                notes.enqueue_new_filings(self.state.config, new, now, self.notifier())   # by day the job waits in the queue until the night window opens
            except Exception:
                logger.exception("AI notes for new filings failed")
        if new is None:
            return {"status": "failed", "deals": len(deals), "sent": sent}
        return {"status": "ok", "new": len(new), "deals": len(deals), "sent": sent}

    @staticmethod
    def _scope() -> dict[str, str]:
        """Stocks you hold (any book), have in the plan or watch: {symbol: why} (backend.ai.notes.scope, shared with
        the AI notes so both read the same names)."""
        from backend.ai import notes
        return {s: v["why"] for s, v in notes.scope().items()}

    @staticmethod
    def _ai_note(kind: str, key: str):
        """🤖 The AI's note for a filing / deal source when one exists at send time (the line goes on the alert)."""
        try:
            from backend.ai import notes
            return notes.note_for(kind, key)
        except Exception:
            return None

    def alert_deals(self, new: list[dict]) -> int:
        """Red flags and fund moves from newly fetched deals / disclosures, on stocks in scope; once each."""
        scope = self._scope()
        syms = sorted({d["symbol"] for d in new} & set(scope))
        if not syms:
            return 0
        cfg, notifier, sent = self.state.config.alerts, self.notifier(), 0
        dates = {(d["symbol"], d["date"]) for d in new}
        for sym, s in holders.signals(syms, cfg).items():
            for key, icon in (("red", "🚩"), ("buy", "🏦"), ("sell", "🏦")):
                for i in s[key]:
                    if (sym, i["date"]) not in dates:
                        continue
                    note = self._ai_note("deal", f"{sym}|{i['date']}")
                    if notifier.send(f"deal|{sym}|{i['label']}|{i['date']}", "filing", msg.deal_alert(sym, icon, i, scope[sym], note), sym):
                        sent += 1
                        self._note_sent(note)
        return sent

    @staticmethod
    def _note_sent(note) -> None:
        if note:
            from backend.ai import notes
            notes.mark_alerted(note["id"])

    def alert_filings(self, new: list[dict], now: datetime) -> int:
        """One message per important filing (from the last 24 h) on a stock you hold, watch or have in the plan."""
        scope = self._scope()
        since = (now - timedelta(hours=24)).replace(tzinfo=None).isoformat()
        notifier, sent = self.notifier(), 0
        for f in sorted(new, key=lambda f: f["dt"]):
            why = scope.get(f["symbol"])
            if not (why and f["kind"] in filings.IMPORTANT and f["dt"] >= since):
                continue
            note = self._ai_note("filing", f["seq_id"])
            if notifier.send(f"filing|{f['seq_id']}", "filing", msg.filing_alert(f, why, note), f["symbol"]):
                sent += 1
                self._note_sent(note)
        return sent

    @staticmethod
    def refresh_events() -> Optional[int]:
        """NSE results calendar for the alerts; a failure keeps the stored calendar."""
        try:
            return events.refresh_from_nse()
        except Exception as exc:
            logger.warning("Results calendar refresh failed (%s); keeping the stored one", exc)
            return None

    def run_guard(self, as_of: Optional[str] = None) -> dict:
        """The sell rules on the positions you added yourself ("I bought"). Kite holdings are never read. Never raises."""
        st = self.state
        try:
            if as_of is None:
                with get_db() as conn:
                    as_of = conn.execute("SELECT MAX(as_of) FROM scan_runs").fetchone()[0]
            if not guard.positions():
                return {"positions": 0}
            mc, _ = self._market()
            return guard.run(as_of, self._tokens(), st.config.alerts, self.notifier(),
                             lambda h: decorate(h, "guard", None, mc))
        except Exception as exc:
            logger.exception("Position guard failed")
            return {"error": str(exc)}

    def _forming_quotes(self) -> dict:
        """Forming candidates of the alert screens with one Kite quote call: {status, items}.
        items: [{symbol, p, token, ltp, live, vol}] for names with a price."""
        st, cfg = self.state, self.state.config.alerts
        if not st.token_valid():
            return {"status": "no_login", "items": []}
        stocks = {s["symbol"]: s for s in uni.resolve_universe(st.config.ingest)["stocks"]}
        with get_db() as conn:
            as_of = conn.execute("SELECT MAX(as_of) FROM scan_runs").fetchone()[0]
            rows = conn.execute(
                f"SELECT symbol, payload FROM scan_results WHERE as_of = ? AND stage = 'forming' AND screen IN "
                f"({','.join('?' * len(cfg.screens))}) ORDER BY {msg.SCREEN_ORDER_SQL.format(col='screen')}",
                (as_of, *cfg.screens)).fetchall()
        cands = {}                                         # a name on several screens: VCP, else flag
        for r in rows:
            if r["symbol"] in stocks and r["symbol"] not in cands:
                cands[r["symbol"]] = json.loads(r["payload"])
        if not cands:
            return {"status": "no_candidates", "items": []}
        keys = {f"NSE:{stocks[s].get('tradingsymbol', s)}": s for s in cands}
        index_key = f"NSE:{uni.INDEX_NAME}"                 # rides in the same quote call
        try:
            quotes = st.client.quote(list(keys) + [index_key])
        except Exception as exc:
            logger.warning("Intraday quote failed: %s", exc)
            return {"status": "quote_failed", "error": str(exc), "items": []}
        items = []
        for key, sym in keys.items():
            q = quotes.get(key) or {}
            ltp = q.get("last_price")
            if not ltp:
                continue
            ohlc, vol = q.get("ohlc") or {}, q.get("volume") or q.get("volume_traded")
            live = {"ltp": ltp, "open": ohlc.get("open") or ltp, "high": max(ohlc.get("high") or ltp, ltp),
                    "low": min(ohlc.get("low") or ltp, ltp), "volume": vol or 0}
            items.append({"symbol": sym, "p": dict(cands[sym], symbol=sym), "token": stocks[sym]["instrument_token"],
                          "ltp": ltp, "live": live, "vol": vol, "vwap": q.get("average_price"),
                          "day_pct": lv.day_pct(q)})
        iq = quotes.get(index_key) or {}
        prev = (iq.get("ohlc") or {}).get("close")
        index_today = round((iq["last_price"] / prev - 1) * 100, 2) if iq.get("last_price") and prev else None
        return {"status": "ok", "checked": len(keys), "items": items, "index_today": index_today}

    def _market(self, index_today: Optional[float] = None) -> tuple[MarketContext, dict]:
        mc = MarketContext.load(self.state.config.alerts, index_today=index_today)
        return mc, {"size_factor": mc.size_factor, "size_note": mc.size_note}

    @staticmethod
    def _history(token: int) -> tuple[list[dict], Optional[float]]:
        """(daily candles, 50-day average volume)."""
        with get_db() as conn:
            hist = get_candles(conn, token)
        recent = hist[-50:]
        return hist, (sum(c["volume"] for c in recent) / len(recent) if recent else None)

    def run_intraday(self, now: datetime) -> dict:
        """Heads-up when a Forming name trades above its pivot (with a volume verdict), and a
        failed-breakout alert when a crossed name falls back under it."""
        self.last_intraday = now
        st, cfg, today = self.state, self.state.config.alerts, now.date().isoformat()
        res = self._forming_quotes()
        if res["status"] != "ok":
            out = {k: v for k, v in res.items() if k != "items"}
            if res["status"] != "no_login" and cfg.live_guard:
                out["guard"] = self._live_guard(now)        # positions are watched even with no Forming names
            return out
        notifier, sent, failed, held = self.notifier(), 0, 0, 0
        mc, sizing = self._market(res.get("index_today"))
        with get_db() as conn:
            day_flags = itd.flags(conn, today)
        upgraded = 0
        for it in res["items"]:
            sym, p, ltp = it["symbol"], it["p"], it["ltp"]
            fl = day_flags.get(sym, set())
            if ltp > p["pivot"]:
                with get_db() as conn:
                    itd.set_flag(conn, today, sym, "crossed", f"{ltp}")
                key = f"{today}|intraday|{sym}"
                if notifier.already_sent(key):
                    # a 🟡 Wait that turns 🟢 gets one ⬆️ note; nothing else repeats
                    if cfg.entry_checklist and "entry_wait" in fl and "entry_valid" not in fl:
                        upgraded += self._entry_upgrade(it, now, res.get("index_today"), mc, sizing)
                    continue                             # skip the chart work for names already alerted today
                send_ok, t = gate("intraday", p, mc, cfg)
                if not send_ok:
                    held += 1                            # B setup / muted: no heads-up (and so no failed alert)
                    continue
                hist, avg = self._history(it["token"])
                pace = volume_pace(it["vol"], avg, now)
                ck = self._checklist(it, pace, now, res.get("index_today")) if cfg.entry_checklist else None
                stop = self._entry_stop(p)
                png, ctx = decision_for(hist, p, "intraday", cfg, st.config.bases, st.config.vcp,
                                        live=it["live"], **sizing) if hist else (None, None)
                html = msg.intraday_alert(p, ltp, it["vol"], pace, avg, ctx,
                                          volume=itd.volume_verdict(pace, cfg.intraday_volume_mult),
                                          zone_top=p["pivot"] * (1 + cfg.buy_zone_pct),
                                          check_time=cfg.close_check_time if cfg.close_check else "evening",
                                          ck=ck, stop=stop, size=lv.size(ltp, stop, cfg, mc.size_factor)
                                          if ck and ck["verdict"] == "valid" else None, size_note=mc.size_note)
                if notifier.send(key, "intraday", decorate(html, "intraday", t, mc), sym, png):
                    sent += 1
                    if ck and ck["verdict"]:
                        with get_db() as conn:
                            itd.set_flag(conn, today, sym, f"entry_{ck['verdict']}", f"{ltp}|{ck['passed']}")
            elif ("crossed" in fl and ltp < p["pivot"] * (1 - cfg.failed_buffer_pct)
                  and notifier.already_sent(f"{today}|intraday|{sym}")):     # only names you were told about
                with get_db() as conn:
                    new = itd.set_flag(conn, today, sym, "failed", f"{ltp}")
                stop = msg.setup_stop(p)
                if new and notifier.send(f"{today}|failed|{sym}", "intraday",
                                         decorate(msg.failed_alert(p, ltp, it["live"]["high"], stop), "failed", None,
                                                  mc), sym):
                    failed += 1
        guarded = self._live_guard(now, res.get("index_today")) if cfg.live_guard else {}
        return {"status": "ok", "checked": res["checked"], "sent": sent, "failed": failed, "held": held,
                "upgraded": upgraded, "guard": guarded}

    # --- entry checklist and live guard (market hours) ---

    def _entry_stop(self, p: dict) -> float:
        """The setup's stop (VCP / flag), else pivot - max_stop: the same rule the guard starts from."""
        return msg.setup_stop(p) or round(p["pivot"] * (1 - self.state.config.vcp.max_stop), 2)

    def _checklist(self, it: dict, pace: Optional[float], now: datetime, index_pct: Optional[float]) -> dict:
        orh = self.state.live.or_high(it["symbol"], it["token"], now)
        return lv.checklist(it["ltp"], it["p"]["pivot"], self.state.config.alerts, it.get("vwap"), pace,
                            it.get("day_pct"), index_pct, orh)

    def _entry_upgrade(self, it: dict, now: datetime, index_pct: Optional[float], mc, sizing: dict) -> int:
        """One ⬆️ note when a name told 🟡 Wait earlier today now passes all five checks."""
        cfg, today, sym = self.state.config.alerts, now.date().isoformat(), it["symbol"]
        _, avg = self._history(it["token"])
        ck = self._checklist(it, volume_pace(it["vol"], avg, now), now, index_pct)
        if ck["verdict"] != "valid":
            return 0
        with get_db() as conn:
            if not itd.set_flag(conn, today, sym, "entry_valid", f"{it['ltp']}"):
                return 0
        stop = self._entry_stop(it["p"])
        html = msg.entry_upgrade(it["p"], it["ltp"], ck, stop, lv.size(it["ltp"], stop, cfg, mc.size_factor),
                                 mc.size_note)
        return int(self.notifier().send(f"{today}|entry-up|{sym}", "intraday",
                                        decorate(html, "intraday", None, mc), sym))

    def _position_quotes(self, positions: list[dict]) -> dict:
        """{symbol: quote} for the followed positions plus the Nifty 500 ('_index'), one Kite call."""
        stocks = {s["symbol"]: s for s in uni.resolve_universe(self.state.config.ingest)["stocks"]}
        keys = {f"NSE:{(stocks.get(p['symbol']) or {}).get('tradingsymbol', p['symbol'])}": p["symbol"]
                for p in positions}
        index_key = f"NSE:{uni.INDEX_NAME}"
        q = self.state.client.quote(list(keys) + [index_key]) or {}
        out = {sym: dict(q[k], token=(stocks.get(sym) or {}).get("instrument_token")) for k, sym in keys.items()
               if (q.get(k) or {}).get("last_price")}
        out["_index"] = q.get(index_key) or {}
        return out

    def _live_guard(self, now: datetime, index_pct: Optional[float] = None) -> dict:
        """Positions under their stop or under heavy selling: one alert per stock per day, with buttons."""
        cfg, today = self.state.config.alerts, now.date().isoformat()
        positions = guard.positions()
        if not positions:
            return {"positions": 0}
        try:
            quotes = self._position_quotes(positions)
        except Exception as exc:
            logger.warning("Live guard quote failed: %s", exc)
            return {"positions": len(positions), "error": str(exc)[:200]}
        idx = lv.day_pct(quotes.get("_index") or {}) if index_pct is None else index_pct
        notifier, sent = self.notifier(), 0
        with get_db() as conn:
            day_flags = itd.flags(conn, today)
        for pos in positions:
            q, sym = quotes.get(pos["symbol"]), pos["symbol"]
            if not q or "g_quiet" in day_flags.get(sym, set()):
                continue
            ltp, dp = q["last_price"], lv.day_pct(q)
            pace = volume_pace(q.get("volume") or q.get("volume_traded"), lv.avg_volume(q.get("token")), now)
            r = lv.r_multiple(pos, ltp)
            buttons = [("📈 Chart", None, f"c:{sym}"), ("🔕 Quiet today", None, f"q:{sym}")]
            for rule in lv.guard_rules(pos, ltp, dp, pace, cfg):
                flag = {"under_stop": "g_under", "heavy": "g_heavy"}[rule]
                with get_db() as conn:
                    if not itd.set_flag(conn, today, sym, flag, f"{ltp}"):
                        continue
                html = (msg.live_stop_alert(pos, ltp, r, pace, cfg.hard_stop, cfg.close_check_time)
                        if rule == "under_stop" else msg.heavy_selling_alert(pos, ltp, dp, pace, idx, r))
                if notifier.send(f"{today}|live-{rule}|{sym}", "guard", html, sym, buttons=buttons):
                    sent += 1
        return {"positions": len(positions), "sent": sent}

    def _gap_positions(self, now: datetime) -> dict:
        """09:10: positions that open under their stop (a gap the stop can't protect against)."""
        cfg, today = self.state.config.alerts, now.date().isoformat()
        positions = [p for p in guard.positions() if p.get("stop")]
        if not (cfg.live_guard and positions):
            return {"positions": 0}
        quotes = self._position_quotes(positions)
        broken, ok = [], 0
        for pos in positions:
            q = quotes.get(pos["symbol"])
            open_px = ((q or {}).get("ohlc") or {}).get("open") or (q or {}).get("last_price")
            if not open_px:
                continue
            if open_px < pos["stop"]:
                broken.append((pos, open_px))
                with get_db() as conn:
                    itd.set_flag(conn, today, pos["symbol"], "g_gap", f"{open_px}")
            else:
                ok += 1
        sent = bool(broken) and self.notifier().send(f"{today}|gap-positions", "guard",
                                                     msg.gap_positions(broken, ok))
        return {"positions": len(positions), "under": len(broken), "sent": sent}

    def _close_positions(self, now: datetime) -> dict:
        """15:15: positions that were under the stop or sold off heavily today: still under -> exit; back above -> ✅."""
        cfg, today = self.state.config.alerts, now.date().isoformat()
        if not cfg.live_guard:
            return {}
        with get_db() as conn:
            day_flags = itd.flags(conn, today)
        positions = guard.positions()
        if not positions:
            return {}
        quotes = self._position_quotes(positions)
        rows, fine = [], []
        for pos in positions:
            q, fl = quotes.get(pos["symbol"]), day_flags.get(pos["symbol"], set())
            if not q:
                continue
            ltp = q["last_price"]
            if pos.get("stop") and ltp < pos["stop"]:
                rows.append((pos, ltp, "under_stop"))
            elif fl & {"g_under", "g_gap"}:
                rows.append((pos, ltp, "recovered"))
            elif "g_heavy" in fl:
                rows.append((pos, ltp, "heavy"))
            else:
                fine.append(pos["symbol"])
        sent = bool(rows) and self.notifier().send(f"{today}|close-positions", "guard",
                                                   msg.close_positions(rows, fine, cfg.close_check_time))
        return {"flagged": len(rows), "sent": sent}

    def run_close_check(self, now: datetime) -> dict:
        """15:15: holding / weak / extended for every Forming name above its pivot. Up to 3 holding names
        get their own chart; everything else (or everything, when 3 or fewer) is covered too."""
        st, cfg, today, started = self.state, self.state.config.alerts, now.date(), now_ist()
        res = self._forming_quotes()
        if res["status"] == "no_login":
            self.record("close_check", today, "failed", "Kite login missing at the close check", started)
            return {"status": "no_login"}
        if res["status"] == "quote_failed":
            self.not_before["close_check"] = now + timedelta(minutes=2)     # one quick retry before 15:30
            return {"status": "quote_failed"}
        items = []
        for it in res["items"]:
            p, ltp, live = it["p"], it["ltp"], it["live"]
            zone_top = p["pivot"] * (1 + cfg.buy_zone_pct)
            if ltp <= p["pivot"]:
                continue
            hist, avg = self._history(it["token"])
            pace = volume_pace(it["vol"], avg, now)
            rng = itd.day_range(ltp, live["low"], live["high"])
            verdict = itd.close_verdict(ltp, p["pivot"], zone_top, pace, rng, cfg)
            ck = self._checklist(it, pace, now, res.get("index_today")) if cfg.entry_checklist else None
            items.append(dict(it, hist=hist, pace=pace, rng=rng, verdict=verdict, zone_top=zone_top,
                              pivot=p["pivot"], ck=ck))
        mc, sizing = self._market(res.get("index_today"))
        for i in items:
            i["send_ok"], i["tier"] = gate("close_check", i["p"], mc, cfg)
        order = {"holding": 0, "weak": 1, "extended": 2}
        items.sort(key=lambda i: (order[i["verdict"]], i["tier"] == "B", -(i["pace"] or 0)))
        with get_db() as conn:
            for i in items:
                itd.set_flag(conn, today.isoformat(), i["symbol"], f"close_{i['verdict']}",
                             f"{i['ltp']}|{i['pace']}|{i['rng']}")
        notifier, sent = self.notifier(), 0
        eligible = [i for i in items if i["send_ok"]]           # A (or every name when tiering is off / push)
        muted = mc.status == "correction" and cfg.correction_mode == "mute_buys"
        listable = [] if muted else (eligible if cfg.b_alerts == "off" else items)   # B names: summary only
        individual = eligible if len(listable) <= 3 else [i for i in eligible if i["verdict"] == "holding"][:3]
        for i in individual:
            live = dict(i["live"], pace=i["pace"], range=i["rng"], verdict=i["verdict"])
            png, ctx = decision_for(i["hist"], i["p"], "close_check", cfg, st.config.bases, st.config.vcp,
                                    live=live, **sizing) if i["hist"] else (None, None)
            html = msg.close_alert(i["p"], i["verdict"], i["ltp"], i["pace"], i["rng"], i["zone_top"], ctx, i["ck"],
                                   cfg.job_time)
            if notifier.send(f"{today}|close|{i['symbol']}", "close_check", decorate(html, "close_check", i["tier"], mc),
                             i["symbol"], png):
                sent += 1
        rest = {i["symbol"] for i in individual}
        if len(listable) > len(rest & {i["symbol"] for i in listable}) and notifier.send(
                f"{today}|close-summary", "close_check", decorate(msg.close_summary(listable, rest, cfg.close_check_time), "summary", None, mc)):
            sent += 1
        counts = {v: sum(i["verdict"] == v for i in items) for v in order}
        try:
            pos_res = self._close_positions(now)               # positions under the stop / sold off today
        except Exception as exc:
            logger.warning("15:15 positions check failed: %s", exc)
            pos_res = {"error": str(exc)[:200]}
        self.record("close_check", today, "ok", f"above pivot {len(items)}: {counts}; sent {sent}; "
                    f"positions {pos_res}", started)
        return {"status": "ok", "above": len(items), "counts": counts, "sent": sent, "positions": pos_res}

    def run_top_setups(self, now: datetime, force: bool = False) -> dict:
        """After today's evening scan: rank, save the plan, send the Top N (header + one chart each)."""
        st, cfg, today, started = self.state, self.state.config.alerts, now.date(), now_ist()
        with get_db() as conn:
            as_of = conn.execute("SELECT MAX(as_of) FROM scan_runs").fetchone()[0]
        if not force and (as_of != today.isoformat() or "eod" not in self.done_today(today)):
            self.not_before["top_setups"] = now + timedelta(minutes=15)     # wait for the evening scan
            self.record("top_setups", today, "waiting", f"waiting for today's evening scan (latest {as_of})", started)
            return {"status": "waiting"}
        if as_of is None:
            self.record("top_setups", today, "ok", "no scan yet", started)
            return {"status": "no_scan"}
        tokens = self._tokens()
        items = pl.build_plan(as_of, cfg, st.config.vcp, tokens)
        mc, sizing = self._market()
        notifier, sent, charts = self.notifier(), 0, []
        for n, i in enumerate(items, 1):
            with get_db() as conn:
                cs = get_candles(conn, tokens[i["symbol"]]) if i["symbol"] in tokens else []
            png, ctx = decision_for(cs, i["payload"], "near_pivot", cfg, st.config.bases, st.config.vcp,
                                    **sizing) if cs else (None, None)
            if ctx:
                i["stop"], i["size"] = round(ctx["stop"], 2), ctx["size"]
            charts.append((n, i, png, ctx))
        pl.save_plan(as_of, [{k: v for k, v in i.items() if k != "payload"} for i in items])
        if self.summary():                                  # one evening report instead of the separate messages
            from backend.alerts import report
            ok = report.send(notifier, as_of, items if cfg.top_setups else [],
                             msg.market_line(mc.health) if mc.health else None)
            self.record("top_setups", today, "ok", f"{len(items)} setups ranked; evening report "
                        f"{'sent' if ok else 'not sent'}", started)
            return {"status": "ok", "items": len(items), "sent": int(ok)}
        if items and notifier.send(f"{as_of}|top", "plan", decorate(msg.top_setups(items, as_of), "plan", None, mc)):
            sent += 1
            for n, i, png, ctx in charts:
                if png and notifier.send(f"{as_of}|top|{i['symbol']}", "plan", msg.top_setup_caption(n, i, ctx),
                                         i["symbol"], png):
                    sent += 1
        self.record("top_setups", today, "ok", f"{len(items)} setups ranked; sent {sent}", started)
        return {"status": "ok", "items": len(items), "sent": sent}

    def run_breakout_scan(self, as_of: str) -> int:
        """The Trend Breakout scan on the day's candles; in summary style it goes into the evening report, otherwise
        it is sent now as its own (silent) message."""
        from backend.engine import trend_breakout as tb
        res = tb.run_scan(self.state.config, self.state.client)
        if "error" in res:
            logger.info("Trend Breakout scan: %s", res["error"])
            return 0
        if self.summary():
            return 0
        return int(self.notifier().send(f"{res['as_of']}|breakout", "plan", msg.breakout_scan(res), silent=True))

    def run_paper(self) -> int:
        """Paper trading's new sessions; in summary style they go into the evening report, otherwise a silent message."""
        from backend.engine import paper
        res = paper.process(self.state.config)
        for k, a in ((res.get("report") or {}).get("accounts") or {}).items():
            self._trail_alerts(msg.PAPER, f"paper-{k}", a.get("raised") or [])
        if res["status"] != "ok" or not res["sessions"] or self.summary():
            return 0
        rep = res["report"]
        return int(self.notifier().send(f"{rep['as_of']}|paper", "plan", msg.paper_report(rep), silent=True))

    def _trail_alerts(self, where: str, tag: str, raised: list) -> None:
        """E+ · a late trail raised a stop (once per account, stock and day)."""
        for r in raised:
            self.notifier().send(f"{r['day']}|trail|{tag}|{r['symbol']}", "real" if tag == "real" else "plan",
                                 msg.trail_raised(where, r["symbol"], r["stop"], r["best"], r["entry"]))

    def run_day_summary(self, now: datetime) -> dict:
        """🌙 One close-of-day message across the books (alerts.day_summary_time, trading days): Real, Paper,
        Options and the AI, one line each (backend/alerts/daysum.py gathers, msg.day_summary
        writes); key {day}|day-summary, kind "summary" (never held). Recorded once a day in job_runs."""
        from backend.alerts import daysum
        today, started = now.date(), now_ist()
        try:
            text = msg.day_summary(daysum.parts(self.state.config, today.isoformat()), today.isoformat())
            sent = bool(text) and self.notifier().send(f"{today}|day-summary", "summary", text)
        except Exception as exc:
            logger.exception("Day summary failed")
            self.record("day_summary", today, "failed", str(exc)[:200], started)
            return {"status": "failed", "error": str(exc)[:200]}
        note = "sent" if sent else "nothing to report" if not text else "not sent (Telegram off or sent already)"
        self.record("day_summary", today, "ok", note, started)
        return {"status": "ok", "sent": sent, "text": text}

    def run_real_evening(self) -> int:
        """Mark the real account and plan tomorrow's orders (a loud message when there are any)."""
        from backend.engine import real
        res = real.evening(self.state.config)
        self._trail_alerts(msg.REAL, "real", res.get("raised") or [])
        sent = 0
        try:                                               # the read-only fill check, then the "Real vs webhook side" line
            sent += self._real_fills(res.get("as_of") or now_ist()[:10], evening=True)
        except Exception:
            logger.exception("Real fill check (evening) failed")
        if res["status"] != "ok" or not (res["orders"] or res["missed"]):
            return sent
        return sent + int(self.notifier().send(f"{res['as_of']}|real-plan", "real", msg.real_plan(res, self.state.config.real.variant)))

    def _real_fills(self, day: str, evening: bool = False) -> int:
        """confirm_from_api (one read-only call; nothing is ever sent from it) and its Telegram lines: the buys it
        confirmed, and in the evening the "Real vs webhook side" line (a flag only). Silent without a key."""
        from backend.engine import real
        cfg = self.state.config
        res = real.confirm_from_api(cfg)
        n, sent = self.notifier(), 0
        if res["status"] == "ok" and cfg.real.confirm_alert:
            if res["confirmed"]:
                sent += int(n.send(f"{day}|real-fills|{'|'.join(res['confirmed'])}", "real", msg.real_fills_checked(res)))
            line = real.fills_line(res) if evening else None
            if line:
                sent += int(n.send(f"{day}|real-fills-line", "real", line))
        return sent

    def run_real_confirm(self, now: datetime) -> dict:
        """real.confirm_time on trading days: the read-only fill check (no order of any kind)."""
        from backend.engine import real
        from backend.broker import tradetron_api as tta
        today, started = now.date(), now_ist()
        if not tta.key():                                  # no API key: with the gate on, the nudge instead (nothing read or sent)
            still = sorted(real.unconfirmed()) if real.gate_on(self.state.config) else []
            if still and self.state.config.real.confirm_alert:
                self.notifier().send(f"{today}|real-nudge", "real", msg.real_confirm_nudge(still))
            self.record("real_confirm", today, "ok", f"no key: {len(still)} unconfirmed", started)
            return {"status": "no key", "still": still}
        self._real_fills(today.isoformat())
        last = real._saved_check()
        self.record("real_confirm", today, "ok", f"{last.get('status')}: {len(last.get('confirmed') or [])} confirmed, "
                    f"{len(last.get('still') or [])} unconfirmed", started)
        return last

    def _gate_alerts(self, day: str, gated: list) -> int:
        """The EXIT gate held an EXIT (the buy was never confirmed): one alert a day per stock, never held."""
        n = self.notifier()
        return sum(int(n.send(f"{day}|real-unconfirmed|{g['symbol']}", "real", msg.real_unconfirmed(g))) for g in gated or [])

    def run_real_preview(self, now: datetime) -> dict:
        """The morning reminder (real.preview_time): today's orders as they go out at send_time, with the skips (the
        results guard runs once more here: the calendar may have changed since the evening)."""
        from backend.engine import real
        today, started, cfg = now.date(), now_ist(), self.state.config
        real.guard_today(cfg, now)
        m = real.morning(cfg, now)
        st = real._state() or {}
        orders = [o for o in st.get("orders", []) if not o.get("sent")]
        sent = False
        if orders:
            sent = self.notifier().send(f"{today}|real-preview", "real",
                                        msg.real_preview(orders, cfg.real.variant, cfg.real.send_time, bool(cfg.real.enabled), real.killed(), m))
        self.record("real_preview", today, "ok", f"{len(orders)} queued · {len(m['skipped'])} skipped · sent {int(bool(sent))}", started)
        return {"orders": len(orders), "skipped": m["skipped"], "sent": bool(sent)}

    def run_real_send(self, now: datetime) -> dict:
        """The morning send: EXITs, then BUYs, to Tradetron (once each)."""
        from backend.engine import real
        today, started = now.date(), now_ist()
        res = real.send_orders(self.state.config, self.state.client, today.isoformat())
        self.record("real_send", today, "ok", f"{res['status']}: sent {len(res['sent'])}, failed {len(res['failed'])}, "
                    f"skipped {len(res.get('skipped') or [])}", started)
        if res["sent"] or res["failed"]:
            self.notifier().send(f"{today}|real-sent", "real", msg.real_sent(res, ampm(now.strftime('%H:%M')), gate=real.gate_on(self.state.config)))
        self._gate_alerts(today.isoformat(), res.get("gated"))
        return res

    def run_real_stops(self, now: datetime) -> dict:
        """Every stop_check_min in market hours: an EXIT for a position at or under its stop."""
        from backend.engine import real
        self.last_real_stops = now
        res = real.watch_stops(self.state.config, self.state.client, now.date().isoformat())
        n = self.notifier()
        for h in res["hits"]:
            n.send(f"{now.date()}|real-stop|{h['symbol']}", "real", msg.real_stop(h, ampm(now.strftime('%H:%M'))))
        self._gate_alerts(now.date().isoformat(), res.get("gated"))
        if res["status"] == "no_login":                   # loud, again every nologin_alarm_min until the login
            slot = (now.hour * 60 + now.minute) // max(1, self.state.config.real.nologin_alarm_min)
            n.send(f"{now.date()}|real-nologin|{slot}", "real", msg.real_nologin(now.strftime('%H:%M')))
        return res

    # --- 🧩 the options book ---

    def run_options_tick(self, now: datetime) -> dict:
        """The options book's minute: range at 9:30, bias on 5-minute closes, MTM and exits while open, the time and
        hard exits, the close snapshot, the day summary (see backend/options/book.py); then the alerts the book recorded
        go to Telegram, each once."""
        from backend.options import book as obook
        self.last_options_tick = now
        try:
            res = obook.tick(self.state.config, self.state.client, now)
        except Exception as exc:
            logger.exception("Options tick failed")
            return {"status": "failed", "error": str(exc)}
        try:
            res["sent"] = self.send_options_alerts(now)
        except Exception:
            logger.exception("Options alerts failed")
        return res

    def send_options_alerts(self, now: datetime) -> int:
        """🧩 Send the alerts the options book recorded today and has not sent yet (the bias and the spread, the exits,
        no trade, the one-liners; the day summary goes into the one 🌙 day summary instead), tagged paper, on the real-trading channel. Each alert
        has a key unique for its day: the notifier's sent_alerts table makes the send once-only across restarts and
        `options_sent` keeps the minute cheap (no DB read for an alert already handled). No Kite call here."""
        from backend.options import book as obook
        st = obook._state()
        if not st or not st.get("alerts"):
            return 0
        today, n, sent = now.date().isoformat(), self.notifier(), 0
        for a in st["alerts"]:
            if a.get("day") != today:
                continue
            key = f"{today}|opt-{a.get('kind')}-{a.get('key') or a.get('kind')}"
            if key in self.options_sent:
                continue
            if a.get("kind") == "summary":                 # 🌙 in the one day summary (run_day_summary); the row stays
                self.options_sent.add(key)
                continue
            ok = n.send(key, "real", msg.options_alert(a))
            if ok or not getattr(n, "ready", True) or getattr(n, "already_sent", lambda k: True)(key):
                self.options_sent.add(key)              # sent, held, or not sendable at all: never again this day
            sent += int(bool(ok))
        if len(self.options_sent) > 500:
            self.options_sent = {k for k in self.options_sent if k.startswith(today)}
        return sent

    def _rerun_breakout(self) -> None:
        """Re-run the Trend Breakout backtest each evening so its results run to the latest day."""
        from backend.engine import trend_breakout as tb
        if tb.saved() is None or tb.status()["state"] != "idle":
            return
        # (a run made before a rule change, e.g. E -> E+, is "stale" and is simply replaced by this one)
        threading.Thread(target=tb.run, args=(self.state.config,), name="breakout-daily", daemon=True).start()

    def run_morning_plan(self, now: datetime) -> dict:
        today, started = now.date(), now_ist()
        plan_as_of, items = pl.latest_plan(before=today.isoformat())
        mc, _ = self._market()
        sent = self.notifier().send(f"{today}|morning", "plan",
                                    decorate(msg.morning_plan(items, plan_as_of or "no", today.isoformat(),
                                                     self.state.config.alerts.intraday_start,
                                                     self.state.config.alerts.close_check_time), "plan",
                                             None, mc)) if plan_as_of else False
        self.record("morning_plan", today, "ok", f"{len(items)} triggers; sent {sent}" if plan_as_of
                    else "no saved plan yet", started)
        return {"status": "ok", "items": len(items), "sent": sent}

    def run_gap_check(self, now: datetime) -> dict:
        """After the pre-open session: how each plan name opened (one Kite quote call)."""
        st, today, started = self.state, now.date(), now_ist()
        plan_as_of, items = pl.latest_plan(before=today.isoformat())
        if not items and not guard.positions():
            self.record("gap_check", today, "ok", "no plan", started)
            return {"status": "no_plan"}
        if not st.token_valid():
            self.record("gap_check", today, "failed", "Kite login missing at the opening check", started)
            return {"status": "no_login"}
        try:
            pos_res = self._gap_positions(now)                 # positions opening under their stop
        except Exception as exc:
            logger.warning("Opening check (positions) failed: %s", exc)
            pos_res = {"error": str(exc)[:200]}
        if not items:
            self.record("gap_check", today, "ok", f"no plan; positions {pos_res}", started)
            return {"status": "no_plan", "positions": pos_res}
        stocks = {s["symbol"]: s for s in uni.resolve_universe(st.config.ingest)["stocks"]}
        keys = {f"NSE:{stocks[i['symbol']].get('tradingsymbol', i['symbol'])}": i for i in items
                if i["symbol"] in stocks}
        try:
            quotes = st.client.quote(list(keys))
        except Exception as exc:
            logger.warning("Opening check quote failed: %s", exc)
            self.not_before["gap_check"] = now + timedelta(minutes=2)
            return {"status": "quote_failed"}
        results = []
        for key, i in keys.items():
            q = quotes.get(key) or {}
            open_px = (q.get("ohlc") or {}).get("open") or q.get("last_price")
            verdict = pl.gap_verdict(open_px, i) if open_px else None
            if verdict:
                results.append((i, open_px, verdict))
        sent = bool(results) and self.notifier().send(f"{today}|gap", "plan", msg.gap_check(results))
        self.record("gap_check", today, "ok", f"{len(results)} of {len(keys)} worth a note; sent {sent}; "
                    f"positions {pos_res}", started)
        return {"status": "ok", "notes": len(results), "sent": sent, "positions": pos_res}

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
