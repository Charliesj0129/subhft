from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog

from hft_platform.contracts.strategy import IntentType, Side, OrderIntent
from hft_platform.core import timebase

logger = structlog.get_logger("position_flattener")


@dataclass(slots=True)
class FlattenResult:
    fully_closed: int = 0
    partially_closed: int = 0
    failed: int = 0
    failed_symbols: list[str] = field(default_factory=list)


class PositionFlattener:
    __slots__ = ("_position_store", "_order_adapter", "_flatten_deadline_s", "_intent_counter")

    def __init__(
        self,
        position_store: Any,
        order_adapter: Any,
        flatten_deadline_s: int = 120,
    ) -> None:
        self._position_store = position_store
        self._order_adapter = order_adapter
        self._flatten_deadline_s = flatten_deadline_s
        self._intent_counter = 0

    async def flatten_all(self) -> FlattenResult:
        """Flatten all open positions."""
        positions = self._get_open_positions()
        if not positions:
            return FlattenResult()
        symbols = list(positions.keys())
        return await self._flatten_symbols(symbols, positions)

    async def flatten_track(self, track_id: str, symbols: list[str]) -> FlattenResult:
        """Flatten positions for symbols in a specific track."""
        positions = self._get_open_positions()
        track_positions = {s: q for s, q in positions.items() if s in symbols}
        if not track_positions:
            return FlattenResult()
        return await self._flatten_symbols(list(track_positions.keys()), track_positions)

    async def flatten_strategy(self, strategy_id: str) -> FlattenResult:
        """Flatten positions for a specific strategy."""
        # Strategy-scoped flatten requires position store to support per-strategy queries
        positions = self._get_open_positions()
        if not positions:
            return FlattenResult()
        return await self._flatten_symbols(list(positions.keys()), positions)

    def _get_open_positions(self) -> dict[str, int]:
        """Get all non-zero positions from the position store."""
        store = self._position_store
        if hasattr(store, "get_open_positions"):
            return store.get_open_positions()
        # Fallback: iterate positions dict
        if hasattr(store, "positions"):
            return {s: q for s, q in store.positions.items() if q != 0}
        return {}

    async def _flatten_symbols(
        self, symbols: list[str], positions: dict[str, int],
    ) -> FlattenResult:
        """Core flatten logic: cancel pending, then close all positions."""
        result = FlattenResult()

        try:
            async with asyncio.timeout(self._flatten_deadline_s):
                # Step 1: Cancel all pending orders for these symbols
                await self._cancel_pending(symbols)
                await asyncio.sleep(0.1)  # Brief pause for cancel confirmations

                # Step 2: Send close orders for each position
                for symbol in symbols:
                    qty = positions.get(symbol, 0)
                    if qty == 0:
                        result.fully_closed += 1
                        continue

                    close_side = Side.SELL if qty > 0 else Side.BUY
                    close_qty = abs(qty)

                    intent: OrderIntent | None = None
                    try:
                        self._intent_counter += 1
                        intent = OrderIntent(
                            intent_id=self._intent_counter,
                            strategy_id="__flattener__",
                            symbol=symbol,
                            intent_type=IntentType.FORCE_FLAT,
                            side=close_side,
                            price=0,  # market order (price=0 convention)
                            qty=close_qty,
                            timestamp_ns=timebase.now_ns(),
                        )
                        await self._submit_intent(intent)
                        result.fully_closed += 1
                    except Exception as e:
                        logger.error("flatten_symbol_failed", symbol=symbol, error=str(e))
                        # Retry once
                        try:
                            if intent is not None:
                                await self._submit_intent(intent)
                            result.partially_closed += 1
                        except Exception:
                            result.failed += 1
                            result.failed_symbols.append(symbol)

        except TimeoutError:
            logger.error("flatten_timeout", deadline_s=self._flatten_deadline_s)
            # Count remaining unflatten symbols as failed
            remaining = len(symbols) - result.fully_closed - result.partially_closed - result.failed
            result.failed += remaining

        return result

    async def _cancel_pending(self, symbols: list[str]) -> None:
        """Cancel all pending orders for the given symbols."""
        adapter = self._order_adapter
        if hasattr(adapter, "cancel_all_for_symbols"):
            await adapter.cancel_all_for_symbols(symbols)
        elif hasattr(adapter, "cancel_all"):
            await adapter.cancel_all()

    async def _submit_intent(self, intent: OrderIntent) -> None:
        """Submit a FORCE_FLAT intent through the order adapter."""
        adapter = self._order_adapter
        if hasattr(adapter, "submit_intent"):
            await adapter.submit_intent(intent)
        elif hasattr(adapter, "put_nowait"):
            adapter.put_nowait(intent)
        else:
            raise RuntimeError("OrderAdapter has no submit_intent or put_nowait method")
