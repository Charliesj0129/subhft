"""Async Telegram Bot API sender with rate limiting.

Commands (``/stop`` and the report commands) are served by the bot service,
``hft_platform.bot``; ``/stop`` writes the engine's kill switch there.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING

import structlog

try:
    import aiohttp
except ImportError:  # pragma: no cover — aiohttp optional at import time
    aiohttp = None

if TYPE_CHECKING:
    from aiohttp import ClientSession as _AiohttpClientSession

logger = structlog.get_logger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramSender:
    """Fire-and-forget async Telegram message sender with rate limiting.

    Reads credentials from environment if not provided:
      - HFT_TELEGRAM_BOT_TOKEN
      - HFT_TELEGRAM_CHAT_ID

    Critical messages (critical=True) are retried up to _MAX_CRITICAL_RETRIES
    times with exponential backoff on transient errors (429, 5xx, network
    exceptions). Non-critical messages are fire-and-forget (single attempt).
    """

    _MAX_CRITICAL_RETRIES: int = 2
    _RETRY_BACKOFF_S: float = 1.0
    _TRANSIENT_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

    __slots__ = (
        "_token",
        "_chat_id",
        "_enabled",
        "_rate_limit_s",
        "_last_send_ts",
        "_session",
        "_aiohttp_warned",
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
        self._session: _AiohttpClientSession | None = None
        self._aiohttp_warned: bool = False

        if self._enabled and aiohttp is None:
            logger.error(
                "telegram.aiohttp_missing_at_startup",
                hint="HFT_TELEGRAM_ENABLED=1 but aiohttp is not installed. Install with: pip install aiohttp",
            )
            self._enabled = False

    _MAX_MESSAGE_LEN: int = 4096

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

        # Split oversized messages into <=4096-char chunks on newline boundaries
        if len(text) > self._MAX_MESSAGE_LEN:
            chunks = self._split_text(text, self._MAX_MESSAGE_LEN)
            all_ok = True
            for chunk in chunks:
                ok = await self._send_single(chunk, critical=critical)
                if not ok:
                    all_ok = False
            return all_ok

        return await self._send_single(text, critical=critical)

    @staticmethod
    def _split_text(text: str, max_len: int) -> list[str]:
        """Split *text* into chunks of at most *max_len* chars on newline boundaries."""
        chunks: list[str] = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            # Find the last newline within the limit
            split_at = text.rfind("\n", 0, max_len)
            if split_at <= 0:
                # No newline found; hard-split at max_len
                split_at = max_len
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")
        return chunks

    async def _send_single(self, text: str, *, critical: bool = False) -> bool:
        """Send a single <=4096-char message to the configured chat."""
        now = time.monotonic()
        if not critical and (now - self._last_send_ts) < self._rate_limit_s:
            # P3-b2 (2026-04-27): rate-limit drops were previously logged at
            # DEBUG only AND silently merged with "send failed" via the bool
            # return. Bump to INFO + dedicated metric so dashboards can
            # distinguish "client-side throttled" from "downstream failure".
            logger.info(
                "telegram.rate_limited",
                elapsed_s=round(now - self._last_send_ts, 3),
                rate_limit_s=self._rate_limit_s,
                critical=critical,
            )
            try:
                from hft_platform.observability.metrics import MetricsRegistry

                MetricsRegistry.get().bot_rate_limited_total.labels(critical=str(critical).lower()).inc()
            except Exception:  # noqa: BLE001
                pass
            return False

        if aiohttp is None:  # pragma: no cover
            if not self._aiohttp_warned:
                self._aiohttp_warned = True
                logger.warning("telegram.aiohttp_unavailable")
            return False

        max_attempts = self._MAX_CRITICAL_RETRIES + 1 if critical else 1
        url = _TELEGRAM_API_BASE.format(token=self._token, method="sendMessage")
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }

        for attempt in range(max_attempts):
            try:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()

                async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        self._last_send_ts = time.monotonic()
                        logger.debug("telegram.sent", chat_id=self._chat_id, critical=critical)
                        return True

                    body = await resp.text()

                    if critical and attempt < max_attempts - 1 and resp.status in self._TRANSIENT_STATUSES:
                        delay = self._RETRY_BACKOFF_S * (2**attempt)
                        logger.warning(
                            "telegram.retry",
                            attempt=attempt + 1,
                            max_attempts=max_attempts,
                            status=resp.status,
                            delay_s=delay,
                        )
                        await asyncio.sleep(delay)
                        continue

                    logger.warning(
                        "telegram.send_failed",
                        status=resp.status,
                        body=body[:200],
                    )
                    return False

            except Exception as exc:  # noqa: BLE001
                # H15: Close broken session so a fresh one is created on retry
                if self._session is not None:
                    try:
                        await self._session.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._session = None
                if critical and attempt < max_attempts - 1:
                    delay = self._RETRY_BACKOFF_S * (2**attempt)
                    logger.warning(
                        "telegram.retry_on_exception",
                        attempt=attempt + 1,
                        max_attempts=max_attempts,
                        exc=str(exc),
                        delay_s=delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning("telegram.send_exception", exc=str(exc))
                return False

        return False  # exhausted all retries

    async def close(self) -> None:
        """Close the underlying aiohttp session."""
        if self._session is not None:
            session = self._session
            if not session.closed:
                await session.close()
        self._session = None
