from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.core import timebase
from hft_platform.execution.normalizer import RawExecEvent
from hft_platform.execution.router import ExecutionRouter
from hft_platform.order.adapter import OrderAdapter
from hft_platform.services.system import HFTSystem


class _Runner:
    def __init__(self) -> None:
        self.running = True
        self._stale_event_skip_total = 0

    async def run(self) -> None:
        await asyncio.sleep(0)

    def reset_stale_counter(self) -> None:
        self._stale_event_skip_total = 0


class _ExecutionGateway(_Runner):
    def stop(self) -> None:
        return None


class _ReconnectMdService(_Runner):
    def __init__(self) -> None:
        super().__init__()
        self._callbacks: list = []

    def register_on_reconnect(self, callback) -> None:
        self._callbacks.append(callback)

    async def trigger_reconnect(self, reason: str = "heartbeat_gap") -> None:
        for callback in self._callbacks:
            result = callback(reason)
            if asyncio.iscoroutine(result):
                await result


class _BrokerClient:
    def login(self) -> None:
        return None

    def close(self, logout: bool = True) -> None:
        return None

    def set_execution_callbacks(self, on_order, on_deal) -> None:
        return None


class _PlatformInputs:
    def bind_runtime_probes(self, **kwargs) -> None:
        return None


def _write_order_config(tmp_path: Path) -> str:
    cfg = tmp_path / "order_adapter.yaml"
    cfg.write_text(
        "\n".join(
            [
                "rate_limits:",
                "  shioaji_soft_cap: 1000",
                "  shioaji_hard_cap: 2000",
                "  window_seconds: 10",
                "circuit_breaker:",
                "  threshold: 5",
                "  timeout_seconds: 60",
            ]
        )
        + "\n"
    )
    return str(cfg)


def _write_symbols_config(tmp_path: Path) -> str:
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: 'TXFD6'\n    exchange: 'TAIFEX'\n    price_scale: 10000\n")
    return str(cfg)


def _make_fill_raw(*, order_id: str, fill_id: str) -> RawExecEvent:
    return RawExecEvent(
        topic="deal",
        data={
            "ordno": order_id,
            "code": "TXFD6",
            "action": "Buy",
            "price": 123.0,
            "quantity": 1,
            "seqno": fill_id,
            "account_id": "FUTACC1",
            "ts": timebase.now_ns(),
        },
        ingest_ts_ns=timebase.now_ns(),
    )


def _build_registry(tmp_path: Path) -> SimpleNamespace:
    order_id_map: dict[str, str] = {}
    raw_exec_queue: asyncio.Queue = asyncio.Queue()
    md_service = _ReconnectMdService()
    order_client = _BrokerClient()
    md_client = _BrokerClient()
    client = MagicMock()
    client.get_exchange.return_value = "TAIFEX"
    client.place_order.return_value = {"seqno": "SEQ1", "ordno": "ORD1"}
    client.cancel_order.return_value = {}
    client.update_order.return_value = {}

    order_adapter = OrderAdapter(
        config_path=_write_order_config(tmp_path),
        order_queue=asyncio.Queue(),
        broker_client=client,
        order_id_map=order_id_map,
    )
    position_store = MagicMock()
    position_store.positions = {}
    position_store.on_fill.return_value = MagicMock(realized_pnl=0)
    exec_service = ExecutionRouter(
        bus=MagicMock(cursor=-1),
        raw_queue=raw_exec_queue,
        order_id_map=order_id_map,
        position_store=position_store,
        terminal_handler=order_adapter,
    )
    return SimpleNamespace(
        bus=MagicMock(cursor=-1),
        raw_queue=asyncio.Queue(),
        raw_exec_queue=raw_exec_queue,
        risk_queue=asyncio.Queue(),
        order_queue=asyncio.Queue(),
        recorder_queue=asyncio.Queue(),
        position_store=position_store,
        order_id_map=order_id_map,
        storm_guard=SimpleNamespace(state=0),
        md_client=md_client,
        order_client=order_client,
        client=client,
        symbol_metadata=SimpleNamespace(),
        price_scale_provider=SimpleNamespace(),
        md_service=md_service,
        order_adapter=order_adapter,
        execution_gateway=_ExecutionGateway(),
        exec_service=exec_service,
        risk_engine=_Runner(),
        recon_service=_Runner(),
        strategy_runner=_Runner(),
        recorder=_Runner(),
        gateway_service=None,
        checkpoint_writer=None,
        platform_degrade_inputs=_PlatformInputs(),
    )


