"""Causal entries, fixed exit signals, NO time-based forced exit.

`pdq_causal_walkforward.py` re-scored the fixed GA-winning exit policy
(Supertrend 1m/ATR3/factor2.1 first-of-two with a liquidity overlay) against
a no-lookahead entry population, but kept the original max_hold_s=900s
forced exit. Roughly half of all exits in that run hit the 900s wall
(`max_hold_exit_rate` 47-60% per month) rather than a Supertrend flip or
liquidity signal, so the time cap dominates the result.

This script removes the time cap entirely: an event now exits only on a
confirmed Supertrend flip or a liquidity-recovery signal, with the sole
backstop being the end of its own trading day (a real data-availability
bound, not a strategy parameter). It reuses the causal entry population
already exported by `pdq_causal_walkforward.py` unmodified, and reuses the
Supertrend-state / bar-building / execution-mapping machinery from
`pdq_supertrend_exit_search.py` unmodified. The only new code is a
per-event-deadline variant of the two exit-time scanners (the originals
take one shared `max_hold_s` scalar; this needs a per-event day-end
deadline instead).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from numba import njit

ROOT = Path(__file__).resolve().parents[2]
BASE_TOOL = ROOT / "research/tools/pdq_tsi15_decomposition_audit.py"
EXIT_TOOL = ROOT / "research/tools/pdq_supertrend_exit_search.py"
CAUSAL_EVENTS_PATH = ROOT / "outputs/liquidity_score/pdq_causal_walkforward/causal_event_level_paths.csv.gz"
OUT_DIR = ROOT / "outputs/liquidity_score/pdq_causal_uncapped_exit"

MAX_EXECUTION_LAG_S = 5
MAX_OBSERVATION_GAP_S = 5
COSTS = (2.0, 4.0, 6.0)

FIXED_EXIT_PARAMS = {
    "timeframe": "1m",
    "atr_period": 3,
    "factor": 2.1,
    "min_depth_ratio": 1.3,
    "max_spread_ratio": 0.5,
    "min_zlogl_delta": 0.0,
    "confirmations": 3,
}


def load_module(path: Path, name: str) -> Any:
    """Load `path` under its real dotted package name, reusing any existing load.

    See `pdq_causal_walkforward.load_module` for why the name must be the
    canonical `research.tools.<module>` path rather than an arbitrary
    synthetic one: it keeps this file's dataclass/numba-cache behavior
    identical whether it's loaded standalone or alongside the normal
    `import research.tools.pdq_supertrend_exit_search` the existing pytest
    suite uses.
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


@njit(cache=True)
def _armed_flip_exit_times_deadline_numba(
    bar_end_s: np.ndarray,
    states: np.ndarray,
    entry_s: np.ndarray,
    position_dirs: np.ndarray,
    deadlines: np.ndarray,
    execution_seconds: np.ndarray,
    max_execution_lag_s: int,
) -> np.ndarray:
    exits = np.full(len(entry_s), -1, dtype=np.int64)
    for event_index in range(len(entry_s)):
        start_s = entry_s[event_index]
        position_dir = position_dirs[event_index]
        deadline = deadlines[event_index]
        bar_index = np.searchsorted(bar_end_s, start_s, side="right") - 1
        armed = bar_index >= 0 and states[bar_index] == position_dir
        future_index = max(0, bar_index + 1)
        while future_index < len(states) and bar_end_s[future_index] <= deadline:
            state = states[future_index]
            if state == position_dir:
                armed = True
            elif armed and state == -position_dir:
                signal_s = bar_end_s[future_index]
                execution_index = np.searchsorted(execution_seconds, signal_s)
                executable = (
                    execution_index < len(execution_seconds)
                    and execution_seconds[execution_index] - signal_s <= max_execution_lag_s
                )
                if executable:
                    exits[event_index] = signal_s
                    break
                armed = False
            future_index += 1
    return exits


