"""Exit-only search for the fixed PDQ_cont q95 + TSI15 entry set."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from numba import njit

ROOT = Path(__file__).resolve().parents[2]
BASE_TOOL = ROOT / "research/tools/pdq_tsi15_decomposition_audit.py"
ENTRY_PATH = ROOT / "outputs/liquidity_score/pdq_tsi15_decomposition_audit/event_level_proxy_paths.csv.gz"
OUT_DIR = ROOT / "outputs/liquidity_score/pdq_supertrend_exit_search"

TIMEFRAMES = {"1m": 60, "2m": 120, "3m": 180, "5m": 300, "10m": 600, "15m": 900}
ATR_VALUES = tuple(range(3, 61))
FACTOR_VALUES = tuple(round(value, 1) for value in np.arange(0.5, 8.01, 0.1))
MAX_HOLDS = (300, 600, 900, 1800)
EXIT_MODES = ("supertrend", "liquidity", "first")
DEPTH_RATIOS = (1.0, 1.1, 1.2, 1.3, 1.5, 2.0)
SPREAD_RATIOS = (1.0, 0.9, 0.8, 0.7, 0.5)
ZLOGL_DELTAS = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0)
CONFIRMATIONS = (1, 3, 5, 10, 30)
COSTS = (2.0, 4.0, 6.0)
MAX_EXECUTION_LAG_S = 5
MAX_OBSERVATION_GAP_S = 5
SESSION_GAP_S = 300


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pdq = load_module(BASE_TOOL, "pdq_exit_base_tool")


@njit(cache=True)
def _same_band_value_numba(left: float, right: float) -> bool:
    return left == right


def same_band_value(left: float, right: float) -> bool:
    """Match Pine's exact previous-band state identity."""
    return bool(_same_band_value_numba(left, right))


@njit(cache=True)
def _compute_supertrend_direction_numba(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    atr_period: int,
    factor: float,
) -> np.ndarray:
    size = len(close)
    tr = np.empty(size, dtype=float)
    if size:
        tr[0] = high[0] - low[0]
    for index in range(1, size):
        tr[index] = max(
            high[index] - low[index],
            abs(high[index] - close[index - 1]),
            abs(low[index] - close[index - 1]),
        )

    atr = np.full(size, np.nan, dtype=float)
    if size >= atr_period:
        atr[atr_period - 1] = float(np.mean(tr[:atr_period]))
        for index in range(atr_period, size):
            atr[index] = (atr[index - 1] * (atr_period - 1) + tr[index]) / atr_period

    hl2 = (high + low) / 2.0
    basic_upper = hl2 + factor * atr
    basic_lower = hl2 - factor * atr
    final_upper = np.full(size, np.nan, dtype=float)
    final_lower = np.full(size, np.nan, dtype=float)
    supertrend = np.full(size, np.nan, dtype=float)
    pine_direction = np.full(size, np.nan, dtype=float)

    for index in range(size):
        if not np.isfinite(atr[index]):
            continue
        if index == 0 or not np.isfinite(final_upper[index - 1]):
            final_upper[index] = basic_upper[index]
            final_lower[index] = basic_lower[index]
            pine_direction[index] = 1.0
            supertrend[index] = final_upper[index]
            continue

        final_upper[index] = (
            basic_upper[index]
            if basic_upper[index] < final_upper[index - 1] or close[index - 1] > final_upper[index - 1]
            else final_upper[index - 1]
        )
        final_lower[index] = (
            basic_lower[index]
            if basic_lower[index] > final_lower[index - 1] or close[index - 1] < final_lower[index - 1]
            else final_lower[index - 1]
        )
        if _same_band_value_numba(supertrend[index - 1], final_upper[index - 1]):
            pine_direction[index] = -1.0 if close[index] > final_upper[index] else 1.0
        else:
            pine_direction[index] = 1.0 if close[index] < final_lower[index] else -1.0
        supertrend[index] = final_lower[index] if pine_direction[index] < 0 else final_upper[index]

    direction = np.full(size, np.nan, dtype=float)
    for index in range(size):
        if pine_direction[index] < 0:
            direction[index] = 1.0
        elif pine_direction[index] > 0:
            direction[index] = -1.0
    return direction


