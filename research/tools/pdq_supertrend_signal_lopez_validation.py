"""Test the Supertrend indicator itself as a signal, alone and combined, and
validate with the Lopez de Prado overfitting-control toolkit (PBO/CSCV + DSR).

Every earlier script in this thread used Supertrend as an EXIT policy riding
on top of a fixed, already-decided causal PDQ_cont+TSI15 entry. This script
asks a different question directly: is Supertrend itself -- the plain
`ta.supertrend(factor, atrPeriod)` direction-flip indicator, tested as a
signal, not an exit -- a source of edge, alone or combined with the existing
entries? And instead of the simple "rank on IS, reveal OOS only for top-20"
method used in every prior script this session, this one applies the two
headline members of the Lopez de Prado backtest-overfitting toolkit:

- PBO via CSCV (Bailey, Borwein, Lopez de Prado & Zhu 2017, "The Probability
  of Backtest Overfitting"): partition the trading-day calendar into
  `n_groups` contiguous blocks, enumerate all C(n_groups, n_groups/2)
  symmetric train/test splits, and for each split pick the IS-best trial and
  check whether it still ranks above the OOS median. PBO is the fraction of
  splits where it does not.
- Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014): the probability the
  true Sharpe of the grid's selected trial is positive after deflating for
  the number of trials searched, the variance across trial Sharpes, and the
  selected trial's own skew/kurtosis.

Why not this repo's own `HftNativeRunner.run_cpcv` (`research/backtest/
hft_native_runner.py`, `CPCVConfig`/`CPCVResult` in `research/backtest/
types.py`): that path requires an `AlphaProtocol` object and a full
hftbacktest run (tick-level `.npz`, queue/latency simulation) per CSCV path
per trial -- intractable at 1,014-combination grid-search scale, and it
computes a different, coarser statistic (one alpha's own OOS-Sharpe
consistency across paths, not the Bailey et al. across-trials selection-bias
PBO). This script implements the actual paper's CSCV/PBO statistic directly
against the vectorized per-trial daily-PnL matrix already used throughout
this session's grid searches, and reuses `n_groups=6` to match this repo's
own CPCVConfig default (giving the same C(6,3)=20 paths) for comparability.

Two signal variants, same 6 timeframe x 13 atr_period x 13 factor grid used
by the uncapped and v2 exit searches (1,014 combinations each):

- "alone": pure Supertrend flip-to-flip trend following. Enter at every
  confirmed direction flip, exit at the next flip or day-end backstop
  (whichever is first) -- no dependency on PDQ_cont/TSI15 at all. This is
  literally what the pasted Pine indicator signals (direction persists until
  reversal); it is not the same exercise as the exit-search grid.
- "combined": the existing causal PDQ_cont+TSI15 entries (`CAUSAL_EVENTS_
  PATH`, unchanged), gated by requiring the entry's direction agree with the
  Supertrend state observed at (or just before) entry time, for each grid
  cell. Exit policy held at the already-published fixed gene (1m/ATR3/
  factor2.1/900s) so this isolates the entry-side agreement filter as the
  only variable -- consistent with every other script this session testing
  exactly one dimension at a time.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BASE_TOOL = ROOT / "research/tools/pdq_tsi15_decomposition_audit.py"
EXIT_TOOL = ROOT / "research/tools/pdq_supertrend_exit_search.py"
GRID_TOOL = ROOT / "research/tools/pdq_causal_supertrend_grid_search.py"
CAUSAL_EVENTS_PATH = ROOT / "outputs/liquidity_score/pdq_causal_walkforward/causal_event_level_paths.csv.gz"
OUT_DIR = ROOT / "outputs/liquidity_score/pdq_supertrend_signal_lopez_validation"

IS_OOS_CUTOFF_DAY = "2026-06-01"  # kept for headline reporting continuity with earlier scripts
COST4 = 4.0
TRADING_PERIODS_PER_YEAR = 252.0

TIMEFRAMES = ("1m", "2m", "3m", "5m", "10m", "15m")
ATR_PERIODS = (3, 5, 7, 8, 10, 13, 17, 21, 26, 34, 42, 50, 60)
FACTORS = (0.6, 0.8, 1.0, 1.2, 1.5, 1.8, 2.1, 2.5, 3.0, 3.5, 4.5, 6.0, 8.0)

FIXED_LIQUIDITY_PARAMS = {
    "exit_mode": "first",
    "min_depth_ratio": 1.3,
    "max_spread_ratio": 0.5,
    "min_zlogl_delta": 0.0,
    "confirmations": 3,
}
PUBLISHED_GENE_KWARGS = {"timeframe": "1m", "atr_period": 3, "factor": 2.1, "max_hold_s": 900}

CSCV_N_GROUPS = 6  # matches research/backtest/types.py CPCVConfig default -> C(6,3) = 20 paths

ALONE_IS_MIN_N = 15
ALONE_IS_MIN_ACTIVE_DAYS = 8
COMBINED_IS_MIN_N = 300
COMBINED_IS_MIN_ACTIVE_DAYS = 15

_NORMAL = statistics.NormalDist(mu=0.0, sigma=1.0)
_EULER_GAMMA = 0.5772156649015329


def load_module(path: Path, name: str) -> Any:
    """Load `path` under its real dotted package name, reusing any existing load."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load_module(BASE_TOOL, "research.tools.pdq_tsi15_decomposition_audit")
