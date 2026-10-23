"""One order gate, whatever path the order took (risk redesign B2).

``OrderAdapter.execute`` holds the dispatch guards: platform reduce-only, the
circuit breakers, the rate limits, and now ``HFT_ORDER_MODE``. With the gateway
on, ``GatewayService`` used to put commands straight onto ``_api_queue`` and
skip all of them. These tests run the same guard through both paths and
require the same outcome.

The risk-engine fast paths (FastGate, the Rust validator) are covered here too:
neither may block an exit, and a Rust approval may not skip a Python cap.
"""

from __future__ import annotations

import asyncio
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from hft_platform.contracts.strategy import (
    IntentType,
    OrderCommand,
    OrderIntent,
    RiskDecision,
    Side,
    StormGuardState,
)
from hft_platform.core import timebase
from hft_platform.core.rate_limiter import PerSymbolRateResult
from hft_platform.gateway.channel import IntentEnvelope, LocalIntentChannel
from hft_platform.gateway.dedup import IdempotencyStore
from hft_platform.gateway.exposure import ExposureStore
from hft_platform.gateway.policy import GatewayPolicy
from hft_platform.gateway.service import GatewayService
from hft_platform.order.deadletter import RejectionReason


def _intent(intent_type: IntentType = IntentType.NEW, *, side: Side = Side.BUY, key: str = "k1") -> OrderIntent:
    return OrderIntent(
        intent_id=7,
        strategy_id="s1",
        symbol="TMFK6",
        intent_type=intent_type,
        side=side,
        price=46_600 * 10_000,
        qty=1,
        idempotency_key=key,
    )


def _cmd(intent: OrderIntent) -> OrderCommand:
    now = timebase.now_ns()
    return OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=now + 10_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=now,
    )


@pytest.fixture(autouse=True)
def _adapter_deps():
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
        md.return_value = AsyncMock()
        yield


def _adapter(tmp_path, monkeypatch, *, gateway_on: bool, order_mode: str = "sim"):
    from hft_platform.order.adapter import OrderAdapter

    monkeypatch.setenv("HFT_GATEWAY_ENABLED", "1" if gateway_on else "0")
    monkeypatch.setenv("HFT_ORDER_MODE", order_mode)
    cfg = tmp_path / "order_config.yaml"
    cfg.write_text("rate_limits: {}\ncircuit_breaker: {}\n")
    client = MagicMock(spec=["place_order", "get_exchange", "cancel_order", "update_order"])
    oa = OrderAdapter(config_path=str(cfg), order_queue=asyncio.Queue(), broker_client=client)
    oa.per_symbol_rate_limiter = MagicMock()
    oa.per_symbol_rate_limiter.check.return_value = PerSymbolRateResult.OK
    oa.strategy_cb_mgr = MagicMock()
    oa.strategy_cb_mgr.is_open.return_value = False
    oa.circuit_breaker = MagicMock()
    oa.circuit_breaker.is_open.return_value = False
    oa.rate_limiter = MagicMock()
    oa.rate_limiter.check.return_value = True
    oa.shadow_sink = MagicMock()
    oa.shadow_sink.enabled = False
    oa.platform_degrade_controller = None
    oa._storm_guard = None
    oa._record_local_reject = MagicMock()
    # Accepted commands stay on ``_api_queue``: no worker drains it here.
    oa.running = True
    return oa


def _arm(oa, guard: str) -> None:
    if guard == "circuit_breaker":
        oa.circuit_breaker.is_open.return_value = True
    elif guard == "rate_limit":
        oa.rate_limiter.check.return_value = False
    elif guard == "platform_reduce_only":
        controller = MagicMock()
        controller.reduce_only_active = True
        controller.reference_available_net_qty.return_value = 0
        oa.platform_degrade_controller = controller
    elif guard == "order_mode_disabled":
        oa._order_mode = "disabled"
    elif guard == "order_mode_sim_on_real_session":
        oa.client.mode = "real"
    else:
        raise AssertionError(guard)


def _gateway(oa) -> GatewayService:
    intent = _intent()
    risk_engine = MagicMock()
    risk_engine.evaluate.return_value = RiskDecision(approved=True, intent=intent)
    risk_engine.create_command.side_effect = lambda i: _cmd(i)
    storm_guard = MagicMock()
    storm_guard.state = StormGuardState.NORMAL
    storm_guard.is_halt_exempt.return_value = False
    return GatewayService(
        channel=LocalIntentChannel(maxsize=8, ttl_ms=0),
        risk_engine=risk_engine,
        order_adapter=oa,
        exposure_store=ExposureStore(),
        dedup_store=IdempotencyStore(persist_enabled=False),
        storm_guard=storm_guard,
        policy=GatewayPolicy(storm_guard=storm_guard),
    )


