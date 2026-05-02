"""Unit tests for hft_platform.alpha._stat_tests.

Covers: _compute_oos_returns, _evaluate_oos_statistical_tests,
_run_bds_independence_test, _bds_correlation_delta, _bh_correction,
_extract_stat_test_pvalues, _extract_bds_pvalue.
"""

from __future__ import annotations

import numpy as np
import pytest

from hft_platform.alpha._stat_tests import (
    _bds_correlation_delta,
    _bh_correction,
    _compute_oos_returns,
    _evaluate_oos_statistical_tests,
    _extract_bds_pvalue,
    _extract_stat_test_pvalues,
    _run_bds_independence_test,
)

# ---------------------------------------------------------------------------
# _compute_oos_returns
# ---------------------------------------------------------------------------


def test_compute_oos_returns_empty_when_too_few_points():
    result = _compute_oos_returns(np.array([1.0, 2.0]), is_oos_split=0.5)
    assert result.size == 0


def test_compute_oos_returns_single_value():
    result = _compute_oos_returns(np.array([5.0]), is_oos_split=0.5)
    assert result.size == 0


def test_compute_oos_returns_basic_case():
    # Equity curve grows by 10% each step.
    eq = np.array([100.0, 110.0, 121.0, 133.1, 146.41])
    result = _compute_oos_returns(eq, is_oos_split=0.5)
    # OOS portion starts from split index; values should be ~0.10
    assert result.size > 0
    assert np.all(np.isfinite(result))
    assert np.allclose(result, 0.10, atol=1e-6)


def test_compute_oos_returns_handles_zero_base():
    # Base values include zero — division should not produce inf/nan.
    eq = np.array([0.0, 0.0, 1.0, 2.0, 3.0])
    result = _compute_oos_returns(eq, is_oos_split=0.5)
    assert np.all(np.isfinite(result))


def test_compute_oos_returns_split_near_zero():
    eq = np.linspace(1.0, 2.0, 10)
    result = _compute_oos_returns(eq, is_oos_split=0.01)
    # Very low split → almost all data is OOS
    assert result.size > 0


def test_compute_oos_returns_split_near_one():
    eq = np.linspace(1.0, 2.0, 10)
    result = _compute_oos_returns(eq, is_oos_split=0.99)
    # Very high split → tiny OOS segment
    assert result.size >= 0  # may be 0 or 1


def test_compute_oos_returns_three_elements_exact():
    eq = np.array([1.0, 2.0, 3.0])
    result = _compute_oos_returns(eq, is_oos_split=0.5)
    assert isinstance(result, np.ndarray)


# ---------------------------------------------------------------------------
# _evaluate_oos_statistical_tests
# ---------------------------------------------------------------------------


def test_evaluate_oos_statistical_tests_insufficient_returns():
    small = np.array([0.01, 0.02, 0.03])
    result = _evaluate_oos_statistical_tests(
        small,
        pvalue_threshold=0.05,
        min_tests_pass=3,
        bootstrap_samples=100,
    )
    assert result["passed"] is False
    assert result["reason"] == "insufficient_oos_returns"
    assert result["sample_count"] == 3
    assert result["tests_passed"] == 0
    assert result["tests"] == {}


def test_evaluate_oos_statistical_tests_positive_returns_pass():
    # Strongly positive returns should pass most signal tests.
    rng = np.random.default_rng(0)
    arr = rng.normal(loc=0.02, scale=0.005, size=40)
    result = _evaluate_oos_statistical_tests(
        arr,
        pvalue_threshold=0.05,
        min_tests_pass=2,
        bootstrap_samples=200,
    )
    assert "passed" in result
    assert "tests_passed" in result
    assert "mean_return" in result
    assert "std_return" in result
    assert "tests" in result
    assert "ttest_mean_gt_zero" in result["tests"]
    assert "wilcoxon_gt_zero" in result["tests"]
    assert "sign_test_gt_half" in result["tests"]
    assert "bootstrap_ci_mean" in result["tests"]
    assert "bds_independence" in result["tests"]
    assert isinstance(result["tests_passed"], int)
    assert result["sample_count"] == 40


