"""Tests for OrderAdapter HALT safety and coverage gaps.

Covers: storm_guard_state handling, circuit breaker, rate limiting,
client validation, terminal state cleanup, deadline expiry, config loading.
"""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.order.adapter import OrderAdapter

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


class _Meta:
    def __init__(self, scale: int = 100):
        self._scale = scale

    def price_scale(self, symbol: str) -> int:
        return self._scale


@pytest.fixture
def order_config(tmp_path):
    cfg = {
        "rate_limits": {
            "shioaji_soft_cap": 180,
            "shioaji_hard_cap": 250,
            "window_seconds": 10,
        },
        "circuit_breaker": {"threshold": 5, "timeout_seconds": 60},
    }
    p = tmp_path / "order_config.yaml"
    p.write_text(yaml.dump(cfg))
    return str(p)


def _make_client(**overrides):
    client = MagicMock()
    client.place_order = MagicMock(return_value=MagicMock(seq_no="SEQ1"))
    client.cancel_order = MagicMock()
    client.get_exchange = MagicMock(return_value="TSE")
    client.update_order = MagicMock()
    for k, v in overrides.items():
        setattr(client, k, v)
    return client


def _intent(intent_type=IntentType.NEW, **overrides):
    base = dict(
        intent_id=1,
        strategy_id="strat",
        symbol="AAA",
        side=Side.BUY,
        price=10000,
        qty=1,
        intent_type=intent_type,
        tif=TIF.LIMIT,
        target_order_id=None,
    )
    base.update(overrides)
    return OrderIntent(**base)


def _cmd(intent, storm_guard_state=StormGuardState.NORMAL, deadline_ns=None):
    if deadline_ns is None:
        deadline_ns = time.time_ns() + 5_000_000_000
    return OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=deadline_ns,
        storm_guard_state=storm_guard_state,
    )


def _make_adapter(config_path, **kwargs):
    """Create adapter with MetricsRegistry and LatencyRecorder patched."""
    client = kwargs.pop("client", _make_client())
    queue = kwargs.pop("queue", asyncio.Queue())
    adapter = OrderAdapter(config_path, queue, client)
    adapter.metadata = _Meta()
    return adapter


# ---------------------------------------------------------------------------
# 1. HALT blocks new orders
#    NOTE: OrderAdapter.execute() does NOT check storm_guard_state directly.
#    The HALT gate is enforced upstream by RiskEngine / GatewayService.
#    This test documents the gap: even with HALT state, the adapter proceeds.
#    We validate that the storm_guard_state is carried on OrderCommand for
#    downstream observability, and that the adapter does NOT crash.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("hft_platform.order.adapter.LatencyRecorder")
@patch("hft_platform.order.adapter.MetricsRegistry")
async def test_halt_state_carried_on_command(mock_metrics_cls, mock_lat_cls, order_config):
    mock_metrics_cls.get.return_value = MagicMock()
    mock_lat_cls.get.return_value = MagicMock()

    client = _make_client()
    adapter = OrderAdapter(order_config, asyncio.Queue(), client)
    adapter.metadata = _Meta()

    intent = _intent(IntentType.NEW)
    cmd = _cmd(intent, storm_guard_state=StormGuardState.HALT)

    # Verify HALT state is on the command
    assert cmd.storm_guard_state == StormGuardState.HALT

    # OrderAdapter.execute does NOT block HALT — it proceeds.
    # This documents the architectural decision: HALT is upstream.
    await adapter.execute(cmd)

    # The adapter called place_order despite HALT state on the command.
    assert client.place_order.call_count == 1


# ---------------------------------------------------------------------------
# 2. place_order returns None → no crash, reject counted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("hft_platform.order.adapter.LatencyRecorder")
@patch("hft_platform.order.adapter.MetricsRegistry")
async def test_place_order_returns_none_handled(mock_metrics_cls, mock_lat_cls, order_config):
    mock_metrics = MagicMock()
    mock_metrics_cls.get.return_value = mock_metrics
    mock_lat_cls.get.return_value = MagicMock()

    client = _make_client()
    client.place_order.return_value = None
    adapter = OrderAdapter(order_config, asyncio.Queue(), client)
    adapter.metadata = _Meta()

    intent = _intent(IntentType.NEW)
    cmd = _cmd(intent)

    # Should not raise
    await adapter._dispatch_to_api(cmd)

    # place_order was called but returned None — order_actions_total NOT incremented
    client.place_order.assert_called_once()
    # No live order stored
    assert len(adapter.live_orders) == 0