async def _via_gateway(oa) -> None:
    svc = _gateway(oa)
    await svc._process_envelope(IntentEnvelope(intent=_intent(), enqueued_ns=0, ack_token="t"))


async def _via_risk_engine_path(oa) -> None:
    await oa.execute(_cmd(_intent()))


_GUARDS = [
    ("circuit_breaker", RejectionReason.CIRCUIT_BREAKER),
    ("rate_limit", RejectionReason.RATE_LIMIT),
    ("platform_reduce_only", RejectionReason.PLATFORM_REDUCE_ONLY),
    ("order_mode_disabled", RejectionReason.ORDER_MODE),
    ("order_mode_sim_on_real_session", RejectionReason.ORDER_MODE),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("gateway_on", [False, True], ids=["gateway_off", "gateway_on"])
@pytest.mark.parametrize(("guard", "reason"), _GUARDS, ids=[g for g, _ in _GUARDS])
async def test_order_gate_refuses_the_same_order_with_gateway_on_and_off(
    tmp_path, monkeypatch, gateway_on, guard, reason
):
    monkeypatch.setenv("HFT_GATEWAY_STARTUP_HOLDOFF_S", "0")
    oa = _adapter(tmp_path, monkeypatch, gateway_on=gateway_on)
    _arm(oa, guard)

    await (_via_gateway(oa) if gateway_on else _via_risk_engine_path(oa))

    assert oa._api_queue.qsize() == 0, "a refused order reached the broker queue"
    oa._record_local_reject.assert_called_once()
    assert oa._record_local_reject.call_args.args[1] == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("gateway_on", [False, True], ids=["gateway_off", "gateway_on"])
async def test_order_gate_passes_an_unguarded_order_on_both_paths(tmp_path, monkeypatch, gateway_on):
    monkeypatch.setenv("HFT_GATEWAY_STARTUP_HOLDOFF_S", "0")
    oa = _adapter(tmp_path, monkeypatch, gateway_on=gateway_on)

    await (_via_gateway(oa) if gateway_on else _via_risk_engine_path(oa))

    assert oa._api_queue.qsize() == 1
    oa._record_local_reject.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_refusal_releases_exposure_and_commits_dedup(tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_GATEWAY_STARTUP_HOLDOFF_S", "0")
    oa = _adapter(tmp_path, monkeypatch, gateway_on=True)
    _arm(oa, "circuit_breaker")
    svc = _gateway(oa)

    await svc._process_envelope(IntentEnvelope(intent=_intent(key="kx"), enqueued_ns=0, ack_token="t"))

    rec = svc._dedup.check_or_reserve("kx")
    assert rec is not None and rec.approved is False and rec.reason_code == "ORDER_GATE_REJECT"
    assert svc._exposure.get_exposure("default", "s1", "TMFK6") == 0
    assert svc._exposure.get_global_notional() == 0
    assert svc._dispatched == 0 and svc._rejected == 1


# ── HFT_ORDER_MODE at dispatch ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("intent_type", [IntentType.NEW, IntentType.FORCE_FLAT, IntentType.AMEND])
async def test_order_mode_disabled_refuses_every_order_but_a_cancel(tmp_path, monkeypatch, intent_type):
    oa = _adapter(tmp_path, monkeypatch, gateway_on=False, order_mode="disabled")

    accepted = await oa.execute(_cmd(_intent(intent_type)))

    assert accepted is False
    assert oa._api_queue.qsize() == 0
    assert oa._record_local_reject.call_args.args[1] == RejectionReason.ORDER_MODE


@pytest.mark.asyncio
async def test_order_mode_disabled_still_sends_a_cancel(tmp_path, monkeypatch):
    oa = _adapter(tmp_path, monkeypatch, gateway_on=False, order_mode="disabled")

    accepted = await oa.execute(_cmd(_intent(IntentType.CANCEL)))

    assert accepted is True
    assert oa._api_queue.qsize() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("order_mode", "session", "refused"),
    [
        ("sim", "real", True),
        ("sim", "simulation", False),
        ("live", "real", False),
        ("real", "real", False),
    ],
)
async def test_order_mode_sim_refuses_a_real_session(tmp_path, monkeypatch, order_mode, session, refused):
    oa = _adapter(tmp_path, monkeypatch, gateway_on=False, order_mode=order_mode)
    oa.client.mode = session

    accepted = await oa.execute(_cmd(_intent(IntentType.FORCE_FLAT, side=Side.SELL)))

    assert accepted is not refused
    assert oa._api_queue.qsize() == (0 if refused else 1)


@pytest.mark.asyncio
async def test_order_mode_is_rechecked_at_the_broker_boundary(tmp_path, monkeypatch):
    """The HALT drain calls ``_dispatch_to_api`` directly, past ``execute``."""
    oa = _adapter(tmp_path, monkeypatch, gateway_on=False, order_mode="disabled")

    sent = await oa._dispatch_to_api(_cmd(_intent(IntentType.FORCE_FLAT, side=Side.SELL)))

    assert sent is False
    oa.client.place_order.assert_not_called()
    assert oa._record_local_reject.call_args.args[1] == RejectionReason.ORDER_MODE


def test_shioaji_facade_reports_its_session_mode():
    from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

    facade = ShioajiClientFacade.__new__(ShioajiClientFacade)
    facade._client = MagicMock(mode="simulation")
    assert facade.mode == "simulation"


# ── Risk-engine fast paths ────────────────────────────────────────────────


def _engine(config: dict, position: int = 0):
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    yaml.dump(config, tmp)
    tmp.close()
    with (
        patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
        patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
        patch("hft_platform.recorder.audit.get_audit_writer"),
    ):
        mock_mr.get.return_value = MagicMock()
        mock_lr.get.return_value = MagicMock()
        from hft_platform.risk.engine import RiskEngine

        return RiskEngine(
            config_path=tmp.name,
            intent_queue=asyncio.Queue(),
            order_queue=asyncio.Queue(),
            position_provider=lambda _symbol, _sid=None: position,
        )


_CFG = {
    "global_defaults": {"max_price_cap": 100_000.0, "tick_size": 1.0, "price_band_ticks": 1_000_000},
    "strategies": {},
}


def test_fast_gate_kill_flag_does_not_block_a_force_flat():
    engine = _engine(_CFG, position=1)
    engine._fast_gate = MagicMock()
    engine._fast_gate.check.return_value = (False, 1)  # FASTGATE_KILL_SWITCH

    flat = engine.evaluate(_intent(IntentType.FORCE_FLAT, side=Side.SELL))
    opener = engine.evaluate(_intent(IntentType.NEW, side=Side.BUY))

    assert flat.approved, flat.reason_code
    assert not opener.approved and opener.reason_code == "FASTGATE_KILL_SWITCH"


def test_fast_gate_kill_flag_does_not_block_a_reducing_order():
    engine = _engine(_CFG, position=1)
    engine._fast_gate = MagicMock()
    engine._fast_gate.check.return_value = (False, 1)

    cover = engine.evaluate(_intent(IntentType.NEW, side=Side.SELL))

    assert cover.approved, cover.reason_code
    engine._fast_gate.check.assert_not_called()


def test_rust_validator_does_not_judge_a_reducing_order():
    engine = _engine(_CFG, position=1)
    engine._rust_validator = MagicMock()
    engine._rust_validator.check.return_value = (False, 4)  # MAX_NOTIONAL_EXCEEDED

    cover = engine.evaluate(_intent(IntentType.NEW, side=Side.SELL))
    opener = engine.evaluate(_intent(IntentType.NEW, side=Side.BUY))

    assert cover.approved, cover.reason_code
    assert not opener.approved and opener.reason_code == "MAX_NOTIONAL_EXCEEDED"


def test_rust_approval_does_not_skip_a_per_root_price_cap():
    cfg = {"global_defaults": {**_CFG["global_defaults"], "max_price_cap_root_TMF": 40_000.0}, "strategies": {}}
    engine = _engine(cfg)
    engine._rust_validator = MagicMock()
    engine._rust_validator.check.return_value = (True, 0)

    decision = engine.evaluate(_intent(IntentType.NEW, side=Side.BUY))  # 46,600 > the 40,000 TMF cap

    assert not decision.approved
    assert "CAP" in str(decision.reason_code).upper()
