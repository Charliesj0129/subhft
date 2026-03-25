"""Tests for PositionFlattener: flatten_all empty, with positions, idempotent."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.ops.position_flattener import FlattenResult, PositionFlattener


def _make_flattener(positions: dict[str, int] | None = None) -> PositionFlattener:
    store = MagicMock()
    if positions is None:
        positions = {}
    store.get_open_positions = MagicMock(return_value=positions)
    adapter = MagicMock()
    adapter.submit_intent = AsyncMock()
    adapter.cancel_all_for_symbols = AsyncMock()
    return PositionFlattener(position_store=store, order_adapter=adapter)


class TestFlattenAllEmpty:
    @pytest.mark.asyncio()
    async def test_returns_zero_result(self) -> None:
        flattener = _make_flattener({})
        result = await flattener.flatten_all()
        assert result.fully_closed == 0
        assert result.failed == 0
        assert result.partially_closed == 0


class TestFlattenWithPositions:
    @pytest.mark.asyncio()
    async def test_closes_long_position(self) -> None:
        flattener = _make_flattener({"2330": 100})
        result = await flattener.flatten_all()
        assert result.fully_closed == 1
        assert result.failed == 0

    @pytest.mark.asyncio()
    async def test_closes_short_position(self) -> None:
        flattener = _make_flattener({"2317": -50})
        result = await flattener.flatten_all()
        assert result.fully_closed == 1

    @pytest.mark.asyncio()
    async def test_multiple_positions(self) -> None:
        flattener = _make_flattener({"2330": 100, "2317": -50, "2454": 200})
        result = await flattener.flatten_all()
        assert result.fully_closed == 3
        assert result.failed == 0


class TestFlattenIdempotent:
    @pytest.mark.asyncio()
    async def test_second_flatten_same_result(self) -> None:
        flattener = _make_flattener({"2330": 100})
        result1 = await flattener.flatten_all()
        result2 = await flattener.flatten_all()
        assert result1.fully_closed == result2.fully_closed


class TestFlattenResult:
    def test_default_values(self) -> None:
        r = FlattenResult()
        assert r.fully_closed == 0
        assert r.partially_closed == 0
        assert r.failed == 0
        assert r.failed_symbols == []
