"""Sweep the Supertrend bar timeframe on the causal (no-lookahead) entries.

`pdq_causal_walkforward.py` established the honest baseline: causal entries,
fixed exit policy (Supertrend 1m/ATR3/factor2.1, 900s max hold, first-of-two
with the liquidity overlay), overall net cost4 = -3.05. This sweeps only the
Supertrend bar timeframe across the six the base tool supports
(1m/2m/3m/5m/10m/15m), holding ATR period (3), factor (2.1), max hold (900s),
and the liquidity overlay fixed at their previously published values.

ATR period/factor are NOT re-optimized per timeframe. Doing so would be a
second round of in-sample re-fitting on the same evaluation data this
session already flagged as a lookahead risk once (the original per-split
quantile calibration); holding them fixed keeps this a single-dimension,
honest comparison at the cost of not knowing whether a re-tuned ATR/factor
would suit a different timeframe better.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
EXIT_TOOL = ROOT / "research/tools/pdq_supertrend_exit_search.py"
CAUSAL_EVENTS_PATH = ROOT / "outputs/liquidity_score/pdq_causal_walkforward/causal_event_level_paths.csv.gz"
OUT_DIR = ROOT / "outputs/liquidity_score/pdq_causal_timeframe_sweep"

COSTS = (2.0, 4.0, 6.0)
TIMEFRAMES = ("1m", "2m", "3m", "5m", "10m", "15m")


def load_module(path: Path, name: str) -> Any:
    """Load `path` under its real dotted package name, reusing any existing load.

    See `pdq_causal_walkforward.load_module` for why the name must be the
    canonical `research.tools.<module>` path: it keeps this file's numba
    -cache behavior identical whether run standalone or alongside the
    existing pytest suite's normal `import research.tools.X`.
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


FIXED_PARAMS = {
    "atr_period": 3,
    "factor": 2.1,
    "max_hold_s": 900,
    "exit_mode": "first",
    "min_depth_ratio": 1.3,
    "max_spread_ratio": 0.5,
    "min_zlogl_delta": 0.0,
    "confirmations": 3,
}


def monthly_from_paths(paths: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    complete = paths.dropna(subset=["gross_pnl"]).assign(month=lambda frame: frame["day"].str.slice(0, 7))
    monthly = (
        complete.groupby("month", sort=True)
        .agg(
            n=("gross_pnl", "count"),
            active_days=("day", "nunique"),
            gross_mean=("gross_pnl", "mean"),
        )
        .reset_index()
    )
    for cost in COSTS:
        monthly[f"net_mean_cost{int(cost)}"] = monthly["gross_mean"] - cost
    monthly["timeframe"] = timeframe
    return monthly


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    exit_search = load_module(EXIT_TOOL, "research.tools.pdq_supertrend_exit_search")
    exit_search.ENTRY_PATH = CAUSAL_EVENTS_PATH
    evaluator = exit_search.ExitEvaluator()

    summary_rows = []
    monthly_frames = []
    for timeframe in TIMEFRAMES:
        gene = exit_search.Gene(timeframe=timeframe, **FIXED_PARAMS)
        paths = evaluator.paths(gene)
        complete = paths.dropna(subset=["gross_pnl"])
        monthly_frames.append(monthly_from_paths(paths, timeframe))

        has_events = len(complete) > 0
        reason_counts = complete["reason"].value_counts(normalize=True) if has_events else pd.Series(dtype=float)

        gross_mean = float(complete["gross_pnl"].mean()) if has_events else float("nan")
        row = {
            "timeframe": timeframe,
            "n": int(len(complete)),
            "active_days": int(complete["day"].nunique()) if has_events else 0,
            "gross_mean": gross_mean,
            "hit_rate": float((complete["gross_pnl"] > 0).mean()) if has_events else float("nan"),
            "supertrend_exit_rate": float(reason_counts.get("supertrend", 0.0)),
            "liquidity_exit_rate": float(reason_counts.get("liquidity", 0.0)),
            "max_hold_exit_rate": float(reason_counts.get("max_hold", 0.0)),
        }
        for cost in COSTS:
            row[f"net_mean_cost{int(cost)}"] = gross_mean - cost if len(complete) else float("nan")
        summary_rows.append(row)

    summary = pd.DataFrame(summary_rows).sort_values("net_mean_cost4", ascending=False).reset_index(drop=True)
    monthly_all = pd.concat(monthly_frames, ignore_index=True)

    summary.to_csv(OUT_DIR / "timeframe_sweep_summary_cost4.csv", index=False)
    monthly_all.to_csv(OUT_DIR / "timeframe_sweep_monthly_cost4.csv", index=False)

    metadata = {
        "source_events": str(CAUSAL_EVENTS_PATH.relative_to(ROOT)),
        "timeframes_swept": list(TIMEFRAMES),
        "fixed_params_besides_timeframe": FIXED_PARAMS,
        "best_timeframe_cost4": summary.iloc[0]["timeframe"] if len(summary) else None,
        "note": (
            "ATR period/factor/liquidity overlay held at previously published "
            "values for every timeframe -- not re-optimized per timeframe -- "
            "to avoid a second round of in-sample re-fitting on the same "
            "evaluation data."
        ),
    }
    (OUT_DIR / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))

    print(json.dumps(metadata, indent=2, sort_keys=True))
    print("\nTimeframe summary (cost4, ranked best to worst):")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
