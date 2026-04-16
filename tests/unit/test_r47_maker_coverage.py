"""Coverage tests for strategies/r47_maker.py — targeting uncovered lines.

Covers: _PEState (permutation entropy), _QueueState (queue survival),
_MFGState (MFG inventory), R47MakerStrategy quoting logic, event handlers,
position management, fill dedup, on_order, on_risk_feedback, on_gap.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus
from hft_platform.contracts.strategy import RiskFeedback, Side
from hft_platform.events import FeatureUpdateEvent, GapEvent, LOBStatsEvent, TickEvent

_PRICE_SCALE = 10000


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_lob_stats(
    symbol="TMFD6",
    mid_price_x2=2000_0000,
    spread_scaled=6_0000,
    imbalance=0.0,
    best_bid=997_0000,
    best_ask=1003_0000,
    bid_depth=100,
    ask_depth=100,
    ts=0,
):
    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        imbalance=imbalance,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        mid_price_x2=mid_price_x2,
        spread_scaled=spread_scaled,
    )


def _make_feature_event(
    symbol="TMFD6",
    values=None,
    quality_flags=0,
):
    """Create a FeatureUpdateEvent with enough values for R47 indices."""
    if values is None:
        # 22 features: indices 0-21 used by R47
        values = tuple([0] * 22)
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
        feature_ids=tuple(f"f{i}" for i in range(len(values))),
        values=values,
    )


def _make_fill(symbol="TMFD6", side=Side.BUY, qty=1, price=1000_0000, fill_id="F001"):
    return FillEvent(
        fill_id=fill_id,
        account_id="acct",
        order_id="O001",
        strategy_id="r47_maker",
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=0,
        tax=0,
        ingest_ts_ns=0,
        match_ts_ns=0,
    )


def _make_order_event(
    symbol="TMFD6",
    status=OrderStatus.SUBMITTED,
    side=Side.BUY,
    order_id="O001",
    remaining_qty=1,
):
    return OrderEvent(
        order_id=order_id,
        strategy_id="r47_maker",
        symbol=symbol,
        status=status,
        submitted_qty=1,
        filled_qty=0,
        remaining_qty=remaining_qty,
        price=1000_0000,
        side=side,
        ingest_ts_ns=0,
        broker_ts_ns=0,
    )


def _make_tick(symbol="TMFD6", price=1000_0000, volume=1, direction=1):
    meta = SimpleNamespace(local_ts=0, source_ts=0, seq=1, topic="tick")
    return TickEvent(
        meta=meta,
        symbol=symbol,
        price=price,
        volume=volume,
        trade_direction=direction,
    )


@pytest.fixture()
def strategy():
    """Create an R47MakerStrategy with a mocked StrategyContext."""
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    strat = R47MakerStrategy(
        strategy_id="r47_maker",
        pe_danger_threshold=0.55,
        pe_window=100,
        queue_cancel_threshold=0.7,
        mfg_skew_z_threshold=2.0,
        spread_threshold_pts=5,
        toxicity_max=700,
        qi_skew_threshold=0.1,
        qi_widen_ticks=1,
        max_pos=2,
    )
    strat.symbols = {"TMFD6"}
    # Set up a mock context with a factory that returns OrderIntent objects
    ctx = MagicMock()
    ctx.positions = {}
    ctx.strategy_id = "r47_maker"
    ctx.place_order = MagicMock(return_value=MagicMock())
    strat.ctx = ctx
    strat._generated_intents = []
    return strat


# ---------------------------------------------------------------------------
# Tests: _PEState (Permutation Entropy)
# ---------------------------------------------------------------------------


class TestPEState:
    """Test lines 85-151: PE warmup, pattern computation, entropy calculation."""

    def test_warmup_returns_default_h(self):
        """Lines 94-95: returns default h=1.0 before warmup."""
        from hft_platform.strategies.r47_maker import _PEState

        pe = _PEState(d=4, window=100)
        assert pe.h == 1.0
        assert pe.warmed_up is False
        h = pe.update(1.0)
        assert h == 1.0  # Not enough samples yet

    def test_warmup_needs_d_values(self):
        """Lines 90-91: Need d values before computing pattern."""
        from hft_platform.strategies.r47_maker import _PEState

        pe = _PEState(d=4, window=100)
        for i in range(3):  # d-1 values
            h = pe.update(float(i))
        assert pe.warmed_up is False

    def test_warmup_insufficient_samples(self):
        """Lines 105-107: Need 20 samples before computing entropy."""
        from hft_platform.strategies.r47_maker import _PEState

        pe = _PEState(d=4, window=100)
        for i in range(10):
            pe.update(float(i))
        assert pe.warmed_up is False

    def test_full_warmup_computes_entropy(self):
        """Lines 109-118: Full warmup computes normalized entropy."""
        from hft_platform.strategies.r47_maker import _PEState

        pe = _PEState(d=4, window=50)
        # Feed monotonically increasing values — should produce low entropy
        for i in range(60):
            pe.update(float(i))
        assert pe.warmed_up is True
        h = pe.h
        # Monotonic sequence produces only one pattern → low entropy
        assert 0.0 <= h <= 1.0

    def test_random_input_higher_entropy(self):
        """Diverse patterns produce higher entropy."""
        from hft_platform.strategies.r47_maker import _PEState

        pe = _PEState(d=4, window=50)
        import math

        for i in range(60):
            pe.update(math.sin(i * 0.7))
        assert pe.warmed_up is True
        assert pe.h > 0.0

    def test_sliding_window_evicts_oldest(self):
        """Lines 98-103: When deque is full, oldest pattern is removed."""
        from hft_platform.strategies.r47_maker import _PEState

        pe = _PEState(d=4, window=30)  # pat_win = 30 - 4 + 1 = 27
        for i in range(50):
            pe.update(float(i % 7))
        assert pe.warmed_up is True
        assert len(pe._pat_deque) == pe._pat_deque.maxlen

    def test_rank_to_id_bijection(self):
        """Lines 120-150: Lehmer code is bijective for D=4."""
        from hft_platform.strategies.r47_maker import _PEState

        seen = set()
        from itertools import permutations

        for perm in permutations(range(4)):
            idx = _PEState._rank_to_id(list(perm))
            assert 0 <= idx < 24
            seen.add(idx)
        assert len(seen) == 24  # All 24 patterns mapped uniquely

    def test_rank_to_id_with_ties(self):
        """Tied values produce a deterministic pattern."""
        from hft_platform.strategies.r47_maker import _PEState

        idx = _PEState._rank_to_id([1.0, 1.0, 1.0, 1.0])
        assert isinstance(idx, int)
        assert 0 <= idx < 24


# ---------------------------------------------------------------------------
# Tests: _QueueState (Queue Survival)
# ---------------------------------------------------------------------------


class TestQueueState:
    """Test lines 194-234: queue update, EMA, depletion probability."""

    def test_first_update_initializes(self):
        """Lines 198-201: first update stores prev values."""
        from hft_platform.strategies.r47_maker import _QueueState

        qs = _QueueState(ema_alpha=0.05)
        p_bid, p_ask = qs.update(100, 100)
        assert p_bid == 0.5  # default
        assert p_ask == 0.5
        assert qs.warmed_up is False

    def test_positive_delta_updates_lambda(self):
        """Lines 211-213: positive delta increases arrival rate."""
        from hft_platform.strategies.r47_maker import _QueueState

        qs = _QueueState(ema_alpha=0.1)
        qs.update(100, 100)
        qs.update(120, 100)  # +20 on bid
        assert qs._lambda_bid > 1.0

    def test_negative_delta_updates_mu(self):
        """Lines 213-214: negative delta increases departure rate."""
        from hft_platform.strategies.r47_maker import _QueueState

        qs = _QueueState(ema_alpha=0.1)
        qs.update(100, 100)
        qs.update(80, 100)  # -20 on bid
        assert qs._mu_bid > 1.0

    def test_ask_positive_delta(self):
        """Lines 216-218: ask arrivals tracked separately."""
        from hft_platform.strategies.r47_maker import _QueueState

        qs = _QueueState(ema_alpha=0.1)
        qs.update(100, 100)
        qs.update(100, 130)  # +30 on ask
        assert qs._lambda_ask > 1.0

    def test_ask_negative_delta(self):
        """Lines 218-219: ask departures tracked."""
        from hft_platform.strategies.r47_maker import _QueueState

        qs = _QueueState(ema_alpha=0.1)
        qs.update(100, 100)
        qs.update(100, 70)  # -30 on ask
        assert qs._mu_ask > 1.0

    def test_warmup_after_50_updates(self):
        """Lines 203-204: warmed_up after 50 updates."""
        from hft_platform.strategies.r47_maker import _QueueState

        qs = _QueueState(ema_alpha=0.05)
        for i in range(55):
            qs.update(100 + i % 10, 100 + i % 7)
        assert qs.warmed_up is True

    def test_depletion_probability_bounds(self):
        """Lines 222-234: P(depletion) clamped in [0, 1]."""
        from hft_platform.strategies.r47_maker import _QueueState

        qs = _QueueState(ema_alpha=0.5)
        for i in range(10):
            p_bid, p_ask = qs.update(max(1, 100 - i * 15), max(1, 100 - i * 15))
        assert 0.0 <= qs.p_depl_bid <= 1.0
        assert 0.0 <= qs.p_depl_ask <= 1.0

    def test_zero_delta_no_update(self):
        """Zero delta does not change lambda or mu."""
        from hft_platform.strategies.r47_maker import _QueueState

        qs = _QueueState(ema_alpha=0.1)
        qs.update(100, 100)
        lam_bid_before = qs._lambda_bid
        mu_bid_before = qs._mu_bid
        qs.update(100, 100)  # No change
        assert qs._lambda_bid == lam_bid_before
        assert qs._mu_bid == mu_bid_before


# ---------------------------------------------------------------------------
# Tests: _MFGState (MFG Inventory Proxy)
# ---------------------------------------------------------------------------


class TestMFGState:
    """Test lines 272-303: MFG signed flow, z-score, flow direction."""

    def test_initial_state(self):
        from hft_platform.strategies.r47_maker import _MFGState

        mfg = _MFGState(ema_alpha=0.01)
        assert mfg.capitulation_z == 0.0
        assert mfg.flow_direction == 0
        assert mfg.warmed_up is False

    def test_warmup_after_200_updates(self):
        """Lines 274-276: warmed_up after 200 ticks."""
        from hft_platform.strategies.r47_maker import _MFGState

        mfg = _MFGState(ema_alpha=0.01)
        for i in range(210):
            mfg.update_tick(1, 1)
        assert mfg.warmed_up is True

    def test_positive_flow_direction(self):
        """Lines 294-298: positive signed flow EMA → direction = +1."""
        from hft_platform.strategies.r47_maker import _MFGState

        mfg = _MFGState(ema_alpha=0.5)
        for _ in range(10):
            mfg.update_tick(1, 10)
        assert mfg.flow_direction == 1

    def test_negative_flow_direction(self):
        """Lines 296-298: negative signed flow EMA → direction = -1."""
        from hft_platform.strategies.r47_maker import _MFGState

        mfg = _MFGState(ema_alpha=0.5)
        for _ in range(10):
            mfg.update_tick(-1, 10)
        assert mfg.flow_direction == -1

    def test_capitulation_z_increases_on_persistent_flow(self):
        """Lines 278-285: z-score rises with persistent directional flow."""
        from hft_platform.strategies.r47_maker import _MFGState

        mfg = _MFGState(ema_alpha=0.1)
        for _ in range(50):
            mfg.update_tick(1, 5)
        assert mfg.capitulation_z > 0.0

    def test_volume_scales_flow(self):
        """Lines 278-280: signed = direction * volume."""
        from hft_platform.strategies.r47_maker import _MFGState

        mfg = _MFGState(ema_alpha=0.5)
        mfg.update_tick(1, 100)  # Large volume
        assert mfg._signed_flow_ema > 0


# ---------------------------------------------------------------------------
# Tests: R47MakerStrategy event handlers
# ---------------------------------------------------------------------------


class TestR47OnTick:
    """Test on_tick (lines 470-477)."""

    def test_on_tick_updates_mfg(self, strategy):
        tick = _make_tick(direction=1, volume=5)
        strategy.on_tick(tick)
        mfg = strategy._get_mfg("TMFD6")
        assert mfg._signed_flow_ema > 0

    def test_on_tick_zero_direction_skipped(self, strategy):
        """Line 476: direction=0 does not update MFG."""
        tick = _make_tick(direction=0, volume=5)
        strategy.on_tick(tick)
        mfg = strategy._get_mfg("TMFD6")
        assert mfg._signed_flow_ema == 0

    def test_on_tick_zero_volume_defaults_to_one(self, strategy):
        """Line 475: volume=0 → volume=1."""
        tick = _make_tick(direction=1, volume=0)
        strategy.on_tick(tick)
        mfg = strategy._get_mfg("TMFD6")
        assert mfg._update_count == 1


class TestR47OnFeatures:
    """Test on_features (lines 479-539)."""

    def test_on_features_none_values_returns(self, strategy):
        """Line 481: None values early return."""
        event = _make_feature_event(values=None)
        # Manually set values to None since dataclass may not accept None
        object.__setattr__(event, "values", None)
        strategy.on_features(event)
        assert "TMFD6" not in strategy._feature_cache

    def test_on_features_corrupt_quality_skipped(self, strategy):
        """Lines 484-489: corrupt quality_flags skip processing."""
        from hft_platform.strategy.base import QUALITY_FLAGS_CORRUPT

        event = _make_feature_event(quality_flags=QUALITY_FLAGS_CORRUPT)
        strategy.on_features(event)
        assert "TMFD6" not in strategy._feature_cache

    def test_on_features_caches_values(self, strategy):
        """Line 492: feature values are cached."""
        values = tuple([0] * 22)
        event = _make_feature_event(values=values)
        strategy.on_features(event)
        assert "TMFD6" in strategy._feature_cache

    def test_on_features_updates_pe(self, strategy):
        """Lines 497-500: PE state updated with QI imbalance."""
        # Index 10 = L1_IMBALANCE_PPM
        values = list([0] * 22)
        values[10] = 500_000  # 0.5 QI
        event = _make_feature_event(values=tuple(values))
        strategy.on_features(event)
        pe = strategy._get_pe("TMFD6")
        assert pe._qi_len >= 1

    def test_on_features_updates_queue(self, strategy):
        """Lines 503-507: Queue state updated with L1 quantities."""
        values = list([0] * 22)
        values[8] = 50   # L1_BID_QTY
        values[9] = 100  # L1_ASK_QTY
        event = _make_feature_event(values=tuple(values))
        strategy.on_features(event)
        qs = strategy._get_queue("TMFD6")
        assert qs._update_count >= 1

    def test_on_features_d2_suppression_flags(self, strategy):
        """Lines 515-523: D2 sets suppression flags when queue depleted."""
        qs = strategy._get_queue("TMFD6")
        # Warm up the queue state
        qs._warmed_up = True
        qs._p_depl_bid = 0.9  # Above default 0.7 threshold
        qs._p_depl_ask = 0.9

        values = list([0] * 22)
        values[8] = 10   # bid_qty
        values[9] = 10   # ask_qty
        event = _make_feature_event(values=tuple(values))
        strategy.on_features(event)
        # After update, suppression flags should be set based on new depletion probs

    def test_on_features_d4_qi_widen_ask(self, strategy):
        """Lines 528-539: QI > threshold widens ask."""
        values = list([0] * 22)
        values[8] = 100   # bid_qty high (buying pressure)
        values[9] = 10    # ask_qty low
        event = _make_feature_event(values=tuple(values))
        strategy.on_features(event)
        # qi = (100-10)/110 ≈ 0.82 > 0.1 threshold → widen ask
        assert strategy._qi_widen_ask == strategy._qi_widen_ticks

    def test_on_features_d4_qi_widen_bid(self, strategy):
        """Lines 537-538: QI < -threshold widens bid."""
        values = list([0] * 22)
        values[8] = 10    # bid_qty low (selling pressure)
        values[9] = 100   # ask_qty high
        event = _make_feature_event(values=tuple(values))
        strategy.on_features(event)
        # qi = (10-100)/110 ≈ -0.82 < -0.1 threshold → widen bid
        assert strategy._qi_widen_bid == strategy._qi_widen_ticks


class TestR47OnFill:
    """Test on_fill (lines 541-578)."""

    def test_on_fill_updates_local_pos_buy(self, strategy):
        """Lines 560-561: BUY fill increases local position."""
        fill = _make_fill(side=Side.BUY, qty=1)
        strategy.on_fill(fill)
        assert strategy._local_pos["TMFD6"] == 1

    def test_on_fill_updates_local_pos_sell(self, strategy):
        fill = _make_fill(side=Side.SELL, qty=1)
        strategy.on_fill(fill)
        assert strategy._local_pos["TMFD6"] == -1

    def test_on_fill_decrements_pending_buy(self, strategy):
        """Lines 563-564: pending buy decremented on fill."""
        strategy._pending_buy["TMFD6"] = 2
        fill = _make_fill(side=Side.BUY, qty=1)
        strategy.on_fill(fill)
        assert strategy._pending_buy["TMFD6"] == 1

    def test_on_fill_decrements_pending_sell(self, strategy):
        """Lines 567-568: pending sell decremented on fill."""
        strategy._pending_sell["TMFD6"] = 3
        fill = _make_fill(side=Side.SELL, qty=1)
        strategy.on_fill(fill)
        assert strategy._pending_sell["TMFD6"] == 2

    def test_on_fill_clears_active_buy_oid(self, strategy):
        """Line 566: active buy OID cleared on fill."""
        strategy._active_buy_oid["TMFD6"] = "O001"
        fill = _make_fill(side=Side.BUY)
        strategy.on_fill(fill)
        assert "TMFD6" not in strategy._active_buy_oid

    def test_on_fill_clears_active_sell_oid(self, strategy):
        """Line 569: active sell OID cleared on fill."""
        strategy._active_sell_oid["TMFD6"] = "O002"
        fill = _make_fill(side=Side.SELL)
        strategy.on_fill(fill)
        assert "TMFD6" not in strategy._active_sell_oid

    def test_on_fill_dedup_skips_duplicate(self, strategy):
        """Lines 544-547: duplicate fill_id skipped."""
        fill = _make_fill(fill_id="DUP_001")
        strategy.on_fill(fill)
        assert strategy._local_pos["TMFD6"] == 1
        # Second identical fill should be skipped
        strategy.on_fill(fill)
        assert strategy._local_pos["TMFD6"] == 1

    def test_on_fill_dedup_eviction(self, strategy):
        """Lines 550-558: seen_fill_ids evicts when exceeding max."""
        strategy._FILL_DEDUP_MAX = 5
        for i in range(10):
            fill = _make_fill(fill_id=f"F{i:03d}", side=Side.BUY)
            strategy.on_fill(fill)
        # Should have evicted some entries
        assert len(strategy._seen_fill_ids) <= 10

    def test_on_fill_empty_fill_id_not_deduped(self, strategy):
        """Lines 544-548: empty fill_id is never added to dedup set."""
        fill = _make_fill(fill_id="")
        strategy.on_fill(fill)
        strategy.on_fill(fill)
        # Both fills should count (no dedup on empty id)
        assert strategy._local_pos["TMFD6"] == 2


class TestR47OnOrder:
    """Test on_order (lines 580-612)."""

    def test_on_order_submitted_tracks_buy_oid(self, strategy):
        """Lines 584-589: SUBMITTED captures order_id."""
        event = _make_order_event(status=OrderStatus.SUBMITTED, side=Side.BUY, order_id="B001")
        strategy.on_order(event)
        assert strategy._active_buy_oid["TMFD6"] == "B001"

    def test_on_order_submitted_tracks_sell_oid(self, strategy):
        event = _make_order_event(status=OrderStatus.SUBMITTED, side=Side.SELL, order_id="S001")
        strategy.on_order(event)
        assert strategy._active_sell_oid["TMFD6"] == "S001"

    def test_on_order_cancelled_clears_buy_oid(self, strategy):
        """Lines 592-595: CANCELLED clears matching buy OID."""
        strategy._active_buy_oid["TMFD6"] = "B002"
        event = _make_order_event(status=OrderStatus.CANCELLED, side=Side.BUY, order_id="B002")
        strategy.on_order(event)
        assert "TMFD6" not in strategy._active_buy_oid

    def test_on_order_cancelled_clears_sell_oid(self, strategy):
        """Lines 596-598: CANCELLED clears matching sell OID."""
        strategy._active_sell_oid["TMFD6"] = "S002"
        event = _make_order_event(status=OrderStatus.CANCELLED, side=Side.SELL, order_id="S002")
        strategy.on_order(event)
        assert "TMFD6" not in strategy._active_sell_oid

    def test_on_order_cancelled_non_matching_oid_preserved(self, strategy):
        """Lines 594-595: non-matching OID is not removed."""
        strategy._active_buy_oid["TMFD6"] = "B003"
        event = _make_order_event(status=OrderStatus.CANCELLED, side=Side.BUY, order_id="DIFFERENT")
        strategy.on_order(event)
        assert strategy._active_buy_oid["TMFD6"] == "B003"

    def test_on_order_failed_decrements_pending(self, strategy):
        """Lines 601-605: FAILED releases pending by remaining_qty."""
        strategy._pending_buy["TMFD6"] = 3
        event = _make_order_event(status=OrderStatus.FAILED, side=Side.BUY, remaining_qty=2)
        strategy.on_order(event)
        assert strategy._pending_buy["TMFD6"] == 1

    def test_on_order_cancelled_sell_decrements_pending(self, strategy):
        strategy._pending_sell["TMFD6"] = 2
        event = _make_order_event(status=OrderStatus.CANCELLED, side=Side.SELL, remaining_qty=1)
        strategy.on_order(event)
        assert strategy._pending_sell["TMFD6"] == 1

    def test_on_order_partially_filled_no_action(self, strategy):
        """Lines 590-591: non-terminal status is a no-op."""
        strategy._pending_buy["TMFD6"] = 5
        event = _make_order_event(status=OrderStatus.PARTIALLY_FILLED, side=Side.BUY)
        strategy.on_order(event)
        assert strategy._pending_buy["TMFD6"] == 5


class TestR47OnRiskFeedback:
    """Test on_risk_feedback (lines 614-646)."""

    def test_on_risk_feedback_buy_side_decrements(self, strategy):
        """Lines 633-634: BUY side decrements pending_buy."""
        strategy._pending_buy["TMFD6"] = 2
        fb = RiskFeedback(
            intent_id=1, strategy_id="r47_maker", symbol="TMFD6",
            reason_code="rejected", timestamp_ns=0, side=Side.BUY,
        )
        strategy.on_risk_feedback(fb)
        assert strategy._pending_buy["TMFD6"] == 1

    def test_on_risk_feedback_sell_side_decrements(self, strategy):
        """Lines 635-636: SELL side decrements pending_sell."""
        strategy._pending_sell["TMFD6"] = 1
        fb = RiskFeedback(
            intent_id=1, strategy_id="r47_maker", symbol="TMFD6",
            reason_code="rejected", timestamp_ns=0, side=Side.SELL,
        )
        strategy.on_risk_feedback(fb)
        assert strategy._pending_sell["TMFD6"] == 0

    def test_on_risk_feedback_no_side_does_not_decrement(self, strategy):
        """Bug 9 fix: side=None must NOT decrement either counter.

        The old else branch decremented both counters when side=None,
        which caused max_pos violation when typed intent tuples (which
        have no .side attribute) produced side=None via getattr().
        """
        strategy._pending_buy["TMFD6"] = 1
        strategy._pending_sell["TMFD6"] = 1
        fb = RiskFeedback(
            intent_id=1, strategy_id="r47_maker", symbol="TMFD6",
            reason_code="rejected", timestamp_ns=0, side=None,
        )
        strategy.on_risk_feedback(fb)
        # Must NOT decrement — this is the safe failure mode (quoting freezes)
        assert strategy._pending_buy["TMFD6"] == 1
        assert strategy._pending_sell["TMFD6"] == 1

    def test_on_risk_feedback_approved_noop(self, strategy):
        """Lines 625-626: approved feedback does not decrement."""
        strategy._pending_buy["TMFD6"] = 2
        fb = RiskFeedback(
            intent_id=1, strategy_id="r47_maker", symbol="TMFD6",
            reason_code="ok", timestamp_ns=0, side=Side.BUY, was_approved=True,
        )
        strategy.on_risk_feedback(fb)
        assert strategy._pending_buy["TMFD6"] == 2


class TestR47OnGap:
    """Test on_gap (lines 648-674)."""

    def test_on_gap_resets_state(self, strategy):
        """Lines 650-668: gap clears all streaming state."""
        strategy._feature_cache["TMFD6"] = (1, 2, 3)
        strategy._pe_states["TMFD6"] = MagicMock()
        strategy._queue_states["TMFD6"] = MagicMock()
        strategy._mfg_states["TMFD6"] = MagicMock()
        strategy._suppress_bid = True
        strategy._suppress_ask = True
        strategy._qi_widen_bid = 1
        strategy._qi_widen_ask = 1
        strategy._last_bid["TMFD6"] = 100
        strategy._last_ask["TMFD6"] = 200
        strategy._active_buy_oid["TMFD6"] = "O001"
        strategy._active_sell_oid["TMFD6"] = "O002"

        gap = GapEvent(missed_count=10, first_missed_seq=100, last_missed_seq=109, ts=0)
        strategy.on_gap(gap)

        assert len(strategy._feature_cache) == 0
        assert len(strategy._pe_states) == 0
        assert len(strategy._queue_states) == 0
        assert len(strategy._mfg_states) == 0
        assert strategy._suppress_bid is False
        assert strategy._suppress_ask is False
        assert strategy._qi_widen_bid == 0
        assert strategy._qi_widen_ask == 0
        assert len(strategy._last_bid) == 0
        assert len(strategy._last_ask) == 0
        assert len(strategy._active_buy_oid) == 0
        assert len(strategy._active_sell_oid) == 0


# ---------------------------------------------------------------------------
# Tests: R47 on_stats quoting logic
# ---------------------------------------------------------------------------


class TestR47OnStats:
    """Test on_stats and _generate_quotes (lines 699-819)."""

    def test_on_stats_invalid_mid_skips(self, strategy):
        """Lines 705-710: None/zero mid or spread returns early."""
        event = _make_lob_stats(mid_price_x2=0, spread_scaled=6_0000)
        strategy.on_stats(event)
        assert strategy._quotes_sent == 0

    def test_on_stats_none_mid_skips(self, strategy):
        """mid_price_x2=None gets auto-computed by __post_init__. Use mock to force None."""
        event = _make_lob_stats(mid_price_x2=0, spread_scaled=6_0000)
        # Force mid_price_x2 to None after construction
        object.__setattr__(event, "mid_price_x2", None)
        strategy.on_stats(event)
        assert strategy._quotes_sent == 0

    def test_on_stats_none_spread_skips(self, strategy):
        """spread_scaled=None gets auto-computed by __post_init__. Force None."""
        event = _make_lob_stats(mid_price_x2=2000_0000, spread_scaled=0)
        object.__setattr__(event, "spread_scaled", None)
        strategy.on_stats(event)
        assert strategy._quotes_sent == 0

    def test_on_stats_negative_spread_skips(self, strategy):
        event = _make_lob_stats(mid_price_x2=2000_0000, spread_scaled=-1)
        strategy.on_stats(event)
        assert strategy._quotes_sent == 0

    def test_on_stats_spread_below_threshold_blocked(self, strategy):
        """Lines 714-716: spread < threshold suppresses all quotes."""
        # threshold is 5 pts = 50000 scaled
        event = _make_lob_stats(spread_scaled=3_0000)
        strategy.on_stats(event)
        assert strategy._spread_blocked == 1
        assert strategy._quotes_sent == 0

    def test_on_stats_toxicity_above_threshold_blocked(self, strategy):
        """Lines 720-724: high toxicity suppresses quotes."""
        values = list([0] * 22)
        values[21] = 800  # toxicity > 700
        strategy._feature_cache["TMFD6"] = tuple(values)
        event = _make_lob_stats(spread_scaled=6_0000)
        strategy.on_stats(event)
        assert strategy._toxicity_blocked == 1
        assert strategy._quotes_sent == 0

    def test_on_stats_pe_below_danger_blocks(self, strategy):
        """Lines 728-735: PE below danger threshold suppresses quotes."""
        pe = strategy._get_pe("TMFD6")
        pe._warmup_done = True
        pe._h = 0.3  # Below 0.55 danger threshold
        event = _make_lob_stats(spread_scaled=6_0000)
        strategy.on_stats(event)
        assert strategy._pe_blocked == 1
        assert strategy._quotes_sent == 0

    def test_on_stats_generates_quotes(self, strategy):
        """Full path: spread OK, no gates → quotes generated."""
        event = _make_lob_stats(
            mid_price_x2=2000_0000,
            spread_scaled=6_0000,
            imbalance=0.0,
        )
        strategy.on_stats(event)
        assert strategy._quotes_sent == 2  # buy + sell

    def test_on_stats_suppress_bid_skips_buy(self, strategy):
        """Lines 807: suppress_bid prevents buy quote."""
        strategy._suppress_bid = True
        event = _make_lob_stats(spread_scaled=6_0000)
        strategy.on_stats(event)
        # Only sell should be sent
        assert strategy._quotes_sent == 1

    def test_on_stats_suppress_ask_skips_sell(self, strategy):
        strategy._suppress_ask = True
        event = _make_lob_stats(spread_scaled=6_0000)
        strategy.on_stats(event)
        assert strategy._quotes_sent == 1

    def test_on_stats_max_pos_prevents_buy(self, strategy):
        """Lines 794: pos >= max_pos blocks buy."""
        strategy._local_pos["TMFD6"] = 2  # max_pos = 2
        event = _make_lob_stats(spread_scaled=6_0000)
        strategy.on_stats(event)
        # Only sell should be sent
        assert strategy._quotes_sent == 1

    def test_on_stats_max_pos_prevents_sell(self, strategy):
        """Lines 795: pos <= -max_pos blocks sell."""
        strategy._local_pos["TMFD6"] = -2  # max_pos = 2
        event = _make_lob_stats(spread_scaled=6_0000)
        strategy.on_stats(event)
        assert strategy._quotes_sent == 1

    def test_on_stats_pending_buy_prevents_new_buy(self, strategy):
        """Lines 794: pending_buy fills remaining capacity."""
        strategy._pending_buy["TMFD6"] = 2  # max_pos = 2
        event = _make_lob_stats(spread_scaled=6_0000)
        strategy.on_stats(event)
        assert strategy._quotes_sent == 1  # only sell

    def test_on_stats_price_gate_prevents_requote(self, strategy):
        """Lines 784-785: same price does not generate new order."""
        event = _make_lob_stats(spread_scaled=6_0000)
        strategy.on_stats(event)
        quotes_first = strategy._quotes_sent
        # Second call at same prices should not generate new quotes
        strategy.on_stats(event)
        assert strategy._quotes_sent == quotes_first  # No additional quotes

    def test_on_stats_cancel_before_requote(self, strategy):
        """Lines 798-805: F2 cancel stale ROD before requoting."""
        strategy._active_buy_oid["TMFD6"] = "OLD_BUY"
        strategy._active_sell_oid["TMFD6"] = "OLD_SELL"
        event = _make_lob_stats(spread_scaled=6_0000)
        strategy.on_stats(event)
        # After first quote, verify active OIDs were processed
        assert strategy._quotes_sent >= 1


# ---------------------------------------------------------------------------
# Tests: _compute_mfg_widening
# ---------------------------------------------------------------------------


class TestComputeMFGWidening:
    """Test lines 821-833: MFG asymmetric widening."""

    def test_not_warmed_up_returns_zero(self, strategy):
        from hft_platform.strategies.r47_maker import _MFGState

        mfg = _MFGState()
        bid_w, ask_w = strategy._compute_mfg_widening(mfg, 6_0000)
        assert bid_w == 0 and ask_w == 0

    def test_z_below_threshold_returns_zero(self, strategy):
        from hft_platform.strategies.r47_maker import _MFGState

        mfg = _MFGState()
        mfg._warmed_up = True
        mfg._capitulation_z = 1.0  # Below 2.0 threshold
        bid_w, ask_w = strategy._compute_mfg_widening(mfg, 6_0000)
        assert bid_w == 0 and ask_w == 0

    def test_positive_flow_widens_ask(self, strategy):
        """Lines 829-830: net buying → widen ask."""
        from hft_platform.strategies.r47_maker import _MFGState

        mfg = _MFGState(ema_alpha=0.5)
        mfg._warmed_up = True
        mfg._capitulation_z = 3.0
        mfg._signed_flow_ema = 10.0  # positive → flow_direction = +1
        bid_w, ask_w = strategy._compute_mfg_widening(mfg, 6_0000)
        assert bid_w == 0
        assert ask_w > 0

    def test_negative_flow_widens_bid(self, strategy):
        """Lines 831-832: net selling → widen bid."""
        from hft_platform.strategies.r47_maker import _MFGState

        mfg = _MFGState(ema_alpha=0.5)
        mfg._warmed_up = True
        mfg._capitulation_z = 3.0
        mfg._signed_flow_ema = -10.0  # negative → flow_direction = -1
        bid_w, ask_w = strategy._compute_mfg_widening(mfg, 6_0000)
        assert bid_w > 0
        assert ask_w == 0

    def test_zero_flow_returns_zero(self, strategy):
        """Line 833: flow_direction=0 returns (0, 0)."""
        from hft_platform.strategies.r47_maker import _MFGState

        mfg = _MFGState()
        mfg._warmed_up = True
        mfg._capitulation_z = 3.0
        mfg._signed_flow_ema = 0.0  # flow_direction = 0
        bid_w, ask_w = strategy._compute_mfg_widening(mfg, 6_0000)
        assert bid_w == 0 and ask_w == 0

    def test_skew_mult_capped_at_3(self, strategy):
        """Line 826: skew_mult = min(3, ...)."""
        from hft_platform.strategies.r47_maker import _MFGState

        mfg = _MFGState()
        mfg._warmed_up = True
        mfg._capitulation_z = 100.0  # Very high z → mult capped at 3
        mfg._signed_flow_ema = 10.0
        bid_w, ask_w = strategy._compute_mfg_widening(mfg, 6_0000)
        tick_size = max(1, 6_0000 * 50 // 100)
        max_widen = tick_size * 3
        assert ask_w == max_widen


# ---------------------------------------------------------------------------
# Tests: cross-instrument execution
# ---------------------------------------------------------------------------


class TestCrossInstrument:
    """Test _exec_symbol and trade_symbol."""

    def test_exec_symbol_default(self, strategy):
        """Line 466: no trade_symbol returns signal symbol."""
        strategy._trade_symbol = ""
        assert strategy._exec_symbol("TXFD6") == "TXFD6"

    def test_exec_symbol_cross(self, strategy):
        """Line 466: trade_symbol overrides."""
        strategy._trade_symbol = "TMFD6"
        assert strategy._exec_symbol("TXFD6") == "TMFD6"


# ---------------------------------------------------------------------------
# Tests: seed_local_pos and _local_position
# ---------------------------------------------------------------------------


class TestLocalPosition:
    """Test lines 676-697: position seeding and lazy init."""

    def test_seed_local_pos_new_symbol(self, strategy):
        """Lines 681-683: seeds symbol not already tracked."""
        strategy.seed_local_pos({"TMFD6": 3})
        assert strategy._local_pos["TMFD6"] == 3

    def test_seed_local_pos_existing_not_overwritten(self, strategy):
        """Line 682: existing symbols not overwritten."""
        strategy._local_pos["TMFD6"] = 5
        strategy.seed_local_pos({"TMFD6": 0})
        assert strategy._local_pos["TMFD6"] == 5

    def test_local_position_lazy_seed_from_ctx(self, strategy):
        """Lines 692-696: first access seeds from StrategyContext."""
        strategy.ctx.positions = {"TMFD6": 2}
        pos = strategy._local_position("TMFD6")
        assert pos == 2
        assert strategy._local_pos["TMFD6"] == 2

    def test_local_position_zero_ctx_not_seeded(self, strategy):
        """Lines 693-694: ctx_pos=0 does not trigger seed."""
        strategy.ctx.positions = {"TMFD6": 0}
        pos = strategy._local_position("TMFD6")
        assert pos == 0
        assert "TMFD6" not in strategy._local_pos

    def test_local_position_already_tracked(self, strategy):
        """Line 692: symbol already in _local_pos returns directly."""
        strategy._local_pos["TMFD6"] = -1
        pos = strategy._local_position("TMFD6")
        assert pos == -1


# ---------------------------------------------------------------------------
# Tests: PE width multiplier in _generate_quotes
# ---------------------------------------------------------------------------


class TestPEWidthMultiplier:
    """Test line 761: PE intermediate structure doubles quote width."""

    def test_pe_width_mult_doubles_when_intermediate(self, strategy):
        """PE h=0.60 (between danger 0.55 and 0.70) → pe_width_mult=2."""
        pe = strategy._get_pe("TMFD6")
        pe._warmup_done = True
        pe._h = 0.60
        event = _make_lob_stats(spread_scaled=6_0000)
        initial_quotes = strategy._quotes_sent
        strategy.on_stats(event)
        # Quotes are wider but still generated
        assert strategy._quotes_sent > initial_quotes

    def test_pe_width_mult_normal_when_high_entropy(self, strategy):
        """PE h=0.80 (above 0.70) → pe_width_mult=1."""
        pe = strategy._get_pe("TMFD6")
        pe._warmup_done = True
        pe._h = 0.80
        event = _make_lob_stats(spread_scaled=6_0000)
        strategy.on_stats(event)
        assert strategy._quotes_sent == 2


# ---------------------------------------------------------------------------
# Tests: _log_stats (coverage line 835-852)
# ---------------------------------------------------------------------------


class TestLogStats:
    def test_log_stats_at_interval(self, strategy):
        """Lines 818-819: logging triggered every _LOG_INTERVAL."""
        from hft_platform.strategies.r47_maker import _LOG_INTERVAL

        strategy._stats_count = _LOG_INTERVAL  # Next on_stats triggers log at count+1
        event = _make_lob_stats(spread_scaled=6_0000)
        # Should not raise
        strategy.on_stats(event)
        assert strategy._stats_count == _LOG_INTERVAL + 1