def compute_supertrend_direction(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    *,
    atr_period: int,
    factor: float,
) -> np.ndarray:
    """Return TradingView-compatible direction: +1 uptrend, -1 downtrend."""
    high_arr = np.asarray(high, dtype=float)
    low_arr = np.asarray(low, dtype=float)
    close_arr = np.asarray(close, dtype=float)
    if len(high_arr) != len(close_arr) or len(low_arr) != len(close_arr):
        raise ValueError("high, low, and close must have equal lengths")
    if atr_period < 1 or factor <= 0:
        raise ValueError("atr_period and factor must be positive")
    return _compute_supertrend_direction_numba(
        high_arr,
        low_arr,
        close_arr,
        atr_period,
        factor,
    )


@njit(cache=True)
def _armed_flip_exit_times_numba(
    bar_end_s: np.ndarray,
    states: np.ndarray,
    entry_s: np.ndarray,
    position_dirs: np.ndarray,
    max_hold_s: int,
    execution_seconds: np.ndarray,
    max_execution_lag_s: int,
) -> np.ndarray:
    exits = np.full(len(entry_s), -1, dtype=np.int64)
    for event_index in range(len(entry_s)):
        start_s = entry_s[event_index]
        position_dir = position_dirs[event_index]
        bar_index = np.searchsorted(bar_end_s, start_s, side="right") - 1
        armed = bar_index >= 0 and states[bar_index] == position_dir
        deadline = start_s + max_hold_s
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


def armed_flip_exit_times(
    bar_end_s: np.ndarray,
    states: np.ndarray,
    entry_s: np.ndarray,
    position_dirs: np.ndarray,
    *,
    max_hold_s: int,
    execution_seconds: np.ndarray | None = None,
    max_execution_lag_s: int = MAX_EXECUTION_LAG_S,
) -> np.ndarray:
    """Find confirmed bar-end flip times without using an incomplete bar."""
    if len(bar_end_s) != len(states):
        raise ValueError("bar_end_s and states must have equal lengths")
    if len(entry_s) != len(position_dirs):
        raise ValueError("entry_s and position_dirs must have equal lengths")

    clean_states = np.where(np.isfinite(states), states, 0).astype(np.int8)
    executable = (
        np.asarray(bar_end_s, dtype=np.int64)
        if execution_seconds is None
        else np.asarray(execution_seconds, dtype=np.int64)
    )
    return _armed_flip_exit_times_numba(
        np.asarray(bar_end_s, dtype=np.int64),
        clean_states,
        np.asarray(entry_s, dtype=np.int64),
        np.asarray(position_dirs, dtype=np.int8),
        max_hold_s,
        executable,
        max_execution_lag_s,
    )


def first_armed_flip_index(states: np.ndarray, position_dir: int) -> int | None:
    """Return the first opposite Supertrend state after alignment."""
    armed = False
    for index, state in enumerate(states):
        if state == position_dir:
            armed = True
        elif armed and state == -position_dir:
            return index
    return None


def first_liquidity_recovery_index(
    depth: np.ndarray,
    spread: np.ndarray,
    zlogl: np.ndarray,
    *,
    min_depth_ratio: float,
    max_spread_ratio: float,
    min_zlogl_delta: float,
    confirmations: int,
) -> int | None:
    """Return the first index completing a consecutive recovery run."""
    if len(depth) == 0 or confirmations < 1:
        return None
    base_depth = float(depth[0])
    base_spread = float(spread[0])
    base_zlogl = float(zlogl[0])
    if not np.isfinite([base_depth, base_spread, base_zlogl]).all():
        return None
    if base_depth <= 0 or base_spread <= 0:
        return None

    consecutive = 0
    for index in range(1, len(depth)):
        recovered = (
            np.isfinite(depth[index])
            and np.isfinite(spread[index])
            and np.isfinite(zlogl[index])
            and depth[index] / base_depth >= min_depth_ratio
            and spread[index] / base_spread <= max_spread_ratio
            and zlogl[index] - base_zlogl >= min_zlogl_delta
        )
        consecutive = consecutive + 1 if recovered else 0
        if consecutive >= confirmations:
            return index
    return None


