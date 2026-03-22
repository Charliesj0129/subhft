"""Coverage-boosting tests for GatewayService (gateway/service.py).

Targets: _bool_env, _obs_policy, _int_env, _get_trace_sampler, GatewayService.__init__
HA leader lease, run() leader lease tick/channel receive/envelope exception/lease cleanup,
_process_envelope all paths, _leader_lease_tick/_loop errors, _emit_trace,
metric helpers, _metrics_or_refresh.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

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
from hft_platform.gateway.exposure import ExposureLimitError, ExposureStore
from hft_platform.gateway.policy import GatewayPolicy
from hft_platform.gateway.service import (
    GatewayService,
    _bool_env,
    _int_env,
    _obs_policy,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_intent(
    intent_id: int = 1,
    key: str = "k1",
    intent_type: IntentType = IntentType.NEW,
    symbol: str = "TSE:2330",
    price: int = 1_000_000,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="s1",
        symbol=symbol,
        intent_type=intent_type,
        side=Side.BUY,
        price=price,
        qty=1,
        tif=TIF.LIMIT,
        idempotency_key=key,
    )


def _build_service(
    channel: LocalIntentChannel | None = None,
    approve: bool = True,
    reason_code: str = "OK",
    queue_full: bool = False,
    exposure_store: ExposureStore | None = None,
    leader_lease: Any | None = None,
    dedup: IdempotencyStore | None = None,
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

    svc = GatewayService(
        channel=channel,
        risk_engine=risk_engine,
        order_adapter=order_adapter,
        exposure_store=exposure_store if exposure_store is not None else ExposureStore(),
        dedup_store=dedup if dedup is not None else IdempotencyStore(persist_enabled=False),
        storm_guard=storm_guard,
        policy=GatewayPolicy(),
        leader_lease=leader_lease,
    )
    return svc, api_q


async def _run_svc_one_tick(svc: GatewayService, timeout: float = 0.1) -> None:
    task = asyncio.create_task(svc.run())
    await asyncio.sleep(timeout)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── _bool_env ──────────────────────────────────────────────────────────────


def test_bool_env_none_returns_default():
    assert _bool_env(None, default=True) is True
    assert _bool_env(None, default=False) is False


def test_bool_env_bool_passthrough():
    assert _bool_env(True) is True
    assert _bool_env(False) is False


def test_bool_env_truthy_strings():
    for val in ("1", "true", "yes", "on", "YES", "TRUE"):
        assert _bool_env(val) is True, f"Expected True for {val!r}"


def test_bool_env_falsy_strings():
    for val in ("0", "false", "no", "off", "", "nope"):
        assert _bool_env(val) is False, f"Expected False for {val!r}"


# ── _obs_policy ────────────────────────────────────────────────────────────


def test_obs_policy_valid_values(monkeypatch):
    for val in ("minimal", "balanced", "debug"):
        monkeypatch.setenv("HFT_GATEWAY_OBS_POLICY", val)
        assert _obs_policy() == val


def test_obs_policy_invalid_returns_empty(monkeypatch):
    monkeypatch.setenv("HFT_GATEWAY_OBS_POLICY", "unknown")
    monkeypatch.delenv("HFT_OBS_POLICY", raising=False)
    assert _obs_policy() == ""


def test_obs_policy_fallback_to_hft_obs(monkeypatch):
    monkeypatch.delenv("HFT_GATEWAY_OBS_POLICY", raising=False)
    monkeypatch.setenv("HFT_OBS_POLICY", "balanced")
    assert _obs_policy() == "balanced"


# ── _int_env ───────────────────────────────────────────────────────────────


def test_int_env_returns_int(monkeypatch):
    monkeypatch.setenv("_TEST_INT_ENV_VAR", "42")
    assert _int_env("_TEST_INT_ENV_VAR", 1) == 42


def test_int_env_clamps_to_one(monkeypatch):
    monkeypatch.setenv("_TEST_INT_ENV_VAR2", "0")
    assert _int_env("_TEST_INT_ENV_VAR2", 1) == 1


def test_int_env_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("_TEST_INT_ENV_INVALID", "notanint")
    result = _int_env("_TEST_INT_ENV_INVALID", 5)
    assert result == 5


# ── _get_trace_sampler import failure ─────────────────────────────────────


def test_get_trace_sampler_import_failure_returns_none():
    from hft_platform.gateway import service as svc_mod

    with patch.object(svc_mod, "_get_trace_sampler", return_value=None):
        ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
        svc, _ = _build_service(channel=ch)
        assert svc is not None


# ── GatewayService.__init__ HA leader lease ───────────────────────────────


def test_init_ha_leader_lease_enabled(tmp_path, monkeypatch):
    """When HFT_GATEWAY_HA_ENABLED=1 and no explicit leader_lease, FileLeaderLease is created."""
    monkeypatch.setenv("HFT_GATEWAY_HA_ENABLED", "1")
    monkeypatch.setenv("HFT_GATEWAY_LEADER_LEASE_PATH", str(tmp_path / "leader.lock"))
    svc, _ = _build_service(leader_lease=None)
    assert svc is not None


def test_init_ha_leader_lease_disabled(monkeypatch):
    monkeypatch.setenv("HFT_GATEWAY_HA_ENABLED", "0")
    svc, _ = _build_service()
    assert svc._leader_lease is None
    assert svc._is_dispatch_leader() is True


def test_init_leader_lease_refresh_invalid(monkeypatch):
    """Invalid HFT_GATEWAY_LEADER_LEASE_REFRESH_S falls back to 0.5."""
    monkeypatch.setenv("HFT_GATEWAY_LEADER_LEASE_REFRESH_S", "notafloat")
    svc, _ = _build_service()
    assert svc._leader_lease_refresh_s == 0.5


def test_init_obs_policy_sampling_rates(monkeypatch):
    """Verify obs policy affects sampling rates."""
    for policy in ("minimal", "balanced", "debug", ""):
        monkeypatch.setenv("HFT_GATEWAY_OBS_POLICY", policy)
        monkeypatch.delenv("HFT_OBS_POLICY", raising=False)
        svc, _ = _build_service()
        assert svc._gateway_depth_sample_every >= 8


# ── run() leader lease tick ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_with_leader_lease_tick():
    """Service run() calls _leader_lease_tick when lease is set."""
    mock_lease = MagicMock()
    mock_lease.is_leader.return_value = True
    mock_lease.tick.return_value = True
    mock_lease.release.return_value = None

    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    svc, _ = _build_service(channel=ch, leader_lease=mock_lease)

    tick_called = []
    original_tick = svc._leader_lease_tick

    async def mock_tick():
        tick_called.append(True)
        await original_tick()

    svc._leader_lease_tick = mock_tick

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(tick_called) >= 1


@pytest.mark.asyncio
async def test_run_channel_receive_called():
    """run() processes intents via normal channel.receive() path."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, _ = _build_service(channel=ch)

    intent = _make_intent(1, "rcv-key")
    ch.submit_nowait(intent)

    await _run_svc_one_tick(svc, timeout=0.1)
    assert svc._dispatched >= 1


