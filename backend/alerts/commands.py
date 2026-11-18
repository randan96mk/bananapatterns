"""Telegram bot commands, read by long-polling getUpdates in a background thread.

  /login   fresh one-time Kite login link
  /status  is the Kite session OK, until when
  skip SYM / unskip SYM / hold   the morning dry run: skip one planned real BUY for today, send it after all,
                                 or hold every BUY (EXITs always go out); also with a slash; refused after send_until
  full SYM                       🤖 AI view: the rule size restored for one BUY this morning (size-by-score, locked)
  filled SYM / unfilled SYM / yes   💰 Real fill confirmation: filled confirms a booked BUY (its stop is then watched
                                 and may send an EXIT); unfilled asks first and removes it from tracking on `yes`
                                 within 10 min (nothing is ever sent by either)
  fut skip SYM / fut unskip SYM / fut hold   the same for the futures book's plan (paper fill and real send)
  opt / opt close / opt hold   the options book: today's status and the open spread; close the spread now; no new
                               entries today
  ai / ai stop / ai start      🤖 the local AI: one status line; the full stop (unload, empty the queue); clear it
  sugg / accept N / yes N / dismiss N [wrong|knew|later] / why N   🤖 suggestions: the open list; accept #N (exit and
                               hold need `yes N` within 10 min; a skip goes straight through); dismiss with a reason;
                               the evidence of #N. Nothing runs without your reply
  ask <question>               🤖 ask the app: the day model answers from one read-only tool ("thinking…" first)
  tips / tip N / take N [lots|half] / skip N / exit N / trail N <px> / pause N / resume N / tips channels
                               💡 Tips: the status line, a hint's card, take it into the Tips paper book (2 = lots),
                               skip it (still scored), close it, move its stop, pause / resume it, the channels read

Only messages from the configured chat_id are answered; anyone else is ignored.
Commands older than STALE_SEC (sent while the app was off) are skipped. The poller
runs only once a chat_id is set, so "Detect chat ID" still sees the first messages.
"""

import threading
import uuid
from datetime import datetime, timedelta
from typing import Callable, Optional

from backend.alerts import messages as msg
from backend.alerts.dispatch import Notifier
from backend.alerts.login import send_login_link, session_ok
from backend.alerts.telegram import TelegramClient
from backend.config import IST
from backend.database.models import get_candles, get_db, now_ist
from backend.kite_api import auth
from backend.timefmt import ampm, ampm_date
from backend.logging_config.logger import setup_logger

logger = setup_logger("bot")

STALE_SEC = 120
MORNING_WORDS = ("skip", "unskip", "hold", "full")         # full SYM: 🤖 the rule size restored this morning (AI view)
OPTIONS_WORDS = ("close", "hold")
AI_WORDS = ("stop", "start", "rerun")
FILL_WORDS = ("filled", "unfilled")                         # 💰 Real: confirm a booked BUY's fill / remove one never filled
SUGG_WORDS = ("sugg", "accept", "yes", "dismiss", "why")     # 🤖 suggestions: "accept 3", "dismiss 3 wrong", "why 3"
TIPS_WORDS = ("tip", "take", "skip", "exit", "trail", "pause", "resume")   # 💡 with a hint number: "take 12 2", "skip 12"


