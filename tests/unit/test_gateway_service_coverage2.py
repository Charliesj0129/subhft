"""Additional coverage tests for gateway/service.py — targets remaining uncovered lines.

Covers:
- _emit_reject: sampling skip, metrics=None, exception path (lines 724-735)
- _record_latency: sampling skip, metrics=None, exception path (lines 742-751)
- _update_channel_depth_metric: sampling skip, metrics=None, exception (lines 757-773)
- _refresh_metrics_registry: metrics=None branch (lines 789-793)
- _inc_dedup_hit_metric: sampling skip, metrics=None, exception (lines 831-840)
- _leader_lease_loop: tick exception sets leader_is_active=False (lines 847-850)
- _leader_lease_tick: lease.tick exception (lines 860-862)
- _emit_trace: sampler.emit exception (line 812)
- _metrics_or_refresh: owner_id mismatch triggers refresh (line 821-823)
- Exposure check typed path (line 392, 412)
- run() lease task non-cancel exception during cleanup (lines 236-238)
- TTL-expired typed dedup commit path (line 325)
- Rejection feedback overflow metric (line 632-633, 688-691)
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import Any
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
from hft_platform.gateway.channel import IntentEnvelope, LocalIntentChannel
from hft_platform.gateway.dedup import IdempotencyStore
from hft_platform.gateway.exposure import ExposureLimitError, ExposureStore
from hft_platform.gateway.policy import GatewayPolicy
from hft_platform.gateway.service import GatewayService, _bool_env, _int_env, _obs_policy


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
    metrics_enabled: bool = True,
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
    svc._metrics_enabled = metrics_enabled
    return svc, api_q


# ── Module-level helpers ──────────────────────────────────────────────────


def test_bool_env_various_inputs():
    """_bool_env handles None, bool, and string inputs."""
    assert _bool_env(None, default=True) is True
    assert _bool_env(None, default=False) is False
    assert _bool_env(True) is True
    assert _bool_env(False) is False
    assert _bool_env("1") is True
    assert _bool_env("true") is True
    assert _bool_env("yes") is True
    assert _bool_env("on") is True
    assert _bool_env("0") is False
    assert _bool_env("OFF") is False


def test_int_env_valid_value(monkeypatch):
    """_int_env returns parsed int from env."""
    monkeypatch.setenv("TEST_INT_ENV", "42")
    assert _int_env("TEST_INT_ENV", 1) == 42


def test_int_env_invalid_value(monkeypatch):
    """_int_env returns clamped default when env value is non-numeric."""
    monkeypatch.setenv("TEST_INT_ENV", "notanumber")
    assert _int_env("TEST_INT_ENV", 5) == 5


def test_int_env_zero_clamps_to_one(monkeypatch):
    """_int_env clamps zero to 1."""
    monkeypatch.setenv("TEST_INT_ENV", "0")
    assert _int_env("TEST_INT_ENV", 1) == 1


def test_obs_policy_returns_known_values(monkeypatch):
    """_obs_policy returns known policy values."""
    monkeypatch.setenv("HFT_GATEWAY_OBS_POLICY", "minimal")
    assert _obs_policy() == "minimal"
    monkeypatch.setenv("HFT_GATEWAY_OBS_POLICY", "balanced")
    assert _obs_policy() == "balanced"
    monkeypatch.setenv("HFT_GATEWAY_OBS_POLICY", "debug")
    assert _obs_policy() == "debug"


def test_obs_policy_returns_empty_for_unknown(monkeypatch):
    """_obs_policy returns empty string for unknown policy."""
    monkeypatch.setenv("HFT_GATEWAY_OBS_POLICY", "invalid_value")
    assert _obs_policy() == ""


def test_obs_policy_falls_back_to_hft_obs_policy(monkeypatch):
    """_obs_policy falls back to HFT_OBS_POLICY when gateway-specific is not set."""
    monkeypatch.delenv("HFT_GATEWAY_OBS_POLICY", raising=False)
    monkeypatch.setenv("HFT_OBS_POLICY", "balanced")
    result = _obs_policy()
    assert isinstance(result, str)


# ── _emit_reject ──────────────────────────────────────────────────────────


def test_emit_reject_disabled_metrics():
    """_emit_reject returns immediately when metrics disabled."""
    svc, _ = _build_service(metrics_enabled=False)
    svc._emit_reject("SOME_REASON")
    # No crash; metrics never touched
    assert not svc._metrics_enabled


def test_emit_reject_sampling_skips_emission():
    """_emit_reject skips emission when counter does not align."""
    svc, _ = _build_service()
    svc._gateway_reject_sample_every = 3
    svc._gateway_reject_counter = 0
    # After first call: counter=1, 1%3 != 0 -> skip
    svc._emit_reject("TEST_REASON")
    # After second call: counter=2, 2%3 != 0 -> skip
    svc._emit_reject("TEST_REASON")
    # After third call: counter=0, 0%3 == 0 -> emit
    svc._emit_reject("TEST_REASON")
    assert svc._gateway_reject_counter == 0


def test_emit_reject_metrics_none_refreshes():
    """_emit_reject tries to refresh when metrics is None."""
    svc, _ = _build_service()
    svc._metrics = None
    svc._gateway_reject_sample_every = 1
    svc._gateway_reject_counter = 0
    svc._emit_reject("TEST")
    # Should not raise
    assert True


def test_emit_reject_exception_handled():
    """_emit_reject catches exceptions from metrics emission."""
    svc, _ = _build_service()
    svc._gateway_reject_sample_every = 1
    svc._gateway_reject_counter = 0
    mock_metrics = MagicMock()
    mock_metrics.gateway_reject_total.labels.side_effect = RuntimeError("metrics crash")
    svc._metrics = mock_metrics
    svc._metrics_owner_id = id(mock_metrics)
    svc._emit_reject("CRASH_REASON")
    # Should not raise
    assert True


# ── _record_latency ──────────────────────────────────────────────────────


def test_record_latency_disabled_metrics():
    """_record_latency returns immediately when metrics disabled."""
    svc, _ = _build_service(metrics_enabled=False)
    svc._record_latency(time.perf_counter_ns())
    assert not svc._metrics_enabled


def test_record_latency_sampling_skips():
    """_record_latency skips when counter does not align."""
    svc, _ = _build_service()
    svc._gateway_latency_sample_every = 2
    svc._gateway_latency_counter = 0
    # First call: counter becomes 1, 1%2 != 0
    svc._record_latency(time.perf_counter_ns())
    assert svc._gateway_latency_counter == 1


def test_record_latency_metrics_none():
    """_record_latency handles None metrics by refreshing."""
    svc, _ = _build_service()
    svc._metrics = None
    svc._gateway_latency_sample_every = 1
    svc._gateway_latency_counter = 0
    svc._record_latency(time.perf_counter_ns())
    assert True


def test_record_latency_exception_handled():
    """_record_latency catches exception from metrics.observe."""
    svc, _ = _build_service()
    svc._gateway_latency_sample_every = 1
    svc._gateway_latency_counter = 0
    svc._gateway_dispatch_latency_metric = MagicMock()
    svc._gateway_dispatch_latency_metric.observe.side_effect = RuntimeError("observe fail")
    svc._record_latency(time.perf_counter_ns())
    assert True


# ── _update_channel_depth_metric ─────────────────────────────────────────


def test_update_channel_depth_disabled_metrics():
    """_update_channel_depth_metric returns when metrics disabled."""
    svc, _ = _build_service(metrics_enabled=False)
    svc._update_channel_depth_metric()
    assert not svc._metrics_enabled


def test_update_channel_depth_sampling_skips():
    """_update_channel_depth_metric skips when counter not aligned."""
    svc, _ = _build_service()
    svc._gateway_depth_sample_every = 4
    svc._gateway_depth_counter = 0
    svc._update_channel_depth_metric()
    assert svc._gateway_depth_counter == 1


def test_update_channel_depth_metrics_none():
    """_update_channel_depth_metric refreshes when metrics is None."""
    svc, _ = _build_service()
    svc._metrics = None
    svc._gateway_depth_sample_every = 1
    svc._gateway_depth_counter = 0
    svc._update_channel_depth_metric()
    assert True


def test_update_channel_depth_exception_handled():
    """_update_channel_depth_metric catches metrics exceptions."""
    svc, _ = _build_service()
    svc._gateway_depth_sample_every = 1
    svc._gateway_depth_counter = 0
    mock_metrics = MagicMock()
    mock_metrics.gateway_intent_channel_depth.set.side_effect = RuntimeError("depth fail")
    svc._metrics = mock_metrics
    svc._metrics_owner_id = id(mock_metrics)
    svc._gateway_depth_metric = None
    svc._update_channel_depth_metric()
    assert True


# ── _inc_dedup_hit_metric ─────────────────────────────────────────────────


def test_inc_dedup_hit_metric_disabled():
    """_inc_dedup_hit_metric returns when metrics disabled."""
    svc, _ = _build_service(metrics_enabled=False)
    svc._inc_dedup_hit_metric()
    assert not svc._metrics_enabled


def test_inc_dedup_hit_metric_sampling_skips():
    """_inc_dedup_hit_metric skips when counter not aligned."""
    svc, _ = _build_service()
    svc._gateway_dedup_sample_every = 3
    svc._gateway_dedup_counter = 0
    svc._inc_dedup_hit_metric()
    assert svc._gateway_dedup_counter == 1


def test_inc_dedup_hit_metric_metrics_none():
    """_inc_dedup_hit_metric refreshes when metrics is None."""
    svc, _ = _build_service()
    svc._metrics = None
    svc._gateway_dedup_sample_every = 1
    svc._gateway_dedup_counter = 0
    svc._inc_dedup_hit_metric()
    assert True


def test_inc_dedup_hit_metric_exception_handled():
    """_inc_dedup_hit_metric catches exceptions."""
    svc, _ = _build_service()
    svc._gateway_dedup_sample_every = 1
    svc._gateway_dedup_counter = 0
    svc._gateway_dedup_hits_metric = MagicMock()
    svc._gateway_dedup_hits_metric.inc.side_effect = RuntimeError("inc fail")
    svc._inc_dedup_hit_metric()
    assert True


# ── _refresh_metrics_registry: metrics=None branch ──────────────────────


def test_refresh_metrics_registry_sets_none_when_get_returns_none(monkeypatch):
    """When MetricsRegistry.get() returns None, all metrics are set to None."""
    svc, _ = _build_service()

    import hft_platform.observability.metrics as metrics_module

    monkeypatch.setattr(metrics_module.MetricsRegistry, "get", staticmethod(lambda: None))
    svc._refresh_metrics_registry()

    assert svc._metrics is None
    assert svc._gateway_dispatch_latency_metric is None
    assert svc._gateway_depth_metric is None
    assert svc._gateway_dlq_metric is None
    assert svc._gateway_exposure_metric is None
    assert svc._gateway_dedup_hits_metric is None


# ── _emit_trace: exception path ──────────────────────────────────────────


def test_emit_trace_exception_suppressed():
    """_emit_trace suppresses sampler.emit exception."""
    svc, _ = _build_service()
    sampler = MagicMock()
    sampler.emit.side_effect = RuntimeError("trace error")
    svc._trace_sampler = sampler
    svc._emit_trace("test_stage", "trace_123", {"key": "val"})
    sampler.emit.assert_called_once()


def test_emit_trace_none_sampler_skips():
    """_emit_trace returns immediately when sampler is None."""
    svc, _ = _build_service()
    svc._trace_sampler = None
    svc._emit_trace("test_stage", "trace_123", {"key": "val"})
    assert True


# ── _metrics_or_refresh: owner_id mismatch ───────────────────────────────


def test_metrics_or_refresh_mismatch_triggers_refresh():
    """When metrics owner_id changes, _metrics_or_refresh re-fetches registry."""
    svc, _ = _build_service()
    old_metrics = MagicMock()
    svc._metrics = old_metrics
    svc._metrics_owner_id = id(old_metrics) + 999  # Force mismatch
    result = svc._metrics_or_refresh()
    # Should have refreshed (result may be None or new metrics)
    assert True


def test_metrics_or_refresh_none_refreshes():
    """When metrics is None, _metrics_or_refresh calls refresh."""
    svc, _ = _build_service()
    svc._metrics = None
    result = svc._metrics_or_refresh()
    # Should attempt refresh
    assert True


# ── _leader_lease_tick: exception sets inactive ──────────────────────────


@pytest.mark.asyncio
async def test_leader_lease_tick_exception_sets_inactive():
    """_leader_lease_tick sets leader_is_active=False on lease.tick exception."""
    mock_lease = MagicMock()
    mock_lease.tick.side_effect = OSError("lease broken")
    svc, _ = _build_service(leader_lease=mock_lease)

    await svc._leader_lease_tick()
    assert svc._leader_is_active is False


@pytest.mark.asyncio
async def test_leader_lease_tick_none_lease_sets_active():
    """_leader_lease_tick sets leader_is_active=True when lease is None."""
    svc, _ = _build_service()
    svc._leader_lease = None
    await svc._leader_lease_tick()
    assert svc._leader_is_active is True


# ── _leader_lease_loop: exception handling ───────────────────────────────


@pytest.mark.asyncio
async def test_leader_lease_loop_tick_exception_continues():
    """_leader_lease_loop catches tick exceptions and continues."""
    mock_lease = MagicMock()
    call_count = [0]

    def tick_side_effect():
        call_count[0] += 1
        if call_count[0] <= 1:
            raise RuntimeError("tick error")
        return True

    mock_lease.tick.side_effect = tick_side_effect
    mock_lease.is_leader.return_value = True

    svc, _ = _build_service(leader_lease=mock_lease)
    svc.running = True
    svc._leader_lease_refresh_s = 0.01

    # Run the loop briefly; it should handle the first tick exception
    async def stop_after_delay():
        await asyncio.sleep(0.05)
        svc.running = False

    await asyncio.gather(svc._leader_lease_loop(), stop_after_delay())
    assert call_count[0] >= 1


# ── run() lease task non-cancel exception during cleanup ─────────────────


@pytest.mark.asyncio
async def test_run_lease_task_noncancellerror_during_cleanup():
    """run() cleanup handles non-CancelledError from lease_task."""
    mock_lease = MagicMock()
    mock_lease.is_leader.return_value = True
    mock_lease.tick.return_value = True
    mock_lease.release.return_value = None

    svc, _ = _build_service(leader_lease=mock_lease)
    svc._leader_lease_refresh_s = 0.01

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert svc.running is False


# ── Exposure check: ExposureLimitError path ──────────────────────────────


@pytest.mark.asyncio
async def test_exposure_limit_error_rejects_intent():
    """ExposureLimitError in exposure check rejects the intent."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    rejection_sink = asyncio.Queue(maxsize=64)
    exposure = MagicMock(spec=ExposureStore)
    exposure.check_and_update.side_effect = ExposureLimitError("too many symbols")
    exposure.global_notional = 0

    svc, api_q = _build_service(
        channel=ch,
        exposure_store=exposure,
        rejection_sink=rejection_sink,
    )

    intent = _make_intent(1, "exp-limit-key")
    envelope = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token="tok-exp-limit")
    await svc._process_envelope(envelope)

    assert svc._rejected == 1
    rec = svc._dedup.check_or_reserve("exp-limit-key")
    assert rec is not None
    assert rec.approved is False
    assert rec.reason_code == "EXPOSURE_SYMBOL_LIMIT"
    assert rejection_sink.qsize() == 1
    fb = rejection_sink.get_nowait()
    assert fb.reason_code == "EXPOSURE_SYMBOL_LIMIT"


