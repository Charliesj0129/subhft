"""Day-session-only re-test: exclude the night session entirely.

TAIFEX index futures trade a day session (08:45-13:45 Taipei time) and a
night session (15:00-05:00 next day). Earlier session context noted the
underlying secbar export already treats these differently (3-min bars
08:45-13:45, 5-min bars 15:00-05:00). This script asks whether the causal
result changes if the night session is excised entirely rather than mixed
in: entries only fire in the day session, and the Supertrend/liquidity
exit signals only ever see day-session price action (a position that would
need night-session data to close is left incomplete and dropped from the
result, the same way any other data-boundary incompleteness is handled
elsewhere in these tools).

Reuses the causal entry population and secbar features unmodified; the
rolling PDQ/TSI/Fisher/EMA indicators and the causal entry quantile
calibration are computed over the FULL history first (so they still see
continuous context), and only the final entry/secbar rows fed to the exit
evaluator are restricted to day-session hours. Exit policy is the
already-published, not-re-tuned config: Supertrend 1m/ATR3/factor2.1,
900s max hold, exit_mode=first, liquidity overlay -- the standing best
configuration from this session's other sweeps, held fixed here so this
test isolates the session-filtering question alone.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BASE_TOOL = ROOT / "research/tools/pdq_tsi15_decomposition_audit.py"
EXIT_TOOL = ROOT / "research/tools/pdq_supertrend_exit_search.py"
CAUSAL_EVENTS_PATH = ROOT / "outputs/liquidity_score/pdq_causal_walkforward/causal_event_level_paths.csv.gz"
OUT_DIR = ROOT / "outputs/liquidity_score/pdq_causal_day_session_only"

COSTS = (2.0, 4.0, 6.0)
MAX_EXECUTION_LAG_S = 5

FIXED_GENE_PARAMS = {
    "timeframe": "1m",
    "atr_period": 3,
    "factor": 2.1,
    "max_hold_s": 900,
    "exit_mode": "first",
    "min_depth_ratio": 1.3,
    "max_spread_ratio": 0.5,
    "min_zlogl_delta": 0.0,
    "confirmations": 3,
}

DAY_SESSION_START_MIN = 8 * 60 + 45
DAY_SESSION_END_MIN = 13 * 60 + 45


def load_module(path: Path, name: str) -> Any:
    """Load `path` under its real dotted package name, reusing any existing load.

    See `pdq_causal_walkforward.load_module` for why the name must be the
    canonical `research.tools.<module>` path.
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


def day_session_mask(seconds: np.ndarray) -> np.ndarray:
    tpe = pd.to_datetime(seconds, unit="s", utc=True).tz_convert("Asia/Taipei")
    minute_of_day = tpe.hour * 60 + tpe.minute
    return (minute_of_day >= DAY_SESSION_START_MIN) & (minute_of_day < DAY_SESSION_END_MIN)


