"""Report distribution: channel loading, sending, and multi-channel routing.

This module is intentionally separate from the existing TelegramSender
(which uses fire-and-forget with rate-limit drops). ReportSender implements
retry logic (429 back-off, 5xx exponential back-off) suitable for
scheduled daily report delivery where message loss is unacceptable.
"""

from __future__ import annotations

import asyncio
import os

import structlog

from hft_platform.reports.models import ChannelConfig, ComposedReport

try:
    import requests as _requests
except ImportError:  # pragma: no cover
    _requests = None  # type: ignore[assignment]

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # noqa: F841

logger = structlog.get_logger(__name__)

__all__ = ["load_channels", "ReportSender", "Distributor"]

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_TELEGRAM_PHOTO_API = "https://api.telegram.org/bot{token}/sendPhoto"


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
        channels.append(ChannelConfig(name="owner", chat_id=owner_id, tier="paid", enabled=True))

    paid_id = os.environ.get("HFT_REPORT_PAID_CHANNEL_ID", "").strip()
    if paid_id:
        paid_enabled = os.environ.get("HFT_REPORT_PAID_ENABLED", "0").strip() == "1"
        channels.append(ChannelConfig(name="paid", chat_id=paid_id, tier="paid", enabled=paid_enabled))

    free_id = os.environ.get("HFT_REPORT_FREE_CHANNEL_ID", "").strip()
    if free_id:
        free_enabled = os.environ.get("HFT_REPORT_FREE_ENABLED", "0").strip() == "1"
        channels.append(ChannelConfig(name="free", chat_id=free_id, tier="free", enabled=free_enabled))

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

    async def _do_post(self, url: str, payload: dict) -> tuple[int, str]:  # type: ignore[type-arg]
        """POST JSON to URL. Returns (status_code, response_body).

        Uses ``requests`` (sync, in thread) if available, else ``aiohttp``.
        """
        if _requests is not None:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: _requests.post(url, json=payload, timeout=30),
            )
            return resp.status_code, resp.text

        if aiohttp is not None:
            if self._session is None:
                self._session = aiohttp.ClientSession()
            async with self._session.post(url, json=payload) as resp:
                body = await resp.text()
                return resp.status, body

        raise RuntimeError("Neither requests nor aiohttp available for ReportSender")

    def __init__(self, bot_token: str = "") -> None:
        self._token: str = bot_token or os.environ.get("HFT_TELEGRAM_BOT_TOKEN", "")
        self._session: object | None = None  # aiohttp.ClientSession or None

    async def send(
        self,
        chat_id: str,
        text: str,
        parse_mode: str = "HTML",
    ) -> bool:
        """Send a single message to *chat_id*.

        Uses ``requests`` (sync) if available, falls back to ``aiohttp``.
        Returns True on success, False on permanent failure.
        """
        if not self._token:
            logger.warning("report_sender.no_token", chat_id=chat_id)
            return False

        url = _TELEGRAM_API.format(token=self._token)
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}

        for attempt in range(self._MAX_RETRIES):
            try:
                status, body = await self._do_post(url, payload)
                if status == 200:
                    return True
                if status == 429:
                    retry_after = 5.0
                    try:
                        import json

                        data = json.loads(body) if isinstance(body, str) else body
                        retry_after = float(data.get("parameters", {}).get("retry_after", 5))
                    except Exception:  # noqa: BLE001
                        pass
                    logger.warning("report_sender.rate_limited", attempt=attempt, retry_after=retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                if status >= 500:
                    backoff = 2**attempt
                    logger.warning("report_sender.server_error", status=status, attempt=attempt, backoff=backoff)
                    await asyncio.sleep(backoff)
                    continue
                logger.error("report_sender.client_error", status=status, chat_id=chat_id)
                return False
            except Exception as exc:  # noqa: BLE001
                backoff = 2**attempt
                # Sanitize exception message to avoid leaking bot token from URL
                exc_msg = str(exc)
                if "api.telegram.org/bot" in exc_msg:
                    exc_msg = type(exc).__name__
                logger.warning(
                    "report_sender.exception",
                    exc=exc_msg,
                    attempt=attempt,
                    backoff=backoff,
                    chat_id=chat_id,
                )
                await asyncio.sleep(backoff)

        logger.error("report_sender.max_retries_exceeded", chat_id=chat_id)
        return False

    async def _do_multipart_post(self, url: str, data: dict, files: dict) -> tuple[int, str]:  # type: ignore[type-arg]
        """POST multipart form data. Returns (status_code, response_body).

        Uses ``requests`` (sync, in thread) if available, else ``aiohttp``.
        """
        if _requests is not None:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: _requests.post(url, data=data, files=files, timeout=30),
            )
            return resp.status_code, resp.text

        if aiohttp is not None:
            if self._session is None:
                self._session = aiohttp.ClientSession()
            form = aiohttp.FormData()
            for k, v in data.items():
                form.add_field(k, str(v))
            for k, (filename, content, content_type) in files.items():
                form.add_field(k, content, filename=filename, content_type=content_type)
            async with self._session.post(url, data=form) as resp:
                body = await resp.text()
                return resp.status, body

        raise RuntimeError("Neither requests nor aiohttp available for ReportSender")

    async def send_photo(
        self,
        chat_id: str,
        photo: bytes,
        caption: str = "",
    ) -> bool:
        """Send a photo via Telegram Bot API.

        Uses multipart form data to upload photo bytes.
        Returns True on success, False on permanent failure.
        """
        if not self._token:
            logger.warning("report_sender.no_token", chat_id=chat_id)
            return False

        url = _TELEGRAM_PHOTO_API.format(token=self._token)
        data: dict[str, str] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
            data["parse_mode"] = "HTML"
        files = {"photo": ("chart.png", photo, "image/png")}

        for attempt in range(self._MAX_RETRIES):
            try:
                status, body = await self._do_multipart_post(url, data, files)
                if status == 200:
                    return True
                if status == 429:
                    retry_after = 5.0
                    try:
                        import json

                        parsed = json.loads(body) if isinstance(body, str) else body
                        retry_after = float(parsed.get("parameters", {}).get("retry_after", 5))
                    except Exception:  # noqa: BLE001
                        pass
                    logger.warning("report_sender.photo_rate_limited", attempt=attempt, retry_after=retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                if status >= 500:
                    backoff = 2**attempt
                    logger.warning("report_sender.photo_server_error", status=status, attempt=attempt, backoff=backoff)
                    await asyncio.sleep(backoff)
                    continue
                logger.error("report_sender.photo_client_error", status=status, chat_id=chat_id)
                return False
            except Exception as exc:  # noqa: BLE001
                backoff = 2**attempt
                exc_msg = str(exc)
                if "api.telegram.org/bot" in exc_msg:
                    exc_msg = type(exc).__name__
                logger.warning(
                    "report_sender.photo_exception",
                    exc=exc_msg,
                    attempt=attempt,
                    backoff=backoff,
                    chat_id=chat_id,
                )
                await asyncio.sleep(backoff)

        logger.error("report_sender.photo_max_retries_exceeded", chat_id=chat_id)
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
        """Close the underlying HTTP session if any."""
        if self._session is not None and hasattr(self._session, "close"):
            await self._session.close()  # type: ignore[misc]
            self._session = None


# ---------------------------------------------------------------------------
# Distributor
# ---------------------------------------------------------------------------


class Distributor:
    """Routes composed report messages to the appropriate channels by tier.

    Each enabled channel receives message parts filtered by its tier:
    - ``"free"`` channels only receive parts where ``min_tier == "free"``.
    - ``"paid"`` and ``"owner"`` channels receive all parts (free + paid).
    """

    _TIER_INCLUDES: dict[str, set[str]] = {
        "free": {"free"},
        "paid": {"free", "paid"},
        "owner": {"free", "paid"},
    }

    def __init__(self, sender: ReportSender, channels: list[ChannelConfig]) -> None:
        self._sender = sender
        self._channels = channels

    async def send(self, composed: ComposedReport) -> None:
        """Distribute composed messages to all enabled channels."""
        for channel in self._channels:
            if not channel.enabled:
                logger.debug(
                    "distributor.skip_disabled",
                    channel=channel.name,
                    chat_id=channel.chat_id,
                )
                continue

            allowed_tiers = self._TIER_INCLUDES.get(channel.tier, {"free", "paid"})
            parts = [p for p in composed.messages if p.min_tier in allowed_tiers]

            if not parts:
                logger.debug(
                    "distributor.no_messages_for_tier",
                    channel=channel.name,
                    tier=channel.tier,
                )
                continue

            sent = 0
            for i, part in enumerate(parts):
                ok = False
                if part.kind == "text":
                    ok = await self._sender.send(channel.chat_id, part.content)
                elif part.kind == "image" and part.image is not None:
                    ok = await self._sender.send_photo(channel.chat_id, part.image, caption=part.caption)
                if ok:
                    sent += 1
                if i < len(parts) - 1:
                    await asyncio.sleep(1.5)

            logger.info(
                "distributor.sent",
                channel=channel.name,
                chat_id=channel.chat_id,
                tier=channel.tier,
                sent=sent,
                total=len(parts),
            )
