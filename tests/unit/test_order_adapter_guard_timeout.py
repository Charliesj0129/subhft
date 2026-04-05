"""Tests for XB-02: API guard timeout separated from circuit breaker failures.

Verifies that semaphore guard timeouts return a distinct sentinel, do NOT
increment the circuit breaker failure counter, and increment the dedicated
api_guard_timeout_total metric.
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
from hft_platform.order.adapter import OrderAdapter, _GUARD_TIMEOUT

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
        metrics.api_guard_timeout_total = MagicMock()
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
    adapter = OrderAdapter(config_path=tmp_config, order_queue=order_q, broker_client=client)
    adapter.circuit_breaker = MagicMock()
    adapter.circuit_breaker.is_open.return_value = False
    return adapter


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


def _exhaust_semaphore(adapter: OrderAdapter) -> None:
    """Replace semaphore with a 1-slot version and acquire it so next acquire blocks."""
    adapter._api_semaphore = asyncio.Semaphore(1)
    # Acquire the single slot synchronously (Semaphore.acquire is also sync-capable
    # when not awaited, but we use the internal counter directly for reliability)
    adapter._api_semaphore._value = 0  # noqa: SLF001


@pytest.mark.asyncio
async def test_guard_timeout_returns_sentinel_not_none(tmp_config):
    """When the API semaphore guard times out, _call_api returns _GUARD_TIMEOUT (not None)."""
    adapter = _make_adapter(tmp_config)
    adapter._api_guard_timeout_s = 0.001
    _exhaust_semaphore(adapter)

    result = await adapter._call_api(
        "place_order", lambda: None, intent=_make_intent(), max_retries=0,
    )

    assert result is _GUARD_TIMEOUT
    assert result is not None


@pytest.mark.asyncio
async def test_guard_timeout_does_not_call_circuit_breaker_record_failure(tmp_config):
    """Guard timeout must NOT increment circuit breaker failure counter."""
    adapter = _make_adapter(tmp_config)
    adapter._api_guard_timeout_s = 0.001
    _exhaust_semaphore(adapter)

    await adapter._call_api(
        "place_order", lambda: None, intent=_make_intent(), max_retries=0,
    )

    adapter.circuit_breaker.record_failure.assert_not_called()


@pytest.mark.asyncio
async def test_actual_broker_failure_calls_circuit_breaker_record_failure(tmp_config):
    """An actual broker error (non-transient, exhausted retries) DOES call record_failure."""
    adapter = _make_adapter(tmp_config)
    adapter._api_timeout_s = 0.01

    def _raise(*a, **kw):
        raise ValueError("broker exploded")

    result = await adapter._call_api(
        "place_order", _raise, intent=_make_intent(), max_retries=0,
    )

    assert result is None
    adapter.circuit_breaker.record_failure.assert_called_once()


@pytest.mark.asyncio
async def test_guard_timeout_increments_api_guard_timeout_metric(tmp_config):
    """Guard timeout must increment api_guard_timeout_total counter."""
    adapter = _make_adapter(tmp_config)
    adapter._api_guard_timeout_s = 0.001
    _exhaust_semaphore(adapter)

    await adapter._call_api(
        "place_order", lambda: None, intent=_make_intent(), max_retries=0,
    )

    adapter.metrics.api_guard_timeout_total.inc.assert_called_once()
    # Should NOT increment order_reject_total (that's for real failures)
    adapter.metrics.order_reject_total.inc.assert_not_called()


@pytest.mark.asyncio
async def test_guard_timeout_does_not_track_phantom(tmp_config):
    """Guard timeout should NOT create phantom order candidates (no broker call was attempted)."""
    adapter = _make_adapter(tmp_config)
    adapter._api_guard_timeout_s = 0.001
    _exhaust_semaphore(adapter)

    intent = _make_intent()
    await adapter._call_api(
        "place_order", lambda: None, intent=intent, max_retries=0,
    )

    assert len(adapter._phantom_order_keys) == 0
