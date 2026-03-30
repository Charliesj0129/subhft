"""Unit tests for ExecutionOptimizer limit/market decision logic."""

from __future__ import annotations

from hft_platform.execution.execution_optimizer import (
    ExecutionOptimizer,
    OrderType,
)
from hft_platform.execution.regime_classifier import Regime

# --- 1. Default construction ---


def test_default_construction() -> None:
    opt = ExecutionOptimizer()
    assert opt.enabled is True
    assert opt.is_pending is False


def test_custom_construction() -> None:
    opt = ExecutionOptimizer(
        spread_threshold_pts=3,
        fill_score_threshold=2.0,
        limit_timeout_ns=5_000_000_000,
        enabled=False,
    )
    assert opt.enabled is False
    assert opt.is_pending is False


# --- 2. Disabled always returns MARKET ---


def test_disabled_always_market() -> None:
    opt = ExecutionOptimizer(enabled=False)
    result = opt.decide(
        spread_pts=5,
        near_depth=5,
        opp_depth=20,
        imbalance_ppm=0,
        side=+1,
        ts_ns=1_000_000_000,
    )
    assert result == OrderType.MARKET
    assert opt.is_pending is False


# --- 3. Spread < threshold -> MARKET ---


def test_narrow_spread_returns_market() -> None:
    opt = ExecutionOptimizer(spread_threshold_pts=2)
    result = opt.decide(
        spread_pts=1,
        near_depth=5,
        opp_depth=20,
        imbalance_ppm=0,
        side=+1,
        ts_ns=1_000_000_000,
    )
    assert result == OrderType.MARKET


def test_exact_spread_threshold_returns_limit_if_score_favorable() -> None:
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.5)
    result = opt.decide(
        spread_pts=2,
        near_depth=5,
        opp_depth=10,
        imbalance_ppm=0,
        side=+1,
        ts_ns=1_000_000_000,
    )
    # fill_score = 10/5 = 2.0 >= 1.5 -> LIMIT
    assert result == OrderType.LIMIT


# --- 4. Spread >= threshold + favorable fill score -> LIMIT ---


def test_favorable_fill_score_returns_limit() -> None:
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.5)
    result = opt.decide(
        spread_pts=3,
        near_depth=10,
        opp_depth=20,
        imbalance_ppm=100_000,
        side=+1,
        ts_ns=1_000_000_000,
    )
    # fill_score = 20/10 = 2.0 >= 1.5 -> LIMIT
    assert result == OrderType.LIMIT
    assert opt.is_pending is True


# --- 5. Spread >= threshold + unfavorable fill score -> MARKET ---


def test_unfavorable_fill_score_returns_market() -> None:
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.5)
    result = opt.decide(
        spread_pts=3,
        near_depth=20,
        opp_depth=10,
        imbalance_ppm=0,
        side=-1,
        ts_ns=1_000_000_000,
    )
    # fill_score = 10/20 = 0.5 < 1.5 -> MARKET
    assert result == OrderType.MARKET
    assert opt.is_pending is False


def test_borderline_fill_score_below_threshold() -> None:
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.5)
    result = opt.decide(
        spread_pts=3,
        near_depth=10,
        opp_depth=14,
        imbalance_ppm=0,
        side=+1,
        ts_ns=1_000_000_000,
    )
    # fill_score = 14/10 = 1.4 < 1.5 -> MARKET
    assert result == OrderType.MARKET


# --- 6. Urgent always MARKET ---


def test_urgent_always_market() -> None:
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.0)
    result = opt.decide(
        spread_pts=5,
        near_depth=5,
        opp_depth=20,
        imbalance_ppm=0,
        side=+1,
        ts_ns=1_000_000_000,
        urgent=True,
    )
    assert result == OrderType.MARKET


def test_urgent_overrides_favorable_conditions() -> None:
    opt = ExecutionOptimizer(spread_threshold_pts=1, fill_score_threshold=0.5)
    # Everything screams LIMIT, but urgent overrides
    result = opt.decide(
        spread_pts=10,
        near_depth=1,
        opp_depth=100,
        imbalance_ppm=500_000,
        side=+1,
        ts_ns=1_000_000_000,
        urgent=True,
    )
    assert result == OrderType.MARKET


# --- 7. Timeout triggers cancel ---


def test_timeout_triggers_after_elapsed() -> None:
    opt = ExecutionOptimizer(
        spread_threshold_pts=2,
        fill_score_threshold=1.0,
        limit_timeout_ns=3_000_000_000,
    )
    # Decide LIMIT
    result = opt.decide(
        spread_pts=3,
        near_depth=5,
        opp_depth=10,
        imbalance_ppm=0,
        side=+1,
        ts_ns=1_000_000_000,
    )
    assert result == OrderType.LIMIT
    assert opt.is_pending is True

    # Before timeout
    assert opt.check_timeout(2_000_000_000) is False

    # Exactly at timeout
    assert opt.check_timeout(4_000_000_000) is True


