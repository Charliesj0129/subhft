"""Supertrend parameter-space grid search on the causal (no-lookahead) entries.

Earlier scripts this session tested one Supertrend dimension at a time
(timeframe alone, the 900s cap alone, day-session-only) against the fixed,
already-published exit gene. This script asks the broader question directly:
across a joint grid of timeframe x atr_period x factor x max_hold_s (the four
parameters that actually define the Supertrend exit rule and its timeout),
is there a materially better combination than the standing 1m/ATR3/factor2.1
/900s pick -- or does the whole neighborhood stay net-negative?

The liquidity overlay (min_depth_ratio/max_spread_ratio/min_zlogl_delta/
confirmations) and exit_mode are held at the already-published values so this
sweep isolates the Supertrend-specific parameter space; re-opening the
overlay too would multiply the grid without adding evidence about the
question asked.

Grid size: 6 timeframes x 8 atr_period x 8 factor x 4 max_hold_s = 1,536
combinations -- the >=1000 the user asked for.

Honesty about multiple comparisons: ranking 1,536 combinations by in-sample
performance and reporting only the winner is exactly the kind of
researcher-degree-of-freedom this session has been removing elsewhere. So
this script holds out June (2026-06, the only month positive under every
other exit-parameter variant tested so far) as a genuine blind fold: the
grid is ranked on March-May only, and June is revealed for just the top-K
ranked candidates -- after ranking, not before. A positive OOS reveal here
is corroborating evidence, not proof: n~260/~12 active days is a thin fold,
and it is the same month already suspected of regime concentration.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BASE_TOOL = ROOT / "research/tools/pdq_tsi15_decomposition_audit.py"
EXIT_TOOL = ROOT / "research/tools/pdq_supertrend_exit_search.py"
CAUSAL_EVENTS_PATH = ROOT / "outputs/liquidity_score/pdq_causal_walkforward/causal_event_level_paths.csv.gz"
OUT_DIR = ROOT / "outputs/liquidity_score/pdq_causal_supertrend_grid_search"

COSTS = (2.0, 4.0, 6.0)
IS_OOS_CUTOFF_DAY = "2026-06-01"  # IS = Mar-May causal entries; OOS = June, revealed only for top-K IS picks

TIMEFRAMES = ("1m", "2m", "3m", "5m", "10m", "15m")
ATR_PERIODS = (3, 5, 8, 13, 21, 34, 50, 60)
FACTORS = (0.8, 1.2, 1.6, 2.1, 2.7, 3.5, 4.5, 6.0)
MAX_HOLDS = (300, 600, 900, 1800)

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
    canonical `research.tools.<module>` name (numba disk-cache + PEP 563
    dataclass resolution both key off the compiling module's registered name).
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


exit_search = load_module(EXIT_TOOL, "research.tools.pdq_supertrend_exit_search")
load_module(BASE_TOOL, "research.tools.pdq_tsi15_decomposition_audit")
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
        for timeframe, atr_period, factor, max_hold_s in itertools.product(TIMEFRAMES, ATR_PERIODS, FACTORS, MAX_HOLDS)
    ]


def summarize_split(paths: pd.DataFrame, mask: np.ndarray) -> dict[str, float | int]:
    """Aggregate one side of an IS/OOS split; NaN-safe for empty folds."""
    sub = paths.loc[mask].dropna(subset=["gross_pnl"])
    if sub.empty:
        row: dict[str, float | int] = {
            "n": 0,
            "active_days": 0,
            "gross_mean": float("nan"),
            "hit_rate": float("nan"),
            "avg_hold_s": float("nan"),
        }
        for cost in COSTS:
            row[f"net_mean_cost{int(cost)}"] = float("nan")
        return row
    pnl = sub["gross_pnl"].to_numpy(dtype=float)
    row = {
        "n": int(len(sub)),
        "active_days": int(sub["day"].nunique()),
        "gross_mean": float(np.mean(pnl)),
        "hit_rate": float(np.mean(pnl > 0)),
        "avg_hold_s": float(sub["hold_s"].mean()),
    }
    for cost in COSTS:
        row[f"net_mean_cost{int(cost)}"] = row["gross_mean"] - cost
    return row


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
        is_summary = summarize_split(paths, is_mask)
        rows.append({**asdict(gene), **{f"is_{key}": value for key, value in is_summary.items()}})
    grid_df = pd.DataFrame(rows)

    eligible = grid_df[(grid_df["is_n"] >= IS_MIN_N) & (grid_df["is_active_days"] >= IS_MIN_ACTIVE_DAYS)].copy()
    ranked = eligible.sort_values("is_net_mean_cost4", ascending=False).reset_index(drop=True)

    top = ranked.head(TOP_K_FOR_OOS_REVEAL).copy()
    oos_rows = []
    for _, row in top.iterrows():
        gene = gene_from_grid_row(row)
        paths = evaluator.paths(gene)
        oos_summary = summarize_split(paths, oos_mask)
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
    published_is = summarize_split(published_paths, is_mask)
    published_oos = summarize_split(published_paths, oos_mask)

    metadata = {
        "theoretical_grid_size": len(genes),
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
            "ranking 1536 combinations on IS and revealing OOS only for the top 20 controls the "
            "obvious overfitting risk, but the OOS fold is June only (~260 events / ~12 active days) "
            "and June is already the sole positive month under every other exit-parameter variant "
            "tested this session -- a positive OOS reveal here is consistent with that known regime "
            "concentration, not independent confirmation of a new edge"
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
