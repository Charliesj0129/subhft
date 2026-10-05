"""A cancel that timed out may have landed: do not repeat it blind, and do watch it.

THESHOW, R47 (SIM), 2026-10-05: ``cancel_order`` hit its 3 s wrapper timeout and
``_call_api`` retried it twice at once (3 x 3 s). The first attempt had often
already reached the broker, so the retry was answered "already cancelled" and
came back as ``FAILED`` 0.02-1.28 s after ``CANCELLED`` -- 34 orders, 39 rows --
and R47 released its pending slot a second time on each. Meanwhile the three
blocked seconds queued the paired SELL cancel behind it (see
``test_cancel_survives_expired_deadline``). The F3a watch never saw any of it:
it was registered only after a cancel call *succeeded*, and it was only looked at
inside the 60 s-rate-limited TTL sweep.

New behaviour:

* a ``cancel_order`` TIMEOUT is not retried inside ``_call_api`` (same reason
  ``place_order`` is not: the first attempt may still complete);
* it is watched like a successful cancel, but with a short window
  (``HFT_CANCEL_TIMEOUT_CONFIRM_S``, 10 s): if the order is still live then, the
  cancel is re-sent ONCE with the Trade handle we hold; if the order is gone
  (CANCELLED arrived) nothing is sent;
* any other transient error keeps its immediate retries;
* the watch is checked every 5 s on its own, not once a minute.
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

_SID = "strat1"
_CONFIRM_S = 60.0
_TIMEOUT_CONFIRM_S = 10.0


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
    adapter._cancel_timeout_confirm_s = _TIMEOUT_CONFIRM_S
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


async def _dispatch_cancel(adapter, target: str = "OID-1") -> tuple[str, Any, bool]:
    """Register a live order and dispatch a cancel for it, as R47 does."""
    key = f"{_SID}:{target}"
    trade = MagicMock(name="trade")
    adapter.live_orders[key] = trade
    ok = await adapter._dispatch_to_api(_cancel_cmd(99, target))
    return key, trade, ok


def _age_watch(adapter, key: str, age_s: float) -> None:
    adapter._cancel_watch[key] = adapter._cancel_watch[key]._replace(at=time.monotonic() - age_s)


async def _settle_retries(adapter) -> None:
    pending = list(adapter._cancel_retry_tasks)
    if pending:
        await asyncio.gather(*pending)


def _audit_events(adapter) -> list[str]:
    return [c.args[0].get("event") for c in adapter._audit_writer.log_order.call_args_list]


# ── the timeout itself is not repeated ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_cancel_timeout_is_not_retried_inside_the_call(tmp_config):
    adapter = _adapter(tmp_config)
    adapter.client.cancel_order.side_effect = TimeoutError()

    _, _, ok = await _dispatch_cancel(adapter)

    assert ok is False
    assert adapter.client.cancel_order.call_count == 1, "the first attempt may still land; repeating it is E3"


@pytest.mark.asyncio
async def test_a_cancel_timeout_counts_one_breaker_failure_as_before(tmp_config):
    adapter = _adapter(tmp_config)
    adapter.circuit_breaker = MagicMock()
    adapter.circuit_breaker.is_open.return_value = False
    adapter.client.cancel_order.side_effect = TimeoutError()

    await _dispatch_cancel(adapter)

    adapter.circuit_breaker.record_failure.assert_called_once()


@pytest.mark.asyncio
async def test_a_cancel_timeout_does_not_feed_order_rtt(tmp_config):
    """Unchanged: StormGuard's order_rtt input is not widened by this fix."""
    adapter = _adapter(tmp_config)
    adapter._observe_order_rtt = MagicMock()
    adapter.client.cancel_order.side_effect = TimeoutError()

    await _dispatch_cancel(adapter)

    adapter._observe_order_rtt.assert_not_called()


@pytest.mark.asyncio
async def test_a_non_timeout_transient_error_keeps_its_immediate_retries(tmp_config):
    adapter = _adapter(tmp_config)
    adapter.client.cancel_order.side_effect = [ConnectionError("reset"), ConnectionError("reset"), MagicMock()]

    key, _, ok = await _dispatch_cancel(adapter)

    assert ok is True
    assert adapter.client.cancel_order.call_count == 3
    assert adapter._cancel_watch[key].confirm_s is None, "a successful cancel keeps the long window"


# ── the timed-out cancel is watched ───────────────────────────────────────


@pytest.mark.asyncio
async def test_a_second_cancel_for_a_timed_out_target_is_not_sent_blind(tmp_config):
    """The first may have landed; until the watch decides, a repeat is a no-op."""
    adapter = _adapter(tmp_config)
    adapter.client.cancel_order.side_effect = TimeoutError()
    key, _, _ = await _dispatch_cancel(adapter)

    assert await adapter._dispatch_to_api(_cancel_cmd(100, "OID-1")) is True

    assert adapter.client.cancel_order.call_count == 1
    assert "cancel_no_op_already_inflight" in _audit_events(adapter)
    assert key in adapter._cancel_watch


@pytest.mark.asyncio
async def test_a_timed_out_cancel_is_watched_with_the_short_window(tmp_config):
    adapter = _adapter(tmp_config)
    adapter.client.cancel_order.side_effect = TimeoutError()

    key, _, _ = await _dispatch_cancel(adapter)

    assert key in adapter._cancel_watch
    assert adapter._cancel_watch[key].confirm_s == _TIMEOUT_CONFIRM_S
    assert "cancel_timeout" in _audit_events(adapter)


