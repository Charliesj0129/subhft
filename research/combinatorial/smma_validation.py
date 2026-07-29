"""Statistical and executable-price validation for SMMA hypotheses."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Mapping, Sequence

import numpy as np
from scipy.stats import norm, spearmanr

from research.backtest.cost_models import load_cost_profile

SPLIT_ORDER: tuple[str, ...] = ("discovery", "selection", "locked_validation", "final_holdout")
SPLIT_RATIOS: tuple[float, ...] = (0.50, 0.25, 0.15, 0.10)
MIN_DAYS_FOR_PROMOTION = 100
THRESHOLD_QUANTILES: tuple[float, ...] = (0.50, 0.70, 0.85, 0.95)
MINIMUM_EDGE_POINTS: Mapping[str, float] = {"TXF": 1.0, "TMF": 5.0}
STRICT_EDGE_TARGET_POINTS: Mapping[str, float] = {"TXF": 10.0, "TMF": 10.0}
ENTRY_RULE_VERSION = "quantile_gte_v2"


@dataclass(frozen=True, slots=True)
class SplitPlan:
    labels: np.ndarray
    days: tuple[str, ...]
    assignments: Mapping[str, str]
    split_hash: str

    def mask(self, name: str) -> np.ndarray:
        if name not in SPLIT_ORDER:
            raise ValueError(f"unknown split: {name}")
        return self.labels == name


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    trade_pnl: np.ndarray
    entry_indices: np.ndarray
    exit_indices: np.ndarray
    net_edge: float
    net_sharpe: float
    turnover: float


@dataclass(frozen=True, slots=True)
class ThresholdResolution:
    """Discovery-only evidence for one frozen quantile entry cut."""

    cut: float
    quantile: float
    comparator: str
    finite_count: int
    distinct_count: int
    below_count: int
    tie_count: int
    active_count: int
    active_rate: float


@dataclass(frozen=True, slots=True)
class KillMetrics:
    raw_ic: float
    detrended_ic: float
    nonoverlap_ic: float
    overlap_ratio: float
    net_edge: float
    net_sharpe: float
    turnover: float
    strict_edge_gap: float
    passed: bool
    reasons: tuple[str, ...]
    clustered_sharpe: float = 0.0


@dataclass(frozen=True, slots=True)
class LockedMetrics:
    bootstrap_lower_95: float
    bootstrap_pvalue: float
    permutation_pvalue: float
    deflated_sharpe: float
    walk_forward_positive_fraction: float
    walk_forward_sharpes: tuple[float, ...]
    passed: bool
    walk_forward_active_folds: int = 0
    deflated_sharpe_trials_effective: int = 1
    deflated_sharpe_trials_raw: int = 1
    gate_results: dict[str, bool] = field(default_factory=dict)
    effective_gate_count: int = 0
    failure_reasons: tuple[str, ...] = ()


def build_split_plan(trading_days: Sequence[str] | np.ndarray) -> SplitPlan:
    """Split whole trading days contiguously in 50/25/15/10 proportions."""
    day_array = np.asarray(trading_days).astype("<U10").reshape(-1)
    ordered_days = tuple(dict.fromkeys(str(value) for value in day_array))
    if len(ordered_days) < 4:
        raise ValueError("at least four trading days are required for four frozen splits")
    counts: list[int] = []
    allocated = 0
    for index, ratio in enumerate(SPLIT_RATIOS):
        if index == len(SPLIT_RATIOS) - 1:
            count = len(ordered_days) - allocated
        else:
            count = max(1, int(round(len(ordered_days) * ratio)))
            remaining_splits = len(SPLIT_RATIOS) - index - 1
            count = min(count, len(ordered_days) - allocated - remaining_splits)
        counts.append(count)
        allocated += count
    assignments: dict[str, str] = {}
    cursor = 0
    for name, count in zip(SPLIT_ORDER, counts, strict=True):
        for day in ordered_days[cursor : cursor + count]:
            assignments[day] = name
        cursor += count
    labels = np.asarray([assignments[str(value)] for value in day_array], dtype="<U18")
    canonical = json.dumps(
        {"days": ordered_days, "assignments": assignments},
        sort_keys=True,
        separators=(",", ":"),
    )
    return SplitPlan(
        labels=labels,
        days=ordered_days,
        assignments=assignments,
        split_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def forward_target_indices(
    *,
    horizon: str,
    timeframe_min: int,
    split_labels: Sequence[str] | np.ndarray,
    reset_mask: Sequence[bool] | np.ndarray,
    session_close: Sequence[bool] | np.ndarray,
) -> np.ndarray:
    """Build causal target indices; labels never cross a split or reset."""
    labels = np.asarray(split_labels).astype("<U18").reshape(-1)
    resets = np.asarray(reset_mask, dtype=np.bool_).reshape(-1)
    closes = np.asarray(session_close, dtype=np.bool_).reshape(-1)
    if len({labels.size, resets.size, closes.size}) != 1:
        raise ValueError("target-index inputs must have identical lengths")
    out = np.full(labels.size, -1, dtype=np.int64)
    if horizon == "1h":
        step = max(1, math.ceil(60 / int(timeframe_min)))
    elif horizon == "4h":
        step = max(1, math.ceil(240 / int(timeframe_min)))
    elif horizon == "session":
        step = 0
    else:
        raise ValueError(f"unknown horizon: {horizon}")
    for index in range(labels.size):
        if horizon == "session":
            candidates = np.flatnonzero(closes[index + 1 :])
            if candidates.size == 0:
                continue
            target = index + 1 + int(candidates[0])
        else:
            target = index + step
        if target >= labels.size or labels[target] != labels[index]:
            continue
        if np.any(resets[index + 1 : target + 1]):
            continue
        out[index] = target
    return out


def forward_returns(close: Sequence[float] | np.ndarray, target_indices: np.ndarray) -> np.ndarray:
    close_arr = np.asarray(close, dtype=np.float64).reshape(-1)
    targets = np.asarray(target_indices, dtype=np.int64).reshape(-1)
    if close_arr.size != targets.size:
        raise ValueError("close and target_indices lengths differ")
    out = np.full(close_arr.size, np.nan, dtype=np.float64)
    valid = (targets >= 0) & (targets < close_arr.size) & np.isfinite(close_arr) & (np.abs(close_arr) > 1e-12)
    indices = np.flatnonzero(valid)
    target_values = close_arr[targets[indices]]
    finite = np.isfinite(target_values)
    selected = indices[finite]
    out[selected] = (target_values[finite] / close_arr[selected]) - 1.0
    return out


def rolling_detrend(values: Sequence[float] | np.ndarray, window: int = 5) -> np.ndarray:
    """Subtract a trailing-only rolling mean; no future sample is used."""
    array = np.asarray(values, dtype=np.float64).reshape(-1)
    out = np.full(array.size, np.nan, dtype=np.float64)
    width = max(1, int(window))
    for index in range(array.size):
        if index + 1 < width:
            continue
        start = max(0, index - width + 1)
        view = array[start : index + 1]
        finite = view[np.isfinite(view)]
        if np.isfinite(array[index]) and finite.size == width:
            out[index] = array[index] - float(np.mean(finite))
    return out


def spearman_ic(signal: Sequence[float] | np.ndarray, target: Sequence[float] | np.ndarray) -> float:
    signal_arr = np.asarray(signal, dtype=np.float64).reshape(-1)
    target_arr = np.asarray(target, dtype=np.float64).reshape(-1)
    count = min(signal_arr.size, target_arr.size)
    valid = np.isfinite(signal_arr[:count]) & np.isfinite(target_arr[:count])
    if int(np.count_nonzero(valid)) < 5:
        return 0.0
    lhs, rhs = signal_arr[:count][valid], target_arr[:count][valid]
    if np.ptp(lhs) <= 1e-12 or np.ptp(rhs) <= 1e-12:
        return 0.0
    result = spearmanr(lhs, rhs)
    value = float(result.statistic)
    return value if np.isfinite(value) else 0.0


def resolve_quantile_threshold(
    signal: Sequence[float] | np.ndarray,
    *,
    direction: int,
    quantile: float,
) -> ThresholdResolution:
    """Resolve an absolute entry cut from finite discovery-only directed signals."""
    if direction not in (-1, 1):
        raise ValueError("direction must be -1 or 1")
    if not (0.0 < float(quantile) < 1.0):
        raise ValueError("quantile must be in (0, 1)")
    directed = np.asarray(signal, dtype=np.float64).reshape(-1) * float(direction)
    finite = directed[np.isfinite(directed)]
    if finite.size == 0:
        raise ValueError("cannot resolve a quantile threshold from an empty signal")
    cut = float(np.quantile(finite, float(quantile), method="higher"))
    active = finite >= cut
    tie_count = int(np.count_nonzero(finite == cut))
    active_count = int(np.count_nonzero(active))
    return ThresholdResolution(
        cut=cut,
        quantile=float(quantile),
        comparator=">=",
        finite_count=int(finite.size),
        distinct_count=int(np.unique(finite).size),
        below_count=int(np.count_nonzero(finite < cut)),
        tie_count=tie_count,
        active_count=active_count,
        active_rate=float(active_count / finite.size),
    )


def activation_mask(
    signal: Sequence[float] | np.ndarray,
    *,
    direction: int,
    threshold: float,
) -> np.ndarray:
    """Apply the single governed entry comparator used by every mining path."""

    if direction not in (-1, 1):
        raise ValueError("direction must be -1 or 1")
    directed = np.asarray(signal, dtype=np.float64).reshape(-1) * float(direction)
    return np.isfinite(directed) & (directed >= float(threshold))


def simulate_next_bar_execution(
    *,
    signal: Sequence[float] | np.ndarray,
    direction: int,
    threshold: float,
    target_indices: np.ndarray,
    bid_open: Sequence[float] | np.ndarray,
    ask_open: Sequence[float] | np.ndarray,
    bid_close: Sequence[float] | np.ndarray,
    ask_close: Sequence[float] | np.ndarray,
    reset_mask: Sequence[bool] | np.ndarray,
    instrument_profile: str | None = None,
    contracts: Sequence[str] | np.ndarray | None = None,
    cost_mode: str = "root_proxy",
) -> ExecutionResult:
    """One-position taker simulation using next-open entry and target-close BBO."""
    if direction not in (-1, 1):
        raise ValueError("direction must be -1 or 1")
    signal_arr = np.asarray(signal, dtype=np.float64).reshape(-1)
    targets = np.asarray(target_indices, dtype=np.int64).reshape(-1)
    entry_bids = np.asarray(bid_open, dtype=np.float64).reshape(-1)
    entry_asks = np.asarray(ask_open, dtype=np.float64).reshape(-1)
    exit_bids = np.asarray(bid_close, dtype=np.float64).reshape(-1)
    exit_asks = np.asarray(ask_close, dtype=np.float64).reshape(-1)
    resets = np.asarray(reset_mask, dtype=np.bool_).reshape(-1)
    contract_values = None if contracts is None else np.asarray(contracts).astype("<U16").reshape(-1)
    if cost_mode not in {"per_contract", "root_proxy"}:
        raise ValueError("cost_mode must be 'per_contract' or 'root_proxy'")
    if cost_mode == "per_contract" and contract_values is None:
        raise ValueError("per_contract cost mode requires a contract value for every bar")
    if cost_mode == "root_proxy" and not instrument_profile:
        raise ValueError("root_proxy cost mode requires instrument_profile")
    if (
        len(
            {
                signal_arr.size,
                targets.size,
                entry_bids.size,
                entry_asks.size,
                exit_bids.size,
                exit_asks.size,
                resets.size,
                *(() if contract_values is None else (contract_values.size,)),
            }
        )
        != 1
    ):
        raise ValueError("execution inputs must have identical lengths")
    active = activation_mask(signal_arr, direction=direction, threshold=threshold)
    proxy_cost = load_cost_profile(str(instrument_profile)).rt_cost_pts if cost_mode == "root_proxy" else None
    contract_costs = (
        {
            contract: load_cost_profile(contract).rt_cost_pts
            for contract in sorted(str(value) for value in np.unique(contract_values))
        }
        if contract_values is not None and cost_mode == "per_contract"
        else {}
    )
    pnl: list[float] = []
    entries: list[int] = []
    exits: list[int] = []
    next_free = 0
    for decision in range(signal_arr.size - 1):
        if decision < next_free:
            continue
        if not active[decision]:
            continue
        entry = decision + 1
        intended_exit = int(targets[decision])
        if intended_exit < entry or intended_exit >= signal_arr.size:
            continue
        reset_points = np.flatnonzero(resets[entry : intended_exit + 1])
        exit_index = entry + int(reset_points[0]) - 1 if reset_points.size else intended_exit
        if exit_index < entry:
            continue
        if not (
            np.isfinite(entry_bids[entry])
            and np.isfinite(entry_asks[entry])
            and np.isfinite(exit_bids[exit_index])
            and np.isfinite(exit_asks[exit_index])
        ):
            continue
        if direction > 0:
            gross = exit_bids[exit_index] - entry_asks[entry]
        else:
            gross = entry_bids[entry] - exit_asks[exit_index]
        if proxy_cost is not None:
            cost = float(proxy_cost)
        else:
            if contract_values is None:
                raise RuntimeError("per-contract execution lost its validated contract array")
            cost = contract_costs[str(contract_values[entry])]
        pnl.append(float(gross - cost))
        entries.append(entry)
        exits.append(exit_index)
        next_free = exit_index + 1
    trade_pnl = np.asarray(pnl, dtype=np.float64)
    edge = float(np.mean(trade_pnl)) if trade_pnl.size else 0.0
    standard_deviation = float(np.std(trade_pnl, ddof=1)) if trade_pnl.size > 1 else 0.0
    sharpe = edge / standard_deviation if standard_deviation > 1e-12 else 0.0
    return ExecutionResult(
        trade_pnl=trade_pnl,
        entry_indices=np.asarray(entries, dtype=np.int64),
        exit_indices=np.asarray(exits, dtype=np.int64),
        net_edge=edge,
        net_sharpe=float(sharpe),
        turnover=float((2 * trade_pnl.size) / max(1, signal_arr.size)),
    )


def evaluate_recent_kill_criteria(
    *,
    signal: Sequence[float] | np.ndarray,
    direction: int,
    target_returns: Sequence[float] | np.ndarray,
    execution: ExecutionResult,
    root: str,
    nonoverlap_step: int,
    trading_days: Sequence[str] | np.ndarray | None = None,
    recent_fraction: float = 0.25,
    trade_activity_reason: str | None = None,
) -> KillMetrics:
    """Apply the preregistered recent-quarter IC and net-edge kills."""
    if direction not in (-1, 1):
        raise ValueError("direction must be -1 or 1")
    signal_arr = np.asarray(signal, dtype=np.float64).reshape(-1) * float(direction)
    target = np.asarray(target_returns, dtype=np.float64).reshape(-1)
    count = min(signal_arr.size, target.size)
    if not (0.0 < float(recent_fraction) <= 1.0):
        raise ValueError("recent_fraction must be in (0, 1]")
    start = int(count * (1.0 - float(recent_fraction)))
    recent_signal = signal_arr[start:count]
    recent_target = target[start:count]
    raw_ic = spearman_ic(recent_signal, recent_target)
    detrended_ic = spearman_ic(recent_signal, rolling_detrend(recent_target))
    stride = max(1, int(nonoverlap_step))
    nonoverlap_ic = spearman_ic(recent_signal[::stride], recent_target[::stride])
    overlap_ratio = abs(raw_ic) / max(abs(nonoverlap_ic), 1e-12)
    if root not in MINIMUM_EDGE_POINTS:
        raise ValueError(f"unsupported root for edge thresholds: {root!r}")
    minimum_edge = MINIMUM_EDGE_POINTS[root]
    reasons: list[str] = []
    if detrended_ic <= 0.01:
        reasons.append("detrended_ic")
    if abs(raw_ic) > 1.6 * abs(detrended_ic):
        reasons.append("raw_ic_inflation")
    if overlap_ratio > 3.0:
        reasons.append("overlap_inflation")
    if execution.trade_pnl.size == 0:
        reasons.append(trade_activity_reason or "no_executable_trades")
    elif execution.net_edge <= minimum_edge:
        reasons.append("net_edge")
    daily_sharpe = (
        clustered_execution_sharpe(execution, trading_days) if trading_days is not None else execution.net_sharpe
    )
    return KillMetrics(
        raw_ic=raw_ic,
        detrended_ic=detrended_ic,
        nonoverlap_ic=nonoverlap_ic,
        overlap_ratio=float(overlap_ratio),
        net_edge=execution.net_edge,
        net_sharpe=execution.net_sharpe,
        turnover=execution.turnover,
        strict_edge_gap=float(STRICT_EDGE_TARGET_POINTS[root] - execution.net_edge),
        passed=not reasons,
        reasons=tuple(reasons),
        clustered_sharpe=daily_sharpe,
    )


def monotonic_horizon_pollution(metrics: Mapping[str, KillMetrics]) -> bool:
    """Kill when |IC| grows monotonically from 1h to 4h to session."""
    if not all(name in metrics for name in ("1h", "4h", "session")):
        return False
    values = [abs(metrics[name].detrended_ic) for name in ("1h", "4h", "session")]
    return bool(values[0] < values[1] < values[2])


def block_bootstrap_mean(
    values: Sequence[float] | np.ndarray,
    *,
    samples: int = 2_000,
    seed: int,
    block_size: int | None = None,
) -> tuple[float, float]:
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
    """Bootstrap whole trading-day/session clusters, retaining intracluster dependence."""
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


def clustered_execution_sharpe(
    execution: ExecutionResult,
    trading_days: Sequence[str] | np.ndarray,
) -> float:
    """Sharpe of daily clustered PnL, on the same scale used by locked DSR."""
    days = np.asarray(trading_days).astype("<U10").reshape(-1)
    if (
        len(
            {
                execution.trade_pnl.size,
                execution.entry_indices.size,
                execution.exit_indices.size,
            }
        )
        != 1
    ):
        raise ValueError("trade pnl and entry/exit indices must have identical lengths")
    if np.any(execution.entry_indices < 0) or np.any(execution.entry_indices >= days.size):
        raise ValueError("execution entry index is outside the trading-day array")
    trade_days = days[execution.entry_indices]
    ordered_days = tuple(dict.fromkeys(str(value) for value in trade_days))
    daily_pnl = np.asarray(
        [np.sum(execution.trade_pnl[trade_days == day]) for day in ordered_days],
        dtype=np.float64,
    )
    if daily_pnl.size < 2:
        return 0.0
    standard_deviation = float(np.std(daily_pnl, ddof=1))
    mean = float(np.mean(daily_pnl))
    if standard_deviation > 1e-12:
        return mean / standard_deviation
    return 0.0


def permutation_pvalue(
    signal: Sequence[float] | np.ndarray,
    target: Sequence[float] | np.ndarray,
    *,
    samples: int = 2_000,
    seed: int,
) -> float:
    signal_arr = np.asarray(signal, dtype=np.float64).reshape(-1)
    target_arr = np.asarray(target, dtype=np.float64).reshape(-1)
    count = min(signal_arr.size, target_arr.size)
    valid = np.isfinite(signal_arr[:count]) & np.isfinite(target_arr[:count])
    lhs, rhs = signal_arr[:count][valid], target_arr[:count][valid]
    if lhs.size < 5:
        return 1.0
    observed = abs(spearman_ic(lhs, rhs))
    rng = np.random.default_rng(int(seed))
    exceed = 0
    for _ in range(max(1, int(samples))):
        exceed += abs(spearman_ic(lhs, rng.permutation(rhs))) >= observed
    return float((exceed + 1) / (max(1, int(samples)) + 1))


def benjamini_hochberg(pvalues: Sequence[float], q: float = 0.10) -> np.ndarray:
    """Return a boolean BH rejection mask in original order."""
    values = np.asarray(pvalues, dtype=np.float64).reshape(-1)
    if values.size == 0:
        return np.asarray([], dtype=np.bool_)
    order = np.argsort(values, kind="stable")
    sorted_values = values[order]
    thresholds = float(q) * np.arange(1, values.size + 1) / float(values.size)
    passing = np.flatnonzero(sorted_values <= thresholds)
    mask = np.zeros(values.size, dtype=np.bool_)
    if passing.size:
        mask[order[: int(passing[-1]) + 1]] = True
    return mask


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


def deflated_sharpe(
    sharpe: float,
    *,
    effective_trials: int,
    observations: int,
    trial_sharpe_std: float,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Bailey/López de Prado DSR probability using an effective test count."""
    if observations < 2 or effective_trials < 1 or not np.isfinite(sharpe) or not np.isfinite(trial_sharpe_std):
        return 0.0
    trials = max(1.0, float(effective_trials))
    if trials <= 1.0 or trial_sharpe_std <= 1e-12:
        expected_max = 0.0
    else:
        euler_gamma = 0.5772156649015329
        expected_max = float(trial_sharpe_std) * (
            (1.0 - euler_gamma) * norm.ppf(1.0 - (1.0 / trials))
            + euler_gamma * norm.ppf(1.0 - (1.0 / (trials * math.e)))
        )
    variance_term = 1.0 - float(skewness) * float(sharpe) + ((float(kurtosis) - 1.0) / 4.0) * float(sharpe) ** 2
    standard_error = math.sqrt(max(1e-12, variance_term / (observations - 1)))
    return float(norm.cdf((sharpe - expected_max) / standard_error))


