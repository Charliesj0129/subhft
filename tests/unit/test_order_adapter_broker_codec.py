"""Tests for B1 audit fix: OrderAdapter must use BrokerOrderCodec protocol, not ShioajiOrderCodec."""

from __future__ import annotations

import ast
import asyncio
import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.protocol import BrokerOrderCodec


class _StubCodec:
    """Minimal BrokerOrderCodec-conformant stub for testing."""

    __slots__ = ("calls",)

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def encode_side(self, side: str) -> Any:
        self.calls.append(("side", side))
        return side

    def encode_tif(self, tif: str) -> Any:
        self.calls.append(("tif", tif))
        return tif

    def encode_price_type(self, price_type: str) -> Any:
        self.calls.append(("price_type", price_type))
        return price_type


def test_stub_codec_satisfies_protocol():
    """Verify our test stub satisfies BrokerOrderCodec protocol."""
    assert isinstance(_StubCodec(), BrokerOrderCodec)


def test_order_adapter_no_shioaji_import():
    """OrderAdapter module must NOT import ShioajiOrderCodec (MB-02)."""
    source_path = inspect.getfile(__import__("hft_platform.order.adapter", fromlist=["OrderAdapter"]))
    with open(source_path) as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and "shioaji" in node.module:
                imported_names = [alias.name for alias in node.names]
                pytest.fail(
                    f"MB-02 violation: order/adapter.py imports {imported_names} from {node.module}"
                )


def test_order_adapter_accepts_protocol_codec():
    """OrderAdapter must accept any BrokerOrderCodec, not just ShioajiOrderCodec."""
    from hft_platform.order.adapter import OrderAdapter

    stub = _StubCodec()
    queue: asyncio.Queue = asyncio.Queue()
    mock_client = MagicMock()

    adapter = OrderAdapter(
        config_path="/dev/null",
        order_queue=queue,
        broker_client=mock_client,
        broker_codec=stub,
    )
    assert adapter._broker_codec is stub


def test_order_adapter_requires_codec_injection():
    """OrderAdapter must NOT silently default to ShioajiOrderCodec."""
    from hft_platform.order.adapter import OrderAdapter

    queue: asyncio.Queue = asyncio.Queue()
    mock_client = MagicMock()

    adapter = OrderAdapter(
        config_path="/dev/null",
        order_queue=queue,
        broker_client=mock_client,
    )
    assert adapter._broker_codec is None
