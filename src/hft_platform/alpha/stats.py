"""The one statistics module for research gates and generators (research v2 C3).

Every multiple-testing correction, resampler, and information-coefficient
estimate that a gate or a generator relies on is defined here once; the
gates in ``hft_platform.alpha`` and the generators under ``research/`` import
these, and none keeps a private copy.

Deflated Sharpe has one definition: the Bailey and Lopez de Prado (2014)
probability that the true Sharpe exceeds ``SR0``, the Sharpe the best of
``N`` null trials is expected to reach. Gates whose pre-registered
thresholds are in Sharpe units (``deflated_sharpe_min``,
``opt_min_deflated_sharpe``) compare ``SR - SR0`` against them, with ``SR0``
from :func:`expected_max_sharpe`; the probability is reported next to it.

Pure functions, no I/O. Offline research metrics, so float is allowed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import NamedTuple

import numpy as np
from scipy.stats import norm, spearmanr

EULER_GAMMA = 0.5772156649015329
IC_DEGENERATE_PTP = 1e-12


# ---------------------------------------------------------------------------
# Sharpe: probabilistic, expected maximum, deflated
# ---------------------------------------------------------------------------


def expected_max_sharpe(n_trials: float, trial_sharpe_std: float) -> float:
    """``SR0``: the Sharpe the best of ``n_trials`` null trials is expected to reach.

    ``trial_sharpe_std`` is the standard deviation of the trials' Sharpe
    estimates, in the unit the result is wanted in. One trial, or no spread,
    gives 0.
    """
    trials = float(n_trials)
    std = float(trial_sharpe_std)
    if not math.isfinite(trials) or not math.isfinite(std):
        raise ValueError(f"n_trials and trial_sharpe_std must be finite, got {n_trials!r}, {trial_sharpe_std!r}")
    if trials <= 1.0 or std <= 1e-12:
        return 0.0
    return std * (
        (1.0 - EULER_GAMMA) * float(norm.ppf(1.0 - 1.0 / trials))
        + EULER_GAMMA * float(norm.ppf(1.0 - 1.0 / (trials * math.e)))
    )


def null_sharpe_std(observations: int) -> float:
    """Standard deviation of a per-observation Sharpe estimate when the true Sharpe is 0.

    The Lo (2002) estimator variance at ``SR = 0`` is ``1 / (T - 1)``
    whatever the skew and kurtosis; it stands in for the trials' spread when
    the trials themselves were not kept.
    """
    if observations < 2:
        raise ValueError(f"need at least 2 observations, got {observations}")
    return 1.0 / math.sqrt(observations - 1)


def selection_sr0(
    n_trials: int,
    *,
    observations: int,
    trial_sharpes: Sequence[float] | np.ndarray = (),
    annualization: float = 1.0,
) -> float:
    """``SR0`` for picking the best of ``n_trials``, in the trials' Sharpe unit.

    The trials' spread is the sample spread of ``trial_sharpes`` floored at
    the null estimator spread over ``observations`` (scaled by
    ``sqrt(annualization)`` for annualized Sharpes), so trials that were not
    kept, or near-identical trials, still deflate. One trial gives 0.
    """
    sharpes = np.asarray(trial_sharpes, dtype=np.float64).reshape(-1)
    sharpes = sharpes[np.isfinite(sharpes)]
    spread = float(np.std(sharpes, ddof=1)) if sharpes.size > 1 else 0.0
    floor = null_sharpe_std(observations) * math.sqrt(float(annualization))
    return expected_max_sharpe(max(1, int(n_trials)), max(spread, floor))


def probabilistic_sharpe(
    sharpe: float,
    *,
    observations: int,
    benchmark: float = 0.0,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Probability that the true per-observation Sharpe exceeds ``benchmark``.

    ``sharpe`` and ``benchmark`` are per observation (not annualized);
    ``kurtosis`` is the raw (non-excess) kurtosis. Fewer than 2 observations,
    or a non-finite Sharpe, gives 0.
    """
    if observations < 2 or not math.isfinite(sharpe) or not math.isfinite(benchmark):
        return 0.0
    variance_term = 1.0 - float(skewness) * float(sharpe) + ((float(kurtosis) - 1.0) / 4.0) * float(sharpe) ** 2
    standard_error = math.sqrt(max(1e-12, variance_term / (observations - 1)))
    return float(norm.cdf((float(sharpe) - float(benchmark)) / standard_error))


