"""Tests for ScenarioBuilder and scenario rules SC-01 through SC-03.

TDD: tests written first, then implementation.
"""

from __future__ import annotations

from hft_platform.reports.models import (
    FlowBar,
    PriceLevel,
    Scenario,
    ScenarioReport,
    SessionData,
    SignalReport,
)
from hft_platform.reports.rules.scenario_rules import (
    scenario_break_below_support,
    scenario_hold_and_bounce,
    scenario_range_bound,
)
from hft_platform.reports.scenarios import ScenarioBuilder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PLATFORM_SCALE = 10_000


def _fb() -> FlowBar:
    return FlowBar(
        ts="t",
        ticks=1,
        total_vol=1,
        uptick_vol=0,
        downtick_vol=1,
        flat_vol=0,
        ud_ratio=0.5,
        net_flow=-1,
    )


def _make_signal(
    bias: str,
    supports: list[PriceLevel] | None = None,
    resistances: list[PriceLevel] | None = None,
    bias_confidence: float = 0.75,
) -> SignalReport:
    sd = SessionData(
        session="night",
        symbol="TXFD6",
        date="2026-03-27",
        open=330490000,
        high=330490000,
        low=323750000,
        close=324380000,
        volume=58107,
        tick_count=38153,
        bars_5m=[],
        flow_5m=[_fb()] * 10,
        large_trades=[],
        spread_dist={},
        depth_imbalance=[],
    )
    default_supports = [
        PriceLevel(price=323750000, strength=0.9, reason="雙底"),
        PriceLevel(price=320000000, strength=0.6, reason="整千"),
    ]
    default_resistances = [
        PriceLevel(price=327500000, strength=0.9, reason="壓力"),
        PriceLevel(price=330000000, strength=0.7, reason="整千"),
    ]
    return SignalReport(
        session_data=sd,
        total_net_flow=-1581,
        ud_ratio_session=0.906,
        strongest_sell=_fb(),
        strongest_buy=_fb(),
        large_buy_volume=380,
        large_sell_volume=650,
        large_net=-270,
        key_large_trades=[],
        supports=supports if supports is not None else default_supports,
        resistances=resistances if resistances is not None else default_resistances,
        bias=bias,
        bias_confidence=bias_confidence,
        rule_scores={},
    )


# ---------------------------------------------------------------------------
# scenario_rules tests
# ---------------------------------------------------------------------------


class TestScenarioBreakBelowSupport:
    """SC-01: break below support."""

    def test_returns_scenario_with_two_supports(self) -> None:
        sig = _make_signal("bearish")
        result = scenario_break_below_support(sig)
        assert result is not None
        assert isinstance(result, Scenario)

    def test_id_is_sc01(self) -> None:
        sig = _make_signal("bearish")
        result = scenario_break_below_support(sig)
        assert result is not None
        assert result.id == "SC-01"

    def test_probability_higher_when_bearish(self) -> None:
        sig = _make_signal("bearish")
        result = scenario_break_below_support(sig)
        assert result is not None
        assert result.probability == "較高"

    def test_probability_lower_when_bullish(self) -> None:
        sig = _make_signal("bullish")
        result = scenario_break_below_support(sig)
        assert result is not None
        assert result.probability == "較低"

    def test_probability_lower_when_neutral(self) -> None:
        sig = _make_signal("neutral")
        result = scenario_break_below_support(sig)
        assert result is not None
        assert result.probability == "較低"

    def test_condition_references_s1_price(self) -> None:
        sig = _make_signal("bearish")
        result = scenario_break_below_support(sig)
        assert result is not None
        # S1 is the first (strongest) support: 323750000 → 32,375
        assert "32,375" in result.condition

    def test_target_is_s2_price(self) -> None:
        sig = _make_signal("bearish")
        result = scenario_break_below_support(sig)
        assert result is not None
        # S2 is the second support: 320000000
        assert result.target == 320000000

    def test_returns_none_with_fewer_than_two_supports(self) -> None:
        sig = _make_signal(
            "bearish",
            supports=[PriceLevel(price=323750000, strength=0.9, reason="test")],
        )
        result = scenario_break_below_support(sig)
        assert result is None

    def test_returns_none_with_empty_supports(self) -> None:
        sig = _make_signal("bearish", supports=[])
        result = scenario_break_below_support(sig)
        assert result is None


