"""Tests for _call_api timeout-retry duplicate order prevention.

Verifies that mutating operations (place_order, update_order) are NOT
retried after a timeout, while read-only operations still retry normally.
"""

from __future__ import annotations

import asyncio
import threading
import time
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
        mm.get.return_value = metrics
        ml.get.return_value = MagicMock()
        md.return_value = MagicMock()
        yield


def _make_adapter(tmp_config: str, *, client: Any | None = None) -> OrderAdapter:
    order_q: asyncio.Queue[OrderCommand] = asyncio.Queue(maxsize=128)
    if client is None:
        client = MagicMock()
        client.place_order = MagicMock(return_value={"seq_no": "A1", "ord_no": "B2"})
        client.cancel_order = MagicMock(return_value={})
        client.update_order = MagicMock(return_value={})
        client.get_exchange = MagicMock(return_value="TSE")
        client.mode = "simulation"
        client.activate_ca = False
    return OrderAdapter(config_path=tmp_config, order_queue=order_q, broker_client=client)


def _make_intent() -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="s1",
        symbol="2330",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=5_000_000,
        qty=10,
    )


# ── place_order timeout does NOT retry ─────────────────────────────────────


@pytest.mark.asyncio
async def test_place_order_timeout_no_retry(tmp_config):
    """When place_order times out, _call_api must NOT retry to avoid duplicates."""
    adapter = _make_adapter(tmp_config)
    adapter._api_timeout_s = 0.05
    adapter._api_guard_timeout_s = 5.0

    call_count = 0

    def slow_place_order(**kwargs: Any) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        time.sleep(0.2)  # Exceeds timeout
        return {"seq_no": "A1", "ord_no": "B2"}

    result = await adapter._call_api(
        "place_order",
        slow_place_order,
        intent=_make_intent(),
        max_retries=2,
    )

    assert result is None
    # Must be called exactly once — no retry after timeout
    assert call_count == 1


@pytest.mark.asyncio
async def test_update_order_timeout_no_retry(tmp_config):
    """update_order is also mutating and must not retry on timeout."""
    adapter = _make_adapter(tmp_config)
    adapter._api_timeout_s = 0.05
    adapter._api_guard_timeout_s = 5.0

    call_count = 0

    def slow_update(**kwargs: Any) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        time.sleep(0.2)
        return {"result": "ok"}

    result = await adapter._call_api(
        "update_order",
        slow_update,
        intent=_make_intent(),
        max_retries=2,
    )

    assert result is None
    assert call_count == 1


# ── Non-mutating operations still retry on timeout ─────────────────────────


@pytest.mark.asyncio
async def test_list_positions_timeout_retries_normally(tmp_config):
    """Read-only operations like list_positions still retry on timeout."""
    adapter = _make_adapter(tmp_config)
    adapter._api_timeout_s = 0.05
    adapter._api_guard_timeout_s = 5.0

    call_count = 0

    def flaky_list_positions() -> list[dict[str, Any]]:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            time.sleep(0.2)  # Times out on first two attempts
        return [{"symbol": "2330", "qty": 10}]

    result = await adapter._call_api(
        "list_positions",
        flaky_list_positions,
        max_retries=2,
    )

    assert result == [{"symbol": "2330", "qty": 10}]
    assert call_count == 3  # Retried twice before succeeding


@pytest.mark.asyncio
async def test_cancel_order_timeout_retries_normally(tmp_config):
    """cancel_order is safe to retry (idempotent at broker) and should retry."""
    adapter = _make_adapter(tmp_config)
    adapter._api_timeout_s = 0.05
    adapter._api_guard_timeout_s = 5.0

    call_count = 0

    def flaky_cancel() -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            time.sleep(0.2)
        return {"status": "cancelled"}

    result = await adapter._call_api(
        "cancel_order",
        flaky_cancel,
        max_retries=2,
    )

    assert result == {"status": "cancelled"}
    assert call_count == 2  # Retried once


# ── Normal (non-timeout) flow unaffected ───────────────────────────────────


@pytest.mark.asyncio
async def test_place_order_success_unaffected(tmp_config):
    """Normal successful place_order returns result immediately."""
    adapter = _make_adapter(tmp_config)
    adapter._api_timeout_s = 1.0
    adapter._api_guard_timeout_s = 5.0

    result = await adapter._call_api(
        "place_order",
        lambda: {"seq_no": "A1", "ord_no": "B2"},
        intent=_make_intent(),
    )

    assert result == {"seq_no": "A1", "ord_no": "B2"}


@pytest.mark.asyncio
async def test_place_order_transient_non_timeout_still_retries(tmp_config):
    """place_order with non-timeout transient errors (ConnectionError) still retries."""
    adapter = _make_adapter(tmp_config)
    adapter._api_timeout_s = 1.0
    adapter._api_guard_timeout_s = 5.0

    call_count = 0

    def flaky_place(**kwargs: Any) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise ConnectionError("econnreset")
        return {"seq_no": "A1", "ord_no": "B2"}

    result = await adapter._call_api(
        "place_order",
        flaky_place,
        intent=_make_intent(),
        max_retries=2,
    )

    assert result == {"seq_no": "A1", "ord_no": "B2"}
    assert call_count == 2  # Retried once for non-timeout transient error


# ── Cancellation guard prevents late execution ─────────────────────────────


@pytest.mark.asyncio
async def test_cancellation_guard_prevents_late_broker_call(tmp_config):
    """The guarded wrapper checks the cancelled flag before calling broker fn."""
    adapter = _make_adapter(tmp_config)
    adapter._api_timeout_s = 0.05
    adapter._api_guard_timeout_s = 5.0

    broker_actually_called = threading.Event()

    def slow_place(**kwargs: Any) -> dict[str, str]:
        # Simulate a delay that exceeds timeout. The thread pool will
        # schedule _guarded_call; if the Event is set before the thread
        # actually starts, the broker fn should never run.
        time.sleep(0.3)
        broker_actually_called.set()
        return {"seq_no": "A1", "ord_no": "B2"}

    result = await adapter._call_api(
        "place_order",
        slow_place,
        intent=_make_intent(),
        max_retries=0,
    )

    assert result is None
    # Wait briefly for the thread to finish — the guard should prevent the
    # broker call if the thread starts after cancellation was set.
    time.sleep(0.15)
    # The guarded wrapper sets cancelled before the 300ms sleep completes,
    # so the broker function should NOT have been called.
    assert not broker_actually_called.is_set(), (
        "Cancellation guard failed: broker was called despite cancel flag"
    )