def deflated_sharpe(
    sharpe: float,
    *,
    effective_trials: int,
    observations: int,
    trial_sharpe_std: float,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Deflated Sharpe ratio: :func:`probabilistic_sharpe` against ``SR0``.

    All Sharpe inputs are per observation. Fewer than 2 observations, no
    trials, or a non-finite input gives 0.
    """
    if observations < 2 or effective_trials < 1 or not math.isfinite(sharpe) or not math.isfinite(trial_sharpe_std):
        return 0.0
    sr0 = expected_max_sharpe(max(1.0, float(effective_trials)), trial_sharpe_std)
    return probabilistic_sharpe(sharpe, observations=observations, benchmark=sr0, skewness=skewness, kurtosis=kurtosis)


def sample_skew_kurtosis(values: Sequence[float] | np.ndarray) -> tuple[float, float]:
    """Population skewness and raw kurtosis; ``(0, 3)`` (normal) when undefined."""
    array = np.asarray(values, dtype=np.float64).reshape(-1)
    array = array[np.isfinite(array)]
    if array.size < 3:
        return 0.0, 3.0
    centered = array - float(np.mean(array))
    m2 = float(np.mean(centered**2))
    if m2 <= 1e-24:
        return 0.0, 3.0
    return float(np.mean(centered**3)) / m2**1.5, float(np.mean(centered**4)) / m2**2


# ---------------------------------------------------------------------------
# Multiple testing
# ---------------------------------------------------------------------------


class BHResult(NamedTuple):
    """Benjamini-Hochberg outcome, both arrays in the input order."""

    rejected: np.ndarray
    adjusted: np.ndarray


def benjamini_hochberg(pvalues: Sequence[float], q: float, *, n_tests: int | None = None) -> BHResult:
    """Benjamini-Hochberg step-up FDR control at level ``q``.

    ``n_tests`` is the number of hypotheses tested on the same data, which
    may exceed the p-values in hand (the trials ledger knows tests whose
    p-values were not kept; each counts as ``p = 1``). It may not be fewer
    than the p-values given. Adjusted p-values are the BH q-values, capped
    at 1.
    """
    values = np.asarray(pvalues, dtype=np.float64).reshape(-1)
    m = values.size if n_tests is None else int(n_tests)
    if m < values.size:
        raise ValueError(f"n_tests={m} is fewer than the {values.size} p-values given")
    if values.size == 0:
        return BHResult(np.asarray([], dtype=np.bool_), np.asarray([], dtype=np.float64))
    if not np.all(np.isfinite(values)) or np.any(values < 0.0) or np.any(values > 1.0):
        raise ValueError("p-values must be finite and within [0, 1]")
    order = np.argsort(values, kind="stable")
    sorted_values = values[order]
    ranks = np.arange(1, values.size + 1, dtype=np.float64)
    passing = np.flatnonzero(sorted_values <= float(q) * ranks / float(m))
    rejected = np.zeros(values.size, dtype=np.bool_)
    if passing.size:
        rejected[order[: int(passing[-1]) + 1]] = True
    stepped = np.minimum.accumulate((sorted_values * float(m) / ranks)[::-1])[::-1]
    adjusted = np.empty(values.size, dtype=np.float64)
    adjusted[order] = np.minimum(stepped, 1.0)
    return BHResult(rejected, adjusted)


def effective_test_count(signals: Sequence[Sequence[float]] | np.ndarray) -> int:
    """Li-Ji effective count from the correlation eigenvalues of test signals."""
    matrix = np.asarray(signals, dtype=np.float64)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    if matrix.ndim != 2 or matrix.shape[0] < 1:
        raise ValueError("signals must be a non-empty tests-by-observations matrix")
    usable: list[np.ndarray] = []
    for row in matrix:
        finite = np.isfinite(row)
        if int(np.count_nonzero(finite)) < 2:
            continue
        filled = row.copy()
        filled[~finite] = float(np.mean(filled[finite]))
        deviation = filled - float(np.mean(filled))
        scale = float(np.std(deviation, ddof=1))
        if scale > 1e-12:
            usable.append(deviation / scale)
    if not usable:
        return 1
    standardized = np.vstack(usable)
    singular_values = np.linalg.svd(standardized, compute_uv=False)
    eigenvalues = np.square(singular_values) / max(1, standardized.shape[1] - 1)
    eigenvalues = np.clip(eigenvalues, 0.0, None)
    nearest_integer = np.rint(eigenvalues)
    eigenvalues = np.where(
        np.isclose(eigenvalues, nearest_integer, rtol=1e-10, atol=1e-12),
        nearest_integer,
        eigenvalues,
    )
    contributions = (eigenvalues >= 1.0).astype(np.float64) + (eigenvalues - np.floor(eigenvalues))
    estimate = int(math.ceil(float(np.sum(contributions)) - 1e-12))
    return max(1, min(standardized.shape[0], estimate))


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------


def day_bootstrap(
    daily_pnl: Sequence[float],
    *,
    n_resamples: int,
    rng_seed: int,
) -> np.ndarray:
    """Day-level iid bootstrap: ``(n_resamples, len(daily_pnl))`` resampled days.

    Raises:
        ValueError: if fewer than 2 days are provided.
    """
    n = len(daily_pnl)
    if n < 2:
        raise ValueError(f"insufficient days for bootstrap: n={n}, need >= 2")
    rng = np.random.default_rng(rng_seed)
    arr = np.asarray(daily_pnl, dtype=float)
    idx = rng.integers(low=0, high=n, size=(n_resamples, n))
    return arr[idx]


def stationary_block_bootstrap(
    daily_pnl: Sequence[float],
    *,
    block_size: int,
    n_resamples: int,
    rng_seed: int,
) -> np.ndarray:
    """Politis-Romano stationary block bootstrap, circular, ``(n_resamples, n)``.

    Geometric block lengths with mean ``block_size``.

    Raises:
        ValueError: if block_size <= 0 or input shorter than block_size.
    """
    n = len(daily_pnl)
    if block_size <= 0:
        raise ValueError(f"block_size must be > 0, got {block_size}")
    if n < block_size:
        raise ValueError(f"input length {n} < block_size {block_size}")

    rng = np.random.default_rng(rng_seed)
    arr = np.asarray(daily_pnl, dtype=float)
    p = 1.0 / float(block_size)

    samples = np.empty((n_resamples, n), dtype=float)
    for r in range(n_resamples):
        out = np.empty(n, dtype=float)
        i = 0
        while i < n:
            start = int(rng.integers(low=0, high=n))
            length = int(rng.geometric(p=p))
            for j in range(length):
                if i >= n:
                    break
                out[i] = arr[(start + j) % n]
                i += 1
        samples[r] = out
    return samples


def moving_block_bootstrap_mean(
    values: Sequence[float] | np.ndarray,
    *,
    samples: int = 2_000,
    seed: int,
    block_size: int | None = None,
) -> tuple[float, float]:
    """Moving-block bootstrap of the mean: ``(5% quantile, P(mean <= 0))``.

    Fixed blocks of ``block_size`` (default ``round(sqrt(n))``, at least 2);
    fewer than 2 finite values gives ``(0, 1)``.
    """
    array = np.asarray(values, dtype=np.float64).reshape(-1)
    array = array[np.isfinite(array)]
    if array.size < 2:
        return 0.0, 1.0
    width = block_size or max(2, int(round(math.sqrt(array.size))))
    rng = np.random.default_rng(int(seed))
    means: np.ndarray = np.empty(max(1, int(samples)), dtype=np.float64)
    blocks_needed = math.ceil(array.size / width)
    max_start = max(1, array.size - width + 1)
    for index in range(means.size):
        starts = rng.integers(0, max_start, size=blocks_needed)
        sampled = np.concatenate([array[start : start + width] for start in starts])[: array.size]
        means[index] = float(np.mean(sampled))
    return float(np.quantile(means, 0.05)), float(np.mean(means <= 0.0))


def cluster_bootstrap_mean(
    values: Sequence[float] | np.ndarray,
    clusters: Sequence[str] | np.ndarray,
    *,
    samples: int = 2_000,
    seed: int,
) -> tuple[float, float]:
    """Bootstrap whole clusters (days, sessions): ``(5% quantile, P(mean <= 0))``."""
    array = np.asarray(values, dtype=np.float64).reshape(-1)
    cluster_array = np.asarray(clusters).astype("<U32").reshape(-1)
    if array.size != cluster_array.size:
        raise ValueError("values and clusters must have identical lengths")
    valid = np.isfinite(array)
    array = array[valid]
    cluster_array = cluster_array[valid]
    ordered = tuple(dict.fromkeys(str(value) for value in cluster_array))
    if array.size < 2 or len(ordered) < 2:
        return 0.0, 1.0
    grouped = [array[cluster_array == cluster] for cluster in ordered]
    rng = np.random.default_rng(int(seed))
    means: np.ndarray = np.empty(max(1, int(samples)), dtype=np.float64)
    for index in range(means.size):
        selected = rng.integers(0, len(grouped), size=len(grouped))
        sample = np.concatenate([grouped[item] for item in selected])
        means[index] = float(np.mean(sample))
    return float(np.quantile(means, 0.05)), float(np.mean(means <= 0.0))


# ---------------------------------------------------------------------------
# Information coefficient
# ---------------------------------------------------------------------------


def _finite_pairs(
    signal: Sequence[float] | np.ndarray, target: Sequence[float] | np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    lhs_all = np.asarray(signal, dtype=np.float64).reshape(-1)
    rhs_all = np.asarray(target, dtype=np.float64).reshape(-1)
    count = min(lhs_all.size, rhs_all.size)
    lhs, rhs = lhs_all[:count], rhs_all[:count]
    valid = np.isfinite(lhs) & np.isfinite(rhs)
    return lhs[valid], rhs[valid]


def rank_ic(signal: Sequence[float] | np.ndarray, target: Sequence[float] | np.ndarray, *, min_obs: int) -> float:
    """Spearman IC over the finite pairs; NaN when undefined.

    Undefined means fewer than ``min_obs`` finite pairs, or either side
    constant (range at most ``IC_DEGENERATE_PTP``). Callers that report an
    undefined IC as 0 do so themselves, by name.
    """
    lhs, rhs = _finite_pairs(signal, target)
    if lhs.size < max(2, int(min_obs)):
        return math.nan
    if float(np.ptp(lhs)) <= IC_DEGENERATE_PTP or float(np.ptp(rhs)) <= IC_DEGENERATE_PTP:
        return math.nan
    value = float(spearmanr(lhs, rhs).statistic)
    return value if math.isfinite(value) else math.nan


def pearson_ic(signal: Sequence[float] | np.ndarray, target: Sequence[float] | np.ndarray, *, min_obs: int) -> float:
    """Pearson IC over the finite pairs; NaN when undefined (as :func:`rank_ic`)."""
    lhs, rhs = _finite_pairs(signal, target)
    if lhs.size < max(2, int(min_obs)):
        return math.nan
    if float(np.ptp(lhs)) <= IC_DEGENERATE_PTP or float(np.ptp(rhs)) <= IC_DEGENERATE_PTP:
        return math.nan
    value = float(np.corrcoef(lhs, rhs)[0, 1])
    return value if math.isfinite(value) else math.nan


def min_detectable_ic(observations: int, *, alpha: float = 0.05, power: float = 0.80) -> float:
    """MDIC: the smallest |IC| a one-sided test at ``alpha`` detects with ``power``.

    Fisher-z approximation, ``tanh((z_{1-alpha} + z_{power}) / sqrt(n - 3))``,
    for ``observations`` independent pairs; overlapping or autocorrelated
    pairs need their effective count passed instead. Fewer than 4 pairs
    detects nothing, so the result is 1.
    """
    if not 0.0 < alpha < 1.0 or not 0.0 < power < 1.0:
        raise ValueError(f"alpha and power must be in (0, 1), got {alpha}, {power}")
    if observations < 4:
        return 1.0
    z = float(norm.ppf(1.0 - alpha)) + float(norm.ppf(power))
    return float(math.tanh(z / math.sqrt(observations - 3)))
