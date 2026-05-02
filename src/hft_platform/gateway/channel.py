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
from typing import Callable, Deque, Protocol, TypeAlias

from structlog import get_logger

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.core import timebase

logger = get_logger("gateway.channel")


TypedIntentFrame: TypeAlias = tuple[
    str,  # 0  marker = "typed_intent_v1"
    int,  # 1  intent_id
    str,  # 2  strategy_id
    str,  # 3  symbol
    int,  # 4  intent_type (enum int)
    int,  # 5  side (enum int)
    int,  # 6  price
    int,  # 7  qty
    int,  # 8  tif (enum int)
    str,  # 9  target_order_id
    int,  # 10 timestamp_ns
    int,  # 11 source_ts_ns
    str,  # 12 reason
    str,  # 13 trace_id
    str,  # 14 idempotency_key
    int,  # 15 ttl_ns
    int,  # 16 decision_price (LOB mid at signal time, scaled x10000)
    str,  # 17 price_type ("LMT"/"MKT")
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
    decision_price: int = 0
    price_type: str = "LMT"


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
        on_ttl_expired: "Callable[[IntentEnvelope | TypedIntentEnvelope], None] | None" = None,
    ) -> None:
        _maxsize = maxsize if maxsize is not None else int(os.getenv("HFT_INTENT_CHANNEL_SIZE", "4096"))
        _ttl_ms = ttl_ms if ttl_ms is not None else int(os.getenv("HFT_INTENT_TTL_MS", "500"))
        _dlq_size = dlq_maxsize if dlq_maxsize is not None else int(os.getenv("HFT_INTENT_DLQ_SIZE", "1000"))

        self._queue: asyncio.Queue[IntentEnvelope | TypedIntentEnvelope] = asyncio.Queue(maxsize=_maxsize)
        self._timeout_ns: int = _ttl_ms * 1_000_000  # convert to ns; 0 = disabled
        self._dlq: Deque[IntentEnvelope | TypedIntentEnvelope] = deque(maxlen=_dlq_size)
        self._on_ttl_expired = on_ttl_expired

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
        if not frame or len(frame) < 16 or frame[0] != "typed_intent_v1":  # 16 legacy, 17 with decision_price
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
                    if self._on_ttl_expired is not None:
                        try:
                            self._on_ttl_expired(envelope)
                        except Exception:
                            logger.exception("on_ttl_expired callback error")
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

    def set_on_ttl_expired(self, cb: "Callable[[IntentEnvelope | TypedIntentEnvelope], None] | None") -> None:
        """Set callback invoked when an envelope expires at channel level."""
        self._on_ttl_expired = cb

    def drain_nowait(self) -> list[IntentEnvelope | TypedIntentEnvelope]:
        """Non-blocking drain: remove and return all pending envelopes.

        Used by HALT supervisor to clear the channel while preserving
        safety orders (CANCEL/FORCE_FLAT) and halt-exempt strategies.
        Caller is responsible for re-submitting items that should be kept.
        """
        items: list[IntentEnvelope | TypedIntentEnvelope] = []
        while not self._queue.empty():
            try:
                items.append(self._queue.get_nowait())
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
        return items

    @staticmethod
    def envelope_intent_type(envelope: IntentEnvelope | TypedIntentEnvelope) -> IntentType | None:
        """Extract IntentType from an envelope (works for both typed and untyped)."""
        if isinstance(envelope, IntentEnvelope):
            return getattr(envelope.intent, "intent_type", None)
        if isinstance(envelope, TypedIntentEnvelope):
            try:
                return IntentType(int(envelope.payload[4]))
            except (IndexError, ValueError):
                return None
        return None

    @staticmethod
    def envelope_strategy_id(envelope: IntentEnvelope | TypedIntentEnvelope) -> str:
        """Extract strategy_id from an envelope (works for both typed and untyped)."""
        if isinstance(envelope, IntentEnvelope):
            return getattr(envelope.intent, "strategy_id", "")
        if isinstance(envelope, TypedIntentEnvelope):
            try:
                return str(envelope.payload[2])
            except (IndexError, ValueError):
                return ""
        return ""

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
        decision_price=int(frame[16]) if len(frame) > 16 else 0,
        price_type=str(frame[17]) if len(frame) > 17 else "LMT",
    )


def typed_view_to_intent(view: TypedIntentView) -> OrderIntent:
    return OrderIntent(
        intent_id=int(view.intent_id),
        strategy_id=str(view.strategy_id),
        symbol=str(view.symbol),
        intent_type=IntentType(int(view.intent_type)),
        side=Side(int(view.side)),
        price=int(view.price),
        qty=int(view.qty),
        tif=TIF(int(view.tif)),
        target_order_id=view.target_order_id,
        timestamp_ns=int(view.timestamp_ns),
        source_ts_ns=int(view.source_ts_ns),
        reason=str(view.reason),
        trace_id=str(view.trace_id),
        idempotency_key=str(view.idempotency_key),
        ttl_ns=int(view.ttl_ns),
        decision_price=int(view.decision_price),
        price_type=str(view.price_type or "LMT"),
    )


def typed_frame_to_intent(frame: TypedIntentFrame) -> OrderIntent:
    return typed_view_to_intent(typed_frame_to_view(frame))
