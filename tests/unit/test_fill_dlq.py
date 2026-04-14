"""Tests for orphaned fill DLQ (WU-03)."""

import os

from unittest.mock import MagicMock

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import Side
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


def _make_fill(symbol: str, order_id: str = "ORD1") -> FillEvent:
    return FillEvent(
        fill_id=f"fill_{order_id}",
        account_id="ACC1",
        order_id=order_id,
        strategy_id="test",
        symbol=symbol,
        side=Side.BUY,
        qty=1,
        price=1000000,
        fee=0,
        tax=0,
        ingest_ts_ns=0,
        match_ts_ns=0,
    )


class TestOverflowEvictionPersistence:
    """Evicted fills must survive persist→load round-trip.

    Production scenario: DLQ hits 1000 entries, oldest fills evicted.
    On graceful shutdown, persist() must include evicted fills, not overwrite them.
    """

    def test_evicted_fills_survive_persist_load_cycle(self, tmp_path):
        persist_path = str(tmp_path / "dlq.jsonl")
        overflow_path = str(tmp_path / "dlq_overflow.jsonl")
        dlq = OrphanedFillDLQ(max_size=2, persist_path=persist_path)

        # Add 3 fills → first one is evicted
        dlq.add(_make_fill("EVICTED", "E1"))
        dlq.add(_make_fill("KEPT1", "K1"))
        dlq.add(_make_fill("KEPT2", "K2"))
        assert dlq.count == 2

        # Graceful shutdown
        dlq.persist()

        # Reload into new DLQ (larger so it can hold all)
        dlq2 = OrphanedFillDLQ(max_size=100, persist_path=persist_path)
        dlq2.load()

        # The evicted fill MUST be recovered
        all_fills = dlq2.drain()
        symbols = {f.symbol for f in all_fills}
        assert "EVICTED" in symbols, "Evicted fill was permanently lost"
        assert "KEPT1" in symbols
        assert "KEPT2" in symbols

    def test_multiple_evictions_all_recovered(self, tmp_path):
        persist_path = str(tmp_path / "dlq.jsonl")
        dlq = OrphanedFillDLQ(max_size=2, persist_path=persist_path)

        # Add 5 fills → 3 evicted, only last 2 in memory
        for i in range(5):
            dlq.add(_make_fill(f"SYM{i}", f"ORD{i}"))
        assert dlq.count == 2

        dlq.persist()

        dlq2 = OrphanedFillDLQ(max_size=100, persist_path=persist_path)
        dlq2.load()

        all_fills = dlq2.drain()
        symbols = {f.symbol for f in all_fills}
        # All 5 should be recoverable (3 evicted + 2 in-memory)
        for i in range(5):
            assert f"SYM{i}" in symbols, f"SYM{i} lost"