def choose_exit_index(
    *,
    supertrend_index: int | None,
    liquidity_index: int | None,
    max_index: int,
) -> tuple[int, str]:
    """Choose the earliest causal exit, falling back to max hold."""
    candidates = [
        (index, reason)
        for index, reason in (
            (supertrend_index, "supertrend"),
            (liquidity_index, "liquidity"),
        )
        if index is not None
    ]
    if not candidates:
        return max_index, "max_hold"
    return min(candidates, key=lambda item: item[0])


def execution_indices_for_times(
    seconds: np.ndarray,
    target_s: np.ndarray,
    *,
    max_lag_s: int,
) -> np.ndarray:
    """Map target timestamps to the first executable observation within a lag cap."""
    seconds_arr = np.asarray(seconds, dtype=np.int64)
    target_arr = np.asarray(target_s, dtype=np.int64)
    indices = np.searchsorted(seconds_arr, target_arr, side="left")
    valid = indices < len(seconds_arr)
    clipped = np.minimum(indices, max(len(seconds_arr) - 1, 0))
    if len(seconds_arr):
        valid &= seconds_arr[clipped] - target_arr <= max_lag_s
    result = np.full(len(target_arr), -1, dtype=np.int64)
    result[valid] = indices[valid]
    return result


def complete_event_mask(
    seconds: np.ndarray,
    entry_s: np.ndarray,
    *,
    max_hold_s: int,
    max_lag_s: int,
) -> np.ndarray:
    """Return events with an executable observation at a common horizon."""
    targets = np.asarray(entry_s, dtype=np.int64) + max_hold_s
    return (
        execution_indices_for_times(
            seconds,
            targets,
            max_lag_s=max_lag_s,
        )
        >= 0
    )


@njit(cache=True)
def _liquidity_exit_times_numba(
    seconds: np.ndarray,
    depth: np.ndarray,
    spread: np.ndarray,
    zlogl: np.ndarray,
    entry_indices: np.ndarray,
    entry_s: np.ndarray,
    max_hold_s: int,
    min_depth_ratio: float,
    max_spread_ratio: float,
    min_zlogl_delta: float,
    confirmations: int,
    max_observation_gap_s: int,
) -> np.ndarray:
    exits = np.full(len(entry_indices), -1, dtype=np.int64)
    for event_index in range(len(entry_indices)):
        start_index = entry_indices[event_index]
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
        deadline = entry_s[event_index] + max_hold_s
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


def liquidity_exit_times_for_events(
    seconds: np.ndarray,
    depth: np.ndarray,
    spread: np.ndarray,
    zlogl: np.ndarray,
    *,
    entry_indices: np.ndarray,
    entry_s: np.ndarray,
    max_hold_s: int,
    min_depth_ratio: float,
    max_spread_ratio: float,
    min_zlogl_delta: float,
    confirmations: int,
    max_observation_gap_s: int,
) -> np.ndarray:
    """Find causal liquidity exits while resetting confirmation across gaps."""
    return _liquidity_exit_times_numba(
        np.asarray(seconds, dtype=np.int64),
        np.asarray(depth, dtype=float),
        np.asarray(spread, dtype=float),
        np.asarray(zlogl, dtype=float),
        np.asarray(entry_indices, dtype=np.int64),
        np.asarray(entry_s, dtype=np.int64),
        max_hold_s,
        min_depth_ratio,
        max_spread_ratio,
        min_zlogl_delta,
        confirmations,
        max_observation_gap_s,
    )


@dataclass(frozen=True)
class Gene:
    timeframe: str
    atr_period: int
    factor: float
    max_hold_s: int
    exit_mode: str
    min_depth_ratio: float
    max_spread_ratio: float
    min_zlogl_delta: float
    confirmations: int


def canonical_gene(gene: Gene) -> Gene:
    """Remove parameters that cannot affect the selected exit policy."""
    if gene.exit_mode == "supertrend":
        return Gene(
            gene.timeframe,
            gene.atr_period,
            gene.factor,
            gene.max_hold_s,
            gene.exit_mode,
            1.0,
            1.0,
            0.0,
            1,
        )
    if gene.exit_mode == "liquidity":
        return Gene(
            "1m",
            10,
            3.0,
            gene.max_hold_s,
            gene.exit_mode,
            gene.min_depth_ratio,
            gene.max_spread_ratio,
            gene.min_zlogl_delta,
            gene.confirmations,
        )
    return gene


