"""Bug 28 regression: Shioaji Trade is Pydantic v2 strict — setting unknown
field raises ValueError (NOT AttributeError/TypeError). The dispatch path's
timestamp injection swallowed only AttributeError/TypeError, so every
NEW dispatch propagated the ValueError up to ``_api_worker`` and was marked
``phantom_dispatch_failed`` despite ``place_order`` already succeeding.

Operational impact (observed 2026-04-20 04:21 UTC):
- R47_MAKER_TMF placed 2 TMFE6 orders → both reached broker → both filled
  (position_pnl_realized credited 12 NTD)
- Adapter recorded both as phantom_dispatch_failed → pending counters poisoned
- phantom_recovery_releases_total fired 30s later via Bug-D TTL sweep

Fix: include ValueError in the timestamp-injection except tuples at
``adapter.py:1777`` and ``adapter.py:1907``.
"""

from __future__ import annotations

import asyncio
import os
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


class _StubCodec:
    def encode_side(self, side: Any) -> str:
        return "Buy"

    def encode_tif(self, tif: Any) -> str:
        return "IOC"

    def encode_price_type(self, price_type: Any) -> str:
        return "LMT"


class _StrictPydanticTrade:
    """Mimics Shioaji v2 Trade: rejects unknown fields with ValueError."""

    seq_no = "S1"
    ord_no = "O1"
    order_id = "ID1"
    id = "X1"
    order = None
    status = None

    def __setattr__(self, name: str, value: Any) -> None:  # noqa: D401
        raise ValueError(f'"Trade" object has no field "{name}"')


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
def mock_deps(tmp_path):
    with (
        patch.dict(os.environ, {"HFT_ORDER_ID_MAP_PERSIST_PATH": str(tmp_path / "oid_map.jsonl")}),
        patch("hft_platform.order.adapter.MetricsRegistry") as mm,
        patch("hft_platform.order.adapter.LatencyRecorder") as ml,
        patch("hft_platform.order.adapter.SymbolMetadata") as ms,
        patch("hft_platform.order.adapter.PriceCodec") as mp,
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.order.adapter.get_dlq") as md,
    ):
        metrics_mock = MagicMock()
        metrics_mock.order_reject_total = MagicMock()
        metrics_mock.order_actions_total = MagicMock()
        metrics_mock.order_actions_total.labels.return_value = MagicMock()
        metrics_mock.circuit_breaker_state = MagicMock()
        mm.get.return_value = metrics_mock
        ml.get.return_value = MagicMock()
        md.return_value = AsyncMock()
        mp_inst = MagicMock()
        mp_inst.descale.return_value = 500.0
        mp.return_value = mp_inst
        meta_inst = MagicMock()
        meta_inst.exchange.return_value = "TSE"
        meta_inst.product_type.return_value = None
        meta_inst.order_params.return_value = {}
        ms.return_value = meta_inst
        yield


def _make_client_returning_strict_trade():
    client = MagicMock()
    client.place_order = MagicMock(return_value=_StrictPydanticTrade())
    client.cancel_order = MagicMock(return_value=MagicMock())
    client.update_order = MagicMock(return_value=MagicMock())
    client.get_exchange = MagicMock(return_value="TSE")
    client.mode = "simulation"
    client.activate_ca = False
    return client


def _make_adapter(tmp_config: str):
    from hft_platform.order.adapter import OrderAdapter

    client = _make_client_returning_strict_trade()
    q: asyncio.Queue = asyncio.Queue()
    adapter = OrderAdapter(
        config_path=tmp_config,
        order_queue=q,
        broker_client=client,
        broker_codec=_StubCodec(),
    )
    adapter.shadow_sink.enabled = False
    return adapter


def _make_intent(intent_id: int = 1) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="strat1",
        symbol="TMFE6",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=500_0000,
        qty=1,
        target_order_id=None,
        reason="",
    )


def _make_cmd(intent: OrderIntent) -> OrderCommand:
    return OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=timebase.now_ns() + 10**10,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=timebase.now_ns(),
    )


@pytest.mark.asyncio
async def test_dispatch_succeeds_when_trade_setattr_raises_value_error(tmp_config):
    """Bug 28: Pydantic-strict Trade with __setattr__→ValueError must NOT
    cause _dispatch_to_api to propagate / return False. Order succeeded at
    broker; adapter must record it in live_orders despite timestamp setattr
    failure."""
    adapter = _make_adapter(tmp_config)
    intent = _make_intent(intent_id=42)
    cmd = _make_cmd(intent)

    # Must not raise; must return True (dispatch succeeded at broker)
    result = await adapter._dispatch_to_api(cmd)

    assert result is True, "dispatch must succeed even when trade.timestamp setattr raises ValueError"
    order_key = "strat1:42"
    assert order_key in adapter.live_orders, "trade must be recorded in live_orders"
