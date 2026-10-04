"""A cancel the broker accepted, but that never took effect, must be noticed.

THESHOW, R47 (SIM), 2026-09-03..10-03: four orders had a cancel dispatched
about one second after the NEW (``audit.orders_log`` dispatched/CANCEL), no
CANCELLED row ever followed, and each order filled 5.6-27 minutes later. Three
of them (2026-09-22) took the position to +2 against max_pos=1. A further 49
orders never reached a terminal row. The adapter wrote the ``dispatched`` audit
row and then never looked at the cancel again; after 300 s the TTL sweep dropped
the Trade handle, so the order could not be cancelled any more.

``check_unconfirmed_cancels`` reports a cancel whose order is still live after
``HFT_CANCEL_CONFIRM_S`` and re-sends it once, before that handle is dropped.
It never touches the strategy's pending slot.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType, OrderCommand, OrderIntent, Side, StormGuardState
from hft_platform.core import timebase
from hft_platform.order import adapter as adapter_module

_SID = "strat1"
_CONFIRM_S = 60.0


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
        mm.get.return_value = metrics_mock
        ml.get.return_value = MagicMock()
        md.return_value = AsyncMock()
        mp.return_value = MagicMock()
        meta_inst = MagicMock()
        meta_inst.exchange.return_value = "TAIFEX"
        meta_inst.product_type.return_value = None
        meta_inst.order_params.return_value = {}
        ms.return_value = meta_inst
        yield metrics_mock


def _adapter(tmp_config: str):
    from hft_platform.order.adapter import OrderAdapter

    client = MagicMock()
    client.place_order = MagicMock(return_value=MagicMock())
    client.cancel_order = MagicMock(return_value=MagicMock())
    client.get_exchange = MagicMock(return_value="TAIFEX")
    client.mode = "simulation"
    client.activate_ca = False
    adapter = OrderAdapter(
        config_path=tmp_config,
        order_queue=asyncio.Queue(),
        broker_client=client,
        broker_codec=_StubCodec(),
    )
    adapter.shadow_sink.enabled = False
    adapter._cancel_confirm_s = _CONFIRM_S
    adapter._audit_writer = MagicMock()
    return adapter


def _cancel_cmd(intent_id: int, target: str) -> OrderCommand:
    intent = OrderIntent(
        intent_id=intent_id,
        strategy_id=_SID,
        symbol="TMFJ6",
        intent_type=IntentType.CANCEL,
        side=Side.BUY,
        price=0,
        qty=0,
        target_order_id=target,
        reason="",
    )
    return OrderCommand(
        cmd_id=intent_id,
        intent=intent,
        deadline_ns=timebase.now_ns() + 10**10,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=timebase.now_ns(),
    )


async def _dispatch_cancel(adapter, target: str = "OID-1") -> tuple[str, Any]:
    """Register a live order and dispatch a cancel for it, as R47 does."""
    key = f"{_SID}:{target}"
    trade = MagicMock(name="trade")
    adapter.live_orders[key] = trade
    assert await adapter._dispatch_to_api(_cancel_cmd(99, target)) is True
    return key, trade


def _age_watch(adapter, key: str, age_s: float) -> None:
    adapter._cancel_watch[key] = adapter._cancel_watch[key]._replace(at=time.monotonic() - age_s)


async def _settle_retries(adapter) -> None:
    pending = list(adapter._cancel_retry_tasks)
    if pending:
        await asyncio.gather(*pending)


def _audit_events(adapter) -> list[str]:
    return [c.args[0].get("event") for c in adapter._audit_writer.log_order.call_args_list]


@pytest.mark.asyncio
async def test_a_dispatched_cancel_is_watched_until_its_order_leaves_live_orders(tmp_config):
    adapter = _adapter(tmp_config)

    key, _ = await _dispatch_cancel(adapter)

    assert key in adapter._cancel_watch
    assert adapter._cancel_watch[key].attempts == 0


@pytest.mark.asyncio
async def test_a_cancel_whose_order_disappeared_is_confirmed_and_dropped(tmp_config, mock_deps):
    adapter = _adapter(tmp_config)
    key, _ = await _dispatch_cancel(adapter)
    _age_watch(adapter, key, _CONFIRM_S + 5)
    del adapter.live_orders[key]  # CANCELLED arrived, or the fill completed it

    assert await adapter.check_unconfirmed_cancels() == 0

    assert key not in adapter._cancel_watch
    mock_deps.cancel_unconfirmed_total.labels.assert_not_called()


@pytest.mark.asyncio
async def test_a_cancel_inside_the_confirm_window_is_left_alone(tmp_config):
    adapter = _adapter(tmp_config)
    key, _ = await _dispatch_cancel(adapter)
    _age_watch(adapter, key, _CONFIRM_S - 20)

    assert await adapter.check_unconfirmed_cancels() == 0

    assert adapter.client.cancel_order.call_count == 1, "only the original dispatch"
    assert key in adapter._cancel_watch


@pytest.mark.asyncio
async def test_an_unconfirmed_cancel_is_reported_and_resent_once_with_the_same_handle(tmp_config, mock_deps):
    adapter = _adapter(tmp_config)
    key, trade = await _dispatch_cancel(adapter)
    _age_watch(adapter, key, _CONFIRM_S + 5)

    assert await adapter.check_unconfirmed_cancels() == 1
    await _settle_retries(adapter)

    assert adapter.client.cancel_order.call_count == 2
    adapter.client.cancel_order.assert_called_with(trade)
    mock_deps.cancel_unconfirmed_total.labels.assert_called_with(outcome="retried")
    mock_deps.cancel_retry_total.labels.assert_called_with(result="sent")
    assert "cancel_unconfirmed" in _audit_events(adapter)
    assert "cancel_retry" in _audit_events(adapter)
    assert adapter._cancel_watch[key].attempts == 1


@pytest.mark.asyncio
async def test_the_second_look_gives_up_instead_of_cancelling_forever(tmp_config, mock_deps):
    adapter = _adapter(tmp_config)
    key, _ = await _dispatch_cancel(adapter)
    _age_watch(adapter, key, _CONFIRM_S + 5)
    await adapter.check_unconfirmed_cancels()
    await _settle_retries(adapter)
    _age_watch(adapter, key, _CONFIRM_S + 5)

    assert await adapter.check_unconfirmed_cancels() == 1
    await _settle_retries(adapter)

    assert adapter.client.cancel_order.call_count == 2, "dispatch + exactly one retry"
    mock_deps.cancel_unconfirmed_total.labels.assert_called_with(outcome="gave_up")
    assert key not in adapter._cancel_watch


@pytest.mark.asyncio
async def test_an_order_not_yet_registered_is_reported_but_cannot_be_resent(tmp_config, mock_deps):
    adapter = _adapter(tmp_config)
    key, _ = await _dispatch_cancel(adapter)
    adapter.live_orders[key] = adapter_module._PENDING_SENTINEL
    _age_watch(adapter, key, _CONFIRM_S + 5)

    await adapter.check_unconfirmed_cancels()
    await _settle_retries(adapter)

    assert adapter.client.cancel_order.call_count == 1, "no handle, no retry"
    mock_deps.cancel_unconfirmed_total.labels.assert_called_with(outcome="gave_up")


@pytest.mark.asyncio
async def test_a_failing_retry_is_contained_and_counted(tmp_config, mock_deps):
    adapter = _adapter(tmp_config)
    key, _ = await _dispatch_cancel(adapter)
    _age_watch(adapter, key, _CONFIRM_S + 5)
    adapter.client.cancel_order.side_effect = RuntimeError("broker down")

    await adapter.check_unconfirmed_cancels()
    await _settle_retries(adapter)

    mock_deps.cancel_retry_total.labels.assert_called_with(result="failed")


@pytest.mark.asyncio
async def test_checking_a_cancel_never_releases_the_strategy_pending_slot(tmp_config):
    adapter = _adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=16)
    adapter.set_rejection_sink(sink)
    key, _ = await _dispatch_cancel(adapter)
    _age_watch(adapter, key, _CONFIRM_S + 5)

    await adapter.check_unconfirmed_cancels()
    await _settle_retries(adapter)

    assert sink.empty(), "the release stays with the TTL sweep, unchanged"
    assert key in adapter.live_orders


@pytest.mark.asyncio
async def test_the_ttl_sweep_resends_the_cancel_before_it_drops_the_handle(tmp_config):
    """The order is past the 300 s TTL in the same sweep that finds the cancel
    unconfirmed: the retry has to go out first, with the handle still held."""
    adapter = _adapter(tmp_config)
    key, trade = await _dispatch_cancel(adapter)
    adapter._live_orders_ttl_s = 300.0
    adapter._live_orders_inserted_at[key] = time.monotonic() - 301
    _age_watch(adapter, key, 301)
    adapter._live_orders_last_sweep_s = 0.0

    await adapter.sweep_stale_live_orders()
    await _settle_retries(adapter)

    assert adapter.client.cancel_order.call_count == 2
    adapter.client.cancel_order.assert_called_with(trade)
    assert key not in adapter.live_orders, "the sweep still evicts as before"


@pytest.mark.asyncio
async def test_the_cancel_watch_is_bounded(tmp_config):
    adapter = _adapter(tmp_config)
    cap = adapter._cancel_inflight_max
    intent = _cancel_cmd(1, "x").intent

    for i in range(cap + 25):
        adapter._watch_cancel(f"{_SID}:{i}", intent, i)

    assert len(adapter._cancel_watch) == cap
    assert f"{_SID}:0" not in adapter._cancel_watch