def test_timeout_not_triggered_when_idle() -> None:
    opt = ExecutionOptimizer(limit_timeout_ns=1_000_000_000)
    # No pending decision
    assert opt.check_timeout(999_999_999_999) is False


def test_timeout_after_threshold() -> None:
    opt = ExecutionOptimizer(
        spread_threshold_pts=2,
        fill_score_threshold=1.0,
        limit_timeout_ns=2_000_000_000,
    )
    opt.decide(
        spread_pts=3,
        near_depth=5,
        opp_depth=10,
        imbalance_ppm=0,
        side=+1,
        ts_ns=5_000_000_000,
    )
    # elapsed = 8B - 5B = 3B >= 2B -> timeout
    assert opt.check_timeout(8_000_000_000) is True


# --- 8. on_fill resets state ---


def test_on_fill_resets_pending_state() -> None:
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.0)
    opt.decide(
        spread_pts=3,
        near_depth=5,
        opp_depth=10,
        imbalance_ppm=0,
        side=+1,
        ts_ns=1_000_000_000,
    )
    assert opt.is_pending is True

    opt.on_fill()
    assert opt.is_pending is False


def test_on_fill_when_idle_is_noop() -> None:
    opt = ExecutionOptimizer()
    opt.on_fill()  # should not raise
    assert opt.is_pending is False


# --- 9. on_cancel resets state ---


def test_on_cancel_resets_pending_state() -> None:
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.0)
    opt.decide(
        spread_pts=3,
        near_depth=5,
        opp_depth=10,
        imbalance_ppm=0,
        side=-1,
        ts_ns=1_000_000_000,
    )
    assert opt.is_pending is True

    opt.on_cancel()
    assert opt.is_pending is False


def test_on_cancel_when_idle_is_noop() -> None:
    opt = ExecutionOptimizer()
    opt.on_cancel()  # should not raise
    assert opt.is_pending is False


# --- 10. Edge cases: zero depth, negative depth ---


def test_zero_near_depth_uses_clamp() -> None:
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.5)
    # near_depth=0 -> clamped to 1, fill_score = 10/1 = 10.0 >= 1.5
    result = opt.decide(
        spread_pts=3,
        near_depth=0,
        opp_depth=10,
        imbalance_ppm=0,
        side=+1,
        ts_ns=1_000_000_000,
    )
    assert result == OrderType.LIMIT


def test_zero_opp_depth_returns_market() -> None:
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.5)
    # opp_depth=0 -> fill_score = 0/10 = 0.0 < 1.5
    result = opt.decide(
        spread_pts=3,
        near_depth=10,
        opp_depth=0,
        imbalance_ppm=0,
        side=+1,
        ts_ns=1_000_000_000,
    )
    assert result == OrderType.MARKET


def test_both_depths_zero_returns_market() -> None:
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.5)
    # near=0 clamped to 1, opp=0 -> fill_score = 0/1 = 0.0 < 1.5
    result = opt.decide(
        spread_pts=3,
        near_depth=0,
        opp_depth=0,
        imbalance_ppm=0,
        side=+1,
        ts_ns=1_000_000_000,
    )
    assert result == OrderType.MARKET


def test_negative_near_depth_clamped() -> None:
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.5)
    # near_depth=-5 -> clamped to 1, fill_score = 10/1 = 10.0
    result = opt.decide(
        spread_pts=3,
        near_depth=-5,
        opp_depth=10,
        imbalance_ppm=0,
        side=+1,
        ts_ns=1_000_000_000,
    )
    assert result == OrderType.LIMIT


def test_negative_opp_depth_returns_market() -> None:
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.5)
    # Negative opp_depth -> fill_score negative < 1.5
    result = opt.decide(
        spread_pts=3,
        near_depth=10,
        opp_depth=-5,
        imbalance_ppm=0,
        side=+1,
        ts_ns=1_000_000_000,
    )
    assert result == OrderType.MARKET


# --- OrderType enum ---


def test_order_type_values() -> None:
    assert OrderType.MARKET == 0
    assert OrderType.LIMIT == 1
    assert int(OrderType.MARKET) == 0
    assert int(OrderType.LIMIT) == 1


# --- Enable/disable toggle ---


def test_disable_clears_pending() -> None:
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.0)
    opt.decide(
        spread_pts=3,
        near_depth=5,
        opp_depth=10,
        imbalance_ppm=0,
        side=+1,
        ts_ns=1_000_000_000,
    )
    assert opt.is_pending is True
    opt.enabled = False
    assert opt.is_pending is False


