"""CE2-02: LocalIntentChannel — asyncio.Queue wrapper with TTL envelope + DLQ.

Design decisions (D1):
- In-process only; wraps asyncio.Queue for single-loop usage.
- TTL-expired envelopes are routed to a bounded DLQ deque and skipped.
- submit_nowait() is the hot-path entry point (raises QueueFull on backpressure).
- Protocol class IntentChannelProtocol enables future network-transport swap.
"""
from __future__ import annotations

import asyncio
import os
from collections import deque
from dataclasses import dataclass
from typing import Deque, Protocol

from structlog import get_logger

from hft_platform.contracts.strategy import OrderIntent
from hft_platform.core import timebase

logger = get_logger("gateway.channel")


@dataclass(slots=True)
class IntentEnvelope:
    """Wrapper carrying TTL metadata alongside the intent."""

    intent: OrderIntent
    enqueued_ns: int  # Monotonic nanoseconds at submission
    ack_token: str  # Correlation ID for caller (= idempotency_key or str(intent_id))


class IntentChannelProtocol(Protocol):
    """Structural subtype for future transport backends."""

    def submit_nowait(self, intent: OrderIntent) -> str: ...
    async def receive(self) -> IntentEnvelope: ...
    def task_done(self) -> None: ...
    def qsize(self) -> int: ...


class LocalIntentChannel:
    """Bounded asyncio.Queue with TTL expiry and DLQ sidecar.

    Env vars:
        HFT_INTENT_CHANNEL_SIZE: max pending envelopes (default 4096)
        HFT_INTENT_TTL_MS:       envelope TTL in ms (default 500; 0=disabled)
        HFT_INTENT_DLQ_SIZE:     max DLQ entries (default 1000)
    """

    def __init__(
        self,
        maxsize: int | None = None,
        ttl_ms: int | None = None,
        dlq_maxsize: int | None = None,
    ) -> None:
        _maxsize = maxsize if maxsize is not None else int(os.getenv("HFT_INTENT_CHANNEL_SIZE", "4096"))
        _ttl_ms = ttl_ms if ttl_ms is not None else int(os.getenv("HFT_INTENT_TTL_MS", "500"))
        _dlq_size = dlq_maxsize if dlq_maxsize is not None else int(os.getenv("HFT_INTENT_DLQ_SIZE", "1000"))

        self._queue: asyncio.Queue[IntentEnvelope] = asyncio.Queue(maxsize=_maxsize)
        self._timeout_ns: int = _ttl_ms * 1_000_000  # convert to ns; 0 = disabled
        self._dlq: Deque[IntentEnvelope] = deque(maxlen=_dlq_size)

    # ── Hot path ──────────────────────────────────────────────────────────

    def submit_nowait(self, intent: OrderIntent) -> str:
        """Enqueue intent; raises asyncio.QueueFull on backpressure.

        Returns the ack_token (idempotency_key if set, else str(intent_id)).
        This method must NOT block — it is called from the strategy hot path.
        """
        token = intent.idempotency_key or str(intent.intent_id)
        envelope = IntentEnvelope(
            intent=intent,
            enqueued_ns=timebase.now_ns(),
            ack_token=token,
        )
        self._queue.put_nowait(envelope)
        return token

    async def receive(self) -> IntentEnvelope:
        """Receive the next non-expired envelope; expired ones go to DLQ.

        Loops until a live envelope is found. Never raises on expiry.
        """
        while True:
            envelope = await self._queue.get()
            if self._timeout_ns > 0:
                age_ns = timebase.now_ns() - envelope.enqueued_ns
                if age_ns > self._timeout_ns:
                    self._dlq.append(envelope)
                    self._queue.task_done()
                    logger.warning(
                        "Intent TTL expired, routed to DLQ",
                        ack_token=envelope.ack_token,
                        age_ms=age_ns / 1_000_000,
                    )
                    continue
            return envelope

    def task_done(self) -> None:
        self._queue.task_done()

    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def dlq(self) -> Deque[IntentEnvelope]:
        """Read-only view of the dead-letter queue."""
        return self._dlq

    def dlq_size(self) -> int:
        return len(self._dlq)
