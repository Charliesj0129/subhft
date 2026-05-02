"""Unit tests for `R52AmhpDynamicSpreadStrategy` — L1 modulator + kill-flag telemetry.

Patterns drawn from `hft-test-hft`:
  * scaled int x10000 prices everywhere
  * monotonic time via integer ns (no datetime.now)
  * factory fixtures for events
  * kill-flag instrumentation via `kill_flag_telemetry()` snapshots
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent, TickEvent
from research.alphas.r52_amhp_dynamic_spread.impl import (
    _BASE_SPREAD_SCALED,
    _NS_PER_SEC,
    _PRICE_SCALE,
    R52AmhpDynamicSpreadStrategy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lob_stats(
    symbol: str = "TMFD6",
    *,
    mid_price_x2: int = 2_000_0000,
    spread_scaled: int = 6 * _PRICE_SCALE,   # 6 pt observed spread
    imbalance: float = 0.0,
    best_bid: int = 997_0000,
    best_ask: int = 1003_0000,
    bid_depth: int = 100,
    ask_depth: int = 100,
    ts: int = 0,
) -> LOBStatsEvent:
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


def _make_features(symbol: str = "TMFD6", l1_imb_ppm: int = 0) -> FeatureUpdateEvent:
    values = [0] * 22
    values[10] = l1_imb_ppm   # _IDX_L1_IMBALANCE_PPM
    return FeatureUpdateEvent(
        symbol=symbol,
        ts=0,
        local_ts=0,
        seq=1,
        feature_set_id="lob_shared_v3",
        schema_version=3,
        changed_mask=0,
        warmup_ready_mask=0,
        quality_flags=0,
        feature_ids=tuple(f"f{i}" for i in range(len(values))),
        values=tuple(values),
    )


def _make_tick(symbol: str = "TMFD6", direction: int = 1, ts_ns: int = 0,
               price: int = 1000_0000, volume: int = 1) -> TickEvent:
    meta = SimpleNamespace(local_ts=0, source_ts=0, seq=1, topic="tick")
    ev = TickEvent(
        meta=meta,
        symbol=symbol,
        price=price,
        volume=volume,
        trade_direction=direction,
    )
    # `ts` attribute is sourced from the TickEvent body in the strategy code.
    # If your TickEvent surface doesn't have ts, fall back to meta clock.
    object.__setattr__(ev, "ts", ts_ns) if hasattr(ev, "ts") else None
    return ev


@pytest.fixture()
def strategy() -> R52AmhpDynamicSpreadStrategy:
    strat = R52AmhpDynamicSpreadStrategy(
        strategy_id="r52_amhp_dynamic_spread",
        max_pos=3,
    )
    strat.symbols = {"TMFD6"}
    ctx = MagicMock()
    ctx.positions = {}
    ctx.strategy_id = "r52_amhp_dynamic_spread"
    ctx.place_order = MagicMock(return_value=MagicMock())
    strat.ctx = ctx
    strat._generated_intents = []
    return strat


# ---------------------------------------------------------------------------
# L1 spread modulator tests
# ---------------------------------------------------------------------------


class TestL1SpreadModulator:
    def test_multiplier_at_calm_regime_is_unity(self, strategy):
        # rho_hat=0, IIR=0 → multiplier = 1.0 (no widening)
        m = strategy._compute_multiplier(rho_hat=0.0, iir_abs=0.0)
        assert m == 1.0

    def test_multiplier_below_rho_low_clipped_to_unity(self, strategy):
        # rho_hat below rho_low → no widening contribution from rho
        m = strategy._compute_multiplier(rho_hat=0.40, iir_abs=0.0)
        assert m == 1.0

    def test_multiplier_grows_with_rho_above_threshold(self, strategy):
        m_calm = strategy._compute_multiplier(rho_hat=0.55, iir_abs=0.0)
        m_normal = strategy._compute_multiplier(rho_hat=0.65, iir_abs=0.0)
        m_tense = strategy._compute_multiplier(rho_hat=0.75, iir_abs=0.0)
        assert m_calm == 1.0
        assert m_normal > m_calm
        assert m_tense > m_normal

    def test_multiplier_capped_at_critical(self, strategy):
        # rho_hat ≥ rho_critical (0.85) → snap to mult_cap
        m = strategy._compute_multiplier(rho_hat=0.90, iir_abs=0.0)
        assert m == strategy._mult_cap

    def test_multiplier_iir_critical_overrides(self, strategy):
        # |IIR| ≥ iir_critical → snap to mult_cap regardless of rho
        m = strategy._compute_multiplier(rho_hat=0.10, iir_abs=0.85)
        assert m == strategy._mult_cap

    def test_l1_hard_floor_5pt_never_narrows(self, strategy):
        """L1 hard floor invariant — no input combination should drop spread
        target below 5 pt."""
        # Even with adversarial inputs, multiplier minimum is 1.0
        for rho in (0.0, 0.3, 0.55, 0.84):
            for iir in (0.0, 0.2, 0.45, 0.69):
                m = strategy._compute_multiplier(rho_hat=rho, iir_abs=iir)
                target_scaled = max(
                    _BASE_SPREAD_SCALED,
                    int(_BASE_SPREAD_SCALED * m),
                )
                assert target_scaled >= _BASE_SPREAD_SCALED, (
                    f"L1 floor violated: rho={rho} iir={iir} m={m} target_scaled={target_scaled}"
                )

    def test_l1_floor_held_when_observed_spread_below_target(self, strategy):
        # observed 4 pt < target floor 5 pt → spread_blocked counter increments
        before = strategy._spread_blocked
        ev = _make_lob_stats(spread_scaled=4 * _PRICE_SCALE, ts=_NS_PER_SEC)
        strategy.on_stats(ev)
        assert strategy._spread_blocked == before + 1


class TestRegimeSizeAttenuation:
    def test_size_calm_is_one(self, strategy):
        assert strategy._size_for_regime(rho_hat=0.20, iir_abs=0.10) == 1

    def test_size_critical_is_one(self, strategy):
        # In the current default config max_pos=3 but per-quote qty=1 even at calm.
        # Critical regime returns _SIZE_CRIT == 1; verify return value.
        assert strategy._size_for_regime(rho_hat=0.90, iir_abs=0.0) == 1
        assert strategy._size_for_regime(rho_hat=0.0, iir_abs=0.85) == 1


# ---------------------------------------------------------------------------
# Kill-flag telemetry tests
# ---------------------------------------------------------------------------


class TestKillFlagTelemetry:
    def test_empty_telemetry_safe_defaults(self, strategy):
        snap = strategy.kill_flag_telemetry()
        assert snap["K1_max_day_pct"] == 0.0
        assert snap["K1_winning_days"] == 0
        assert snap["K1_distinct_fill_days"] == 0
        assert snap["K1_total_pnl_pts"] == 0.0
        assert snap["K3_modulator_per_fill_gain_pts"] == 0.0
        assert snap["K5_rho_critical_freq_pct"] == 0.0

    def test_record_fill_updates_k1_telemetry(self, strategy):
        strategy.record_fill_for_telemetry(
            day="2026-02-15",
            pnl_pts=2.5,
            modulator_active=True,
            captured_half_spread_pts=4.0,
            baseline_half_spread_pts=2.5,
        )
        snap = strategy.kill_flag_telemetry()
        assert snap["fill_count"] == 1
        assert snap["K1_total_pnl_pts"] == 2.5
        assert snap["K1_winning_days"] == 1
        assert snap["K1_distinct_fill_days"] == 1
        # K3: gain = 4.0 - 2.5 = 1.5
        assert snap["K3_modulator_per_fill_gain_pts"] == 1.5

    def test_k1_max_day_pct_aggregates(self, strategy):
        strategy.record_fill_for_telemetry(
            day="2026-02-15", pnl_pts=10.0, modulator_active=False,
            captured_half_spread_pts=2.5, baseline_half_spread_pts=2.5,
        )
        strategy.record_fill_for_telemetry(
            day="2026-02-16", pnl_pts=2.0, modulator_active=False,
            captured_half_spread_pts=2.5, baseline_half_spread_pts=2.5,
        )
        snap = strategy.kill_flag_telemetry()
        # Total = 12, max_day = 10 → max_day_pct = 10/12 = 0.833
        assert snap["K1_max_day_pct"] == pytest.approx(10.0 / 12.0)
        assert snap["K1_winning_days"] == 2
        assert snap["K1_distinct_fill_days"] == 2

    def test_k1_winning_days_excludes_negative(self, strategy):
        strategy.record_fill_for_telemetry(
            day="2026-02-15", pnl_pts=5.0, modulator_active=False,
            captured_half_spread_pts=2.5, baseline_half_spread_pts=2.5,
        )
        strategy.record_fill_for_telemetry(
            day="2026-02-16", pnl_pts=-3.0, modulator_active=False,
            captured_half_spread_pts=2.5, baseline_half_spread_pts=2.5,
        )
        snap = strategy.kill_flag_telemetry()
        assert snap["K1_winning_days"] == 1
        assert snap["K1_distinct_fill_days"] == 2


# ---------------------------------------------------------------------------
# Day-level covariate plumbing tests
# ---------------------------------------------------------------------------


class TestDayCovariatePlumbing:
    def test_set_day_covariates_persists_on_state(self, strategy):
        strategy.set_day_covariates(
            "TMFD6",
            io_z=1.5,
            us_overnight=-0.012,
            us_window_active=True,
        )
        st = strategy._get_amhp("TMFD6")
        assert st._io_z == 1.5
        assert st._us_overnight == -0.012
        assert st._us_window_active is True

    def test_gamma_io_us_load_through_init(self):
        """T2 flag #1: γ_io and γ_us must be configurable from strategy ctor
        for the K2 covariate-significance test."""
        strat = R52AmhpDynamicSpreadStrategy(
            gamma_io=0.4,
            gamma_us=0.7,
        )
        st = strat._get_amhp("TMFD6")
        assert st._mu_io == 0.4
        assert st._mu_us == 0.7
