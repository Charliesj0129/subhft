"""A guard that refuses an intent is not a dead letter.

A dead letter is an order whose fate the platform does not know -- dispatched
and timed out, or the transport died mid-call -- and someone may have to
reconcile or replay it. A refusal is the opposite: a decision taken locally,
with a known outcome and nothing to replay.

Production shape these are written from (THESHOW order DLQ, 2026-08-28..09-16)::

    circuit_breaker    1733   <- refusals; never left the platform
    connection_error    334   <- genuine dead letters

``OrderDeadLetterQueueGrowing`` is critical and fires on ``increase()`` of the
sum, so 84% of its pages were the platform correctly saying no. The 00:45 open
minute is the top minute on every affected day: the order session had lapsed
overnight, dispatch failures opened the breaker, and every intent behind it was
counted as a lost order.

These tests pin both halves of the boundary -- refusals are booked on
``order_local_reject_total{reason=...}`` and leave the DLQ alone, while a real
dispatch failure still dead-letters -- plus the two things a refusal must keep
doing: release the strategy's pending slot, and resolve the dedup reservation.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.core import timebase
from hft_platform.order.adapter import OrderAdapter
from hft_platform.order.deadletter import DeadLetterQueue, RejectionReason


def _make_intent(**overrides) -> OrderIntent:
    defaults = {
        "intent_id": 1,
        "strategy_id": "R47_MAKER_TMF",
        "symbol": "TMFJ6",
        "intent_type": IntentType.NEW,
        "side": Side.SELL,
        "price": 2_200_0000,
        "qty": 1,
        "tif": TIF.LIMIT,
    }
    defaults.update(overrides)
    return OrderIntent(**defaults)


def _make_cmd(intent: OrderIntent, cmd_id: int = 1) -> OrderCommand:
    import time

    return OrderCommand(
        cmd_id=cmd_id,
        intent=intent,
        deadline_ns=time.monotonic_ns() + 5_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=timebase.now_ns(),
    )


def _make_adapter(tmp_path) -> OrderAdapter:
    config_file = tmp_path / "order_config.yaml"
    config_file.write_text(
        "rate_limits:\n"
        "  shioaji_soft_cap: 180\n"
        "  shioaji_hard_cap: 250\n"
        "  window_seconds: 10\n"
        "circuit_breaker:\n"
        "  threshold: 5\n"
        "  timeout_seconds: 60\n"
    )
    client = MagicMock()
    client.place_order = MagicMock(return_value={"id": "T1"})
    client.cancel_order = MagicMock()
    client.get_exchange = MagicMock(return_value="TSE")
    adapter = OrderAdapter(str(config_file), asyncio.Queue(), client)
    adapter.shadow_sink.enabled = False
    adapter._dlq = DeadLetterQueue(dlq_dir=str(tmp_path / "dlq"), max_buffer_size=100)
    return adapter


def _rejects(adapter: OrderAdapter, reason: str) -> float:
    return adapter.metrics.order_local_reject_total.labels(reason=reason)._value.get()


class TestRefusalsSkipTheDeadLetterQueue:
    @pytest.mark.asyncio
    async def test_an_open_breaker_is_booked_not_dead_lettered(self, tmp_path) -> None:
        """The 1,733-entry case: an open breaker refuses, it does not lose orders."""
        adapter = _make_adapter(tmp_path)
        for _ in range(adapter.circuit_breaker.threshold):
            adapter.circuit_breaker.record_failure()
        assert adapter.circuit_breaker.is_open()

        before = _rejects(adapter, "circuit_breaker")
        await adapter.execute(_make_cmd(_make_intent()))

        assert _rejects(adapter, "circuit_breaker") == before + 1
        assert (await adapter._dlq.get_stats())["total_entries"] == 0
        adapter.client.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_rate_limit_is_booked_not_dead_lettered(self, tmp_path) -> None:
        adapter = _make_adapter(tmp_path)
        for _ in range(260):
            adapter.rate_limiter.record()
        assert not adapter.check_rate_limit()

        before = _rejects(adapter, "rate_limit")
        await adapter.execute(_make_cmd(_make_intent()))

        assert _rejects(adapter, "rate_limit") == before + 1
        assert (await adapter._dlq.get_stats())["total_entries"] == 0

    @pytest.mark.asyncio
    async def test_reduce_only_is_booked_not_dead_lettered(self, tmp_path) -> None:
        """The other big Telegram source: reduce-only refuses opening orders."""
        adapter = _make_adapter(tmp_path)
        adapter._platform_degrade_allows = MagicMock(return_value=False)  # type: ignore[method-assign]

        before = _rejects(adapter, "platform_reduce_only")
        await adapter.execute(_make_cmd(_make_intent()))

        assert _rejects(adapter, "platform_reduce_only") == before + 1
        assert (await adapter._dlq.get_stats())["total_entries"] == 0

    @pytest.mark.asyncio
    async def test_a_halt_refusal_is_booked_not_dead_lettered(self, tmp_path) -> None:
        adapter = _make_adapter(tmp_path)
        storm_guard = MagicMock()
        storm_guard.state = StormGuardState.HALT
        storm_guard.is_halt_exempt.return_value = False
        storm_guard._intent_reduces_position = MagicMock(return_value=False)
        adapter._storm_guard = storm_guard

        before = _rejects(adapter, "stormguard_halt")
        await adapter.execute(_make_cmd(_make_intent()))

        assert _rejects(adapter, "stormguard_halt") == before + 1
        assert (await adapter._dlq.get_stats())["total_entries"] == 0

    @pytest.mark.asyncio
    async def test_every_refusal_reason_is_labelled_separately(self, tmp_path) -> None:
        """``dlq_size_total`` had no reason label; that is what made it useless."""
        adapter = _make_adapter(tmp_path)
        before_rate = _rejects(adapter, "rate_limit")
        before_breaker = _rejects(adapter, "circuit_breaker")

        for _ in range(260):
            adapter.rate_limiter.record()
        await adapter.execute(_make_cmd(_make_intent(intent_id=1), cmd_id=1))

        assert _rejects(adapter, "rate_limit") == before_rate + 1
        assert _rejects(adapter, "circuit_breaker") == before_breaker


class TestDispatchFailuresStillDeadLetter:
    @pytest.mark.asyncio
    async def test_a_failed_dispatch_is_still_a_dead_letter(self, tmp_path) -> None:
        """The 334 connection_error entries are real: do not gut the DLQ."""
        adapter = _make_adapter(tmp_path)
        intent = _make_intent()

        await adapter._add_to_dlq(intent, RejectionReason.CONNECTION_ERROR, "api_failure")

        stats = await adapter._dlq.get_stats()
        assert stats["total_entries"] == 1


class TestARefusalStillReleasesAndResolves:
    @pytest.mark.asyncio
    async def test_a_refused_new_order_releases_the_pending_slot(self, tmp_path) -> None:
        """PR #482's pairing: without RiskFeedback the maker freezes for good."""
        adapter = _make_adapter(tmp_path)
        sink: asyncio.Queue = asyncio.Queue()
        adapter._rejection_sink = sink
        for _ in range(adapter.circuit_breaker.threshold):
            adapter.circuit_breaker.record_failure()

        await adapter.execute(_make_cmd(_make_intent()))

        feedback = sink.get_nowait()
        assert feedback.strategy_id == "R47_MAKER_TMF"
        assert feedback.reason_code == "circuit_breaker_open"
        assert feedback.was_approved is False

    @pytest.mark.asyncio
    async def test_a_refused_amend_does_not_release_a_slot_it_never_took(self, tmp_path) -> None:
        adapter = _make_adapter(tmp_path)
        sink: asyncio.Queue = asyncio.Queue()
        adapter._rejection_sink = sink
        for _ in range(adapter.circuit_breaker.threshold):
            adapter.circuit_breaker.record_failure()

        await adapter.execute(_make_cmd(_make_intent(intent_type=IntentType.AMEND)))

        assert sink.empty()

    @pytest.mark.asyncio
    async def test_a_refusal_resolves_the_dedup_reservation(self, tmp_path) -> None:
        """A reserved key stays reserved forever unless the refusal commits it."""
        adapter = _make_adapter(tmp_path)
        adapter._dedup_commit = MagicMock()  # type: ignore[method-assign]
        for _ in range(adapter.circuit_breaker.threshold):
            adapter.circuit_breaker.record_failure()

        intent = _make_intent(idempotency_key="k-1")
        await adapter.execute(_make_cmd(intent))

        adapter._dedup_commit.assert_any_call("k-1", False, "Circuit breaker open", 0)

    @pytest.mark.asyncio
    async def test_a_refusal_never_reaches_the_broker(self, tmp_path) -> None:
        adapter = _make_adapter(tmp_path)
        adapter._enqueue_api = AsyncMock()  # type: ignore[method-assign]
        adapter.running = True
        for _ in range(adapter.circuit_breaker.threshold):
            adapter.circuit_breaker.record_failure()

        await adapter.execute(_make_cmd(_make_intent()))

        adapter._enqueue_api.assert_not_awaited()
        adapter.client.place_order.assert_not_called()
