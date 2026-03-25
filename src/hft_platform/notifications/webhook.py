"""WebhookSender — secondary notification channel via HTTP POST.

Sends JSON payload {"content": text} to a configured webhook URL.
Supports Discord, LINE Notify, Slack, or any generic webhook.

Configured via ``HFT_WEBHOOK_URL`` environment variable.
If not set, webhook is disabled.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


class WebhookSender:
    """Fire-and-forget async webhook sender for critical alert fan-out."""

    __slots__ = ("_url", "_timeout")

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self._url: str = url
        self._timeout: float = timeout

    async def send(self, text: str) -> bool:
        """POST JSON to webhook.  Returns True on 2xx, False on failure."""
        if not self._url:
            return False

        import aiohttp  # noqa: PLC0415

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._url,
                    json={"content": text},
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                ) as resp:
                    ok = 200 <= resp.status < 300
                    if ok:
                        logger.debug("webhook.sent", url=self._url[:50])
                    else:
                        logger.warning(
                            "webhook.non_2xx",
                            url=self._url[:50],
                            status=resp.status,
                        )
                    return ok
        except Exception:  # noqa: BLE001
            logger.warning("webhook.send_failed", url=self._url[:50], exc_info=True)
            return False