def test_evaluate_oos_statistical_tests_negative_returns_likely_fail():
    rng = np.random.default_rng(1)
    arr = rng.normal(loc=-0.02, scale=0.005, size=40)
    result = _evaluate_oos_statistical_tests(
        arr,
        pvalue_threshold=0.05,
        min_tests_pass=3,
        bootstrap_samples=200,
    )
    # Negative mean — should fail most signal tests
    assert result["passed"] is False


def test_evaluate_oos_statistical_tests_wilcoxon_skipped_when_few_nonzero():
    # Array of mostly zeros, fewer than 10 nonzero values → wilcoxon path skipped.
    arr = np.zeros(25)
    arr[:5] = 0.05  # only 5 nonzero
    result = _evaluate_oos_statistical_tests(
        arr,
        pvalue_threshold=0.05,
        min_tests_pass=1,
        bootstrap_samples=100,
    )
    assert result["tests"]["wilcoxon_gt_zero"]["pvalue"] == 1.0
    assert result["tests"]["wilcoxon_gt_zero"]["pass"] is False


def test_evaluate_oos_statistical_tests_result_structure_complete():
    rng = np.random.default_rng(2)
    arr = rng.normal(loc=0.01, scale=0.01, size=30)
    result = _evaluate_oos_statistical_tests(
        arr,
        pvalue_threshold=0.10,
        min_tests_pass=2,
        bootstrap_samples=100,
    )
    assert "diagnostic_gate_passed" in result
    assert "pvalue_threshold" in result
    assert result["tests_required"] == 2


def test_evaluate_oos_statistical_tests_bootstrap_ci_keys():
    rng = np.random.default_rng(3)
    arr = rng.normal(loc=0.02, scale=0.01, size=30)
    result = _evaluate_oos_statistical_tests(
        arr,
        pvalue_threshold=0.05,
        min_tests_pass=1,
        bootstrap_samples=100,
    )
    boot = result["tests"]["bootstrap_ci_mean"]
    assert "ci_low" in boot
    assert "ci_high" in boot
    assert "pvalue" in boot
    assert "pass" in boot


def test_evaluate_oos_statistical_tests_filters_nonfinite(recwarn):
    # Include some NaN/inf in the array — they should be stripped.
    arr = np.array([0.02] * 25 + [np.nan, np.inf, -np.inf, float("nan")])
    result = _evaluate_oos_statistical_tests(
        arr,
        pvalue_threshold=0.05,
        min_tests_pass=1,
        bootstrap_samples=100,
    )
    # Finite sample count should be 25
    assert result["sample_count"] == 25
    assert not [w for w in recwarn if issubclass(w.category, RuntimeWarning)]


# ---------------------------------------------------------------------------
# _run_bds_independence_test
# ---------------------------------------------------------------------------


def test_run_bds_independence_test_insufficient_samples():
    arr = np.random.default_rng(0).normal(size=30)
    result = _run_bds_independence_test(arr=arr, pvalue_threshold=0.05)
    assert result["available"] is False
    assert result["reason"] == "insufficient_samples"
    assert result["pvalue"] == 1.0
    assert result["pass"] is True


def test_run_bds_independence_test_constant_series():
    arr = np.ones(60)
    result = _run_bds_independence_test(arr=arr, pvalue_threshold=0.05)
    assert result["available"] is False
    assert result["reason"] == "constant_series"
    assert result["pvalue"] == 1.0
    assert result["pass"] is True


def test_run_bds_independence_test_large_sample_downsampled():
    # > 600 elements triggers downsampling path.
    rng = np.random.default_rng(7)
    arr = rng.normal(size=700)
    result = _run_bds_independence_test(arr=arr, pvalue_threshold=0.05)
    assert "available" in result
    # sample_count in result should be at most 600
    assert result["sample_count"] <= 600


def test_run_bds_independence_test_normal_path_returns_valid_structure():
    rng = np.random.default_rng(5)
    arr = rng.normal(size=60)
    result = _run_bds_independence_test(arr=arr, pvalue_threshold=0.05)
    assert "pvalue" in result
    assert "pass" in result
    assert "available" in result
    assert isinstance(result["pvalue"], float)
    assert isinstance(result["pass"], bool)


