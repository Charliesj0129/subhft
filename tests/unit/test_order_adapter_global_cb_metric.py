"""Tests for global CircuitBreaker Prometheus metric emission in OrderAdapter.

Verifies that _update_cb_metric() is called after record_failure / record_success
and that circuit_breaker_state.labels(component="order_adapter").set() receives
the correct values (1=open, 0=closed).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.core import timebase
from hft_platform.order.adapter import OrderAdapter
from hft_platform.order.circuit_breaker import CircuitBreaker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STRATEGY_ID = "strat_cb_metric_test"


def _make_intent(**overrides: Any) -> OrderIntent:
    defaults: dict[str, Any] = {
        "intent_id": 1,
        "strategy_id": STRATEGY_ID,
        "symbol": "2330",
        "intent_type": IntentType.NEW,
        "side": Side.BUY,
        "price": 5950000,
        "qty": 1,
        "tif": TIF.LIMIT,
    }
    defaults.update(overrides)
    return OrderIntent(**defaults)


def _make_cmd(intent: OrderIntent, cmd_id: int = 1) -> OrderCommand:
    deadline_ns = time.monotonic_ns() + 10_000_000_000  # 10s
    return OrderCommand(
        cmd_id=cmd_id,
        intent=intent,
        deadline_ns=deadline_ns,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=timebase.now_ns(),
    )


def _make_adapter(tmp_path: Any, client: Any = None) -> OrderAdapter:
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
        client.place_order = MagicMock(
            return_value=MagicMock(seq_no="S1", ord_no="O1", id="X1", order=None)
        )
        client.cancel_order = MagicMock()
        client.get_exchange = MagicMock(return_value="TSE")
    queue: asyncio.Queue[OrderCommand] = asyncio.Queue()
    adapter = OrderAdapter(str(config_file), queue, client)
    return adapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_deps():
    with (
        patch("hft_platform.order.adapter.MetricsRegistry") as mm,
        patch("hft_platform.order.adapter.LatencyRecorder") as ml,
        patch("hft_platform.order.adapter.SymbolMetadata") as ms,
        patch("hft_platform.order.adapter.PriceCodec") as mp,
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.order.adapter.get_dlq") as md,
    ):
        mm.get.return_value = MagicMock()
        ml.get.return_value = MagicMock()
        md.return_value = AsyncMock()
        mp_inst = MagicMock()
        mp_inst.descale.return_value = 595.0
        mp.return_value = mp_inst
        meta_inst = MagicMock()
        meta_inst.order_params.return_value = {}
        ms.return_value = meta_inst
        yield


# ---------------------------------------------------------------------------
# Test 1: failure path emits set(1) when CB trips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cb_metric_set_to_1_when_tripped(tmp_path):
    """After enough failures to trip the global CB, set(1) is called on the gauge."""
    client = MagicMock()
    client.place_order = MagicMock(side_effect=OSError("broker down"))
    client.get_exchange = MagicMock(return_value="TSE")

    adapter = _make_adapter(tmp_path, client=client)

    # Replace CB with a low-threshold one so we can trip it quickly
    adapter.circuit_breaker = CircuitBreaker(threshold=2, timeout_s=60)

    # Build a mock gauge label handle and wire it into metrics
    gauge_label_mock = MagicMock()
    adapter.metrics.circuit_breaker_state = MagicMock()
    adapter.metrics.circuit_breaker_state.labels.return_value = gauge_label_mock

    # Set up codec so dispatch proceeds to broker call
    codec = MagicMock()
    codec.encode_side.return_value = "Buy"
    codec.encode_tif.return_value = "ROD"
    codec.encode_price_type.return_value = "LMT"
    adapter._broker_codec = codec

    intent = _make_intent()
    cmd = _make_cmd(intent)

    # First failure: CB still closed → set(0) expected
    await adapter._dispatch_to_api(cmd)
    assert gauge_label_mock.set.called
    # Last call after first failure: CB is still closed (threshold=2, only 1 failure)
    last_val = gauge_label_mock.set.call_args_list[-1].args[0]
    assert last_val == 0, f"Expected 0 (closed) after 1 failure, got {last_val}"

    # Second failure: CB trips → set(1) expected
    await adapter._dispatch_to_api(cmd)
    last_val = gauge_label_mock.set.call_args_list[-1].args[0]
    assert last_val == 1, f"Expected 1 (open) after threshold reached, got {last_val}"

    # Verify labels were called with correct component tag every time
    for c in adapter.metrics.circuit_breaker_state.labels.call_args_list:
        assert c == call(component="order_adapter"), f"Unexpected labels call: {c}"


# ---------------------------------------------------------------------------
# Test 2: success path emits set(0) after CB resets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cb_metric_set_to_0_after_timeout_reset(tmp_path):
    """After the CB timeout expires and a success is recorded, set(0) is called."""
    trade_mock = MagicMock()
    trade_mock.seq_no = "S1"
    trade_mock.ord_no = "O1"
    trade_mock.id = "X1"
    trade_mock.order = None

    client = MagicMock()
    client.place_order = MagicMock(return_value=trade_mock)
    client.get_exchange = MagicMock(return_value="TSE")

    adapter = _make_adapter(tmp_path, client=client)

    # Pre-trip the CB using a very short timeout and manually expire it
    adapter.circuit_breaker = CircuitBreaker(threshold=2, timeout_s=0)
    adapter.circuit_breaker.record_failure()
    adapter.circuit_breaker.record_failure()
    # CB is now open; with timeout_s=0 it should immediately allow next check
    assert adapter.circuit_breaker.is_open() is False, (
        "CB with timeout_s=0 should auto-close after calling is_open()"
    )

    # Wire the metric mock
    gauge_label_mock = MagicMock()
    adapter.metrics.circuit_breaker_state = MagicMock()
    adapter.metrics.circuit_breaker_state.labels.return_value = gauge_label_mock

    codec = MagicMock()
    codec.encode_side.return_value = "Buy"
    codec.encode_tif.return_value = "ROD"
    codec.encode_price_type.return_value = "LMT"
    adapter._broker_codec = codec

    intent = _make_intent()
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    # After a successful dispatch, set(0) should have been called
    assert gauge_label_mock.set.called, "set() should be called after success"
    set_values = [c.args[0] for c in gauge_label_mock.set.call_args_list]
    assert 0 in set_values, f"Expected set(0) in calls: {set_values}"


# ---------------------------------------------------------------------------
# Test 3: _update_cb_metric silently absorbs gauge exceptions
# ---------------------------------------------------------------------------


def test_update_cb_metric_does_not_raise_on_metrics_error(tmp_path):
    """_update_cb_metric must never propagate exceptions from the metrics layer."""
    adapter = _make_adapter(tmp_path)

    # Make the gauge raise an unexpected error
    adapter.metrics.circuit_breaker_state = MagicMock()
    adapter.metrics.circuit_breaker_state.labels.side_effect = RuntimeError("metrics broken")

    # Must not raise
    result = adapter._update_cb_metric()

    # Metrics errors are silently swallowed; circuit breaker state is unchanged
    assert result is None
    assert not adapter.circuit_breaker.is_open()


# ---------------------------------------------------------------------------
# Test 4: validate_client failure path also emits metric
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cb_metric_emitted_on_validate_client_failure(tmp_path):
    """When _validate_client returns False, _update_cb_metric is called."""
    adapter = _make_adapter(tmp_path)

    gauge_label_mock = MagicMock()
    adapter.metrics.circuit_breaker_state = MagicMock()
    adapter.metrics.circuit_breaker_state.labels.return_value = gauge_label_mock

    # Force _validate_client to return False
    adapter._validate_client = MagicMock(return_value=False)  # type: ignore[method-assign]

    # Bypass other checks
    adapter._broker_codec = MagicMock()

    intent = _make_intent()
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    adapter.metrics.circuit_breaker_state.labels.assert_called_with(component="order_adapter")
    assert gauge_label_mock.set.called, "set() must be called after validate_client failure"
