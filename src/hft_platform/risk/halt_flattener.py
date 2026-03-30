"""HALT Auto-Flatten: emit closing orders when StormGuard enters HALT state.

Disabled by default (HFT_HALT_AUTO_FLATTEN=0). When enabled, iterates
all non-zero positions on HALT and emits closing OrderIntents tagged
with reason="halt_flatten" to bypass the HALT check in risk evaluation.
"""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.core import timebase

logger = get_logger("risk.halt_flattener")


class HaltFlattener:
    """Generates closing orders for all open positions on HALT.

    Parameters
    ----------
    position_store : Any
        Object with a ``positions`` dict mapping keys to Position objects.
        Each Position must expose ``symbol``, ``strategy_id``, ``net_qty``.
    submit_fn : Callable
        Async callable that accepts an ``OrderIntent`` and submits it for
        execution. The intent will have ``reason="halt_flatten"`` and should
        be allowed through risk even under HALT via storm_guard_override.
    """

    __slots__ = ("_position_store", "_submit_fn", "_enabled", "_next_intent_id")

    def __init__(
        self,
        position_store: Any,
        submit_fn: Callable[[OrderIntent], Awaitable[None]],
        *,
        enabled: bool | None = None,
    ) -> None:
        self._position_store = position_store
        self._submit_fn = submit_fn
        if enabled is not None:
            self._enabled = enabled
        else:
            self._enabled = os.getenv("HFT_HALT_AUTO_FLATTEN", "0").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        self._next_intent_id = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _allocate_intent_id(self) -> int:
        self._next_intent_id += 1
        return self._next_intent_id

    async def on_halt(self) -> int:
        """Called when StormGuard transitions to HALT.

        Returns the number of closing intents emitted.
        """
        if not self._enabled:
            logger.debug("halt_flattener_disabled")
            return 0

        positions = getattr(self._position_store, "positions", {})
        if not positions:
            logger.info("halt_flatten_no_positions")
            return 0

        emitted = 0
        now_ns = timebase.now_ns()

        for key, pos in list(positions.items()):
            net_qty = getattr(pos, "net_qty", 0)
            if net_qty == 0:
                continue

            symbol = getattr(pos, "symbol", "")
            strategy_id = getattr(pos, "strategy_id", "")

            # Determine closing side: sell if long, buy if short
            if net_qty > 0:
                close_side = Side.SELL
                close_qty = net_qty
            else:
                close_side = Side.BUY
                close_qty = abs(net_qty)

            intent = OrderIntent(
                intent_id=self._allocate_intent_id(),
                strategy_id=strategy_id,
                symbol=symbol,
                intent_type=IntentType.FORCE_FLAT,
                side=close_side,
                price=0,  # Market order — price=0 signals MKT
                qty=close_qty,
                timestamp_ns=now_ns,
                source_ts_ns=now_ns,
                reason="halt_flatten",  # Tagged to bypass HALT check in risk engine
            )

            try:
                await self._submit_fn(intent)
                emitted += 1
                logger.info(
                    "halt_flatten_emit",
                    symbol=symbol,
                    strategy_id=strategy_id,
                    side=close_side.name,
                    qty=close_qty,
                    position_key=key,
                )
            except Exception as exc:
                logger.error(
                    "halt_flatten_submit_error",
                    symbol=symbol,
                    strategy_id=strategy_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        logger.warning("halt_flatten_complete", emitted=emitted, total_positions=len(positions))
        return emitted