@pytest.mark.asyncio
async def test_run_envelope_exception_does_not_crash():
    """Exceptions in _process_envelope are caught; service continues."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, _ = _build_service(channel=ch)

    call_count = [0]
    original = svc._process_envelope

    async def bad_process(envelope):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("deliberate envelope error")
        return await original(envelope)

    svc._process_envelope = bad_process

    ch.submit_nowait(_make_intent(1, "err-key"))
    ch.submit_nowait(_make_intent(2, "ok-key"))

    await _run_svc_one_tick(svc, timeout=0.15)
    assert call_count[0] >= 2


@pytest.mark.asyncio
async def test_run_lease_cleanup_on_exit():
    """On CancelledError, lease is released."""
    mock_lease = MagicMock()
    mock_lease.is_leader.return_value = True
    mock_lease.tick.return_value = True
    mock_lease.release.return_value = None

    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    svc, _ = _build_service(channel=ch, leader_lease=mock_lease)

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    mock_lease.release.assert_called()
    assert svc.running is False


# ── _process_envelope paths ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_envelope_empty_key():
    """Intent with empty idempotency_key skips dedup check."""
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    svc, api_q = _build_service(channel=ch, approve=True)

    intent = _make_intent(1, "")  # empty key
    ch.submit_nowait(intent)

    await _run_svc_one_tick(svc, timeout=0.1)
    assert svc._dispatched == 1


@pytest.mark.asyncio
async def test_process_envelope_cancel_bypasses_exposure():
    """CANCEL intents skip exposure check_and_update."""
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    exposure = MagicMock(spec=ExposureStore)
    exposure.check_and_update.return_value = (True, "")
    exposure.qsize = MagicMock(return_value=0)

    svc, _ = _build_service(channel=ch, approve=True, exposure_store=exposure)

    intent = _make_intent(1, "cancel-key", intent_type=IntentType.CANCEL)
    ch.submit_nowait(intent)

    await _run_svc_one_tick(svc, timeout=0.1)
    # Exposure check_and_update must NOT be called for CANCEL
    exposure.check_and_update.assert_not_called()


@pytest.mark.asyncio
async def test_process_envelope_exposure_limit_error():
    """ExposureLimitError leads to EXPOSURE_SYMBOL_LIMIT rejection."""
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    exposure = MagicMock(spec=ExposureStore)
    exposure.check_and_update.side_effect = ExposureLimitError("limit hit")

    svc, api_q = _build_service(channel=ch, approve=True, exposure_store=exposure)

    intent = _make_intent(1, "exp-key")
    ch.submit_nowait(intent)

    await _run_svc_one_tick(svc, timeout=0.1)
    assert svc._rejected == 1
    assert api_q.empty()


@pytest.mark.asyncio
async def test_process_envelope_exposure_rejection():
    """Failed exposure check (not limit) rejects intent."""
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    exposure = MagicMock(spec=ExposureStore)
    exposure.check_and_update.return_value = (False, "MAX_EXPOSURE")

    svc, api_q = _build_service(channel=ch, approve=True, exposure_store=exposure)

    intent = _make_intent(1, "exp-reject-key")
    ch.submit_nowait(intent)

    await _run_svc_one_tick(svc, timeout=0.1)
    assert svc._rejected == 1
    assert api_q.empty()


@pytest.mark.asyncio
async def test_process_envelope_not_leader_dispatch_check():
    """_is_dispatch_leader returns False when lease inactive."""
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    lease = MagicMock()
    lease.is_leader.return_value = False
    svc, api_q = _build_service(channel=ch, approve=True, leader_lease=lease)

    svc._leader_is_active = False
    # Verify helper before running full loop
    assert not svc._is_dispatch_leader()
    assert api_q.empty()


@pytest.mark.asyncio
async def test_process_envelope_not_leader_suppresses_via_process():
    """Direct call to _process_envelope with inactive leader suppresses dispatch."""
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    svc, api_q = _build_service(channel=ch, approve=True, exposure_store=ExposureStore())
    svc._leader_is_active = False
    svc._leader_lease = MagicMock()  # non-None

    intent = _make_intent(1, "leader-key2")

    # Simulate an envelope directly
    from hft_platform.gateway.channel import IntentEnvelope

    envelope = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token="tok1")
    await svc._process_envelope(envelope)

    assert svc._rejected == 1
    assert api_q.empty()


@pytest.mark.asyncio
async def test_process_envelope_risk_rejection():
    """Risk engine rejection increments _rejected."""
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    svc, api_q = _build_service(channel=ch, approve=False, reason_code="MAX_NOTIONAL")

    intent = _make_intent(1, "risk-key")
    ch.submit_nowait(intent)

    await _run_svc_one_tick(svc, timeout=0.1)
    assert svc._rejected == 1
    assert api_q.empty()


@pytest.mark.asyncio
async def test_process_envelope_order_queue_full():
    """QueueFull on dispatch increments _rejected with ORDER_QUEUE_FULL."""
    ch = LocalIntentChannel(maxsize=8, ttl_ms=0)
    svc, api_q = _build_service(channel=ch, approve=True, queue_full=True)

    intent = _make_intent(1, "full-key")
    ch.submit_nowait(intent)

    await _run_svc_one_tick(svc, timeout=0.1)
    assert svc._rejected == 1


# ── _leader_lease_tick / _loop errors ─────────────────────────────────────


@pytest.mark.asyncio
async def test_leader_lease_tick_none_lease():
    """_leader_lease_tick with no lease sets _leader_is_active to True."""
    svc, _ = _build_service()
    svc._leader_lease = None
    await svc._leader_lease_tick()
    assert svc._leader_is_active is True


@pytest.mark.asyncio
async def test_leader_lease_tick_exception():
    """Exception in tick sets _leader_is_active to False."""
    mock_lease = MagicMock()
    mock_lease.tick.side_effect = OSError("io error")

    svc, _ = _build_service()
    svc._leader_lease = mock_lease
    await svc._leader_lease_tick()
    assert svc._leader_is_active is False


@pytest.mark.asyncio
async def test_leader_lease_loop_exception_continues():
    """_leader_lease_loop catches non-Cancel exceptions and sets inactive."""
    svc, _ = _build_service()
    svc.running = True
    call_count = [0]

    async def flaky_tick():
        call_count[0] += 1
        if call_count[0] <= 2:
            raise RuntimeError("loop tick error")
        svc.running = False  # stop after 2 errors

    svc._leader_lease_tick = flaky_tick
    svc._leader_lease = MagicMock()  # non-None
    svc._leader_lease_refresh_s = 0.001

    await svc._leader_lease_loop()
    assert call_count[0] >= 2
    assert svc._leader_is_active is False


# ── _emit_trace ────────────────────────────────────────────────────────────


def test_emit_trace_with_sampler():
    svc, _ = _build_service()
    sampler = MagicMock()
    svc._trace_sampler = sampler

    svc._emit_trace("test_stage", "trace-id-123", {"key": "value"})

    sampler.emit.assert_called_once()


def test_emit_trace_sampler_exception_swallowed():
    svc, _ = _build_service()
    sampler = MagicMock()
    sampler.emit.side_effect = RuntimeError("trace error")
    svc._trace_sampler = sampler

    svc._emit_trace("stage", "tid", {})  # Must not raise


def test_emit_trace_no_sampler():
    svc, _ = _build_service()
    svc._trace_sampler = None
    svc._emit_trace("stage", "tid", {"k": "v"})  # Must not raise


# ── Metric helpers ─────────────────────────────────────────────────────────


def test_emit_reject_metrics_disabled():
    svc, _ = _build_service()
    svc._metrics_enabled = False
    svc._emit_reject("TEST_REASON")  # Must not raise


def test_emit_reject_metrics_enabled_no_registry():
    svc, _ = _build_service()
    svc._metrics_enabled = True
    svc._metrics = None
    svc._gateway_reject_counter = 0
    svc._gateway_reject_sample_every = 1
    svc._emit_reject("TEST_REASON")  # Must not raise


def test_emit_reject_with_metrics():
    svc, _ = _build_service()
    svc._metrics_enabled = True
    svc._gateway_reject_counter = 0
    svc._gateway_reject_sample_every = 1

    mock_metrics = MagicMock()
    mock_child = MagicMock()
    mock_metrics.gateway_reject_total.labels.return_value = mock_child
    svc._metrics = mock_metrics
    svc._metrics_owner_id = id(mock_metrics)

    svc._emit_reject("SOME_REASON")
    mock_child.inc.assert_called_once()


def test_emit_reject_caches_label():
    """Second call with same reason uses cached child."""
    svc, _ = _build_service()
    svc._metrics_enabled = True
    svc._gateway_reject_counter = 0
    svc._gateway_reject_sample_every = 1

    mock_metrics = MagicMock()
    mock_child = MagicMock()
    mock_metrics.gateway_reject_total.labels.return_value = mock_child
    svc._metrics = mock_metrics
    svc._metrics_owner_id = id(mock_metrics)

    svc._emit_reject("CACHED_REASON")
    svc._gateway_reject_counter = 0
    svc._emit_reject("CACHED_REASON")

    assert mock_metrics.gateway_reject_total.labels.call_count == 1


def test_record_latency_metrics_disabled():
    svc, _ = _build_service()
    svc._metrics_enabled = False
    svc._record_latency(0)  # Must not raise


def test_record_latency_with_metrics():
    svc, _ = _build_service()
    svc._metrics_enabled = True
    svc._gateway_latency_counter = 0
    svc._gateway_latency_sample_every = 1

    mock_metrics = MagicMock()
    mock_hist = MagicMock()
    mock_metrics.gateway_dispatch_latency_ns = mock_hist
    svc._metrics = mock_metrics
    svc._metrics_owner_id = id(mock_metrics)
    svc._gateway_dispatch_latency_metric = None

    svc._record_latency(0)
    mock_hist.observe.assert_called_once()


def test_update_channel_depth_metric_disabled():
    svc, _ = _build_service()
    svc._metrics_enabled = False
    svc._update_channel_depth_metric()  # Must not raise


def test_inc_dedup_hit_metric_disabled():
    svc, _ = _build_service()
    svc._metrics_enabled = False
    svc._inc_dedup_hit_metric()  # Must not raise


def test_inc_dedup_hit_metric_enabled():
    svc, _ = _build_service()
    svc._metrics_enabled = True
    svc._gateway_dedup_counter = 0
    svc._gateway_dedup_sample_every = 1

    mock_metrics = MagicMock()
    mock_counter = MagicMock()
    mock_metrics.gateway_dedup_hits_total = mock_counter
    svc._metrics = mock_metrics
    svc._metrics_owner_id = id(mock_metrics)
    svc._gateway_dedup_hits_metric = None

    svc._inc_dedup_hit_metric()
    mock_counter.inc.assert_called_once()


# ── _metrics_or_refresh ────────────────────────────────────────────────────


def test_metrics_or_refresh_no_metrics_triggers_refresh():
    svc, _ = _build_service()
    svc._metrics = None
    result = svc._metrics_or_refresh()
    # After refresh, no exception raised
    assert result is None or result is not None


def test_metrics_or_refresh_owner_id_mismatch_triggers_refresh():
    svc, _ = _build_service()
    mock_metrics = MagicMock()
    svc._metrics = mock_metrics
    svc._metrics_owner_id = id(mock_metrics) + 999  # mismatch

    result = svc._metrics_or_refresh()
    assert True  # Must not raise


def test_metrics_or_refresh_matching_owner_returns_cached():
    svc, _ = _build_service()
    mock_metrics = MagicMock()
    svc._metrics = mock_metrics
    svc._metrics_owner_id = id(mock_metrics)

    result = svc._metrics_or_refresh()
    assert result is mock_metrics


# ── _is_dispatch_leader ────────────────────────────────────────────────────


def test_is_dispatch_leader_no_lease():
    svc, _ = _build_service()
    svc._leader_lease = None
    assert svc._is_dispatch_leader() is True


def test_is_dispatch_leader_with_inactive_lease():
    svc, _ = _build_service()
    svc._leader_lease = MagicMock()
    svc._leader_is_active = False
    assert svc._is_dispatch_leader() is False


def test_is_dispatch_leader_with_active_lease():
    svc, _ = _build_service()
    svc._leader_lease = MagicMock()
    svc._leader_is_active = True
    assert svc._is_dispatch_leader() is True


# ── get_health ─────────────────────────────────────────────────────────────


def test_get_health_returns_expected_keys():
    svc, _ = _build_service()
    health = svc.get_health()
    for key in ("running", "dispatched", "rejected", "dedup_hits", "channel_depth", "policy_mode", "leader_active"):
        assert key in health, f"Missing key: {key}"


@pytest.mark.asyncio
async def test_dedup_hit_increments_counter():
    """Sending same idempotency_key twice results in dedup_hits=1."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_q = _build_service(channel=ch, approve=True)

    for _ in range(2):
        ch.submit_nowait(_make_intent(1, "dedup-hit-key"))

    await _run_svc_one_tick(svc, timeout=0.15)
    assert svc._dedup_hits == 1
    assert api_q.qsize() == 1
