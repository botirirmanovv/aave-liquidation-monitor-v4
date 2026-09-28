"""Telegram alerting.

Notifications must never take the bot down, so every failure here is logged and
swallowed. Repeated identical messages are suppressed for a cooldown window,
because a flapping RPC endpoint would otherwise generate hundreds of alerts.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import aiohttp

from .config import TelegramConfig

log = logging.getLogger("aave_bot.alerts")

TELEGRAM_API = "https://api.telegram.org"
DEFAULT_COOLDOWN_SECONDS = 300.0
REQUEST_TIMEOUT_SECONDS = 10.0


class Notifier:
    def __init__(self, config: TelegramConfig, prefix: str = "") -> None:
        self.config = config
        self.prefix = prefix
        self._session: aiohttp.ClientSession | None = None
        self._last_sent: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def _suppressed(self, dedup_key: str | None, cooldown: float) -> bool:
        if not dedup_key:
            return False
        now = time.monotonic()
        previous = self._last_sent.get(dedup_key)
        if previous is not None and (now - previous) < cooldown:
            return True
        self._last_sent[dedup_key] = now
        return False

    async def send(
        self,
        text: str,
        *,
        dedup_key: str | None = None,
        cooldown: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> bool:
        """Returns True when a message was actually delivered."""
        if not self.enabled:
            return False
        if self._suppressed(dedup_key, cooldown):
            log.debug("alert suppressed by cooldown: %s", dedup_key)
            return False

        body = f"{self.prefix}{text}" if self.prefix else text
        url = f"{TELEGRAM_API}/bot{self.config.bot_token}/sendMessage"
        payload = {
            "chat_id": self.config.chat_id,
            "text": body,
            "disable_web_page_preview": True,
        }

        try:
            async with self._lock:
                session = await self._get_session()
                async with session.post(url, json=payload) as response:
                    if response.status != 200:
                        detail = (await response.text())[:200]
                        log.warning("telegram rejected the alert (%s): %s",
                                    response.status, detail)
                        return False
            return True
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.warning("could not deliver telegram alert: %s", exc)
            return False

    async def send_document(self, path: Path, *, caption: str = "") -> bool:
        """Send a file. Caption is truncated to Telegram's 1024-char limit."""
        if not self.enabled:
            return False
        url = f"{TELEGRAM_API}/bot{self.config.bot_token}/sendDocument"
        data = aiohttp.FormData()
        data.add_field("chat_id", self.config.chat_id)
        if caption:
            body = f"{self.prefix}{caption}" if self.prefix else caption
            data.add_field("caption", body[:1024])
        data.add_field(
            "document",
            path.read_bytes(),
            filename=path.name,
            content_type="text/plain",
        )
        try:
            async with self._lock:
                session = await self._get_session()
                async with session.post(url, data=data) as response:
                    if response.status != 200:
                        detail = (await response.text())[:200]
                        log.warning("telegram rejected the document (%s): %s",
                                    response.status, detail)
                        return False
            return True
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.warning("could not deliver telegram document: %s", exc)
            return False

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
