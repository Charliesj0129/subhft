"""Formal rescue test for the T1-G normal-spread / extreme-low-imbalance-reversal / 30m cell.

Consumes a regime_review-style annotated cell (tmf_spread_bucket=="normal_2_5",
branch=="extreme_low_imbalance_reversal", horizon=="30m") and applies this
candidate's own pre-registered rescue rule (same shape as
t1g_hypothesis_review.py's RESCUE_MIN_MEAN/RESCUE_MIN_N/RESCUE_MIN_POSFRAC
two-stage gate: full-sample gate, then OOS-dated slice mean confirmation).
Does not tune thresholds, create orders, or modify production behavior.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from research.experiments.validations.t1g_extreme_imbalance_v0.regime_review import annotate_regimes

DEFAULT_LABELED_DIAGNOSTIC_PATH = Path(
    "research/experiments/validations/t1g_extreme_imbalance_v0/labeled_diagnostic_iteration25_g6_extend.json"
)
TARGET_BRANCH = "extreme_low_imbalance_reversal"
TARGET_SPREAD_BUCKET = "normal_2_5"
TARGET_HORIZON_LABEL = "label_30m_net_pts"

# Matches the June-forward OOS slice this candidate already established in
# june_oos_cell_summary_iteration23/24_*.json ("predeclared_cell" restricted
# to TXFF6/June dates) for this exact cell.
OOS_DATE_CUTOFF = "2026-06-01"

# Same rescue-rule shape/thresholds as t1g_hypothesis_review.py's RESCUE_MIN_*.
RESCUE_MIN_MEAN = 10.0
RESCUE_MIN_N = 20
RESCUE_MIN_POSFRAC = 0.5


def load_full_rows(path: Path = DEFAULT_LABELED_DIAGNOSTIC_PATH) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = data["full_rows"]
    return rows


def filter_target_cell(annotated_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in annotated_rows
        if row.get("branch") == TARGET_BRANCH
        and row.get("tmf_spread_bucket") == TARGET_SPREAD_BUCKET
        and row.get(TARGET_HORIZON_LABEL) is not None
    ]


def score(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "events": 0,
            "mean_net_pts": None,
            "median_net_pts": None,
            "positive_fraction": None,
        }
    arr = np.asarray(values, dtype=np.float64)
    return {
        "events": len(values),
        "mean_net_pts": round(float(arr.mean()), 4),
        "median_net_pts": round(float(np.median(arr)), 4),
        "positive_fraction": round(float((arr > 0.0).mean()), 4),
    }


def evaluate_rescue(cell_rows: list[dict[str, Any]]) -> dict[str, Any]:
    full_values = [float(row[TARGET_HORIZON_LABEL]) for row in cell_rows]
    oos_values = [float(row[TARGET_HORIZON_LABEL]) for row in cell_rows if str(row.get("date", "")) >= OOS_DATE_CUTOFF]

    full_score = score(full_values)
    oos_score = score(oos_values)

    full_gate_passes = (
        full_score["events"] >= RESCUE_MIN_N
        and full_score["mean_net_pts"] is not None
        and full_score["mean_net_pts"] >= RESCUE_MIN_MEAN
        and full_score["positive_fraction"] is not None
        and full_score["positive_fraction"] >= RESCUE_MIN_POSFRAC
    )
    oos_confirms = (
        full_gate_passes and oos_score["mean_net_pts"] is not None and oos_score["mean_net_pts"] >= RESCUE_MIN_MEAN
    )

    if oos_confirms:
        verdict = "RESCUED"
    elif full_gate_passes:
        verdict = "FULL_SAMPLE_GATE_PASSES_OOS_UNCONFIRMED"
    else:
        verdict = "NOT_RESCUED"

    data_starved = oos_score["events"] < RESCUE_MIN_N

    return {
        "candidate": "t1g_txf_extreme_imbalance_reversal_momentum_v0",
        "target_cell": {
            "tmf_spread_bucket": TARGET_SPREAD_BUCKET,
            "branch": TARGET_BRANCH,
            "horizon": "30m",
        },
        "rescue_rule": (
            f"full-sample mean_net_pts>={RESCUE_MIN_MEAN} AND N>={RESCUE_MIN_N} "
            f"AND positive_fraction>={RESCUE_MIN_POSFRAC}, then OOS(date>={OOS_DATE_CUTOFF}) "
            f"mean_net_pts>={RESCUE_MIN_MEAN}"
        ),
        "full_sample": full_score,
        "oos_dated_slice": oos_score,
        "full_sample_gate_passes": full_gate_passes,
        "oos_confirms": oos_confirms,
        "verdict": verdict,
        "data_starved": data_starved,
        "data_starved_note": (
            f"OOS-dated slice N={oos_score['events']} is below RESCUE_MIN_N={RESCUE_MIN_N}; "
            "any verdict from this slice is not statistically meaningful, regardless of sign."
            if data_starved
            else None
        ),
    }


def main() -> None:
    full_rows = load_full_rows()
    annotated = annotate_regimes(full_rows, horizons_minutes=(5, 15, 30))
    cell_rows = filter_target_cell(annotated)
    result = evaluate_rescue(cell_rows)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
