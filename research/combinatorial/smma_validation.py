"""Statistical and executable-price validation for SMMA hypotheses."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Mapping, Sequence

import numpy as np
from scipy.stats import norm, spearmanr

from research.backtest.cost_models import load_cost_profile

SPLIT_ORDER: tuple[str, ...] = ("discovery", "selection", "locked_validation", "final_holdout")
SPLIT_RATIOS: tuple[float, ...] = (0.50, 0.25, 0.15, 0.10)
MIN_DAYS_FOR_PROMOTION = 100


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


@dataclass(frozen=True, slots=True)
class LockedMetrics:
    bootstrap_lower_95: float
    bootstrap_pvalue: float
    permutation_pvalue: float
    deflated_sharpe: float
    walk_forward_positive_fraction: float
    walk_forward_sharpes: tuple[float, ...]
    passed: bool


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
    instrument_profile: str,
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
            }
        )
        != 1
    ):
        raise ValueError("execution inputs must have identical lengths")
    cost = load_cost_profile(instrument_profile).rt_cost_pts
    pnl: list[float] = []
    entries: list[int] = []
    exits: list[int] = []
    next_free = 0
    for decision in range(signal_arr.size - 1):
        if decision < next_free or not np.isfinite(signal_arr[decision]):
            continue
        directed_signal = signal_arr[decision] * float(direction)
        if directed_signal <= float(threshold):
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
    target_returns: Sequence[float] | np.ndarray,
    execution: ExecutionResult,
    root: str,
    nonoverlap_step: int,
    recent_fraction: float = 0.25,
) -> KillMetrics:
    """Apply the preregistered recent-quarter IC and net-edge kills."""
    signal_arr = np.asarray(signal, dtype=np.float64).reshape(-1)
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
    minimum_edge = 1.0 if root == "TXF" else 5.0
    reasons: list[str] = []
    if detrended_ic <= 0.01:
        reasons.append("detrended_ic")
    if abs(raw_ic) > 1.6 * abs(detrended_ic):
        reasons.append("raw_ic_inflation")
    if overlap_ratio > 3.0:
        reasons.append("overlap_inflation")
    if execution.net_edge <= minimum_edge:
        reasons.append("net_edge")
    return KillMetrics(
        raw_ic=raw_ic,
        detrended_ic=detrended_ic,
        nonoverlap_ic=nonoverlap_ic,
        overlap_ratio=float(overlap_ratio),
        net_edge=execution.net_edge,
        net_sharpe=execution.net_sharpe,
        turnover=execution.turnover,
        strict_edge_gap=float(10.0 - execution.net_edge),
        passed=not reasons,
        reasons=tuple(reasons),
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


def deflated_sharpe(sharpe: float, *, trials: int, observations: int) -> float:
    """Conservative normal approximation to the deflated-Sharpe probability."""
    if observations < 2 or trials < 1 or not np.isfinite(sharpe):
        return 0.0
    expected_max = float(norm.ppf(1.0 - (1.0 / max(2.0, float(trials)))))
    standard_error = math.sqrt(max(1e-12, (1.0 + (0.5 * sharpe * sharpe)) / (observations - 1)))
    return float(norm.cdf((sharpe - expected_max) / standard_error))


def purged_walk_forward_sharpes(
    trade_pnl: Sequence[float] | np.ndarray,
    *,
    folds: int = 5,
    purge: int = 1,
) -> tuple[float, ...]:
    """Five contiguous OOS folds with a purge at each boundary."""
    values = np.asarray(trade_pnl, dtype=np.float64).reshape(-1)
    if values.size < folds:
        return tuple(0.0 for _ in range(folds))
    fold_indices = np.array_split(np.arange(values.size), folds)
    sharpes: list[float] = []
    for indices in fold_indices:
        trimmed = indices[int(purge) :] if indices.size > purge else np.asarray([], dtype=np.int64)
        view = values[trimmed]
        if view.size < 2 or float(np.std(view, ddof=1)) <= 1e-12:
            sharpes.append(0.0)
        else:
            sharpes.append(float(np.mean(view) / np.std(view, ddof=1)))
    return tuple(sharpes)


def locked_validation(
    *,
    signal: Sequence[float] | np.ndarray,
    target_returns: Sequence[float] | np.ndarray,
    execution: ExecutionResult,
    actual_trials: int,
    seed: int,
) -> LockedMetrics:
    lower, bootstrap_p = block_bootstrap_mean(execution.trade_pnl, samples=2_000, seed=seed)
    perm_p = permutation_pvalue(signal, target_returns, samples=2_000, seed=seed + 1)
    fold_sharpes = purged_walk_forward_sharpes(execution.trade_pnl, folds=5, purge=1)
    positive_fraction = float(np.mean(np.asarray(fold_sharpes) > 0.0))
    dsr = deflated_sharpe(
        execution.net_sharpe,
        trials=max(1, int(actual_trials)),
        observations=int(execution.trade_pnl.size),
    )
    passed = bool(lower > 0.0 and bootstrap_p <= 0.10 and perm_p <= 0.10 and dsr >= 0.50 and positive_fraction >= 0.60)
    return LockedMetrics(
        bootstrap_lower_95=lower,
        bootstrap_pvalue=bootstrap_p,
        permutation_pvalue=perm_p,
        deflated_sharpe=dsr,
        walk_forward_positive_fraction=positive_fraction,
        walk_forward_sharpes=fold_sharpes,
        passed=passed,
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
