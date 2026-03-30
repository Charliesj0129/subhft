"""Tests for order adapter dead letter queue, circuit breaker, and rate limiter.

Covers:
- DeadLetterQueue: entry creation, storage, eviction, retrieval
- CircuitBreaker / StrategyCircuitBreakerManager: state transitions
- RateLimiter / PerSymbolRateLimiter: window-based rate enforcement
"""

from collections import deque
from pathlib import Path

import pytest

from hft_platform.core import timebase
from hft_platform.order.circuit_breaker import (
    CircuitBreaker,
    StrategyCircuitBreakerManager,
)
from hft_platform.order.deadletter import (
    DeadLetterEntry,
    DeadLetterQueue,
    RejectionReason,
)
from hft_platform.core.rate_limiter import (
    PerSymbolRateLimiter,
    PerSymbolRateResult,
    RateLimiter,
)

# ---------------------------------------------------------------------------
# Dead Letter Queue
# ---------------------------------------------------------------------------


class TestDeadLetterEntry:
    def test_entry_fields(self):
        ts = timebase.now_ns()
        entry = DeadLetterEntry(
            timestamp_ns=ts,
            order_id="ORD-001",
            strategy_id="strat_a",
            symbol="2330",
            side="BUY",
            price=5000000,  # scaled x10000
            qty=10,
            reason=RejectionReason.RATE_LIMIT.value,
            error_message="rate exceeded",
        )
        assert entry.timestamp_ns == ts
        assert entry.order_id == "ORD-001"
        assert entry.strategy_id == "strat_a"
        assert entry.symbol == "2330"
        assert entry.side == "BUY"
        assert entry.price == 5000000
        assert entry.qty == 10
        assert entry.reason == "rate_limit"
        assert entry.error_message == "rate exceeded"
        assert entry.retry_count == 0
        assert entry.metadata == {}

    def test_to_dict_roundtrip(self):
        entry = DeadLetterEntry(
            timestamp_ns=timebase.now_ns(),
            order_id="ORD-002",
            strategy_id="strat_b",
            symbol="2317",
            side="SELL",
            price=1230000,
            qty=5,
            reason="broker_reject",
            error_message="insufficient margin",
            metadata={"extra": "info"},
            trace_id="trace-abc",
        )
        d = entry.to_dict()
        restored = DeadLetterEntry.from_dict(d)
        assert restored.order_id == entry.order_id
        assert restored.metadata == {"extra": "info"}
        assert restored.trace_id == "trace-abc"


