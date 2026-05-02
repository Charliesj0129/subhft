"""Tests for TCA arrival_price stamping in _dispatch_to_api."""

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
from hft_platform.core import timebase
from hft_platform.order.adapter import OrderAdapter


class _StubCodec:
    def encode_side(self, side):
        return "Buy"

    def encode_tif(self, tif):
        return "ROD"

    def encode_price_type(self, price_type):
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
        md.return_value = MagicMock()
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
    return client


def _make_intent(decision_price: int = 1_000_000) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="strat1",
        symbol="2330",
        side=Side.BUY,
        qty=1,
        price=1_000_000,
        intent_type=IntentType.NEW,
        source_ts_ns=timebase.now_ns(),
        decision_price=decision_price,
    )


def _make_cmd(intent: OrderIntent, arrival_price: int = 0) -> OrderCommand:
    return OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=timebase.now_ns() + 10**10,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=timebase.now_ns(),
        decision_price=int(intent.decision_price),
        arrival_price=arrival_price,
    )


def _make_adapter(tmp_config, client=None, mid_price_fn=None):
    if client is None:
        client = _make_client()
    q: asyncio.Queue = asyncio.Queue()
    adapter = OrderAdapter(
        config_path=tmp_config,
        order_queue=q,
        broker_client=client,
        broker_codec=_StubCodec(),
        mid_price_fn=mid_price_fn,
    )
    return adapter


@pytest.mark.asyncio
async def test_tca_arrival_stamped_from_mid_price_fn(tmp_config):
    """When arrival_price=0 (RiskEngine path), _dispatch_to_api stamps from _mid_price_fn."""
    mid_price = 1_005_000
    adapter = _make_adapter(tmp_config, mid_price_fn=lambda sym: mid_price)

    intent = _make_intent(decision_price=1_000_000)
    cmd = _make_cmd(intent, arrival_price=0)

    await adapter._dispatch_to_api(cmd)

    order_key = f"{intent.strategy_id}:{intent.intent_id}"
    assert order_key in adapter._cmd_tca_map
    stored_decision, stored_arrival = adapter._cmd_tca_map[order_key]
    assert stored_decision == 1_000_000
    assert stored_arrival == mid_price


@pytest.mark.asyncio
async def test_tca_arrival_preserved_when_already_set(tmp_config):
    """When arrival_price is already set, it is NOT overwritten."""
    adapter = _make_adapter(tmp_config, mid_price_fn=lambda sym: 9_999_999)

    intent = _make_intent(decision_price=1_000_000)
    cmd = _make_cmd(intent, arrival_price=1_002_000)

    await adapter._dispatch_to_api(cmd)

    order_key = f"{intent.strategy_id}:{intent.intent_id}"
    _, stored_arrival = adapter._cmd_tca_map[order_key]
    assert stored_arrival == 1_002_000


@pytest.mark.asyncio
async def test_tca_arrival_falls_back_to_decision_price(tmp_config):
    """When _mid_price_fn is None and arrival=0, falls back to decision_price."""
    adapter = _make_adapter(tmp_config, mid_price_fn=None)

    intent = _make_intent(decision_price=1_000_000)
    cmd = _make_cmd(intent, arrival_price=0)

    await adapter._dispatch_to_api(cmd)

    order_key = f"{intent.strategy_id}:{intent.intent_id}"
    _, stored_arrival = adapter._cmd_tca_map[order_key]
    assert stored_arrival == 1_000_000
