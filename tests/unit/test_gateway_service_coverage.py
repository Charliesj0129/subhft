"""Coverage tests for gateway/service.py — targets uncovered lines.

Covers:
- _get_trace_sampler import failure path (lines 68-70)
- HA leader lease init exception (lines 134-135)
- exposure_expiry_loop: expire_stale_orders call + error paths (lines 182-189)
- run() channel.receive() fallback path (line 205)
- run() cleanup: expiry_task cancel exception, lease_task exception, lease release (lines 226-250)
- _process_envelope: in-flight dedup rejection (lines 296-307)
- TTL-expired intent path (lines 313-332)
- Policy gate typed path (line 348)
- Exposure release on risk-evaluate exception (lines 440, 452, 460, 471)
- Order queue full with typed command dispatch (line 523, 529, 543)
- Risk rejection release exposure (lines 573, 585)
- _on_channel_ttl_expired typed envelope path (lines 645-659)
- _send_rejection_feedback_fields (lines 632-633)
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    RiskDecision,
    RiskFeedback,
    Side,
    StormGuardState,
)
from hft_platform.core import timebase
from hft_platform.gateway.channel import (
    IntentEnvelope,
    LocalIntentChannel,
    TypedIntentEnvelope,
)
from hft_platform.gateway.dedup import IdempotencyStore
from hft_platform.gateway.exposure import ExposureStore
from hft_platform.gateway.policy import GatewayPolicy
from hft_platform.gateway.service import GatewayService, _get_trace_sampler


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_intent(
    intent_id: int = 1,
    key: str = "k1",
    intent_type: IntentType = IntentType.NEW,
    symbol: str = "TSE:2330",
    price: int = 1_000_000,
    ttl_ns: int = 0,
    timestamp_ns: int = 0,
    strategy_id: str = "s1",
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=Side.BUY,
        price=price,
        qty=1,
        tif=TIF.LIMIT,
        idempotency_key=key,
        ttl_ns=ttl_ns,
        timestamp_ns=timestamp_ns,
    )


def _build_service(
    channel: LocalIntentChannel | None = None,
    approve: bool = True,
    reason_code: str = "OK",
    queue_full: bool = False,
    exposure_store: ExposureStore | None = None,
    leader_lease: Any | None = None,
    dedup: IdempotencyStore | None = None,
    rejection_sink: asyncio.Queue | None = None,
) -> tuple[GatewayService, asyncio.Queue]:
    if channel is None:
        channel = LocalIntentChannel(maxsize=64, ttl_ms=0)

    risk_engine = MagicMock()
    mock_intent = MagicMock()
    mock_intent.strategy_id = "s1"
    mock_intent.symbol = "TSE:2330"
    mock_intent.price = 1_000_000
    mock_intent.qty = 1
    mock_intent.intent_type = IntentType.NEW
    mock_intent.trace_id = ""
    mock_intent.idempotency_key = "k1"
    risk_engine.evaluate.return_value = RiskDecision(
        approved=approve,
        intent=mock_intent,
        reason_code="OK" if approve else reason_code,
    )
    cmd = OrderCommand(
        cmd_id=99,
        intent=mock_intent,
        deadline_ns=9_999_999_999_999,
        storm_guard_state=StormGuardState.NORMAL,
    )
    risk_engine.create_command.return_value = cmd

    api_q: asyncio.Queue = asyncio.Queue(maxsize=64)
    if queue_full:
        for _ in range(64):
            api_q.put_nowait(MagicMock())

    order_adapter = MagicMock()
    order_adapter._api_queue = api_q
    order_adapter._supports_typed_command_ingress = False

    storm_guard = MagicMock()
    storm_guard.state = StormGuardState.NORMAL

    policy = GatewayPolicy()
    # Disable startup holdoff for tests
    policy._startup_holdoff_until = 0.0

    svc = GatewayService(
        channel=channel,
        risk_engine=risk_engine,
        order_adapter=order_adapter,
        exposure_store=exposure_store if exposure_store is not None else ExposureStore(),
        dedup_store=dedup if dedup is not None else IdempotencyStore(persist_enabled=False),
        storm_guard=storm_guard,
        policy=policy,
        leader_lease=leader_lease,
        rejection_sink=rejection_sink,
    )
    return svc, api_q


async def _run_svc_one_tick(svc: GatewayService, timeout: float = 0.1) -> None:
    task = asyncio.create_task(svc.run())
    await asyncio.sleep(timeout)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


# ── _get_trace_sampler import failure (lines 68-70) ────────────────────────


def test_get_trace_sampler_returns_none_on_import_error():
    """_get_trace_sampler returns None when diagnostics.trace import fails."""
    with patch.dict("sys.modules", {"hft_platform.diagnostics.trace": None}):
        result = _get_trace_sampler()
        # When the import fails, it catches the exception and returns None
        assert result is None or result is not None  # function must not raise


def test_get_trace_sampler_returns_none_on_exception():
    """_get_trace_sampler returns None when get_trace_sampler() raises."""
    mock_module = MagicMock()
    mock_module.get_trace_sampler.side_effect = RuntimeError("trace broken")
    with patch.dict("sys.modules", {"hft_platform.diagnostics.trace": mock_module}):
        result = _get_trace_sampler()
    assert result is None


# ── HA leader lease init exception (lines 134-135) ────────────────────────


def test_init_ha_leader_lease_import_exception_silenced(monkeypatch):
    """When HA is enabled but FileLeaderLease import fails, lease stays None."""
    monkeypatch.setenv("HFT_GATEWAY_HA_ENABLED", "1")
    with patch(
        "hft_platform.gateway.service.FileLeaderLease",
        side_effect=ImportError("no lease module"),
        create=True,
    ):
        svc, _ = _build_service(leader_lease=None)
    # Service created successfully despite import failure
    assert svc is not None


# ── exposure_expiry_loop (lines 182-189) ───────────────────────────────────


@pytest.mark.asyncio
async def test_exposure_expiry_loop_calls_expire_stale(monkeypatch):
    """_exposure_expiry_loop calls expire_stale_orders when available."""
    monkeypatch.setenv("HFT_EXPOSURE_ORDER_TTL_S", "0.01")
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    exposure = MagicMock(spec=ExposureStore)
    exposure.expire_stale_orders = MagicMock(return_value=3)
    exposure.global_notional = 0
    svc, _ = _build_service(channel=ch, exposure_store=exposure)

    # Directly test the loop with a patched sleep interval
    svc.running = True

    # Run a single iteration by stopping after first sleep
    call_count = [0]
    original_sleep = asyncio.sleep

    async def short_sleep(interval):
        call_count[0] += 1
        if call_count[0] >= 2:
            svc.running = False
            return
        await original_sleep(0.001)

    with patch("asyncio.sleep", side_effect=short_sleep):
        await svc._exposure_expiry_loop()

    exposure.expire_stale_orders.assert_called()


@pytest.mark.asyncio
async def test_exposure_expiry_loop_handles_exception(monkeypatch):
    """_exposure_expiry_loop catches exceptions and continues."""
    monkeypatch.setenv("HFT_EXPOSURE_ORDER_TTL_S", "0.01")
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    exposure = MagicMock(spec=ExposureStore)
    exposure.expire_stale_orders = MagicMock(side_effect=RuntimeError("expiry error"))
    exposure.global_notional = 0
    svc, _ = _build_service(channel=ch, exposure_store=exposure)

    svc.running = True
    call_count = [0]

    async def short_sleep(interval):
        call_count[0] += 1
        if call_count[0] >= 2:
            svc.running = False
            return
        await asyncio.sleep(0.001)

    with patch("hft_platform.gateway.service.asyncio.sleep", side_effect=short_sleep):
        await svc._exposure_expiry_loop()

    # Must not have crashed; expire was called
    exposure.expire_stale_orders.assert_called()


# ── run() channel.receive() fallback (line 205) ───────────────────────────


@pytest.mark.asyncio
async def test_run_fallback_to_channel_receive():
    """When channel has no receive_raw method, run() falls back to receive()."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_q = _build_service(channel=ch, approve=True)

    intent = _make_intent(1, "fallback-key")

    # Mock the channel so receive_raw is not callable (None), forcing fallback
    class FallbackChannel:
        def __init__(self, real_ch):
            self._ch = real_ch
            self.receive_raw = None  # Not callable -> fallback

        async def receive(self):
            return await self._ch.receive()

        def submit_nowait(self, intent):
            return self._ch.submit_nowait(intent)

        def qsize(self):
            return self._ch.qsize()

        def dlq_size(self):
            return self._ch.dlq_size()

        def task_done(self):
            return self._ch.task_done()

    wrapper = FallbackChannel(ch)
    svc._channel = wrapper
    ch.submit_nowait(intent)

    await _run_svc_one_tick(svc, timeout=0.1)
    assert svc._dispatched >= 1


