"""Minimal Telegram Bot API client (sendMessage, sendPhoto, getUpdates, getMe).

Plain HTTPS via httpx, paced at 1 message per second (Telegram's per-chat
limit), retried on 429 (honouring retry_after) and 5xx / network errors.
The token comes from TELEGRAM_BOT_TOKEN in .env and is never logged.
"""

import json
import os
import time
from typing import Callable, Optional

import httpx

from backend.kite_api.throttle import TokenBucket
from backend.logging_config.logger import setup_logger

logger = setup_logger("telegram")

API = "https://api.telegram.org/bot{token}/{method}"
MAX_TEXT = 4096
MAX_CAPTION = 1024


class TelegramError(Exception):
    pass


def bot_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def keyboard(buttons: list) -> str:
    """Inline buttons: (label, url) link buttons or (label, None, data) callback buttons. A flat list is one row;
    a list of lists is several rows."""
    rows = buttons if buttons and isinstance(buttons[0], list) else [buttons]
    return json.dumps({"inline_keyboard": [[{"text": b[0], "url": b[1]} if b[1] else
                                            {"text": b[0], "callback_data": b[2][:64]} for b in row] for row in rows]})


class TelegramClient:
    def __init__(self, token: Optional[str] = None, http: Optional[httpx.Client] = None,
                 sleep: Callable[[float], None] = time.sleep, retries: int = 3):
        self.token = bot_token() if token is None else token
        self.http = http or httpx.Client(timeout=30)
        self.sleep = sleep
        self.retries = retries
        self.bucket = TokenBucket(1.0, sleep=sleep)

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _call(self, method: str, data: Optional[dict] = None, files: Optional[dict] = None) -> dict:
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set in .env")
        url = API.format(token=self.token, method=method)
        last = ""
        for attempt in range(self.retries + 1):
            if method.startswith("send"):
                self.bucket.acquire()
            try:
                r = self.http.post(url, data=data, files=files)
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            except (httpx.HTTPError, ValueError) as exc:
                last = f"{type(exc).__name__}: {exc}"
                self.sleep(2 ** attempt)
                continue
            if body.get("ok"):
                return body.get("result")
            desc = body.get("description", f"HTTP {r.status_code}")
            if r.status_code == 429:
                wait = (body.get("parameters") or {}).get("retry_after", 2 ** attempt)
                logger.warning("Telegram rate limited; retrying in %ss", wait)
                self.sleep(float(wait))
                last = desc
                continue
            if r.status_code >= 500:
                last = desc
                self.sleep(2 ** attempt)
                continue
            raise TelegramError(f"{method}: {desc}")       # 400/401/403: not retryable
        raise TelegramError(f"{method}: gave up after retries ({last})")

    def get_me(self) -> dict:
        return self._call("getMe")

    def send_message(self, chat_id: str, html: str, buttons: Optional[list] = None, silent: bool = False) -> dict:
        """buttons: see keyboard(). silent: delivered without a sound (Telegram's disable_notification)."""
        data = {"chat_id": chat_id, "text": html[:MAX_TEXT], "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if buttons:
            data["reply_markup"] = keyboard(buttons)
        if silent:
            data["disable_notification"] = "true"
        return self._call("sendMessage", data)

    def answer_callback(self, callback_id: str, text: str = "") -> dict:
        return self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})

    def get_updates(self, offset: Optional[int] = None, timeout: int = 20) -> list[dict]:
        """New messages to the bot (long poll up to `timeout` s). Pass offset = last update_id + 1."""
        data = {"timeout": str(timeout), "allowed_updates": '["message", "callback_query"]'}
        if offset is not None:
            data["offset"] = str(offset)
        return self._call("getUpdates", data) or []

    def send_photo(self, chat_id: str, png: bytes, caption_html: str, buttons: Optional[list] = None,
                   silent: bool = False) -> dict:
        data = {"chat_id": chat_id, "caption": caption_html[:MAX_CAPTION], "parse_mode": "HTML"}
        if buttons:
            data["reply_markup"] = keyboard(buttons)
        if silent:
            data["disable_notification"] = "true"
        return self._call("sendPhoto", data, files={"photo": ("chart.png", png, "image/png")})

    def send_album(self, chat_id: str, photos: list[tuple[bytes, str]], silent: bool = True) -> list:
        """2-10 charts as one grouped album, each with its own caption (sendMediaGroup takes no buttons)."""
        photos = photos[:10]
        media = [{"type": "photo", "media": f"attach://p{i}", "caption": cap[:MAX_CAPTION], "parse_mode": "HTML"}
                 for i, (_, cap) in enumerate(photos)]
        data = {"chat_id": chat_id, "media": json.dumps(media)}
        if silent:
            data["disable_notification"] = "true"
        return self._call("sendMediaGroup", data,
                          files={f"p{i}": (f"chart{i}.png", png, "image/png") for i, (png, _) in enumerate(photos)})

    def detect_chats(self) -> list[dict]:
        """Chats that have messaged the bot recently (send it any message first)."""
        updates = self._call("getUpdates") or []
        chats = {}
        for u in updates:
            msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
            chat = msg.get("chat")
            if chat:
                name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
                chats[str(chat["id"])] = {"chat_id": str(chat["id"]), "type": chat.get("type"),
                                          "name": name or chat.get("username") or ""}
        return list(chats.values())
