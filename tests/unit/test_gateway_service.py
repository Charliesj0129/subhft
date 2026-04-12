"""Tests for CE2-03: GatewayService."""

import asyncio
from contextlib import suppress
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    RiskDecision,
    Side,
    StormGuardState,
)
from hft_platform.gateway.channel import LocalIntentChannel
from hft_platform.gateway.dedup import IdempotencyStore
from hft_platform.gateway.exposure import ExposureStore
from hft_platform.gateway.policy import GatewayPolicy
from hft_platform.gateway.service import GatewayService


def _make_intent(
    intent_id: int = 1, key: str = "k1", intent_type: IntentType = IntentType.NEW, symbol: str = "TSE:2330"
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="s1",
        symbol=symbol,
        intent_type=intent_type,
        side=Side.BUY,
        price=1_000_000,
        qty=1,
        tif=TIF.LIMIT,
        idempotency_key=key,
    )


def _make_service(channel=None, approve=True, queue_full=False, exposure_store=None):
    if channel is None:
        channel = LocalIntentChannel(maxsize=64, ttl_ms=0)

    risk_engine = MagicMock()
    risk_engine.evaluate.return_value = RiskDecision(
        approved=approve, intent=MagicMock(), reason_code="OK" if approve else "TEST_REJECT"
    )

    cmd = OrderCommand(cmd_id=1, intent=MagicMock(), deadline_ns=999, storm_guard_state=StormGuardState.NORMAL)
    risk_engine.create_command.return_value = cmd

    api_queue = asyncio.Queue(maxsize=64)
    if queue_full:
        for _ in range(64):
            api_queue.put_nowait(MagicMock())
    order_adapter = MagicMock()
    order_adapter._api_queue = api_queue

    storm_guard = MagicMock()
    storm_guard.state = StormGuardState.NORMAL

    svc = GatewayService(
        channel=channel,
        risk_engine=risk_engine,
        order_adapter=order_adapter,
        exposure_store=exposure_store if exposure_store is not None else ExposureStore(),
        dedup_store=IdempotencyStore(persist_enabled=False),
        storm_guard=storm_guard,
        policy=GatewayPolicy(),
    )
    return svc, api_queue


@pytest.mark.asyncio
async def test_service_dispatches_approved_intent():
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_queue = _make_service(channel=ch, approve=True)

    intent = _make_intent(1, "k1")
    ch.submit_nowait(intent)

    # Run one iteration
    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert api_queue.qsize() == 1
    health = svc.get_health()
    assert health["dispatched"] == 1
    assert health["rejected"] == 0


@pytest.mark.asyncio
async def test_service_rejected_by_risk():
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_queue = _make_service(channel=ch, approve=False)

    intent = _make_intent(1, "k1")
    ch.submit_nowait(intent)

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert api_queue.qsize() == 0
    assert svc._rejected == 1


@pytest.mark.asyncio
async def test_service_dedup_hit_does_not_redispatch():
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_queue = _make_service(channel=ch, approve=True)

    for _ in range(3):
        ch.submit_nowait(_make_intent(1, "same-key"))

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # First intent dispatched; 2 dedup hits
    assert api_queue.qsize() == 1
    assert svc._dedup_hits == 2


@pytest.mark.asyncio
async def test_service_get_health_keys():
    svc, _ = _make_service()
    health = svc.get_health()
    required_keys = {"running", "dispatched", "rejected", "dedup_hits", "channel_depth", "policy_mode"}
    assert required_keys.issubset(health.keys())


@pytest.mark.asyncio
async def test_service_cancelled_error_stops_loop():
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, _ = _make_service(channel=ch)

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert svc.running is False


@pytest.mark.asyncio
async def test_service_halt_policy_blocks_new():
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_queue = _make_service(channel=ch, approve=True)
    svc._policy.set_halt()
    svc._storm_guard.state = StormGuardState.HALT

    ch.submit_nowait(_make_intent(1, "k-halt", IntentType.NEW))

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert api_queue.qsize() == 0
    assert svc._rejected >= 1


