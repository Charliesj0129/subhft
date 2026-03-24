"""Async Telegram Bot API sender with rate limiting and command poller."""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Optional

import structlog

try:
    import aiohttp
except ImportError:  # pragma: no cover — aiohttp optional at import time
    aiohttp = None  # type: ignore[assignment]

if TYPE_CHECKING:
    import aiohttp as _aiohttp_type  # noqa: F401

logger = structlog.get_logger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramSender:
    """Fire-and-forget async Telegram message sender with rate limiting.

    Reads credentials from environment if not provided:
      - HFT_TELEGRAM_BOT_TOKEN
      - HFT_TELEGRAM_CHAT_ID
    """

    __slots__ = (
        "_token",
        "_chat_id",
        "_enabled",
        "_rate_limit_s",
        "_last_send_ts",
        "_session",
    )

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        enabled: bool = False,
        rate_limit_seconds: float = 1.0,
    ) -> None:
        token = bot_token or os.environ.get("HFT_TELEGRAM_BOT_TOKEN", "")
        cid = chat_id or os.environ.get("HFT_TELEGRAM_CHAT_ID", "")
        self._token: str = token
        self._chat_id: str = cid
        self._enabled: bool = enabled and bool(token) and bool(cid)
        self._rate_limit_s: float = rate_limit_seconds
        self._last_send_ts: float = 0.0
        self._session: Optional[object] = None

    async def send(self, text: str, *, critical: bool = False) -> bool:
        """Send a message to the configured chat.

        Args:
            text: Message text (HTML parse mode).
            critical: If True, bypass rate limiting.

        Returns:
            True if message was sent successfully, False otherwise.
        """
        if not self._enabled:
            return False

        now = time.monotonic()
        if not critical and (now - self._last_send_ts) < self._rate_limit_s:
            logger.debug(
                "telegram.rate_limited",
                elapsed_s=round(now - self._last_send_ts, 3),
                rate_limit_s=self._rate_limit_s,
            )
            return False

        if aiohttp is None:  # pragma: no cover
            logger.warning("telegram.aiohttp_unavailable")
            return False

        try:
            if self._session is None or self._session.closed:  # type: ignore[union-attr]
                self._session = aiohttp.ClientSession()

            url = _TELEGRAM_API_BASE.format(token=self._token, method="sendMessage")
            payload = {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
            }
            async with self._session.post(url, json=payload) as resp:  # type: ignore[union-attr]
                if resp.status == 200:
                    self._last_send_ts = time.monotonic()
                    logger.debug("telegram.sent", chat_id=self._chat_id, critical=critical)
                    return True
                body = await resp.text()
                logger.warning(
                    "telegram.send_failed",
                    status=resp.status,
                    body=body[:200],
                )
                return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram.send_exception", exc=str(exc))
            return False

    async def close(self) -> None:
        """Close the underlying aiohttp session."""
        if self._session is not None:
            session = self._session
            if not session.closed:  # type: ignore[union-attr]
                await session.close()  # type: ignore[union-attr]
        self._session = None


class TelegramCommandPoller:
    """Poll Telegram getUpdates and dispatch /stop and /status commands.

    Only responds to messages originating from the whitelisted chat_id.
    """

    __slots__ = (
        "_token",
        "_chat_id",
        "_redis",
        "_poll_interval",
        "_offset",
    )

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        redis_client: object,
        poll_interval: float = 5.0,
    ) -> None:
        self._token: str = bot_token
        self._chat_id: str = str(chat_id)
        self._redis = redis_client
        self._poll_interval: float = poll_interval
        self._offset: int = 0

    async def _reply(self, session: object, text: str) -> None:
        """Send a reply back to the operator chat (best-effort)."""
        url = _TELEGRAM_API_BASE.format(token=self._token, method="sendMessage")
        payload = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}
        try:
            async with session.post(url, json=payload):  # type: ignore[union-attr]
                pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram.reply_exception", exc=str(exc))

    async def poll_once(self) -> None:
        """Fetch pending updates and process commands."""
        if aiohttp is None:  # pragma: no cover
            logger.warning("telegram.poller.aiohttp_unavailable")
            return

        url = _TELEGRAM_API_BASE.format(token=self._token, method="getUpdates")
        params = {"offset": self._offset, "timeout": 1}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        logger.warning("telegram.poll_bad_status", status=resp.status)
                        return
                    data = await resp.json()

                updates = data.get("result", [])
                for update in updates:
                    update_id: int = update["update_id"]
                    self._offset = update_id + 1

                    message = update.get("message", {})
                    from_id = str(message.get("from", {}).get("id", ""))
                    text = message.get("text", "").strip()

                    if from_id != self._chat_id:
                        logger.debug(
                            "telegram.poller.ignored_unknown_sender",
                            from_id=from_id,
                        )
                        continue

                    if text == "/stop":
                        self._redis.set("hft:emergency_halt", "1")
                        logger.warning("telegram.poller.emergency_halt_activated")
                        await self._reply(session, "🔴 Emergency HALT activated")

                    elif text == "/status":
                        await self._reply(session, "Status: running")

        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram.poller.poll_exception", exc=str(exc))

    async def run(self) -> None:
        """Run the polling loop indefinitely."""
        import asyncio  # noqa: PLC0415

        logger.info("telegram.poller.started", poll_interval=self._poll_interval)
        while True:
            await self.poll_once()
            await asyncio.sleep(self._poll_interval)
