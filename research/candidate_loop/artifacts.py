"""Hive-style Parquet artifact tree (spec §9).

Root ``research/candidate_loop/artifacts/`` (gitignored).  Every directory
level carries its version so nothing is ever overwritten across versions;
``experiment_results.artifact_path`` points at the ``split=<S>/`` directory.

Written directly with pyarrow (no pandas round-trip needed for these small
per-candidate frames).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from research.candidate_loop.evaluator import DayEval

DEFAULT_ARTIFACT_ROOT = Path("research/candidate_loop/artifacts")


def panel_cache_dir(root: Path, data_version: str) -> Path:
    """Shared primitive panel cache: ``artifacts/panels/data_version=<V>/``."""
    return root / "panels" / f"data_version={data_version}"


def split_artifact_dir(
    root: Path, data_version: str, evaluator_version: str, alpha_id: str, split: str
) -> Path:
    return (
        root
        / f"data_version={data_version}"
        / f"evaluator_version={evaluator_version}"
        / f"alpha_id={alpha_id}"
        / f"split={split}"
    )


def _write_parquet(path: Path, columns: dict[str, list[Any]]) -> None:
    pq.write_table(pa.table(columns), str(path))


def write_split_artifacts(
    out_dir: Path,
    day_evals: list[DayEval],
    metrics: dict[str, Any],
    diagnostics: dict[str, Any],
) -> Path:
    """Write the §9 per-split artifact set; returns ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    used = [d for d in day_evals if not d.skipped_reason]

    _write_parquet(
        out_dir / "day_metrics.parquet",
        {
            "day": [d.day for d in day_evals],
            "symbol": [d.symbol for d in day_evals],
            "skipped_reason": [d.skipped_reason for d in day_evals],
            "n_valid": [d.n_valid for d in day_evals],
            "counts_for_stats": [d.counts_for_stats for d in day_evals],
            "signal_std": [d.signal_std for d in day_evals],
            "ic": [d.ic for d in day_evals],
            "rank_ic": [d.rank_ic for d in day_evals],
            "flips": [d.flips for d in day_evals],
            "median_spread_pts": [d.median_spread_pts for d in day_evals],
        },
    )
    _write_parquet(
        out_dir / "regime_metrics.parquet",
        {
            "day": [d.day for d in used],
            "regime_ic_out": [d.regime_ic_out for d in used],
            "tight_ic": [d.tight_ic for d in used],
            "wide_ic": [d.wide_ic for d in used],
        },
    )
    decay_rows = [(d.day, mult, ic) for d in used for mult, ic in sorted(d.decay_ics.items())]
    _write_parquet(
        out_dir / "horizon_decay.parquet",
        {
            "day": [r[0] for r in decay_rows],
            "multiplier": [r[1] for r in decay_rows],
            "ic": [r[2] for r in decay_rows],
        },
    )
    latency_rows = [(d.day, delta, ic) for d in used for delta, ic in sorted(d.latency_ics.items())]
    _write_parquet(
        out_dir / "latency_stress.parquet",
        {
            "day": [r[0] for r in latency_rows],
            "delta_ms": [r[1] for r in latency_rows],
            "ic": [r[2] for r in latency_rows],
        },
    )
    bucket_rows = [
        (d.day, b, float(d.bucket_sums_pts[b]), int(d.bucket_counts[b]))
        for d in used
        if d.bucket_counts.size
        for b in range(d.bucket_counts.size)
    ]
    _write_parquet(
        out_dir / "signal_bucket_returns.parquet",
        {
            "day": [r[0] for r in bucket_rows],
            "bucket": [r[1] for r in bucket_rows],
            "sum_fwd_pts": [r[2] for r in bucket_rows],
            "count": [r[3] for r in bucket_rows],
        },
    )
    payload = {"metrics": metrics, "diagnostics": diagnostics}
    (out_dir / "diagnostics.json").write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return out_dir


__all__ = [
    "DEFAULT_ARTIFACT_ROOT",
    "panel_cache_dir",
    "split_artifact_dir",
    "write_split_artifacts",
]
