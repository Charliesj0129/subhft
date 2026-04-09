"""Tests for D-03 phantom order candidate tracking on API timeout.

Verifies that timed-out mutating operations add entries to the phantom order
set and increment the phantom metric, while non-mutating timeouts do not.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    IntentType,
    OrderIntent,
    Side,
)
from hft_platform.order.adapter import OrderAdapter

# ── Fixtures ───────────────────────────────────────────────────────────────


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
    """Patch heavy infra so tests don't need full stack."""
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
        metrics.phantom_order_candidates_total = MagicMock()
        mm.get.return_value = metrics
        ml.get.return_value = MagicMock()
        md.return_value = MagicMock()
        yield


def _make_adapter(tmp_config: str, *, client: Any | None = None) -> OrderAdapter:
    order_q: asyncio.Queue = asyncio.Queue(maxsize=128)
    if client is None:
        client = MagicMock()
        client.place_order = MagicMock(return_value={"seq_no": "A1", "ord_no": "B2"})
        client.cancel_order = MagicMock(return_value={})
        client.update_order = MagicMock(return_value={})
        client.get_exchange = MagicMock(return_value="TSE")
        client.mode = "simulation"
        client.activate_ca = False
    return OrderAdapter(config_path=tmp_config, order_queue=order_q, broker_client=client)


def _make_intent(intent_id: int = 1) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="s1",
        symbol="2330",
        price=100_0000,
        qty=1,
        side=Side.BUY,
        intent_type=IntentType.NEW,
    )


# ── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mutating_timeout_adds_phantom_candidate(tmp_config):
    """A mutating op timeout with intent should add to phantom set and inc metric."""
    adapter = _make_adapter(tmp_config)
    # Make _api_timeout_s very short so we timeout quickly
    adapter._api_timeout_s = 0.01

    # Broker call that hangs forever
    def _hang(*a, **kw):
        import time

        time.sleep(5)

    intent = _make_intent()

    result = await adapter._call_api(
        "place_order",
        _hang,
        intent=intent,
        max_retries=0,
    )

    assert result is None
    # R2-02: 2-part key format matching order_key convention
    expected_key = f"{intent.strategy_id}:{intent.intent_id}"
    assert expected_key in adapter._phantom_order_keys
    adapter.metrics.phantom_order_candidates_total.inc.assert_called_once()


@pytest.mark.asyncio
async def test_non_mutating_timeout_does_not_add_phantom(tmp_config):
    """A non-mutating op timeout should NOT add to phantom set."""
    adapter = _make_adapter(tmp_config)
    adapter._api_timeout_s = 0.01

    def _hang(*a, **kw):
        import time

        time.sleep(5)

    intent = _make_intent()

    result = await adapter._call_api(
        "get_order_status",
        _hang,
        intent=intent,
        max_retries=0,
    )

    assert result is None
    assert len(adapter._phantom_order_keys) == 0
    adapter.metrics.phantom_order_candidates_total.inc.assert_not_called()


@pytest.mark.asyncio
async def test_mutating_timeout_without_intent_no_phantom(tmp_config):
    """A mutating op timeout without intent should NOT add to phantom set."""
    adapter = _make_adapter(tmp_config)
    adapter._api_timeout_s = 0.01

    def _hang(*a, **kw):
        import time

        time.sleep(5)

    result = await adapter._call_api(
        "place_order",
        _hang,
        intent=None,
        max_retries=0,
    )

    assert result is None
    assert len(adapter._phantom_order_keys) == 0
    adapter.metrics.phantom_order_candidates_total.inc.assert_not_called()


def test_get_phantom_candidates_returns_frozenset(tmp_config):
    """get_phantom_candidates returns a frozenset copy of tracked keys."""
    adapter = _make_adapter(tmp_config)
    import time

    now = time.monotonic()
    adapter._phantom_order_keys["s1:1"] = now
    adapter._phantom_order_keys["s2:2"] = now

    result = adapter.get_phantom_candidates()

    assert isinstance(result, frozenset)
    assert result == frozenset({"s1:1", "s2:2"})
    # Mutating the returned set should not affect the adapter
    assert len(adapter._phantom_order_keys) == 2


