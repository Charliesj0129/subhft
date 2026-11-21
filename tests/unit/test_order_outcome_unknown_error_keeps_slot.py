from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side
from hft_platform.core import timebase
from hft_platform.order.adapter import OrderAdapter
from hft_platform.risk.storm_guard import StormGuardState

_STRATEGY = "R47_MAKER_TMF"
_KEY = f"{_STRATEGY}:7"


class ShioajiError(Exception):
    pass


class ResponseError(ShioajiError):
    pass


class OrderOutcomeUnknownError(ResponseError):
    pass


class _SubclassedOutcomeUnknown(OrderOutcomeUnknownError):
    pass


class _OrderOutcomeUnknownErrorLookalike(Exception):
    pass


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


class _LateBroker:
    def __init__(self, outcome: Any) -> None:
        self.release = threading.Event()
        self._outcome = outcome

    def __call__(self, *_a: Any, **_kw: Any) -> Any:
        self.release.wait(timeout=5.0)
        raise self._outcome


def _adapter(tmp_config: str, *, place_order: Any) -> OrderAdapter:
    client = MagicMock()
    client.get_exchange = MagicMock(return_value="TAIFEX")
    client.place_order = place_order
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
    adapter._audit_writer = MagicMock()
    adapter.set_rejection_sink(asyncio.Queue(maxsize=64))
    adapter._api_timeout_s = 0.05
    return adapter


def _command(intent_id: int = 7) -> OrderCommand:
    intent = OrderIntent(
        intent_id=intent_id,
        strategy_id=_STRATEGY,
        symbol="TMFI6",
        price=4623_9000,
        qty=1,
        side=Side.BUY,
        intent_type=IntentType.NEW,
        tif=TIF.LIMIT,
    )
    return OrderCommand(
        cmd_id=intent_id,
        intent=intent,
        deadline_ns=timebase.now_ns() + 1_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
    )


def _outcomes(adapter: OrderAdapter) -> list[str]:
    return [c.kwargs["outcome"] for c in adapter.metrics.phantom_bound_total.labels.call_args_list]


async def _until(predicate: Any, timeout_s: float = 5.0) -> None:
    for _ in range(int(timeout_s / 0.005)):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never became true")


def test_order_outcome_unknown_error_may_have_sent_when_class_is_in_the_mro() -> None:
    assert OrderAdapter._error_may_have_sent(OrderOutcomeUnknownError("order may or may not exist")) is True


def test_order_outcome_unknown_error_may_have_sent_when_raised_through_a_subclass() -> None:
    assert OrderAdapter._error_may_have_sent(_SubclassedOutcomeUnknown("reconcile first")) is True


@pytest.mark.parametrize(
    "exc",
    [
        ResponseError("Session error NotReady"),
        ShioajiError("token expired"),
        ValueError("Unsupported futures oc_type"),
        _OrderOutcomeUnknownErrorLookalike("different class name"),
    ],
)
def test_error_may_have_sent_stays_false_when_the_sdk_refused_before_an_order_existed(exc: Exception) -> None:
    assert OrderAdapter._error_may_have_sent(exc) is False


@pytest.mark.parametrize("exc", [asyncio.TimeoutError(), TimeoutError(), RuntimeError("request timed out")])
def test_error_may_have_sent_stays_true_when_the_request_timed_out(exc: Exception) -> None:
    assert OrderAdapter._error_may_have_sent(exc) is True


@pytest.mark.asyncio
async def test_late_order_outcome_unknown_error_keeps_the_slot_for_the_ttl_when_place_order_timed_out(tmp_config):
    broker = _LateBroker(OrderOutcomeUnknownError("order may or may not exist"))
    adapter = _adapter(tmp_config, place_order=broker)
    ok = await adapter._dispatch_to_api(_command())
    assert ok is False

    broker.release.set()
    await _until(lambda: _outcomes(adapter))

    assert _outcomes(adapter) == ["kept_for_ttl"]
    assert adapter.get_phantom_candidates() == frozenset({_KEY})


@pytest.mark.asyncio
async def test_late_refusal_error_releases_the_slot_when_place_order_timed_out_before_the_sdk_refused(tmp_config):
    broker = _LateBroker(ResponseError("Session error NotReady"))
    adapter = _adapter(tmp_config, place_order=broker)
    ok = await adapter._dispatch_to_api(_command())
    assert ok is False

    broker.release.set()
    await _until(lambda: _outcomes(adapter))

    assert _outcomes(adapter) == ["released_not_sent"]
    assert adapter.get_phantom_candidates() == frozenset()


def _intent(intent_type: IntentType) -> OrderIntent:
    return OrderIntent(
        intent_id=7,
        strategy_id=_STRATEGY,
        symbol="TMFI6",
        price=4623_9000,
        qty=1,
        side=Side.BUY,
        intent_type=intent_type,
        tif=TIF.LIMIT,
    )


@pytest.mark.asyncio
async def test_synchronous_order_outcome_unknown_error_keeps_the_phantom_slot_when_place_order_raised(tmp_config):
    place_order = MagicMock(side_effect=OrderOutcomeUnknownError("order may or may not exist"))
    adapter = _adapter(tmp_config, place_order=place_order)

    ok = await adapter._dispatch_to_api(_command())

    assert ok is False
    assert place_order.call_count == 1
    assert adapter.get_phantom_candidates() == frozenset({_KEY})
    feedback = adapter._rejection_sink.get_nowait()
    assert feedback.was_approved is True
    adapter._add_to_dlq.assert_not_awaited()
    adapter.metrics.order_outcome_unknown_total.labels.assert_called_with(op="place_order")
    adapter.metrics.order_reject_total.inc.assert_not_called()
    assert adapter.circuit_breaker.failure_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "order may or may not exist",
        "connection reset: order may or may not exist",
        "ETIMEDOUT while waiting; temporarily unavailable",
    ],
)
async def test_order_outcome_unknown_error_is_never_retried_when_place_order_raised(tmp_config, message):
    place_order = MagicMock(side_effect=OrderOutcomeUnknownError(message))
    adapter = _adapter(tmp_config, place_order=place_order)

    result = await adapter._call_api("place_order", place_order, intent=_intent(IntentType.NEW))

    assert result is None
    assert place_order.call_count == 1


@pytest.mark.asyncio
async def test_order_outcome_unknown_error_is_counted_as_a_failure_when_the_intent_is_not_a_new(tmp_config):
    place_order = MagicMock(side_effect=OrderOutcomeUnknownError("order may or may not exist"))
    adapter = _adapter(tmp_config, place_order=place_order)

    result = await adapter._call_api("place_order", place_order, intent=_intent(IntentType.FORCE_FLAT))

    assert result is None
    assert place_order.call_count == 1
    adapter.metrics.order_outcome_unknown_total.labels.assert_called_with(op="place_order")
    adapter.metrics.order_reject_total.inc.assert_called_once()
    assert adapter.get_phantom_candidates() == frozenset()


@pytest.mark.asyncio
async def test_a_plain_refusal_error_still_releases_the_slot_when_place_order_raised(tmp_config):
    place_order = MagicMock(side_effect=ResponseError("Session error NotReady"))
    adapter = _adapter(tmp_config, place_order=place_order)

    ok = await adapter._dispatch_to_api(_command())

    assert ok is False
    assert place_order.call_count == 1
    assert adapter.get_phantom_candidates() == frozenset()
    feedback = adapter._rejection_sink.get_nowait()
    assert feedback.was_approved is False
    adapter.metrics.order_reject_total.inc.assert_called()