def armed_flip_exit_times_deadline(
    bar_end_s: np.ndarray,
    states: np.ndarray,
    entry_s: np.ndarray,
    position_dirs: np.ndarray,
    deadlines: np.ndarray,
    *,
    execution_seconds: np.ndarray,
    max_execution_lag_s: int = MAX_EXECUTION_LAG_S,
) -> np.ndarray:
    clean_states = np.where(np.isfinite(states), states, 0).astype(np.int8)
    return _armed_flip_exit_times_deadline_numba(
        np.asarray(bar_end_s, dtype=np.int64),
        clean_states,
        np.asarray(entry_s, dtype=np.int64),
        np.asarray(position_dirs, dtype=np.int8),
        np.asarray(deadlines, dtype=np.int64),
        np.asarray(execution_seconds, dtype=np.int64),
        max_execution_lag_s,
    )


@njit(cache=True)
def _liquidity_exit_times_deadline_numba(
    seconds: np.ndarray,
    depth: np.ndarray,
    spread: np.ndarray,
    zlogl: np.ndarray,
    entry_indices: np.ndarray,
    deadlines: np.ndarray,
    min_depth_ratio: float,
    max_spread_ratio: float,
    min_zlogl_delta: float,
    confirmations: int,
    max_observation_gap_s: int,
) -> np.ndarray:
    exits = np.full(len(entry_indices), -1, dtype=np.int64)
    for event_index in range(len(entry_indices)):
        start_index = entry_indices[event_index]
        deadline = deadlines[event_index]
        base_depth = depth[start_index]
        base_spread = spread[start_index]
        base_zlogl = zlogl[start_index]
        if (
            not np.isfinite(base_depth)
            or not np.isfinite(base_spread)
            or not np.isfinite(base_zlogl)
            or base_depth <= 0
            or base_spread <= 0
        ):
            continue
        consecutive = 0
        index = start_index + 1
        while index < len(seconds) and seconds[index] <= deadline:
            if seconds[index] - seconds[index - 1] > max_observation_gap_s:
                consecutive = 0
            recovered = (
                np.isfinite(depth[index])
                and np.isfinite(spread[index])
                and np.isfinite(zlogl[index])
                and depth[index] / base_depth >= min_depth_ratio
                and spread[index] / base_spread <= max_spread_ratio
                and zlogl[index] - base_zlogl >= min_zlogl_delta
            )
            if recovered:
                consecutive += 1
                if consecutive >= confirmations:
                    exits[event_index] = seconds[index]
                    break
            else:
                consecutive = 0
            index += 1
    return exits


def liquidity_exit_times_deadline(
    seconds: np.ndarray,
    depth: np.ndarray,
    spread: np.ndarray,
    zlogl: np.ndarray,
    entry_indices: np.ndarray,
    deadlines: np.ndarray,
    *,
    min_depth_ratio: float,
    max_spread_ratio: float,
    min_zlogl_delta: float,
    confirmations: int,
    max_observation_gap_s: int,
) -> np.ndarray:
    return _liquidity_exit_times_deadline_numba(
        np.asarray(seconds, dtype=np.int64),
        np.asarray(depth, dtype=float),
        np.asarray(spread, dtype=float),
        np.asarray(zlogl, dtype=float),
        np.asarray(entry_indices, dtype=np.int64),
        np.asarray(deadlines, dtype=np.int64),
        min_depth_ratio,
        max_spread_ratio,
        min_zlogl_delta,
        confirmations,
        max_observation_gap_s,
    )


