"""Telegram bot commands, read by long-polling getUpdates in a background thread.

  /login   fresh one-time Kite login link
  /status  is the Kite session OK, until when

Only messages from the configured chat_id are answered; anyone else is ignored.
Commands older than STALE_SEC (sent while the app was off) are skipped. The poller
runs only once a chat_id is set, so "Detect chat ID" still sees the first messages.
"""

import threading
import uuid
from datetime import datetime
from typing import Callable, Optional

from backend.alerts import messages as msg
from backend.alerts.dispatch import Notifier
from backend.alerts.login import send_login_link, session_ok
from backend.alerts.telegram import TelegramClient
from backend.config import IST
from backend.database.models import get_db
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
        """Read new messages and answer the commands; returns how many were answered."""
        handled = 0
        for u in notifier.client.get_updates(self.offset, timeout):
            self.offset = u["update_id"] + 1
            m = u.get("message") or {}
            text = (m.get("text") or "").strip().split("@")[0].split(" ")[0].lower()
            if str((m.get("chat") or {}).get("id")) != notifier.chat_id or not text.startswith("/"):
                continue
            if self.clock().timestamp() - m.get("date", 0) > STALE_SEC:
                continue
            if self.handle(text, notifier):
                handled += 1
        return handled

    def handle(self, command: str, notifier: Notifier) -> bool:
        st, now = self.state, self.clock()
        if command == "/login":
            if send_login_link(st, notifier, "requested", f"bot-login|{uuid.uuid4().hex}", now):
                return True
            return notifier.send(f"bot|{uuid.uuid4().hex}", "bot",
                                 msg.system_alert("Can't make a login link: KITE_API_KEY / KITE_API_SECRET not set."))
        if command == "/status":
            if session_ok(st):
                with get_db() as conn:
                    _, issued = auth.load_token(conn)
                until = f" until {auth.expiry_for(issued):%H:%M %d %b}" if issued and st.token_source == "db" else ""
                text = f"✅ Kite session OK{until}."
            else:
                text = "🔑 No Kite session. Send /login for a link."
            return notifier.send(f"bot|{uuid.uuid4().hex}", "bot", text)
        if command in ("/start", "/help"):
            return notifier.send(f"bot|{uuid.uuid4().hex}", "bot",
                                 "Commands: /login (Kite login link) · /status (Kite session)")
        return False