@pytest.mark.asyncio
async def test_order_queue_full_rejects_and_commits_dedup():
    """D1: When api_queue is full, intent is rejected and dedup records ORDER_QUEUE_FULL."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_queue = _make_service(channel=ch, queue_full=True)

    intent = _make_intent(1, "k_full")
    ch.submit_nowait(intent)

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert svc._dispatched == 0
    assert svc._rejected == 1
    rec = svc._dedup.check_or_reserve("k_full")
    assert rec is not None
    assert rec.approved is False
    assert rec.reason_code == "ORDER_QUEUE_FULL"


@pytest.mark.asyncio
async def test_exposure_symbol_limit_commits_dedup():
    """D2: ExposureLimitError is caught and dedup records EXPOSURE_SYMBOL_LIMIT.

    H1 fix: exposure is released after successful dispatch, so zero-balance entries
    are evicted by _evict_zeroes(). To reliably trigger ExposureLimitError we
    pre-populate the ExposureStore with a non-zero notional entry that cannot be
    evicted, consuming the single available symbol slot before svc2 runs.
    """
    exposure = ExposureStore(max_symbols=1)

    # Directly inject a non-zero entry so eviction cannot free the slot
    with exposure._lock:
        exposure._exposure.setdefault("", {}).setdefault("s1", {})["TSE:SYM_A"] = 1_000_000
        exposure._symbol_count = 1
        exposure._global_notional = 1_000_000

    # Second intent with a new symbol should hit ExposureLimitError
    ch2 = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc2, _ = _make_service(channel=ch2, exposure_store=exposure)
    ch2.submit_nowait(_make_intent(2, "k2", symbol="TSE:SYM_B"))
    task2 = asyncio.create_task(svc2.run())
    await asyncio.sleep(0.05)
    task2.cancel()
    with suppress(asyncio.CancelledError):
        await task2

    assert svc2._rejected >= 1
    rec = svc2._dedup.check_or_reserve("k2")
    assert rec is not None
    assert rec.approved is False
    assert rec.reason_code == "EXPOSURE_SYMBOL_LIMIT"


@pytest.mark.asyncio
async def test_service_typed_intent_path_uses_typed_risk_methods():
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_queue = _make_service(channel=ch, approve=True)

    typed_intent = _make_intent(11, "typed-k")
    frame = (
        "typed_intent_v1",
        typed_intent.intent_id,
        typed_intent.strategy_id,
        typed_intent.symbol,
        int(typed_intent.intent_type),
        int(typed_intent.side),
        typed_intent.price,
        typed_intent.qty,
        int(typed_intent.tif),
        typed_intent.target_order_id or "",
        typed_intent.timestamp_ns,
        typed_intent.source_ts_ns,
        typed_intent.reason,
        typed_intent.trace_id,
        typed_intent.idempotency_key,
        typed_intent.ttl_ns,
    )

    svc._risk_engine.typed_frame_view.return_value = typed_intent
    svc._risk_engine.evaluate_typed_frame.return_value = RiskDecision(approved=True, intent=typed_intent)
    svc._risk_engine.create_command_from_typed_frame.return_value = OrderCommand(
        cmd_id=99,
        intent=typed_intent,
        deadline_ns=999,
        storm_guard_state=StormGuardState.NORMAL,
    )

    ch.submit_typed_nowait(frame)

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert api_queue.qsize() == 1
    svc._risk_engine.evaluate_typed_frame.assert_called()
    svc._risk_engine.create_command_from_typed_frame.assert_called()
    eval_call = svc._risk_engine.evaluate_typed_frame.call_args
    cmd_call = svc._risk_engine.create_command_from_typed_frame.call_args
    assert "intent_view" in eval_call.kwargs
    assert "intent_view" in cmd_call.kwargs
    assert eval_call.kwargs["intent_view"] is typed_intent
    assert cmd_call.kwargs["intent_view"] is typed_intent


@pytest.mark.asyncio
async def test_service_typed_intent_path_uses_typed_adapter_submit_when_available():
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_queue = _make_service(channel=ch, approve=True)
    svc._order_adapter.submit_typed_command_nowait = MagicMock()
    svc._order_adapter._supports_typed_command_ingress = True

    typed_intent = _make_intent(12, "typed-k2")
    frame = (
        "typed_intent_v1",
        typed_intent.intent_id,
        typed_intent.strategy_id,
        typed_intent.symbol,
        int(typed_intent.intent_type),
        int(typed_intent.side),
        typed_intent.price,
        typed_intent.qty,
        int(typed_intent.tif),
        typed_intent.target_order_id or "",
        typed_intent.timestamp_ns,
        typed_intent.source_ts_ns,
        typed_intent.reason,
        typed_intent.trace_id,
        typed_intent.idempotency_key,
        typed_intent.ttl_ns,
    )

    svc._risk_engine.typed_frame_view.return_value = typed_intent
    svc._risk_engine.evaluate_typed_frame.return_value = RiskDecision(approved=True, intent=typed_intent)
    svc._risk_engine.create_typed_command_frame_from_typed_frame.return_value = (
        "typed_order_cmd_v1",
        123,
        999,
        int(StormGuardState.NORMAL),
        111,
        frame,
    )

    ch.submit_typed_nowait(frame)

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert api_queue.qsize() == 0
    svc._order_adapter.submit_typed_command_nowait.assert_called_once()
    svc._risk_engine.create_typed_command_frame_from_typed_frame.assert_called_once()


# ── P1-A: set_halt / set_normal delegation ───────────────────────────────────


def test_set_halt_transitions_policy_to_halt():
    """GatewayService.set_halt() must delegate to policy and set mode to HALT."""
    svc, _ = _make_service()
    from hft_platform.gateway.policy import GatewayPolicyMode

    assert svc._policy.mode == GatewayPolicyMode.NORMAL
    svc.set_halt()
    assert svc._policy.mode == GatewayPolicyMode.HALT


def test_set_normal_resets_policy_from_halt():
    """GatewayService.set_normal() must reset policy mode to NORMAL after HALT."""
    svc, _ = _make_service()
    from hft_platform.gateway.policy import GatewayPolicyMode

    svc.set_halt()
    assert svc._policy.mode == GatewayPolicyMode.HALT
    svc.set_normal()
    assert svc._policy.mode == GatewayPolicyMode.NORMAL


def test_set_halt_is_idempotent():
    """Calling set_halt() multiple times must not raise and policy stays in HALT."""
    svc, _ = _make_service()
    from hft_platform.gateway.policy import GatewayPolicyMode

    svc.set_halt()
    svc.set_halt()  # second call must be no-op
    assert svc._policy.mode == GatewayPolicyMode.HALT


# ── H6: DLQ size metric + H10: exposure notional metric ────────────────────


def _make_mock_metrics():
    """Return a mock MetricsRegistry with the two new gauges."""
    metrics = MagicMock()
    metrics.gateway_intent_channel_depth = MagicMock()
    metrics.gateway_dlq_size = MagicMock()
    metrics.gateway_exposure_global_notional_scaled = MagicMock()
    return metrics


def _inject_metrics(svc, mock_metrics) -> None:
    """Inject a mock MetricsRegistry into a GatewayService instance, updating owner_id."""
    svc._metrics = mock_metrics
    svc._metrics_owner_id = id(mock_metrics)
    svc._metrics_enabled = True
    svc._gateway_depth_counter = 0
    svc._gateway_depth_sample_every = 1  # emit on every call
    svc._gateway_depth_metric = mock_metrics.gateway_intent_channel_depth
    svc._gateway_dlq_metric = mock_metrics.gateway_dlq_size
    svc._gateway_exposure_metric = mock_metrics.gateway_exposure_global_notional_scaled


def test_update_channel_depth_emits_dlq_size():
    """_update_channel_depth_metric must call .set() on gateway_dlq_size gauge."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=50)
    exposure_store = ExposureStore()
    svc, _ = _make_service(channel=ch, exposure_store=exposure_store)

    mock_metrics = _make_mock_metrics()
    _inject_metrics(svc, mock_metrics)

    svc._update_channel_depth_metric()

    mock_metrics.gateway_dlq_size.set.assert_called_once()
    dlq_value = mock_metrics.gateway_dlq_size.set.call_args[0][0]
    assert dlq_value == ch.dlq_size()