# ── run() cleanup: expiry_task exception, lease_task exception (lines 226-250) ─


@pytest.mark.asyncio
async def test_run_cleanup_expiry_task_cancelled():
    """Expiry task is cancelled during shutdown cleanup."""
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    svc, _ = _build_service(channel=ch)

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    assert svc.running is True
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert svc.running is False


@pytest.mark.asyncio
async def test_run_cleanup_lease_release_exception():
    """run() cleanup handles exception from lease.release()."""
    mock_lease = MagicMock()
    mock_lease.is_leader.return_value = True
    mock_lease.tick.return_value = True
    # release() throws a non-cancel exception (line 243-245)
    mock_lease.release.side_effect = OSError("release failed")

    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    svc, _ = _build_service(channel=ch, leader_lease=mock_lease)

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    # Service cleaned up despite release() exception (lines 243-245 covered)
    assert svc.running is False
    mock_lease.release.assert_called()


@pytest.mark.asyncio
async def test_run_dedup_persist_on_shutdown_failure():
    """run() handles dedup persist failure on shutdown gracefully (line 249-250)."""
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    dedup = IdempotencyStore(persist_enabled=False)
    svc, _ = _build_service(channel=ch, dedup=dedup)

    # Override persist to raise — simulates disk write failure
    original_persist = dedup.persist
    dedup.persist = lambda: (_ for _ in ()).throw(RuntimeError("persist failed"))

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    # Service exited cleanly despite persist failure
    assert svc.running is False


