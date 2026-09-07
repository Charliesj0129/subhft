"""A guard that rejects BEFORE the broker call must give the pending slot back.

``_add_to_dlq`` records the order and commits the dedup entry. It emits no
``RiskFeedback``. A maker that incremented ``_pending_buy`` / ``_pending_sell``
at submit therefore never decrements it, and with both counters held
``can_buy`` and ``can_sell`` are both False -- the strategy goes silent until a
human restarts the engine.

Measured on THESHOW, 2026-09-07, day session:

    00:45:00Z  session_phase_transition futures_day CLOSED -> OPEN
    00:45:01Z  place_order -> SessionNotEstablished (paper venue)
               -> DLQ connection_error -> pending RELEASED   (PR #474 path)
    ... x5 ...
    00:45:06Z  circuit breaker OPEN (threshold 5)
               -> DLQ circuit_breaker  -> pending NOT released   <-- X
    00:45:07Z  same again
               => strategy_pending_qty{side="SELL"} = 2, pinned
    00:47:13Z  strategy_intents_total freezes at 1129
    ... 2h10m of an open session with no quotes ...

The five *dispatch* failures released correctly because that path already
called ``_send_dispatch_rejection``. The two *pre-dispatch* rejections did
not. Same consequence, different branch.

Note the asymmetry this replaces: the StormGuard HALT skip in the API worker
already called ``_send_dispatch_rejection``, while the HALT rejection in
``execute`` did not -- the same reason, two places, opposite behaviour.
"""

from __future__ import annotations

import asyncio
import inspect
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
    codec.encode_side.return_value = "Sell"
    codec.encode_tif.return_value = "ROD"
    codec.encode_price_type.return_value = "LMT"
    adapter._broker_codec = codec
    adapter._dlq = MagicMock()
    adapter._dlq.add = AsyncMock()
    return adapter


def _command(
    intent_id: int = 1,
    side: Side = Side.SELL,
    intent_type: IntentType = IntentType.NEW,
    state: StormGuardState = StormGuardState.NORMAL,
) -> OrderCommand:
    intent = OrderIntent(
        intent_id=intent_id,
        strategy_id="R47_MAKER_TMF",
        symbol="TMFI6",
        price=4729_3000,
        qty=1,
        side=side,
        intent_type=intent_type,
        tif=TIF.LIMIT,
    )
    return OrderCommand(
        cmd_id=intent_id,
        intent=intent,
        deadline_ns=timebase.now_ns() + 1_000_000_000,
        storm_guard_state=state,
    )


def _drain(sink: asyncio.Queue) -> list[RiskFeedback]:
    out: list[RiskFeedback] = []
    while not sink.empty():
        out.append(sink.get_nowait())
    return out


def _armed(tmp_config: str) -> tuple[OrderAdapter, asyncio.Queue]:
    adapter = _adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)
    return adapter, sink


def _assert_released(feedbacks: list[RiskFeedback], reason_code: str) -> None:
    assert len(feedbacks) == 1, f"expected exactly one release, got {[f.reason_code for f in feedbacks]}"
    fb = feedbacks[0]
    assert fb.reason_code == reason_code
    assert fb.strategy_id == "R47_MAKER_TMF"
    assert fb.symbol == "TMFI6"
    # A sideless release decrements neither counter; an approved one makes
    # ``on_risk_feedback`` return early, so both are releases that do nothing.
    assert fb.side == Side.SELL
    assert fb.was_approved is False


class TestTheCircuitBreakerFreeze:
    """The exact 2026-09-07 shape."""

    @pytest.mark.asyncio
    async def test_an_open_global_circuit_breaker_releases_the_pending_slot(self, tmp_config):
        adapter, sink = _armed(tmp_config)
        adapter.circuit_breaker.is_open = MagicMock(return_value=True)

        await adapter.execute(_command())

        _assert_released(_drain(sink), "circuit_breaker_open")

    @pytest.mark.asyncio
    async def test_five_session_errors_then_a_breaker_rejection_still_releases(self, tmp_config):
        """Reproduce the sequence, not just the end state.

        Five recorded failures is the configured threshold, so the sixth
        intent meets an open breaker -- which is exactly how the live freeze
        was reached.
        """
        adapter, sink = _armed(tmp_config)
        for _ in range(5):
            adapter.circuit_breaker.record_failure()
        assert adapter.circuit_breaker.is_open(), "threshold 5 should have opened the breaker"

        await adapter.execute(_command(intent_id=6))

        _assert_released(_drain(sink), "circuit_breaker_open")

    @pytest.mark.asyncio
    async def test_two_rejections_release_two_slots(self, tmp_config):
        """The live counter reached 2 and stayed there; both must come back."""
        adapter, sink = _armed(tmp_config)
        adapter.circuit_breaker.is_open = MagicMock(return_value=True)

        await adapter.execute(_command(intent_id=1223))
        await adapter.execute(_command(intent_id=1224))

        feedbacks = _drain(sink)
        assert len(feedbacks) == 2
        assert all(f.side == Side.SELL and f.was_approved is False for f in feedbacks)