def test_enable_toggle() -> None:
    opt = ExecutionOptimizer(enabled=False)
    assert (
        opt.decide(
            spread_pts=5,
            near_depth=1,
            opp_depth=100,
            imbalance_ppm=0,
            side=+1,
            ts_ns=0,
        )
        == OrderType.MARKET
    )

    opt.enabled = True
    assert (
        opt.decide(
            spread_pts=5,
            near_depth=1,
            opp_depth=100,
            imbalance_ppm=0,
            side=+1,
            ts_ns=0,
        )
        == OrderType.LIMIT
    )


# --- Side handling ---


def test_sell_side_limit_decision() -> None:
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.0)
    result = opt.decide(
        spread_pts=3,
        near_depth=5,
        opp_depth=10,
        imbalance_ppm=-200_000,
        side=-1,
        ts_ns=1_000_000_000,
    )
    assert result == OrderType.LIMIT


def test_consecutive_decisions_reset_pending_start() -> None:
    opt = ExecutionOptimizer(
        spread_threshold_pts=2,
        fill_score_threshold=1.0,
        limit_timeout_ns=3_000_000_000,
    )
    # First LIMIT decision at t=1s
    opt.decide(
        spread_pts=3,
        near_depth=5,
        opp_depth=10,
        imbalance_ppm=0,
        side=+1,
        ts_ns=1_000_000_000,
    )
    # Second LIMIT decision at t=2s (resets pending start)
    opt.decide(
        spread_pts=3,
        near_depth=5,
        opp_depth=10,
        imbalance_ppm=0,
        side=+1,
        ts_ns=2_000_000_000,
    )
    # Timeout should be based on second decision (t=2s + 3s = 5s)
    assert opt.check_timeout(4_500_000_000) is False
    assert opt.check_timeout(5_000_000_000) is True


# --- Regime-aware execution (Direction C, R24) ---


def test_adverse_regime_forces_market() -> None:
    """ADVERSE regime should force MARKET even when heuristic says LIMIT."""
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.0)
    # Without regime, this would return LIMIT
    result_neutral = opt.decide(
        spread_pts=5, near_depth=1, opp_depth=20,
        imbalance_ppm=0, side=+1, ts_ns=0,
    )
    assert result_neutral == OrderType.LIMIT

    opt.on_fill()  # reset state

    # With ADVERSE, forced to MARKET
    result_adverse = opt.decide(
        spread_pts=5, near_depth=1, opp_depth=20,
        imbalance_ppm=0, side=+1, ts_ns=0,
        regime=Regime.ADVERSE,
    )
    assert result_adverse == OrderType.MARKET


def test_favorable_regime_relaxes_spread_threshold() -> None:
    """FAVORABLE regime should lower spread threshold by 1."""
    opt = ExecutionOptimizer(spread_threshold_pts=3, fill_score_threshold=1.0)
    # spread=2 is below threshold (3) in NEUTRAL
    result_neutral = opt.decide(
        spread_pts=2, near_depth=1, opp_depth=20,
        imbalance_ppm=0, side=+1, ts_ns=0,
    )
    assert result_neutral == OrderType.MARKET

    # In FAVORABLE, threshold becomes 2, so spread=2 passes
    result_favorable = opt.decide(
        spread_pts=2, near_depth=1, opp_depth=20,
        imbalance_ppm=0, side=+1, ts_ns=0,
        regime=Regime.FAVORABLE,
    )
    assert result_favorable == OrderType.LIMIT


def test_favorable_regime_relaxes_fill_score() -> None:
    """FAVORABLE regime should lower fill score threshold by 0.5."""
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.5)
    # near=5, opp=7 -> fill_score = 7/5 = 1.4, below 1.5 threshold
    result_neutral = opt.decide(
        spread_pts=3, near_depth=5, opp_depth=7,
        imbalance_ppm=0, side=+1, ts_ns=0,
    )
    assert result_neutral == OrderType.MARKET

    # In FAVORABLE, threshold becomes 1.0, so 1.4 passes
    result_favorable = opt.decide(
        spread_pts=3, near_depth=5, opp_depth=7,
        imbalance_ppm=0, side=+1, ts_ns=0,
        regime=Regime.FAVORABLE,
    )
    assert result_favorable == OrderType.LIMIT


def test_neutral_regime_uses_original_heuristic() -> None:
    """NEUTRAL regime should behave identically to original heuristic."""
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.5)
    result = opt.decide(
        spread_pts=3, near_depth=5, opp_depth=10,
        imbalance_ppm=0, side=+1, ts_ns=0,
        regime=Regime.NEUTRAL,
    )
    # fill_score = 10/5 = 2.0 >= 1.5 -> LIMIT
    assert result == OrderType.LIMIT


def test_adverse_overrides_urgent() -> None:
    """Both ADVERSE and urgent force MARKET (no conflict)."""
    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.0)
    result = opt.decide(
        spread_pts=5, near_depth=1, opp_depth=20,
        imbalance_ppm=0, side=+1, ts_ns=0,
        urgent=True, regime=Regime.ADVERSE,
    )
    assert result == OrderType.MARKET