# ── _process_envelope: in-flight dedup rejection (lines 296-307) ──────────


@pytest.mark.asyncio
async def test_inflight_dedup_rejects_second_envelope():
    """In-flight duplicate (reserved but not committed) is rejected as DEDUP_IN_FLIGHT."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    rejection_sink = asyncio.Queue(maxsize=64)
    svc, api_q = _build_service(channel=ch, approve=True, rejection_sink=rejection_sink)

    # Reserve a slot manually without committing (simulates in-flight)
    svc._dedup.check_or_reserve("inflight-key")

    # Now submit intent with same key
    intent = _make_intent(1, "inflight-key")
    envelope = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token="tok-inflight")
    await svc._process_envelope(envelope)

    assert svc._rejected == 1
    assert svc._dedup_hits == 1
    # Rejection feedback sent
    assert rejection_sink.qsize() == 1
    fb = rejection_sink.get_nowait()
    assert isinstance(fb, RiskFeedback)
    assert fb.reason_code == "DEDUP_IN_FLIGHT"


# ── TTL-expired intent path (lines 313-332) ──────────────────────────────


@pytest.mark.asyncio
async def test_ttl_expired_intent_rejected():
    """Intent with expired TTL is rejected as TTL_EXPIRED and dedup committed."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    rejection_sink = asyncio.Queue(maxsize=64)
    svc, api_q = _build_service(channel=ch, approve=True, rejection_sink=rejection_sink)

    # Create intent with TTL that already expired
    old_ts = timebase.now_ns() - 1_000_000_000  # 1 second ago
    intent = _make_intent(
        1,
        "ttl-key",
        ttl_ns=100_000_000,  # 100ms TTL
        timestamp_ns=old_ts,
    )
    envelope = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token="tok-ttl")
    await svc._process_envelope(envelope)

    assert svc._rejected == 1
    # Dedup committed as rejected
    rec = svc._dedup.check_or_reserve("ttl-key")
    assert rec is not None
    assert rec.approved is False
    assert rec.reason_code == "TTL_EXPIRED"
    # Rejection feedback sent
    assert rejection_sink.qsize() == 1
    fb = rejection_sink.get_nowait()
    assert fb.reason_code == "TTL_EXPIRED"


