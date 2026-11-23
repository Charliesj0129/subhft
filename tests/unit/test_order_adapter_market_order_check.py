"""The MKT/MKP-requires-IOC/FOK check must read the canonical order, not the broker encoding.

The Fubon codec returns SDK enums (``PriceType.Market``, ``TimeInForce.ROD``), which never equal the
strings "MKT"/"ROD", so a check on the encoded values let every Fubon market order with ROD through.
The Shioaji codec returns the same strings the canonical order carries, so its behaviour must not move.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.unit.fubon_mock_helper import PriceType, TimeInForce, install_fubon_neo_mock

install_fubon_neo_mock()

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side, StormGuardState
from hft_platform.core import timebase
from hft_platform.feed_adapter.fubon.order_codec import FubonOrderCodec
from hft_platform.feed_adapter.shioaji.order_codec import ShioajiOrderCodec


@pytest.fixture
def tmp_config(tmp_path: Any) -> str:
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
def _mock_deps(tmp_path: Any) -> Any:
    with (
        patch.dict(os.environ, {"HFT_ORDER_ID_MAP_PERSIST_PATH": str(tmp_path / "oid_map.jsonl")}),
        patch("hft_platform.order.adapter.MetricsRegistry") as metrics_registry,
        patch("hft_platform.order.adapter.LatencyRecorder") as latency,
        patch("hft_platform.order.adapter.SymbolMetadata") as symbol_metadata,
        patch("hft_platform.order.adapter.PriceCodec") as price_codec,
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.order.adapter.get_dlq") as dlq,
    ):
        metrics = MagicMock()
        metrics.order_actions_total.labels.return_value = MagicMock()
        metrics_registry.get.return_value = metrics
        latency.get.return_value = MagicMock()
        dlq.return_value = AsyncMock()
        price_codec.return_value.descale.return_value = 500.0
        meta = MagicMock()
        meta.exchange.return_value = "TAIFEX"
        meta.product_type.return_value = "future"
        meta.order_params.return_value = {}
        symbol_metadata.return_value = meta
        yield


def _make_adapter(tmp_config: str, codec: Any) -> Any:
    from hft_platform.order.adapter import OrderAdapter

    client = MagicMock()
    client.place_order = MagicMock(return_value=MagicMock(seq_no="S1", ord_no="O1", order_id="ID1", order=None))
    client.get_exchange = MagicMock(return_value="TAIFEX")
    client.mode = "simulation"
    client.activate_ca = False
    adapter = OrderAdapter(
        config_path=tmp_config, order_queue=asyncio.Queue(), broker_client=client, broker_codec=codec
    )
    adapter.shadow_sink.enabled = False
    return adapter


def _make_cmd(*, price_type: str, tif: TIF, intent_id: int = 1) -> OrderCommand:
    intent = OrderIntent(
        intent_id=intent_id,
        strategy_id="s1",
        symbol="TMFK6",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=22000_0000,
        qty=1,
        tif=tif,
        price_type=price_type,
    )
    return OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=timebase.now_ns() + 10**10,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=timebase.now_ns(),
    )


def test_fubon_codec_encodes_market_and_rod_to_enums_that_do_not_equal_the_canonical_strings() -> None:
    codec = FubonOrderCodec()

    assert codec.encode_price_type("MKT") == PriceType.Market
    assert codec.encode_price_type("MKT") != "MKT"
    assert codec.encode_tif(TIF.LIMIT) == TimeInForce.ROD
    assert codec.encode_tif(TIF.LIMIT) != "ROD"


@pytest.mark.asyncio
@pytest.mark.parametrize("tif", [TIF.LIMIT, TIF.ROD])
async def test_dispatch_new_fubon_codec_market_rod_is_rejected(tmp_config: str, tif: TIF) -> None:
    adapter = _make_adapter(tmp_config, FubonOrderCodec())

    result = await adapter._dispatch_to_api(_make_cmd(price_type="MKT", tif=tif))

    assert result is False
    adapter.client.place_order.assert_not_called()
    adapter.metrics.order_reject_total.inc.assert_called_once()
    assert "s1:1" not in adapter.live_orders


@pytest.mark.asyncio
@pytest.mark.parametrize("tif", [TIF.IOC, TIF.FOK])
async def test_dispatch_new_fubon_codec_market_ioc_or_fok_is_sent(tmp_config: str, tif: TIF) -> None:
    adapter = _make_adapter(tmp_config, FubonOrderCodec())

    result = await adapter._dispatch_to_api(_make_cmd(price_type="MKT", tif=tif))

    assert result is True
    sent = adapter.client.place_order.call_args.kwargs
    assert sent["price_type"] == PriceType.Market
    assert sent["tif"] in (TimeInForce.IOC, TimeInForce.FOK)


@pytest.mark.asyncio
async def test_dispatch_new_fubon_codec_limit_rod_is_sent(tmp_config: str) -> None:
    adapter = _make_adapter(tmp_config, FubonOrderCodec())

    result = await adapter._dispatch_to_api(_make_cmd(price_type="LMT", tif=TIF.LIMIT))

    assert result is True
    adapter.client.place_order.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("price_type", ["MKT", "MKP"])
@pytest.mark.parametrize("tif", [TIF.LIMIT, TIF.ROD])
async def test_dispatch_new_shioaji_codec_market_rod_is_still_rejected(
    tmp_config: str, price_type: str, tif: TIF
) -> None:
    adapter = _make_adapter(tmp_config, ShioajiOrderCodec())

    result = await adapter._dispatch_to_api(_make_cmd(price_type=price_type, tif=tif))

    assert result is False
    adapter.client.place_order.assert_not_called()
    adapter.metrics.order_reject_total.inc.assert_called_once()
    assert "s1:1" not in adapter.live_orders


@pytest.mark.asyncio
@pytest.mark.parametrize(("price_type", "tif"), [("MKT", TIF.IOC), ("MKP", TIF.FOK), ("LMT", TIF.LIMIT)])
async def test_dispatch_new_shioaji_codec_valid_combinations_are_still_sent(
    tmp_config: str, price_type: str, tif: TIF
) -> None:
    adapter = _make_adapter(tmp_config, ShioajiOrderCodec())

    result = await adapter._dispatch_to_api(_make_cmd(price_type=price_type, tif=tif))

    assert result is True
    adapter.client.place_order.assert_called_once()