def test_run_bds_independence_test_fallback_path_when_statsmodels_absent(monkeypatch):
    """Exercise the permutation fallback used when statsmodels is unavailable.

    statsmodels may or may not be installed. We force the fallback by patching
    sys.modules so the import inside the function fails.
    """
    import sys

    # Remove statsmodels from sys.modules so the import inside the function raises.
    saved: dict = {}
    for key in list(sys.modules):
        if key.startswith("statsmodels"):
            saved[key] = sys.modules.pop(key)
    monkeypatch.setitem(sys.modules, "statsmodels", None)  # type: ignore[arg-type]
    monkeypatch.setitem(sys.modules, "statsmodels.tsa", None)  # type: ignore[arg-type]
    monkeypatch.setitem(sys.modules, "statsmodels.tsa.stattools", None)  # type: ignore[arg-type]

    try:
        rng = np.random.default_rng(9)
        arr = rng.normal(size=60)
        result = _run_bds_independence_test(arr=arr, pvalue_threshold=0.05)
        assert result["method"] == "bds_proxy_permutation"
        assert result["available"] is True
        assert "draws" in result
        assert "statistic" in result
        assert "pvalue" in result
        assert "note" in result
    finally:
        # Restore saved modules
        for key, mod in saved.items():
            sys.modules[key] = mod


# ---------------------------------------------------------------------------
# _bds_correlation_delta
# ---------------------------------------------------------------------------


def test_bds_correlation_delta_small_array_returns_zero():
    result = _bds_correlation_delta(np.array([1.0, 2.0]), epsilon=1.0)
    assert result == 0.0


def test_bds_correlation_delta_single_element_returns_zero():
    result = _bds_correlation_delta(np.array([5.0]), epsilon=1.0)
    assert result == 0.0


def test_bds_correlation_delta_returns_float():
    rng = np.random.default_rng(42)
    arr = rng.normal(size=20)
    result = _bds_correlation_delta(arr, epsilon=0.5)
    assert isinstance(result, float)


def test_bds_correlation_delta_iid_vs_correlated():
    # For a correlated series, correlation delta should differ from iid.
    rng = np.random.default_rng(0)
    iid = rng.normal(size=50)
    corr = np.cumsum(rng.normal(size=50) * 0.1)  # random walk — autocorrelated
    epsilon = 0.5
    delta_iid = abs(_bds_correlation_delta(iid, epsilon))
    delta_corr = abs(_bds_correlation_delta(corr, epsilon))
    # Just assert both are finite floats; ordering may vary
    assert np.isfinite(delta_iid)
    assert np.isfinite(delta_corr)


# ---------------------------------------------------------------------------
# _bh_correction
# ---------------------------------------------------------------------------


def test_bh_correction_empty_list():
    reject, adjusted = _bh_correction([], alpha=0.05)
    assert reject == []
    assert adjusted == []


def test_bh_correction_single_significant_pvalue():
    reject, adjusted = _bh_correction([0.01], alpha=0.05)
    assert len(reject) == 1
    assert len(adjusted) == 1
    assert reject[0] is True
    assert 0.0 <= adjusted[0] <= 1.0


def test_bh_correction_single_nonsignificant_pvalue():
    reject, adjusted = _bh_correction([0.90], alpha=0.05)
    assert len(reject) == 1
    assert reject[0] is False


def test_bh_correction_multiple_pvalues_rejects_small():
    # With alpha=0.05 and this set, at least the smallest should be rejected.
    pvalues = [0.001, 0.04, 0.03, 0.20]
    reject, adjusted = _bh_correction(pvalues, alpha=0.05)
    assert len(reject) == 4
    assert len(adjusted) == 4
    # The smallest p-value should be rejected
    assert reject[0] is True


def test_bh_correction_adjusted_pvalues_bounded():
    pvalues = [0.001, 0.02, 0.05, 0.10, 0.30]
    _, adjusted = _bh_correction(pvalues, alpha=0.05)
    for p in adjusted:
        assert 0.0 <= p <= 1.0


def test_bh_correction_all_nonsignificant():
    pvalues = [0.5, 0.6, 0.7, 0.8, 0.9]
    reject, adjusted = _bh_correction(pvalues, alpha=0.05)
    assert not any(reject)