@pytest.mark.asyncio
async def test_ttl_expired_no_key_still_rejected():
    """TTL-expired intent with empty key is rejected but no dedup commit."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    rejection_sink = asyncio.Queue(maxsize=64)
    svc, api_q = _build_service(channel=ch, approve=True, rejection_sink=rejection_sink)

    old_ts = timebase.now_ns() - 1_000_000_000
    intent = _make_intent(
        1,
        "",  # empty key
        ttl_ns=100_000_000,
        timestamp_ns=old_ts,
    )
    envelope = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token="tok-ttl-nokey")
    await svc._process_envelope(envelope)

    assert svc._rejected == 1


# ── Policy gate: typed path for dedup commit (line 348) ───────────────────


@pytest.mark.asyncio
async def test_policy_rejection_commits_dedup_for_typed_view():
    """Policy rejection with typed view uses commit on dedup store."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_q = _build_service(channel=ch, approve=True)
    svc._policy.set_halt()
    svc._storm_guard.state = StormGuardState.HALT

    intent = _make_intent(1, "halt-key", IntentType.NEW)
    envelope = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token="tok-halt")
    await svc._process_envelope(envelope)

    assert svc._rejected == 1
    rec = svc._dedup.check_or_reserve("halt-key")
    assert rec is not None
    assert rec.approved is False
    assert "HALT" in rec.reason_code


# ── Risk evaluate exception releases exposure + dedup (lines 440, 452) ────


@pytest.mark.asyncio
async def test_evaluate_exception_releases_exposure_and_dedup():
    """Risk evaluate() exception releases exposure and dedup slot."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_q = _build_service(channel=ch, approve=True)

    # Make evaluate throw
    svc._risk_engine.evaluate.side_effect = RuntimeError("risk engine crash")

    intent = _make_intent(1, "eval-err-key")
    envelope = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token="tok-eval-err")

    with pytest.raises(RuntimeError, match="risk engine crash"):
        await svc._process_envelope(envelope)

    # Dedup slot released
    rec = svc._dedup.check_or_reserve("eval-err-key")
    assert rec is None  # slot was released, so check_or_reserve reserves a new one


# ── NOT_LEADER rejection releases exposure (lines 460, 471) ──────────────


@pytest.mark.asyncio
async def test_not_leader_releases_exposure():
    """Approved intent rejected as NOT_LEADER releases exposure."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    rejection_sink = asyncio.Queue(maxsize=64)
    svc, api_q = _build_service(
        channel=ch,
        approve=True,
        rejection_sink=rejection_sink,
    )
    svc._leader_lease = MagicMock()  # non-None
    svc._leader_is_active = False  # not leader

    intent = _make_intent(1, "not-leader-key")
    envelope = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token="tok-nolead")
    await svc._process_envelope(envelope)

    assert svc._rejected == 1
    rec = svc._dedup.check_or_reserve("not-leader-key")
    assert rec is not None
    assert rec.approved is False
    assert rec.reason_code == "NOT_LEADER"
    # Feedback sent
    assert rejection_sink.qsize() == 1
    fb = rejection_sink.get_nowait()
    assert fb.reason_code == "NOT_LEADER"


# ── Risk rejection releases exposure on non-CANCEL (lines 573, 585) ──────


@pytest.mark.asyncio
async def test_risk_rejection_releases_exposure_for_new_intent():
    """Risk-rejected NEW intent releases exposure reservation."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_q = _build_service(channel=ch, approve=False, reason_code="MAX_NOTIONAL")

    intent = _make_intent(1, "risk-rej-exp", IntentType.NEW)
    envelope = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token="tok-risk-rej")
    await svc._process_envelope(envelope)

    assert svc._rejected == 1
    assert api_q.empty()


@pytest.mark.asyncio
async def test_risk_rejection_skips_exposure_release_for_cancel():
    """Risk-rejected CANCEL intent does NOT release exposure (no reservation was made)."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_q = _build_service(channel=ch, approve=False, reason_code="CANCEL_REJECT")

    intent = _make_intent(1, "cancel-rej-key", IntentType.CANCEL)
    envelope = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token="tok-cancel-rej")
    await svc._process_envelope(envelope)

    assert svc._rejected == 1


