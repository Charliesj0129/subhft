"""T1G read-only market-regime hypothesis review.

This module consumes the labeled diagnostic artifact and performs descriptive
regime splits. It does not tune signal thresholds, create orders, or modify
production behavior.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from research.t1.regime_viability import NS_PER_MINUTE, _session_start_ns

DEFAULT_IN_PATH = Path("research/experiments/validations/t1g_extreme_imbalance_v0/labeled_diagnostic.json")
DEFAULT_OUT_PATH = Path("research/experiments/validations/t1g_extreme_imbalance_v0/regime_review.json")
DEFAULT_HORIZONS_MINUTES = (5, 15, 30)
CANDIDATE_BRANCHES = {"extreme_high_imbalance_momentum", "extreme_low_imbalance_reversal"}
REGIME_DIMENSIONS = ("time_bucket", "txf_move_bucket", "tmf_spread_bucket")


def _first_entry_spread(row: dict[str, Any], horizons_minutes: Sequence[int]) -> float | None:
    for horizon in horizons_minutes:
        value = row.get(f"label_{horizon}m_entry_spread_pts")
        if value is not None:
            return float(value)
    return None


def classify_market_regime(
    row: dict[str, Any],
    *,
    horizons_minutes: Sequence[int] = DEFAULT_HORIZONS_MINUTES,
) -> dict[str, str]:
    session_start = _session_start_ns(str(row["date"]), tz_offset_hours=8)
    minutes_since_open = (int(row["decision_time_ns"]) - session_start) / NS_PER_MINUTE
    if minutes_since_open < 60:
        time_bucket = "opening_0_60m"
    elif minutes_since_open < 180:
        time_bucket = "mid_60_180m"
    else:
        time_bucket = "late_180m_plus"

    return_pts = float(row.get("return_pts", 0.0))
    if return_pts <= -100.0:
        txf_move_bucket = "large_down_le_-100"
    elif return_pts < 0.0:
        txf_move_bucket = "down_-100_0"
    elif return_pts == 0.0:
        txf_move_bucket = "flat_0"
    elif return_pts < 100.0:
        txf_move_bucket = "up_0_100"
    else:
        txf_move_bucket = "large_up_ge_100"

    spread = _first_entry_spread(row, horizons_minutes)
    if spread is None:
        tmf_spread_bucket = "missing_entry_spread"
    elif spread <= 2.0:
        tmf_spread_bucket = "tight_le_2"
    elif spread <= 5.0:
        tmf_spread_bucket = "normal_2_5"
    else:
        tmf_spread_bucket = "wide_gt_5"

    return {
        "time_bucket": time_bucket,
        "txf_move_bucket": txf_move_bucket,
        "tmf_spread_bucket": tmf_spread_bucket,
    }


def annotate_regimes(
    rows: Sequence[dict[str, Any]],
    *,
    horizons_minutes: Sequence[int] = DEFAULT_HORIZONS_MINUTES,
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for row in rows:
        out = dict(row)
        out.update(classify_market_regime(out, horizons_minutes=horizons_minutes))
        annotated.append(out)
    return annotated


def _horizon_stats(values: Sequence[float], *, min_events: int) -> dict[str, Any]:
    if not values:
        return {
            "events": 0,
            "mean_net_pts": None,
            "median_net_pts": None,
            "positive_ratio": None,
            "remove_best_mean_net_pts": None,
            "survives_remove_best": False,
        }
    sorted_values = sorted(float(v) for v in values)
    remove_best = sorted_values[:-1]
    remove_best_mean = round(float(np.mean(remove_best)), 10) if remove_best else None
    median = round(float(np.median(sorted_values)), 10)
    positive_ratio = round(float(sum(v > 0.0 for v in sorted_values) / len(sorted_values)), 10)
    survives = (
        len(sorted_values) >= min_events
        and remove_best_mean is not None
        and remove_best_mean > 0.0
        and median > 0.0
        and positive_ratio >= 0.5
    )
    return {
        "events": len(sorted_values),
        "mean_net_pts": round(float(np.mean(sorted_values)), 10),
        "median_net_pts": median,
        "positive_ratio": positive_ratio,
        "remove_best_mean_net_pts": remove_best_mean,
        "survives_remove_best": survives,
    }


def regime_split_scorecard(
    rows: Sequence[dict[str, Any]],
    *,
    dimension: str,
    horizons_minutes: Sequence[int] = DEFAULT_HORIZONS_MINUTES,
    min_events: int = 5,
) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        branch = str(row.get("branch"))
        if branch not in CANDIDATE_BRANCHES:
            continue
        regime = str(row.get(dimension, "missing"))
        group = groups.setdefault(regime, {"events": 0, "unique_dates": set(), "branches": {}})
        group["events"] += 1
        group["unique_dates"].add(str(row.get("date")))
        branch_data = group["branches"].setdefault(branch, {"events": 0, "unique_dates": set(), "horizons": {}})
        branch_data["events"] += 1
        branch_data["unique_dates"].add(str(row.get("date")))
        for horizon in horizons_minutes:
            value = row.get(f"label_{horizon}m_net_pts")
            if value is not None:
                branch_data["horizons"].setdefault(f"{horizon}m", []).append(float(value))

    cleaned: dict[str, Any] = {}
    for regime, group in sorted(groups.items()):
        branches: dict[str, Any] = {}
        for branch, branch_data in sorted(group["branches"].items()):
            branches[branch] = {
                "events": int(branch_data["events"]),
                "unique_dates": len(branch_data["unique_dates"]),
                "horizons": {
                    f"{horizon}m": _horizon_stats(
                        branch_data["horizons"].get(f"{horizon}m", []),
                        min_events=min_events,
                    )
                    for horizon in horizons_minutes
                },
            }
        cleaned[regime] = {
            "events": int(group["events"]),
            "unique_dates": len(group["unique_dates"]),
            "branches": branches,
        }
    return {
        "dimension": dimension,
        "min_events": min_events,
        "groups": cleaned,
    }


def _surviving_cells(scorecards: dict[str, Any]) -> list[dict[str, Any]]:
    survivors: list[dict[str, Any]] = []
    for dimension, scorecard in scorecards.items():
        for regime, group in scorecard["groups"].items():
            for branch, branch_data in group["branches"].items():
                for horizon, stats in branch_data["horizons"].items():
                    if stats["survives_remove_best"]:
                        survivors.append(
                            {
                                "dimension": dimension,
                                "regime": regime,
                                "branch": branch,
                                "horizon": horizon,
                                **stats,
                            }
                        )
    return survivors


def build_report(
    *,
    in_path: Path = DEFAULT_IN_PATH,
    horizons_minutes: Sequence[int] = DEFAULT_HORIZONS_MINUTES,
    min_events: int = 5,
) -> dict[str, Any]:
    labeled = json.loads(in_path.read_text(encoding="utf-8"))
    full_rows = labeled["full_rows"]
    annotated = annotate_regimes(full_rows, horizons_minutes=horizons_minutes)
    candidate_rows = [row for row in annotated if row.get("branch") in CANDIDATE_BRANCHES]
    scorecards = {
        dimension: regime_split_scorecard(
            candidate_rows,
            dimension=dimension,
            horizons_minutes=horizons_minutes,
            min_events=min_events,
        )
        for dimension in REGIME_DIMENSIONS
    }
    survivors = _surviving_cells(scorecards)
    return {
        "schema": "research.t1g_extreme_imbalance_regime_review.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "candidate": "t1g_txf_extreme_imbalance_reversal_momentum_v0",
        "input_artifact": str(in_path),
        "review_type": "hypothesis_review_read_only_regime_split",
        "regime_policy": {
            "thresholds_retuned": False,
            "dimensions": list(REGIME_DIMENSIONS),
            "time_buckets": ["opening_0_60m", "mid_60_180m", "late_180m_plus"],
            "txf_move_buckets": [
                "large_down_le_-100",
                "down_-100_0",
                "flat_0",
                "up_0_100",
                "large_up_ge_100",
            ],
            "tmf_spread_buckets": ["tight_le_2", "normal_2_5", "wide_gt_5", "missing_entry_spread"],
            "survival_rule": (
                "events >= min_events, median > 0, positive_ratio >= 0.5, "
                "and remove_best_mean_net_pts > 0"
            ),
            "min_events": min_events,
        },
        "coverage": {
            "input_rows": len(full_rows),
            "candidate_rows": len(candidate_rows),
            "candidate_rows_with_any_label": sum(
                any(row.get(f"label_{horizon}m_net_pts") is not None for horizon in horizons_minutes)
                for row in candidate_rows
            ),
            "unique_candidate_dates": len({str(row.get("date")) for row in candidate_rows}),
        },
        "scorecards": scorecards,
        "surviving_cells": survivors,
        "interpretation": (
            "No robust regime-specific mechanism survived the pre-registered remove-best rule."
            if not survivors
            else "Some descriptive regime cells survive remove-best and require fresh OOS before any spec."
        ),
        "production_behavior_changed": False,
        "cost_model_changed": False,
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in-path", default=str(DEFAULT_IN_PATH))
    parser.add_argument("--out-path", default=str(DEFAULT_OUT_PATH))
    parser.add_argument("--horizons-minutes", default="5,15,30")
    parser.add_argument("--min-events", type=int, default=5)
    args = parser.parse_args(argv)

    horizons = tuple(int(v.strip()) for v in args.horizons_minutes.split(",") if v.strip())
    report = build_report(
        in_path=Path(args.in_path),
        horizons_minutes=horizons,
        min_events=args.min_events,
    )
    out_path = Path(args.out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**report, "scorecards": "omitted from stdout"}, indent=2))


if __name__ == "__main__":
    main()
