"""D2: Terminal callback arriving before _register_broker_ids must be deferred and drained."""

from __future__ import annotations

import asyncio
import collections
import threading
import time
from unittest.mock import MagicMock

import pytest

from hft_platform.order.adapter import _PENDING_SENTINEL, _TERMINAL_BEFORE_REGISTERED


def test_sentinel_objects_exist():
    assert _PENDING_SENTINEL is not None
    assert _TERMINAL_BEFORE_REGISTERED is not None
    assert _PENDING_SENTINEL is not _TERMINAL_BEFORE_REGISTERED


class TestDeferredTerminal:
    @pytest.fixture()
    def adapter(self):
        from hft_platform.order.adapter import OrderAdapter

        a = OrderAdapter.__new__(OrderAdapter)
        a.live_orders = {}
        a._live_orders_lock = asyncio.Lock()
        a._pending_order_keys = set()
        a._deferred_terminals = collections.deque(maxlen=256)
        a._cmd_created_ns_map = {}
        a._cmd_tca_map = {}
        a._pending_fill_index = {}
        a._pending_fill_registered_at = {}
        a._pending_fill_lock = threading.Lock()
        # P1-3 follow-up: terminal trackers must be seeded so that
        # ``on_terminal_state`` -> ``_record_recent_terminal`` ->
        # ``_clear_cancel_inflight`` does not AttributeError. Defaults mirror
        # ``OrderAdapter.__init__`` (env-driven; matched here as constants).
        a._recently_terminal_orders = collections.OrderedDict()
        a._recently_terminal_max = 2048
        a._recently_terminal_ttl_s = 60.0
        a._cancel_inflight_targets = collections.OrderedDict()
        a._cancel_inflight_max = 2048
        a._cancel_inflight_ttl_s = 30.0
        a.order_id_resolver = MagicMock()
        a.metrics = MagicMock()
        return a

    @pytest.mark.asyncio
    async def test_terminal_deferred_when_order_pending(self, adapter):
        async with adapter._live_orders_lock:
            adapter.live_orders["s1:42"] = _PENDING_SENTINEL
            adapter._pending_order_keys.add("s1:42")

        adapter.order_id_resolver.resolve_order_key.return_value = "s1:ABC123"

        await adapter.on_terminal_state("s1", "ABC123")

        assert len(adapter._deferred_terminals) == 1
        assert adapter._deferred_terminals[0][0] == "s1"
        assert adapter._deferred_terminals[0][1] == "ABC123"
        adapter.metrics.terminal_before_registration_total.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_normal_terminal_deletes_order(self, adapter):
        adapter.live_orders["s1:42"] = MagicMock()  # real trade
        adapter.order_id_resolver.resolve_order_key.return_value = "s1:42"

        await adapter.on_terminal_state("s1", "42")

        assert "s1:42" not in adapter.live_orders

    @pytest.mark.asyncio
    async def test_drain_resolves_deferred(self, adapter):
        adapter.live_orders["s1:42"] = MagicMock()
        adapter._deferred_terminals = collections.deque([("s1", "ABC123", time.monotonic())], maxlen=256)
        adapter.order_id_resolver.resolve_order_key.return_value = "s1:42"

        await adapter._drain_deferred_terminals("s1:42", MagicMock())

        assert "s1:42" not in adapter.live_orders
        assert len(adapter._deferred_terminals) == 0

    @pytest.mark.asyncio
    async def test_deferred_terminal_expires_after_30s(self, adapter):
        old_ts = time.monotonic() - 31.0
        adapter._deferred_terminals = collections.deque([("s1", "OLD_ORDER", old_ts)], maxlen=256)
        adapter.order_id_resolver.resolve_order_key.return_value = "s1:OLD_ORDER"

        await adapter._drain_deferred_terminals("s1:99", MagicMock())

        assert len(adapter._deferred_terminals) == 0
        adapter.metrics.deferred_terminal_expired_total.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_unresolved_deferred_stays_in_queue(self, adapter):
        """If resolved key not in live_orders yet, stay in deferred list."""
        adapter.live_orders = {}
        adapter._deferred_terminals = collections.deque([("s1", "XYZ", time.monotonic())], maxlen=256)
        adapter.order_id_resolver.resolve_order_key.return_value = "s1:DIFFERENT_KEY"

        await adapter._drain_deferred_terminals("s1:99", MagicMock())

        # Should remain since "s1:DIFFERENT_KEY" is not in live_orders
        assert len(adapter._deferred_terminals) == 1

    @pytest.mark.asyncio
    async def test_terminal_when_no_pending_cleans_up(self, adapter):
        """Terminal with no pending orders falls through to direct cleanup."""
        real_trade = MagicMock()
        adapter.live_orders["s1:42"] = real_trade
        adapter._pending_order_keys = set()
        adapter.order_id_resolver.resolve_order_key.return_value = "s1:42"

        await adapter.on_terminal_state("s1", "42")

        assert "s1:42" not in adapter.live_orders


class TestPendingCloseQtySentinelFilter:
    def test_sentinels_are_skipped(self):
        from hft_platform.order.adapter import OrderAdapter

        a = OrderAdapter.__new__(OrderAdapter)
        real_trade = {"contract_code": "2330", "action": "BUY", "qty": 5}
        a.live_orders = {
            "s1:1": _PENDING_SENTINEL,
            "s1:2": real_trade,
        }
        from hft_platform.contracts.strategy import Side

        qty = a._pending_close_qty("2330", Side.BUY)
        # Only real_trade counts, sentinel is skipped
        assert qty == 5

    def test_terminal_before_registered_sentinel_skipped(self):
        from hft_platform.order.adapter import OrderAdapter

        a = OrderAdapter.__new__(OrderAdapter)
        a.live_orders = {
            "s1:1": _TERMINAL_BEFORE_REGISTERED,
            "s1:2": {"contract_code": "2330", "action": "SELL", "qty": 3},
        }
        from hft_platform.contracts.strategy import Side

        qty = a._pending_close_qty("2330", Side.SELL)
        assert qty == 3

    def test_no_sentinels_normal_operation(self):
        from hft_platform.order.adapter import OrderAdapter

        a = OrderAdapter.__new__(OrderAdapter)
        a.live_orders = {
            "s1:1": {"contract_code": "2330", "action": "BUY", "qty": 10},
            "s1:2": {"contract_code": "2330", "action": "BUY", "qty": 5},
        }
        from hft_platform.contracts.strategy import Side

        qty = a._pending_close_qty("2330", Side.BUY)
        assert qty == 15