class DaySessionExitEvaluator:
    """Same causal entries and fixed exit policy, night session excised."""

    def __init__(self) -> None:
        secbar = pdq.add_pdq_features(pdq.load_wide())
        secbar_mask = day_session_mask(secbar["sec"].to_numpy(dtype=np.int64))
        self.night_secbar_rows_dropped = int((~secbar_mask).sum())
        self.secbar = secbar.loc[secbar_mask].reset_index(drop=True)

        events = pd.read_csv(CAUSAL_EVENTS_PATH)
        events = events[events["label"].eq("TSI15_align")].copy()
        events_mask = day_session_mask(events["sec"].to_numpy(dtype=np.int64))
        self.night_events_dropped = int((~events_mask).sum())
        events = events.loc[events_mask].copy()
        self.events = events.sort_values(["sec", "day"], kind="mergesort").reset_index(drop=True)

        self.seconds = self.secbar["sec"].to_numpy(dtype=np.int64)
        self.mid = self.secbar["mid_agg"].to_numpy(dtype=float)
        self.depth = self.secbar["d5_agg"].to_numpy(dtype=float)
        self.spread = self.secbar["spread_agg"].to_numpy(dtype=float)
        self.zlogl = self.secbar["zlogL_min"].to_numpy(dtype=float)

        raw_entry_s = self.events["sec"].to_numpy(dtype=np.int64)
        eligible = exit_search.complete_event_mask(
            self.seconds,
            raw_entry_s,
            max_hold_s=FIXED_GENE_PARAMS["max_hold_s"],
            max_lag_s=MAX_EXECUTION_LAG_S,
        )
        self.events = self.events.loc[eligible].reset_index(drop=True)
        self.entry_s = self.events["sec"].to_numpy(dtype=np.int64)
        self.position_dirs = self.events["direction"].to_numpy(dtype=np.int8)
        self.entry_indices = np.searchsorted(self.seconds, self.entry_s, side="left")
        if np.any(self.entry_indices >= len(self.seconds)) or not np.array_equal(
            self.seconds[self.entry_indices], self.entry_s
        ):
            raise RuntimeError("Entry timestamp is missing from the day-session secbar")
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
        gene = exit_search.Gene(**FIXED_GENE_PARAMS)
        states = exit_search.compute_supertrend_direction(
            self.bars["high"], self.bars["low"], self.bars["close"], atr_period=gene.atr_period, factor=gene.factor
        )
        st_exit = exit_search.armed_flip_exit_times(
            self.bars["bar_end_s"],
            states,
            self.entry_s,
            self.position_dirs,
            max_hold_s=gene.max_hold_s,
            execution_seconds=self.seconds,
            max_execution_lag_s=MAX_EXECUTION_LAG_S,
        )
        liq_exit = exit_search.liquidity_exit_times_for_events(
            self.seconds,
            self.depth,
            self.spread,
            self.zlogl,
            entry_indices=self.entry_indices,
            entry_s=self.entry_s,
            max_hold_s=gene.max_hold_s,
            min_depth_ratio=gene.min_depth_ratio,
            max_spread_ratio=gene.max_spread_ratio,
            min_zlogl_delta=gene.min_zlogl_delta,
            confirmations=gene.confirmations,
            max_observation_gap_s=MAX_EXECUTION_LAG_S,
        )

        deadline = self.entry_s + gene.max_hold_s
        st_valid = (st_exit >= self.entry_s) & (st_exit <= deadline)
        liq_valid = (liq_exit >= self.entry_s) & (liq_exit <= deadline)

        exit_s = deadline.copy()
        reason = np.full(len(self.events), "max_hold", dtype=object)
        exit_s[st_valid] = st_exit[st_valid]
        reason[st_valid] = "supertrend"
        use_liq = liq_valid & ((reason == "max_hold") | (liq_exit < exit_s))
        exit_s[use_liq] = liq_exit[use_liq]
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


def monthly_report(paths: pd.DataFrame) -> pd.DataFrame:
    complete = paths.dropna(subset=["gross_pnl"]).assign(month=lambda frame: frame["day"].str.slice(0, 7))
    monthly = (
        complete.groupby("month", sort=True)
        .agg(
            n=("gross_pnl", "count"),
            active_days=("day", "nunique"),
            gross_mean=("gross_pnl", "mean"),
            hit_rate=("gross_pnl", lambda s: float((s > 0).mean())),
        )
        .reset_index()
    )
    for cost in COSTS:
        monthly[f"net_mean_cost{int(cost)}"] = monthly["gross_mean"] - cost
    return monthly


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    evaluator = DaySessionExitEvaluator()
    paths = evaluator.paths()
    monthly = monthly_report(paths)
    complete = paths.dropna(subset=["gross_pnl"])

    overall = {
        "n": int(len(complete)),
        "active_days": int(complete["day"].nunique()) if len(complete) else 0,
        "gross_mean": float(complete["gross_pnl"].mean()) if len(complete) else float("nan"),
    }
    for cost in COSTS:
        overall[f"net_mean_cost{int(cost)}"] = overall["gross_mean"] - cost if len(complete) else float("nan")

    metadata = {
        "day_session_window_tpe": "08:45-13:45",
        "night_secbar_rows_dropped": evaluator.night_secbar_rows_dropped,
        "night_events_dropped": evaluator.night_events_dropped,
        "surviving_events_n": int(len(evaluator.events)),
        "fixed_gene": FIXED_GENE_PARAMS,
        "overall_cost4": overall,
        "note": (
            "rolling indicators and causal entry quantile calibration were computed over the FULL "
            "history (unchanged from pdq_causal_walkforward); only the final entry/secbar rows fed "
            "to the exit evaluator are restricted to day-session hours"
        ),
    }
    (OUT_DIR / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))
    monthly.to_csv(OUT_DIR / "day_session_monthly_cost4.csv", index=False)
    paths.to_csv(OUT_DIR / "day_session_event_paths.csv.gz", index=False)

    print(json.dumps(metadata, indent=2, sort_keys=True))
    print("\nDay-session-only monthly net (cost4):")
    print(monthly.to_string(index=False))


if __name__ == "__main__":
    main()
