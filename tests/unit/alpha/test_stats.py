"""Tests for ``hft_platform.alpha.stats``, the one statistics module (research v2 C3).

The ``*_matches_the_retired_definition`` tests pin values produced by the
definitions this module replaced (``_stat_tests._bh_correction``,
``_resampling``, ``research.combinatorial.smma_validation``) on fixed inputs,
so the move kept them numerically identical.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy.stats import norm, spearmanr

from hft_platform.alpha import stats

_PVALUES = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216]
_SERIES = np.sin(np.arange(60) / 4.0) + 0.1
_SIGNAL = np.cos(np.arange(60) / 3.0)


# ---------------------------------------------------------------------------
# Benjamini-Hochberg
# ---------------------------------------------------------------------------


def test_bh_matches_the_retired_definition() -> None:
    result = stats.benjamini_hochberg(_PVALUES, 0.05)
    assert result.rejected.tolist() == [True, True] + [False] * 8
    expected = [0.01, 0.04, 0.084, 0.084, 0.084, 0.1, 0.10571428571428572, 0.216, 0.216, 0.216]
    np.testing.assert_allclose(result.adjusted, expected, rtol=0, atol=1e-15)


def test_bh_empty_input_rejects_nothing() -> None:
    result = stats.benjamini_hochberg([], 0.05)
    assert result.rejected.tolist() == []
    assert result.adjusted.tolist() == []


@pytest.mark.parametrize(
    ("pvalues", "q", "rejected"),
    [
        ([0.01], 0.05, [True]),
        ([0.90], 0.05, [False]),
        ([0.01, 0.01, 0.01, 0.01], 0.1, [True, True, True, True]),
        ([0.5, 0.6, 0.7, 0.8], 0.1, [False, False, False, False]),
        ([0.01, 0.5, 0.6, 0.7], 0.1, [True, False, False, False]),
        ([0.001, 0.002, 0.5, 0.9], 0.05, [True, True, False, False]),
        ([0.04, 0.001, 0.03, 0.20], 0.05, [False, True, False, False]),
    ],
)
def test_bh_rejects_the_step_up_set_in_input_order(pvalues: list[float], q: float, rejected: list[bool]) -> None:
    result = stats.benjamini_hochberg(pvalues, q)
    assert result.rejected.tolist() == rejected
    assert len(result.adjusted) == len(pvalues)
    assert np.all((result.adjusted >= 0.0) & (result.adjusted <= 1.0))


def test_bh_adjusted_pvalues_are_monotone_in_the_raw_order() -> None:
    result = stats.benjamini_hochberg(_PVALUES, 0.05)
    order = np.argsort(_PVALUES)
    assert np.all(np.diff(result.adjusted[order]) >= 0.0)


def test_bh_untested_ledger_trials_make_rejection_stricter() -> None:
    in_hand = stats.benjamini_hochberg([0.01, 0.02], 0.05)
    with_ledger = stats.benjamini_hochberg([0.01, 0.02], 0.05, n_tests=10)
    assert in_hand.rejected.tolist() == [True, True]
    assert with_ledger.rejected.tolist() == [False, False]
    assert np.all(with_ledger.adjusted >= in_hand.adjusted)


def test_bh_refuses_fewer_tests_than_pvalues() -> None:
    with pytest.raises(ValueError, match="fewer than"):
        stats.benjamini_hochberg([0.01, 0.02, 0.03], 0.05, n_tests=2)


@pytest.mark.parametrize("bad", [math.nan, -0.1, 1.5])
def test_bh_refuses_a_pvalue_outside_the_unit_interval(bad: float) -> None:
    with pytest.raises(ValueError, match="within"):
        stats.benjamini_hochberg([0.01, bad], 0.05)


def test_effective_test_count_matches_the_retired_definition() -> None:
    assert stats.effective_test_count(np.vstack([_SERIES, _SIGNAL, _SERIES + _SIGNAL])) == 3


# ---------------------------------------------------------------------------
# Sharpe deflation
# ---------------------------------------------------------------------------


def test_deflated_sharpe_matches_the_retired_definition() -> None:
    value = stats.deflated_sharpe(
        0.12, effective_trials=50, observations=250, trial_sharpe_std=0.05, skewness=-0.4, kurtosis=5.0
    )
    assert value == pytest.approx(0.5377177695839965, abs=1e-15)
    one = stats.deflated_sharpe(0.12, effective_trials=1, observations=250, trial_sharpe_std=0.05)
    assert one == pytest.approx(0.9704055641229818, abs=1e-15)


def test_deflated_sharpe_is_psr_against_the_expected_maximum() -> None:
    sr0 = stats.expected_max_sharpe(50, 0.05)
    psr = stats.probabilistic_sharpe(0.12, observations=250, benchmark=sr0, skewness=-0.4, kurtosis=5.0)
    dsr = stats.deflated_sharpe(
        0.12, effective_trials=50, observations=250, trial_sharpe_std=0.05, skewness=-0.4, kurtosis=5.0
    )
    assert dsr == psr


def test_probabilistic_sharpe_matches_the_closed_form() -> None:
    sr, n, skew, kurt = 0.1, 400, 0.2, 4.0
    se = math.sqrt((1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr**2) / (n - 1))
    expected = float(norm.cdf(sr / se))
    assert stats.probabilistic_sharpe(sr, observations=n, skewness=skew, kurtosis=kurt) == pytest.approx(expected)


@pytest.mark.parametrize("observations", [0, 1])
def test_probabilistic_sharpe_needs_two_observations(observations: int) -> None:
    assert stats.probabilistic_sharpe(1.0, observations=observations) == 0.0


def test_expected_max_sharpe_is_zero_for_one_trial_and_grows_with_trials() -> None:
    assert stats.expected_max_sharpe(1, 0.5) == 0.0
    assert stats.expected_max_sharpe(100, 0.0) == 0.0
    values = [stats.expected_max_sharpe(n, 0.5) for n in (2, 10, 100, 1000)]
    assert all(b > a > 0.0 for a, b in zip(values, values[1:], strict=False))


def test_expected_max_sharpe_refuses_a_non_finite_input() -> None:
    with pytest.raises(ValueError, match="finite"):
        stats.expected_max_sharpe(math.inf, 0.5)


def test_null_sharpe_std_is_the_lo_estimator_spread_at_zero() -> None:
    assert stats.null_sharpe_std(101) == pytest.approx(0.1)
    with pytest.raises(ValueError, match="at least 2"):
        stats.null_sharpe_std(1)


def test_selection_sr0_of_one_trial_deflates_nothing() -> None:
    assert stats.selection_sr0(1, observations=30, trial_sharpes=[1.0, 3.0], annualization=252.0) == 0.0


def test_selection_sr0_floors_identical_trials_at_the_null_spread() -> None:
    floored = stats.selection_sr0(5, observations=101, trial_sharpes=[2.0] * 5)
    assert floored == pytest.approx(stats.expected_max_sharpe(5, 0.1))


def test_selection_sr0_uses_the_trials_spread_when_it_is_wider() -> None:
    trials = [0.4, 1.2, 2.0, 1.2, 0.4]
    sr0 = stats.selection_sr0(5, observations=100_000, trial_sharpes=trials, annualization=252.0)
    assert sr0 == pytest.approx(stats.expected_max_sharpe(5, float(np.std(trials, ddof=1))))


@pytest.mark.parametrize("n_trials", [2, 3, 5, 10, 30, 100, 1_000, 10_000])
@pytest.mark.parametrize("observations", [2, 10, 30, 250, 10_000, 1_000_000])
def test_selection_sr0_never_deflates_less_than_the_retired_penalty(n_trials: int, observations: int) -> None:
    """The retired haircut was ``sqrt(2 ln N / T)`` off an annualized Sharpe; SR0 is never smaller."""
    retired = math.sqrt(2.0 * math.log(n_trials) / observations)
    assert stats.selection_sr0(n_trials, observations=observations, annualization=252.0) >= retired


def test_sample_skew_kurtosis_is_normal_when_undefined() -> None:
    assert stats.sample_skew_kurtosis([1.0, 2.0]) == (0.0, 3.0)
    assert stats.sample_skew_kurtosis([5.0] * 10) == (0.0, 3.0)
    skew, kurt = stats.sample_skew_kurtosis([0.0, 0.0, 0.0, 10.0])
    assert skew > 0.0 and kurt > 1.0


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------


def test_moving_block_bootstrap_matches_the_retired_definition() -> None:
    assert stats.moving_block_bootstrap_mean(_SERIES, samples=200, seed=11) == (-0.207270638374547, 0.255)


def test_cluster_bootstrap_matches_the_retired_definition() -> None:
    clusters = np.repeat([f"d{i}" for i in range(12)], 5)
    assert stats.cluster_bootstrap_mean(_SERIES, clusters, samples=200, seed=11) == (-0.13313216896645513, 0.16)


def test_day_and_stationary_bootstraps_match_the_retired_definitions() -> None:
    stationary = stats.stationary_block_bootstrap(list(_SERIES), block_size=5, n_resamples=20, rng_seed=3)
    day = stats.day_bootstrap(list(_SERIES), n_resamples=20, rng_seed=3)
    assert float(stationary.sum()) == pytest.approx(246.97463480820096, abs=1e-9)
    assert float(day.sum()) == pytest.approx(265.8103753155091, abs=1e-9)


def test_block_bootstraps_of_too_few_values_report_no_evidence() -> None:
    assert stats.moving_block_bootstrap_mean([1.0], seed=1) == (0.0, 1.0)
    assert stats.cluster_bootstrap_mean([1.0, 2.0], ["a", "a"], seed=1) == (0.0, 1.0)


# ---------------------------------------------------------------------------
# Information coefficient
# ---------------------------------------------------------------------------


def test_rank_ic_matches_the_retired_definition() -> None:
    assert stats.rank_ic(_SIGNAL, _SERIES, min_obs=5) == pytest.approx(-0.09430397332592388, abs=1e-15)


def test_rank_ic_is_spearman_over_finite_pairs() -> None:
    signal = _SIGNAL.copy()
    signal[::7] = np.nan
    finite = np.isfinite(signal)
    expected = float(spearmanr(signal[finite], _SERIES[finite]).statistic)
    assert stats.rank_ic(signal, _SERIES, min_obs=5) == pytest.approx(expected)


def test_pearson_ic_is_the_correlation_over_finite_pairs() -> None:
    expected = float(np.corrcoef(_SIGNAL, _SERIES)[0, 1])
    assert stats.pearson_ic(_SIGNAL, _SERIES, min_obs=5) == pytest.approx(expected)


@pytest.mark.parametrize("estimator", [stats.rank_ic, stats.pearson_ic])
def test_ic_is_undefined_on_thin_or_constant_input(estimator) -> None:
    assert math.isnan(estimator(_SIGNAL[:4], _SERIES[:4], min_obs=5))
    assert math.isnan(estimator(np.ones(60), _SERIES, min_obs=5))
    assert math.isnan(estimator(_SIGNAL, np.full(60, 3.0), min_obs=5))


def test_min_detectable_ic_matches_the_fisher_z_form() -> None:
    z = float(norm.ppf(0.95) + norm.ppf(0.80))
    assert stats.min_detectable_ic(103) == pytest.approx(math.tanh(z / 10.0))


def test_min_detectable_ic_shrinks_with_sample_and_detects_nothing_below_four() -> None:
    values = [stats.min_detectable_ic(n) for n in (10, 100, 1_000, 100_000)]
    assert all(b < a for a, b in zip(values, values[1:], strict=False))
    assert stats.min_detectable_ic(3) == 1.0


@pytest.mark.parametrize(("alpha", "power"), [(0.0, 0.8), (0.05, 1.0)])
def test_min_detectable_ic_refuses_a_degenerate_level(alpha: float, power: float) -> None:
    with pytest.raises(ValueError, match="in \\(0, 1\\)"):
        stats.min_detectable_ic(100, alpha=alpha, power=power)


# ---------------------------------------------------------------------------
# Plain Sharpe and chunked IC (research v2 C4)
# ---------------------------------------------------------------------------


def test_sharpe_ratio_is_mean_over_population_std() -> None:
    values = np.array([1.0, -0.5, 2.0, 0.25, -1.0])
    expected = float(np.mean(values) / np.std(values) * math.sqrt(252.0))
    assert stats.sharpe_ratio(values, annualization=252.0) == pytest.approx(expected, rel=1e-15)


@pytest.mark.parametrize("values", [[], [3.0], [5.0] * 40, [0.1] * 7, [1.1] * 30, [1e9 + 0.3] * 25])
def test_sharpe_ratio_of_a_constant_or_short_series_is_zero(values: list[float]) -> None:
    """``np.std`` of a constant series can come out as ~1e-16, not 0; the retired copies
    divided by it and reported Sharpe in the 1e16 range for a strategy that earns the same
    every day."""
    assert stats.sharpe_ratio(values, annualization=252.0) == 0.0


def test_chunked_ic_matches_the_retired_definition() -> None:
    mean, std, series = stats.chunked_ic(_SIGNAL, _SERIES, buckets=5)
    assert series.size == 5
    chunks = [float(np.corrcoef(_SIGNAL[i : i + 12], _SERIES[i : i + 12])[0, 1]) for i in range(0, 60, 12)]
    np.testing.assert_allclose(series, chunks, rtol=0, atol=1e-12)
    assert mean == pytest.approx(float(np.mean(chunks)))
    assert std == pytest.approx(float(np.std(chunks)))


def test_chunked_ic_drops_constant_chunks_and_short_input() -> None:
    signal = _SIGNAL.copy()
    signal[:12] = 1.0
    _, _, series = stats.chunked_ic(signal, _SERIES, buckets=5)
    assert series.size == 4
    assert stats.chunked_ic(_SIGNAL[:7], _SERIES[:7]) == (0.0, 0.0, pytest.approx(np.asarray([])))


def test_ic_ttest_is_one_sided_for_a_positive_mean() -> None:
    t_pos, p_pos = stats.ic_ttest([0.1, 0.12, 0.08, 0.11, 0.09])
    t_neg, p_neg = stats.ic_ttest([-0.1, -0.12, -0.08, -0.11, -0.09])
    assert t_pos > 0 and p_pos < 0.01
    assert t_neg < 0 and p_neg > 0.99
    assert stats.ic_ttest([0.1, np.nan]) == (0.0, 1.0)
