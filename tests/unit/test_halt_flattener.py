"""Tests for HaltFlattener (WU-03)."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from hft_platform.contracts.strategy import IntentType, Side
from hft_platform.risk.halt_flattener import HaltFlattener


@dataclass
class FakePosition:
    """Minimal position stub for testing."""

    symbol: str
    strategy_id: str
    net_qty: int


class FakePositionStore:
    """Minimal PositionStore stub."""

    def __init__(self, positions: dict | None = None) -> None:
        self.positions: dict = positions or {}


class TestHaltFlattener:
    """Unit tests for HaltFlattener."""

    # ------------------------------------------------------------------
    # Construction / config
    # ------------------------------------------------------------------

    def test_disabled_by_default(self) -> None:
        store = FakePositionStore()
        submit = AsyncMock()
        flattener = HaltFlattener(store, submit)
        assert not flattener.enabled

    def test_enabled_via_param(self) -> None:
        store = FakePositionStore()
        submit = AsyncMock()
        flattener = HaltFlattener(store, submit, enabled=True)
        assert flattener.enabled

    # ------------------------------------------------------------------
    # on_halt when disabled
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_on_halt_disabled_returns_zero(self) -> None:
        store = FakePositionStore({"k": FakePosition("2330", "s1", 10)})
        submit = AsyncMock()
        flattener = HaltFlattener(store, submit, enabled=False)
        result = await flattener.on_halt()
        assert result == 0
        submit.assert_not_called()

    # ------------------------------------------------------------------
    # on_halt with no positions
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_on_halt_no_positions(self) -> None:
        store = FakePositionStore()
        submit = AsyncMock()
        flattener = HaltFlattener(store, submit, enabled=True)
        result = await flattener.on_halt()
        assert result == 0

    # ------------------------------------------------------------------
    # on_halt with zero-qty positions (skip)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_on_halt_skips_flat_positions(self) -> None:
        store = FakePositionStore(
            {
                "k1": FakePosition("2330", "s1", 0),
                "k2": FakePosition("2317", "s1", 0),
            }
        )
        submit = AsyncMock()
        flattener = HaltFlattener(store, submit, enabled=True)
        result = await flattener.on_halt()
        assert result == 0
        submit.assert_not_called()

    # ------------------------------------------------------------------
    # on_halt with long position -> SELL
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_on_halt_long_emits_sell(self) -> None:
        store = FakePositionStore(
            {
                "k1": FakePosition("2330", "s1", 5),
            }
        )
        submit = AsyncMock()
        flattener = HaltFlattener(store, submit, enabled=True)
        result = await flattener.on_halt()
        assert result == 1
        submit.assert_called_once()
        intent = submit.call_args[0][0]
        assert intent.side == Side.SELL
        assert intent.qty == 5
        assert intent.symbol == "2330"
        assert intent.intent_type == IntentType.NEW
        assert intent.reason == "halt_flatten"

    # ------------------------------------------------------------------
    # on_halt with short position -> BUY
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_on_halt_short_emits_buy(self) -> None:
        store = FakePositionStore(
            {
                "k1": FakePosition("2454", "s2", -3),
            }
        )
        submit = AsyncMock()
        flattener = HaltFlattener(store, submit, enabled=True)
        result = await flattener.on_halt()
        assert result == 1
        intent = submit.call_args[0][0]
        assert intent.side == Side.BUY
        assert intent.qty == 3
        assert intent.symbol == "2454"

    # ------------------------------------------------------------------
    # on_halt with multiple positions
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_on_halt_multiple_positions(self) -> None:
        store = FakePositionStore(
            {
                "k1": FakePosition("2330", "s1", 10),
                "k2": FakePosition("2317", "s1", -5),
                "k3": FakePosition("2454", "s2", 0),  # Flat, should skip
            }
        )
        submit = AsyncMock()
        flattener = HaltFlattener(store, submit, enabled=True)
        result = await flattener.on_halt()
        assert result == 2
        assert submit.call_count == 2

    # ------------------------------------------------------------------
    # submit_fn error handling
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_on_halt_continues_on_submit_error(self) -> None:
        """If one submit fails, flattener should continue to next position."""
        store = FakePositionStore(
            {
                "k1": FakePosition("2330", "s1", 10),
                "k2": FakePosition("2317", "s1", 5),
            }
        )

        call_count = 0

        async def flaky_submit(intent):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("broker down")

        flattener = HaltFlattener(store, flaky_submit, enabled=True)
        result = await flattener.on_halt()
        # First fails, second succeeds
        assert result == 1
        assert call_count == 2

    # ------------------------------------------------------------------
    # Intent ID uniqueness
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_intent_ids_are_unique(self) -> None:
        store = FakePositionStore(
            {
                "k1": FakePosition("2330", "s1", 1),
                "k2": FakePosition("2317", "s1", 1),
            }
        )
        submitted: list = []
        submit = AsyncMock(side_effect=lambda i: submitted.append(i))
        flattener = HaltFlattener(store, submit, enabled=True)
        await flattener.on_halt()
        ids = [i.intent_id for i in submitted]
        assert len(ids) == len(set(ids))
