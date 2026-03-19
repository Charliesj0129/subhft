"""Tests for WU-02: StartupPositionVerifier."""

from __future__ import annotations

import json
import os
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.execution.positions import Position, PositionStore
from hft_platform.execution.startup_recon import StartupPositionVerifier, startup_recon_status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeBrokerPosition:
    """Mimics a broker position object."""

    def __init__(self, code: str, quantity: int, direction: str = "") -> None:
        self.code = code
        self.quantity = quantity
        self.direction = direction


class FakeBrokerClient:
    """Minimal broker client stub exposing get_positions."""

    def __init__(self, positions: List[Any] | None = None) -> None:
        self._positions: List[Any] = positions if positions is not None else []

    def get_positions(self) -> List[Any]:
        return self._positions


def _make_store_with_positions(entries: dict[str, int]) -> PositionStore:
    """Build a PositionStore pre-loaded with {symbol: net_qty} entries."""
    store = PositionStore()
    for symbol, qty in entries.items():
        key = f"ACC:STRAT:{symbol}"
        pos = Position(account_id="ACC", strategy_id="STRAT", symbol=symbol, net_qty=qty)
        store.positions[key] = pos
    return store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matching_positions() -> None:
    """No discrepancies when broker and local positions match."""
    client = FakeBrokerClient([
        FakeBrokerPosition("2330", 100),
        FakeBrokerPosition("2317", 50),
    ])
    store = _make_store_with_positions({"2330": 100, "2317": 50})
    verifier = StartupPositionVerifier(client, store)

    result = await verifier.verify()

    assert result == []
    assert verifier.status == 1
    assert startup_recon_status._value.get() == 1


@pytest.mark.asyncio
async def test_mismatch_detected() -> None:
    """Discrepancies are reported when positions differ."""
    client = FakeBrokerClient([
        FakeBrokerPosition("2330", 100),
        FakeBrokerPosition("2454", 200),
    ])
    store = _make_store_with_positions({"2330": 100, "2454": 150})
    verifier = StartupPositionVerifier(client, store)

    result = await verifier.verify()

    assert len(result) == 1
    assert result[0].symbol == "2454"
    assert result[0].local_qty == 150
    assert result[0].broker_qty == 200
    assert result[0].diff == -50
    assert verifier.status == 2
    assert startup_recon_status._value.get() == 2


@pytest.mark.asyncio
async def test_mismatch_extra_symbols() -> None:
    """Discrepancies include symbols only present on one side."""
    client = FakeBrokerClient([FakeBrokerPosition("2330", 100)])
    store = _make_store_with_positions({"2317": 50})
    verifier = StartupPositionVerifier(client, store)

    result = await verifier.verify()

    symbols = {d.symbol for d in result}
    assert "2330" in symbols  # broker only
    assert "2317" in symbols  # local only
    assert len(result) == 2


@pytest.mark.asyncio
async def test_blocking_mode_raises_on_mismatch() -> None:
    """In blocking mode, RuntimeError is raised when discrepancies exist."""
    client = FakeBrokerClient([FakeBrokerPosition("2330", 100)])
    store = _make_store_with_positions({"2330": 200})
    verifier = StartupPositionVerifier(client, store, blocking=True)

    with pytest.raises(RuntimeError, match="discrepancies found in blocking mode"):
        await verifier.verify()

    assert verifier.status == 2


@pytest.mark.asyncio
async def test_blocking_mode_passes_when_matching() -> None:
    """Blocking mode does not raise when positions match."""
    client = FakeBrokerClient([FakeBrokerPosition("2330", 100)])
    store = _make_store_with_positions({"2330": 100})
    verifier = StartupPositionVerifier(client, store, blocking=True)

    result = await verifier.verify()

    assert result == []
    assert verifier.status == 1


