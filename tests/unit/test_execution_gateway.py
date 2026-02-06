import asyncio

import pytest

from hft_platform.execution.gateway import ExecutionGateway


class DummyAdapter:
    def __init__(self):
        self.run_called = False
        self.execute_called = False
        self.terminal_called = False
        self.running = True

    async def run(self) -> None:
        self.run_called = True

    async def execute(self, cmd):
        self.execute_called = True

    async def on_terminal_state(self, strategy_id: str, order_id: str) -> None:
        self.terminal_called = True


@pytest.mark.asyncio
async def test_execution_gateway_run_sets_flags():
    adapter = DummyAdapter()
    gateway = ExecutionGateway(adapter)
    await asyncio.wait_for(gateway.run(), timeout=1.0)
    assert adapter.run_called is True


@pytest.mark.asyncio
async def test_execution_gateway_execute_delegates():
    adapter = DummyAdapter()
    gateway = ExecutionGateway(adapter)
    await gateway.execute({"cmd": "noop"})
    assert adapter.execute_called is True


@pytest.mark.asyncio
async def test_execution_gateway_on_terminal_state_delegates():
    adapter = DummyAdapter()
    gateway = ExecutionGateway(adapter)
    await gateway.on_terminal_state("strat", "order-1")
    assert adapter.terminal_called is True
