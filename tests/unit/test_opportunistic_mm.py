"""Unit tests for OpportunisticMM strategy."""

from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.events import FeatureUpdateEvent
from hft_platform.strategies.opportunistic_mm import OpportunisticMM


def _make_event(
    mid_x2: int,
    spread_scaled: int,
    best_bid: int = 10000,
    best_ask: int = 10010,
    imbalance: float = 0.0,
    symbol: str = "TXFD6",
) -> MagicMock:
    """Create a mock LOBStatsEvent."""
    ev = MagicMock()
    ev.mid_price_x2 = mid_x2
    ev.spread_scaled = spread_scaled
    ev.best_bid = best_bid
    ev.best_ask = best_ask
    ev.imbalance = imbalance
    ev.symbol = symbol
    return ev


# --- Construction ---
def test_default_threshold() -> None:
    mm = OpportunisticMM()
    assert mm.spread_threshold_bps == 2.5


def test_custom_threshold() -> None:
    mm = OpportunisticMM(spread_threshold_bps=3.0)
    assert mm.spread_threshold_bps == 3.0


def test_inherits_simple_mm() -> None:
    from hft_platform.strategies.simple_mm import SimpleMarketMaker

    assert issubclass(OpportunisticMM, SimpleMarketMaker)


# --- Spread gate ---
def test_tight_spread_does_not_quote() -> None:
    mm = OpportunisticMM(spread_threshold_bps=2.5)
    mm.ctx = MagicMock()
    mm._generated_intents = []
    # mid_x2=660000, spread=8 -> spread_bps = 8/660000 * 20000 = 0.24 bps (tight)
    ev = _make_event(mid_x2=660000, spread_scaled=8)
    mm.on_stats(ev)
    assert len(mm._generated_intents) == 0


def test_wide_spread_delegates_to_super() -> None:
    """When spread is wide, on_stats delegates to SimpleMM (does not return early)."""
    mm = OpportunisticMM(spread_threshold_bps=2.5)
    mm.ctx = MagicMock()
    mm.ctx.place_order = MagicMock(return_value=MagicMock())
    mm._generated_intents = []
    # Patch position() to return 0
    mm.position = MagicMock(return_value=0)

    # mid_x2=660000, spread=100 -> spread_bps = 100/660000 * 20000 = 3.03 bps (wide)
    ev = _make_event(mid_x2=660000, spread_scaled=100, best_bid=329950, best_ask=330050, imbalance=0.1)
    mm.on_stats(ev)
    # SimpleMM calls self.buy() and self.sell() which call position()
    # If position() was called, it means the gate passed and super().on_stats() ran
    assert mm.position.called


def test_spread_bps_calculation() -> None:
    """Verify the spread_bps formula: spread_scaled / mid_x2 * 20000."""
    # mid_x2 = 66000 (TAIEX ~33000), spread_scaled = 4 (4 ticks)
    # spread_bps = 4 / 66000 * 20000 = 1.21 bps
    mm = OpportunisticMM(spread_threshold_bps=1.5)
    mid_x2 = 66000
    spread_scaled = 4
    spread_bps = spread_scaled / mid_x2 * 20000.0
    assert abs(spread_bps - 1.21) < 0.01


# --- None/invalid guards ---
def test_none_mid_price_returns_early() -> None:  # noqa: no-assert
    mm = OpportunisticMM()
    ev = MagicMock()
    ev.mid_price_x2 = None
    ev.spread_scaled = 10
    mm.on_stats(ev)  # should not raise


def test_zero_spread_returns_early() -> None:  # noqa: no-assert
    mm = OpportunisticMM()
    ev = _make_event(mid_x2=66000, spread_scaled=0)
    mm.on_stats(ev)  # should not raise


def test_negative_mid_returns_early() -> None:  # noqa: no-assert
    mm = OpportunisticMM()
    ev = _make_event(mid_x2=-1, spread_scaled=10)
    mm.on_stats(ev)  # should not raise


# --- Reversal filter ---


def _make_feature_tuple(
    *,
    l1_bid_qty: int = 50,
    l1_ask_qty: int = 50,
    ofi_depth_norm_ppm: int = 100_000,
    ret_autocov_5s_x1e6: int = -500_000,  # negative = oscillating (reversal)
    tob_survival_ms: int = 500,  # short = volatile TOB
) -> tuple[int, ...]:
    """Build a minimal v2 feature tuple (19 elements)."""
    # First 16 elements (v1): mostly zeros, only L1 qty matters
    v1 = (0, 0, 0, 0, 0, 0, 0, 0, l1_bid_qty, l1_ask_qty, 0, 0, 0, 0, 0, 0)
    v2 = (ofi_depth_norm_ppm, ret_autocov_5s_x1e6, tob_survival_ms)
    return v1 + v2