@pytest.mark.asyncio
async def test_a_timed_out_cancel_still_live_is_resent_once_after_the_short_window(tmp_config, mock_deps):
    adapter = _adapter(tmp_config)
    adapter.client.cancel_order.side_effect = [TimeoutError(), MagicMock()]
    key, trade, _ = await _dispatch_cancel(adapter)
    _age_watch(adapter, key, _TIMEOUT_CONFIRM_S + 1)

    assert await adapter.check_unconfirmed_cancels() == 1
    await _settle_retries(adapter)

    assert adapter.client.cancel_order.call_count == 2
    adapter.client.cancel_order.assert_called_with(trade)
    mock_deps.cancel_unconfirmed_total.labels.assert_called_with(outcome="retried")
    mock_deps.cancel_retry_total.labels.assert_called_with(result="sent")
    assert adapter._cancel_watch[key].attempts == 1
    assert adapter._cancel_watch[key].confirm_s is None, "the second look uses the long window"


@pytest.mark.asyncio
async def test_a_timed_out_cancel_inside_the_short_window_is_left_alone(tmp_config):
    adapter = _adapter(tmp_config)
    adapter.client.cancel_order.side_effect = TimeoutError()
    key, _, _ = await _dispatch_cancel(adapter)
    _age_watch(adapter, key, _TIMEOUT_CONFIRM_S - 4)

    assert await adapter.check_unconfirmed_cancels() == 0

    assert adapter.client.cancel_order.call_count == 1


@pytest.mark.asyncio
async def test_a_timed_out_cancel_whose_order_ended_is_never_resent(tmp_config, mock_deps):
    """The case E3 measured: the first cancel landed, CANCELLED arrived."""
    adapter = _adapter(tmp_config)
    adapter.client.cancel_order.side_effect = TimeoutError()
    key, _, _ = await _dispatch_cancel(adapter)
    _age_watch(adapter, key, _TIMEOUT_CONFIRM_S + 1)
    del adapter.live_orders[key]  # CANCELLED arrived

    assert await adapter.check_unconfirmed_cancels() == 0
    await _settle_retries(adapter)

    assert adapter.client.cancel_order.call_count == 1, "no second cancel -> no FAILED after CANCELLED"
    assert key not in adapter._cancel_watch
    mock_deps.cancel_unconfirmed_total.labels.assert_not_called()


@pytest.mark.asyncio
async def test_a_resent_cancel_that_times_out_again_is_labelled_timeout(tmp_config, mock_deps):
    adapter = _adapter(tmp_config)
    adapter.client.cancel_order.side_effect = TimeoutError()
    key, _, _ = await _dispatch_cancel(adapter)
    _age_watch(adapter, key, _TIMEOUT_CONFIRM_S + 1)

    await adapter.check_unconfirmed_cancels()
    await _settle_retries(adapter)

    assert adapter.client.cancel_order.call_count == 2, "dispatch + exactly one re-send"
    mock_deps.cancel_retry_total.labels.assert_called_with(result="timeout")


@pytest.mark.asyncio
async def test_the_second_look_after_a_timed_out_cancel_gives_up(tmp_config, mock_deps):
    adapter = _adapter(tmp_config)
    adapter.client.cancel_order.side_effect = TimeoutError()
    key, _, _ = await _dispatch_cancel(adapter)
    _age_watch(adapter, key, _TIMEOUT_CONFIRM_S + 1)
    await adapter.check_unconfirmed_cancels()
    await _settle_retries(adapter)
    _age_watch(adapter, key, _CONFIRM_S + 1)

    assert await adapter.check_unconfirmed_cancels() == 1
    await _settle_retries(adapter)

    assert adapter.client.cancel_order.call_count == 2
    mock_deps.cancel_unconfirmed_total.labels.assert_called_with(outcome="gave_up")
    assert key not in adapter._cancel_watch


# ── the watch is checked on its own clock ─────────────────────────────────


@pytest.mark.asyncio
async def test_the_cancel_watch_is_checked_even_while_the_60s_sweep_is_closed(tmp_config, mock_deps):
    adapter = _adapter(tmp_config)
    adapter.client.cancel_order.side_effect = [TimeoutError(), MagicMock()]
    key, trade, _ = await _dispatch_cancel(adapter)
    _age_watch(adapter, key, _TIMEOUT_CONFIRM_S + 1)
    adapter._live_orders_last_sweep_s = time.monotonic()  # the 60 s gate says "not yet"

    assert await adapter.sweep_stale_live_orders() == 0
    await _settle_retries(adapter)

    # Only the watch can produce "retried": the old in-call retry also made a 2nd call.
    mock_deps.cancel_unconfirmed_total.labels.assert_called_with(outcome="retried")
    mock_deps.cancel_retry_total.labels.assert_called_with(result="sent")
    adapter.client.cancel_order.assert_called_with(trade)


@pytest.mark.asyncio
async def test_the_cancel_watch_check_is_rate_limited_to_its_own_interval(tmp_config):
    adapter = _adapter(tmp_config)
    adapter.client.cancel_order.side_effect = TimeoutError()
    key, _, _ = await _dispatch_cancel(adapter)
    adapter._live_orders_last_sweep_s = time.monotonic()
    adapter._cancel_check_last_s = time.monotonic()  # checked a moment ago
    _age_watch(adapter, key, _TIMEOUT_CONFIRM_S + 1)

    await adapter.sweep_stale_live_orders()
    await _settle_retries(adapter)

    assert adapter.client.cancel_order.call_count == 1, "inside the 5 s interval: not checked again"


@pytest.mark.asyncio
async def test_a_failing_watch_check_does_not_stop_the_sweep(tmp_config):
    adapter = _adapter(tmp_config)
    adapter.check_unconfirmed_cancels = AsyncMock(side_effect=RuntimeError("boom"))
    adapter._live_orders_last_sweep_s = float("-inf")

    assert await adapter.sweep_stale_live_orders() == 0

    adapter.check_unconfirmed_cancels.assert_awaited_once()
