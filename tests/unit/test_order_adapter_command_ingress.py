from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from hft_platform.contracts.strategy import OrderCommand
from hft_platform.order.adapter import OrderAdapter

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _adapter_with_queue(maxsize: int = 1) -> OrderAdapter:
    adapter = OrderAdapter.__new__(OrderAdapter)
    adapter._api_queue = asyncio.Queue(maxsize=maxsize)
    return adapter


@pytest.mark.asyncio
async def test_submit_command_uses_nonblocking_fast_path() -> None:
    adapter = _adapter_with_queue()
    command = cast(OrderCommand, SimpleNamespace(cmd_id=1))

    await adapter.submit_command(command)

    assert adapter._api_queue.get_nowait() is command


@pytest.mark.asyncio
async def test_submit_command_waits_for_bounded_queue_drain() -> None:
    adapter = _adapter_with_queue()
    adapter._api_queue.put_nowait(SimpleNamespace(cmd_id=0))
    command = cast(OrderCommand, SimpleNamespace(cmd_id=1))

    async def drain() -> None:
        await asyncio.sleep(0)
        adapter._api_queue.get_nowait()

    drain_task = asyncio.create_task(drain())
    await adapter.submit_command(command, timeout_s=0.05)
    await drain_task

    assert adapter._api_queue.get_nowait() is command


@pytest.mark.asyncio
async def test_submit_command_propagates_timeout_when_queue_stays_full() -> None:
    adapter = _adapter_with_queue()
    adapter._api_queue.put_nowait(SimpleNamespace(cmd_id=0))

    with pytest.raises(asyncio.TimeoutError):
        await adapter.submit_command(cast(OrderCommand, SimpleNamespace(cmd_id=1)), timeout_s=0.001)


def test_gateway_uses_public_order_adapter_ingress() -> None:
    path = _REPO_ROOT / "src" / "hft_platform" / "gateway" / "service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    assert "submit_command" in attributes
    assert "_api_queue" not in attributes
