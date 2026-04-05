"""Unit tests for R25 Large Order Flow Detection alpha (Phase A).

Tests cover:
  - LargeOrderFlowAlpha: sweep detection, OFI confirmation, signal generation
  - AlphaProtocol conformance
  - Edge cases: warmup, cooldown, direction reversal, stale sweep decay
"""

from __future__ import annotations

from research.alphas.r25_large_order_flow.impl import (
    _FE_MID_PRICE_X2,
    _FE_OFI_EMA5S,
    _FE_SPREAD_SCALED,
    _MANIFEST,
    LargeOrderFlowAlpha,
)
from research.registry.schemas import AlphaProtocol

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TICK = 10_000  # 1 point in scaled-int (x10000)
_S = 1_000_000_000  # 1s in nanoseconds


def _make_features(
    mid_x2: int = 400 * _TICK,
    spread: int = 1 * _TICK,
    ofi_ema5s: int = 0,
    ofi_ema30s: int = 0,
) -> tuple[int, ...]:
    """Build a minimal 27-slot feature tuple for lob_shared_v3.

    Only fills the slots the alpha actually reads; rest are zero.
    """
    feat = [0] * 27
    feat[_FE_MID_PRICE_X2] = mid_x2
    feat[_FE_SPREAD_SCALED] = spread
    feat[_FE_OFI_EMA5S] = ofi_ema5s
    feat[23] = ofi_ema30s  # ofi_l1_ema30s
    return tuple(feat)


# ---------------------------------------------------------------------------
# Protocol & Manifest Tests
# ---------------------------------------------------------------------------

class TestManifest:
    def test_alpha_id(self) -> None:
        assert _MANIFEST.alpha_id == "r25_large_order_flow"

    def test_paper_refs_count(self) -> None:
        assert len(_MANIFEST.paper_refs) == 4

    def test_complexity(self) -> None:
        assert _MANIFEST.complexity == "O(1)"

    def test_latency_profile_set(self) -> None:
        assert _MANIFEST.latency_profile is not None

    def test_feature_set_version(self) -> None:
        assert _MANIFEST.feature_set_version == "lob_shared_v3"

    def test_conforms_to_alpha_protocol(self) -> None:
        alpha = LargeOrderFlowAlpha()
        assert isinstance(alpha, AlphaProtocol)


# ---------------------------------------------------------------------------
# Warmup Tests
# ---------------------------------------------------------------------------

class TestWarmup:
    def test_returns_zero_during_warmup(self) -> None:
        alpha = LargeOrderFlowAlpha()
        base_mid = 400 * _TICK
        base_ts = 1_000 * _S

        for i in range(60):
            signal = alpha.update(
                features=_make_features(mid_x2=base_mid),
                ts_ns=base_ts + i * _S,
            )
            assert signal == 0.0

    def test_no_features_returns_zero(self) -> None:
        alpha = LargeOrderFlowAlpha()
        assert alpha.update() == 0.0

    def test_short_features_returns_zero(self) -> None:
        alpha = LargeOrderFlowAlpha()
        assert alpha.update(features=(1, 2, 3)) == 0.0


# ---------------------------------------------------------------------------
# Sweep Detection Tests
# ---------------------------------------------------------------------------

