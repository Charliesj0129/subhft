"""A dispatch that never reached the broker must give the pending slot back.

Measured on THESHOW: on 2026-08-31 ``place_order`` raised
``401 Token is expired``. ``_is_transient_error`` classifies that as
NON-transient (correctly -- retrying an expired session does not help), so
``_call_api`` returned ``None`` from its non-transient branch, which registers
no phantom. ``_dispatch_to_api`` then popped ``live_orders``, untracked the
intent, DLQ'd, and returned ``False`` without any feedback -- blinding all
three reclaim paths at once:

* ``release_stale_phantom_pendings`` -- no phantom was ever registered;
* ``sweep_stale_live_orders``        -- the entry was just deleted;
* ``on_risk_feedback``               -- no RiskFeedback was emitted.

R47_MAKER_TMF's ``_pending_buy``/``_pending_sell`` stayed at 1/1, which makes
``can_buy`` and ``can_sell`` both False. The strategy went silent for 10 days
and needed a human to re-arm it.

The Bug 23 defence must survive: when a MUTATING call times out a phantom IS
registered and the order may have reached the broker, so that release must
stay suppressed (``was_approved=True``) or two phantoms can fill and breach
max_pos.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    RiskFeedback,
    Side,
)
from hft_platform.core import timebase
from hft_platform.order.adapter import OrderAdapter
from hft_platform.risk.storm_guard import StormGuardState


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
    with (
        patch("hft_platform.order.adapter.MetricsRegistry") as mm,
        patch("hft_platform.order.adapter.LatencyRecorder") as ml,
        patch("hft_platform.order.adapter.SymbolMetadata"),
        patch("hft_platform.order.adapter.PriceCodec"),
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.order.adapter.get_dlq") as md,
    ):
        mm.get.return_value = MagicMock()
        ml.get.return_value = MagicMock()
        md.return_value = MagicMock()
        yield


def _adapter(tmp_config: str) -> OrderAdapter:
    client = MagicMock()
    client.get_exchange = MagicMock(return_value="TAIFEX")
    client.mode = "simulation"
    client.activate_ca = False
    adapter = OrderAdapter(
        config_path=tmp_config,
        order_queue=asyncio.Queue(maxsize=128),
        broker_client=client,
    )
    codec = MagicMock()
    codec.encode_side.return_value = "Buy"
    codec.encode_tif.return_value = "ROD"
    codec.encode_price_type.return_value = "LMT"
    adapter._broker_codec = codec
    adapter._add_to_dlq = AsyncMock()
    return adapter


def _command(intent_id: int = 1, side: Side = Side.BUY) -> OrderCommand:
    intent = OrderIntent(
        intent_id=intent_id,
        strategy_id="R47_MAKER_TMF",
        symbol="TMFI6",
        price=4623_9000,
        qty=1,
        side=side,
        intent_type=IntentType.NEW,
        tif=TIF.LIMIT,
    )
    return OrderCommand(
        cmd_id=intent_id,
        intent=intent,
        deadline_ns=timebase.now_ns() + 1_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
    )


def _drain(sink: asyncio.Queue) -> list[RiskFeedback]:
    out: list[RiskFeedback] = []
    while not sink.empty():
        out.append(sink.get_nowait())
    return out


async def _dispatch_with_api_result(adapter: OrderAdapter, cmd: OrderCommand, result) -> bool:
    """Drive ``_dispatch_to_api`` with ``_call_api`` forced to ``result``."""

    async def _fake_call_api(op, fn, *args, **kwargs):
        return result

    with patch.object(adapter, "_call_api", _fake_call_api):
        return await adapter._dispatch_to_api(cmd)


@pytest.mark.asyncio
async def test_non_transient_dispatch_failure_releases_the_pending_slot(tmp_config):
    """The 2026-08-31 freeze: an expired token left the slot held forever."""
    adapter = _adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)

    ok = await _dispatch_with_api_result(adapter, _command(), None)

    assert ok is False
    feedbacks = _drain(sink)
    assert len(feedbacks) == 1, "a failed dispatch must tell the strategy exactly once"
    assert feedbacks[0].strategy_id == "R47_MAKER_TMF"
    assert feedbacks[0].symbol == "TMFI6"
    assert feedbacks[0].side == Side.BUY, "a sideless release would decrement both counters (Bug 9)"


@pytest.mark.asyncio
async def test_the_release_is_not_approved_so_the_strategy_actually_decrements(tmp_config):
    """DEC2-001: ``was_approved=True`` makes R47.on_risk_feedback return early."""
    adapter = _adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)

    await _dispatch_with_api_result(adapter, _command(), None)

    feedbacks = _drain(sink)
    assert feedbacks[0].was_approved is False, "an approved release is a release that does nothing"


@pytest.mark.asyncio
async def test_guard_timeout_releases_immediately(tmp_config):
    """The API guard trips before the semaphore is acquired: nothing was sent."""
    from hft_platform.order.adapter import _GUARD_TIMEOUT

    adapter = _adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)

    await _dispatch_with_api_result(adapter, _command(), _GUARD_TIMEOUT)

    feedbacks = _drain(sink)
    assert len(feedbacks) == 1
    assert feedbacks[0].reason_code == "api_timeout"
    assert feedbacks[0].was_approved is False


@pytest.mark.asyncio
async def test_registered_phantom_keeps_the_slot_held(tmp_config):
    """Bug 23: a mutating timeout may have reached the broker -- do not re-arm."""
    adapter = _adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)
    cmd = _command()
    with adapter._phantom_lock:
        adapter._register_phantom(cmd.intent)

    await _dispatch_with_api_result(adapter, cmd, None)

    feedbacks = _drain(sink)
    assert len(feedbacks) == 1
    assert feedbacks[0].was_approved is True, (
        "an order that may have reached the broker must keep its pending slot; "
        "releasing it lets a duplicate fill breach max_pos (Bug 23)"
    )