class TestScenarioHoldAndBounce:
    """SC-02: hold support and bounce to resistance."""

    def test_returns_scenario_with_supports_and_resistances(self) -> None:
        sig = _make_signal("bearish")
        result = scenario_hold_and_bounce(sig)
        assert result is not None
        assert isinstance(result, Scenario)

    def test_id_is_sc02(self) -> None:
        sig = _make_signal("bearish")
        result = scenario_hold_and_bounce(sig)
        assert result is not None
        assert result.id == "SC-02"

    def test_probability_lower_when_bearish(self) -> None:
        sig = _make_signal("bearish")
        result = scenario_hold_and_bounce(sig)
        assert result is not None
        assert result.probability == "較低"

    def test_probability_higher_when_bullish(self) -> None:
        sig = _make_signal("bullish")
        result = scenario_hold_and_bounce(sig)
        assert result is not None
        assert result.probability == "較高"

    def test_probability_higher_when_neutral(self) -> None:
        sig = _make_signal("neutral")
        result = scenario_hold_and_bounce(sig)
        assert result is not None
        assert result.probability == "較高"

    def test_condition_references_s1_and_r1(self) -> None:
        sig = _make_signal("bearish")
        result = scenario_hold_and_bounce(sig)
        assert result is not None
        # S1 = 32,375; R1 = 32,750
        assert "32,375" in result.condition
        assert "32,750" in result.condition

    def test_target_is_r1_price(self) -> None:
        sig = _make_signal("bearish")
        result = scenario_hold_and_bounce(sig)
        assert result is not None
        # R1 is the first (strongest) resistance: 327500000
        assert result.target == 327500000

    def test_returns_none_with_no_resistances(self) -> None:
        sig = _make_signal("bearish", resistances=[])
        result = scenario_hold_and_bounce(sig)
        assert result is None

    def test_returns_none_with_no_supports(self) -> None:
        sig = _make_signal("bearish", supports=[])
        result = scenario_hold_and_bounce(sig)
        assert result is None


class TestScenarioRangeBound:
    """SC-03: range-bound oscillation."""

    def test_returns_scenario_with_supports_and_resistances(self) -> None:
        sig = _make_signal("neutral")
        result = scenario_range_bound(sig)
        assert result is not None
        assert isinstance(result, Scenario)

    def test_id_is_sc03(self) -> None:
        sig = _make_signal("neutral")
        result = scenario_range_bound(sig)
        assert result is not None
        assert result.id == "SC-03"

    def test_probability_always_lower(self) -> None:
        for bias in ("bearish", "bullish", "neutral"):
            sig = _make_signal(bias)
            result = scenario_range_bound(sig)
            assert result is not None
            assert result.probability == "較低", f"Expected 較低 for bias={bias}"

    def test_condition_references_s1_and_r1(self) -> None:
        sig = _make_signal("neutral")
        result = scenario_range_bound(sig)
        assert result is not None
        assert "32,375" in result.condition
        assert "32,750" in result.condition

    def test_target_is_zero(self) -> None:
        sig = _make_signal("neutral")
        result = scenario_range_bound(sig)
        assert result is not None
        assert result.target == 0

    def test_returns_none_with_no_supports(self) -> None:
        sig = _make_signal("neutral", supports=[])
        result = scenario_range_bound(sig)
        assert result is None

    def test_returns_none_with_no_resistances(self) -> None:
        sig = _make_signal("neutral", resistances=[])
        result = scenario_range_bound(sig)
        assert result is None


# ---------------------------------------------------------------------------
# ScenarioBuilder tests
# ---------------------------------------------------------------------------


class TestScenarioBuilderDirection:
    """Direction field maps correctly from bias."""

    def test_bearish_direction(self) -> None:
        sig = _make_signal("bearish")
        report = ScenarioBuilder().build(sig)
        assert report.direction == "偏空"

    def test_bullish_direction(self) -> None:
        sig = _make_signal("bullish")
        report = ScenarioBuilder().build(sig)
        assert report.direction == "偏多"

    def test_neutral_direction(self) -> None:
        sig = _make_signal("neutral")
        report = ScenarioBuilder().build(sig)
        assert report.direction == "中性"


class TestScenarioBuilderConfidence:
    """confidence_pct = int(50 + bias_confidence * 30)."""

    def test_confidence_formula(self) -> None:
        sig = _make_signal("bearish", bias_confidence=0.75)
        report = ScenarioBuilder().build(sig)
        assert report.confidence_pct == int(50 + 0.75 * 30)  # 72

    def test_confidence_bearish_at_least_60(self) -> None:
        sig = _make_signal("bearish", bias_confidence=0.75)
        report = ScenarioBuilder().build(sig)
        assert report.confidence_pct >= 60

    def test_confidence_zero_bias_is_50(self) -> None:
        sig = _make_signal("neutral", bias_confidence=0.0)
        report = ScenarioBuilder().build(sig)
        assert report.confidence_pct == 50