class TestSweepDetection:
    def _warmup(self, alpha: LargeOrderFlowAlpha, mid_x2: int, base_ts: int) -> int:
        """Feed warmup events and return next timestamp."""
        for i in range(61):
            alpha.update(features=_make_features(mid_x2=mid_x2), ts_ns=base_ts + i * _S)
        return base_ts + 61 * _S

    def test_buy_sweep_with_ofi_confirms(self) -> None:
        alpha = LargeOrderFlowAlpha(sweep_min_ticks=2, ofi_threshold=50)
        base_mid = 400 * _TICK
        base_ts = 1_000 * _S

        ts = self._warmup(alpha, base_mid, base_ts)

        # Price moves up 1 tick
        ts += _S
        alpha.update(features=_make_features(mid_x2=base_mid + 1 * _TICK, ofi_ema5s=80), ts_ns=ts)

        # Price moves up another tick (now 2 ticks cumulative) with strong OFI
        ts += _S
        signal = alpha.update(
            features=_make_features(mid_x2=base_mid + 2 * _TICK, ofi_ema5s=100),
            ts_ns=ts,
        )

        assert signal == 1.0  # buy signal confirmed

    def test_sell_sweep_with_ofi_confirms(self) -> None:
        alpha = LargeOrderFlowAlpha(sweep_min_ticks=2, ofi_threshold=50)
        base_mid = 400 * _TICK
        base_ts = 1_000 * _S

        ts = self._warmup(alpha, base_mid, base_ts)

        ts += _S
        alpha.update(features=_make_features(mid_x2=base_mid - 1 * _TICK, ofi_ema5s=-80), ts_ns=ts)

        ts += _S
        signal = alpha.update(
            features=_make_features(mid_x2=base_mid - 2 * _TICK, ofi_ema5s=-100),
            ts_ns=ts,
        )

        assert signal == -1.0  # sell signal confirmed

    def test_sweep_without_ofi_no_signal(self) -> None:
        alpha = LargeOrderFlowAlpha(sweep_min_ticks=2, ofi_threshold=50)
        base_mid = 400 * _TICK
        base_ts = 1_000 * _S

        ts = self._warmup(alpha, base_mid, base_ts)

        # Price moves up 2 ticks but OFI is weak (below threshold)
        ts += _S
        alpha.update(features=_make_features(mid_x2=base_mid + 1 * _TICK, ofi_ema5s=10), ts_ns=ts)

        ts += _S
        signal = alpha.update(
            features=_make_features(mid_x2=base_mid + 2 * _TICK, ofi_ema5s=20),
            ts_ns=ts,
        )

        assert signal == 0.0  # OFI too weak

    def test_sweep_with_opposite_ofi_no_signal(self) -> None:
        alpha = LargeOrderFlowAlpha(sweep_min_ticks=2, ofi_threshold=50)
        base_mid = 400 * _TICK
        base_ts = 1_000 * _S

        ts = self._warmup(alpha, base_mid, base_ts)

        # Price moves up but OFI is negative (opposite sign)
        ts += _S
        alpha.update(features=_make_features(mid_x2=base_mid + 1 * _TICK, ofi_ema5s=-80), ts_ns=ts)

        ts += _S
        signal = alpha.update(
            features=_make_features(mid_x2=base_mid + 2 * _TICK, ofi_ema5s=-100),
            ts_ns=ts,
        )

        assert signal == 0.0  # OFI wrong direction

    def test_insufficient_sweep_no_signal(self) -> None:
        alpha = LargeOrderFlowAlpha(sweep_min_ticks=2, ofi_threshold=50)
        base_mid = 400 * _TICK
        base_ts = 1_000 * _S

        ts = self._warmup(alpha, base_mid, base_ts)

        # Only 1 tick move (< 2 tick threshold)
        ts += _S
        signal = alpha.update(
            features=_make_features(mid_x2=base_mid + 1 * _TICK // 2, ofi_ema5s=100),
            ts_ns=ts,
        )

        assert signal == 0.0  # not enough ticks

    def test_direction_reversal_resets_sweep(self) -> None:
        alpha = LargeOrderFlowAlpha(sweep_min_ticks=2, ofi_threshold=50)
        base_mid = 400 * _TICK
        base_ts = 1_000 * _S

        ts = self._warmup(alpha, base_mid, base_ts)

        # Move up 1 tick
        ts += _S
        alpha.update(features=_make_features(mid_x2=base_mid + 1 * _TICK, ofi_ema5s=80), ts_ns=ts)

        # Reverse: move down 1 tick (resets accumulator)
        ts += _S
        alpha.update(features=_make_features(mid_x2=base_mid, ofi_ema5s=-50), ts_ns=ts)

        # Now up again — only 1 tick from new anchor, not enough
        ts += _S
        signal = alpha.update(
            features=_make_features(mid_x2=base_mid + 1 * _TICK, ofi_ema5s=80),
            ts_ns=ts,
        )

        assert signal == 0.0  # reset by direction change

    def test_stale_sweep_decays(self) -> None:
        alpha = LargeOrderFlowAlpha(sweep_min_ticks=2, sweep_max_events=3, ofi_threshold=50)
        base_mid = 400 * _TICK
        base_ts = 1_000 * _S

        ts = self._warmup(alpha, base_mid, base_ts)

        # Move up 1 tick
        ts += _S
        alpha.update(features=_make_features(mid_x2=base_mid + 1 * _TICK, ofi_ema5s=80), ts_ns=ts)

        # No price change for 4 events (> sweep_max_events=3)
        for _ in range(4):
            ts += _S
            alpha.update(features=_make_features(mid_x2=base_mid + 1 * _TICK, ofi_ema5s=80), ts_ns=ts)

        assert alpha.sweep_ticks == 0  # sweep expired


# ---------------------------------------------------------------------------
# Cooldown Tests
# ---------------------------------------------------------------------------

class TestCooldown:
    def test_cooldown_prevents_rapid_signals(self) -> None:
        alpha = LargeOrderFlowAlpha(sweep_min_ticks=2, ofi_threshold=50)
        base_mid = 400 * _TICK
        base_ts = 1_000 * _S

        # Manually set last signal time
        alpha._last_signal_ts = base_ts
        alpha._tick_count = 100  # past warmup

        # Try to trigger within cooldown (5s < 10s default)
        ts = base_ts + 5 * _S
        alpha._prev_mid_x2 = base_mid
        alpha.update(features=_make_features(mid_x2=base_mid + 1 * _TICK, ofi_ema5s=100), ts_ns=ts)

        ts += _S
        signal = alpha.update(
            features=_make_features(mid_x2=base_mid + 2 * _TICK, ofi_ema5s=100),
            ts_ns=ts,
        )

        assert signal == 0.0  # blocked by cooldown

    def test_signal_after_cooldown(self) -> None:
        alpha = LargeOrderFlowAlpha(sweep_min_ticks=2, ofi_threshold=50)
        base_mid = 400 * _TICK
        base_ts = 1_000 * _S

        alpha._last_signal_ts = base_ts
        alpha._tick_count = 100
        alpha._prev_mid_x2 = base_mid

        # After cooldown (15s > 10s)
        ts = base_ts + 15 * _S
        alpha.update(features=_make_features(mid_x2=base_mid + 1 * _TICK, ofi_ema5s=100), ts_ns=ts)

        ts += _S
        signal = alpha.update(
            features=_make_features(mid_x2=base_mid + 2 * _TICK, ofi_ema5s=100),
            ts_ns=ts,
        )

        assert signal == 1.0  # allowed after cooldown


# ---------------------------------------------------------------------------
# State Management Tests
# ---------------------------------------------------------------------------

class TestStateManagement:
    def test_reset_clears_all_state(self) -> None:
        alpha = LargeOrderFlowAlpha()

        # Feed some data
        for i in range(10):
            alpha.update(
                features=_make_features(mid_x2=400 * _TICK + i * _TICK),
                ts_ns=1_000 * _S + i * _S,
            )

        alpha.reset()
        assert alpha.signal == 0.0
        assert alpha._tick_count == 0
        assert alpha.sweep_direction == 0
        assert alpha.sweep_ticks == 0

    def test_get_signal_matches_signal_property(self) -> None:
        alpha = LargeOrderFlowAlpha()
        assert alpha.get_signal() == alpha.signal == 0.0

    def test_signal_resets_sweep_accumulator(self) -> None:
        alpha = LargeOrderFlowAlpha(sweep_min_ticks=2, ofi_threshold=50)
        base_mid = 400 * _TICK
        base_ts = 1_000 * _S

        # Warmup
        for i in range(61):
            alpha.update(features=_make_features(mid_x2=base_mid), ts_ns=base_ts + i * _S)

        ts = base_ts + 61 * _S

        # Trigger signal
        ts += _S
        alpha.update(features=_make_features(mid_x2=base_mid + 1 * _TICK, ofi_ema5s=80), ts_ns=ts)
        ts += _S
        signal = alpha.update(
            features=_make_features(mid_x2=base_mid + 2 * _TICK, ofi_ema5s=100),
            ts_ns=ts,
        )
        assert signal == 1.0

        # After signal, sweep accumulator should be reset
        assert alpha.sweep_direction == 0
        assert alpha.sweep_ticks == 0
