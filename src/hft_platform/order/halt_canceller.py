"""HALT auto-cancel: enqueue CANCEL for all live orders when StormGuard is HALT."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import structlog
from prometheus_client import Counter

from hft_platform.contracts.strategy import (
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.core import timebase

logger = structlog.get_logger(__name__)

halt_cancel_orders_total = Counter(
    "halt_cancel_orders_total",
    "Total CANCEL commands enqueued due to HALT state",
)

_BATCH_SIZE = 20
_BATCH_DELAY_S = 0.005  # 5ms between batches


async def cancel_all_live_orders(order_adapter: Any, storm_guard: Any) -> int:
    """Enqueue CANCEL OrderCommands for every live order when HALT.

    Idempotent: if there are no live orders or state is not HALT, returns 0.

    Args:
        order_adapter: OrderAdapter instance with ``live_orders``, ``_live_orders_lock``,
            and ``order_queue``.
        storm_guard: Object exposing ``.state`` as :class:`StormGuardState`.

    Returns:
        Number of cancel commands enqueued.
    """
    if storm_guard.state != StormGuardState.HALT:
        logger.debug("halt_cancel_skipped", reason="not_in_halt", state=int(storm_guard.state))
        return 0

    # Snapshot live orders under lock to avoid mutation during iteration.
    async with order_adapter._live_orders_lock:
        snapshot: Dict[str, Any] = dict(order_adapter.live_orders)

    if not snapshot:
        logger.info("halt_cancel_noop", reason="no_live_orders")
        return 0

    count = 0
    batch: list[OrderCommand] = []

    for order_key, _trade in snapshot.items():
        parts = order_key.split(":", 1)
        strategy_id = parts[0] if len(parts) > 1 else ""
        target_order_id = parts[1] if len(parts) > 1 else order_key

        now_ns = timebase.now_ns()

        intent = OrderIntent(
            intent_id=now_ns,
            strategy_id=strategy_id,
            symbol="",
            intent_type=IntentType.CANCEL,
            side=Side.BUY,
            price=0,
            qty=0,
            target_order_id=target_order_id,
            timestamp_ns=now_ns,
            reason="HALT_AUTO_CANCEL",
        )

        cmd = OrderCommand(
            cmd_id=now_ns,
            intent=intent,
            deadline_ns=now_ns + 5_000_000_000,  # 5s deadline
            storm_guard_state=StormGuardState.HALT,
            created_ns=now_ns,
        )

        batch.append(cmd)
        count += 1

        if len(batch) >= _BATCH_SIZE:
            for c in batch:
                order_adapter.order_queue.put_nowait(c)
            batch.clear()
            await asyncio.sleep(_BATCH_DELAY_S)

    # Flush remaining
    for c in batch:
        order_adapter.order_queue.put_nowait(c)

    halt_cancel_orders_total.inc(count)
    logger.warning(
        "halt_cancel_enqueued",
        count=count,
        storm_guard_state=int(storm_guard.state),
    )
    return count
