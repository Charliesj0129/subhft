"""Tests for OrderAdapter idempotency dedup in non-gateway path (D-01)."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.core import timebase


def make_cmd(
    intent_type: IntentType = IntentType.NEW,
    symbol: str = "2330",
    price: int = 5_000_000,
    qty: int = 10,
    strategy_id: str = "s1",
    idempotency_key: str = "",
) -> OrderCommand:
    intent = OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=Side.BUY,
        price=price,
        qty=qty,
        idempotency_key=idempotency_key,
    )
    now = timebase.now_ns()
    return OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=now + 10_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=now,
    )


@pytest.fixture
def tmp_config(tmp_path):
    cfg_file = tmp_path / "order_config.yaml"
    cfg_file.write_text(
        "rate_limits:\n"
        "  shioaji_soft_cap: 180\n"
        "  shioaji_hard_cap: 250\n"
        "  window_seconds: 10\n"
        "circuit_breaker:\n"
        "  threshold: 5\n"
        "  timeout_seconds: 60\n"
    )
    return str(cfg_file)


@pytest.fixture
def mock_deps():
    with (
        patch("hft_platform.order.adapter.MetricsRegistry") as mm,
        patch("hft_platform.order.adapter.LatencyRecorder") as ml,
        patch("hft_platform.order.adapter.SymbolMetadata"),
        patch("hft_platform.order.adapter.PriceCodec"),
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.order.adapter.get_dlq") as md,
    ):
        metrics_mock = MagicMock()
        metrics_mock.order_reject_total = MagicMock()
        metrics_mock.order_actions_total = MagicMock()
        metrics_mock.order_actions_total.labels.return_value = MagicMock()
        mm.get.return_value = metrics_mock
        ml.get.return_value = MagicMock()
        dlq_mock = AsyncMock()
        md.return_value = dlq_mock
        yield {"metrics": metrics_mock, "dlq": dlq_mock}


def _make_adapter(tmp_config, env_overrides=None):
    """Create OrderAdapter with optional env var overrides."""
    from hft_platform.order.adapter import OrderAdapter

    env = {"HFT_GATEWAY_ENABLED": "0"}
    if env_overrides:
        env.update(env_overrides)

    with patch.dict(os.environ, env, clear=False):
        client = MagicMock()
        client.place_order = MagicMock()
        client.get_exchange = MagicMock(return_value="TSE")
        q: asyncio.Queue = asyncio.Queue()
        adapter = OrderAdapter(config_path=tmp_config, order_queue=q, broker_client=client)
    return adapter


@pytest.mark.asyncio
async def test_duplicate_key_rejected(tmp_config, mock_deps):
    """Duplicate idempotency_key should be rejected on second execute."""
    adapter = _make_adapter(tmp_config)
    assert adapter._dedup_store is not None

    cmd1 = make_cmd(idempotency_key="order-abc-123")
    cmd2 = make_cmd(idempotency_key="order-abc-123")

    # First call: should pass dedup (reserve slot)
    await adapter.execute(cmd1)

    # Second call with same key: should be rejected
    await adapter.execute(cmd2)

    mock_deps["metrics"].order_reject_total.inc.assert_called()
    mock_deps["dlq"].add.assert_called()
    last_call_kwargs = mock_deps["dlq"].add.call_args
    assert "Duplicate idempotency_key" in str(last_call_kwargs)


@pytest.mark.asyncio
async def test_empty_key_bypasses_dedup(tmp_config, mock_deps):
    """Empty idempotency_key should bypass dedup entirely."""
    adapter = _make_adapter(tmp_config)

    cmd1 = make_cmd(idempotency_key="")
    cmd2 = make_cmd(idempotency_key="")

    # Both should pass dedup (empty key = no dedup)
    await adapter.execute(cmd1)
    await adapter.execute(cmd2)

    # Neither should be rejected for dedup (may be rejected for other reasons,
    # but order_reject_total should not be called for dedup specifically)
    # Check that DLQ was not called with "Duplicate idempotency_key"
    for call in mock_deps["dlq"].add.call_args_list:
        assert "Duplicate idempotency_key" not in str(call)


@pytest.mark.asyncio
async def test_unique_key_passes(tmp_config, mock_deps):
    """Different idempotency_keys should both pass dedup."""
    adapter = _make_adapter(tmp_config)

    cmd1 = make_cmd(idempotency_key="key-001")
    cmd2 = make_cmd(idempotency_key="key-002")

    await adapter.execute(cmd1)
    await adapter.execute(cmd2)

    # Neither should be rejected for dedup
    for call in mock_deps["dlq"].add.call_args_list:
        assert "Duplicate idempotency_key" not in str(call)


@pytest.mark.asyncio
async def test_cancel_bypasses_dedup_even_with_duplicate_key(tmp_config, mock_deps):
    """CANCEL intent should bypass dedup even if idempotency_key is duplicate."""
    adapter = _make_adapter(tmp_config)

    # First: reserve the key with a NEW order
    cmd_new = make_cmd(idempotency_key="cancel-key-001")
    await adapter.execute(cmd_new)

    # Second: CANCEL with same key — must pass (safety-exempt)
    cmd_cancel = make_cmd(intent_type=IntentType.CANCEL, idempotency_key="cancel-key-001")
    await adapter.execute(cmd_cancel)

    # DLQ should NOT have "Duplicate idempotency_key" for the cancel
    for call in mock_deps["dlq"].add.call_args_list:
        assert "Duplicate idempotency_key" not in str(call)


@pytest.mark.asyncio
async def test_force_flat_bypasses_dedup_even_with_duplicate_key(tmp_config, mock_deps):
    """FORCE_FLAT intent should bypass dedup even if idempotency_key is duplicate."""
    adapter = _make_adapter(tmp_config)

    # First: reserve the key with a NEW order
    cmd_new = make_cmd(idempotency_key="ff-key-001")
    await adapter.execute(cmd_new)

    # Second: FORCE_FLAT with same key — must pass (safety-exempt)
    cmd_ff = make_cmd(intent_type=IntentType.FORCE_FLAT, idempotency_key="ff-key-001")
    await adapter.execute(cmd_ff)

    # DLQ should NOT have "Duplicate idempotency_key" for the force_flat
    for call in mock_deps["dlq"].add.call_args_list:
        assert "Duplicate idempotency_key" not in str(call)


def test_dedup_store_not_created_when_gateway_enabled(tmp_config, mock_deps):
    """When HFT_GATEWAY_ENABLED=1, _dedup_store should be None (avoid double-dedup)."""
    adapter = _make_adapter(tmp_config, env_overrides={"HFT_GATEWAY_ENABLED": "1"})
    assert adapter._dedup_store is None


def test_dedup_store_created_when_gateway_disabled(tmp_config, mock_deps):
    """When HFT_GATEWAY_ENABLED=0 (default), _dedup_store should be created."""
    adapter = _make_adapter(tmp_config, env_overrides={"HFT_GATEWAY_ENABLED": "0"})
    assert adapter._dedup_store is not None


@pytest.mark.asyncio
async def test_dedup_slot_committed_false_on_unexpected_exception(tmp_config, mock_deps):
    """R2-01: If execute() raises after dedup reservation, slot must be committed as False."""
    from unittest.mock import patch as _patch

    adapter = _make_adapter(tmp_config)
    assert adapter._dedup_store is not None

    key = "orphan-test-key-001"
    cmd = make_cmd(idempotency_key=key)

    # Force an unexpected exception in the validation path after dedup reserve.
    # Patch _platform_degrade_allows which is called after all rate/CB checks.
    with _patch.object(type(adapter), "_platform_degrade_allows", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            await adapter.execute(cmd)

    # The dedup slot should have been committed as rejected (not left as orphan).
    # Verify by attempting to reserve the same key again — it should return the
    # committed entry (not None which would mean it's still pending/orphaned).
    existing = adapter._dedup_store.check_or_reserve(key)
    assert existing is not None, "Dedup slot was orphaned — should have been committed as False"
    assert existing.approved is False
    assert existing.reason_code == "internal_error"