exit_search = load_module(EXIT_TOOL, "research.tools.pdq_supertrend_exit_search")
grid_search = load_module(GRID_TOOL, "research.tools.pdq_causal_supertrend_grid_search")
exit_search.ENTRY_PATH = CAUSAL_EVENTS_PATH


def grid() -> list[tuple[str, int, float]]:
    return list(itertools.product(TIMEFRAMES, ATR_PERIODS, FACTORS))


def build_day_end_array(evaluator: Any) -> np.ndarray:
    """Per-secbar-row day-end second, index-aligned with `evaluator.seconds`."""
    return evaluator.secbar.groupby("day")["sec"].transform("max").to_numpy(dtype=np.int64)


def lookup_day_end(evaluator: Any, day_end_arr: np.ndarray, times_s: np.ndarray) -> np.ndarray:
    idx = np.searchsorted(evaluator.seconds, times_s, side="left")
    idx = np.clip(idx, 0, len(evaluator.seconds) - 1)
    return day_end_arr[idx]


def lookup_day(evaluator: Any, times_s: np.ndarray) -> np.ndarray:
    idx = np.searchsorted(evaluator.seconds, times_s, side="left")
    idx = np.clip(idx, 0, len(evaluator.seconds) - 1)
    return evaluator.secbar["day"].to_numpy()[idx]


def _empty_trades_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=["day", "entry_s", "exit_s", "hold_s", "reason", "gross_pnl"])


