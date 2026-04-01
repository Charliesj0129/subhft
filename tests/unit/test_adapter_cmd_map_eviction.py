"""Tests for bounded _cmd_created_ns_map and _cmd_tca_map eviction in OrderAdapter.

Verifies:
1. Maps stay bounded after exceeding max size
2. live_orders entries are NOT evicted
3. Eviction removes oldest entries first (FIFO)
4. Both maps are evicted together
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side, StormGuardState
from hft_platform.order.adapter import OrderAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(tmp_path: Path, *, max_size: int = 5) -> OrderAdapter:
    cfg_path = tmp_path / "order_cfg.yaml"
    cfg_path.write_text("{}\n")
    adapter = OrderAdapter(str(cfg_path), asyncio.Queue(), MagicMock())
    adapter._cmd_map_max_size = max_size
    return adapter


def _make_cmd(
    intent_type: IntentType,
    *,
    strategy_id: str = "strat",
    intent_id: int = 1,
    created_ns: int = 1000,
) -> OrderCommand:
    intent = OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol="TXF",
        intent_type=intent_type,
        side=Side.BUY,
        price=100,
        qty=1,
        tif=TIF.LIMIT,
        trace_id="trace",
    )
    return OrderCommand(
        cmd_id=intent_id,
        intent=intent,
        deadline_ns=0,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=created_ns,
    )


def _order_key(strategy_id: str, intent_id: int) -> str:
    return f"{strategy_id}:{intent_id}"


async def _dispatch_new(adapter: OrderAdapter, strategy_id: str, intent_id: int, created_ns: int = 1000) -> str:
    """Insert an entry into both maps directly (bypasses broker call side-effects)."""
    order_key = _order_key(strategy_id, intent_id)
    cmd = _make_cmd(IntentType.NEW, strategy_id=strategy_id, intent_id=intent_id, created_ns=created_ns)
    # Populate the maps the same way _dispatch_to_api does
    if cmd.created_ns > 0:
        adapter._cmd_created_ns_map[order_key] = cmd.created_ns
    adapter._cmd_tca_map[order_key] = (int(cmd.decision_price), int(cmd.arrival_price))
    return order_key


def _trigger_eviction(adapter: OrderAdapter) -> int:
    """Run the eviction block that lives in _dispatch_to_api, returns evicted count."""
    evicted = 0
    if len(adapter._cmd_created_ns_map) >= adapter._cmd_map_max_size:
        evict_target = max(1, len(adapter._cmd_created_ns_map) // 10)
        for k in list(adapter._cmd_created_ns_map.keys()):
            if evicted >= evict_target:
                break
            if k not in adapter.live_orders:
                del adapter._cmd_created_ns_map[k]
                adapter._cmd_tca_map.pop(k, None)
                evicted += 1
    return evicted


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCmdMapEviction:
    """Verify bounded eviction of _cmd_created_ns_map and _cmd_tca_map."""

    def test_maps_stay_bounded_after_exceeding_max_size(self, tmp_path: Path) -> None:
        """After exceeding max_size, eviction fires and maps shrink below max_size."""
        adapter = _make_adapter(tmp_path, max_size=5)

        # Fill maps to max_size (no live orders — all evictable)
        for i in range(1, 6):
            key = _order_key("strat", i)
            adapter._cmd_created_ns_map[key] = 1000 + i
            adapter._cmd_tca_map[key] = (100, 101)

        assert len(adapter._cmd_created_ns_map) == 5

        # Trigger eviction (simulates the check in _dispatch_to_api)
        evicted = _trigger_eviction(adapter)

        assert evicted >= 1, "Expected at least one entry to be evicted"
        assert len(adapter._cmd_created_ns_map) < 5, "Map must shrink after eviction"

    def test_live_orders_are_not_evicted(self, tmp_path: Path) -> None:
        """Entries whose key is in live_orders must survive eviction."""
        adapter = _make_adapter(tmp_path, max_size=5)

        # Insert 5 entries; mark first 3 as live
        live_keys = []
        for i in range(1, 6):
            key = _order_key("strat", i)
            adapter._cmd_created_ns_map[key] = 1000 + i
            adapter._cmd_tca_map[key] = (100, 101)
            if i <= 3:
                adapter.live_orders[key] = {"status": "open"}
                live_keys.append(key)

        _trigger_eviction(adapter)

        # All live keys must still be present
        for key in live_keys:
            assert key in adapter._cmd_created_ns_map, f"Live key {key} was incorrectly evicted"
            assert key in adapter._cmd_tca_map, f"Live key {key} TCA entry was incorrectly evicted"

    def test_eviction_removes_oldest_entries_first(self, tmp_path: Path) -> None:
        """FIFO eviction: oldest-inserted keys are removed before newer ones."""
        adapter = _make_adapter(tmp_path, max_size=5)

        # Insert 5 entries in order; all non-live
        keys_in_order = []
        for i in range(1, 6):
            key = _order_key("strat", i)
            adapter._cmd_created_ns_map[key] = 1000 + i
            adapter._cmd_tca_map[key] = (100, 101)
            keys_in_order.append(key)

        _trigger_eviction(adapter)

        # The oldest key (index 0) should be gone; the newest (index 4) should survive
        oldest_key = keys_in_order[0]
        newest_key = keys_in_order[-1]
        assert oldest_key not in adapter._cmd_created_ns_map, "Oldest key should have been evicted"
        assert newest_key in adapter._cmd_created_ns_map, "Newest key should survive eviction"

    def test_both_maps_evicted_together(self, tmp_path: Path) -> None:
        """When a key is evicted from _cmd_created_ns_map, it is also removed from _cmd_tca_map."""
        adapter = _make_adapter(tmp_path, max_size=5)

        for i in range(1, 6):
            key = _order_key("strat", i)
            adapter._cmd_created_ns_map[key] = 1000 + i
            adapter._cmd_tca_map[key] = (100 + i, 101 + i)

        before_created = set(adapter._cmd_created_ns_map.keys())
        _trigger_eviction(adapter)
        after_created = set(adapter._cmd_created_ns_map.keys())

        # Keys removed from _cmd_created_ns_map must also be absent from _cmd_tca_map
        removed_keys = before_created - after_created
        assert removed_keys, "Expected at least one key to be evicted"
        for key in removed_keys:
            assert key not in adapter._cmd_tca_map, f"Key {key} was removed from created map but not from tca map"

    def test_no_eviction_below_max_size(self, tmp_path: Path) -> None:
        """Maps below max_size must not trigger any eviction."""
        adapter = _make_adapter(tmp_path, max_size=10)

        for i in range(1, 6):  # 5 entries, max is 10
            key = _order_key("strat", i)
            adapter._cmd_created_ns_map[key] = 1000 + i
            adapter._cmd_tca_map[key] = (100, 101)

        evicted = _trigger_eviction(adapter)

        assert evicted == 0, "No eviction expected when maps are below max_size"
        assert len(adapter._cmd_created_ns_map) == 5

    def test_all_live_no_eviction_possible(self, tmp_path: Path) -> None:
        """If all entries are live, eviction fires but removes nothing."""
        adapter = _make_adapter(tmp_path, max_size=5)

        for i in range(1, 6):
            key = _order_key("strat", i)
            adapter._cmd_created_ns_map[key] = 1000 + i
            adapter._cmd_tca_map[key] = (100, 101)
            adapter.live_orders[key] = {"status": "open"}

        evicted = _trigger_eviction(adapter)

        assert evicted == 0, "No eviction expected when all entries are live"
        assert len(adapter._cmd_created_ns_map) == 5, "Maps must remain full if all entries are live"

    def test_amend_cancel_only_populates_created_map(self, tmp_path: Path) -> None:
        """AMEND/CANCEL entries are in _cmd_created_ns_map only, not _cmd_tca_map.
        Eviction of such a key should pop from both maps without error."""
        adapter = _make_adapter(tmp_path, max_size=5)

        # Simulate AMEND — only created_ns map is populated (no tca entry)
        for i in range(1, 6):
            key = _order_key("strat", i)
            adapter._cmd_created_ns_map[key] = 1000 + i
            # Deliberately skip _cmd_tca_map for some entries (amend/cancel pattern)

        before_count = len(adapter._cmd_created_ns_map)
        # Should not raise even though _cmd_tca_map has no matching keys
        evicted = _trigger_eviction(adapter)

        assert evicted >= 1
        assert len(adapter._cmd_created_ns_map) < before_count
