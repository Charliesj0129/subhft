"""A timed-out place_order is an unknown outcome, not a failure.

THESHOW 2026-10-28..11-02: 120 place_order calls hit the 3 s wrapper timeout
and 116 of them were accepted by the broker, answered 5-30 s later. Each was
counted as a reject, a circuit-breaker failure and a dead letter at the timeout,
so OrderDeadLetterQueueGrowing fired for 143 minutes on 11-02 over orders that
existed. Now:

- at the timeout: ``order_outcome_unknown_total{op}``; no reject, no global or
  per-strategy breaker failure, no dead letter; the strategy keeps its slot
  (``was_approved=True``) as before;
- answered "not sent": reject + DLQ ``connection_error``;
- never answered within the phantom TTL: reject + DLQ ``outcome_unresolved``;
- answered with a Trade: neither (bound and cancelled by ``_bind_late_trade``).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side
from hft_platform.core import timebase
from hft_platform.order.adapter import OrderAdapter
from hft_platform.order.deadletter import DeadLetterQueue, RejectionReason
from hft_platform.risk.storm_guard import StormGuardState

_STRATEGY = "R47_MAKER_TMF"


@pytest.fixture()
def tmp_config(tmp_path):
    cfg = tmp_path / "order.yaml"
    cfg.write_text(
        "rate_limits:\n  shioaji_soft_cap: 180\n  shioaji_hard_cap: 250\n  window_seconds: 10\n"
        "circuit_breaker:\n  threshold: 5\n  timeout_seconds: 60\n"
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
    """A place_order that answers only when released -- after the wrapper timed out."""

    def __init__(self, outcome: Any) -> None:
        self.release = threading.Event()
        self._outcome = outcome

    def __call__(self, *_a: Any, **_kw: Any) -> Any:
        self.release.wait(timeout=5.0)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def _adapter(tmp_config: str, place_order: Any) -> OrderAdapter:
    client = MagicMock()
    client.get_exchange = MagicMock(return_value="TAIFEX")
    client.place_order = place_order
    client.mode = "simulation"
    client.activate_ca = False
    adapter = OrderAdapter(config_path=tmp_config, order_queue=asyncio.Queue(maxsize=128), broker_client=client)
    codec = MagicMock()
    codec.encode_side.return_value = "Buy"
    codec.encode_tif.return_value = "ROD"
    codec.encode_price_type.return_value = "LMT"
    adapter._broker_codec = codec
    adapter._add_to_dlq = AsyncMock()
    adapter._audit_writer = MagicMock()
    adapter.set_rejection_sink(asyncio.Queue(maxsize=64))
    adapter._api_timeout_s = 0.05  # the broker above answers only after it
    return adapter


def _command(intent_id: int = 7) -> OrderCommand:
    return OrderCommand(
        cmd_id=intent_id,
        intent=OrderIntent(
            intent_id=intent_id,
            strategy_id=_STRATEGY,
            symbol="TMFI6",
            price=4623_9000,
            qty=1,
            side=Side.BUY,
            intent_type=IntentType.NEW,
            tif=TIF.LIMIT,
        ),
        deadline_ns=timebase.now_ns() + 1_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
    )


async def _until(predicate: Any, timeout_s: float = 5.0) -> None:
    for _ in range(int(timeout_s / 0.005)):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never became true")


def _dlq_reasons(adapter: OrderAdapter) -> list[Any]:
    return [c.args[1] for c in adapter._add_to_dlq.await_args_list]


@pytest.mark.asyncio
async def test_a_timed_out_place_order_is_counted_unknown_not_failed(tmp_config):
    broker = _LateBroker({"order": {"ordno": "A1B2C", "seqno": "000123"}})
    adapter = _adapter(tmp_config, broker)

    ok = await adapter._dispatch_to_api(_command())
    broker.release.set()

    assert ok is False
    adapter.metrics.order_outcome_unknown_total.labels.assert_called_with(op="place_order")
    adapter.metrics.order_reject_total.inc.assert_not_called()
    assert adapter.circuit_breaker.failure_count == 0
    assert adapter.strategy_cb_mgr.get_breaker(_STRATEGY).failure_count == 0
    assert _dlq_reasons(adapter) == []
    feedback = adapter._rejection_sink.get_nowait()
    assert feedback.was_approved is True, "the order may be live: the strategy keeps its slot"


@pytest.mark.asyncio
async def test_five_timeouts_do_not_trip_the_breaker(tmp_config):
    """Five in a row used to open the global breaker for 60 s over orders that existed."""
    broker = _LateBroker({"order": {"ordno": "A1"}})
    adapter = _adapter(tmp_config, broker)

    for i in range(5):
        await adapter._dispatch_to_api(_command(intent_id=10 + i))
    broker.release.set()

    assert adapter.circuit_breaker.is_open() is False


@pytest.mark.asyncio
async def test_a_timed_out_order_answered_not_sent_is_dead_lettered_then(tmp_config):
    broker = _LateBroker(RuntimeError("Session not ready"))
    adapter = _adapter(tmp_config, broker)
    await adapter._dispatch_to_api(_command())
    assert _dlq_reasons(adapter) == []

    broker.release.set()
    await _until(lambda: _dlq_reasons(adapter))

    assert _dlq_reasons(adapter) == [RejectionReason.CONNECTION_ERROR]
    adapter.metrics.order_reject_total.inc.assert_called_once()
    assert adapter.circuit_breaker.failure_count == 1, "a known failure counts toward the breaker"
    assert adapter.strategy_cb_mgr.get_breaker(_STRATEGY).failure_count == 1


@pytest.mark.asyncio
async def test_a_ttl_release_then_a_late_not_sent_answer_is_booked_once(tmp_config):
    broker = _LateBroker(RuntimeError("Session not ready"))
    adapter = _adapter(tmp_config, broker)
    late_seen = asyncio.Event()
    real_reporter = adapter._late_place_reporter

    def _reporter(intent: OrderIntent, start_ns: int):
        inner = real_reporter(intent, start_ns)

        def _wrapped(kind: str, value: Any) -> None:
            inner(kind, value)
            late_seen.set()

        return _wrapped

    adapter._late_place_reporter = _reporter
    await adapter._dispatch_to_api(_command())
    assert await adapter.release_stale_phantom_pendings(ttl_s=0.0) == 1

    broker.release.set()
    await asyncio.wait_for(late_seen.wait(), timeout=5.0)
    await asyncio.sleep(0)  # let a (wrongly) spawned DLQ task run

    assert _dlq_reasons(adapter) == [RejectionReason.OUTCOME_UNRESOLVED]
    adapter.metrics.order_reject_total.inc.assert_called_once()


def _intent(intent_type: IntentType) -> OrderIntent:
    return OrderIntent(
        intent_id=99,
        strategy_id=_STRATEGY,
        symbol="TMFI6",
        price=4623_9000,
        qty=1,
        side=Side.SELL,
        intent_type=intent_type,
        tif=TIF.LIMIT,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent_type", "op"), [(IntentType.FORCE_FLAT, "place_order"), (IntentType.AMEND, "update_order")]
)
async def test_a_timed_out_flatten_or_amend_is_still_a_failure(tmp_config, intent_type, op):
    # Review of dc21fd6da: only a NEW is booked again later (phantom, late
    # reporter, TTL sweep); a FORCE_FLAT or AMEND timeout must count here.
    broker = _LateBroker({"order": {"ordno": "A1"}})
    adapter = _adapter(tmp_config, broker)

    result = await adapter._call_api(op, broker, intent=_intent(intent_type))
    broker.release.set()

    assert result is None
    adapter.metrics.order_outcome_unknown_total.labels.assert_called_with(op=op)
    adapter.metrics.order_reject_total.inc.assert_called_once()
    assert adapter.circuit_breaker.failure_count == 1
    assert adapter.strategy_cb_mgr.get_breaker(_STRATEGY).failure_count == 1
    assert adapter.get_phantom_candidates() == frozenset(), "no phantom: nothing books it later"


@pytest.mark.asyncio
async def test_a_failed_dlq_add_is_logged(tmp_config):
    adapter = _adapter(tmp_config, _LateBroker(None))
    adapter._add_to_dlq = AsyncMock(side_effect=RuntimeError("dlq disk"))

    with patch("hft_platform.order.adapter.logger") as log:
        adapter._spawn_dlq_add(_intent(IntentType.NEW), RejectionReason.CONNECTION_ERROR, "x")
        await _until(lambda: not adapter._phantom_bind_tasks)

    assert any(c.args and c.args[0] == "dlq_add_failed" for c in log.error.call_args_list)


@pytest.mark.asyncio
async def test_a_timed_out_order_answered_with_a_trade_is_not_dead_lettered(tmp_config):
    broker = _LateBroker({"order": {"ordno": "A1B2C", "seqno": "000123"}})
    adapter = _adapter(tmp_config, broker)
    adapter._bind_late_trade = AsyncMock()  # what is done with it is pinned elsewhere
    await adapter._dispatch_to_api(_command())

    broker.release.set()
    await _until(lambda: adapter._bind_late_trade.await_count == 1)

    assert _dlq_reasons(adapter) == []
    adapter.metrics.order_reject_total.inc.assert_not_called()


@pytest.mark.asyncio
async def test_a_timed_out_order_never_answered_is_dead_lettered_at_the_ttl(tmp_config):
    broker = _LateBroker({"order": {"ordno": "A1"}})
    adapter = _adapter(tmp_config, broker)
    await adapter._dispatch_to_api(_command())

    released = await adapter.release_stale_phantom_pendings(ttl_s=0.0)
    broker.release.set()

    assert released == 1
    assert _dlq_reasons(adapter) == [RejectionReason.OUTCOME_UNRESOLVED]
    adapter.metrics.order_reject_total.inc.assert_called_once()


@pytest.mark.asyncio
async def test_a_late_trade_records_its_real_duration_and_broker_stamp(tmp_config):
    status = MagicMock()
    status.order_datetime = "2026-11-02 09:13:05.123000"
    trade = MagicMock()
    trade.status = status
    trade.order = {"ordno": "A1"}
    broker = _LateBroker(trade)
    adapter = _adapter(tmp_config, broker)
    adapter._bind_late_trade = AsyncMock()
    await adapter._dispatch_to_api(_command())

    with patch("hft_platform.order.adapter.logger") as log:
        broker.release.set()
        await _until(lambda: adapter._bind_late_trade.await_count == 1)

    stages = [c.args[0] for c in adapter.latency.record.call_args_list]
    assert "api_place_order_late" in stages
    late = [c for c in log.warning.call_args_list if c.args and c.args[0] == "place_order_late_result"]
    assert late and late[0].kwargs["order_datetime"] == "2026-11-02 09:13:05.123000"
    assert late[0].kwargs["sent_at_ns"] > 0


# --------------------------------------------------------------------------- #
# The DLQ counts by reason, every reason born at 0                             #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def _fresh_registry():
    from hft_platform.observability.metrics import MetricsRegistry

    MetricsRegistry._instance = None
    try:
        yield
    finally:
        MetricsRegistry._instance = None


def _real_dlq(tmp_path) -> DeadLetterQueue:
    return DeadLetterQueue(dlq_dir=str(tmp_path / "dlq"))


def _adds(dlq: DeadLetterQueue) -> dict[str, float]:
    return {
        s.labels["reason"]: s.value
        for m in dlq._metrics.order_dlq_adds_total.collect()
        for s in m.samples
        if s.name.endswith("_total")
    }


def test_every_dlq_reason_exists_at_zero(tmp_path, _fresh_registry):
    dlq = _real_dlq(tmp_path)

    assert _adds(dlq) == {r.value: 0.0 for r in RejectionReason}


@pytest.mark.asyncio
async def test_a_dlq_add_is_counted_under_its_reason(tmp_path, _fresh_registry):
    dlq = _real_dlq(tmp_path)

    await dlq.add(
        order_id="1",
        strategy_id=_STRATEGY,
        symbol="TMFI6",
        side="BUY",
        price=4623_9000,
        qty=1,
        reason=RejectionReason.OUTCOME_UNRESOLVED,
        error_message="phantom_ttl_30s",
    )

    assert _adds(dlq)["outcome_unresolved"] == 1.0
    assert _adds(dlq)["connection_error"] == 0.0