@pytest.mark.asyncio
async def test_blocking_mode_from_env() -> None:
    """HFT_STARTUP_RECON_BLOCK=1 enables blocking mode."""
    with patch.dict(os.environ, {"HFT_STARTUP_RECON_BLOCK": "1"}):
        client = FakeBrokerClient([FakeBrokerPosition("2330", 100)])
        store = _make_store_with_positions({"2330": 200})
        verifier = StartupPositionVerifier(client, store)

        assert verifier.blocking is True
        with pytest.raises(RuntimeError):
            await verifier.verify()


@pytest.mark.asyncio
async def test_checkpoint_loading(tmp_path) -> None:
    """Checkpoint file supplements local positions for symbols not in store."""
    checkpoint_file = tmp_path / "checkpoint.json"
    checkpoint_file.write_text(json.dumps({"2454": 300}))

    # Broker has 2454=300 but local store is empty — checkpoint fills the gap
    client = FakeBrokerClient([FakeBrokerPosition("2454", 300)])
    store = _make_store_with_positions({})
    verifier = StartupPositionVerifier(
        client, store, checkpoint_path=str(checkpoint_file)
    )

    result = await verifier.verify()

    assert result == []
    assert verifier.status == 1


@pytest.mark.asyncio
async def test_checkpoint_does_not_override_store(tmp_path) -> None:
    """Checkpoint data does not override existing PositionStore entries."""
    checkpoint_file = tmp_path / "checkpoint.json"
    checkpoint_file.write_text(json.dumps({"2330": 999}))

    client = FakeBrokerClient([FakeBrokerPosition("2330", 100)])
    store = _make_store_with_positions({"2330": 100})
    verifier = StartupPositionVerifier(
        client, store, checkpoint_path=str(checkpoint_file)
    )

    result = await verifier.verify()

    # Checkpoint value (999) is ignored because 2330 is already in store
    assert result == []


@pytest.mark.asyncio
async def test_checkpoint_missing_file() -> None:
    """Missing checkpoint file is handled gracefully."""
    client = FakeBrokerClient([])
    store = _make_store_with_positions({})
    verifier = StartupPositionVerifier(
        client, store, checkpoint_path="/nonexistent/checkpoint.json"
    )

    result = await verifier.verify()

    assert result == []
    assert verifier.status == 1


@pytest.mark.asyncio
async def test_broker_unavailable_non_blocking() -> None:
    """Broker failure in non-blocking mode sets status=3 without raising."""
    client = MagicMock()
    client.get_positions.side_effect = ConnectionError("broker down")
    store = _make_store_with_positions({})
    verifier = StartupPositionVerifier(client, store, blocking=False)

    result = await verifier.verify()

    assert result == []
    assert verifier.status == 3
    assert startup_recon_status._value.get() == 3


@pytest.mark.asyncio
async def test_broker_unavailable_blocking_raises() -> None:
    """Broker failure in blocking mode raises RuntimeError."""
    client = MagicMock()
    client.get_positions.side_effect = ConnectionError("broker down")
    store = _make_store_with_positions({})
    verifier = StartupPositionVerifier(client, store, blocking=True)

    with pytest.raises(RuntimeError, match="verification error in blocking mode"):
        await verifier.verify()

    assert verifier.status == 3


@pytest.mark.asyncio
async def test_empty_positions_both_sides() -> None:
    """No discrepancies when both broker and local are empty."""
    client = FakeBrokerClient([])
    store = _make_store_with_positions({})
    verifier = StartupPositionVerifier(client, store)

    result = await verifier.verify()

    assert result == []
    assert verifier.status == 1


@pytest.mark.asyncio
async def test_sell_direction_negates_qty() -> None:
    """Broker positions with Action.Sell direction produce negative qty."""
    client = FakeBrokerClient([FakeBrokerPosition("2330", 100, "Action.Sell")])
    store = _make_store_with_positions({"2330": -100})
    verifier = StartupPositionVerifier(client, store)

    result = await verifier.verify()

    assert result == []
    assert verifier.status == 1