def test_update_channel_depth_emits_exposure_notional():
    """_update_channel_depth_metric must call .set() on gateway_exposure_global_notional_scaled gauge."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    exposure_store = ExposureStore()
    svc, _ = _make_service(channel=ch, exposure_store=exposure_store)

    mock_metrics = _make_mock_metrics()
    _inject_metrics(svc, mock_metrics)

    svc._update_channel_depth_metric()

    mock_metrics.gateway_exposure_global_notional_scaled.set.assert_called_once()
    notional_value = mock_metrics.gateway_exposure_global_notional_scaled.set.call_args[0][0]
    assert notional_value == exposure_store.global_notional


def test_refresh_metrics_registry_caches_dlq_and_exposure_metrics(monkeypatch):
    """_refresh_metrics_registry must populate _gateway_dlq_metric and _gateway_exposure_metric."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, _ = _make_service(channel=ch)

    mock_metrics = _make_mock_metrics()

    import hft_platform.observability.metrics as metrics_module

    monkeypatch.setattr(metrics_module.MetricsRegistry, "get", staticmethod(lambda: mock_metrics))

    svc._refresh_metrics_registry()

    assert svc._gateway_dlq_metric is mock_metrics.gateway_dlq_size
    assert svc._gateway_exposure_metric is mock_metrics.gateway_exposure_global_notional_scaled


