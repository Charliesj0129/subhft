"""Standalone re-validation of the PDQ "opportunity/permission" layer itself.

Every prior script in this research thread bundled three independently-
authored components into one strategy: the PDQ_cont liquidity/displacement
formula (entry gate), a TSI15 slow-trend filter (direction), and a Supertrend
indicator (exit, and separately tested as a signal). Both TSI15-as-filter and
Supertrend-as-signal were shown this session not to survive rigorous testing
(`pdq_causal_walkforward.py`: causal re-test flips the combo net-negative;
`pdq_supertrend_signal_lopez_validation.py`: PBO~=0.45, DSR~=0 for Supertrend
alone/combined). Those results killed the *combination*; they never isolated
whether the original liquidity/PDQ formula itself
(`outputs/liquidity_score/RESEARCH_SUMMARY_FROM_START.md` sections 3-8:
distance-weighted depth, harmonic mean of both book sides, spread penalty,
rolling z-score of log1p(L), feeding a common/residual NImp decomposition)
still carries information on its own.

That research summary's own conclusion (sections 4 and 8) was explicit:

    "A liquidity shock should therefore be treated as an opportunity or
    permission layer, not a long/short signal."
    "PDQ is a routing signal, not a direction signal."

This script takes that claim at face value and re-tests it in isolation,
with no TSI15 and no Supertrend anywhere:

- Direction, where a direction is needed at all, uses only the opportunity
  layer's own contemporaneous common-component sign (`signC60`) -- not an
  externally-fitted filter.
- Exit uses a dead-simple fixed-time horizon (300s / 900s) with a day-end
  backstop -- not the GA-tuned Supertrend/liquidity-recovery exit machinery.

Two tests, both built on a causal, expanding, prior-days-only calibration of
the opportunity thresholds (`build_opportunity_mask_param`, a direct
parameterization of `pdq_causal_walkforward.build_opportunity_mask_causal`;
15-day warmup, identical spread/depth gates, identical stable-book
exclusion):

1. PRIMARY -- the formula's own literal claim. Does the opportunity mask, at
   *onset* (first second of each contiguous firing run, to de-autocorrelate
   the sample), predict elevated forward ABSOLUTE displacement relative to a
   non-opportunity baseline, across multiple horizons? Significance via a
   day-local circular-shift permutation null: for each of 2,000 shifts, the
   opportunity mask is cyclically rotated by an independent random offset
   *within each trading day*, which preserves every day's own volatility
   level and the mask's per-day event count while destroying only the
   alignment between "opportunity now" and "future move now" -- the correct
   null for "does this signal carry information beyond which days are
   generally more volatile."

2. SECONDARY -- direct numeric comparability to
   `pdq_supertrend_signal_lopez_validation.py`'s headline PBO/DSR figures
   (PBO~=0.45, DSR 0.0003-0.034 for the TSI15/Supertrend-contaminated
   variants). Sweeps the three numeric knobs inside the opportunity
   definition that were fixed by hand and never grid-searched in any prior
   script (|C60| quantile, rvexp quantile, cross-root-agreement minimum;
   5x4x3 = 60 combinations), combined only with contemporaneous `signC60`
   direction and a fixed exit, reusing `cscv_pbo` / `deflated_sharpe_ratio`
   / `daily_net_series` verbatim from `pdq_supertrend_signal_lopez_
   validation.py` for exact methodological parity.

Known, inherited limitation (documented in `RESEARCH_SUMMARY_FROM_START.md`
section 17 item 5, not introduced here): trade events are not deduplicated
for concurrent/overlapping positions, matching every prior script in this
program.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BASE_TOOL = ROOT / "research/tools/pdq_tsi15_decomposition_audit.py"
LOPEZ_TOOL = ROOT / "research/tools/pdq_supertrend_signal_lopez_validation.py"
GRID_TOOL = ROOT / "research/tools/pdq_causal_supertrend_grid_search.py"
OUT_DIR = ROOT / "outputs/liquidity_score/pdq_opportunity_standalone_validation"

WARMUP_DAYS = 15
IS_OOS_CUTOFF_DAY = "2026-06-01"
ROOTS = ("TXF", "MXF", "TMF")

# Test 1: published opportunity definition only (matches pdq_causal_walkforward.py).
PUBLISHED_C_Q = 0.95
PUBLISHED_RV_Q = 0.90
PUBLISHED_CROSS_SYNC_MIN = 2
MAGNITUDE_HORIZONS_S = (60, 180, 300, 600, 900)
N_PERMUTATIONS = 2000
PERMUTATION_SEED = 20260710
MIN_ELIGIBLE_SIDE = 5

# Test 2: parameter-grid robustness, PBO/DSR-scored.
C_QUANTILES = (0.90, 0.92, 0.95, 0.97, 0.99)
RV_QUANTILES = (0.80, 0.85, 0.90, 0.95)
CROSS_SYNC_MINS = (1, 2, 3)
FIXED_HOLDS_S = (300, 900)
CSCV_N_GROUPS = 6
GRID_IS_MIN_N = 300
GRID_IS_MIN_ACTIVE_DAYS = 15


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


pdq = load_module(BASE_TOOL, "research.tools.pdq_tsi15_decomposition_audit")
lopez = load_module(LOPEZ_TOOL, "research.tools.pdq_supertrend_signal_lopez_validation")
grid_search = load_module(GRID_TOOL, "research.tools.pdq_causal_supertrend_grid_search")


# ---------------------------------------------------------------------------
# Shared dataset / opportunity-mask construction
# ---------------------------------------------------------------------------


def build_dataset() -> pd.DataFrame:
    df = pdq.load_wide()
    df = pdq.add_pdq_features(df)
    return df


def compute_agree(df: pd.DataFrame) -> np.ndarray:
    """Count of roots whose own NImp60 sign matches signC60 (0-3); the raw
    input `add_pdq_features` collapses into the fixed `cross_sync_ge2`
    column. Recomputed here so the cross-root-agreement threshold can vary.
    """
    sign_c = df["signC60"].to_numpy(dtype=np.int8)
    agree = np.zeros(len(df), dtype=np.int8)
    for root in ROOTS:
        agree += (pdq.signed(df[f"nimp60_{root}"]).astype(np.int8) == sign_c).astype(np.int8)
    return agree


def build_opportunity_mask_param(
    df: pd.DataFrame,
    agree: np.ndarray,
    c_q: float,
    rv_q: float,
    cross_sync_min: int,
    warmup_days: int = WARMUP_DAYS,
) -> pd.Series:
    """Causal, expanding, prior-days-only calibration -- a direct
    parameterization of `pdq_causal_walkforward.build_opportunity_mask_
    causal` over its three free numeric knobs. Spread/depth quantile gates
    (0.90 / 0.20) and the stable-book exclusion are held fixed at that
    script's published values throughout.
    """
    days_sorted = sorted(df["day"].unique())
    day_rank = df["day"].map({d: r for r, d in enumerate(days_sorted)}).to_numpy()
    c60_abs = df["C60"].abs()
    rvexp = df["rvexp"]
    spread = df["spread_agg"]
    d5 = df["d5_agg"]
    cross_ok = agree >= cross_sync_min

    mask = pd.Series(False, index=df.index)
    for rank in range(warmup_days, len(days_sorted)):
        prior = day_rank < rank
        current = day_rank == rank
        if not prior.any() or not current.any():
            continue
        q_abs_c = c60_abs[prior].quantile(c_q)
        q_rv = rvexp[prior].quantile(rv_q)
        q_spread = spread[prior].quantile(0.90)
        q_d5 = d5[prior].quantile(0.20)
        med_spread = spread[prior].median()
        med_d5 = d5[prior].median()
        stable_book = (spread <= med_spread) & (d5 >= med_d5)
        day_mask = (
            current
            & c60_abs.gt(q_abs_c).to_numpy()
            & rvexp.gt(q_rv).to_numpy()
            & cross_ok
            & ~stable_book.to_numpy()
            & spread.le(q_spread).to_numpy()
            & d5.ge(q_d5).to_numpy()
            & df["signC60"].ne(0).to_numpy()
        )
        mask |= pd.Series(day_mask, index=df.index)
    return mask.fillna(False)


# ---------------------------------------------------------------------------
# Test 1: onset / forward-magnitude permutation test
# ---------------------------------------------------------------------------


def onset_mask(mask: np.ndarray, day_code: np.ndarray) -> np.ndarray:
    """True only at the first row of each contiguous same-day True run."""
    prev_mask = np.empty_like(mask)
    prev_mask[0] = False
    prev_mask[1:] = mask[:-1]
    prev_day = np.empty_like(day_code)
    prev_day[0] = day_code[0] - 1
    prev_day[1:] = day_code[:-1]
    return mask & (~prev_mask | (day_code != prev_day))


def forward_move_matrix(
    df: pd.DataFrame, horizons_s: tuple[int, ...]
) -> tuple[dict[int, np.ndarray], dict[int, np.ndarray]]:
    """Per-horizon forward max-absolute-displacement and same-day validity."""
    max_h = max(horizons_s)
    entry, future, same = pdq.path_arrays(df, max_horizon_s=max_h)
    moves: dict[int, np.ndarray] = {}
    valid: dict[int, np.ndarray] = {}
    for h in horizons_s:
        step = h // 5
        path = future[:, : step + 1] - entry[:, None]
        with np.errstate(invalid="ignore"):
            moves[h] = np.nanmax(np.abs(path[:, 1:]), axis=1)
        valid[h] = same[:, step]
    return moves, valid


def precompute_day_layout(day_code: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """`day_code` must be sorted ascending (rows already sorted by day, sec).

    Returns (day_start, day_len, compact_rank, local_pos): day_start/day_len
    indexed by compact day position 0..k-1; compact_rank/local_pos per row.
    """
    unique_days, first_idx, counts = np.unique(day_code, return_index=True, return_counts=True)
    day_start = first_idx
    day_len = counts
    compact_rank = np.searchsorted(unique_days, day_code)
    local_pos = np.arange(len(day_code)) - day_start[compact_rank]
    return day_start, day_len, compact_rank, local_pos


def day_local_circular_shift(
    mask: np.ndarray,
    day_start: np.ndarray,
    day_len: np.ndarray,
    compact_rank: np.ndarray,
    local_pos: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    """Cyclically rotate `mask` by an independent random offset within each
    trading day. Preserves each day's own True-count and internal clustering
    exactly; destroys only the alignment with any other same-length series
    (e.g. the forward-move array) indexed the same way.
    """
    shift_per_day = rng.integers(0, np.maximum(day_len, 1))
    shift_for_row = shift_per_day[compact_rank]
    new_local = (local_pos - shift_for_row) % day_len[compact_rank]
    new_global = day_start[compact_rank] + new_local
    return mask[new_global]


def permutation_test_horizon(
    onset: np.ndarray,
    valid: np.ndarray,
    move: np.ndarray,
    day_start: np.ndarray,
    day_len: np.ndarray,
    compact_rank: np.ndarray,
    local_pos: np.ndarray,
    n_perm: int,
    rng: np.random.Generator,
) -> dict[str, float]:
    eligible_onset = onset & valid
    eligible_baseline = valid & ~onset
    n_onset = int(eligible_onset.sum())
    n_baseline = int(eligible_baseline.sum())
    if n_onset < MIN_ELIGIBLE_SIDE or n_baseline < MIN_ELIGIBLE_SIDE:
        return {
            "n_onset": n_onset,
            "n_baseline": n_baseline,
            "observed_gap": float("nan"),
            "null_mean": float("nan"),
            "null_std": float("nan"),
            "p_value": float("nan"),
            "n_valid_permutations": 0,
        }
    observed_gap = float(move[eligible_onset].mean() - move[eligible_baseline].mean())
    null_gaps = np.full(n_perm, np.nan, dtype=float)
    for i in range(n_perm):
        shifted = day_local_circular_shift(onset, day_start, day_len, compact_rank, local_pos, rng)
        elig_s = shifted & valid
        elig_sb = valid & ~shifted
        if elig_s.sum() < MIN_ELIGIBLE_SIDE or elig_sb.sum() < MIN_ELIGIBLE_SIDE:
            continue
        null_gaps[i] = move[elig_s].mean() - move[elig_sb].mean()
    null_gaps = null_gaps[~np.isnan(null_gaps)]
    p_value = float(np.mean(null_gaps >= observed_gap)) if len(null_gaps) else float("nan")
    return {
        "n_onset": n_onset,
        "n_baseline": n_baseline,
        "observed_gap": observed_gap,
        "null_mean": float(np.mean(null_gaps)) if len(null_gaps) else float("nan"),
        "null_std": float(np.std(null_gaps, ddof=1)) if len(null_gaps) > 1 else float("nan"),
        "p_value": p_value,
        "n_valid_permutations": int(len(null_gaps)),
    }


def run_magnitude_test(df: pd.DataFrame) -> dict[str, Any]:
    days_sorted = sorted(df["day"].unique())
    day_rank_full = df["day"].map({d: r for r, d in enumerate(days_sorted)}).to_numpy()
    keep = day_rank_full >= WARMUP_DAYS
    sub = df.loc[keep].reset_index(drop=True)

    mask = build_opportunity_mask_param(
        sub,
        compute_agree(sub),
        PUBLISHED_C_Q,
        PUBLISHED_RV_Q,
        PUBLISHED_CROSS_SYNC_MIN,
        warmup_days=0,  # `sub` already excludes warmup days
    ).to_numpy()

    days_sub_sorted = sorted(sub["day"].unique())
    day_code_full = sub["day"].map({d: r for r, d in enumerate(days_sub_sorted)}).to_numpy()
    onset = onset_mask(mask, day_code_full)
    moves, valids = forward_move_matrix(sub, MAGNITUDE_HORIZONS_S)

    subsets = {
        "full": np.ones(len(sub), dtype=bool),
        "is_only": (sub["day"].to_numpy() < IS_OOS_CUTOFF_DAY),
        "oos_only": (sub["day"].to_numpy() >= IS_OOS_CUTOFF_DAY),
    }
    rng = np.random.default_rng(PERMUTATION_SEED)
    result: dict[str, Any] = {
        "opportunity_definition": {
            "c_quantile": PUBLISHED_C_Q,
            "rv_quantile": PUBLISHED_RV_Q,
            "cross_sync_min": PUBLISHED_CROSS_SYNC_MIN,
            "warmup_days_excluded": WARMUP_DAYS,
        },
        "n_permutations": N_PERMUTATIONS,
        "n_onset_events_full": int(onset.sum()),
        "calendar_days_excl_warmup": len(days_sub_sorted),
    }
    for subset_name, subset_rows in subsets.items():
        day_start, day_len, compact_rank, local_pos = precompute_day_layout(day_code_full[subset_rows])
        per_horizon = {}
        for h in MAGNITUDE_HORIZONS_S:
            per_horizon[str(h)] = permutation_test_horizon(
                onset[subset_rows],
                valids[h][subset_rows],
                moves[h][subset_rows],
                day_start,
                day_len,
                compact_rank,
                local_pos,
                N_PERMUTATIONS,
                rng,
            )
        result[subset_name] = per_horizon
    return result


# ---------------------------------------------------------------------------
# Test 2: parameter-grid robustness, scored via CSCV/PBO + DSR
# ---------------------------------------------------------------------------


def _empty_trades_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=["day", "gross_pnl", "hold_s"])


def exit_at_horizon_or_day_end(
    future: np.ndarray, same: np.ndarray, idx: np.ndarray, step: int
) -> tuple[np.ndarray, np.ndarray]:
    """Exit price/hold at min(horizon, day-end); `same[:, 0]` is always True
    so at least the entry price itself is always a valid fallback.
    """
    sub_same = same[idx, : step + 1]
    sub_future = future[idx, : step + 1]
    rev = sub_same[:, ::-1]
    last_true_rev = rev.argmax(axis=1)
    last_true_idx = step - last_true_rev
    exit_price = sub_future[np.arange(len(idx)), last_true_idx]
    hold_actual = last_true_idx * 5.0
    return exit_price, hold_actual


def run_parameter_grid(df: pd.DataFrame, agree: np.ndarray, all_days: list[str]) -> dict[int, dict[str, Any]]:
    entry_mid, future, same = pdq.path_arrays(df, max_horizon_s=max(FIXED_HOLDS_S))
    direction_full = df["signC60"].to_numpy(dtype=np.int8)
    day_full = df["day"].to_numpy()

    combos = list(itertools.product(C_QUANTILES, RV_QUANTILES, CROSS_SYNC_MINS))
    per_hold: dict[int, dict[str, Any]] = {h: {"rows": [], "daily_cols": {}} for h in FIXED_HOLDS_S}

    for c_q, rv_q, cross_min in combos:
        mask = build_opportunity_mask_param(df, agree, c_q, rv_q, cross_min).to_numpy()
        emask = mask & (direction_full != 0)
        idx_all = np.flatnonzero(emask)
        key = (c_q, rv_q, cross_min)
        for hold_s in FIXED_HOLDS_S:
            step = hold_s // 5
            if len(idx_all) == 0:
                trades = _empty_trades_frame()
            else:
                exit_price, hold_actual = exit_at_horizon_or_day_end(future, same, idx_all, step)
                gross = direction_full[idx_all].astype(float) * (exit_price - entry_mid[idx_all])
                trades = pd.DataFrame({"day": day_full[idx_all], "gross_pnl": gross, "hold_s": hold_actual})
            is_mask = trades["day"].to_numpy() < IS_OOS_CUTOFF_DAY if len(trades) else np.array([], dtype=bool)
            is_stats = grid_search.summarize_split(trades, is_mask)
            per_hold[hold_s]["rows"].append(
                {
                    "c_quantile": c_q,
                    "rv_quantile": rv_q,
                    "cross_sync_min": cross_min,
                    **{f"is_{k}": v for k, v in is_stats.items()},
                }
            )
            per_hold[hold_s]["daily_cols"][key] = lopez.daily_net_series(trades, all_days)
    return per_hold


def rank_and_select(
    grid_df: pd.DataFrame, *, min_n: int, min_active_days: int
) -> tuple[pd.DataFrame, pd.Series | None]:
    eligible = grid_df[(grid_df["is_n"] >= min_n) & (grid_df["is_active_days"] >= min_active_days)].copy()
    if eligible.empty:
        return eligible, None
    ranked = eligible.sort_values("is_net_mean_cost4", ascending=False).reset_index(drop=True)
    return ranked, ranked.iloc[0]


def summarize_grid(per_hold: dict[int, dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for hold_s, payload in per_hold.items():
        grid_df = pd.DataFrame(payload["rows"])
        combos = list(payload["daily_cols"].keys())
        matrix = np.column_stack([payload["daily_cols"][c].to_numpy() for c in combos])
        ranked, winner = rank_and_select(grid_df, min_n=GRID_IS_MIN_N, min_active_days=GRID_IS_MIN_ACTIVE_DAYS)
        pbo = lopez.cscv_pbo(matrix, CSCV_N_GROUPS)
        trial_sharpes = lopez._period_sharpe(matrix)

        winner_dsr: dict[str, Any] = {"dsr": float("nan")}
        if winner is not None:
            key = (winner["c_quantile"], winner["rv_quantile"], int(winner["cross_sync_min"]))
            winner_dsr = lopez.deflated_sharpe_ratio(payload["daily_cols"][key].to_numpy(), trial_sharpes)

        published_key = (PUBLISHED_C_Q, PUBLISHED_RV_Q, PUBLISHED_CROSS_SYNC_MIN)
        published_dsr = lopez.deflated_sharpe_ratio(payload["daily_cols"][published_key].to_numpy(), trial_sharpes)

        result[str(hold_s)] = {
            "grid_df": grid_df,
            "eligible_count": int(len(ranked)),
            "winner": None if winner is None else winner.to_dict(),
            "pbo_cscv": pbo,
            "winner_dsr": winner_dsr,
            "published_cell_dsr": published_dsr,
        }
    return result


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = build_dataset()
    agree = compute_agree(df)
    all_days = sorted(df["day"].unique())[WARMUP_DAYS:]

    print("Running Test 1 (magnitude / permission-layer permutation test)...")
    test1 = run_magnitude_test(df)

    print("Running Test 2 (parameter-grid PBO/CSCV + DSR, 60 combos x 2 holds)...")
    per_hold = run_parameter_grid(df, agree, all_days)
    test2 = summarize_grid(per_hold)

    for hold_s, payload in test2.items():
        payload["grid_df"].to_csv(OUT_DIR / f"grid_results_hold{hold_s}s.csv", index=False)

    metadata = {
        "warmup_days_excluded": WARMUP_DAYS,
        "calendar_days_total": int(len(sorted(df["day"].unique()))),
        "test1_magnitude_permission_layer": test1,
        "test2_parameter_grid": {
            hold_s: {k: v for k, v in payload.items() if k != "grid_df"} for hold_s, payload in test2.items()
        },
        "cscv_n_groups": CSCV_N_GROUPS,
        "grid_size": len(C_QUANTILES) * len(RV_QUANTILES) * len(CROSS_SYNC_MINS),
        "known_limitations": (
            "Trade events are not deduplicated for concurrent/overlapping positions, matching "
            "every prior script in this program (see RESEARCH_SUMMARY_FROM_START.md section 17 "
            "item 5). Test 2's trial_sharpes (DSR null benchmark) come from 60 grid cells that "
            "are not independent (adjacent quantile thresholds produce highly correlated trials), "
            "so N=60 is a conservative upper bound on effective independent trials, not the true "
            "count -- same caveat as pdq_supertrend_signal_lopez_validation.py."
        ),
    }
    (OUT_DIR / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True, default=str))
    print(json.dumps(metadata, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