class TestEveryPreDispatchGuardReleases:
    @pytest.mark.asyncio
    async def test_storm_guard_halt_releases_the_pending_slot(self, tmp_config):
        """The API worker's HALT skip already released; ``execute``'s did not."""
        adapter, sink = _armed(tmp_config)

        await adapter.execute(_command(state=StormGuardState.HALT))

        _assert_released(_drain(sink), "dispatch_halt_reject")

    @pytest.mark.asyncio
    async def test_per_strategy_circuit_breaker_releases_the_pending_slot(self, tmp_config):
        adapter, sink = _armed(tmp_config)
        # The manager has __slots__, so replace the object rather than an attribute.
        adapter.strategy_cb_mgr = MagicMock()
        adapter.strategy_cb_mgr.is_open = MagicMock(return_value=True)

        await adapter.execute(_command())

        _assert_released(_drain(sink), "strategy_circuit_breaker_open")

    @pytest.mark.asyncio
    async def test_per_symbol_hard_rate_limit_releases_the_pending_slot(self, tmp_config):
        from hft_platform.order.adapter import PerSymbolRateResult

        adapter, sink = _armed(tmp_config)
        # The limiter has __slots__, so replace the object rather than an attribute.
        adapter.per_symbol_rate_limiter = MagicMock()
        adapter.per_symbol_rate_limiter.check = MagicMock(return_value=PerSymbolRateResult.HARD)

        await adapter.execute(_command())

        _assert_released(_drain(sink), "per_symbol_rate_limit")

    @pytest.mark.asyncio
    async def test_global_rate_limit_releases_the_pending_slot(self, tmp_config):
        adapter, sink = _armed(tmp_config)
        adapter.check_rate_limit = MagicMock(return_value=False)

        await adapter.execute(_command())

        _assert_released(_drain(sink), "rate_limit_exceeded")

    @pytest.mark.asyncio
    async def test_platform_reduce_only_releases_the_pending_slot(self, tmp_config):
        adapter, sink = _armed(tmp_config)
        adapter._platform_degrade_allows = MagicMock(return_value=False)

        await adapter.execute(_command())

        _assert_released(_drain(sink), "platform_reduce_only")

    @pytest.mark.asyncio
    async def test_client_validation_failure_releases_the_pending_slot(self, tmp_config):
        adapter, sink = _armed(tmp_config)
        adapter._validate_client = MagicMock(return_value=False)

        await adapter.execute(_command())

        _assert_released(_drain(sink), "client_validation_failed")

    @pytest.mark.asyncio
    async def test_a_duplicate_idempotency_key_releases_the_pending_slot(self, tmp_config):
        adapter, sink = _armed(tmp_config)
        existing = MagicMock()
        existing.approved = True
        store = MagicMock()
        store.check_and_reserve = MagicMock(return_value=existing)
        adapter._dedup_store = store
        cmd = _command()
        object.__setattr__(cmd.intent, "idempotency_key", "dup-key")

        await adapter.execute(cmd)

        _assert_released(_drain(sink), "duplicate_idempotency_key")

    @pytest.mark.asyncio
    async def test_a_missing_broker_codec_releases_the_pending_slot(self, tmp_config):
        """Rejected one line before ``place_order`` -- nothing was sent."""
        adapter, sink = _armed(tmp_config)
        adapter._broker_codec = None

        ok = await adapter._dispatch_to_api(_command())

        assert ok is False
        _assert_released(_drain(sink), "no_broker_codec")


class TestTheReleaseIsNotOverApplied:
    """A release that fires when no slot was taken is its own defect."""

    @pytest.mark.asyncio
    async def test_an_amend_rejection_does_not_release_a_slot_it_never_took(self, tmp_config):
        """``on_risk_feedback`` decrements purely on ``feedback.side``.

        Only a NEW submit ever incremented a counter, so releasing on an
        AMEND would decrement a slot belonging to a different order.
        """
        adapter, sink = _armed(tmp_config)
        adapter.circuit_breaker.is_open = MagicMock(return_value=True)

        await adapter.execute(_command(intent_type=IntentType.AMEND))

        assert _drain(sink) == []

    @pytest.mark.asyncio
    async def test_a_cancel_is_never_blocked_by_these_guards_at_all(self, tmp_config):
        """CANCEL is ``_safety_exempt``: it must reach dispatch during HALT.

        This is the property that keeps a HALT evacuation possible, and it is
        why the release gate does not need to consider CANCEL.
        """
        adapter, sink = _armed(tmp_config)
        adapter.circuit_breaker.is_open = MagicMock(return_value=True)
        adapter._dispatch_to_api = AsyncMock(return_value=True)

        await adapter.execute(_command(intent_type=IntentType.CANCEL, state=StormGuardState.HALT))

        adapter._dispatch_to_api.assert_awaited_once()
        assert _drain(sink) == []


class TestTheLeakCannotBeReintroduced:
    def test_no_pre_dispatch_guard_in_execute_dlqs_without_releasing(self):
        """``execute`` must reject only through ``_reject_before_dispatch``.

        Source-level, because the failure mode is a *new* guard added later
        that calls ``_add_to_dlq`` and returns -- which no behavioural test
        for today's guards would catch.
        """
        source = inspect.getsource(OrderAdapter.execute)
        assert "_reject_before_dispatch" in source
        assert "_add_to_dlq" not in source, (
            "execute() must route rejections through _reject_before_dispatch so "
            "the strategy's pending slot is always released; a bare _add_to_dlq "
            "records the order and tells the strategy nothing"
        )

    def test_the_helper_keeps_dlq_and_release_in_one_call(self):
        source = inspect.getsource(OrderAdapter._reject_before_dispatch)
        assert "_add_to_dlq" in source
        assert "_send_dispatch_rejection" in source
        assert "phantom_pending=False" in source, (
            "nothing reached the broker on these paths, so the slot must be "
            "released immediately rather than left to the phantom TTL"
        )