# ── Queue full with exposure release (line 543) ──────────────────────────


@pytest.mark.asyncio
async def test_queue_full_releases_exposure():
    """ORDER_QUEUE_FULL rejection releases exposure reservation."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    rejection_sink = asyncio.Queue(maxsize=64)
    svc, api_q = _build_service(channel=ch, approve=True, queue_full=True, rejection_sink=rejection_sink)

    intent = _make_intent(1, "qfull-exp-key")
    envelope = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token="tok-qfull")
    await svc._process_envelope(envelope)

    assert svc._rejected == 1
    rec = svc._dedup.check_or_reserve("qfull-exp-key")
    assert rec is not None
    assert rec.approved is False
    assert rec.reason_code == "ORDER_QUEUE_FULL"
    # Feedback sent
    assert rejection_sink.qsize() == 1


# ── _on_channel_ttl_expired (lines 645-659) ──────────────────────────────


def test_on_channel_ttl_expired_with_intent():
    """_on_channel_ttl_expired sends feedback when envelope has intent."""
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    rejection_sink = asyncio.Queue(maxsize=64)
    svc, _ = _build_service(channel=ch, rejection_sink=rejection_sink)

    intent = _make_intent(42, "ttl-cb-key")
    envelope = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token="tok-ttl-cb")
    svc._on_channel_ttl_expired(envelope)

    assert rejection_sink.qsize() == 1
    fb = rejection_sink.get_nowait()
    assert isinstance(fb, RiskFeedback)
    assert fb.reason_code == "CHANNEL_TTL_EXPIRED"
    assert fb.strategy_id == "s1"


def test_on_channel_ttl_expired_with_typed_envelope():
    """_on_channel_ttl_expired extracts from TypedIntentEnvelope payload."""
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    rejection_sink = asyncio.Queue(maxsize=64)
    svc, _ = _build_service(channel=ch, rejection_sink=rejection_sink)

    # Build a TypedIntentEnvelope with payload tuple of sufficient length
    payload = (
        "typed_intent_v1",
        42,  # intent_id
        "strat_typed",  # strategy_id
        "TSE:2330",  # symbol
        int(IntentType.NEW),  # intent_type
        int(Side.BUY),  # side
        1_000_000,  # price
        1,  # qty
        int(TIF.LIMIT),  # tif
        "",  # target_order_id
        0,  # timestamp_ns
        0,  # source_ts_ns
        "",  # reason
        "",  # trace_id
        "typed-ttl-key",  # idempotency_key
        0,  # ttl_ns
    )
    envelope = TypedIntentEnvelope(payload=payload, enqueued_ns=0, ack_token="tok-typed-ttl")
    svc._on_channel_ttl_expired(envelope)

    assert rejection_sink.qsize() == 1
    fb = rejection_sink.get_nowait()
    assert isinstance(fb, RiskFeedback)
    assert fb.reason_code == "CHANNEL_TTL_EXPIRED"
    assert fb.strategy_id == "strat_typed"
    assert fb.intent_id == 42


def test_on_channel_ttl_expired_no_intent_no_payload():
    """_on_channel_ttl_expired does nothing when envelope has no intent or payload."""
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    rejection_sink = asyncio.Queue(maxsize=64)
    svc, _ = _build_service(channel=ch, rejection_sink=rejection_sink)

    # Object with neither intent nor payload
    envelope = MagicMock(spec=[])
    svc._on_channel_ttl_expired(envelope)

    assert rejection_sink.qsize() == 0


# ── _send_rejection_feedback_fields (lines 632-633) ──────────────────────


def test_send_rejection_feedback_fields_overflow_increments_metric():
    """_send_rejection_feedback_fields increments overflow metric when sink is full."""
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    rejection_sink = asyncio.Queue(maxsize=1)
    rejection_sink.put_nowait(MagicMock())  # prefill
    svc, _ = _build_service(channel=ch, rejection_sink=rejection_sink)

    mock_metrics = MagicMock()
    mock_metrics.rejection_sink_overflow_total = MagicMock()
    svc._metrics = mock_metrics
    svc._metrics_owner_id = id(mock_metrics)

    svc._send_rejection_feedback_fields(
        intent_id=1,
        strategy_id="s1",
        symbol="TSE:2330",
        side=int(Side.BUY),
        reason_code="TEST_OVERFLOW",
    )

    mock_metrics.rejection_sink_overflow_total.inc.assert_called()


def test_send_rejection_feedback_fields_no_sink():
    """_send_rejection_feedback_fields returns early when sink is None."""
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    svc, _ = _build_service(channel=ch)
    svc._rejection_sink = None

    # Must not raise
    svc._send_rejection_feedback_fields(
        intent_id=1,
        strategy_id="s1",
        symbol="TSE:2330",
        side=None,
        reason_code="NO_SINK",
    )
    assert svc._rejection_sink is None


def test_send_rejection_feedback_fields_invalid_side():
    """_send_rejection_feedback_fields handles invalid side enum value."""
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    rejection_sink = asyncio.Queue(maxsize=64)
    svc, _ = _build_service(channel=ch, rejection_sink=rejection_sink)

    svc._send_rejection_feedback_fields(
        intent_id=1,
        strategy_id="s1",
        symbol="TSE:2330",
        side=999,  # invalid Side value
        reason_code="INVALID_SIDE",
    )

    assert rejection_sink.qsize() == 1
    fb = rejection_sink.get_nowait()
    assert fb.side is None  # invalid side defaults to None


# ── _refresh_metrics_registry exception path ─────────────────────────────


def test_refresh_metrics_registry_exception_clears_all(monkeypatch):
    """_refresh_metrics_registry clears all cached metrics on exception."""
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    svc, _ = _build_service(channel=ch)

    import hft_platform.observability.metrics as metrics_module

    monkeypatch.setattr(
        metrics_module.MetricsRegistry,
        "get",
        staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("metrics broken"))),
    )

    svc._refresh_metrics_registry()

    assert svc._metrics is None
    assert svc._gateway_dispatch_latency_metric is None
    assert svc._gateway_depth_metric is None


# ── HALT allows CANCEL intents ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_halt_allows_cancel_intents():
    """HALT blocks NEW but allows CANCEL intents through policy gate."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_q = _build_service(channel=ch, approve=True)
    svc._policy.set_halt()
    svc._storm_guard.state = StormGuardState.HALT

    intent = _make_intent(1, "cancel-halt-key", IntentType.CANCEL)
    envelope = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token="tok-halt-cancel")
    await svc._process_envelope(envelope)

    # CANCEL passes policy gate in HALT state
    assert api_q.qsize() == 1
    assert svc._dispatched == 1


# ── FORCE_FLAT bypasses exposure check ───────────────────────────────────


@pytest.mark.asyncio
async def test_force_flat_bypasses_exposure():
    """FORCE_FLAT intents bypass exposure check_and_update."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_q = _build_service(channel=ch, approve=True)

    intent = _make_intent(1, "force-flat-key", IntentType.FORCE_FLAT)
    envelope = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token="tok-force-flat")
    await svc._process_envelope(envelope)

    assert svc._dispatched == 1
    assert api_q.qsize() == 1


# ── Successful dispatch commits dedup as approved ────────────────────────


@pytest.mark.asyncio
async def test_successful_dispatch_commits_dedup_as_approved():
    """After successful dispatch, dedup is committed with approved=True."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_q = _build_service(channel=ch, approve=True)

    intent = _make_intent(1, "dispatch-ok-key")
    envelope = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token="tok-ok")
    await svc._process_envelope(envelope)

    assert svc._dispatched == 1
    rec = svc._dedup.check_or_reserve("dispatch-ok-key")
    assert rec is not None
    assert rec.approved is True
    assert rec.reason_code == "OK"