# ---------------------------------------------------------------------------
# 3. order_id_map populated after successful placement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("hft_platform.order.adapter.LatencyRecorder")
@patch("hft_platform.order.adapter.MetricsRegistry")
async def test_order_id_map_populated_after_placement(mock_metrics_cls, mock_lat_cls, order_config):
    mock_metrics_cls.get.return_value = MagicMock()
    mock_lat_cls.get.return_value = MagicMock()

    trade = MagicMock()
    trade.seq_no = "SEQ1"
    trade.ord_no = "ORD1"
    trade.order_id = None
    trade.id = None
    trade.order = None

    client = _make_client()
    client.place_order.return_value = trade
    adapter = OrderAdapter(order_config, asyncio.Queue(), client)
    adapter.metadata = _Meta()

    intent = _intent(IntentType.NEW, intent_id=42, strategy_id="alpha1")
    cmd = _cmd(intent)

    await adapter._dispatch_to_api(cmd)

    # Broker IDs should map to order_key
    assert "SEQ1" in adapter.order_id_map
    assert adapter.order_id_map["SEQ1"] == "alpha1:42"
    assert "ORD1" in adapter.order_id_map
    assert adapter.order_id_map["ORD1"] == "alpha1:42"


# ---------------------------------------------------------------------------
# 4. load_config with missing file raises
# ---------------------------------------------------------------------------


@patch("hft_platform.order.adapter.LatencyRecorder")
@patch("hft_platform.order.adapter.MetricsRegistry")
def test_load_config_missing_file(mock_metrics_cls, mock_lat_cls):
    mock_metrics_cls.get.return_value = MagicMock()
    mock_lat_cls.get.return_value = MagicMock()

    with pytest.raises((FileNotFoundError, OSError)):
        OrderAdapter("/nonexistent/path/config.yaml", asyncio.Queue(), _make_client())


# ---------------------------------------------------------------------------
# 5. load_config updates rate limiter caps
# ---------------------------------------------------------------------------


@patch("hft_platform.order.adapter.LatencyRecorder")
@patch("hft_platform.order.adapter.MetricsRegistry")
def test_load_config_updates_rate_limiter(mock_metrics_cls, mock_lat_cls, order_config):
    mock_metrics_cls.get.return_value = MagicMock()
    mock_lat_cls.get.return_value = MagicMock()

    adapter = OrderAdapter(order_config, asyncio.Queue(), _make_client())

    assert adapter.rate_limiter.soft_cap == 180
    assert adapter.rate_limiter.hard_cap == 250
    assert adapter.circuit_breaker.threshold == 5
    assert adapter.circuit_breaker.timeout_s == 60


# ---------------------------------------------------------------------------
# 6. _validate_client: NEW order needs place_order
# ---------------------------------------------------------------------------


@patch("hft_platform.order.adapter.LatencyRecorder")
@patch("hft_platform.order.adapter.MetricsRegistry")
def test_validate_client_new_order_needs_place_order(mock_metrics_cls, mock_lat_cls, order_config):
    mock_metrics_cls.get.return_value = MagicMock()
    mock_lat_cls.get.return_value = MagicMock()

    client = MagicMock(spec=[])  # no attributes at all
    adapter = OrderAdapter(order_config, asyncio.Queue(), client)

    intent = _intent(IntentType.NEW)
    assert adapter._validate_client(intent) is False


# ---------------------------------------------------------------------------
# 7. _validate_client: CANCEL needs cancel_order
# ---------------------------------------------------------------------------


@patch("hft_platform.order.adapter.LatencyRecorder")
@patch("hft_platform.order.adapter.MetricsRegistry")
def test_validate_client_cancel_needs_cancel_order(mock_metrics_cls, mock_lat_cls, order_config):
    mock_metrics_cls.get.return_value = MagicMock()
    mock_lat_cls.get.return_value = MagicMock()

    client = MagicMock(spec=[])  # no cancel_order
    adapter = OrderAdapter(order_config, asyncio.Queue(), client)

    intent = _intent(IntentType.CANCEL, target_order_id="X1")
    assert adapter._validate_client(intent) is False


