"""Bug #29: Cancel-target-not-found race. When a strategy emits CANCEL for an
order whose Filled/Cancelled callback already fired, the adapter logs WARNING +
increments ``order_reject_total`` + DLQs the intent. This is a benign race —
the desired post-condition (order not working) already holds.

Fix: track recently terminal order_keys in a bounded LRU. If a CANCEL target
is absent from ``live_orders`` BUT present in the LRU, treat as success
(``not_found_local``). True unknown order_ids (typos, strategy bugs) still
WARN + DLQ.
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
        metrics_mock.order_cancel_already_terminal_total = MagicMock()
        metrics_mock.order_cancel_already_terminal_total.labels.return_value = MagicMock()
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
        yield metrics_mock


def _make_client():
    client = MagicMock()
    client.place_order = MagicMock(return_value=MagicMock())
    client.cancel_order = MagicMock(return_value=MagicMock())
    client.update_order = MagicMock(return_value=MagicMock())
    client.get_exchange = MagicMock(return_value="TSE")
    client.mode = "simulation"
    client.activate_ca = False
    return client


def _make_adapter(tmp_config: str):
    from hft_platform.order.adapter import OrderAdapter

    client = _make_client()
    q: asyncio.Queue = asyncio.Queue()
    adapter = OrderAdapter(
        config_path=tmp_config,
        order_queue=q,
        broker_client=client,
        broker_codec=_StubCodec(),
    )
    adapter.shadow_sink.enabled = False
    return adapter


def _make_cancel_intent(intent_id: int, target_order_id: str) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="strat1",
        symbol="TMFE6",
        intent_type=IntentType.CANCEL,
        side=Side.BUY,
        price=0,
        qty=0,
        target_order_id=target_order_id,
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
async def test_cancel_after_recent_terminal_logs_info_no_dlq(tmp_config, mock_deps):
    """Race (a): order_id was just removed from live_orders by on_terminal_state.
    A CANCEL for that order_id arriving moments later must be demoted to INFO,
    increment ``order_cancel_already_terminal_total``, and NOT increment
    ``order_reject_total`` or hit DLQ."""
    adapter = _make_adapter(tmp_config)
    target_oid = "OID-recently-filled"

    # Simulate: order was live then on_terminal_state removed it (filled/cancelled)
    adapter._record_recent_terminal(f"strat1:{target_oid}", reason="filled")

    cancel = _make_cancel_intent(intent_id=99, target_order_id=target_oid)
    cmd = _make_cmd(cancel)

    result = await adapter._dispatch_to_api(cmd)

    assert result is True, "demoted cancel must report success"
    mock_deps.order_cancel_already_terminal_total.labels.assert_called_with(reason="not_found_local")
    mock_deps.order_cancel_already_terminal_total.labels.return_value.inc.assert_called()
    mock_deps.order_reject_total.inc.assert_not_called()
    adapter._dlq.add.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_for_never_seen_order_id_still_warns_and_dlqs(tmp_config, mock_deps):
    """Negative: a CANCEL for an order_id that NEVER existed (typo, strategy bug)
    must preserve the existing WARNING + order_reject_total + DLQ behavior, and
    must NOT increment ``order_cancel_already_terminal_total``."""
    adapter = _make_adapter(tmp_config)

    cancel = _make_cancel_intent(intent_id=100, target_order_id="OID-typo-never-existed")
    cmd = _make_cmd(cancel)

    result = await adapter._dispatch_to_api(cmd)

    assert result is False, "true unknown cancel must report failure"
    mock_deps.order_reject_total.inc.assert_called_once()
    mock_deps.order_cancel_already_terminal_total.labels.assert_not_called()
    adapter._dlq.add.assert_called_once()


@pytest.mark.asyncio
async def test_recently_terminal_lru_bounded(tmp_config):
    """LRU must be bounded to prevent OOM on long-running engines."""
    adapter = _make_adapter(tmp_config)
    cap = adapter._recently_terminal_max
    for i in range(cap + 50):
        adapter._record_recent_terminal(f"strat:{i}", reason="filled")
    assert len(adapter._recently_terminal_orders) <= cap
    assert adapter._is_recently_terminal(f"strat:{cap + 49}") is True
    assert adapter._is_recently_terminal("strat:0") is False, "oldest entry should be evicted"
