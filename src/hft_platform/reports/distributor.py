"""Report distribution: channel loading, sending, and multi-channel routing.

This module is intentionally separate from the existing TelegramSender
(which uses fire-and-forget with rate-limit drops). ReportSender implements
retry logic (429 back-off, 5xx exponential back-off) suitable for
scheduled daily report delivery where message loss is unacceptable.
"""
from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import structlog

from hft_platform.reports.models import ChannelConfig

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)

__all__ = ["load_channels", "ReportSender", "Distributor"]

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


# ---------------------------------------------------------------------------
# Channel loading
# ---------------------------------------------------------------------------


def load_channels() -> list[ChannelConfig]:
    """Build the list of distribution channels from environment variables.

    Channel discovery rules:
    - ``HFT_TELEGRAM_CHAT_ID``         → owner channel, tier "paid", always enabled
    - ``HFT_REPORT_PAID_CHANNEL_ID``   + ``HFT_REPORT_PAID_ENABLED``  → tier "paid"
    - ``HFT_REPORT_FREE_CHANNEL_ID``   + ``HFT_REPORT_FREE_ENABLED``  → tier "free"

    A channel is skipped entirely when its chat_id env var is absent or empty.
    ``*_ENABLED`` defaults to "0" (disabled) when not set.
    """
    channels: list[ChannelConfig] = []

    owner_id = os.environ.get("HFT_TELEGRAM_CHAT_ID", "").strip()
    if owner_id:
        channels.append(
            ChannelConfig(name="owner", chat_id=owner_id, tier="paid", enabled=True)
        )

    paid_id = os.environ.get("HFT_REPORT_PAID_CHANNEL_ID", "").strip()
    if paid_id:
        paid_enabled = os.environ.get("HFT_REPORT_PAID_ENABLED", "0").strip() == "1"
        channels.append(
            ChannelConfig(name="paid", chat_id=paid_id, tier="paid", enabled=paid_enabled)
        )

    free_id = os.environ.get("HFT_REPORT_FREE_CHANNEL_ID", "").strip()
    if free_id:
        free_enabled = os.environ.get("HFT_REPORT_FREE_ENABLED", "0").strip() == "1"
        channels.append(
            ChannelConfig(name="free", chat_id=free_id, tier="free", enabled=free_enabled)
        )

    return channels


# ---------------------------------------------------------------------------
# ReportSender
# ---------------------------------------------------------------------------


class ReportSender:
    """Reliable Telegram message sender with retry logic.

    Supports:
    - 429 rate-limit: sleep Retry-After header value (default 5 s), up to 3 retries.
    - 5xx server errors: exponential back-off (2^attempt seconds), up to 3 retries.
    - 4xx other: log and return False immediately (no retry).
    - Network exceptions: exponential back-off, up to 3 retries.
    """

    _MAX_RETRIES = 3

    def __init__(self, bot_token: str = "") -> None:
        self._token: str = bot_token or os.environ.get("HFT_TELEGRAM_BOT_TOKEN", "")
        self._session: aiohttp.ClientSession | None = None  # type: ignore[name-defined]

    async def _ensure_session(self) -> None:
        """Lazily create the aiohttp ClientSession."""
        if self._session is None:
            if aiohttp is None:  # pragma: no cover
                raise RuntimeError("aiohttp is required for ReportSender")
            self._session = aiohttp.ClientSession()

    async def send(
        self,
        chat_id: str,
        text: str,
        parse_mode: str = "HTML",
    ) -> bool:
        """Send a single message to *chat_id*.

        Returns True on success, False on permanent failure.
        """
        if not self._token:
            logger.warning("report_sender.no_token", chat_id=chat_id)
            return False

        await self._ensure_session()
        url = _TELEGRAM_API.format(token=self._token)
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}

        for attempt in range(self._MAX_RETRIES):
            try:
                async with self._session.post(url, json=payload) as resp:  # type: ignore[union-attr]
                    status = resp.status
                    if status == 200:
                        return True
                    if status == 429:
                        retry_after: float = 5.0
                        try:
                            data = await resp.json()
                            retry_after = float(
                                data.get("parameters", {}).get("retry_after", 5)
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        logger.warning(
                            "report_sender.rate_limited",
                            attempt=attempt,
                            retry_after=retry_after,
                            chat_id=chat_id,
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    if status >= 500:
                        backoff = 2**attempt
                        logger.warning(
                            "report_sender.server_error",
                            status=status,
                            attempt=attempt,
                            backoff=backoff,
                            chat_id=chat_id,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    # 4xx other — permanent failure
                    logger.error(
                        "report_sender.client_error",
                        status=status,
                        chat_id=chat_id,
                    )
                    return False
            except Exception as exc:  # noqa: BLE001
                backoff = 2**attempt
                logger.warning(
                    "report_sender.exception",
                    exc=str(exc),
                    attempt=attempt,
                    backoff=backoff,
                    chat_id=chat_id,
                )
                await asyncio.sleep(backoff)

        logger.error("report_sender.max_retries_exceeded", chat_id=chat_id)
        return False

    async def send_batch(
        self,
        chat_id: str,
        messages: list[str],
        delay_s: float = 1.5,
    ) -> int:
        """Send *messages* sequentially, waiting *delay_s* between each.

        Returns the count of successfully sent messages.
        """
        sent = 0
        for i, msg in enumerate(messages):
            ok = await self.send(chat_id, msg)
            if ok:
                sent += 1
            if i < len(messages) - 1:
                await asyncio.sleep(delay_s)
        return sent

    async def close(self) -> None:
        """Close the underlying aiohttp session."""
        if self._session is not None:
            await self._session.close()
            self._session = None


# ---------------------------------------------------------------------------
# Distributor
# ---------------------------------------------------------------------------


class Distributor:
    """Routes rendered report messages to the appropriate channels by tier.

    ``rendered`` is a mapping of tier → list[str] (message parts).
    Each enabled channel receives the messages for its tier.
    Disabled channels are silently skipped.
    """

    def __init__(self, sender: ReportSender, channels: list[ChannelConfig]) -> None:
        self._sender = sender
        self._channels = channels

    async def send(self, rendered: dict[str, list[str]]) -> None:
        """Distribute rendered messages to all enabled channels."""
        for channel in self._channels:
            if not channel.enabled:
                logger.debug(
                    "distributor.skip_disabled",
                    channel=channel.name,
                    chat_id=channel.chat_id,
                )
                continue
            messages = rendered.get(channel.tier, [])
            if not messages:
                logger.debug(
                    "distributor.no_messages_for_tier",
                    channel=channel.name,
                    tier=channel.tier,
                )
                continue
            sent = await self._sender.send_batch(channel.chat_id, messages)
            logger.info(
                "distributor.sent",
                channel=channel.name,
                chat_id=channel.chat_id,
                tier=channel.tier,
                sent=sent,
                total=len(messages),
            )
