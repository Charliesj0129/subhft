"""Tests for order coalescing key correctness.

Verifies that distinct NEW orders for the same strategy+symbol are NOT
silently dropped by the coalesce window (X-C2 fix).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.order.adapter import OrderAdapter


@pytest.fixture()
def tmp_config(tmp_path):
    cfg = tmp_path / "order.yaml"
    cfg.write_text(
        "rate_limits:\n"
        "  shioaji_soft_cap: 180\n"
        "  shioaji_hard_cap: 250\n"
        "  window_seconds: 10\n"
        "circuit_breaker:\n"
        "  threshold: 5\n"
        "  timeout_seconds: 60\n"
    )
    return str(cfg)


@pytest.fixture(autouse=True)
def _mock_infra():
    with (
        patch("hft_platform.order.adapter.MetricsRegistry") as mm,
        patch("hft_platform.order.adapter.LatencyRecorder") as ml,
        patch("hft_platform.order.adapter.SymbolMetadata"),
        patch("hft_platform.order.adapter.PriceCodec"),
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.order.adapter.get_dlq") as md,
    ):
        metrics = MagicMock()
        metrics.order_reject_total = MagicMock()
        metrics.order_actions_total = MagicMock()
        metrics.order_actions_total.labels.return_value = MagicMock()
        mm.get.return_value = metrics
        ml.get.return_value = MagicMock()
        md.return_value = MagicMock()
        yield


def _make_adapter(tmp_config: str) -> OrderAdapter:
    order_q: asyncio.Queue = asyncio.Queue(maxsize=128)
    client = MagicMock()
    client.place_order = MagicMock(return_value={"seq_no": "A1", "ord_no": "B2"})
    client.cancel_order = MagicMock(return_value={})
    client.update_order = MagicMock(return_value={})
    client.get_exchange = MagicMock(return_value="TSE")
    client.mode = "simulation"
    client.activate_ca = False
    return OrderAdapter(config_path=tmp_config, order_queue=order_q, broker_client=client)


def _make_cmd(
    intent_id: int,
    intent_type: IntentType = IntentType.NEW,
    symbol: str = "2330",
    strategy_id: str = "s1",
    target_order_id: str = "",
) -> OrderCommand:
    intent = OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=Side.BUY,
        price=5_000_000,
        qty=10,
        target_order_id=target_order_id,
    )
    return OrderCommand(
        cmd_id=intent_id, intent=intent, deadline_ns=0, storm_guard_state=StormGuardState.NORMAL, created_ns=0
    )


def test_new_orders_with_different_intent_id_not_coalesced(tmp_config):
    """Two NEW orders for same strategy+symbol but different intent_id must NOT merge."""
    adapter = _make_adapter(tmp_config)
    cmd1 = _make_cmd(intent_id=1)
    cmd2 = _make_cmd(intent_id=2)

    adapter._store_pending(cmd1)
    adapter._store_pending(cmd2)

    # Both commands should be in _api_pending with different keys
    keys = list(adapter._api_pending.keys())
    assert len(keys) == 2, f"Expected 2 pending commands, got {len(keys)}: {keys}"
    assert adapter._api_pending[adapter._coalesce_key(cmd1)] is cmd1
    assert adapter._api_pending[adapter._coalesce_key(cmd2)] is cmd2


def test_cancel_orders_for_same_target_do_coalesce(tmp_config):
    """Two CANCEL orders for the same target_order_id SHOULD coalesce (last wins)."""
    adapter = _make_adapter(tmp_config)
    cmd1 = _make_cmd(intent_id=10, intent_type=IntentType.CANCEL, target_order_id="ORD-001")
    cmd2 = _make_cmd(intent_id=11, intent_type=IntentType.CANCEL, target_order_id="ORD-001")

    adapter._store_pending(cmd1)
    adapter._store_pending(cmd2)

    cancel_keys = [k for k in adapter._api_pending if k[0] == "cancel"]
    assert len(cancel_keys) == 1, "CANCEL orders for same target should coalesce"
    assert adapter._api_pending[cancel_keys[0]] is cmd2  # last wins


def test_coalesce_key_includes_intent_id_for_new(tmp_config):
    """Verify the coalesce key structure includes intent_id for NEW orders."""
    adapter = _make_adapter(tmp_config)
    cmd = _make_cmd(intent_id=42)
    key = adapter._coalesce_key(cmd)
    assert key == ("new", "s1", "2330", 42)
