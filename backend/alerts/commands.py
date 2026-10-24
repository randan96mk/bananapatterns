"""Telegram bot commands, read by long-polling getUpdates in a background thread.

  /login   fresh one-time Kite login link
  /status  is the Kite session OK, until when

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
from backend.logging_config.logger import setup_logger

logger = setup_logger("bot")

STALE_SEC = 120


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
            if str((m.get("chat") or {}).get("id")) != notifier.chat_id or not parts or not parts[0].startswith("/"):
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
        """📌 Watch / 🔕 Mute / 📈 Chart / ✅ I bought under an alert (own chat only)."""
        chat = str(((cq.get("message") or {}).get("chat") or {}).get("id"))
        if chat != notifier.chat_id:
            return False
        action, _, sym = (cq.get("data") or "").partition(":")
        toast = {"w": self.watch, "m": self.mute, "c": self.chart, "b": self.bought}.get(action, lambda s, n: "")(
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

    def handle(self, command: str, notifier: Notifier, arg: str = "") -> bool:
        st, now = self.state, self.clock()
        if command == "/login":
            if send_login_link(st, notifier, "requested", f"bot-login|{uuid.uuid4().hex}", now):
                return True
            return self._say(notifier, msg.system_alert("Can't make a login link: KITE_API_KEY / KITE_API_SECRET not set."))
        if command == "/status":
            if session_ok(st):
                with get_db() as conn:
                    _, issued = auth.load_token(conn)
                until = f" until {auth.expiry_for(issued):%H:%M %d %b}" if issued and st.token_source == "db" else ""
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
                                       "/unwatch SYM · /mute SYM · /chart SYM. Buttons under alerts: Watch, Mute, "
                                       "Chart, I bought.")
        return False
