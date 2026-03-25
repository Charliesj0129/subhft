"""Tests for AutonomyMonitor flatten gate polling."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hft_platform.ops.autonomy_monitor import _handle_flatten_request
from hft_platform.ops.flatten_gate import FlattenGate, FlattenStatus
from hft_platform.ops.position_flattener import FlattenResult


@pytest.fixture()
def gate(tmp_path: Path) -> FlattenGate:
    return FlattenGate(path=tmp_path / "flatten_request.json")


class TestAutonomyFlattenPoll:
    @pytest.mark.asyncio()
    async def test_polls_and_executes_pending_request(self, gate: FlattenGate) -> None:
        """Verify that a pending request is claimed and the flattener is called."""
        gate.submit(scope="all", deadline_s=60)

        flattener = AsyncMock()
        flattener.flatten_all = AsyncMock(return_value=FlattenResult(fully_closed=3, partially_closed=0, failed=0))

        await _handle_flatten_request(gate, flattener)

        flattener.flatten_all.assert_awaited_once()
        req = gate.read_request()
        assert req is not None
        assert req.status == FlattenStatus.COMPLETED
        assert req.fully_closed == 3

    @pytest.mark.asyncio()
    async def test_skips_when_no_request(self, gate: FlattenGate) -> None:
        """No-op when no request file exists."""
        flattener = AsyncMock()

        await _handle_flatten_request(gate, flattener)

        flattener.flatten_all.assert_not_awaited()
        assert gate.read_request() is None