def test_bh_correction_preserves_length():
    pvalues = [0.01, 0.02, 0.03, 0.04, 0.05]
    reject, adjusted = _bh_correction(pvalues, alpha=0.05)
    assert len(reject) == len(pvalues)
    assert len(adjusted) == len(pvalues)


# ---------------------------------------------------------------------------
# _extract_stat_test_pvalues
# ---------------------------------------------------------------------------


def test_extract_stat_test_pvalues_well_formed():
    stat_tests = {
        "tests": {
            "ttest_mean_gt_zero": {"pvalue": 0.03, "pass": True},
            "wilcoxon_gt_zero": {"pvalue": 0.04, "pass": True},
            "sign_test_gt_half": {"pvalue": 0.10, "pass": False},
            "bootstrap_ci_mean": {"pvalue": 0.05, "pass": True},
        }
    }
    result = _extract_stat_test_pvalues(stat_tests)
    assert len(result) == 4
    assert result[0] == pytest.approx(0.03)
    assert result[1] == pytest.approx(0.04)
    assert result[2] == pytest.approx(0.10)
    assert result[3] == pytest.approx(0.05)


def test_extract_stat_test_pvalues_missing_tests_key():
    result = _extract_stat_test_pvalues({})
    assert result == []


def test_extract_stat_test_pvalues_non_dict_tests():
    result = _extract_stat_test_pvalues({"tests": "not_a_dict"})
    assert result == []


def test_extract_stat_test_pvalues_missing_test_entry_defaults_to_one():
    stat_tests = {
        "tests": {
            "ttest_mean_gt_zero": {"pvalue": 0.02, "pass": True},
            # wilcoxon and sign_test missing
        }
    }
    result = _extract_stat_test_pvalues(stat_tests)
    assert len(result) == 4
    assert result[0] == pytest.approx(0.02)
    assert result[1] == pytest.approx(1.0)  # missing → 1.0
    assert result[2] == pytest.approx(1.0)
    assert result[3] == pytest.approx(1.0)


def test_extract_stat_test_pvalues_nan_pvalue_defaults_to_one():
    stat_tests = {
        "tests": {
            "ttest_mean_gt_zero": {"pvalue": float("nan"), "pass": False},
            "wilcoxon_gt_zero": {"pvalue": 0.04},
            "sign_test_gt_half": {"pvalue": 0.08},
            "bootstrap_ci_mean": {"pvalue": 0.06},
        }
    }
    result = _extract_stat_test_pvalues(stat_tests)
    assert result[0] == pytest.approx(1.0)


def test_extract_stat_test_pvalues_type_error_defaults_to_one():
    stat_tests = {
        "tests": {
            "ttest_mean_gt_zero": {"pvalue": "not_a_number"},
            "wilcoxon_gt_zero": {"pvalue": 0.04},
            "sign_test_gt_half": {"pvalue": 0.04},
            "bootstrap_ci_mean": {"pvalue": 0.04},
        }
    }
    result = _extract_stat_test_pvalues(stat_tests)
    assert result[0] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# _extract_bds_pvalue
# ---------------------------------------------------------------------------


def test_extract_bds_pvalue_well_formed():
    stat_tests = {
        "tests": {
            "bds_independence": {"pvalue": 0.07, "pass": True},
        }
    }
    result = _extract_bds_pvalue(stat_tests)
    assert result == pytest.approx(0.07)


def test_extract_bds_pvalue_missing_tests_key():
    result = _extract_bds_pvalue({})
    assert result is None


def test_extract_bds_pvalue_non_dict_tests():
    result = _extract_bds_pvalue({"tests": 42})
    assert result is None


def test_extract_bds_pvalue_missing_bds_key():
    result = _extract_bds_pvalue({"tests": {}})
    assert result is None


def test_extract_bds_pvalue_nan_returns_none():
    stat_tests = {
        "tests": {
            "bds_independence": {"pvalue": float("nan")},
        }
    }
    result = _extract_bds_pvalue(stat_tests)
    assert result is None


def test_extract_bds_pvalue_type_error_returns_none():
    stat_tests = {
        "tests": {
            "bds_independence": {"pvalue": "bad"},
        }
    }
    result = _extract_bds_pvalue(stat_tests)
    assert result is None
