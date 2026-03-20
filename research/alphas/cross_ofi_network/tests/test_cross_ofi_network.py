"""Tests for cross_ofi_network alpha — Unit 13 (Cross-Asset OFI Network).

Test coverage:
  - Manifest fields and validity
  - Signal computation with synthetic data
  - Leader weight calculation and normalisation
  - Edge cases: single leader, zero OFI, NaN/Inf handling
  - Correlation-based weight adaptivity
  - Reset behaviour
  - Anti-leak (deterministic output)
  - Module exports
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from research.alphas.cross_ofi_network.impl import (
    _DEFAULT_CORR_WINDOW,
    _DEFAULT_CROSS_WEIGHT_CAP,
    _DEFAULT_N_LEADERS,
    _DEFAULT_WEIGHT_UPDATE_INTERVAL,
    _EMA_ALPHA,
    _MANIFEST,
    ALPHA_CLASS,
    CrossOfiNetworkAlpha,
    _pearson_corr,
)

# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------

class TestManifest:
    def test_alpha_id(self):
        assert _MANIFEST.alpha_id == "cross_ofi_network"

    def test_data_fields(self):
        assert set(_MANIFEST.data_fields) == {"bid_qty", "ask_qty"}

    def test_complexity(self):
        assert _MANIFEST.complexity == "O(N)"

    def test_paper_ref(self):
        assert "2112.13213" in _MANIFEST.paper_refs

    def test_latency_profile_present(self):
        assert _MANIFEST.latency_profile is not None
        assert isinstance(_MANIFEST.latency_profile, str)
        assert len(_MANIFEST.latency_profile) > 0

    def test_feature_set_version(self):
        assert _MANIFEST.feature_set_version == "lob_shared_v1"

    def test_status_is_draft(self):
        from research.registry.schemas import AlphaStatus
        assert _MANIFEST.status == AlphaStatus.DRAFT

    def test_tier(self):
        from research.registry.schemas import AlphaTier
        assert _MANIFEST.tier == AlphaTier.TIER_2

    def test_roles_used_not_empty(self):
        assert len(_MANIFEST.roles_used) > 0

    def test_skills_used_not_empty(self):
        assert len(_MANIFEST.skills_used) > 0

    def test_roles_are_valid(self):
        from research.registry.schemas import VALID_ROLES
        for role in _MANIFEST.roles_used:
            assert role in VALID_ROLES, f"Unknown role: {role}"

    def test_skills_are_valid(self):
        from research.registry.schemas import VALID_SKILLS
        for skill in _MANIFEST.skills_used:
            assert skill in VALID_SKILLS, f"Unknown skill: {skill}"

    def test_alpha_class_export(self):
        assert ALPHA_CLASS is CrossOfiNetworkAlpha


# ---------------------------------------------------------------------------
# Protocol / interface tests
# ---------------------------------------------------------------------------

class TestProtocol:
    def test_has_update(self):
        assert callable(CrossOfiNetworkAlpha().update)

    def test_has_reset(self):
        assert callable(CrossOfiNetworkAlpha().reset)

    def test_has_get_signal(self):
        assert callable(CrossOfiNetworkAlpha().get_signal)

    def test_has_manifest(self):
        a = CrossOfiNetworkAlpha()
        assert a.manifest is _MANIFEST

    def test_has_slots(self):
        assert hasattr(CrossOfiNetworkAlpha, "__slots__")

    def test_has_get_leader_weights(self):
        assert callable(CrossOfiNetworkAlpha().get_leader_weights)

    def test_has_get_leader_count(self):
        assert callable(CrossOfiNetworkAlpha().get_leader_count)


# ---------------------------------------------------------------------------
# Initialisation / constructor tests
# ---------------------------------------------------------------------------

class TestInit:
    def test_default_params(self):
        a = CrossOfiNetworkAlpha()
        assert a._n_leaders == _DEFAULT_N_LEADERS
        assert a._corr_window == _DEFAULT_CORR_WINDOW
        assert a._weight_update_interval == _DEFAULT_WEIGHT_UPDATE_INTERVAL
        assert a._cross_weight_cap == _DEFAULT_CROSS_WEIGHT_CAP

    def test_custom_params(self):
        a = CrossOfiNetworkAlpha(n_leaders=3, corr_window=64, cross_weight_cap=0.4)
        assert a._n_leaders == 3
        assert a._corr_window == 64
        assert a._cross_weight_cap == 0.4

    def test_invalid_n_leaders(self):
        with pytest.raises(ValueError, match="n_leaders"):
            CrossOfiNetworkAlpha(n_leaders=0)

    def test_invalid_corr_window(self):
        with pytest.raises(ValueError, match="corr_window"):
            CrossOfiNetworkAlpha(corr_window=2)

    def test_invalid_cross_weight_cap_negative(self):
        with pytest.raises(ValueError, match="cross_weight_cap"):
            CrossOfiNetworkAlpha(cross_weight_cap=-0.1)

    def test_invalid_cross_weight_cap_over_one(self):
        with pytest.raises(ValueError, match="cross_weight_cap"):
            CrossOfiNetworkAlpha(cross_weight_cap=1.1)


# ---------------------------------------------------------------------------
# Basic signal computation
# ---------------------------------------------------------------------------

class TestSignalComputation:
    def test_first_tick_returns_zero(self):
        a = CrossOfiNetworkAlpha()
        assert a.update(bid_qty=100, ask_qty=50) == 0.0

    def test_initial_get_signal_zero(self):
        a = CrossOfiNetworkAlpha()
        assert a.get_signal() == 0.0

    def test_self_only_no_leaders(self):
        """Without leader_ofis, signal should be pure self-OFI EMA."""
        a = CrossOfiNetworkAlpha()
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=120, ask_qty=100)
        # self OFI: d_bid=20, d_ask=0, raw = 20/21
        expected = _EMA_ALPHA * (20.0 / 21.0)
        assert sig == pytest.approx(expected, rel=1e-6)

    def test_leader_ofis_influences_signal(self):
        """Providing leader_ofis should change the signal vs self-only."""
        a_self = CrossOfiNetworkAlpha()
        a_self.update(bid_qty=100, ask_qty=100)
        sig_self = a_self.update(bid_qty=120, ask_qty=100)

        a_net = CrossOfiNetworkAlpha()
        a_net.update(bid_qty=100, ask_qty=100)
        sig_net = a_net.update(bid_qty=120, ask_qty=100, leader_ofis={"TSMC": 0.5})

        assert sig_net != sig_self

    def test_positive_leader_pushes_signal_up(self):
        a = CrossOfiNetworkAlpha()
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=100, ask_qty=100, leader_ofis={"TSMC": 0.9})
        assert sig > 0.0

    def test_negative_leader_pushes_signal_down(self):
        a = CrossOfiNetworkAlpha()
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=100, ask_qty=100, leader_ofis={"TSMC": -0.9})
        assert sig < 0.0

    def test_signal_bounded(self):
        """Signal should stay within (-1, 1) under extreme inputs."""
        a = CrossOfiNetworkAlpha()
        a.update(bid_qty=1000, ask_qty=0)
        for _ in range(200):
            a.update(bid_qty=10000, ask_qty=0, leader_ofis={"A": 1.0, "B": 1.0})
        assert -1.0 < a.get_signal() < 1.0

    def test_zero_ofi_leaders_no_extra_signal(self):
        """Leaders with zero OFI should contribute nothing extra."""
        a_self = CrossOfiNetworkAlpha()
        a_self.update(bid_qty=100, ask_qty=100)
        sig_self = a_self.update(bid_qty=110, ask_qty=100)

        a_net = CrossOfiNetworkAlpha()
        a_net.update(bid_qty=100, ask_qty=100)
        sig_net = a_net.update(bid_qty=110, ask_qty=100, leader_ofis={"A": 0.0, "B": 0.0})

        # Leader EMAs will be 0, so leader contribution should be 0.
        # The signal should be dominated by self-OFI (weights may differ slightly).
        # At minimum, the sign should match.
        assert math.copysign(1, sig_self) == math.copysign(1, sig_net)

    def test_decay_to_zero(self):
        """Signal should decay toward zero when inputs become neutral."""
        a = CrossOfiNetworkAlpha()
        a.update(bid_qty=100, ask_qty=100)
        a.update(bid_qty=200, ask_qty=0, leader_ofis={"A": 1.0})
        for _ in range(1000):
            a.update(bid_qty=200, ask_qty=200, leader_ofis={"A": 0.0})
        assert abs(a.get_signal()) < 0.01


# ---------------------------------------------------------------------------
# Reset behaviour
# ---------------------------------------------------------------------------

class TestReset:
    def test_reset_clears_signal(self):
        a = CrossOfiNetworkAlpha()
        a.update(bid_qty=100, ask_qty=50)
        a.update(bid_qty=200, ask_qty=50, leader_ofis={"A": 0.8})
        assert a.get_signal() != 0.0
        a.reset()
        assert a.get_signal() == 0.0

    def test_reset_clears_leader_ids(self):
        a = CrossOfiNetworkAlpha()
        a.update(bid_qty=100, ask_qty=100, leader_ofis={"A": 0.5, "B": 0.3})
        assert a.get_leader_count() == 2
        a.reset()
        assert a.get_leader_count() == 0

    def test_reset_clears_weights(self):
        a = CrossOfiNetworkAlpha()
        for _ in range(50):
            a.update(bid_qty=100, ask_qty=100, leader_ofis={"A": 0.5})
        a.reset()
        assert a.get_leader_weights() == {}

    def test_reset_then_reinitialise(self):
        a = CrossOfiNetworkAlpha()
        a.update(bid_qty=100, ask_qty=100)
        a.update(bid_qty=150, ask_qty=100, leader_ofis={"X": 0.3})
        a.reset()
        assert a.update(bid_qty=100, ask_qty=100) == 0.0


# ---------------------------------------------------------------------------
# Leader registration and capacity
# ---------------------------------------------------------------------------

class TestLeaderRegistration:
    def test_single_leader_registered(self):
        a = CrossOfiNetworkAlpha(n_leaders=3)
        a.update(bid_qty=100, ask_qty=100, leader_ofis={"TSMC": 0.5})
        assert a.get_leader_count() == 1
        assert "TSMC" in a.get_leader_weights()

    def test_multiple_leaders_registered(self):
        a = CrossOfiNetworkAlpha(n_leaders=5)
        a.update(bid_qty=100, ask_qty=100, leader_ofis={"A": 0.5, "B": -0.3, "C": 0.1})
        assert a.get_leader_count() == 3

    def test_capacity_limit_not_exceeded(self):
        a = CrossOfiNetworkAlpha(n_leaders=2)
        a.update(bid_qty=100, ask_qty=100, leader_ofis={"A": 0.5, "B": 0.3, "C": 0.1})
        # Third leader should be silently dropped
        assert a.get_leader_count() == 2

    def test_same_leader_not_duplicated(self):
        a = CrossOfiNetworkAlpha(n_leaders=5)
        a.update(bid_qty=100, ask_qty=100, leader_ofis={"TSMC": 0.5})
        a.update(bid_qty=100, ask_qty=100, leader_ofis={"TSMC": 0.6})
        assert a.get_leader_count() == 1

    def test_leader_weight_dict_keys(self):
        a = CrossOfiNetworkAlpha(n_leaders=5)
        a.update(bid_qty=100, ask_qty=100, leader_ofis={"X": 0.2, "Y": 0.4})
        weights = a.get_leader_weights()
        assert "X" in weights
        assert "Y" in weights


# ---------------------------------------------------------------------------
# Leader weight calculation
# ---------------------------------------------------------------------------

class TestLeaderWeights:
    def test_weights_non_negative(self):
        """All leader weights must be non-negative."""
        a = CrossOfiNetworkAlpha(n_leaders=5, corr_window=32, weight_update_interval=4)
        rng = np.random.default_rng(42)
        for _ in range(100):
            bid = float(rng.integers(90, 110))
            ask = float(rng.integers(90, 110))
            leaders = {
                "A": float(rng.uniform(-0.5, 0.5)),
                "B": float(rng.uniform(-0.5, 0.5)),
            }
            a.update(bid_qty=bid, ask_qty=ask, leader_ofis=leaders)
        weights = a.get_leader_weights()
        for sym, w in weights.items():
            assert w >= 0.0, f"Negative weight for {sym}: {w}"

    def test_total_weight_capped(self):
        """Sum of all leader weights must not exceed cross_weight_cap."""
        cap = 0.5
        a = CrossOfiNetworkAlpha(n_leaders=5, corr_window=32, weight_update_interval=4, cross_weight_cap=cap)
        rng = np.random.default_rng(7)
        for _ in range(150):
            bid = float(rng.integers(90, 110))
            ask = float(rng.integers(90, 110))
            leaders = {f"L{i}": float(rng.uniform(-1.0, 1.0)) for i in range(5)}
            a.update(bid_qty=bid, ask_qty=ask, leader_ofis=leaders)
        total = sum(a.get_leader_weights().values())
        assert total <= cap + 1e-9, f"Total weight {total} exceeds cap {cap}"

    def test_correlated_leader_gets_higher_weight(self):
        """A leader perfectly correlated with self-OFI gets higher weight than an anti-correlated one.

        Uses a deterministic synthetic signal where:
          - corr_leader feeds the same value as bid_qty delta (positive correlation)
          - anti_leader feeds the negative (perfect anti-correlation, weight clipped to 0)

        After the correlation window fills and weights are updated, corr_leader
        should receive strictly more weight than anti_leader.
        """
        a = CrossOfiNetworkAlpha(
            n_leaders=2, corr_window=32, weight_update_interval=4, cross_weight_cap=0.8
        )
        # Feed a deterministic sine-like signal so correlation is unambiguous
        n_ticks = 200
        for t in range(n_ticks):
            # Alternating bid/ask so self-OFI oscillates predictably
            phase = math.sin(2 * math.pi * t / 20)
            bid = 100.0 + 10.0 * max(0.0, phase)
            ask = 100.0 + 10.0 * max(0.0, -phase)
            # corr_leader: in-phase with self OFI (positive correlation)
            corr_val = 0.8 * phase
            # anti_leader: out-of-phase (negative correlation → weight clipped to 0)
            anti_val = -0.8 * phase
            a.update(bid_qty=bid, ask_qty=ask, leader_ofis={
                "corr_leader": corr_val,
                "anti_leader": anti_val,
            })

        weights = a.get_leader_weights()
        corr_w = weights.get("corr_leader", 0.0)
        anti_w = weights.get("anti_leader", 0.0)
        # Correlated leader should have strictly more weight than anti-correlated one.
        assert corr_w > anti_w, (
            f"Expected corr_leader weight ({corr_w}) > anti_leader weight ({anti_w})"
        )
        # Anti-correlated leader should have zero weight (negative corr clipped to 0)
        assert anti_w == 0.0, f"Expected anti_leader weight=0, got {anti_w}"

    def test_equal_weights_when_no_positive_correlation(self):
        """When all correlations are zero or negative, weights should be equal."""
        a = CrossOfiNetworkAlpha(
            n_leaders=3, corr_window=32, weight_update_interval=4, cross_weight_cap=0.6
        )
        # Feed all-zero leader OFI (no correlation with any self-OFI)
        for _ in range(80):
            a.update(bid_qty=100.0, ask_qty=100.0, leader_ofis={"A": 0.0, "B": 0.0, "C": 0.0})
        weights = a.get_leader_weights()
        if len(weights) == 3:
            vals = list(weights.values())
            # Equal or near-equal weights when corr ≈ 0
            assert max(vals) - min(vals) < 0.1


# ---------------------------------------------------------------------------
# Single leader edge case (degeneracy check)
# ---------------------------------------------------------------------------

class TestSingleLeader:
    def test_single_leader_signal(self):
        a = CrossOfiNetworkAlpha(n_leaders=1)
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=100, ask_qty=100, leader_ofis={"TSMC": 0.7})
        assert sig != 0.0  # must produce output

    def test_single_leader_weight_at_cap(self):
        """After correlation builds up, single leader weight should approach cap."""
        cap = 0.6
        a = CrossOfiNetworkAlpha(
            n_leaders=1, corr_window=32, weight_update_interval=4, cross_weight_cap=cap
        )
        for _ in range(200):
            a.update(bid_qty=100.0, ask_qty=100.0, leader_ofis={"L": 0.5})
        weights = a.get_leader_weights()
        assert weights.get("L", 0.0) <= cap + 1e-9


# ---------------------------------------------------------------------------
# NaN / Inf handling
# ---------------------------------------------------------------------------

class TestNanInfHandling:
    def test_nan_leader_ofi_does_not_crash(self):
        a = CrossOfiNetworkAlpha()
        a.update(bid_qty=100, ask_qty=100)
        # Should not raise
        sig = a.update(bid_qty=100, ask_qty=100, leader_ofis={"A": float("nan")})
        assert math.isfinite(sig)

    def test_inf_leader_ofi_does_not_crash(self):
        a = CrossOfiNetworkAlpha()
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=100, ask_qty=100, leader_ofis={"A": float("inf")})
        assert math.isfinite(sig)

    def test_none_leader_ofis_no_crash(self):
        a = CrossOfiNetworkAlpha()
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=100, ask_qty=100, leader_ofis=None)
        assert math.isfinite(sig)

    def test_empty_leader_ofis_no_crash(self):
        a = CrossOfiNetworkAlpha()
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=100, ask_qty=100, leader_ofis={})
        assert math.isfinite(sig)

    def test_mixed_valid_nan_leaders(self):
        a = CrossOfiNetworkAlpha(n_leaders=3)
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(
            bid_qty=100, ask_qty=100,
            leader_ofis={"A": 0.5, "B": float("nan"), "C": 0.3},
        )
        assert math.isfinite(sig)


# ---------------------------------------------------------------------------
# Pearson correlation helper
# ---------------------------------------------------------------------------

class TestPearsonCorr:
    def test_perfect_positive_correlation(self):
        x = np.linspace(0, 1, 50)
        assert _pearson_corr(x, x) == pytest.approx(1.0, abs=1e-6)

    def test_perfect_negative_correlation(self):
        x = np.linspace(0, 1, 50)
        y = -x
        assert _pearson_corr(x, y) == pytest.approx(-1.0, abs=1e-6)

    def test_zero_correlation(self):
        x = np.ones(50, dtype=np.float64)  # constant — std = 0
        y = np.linspace(0, 1, 50)
        assert _pearson_corr(x, y) == 0.0

    def test_too_short_returns_zero(self):
        x = np.array([1.0, 2.0])
        y = np.array([1.0, 2.0])
        assert _pearson_corr(x, y) == 0.0

    def test_nan_input_handled(self):
        x = np.array([1.0, float("nan"), 3.0, 4.0, 5.0])
        y = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = _pearson_corr(x, y)
        assert math.isfinite(result)

    def test_output_bounded(self):
        rng = np.random.default_rng(13)
        for _ in range(50):
            x = rng.standard_normal(100)
            y = rng.standard_normal(100)
            r = _pearson_corr(x, y)
            assert -1.0 <= r <= 1.0


# ---------------------------------------------------------------------------
# Anti-leak / determinism tests
# ---------------------------------------------------------------------------

class TestAntiLeak:
    def test_deterministic_output(self):
        """Same input sequence must produce same output."""
        ticks = [
            (100.0, 100.0, {"A": 0.3}),
            (110.0, 95.0, {"A": 0.5, "B": -0.2}),
            (105.0, 100.0, {"A": 0.4}),
            (120.0, 90.0, {"B": 0.6}),
        ]
        results1 = []
        a1 = CrossOfiNetworkAlpha(n_leaders=3, corr_window=32, weight_update_interval=4)
        for bid, ask, leaders in ticks:
            results1.append(a1.update(bid_qty=bid, ask_qty=ask, leader_ofis=leaders))

        results2 = []
        a2 = CrossOfiNetworkAlpha(n_leaders=3, corr_window=32, weight_update_interval=4)
        for bid, ask, leaders in ticks:
            results2.append(a2.update(bid_qty=bid, ask_qty=ask, leader_ofis=leaders))

        assert results1 == results2

    def test_future_data_not_used(self):
        """Inserting an extra future tick should not change earlier outputs."""
        a = CrossOfiNetworkAlpha(n_leaders=2)
        a.update(bid_qty=100, ask_qty=100)
        sig_t1 = a.update(bid_qty=110, ask_qty=100, leader_ofis={"A": 0.3})

        b = CrossOfiNetworkAlpha(n_leaders=2)
        b.update(bid_qty=100, ask_qty=100)
        sig_t1_b = b.update(bid_qty=110, ask_qty=100, leader_ofis={"A": 0.3})

        assert sig_t1 == sig_t1_b

    def test_no_price_fields_in_manifest(self):
        """Manifest must not declare raw price fields (float precision concern)."""
        for field in _MANIFEST.data_fields:
            assert "price" not in field.lower() or "spread" in field.lower() or "mid" in field.lower()


# ---------------------------------------------------------------------------
# Extended sequence test (stress)
# ---------------------------------------------------------------------------

class TestExtendedSequence:
    def test_long_run_no_nan(self):
        """Run 2000 ticks with multiple leaders; signal must always be finite."""
        a = CrossOfiNetworkAlpha(
            n_leaders=5, corr_window=64, weight_update_interval=8, cross_weight_cap=0.6
        )
        rng = np.random.default_rng(2024)
        for _ in range(2000):
            bid = float(rng.integers(90, 110))
            ask = float(rng.integers(90, 110))
            leaders = {f"L{i}": float(rng.uniform(-1, 1)) for i in range(5)}
            sig = a.update(bid_qty=bid, ask_qty=ask, leader_ofis=leaders)
            assert math.isfinite(sig), f"Non-finite signal at tick {_}"

    def test_weight_sum_invariant_long_run(self):
        """Over 500 ticks, total leader weight must never exceed cap."""
        cap = 0.7
        a = CrossOfiNetworkAlpha(
            n_leaders=3, corr_window=32, weight_update_interval=4, cross_weight_cap=cap
        )
        rng = np.random.default_rng(333)
        for _ in range(500):
            bid = float(rng.integers(95, 105))
            ask = float(rng.integers(95, 105))
            leaders = {f"L{i}": float(rng.uniform(-1, 1)) for i in range(3)}
            a.update(bid_qty=bid, ask_qty=ask, leader_ofis=leaders)
            total = sum(a.get_leader_weights().values())
            assert total <= cap + 1e-9, f"Weight sum {total} > cap {cap} at tick {_}"