def build_completed_bars(base: pd.DataFrame, *, timeframe_s: int) -> pd.DataFrame:
    """Build session-anchored bars and discard partial buckets."""
    frame = base[["sec", "mid_agg"]].sort_values("sec", kind="mergesort").copy()
    frame["segment"] = frame["sec"].diff().gt(SESSION_GAP_S).cumsum()
    segment_start = frame.groupby("segment", sort=False)["sec"].transform("first")
    frame["bar_start"] = segment_start + ((frame["sec"] - segment_start) // timeframe_s) * timeframe_s
    bars = (
        frame.groupby(["segment", "bar_start"], sort=True)
        .agg(
            first_sec=("sec", "first"),
            last_sec=("sec", "last"),
            high=("mid_agg", "max"),
            low=("mid_agg", "min"),
            close=("mid_agg", "last"),
        )
        .reset_index(drop=False)
    )
    bars["bar_end_s"] = bars["bar_start"] + timeframe_s
    complete = (bars["first_sec"] - bars["bar_start"] <= MAX_EXECUTION_LAG_S) & (
        bars["bar_end_s"] - bars["last_sec"] <= MAX_EXECUTION_LAG_S
    )
    return bars.loc[complete].reset_index(drop=True)


class ExitEvaluator:
    def __init__(self) -> None:
        self.secbar = pdq.add_pdq_features(pdq.load_wide())
        self.events = pd.read_csv(ENTRY_PATH)
        self.events = self.events[self.events["label"].eq("TSI15_align")].copy()
        self.events = self.events.sort_values(["sec", "day"], kind="mergesort").reset_index(drop=True)
        self.seconds = self.secbar["sec"].to_numpy(dtype=np.int64)
        self.mid = self.secbar["mid_agg"].to_numpy(dtype=float)
        self.depth = self.secbar["d5_agg"].to_numpy(dtype=float)
        self.spread = self.secbar["spread_agg"].to_numpy(dtype=float)
        self.zlogl = self.secbar["zlogL_min"].to_numpy(dtype=float)
        self.source_entry_counts = self.events.groupby("split").size().to_dict()
        eligible = np.ones(len(self.events), dtype=bool)
        raw_entry_s = self.events["sec"].to_numpy(dtype=np.int64)
        for hold_s in MAX_HOLDS:
            eligible &= complete_event_mask(
                self.seconds,
                raw_entry_s,
                max_hold_s=hold_s,
                max_lag_s=MAX_EXECUTION_LAG_S,
            )
        self.events = self.events.loc[eligible].reset_index(drop=True)
        self.entry_s = self.events["sec"].to_numpy(dtype=np.int64)
        self.position_dirs = self.events["direction"].to_numpy(dtype=np.int8)
        self.entry_indices = np.searchsorted(self.seconds, self.entry_s, side="left")
        if np.any(self.entry_indices >= len(self.seconds)):
            raise RuntimeError("Entry timestamp is outside the secbar")
        if not np.array_equal(self.seconds[self.entry_indices], self.entry_s):
            raise RuntimeError("Entry timestamp is missing from the secbar")
        self.entry_mid = self.events["entry_mid"].to_numpy(dtype=float)
        self.is_mask = self.events["split"].eq("IS").to_numpy()
        self.oos_mask = self.events["split"].eq("OOS").to_numpy()
        self.bars = self._build_bars()
        self.st_exit_cache: dict[tuple[str, int, float], np.ndarray] = {}
        self.liq_exit_cache: dict[tuple[float, float, float, int], np.ndarray] = {}

    def _build_bars(self) -> dict[str, dict[str, np.ndarray]]:
        bars_by_timeframe: dict[str, dict[str, np.ndarray]] = {}
        base = self.secbar[["sec", "mid_agg"]]
        for timeframe, seconds in TIMEFRAMES.items():
            bars = build_completed_bars(base, timeframe_s=seconds)
            bars_by_timeframe[timeframe] = {
                "bar_end_s": bars["bar_end_s"].to_numpy(dtype=np.int64),
                "high": bars["high"].to_numpy(dtype=float),
                "low": bars["low"].to_numpy(dtype=float),
                "close": bars["close"].to_numpy(dtype=float),
            }
        return bars_by_timeframe

    def supertrend_exit_times(self, gene: Gene) -> np.ndarray:
        key = (gene.timeframe, gene.atr_period, gene.factor)
        cached = self.st_exit_cache.get(key)
        if cached is not None:
            return cached
        bars = self.bars[gene.timeframe]
        states = compute_supertrend_direction(
            bars["high"],
            bars["low"],
            bars["close"],
            atr_period=gene.atr_period,
            factor=gene.factor,
        )
        exits = armed_flip_exit_times(
            bars["bar_end_s"],
            states,
            self.entry_s,
            self.position_dirs,
            max_hold_s=max(MAX_HOLDS),
            execution_seconds=self.seconds,
            max_execution_lag_s=MAX_EXECUTION_LAG_S,
        )
        executable = execution_indices_for_times(
            self.seconds,
            exits,
            max_lag_s=MAX_EXECUTION_LAG_S,
        )
        exits[executable < 0] = -1
        self.st_exit_cache[key] = exits
        return exits

    def liquidity_exit_times(self, gene: Gene) -> np.ndarray:
        key = (
            gene.min_depth_ratio,
            gene.max_spread_ratio,
            gene.min_zlogl_delta,
            gene.confirmations,
        )
        cached = self.liq_exit_cache.get(key)
        if cached is not None:
            return cached
        exits = liquidity_exit_times_for_events(
            self.seconds,
            self.depth,
            self.spread,
            self.zlogl,
            entry_indices=self.entry_indices,
            entry_s=self.entry_s,
            max_hold_s=max(MAX_HOLDS),
            min_depth_ratio=gene.min_depth_ratio,
            max_spread_ratio=gene.max_spread_ratio,
            min_zlogl_delta=gene.min_zlogl_delta,
            confirmations=gene.confirmations,
            max_observation_gap_s=MAX_OBSERVATION_GAP_S,
        )
        self.liq_exit_cache[key] = exits
        return exits

    def paths(self, gene: Gene) -> pd.DataFrame:
        gene = canonical_gene(gene)
        deadline = self.entry_s + gene.max_hold_s
        st_exit = (
            self.supertrend_exit_times(gene)
            if gene.exit_mode in {"supertrend", "first"}
            else np.full(len(self.events), -1, dtype=np.int64)
        )
        liq_exit = (
            self.liquidity_exit_times(gene)
            if gene.exit_mode in {"liquidity", "first"}
            else np.full(len(self.events), -1, dtype=np.int64)
        )
        st_valid = (st_exit >= self.entry_s) & (st_exit <= deadline)
        liq_valid = (liq_exit >= self.entry_s) & (liq_exit <= deadline)

        exit_s = deadline.copy()
        reason = np.full(len(self.events), "max_hold", dtype=object)
        if gene.exit_mode in {"supertrend", "first"}:
            exit_s[st_valid] = st_exit[st_valid]
            reason[st_valid] = "supertrend"
        if gene.exit_mode in {"liquidity", "first"}:
            use_liq = liq_valid & ((reason == "max_hold") | (liq_exit < exit_s))
            exit_s[use_liq] = liq_exit[use_liq]
            reason[use_liq] = "liquidity"

        exit_indices = execution_indices_for_times(
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
                "event_row": np.arange(len(self.events)),
                "day": self.events["day"],
                "split": self.events["split"],
                "entry_s": self.entry_s,
                "exit_s": exit_s,
                "execution_s": execution_s,
                "hold_s": np.where(complete, execution_s - self.entry_s, np.nan),
                "reason": reason,
                "gross_pnl": gross,
            }
        )

    def summarize(self, gene: Gene, split: str) -> dict[str, float | int]:
        paths = self.paths(gene)
        sub = paths[paths["split"].eq(split) & paths["gross_pnl"].notna()].copy()
        if sub.empty:
            return empty_summary()
        pnl = sub["gross_pnl"].to_numpy(dtype=float)
        daily = sub.groupby("day", sort=True)["gross_pnl"].sum()
        abs_total = float(daily.abs().sum())
        keep_days = daily.abs().sort_values(ascending=False).iloc[3:].index
        drop_top3 = sub[sub["day"].isin(keep_days)]["gross_pnl"]
        row: dict[str, float | int] = {
            "n": int(len(sub)),
            "active_days": int(sub["day"].nunique()),
            "gross_mean": float(np.mean(pnl)),
            "hit_rate": float(np.mean(pnl > 0)),
            "p25": float(np.quantile(pnl, 0.25)),
            "p50": float(np.quantile(pnl, 0.50)),
            "p75": float(np.quantile(pnl, 0.75)),
            "avg_hold_s": float(sub["hold_s"].mean()),
            "median_daily_pnl": float(daily.median()),
            "top5_day_abs_share": (float(daily.abs().nlargest(5).sum() / abs_total) if abs_total > 0 else math.nan),
            "drop_top3_gross_mean": (float(drop_top3.mean()) if len(drop_top3) else math.nan),
            "supertrend_exit_rate": float(sub["reason"].eq("supertrend").mean()),
            "liquidity_exit_rate": float(sub["reason"].eq("liquidity").mean()),
            "max_hold_exit_rate": float(sub["reason"].eq("max_hold").mean()),
        }
        for cost in COSTS:
            row[f"net_mean_cost{int(cost)}"] = row["gross_mean"] - cost
            row[f"drop_top3_net_cost{int(cost)}"] = row["drop_top3_gross_mean"] - cost
        return row

    def evaluate_is(self, gene: Gene) -> dict[str, Any]:
        gene = canonical_gene(gene)
        summary = self.summarize(gene, "IS")
        return {
            **asdict(gene),
            "fitness": fitness(summary),
            **{f"is_{key}": value for key, value in summary.items()},
        }

    def evaluate_oos(self, gene: Gene) -> dict[str, Any]:
        gene = canonical_gene(gene)
        summary = self.summarize(gene, "OOS")
        return {f"oos_{key}": value for key, value in summary.items()}


