"""Unit tests for OpportunisticMM strategy — points-based spread threshold."""

from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.events import FeatureUpdateEvent
from hft_platform.strategies.opportunistic_mm import OpportunisticMM

# Price scale factor (same as production)
_SCALE = 10000


def _make_event(
    mid_x2: int,
    spread_scaled: int,
    best_bid: int = 10000,
    best_ask: int = 10010,
    imbalance: float = 0.0,
    symbol: str = "TMFD6",
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


def _make_tmfd6_event(spread_pts: int, price: int = 23000, imbalance: float = 0.0) -> MagicMock:
    """Create a TMFD6-realistic LOBStatsEvent from spread in points.

    Args:
        spread_pts: Spread in index points (e.g. 5 = 5 point spread).
        price: Approximate index level (default 23000).
        imbalance: LOB imbalance ratio.
    """
    best_bid = price * _SCALE
    best_ask = (price + spread_pts) * _SCALE
    mid_x2 = best_bid + best_ask
    spread_scaled = spread_pts * _SCALE
    return _make_event(
        mid_x2=mid_x2,
        spread_scaled=spread_scaled,
        best_bid=best_bid,
        best_ask=best_ask,
        imbalance=imbalance,
    )


# --- Construction ---
def test_default_threshold() -> None:
    mm = OpportunisticMM()
    assert mm.spread_threshold_pts == 5


def test_custom_threshold() -> None:
    mm = OpportunisticMM(spread_threshold_pts=3)
    assert mm.spread_threshold_pts == 3


def test_threshold_scaled_correctly() -> None:
    mm = OpportunisticMM(spread_threshold_pts=5)
    assert mm._spread_threshold_scaled == 5 * _SCALE


def test_inherits_simple_mm() -> None:
    from hft_platform.strategies.simple_mm import SimpleMarketMaker

    assert issubclass(OpportunisticMM, SimpleMarketMaker)


# --- Spread gate (points-based) ---
def test_tight_spread_does_not_quote() -> None:
    """Spread 3 pts < threshold 5 pts → blocked."""
    mm = OpportunisticMM(spread_threshold_pts=5)
    mm.ctx = MagicMock()
    mm._generated_intents = []
    ev = _make_tmfd6_event(spread_pts=3)  # 3 < 5
    mm.on_stats(ev)
    assert len(mm._generated_intents) == 0
    assert mm._gate_blocked_count == 1


def test_exact_threshold_passes() -> None:
    """Spread exactly at threshold → should pass (>= comparison)."""
    mm = OpportunisticMM(spread_threshold_pts=5)
    mm.ctx = MagicMock()
    mm.ctx.place_order = MagicMock(return_value=MagicMock())
    mm._generated_intents = []
    mm.position = MagicMock(return_value=0)

    ev = _make_tmfd6_event(spread_pts=5)  # 5 >= 5
    mm.on_stats(ev)
    assert mm.position.called
    assert mm._gate_passed_count == 1


def test_wide_spread_delegates_to_super() -> None:
    """Spread 7 pts > threshold 5 pts → delegates to SimpleMM."""
    mm = OpportunisticMM(spread_threshold_pts=5)
    mm.ctx = MagicMock()
    mm.ctx.place_order = MagicMock(return_value=MagicMock())
    mm._generated_intents = []
    mm.position = MagicMock(return_value=0)

    ev = _make_tmfd6_event(spread_pts=7, imbalance=0.1)
    mm.on_stats(ev)
    assert mm.position.called


def test_threshold_independent_of_price_level() -> None:
    """5-point threshold should behave identically at price 20000 and 30000."""
    for price in (20000, 23000, 25000, 30000):
        mm = OpportunisticMM(spread_threshold_pts=5)
        mm.ctx = MagicMock()
        mm.ctx.place_order = MagicMock(return_value=MagicMock())
        mm._generated_intents = []
        mm.position = MagicMock(return_value=0)

        # 4 pts → blocked
        ev_tight = _make_tmfd6_event(spread_pts=4, price=price)
        mm.on_stats(ev_tight)
        assert mm._gate_blocked_count == 1, f"Price {price}: 4 pts should be blocked"

        # 5 pts → passed
        ev_wide = _make_tmfd6_event(spread_pts=5, price=price, imbalance=0.1)
        mm.on_stats(ev_wide)
        assert mm._gate_passed_count == 1, f"Price {price}: 5 pts should pass"


def test_integer_comparison_no_float() -> None:
    """The spread gate uses integer comparison (spread_scaled vs threshold_scaled)."""
    mm = OpportunisticMM(spread_threshold_pts=5)
    # Verify internal state is int, not float
    assert isinstance(mm._spread_threshold_scaled, int)
    assert mm._spread_threshold_scaled == 50000


# --- None/invalid guards ---
def test_none_mid_price_returns_early() -> None:
    mm = OpportunisticMM()
    ev = MagicMock()
    ev.mid_price_x2 = None
    ev.spread_scaled = 10
    ev.symbol = "TMFD6"
    mm.on_stats(ev)
    assert mm._invalid_data_count == 1


def test_zero_spread_returns_early() -> None:
    mm = OpportunisticMM()
    ev = _make_event(mid_x2=460000000, spread_scaled=0)
    mm.on_stats(ev)
    assert mm._invalid_data_count == 1


def test_negative_mid_returns_early() -> None:
    mm = OpportunisticMM()
    ev = _make_event(mid_x2=-1, spread_scaled=50000)
    mm.on_stats(ev)
    assert mm._invalid_data_count == 1


# --- Observability ---
def test_observability_counters() -> None:
    """Verify internal counters track gate decisions correctly."""
    mm = OpportunisticMM(spread_threshold_pts=5)
    mm.ctx = MagicMock()
    mm.ctx.place_order = MagicMock(return_value=MagicMock())
    mm._generated_intents = []
    mm.position = MagicMock(return_value=0)

    # Tight spread — blocked
    ev_tight = _make_tmfd6_event(spread_pts=3)
    mm.on_stats(ev_tight)
    assert mm._gate_blocked_count == 1
    assert mm._gate_passed_count == 0

    # Wide spread — passed
    ev_wide = _make_tmfd6_event(spread_pts=7, imbalance=0.1)
    mm.on_stats(ev_wide)
    assert mm._gate_passed_count == 1
    assert mm._stats_count == 2

    # Invalid data — None
    ev_none = MagicMock()
    ev_none.mid_price_x2 = None
    ev_none.spread_scaled = 10
    ev_none.symbol = "TMFD6"
    mm.on_stats(ev_none)
    assert mm._invalid_data_count == 1


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
    assert mm._check_reversal_condition("TMFD6") is True


def test_reversal_filter_passes_on_favorable_conditions() -> None:
    mm = OpportunisticMM(reversal_filter_enabled=True)
    features = _make_feature_tuple(
        ret_autocov_5s_x1e6=-500_000,
        tob_survival_ms=500,
        l1_bid_qty=50,
        l1_ask_qty=50,
    )
    _inject_features(mm, "TMFD6", features)
    assert mm._check_reversal_condition("TMFD6") is True


def test_reversal_filter_rejects_positive_autocov() -> None:
    mm = OpportunisticMM(reversal_filter_enabled=True, reversal_autocov_threshold=0)
    features = _make_feature_tuple(ret_autocov_5s_x1e6=100_000)
    _inject_features(mm, "TMFD6", features)
    assert mm._check_reversal_condition("TMFD6") is False


def test_reversal_filter_rejects_long_tob_survival() -> None:
    mm = OpportunisticMM(reversal_filter_enabled=True, reversal_tob_max_ms=2000)
    features = _make_feature_tuple(
        ret_autocov_5s_x1e6=-500_000,
        tob_survival_ms=5000,
    )
    _inject_features(mm, "TMFD6", features)
    assert mm._check_reversal_condition("TMFD6") is False


def test_reversal_filter_rejects_extreme_depth_imbalance() -> None:
    mm = OpportunisticMM(reversal_filter_enabled=True, reversal_min_depth_ratio=0.3)
    features = _make_feature_tuple(
        ret_autocov_5s_x1e6=-500_000,
        tob_survival_ms=500,
        l1_bid_qty=5,
        l1_ask_qty=95,
    )
    _inject_features(mm, "TMFD6", features)
    assert mm._check_reversal_condition("TMFD6") is False


def test_reversal_filter_permissive_when_no_features() -> None:
    mm = OpportunisticMM(reversal_filter_enabled=True)
    assert mm._check_reversal_condition("TMFD6") is True


def test_reversal_filter_permissive_with_v1_features() -> None:
    mm = OpportunisticMM(reversal_filter_enabled=True)
    v1_only = tuple(range(16))
    _inject_features(mm, "TMFD6", v1_only)
    assert mm._check_reversal_condition("TMFD6") is True


def test_reversal_filter_gates_quoting() -> None:
    """When reversal filter rejects, on_stats should not quote even with wide spread."""
    mm = OpportunisticMM(
        spread_threshold_pts=5,
        reversal_filter_enabled=True,
        reversal_autocov_threshold=0,
    )
    mm.ctx = MagicMock()
    mm._generated_intents = []
    mm.position = MagicMock(return_value=0)

    features = _make_feature_tuple(ret_autocov_5s_x1e6=100_000)
    _inject_features(mm, "TMFD6", features)

    ev = _make_tmfd6_event(spread_pts=7)
    mm.on_stats(ev)
    assert not mm.position.called
    assert mm._reversal_blocked_count == 1


def test_reversal_filter_allows_quoting() -> None:
    """When reversal filter passes, on_stats delegates to super."""
    mm = OpportunisticMM(
        spread_threshold_pts=5,
        reversal_filter_enabled=True,
        reversal_autocov_threshold=0,
    )
    mm.ctx = MagicMock()
    mm.ctx.place_order = MagicMock(return_value=MagicMock())
    mm._generated_intents = []
    mm.position = MagicMock(return_value=0)

    features = _make_feature_tuple(
        ret_autocov_5s_x1e6=-500_000,
        tob_survival_ms=500,
        l1_bid_qty=50,
        l1_ask_qty=50,
    )
    _inject_features(mm, "TMFD6", features)

    ev = _make_tmfd6_event(spread_pts=7, imbalance=0.1)
    mm.on_stats(ev)
    assert mm.position.called