def test_clear_phantom_candidate_removes_key(tmp_config):
    """clear_phantom_candidate removes the specified key."""
    adapter = _make_adapter(tmp_config)
    import time

    now = time.monotonic()
    adapter._phantom_order_keys["s1:1"] = now
    adapter._phantom_order_keys["s2:2"] = now

    adapter.clear_phantom_candidate("s1:1")

    assert "s1:1" not in adapter._phantom_order_keys
    assert "s2:2" in adapter._phantom_order_keys


def test_clear_phantom_candidate_missing_key_noop(tmp_config):
    """clear_phantom_candidate on a nonexistent key is a no-op."""
    adapter = _make_adapter(tmp_config)
    import time

    adapter._phantom_order_keys["s1:1"] = time.monotonic()

    adapter.clear_phantom_candidate("nonexistent:key:0")

    assert "s1:1" in adapter._phantom_order_keys
    assert len(adapter._phantom_order_keys) == 1


def test_phantom_key_format_is_two_part(tmp_config):
    """R2-02: Phantom key must use 2-part format matching order_key convention."""
    adapter = _make_adapter(tmp_config)
    import time

    adapter._phantom_order_keys["s1:42"] = time.monotonic()

    candidates = adapter.get_phantom_candidates()
    for key in candidates:
        parts = key.split(":")
        assert len(parts) == 2, f"Expected 2-part key, got {len(parts)}-part: {key}"


@pytest.mark.asyncio
async def test_deadline_expired_increments_metric(tmp_config):
    """Expired pre-dispatch orders must increment order_deadline_expired_total."""
    import time as _time

    adapter = _make_adapter(tmp_config)
    adapter.metrics.order_deadline_expired_total = MagicMock()

    # Build a command whose deadline is already in the past
    from hft_platform.contracts.strategy import OrderCommand, StormGuardState

    past_deadline = _time.monotonic_ns() - 1_000_000  # 1 ms in the past
    cmd = OrderCommand(
        cmd_id=1,
        intent=_make_intent(),
        deadline_ns=past_deadline,
        storm_guard_state=StormGuardState.NORMAL,
    )
    await adapter.order_queue.put(cmd)

    # Run the adapter.run() coroutine briefly — the deadline check lives there
    run_task = asyncio.create_task(adapter.run())
    await asyncio.sleep(0.05)
    adapter.running = False
    run_task.cancel()
    try:
        await asyncio.wait_for(run_task, timeout=2.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    adapter.metrics.order_deadline_expired_total.inc.assert_called_once()


@pytest.mark.asyncio
async def test_phantom_set_eviction_at_max_size(tmp_config):
    """R2-03: Phantom set should evict old entries when exceeding max size."""
    adapter = _make_adapter(tmp_config)
    adapter._phantom_order_max = 5  # Low cap for testing
    adapter._api_timeout_s = 0.01

    # Pre-fill with old entries (simulate timestamps from 2 hours ago)
    import time

    old_ts = time.monotonic() - 7200.0  # 2 hours ago
    for i in range(6):
        adapter._phantom_order_keys[f"old_strat:{i}"] = old_ts

    assert len(adapter._phantom_order_keys) == 6

    # Trigger a new phantom via timeout — this should trigger eviction
    def _hang(*a, **kw):
        time.sleep(5)

    intent = _make_intent(intent_id=999)
    await adapter._call_api("place_order", _hang, intent=intent, max_retries=0)

    # Old entries (>1 hour) should be evicted, only the new one remains
    assert len(adapter._phantom_order_keys) <= adapter._phantom_order_max
    assert f"{intent.strategy_id}:{intent.intent_id}" in adapter._phantom_order_keys
