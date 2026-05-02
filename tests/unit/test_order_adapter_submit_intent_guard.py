"""Tests for submit_intent type guard.

Verifies that submit_intent only accepts CANCEL and FORCE_FLAT intents,
rejecting NEW and AMEND to prevent accidental risk bypass.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderIntent,
    Side,
)
from hft_platform.order.adapter import OrderAdapter


def _make_intent(**overrides) -> OrderIntent:
    """Create a minimal OrderIntent with scaled-int price."""
    defaults = {
        "intent_id": 1,
        "strategy_id": "test_strat",
        "symbol": "2330",
        "intent_type": IntentType.CANCEL,
        "side": Side.BUY,
        "price": 5950000,
        "qty": 1,
        "tif": TIF.LIMIT,
    }
    defaults.update(overrides)
    return OrderIntent(**defaults)


def _make_adapter(tmp_path) -> OrderAdapter:
    """Create an OrderAdapter with minimal config and mocked client."""
    config_file = tmp_path / "order_config.yaml"
    config_file.write_text(
        "rate_limits:\n"
        "  shioaji_soft_cap: 180\n"
        "  shioaji_hard_cap: 250\n"
        "  window_seconds: 10\n"
        "circuit_breaker:\n"
        "  threshold: 5\n"
        "  timeout_seconds: 60\n"
    )
    client = MagicMock()
    client.place_order = MagicMock(return_value={"id": "T1"})
    client.cancel_order = MagicMock()
    client.get_exchange = MagicMock(return_value="TSE")
    queue = asyncio.Queue()
    return OrderAdapter(str(config_file), queue, client)


@pytest.mark.asyncio
async def test_submit_intent_accepts_cancel(tmp_path):
    """CANCEL intent passes the type guard and reaches execute."""
    adapter = _make_adapter(tmp_path)
    adapter.execute = AsyncMock()

    intent = _make_intent(intent_type=IntentType.CANCEL)
    await adapter.submit_intent(intent)

    assert adapter.execute.call_count == 1


@pytest.mark.asyncio
async def test_submit_intent_accepts_force_flat(tmp_path):
    """FORCE_FLAT intent passes the type guard and reaches execute."""
    adapter = _make_adapter(tmp_path)
    adapter.execute = AsyncMock()

    intent = _make_intent(intent_type=IntentType.FORCE_FLAT)
    await adapter.submit_intent(intent)

    assert adapter.execute.call_count == 1


@pytest.mark.asyncio
async def test_submit_intent_rejects_new(tmp_path):
    """NEW intent is rejected with ValueError to prevent risk bypass."""
    adapter = _make_adapter(tmp_path)

    intent = _make_intent(intent_type=IntentType.NEW)
    with pytest.raises(ValueError, match="CANCEL/FORCE_FLAT"):
        await adapter.submit_intent(intent)


@pytest.mark.asyncio
async def test_submit_intent_rejects_amend(tmp_path):
    """AMEND intent is rejected with ValueError to prevent risk bypass."""
    adapter = _make_adapter(tmp_path)

    intent = _make_intent(intent_type=IntentType.AMEND)
    with pytest.raises(ValueError, match="CANCEL/FORCE_FLAT"):
        await adapter.submit_intent(intent)
