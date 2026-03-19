"""Tests for orphaned fill DLQ (WU-03)."""
from unittest.mock import MagicMock

from hft_platform.execution.fill_dlq import OrphanedFillDLQ


class TestOrphanedFillDLQ:
    """WU-03: Verify orphaned fill dead-letter queue."""

    def test_add_and_count(self):
        dlq = OrphanedFillDLQ(max_size=100)
        fill = MagicMock(symbol="2330", order_id="123", strategy_id="UNKNOWN")

        dlq.add(fill)

        assert dlq.count == 1

    def test_bounded_size(self):
        dlq = OrphanedFillDLQ(max_size=3)

        for i in range(5):
            fill = MagicMock(symbol=f"sym{i}")
            dlq.add(fill)

        assert dlq.count == 3  # Bounded to max_size

    def test_drain_returns_all_items(self):
        dlq = OrphanedFillDLQ(max_size=100)

        for i in range(3):
            dlq.add(MagicMock(symbol=f"sym{i}"))

        items = dlq.drain()

        assert len(items) == 3
        assert dlq.count == 0  # Empty after drain

    def test_drain_empty_returns_empty_list(self):
        dlq = OrphanedFillDLQ()

        items = dlq.drain()

        assert items == []
        assert dlq.count == 0

    def test_initial_count_is_zero(self):
        dlq = OrphanedFillDLQ()

        assert dlq.count == 0
