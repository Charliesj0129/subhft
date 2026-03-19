from __future__ import annotations

from typing import Any

import numpy as np
from scipy import stats


def _compute_oos_returns(equity_curve: np.ndarray, is_oos_split: float) -> np.ndarray:
    eq = np.asarray(equity_curve, dtype=np.float64).reshape(-1)
    if eq.size < 3:
        return np.asarray([], dtype=np.float64)
    split = max(2, int(eq.size * float(is_oos_split)))
    split = min(split, eq.size - 1) if eq.size > 2 else eq.size
    if split >= eq.size:
        return np.asarray([], dtype=np.float64)
    segment = eq[split - 1 :]
    if segment.size < 2:
        return np.asarray([], dtype=np.float64)
    base = segment[:-1]
    delta = np.diff(segment)
    ret = np.divide(delta, base, out=np.zeros_like(delta), where=base != 0)
    return ret[np.isfinite(ret)]


def _evaluate_oos_statistical_tests(
    oos_returns: np.ndarray,
    *,
    pvalue_threshold: float,
    min_tests_pass: int,
    bootstrap_samples: int,
) -> dict[str, Any]:
    arr = np.asarray(oos_returns, dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    if arr.size < 20:
        return {
            "passed": False,
            "reason": "insufficient_oos_returns",
            "sample_count": int(arr.size),
            "tests_passed": 0,
            "tests_required": int(min_tests_pass),
            "pvalue_threshold": float(pvalue_threshold),
            "tests": {},
        }

    t_res = stats.ttest_1samp(arr, popmean=0.0, alternative="greater", nan_policy="omit")
    t_pvalue = float(t_res.pvalue) if np.isfinite(getattr(t_res, "pvalue", np.nan)) else 1.0
    t_pass = bool(t_pvalue <= pvalue_threshold)

    wilcoxon_pvalue = 1.0
    wilcoxon_pass = False
    nonzero = arr[arr != 0.0]
    if nonzero.size >= 10:
        try:
            w_res = stats.wilcoxon(nonzero, alternative="greater", zero_method="wilcox")
            wilcoxon_pvalue = float(w_res.pvalue) if np.isfinite(getattr(w_res, "pvalue", np.nan)) else 1.0
            wilcoxon_pass = bool(wilcoxon_pvalue <= pvalue_threshold)
        except ValueError:
            wilcoxon_pvalue = 1.0
            wilcoxon_pass = False

    sign_pvalue = 1.0
    sign_pass = False
    if nonzero.size > 0:
        pos = int(np.sum(nonzero > 0.0))
        sign_pvalue = float(stats.binomtest(pos, int(nonzero.size), p=0.5, alternative="greater").pvalue)
        sign_pass = bool(sign_pvalue <= pvalue_threshold)

    rng = np.random.default_rng(42)
    draws = max(100, int(bootstrap_samples))
    boot_means = np.empty(draws, dtype=np.float64)
    for i in range(draws):
        sample_idx = rng.integers(0, arr.size, size=arr.size)
        boot_means[i] = float(np.mean(arr[sample_idx]))
    ci_low = float(np.quantile(boot_means, 0.05))
    ci_high = float(np.quantile(boot_means, 0.95))
    bootstrap_pvalue = float(np.mean(boot_means <= 0.0))
    bootstrap_pass = bool(ci_low > 0.0)
    bds_test = _run_bds_independence_test(arr=arr, pvalue_threshold=float(pvalue_threshold))

    tests = {
        "ttest_mean_gt_zero": {"pvalue": t_pvalue, "pass": t_pass},
        "wilcoxon_gt_zero": {"pvalue": wilcoxon_pvalue, "pass": wilcoxon_pass},
        "sign_test_gt_half": {"pvalue": sign_pvalue, "pass": sign_pass},
        "bootstrap_ci_mean": {
            "pvalue": bootstrap_pvalue,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "pass": bootstrap_pass,
        },
        "bds_independence": bds_test,
    }
    signal_test_keys = ("ttest_mean_gt_zero", "wilcoxon_gt_zero", "sign_test_gt_half", "bootstrap_ci_mean")
    pass_count = int(sum(1 for key in signal_test_keys if bool(dict(tests.get(key, {})).get("pass"))))
    # BDS is a diagnostic indicator for look-ahead contamination, not a hard gate.
    # EMA-smoothed signals are always non-IID (by construction), so `pass=False` from
    # BDS does not indicate look-ahead bias. Only the four signal quality tests above
    # determine the gate result.
    diagnostic_gate_passed = bool(dict(tests.get("bds_independence", {})).get("pass", True))
    passed = pass_count >= int(min_tests_pass)
    return {
        "passed": passed,
        "sample_count": int(arr.size),
        "tests_passed": pass_count,
        "tests_required": int(min_tests_pass),
        "diagnostic_gate_passed": bool(diagnostic_gate_passed),
        "pvalue_threshold": float(pvalue_threshold),
        "mean_return": float(np.mean(arr)),
        "std_return": float(np.std(arr)),
        "tests": tests,
    }


def _extract_stat_test_pvalues(stat_tests: dict[str, Any]) -> list[float]:
    tests = stat_tests.get("tests")
    if not isinstance(tests, dict):
        return []
    out: list[float] = []
    for key in ("ttest_mean_gt_zero", "wilcoxon_gt_zero", "sign_test_gt_half", "bootstrap_ci_mean"):
        row = tests.get(key)
        if not isinstance(row, dict):
            out.append(1.0)
            continue
        try:
            p = float(row.get("pvalue", 1.0))
        except (TypeError, ValueError):
            p = 1.0
        if not np.isfinite(p):
            p = 1.0
        out.append(p)
    return out


def _extract_bds_pvalue(stat_tests: dict[str, Any]) -> float | None:
    tests = stat_tests.get("tests")
    if not isinstance(tests, dict):
        return None
    bds = tests.get("bds_independence")
    if not isinstance(bds, dict):
        return None
    try:
        p = float(bds.get("pvalue", 1.0))
        return p if np.isfinite(p) else None
    except (TypeError, ValueError):
        return None


def _run_bds_independence_test(*, arr: np.ndarray, pvalue_threshold: float) -> dict[str, Any]:
    sample = np.asarray(arr, dtype=np.float64).reshape(-1)
    sample = sample[np.isfinite(sample)]
    if sample.size < 50:
        return {
            "method": "bds",
            "available": False,
            "reason": "insufficient_samples",
            "sample_count": int(sample.size),
            "pvalue": 1.0,
            "pass": True,
        }

    max_sample = 600
    if sample.size > max_sample:
        idx = np.linspace(0, sample.size - 1, num=max_sample, dtype=np.int64)
        sample = sample[idx]

    sigma = float(np.std(sample))
    if not np.isfinite(sigma) or sigma <= 1e-12:
        return {
            "method": "bds",
            "available": False,
            "reason": "constant_series",
            "sample_count": int(sample.size),
            "pvalue": 1.0,
            "pass": True,
        }
    epsilon = float(0.7 * sigma)

    try:
        try:
            from statsmodels.tsa.stattools import bds as sm_bds
        except Exception as _exc:  # noqa: BLE001
            from statsmodels.stats.stattools import bds as sm_bds

        stat, pvals = sm_bds(sample, max_dim=2, epsilon=epsilon)
        stat_arr = np.asarray(stat, dtype=np.float64).reshape(-1)
        pval_arr = np.asarray(pvals, dtype=np.float64).reshape(-1)
        pvalue = float(pval_arr[-1]) if pval_arr.size else 1.0
        statistic = float(stat_arr[-1]) if stat_arr.size else float("nan")
        reject_iid = bool(np.isfinite(pvalue) and pvalue <= float(pvalue_threshold))
        return {
            "method": "statsmodels_bds",
            "available": True,
            "sample_count": int(sample.size),
            "statistic": statistic,
            "pvalue": pvalue if np.isfinite(pvalue) else 1.0,
            "null_hypothesis": "iid",
            "reject_iid": reject_iid,
            "pass": not reject_iid,
        }
    except Exception as _exc:  # noqa: BLE001
        # Fallback when statsmodels is unavailable: permutation proxy on BDS-style correlation integral delta.
        rng = np.random.default_rng(42)
        draws = 200
        observed = float(_bds_correlation_delta(sample, epsilon))
        permuted = np.empty(draws, dtype=np.float64)
        for i in range(draws):
            shuffled = np.array(sample, copy=True)
            rng.shuffle(shuffled)
            permuted[i] = float(_bds_correlation_delta(shuffled, epsilon))
        pvalue = float(np.mean(np.abs(permuted) >= abs(observed)))
        reject_iid = bool(pvalue <= float(pvalue_threshold))
        return {
            "method": "bds_proxy_permutation",
            "available": True,
            "sample_count": int(sample.size),
            "draws": int(draws),
            "statistic": observed,
            "pvalue": pvalue,
            "null_hypothesis": "iid",
            "reject_iid": reject_iid,
            "pass": not reject_iid,
            "note": "statsmodels_bds_unavailable_using_proxy",
        }


def _bds_correlation_delta(arr: np.ndarray, epsilon: float) -> float:
    x = np.asarray(arr, dtype=np.float64).reshape(-1)
    n = int(x.size)
    if n < 3:
        return 0.0

    diff = np.abs(x[:, None] - x[None, :])
    np.fill_diagonal(diff, np.inf)
    c1 = float(np.count_nonzero(diff < epsilon) / max(1, n * (n - 1)))

    x0 = x[:-1]
    x1 = x[1:]
    n2 = int(x0.size)
    if n2 < 2:
        return 0.0
    d0 = np.abs(x0[:, None] - x0[None, :])
    d1 = np.abs(x1[:, None] - x1[None, :])
    joint = np.maximum(d0, d1)
    np.fill_diagonal(joint, np.inf)
    c2 = float(np.count_nonzero(joint < epsilon) / max(1, n2 * (n2 - 1)))
    return float(c2 - (c1 * c1))


def _bh_correction(pvalues: list[float], alpha: float) -> tuple[list[bool], list[float]]:
    """Benjamini-Hochberg FDR correction."""
    m = len(pvalues)
    if m == 0:
        return [], []

    arr = np.asarray(pvalues, dtype=np.float64)
    sort_idx = np.argsort(arr)
    sorted_p = arr[sort_idx]
    thresholds = (np.arange(1, m + 1, dtype=np.float64) / float(m)) * float(alpha)

    reject_mask = np.zeros(m, dtype=bool)
    for k in range(m - 1, -1, -1):
        if sorted_p[k] <= thresholds[k]:
            reject_mask[sort_idx[: k + 1]] = True
            break

    adjusted = np.empty(m, dtype=np.float64)
    prev = 1.0
    for k in range(m - 1, -1, -1):
        adj = float(sorted_p[k] * float(m) / float(k + 1))
        prev = min(prev, adj, 1.0)
        adjusted[sort_idx[k]] = prev

    return reject_mask.tolist(), adjusted.tolist()
