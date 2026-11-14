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
    if (trading and (cfg.top_setups or cfg.alert_style == "summary") and ready("top_setups")
            and hm(cfg.top_setups_time) <= t < time(23, 0)):             # summary style: the evening report
        jobs.append("top_setups")
    if trading and cfg.morning_plan and ready("morning_plan") and hm(cfg.morning_plan_time) <= t < time(12, 0):
        jobs.append("morning_plan")
    if trading and cfg.gap_check and ready("gap_check") and hm(cfg.gap_check_time) <= t < time(10, 0):
        jobs.append("gap_check")
    if now.weekday() == cfg.universe_refresh_weekday and ready("weekly") and t >= time(10, 0):
        jobs.append("weekly")
    if (cfg.filings and FILINGS_START <= t < FILINGS_END
            and (last_filings is None or now - last_filings >= timedelta(minutes=cfg.filings_interval_min))):
        jobs.append("filings")                             # every day: results often come out in the evening
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
        self.last_futures_stops: Optional[datetime] = None  # 📐 futures: the last stop watch on the futures quotes
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
        now = now or self.clock()
        today = now.date()
        jobs = due_jobs(now, self.state.config.alerts, self.done_today(today), hol.is_trading_day(today),
                        self.last_intraday, self.not_before, self.last_login_check, self.last_filings)
        try:                                               # ⚡ Intraday Flow: warm-up, live feed, runs, day close
            from backend.intraday import feed
            if feed.RUNNER is None:
                feed.RUNNER = feed.Runner(self.state)
            feed.RUNNER.tick(now, hol.is_trading_day(today), self._flow_alert)
        except Exception:
            logger.exception("Intraday Flow tick failed")
        from backend.engine import real
        if real.has_account():                             # 💰 only once real trading has started
            jobs += real.due(now, self.state.config, self.done_today(today), hol.is_trading_day(today), self.last_real_stops)
        try:                                               # 📐 the futures book: preview, send, stops, roll
            from backend.futures import book
            if book.has_account():
                jobs += book.due(now, self.state.config, self.done_today(today), hol.is_trading_day(today), self.last_futures_stops)
        except Exception:
            logger.exception("Futures due check failed")
        try:                                               # 🧩 the options book: one tick a minute 9:15-15:40
            from backend.options import book as obook
            jobs += obook.due(now, self.state.config, self.done_today(today), hol.is_trading_day(today), self.last_options_tick)
        except Exception:
            logger.exception("Options due check failed")
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
        return n

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
            return self._run_eod(now, force)

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

        try:                                       # stock futures data: contracts, continuous candles, ban list
            from backend.futures import candles as fcandles
            fut_before = fcandles.status()["to"]
            self.run_futures_data(today)
            self._rerun_futures_backtest(fut_before)
        except Exception:
            logger.exception("Futures data step failed")

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
        try:                                       # 📐 the futures book: mark the day, plan the next open, alerts
            sent["futures"] = self.run_futures_evening()
        except Exception:
            logger.exception("Futures book evening update failed")
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

    def run_futures_data(self, today: Optional[date] = None, force: bool = False) -> dict:
        """The futures data layer's refresh (NFO contracts, continuous candles for the eligible universe, ban list), with
        its own job run record. Needs the Kite login; never fails the evening job."""
        from backend.futures import data as fdata
        today, started = today or datetime.now(IST).date(), now_ist()
        if not self.state.token_valid():
            self.record("futures_data", today, "waiting", "Kite login missing", started)
            return {"status": "waiting_login"}
        try:
            out = fdata.run(self.state.client, self.state.config, today, force=force)
        except Exception as exc:
            self.record("futures_data", today, "failed", str(exc)[:300], started)
            raise
        c, k, b = out.get("contracts") or {}, out.get("candles") or {}, out.get("ban") or {}
        msg = (f"contracts {c.get('contracts', '?')} (as of {c.get('as_of', '?')}); candles {k.get('synced', '?')}/"
               f"{k.get('underlyings', '?')} +{k.get('added', '?')} bars to {k.get('to', '?')}; ban {b.get('date', '?')} "
               f"{len(b.get('symbols') or [])}" + (f"; {'; '.join(out['errors'])}" if out.get("errors") else ""))
        self.record("futures_data", today, out["status"], msg[:500], started)
        return out

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
        if new is None:
            return {"status": "failed", "deals": len(deals), "sent": sent}
        return {"status": "ok", "new": len(new), "deals": len(deals), "sent": sent}

    @staticmethod
    def _scope() -> dict[str, str]:
        """Stocks you hold, watch or have in the plan: {symbol: why}."""
        scope = {p["symbol"]: "you hold it" for p in guard.positions()}
        with get_db() as conn:
            for r in conn.execute("SELECT symbol FROM watchlist"):
                scope.setdefault(r["symbol"], "on your watchlist")
        for i in pl.latest_plan()[1]:
            scope.setdefault(i["symbol"], "in the plan")
        return scope

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
                    if (sym, i["date"]) in dates and notifier.send(
                            f"deal|{sym}|{i['label']}|{i['date']}", "filing", msg.deal_alert(sym, icon, i, scope[sym]),
                            sym):
                        sent += 1
        return sent

    def alert_filings(self, new: list[dict], now: datetime) -> int:
        """One message per important filing (from the last 24 h) on a stock you hold, watch or have in the plan."""
        scope = self._scope()
        since = (now - timedelta(hours=24)).replace(tzinfo=None).isoformat()
        notifier, sent = self.notifier(), 0
        for f in sorted(new, key=lambda f: f["dt"]):
            why = scope.get(f["symbol"])
            if why and f["kind"] in filings.IMPORTANT and f["dt"] >= since and notifier.send(
                    f"filing|{f['seq_id']}", "filing", msg.filing_alert(f, why), f["symbol"]):
                sent += 1
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

    def _flow_alert(self, kind: str, data: dict) -> None:
        """⚡ Intraday Flow: signals, paper fills / exits, the daily loss limit and the day's summary (loud)."""
        d = datetime.now(IST).date()
        if kind == "signal":
            self.notifier().send(f"{d}|flow|{data['symbol']}", "flow", msg.flow_signal(data))
        elif kind == "event":
            p, ev = data["position"], data["event"]
            self.notifier().send(f"{d}|flow-ev|{p.get('id')}|{ev['t']}|{ev.get('why', 'fill')}", "flow", msg.flow_event(p, ev))
        elif kind == "loss_limit":
            self.notifier().send(f"{d}|flow-loss", "flow", msg.flow_loss(data, "signal" if self.state.config.flow.mode == "signals" else "paper"))
        elif kind == "summary":
            self.notifier().send(f"{d}|flow-summary", "flow", msg.flow_summary(data, "signal" if self.state.config.flow.mode == "signals" else "paper"))
        elif kind in ("stopped", "started"):
            at = datetime.now(IST).strftime("%H%M%S")
            self.notifier().send(f"{d}|flow-{kind}|{at}", "flow", msg.flow_switch(kind, data))

    def run_real_evening(self) -> int:
        """Mark the real account and plan tomorrow's orders (a loud message when there are any)."""
        from backend.engine import real
        res = real.evening(self.state.config)
        self._trail_alerts(msg.REAL, "real", res.get("raised") or [])
        if res["status"] != "ok" or not (res["orders"] or res["missed"]):
            return 0
        return int(self.notifier().send(f"{res['as_of']}|real-plan", "real", msg.real_plan(res, self.state.config.real.variant)))

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
            self.notifier().send(f"{today}|real-sent", "real", msg.real_sent(res, ampm(now.strftime('%H:%M'))))
        return res

    def run_real_stops(self, now: datetime) -> dict:
        """Every stop_check_min in market hours: an EXIT for a position at or under its stop."""
        from backend.engine import real
        self.last_real_stops = now
        res = real.watch_stops(self.state.config, self.state.client, now.date().isoformat())
        n = self.notifier()
        for h in res["hits"]:
            n.send(f"{now.date()}|real-stop|{h['symbol']}", "real", msg.real_stop(h, ampm(now.strftime('%H:%M'))))
        if res["status"] == "no_login":                   # loud, again every nologin_alarm_min until the login
            slot = (now.hour * 60 + now.minute) // max(1, self.state.config.real.nologin_alarm_min)
            n.send(f"{now.date()}|real-nologin|{slot}", "real", msg.real_nologin(now.strftime('%H:%M')))
        return res

    # --- 📐 the futures book ---

    def run_futures_evening(self) -> int:
        """Mark the futures book and plan the next open; the plan (tagged per side), the held-name warnings, the
        short side's paper signals, the roll notice at T-3 and the raised stops go to Telegram."""
        from backend.futures import book
        cfg = self.state.config
        res = book.evening(cfg)
        if res["status"] != "ok":
            return 0
        n, day = self.notifier(), res["as_of"]
        sent = 0
        for r in res.get("raised") or []:
            sent += int(n.send(f"{day}|fut-trail|{r['account']}|{r['symbol']}", "real", msg.trail_raised(f"{msg.FUT} · {msg.TAG_OF[r['account']]}", r["symbol"], r["stop"], r["best"], r["entry"])))
        tags = {s: [f"{m} · {s}" for m in ("paper", "real") if getattr(cfg.futures, f"{s}_{m}")] for s in ("long", "short")}
        if any(res["plan"].get(s) for s in ("long", "short")) or res.get("missed"):
            sent += int(n.send(f"{day}|fut-plan", "real", msg.futures_plan(res, tags)))
        w = msg.futures_held_warnings(res.get("warnings") or {})
        if w:
            sent += int(n.send(f"{day}|fut-held", "real", w))
        if cfg.futures.short_paper:
            t = msg.futures_short_signals(res.get("short_signals") or [], res["plan"].get("short", []), res["waiting"].get("short", []))
            if t:
                sent += int(n.send(f"{day}|fut-short", "real", t))
        rd = res.get("roll_day")
        if rd and rd == book.next_session(day):              # T-3: tomorrow is the roll day
            q = book.roll_queue(book._state(), cfg, day)
            if q["items"]:
                sent += int(n.send(f"{day}|fut-roll-notice", "real", msg.futures_roll_notice(q["items"], rd, "notice")))
        return sent

    def run_futures_preview(self, now: datetime) -> dict:
        """The 9:00 futures dry run: lots, margin needed, the skips (the results guard runs once more)."""
        from backend.futures import book
        today, started, cfg = now.date(), now_ist(), self.state.config
        try:
            book.guard_today(cfg, now)
            m = book.morning(cfg, now)
            orders = [o for o in m["orders"] if not o["status"]]
            sent = bool(orders) and self.notifier().send(f"{today}|fut-preview", "real", msg.futures_preview(m, cfg.real.send_time))
            self.record("futures_preview", today, "ok", f"{len(orders)} queued · {len(m['skipped'])} skipped · sent {int(bool(sent))}", started)
            return {"orders": len(orders), "skipped": m["skipped"], "sent": bool(sent)}
        except Exception as exc:
            logger.exception("Futures preview failed")
            self.record("futures_preview", today, "failed", str(exc)[:300], started)
            return {"status": "failed", "error": str(exc)}

    def run_futures_send(self, now: datetime) -> dict:
        """The real sides' futures orders through the sender (the futures sender), once each."""
        from backend.futures import book
        today, started = now.date(), now_ist()
        try:
            res = book.send_orders(self.state.config, self.state.client, today.isoformat())
            self.record("futures_send", today, "ok", f"{res['status']}: sent {len(res['sent'])}, failed {len(res['failed'])}, "
                        f"skipped {len(res.get('skipped') or [])}", started)
            if res["sent"] or res["failed"]:
                self.notifier().send(f"{today}|fut-sent", "real", msg.futures_sent(res, ampm(now.strftime('%H:%M'))))
            return res
        except Exception as exc:
            logger.exception("Futures send failed")
            self.record("futures_send", today, "failed", str(exc)[:300], started)
            return {"status": "failed", "error": str(exc)}

    def run_futures_stops(self, now: datetime) -> dict:
        """Every stop_check_min in market hours: stops on the futures quote; then the daily loss limit and the
        margin warning on the live marks (each alert once a day per account)."""
        from backend.futures import book
        self.last_futures_stops = now
        cfg, n, d = self.state.config, self.notifier(), now.date()
        try:
            res = book.watch_stops(cfg, self.state.client, d.isoformat())
            for h in res["hits"]:
                n.send(f"{d}|fut-stop|{h['account']}|{h['symbol']}", "real", msg.futures_stop(h, ampm(now.strftime('%H:%M'))))
            st = book._state()
            if st:
                for k in book.ACCOUNTS:
                    if not book.switch(cfg, book.side_of(k), book.mode_of(k)):
                        continue
                    g = book.guards(st, cfg, k)
                    if g["daily_loss"]["hit"]:
                        n.send(f"{d}|fut-loss|{k}", "real", msg.futures_daily_loss(book.TAG[k], g["daily_loss"]["used"], g["daily_loss"]["limit"]))
                    if g["margin"]["warn"]:
                        n.send(f"{d}|fut-margin|{k}", "real", msg.futures_margin(book.TAG[k], g["margin"]["pct"], g["margin"]["used"]))
            return res
        except Exception as exc:
            logger.exception("Futures stop watch failed")
            return {"status": "failed", "error": str(exc)}

    def run_futures_roll(self, now: datetime) -> dict:
        """The roll day (T-2) at 9:20: EXIT the near month, then the entry on the next, logged as one roll."""
        from backend.futures import book
        today, started = now.date(), now_ist()
        try:
            res = book.roll(self.state.config, self.state.client, today.isoformat())
            self.record("futures_roll", today, "ok", f"{res['status']}: rolled {len(res.get('rolls') or [])}, exited {len(res.get('exits') or [])}, "
                        f"failed {len(res.get('failed') or [])}", started)
            items = list(res.get("rolls") or [])
            if items or res.get("exits") or res.get("failed"):
                text = msg.futures_roll_notice(items, today.isoformat(), "done")
                for e in res.get("exits") or []:
                    text += f"\n🔴 EXIT <b>{msg.escape(e['symbol'])}</b> {e['side']} · {msg.escape(e['why'])} · net {msg._signed(e['net'])} · <i>{msg.escape(e['tag'])}</i>"
                for f in res.get("failed") or []:
                    text += f"\n⚠️ <b>{msg.escape(f['symbol'])}</b> roll {f['step']} failed: {msg.escape(f['error'])}" + (" · the lot is CLOSED, re-enter by hand" if f.get("exited") else "")
                self.notifier().send(f"{today}|fut-roll", "real", text)
            return res
        except Exception as exc:
            logger.exception("Futures roll failed")
            self.record("futures_roll", today, "failed", str(exc)[:300], started)
            return {"status": "failed", "error": str(exc)}

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
        no trade, the day summary, the one-liners), tagged paper, on the same channel as the futures alerts. Each alert
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

    def _rerun_futures_backtest(self, before_to: Optional[str]) -> None:
        """Re-run Backtest F in the background when the futures candles advanced (a saved run exists, none running)."""
        try:
            from backend.futures import backtest as fb
            from backend.futures import candles as fcandles
            now_to = fcandles.status()["to"]
            run = fb.saved()
            if run is None or fb.status()["state"] != "idle" or not now_to:
                return
            if now_to == before_to and run.get("run", {}).get("to") == now_to:
                return
            threading.Thread(target=fb.run, args=(self.state.config,), name="futures-backtest-daily", daemon=True).start()
        except Exception:
            logger.exception("Backtest F re-run failed")

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
