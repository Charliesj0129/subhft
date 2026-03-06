from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hft_platform.risk.storm_guard import StormGuardState
from hft_platform.services.system import HFTSystem


class _Runner:
    def __init__(self) -> None:
        self.running = True

    async def run(self) -> None:
        await asyncio.sleep(0)


class _OrderClient:
    def set_execution_callbacks(self, on_order, on_deal) -> None:
        return None


class _ExecutionGateway(_Runner):
    def stop(self) -> None:
        return None


class _StormGuard:
    def __init__(self) -> None:
        self.state = StormGuardState.NORMAL

    def update(self, **kwargs) -> None:
        return None

    def trigger_halt(self, reason: str) -> None:
        self.state = StormGuardState.HALT


def _registry(gateway_service=None):
    q = asyncio.Queue()
    return SimpleNamespace(
        bus=SimpleNamespace(),
        raw_queue=q,
        raw_exec_queue=asyncio.Queue(),
        risk_queue=asyncio.Queue(),
        order_queue=asyncio.Queue(),
        recorder_queue=asyncio.Queue(),
        position_store=SimpleNamespace(),
        order_id_map={},
        storm_guard=_StormGuard(),
        md_client=SimpleNamespace(),
        order_client=_OrderClient(),
        client=SimpleNamespace(),
        symbol_metadata=SimpleNamespace(),
        price_scale_provider=SimpleNamespace(),
        md_service=_Runner(),
        order_adapter=_Runner(),
        execution_gateway=_ExecutionGateway(),
        exec_service=_Runner(),
        risk_engine=_Runner(),
        recon_service=_Runner(),
        strategy_runner=_Runner(),
        recorder=_Runner(),
        gateway_service=gateway_service,
    )


def test_stop_calls_bootstrap_teardown_once():
    bootstrapper = MagicMock()
    bootstrapper.build.return_value = _registry()
    with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
        system = HFTSystem({})
    system.stop()
    system.stop()
    assert bootstrapper.teardown.call_count == 1


def test_iter_supervised_services_covers_critical_components():
    bootstrapper = MagicMock()
    bootstrapper.build.return_value = _registry(gateway_service=None)
    with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
        system = HFTSystem({})

    names = {name for name, _component, _factory in system._iter_supervised_services()}
    expected = {"md", "exec_router", "order", "exec_gateway", "recon", "strat", "recorder", "recorder_bridge", "risk"}
    assert expected.issubset(names)
    assert "gateway" not in names


def test_restart_service_backoff_prevents_restart_storm():
    bootstrapper = MagicMock()
    bootstrapper.build.return_value = _registry()
    with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
        system = HFTSystem({})

    def _close_coro(_name, coro):
        coro.close()

    system._start_service = MagicMock(side_effect=_close_coro)
    system._try_restart_service("md", "MarketDataService", system.md_service.run)
    system._try_restart_service("md", "MarketDataService", system.md_service.run)

    assert system._start_service.call_count == 1