class TestScenarioBuilderKeyLevels:
    """key_levels contain top supports as S1/S2/S3 and resistances as R1/R2/R3."""

    def test_key_levels_populated(self) -> None:
        sig = _make_signal("bearish")
        report = ScenarioBuilder().build(sig)
        assert len(report.key_levels) >= 2

    def test_support_labels(self) -> None:
        sig = _make_signal("bearish")
        report = ScenarioBuilder().build(sig)
        labels = [kl.label for kl in report.key_levels]
        assert "S1" in labels

    def test_resistance_labels(self) -> None:
        sig = _make_signal("bearish")
        report = ScenarioBuilder().build(sig)
        labels = [kl.label for kl in report.key_levels]
        assert "R1" in labels

    def test_key_level_prices_are_scaled_ints(self) -> None:
        sig = _make_signal("bearish")
        report = ScenarioBuilder().build(sig)
        for kl in report.key_levels:
            assert isinstance(kl.price, int)

    def test_importance_clamped_1_to_3(self) -> None:
        sig = _make_signal("bearish")
        report = ScenarioBuilder().build(sig)
        for kl in report.key_levels:
            assert 1 <= kl.importance <= 3

    def test_three_supports_yield_s1_s2_s3(self) -> None:
        supports = [
            PriceLevel(price=320000000, strength=0.9, reason="a"),
            PriceLevel(price=315000000, strength=0.7, reason="b"),
            PriceLevel(price=310000000, strength=0.5, reason="c"),
        ]
        sig = _make_signal("bearish", supports=supports)
        report = ScenarioBuilder().build(sig)
        labels = [kl.label for kl in report.key_levels]
        assert "S1" in labels
        assert "S2" in labels
        assert "S3" in labels


class TestScenarioBuilderScenarios:
    """Scenarios list is correctly populated."""

    def test_bearish_has_at_least_two_scenarios(self) -> None:
        sig = _make_signal("bearish")
        report = ScenarioBuilder().build(sig)
        assert len(report.scenarios) >= 2

    def test_scenario_ids_are_unique(self) -> None:
        sig = _make_signal("bearish")
        report = ScenarioBuilder().build(sig)
        ids = [s.id for s in report.scenarios]
        assert len(ids) == len(set(ids))

    def test_all_scenarios_are_scenario_instances(self) -> None:
        sig = _make_signal("bearish")
        report = ScenarioBuilder().build(sig)
        for s in report.scenarios:
            assert isinstance(s, Scenario)

    def test_report_is_scenario_report_instance(self) -> None:
        sig = _make_signal("bearish")
        report = ScenarioBuilder().build(sig)
        assert isinstance(report, ScenarioReport)


class TestScenarioBuilderTradeLevels:
    """entry_zone, target, stop_loss correctness."""

    def test_bearish_entry_zone_above_target(self) -> None:
        """For a short, entry should be above target (we enter near resistance)."""
        sig = _make_signal("bearish")
        report = ScenarioBuilder().build(sig)
        # entry_zone is a tuple (low, high); target is S1 price
        entry_low, entry_high = report.entry_zone
        assert entry_low <= entry_high
        assert entry_high > report.target

    def test_bearish_stop_loss_above_entry_zone(self) -> None:
        """Stop for a short should be above entry zone."""
        sig = _make_signal("bearish")
        report = ScenarioBuilder().build(sig)
        _, entry_high = report.entry_zone
        assert report.stop_loss > entry_high

    def test_bullish_entry_zone_below_target(self) -> None:
        """For a long, entry should be below target."""
        sig = _make_signal("bullish")
        report = ScenarioBuilder().build(sig)
        entry_low, _ = report.entry_zone
        assert entry_low < report.target

    def test_bullish_stop_loss_below_entry_zone(self) -> None:
        """Stop for a long should be below entry zone."""
        sig = _make_signal("bullish")
        report = ScenarioBuilder().build(sig)
        entry_low, _ = report.entry_zone
        assert report.stop_loss < entry_low

    def test_entry_zone_is_tuple_of_two_ints(self) -> None:
        sig = _make_signal("bearish")
        report = ScenarioBuilder().build(sig)
        assert isinstance(report.entry_zone, tuple)
        assert len(report.entry_zone) == 2
        assert isinstance(report.entry_zone[0], int)
        assert isinstance(report.entry_zone[1], int)


class TestScenarioBuilderEdgeCases:
    """Edge cases: empty supports/resistances still produce a report."""

    def test_empty_supports_still_produces_report(self) -> None:
        sig = _make_signal("bearish", supports=[])
        report = ScenarioBuilder().build(sig)
        assert isinstance(report, ScenarioReport)

    def test_empty_resistances_still_produces_report(self) -> None:
        sig = _make_signal("bearish", resistances=[])
        report = ScenarioBuilder().build(sig)
        assert isinstance(report, ScenarioReport)

    def test_empty_both_still_produces_report(self) -> None:
        sig = _make_signal("neutral", supports=[], resistances=[])
        report = ScenarioBuilder().build(sig)
        assert isinstance(report, ScenarioReport)
        assert isinstance(report.entry_zone, tuple)

    def test_single_support_no_sc01(self) -> None:
        sig = _make_signal(
            "bearish",
            supports=[PriceLevel(price=323750000, strength=0.9, reason="test")],
        )
        report = ScenarioBuilder().build(sig)
        sc01_ids = [s for s in report.scenarios if s.id == "SC-01"]
        assert len(sc01_ids) == 0
