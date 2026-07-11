"""Full joint Supertrend grid search: finer atr/factor grid + re-explored hold caps.

`pdq_causal_supertrend_grid_search.py` swept timeframe x atr_period(8) x
factor(8) x max_hold_s(4, the original published set 300/600/900/1800) --
1,536 combinations, 0% IS-positive. `pdq_causal_supertrend_grid_search_
uncapped.py` widened atr_period/factor to 13 values each and removed the
cap entirely -- 1,014 combinations, 10.7% IS-"positive" but every one a
degenerate unbounded-hold artifact that collapsed OOS, confirming the time
cap is necessary risk control rather than something to remove.

This script combines both changes without removing the cap: the same wider
13 x 13 atr_period/factor grid as the uncapped search (so results are
directly comparable to it), joined with a genuinely re-explored max_hold_s
parameter space -- 8 values from 180s to 3600s, not just the original
published 4 -- to check whether a cap value the earlier searches never
tried (e.g. something between 900s and 1800s, or shorter than 300s) does
better than the standing 900s pick at this finer atr/factor resolution.

Grid: 6 timeframes x 13 atr_period x 13 factor x 8 max_hold_s = **8,112**
combinations. Monkeypatches `exit_search.MAX_HOLDS` to the new 8-value hold
set *before* constructing `ExitEvaluator` -- required for correctness, not
just cosmetic: `ExitEvaluator.supertrend_exit_times` / `liquidity_exit_
times` cap their internal scan at `max(MAX_HOLDS)`, so leaving the module
default at 1800 would silently truncate the search window for any
max_hold_s=3600 gene and miss flips between 1800-3600s.

Same overfitting control as both prior grid searches: ranked on March-May
in-sample, June revealed only for the top 20 IS-ranked candidates.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BASE_TOOL = ROOT / "research/tools/pdq_tsi15_decomposition_audit.py"
EXIT_TOOL = ROOT / "research/tools/pdq_supertrend_exit_search.py"
GRID_TOOL = ROOT / "research/tools/pdq_causal_supertrend_grid_search.py"
CAUSAL_EVENTS_PATH = ROOT / "outputs/liquidity_score/pdq_causal_walkforward/causal_event_level_paths.csv.gz"
OUT_DIR = ROOT / "outputs/liquidity_score/pdq_causal_supertrend_grid_search_v2"

IS_OOS_CUTOFF_DAY = "2026-06-01"  # IS = Mar-May causal entries; OOS = June, revealed only for top-K IS picks

TIMEFRAMES = ("1m", "2m", "3m", "5m", "10m", "15m")
ATR_PERIODS = (3, 5, 7, 8, 10, 13, 17, 21, 26, 34, 42, 50, 60)
FACTORS = (0.6, 0.8, 1.0, 1.2, 1.5, 1.8, 2.1, 2.5, 3.0, 3.5, 4.5, 6.0, 8.0)
HOLD_VALUES = (180, 300, 450, 600, 900, 1200, 1800, 3600)

FIXED_LIQUIDITY_PARAMS = {
    "exit_mode": "first",
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


load_module(BASE_TOOL, "research.tools.pdq_tsi15_decomposition_audit")
exit_search = load_module(EXIT_TOOL, "research.tools.pdq_supertrend_exit_search")
grid_search = load_module(GRID_TOOL, "research.tools.pdq_causal_supertrend_grid_search")

exit_search.MAX_HOLDS = HOLD_VALUES
exit_search.ENTRY_PATH = CAUSAL_EVENTS_PATH


def build_grid() -> list[Any]:
    return [
        exit_search.Gene(
            timeframe=timeframe,
            atr_period=atr_period,
            factor=factor,
            max_hold_s=max_hold_s,
            **FIXED_LIQUIDITY_PARAMS,
        )
        for timeframe, atr_period, factor, max_hold_s in itertools.product(
            TIMEFRAMES, ATR_PERIODS, FACTORS, HOLD_VALUES
        )
    ]


def gene_from_grid_row(row: pd.Series) -> Any:
    return exit_search.Gene(
        timeframe=row["timeframe"],
        atr_period=int(row["atr_period"]),
        factor=float(row["factor"]),
        max_hold_s=int(row["max_hold_s"]),
        **FIXED_LIQUIDITY_PARAMS,
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    evaluator = exit_search.ExitEvaluator()
    day = evaluator.events["day"].to_numpy()
    is_mask = day < IS_OOS_CUTOFF_DAY
    oos_mask = ~is_mask

    genes = build_grid()
    rows = []
    for gene in genes:
        paths = evaluator.paths(gene)
        is_summary = grid_search.summarize_split(paths, is_mask)
        rows.append({**asdict(gene), **{f"is_{key}": value for key, value in is_summary.items()}})
    grid_df = pd.DataFrame(rows)

    eligible = grid_df[(grid_df["is_n"] >= IS_MIN_N) & (grid_df["is_active_days"] >= IS_MIN_ACTIVE_DAYS)].copy()
    ranked = eligible.sort_values("is_net_mean_cost4", ascending=False).reset_index(drop=True)

    top = ranked.head(TOP_K_FOR_OOS_REVEAL).copy()
    oos_rows = []
    for _, row in top.iterrows():
        gene = gene_from_grid_row(row)
        paths = evaluator.paths(gene)
        oos_summary = grid_search.summarize_split(paths, oos_mask)
        oos_rows.append({f"oos_{key}": value for key, value in oos_summary.items()})
    validated = pd.concat([top.reset_index(drop=True), pd.DataFrame(oos_rows)], axis=1)

    published_gene = exit_search.Gene(
        timeframe="1m",
        atr_period=3,
        factor=2.1,
        max_hold_s=900,
        **FIXED_LIQUIDITY_PARAMS,
    )
    published_paths = evaluator.paths(published_gene)
    published_is = grid_search.summarize_split(published_paths, is_mask)
    published_oos = grid_search.summarize_split(published_paths, oos_mask)

    hold_value_is_summary = (
        eligible.groupby("max_hold_s")["is_net_mean_cost4"]
        .agg(["count", "median", "max"])
        .reset_index()
        .to_dict(orient="records")
    )

    metadata = {
        "theoretical_grid_size": len(genes),
        "hold_values_tested": list(HOLD_VALUES),
        "is_period": "2026-03-03..2026-05-31 (causal entries only)",
        "oos_period": "2026-06-01..2026-06-13 (genuinely held out of ranking)",
        "is_eligibility_floor": {"min_n": IS_MIN_N, "min_active_days": IS_MIN_ACTIVE_DAYS},
        "is_eligible_count": int(len(eligible)),
        "is_eligible_positive_cost4_share": (
            float((eligible["is_net_mean_cost4"] > 0).mean()) if len(eligible) else float("nan")
        ),
        "is_net_cost4_median": float(eligible["is_net_mean_cost4"].median()) if len(eligible) else float("nan"),
        "is_net_cost4_max": float(eligible["is_net_mean_cost4"].max()) if len(eligible) else float("nan"),
        "is_net_cost4_by_hold_value": hold_value_is_summary,
        "top_k_revealed": TOP_K_FOR_OOS_REVEAL,
        "published_gene_is": published_is,
        "published_gene_oos": published_oos,
        "fixed_liquidity_params": FIXED_LIQUIDITY_PARAMS,
        "known_limitation": (
            "same multiple-comparisons control as both prior grid searches: OOS fold is June only "
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
            [
                "timeframe",
                "atr_period",
                "factor",
                "max_hold_s",
                "is_n",
                "is_net_mean_cost4",
                "oos_n",
                "oos_net_mean_cost4",
            ]
        ].to_string(index=False)
    )


if __name__ == "__main__":
    main()
