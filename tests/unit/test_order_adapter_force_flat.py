"""Tests for FORCE_FLAT bypass in OrderAdapter.

Verifies:
- FORCE_FLAT intents bypass platform degrade reduce-only checks
- CANCEL intents continue to bypass platform degrade checks
- NEW intents are blocked when reduce-only is active (control)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
)
from hft_platform.order.adapter import OrderAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_intent(**overrides) -> OrderIntent:
    defaults = {
        "intent_id": 1,
        "strategy_id": "test_strat",
        "symbol": "2330",
        "intent_type": IntentType.NEW,
        "side": Side.BUY,
        "price": 5950000,  # 595.0 x10000
        "qty": 1,
        "tif": TIF.LIMIT,
    }
    defaults.update(overrides)
    return OrderIntent(**defaults)


def _make_adapter(tmp_path, client=None) -> OrderAdapter:
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
    if client is None:
        client = MagicMock()
        client.place_order = MagicMock(return_value={"id": "T1"})
        client.cancel_order = MagicMock()
        client.get_exchange = MagicMock(return_value="TSE")
    queue: asyncio.Queue[OrderCommand] = asyncio.Queue()
    return OrderAdapter(str(config_file), queue, client)


def _make_reduce_only_controller() -> MagicMock:
    """Controller with reduce_only_active=True that blocks NEW opens."""
    ctrl = MagicMock()
    ctrl.reduce_only_active = True
    # allow_intent returns False for NEW orders that open risk
    ctrl.allow_intent = MagicMock(return_value=False)
    ctrl.reference_available_net_qty = MagicMock(return_value=None)
    return ctrl


# ---------------------------------------------------------------------------
# Test: FORCE_FLAT bypasses platform degrade check
# ---------------------------------------------------------------------------


def test_force_flat_bypasses_platform_degrade(tmp_path):
    adapter = _make_adapter(tmp_path)
    # Replace controller with one that is reduce-only and would block
    adapter.platform_degrade_controller = _make_reduce_only_controller()

    intent = _make_intent(intent_type=IntentType.FORCE_FLAT)
    result = adapter._platform_degrade_allows(intent)

    assert result is True, "FORCE_FLAT must always be allowed even in reduce-only mode"
    # allow_intent should NOT have been called — the bypass is before the controller
    adapter.platform_degrade_controller.allow_intent.assert_not_called()


def test_cancel_bypasses_platform_degrade(tmp_path):
    adapter = _make_adapter(tmp_path)
    adapter.platform_degrade_controller = _make_reduce_only_controller()

    intent = _make_intent(intent_type=IntentType.CANCEL)
    result = adapter._platform_degrade_allows(intent)

    assert result is True, "CANCEL must always be allowed even in reduce-only mode"
    adapter.platform_degrade_controller.allow_intent.assert_not_called()


def test_new_open_blocked_by_platform_degrade(tmp_path):
    """Control test: NEW intents that open risk are blocked in reduce-only mode."""
    adapter = _make_adapter(tmp_path)
    ctrl = _make_reduce_only_controller()
    # Simulate no net position so the intent opens risk
    ctrl.reference_available_net_qty = MagicMock(return_value=0)
    ctrl.allow_intent = MagicMock(return_value=False)
    adapter.platform_degrade_controller = ctrl
    adapter.position_store = None  # no local position store

    intent = _make_intent(intent_type=IntentType.NEW, side=Side.BUY, qty=1)
    # _platform_reduce_only_new_order_allowed is called for NEW in reduce-only
    # We mock _available_close_capacity to return 0
    with patch.object(adapter, "_available_close_capacity", return_value=0):
        result = adapter._platform_degrade_allows(intent)

    assert result is False, "NEW order opening risk should be blocked in reduce-only mode"
