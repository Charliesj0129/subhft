"""Causal (no-lookahead) re-test of the fixed PDQ_cont + TSI15 exit policy.

`pdq_tsi15_decomposition_audit.py` selects entry events using quantile
thresholds computed once per split ("IS" vs "OOS"), and then subsamples to a
fixed historical count via `density_match_prior_pdq_cont`. Both steps use
information from the full split (including future rows relative to any given
day), so the May-June "validation" entries are not a blind test of the entry
rule itself.

This script rebuilds the same TSI15_align entry population using only an
expanding, prior-days-only quantile calibration (a genuine walk-forward), and
drops the magic-count density match entirely since matching a future-derived
target count is itself non-causal. Everything else (PDQ/TSI/Fisher/opening
range feature construction, the TSI15_align direction rule, and the entry
event schema) is reused unmodified from the base audit tool.

The already-selected exit policy (Supertrend 1m/ATR3/factor2.1, 900s max
hold, liquidity overlay, exit_mode=first) is NOT re-tuned here: GA search
happened over the old IS+OOS-combined entry population, so re-running it
against these causal entries would need its own held-out fold this repo does
not currently have. Scoring the fixed, already-published exit policy against
a cleaner entry population is the honest test available without new data.
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
OUT_DIR = ROOT / "outputs/liquidity_score/pdq_causal_walkforward"
CAUSAL_EVENTS_PATH = OUT_DIR / "causal_event_level_paths.csv.gz"

WARMUP_DAYS = 15
COSTS = (2.0, 4.0, 6.0)

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


def load_module(path: Path, name: str) -> Any:
    """Load `path` under its real dotted package name, reusing any existing load.

    Using the canonical `research.tools.<module>` name (rather than an
    arbitrary synthetic one) matters for two reasons: dataclasses with
    deferred (PEP 563) annotations resolve via `sys.modules[cls.__module__]`
    at decoration time, which requires the module to be registered there
    first; and `@njit(cache=True)` functions persist a disk cache keyed by
    source file plus the compiling module's name, so loading the same file
    under a different name than a normal `import research.tools.X` (as the
    existing pytest suite does) would read back a stale, mismatched cache
    entry and crash. Reusing an already-registered module avoids re-running
    that dance -- and avoids a second, independent numba compile -- entirely.
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


def build_opportunity_mask_causal(df: pd.DataFrame, warmup_days: int = WARMUP_DAYS) -> pd.Series:
    """Expanding, prior-days-only quantile calibration. No future rows used."""
    days_sorted = sorted(df["day"].unique())
    day_rank = df["day"].map({day: rank for rank, day in enumerate(days_sorted)}).to_numpy()

    c60_abs = df["C60"].abs()
    rvexp = df["rvexp"]
    spread = df["spread_agg"]
    d5 = df["d5_agg"]

    mask = pd.Series(False, index=df.index)
    for rank in range(warmup_days, len(days_sorted)):
        prior = day_rank < rank
        current = day_rank == rank
        if not prior.any() or not current.any():
            continue
        q_abs_c = c60_abs[prior].quantile(0.95)
        q_rv = rvexp[prior].quantile(0.90)
        q_spread = spread[prior].quantile(0.90)
        q_d5 = d5[prior].quantile(0.20)
        med_spread = spread[prior].median()
        med_d5 = d5[prior].median()
        stable_book = (spread <= med_spread) & (d5 >= med_d5)
        day_mask = (
            current
            & c60_abs.gt(q_abs_c).to_numpy()
            & rvexp.gt(q_rv).to_numpy()
            & df["cross_sync_ge2"].eq(1).to_numpy()
            & ~stable_book.to_numpy()
            & spread.le(q_spread).to_numpy()
            & d5.ge(q_d5).to_numpy()
            & df["signC60"].ne(0).to_numpy()
        )
        mask |= pd.Series(day_mask, index=df.index)
    return mask.fillna(False)


def build_causal_events() -> pd.DataFrame:
    df = pdq.load_wide()
    df = pdq.add_pdq_features(df)
    df = pdq.add_completed_bar_indicators(df)
    df = pdq.add_opening_range(df)

    opportunity = build_opportunity_mask_causal(df)

    sign_c = df["signC60"].to_numpy(dtype=np.int8)
    dir_tsi = df["dir_tsi15"].fillna(0).to_numpy(dtype=np.int8)
    direction = np.where((dir_tsi != 0) & (dir_tsi == sign_c), dir_tsi, 0).astype(np.int8)

    events = pdq.make_events(df, opportunity, direction, "TSI15_align")
    events["split"] = events["day"].str.slice(0, 7)
    return events


def score_fixed_policy(events: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    exit_search = load_module(EXIT_TOOL, "research.tools.pdq_supertrend_exit_search")
    exit_search.ENTRY_PATH = CAUSAL_EVENTS_PATH
    evaluator = exit_search.ExitEvaluator()
    gene = exit_search.Gene(**FIXED_GENE_PARAMS)

    paths = evaluator.paths(gene)
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
            max_hold_exit_rate=("reason", lambda s: float((s == "max_hold").mean())),
        )
        .reset_index()
    )
    for cost in COSTS:
        monthly[f"net_mean_cost{int(cost)}"] = monthly["gross_mean"] - cost

    overall = {
        "n": int(len(complete)),
        "active_days": int(complete["day"].nunique()),
        "gross_mean": float(complete["gross_pnl"].mean()) if len(complete) else float("nan"),
    }
    for cost in COSTS:
        overall[f"net_mean_cost{int(cost)}"] = overall["gross_mean"] - cost if len(complete) else float("nan")

    metadata = {
        "warmup_days_excluded": WARMUP_DAYS,
        "entry_rule": "causal expanding-window PDQ_cont q95 (no density-match, no per-split quantile)",
        "exit_rule": "fixed, already-selected GA winner; not re-tuned against these entries",
        "fixed_gene": FIXED_GENE_PARAMS,
        "source_events_n": int(len(events)),
        "complete_path_n": int(len(complete)),
        "overall_cost4": overall,
        "known_limitation": (
            "the exit policy itself was GA-selected using the old IS+OOS-combined entry "
            "population, not a fold independent of this data; this is the best available "
            "honest test without new data, not a fully independent re-derivation"
        ),
    }
    return monthly, metadata


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    events = build_causal_events()
    events.to_csv(CAUSAL_EVENTS_PATH, index=False)

    monthly, metadata = score_fixed_policy(events)
    monthly.to_csv(OUT_DIR / "causal_monthly_cost4.csv", index=False)
    (OUT_DIR / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))

    print(json.dumps(metadata, indent=2, sort_keys=True))
    print("\nCausal monthly net (cost4):")
    print(monthly.to_string(index=False))


if __name__ == "__main__":
    main()