class BotCommands:
    def __init__(self, state, clock: Callable[[], datetime] = lambda: datetime.now(IST)):
        self.state = state
        self.clock = clock
        self.offset: Optional[int] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="bot-commands", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        client = TelegramClient()                     # one HTTP client for the life of the poller
        while not self._stop.is_set():
            notifier = Notifier(self.state.config.alerts, client)   # re-read: chat_id may change at runtime
            if not notifier.ready:
                self._stop.wait(30)
                continue
            try:
                self.poll_once(notifier)
            except Exception as exc:
                logger.warning("Bot command poll failed: %s", exc)
                self._stop.wait(15)

    def poll_once(self, notifier: Notifier, timeout: int = 20) -> int:
        """Read new messages / button taps and answer them; returns how many were answered."""
        handled = 0
        for u in notifier.client.get_updates(self.offset, timeout):
            self.offset = u["update_id"] + 1
            if u.get("callback_query"):
                handled += self.handle_button(u["callback_query"], notifier)
                continue
            m = u.get("message") or {}
            parts = (m.get("text") or "").strip().split()
            if str((m.get("chat") or {}).get("id")) != notifier.chat_id or not parts:
                continue
            w0 = parts[0].lstrip("/").split("@")[0].lower()
            if w0 == "tips" or (w0 in TIPS_WORDS and len(parts) > 1 and parts[1].isdigit()):   # 💡 "skip 12" (a number) is Tips
                if self.clock().timestamp() - m.get("date", 0) <= STALE_SEC:
                    handled += int(self._say(notifier, self.tips(w0, parts[1:])))
                continue
            if w0 in FILL_WORDS or (w0 == "yes" and len(parts) == 1):                             # 💰 filled SYM / unfilled SYM / yes
                if self.clock().timestamp() - m.get("date", 0) <= STALE_SEC:
                    handled += int(self._say(notifier, self.fills(w0, parts[1].upper() if len(parts) > 1 else "")))
                continue
            if parts[0].lower() == "fut" and len(parts) > 1 and parts[1].lower() in MORNING_WORDS:   # "fut skip NRL" / "fut hold"
                parts = ["/fut" + parts[1].lower()] + parts[2:]
            if parts[0].lower() in ("opt", "/opt"):                                                # "opt" / "opt close" / "opt hold"
                parts = ["/opt" + (parts[1].lower() if len(parts) > 1 and parts[1].lower() in OPTIONS_WORDS else "")] + parts[2:]
            if parts[0].lower() in ("ai", "/ai"):                                                  # "ai" / "ai stop" / "ai start"
                parts = ["/ai" + (parts[1].lower() if len(parts) > 1 and parts[1].lower() in AI_WORDS else "")] + parts[2:]
            if parts[0].lower() in MORNING_WORDS:          # the reminder's reply: "skip NRL" / "hold", no slash needed
                parts[0] = "/" + parts[0].lower()
            if parts[0].lstrip("/").lower() in SUGG_WORDS:                                         # 🤖 suggestions: "accept 3", "dismiss 3 wrong"
                parts = ["/sugg" + parts[0].lstrip("/").lower(), " ".join(parts[1:])] if len(parts) > 1 else ["/sugg" + parts[0].lstrip("/").lower()]
            if parts[0].lower() in ("ask", "/ask"):                                                # 🤖 ask: the words, as typed
                if self.clock().timestamp() - m.get("date", 0) <= STALE_SEC:
                    handled += int(self.ask(" ".join(parts[1:]), notifier))
                continue
            if not parts[0].startswith("/"):
                continue
            if self.clock().timestamp() - m.get("date", 0) > STALE_SEC:
                continue
            arg = parts[1].upper() if len(parts) > 1 else ""
            if self.handle(parts[0].split("@")[0].lower(), notifier, arg):
                handled += 1
        return handled

    def _say(self, notifier: Notifier, html: str) -> bool:
        return notifier.send(f"bot|{uuid.uuid4().hex}", "bot", html)

    def handle_button(self, cq: dict, notifier: Notifier) -> bool:
        """📌 Watch / 🔕 Mute / 📈 Chart / ✅ I bought / 🔕 Quiet today under an alert (own chat only)."""
        chat = str(((cq.get("message") or {}).get("chat") or {}).get("id"))
        if chat != notifier.chat_id:
            return False
        action, _, sym = (cq.get("data") or "").partition(":")
        toast = {"w": self.watch, "m": self.mute, "c": self.chart, "b": self.bought, "q": self.quiet,
                 "r": self.report}.get(
            action, lambda s, n: "")(
            sym, notifier)
        try:
            notifier.client.answer_callback(cq["id"], toast)
        except Exception as exc:                          # the tap itself already did its work
            logger.info("answerCallbackQuery failed: %s", exc)
        return bool(toast)

    # --- actions (shared by buttons and commands); each returns a short confirmation ---

    def watch(self, sym: str, notifier: Notifier) -> str:
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO watchlist (symbol, added_at) VALUES (?, ?)", (sym, now_ist()))
            conn.execute("DELETE FROM muted WHERE symbol = ?", (sym,))
        return f"📌 Watching {sym}: its alerts come even as a B setup."

    def mute(self, sym: str, notifier: Notifier) -> str:
        days = self.state.config.alerts.mute_days
        until = (self.clock().date() + timedelta(days=days)).isoformat()
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO muted (symbol, until) VALUES (?, ?)", (sym, until))
            conn.execute("DELETE FROM watchlist WHERE symbol = ?", (sym,))
        return f"🔕 Muted {sym} until {until} (sell-side alerts still come)."

    def quiet(self, sym: str, notifier: Notifier) -> str:
        """🔕 Quiet today (live guard alerts): no more live alerts on this stock today."""
        from backend.alerts import intraday as itd
        with get_db() as conn:
            itd.set_flag(conn, self.clock().date().isoformat(), sym, "g_quiet")
        return f"🔕 No more live alerts on {sym} today (the evening guard still runs)."

    def chart(self, sym: str, notifier: Notifier) -> str:
        from backend.alerts.preview import alert_chart
        try:
            png, kind = alert_chart(self.state.config, sym)
        except (LookupError, ValueError) as exc:
            return f"No chart: {exc}"
        if not png:
            return f"No base to annotate for {sym}."
        notifier.send(f"bot|{uuid.uuid4().hex}", "plan", f"📈 <b>{msg.escape(sym)}</b> · {kind.replace('_', ' ')}",
                      sym, png)
        return f"📈 Chart for {sym} sent."

    def report(self, arg: str, notifier: Notifier) -> str:
        """The evening report's buttons: r:b:<date> breakout charts, r:t:<date> top setup charts, r:d:<date> details."""
        from backend.alerts import report
        what, _, as_of = arg.partition(":")
        try:
            return report.tap(what, as_of, notifier, self.state.config) or "Unknown button"
        except Exception as exc:
            logger.exception("Report button %s failed", arg)
            return f"Couldn't send that: {exc}"

    def bought(self, sym: str, notifier: Notifier) -> str:
        from backend.alerts import guard
        from backend.alerts.preview import resolve_stock
        try:
            with get_db() as conn:
                row, _ = resolve_stock(conn, sym)
                last = get_candles(conn, row["instrument_token"], limit=1)
        except LookupError as exc:
            return str(exc)
        if not last:
            return f"No price stored for {sym}."
        p = guard.add_manual(sym, last[-1]["close"], self.state.config.vcp)
        self._say(notifier, f"✅ Following <b>{msg.escape(sym)}</b> from {msg.rs(p['avg_price'])}, stop "
                            f"{msg.rs(p['stop'])} ({msg.escape(p['stop_source'])}). Change the stop on the dashboard; "
                            "the evening guard checks your sell rules.")
        return f"✅ Following {sym}."

    def morning(self, command: str, arg: str, notifier: Notifier) -> str:
        """skip SYM / unskip SYM / hold: today's real plan changed for this morning only, then listed again as the
        9:00 reminder does. Refused after send_until (the orders have gone out, or the window is over)."""
        from backend.alerts.jobs import hm
        from backend.engine import real
        cfg, now = self.state.config, self.clock()
        if now.time() >= hm(cfg.real.send_until):
            return f"Too late: today's orders went out at {ampm(cfg.real.send_time)} (the window closed at {ampm(cfg.real.send_until)})."
        if command in ("/skip", "/unskip", "/full") and not arg:
            return f"Which stock? {command} SYMBOL"
        try:
            if command == "/full":
                m = real.full_size(cfg, arg, now)
            elif command == "/hold":
                m = real.hold_today(cfg, "hold all (Telegram)", now)
            else:
                m = real.skip_order(cfg, arg, command == "/skip", "Telegram", now)
        except ValueError as exc:
            return f"Not changed: {msg.escape(str(exc))}"
        orders = [o for o in (real._state() or {}).get("orders", []) if not o.get("sent")]
        head = {"/skip": f"⏸ {msg.escape(arg)} skipped today.", "/unskip": f"▶️ {msg.escape(arg)} goes out again.",
                "/hold": "⏸ Every BUY held today; the EXITs still go out.",
                "/full": f"↩️ {msg.escape(arg)} goes out at the rule size today."}[command]
        return head + "\n" + msg.real_preview(orders, cfg.real.variant, cfg.real.send_time, bool(cfg.real.enabled), real.killed(), m)

    def fills(self, word: str, arg: str) -> str:
        """💰 filled SYM: the booked BUY confirmed (by you) · unfilled SYM: asks first, `yes` within 10 min removes it
        from tracking (cash back, charges reversed) · a bare `yes` with nothing waiting says so. The same functions as
        the Real page's buttons; nothing is ever sent."""
        import json
        from backend.database.models import get_setting, set_setting
        from backend.engine import real
        cfg, now = self.state.config, self.clock()
        if word in ("filled", "unfilled") and not arg:
            return f"Which stock? {word} SYMBOL"
        if word == "filled":
            try:
                real.confirm_position(cfg, arg, "manual", "Telegram")
            except ValueError as exc:
                return f"Not changed: {msg.escape(str(exc))}"
            return (f"✓ <b>{msg.escape(arg)}</b> confirmed filled · its stop is watched from now on and may send an EXIT · "
                    "nothing was sent")
        if word == "unfilled":
            pos = (real._state() or {}).get("positions", {}).get(arg)
            if not pos:
                return f"Not changed: no open position in {msg.escape(arg)}"
            with get_db() as conn:
                set_setting(conn, "real.unfilled_ask", json.dumps({"symbol": arg, "at": now.timestamp()}))
            return (f"Remove <b>{msg.escape(arg)}</b> from tracking as never filled? {pos['qty']} @ {msg.rs(pos['px'])} · the cash "
                    f"gets the cost back and the charges are reversed · no order is sent.\nReply <b>yes</b> within 10 min.")
        with get_db() as conn:
            raw = get_setting(conn, "real.unfilled_ask")
            set_setting(conn, "real.unfilled_ask", "")
        ask = json.loads(raw) if raw else None
        if not ask:
            return "Nothing is waiting for a yes."
        if now.timestamp() - float(ask["at"]) > real.UNFILLED_TTL_S:
            return f"Too late: the question about {msg.escape(ask['symbol'])} expired after 10 min · send unfilled {msg.escape(ask['symbol'])} again."
        try:
            res = real.remove_position(cfg, ask["symbol"], "never filled (Telegram)")
        except ValueError as exc:
            return f"Not changed: {msg.escape(str(exc))}"
        return (f"✗ <b>{msg.escape(ask['symbol'])}</b> removed from tracking · never filled · cash {msg.rs(res['cash'])} · "
                "nothing was sent")

    def futures(self, command: str, arg: str) -> str:
        """fut skip SYM / fut unskip SYM / fut hold: the futures plan changed for this morning only (the paper fill
        and the real send of that side both honour it), then the dry run listed again."""
        from backend.alerts.jobs import hm
        from backend.futures import book
        cfg, now = self.state.config, self.clock()
        if now.time() >= hm(cfg.real.send_until):
            return f"Too late: today's futures orders went out at {ampm(cfg.real.send_time)} (the window closed at {ampm(cfg.real.send_until)})."
        if command in ("/futskip", "/futunskip") and not arg:
            return f"Which stock? fut {command[4:]} SYMBOL"
        try:
            if command == "/futhold":
                m = book.hold_today(cfg, "hold all (Telegram)", now)
            else:
                m = book.skip_order(cfg, arg, command == "/futskip", "Telegram", now)
        except ValueError as exc:
            return f"Not changed: {msg.escape(str(exc))}"
        head = {"/futskip": f"⏸ {msg.escape(arg)} skipped today (futures).", "/futunskip": f"▶️ {msg.escape(arg)} goes out again (futures).",
                "/futhold": "⏸ Every futures entry held today; the EXITs still go out."}[command]
        return head + "\n" + msg.futures_preview(m, cfg.real.send_time)

    def options(self, command: str) -> str:
        """opt: today's status and the open spread · opt close: close the open spread now (the result) · opt hold: no
        new entries today (an open spread keeps its exits). Paper only, like the book."""
        from backend.options import book
        cfg, now = self.state.config, self.clock()
        if command == "/optclose":
            try:
                res = book.close_now(cfg, self.state.client, now)
            except ValueError as exc:
                return f"Not changed: {msg.escape(str(exc))}."
            return msg.options_exit("closed by you", res["trade"])
        if command == "/opthold":
            book.hold_today(cfg, now, "hold today (Telegram)")
            return "⏸ No new options entries today; an open spread keeps its exits.\n" + msg.options_status(book.summary(cfg, now))
        return msg.options_status(book.summary(cfg, now))

    def ai(self, command: str) -> str:
        """ai: one status line · ai stop: the full stop (every model unloaded, the queue emptied; the RAM freed) ·
        ai start: the stop cleared. The model never acts on anything; these only start and stop it."""
        from backend.ai import control
        cfg, now = self.state.config, self.clock()
        if command == "/aistop":
            return msg.ai_stopped(control.full_stop())
        if command == "/airerun":
            sch = getattr(self.state, "scheduler", None)
            if not cfg.ai.enabled or sch is None:
                return "🤖 AI is off: nothing to re-run."
            out = sch.rerun_night(now)
            n = sum(int((v or {}).get("queued", 0) or 0) for v in out.values() if isinstance(v, dict))
            return f"🔁 Night batch queued again · {n} job(s) · the guard decides when they run.\n" + msg.ai_status(control.summary(cfg, now))
        if command == "/aistart":
            control.start()
            return "▶️ AI full stop cleared.\n" + msg.ai_status(control.summary(cfg, now))
        return msg.ai_status(control.summary(cfg, now))

    def ask(self, question: str, notifier: Notifier) -> bool:
        """🤖 ask <question>: "thinking…" at once, then the answer the day model wrote from one read-only tool (or the
        refusal); nothing is changed by an ask. Waits ask.TIMEOUT_S at most."""
        from backend.ai import ask as aask
        if not question.strip():
            return self._say(notifier, "Ask what? <b>ask</b> why did we skip NMDC on thursday")
        if not (self.state.config.ai.enabled and self.state.config.ai.ask):
            return self._say(notifier, "🤖 Ask is off (System › AI).")
        self._say(notifier, msg.ai_thinking(question))
        try:
            row = aask.ask(self.state.config, question, "telegram")
        except ValueError as exc:
            return self._say(notifier, f"Not asked: {msg.escape(str(exc))}")
        return self._say(notifier, msg.ai_ask_reply(row))

    def suggestions(self, command: str, arg: str) -> str:
        """🤖 sugg: the open list · accept N: a skip runs at once (reversible), exit / hold answer with the confirm text
        and wait for `yes N` (10 min) · yes N · dismiss N [wrong|knew|later] · why N: the evidence. Every action an
        accept runs is the same function the page's button calls; the model itself never acts."""
        from backend.ai import suggest
        cfg, now = self.state.config, self.clock()
        words = arg.split()
        if command == "/suggsugg":
            return msg.ai_suggestion_list(suggest.open_suggestions())
        if not words or not words[0].isdigit():
            return f"Which one? {command[5:]} N (the number from the list; <b>sugg</b> lists them)"
        sid = int(words[0])
        s = suggest.get(sid)
        if not s:
            return f"No suggestion #{sid}."
        try:
            if command == "/suggwhy":
                return msg.ai_suggestion_detail(s)
            if command == "/suggdismiss":
                reason = words[1].lower() if len(words) > 1 else "none"
                suggest.dismiss(sid, reason, who="telegram")
                return f"✗ #{sid} dismissed · {reason} · {msg.escape(s['title'])}"
            if command == "/suggaccept":
                step, row = suggest.telegram_accept(sid, cfg, now)
                return msg.ai_accept_confirm(row) if step == "confirm" else msg.ai_accepted(row)
            return msg.ai_accepted(suggest.telegram_yes(sid, cfg, now))
        except (suggest.SuggestionState, ValueError) as exc:
            return f"Not changed: {msg.escape(str(exc))}"

    def tips(self, word: str, args: list) -> str:
        """💡 tips · tips channels · tip N · take N [lots|half] · skip N · exit N · trail N <px> · pause N · resume N. The
        same functions the Tips page's buttons call; paper only."""
        from backend.tips import book as tb
        from backend.tips import reader as rd
        from backend.tips import track as tt
        cfg, now = self.state.config, self.clock()
        if word == "tips":
            if args and args[0].lower() == "channels":
                return msg.tips_channels(rd.channels_of(cfg), rd.status())
            return msg.tips_status(tt.summary(cfg))
        hid = int(args[0])
        rest = [a.lower() for a in args[1:]]
        try:
            if word == "tip":
                h = tb.load(hid)
                if not h:
                    return f"No hint #{hid}."
                return msg.tips_card({**tt.hint_view(h), "pos": h.get("pos")})
            if word == "take":
                half = "half" in rest or "½" in rest
                lots = next((float(a) for a in rest if a.replace(".", "", 1).isdigit()), None)
                h = tb.take(cfg, hid, lots=lots, half=half, who="Telegram", now=now, notify=False)
                return msg.tips_taken(h, now)
            if word == "skip":
                tb.skip(cfg, hid, who="Telegram", now=now)
                return f"⏭ #{hid} skipped · still scored for the channel's record"
            if word == "exit":
                h = tb.exit_now(cfg, hid, who="Telegram", now=now)
                pos = h.get("pos") or {}
                if pos.get("entry_px") is None:
                    return f"✖ #{hid} take cancelled · nothing was filled"
                return msg.tips_line(hid, f"closed by you at {pos.get('exit_px'):g} · {msg.inr(pos.get('realised'), 0, True)} total", now)
            if word == "trail":
                if not rest:
                    return f"Trail to what? trail {hid} &lt;price&gt;"
                tb.trail(cfg, hid, float(rest[0]), who="Telegram", now=now)
                return msg.tips_line(hid, f"stop moved to {rest[0]} · trail on", now)
            if word in ("pause", "resume"):
                tb.pause(cfg, hid, word == "pause", who="Telegram", now=now)
                return msg.tips_line(hid, "paused · no entry until you resume" if word == "pause" else "resumed", now)
        except tb.TipsError as exc:
            return f"Not changed: {msg.escape(str(exc))}"
        except ValueError as exc:
            return f"Not changed: {msg.escape(str(exc))}"
        return ""

    def handle(self, command: str, notifier: Notifier, arg: str = "") -> bool:
        st, now = self.state, self.clock()
        if command in ("/ai", "/aistop", "/aistart", "/airerun"):
            return self._say(notifier, self.ai(command))
        if command.startswith("/sugg"):                                               # 🤖 suggestions
            return self._say(notifier, self.suggestions(command, arg))
        if command in ("/skip", "/unskip", "/hold", "/full"):
            return self._say(notifier, self.morning(command, arg, notifier))
        if command in ("/futskip", "/futunskip", "/futhold"):
            return self._say(notifier, self.futures(command, arg))
        if command in ("/opt", "/optclose", "/opthold"):
            return self._say(notifier, self.options(command))
        if command == "/login":
            if send_login_link(st, notifier, "requested", f"bot-login|{uuid.uuid4().hex}", now):
                return True
            return self._say(notifier, msg.system_alert("Can't make a login link: KITE_API_KEY / KITE_API_SECRET not set."))
        if command == "/status":
            if session_ok(st):
                with get_db() as conn:
                    _, issued = auth.load_token(conn)
                until = f" until {ampm_date(auth.expiry_for(issued))} IST" if issued and st.token_source == "db" else ""
                text = f"✅ Kite session OK{until}."
            else:
                text = "🔑 No Kite session. Send /login for a link."
            return self._say(notifier, text)
        if command == "/top":
            from backend.alerts import plan as pl
            as_of, items = pl.latest_plan()
            return self._say(notifier, msg.top_setups(items, as_of) if items else "No plan yet: it comes after the "
                                                                                  "evening scan.")
        if command in ("/watch", "/unwatch", "/mute", "/chart") and arg:
            if command == "/unwatch":
                with get_db() as conn:
                    conn.execute("DELETE FROM watchlist WHERE symbol = ?", (arg,))
                return self._say(notifier, f"Stopped watching {msg.escape(arg)}.")
            text = {"/watch": self.watch, "/mute": self.mute, "/chart": self.chart}[command](arg, notifier)
            return command == "/chart" or self._say(notifier, msg.escape(text))
        if command == "/watch":
            with get_db() as conn:
                names = [r[0] for r in conn.execute("SELECT symbol FROM watchlist ORDER BY symbol")]
            return self._say(notifier, "📌 Watching: " + (", ".join(names) if names else "nothing yet"))
        if command in ("/start", "/help"):
            return self._say(notifier, "Commands: /login · /status · /top (next session's plan) · /watch [SYM] · "
                                       "/unwatch SYM · /mute SYM · /chart SYM · skip SYM / unskip SYM / hold / full SYM (today's real "
                                       "BUYs, before the send) · filled SYM / unfilled SYM (a real BUY's fill: confirm it, or remove it with yes) · fut skip SYM / fut hold (the futures plan) · opt / opt close / opt hold (the options "
                                       "book) · ai / ai stop / ai start / ai rerun (the local AI) · sugg / accept N / yes N / dismiss N [wrong|knew|later] / why N (the AI's suggestions) · ask &lt;question&gt; (ask the app) · tips / tip N / take N [lots|half] / skip N / exit N / trail N &lt;px&gt; / pause N / resume N / tips channels (💡 Tips). Buttons under alerts: Watch, Mute, Chart, I bought.")
        return False