class TestDeadLetterQueue:
    @pytest.mark.asyncio
    async def test_add_stores_entry(self, tmp_path: Path):
        dlq = DeadLetterQueue(dlq_dir=str(tmp_path / "dlq"), max_buffer_size=100)
        await dlq.add(
            order_id="O1",
            strategy_id="s1",
            symbol="2330",
            side="BUY",
            price=5000000,
            qty=1,
            reason=RejectionReason.CIRCUIT_BREAKER,
            error_message="cb open",
        )
        stats = await dlq.get_stats()
        assert stats["buffer_size"] == 1
        assert stats["total_entries"] == 1

    @pytest.mark.asyncio
    async def test_add_multiple_entries(self, tmp_path: Path):
        dlq = DeadLetterQueue(dlq_dir=str(tmp_path / "dlq"), max_buffer_size=100)
        for i in range(5):
            await dlq.add(
                order_id=f"O{i}",
                strategy_id="s1",
                symbol="2330",
                side="BUY",
                price=5000000,
                qty=1,
                reason=RejectionReason.RATE_LIMIT,
                error_message="limit hit",
            )
        stats = await dlq.get_stats()
        assert stats["buffer_size"] == 5
        assert stats["total_entries"] == 5

    @pytest.mark.asyncio
    async def test_flush_to_disk(self, tmp_path: Path):
        dlq_dir = tmp_path / "dlq"
        dlq = DeadLetterQueue(dlq_dir=str(dlq_dir), max_buffer_size=100)
        await dlq.add(
            order_id="O1",
            strategy_id="s1",
            symbol="2330",
            side="BUY",
            price=5000000,
            qty=1,
            reason=RejectionReason.API_TIMEOUT,
            error_message="timeout",
        )
        flushed = await dlq.flush()
        assert flushed == 1
        stats = await dlq.get_stats()
        assert stats["buffer_size"] == 0
        assert stats["total_flushed"] == 1
        # Verify file on disk
        files = list(dlq_dir.glob("dlq_*.jsonl"))
        assert len(files) == 1

    @pytest.mark.asyncio
    async def test_buffer_eviction_on_overflow(self, tmp_path: Path):
        """When flush fails, oldest entries are dropped to stay within bounds."""
        dlq = DeadLetterQueue(dlq_dir=str(tmp_path / "dlq"), max_buffer_size=3)

        # Patch _flush_locked to simulate flush failure (buffer not cleared)
        original_flush = dlq._flush_locked

        async def failing_flush():
            # Don't actually flush; leave buffer full
            return 0

        # Add entries up to the limit so flush triggers
        for i in range(3):
            await dlq.add(
                order_id=f"O{i}",
                strategy_id="s1",
                symbol="2330",
                side="BUY",
                price=5000000,
                qty=1,
                reason=RejectionReason.UNKNOWN,
                error_message="err",
            )

        # Now patch flush to fail and add one more entry
        dlq._flush_locked = failing_flush  # type: ignore[assignment]
        await dlq.add(
            order_id="O_overflow",
            strategy_id="s1",
            symbol="2330",
            side="BUY",
            price=5000000,
            qty=1,
            reason=RejectionReason.UNKNOWN,
            error_message="overflow",
        )

        stats = await dlq.get_stats()
        # Buffer should not exceed max_buffer_size
        assert stats["buffer_size"] <= dlq.max_buffer_size

    @pytest.mark.asyncio
    async def test_auto_flush_when_buffer_full(self, tmp_path: Path):
        dlq_dir = tmp_path / "dlq"
        dlq = DeadLetterQueue(dlq_dir=str(dlq_dir), max_buffer_size=3)
        for i in range(3):
            await dlq.add(
                order_id=f"O{i}",
                strategy_id="s1",
                symbol="2330",
                side="BUY",
                price=5000000,
                qty=1,
                reason=RejectionReason.VALIDATION_ERROR,
                error_message="bad",
            )
        # Auto-flush should have triggered
        stats = await dlq.get_stats()
        assert stats["total_flushed"] == 3
        assert stats["buffer_size"] == 0

    @pytest.mark.asyncio
    async def test_read_all_from_disk(self, tmp_path: Path):
        dlq_dir = tmp_path / "dlq"
        dlq = DeadLetterQueue(dlq_dir=str(dlq_dir), max_buffer_size=100)
        for i in range(5):
            await dlq.add(
                order_id=f"O{i}",
                strategy_id="s1",
                symbol="2330",
                side="BUY",
                price=5000000,
                qty=1,
                reason=RejectionReason.BROKER_REJECT,
                error_message="rejected",
            )
        await dlq.flush()
        entries = dlq.read_all(limit=10)
        assert len(entries) == 5
        assert entries[0].order_id == "O0"

    @pytest.mark.asyncio
    async def test_read_all_respects_limit(self, tmp_path: Path):
        dlq_dir = tmp_path / "dlq"
        dlq = DeadLetterQueue(dlq_dir=str(dlq_dir), max_buffer_size=100)
        for i in range(10):
            await dlq.add(
                order_id=f"O{i}",
                strategy_id="s1",
                symbol="2330",
                side="BUY",
                price=5000000,
                qty=1,
                reason=RejectionReason.UNKNOWN,
                error_message="err",
            )
        await dlq.flush()
        entries = dlq.read_all(limit=3)
        assert len(entries) == 3

    @pytest.mark.asyncio
    async def test_reason_enum_stored_as_string(self, tmp_path: Path):
        dlq = DeadLetterQueue(dlq_dir=str(tmp_path / "dlq"), max_buffer_size=100)
        await dlq.add(
            order_id="O1",
            strategy_id="s1",
            symbol="2330",
            side="BUY",
            price=5000000,
            qty=1,
            reason=RejectionReason.DEADLINE_EXCEEDED,
            error_message="too late",
        )
        await dlq.flush()
        entries = dlq.read_all()
        assert entries[0].reason == "deadline_exceeded"


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker(threshold=3, timeout_s=60)
        assert cb.is_open() is False
        assert cb.failure_count == 0

    def test_trips_after_threshold(self):
        cb = CircuitBreaker(threshold=3, timeout_s=60)
        assert cb.record_failure() is False
        assert cb.record_failure() is False
        tripped = cb.record_failure()
        assert tripped is True
        assert cb.is_open() is True

    def test_rejects_when_open(self):
        cb = CircuitBreaker(threshold=1, timeout_s=60)
        cb.record_failure()
        assert cb.is_open() is True

    def test_resets_after_cooldown(self):
        cb = CircuitBreaker(threshold=1, timeout_s=1)
        cb.record_failure()
        assert cb.is_open() is True

        # Simulate cooldown by setting open_until to the past (monotonic clock)
        import time

        cb.open_until = time.monotonic() - 1
        assert cb.is_open() is False

    def test_record_success_resets_failure_count(self):
        cb = CircuitBreaker(threshold=5, timeout_s=60)
        cb.record_failure()
        cb.record_failure()
        assert cb.failure_count == 2
        cb.record_success()
        assert cb.failure_count == 0

    def test_does_not_trip_below_threshold(self):
        cb = CircuitBreaker(threshold=5, timeout_s=60)
        for _ in range(4):
            assert cb.record_failure() is False
        assert cb.is_open() is False


