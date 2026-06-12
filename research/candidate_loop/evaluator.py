"""Per-split candidate evaluator (``evaluator_version = eval_v1``, spec §10).

Answers: does this factor, point-in-time, show stable predictive structure for
short-horizon ``future_mid_return``, surviving a crude cost proxy and a 1ms
availability delay? It does NOT answer tradability/execution/sizing.

Designed for the runner's loop shape — outer loop over days (one panel in
memory), inner loop over candidates:

* :func:`evaluate_day` computes one candidate × one panel → :class:`DayEval`
  (per-day ICs, latency/decay ICs, bucket sums, flip events, spread stats);
* :func:`aggregate_split` folds a candidate's ``DayEval`` list into the
  split-level metric dict whose keys mirror ``research.experiment_results``
  columns, including the approved maker extension
  (``taifex_maker_qhat_v1``) when a :class:`QHatTable` is supplied.

Reuses ``research/backtest/metrics.py::compute_ic`` (chunked Pearson) and
``compute_ic_ttest``; Spearman rank IC is a local NaN-aware implementation
(``batch_alpha_eval.information_coefficient`` hard-codes a ``signal != 0``
exclusion that is wrong for two-sided factors).

Cost proxy (``cost_assumption_version = taifex_v1``, FROZEN): discretize the
signal to sign-of-zscore with ±``hysteresis_sigma`` hysteresis; every position
change is a flip; ``gross_pts_per_flip`` = mean |mid move at declared horizon|
at flip rows; ``required_move_threshold_pts = 2*(comm+tax) + median spread``.

Latency stress (``lat_shift_v1``): for each δ the label is re-anchored at
availability time — ``j = asof(local_ts[i] + δ)``, entry mid = mid[j], horizon
from there — which is exactly ``label0[j]``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from research.backtest.metrics import compute_ic, compute_ic_ttest
from research.backtest.q_hat_table import QHatTable
from research.candidate_loop.compiler import evaluate_regime, evaluate_signal
from research.candidate_loop.maker_cost import (
    MAKER_COST_ASSUMPTION_VERSION,
    compute_maker_cost,
)
from research.candidate_loop.panels import Panel
from research.candidate_loop.primitives import (
    future_mid_return,
    parse_canonical_window,
    rolling_zscore,
)
from research.candidate_loop.schema import Window
from research.candidate_loop.validator import ValidCandidate

MIN_IC_ROWS = 32  # below this a day's IC is statistical noise


@dataclass(frozen=True)
class EvaluatorConfig:
    evaluator_version: str
    primitive_version: str
    latency_config_version: str
    cost_assumption_version: str
    tick_size: dict[str, float]
    cost_proxy_zscore_window: str
    hysteresis_sigma: float
    latency_shifts_ms: tuple[int, ...]
    horizon_decay_multipliers: tuple[float, ...]
    bucket_count: int
    dir_coverage_threshold: float
    min_valid_rows_per_day: int
    signal_std_epsilon: float


def load_evaluator_config(path: Path) -> EvaluatorConfig:
    raw = yaml.safe_load(path.read_text())
    return EvaluatorConfig(
        evaluator_version=str(raw["evaluator_version"]),
        primitive_version=str(raw["primitive_version"]),
        latency_config_version=str(raw["latency_config_version"]),
        cost_assumption_version=str(raw["cost_assumption_version"]),
        tick_size={str(k): float(v) for k, v in raw["tick_size"].items()},
        cost_proxy_zscore_window=str(raw["cost_proxy"]["zscore_window"]),
        hysteresis_sigma=float(raw["cost_proxy"]["hysteresis_sigma"]),
        latency_shifts_ms=tuple(int(x) for x in raw["latency_shifts_ms"]),
        horizon_decay_multipliers=tuple(float(x) for x in raw["horizon_decay_multipliers"]),
        bucket_count=int(raw["bucket_count"]),
        dir_coverage_threshold=float(raw["dir_coverage_threshold"]),
        min_valid_rows_per_day=int(raw["min_valid_rows_per_day"]),
        signal_std_epsilon=float(raw["signal_std_epsilon"]),
    )


# ---------------------------------------------------------------------------
# Building blocks (unit-tested directly).
# ---------------------------------------------------------------------------


def shift_label(label0: np.ndarray, local_ts: np.ndarray, delta_ns: int) -> np.ndarray:
    """Re-anchor the label at availability time ``t + δ`` (lat_shift_v1).

    ``j = asof(local_ts[i] + δ)`` (last row at or before), result =
    ``label0[j]`` — entry mid at the asof row, declared horizon from there.
    δ=0 is the identity.
    """
    if delta_ns == 0:
        return label0
    j = np.searchsorted(local_ts, local_ts + delta_ns, side="right").astype(np.int64) - 1
    return label0[j]


def discretize_with_hysteresis(z: np.ndarray, sigma: float) -> tuple[np.ndarray, np.ndarray]:
    """Sign-of-zscore positions with ±sigma hysteresis (taifex_v1 cost proxy).

    Position enters +1 when ``z > +sigma``, −1 when ``z < −sigma``, otherwise
    holds (NaN holds too).  Returns ``(positions int8, flip_indices)`` where a
    flip is ANY position change (including the first entry from 0).
    """
    n = z.size
    pos = np.zeros(n, dtype=np.int8)
    flips: list[int] = []
    cur = 0
    zs = z.tolist()
    for i in range(n):
        zi = zs[i]
        if zi == zi:  # NaN holds
            if zi > sigma:
                new = 1
            elif zi < -sigma:
                new = -1
            else:
                new = cur
            if new != cur:
                flips.append(i)
                cur = new
        pos[i] = cur
    return pos, np.asarray(flips, dtype=np.int64)


def scale_window(horizon: Window, multiplier: float) -> Window:
    if horizon.kind == "events":
        return Window(kind="events", count=max(1, round(horizon.count * multiplier)))
    return Window(kind="time", duration_ns=max(1, int(horizon.duration_ns * multiplier)))


def _pearson_ic(signal: np.ndarray, label: np.ndarray) -> float:
    """Chunked Pearson day IC (reuses backtest compute_ic); 0.0 when too thin."""
    if signal.size < MIN_IC_ROWS:
        return 0.0
    ic_mean, _, _ = compute_ic(signal, label)
    return ic_mean


def _rank_ic(signal: np.ndarray, label: np.ndarray) -> float:
    """Spearman rank IC over already-valid rows; 0.0 when too thin/degenerate."""
    if signal.size < MIN_IC_ROWS:
        return 0.0
    if float(np.std(signal)) == 0.0 or float(np.std(label)) == 0.0:
        return 0.0  # Spearman undefined on constant input
    from scipy.stats import spearmanr

    rho, _ = spearmanr(signal, label)
    return float(rho) if math.isfinite(float(rho)) else 0.0


# ---------------------------------------------------------------------------
# Per-day evaluation.
# ---------------------------------------------------------------------------


@dataclass
class DayEval:
    day: str
    symbol: str
    skipped_reason: str = ""  # 'dir_dirty' | 'empty_panel' | '' (used)
    n_valid: int = 0
    counts_for_stats: bool = False  # n_valid >= min_valid_rows_per_day
    signal_std: float = 0.0
    ic: float = 0.0
    rank_ic: float = 0.0
    regime_ic_out: float = 0.0
    tight_ic: float = 0.0
    wide_ic: float = 0.0
    latency_ics: dict[int, float] = field(default_factory=dict)
    decay_ics: dict[float, float] = field(default_factory=dict)
    bucket_sums_pts: np.ndarray = field(default_factory=lambda: np.zeros(0))
    bucket_counts: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    flips: int = 0
    gross_pts_sum: float = 0.0
    gross_pts_count: int = 0
    median_spread_pts: float = 0.0
    flip_ts: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    flip_depth: np.ndarray = field(default_factory=lambda: np.zeros(0))
    horizon_ms_estimate: float = 0.0


def evaluate_day(valid: ValidCandidate, panel: Panel, cfg: EvaluatorConfig) -> DayEval:
    meta = panel.meta
    day = str(meta.get("day", ""))
    symbol = str(meta.get("symbol", ""))
    if valid.uses_trade_imbalance and not bool(meta.get("dir_clean", False)):
        return DayEval(day=day, symbol=symbol, skipped_reason="dir_dirty")
    cols = panel.columns
    if not cols or cols["local_ts"].size == 0:
        return DayEval(day=day, symbol=symbol, skipped_reason="empty_panel")

    local_ts = cols["local_ts"]
    tick_size = float(meta.get("tick_size", cfg.tick_size.get(symbol, 1.0)))
    horizon = parse_canonical_window(valid.candidate.horizon)

    signal = evaluate_signal(valid.signal_ast, cols)
    if valid.regime_ast is not None:
        regime = evaluate_regime(valid.regime_ast, cols)
        signal_gated = np.where(regime, signal, np.nan)
    else:
        regime = None
        signal_gated = signal

    label = future_mid_return(cols, horizon)
    label_pts = label * cols["mid"]  # forward mid move in points

    finite_both = np.isfinite(signal) & np.isfinite(label)
    in_mask = np.isfinite(signal_gated) & np.isfinite(label)
    n_valid = int(np.count_nonzero(in_mask))
    result = DayEval(day=day, symbol=symbol, n_valid=n_valid)
    result.counts_for_stats = n_valid >= cfg.min_valid_rows_per_day
    if n_valid == 0:
        return result

    sig_in = signal_gated[in_mask]
    lab_in = label[in_mask]
    result.signal_std = float(np.std(sig_in))
    result.ic = _pearson_ic(sig_in, lab_in)
    result.rank_ic = _rank_ic(sig_in, lab_in)

    if regime is not None:
        out_mask = finite_both & ~regime
        if int(np.count_nonzero(out_mask)) >= MIN_IC_ROWS:
            result.regime_ic_out = _pearson_ic(signal[out_mask], label[out_mask])

    # Canonical wide/tight spread split (r33 pattern) within the in-regime rows.
    spreads_in = cols["spread_ticks"][in_mask]
    finite_spread = np.isfinite(spreads_in)
    if int(np.count_nonzero(finite_spread)) > 0:
        med_spread_ticks = float(np.median(spreads_in[finite_spread]))
        tight = in_mask & (cols["spread_ticks"] <= med_spread_ticks)
        wide = in_mask & (cols["spread_ticks"] > med_spread_ticks)
        result.tight_ic = _pearson_ic(signal_gated[tight], label[tight])
        result.wide_ic = _pearson_ic(signal_gated[wide], label[wide])
        result.median_spread_pts = med_spread_ticks * tick_size

    # Latency stress: label re-anchored at t + δ.
    for delta_ms in cfg.latency_shifts_ms:
        lab_d = shift_label(label, local_ts, delta_ms * 1_000_000)
        mask_d = np.isfinite(signal_gated) & np.isfinite(lab_d)
        result.latency_ics[delta_ms] = _pearson_ic(signal_gated[mask_d], lab_d[mask_d])

    # Horizon decay.
    for mult in cfg.horizon_decay_multipliers:
        lab_m = future_mid_return(cols, scale_window(horizon, mult))
        mask_m = np.isfinite(signal_gated) & np.isfinite(lab_m)
        result.decay_ics[mult] = _pearson_ic(signal_gated[mask_m], lab_m[mask_m])

    # 5-bucket mean forward move (points) by signal quantile.
    nb = cfg.bucket_count
    result.bucket_sums_pts = np.zeros(nb)
    result.bucket_counts = np.zeros(nb, dtype=np.int64)
    if n_valid >= nb * 2:
        edges = np.quantile(sig_in, np.linspace(0.0, 1.0, nb + 1)[1:-1])
        bucket_idx = np.searchsorted(edges, sig_in, side="right")
        lab_pts_in = label_pts[in_mask]
        for b in range(nb):
            sel = bucket_idx == b
            result.bucket_sums_pts[b] = float(np.sum(lab_pts_in[sel]))
            result.bucket_counts[b] = int(np.count_nonzero(sel))

    # taifex_v1 cost proxy: sign-of-zscore positions with hysteresis.
    z = rolling_zscore(signal_gated, local_ts, parse_canonical_window(cfg.cost_proxy_zscore_window))
    positions, flip_idx = discretize_with_hysteresis(z, cfg.hysteresis_sigma)
    result.flips = int(flip_idx.size)
    if flip_idx.size:
        moves = np.abs(label_pts[flip_idx])
        finite_moves = np.isfinite(moves)
        result.gross_pts_sum = float(np.sum(moves[finite_moves]))
        result.gross_pts_count = int(np.count_nonzero(finite_moves))
        result.flip_ts = local_ts[flip_idx].astype(np.int64)
        depth = np.where(positions[flip_idx] > 0, cols["bid_qty_1"][flip_idx], cols["ask_qty_1"][flip_idx])
        result.flip_depth = depth.astype(np.float64)

    # ms-equivalent of the declared horizon (decay halflife reporting).
    if horizon.kind == "time":
        result.horizon_ms_estimate = horizon.duration_ns / 1e6
    elif local_ts.size > 1:
        med_dt_ns = float(np.median(np.diff(local_ts)))
        result.horizon_ms_estimate = med_dt_ns * horizon.count / 1e6
    return result


# ---------------------------------------------------------------------------
# Split aggregation.
# ---------------------------------------------------------------------------


def aggregate_split(
    day_evals: list[DayEval],
    *,
    expected_sign: str,
    cfg: EvaluatorConfig,
    cost_per_side_pts: float,
    q_hat: QHatTable | None = None,
    q_hat_symbol: str = "",
) -> dict[str, Any]:
    """Fold per-day evals into split metrics (keys mirror experiment_results)."""
    expected = 1.0 if expected_sign == "positive" else -1.0
    used = [d for d in day_evals if not d.skipped_reason]
    stat_days = [d for d in used if d.counts_for_stats]
    daily_ics = np.asarray([d.ic for d in stat_days], dtype=np.float64)

    ic_mean = float(np.mean(daily_ics)) if daily_ics.size else 0.0
    rank_ic_mean = float(np.mean([d.rank_ic for d in stat_days])) if stat_days else 0.0
    ic_tstat, _ = compute_ic_ttest(daily_ics)
    n_stat = max(len(stat_days), 1)
    sign_consistency = float(np.count_nonzero(np.sign(daily_ics) == expected)) / n_stat if stat_days else 0.0
    day_stability = float(np.count_nonzero(daily_ics > 0.0)) / n_stat if stat_days else 0.0
    n_used = max(len(used), 1)
    std_zero_fraction = float(sum(1 for d in used if d.signal_std <= cfg.signal_std_epsilon)) / n_used if used else 1.0

    # Buckets pooled across days.
    nb = cfg.bucket_count
    bucket_sums = np.zeros(nb)
    bucket_counts = np.zeros(nb, dtype=np.int64)
    for d in used:
        if d.bucket_sums_pts.size == nb:
            bucket_sums += d.bucket_sums_pts
            bucket_counts += d.bucket_counts
    with np.errstate(invalid="ignore"):
        bucket_means = np.where(bucket_counts > 0, bucket_sums / bucket_counts, np.nan)
    finite_buckets = np.isfinite(bucket_means)
    if int(np.count_nonzero(finite_buckets)) >= 2:
        fb = bucket_means[finite_buckets]
        bucket_spread_pts = float((fb[-1] - fb[0]) * expected)
        steps = np.diff(fb) * expected
        bucket_monotonicity = float(np.count_nonzero(steps > 0.0)) / steps.size
    else:
        bucket_spread_pts = 0.0
        bucket_monotonicity = 0.0

    # one_day_concentration: max single-day share of total |bucket spread|.
    day_contrib = []
    for d in stat_days:
        if d.bucket_counts.size == nb and int(d.bucket_counts.sum()) > 0:
            with np.errstate(invalid="ignore"):
                means = np.where(d.bucket_counts > 0, d.bucket_sums_pts / d.bucket_counts, np.nan)
            fin = means[np.isfinite(means)]
            day_contrib.append(abs(float(fin[-1] - fin[0])) if fin.size >= 2 else 0.0)
    total_contrib = float(sum(day_contrib))
    if total_contrib > 0.0:
        one_day_concentration = max(day_contrib) / total_contrib
    else:
        one_day_concentration = 1.0  # fail-closed: no spread evidence at all

    # Latency scores: signed retention vs δ=0.
    latency_means = {
        delta: float(np.mean([d.latency_ics.get(delta, 0.0) for d in stat_days])) if stat_days else 0.0
        for delta in cfg.latency_shifts_ms
    }
    ic0 = latency_means.get(0, 0.0)
    latency_scores = {delta: (latency_means[delta] / ic0 if ic0 != 0.0 else 0.0) for delta in cfg.latency_shifts_ms}

    # Horizon decay halflife (ms-equivalent of the declared horizon).
    decay_means = {
        mult: float(np.mean([d.decay_ics.get(mult, 0.0) for d in stat_days])) if stat_days else 0.0
        for mult in cfg.horizon_decay_multipliers
    }
    horizon_ms = float(np.mean([d.horizon_ms_estimate for d in used])) if used else 0.0
    base_abs = abs(decay_means.get(1.0, 0.0))
    halflife_ms = horizon_ms * max(cfg.horizon_decay_multipliers, default=1.0)
    if base_abs > 0.0:
        for mult in sorted(m for m in cfg.horizon_decay_multipliers if m > 1.0):
            if abs(decay_means[mult]) < 0.5 * base_abs:
                halflife_ms = horizon_ms * mult
                break

    # Regime stability: tight/wide spread IC sign agreement.
    regime_pairs = [(d.tight_ic, d.wide_ic) for d in stat_days if d.tight_ic != 0.0 or d.wide_ic != 0.0]
    regime_stability = (
        float(sum(1 for t, w in regime_pairs if np.sign(t) == np.sign(w))) / len(regime_pairs) if regime_pairs else 0.0
    )
    regime_ic_out_mean = float(np.mean([d.regime_ic_out for d in stat_days])) if stat_days else 0.0
    tight_mean = float(np.mean([d.tight_ic for d in stat_days])) if stat_days else 0.0
    wide_mean = float(np.mean([d.wide_ic for d in stat_days])) if stat_days else 0.0

    # taifex_v1 taker cost proxy.
    total_flips = sum(d.flips for d in used)
    gross_sum = float(sum(d.gross_pts_sum for d in used))
    gross_count = sum(d.gross_pts_count for d in used)
    gross_pts_per_flip = gross_sum / gross_count if gross_count else 0.0
    day_spreads = [d.median_spread_pts for d in used if d.median_spread_pts > 0.0]
    median_spread_pts = float(np.median(day_spreads)) if day_spreads else 0.0
    required_move = 2.0 * cost_per_side_pts + median_spread_pts
    cost_survival_score = gross_pts_per_flip / required_move if required_move > 0.0 else 0.0
    effective_days = len(used)
    turnover_proxy = total_flips / effective_days if effective_days else 0.0

    metrics: dict[str, Any] = {
        "day_count": len(day_evals),
        "effective_day_count": effective_days,
        "stat_day_count": len(stat_days),
        "ic": ic_mean,
        "rank_ic": rank_ic_mean,
        "ic_tstat": float(ic_tstat),
        "sign_consistency": sign_consistency,
        "day_stability": day_stability,
        "signal_std_zero_day_fraction": std_zero_fraction,
        "bucket_spread_pts": bucket_spread_pts,
        "bucket_monotonicity": bucket_monotonicity,
        "one_day_concentration": one_day_concentration,
        "horizon_decay_halflife_ms": halflife_ms,
        "regime_ic_in": ic_mean,
        "regime_ic_out": regime_ic_out_mean,
        "regime_ic_tight_spread": tight_mean,
        "regime_ic_wide_spread": wide_mean,
        "regime_stability": regime_stability,
        "turnover_proxy": turnover_proxy,
        "gross_pts_per_flip": gross_pts_per_flip,
        "median_spread_pts": median_spread_pts,
        "required_move_threshold_pts": required_move,
        "cost_survival_score": cost_survival_score,
        "daily_ics": [float(v) for v in daily_ics],
        "decay_ics": {str(k): v for k, v in decay_means.items()},
    }
    for delta in cfg.latency_shifts_ms:
        metrics[f"latency_{delta}ms_score"] = latency_scores[delta]
        metrics[f"latency_{delta}ms_ic"] = latency_means[delta]

    if q_hat is not None:
        flip_ts = (
            np.concatenate([d.flip_ts for d in used if d.flip_ts.size])
            if any(d.flip_ts.size for d in used)
            else np.zeros(0, dtype=np.int64)
        )
        flip_depth = (
            np.concatenate([d.flip_depth for d in used if d.flip_depth.size])
            if any(d.flip_depth.size for d in used)
            else np.zeros(0)
        )
        maker = compute_maker_cost(
            flip_ts_ns=flip_ts,
            near_side_l1_qty=flip_depth,
            gross_pts_per_flip=gross_pts_per_flip,
            median_spread_pts=median_spread_pts,
            cost_per_side_pts=cost_per_side_pts,
            q_hat=q_hat,
            q_hat_symbol=q_hat_symbol,
        )
        metrics["maker_fill_prob_mean"] = maker.maker_fill_prob_mean
        metrics["maker_required_move_threshold_pts"] = maker.maker_required_move_threshold_pts
        metrics["maker_cost_survival_score"] = maker.maker_cost_survival_score
        metrics["maker_cost_assumption_version"] = MAKER_COST_ASSUMPTION_VERSION
    return metrics


__all__ = [
    "MIN_IC_ROWS",
    "DayEval",
    "EvaluatorConfig",
    "aggregate_split",
    "discretize_with_hysteresis",
    "evaluate_day",
    "load_evaluator_config",
    "scale_window",
    "shift_label",
]