def empty_summary() -> dict[str, float | int]:
    keys = (
        "gross_mean",
        "hit_rate",
        "p25",
        "p50",
        "p75",
        "avg_hold_s",
        "median_daily_pnl",
        "top5_day_abs_share",
        "drop_top3_gross_mean",
        "supertrend_exit_rate",
        "liquidity_exit_rate",
        "max_hold_exit_rate",
    )
    row: dict[str, float | int] = {"n": 0, "active_days": 0}
    row.update({key: math.nan for key in keys})
    for cost in COSTS:
        row[f"net_mean_cost{int(cost)}"] = math.nan
        row[f"drop_top3_net_cost{int(cost)}"] = math.nan
    return row


def positive_rate(values: pd.Series) -> float:
    """Return the positive share over complete paths only."""
    complete = values.dropna()
    return float((complete > 0).mean()) if len(complete) else math.nan


def fitness(summary: dict[str, float | int]) -> float:
    if int(summary["n"]) < 400 or int(summary["active_days"]) < 25:
        return -1_000.0
    net = float(summary["net_mean_cost4"])
    robust = float(summary["drop_top3_net_cost4"])
    hit_rate = float(summary["hit_rate"])
    concentration = float(summary["top5_day_abs_share"])
    if not all(math.isfinite(value) for value in (net, robust, hit_rate, concentration)):
        return -1_000.0
    return min(net, robust) + 1.5 * (hit_rate - 0.5) - 2.0 * max(0.0, concentration - 0.45)