class TestStrategyCircuitBreakerManager:
    def test_creates_breaker_per_strategy(self):
        mgr = StrategyCircuitBreakerManager(default_threshold=3, default_timeout_s=30)
        b1 = mgr.get_breaker("strat_a")
        b2 = mgr.get_breaker("strat_b")
        assert b1 is not b2
        # Same strategy returns same breaker
        assert mgr.get_breaker("strat_a") is b1

    def test_is_open_delegates(self):
        mgr = StrategyCircuitBreakerManager(default_threshold=1, default_timeout_s=60)
        assert mgr.is_open("strat_a") is False
        mgr.record_failure("strat_a")
        assert mgr.is_open("strat_a") is True

    def test_record_success_resets(self):
        mgr = StrategyCircuitBreakerManager(default_threshold=3, default_timeout_s=60)
        mgr.record_failure("strat_a")
        mgr.record_failure("strat_a")
        mgr.record_success("strat_a")
        breaker = mgr.get_breaker("strat_a")
        assert breaker.failure_count == 0

    def test_per_strategy_limits(self):
        limits = {"aggressive": {"cb_threshold": 2, "cb_timeout_s": 10}}
        mgr = StrategyCircuitBreakerManager(
            default_threshold=5,
            default_timeout_s=60,
            strategy_limits=limits,
        )
        b = mgr.get_breaker("aggressive")
        assert b.threshold == 2
        assert b.timeout_s == 10

    def test_cardinality_limit(self):
        mgr = StrategyCircuitBreakerManager(
            default_threshold=5,
            default_timeout_s=60,
            max_strategies=2,
        )
        mgr.get_breaker("s1")
        mgr.get_breaker("s2")
        # Record failures so eviction cannot remove them
        mgr.record_failure("s1")
        mgr.record_failure("s2")
        # Third strategy exceeds limit; returns temporary breaker
        b3 = mgr.get_breaker("s3")
        assert b3.threshold == 1  # temporary open breaker