def purged_walk_forward_sharpes(
    trade_pnl: Sequence[float] | np.ndarray,
    *,
    entry_indices: Sequence[int] | np.ndarray | None = None,
    exit_indices: Sequence[int] | np.ndarray | None = None,
    trading_days: Sequence[str] | np.ndarray | None = None,
    validation_days: Sequence[str] | None = None,
    folds: int = 5,
) -> tuple[float, ...]:
    """Calendar folds that purge trades whose labels cross a fold boundary."""
    values = np.asarray(trade_pnl, dtype=np.float64).reshape(-1)
    if entry_indices is None or exit_indices is None or trading_days is None:
        fold_indices = np.array_split(np.arange(values.size), folds)
        return tuple(
            (
                float(np.mean(values[indices]) / np.std(values[indices], ddof=1))
                if indices.size >= 2 and float(np.std(values[indices], ddof=1)) > 1e-12
                else math.nan
            )
            for indices in fold_indices
        )
    entries = np.asarray(entry_indices, dtype=np.int64).reshape(-1)
    exits = np.asarray(exit_indices, dtype=np.int64).reshape(-1)
    days = np.asarray(trading_days).astype("<U10").reshape(-1)
    if len({values.size, entries.size, exits.size}) != 1:
        raise ValueError("trade pnl and entry/exit indices must have identical lengths")
    if np.any(entries < 0) or np.any(exits < entries) or np.any(exits >= days.size):
        raise ValueError("trade entry/exit indices are outside the trading-day array")
    ordered_days = (
        tuple(validation_days) if validation_days is not None else tuple(dict.fromkeys(str(day) for day in days))
    )
    fold_days = np.array_split(np.asarray(ordered_days, dtype="<U10"), folds)
    sharpes: list[float] = []
    for current_days in fold_days:
        entry_days = days[entries]
        exit_days = days[exits]
        in_fold = np.isin(entry_days, current_days) & np.isin(exit_days, current_days)
        view = values[in_fold]
        if view.size < 2 or float(np.std(view, ddof=1)) <= 1e-12:
            sharpes.append(math.nan)
        else:
            sharpes.append(float(np.mean(view) / np.std(view, ddof=1)))
    return tuple(sharpes)


