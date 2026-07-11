"""Supertrend parameter-space grid search, causal entries, NO holding-time cap.

The prior grid search (`pdq_causal_supertrend_grid_search.py`) swept
timeframe x atr_period x factor x max_hold_s (1,536 combinations) and found
0% of them IS-positive. That grid still capped every exit at a fixed
max_hold_s (300/600/900/1800s). This script removes the time cap from the
grid entirely -- consistent with the earlier `pdq_causal_uncapped_exit.py`
finding that the 900s cap was doing useful risk-management work, not
suppressing edge -- and asks the same question with the cap gone: exits fire
only on a confirmed Supertrend flip or a liquidity-recovery signal, with the
sole backstop being the end of the event's own trading day (a real
data-availability bound, not a strategy parameter).

Removing max_hold_s drops one full grid dimension, so atr_period and factor
are widened to keep the grid at >=1000 combinations: 6 timeframes x 13
atr_period x 13 factor = 1,014. The liquidity overlay
(min_depth_ratio=1.3/max_spread_ratio=0.5/min_zlogl_delta=0.0/
confirmations=3) stays fixed at the already-published values, same as the
capped grid search, so this sweep isolates the Supertrend-specific
parameters.

Same overfitting control as the capped grid search: ranked on March-May
in-sample, June revealed only for the top 20 IS-ranked candidates.
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
EXIT_TOOL = ROOT / "research/tools/pdq_supertrend_exit_search.py"
UNCAPPED_TOOL = ROOT / "research/tools/pdq_causal_uncapped_exit.py"
GRID_TOOL = ROOT / "research/tools/pdq_causal_supertrend_grid_search.py"
CAUSAL_EVENTS_PATH = ROOT / "outputs/liquidity_score/pdq_causal_walkforward/causal_event_level_paths.csv.gz"
OUT_DIR = ROOT / "outputs/liquidity_score/pdq_causal_supertrend_grid_search_uncapped"

MAX_EXECUTION_LAG_S = 5
MAX_OBSERVATION_GAP_S = 5
COSTS = (2.0, 4.0, 6.0)
IS_OOS_CUTOFF_DAY = "2026-06-01"  # IS = Mar-May causal entries; OOS = June, revealed only for top-K IS picks

TIMEFRAMES = ("1m", "2m", "3m", "5m", "10m", "15m")
ATR_PERIODS = (3, 5, 7, 8, 10, 13, 17, 21, 26, 34, 42, 50, 60)
FACTORS = (0.6, 0.8, 1.0, 1.2, 1.5, 1.8, 2.1, 2.5, 3.0, 3.5, 4.5, 6.0, 8.0)

FIXED_LIQUIDITY_PARAMS = {
    "min_depth_ratio": 1.3,
    "max_spread_ratio": 0.5,
    "min_zlogl_delta": 0.0,
    "confirmations": 3,
}

IS_MIN_N = 300
IS_MIN_ACTIVE_DAYS = 15
TOP_K_FOR_OOS_REVEAL = 20


def load_module(path: Path, name: str) -> Any:
    """Load `path` under its real dotted package name, reusing any existing load.

    See `pdq_causal_walkforward.load_module` for why this must be the
    canonical `research.tools.<module>` name.
    """
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
exit_search = load_module(EXIT_TOOL, "research.tools.pdq_supertrend_exit_search")
uncapped = load_module(UNCAPPED_TOOL, "research.tools.pdq_causal_uncapped_exit")
grid_search = load_module(GRID_TOOL, "research.tools.pdq_causal_supertrend_grid_search")


class UncappedGridEvaluator:
    """Causal entries; exit only via Supertrend flip or liquidity signal, no time cap."""

    def __init__(self) -> None:
        self.secbar = pdq.add_pdq_features(pdq.load_wide())
        events = pd.read_csv(CAUSAL_EVENTS_PATH)
        events = events[events["label"].eq("TSI15_align")].copy()
        self.events = events.sort_values(["sec", "day"], kind="mergesort").reset_index(drop=True)

        self.seconds = self.secbar["sec"].to_numpy(dtype=np.int64)
        self.mid = self.secbar["mid_agg"].to_numpy(dtype=float)
        self.depth = self.secbar["d5_agg"].to_numpy(dtype=float)
        self.spread = self.secbar["spread_agg"].to_numpy(dtype=float)
        self.zlogl = self.secbar["zlogL_min"].to_numpy(dtype=float)

        day_end_s = self.secbar.groupby("day")["sec"].max()
        self.deadlines = self.events["day"].map(day_end_s).to_numpy(dtype=np.int64)

        self.entry_s = self.events["sec"].to_numpy(dtype=np.int64)
        self.position_dirs = self.events["direction"].to_numpy(dtype=np.int8)
        self.entry_indices = np.searchsorted(self.seconds, self.entry_s, side="left")
        if np.any(self.entry_indices >= len(self.seconds)) or not np.array_equal(
            self.seconds[self.entry_indices], self.entry_s
        ):
            raise RuntimeError("Entry timestamp is missing from the secbar")
        self.entry_mid = self.events["entry_mid"].to_numpy(dtype=float)

        self.bars = self._build_bars()

        self.liq_exit = uncapped.liquidity_exit_times_deadline(
            self.seconds,
            self.depth,
            self.spread,
            self.zlogl,
            self.entry_indices,
            self.deadlines,
            min_depth_ratio=FIXED_LIQUIDITY_PARAMS["min_depth_ratio"],
            max_spread_ratio=FIXED_LIQUIDITY_PARAMS["max_spread_ratio"],
            min_zlogl_delta=FIXED_LIQUIDITY_PARAMS["min_zlogl_delta"],
            confirmations=FIXED_LIQUIDITY_PARAMS["confirmations"],
            max_observation_gap_s=MAX_OBSERVATION_GAP_S,
        )
        self.liq_valid = (self.liq_exit >= self.entry_s) & (self.liq_exit <= self.deadlines)

    def _build_bars(self) -> dict[str, dict[str, np.ndarray]]:
        bars_by_timeframe: dict[str, dict[str, np.ndarray]] = {}
        base = self.secbar[["sec", "mid_agg"]]
        for timeframe, timeframe_s in exit_search.TIMEFRAMES.items():
            bars = exit_search.build_completed_bars(base, timeframe_s=timeframe_s)
            bars_by_timeframe[timeframe] = {
                "bar_end_s": bars["bar_end_s"].to_numpy(dtype=np.int64),
                "high": bars["high"].to_numpy(dtype=float),
                "low": bars["low"].to_numpy(dtype=float),
                "close": bars["close"].to_numpy(dtype=float),
            }
        return bars_by_timeframe

    def paths(self, timeframe: str, atr_period: int, factor: float) -> pd.DataFrame:
        bars = self.bars[timeframe]
        states = exit_search.compute_supertrend_direction(
            bars["high"], bars["low"], bars["close"], atr_period=atr_period, factor=factor
        )
        st_exit = uncapped.armed_flip_exit_times_deadline(
            bars["bar_end_s"],
            states,
            self.entry_s,
            self.position_dirs,
            self.deadlines,
            execution_seconds=self.seconds,
            max_execution_lag_s=MAX_EXECUTION_LAG_S,
        )
        st_valid = (st_exit >= self.entry_s) & (st_exit <= self.deadlines)

        exit_s = self.deadlines.copy()
        reason = np.full(len(self.events), "day_end", dtype=object)
        use_st = st_valid & ((reason == "day_end") | (st_exit < exit_s))
        exit_s[use_st] = st_exit[use_st]
        reason[use_st] = "supertrend"
        use_liq = self.liq_valid & ((reason == "day_end") | (self.liq_exit < exit_s))
        exit_s[use_liq] = self.liq_exit[use_liq]
        reason[use_liq] = "liquidity"

        exit_indices = exit_search.execution_indices_for_times(self.seconds, exit_s, max_lag_s=MAX_EXECUTION_LAG_S)
        complete = exit_indices >= 0
        execution_s = np.full(len(self.events), -1, dtype=np.int64)
        execution_s[complete] = self.seconds[exit_indices[complete]]
        exit_mid = np.full(len(self.events), np.nan, dtype=float)
        exit_mid[complete] = self.mid[exit_indices[complete]]
        gross = self.position_dirs.astype(float) * (exit_mid - self.entry_mid)

        return pd.DataFrame(
            {
                "day": self.events["day"],
                "entry_s": self.entry_s,
                "exit_s": exit_s,
                "execution_s": execution_s,
                "hold_s": np.where(complete, execution_s - self.entry_s, np.nan),
                "reason": reason,
                "gross_pnl": gross,
            }
        )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    evaluator = UncappedGridEvaluator()
    day = evaluator.events["day"].to_numpy()
    is_mask = day < IS_OOS_CUTOFF_DAY
    oos_mask = ~is_mask

    combos = list(itertools.product(TIMEFRAMES, ATR_PERIODS, FACTORS))
    rows = []
    for timeframe, atr_period, factor in combos:
        paths = evaluator.paths(timeframe, atr_period, factor)
        is_summary = grid_search.summarize_split(paths, is_mask)
        rows.append(
            {
                "timeframe": timeframe,
                "atr_period": atr_period,
                "factor": factor,
                **{f"is_{key}": value for key, value in is_summary.items()},
            }
        )
    grid_df = pd.DataFrame(rows)

    eligible = grid_df[(grid_df["is_n"] >= IS_MIN_N) & (grid_df["is_active_days"] >= IS_MIN_ACTIVE_DAYS)].copy()
    ranked = eligible.sort_values("is_net_mean_cost4", ascending=False).reset_index(drop=True)

    top = ranked.head(TOP_K_FOR_OOS_REVEAL).copy()
    oos_rows = []
    for _, row in top.iterrows():
        paths = evaluator.paths(row["timeframe"], int(row["atr_period"]), float(row["factor"]))
        oos_summary = grid_search.summarize_split(paths, oos_mask)
        oos_rows.append({f"oos_{key}": value for key, value in oos_summary.items()})
    validated = pd.concat([top.reset_index(drop=True), pd.DataFrame(oos_rows)], axis=1)

    published_paths = evaluator.paths("1m", 3, 2.1)
    published_is = grid_search.summarize_split(published_paths, is_mask)
    published_oos = grid_search.summarize_split(published_paths, oos_mask)

    metadata = {
        "theoretical_grid_size": len(combos),
        "exit_rule": "Supertrend flip or liquidity recovery only; backstop is day-end, not a fixed hold",
        "is_period": "2026-03-03..2026-05-31 (causal entries only)",
        "oos_period": "2026-06-01..2026-06-13 (genuinely held out of ranking)",
        "is_eligibility_floor": {"min_n": IS_MIN_N, "min_active_days": IS_MIN_ACTIVE_DAYS},
        "is_eligible_count": int(len(eligible)),
        "is_eligible_positive_cost4_share": (
            float((eligible["is_net_mean_cost4"] > 0).mean()) if len(eligible) else float("nan")
        ),
        "is_net_cost4_median": float(eligible["is_net_mean_cost4"].median()) if len(eligible) else float("nan"),
        "is_net_cost4_max": float(eligible["is_net_mean_cost4"].max()) if len(eligible) else float("nan"),
        "top_k_revealed": TOP_K_FOR_OOS_REVEAL,
        "published_gene_is": published_is,
        "published_gene_oos": published_oos,
        "fixed_liquidity_params": FIXED_LIQUIDITY_PARAMS,
        "known_limitation": (
            "same multiple-comparisons control as the capped grid search: OOS fold is June only "
            "(~12 active days) and June is already the sole positive month under every other "
            "exit-parameter variant tested this session -- a positive OOS reveal here is consistent "
            "with that known regime concentration, not independent confirmation of a new edge"
        ),
    }

    grid_df.to_csv(OUT_DIR / "grid_is_results.csv", index=False)
    validated.to_csv(OUT_DIR / "top20_oos_validation.csv", index=False)
    (OUT_DIR / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))

    print(json.dumps(metadata, indent=2, sort_keys=True))
    print("\nTop 20 IS-ranked, OOS revealed:")
    print(
        validated[
            ["timeframe", "atr_period", "factor", "is_n", "is_net_mean_cost4", "oos_n", "oos_net_mean_cost4"]
        ].to_string(index=False)
    )


if __name__ == "__main__":
    main()