def build_alone_trades(
    evaluator: Any, day_end_arr: np.ndarray, timeframe: str, atr_period: int, factor: float
) -> pd.DataFrame:
    """Pure Supertrend flip-to-flip trend-following trades: enter at every
    confirmed direction flip, exit at the next flip or day-end, whichever is
    first. No dependency on the causal PDQ_cont/TSI15 entries.
    """
    bars = evaluator.bars[timeframe]
    states = exit_search.compute_supertrend_direction(
        bars["high"], bars["low"], bars["close"], atr_period=atr_period, factor=factor
    )
    bar_end_s = bars["bar_end_s"]
    valid = np.isfinite(states)
    if valid.sum() < 2:
        return _empty_trades_frame()

    valid_idx = np.flatnonzero(valid)
    valid_states = states[valid_idx].astype(np.int8)
    changed = np.empty(len(valid_states), dtype=bool)
    changed[0] = True
    changed[1:] = valid_states[1:] != valid_states[:-1]
    flip_bar_idx = valid_idx[changed]
    flip_state = valid_states[changed]
    if len(flip_bar_idx) < 2:
        return _empty_trades_frame()

    entry_bar_idx = flip_bar_idx[:-1]
    next_flip_bar_idx = flip_bar_idx[1:]
    position_dir = flip_state[:-1]
    entry_s = bar_end_s[entry_bar_idx]
    next_flip_s = bar_end_s[next_flip_bar_idx]

    deadline_s = lookup_day_end(evaluator, day_end_arr, entry_s)
    exit_target_s = np.minimum(next_flip_s, deadline_s)
    reason = np.where(next_flip_s <= deadline_s, "supertrend_flip", "day_end")

    entry_exec_idx = exit_search.execution_indices_for_times(
        evaluator.seconds, entry_s, max_lag_s=exit_search.MAX_EXECUTION_LAG_S
    )
    exit_exec_idx = exit_search.execution_indices_for_times(
        evaluator.seconds, exit_target_s, max_lag_s=exit_search.MAX_EXECUTION_LAG_S
    )
    complete = (entry_exec_idx >= 0) & (exit_exec_idx >= 0) & (exit_target_s > entry_s)
    if not np.any(complete):
        return _empty_trades_frame()

    entry_mid = np.full(len(entry_s), np.nan)
    exit_mid = np.full(len(entry_s), np.nan)
    entry_mid[complete] = evaluator.mid[entry_exec_idx[complete]]
    exit_mid[complete] = evaluator.mid[exit_exec_idx[complete]]
    gross = position_dir.astype(float) * (exit_mid - entry_mid)
    day = lookup_day(evaluator, entry_s)
    hold_s = np.where(complete, exit_target_s - entry_s, np.nan)

    frame = pd.DataFrame(
        {
            "day": day,
            "entry_s": entry_s,
            "exit_s": exit_target_s,
            "hold_s": hold_s,
            "reason": reason,
            "gross_pnl": gross,
            "position_dir": position_dir,
        }
    )
    return frame.loc[complete].reset_index(drop=True)


def build_combined_trades(
    evaluator: Any, published_paths: pd.DataFrame, timeframe: str, atr_period: int, factor: float
) -> pd.DataFrame:
    """Causal PDQ_cont+TSI15 entries gated by Supertrend direction agreement
    at (or just before) entry time. Exit policy is the fixed published gene
    (`published_paths` is computed once and reused across the whole grid);
    only the entry-side agreement mask varies per grid cell.
    """
    bars = evaluator.bars[timeframe]
    states = exit_search.compute_supertrend_direction(
        bars["high"], bars["low"], bars["close"], atr_period=atr_period, factor=factor
    )
    clean_states = np.where(np.isfinite(states), states, 0).astype(np.int8)
    bar_end_s = bars["bar_end_s"]
    bar_idx = np.searchsorted(bar_end_s, evaluator.entry_s, side="right") - 1
    state_at_entry = np.where(bar_idx >= 0, clean_states[np.clip(bar_idx, 0, None)], 0)
    agree = state_at_entry == evaluator.position_dirs
    return published_paths.loc[agree].reset_index(drop=True)


def daily_net_series(trades: pd.DataFrame, all_days: list[str]) -> pd.Series:
    sub = trades.dropna(subset=["gross_pnl"]).copy()
    if sub.empty:
        return pd.Series(0.0, index=all_days)
    sub["net"] = sub["gross_pnl"] - COST4
    daily = sub.groupby("day")["net"].sum()
    return daily.reindex(all_days, fill_value=0.0)