class UncappedExitEvaluator:
    """Same causal entries; exit only via Supertrend flip or liquidity signal."""

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

    def _build_bars(self) -> dict[str, np.ndarray]:
        base = self.secbar[["sec", "mid_agg"]]
        bars = exit_search.build_completed_bars(base, timeframe_s=60)
        return {
            "bar_end_s": bars["bar_end_s"].to_numpy(dtype=np.int64),
            "high": bars["high"].to_numpy(dtype=float),
            "low": bars["low"].to_numpy(dtype=float),
            "close": bars["close"].to_numpy(dtype=float),
        }

    def paths(self) -> pd.DataFrame:
        states = exit_search.compute_supertrend_direction(
            self.bars["high"],
            self.bars["low"],
            self.bars["close"],
            atr_period=FIXED_EXIT_PARAMS["atr_period"],
            factor=FIXED_EXIT_PARAMS["factor"],
        )
        st_exit = armed_flip_exit_times_deadline(
            self.bars["bar_end_s"],
            states,
            self.entry_s,
            self.position_dirs,
            self.deadlines,
            execution_seconds=self.seconds,
            max_execution_lag_s=MAX_EXECUTION_LAG_S,
        )
        liq_exit = liquidity_exit_times_deadline(
            self.seconds,
            self.depth,
            self.spread,
            self.zlogl,
            self.entry_indices,
            self.deadlines,
            min_depth_ratio=FIXED_EXIT_PARAMS["min_depth_ratio"],
            max_spread_ratio=FIXED_EXIT_PARAMS["max_spread_ratio"],
            min_zlogl_delta=FIXED_EXIT_PARAMS["min_zlogl_delta"],
            confirmations=FIXED_EXIT_PARAMS["confirmations"],
            max_observation_gap_s=MAX_OBSERVATION_GAP_S,
        )

        st_valid = (st_exit >= self.entry_s) & (st_exit <= self.deadlines)
        liq_valid = (liq_exit >= self.entry_s) & (liq_exit <= self.deadlines)

        exit_s = self.deadlines.copy()
        reason = np.full(len(self.events), "day_end", dtype=object)
        use_st = st_valid & ((reason == "day_end") | (st_exit < exit_s))
        exit_s[use_st] = st_exit[use_st]
        reason[use_st] = "supertrend"
        use_liq = liq_valid & ((reason == "day_end") | (liq_exit < exit_s))
        exit_s[use_liq] = liq_exit[use_liq]
        reason[use_liq] = "liquidity"

        exit_indices = exit_search.execution_indices_for_times(
            self.seconds,
            exit_s,
            max_lag_s=MAX_EXECUTION_LAG_S,
        )
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


def monthly_report(paths: pd.DataFrame) -> pd.DataFrame:
    complete = paths.dropna(subset=["gross_pnl"]).assign(month=lambda frame: frame["day"].str.slice(0, 7))
    monthly = (
        complete.groupby("month", sort=True)
        .agg(
            n=("gross_pnl", "count"),
            active_days=("day", "nunique"),
            gross_mean=("gross_pnl", "mean"),
            hit_rate=("gross_pnl", lambda s: float((s > 0).mean())),
            avg_hold_s=("hold_s", "mean"),
            supertrend_exit_rate=("reason", lambda s: float((s == "supertrend").mean())),
            liquidity_exit_rate=("reason", lambda s: float((s == "liquidity").mean())),
            day_end_exit_rate=("reason", lambda s: float((s == "day_end").mean())),
        )
        .reset_index()
    )
    for cost in COSTS:
        monthly[f"net_mean_cost{int(cost)}"] = monthly["gross_mean"] - cost
    return monthly


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    evaluator = UncappedExitEvaluator()
    paths = evaluator.paths()
    monthly = monthly_report(paths)

    complete = paths.dropna(subset=["gross_pnl"])
    overall = {
        "n": int(len(complete)),
        "active_days": int(complete["day"].nunique()),
        "gross_mean": float(complete["gross_pnl"].mean()) if len(complete) else float("nan"),
        "avg_hold_s": float(complete["hold_s"].mean()) if len(complete) else float("nan"),
        "max_hold_s_observed": float(complete["hold_s"].max()) if len(complete) else float("nan"),
    }
    for cost in COSTS:
        overall[f"net_mean_cost{int(cost)}"] = overall["gross_mean"] - cost if len(complete) else float("nan")

    metadata = {
        "source_events": str(CAUSAL_EVENTS_PATH.relative_to(ROOT)),
        "exit_rule": "Supertrend flip or liquidity recovery only; backstop is day-end, not a fixed hold",
        "fixed_exit_params": FIXED_EXIT_PARAMS,
        "overall_cost4": overall,
    }

    paths.to_csv(OUT_DIR / "uncapped_event_paths.csv.gz", index=False)
    monthly.to_csv(OUT_DIR / "uncapped_monthly_cost4.csv", index=False)
    (OUT_DIR / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))

    print(json.dumps(metadata, indent=2, sort_keys=True))
    print("\nUncapped monthly net (cost4):")
    print(monthly.to_string(index=False))


if __name__ == "__main__":
    main()