def test_update_channel_depth_skips_when_metrics_disabled():
    """_update_channel_depth_metric must be a no-op when metrics are disabled."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, _ = _make_service(channel=ch)

    mock_metrics = _make_mock_metrics()
    _inject_metrics(svc, mock_metrics)
    svc._metrics_enabled = False  # override back to disabled after inject

    svc._update_channel_depth_metric()

    mock_metrics.gateway_dlq_size.set.assert_not_called()
    mock_metrics.gateway_exposure_global_notional_scaled.set.assert_not_called()


# ── Rejection feedback tests ─────────────────────────────────────────────────


def _make_service_with_rejection_sink(channel=None, approve=True, queue_full=False, exposure_store=None):
    """Create GatewayService with a rejection_sink wired for feedback tests."""
    if channel is None:
        channel = LocalIntentChannel(maxsize=64, ttl_ms=0)

    risk_engine = MagicMock()
    risk_engine.evaluate.return_value = RiskDecision(
        approved=approve, intent=MagicMock(), reason_code="OK" if approve else "TEST_REJECT"
    )

    cmd = OrderCommand(cmd_id=1, intent=MagicMock(), deadline_ns=999, storm_guard_state=StormGuardState.NORMAL)
    risk_engine.create_command.return_value = cmd

    api_queue = asyncio.Queue(maxsize=64)
    if queue_full:
        for _ in range(64):
            api_queue.put_nowait(MagicMock())
    order_adapter = MagicMock()
    order_adapter._api_queue = api_queue

    storm_guard = MagicMock()
    storm_guard.state = StormGuardState.NORMAL

    rejection_sink = asyncio.Queue(maxsize=64)

    svc = GatewayService(
        channel=channel,
        risk_engine=risk_engine,
        order_adapter=order_adapter,
        exposure_store=exposure_store if exposure_store is not None else ExposureStore(),
        dedup_store=IdempotencyStore(persist_enabled=False),
        storm_guard=storm_guard,
        policy=GatewayPolicy(),
        rejection_sink=rejection_sink,
    )
    return svc, api_queue, rejection_sink


@pytest.mark.asyncio
async def test_risk_rejection_sends_feedback():
    """Risk rejection must enqueue a RiskFeedback with correct fields."""
    from hft_platform.contracts.strategy import RiskFeedback

    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, _, rejection_sink = _make_service_with_rejection_sink(channel=ch, approve=False)

    intent = _make_intent(42, "k-risk-rej")
    ch.submit_nowait(intent)

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert rejection_sink.qsize() == 1
    fb = rejection_sink.get_nowait()
    assert isinstance(fb, RiskFeedback)
    assert fb.strategy_id == "s1"
    assert fb.symbol == "TSE:2330"
    assert fb.reason_code == "TEST_REJECT"
    assert fb.side == Side.BUY


@pytest.mark.asyncio
async def test_policy_rejection_sends_feedback():
    """HALT policy rejection must enqueue a RiskFeedback."""
    from hft_platform.contracts.strategy import RiskFeedback

    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, _, rejection_sink = _make_service_with_rejection_sink(channel=ch, approve=True)
    svc._policy.set_halt()
    svc._storm_guard.state = StormGuardState.HALT

    ch.submit_nowait(_make_intent(43, "k-policy-rej", IntentType.NEW))

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert rejection_sink.qsize() == 1
    fb = rejection_sink.get_nowait()
    assert isinstance(fb, RiskFeedback)
    assert fb.reason_code == "HALT"


@pytest.mark.asyncio
async def test_queue_full_rejection_sends_feedback():
    """ORDER_QUEUE_FULL rejection must enqueue a RiskFeedback."""
    from hft_platform.contracts.strategy import RiskFeedback

    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, _, rejection_sink = _make_service_with_rejection_sink(channel=ch, queue_full=True)

    ch.submit_nowait(_make_intent(44, "k-qfull"))

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert rejection_sink.qsize() == 1
    fb = rejection_sink.get_nowait()
    assert isinstance(fb, RiskFeedback)
    assert fb.reason_code == "ORDER_QUEUE_FULL"


@pytest.mark.asyncio
async def test_approved_dispatch_sends_no_feedback():
    """Successful dispatch must NOT enqueue any RiskFeedback."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_queue, rejection_sink = _make_service_with_rejection_sink(channel=ch, approve=True)

    ch.submit_nowait(_make_intent(45, "k-ok"))

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert api_queue.qsize() == 1
    assert rejection_sink.qsize() == 0


@pytest.mark.asyncio
async def test_no_rejection_sink_does_not_raise():
    """When rejection_sink is None, rejection must not raise."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, _ = _make_service(channel=ch, approve=False)  # no rejection_sink
    assert svc._rejection_sink is None

    ch.submit_nowait(_make_intent(46, "k-no-sink"))

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert svc._rejected == 1  # rejection counted, no crash