def _build_system(tmp_path: Path) -> HFTSystem:
    bootstrapper = MagicMock()
    bootstrapper.build.return_value = _build_registry(tmp_path)
    bootstrapper.build_platform_degrade_inputs.return_value = _PlatformInputs()
    with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
        return HFTSystem({})


@pytest.fixture(autouse=True)
def _persist_env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYMBOLS_CONFIG", _write_symbols_config(tmp_path))
    monkeypatch.setenv("HFT_ORDER_ID_MAP_PERSIST_PATH", str(tmp_path / "order_id_map.jsonl"))
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", str(tmp_path / "fill_dedup.jsonl"))
    monkeypatch.setenv("HFT_ORDER_ID_MAP_PERSIST_INTERVAL_S", "0")
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_INTERVAL_S", "0")


@pytest.mark.asyncio
async def test_system_crash_restart_restores_order_routing_and_fill_dedup(tmp_path):
    system1 = _build_system(tmp_path)
    await system1.order_adapter._register_broker_ids("R47:101", {"ordno": "ORD_RESTART", "seqno": "SEQ_RESTART"})
    system1.raw_exec_queue.put_nowait(_make_fill_raw(order_id="ORD_RESTART", fill_id="FILL_RESTART"))

    drained = await system1.exec_service.stop(drain_timeout_s=1.0)
    assert drained == 1

    assert os.path.exists(str(tmp_path / "order_id_map.jsonl"))
    assert os.path.exists(str(tmp_path / "fill_dedup.jsonl"))

    system2 = _build_system(tmp_path)
    fill = system2.exec_service.normalizer.normalize_fill(
        _make_fill_raw(order_id="ORD_RESTART", fill_id="FILL_RESTART")
    )

    assert fill is not None
    assert fill.strategy_id == "R47"
    assert "FILL_RESTART" in system2.exec_service._seen_fill_ids

    system2.raw_exec_queue.put_nowait(_make_fill_raw(order_id="ORD_RESTART", fill_id="FILL_RESTART"))
    drained_again = await system2.exec_service.stop(drain_timeout_s=1.0)

    assert drained_again == 0
    system2.position_store.on_fill.assert_not_called()


@pytest.mark.asyncio
async def test_system_reconnect_callback_invalidates_live_orders(tmp_path):
    system = _build_system(tmp_path)
    system.order_adapter.live_orders["R47:101"] = {"broker_id": "ORD_RECONNECT"}
    system.order_adapter._pending_order_keys.add("R47:101")

    await system.md_service.trigger_reconnect("heartbeat_gap")

    assert len(system.order_adapter.live_orders) == 0
    assert len(system.order_adapter._pending_order_keys) == 0


@pytest.mark.asyncio
async def test_system_stop_async_restart_restores_execution_recovery_state(tmp_path):
    system1 = _build_system(tmp_path)
    await system1.order_adapter._register_broker_ids("R47:202", {"ordno": "ORD_STOP", "seqno": "SEQ_STOP"})
    system1.raw_exec_queue.put_nowait(_make_fill_raw(order_id="ORD_STOP", fill_id="FILL_STOP"))

    await system1.stop_async()

    system2 = _build_system(tmp_path)
    fill = system2.exec_service.normalizer.normalize_fill(_make_fill_raw(order_id="ORD_STOP", fill_id="FILL_STOP"))

    assert fill is not None
    assert fill.strategy_id == "R47"
    assert "FILL_STOP" in system2.exec_service._seen_fill_ids