def locked_validation(
    *,
    signal: Sequence[float] | np.ndarray,
    target_returns: Sequence[float] | np.ndarray,
    execution: ExecutionResult,
    actual_trials: int,
    effective_trials: int | None = None,
    trial_sharpe_std: float = 1.0,
    trading_days: Sequence[str] | np.ndarray | None = None,
    validation_days: Sequence[str] | None = None,
    seed: int,
) -> LockedMetrics:
    if trading_days is None:
        trade_clusters: np.ndarray = np.arange(execution.trade_pnl.size).astype("<U32")
        lower, bootstrap_p = block_bootstrap_mean(execution.trade_pnl, samples=2_000, seed=seed)
        dsr_values = execution.trade_pnl
    else:
        day_array = np.asarray(trading_days).astype("<U10").reshape(-1)
        if (
            len(
                {
                    execution.trade_pnl.size,
                    execution.entry_indices.size,
                    execution.exit_indices.size,
                }
            )
            != 1
        ):
            raise ValueError("trade pnl and entry/exit indices must have identical lengths")
        if np.any(execution.entry_indices < 0) or np.any(execution.entry_indices >= day_array.size):
            raise ValueError("execution entry index is outside the trading-day array")
        trade_clusters = day_array[execution.entry_indices]
        lower, bootstrap_p = cluster_bootstrap_mean(
            execution.trade_pnl,
            trade_clusters,
            samples=2_000,
            seed=seed,
        )
        ordered_clusters = tuple(dict.fromkeys(str(value) for value in trade_clusters))
        dsr_values = np.asarray(
            [np.sum(execution.trade_pnl[trade_clusters == cluster]) for cluster in ordered_clusters],
            dtype=np.float64,
        )
    perm_p = permutation_pvalue(signal, target_returns, samples=2_000, seed=seed + 1)
    fold_sharpes = purged_walk_forward_sharpes(
        execution.trade_pnl,
        entry_indices=execution.entry_indices if trading_days is not None else None,
        exit_indices=execution.exit_indices if trading_days is not None else None,
        trading_days=trading_days,
        validation_days=validation_days,
        folds=5,
    )
    finite_folds = np.asarray(fold_sharpes, dtype=np.float64)
    finite_folds = finite_folds[np.isfinite(finite_folds)]
    active_folds = int(finite_folds.size)
    positive_fraction = float(np.mean(finite_folds > 0.0)) if active_folds else 0.0
    dsr_standard_deviation = float(np.std(dsr_values, ddof=1)) if dsr_values.size > 1 else 0.0
    if dsr_values.size > 1 and dsr_standard_deviation > 1e-12:
        dsr_sharpe = float(np.mean(dsr_values) / dsr_standard_deviation)
        centered = dsr_values - float(np.mean(dsr_values))
        standard_deviation = float(np.std(dsr_values, ddof=0))
        skewness = float(np.mean(centered**3) / standard_deviation**3)
        kurtosis = float(np.mean(centered**4) / standard_deviation**4)
    else:
        dsr_sharpe = 0.0
        skewness = 0.0
        kurtosis = 3.0
    effective = max(1, min(int(actual_trials), int(effective_trials or actual_trials)))
    dsr = deflated_sharpe(
        dsr_sharpe,
        effective_trials=effective,
        observations=int(dsr_values.size),
        trial_sharpe_std=float(trial_sharpe_std),
        skewness=skewness,
        kurtosis=kurtosis,
    )
    gate_results = {
        "cluster_bootstrap": bool(lower > 0.0 and bootstrap_p <= 0.10),
        "permutation": bool(perm_p <= 0.10),
        "deflated_sharpe": bool(dsr >= 0.50),
        "walk_forward": bool(active_folds >= 3 and positive_fraction >= 0.60),
    }
    failure_reasons = tuple(
        ("insufficient_trade_activity" if name == "walk_forward" and active_folds < 3 else name)
        for name, passed in gate_results.items()
        if not passed
    )
    passed = all(gate_results.values())
    return LockedMetrics(
        bootstrap_lower_95=lower,
        bootstrap_pvalue=bootstrap_p,
        permutation_pvalue=perm_p,
        deflated_sharpe=dsr,
        walk_forward_positive_fraction=positive_fraction,
        walk_forward_sharpes=fold_sharpes,
        passed=passed,
        walk_forward_active_folds=active_folds,
        deflated_sharpe_trials_effective=effective,
        deflated_sharpe_trials_raw=max(1, int(actual_trials)),
        gate_results=gate_results,
        effective_gate_count=len(gate_results),
        failure_reasons=failure_reasons,
    )


def candidate_rank_key(
    *,
    locked: LockedMetrics,
    kill: KillMetrics,
    complexity: int,
    candidate_id: str,
) -> tuple[float, float, float, float, int, str]:
    """No weighted score: lexicographic preregistered ranking only."""
    return (
        -locked.bootstrap_lower_95,
        -kill.detrended_ic,
        -kill.net_sharpe,
        kill.turnover,
        int(complexity),
        candidate_id,
    )


def metrics_to_dict(metrics: KillMetrics | LockedMetrics | ExecutionResult) -> dict[str, object]:
    payload = asdict(metrics)
    for key, value in list(payload.items()):
        if isinstance(value, np.ndarray):
            payload[key] = value.tolist()
    return payload
