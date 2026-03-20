import asyncio

import pytest

from hft_platform.execution.gateway import ExecutionGateway


class DummyAdapter:
    def __init__(self):
        self.run_called = False
        self.execute_called = False
        self.execute_cmd = None
        self.terminal_called = False
        self.terminal_strategy_id = None
        self.terminal_order_id = None
        self.running = True

    async def run(self) -> None:
        self.run_called = True

    async def execute(self, cmd):
        self.execute_called = True
        self.execute_cmd = cmd

    async def on_terminal_state(self, strategy_id: str, order_id: str) -> None:
        self.terminal_called = True
        self.terminal_strategy_id = strategy_id
        self.terminal_order_id = order_id


@pytest.mark.asyncio
async def test_execution_gateway_run_sets_flags():
    adapter = DummyAdapter()
    gateway = ExecutionGateway(adapter)
    await asyncio.wait_for(gateway.run(), timeout=1.0)
    assert adapter.run_called is True


@pytest.mark.asyncio
async def test_execution_gateway_run_sets_running_true():
    adapter = DummyAdapter()
    gateway = ExecutionGateway(adapter)
    assert gateway.running is False
    await asyncio.wait_for(gateway.run(), timeout=1.0)
    assert gateway.running is True


@pytest.mark.asyncio
async def test_execution_gateway_execute_delegates():
    adapter = DummyAdapter()
    gateway = ExecutionGateway(adapter)
    await gateway.execute({"cmd": "noop"})
    assert adapter.execute_called is True
    assert adapter.execute_cmd == {"cmd": "noop"}


@pytest.mark.asyncio
async def test_execution_gateway_on_terminal_state_delegates():
    adapter = DummyAdapter()
    gateway = ExecutionGateway(adapter)
    await gateway.on_terminal_state("strat", "order-1")
    assert adapter.terminal_called is True
    assert adapter.terminal_strategy_id == "strat"
    assert adapter.terminal_order_id == "order-1"


@pytest.mark.asyncio
async def test_execution_gateway_stop_sets_flags():
    adapter = DummyAdapter()
    gateway = ExecutionGateway(adapter)
    # Simulate running state
    await asyncio.wait_for(gateway.run(), timeout=1.0)
    assert gateway.running is True
    assert adapter.running is True

    gateway.stop()
    assert gateway.running is False
    assert adapter.running is False
