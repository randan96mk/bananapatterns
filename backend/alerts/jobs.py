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
    if trading and cfg.top_setups and ready("top_setups") and hm(cfg.top_setups_time) <= t < time(23, 0):
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
        self.last_holdings: Optional[datetime] = None          # live guard: last Kite holdings re-read
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
                        self.last_intraday, self.not_before, self.last_login_check, self.last_filings)
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
        sent = dispatch_scan(as_of, st.config.alerts, st.config.bases, notifier, self._tokens(),
                             st.config.screen.fresh_sessions, vcp_cfg=st.config.vcp)
        if st.config.alerts.position_guard:
            sent["guard"] = self.run_guard(as_of)
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
        """Sync your Kite holdings (read-only) and check the sell rules. Never raises."""
        st = self.state
        try:
            if as_of is None:
                with get_db() as conn:
                    as_of = conn.execute("SELECT MAX(as_of) FROM scan_runs").fetchone()[0]
            changes = guard.sync(guard.kite_holdings(st.client), st.config.vcp)
            mc, _ = self._market()
            res = guard.run(as_of, self._tokens(), st.config.alerts, self.notifier(),
                            lambda h: decorate(h, "guard", None, mc))
            return dict(res, **changes)
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
        cands = {}                                         # a name on several screens: VCP, else cup, else flag
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
            if res["status"] != "no_login" and cfg.live_guard and cfg.position_guard:
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
        guarded = self._live_guard(now, res.get("index_today")) if cfg.live_guard and cfg.position_guard else {}
        return {"status": "ok", "checked": res["checked"], "sent": sent, "failed": failed, "held": held,
                "upgraded": upgraded, "guard": guarded}

    # --- entry checklist and live guard (market hours) ---

    def _entry_stop(self, p: dict) -> float:
        """The setup's stop (VCP / cup / flag), else pivot - max_stop: the same rule the guard starts from."""
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

    def _sync_holdings(self, now: datetime) -> int:
        """Re-read Kite holdings every holdings_refresh_min; announce positions bought since the last read."""
        cfg = self.state.config.alerts
        if self.last_holdings and now - self.last_holdings < timedelta(minutes=cfg.holdings_refresh_min):
            return 0
        self.last_holdings = now
        try:
            changes = guard.sync(guard.kite_holdings(self.state.client), self.state.config.vcp)
        except Exception as exc:
            logger.warning("Live guard: holdings re-read failed: %s", exc)
            return 0
        added = [p for p in guard.positions() if p["symbol"] in changes["added"]]
        if not added:
            return 0
        key = f"{now.date()}|guard-new|{'+'.join(sorted(changes['added']))}"
        return int(self.notifier().send(key, "guard", msg.new_positions_alert(added)))

    def _live_guard(self, now: datetime, index_pct: Optional[float] = None) -> dict:
        """Positions under their stop or under heavy selling: one alert per stock per day, with buttons."""
        cfg, today = self.state.config.alerts, now.date().isoformat()
        new = self._sync_holdings(now)
        positions = guard.positions()
        if not positions:
            return {"positions": 0, "new": new}
        try:
            quotes = self._position_quotes(positions)
        except Exception as exc:
            logger.warning("Live guard quote failed: %s", exc)
            return {"positions": len(positions), "error": str(exc)[:200], "new": new}
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
        return {"positions": len(positions), "sent": sent, "new": new}

    def _gap_positions(self, now: datetime) -> dict:
        """09:10: positions that open under their stop (a gap the stop can't protect against)."""
        cfg, today = self.state.config.alerts, now.date().isoformat()
        positions = [p for p in guard.positions() if p.get("stop")]
        if not (cfg.live_guard and cfg.position_guard and positions):
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
        if not (cfg.live_guard and cfg.position_guard):
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
        if items and notifier.send(f"{as_of}|top", "plan", decorate(msg.top_setups(items, as_of), "plan", None, mc)):
            sent += 1
            for n, i, png, ctx in charts:
                if png and notifier.send(f"{as_of}|top|{i['symbol']}", "plan", msg.top_setup_caption(n, i, ctx),
                                         i["symbol"], png):
                    sent += 1
        self.record("top_setups", today, "ok", f"{len(items)} setups ranked; sent {sent}", started)
        return {"status": "ok", "items": len(items), "sent": sent}

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
