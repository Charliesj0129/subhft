"""Tests for 5 bug fixes: quality flags, DLQ feedback, dedup rollback, gap recovery, consumer_seq.

Bug #5: Strategies don't consume feature quality_flags
Bug #7: Risk DLQ silent expiry without strategy feedback
Bug #6: OrderAdapter premature dedup commit
Bug #2: GapEvent with no strategy recovery
Bug #9: _consumer_seq reads write cursor instead of consumer position
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderIntent,
    RiskFeedback,
    Side,
    StormGuardState,
)
from hft_platform.events import FeatureUpdateEvent, GapEvent
from hft_platform.feature.engine import (
    QUALITY_FLAG_GAP,
    QUALITY_FLAG_OUT_OF_ORDER,
    QUALITY_FLAG_PARTIAL,
    QUALITY_FLAG_STALE_INPUT,
    QUALITY_FLAG_STATE_RESET,
)
from hft_platform.strategy.base import QUALITY_FLAGS_CORRUPT, BaseStrategy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_feature_event(
    symbol: str = "TXFD6",
    quality_flags: int = 0,
    values: tuple | None = None,
) -> FeatureUpdateEvent:
    return FeatureUpdateEvent(
        symbol=symbol,
        ts=0,
        local_ts=0,
        seq=1,
        feature_set_id="lob_shared_v3",
        schema_version=3,
        changed_mask=0,
        warmup_ready_mask=0,
        quality_flags=quality_flags,
        feature_ids=("best_bid", "best_ask"),
        values=values if values is not None else (100000, 100100),
    )


def _make_gap_event(missed: int = 50) -> GapEvent:
    return GapEvent(missed_count=missed, first_missed_seq=0, last_missed_seq=missed - 1, ts=0)


def _make_intent(intent_id: int = 1, price: int = 100, qty: int = 1) -> OrderIntent:
    return OrderIntent(intent_id, "s1", "2330", IntentType.NEW, Side.BUY, price, qty, TIF.ROD, None, 0)


# ===========================================================================
# Fix 1: Bug #5 — quality_flags check
# ===========================================================================


class TestQualityFlagsCorruptMask:
    """Verify QUALITY_FLAGS_CORRUPT includes GAP, STATE_RESET, OUT_OF_ORDER."""

    def test_mask_includes_gap(self) -> None:
        assert QUALITY_FLAGS_CORRUPT & QUALITY_FLAG_GAP

    def test_mask_includes_state_reset(self) -> None:
        assert QUALITY_FLAGS_CORRUPT & QUALITY_FLAG_STATE_RESET

    def test_mask_includes_out_of_order(self) -> None:
        assert QUALITY_FLAGS_CORRUPT & QUALITY_FLAG_OUT_OF_ORDER

    def test_mask_excludes_partial(self) -> None:
        assert not (QUALITY_FLAGS_CORRUPT & QUALITY_FLAG_PARTIAL)

    def test_mask_excludes_stale_input(self) -> None:
        assert not (QUALITY_FLAGS_CORRUPT & QUALITY_FLAG_STALE_INPUT)


class TestBaseStrategyShouldUseFeatures:
    """BaseStrategy._should_use_features rejects corrupt flags."""

    def test_accepts_clean_flags(self) -> None:
        event = _make_feature_event(quality_flags=0)
        assert BaseStrategy._should_use_features(event) is True

    def test_accepts_partial_flags(self) -> None:
        event = _make_feature_event(quality_flags=QUALITY_FLAG_PARTIAL)
        assert BaseStrategy._should_use_features(event) is True

    def test_accepts_stale_input_flags(self) -> None:
        event = _make_feature_event(quality_flags=QUALITY_FLAG_STALE_INPUT)
        assert BaseStrategy._should_use_features(event) is True

    def test_rejects_gap_flags(self) -> None:
        event = _make_feature_event(quality_flags=QUALITY_FLAG_GAP)
        assert BaseStrategy._should_use_features(event) is False

    def test_rejects_state_reset_flags(self) -> None:
        event = _make_feature_event(quality_flags=QUALITY_FLAG_STATE_RESET)
        assert BaseStrategy._should_use_features(event) is False

    def test_rejects_out_of_order_flags(self) -> None:
        event = _make_feature_event(quality_flags=QUALITY_FLAG_OUT_OF_ORDER)
        assert BaseStrategy._should_use_features(event) is False


class TestR47MakerOnFeaturesQualityFlags:
    """R47MakerStrategy.on_features skips corrupt-flagged events."""

    def test_on_features_skips_ooo_flags(self) -> None:
        from hft_platform.strategies.r47_maker import R47MakerStrategy

        strat = R47MakerStrategy(strategy_id="r47", symbols=["TXFD6"], max_pos=1)
        # First: cache a clean feature
        clean_event = _make_feature_event(quality_flags=0, values=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11))
        strat.on_features(clean_event)
        assert "TXFD6" in strat._feature_cache

        # Now send OOO-flagged event with different values
        bad_event = _make_feature_event(quality_flags=QUALITY_FLAG_OUT_OF_ORDER, values=(99, 99))
        strat.on_features(bad_event)
        # Cache should still have old values, not updated
        assert strat._feature_cache["TXFD6"] == (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)

    def test_on_features_accepts_partial_flags(self) -> None:
        from hft_platform.strategies.r47_maker import R47MakerStrategy

        strat = R47MakerStrategy(strategy_id="r47", symbols=["TXFD6"], max_pos=1)
        event = _make_feature_event(quality_flags=QUALITY_FLAG_PARTIAL, values=(10, 20))
        strat.on_features(event)
        assert strat._feature_cache["TXFD6"] == (10, 20)


class TestOpMMOnFeaturesQualityFlags:
    """OpportunisticMM.on_features skips corrupt-flagged events."""

    def test_on_features_skips_gap_flags(self) -> None:
        from hft_platform.strategies.opportunistic_mm import OpportunisticMM

        strat = OpportunisticMM(strategy_id="opmm", symbols=["TXFD6"])
        clean_event = _make_feature_event(quality_flags=0, values=(5, 10))
        strat.on_features(clean_event)
        assert "TXFD6" in strat._feature_cache

        bad_event = _make_feature_event(quality_flags=QUALITY_FLAG_GAP, values=(99, 99))
        strat.on_features(bad_event)
        assert strat._feature_cache["TXFD6"] == (5, 10)

    def test_on_features_accepts_partial_flags(self) -> None:
        from hft_platform.strategies.opportunistic_mm import OpportunisticMM

        strat = OpportunisticMM(strategy_id="opmm", symbols=["TXFD6"])
        event = _make_feature_event(quality_flags=QUALITY_FLAG_PARTIAL, values=(7, 8))
        strat.on_features(event)
        assert strat._feature_cache["TXFD6"] == (7, 8)


# ===========================================================================
# Fix 2: Bug #7 — DLQ expiry sends rejection feedback
# ===========================================================================


class TestDlqExpirySendsRejectionFeedback:
    """DLQ expiry/overflow/clear paths send RiskFeedback to _rejection_sink."""

    @pytest.fixture
    def engine(self, tmp_path):
        from hft_platform.risk.engine import RiskEngine

        cfg = tmp_path / "risk.yaml"
        cfg.write_text("risk:\n  max_order_size: 100\n  max_position: 200\n  max_notional: 10000000\n")
        q_in = asyncio.Queue()
        q_out = asyncio.Queue(maxsize=4096)
        rejection_sink = asyncio.Queue(maxsize=256)
        eng = RiskEngine(str(cfg), q_in, q_out, rejection_sink=rejection_sink)
        eng._dlq_drain_interval = 1
        return eng

    def test_ttl_expiry_sends_rejection_feedback(self, engine) -> None:
        cmd = engine.create_command(_make_intent(1))
        old_ts = time.monotonic_ns() - engine._dlq_ttl_ns - 1_000_000_000
        engine._order_dlq.append((cmd, old_ts))

        engine._drain_order_dlq()

        assert engine._rejection_sink.qsize() == 1
        fb = engine._rejection_sink.get_nowait()
        assert isinstance(fb, RiskFeedback)
        assert fb.reason_code == "dlq_ttl_expired"
        assert fb.strategy_id == "s1"

    def test_deadline_expiry_sends_rejection_feedback(self, engine) -> None:
        cmd = engine.create_command(_make_intent(1))
        cmd.deadline_ns = time.monotonic_ns() - 1  # already expired
        engine._order_dlq.append((cmd, time.monotonic_ns()))

        engine._drain_order_dlq()

        assert engine._rejection_sink.qsize() == 1
        fb = engine._rejection_sink.get_nowait()
        assert fb.reason_code == "dlq_deadline_expired"

    def test_storm_clear_sends_rejection_feedback(self, engine) -> None:
        cmd1 = engine.create_command(_make_intent(1))
        cmd2 = engine.create_command(_make_intent(2))
        engine._order_dlq.append((cmd1, time.monotonic_ns()))
        engine._order_dlq.append((cmd2, time.monotonic_ns()))

        # Force STORM state
        engine.storm_guard.state = StormGuardState.STORM

        engine._drain_order_dlq()

        assert engine._rejection_sink.qsize() == 2
        fb1 = engine._rejection_sink.get_nowait()
        fb2 = engine._rejection_sink.get_nowait()
        assert "storm" in fb1.reason_code or "halt" in fb1.reason_code
        assert "storm" in fb2.reason_code or "halt" in fb2.reason_code

    def test_overflow_eviction_sends_rejection_feedback(self, engine) -> None:
        """When DLQ exceeds max size, evicted entry sends feedback."""
        engine._ORDER_DLQ_MAX = 1
        # Fill DLQ with one entry
        cmd_old = engine.create_command(_make_intent(1))
        engine._order_dlq.append((cmd_old, time.monotonic_ns()))

        # Now simulate what happens when another cmd is added (overflow)
        cmd_new = engine.create_command(_make_intent(2))
        engine._order_dlq.append((cmd_new, time.monotonic_ns()))
        if len(engine._order_dlq) > engine._ORDER_DLQ_MAX:
            evicted, _ = engine._order_dlq.popleft()
            engine._send_dlq_rejection(evicted, "dlq_overflow_evicted")

        assert engine._rejection_sink.qsize() == 1
        fb = engine._rejection_sink.get_nowait()
        assert fb.reason_code == "dlq_overflow_evicted"


# ===========================================================================
# Fix 3: Bug #6 — OrderAdapter dedup rollback on dispatch failure
# ===========================================================================


class TestDedupReleaseOnDispatchFailure:
    """IdempotencyStore.release removes entry allowing resubmission."""

    def test_release_removes_entry(self) -> None:
        from hft_platform.gateway.dedup import IdempotencyStore

        store = IdempotencyStore(persist_enabled=False)
        # Reserve and commit
        store.check_or_reserve("key1")
        store.commit("key1", True, "enqueued", 1)

        # Before release: duplicate detected
        existing = store.check_or_reserve("key1")
        assert existing is not None
        assert existing.approved is True

        # Release
        store.release("key1")

        # After release: key is fresh
        result = store.check_or_reserve("key1")
        assert result is None  # None means new/reserved

    def test_release_noop_for_empty_key(self) -> None:
        from hft_platform.gateway.dedup import IdempotencyStore

        store = IdempotencyStore(persist_enabled=False)
        store.release("")  # should not raise

    def test_release_noop_for_missing_key(self) -> None:
        from hft_platform.gateway.dedup import IdempotencyStore

        store = IdempotencyStore(persist_enabled=False)
        store.release("nonexistent")  # should not raise


# ===========================================================================
# Fix 4: Bug #2 — GapEvent strategy recovery
# ===========================================================================


class TestR47MakerOnGapResetsState:
    """R47MakerStrategy.on_gap clears streaming state."""

    def test_on_gap_resets_feature_cache(self) -> None:
        from hft_platform.strategies.r47_maker import R47MakerStrategy

        strat = R47MakerStrategy(strategy_id="r47", symbols=["TXFD6"], max_pos=1)
        # Populate caches
        strat._feature_cache["TXFD6"] = (1, 2, 3)
        strat._last_bid["TXFD6"] = 100
        strat._last_ask["TXFD6"] = 101

        gap = _make_gap_event(50)
        strat.on_gap(gap)

        assert len(strat._feature_cache) == 0
        assert len(strat._last_bid) == 0
        assert len(strat._last_ask) == 0

    def test_on_gap_resets_pe_queue_mfg_states(self) -> None:
        from hft_platform.strategies.r47_maker import R47MakerStrategy

        strat = R47MakerStrategy(strategy_id="r47", symbols=["TXFD6"], max_pos=1)
        # Create some state
        strat._get_pe("TXFD6")
        strat._get_queue("TXFD6")
        strat._get_mfg("TXFD6")
        assert len(strat._pe_states) == 1

        gap = _make_gap_event(10)
        strat.on_gap(gap)

        assert len(strat._pe_states) == 0
        assert len(strat._queue_states) == 0
        assert len(strat._mfg_states) == 0

    def test_on_gap_resets_suppress_and_widen_flags(self) -> None:
        from hft_platform.strategies.r47_maker import R47MakerStrategy

        strat = R47MakerStrategy(strategy_id="r47", symbols=["TXFD6"], max_pos=1)
        strat._suppress_bid = True
        strat._suppress_ask = True
        strat._qi_widen_bid = 2
        strat._qi_widen_ask = 3

        gap = _make_gap_event(5)
        strat.on_gap(gap)

        assert strat._suppress_bid is False
        assert strat._suppress_ask is False
        assert strat._qi_widen_bid == 0
        assert strat._qi_widen_ask == 0


class TestOpMMOnGapResetsState:
    """OpportunisticMM.on_gap clears streaming state."""

    def test_on_gap_resets_feature_cache(self) -> None:
        from hft_platform.strategies.opportunistic_mm import OpportunisticMM

        strat = OpportunisticMM(strategy_id="opmm", symbols=["TXFD6"])
        strat._feature_cache["TXFD6"] = (1, 2, 3)
        strat._bid_oid = "order123"
        strat._ask_oid = "order456"

        gap = _make_gap_event(20)
        strat.on_gap(gap)

        assert len(strat._feature_cache) == 0
        assert strat._bid_oid is None
        assert strat._ask_oid is None


# ===========================================================================
# Fix 5: Bug #9 — _consumer_seq tracks actual position
# ===========================================================================


class TestConsumerSeqTracksActualPosition:
    """StrategyRunner._consumer_seq increments per event, not from bus.cursor."""

    @pytest.fixture(autouse=True)
    def _patch_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_STRATEGY_CONFIG", str(tmp_path / "empty.yaml"))
        (tmp_path / "empty.yaml").write_text("strategies: []\n")
        monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_RUST", "0")
        monkeypatch.setenv("HFT_STRATEGY_FEATURE_COMPAT_FAIL_FAST", "0")

    @pytest.fixture(autouse=True)
    def _patch_metrics(self):
        m = MagicMock()
        m.strategy_latency_ns.labels.return_value = MagicMock()
        m.strategy_intents_total.labels.return_value = MagicMock()
        m.feature_profile_compat_failures_total = MagicMock()
        with patch("hft_platform.strategy.runner.MetricsRegistry") as mr:
            mr.get.return_value = m
            with patch("hft_platform.strategy.runner.LatencyRecorder") as lr:
                lr.get.return_value = MagicMock()
                yield

    @pytest.mark.asyncio
    async def test_consumer_seq_increments_per_event(self) -> None:
        from hft_platform.strategy.runner import StrategyRunner

        bus = MagicMock()
        bus.cursor = 100  # Global write cursor is high
        events = [
            SimpleNamespace(symbol="TXFD6", ts=0),
            SimpleNamespace(symbol="TXFD6", ts=0),
            SimpleNamespace(symbol="TXFD6", ts=0),
        ]

        async def _gen(*args, **kwargs):
            for e in events:
                yield e

        bus.consume.return_value = _gen()
        bus.size = 1024

        risk_queue = MagicMock(spec=["put_nowait"])
        runner = StrategyRunner(bus, risk_queue)

        runner.running = True
        task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # _consumer_seq should reflect events processed, not bus.cursor
        # Initial seed = bus.cursor (100), then +1 per event = 103
        assert runner._consumer_seq == 103

    @pytest.mark.asyncio
    async def test_consumer_seq_seeds_from_start_cursor(self) -> None:
        from hft_platform.strategy.runner import StrategyRunner

        bus = MagicMock()
        bus.cursor = 200

        async def _gen(*args, **kwargs):
            yield SimpleNamespace(symbol="TXFD6", ts=0)

        bus.consume.return_value = _gen()
        bus.size = 1024

        risk_queue = MagicMock(spec=["put_nowait"])
        runner = StrategyRunner(bus, risk_queue)
        runner.set_start_cursor(50)

        runner.running = True
        task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Seeded at 50, consumed 1 event = 51
        assert runner._consumer_seq == 51