def random_gene(rng: random.Random) -> Gene:
    return Gene(
        timeframe=rng.choice(tuple(TIMEFRAMES)),
        atr_period=rng.choice(ATR_VALUES),
        factor=rng.choice(FACTOR_VALUES),
        max_hold_s=rng.choice(MAX_HOLDS),
        exit_mode=rng.choice(EXIT_MODES),
        min_depth_ratio=rng.choice(DEPTH_RATIOS),
        max_spread_ratio=rng.choice(SPREAD_RATIOS),
        min_zlogl_delta=rng.choice(ZLOGL_DELTAS),
        confirmations=rng.choice(CONFIRMATIONS),
    )


def mutate(gene: Gene, rng: random.Random, rate: float) -> Gene:
    values = asdict(gene)
    choices = {
        "timeframe": tuple(TIMEFRAMES),
        "atr_period": ATR_VALUES,
        "factor": FACTOR_VALUES,
        "max_hold_s": MAX_HOLDS,
        "exit_mode": EXIT_MODES,
        "min_depth_ratio": DEPTH_RATIOS,
        "max_spread_ratio": SPREAD_RATIOS,
        "min_zlogl_delta": ZLOGL_DELTAS,
        "confirmations": CONFIRMATIONS,
    }
    for field, options in choices.items():
        if rng.random() < rate:
            values[field] = rng.choice(options)
    return Gene(**values)


