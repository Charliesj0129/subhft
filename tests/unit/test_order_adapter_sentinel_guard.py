"""Tests for OrderAdapter: CANCEL/AMEND _PENDING_SENTINEL guard.

Verifies that when live_orders contains _PENDING_SENTINEL or
_TERMINAL_BEFORE_REGISTERED for a target order, CANCEL and AMEND
dispatch paths do NOT call the broker SDK, but DO increment
order_reject_total.
"""

import asyncio
from typing import Any
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
from hft_platform.order.adapter import (
    OrderAdapter,
    _PENDING_SENTINEL,
    _TERMINAL_BEFORE_REGISTERED,
)


class _StubCodec:
    def encode_side(self, side: Any) -> str:
        return "Buy"

    def encode_tif(self, tif: Any) -> str:
        return "ROD"

    def encode_price_type(self, price_type: Any) -> str:
        return "LMT"


@pytest.fixture
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
        mp_inst.descale.return_value = 500.0
        mp.return_value = mp_inst
        meta_inst = MagicMock()
        meta_inst.order_params.return_value = {}
        ms.return_value = meta_inst
        yield


def _make_client():
    client = MagicMock()
    client.place_order = MagicMock(
        return_value=MagicMock(seq_no="S1", ord_no="O1", order_id="ID1", id="X1", order=None)
    )
    client.cancel_order = MagicMock(return_value=MagicMock())
    client.update_order = MagicMock(return_value=MagicMock())
    client.get_exchange = MagicMock(return_value="TSE")
    return client


def _make_intent(
    intent_type: IntentType = IntentType.CANCEL,
    *,
    intent_id: int = 1,
    strategy_id: str = "strat1",
    symbol: str = "2330",
    side: Side = Side.BUY,
    price: int = 500_0000,
    qty: int = 1,
    target_order_id: str | None = "99",
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=price,
        qty=qty,
        target_order_id=target_order_id,
    )


def _make_cmd(intent: OrderIntent) -> OrderCommand:
    return OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=timebase.now_ns() + 10**10,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=0,
    )


def _make_adapter(tmp_config: str, client=None) -> OrderAdapter:
    if client is None:
        client = _make_client()
    q: asyncio.Queue = asyncio.Queue()
    adapter = OrderAdapter(config_path=tmp_config, order_queue=q, broker_client=client, broker_codec=_StubCodec())
    return adapter


def _mock_metrics(adapter: OrderAdapter) -> MagicMock:
    """Replace adapter.metrics with a MagicMock and return it."""
    metrics_mock = MagicMock()
    metrics_mock.order_reject_total = MagicMock()
    metrics_mock.order_actions_total = MagicMock()
    metrics_mock.order_actions_total.labels.return_value = MagicMock()
    adapter.metrics = metrics_mock
    return metrics_mock


# ---------------------------------------------------------------------------
# Test 1: CANCEL with _PENDING_SENTINEL must NOT call cancel_order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_with_pending_sentinel_does_not_call_broker(tmp_config):
    """CANCEL targeting a _PENDING_SENTINEL order must skip broker cancel_order."""
    client = _make_client()
    adapter = _make_adapter(tmp_config, client)
    metrics = _mock_metrics(adapter)

    # Place sentinel for target order key "strat1:99"
    adapter.live_orders["strat1:99"] = _PENDING_SENTINEL

    intent = _make_intent(IntentType.CANCEL, strategy_id="strat1", target_order_id="99")
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    client.cancel_order.assert_not_called()
    metrics.order_reject_total.inc.assert_called_once()


# ---------------------------------------------------------------------------
# Test 2: AMEND with _PENDING_SENTINEL must NOT call update_order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amend_with_pending_sentinel_does_not_call_broker(tmp_config):
    """AMEND targeting a _PENDING_SENTINEL order must skip broker update_order."""
    client = _make_client()
    adapter = _make_adapter(tmp_config, client)
    metrics = _mock_metrics(adapter)

    adapter.live_orders["strat1:99"] = _PENDING_SENTINEL

    intent = _make_intent(IntentType.AMEND, strategy_id="strat1", target_order_id="99")
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    client.update_order.assert_not_called()
    metrics.order_reject_total.inc.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3: CANCEL with a real trade object still calls cancel_order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_with_real_trade_calls_broker(tmp_config):
    """CANCEL with a real (non-sentinel) trade object must call broker cancel_order."""
    client = _make_client()
    adapter = _make_adapter(tmp_config, client)
    metrics = _mock_metrics(adapter)

    fake_trade = MagicMock()
    adapter.live_orders["strat1:99"] = fake_trade

    intent = _make_intent(IntentType.CANCEL, strategy_id="strat1", target_order_id="99")
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    client.cancel_order.assert_called_once()
    assert client.cancel_order.call_args[0][0] is fake_trade
    metrics.order_reject_total.inc.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: CANCEL with _TERMINAL_BEFORE_REGISTERED must NOT call cancel_order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_with_terminal_before_registered_does_not_call_broker(tmp_config):
    """CANCEL targeting _TERMINAL_BEFORE_REGISTERED must skip broker cancel_order."""
    client = _make_client()
    adapter = _make_adapter(tmp_config, client)
    metrics = _mock_metrics(adapter)

    adapter.live_orders["strat1:99"] = _TERMINAL_BEFORE_REGISTERED

    intent = _make_intent(IntentType.CANCEL, strategy_id="strat1", target_order_id="99")
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    client.cancel_order.assert_not_called()
    metrics.order_reject_total.inc.assert_called_once()


# ---------------------------------------------------------------------------
# Test 5: AMEND with _TERMINAL_BEFORE_REGISTERED must NOT call update_order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amend_with_terminal_before_registered_does_not_call_broker(tmp_config):
    """AMEND targeting _TERMINAL_BEFORE_REGISTERED must skip broker update_order."""
    client = _make_client()
    adapter = _make_adapter(tmp_config, client)
    metrics = _mock_metrics(adapter)

    adapter.live_orders["strat1:99"] = _TERMINAL_BEFORE_REGISTERED

    intent = _make_intent(IntentType.AMEND, strategy_id="strat1", target_order_id="99")
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    client.update_order.assert_not_called()
    metrics.order_reject_total.inc.assert_called_once()


# ---------------------------------------------------------------------------
# Test 6: CANCEL missing target (None) still increments reject counter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_missing_target_increments_reject(tmp_config):
    """CANCEL with no matching live order (None) must increment order_reject_total."""
    client = _make_client()
    adapter = _make_adapter(tmp_config, client)
    metrics = _mock_metrics(adapter)

    # No entry in live_orders for strat1:99

    intent = _make_intent(IntentType.CANCEL, strategy_id="strat1", target_order_id="99")
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    client.cancel_order.assert_not_called()
    metrics.order_reject_total.inc.assert_called_once()