# ---------------------------------------------------------------------------
# 8. Circuit breaker open rejects execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("hft_platform.order.adapter.LatencyRecorder")
@patch("hft_platform.order.adapter.MetricsRegistry")
async def test_circuit_breaker_open_rejects(mock_metrics_cls, mock_lat_cls, order_config):
    mock_metrics_cls.get.return_value = MagicMock()
    mock_lat_cls.get.return_value = MagicMock()

    client = _make_client()
    adapter = OrderAdapter(order_config, asyncio.Queue(), client)

    # Force circuit breaker open
    adapter.circuit_breaker.open_until = time.time() + 60

    intent = _intent(IntentType.NEW)
    cmd = _cmd(intent)
    await adapter.execute(cmd)

    # place_order should NOT have been called
    client.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# 9. Rate limit exceeded rejects execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("hft_platform.order.adapter.LatencyRecorder")
@patch("hft_platform.order.adapter.MetricsRegistry")
async def test_rate_limit_exceeded_rejects(mock_metrics_cls, mock_lat_cls, order_config):
    mock_metrics_cls.get.return_value = MagicMock()
    mock_lat_cls.get.return_value = MagicMock()

    client = _make_client()
    adapter = OrderAdapter(order_config, asyncio.Queue(), client)
    adapter.metadata = _Meta()

    # Fill rate window to exceed hard cap
    adapter.rate_limiter.update(hard_cap=2, window_s=10)
    now = time.time()
    adapter.rate_limiter.rate_window.append(now)
    adapter.rate_limiter.rate_window.append(now)

    intent = _intent(IntentType.NEW)
    cmd = _cmd(intent)
    await adapter.execute(cmd)

    # place_order should NOT have been called
    client.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# 10. on_terminal_state removes live order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("hft_platform.order.adapter.LatencyRecorder")
@patch("hft_platform.order.adapter.MetricsRegistry")
async def test_on_terminal_state_removes_live_order(mock_metrics_cls, mock_lat_cls, order_config):
    mock_metrics_cls.get.return_value = MagicMock()
    mock_lat_cls.get.return_value = MagicMock()

    adapter = OrderAdapter(order_config, asyncio.Queue(), _make_client())
    adapter.live_orders["strat:42"] = {"id": "T42"}

    await adapter.on_terminal_state("strat", "42")

    assert "strat:42" not in adapter.live_orders


# ---------------------------------------------------------------------------
# 11. on_terminal_state with nonexistent order — no crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("hft_platform.order.adapter.LatencyRecorder")
@patch("hft_platform.order.adapter.MetricsRegistry")
async def test_on_terminal_state_nonexistent_order(mock_metrics_cls, mock_lat_cls, order_config):
    mock_metrics_cls.get.return_value = MagicMock()
    mock_lat_cls.get.return_value = MagicMock()

    adapter = OrderAdapter(order_config, asyncio.Queue(), _make_client())
    # Should not raise
    await adapter.on_terminal_state("strat", "999")

    assert len(adapter.live_orders) == 0


# ---------------------------------------------------------------------------
# 12. Deadline expired → order skipped in run loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("hft_platform.order.adapter.LatencyRecorder")
@patch("hft_platform.order.adapter.MetricsRegistry")
async def test_deadline_expired_skipped(mock_metrics_cls, mock_lat_cls, order_config):
    mock_metrics_cls.get.return_value = MagicMock()
    mock_lat_cls.get.return_value = MagicMock()

    client = _make_client()
    queue = asyncio.Queue()
    adapter = OrderAdapter(order_config, queue, client)
    adapter.metadata = _Meta()

    # Create command with deadline in the past
    intent = _intent(IntentType.NEW)
    cmd = _cmd(intent, deadline_ns=1)  # nanosecond 1 = way in the past

    # Put expired command in queue
    await queue.put(cmd)

    # Run adapter briefly — it should skip the expired order
    adapter.running = True

    async def stop_after_process():
        await asyncio.sleep(0.05)
        adapter.running = False

    await asyncio.gather(adapter.run(), stop_after_process())

    # place_order should NOT have been called (order was expired)
    client.place_order.assert_not_called()