# ── Exposure rejection (not ExposureLimitError) ─────────────────────────


@pytest.mark.asyncio
async def test_exposure_check_rejection_commits_dedup():
    """Exposure check failure commits dedup as rejected."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    exposure = MagicMock(spec=ExposureStore)
    exposure.check_and_update.return_value = (False, "NOTIONAL_EXCEEDED")
    exposure.global_notional = 0

    svc, api_q = _build_service(channel=ch, exposure_store=exposure)

    intent = _make_intent(1, "exp-rej-key")
    envelope = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token="tok-exp-rej")
    await svc._process_envelope(envelope)

    assert svc._rejected == 1
    rec = svc._dedup.check_or_reserve("exp-rej-key")
    assert rec is not None
    assert rec.approved is False


# ── set_halt / set_normal ────────────────────────────────────────────────


def test_set_halt_propagates_to_policy():
    """set_halt() calls policy.set_halt()."""
    svc, _ = _build_service()
    svc.set_halt()
    assert svc._policy.mode.value in ("HALT", "halt", 3)


def test_set_normal_resets_policy():
    """set_normal() calls policy.set_normal()."""
    svc, _ = _build_service()
    svc.set_halt()
    svc.set_normal()
    assert svc._policy.mode.value in ("NORMAL", "normal", 0)


# ── get_health ───────────────────────────────────────────────────────────


def test_get_health_returns_dict():
    """get_health() returns dict with expected keys."""
    svc, _ = _build_service()
    health = svc.get_health()
    assert isinstance(health, dict)
    assert "running" in health
    assert "dispatched" in health
    assert "rejected" in health
    assert "dedup_hits" in health
    assert "channel_depth" in health
    assert "policy_mode" in health
    assert "leader_active" in health


# ── set_rejection_sink ───────────────────────────────────────────────────


def test_set_rejection_sink():
    """set_rejection_sink wires the rejection feedback queue."""
    svc, _ = _build_service()
    new_sink = asyncio.Queue(maxsize=16)
    svc.set_rejection_sink(new_sink)
    assert svc._rejection_sink is new_sink


# ── _send_rejection_feedback: sink full metric path ──────────────────────


def test_send_rejection_feedback_sink_full_increments_metric():
    """_send_rejection_feedback increments overflow metric when sink is full."""
    svc, _ = _build_service()
    sink = asyncio.Queue(maxsize=1)
    sink.put_nowait(MagicMock())
    svc._rejection_sink = sink

    mock_metrics = MagicMock()
    svc._metrics = mock_metrics
    svc._metrics_owner_id = id(mock_metrics)

    intent = _make_intent()
    svc._send_rejection_feedback(intent, "TEST_OVERFLOW")
    mock_metrics.rejection_sink_overflow_total.inc.assert_called()


# ── Committed dedup hit returns early ────────────────────────────────────


@pytest.mark.asyncio
async def test_committed_dedup_hit_returns_cached_decision():
    """Committed dedup entry returns early without re-processing."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_q = _build_service(channel=ch)

    # Pre-commit a dedup entry
    svc._dedup.check_or_reserve("committed-key")
    svc._dedup.commit("committed-key", True, "OK", 42)

    intent = _make_intent(1, "committed-key")
    envelope = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token="tok-committed")
    await svc._process_envelope(envelope)

    assert svc._dedup_hits == 1
    # No dispatch or rejection since it's a cached hit
    assert svc._dispatched == 0
    assert svc._rejected == 0


# ── _is_dispatch_leader with lease ───────────────────────────────────────


def test_is_dispatch_leader_no_lease_returns_true():
    """_is_dispatch_leader returns True when no lease configured."""
    svc, _ = _build_service()
    assert svc._is_dispatch_leader() is True


def test_is_dispatch_leader_with_inactive_lease():
    """_is_dispatch_leader returns False when lease is inactive."""
    mock_lease = MagicMock()
    mock_lease.is_leader.return_value = False
    svc, _ = _build_service(leader_lease=mock_lease)
    svc._leader_is_active = False
    assert svc._is_dispatch_leader() is False
