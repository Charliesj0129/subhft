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

    def test_retry_resolves_matching_fills(self):
        dlq = OrphanedFillDLQ(max_size=100)
        fill1 = MagicMock(symbol="2330", order_id="ORD1", strategy_id="UNKNOWN")
        fill2 = MagicMock(symbol="2317", order_id="ORD2", strategy_id="UNKNOWN")
        dlq.add(fill1)
        dlq.add(fill2)

        def resolver(fill):
            return "strat_a" if fill.order_id == "ORD1" else "UNKNOWN"

        resolved, still_orphaned = dlq.retry(resolver)

        assert len(resolved) == 1
        assert resolved[0].strategy_id == "strat_a"
        assert len(still_orphaned) == 1
        assert dlq.count == 1  # Only unresolved remain

    def test_retry_all_resolved(self):
        dlq = OrphanedFillDLQ(max_size=100)
        fill = MagicMock(symbol="2330", order_id="ORD1", strategy_id="UNKNOWN")
        dlq.add(fill)

        resolved, still_orphaned = dlq.retry(lambda f: "my_strat")

        assert len(resolved) == 1
        assert len(still_orphaned) == 0
        assert dlq.count == 0

    def test_retry_none_resolved(self):
        dlq = OrphanedFillDLQ(max_size=100)
        fill = MagicMock(symbol="2330", order_id="ORD1", strategy_id="UNKNOWN")
        dlq.add(fill)

        resolved, still_orphaned = dlq.retry(lambda f: "UNKNOWN")

        assert len(resolved) == 0
        assert len(still_orphaned) == 1
        assert dlq.count == 1

    def test_retry_empty_dlq(self):
        dlq = OrphanedFillDLQ()

        resolved, still_orphaned = dlq.retry(lambda f: "strat")

        assert resolved == []
        assert still_orphaned == []