# ---------------------------------------------------------------------------
# Rate Limiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_allows_within_hard_cap(self):
        rl = RateLimiter(soft_cap=5, hard_cap=10, window_s=1)
        assert rl.check() is True

    def test_rejects_at_hard_cap(self):
        rl = RateLimiter(soft_cap=5, hard_cap=3, window_s=10)
        now = timebase.now_s()
        rl.rate_window.extend([now, now, now])
        assert rl.check() is False

    def test_allows_after_window_expires(self):
        rl = RateLimiter(soft_cap=5, hard_cap=3, window_s=1)
        # Add timestamps that are expired
        old = timebase.now_s() - 2
        rl.rate_window.extend([old, old, old])
        assert rl.check() is True
        # Expired entries should be evicted
        assert len(rl.rate_window) == 0

    def test_record_appends_timestamp(self):
        rl = RateLimiter(soft_cap=5, hard_cap=10, window_s=1)
        rl.record()
        assert len(rl.rate_window) == 1

    def test_soft_cap_still_allows(self):
        rl = RateLimiter(soft_cap=2, hard_cap=10, window_s=10)
        now = timebase.now_s()
        rl.rate_window.extend([now, now])
        # At soft cap, check still returns True
        assert rl.check() is True

    def test_update_changes_limits(self):
        rl = RateLimiter(soft_cap=5, hard_cap=10, window_s=1)
        rl.update(hard_cap=2, window_s=5)
        assert rl.hard_cap == 2
        assert rl.window_s == 5
        assert rl.soft_cap == 5  # unchanged


class TestPerSymbolRateLimiter:
    def test_ok_when_no_records(self):
        rl = PerSymbolRateLimiter(soft_limit=5, hard_limit=10, window_s=1.0)
        assert rl.check("2330") == PerSymbolRateResult.OK

    def test_ok_within_limits(self):
        rl = PerSymbolRateLimiter(soft_limit=5, hard_limit=10, window_s=10.0)
        for _ in range(3):
            rl.record("2330")
        assert rl.check("2330") == PerSymbolRateResult.OK

    def test_soft_limit(self):
        rl = PerSymbolRateLimiter(soft_limit=3, hard_limit=10, window_s=10.0)
        for _ in range(3):
            rl.record("2330")
        assert rl.check("2330") == PerSymbolRateResult.SOFT

    def test_hard_limit(self):
        rl = PerSymbolRateLimiter(soft_limit=3, hard_limit=5, window_s=10.0)
        for _ in range(5):
            rl.record("2330")
        assert rl.check("2330") == PerSymbolRateResult.HARD

    def test_window_expiry_resets(self):
        rl = PerSymbolRateLimiter(soft_limit=3, hard_limit=5, window_s=1.0)
        # Insert old timestamps
        rl._windows["2330"] = deque([timebase.now_s() - 2.0] * 5)
        assert rl.check("2330") == PerSymbolRateResult.OK

    def test_per_symbol_isolation(self):
        rl = PerSymbolRateLimiter(soft_limit=2, hard_limit=3, window_s=10.0)
        for _ in range(3):
            rl.record("2330")
        # 2330 at hard limit, but 2317 is clean
        assert rl.check("2330") == PerSymbolRateResult.HARD
        assert rl.check("2317") == PerSymbolRateResult.OK

    def test_cardinality_limit(self):
        rl = PerSymbolRateLimiter(soft_limit=5, hard_limit=10, window_s=10.0, max_symbols=2)
        rl.record("SYM1")
        rl.record("SYM2")
        rl.record("SYM3")  # should be silently dropped
        assert "SYM3" not in rl._windows

    def test_record_increments_call_count(self):
        rl = PerSymbolRateLimiter(soft_limit=5, hard_limit=10, window_s=10.0)
        rl.record("2330")
        rl.record("2330")
        assert rl._call_count == 2