def _period_sharpe(block: np.ndarray) -> np.ndarray:
    """Non-annualized per-period Sharpe of every trial (column) in `block`."""
    mean = block.mean(axis=0)
    std = block.std(axis=0, ddof=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        sharpe = np.where(std > 0, mean / std, 0.0)
    return sharpe


def cscv_pbo(matrix: np.ndarray, n_groups: int) -> dict[str, float | int]:
    """Bailey, Borwein, Lopez de Prado & Zhu (2017) PBO via CSCV.

    `matrix` is T (trading days) x N (trials), daily net PnL. Splits the day
    axis into `n_groups` contiguous blocks, enumerates every symmetric
    train/test combination, and for each one checks whether the IS-best
    trial still outperforms the OOS median across all N trials.
    """
    t_periods, n_trials = matrix.shape
    group_size = t_periods // n_groups
    if group_size < 2 or n_trials < 2:
        return {
            "n_groups": n_groups,
            "n_paths": 0,
            "pbo": float("nan"),
            "mean_logit": float("nan"),
            "mean_is_oos_degradation": float("nan"),
        }

    boundaries = [i * group_size for i in range(n_groups)] + [t_periods]
    groups = [np.arange(boundaries[i], boundaries[i + 1]) for i in range(n_groups)]
    n_test = n_groups // 2
    combos = list(itertools.combinations(range(n_groups), n_test))

    logits = []
    degradations = []
    for test_groups in combos:
        test_idx = np.concatenate([groups[g] for g in test_groups])
        train_groups = [g for g in range(n_groups) if g not in test_groups]
        train_idx = np.concatenate([groups[g] for g in train_groups])

        is_sharpe = _period_sharpe(matrix[train_idx])
        oos_sharpe = _period_sharpe(matrix[test_idx])

        n_star = int(np.argmax(is_sharpe))
        rank = float(np.sum(oos_sharpe <= oos_sharpe[n_star])) / (n_trials + 1)
        rank = min(max(rank, 1e-6), 1 - 1e-6)
        logits.append(math.log(rank / (1 - rank)))
        degradations.append(float(is_sharpe[n_star] - oos_sharpe[n_star]))

    logits_arr = np.asarray(logits)
    return {
        "n_groups": n_groups,
        "n_paths": len(combos),
        "pbo": float(np.mean(logits_arr <= 0)),
        "mean_logit": float(np.mean(logits_arr)),
        "mean_is_oos_degradation": float(np.mean(degradations)),
    }


def expected_max_sharpe_under_null(trial_sharpes: np.ndarray) -> float:
    """E[max Sharpe] across N independent zero-skill trials (Bailey & Lopez
    de Prado 2014, eq. 7), using the observed cross-trial Sharpe variance as
    the null's per-trial variance estimate.
    """
    n = len(trial_sharpes)
    if n < 2:
        return 0.0
    sigma_sr = float(np.std(trial_sharpes, ddof=1))
    if sigma_sr == 0.0:
        return 0.0
    z1 = _NORMAL.inv_cdf(1.0 - 1.0 / n)
    z2 = _NORMAL.inv_cdf(1.0 - 1.0 / (n * math.e))
    return sigma_sr * ((1 - _EULER_GAMMA) * z1 + _EULER_GAMMA * z2)


def _population_skew(x: np.ndarray) -> float:
    n = len(x)
    mean = x.mean()
    std = x.std(ddof=0)
    if std == 0 or n < 3:
        return 0.0
    return float(np.mean(((x - mean) / std) ** 3))


def _population_kurtosis_raw(x: np.ndarray) -> float:
    n = len(x)
    mean = x.mean()
    std = x.std(ddof=0)
    if std == 0 or n < 4:
        return 3.0
    return float(np.mean(((x - mean) / std) ** 4))


def deflated_sharpe_ratio(selected_daily_net: np.ndarray, trial_sharpes: np.ndarray) -> dict[str, float]:
    """Bailey & Lopez de Prado (2014) Deflated Sharpe Ratio.

    `selected_daily_net`: the winning trial's own daily net-PnL series
    (length T, same calendar as `trial_sharpes` was computed over).
    `trial_sharpes`: non-annualized per-period Sharpe of every trial searched
    (same basis/T as `selected_daily_net`), used only to estimate the null's
    expected-max-Sharpe benchmark.
    """
    t_obs = len(selected_daily_net)
    if t_obs < 3:
        return {"dsr": float("nan"), "sr_hat_annualized": float("nan"), "z": float("nan")}

    mean = float(selected_daily_net.mean())
    std = float(selected_daily_net.std(ddof=1))
    sr_hat = mean / std if std > 0 else 0.0
    skew = _population_skew(selected_daily_net)
    kurt = _population_kurtosis_raw(selected_daily_net)
    sr0 = expected_max_sharpe_under_null(trial_sharpes)
    denom = math.sqrt(max(1e-12, 1.0 - skew * sr_hat + (kurt - 1.0) / 4.0 * sr_hat**2))
    z = (sr_hat - sr0) * math.sqrt(t_obs - 1) / denom
    dsr = float(_NORMAL.cdf(z))
    return {
        "n_trials": int(len(trial_sharpes)),
        "t_obs": t_obs,
        "sr_hat_per_period": sr_hat,
        "sr_hat_annualized": sr_hat * math.sqrt(TRADING_PERIODS_PER_YEAR),
        "expected_max_sr_under_null_annualized": sr0 * math.sqrt(TRADING_PERIODS_PER_YEAR),
        "skew": skew,
        "kurtosis_raw": kurt,
        "z": z,
        "dsr": dsr,
    }


def rank_and_select(
    grid_df: pd.DataFrame, *, min_n: int, min_active_days: int
) -> tuple[pd.DataFrame, pd.Series | None]:
    eligible = grid_df[(grid_df["is_n"] >= min_n) & (grid_df["is_active_days"] >= min_active_days)].copy()
    if eligible.empty:
        return eligible, None
    ranked = eligible.sort_values("is_net_mean_cost4", ascending=False).reset_index(drop=True)
    return ranked, ranked.iloc[0]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    evaluator = exit_search.ExitEvaluator()
    day_end_arr = build_day_end_array(evaluator)
    all_days = sorted(evaluator.events["day"].unique().tolist())

    published_gene = exit_search.Gene(**PUBLISHED_GENE_KWARGS, **FIXED_LIQUIDITY_PARAMS)
    published_paths = evaluator.paths(published_gene)

    combos = grid()
    alone_rows: list[dict[str, Any]] = []
    combined_rows: list[dict[str, Any]] = []
    alone_daily_cols: dict[tuple[str, int, float], pd.Series] = {}
    combined_daily_cols: dict[tuple[str, int, float], pd.Series] = {}

    for timeframe, atr_period, factor in combos:
        key = (timeframe, atr_period, factor)

        alone_trades = build_alone_trades(evaluator, day_end_arr, timeframe, atr_period, factor)
        alone_is = (
            grid_search.summarize_split(alone_trades, alone_trades["day"].to_numpy() < IS_OOS_CUTOFF_DAY)
            if len(alone_trades)
            else grid_search.summarize_split(_empty_trades_frame(), np.array([], dtype=bool))
        )
        alone_rows.append(
            {
                "timeframe": timeframe,
                "atr_period": atr_period,
                "factor": factor,
                **{f"is_{k}": v for k, v in alone_is.items()},
            }
        )
        alone_daily_cols[key] = daily_net_series(alone_trades, all_days)

        combined_trades = build_combined_trades(evaluator, published_paths, timeframe, atr_period, factor)
        combined_is = (
            grid_search.summarize_split(combined_trades, combined_trades["day"].to_numpy() < IS_OOS_CUTOFF_DAY)
            if len(combined_trades)
            else grid_search.summarize_split(_empty_trades_frame(), np.array([], dtype=bool))
        )
        combined_rows.append(
            {
                "timeframe": timeframe,
                "atr_period": atr_period,
                "factor": factor,
                **{f"is_{k}": v for k, v in combined_is.items()},
            }
        )
        combined_daily_cols[key] = daily_net_series(combined_trades, all_days)

    alone_grid_df = pd.DataFrame(alone_rows)
    combined_grid_df = pd.DataFrame(combined_rows)

    alone_matrix = np.column_stack([alone_daily_cols[c].to_numpy() for c in combos])
    combined_matrix = np.column_stack([combined_daily_cols[c].to_numpy() for c in combos])

    alone_ranked, alone_winner = rank_and_select(
        alone_grid_df, min_n=ALONE_IS_MIN_N, min_active_days=ALONE_IS_MIN_ACTIVE_DAYS
    )
    combined_ranked, combined_winner = rank_and_select(
        combined_grid_df, min_n=COMBINED_IS_MIN_N, min_active_days=COMBINED_IS_MIN_ACTIVE_DAYS
    )

    alone_pbo = cscv_pbo(alone_matrix, CSCV_N_GROUPS)
    combined_pbo = cscv_pbo(combined_matrix, CSCV_N_GROUPS)

    alone_trial_sharpes = _period_sharpe(alone_matrix)
    combined_trial_sharpes = _period_sharpe(combined_matrix)

    def dsr_for_winner(winner_row: pd.Series | None, daily_cols: dict, trial_sharpes: np.ndarray) -> dict:
        if winner_row is None:
            return {"dsr": float("nan")}
        key = (winner_row["timeframe"], int(winner_row["atr_period"]), float(winner_row["factor"]))
        series = daily_cols[key].to_numpy()
        return deflated_sharpe_ratio(series, trial_sharpes)

    alone_dsr = dsr_for_winner(alone_winner, alone_daily_cols, alone_trial_sharpes)
    combined_dsr = dsr_for_winner(combined_winner, combined_daily_cols, combined_trial_sharpes)

    published_daily = daily_net_series(published_paths, all_days).to_numpy()
    published_dsr = deflated_sharpe_ratio(published_daily, combined_trial_sharpes)

    alone_grid_df.to_csv(OUT_DIR / "alone_grid_results.csv", index=False)
    combined_grid_df.to_csv(OUT_DIR / "combined_grid_results.csv", index=False)

    metadata = {
        "grid_size": len(combos),
        "timeframes": list(TIMEFRAMES),
        "atr_periods": list(ATR_PERIODS),
        "factors": list(FACTORS),
        "calendar_days": len(all_days),
        "cscv_n_groups": CSCV_N_GROUPS,
        "cscv_n_paths": alone_pbo.get("n_paths"),
        "alone": {
            "eligibility_floor": {"min_n": ALONE_IS_MIN_N, "min_active_days": ALONE_IS_MIN_ACTIVE_DAYS},
            "eligible_count": int(len(alone_ranked)) if alone_winner is not None else 0,
            "winner": None if alone_winner is None else alone_winner.to_dict(),
            "pbo_cscv": alone_pbo,
            "dsr": alone_dsr,
        },
        "combined": {
            "eligibility_floor": {"min_n": COMBINED_IS_MIN_N, "min_active_days": COMBINED_IS_MIN_ACTIVE_DAYS},
            "eligible_count": int(len(combined_ranked)) if combined_winner is not None else 0,
            "winner": None if combined_winner is None else combined_winner.to_dict(),
            "pbo_cscv": combined_pbo,
            "dsr": combined_dsr,
        },
        "published_baseline": {
            "gene": {**PUBLISHED_GENE_KWARGS, **FIXED_LIQUIDITY_PARAMS},
            "dsr": published_dsr,
        },
        "known_limitations": (
            "PBO/CSCV and DSR here operate on the fast vectorized event-level daily-PnL "
            "representation used throughout this session's grid searches, not a full "
            "hftbacktest tick-level replay per CSCV path (see module docstring for why); "
            "trial_sharpes used for the DSR null benchmark are the full-grid non-annualized "
            "daily Sharpe of all N combinations, which are not independent (adjacent atr/factor "
            "values produce highly correlated trials), so N should be read as a conservative "
            "upper bound on effective independent trials, not the true count."
        ),
    }
    (OUT_DIR / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True, default=str))
    print(json.dumps(metadata, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