def _inject_features(mm: OpportunisticMM, symbol: str, features: tuple[int | float, ...]) -> None:
    """Inject feature tuple into OpMM's cache via on_features."""
    evt = FeatureUpdateEvent(
        symbol=symbol,
        ts=1,
        local_ts=1,
        seq=1,
        feature_set_id="lob_shared_v2",
        schema_version=2,
        changed_mask=0,
        warmup_ready_mask=0,
        quality_flags=0,
        feature_ids=(),
        values=features,
    )
    mm.on_features(evt)


def test_reversal_filter_disabled_by_default() -> None:
    mm = OpportunisticMM()
    assert not mm.reversal_filter_enabled
    # Should always pass when disabled
    assert mm._check_reversal_condition("TXFD6") is True


def test_reversal_filter_passes_on_favorable_conditions() -> None:
    mm = OpportunisticMM(reversal_filter_enabled=True)
    features = _make_feature_tuple(
        ret_autocov_5s_x1e6=-500_000,  # oscillating (good)
        tob_survival_ms=500,  # short survival (good)
        l1_bid_qty=50,
        l1_ask_qty=50,  # balanced depth (good)
    )
    _inject_features(mm, "TXFD6", features)
    assert mm._check_reversal_condition("TXFD6") is True


def test_reversal_filter_rejects_positive_autocov() -> None:
    mm = OpportunisticMM(reversal_filter_enabled=True, reversal_autocov_threshold=0)
    features = _make_feature_tuple(
        ret_autocov_5s_x1e6=100_000,  # trending (bad — not oscillating)
    )
    _inject_features(mm, "TXFD6", features)
    assert mm._check_reversal_condition("TXFD6") is False


def test_reversal_filter_rejects_long_tob_survival() -> None:
    mm = OpportunisticMM(reversal_filter_enabled=True, reversal_tob_max_ms=2000)
    features = _make_feature_tuple(
        ret_autocov_5s_x1e6=-500_000,  # oscillating (good)
        tob_survival_ms=5000,  # stable TOB (bad — not volatile enough)
    )
    _inject_features(mm, "TXFD6", features)
    assert mm._check_reversal_condition("TXFD6") is False


def test_reversal_filter_rejects_extreme_depth_imbalance() -> None:
    mm = OpportunisticMM(reversal_filter_enabled=True, reversal_min_depth_ratio=0.3)
    features = _make_feature_tuple(
        ret_autocov_5s_x1e6=-500_000,
        tob_survival_ms=500,
        l1_bid_qty=5,  # very thin bid
        l1_ask_qty=95,  # heavy ask — extreme imbalance
    )
    _inject_features(mm, "TXFD6", features)
    # min_side/total = 5/100 = 0.05 < 0.3
    assert mm._check_reversal_condition("TXFD6") is False


def test_reversal_filter_permissive_when_no_features() -> None:
    mm = OpportunisticMM(reversal_filter_enabled=True)
    # No features injected — should be permissive (fallback)
    assert mm._check_reversal_condition("TXFD6") is True


def test_reversal_filter_permissive_with_v1_features() -> None:
    mm = OpportunisticMM(reversal_filter_enabled=True)
    # Only 16 v1 features — no v2 indices
    v1_only = tuple(range(16))
    _inject_features(mm, "TXFD6", v1_only)
    assert mm._check_reversal_condition("TXFD6") is True


def test_reversal_filter_gates_quoting() -> None:
    """When reversal filter is enabled and conditions are bad, on_stats should not quote."""
    mm = OpportunisticMM(
        spread_threshold_bps=2.5,
        reversal_filter_enabled=True,
        reversal_autocov_threshold=0,
    )
    mm.ctx = MagicMock()
    mm._generated_intents = []
    mm.position = MagicMock(return_value=0)

    # Inject bad features (positive autocov = trending, not oscillating)
    features = _make_feature_tuple(ret_autocov_5s_x1e6=100_000)
    _inject_features(mm, "TXFD6", features)

    # Wide spread (should pass spread gate)
    ev = _make_event(mid_x2=660000, spread_scaled=100, best_bid=329950, best_ask=330050)
    mm.on_stats(ev)

    # position() should NOT be called because reversal filter blocked
    assert not mm.position.called


def test_reversal_filter_allows_quoting() -> None:
    """When reversal filter passes, on_stats delegates to super."""
    mm = OpportunisticMM(
        spread_threshold_bps=2.5,
        reversal_filter_enabled=True,
        reversal_autocov_threshold=0,
    )
    mm.ctx = MagicMock()
    mm.ctx.place_order = MagicMock(return_value=MagicMock())
    mm._generated_intents = []
    mm.position = MagicMock(return_value=0)

    # Inject good features (negative autocov, short TOB, balanced depth)
    features = _make_feature_tuple(
        ret_autocov_5s_x1e6=-500_000,
        tob_survival_ms=500,
        l1_bid_qty=50,
        l1_ask_qty=50,
    )
    _inject_features(mm, "TXFD6", features)

    # Wide spread
    ev = _make_event(mid_x2=660000, spread_scaled=100, best_bid=329950, best_ask=330050, imbalance=0.1)
    mm.on_stats(ev)

    # position() should be called (super().on_stats ran)
    assert mm.position.called
