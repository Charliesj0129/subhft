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
from typing import Deque, Protocol, TypeAlias

from structlog import get_logger

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.core import timebase

logger = get_logger("gateway.channel")


TypedIntentFrame: TypeAlias = tuple[
    str,  # marker = "typed_intent_v1"
    int,  # intent_id
    str,  # strategy_id
    str,  # symbol
    int,  # intent_type (enum int)
    int,  # side (enum int)
    int,  # price
    int,  # qty
    int,  # tif (enum int)
    str,  # target_order_id
    int,  # timestamp_ns
    int,  # source_ts_ns
    str,  # reason
    str,  # trace_id
    str,  # idempotency_key
    int,  # ttl_ns
]


@dataclass(slots=True)
class IntentEnvelope:
    """Wrapper carrying TTL metadata alongside the intent."""

    intent: OrderIntent
    enqueued_ns: int  # Monotonic nanoseconds at submission
    ack_token: str  # Correlation ID for caller (= idempotency_key or str(intent_id))


@dataclass(slots=True)
class TypedIntentEnvelope:
    """Low-allocation ingress envelope. Materialized to OrderIntent in receive()."""

    payload: TypedIntentFrame
    enqueued_ns: int
    ack_token: str


@dataclass(slots=True)
class TypedIntentView:
    """Attribute view over TypedIntentFrame for gateway/risk hot path checks."""

    intent_id: int
    strategy_id: str
    symbol: str
    intent_type: int
    side: int
    price: int
    qty: int
    tif: int
    target_order_id: str | None
    timestamp_ns: int
    source_ts_ns: int
    reason: str
    trace_id: str
    idempotency_key: str
    ttl_ns: int


class IntentChannelProtocol(Protocol):
    """Structural subtype for future transport backends."""

    def submit_nowait(self, intent: OrderIntent) -> str: ...
    def submit_typed_nowait(self, frame: TypedIntentFrame) -> str: ...
    async def receive(self) -> IntentEnvelope: ...
    async def receive_raw(self) -> IntentEnvelope | TypedIntentEnvelope: ...
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

        self._queue: asyncio.Queue[IntentEnvelope | TypedIntentEnvelope] = asyncio.Queue(maxsize=_maxsize)
        self._timeout_ns: int = _ttl_ms * 1_000_000  # convert to ns; 0 = disabled
        self._dlq: Deque[IntentEnvelope | TypedIntentEnvelope] = deque(maxlen=_dlq_size)

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

    def submit_typed_nowait(self, frame: TypedIntentFrame) -> str:
        """Typed fast-path ingress from StrategyRunner; avoids OrderIntent alloc on hot path."""
        if not frame or len(frame) < 16 or frame[0] != "typed_intent_v1":
            raise ValueError("Invalid typed intent frame")
        intent_id = int(frame[1])
        idempotency_key = str(frame[14] or "")
        token = idempotency_key or str(intent_id)
        envelope = TypedIntentEnvelope(
            payload=frame,
            enqueued_ns=timebase.now_ns(),
            ack_token=token,
        )
        self._queue.put_nowait(envelope)
        return token

    async def receive(self) -> IntentEnvelope:
        """Receive the next non-expired envelope; expired ones go to DLQ.

        Loops until a live envelope is found. Never raises on expiry.
        """
        envelope = await self.receive_raw()
        if isinstance(envelope, TypedIntentEnvelope):
            return self._materialize_typed_envelope(envelope)
        return envelope

    async def receive_raw(self) -> IntentEnvelope | TypedIntentEnvelope:
        """Receive the next non-expired envelope without forced materialization."""
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
    def dlq(self) -> Deque[IntentEnvelope | TypedIntentEnvelope]:
        """Read-only view of the dead-letter queue."""
        return self._dlq

    def dlq_size(self) -> int:
        return len(self._dlq)

    def _materialize_typed_envelope(self, envelope: TypedIntentEnvelope) -> IntentEnvelope:
        intent = typed_frame_to_intent(envelope.payload)
        return IntentEnvelope(intent=intent, enqueued_ns=envelope.enqueued_ns, ack_token=envelope.ack_token)


def typed_frame_to_view(frame: TypedIntentFrame) -> TypedIntentView:
    return TypedIntentView(
        intent_id=int(frame[1]),
        strategy_id=str(frame[2]),
        symbol=str(frame[3]),
        intent_type=int(frame[4]),
        side=int(frame[5]),
        price=int(frame[6]),
        qty=int(frame[7]),
        tif=int(frame[8]),
        target_order_id=(str(frame[9]) or None),
        timestamp_ns=int(frame[10]),
        source_ts_ns=int(frame[11]),
        reason=str(frame[12]),
        trace_id=str(frame[13]),
        idempotency_key=str(frame[14]),
        ttl_ns=int(frame[15]),
    )


def typed_frame_to_intent(frame: TypedIntentFrame) -> OrderIntent:
    view = typed_frame_to_view(frame)
    return OrderIntent(
        intent_id=view.intent_id,
        strategy_id=view.strategy_id,
        symbol=view.symbol,
        intent_type=IntentType(view.intent_type),
        side=Side(view.side),
        price=view.price,
        qty=view.qty,
        tif=TIF(view.tif),
        target_order_id=view.target_order_id,
        timestamp_ns=view.timestamp_ns,
        source_ts_ns=view.source_ts_ns,
        reason=view.reason,
        trace_id=view.trace_id,
        idempotency_key=view.idempotency_key,
        ttl_ns=view.ttl_ns,
    )