def crossover(left: Gene, right: Gene, rng: random.Random) -> Gene:
    return Gene(
        **{
            field: getattr(left, field) if rng.random() < 0.5 else getattr(right, field)
            for field in Gene.__dataclass_fields__
        }
    )


def theoretical_space_size() -> int:
    return (
        len(TIMEFRAMES)
        * len(ATR_VALUES)
        * len(FACTOR_VALUES)
        * len(MAX_HOLDS)
        * len(EXIT_MODES)
        * len(DEPTH_RATIOS)
        * len(SPREAD_RATIOS)
        * len(ZLOGL_DELTAS)
        * len(CONFIRMATIONS)
    )


def run_ga(
    evaluator: ExitEvaluator,
    *,
    population_size: int,
    generations: int,
    mutation_rate: float,
    elite_fraction: float,
    seed: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = random.Random(seed)
    population = [random_gene(rng) for _ in range(population_size)]
    cache: dict[Gene, dict[str, Any]] = {}
    best_rows: list[dict[str, Any]] = []

    def evaluate(gene: Gene) -> dict[str, Any]:
        gene = canonical_gene(gene)
        if gene not in cache:
            cache[gene] = evaluator.evaluate_is(gene)
        return cache[gene]

    for generation in range(generations):
        ranked = sorted(
            (evaluate(gene) for gene in population),
            key=lambda row: float(row["fitness"]),
            reverse=True,
        )
        best_rows.append(
            {
                "generation": generation,
                "unique_evaluations": len(cache),
                **ranked[0],
            }
        )
        elite_count = max(2, int(population_size * elite_fraction))
        elites = [Gene(**{field: row[field] for field in Gene.__dataclass_fields__}) for row in ranked[:elite_count]]
        pool = [
            Gene(**{field: row[field] for field in Gene.__dataclass_fields__})
            for row in ranked[: max(8, elite_count * 4)]
        ]
        next_population = elites.copy()
        while len(next_population) < population_size:
            left = max(
                rng.sample(pool, min(4, len(pool))),
                key=lambda gene: evaluate(gene)["fitness"],
            )
            right = max(
                rng.sample(pool, min(4, len(pool))),
                key=lambda gene: evaluate(gene)["fitness"],
            )
            next_population.append(mutate(crossover(left, right, rng), rng, mutation_rate))
        population = next_population
    return pd.DataFrame(cache.values()), pd.DataFrame(best_rows)


def gene_from_row(row: pd.Series) -> Gene:
    return Gene(**{field: row[field] for field in Gene.__dataclass_fields__})


def fixed_hold_gene(hold_s: int) -> Gene:
    return Gene(
        timeframe="1m",
        atr_period=10,
        factor=3.0,
        max_hold_s=hold_s,
        exit_mode="liquidity",
        min_depth_ratio=math.inf,
        max_spread_ratio=0.0,
        min_zlogl_delta=math.inf,
        confirmations=30,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--population", type=int, default=192)
    parser.add_argument("--generations", type=int, default=120)
    parser.add_argument("--mutation-rate", type=float, default=0.20)
    parser.add_argument("--elite-fraction", type=float, default=0.12)
    parser.add_argument("--seed", type=int, default=20260709)
    parser.add_argument("--oos-candidates", type=int, default=100)
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    evaluator = ExitEvaluator()
    all_is, best = run_ga(
        evaluator,
        population_size=args.population,
        generations=args.generations,
        mutation_rate=args.mutation_rate,
        elite_fraction=args.elite_fraction,
        seed=args.seed,
    )
    ranked = all_is.sort_values("fitness", ascending=False).reset_index(drop=True)
    top = ranked.head(args.oos_candidates).copy()
    oos_rows = [evaluator.evaluate_oos(gene_from_row(row)) for _, row in top.iterrows()]
    validated = pd.concat([top.reset_index(drop=True), pd.DataFrame(oos_rows)], axis=1)
    validated["oos_target_net6_cost4"] = validated["oos_net_mean_cost4"] >= 6.0

    baselines = []
    for hold_s in MAX_HOLDS:
        gene = fixed_hold_gene(hold_s)
        baselines.append(
            {
                "hold_s": hold_s,
                **{f"is_{key}": value for key, value in evaluator.summarize(gene, "IS").items()},
                **evaluator.evaluate_oos(gene),
            }
        )
    baseline_df = pd.DataFrame(baselines)

    winner = gene_from_row(validated.iloc[0])
    winner_paths = evaluator.paths(winner)
    winner_monthly = (
        winner_paths.dropna(subset=["gross_pnl"])
        .assign(month=lambda frame: frame["day"].str.slice(0, 7))
        .groupby(["split", "month"], sort=True)
        .agg(
            n=("gross_pnl", "count"),
            active_days=("day", "nunique"),
            gross_mean=("gross_pnl", "mean"),
            hit_rate=("gross_pnl", positive_rate),
            avg_hold_s=("hold_s", "mean"),
        )
        .reset_index()
    )
    for cost in COSTS:
        winner_monthly[f"net_mean_cost{int(cost)}"] = winner_monthly["gross_mean"] - cost

    ranked.to_csv(OUT_DIR / "ga_is_all_evaluations.csv", index=False)
    best.to_csv(OUT_DIR / "ga_best_by_generation.csv", index=False)
    validated.to_csv(OUT_DIR / "ga_is_ranked_oos_validation.csv", index=False)
    baseline_df.to_csv(OUT_DIR / "fixed_hold_baselines.csv", index=False)
    winner_paths.to_csv(OUT_DIR / "winner_event_paths.csv", index=False)
    winner_monthly.to_csv(OUT_DIR / "winner_monthly.csv", index=False)
    metadata = {
        "source_secbar": str(pdq.DATA_PATH.relative_to(ROOT)),
        "source_entries": str(ENTRY_PATH.relative_to(ROOT)),
        "entry_rule": "fixed existing TSI15_align events; GA cannot alter entry or direction",
        "is_period": "2026-03-03..2026-04-30",
        "oos_period": "2026-05-01..2026-06-13",
        "oos_blindness": (
            "not blind: the fixed source entry artifact used split-specific quantiles/"
            "density matching and this period was examined in prior research"
        ),
        "is_entries": int(evaluator.is_mask.sum()),
        "oos_entries": int(evaluator.oos_mask.sum()),
        "source_entry_counts": evaluator.source_entry_counts,
        "common_event_universe": (
            f"requires executable observations within {MAX_EXECUTION_LAG_S}s at every max-hold horizon {MAX_HOLDS}"
        ),
        "bar_alignment": "completed bars only; exit timestamp is confirmed bar end",
        "armed_rule": "must observe Supertrend aligned with position before opposite flip exits",
        "liquidity_rule": "D5 ratio up, spread ratio down, and zLogL delta up for consecutive observations",
        "fitness": "IS only: min(cost4 net mean, drop-top3 cost4 net mean), hit-rate bonus, concentration penalty",
        "oos_usage": f"revealed only after IS ranking for top {args.oos_candidates} candidates",
        "population": args.population,
        "generations": args.generations,
        "unique_is_evaluations": int(len(ranked)),
        "theoretical_search_space": theoretical_space_size(),
        "actually_evaluated_100m": False,
        "costs_points": list(COSTS),
        "execution_model": "common-mid path proxy minus fixed round-trip costs; no bid/ask or latency fill simulation",
        "seed": args.seed,
    }
    (OUT_DIR / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))
    print(json.dumps(metadata, indent=2, sort_keys=True))
    print("\nTop IS-ranked candidate with OOS reveal:")
    print(validated.head(1).to_string(index=False))
    print("\nFixed-hold baselines:")
    print(baseline_df.to_string(index=False))


if __name__ == "__main__":
    main()
