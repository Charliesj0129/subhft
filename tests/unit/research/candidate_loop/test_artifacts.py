"""§9 Parquet artifact tree: hive paths, file set, content round-trip."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from research.candidate_loop.artifacts import (
    panel_cache_dir,
    split_artifact_dir,
    write_split_artifacts,
)
from research.candidate_loop.evaluator import DayEval


def _day(day: str, *, skipped: str = "") -> DayEval:
    d = DayEval(day=day, symbol="TXFD6", skipped_reason=skipped)
    if skipped:
        return d
    d.n_valid = 1500
    d.counts_for_stats = True
    d.signal_std = 0.2
    d.ic = 0.4
    d.rank_ic = 0.35
    d.latency_ics = {0: 0.4, 1: 0.2}
    d.decay_ics = {1.0: 0.4, 2.0: 0.1}
    d.bucket_sums_pts = np.array([-3.0, -1.0, 0.0, 1.0, 3.0])
    d.bucket_counts = np.array([300, 300, 300, 300, 300], dtype=np.int64)
    d.flips = 12
    d.median_spread_pts = 1.0
    return d


class TestPaths:
    def test_hive_layout(self) -> None:
        root = Path("artifacts")
        assert panel_cache_dir(root, "dv1") == root / "panels" / "data_version=dv1"
        assert split_artifact_dir(root, "dv1", "eval_v1", "abc123", "train") == (
            root / "data_version=dv1" / "evaluator_version=eval_v1" / "alpha_id=abc123" / "split=train"
        )


class TestWriteSplitArtifacts:
    def test_writes_full_file_set(self, tmp_path: Path) -> None:
        days = [_day("2026-04-13"), _day("2026-04-14"), _day("2026-04-15", skipped="dir_dirty")]
        out = write_split_artifacts(
            tmp_path / "split=train",
            days,
            metrics={"ic": 0.4, "gates_failed": []},
            diagnostics={"effective_day_count": 2},
        )
        for name in (
            "day_metrics.parquet",
            "regime_metrics.parquet",
            "horizon_decay.parquet",
            "latency_stress.parquet",
            "signal_bucket_returns.parquet",
            "diagnostics.json",
        ):
            assert (out / name).exists(), name

    def test_day_metrics_include_skipped_days(self, tmp_path: Path) -> None:
        days = [_day("2026-04-13"), _day("2026-04-15", skipped="dir_dirty")]
        out = write_split_artifacts(tmp_path, days, metrics={}, diagnostics={})
        table = pq.read_table(out / "day_metrics.parquet")
        assert table.num_rows == 2
        assert table.column("skipped_reason").to_pylist() == ["", "dir_dirty"]

    def test_used_only_frames_exclude_skipped_days(self, tmp_path: Path) -> None:
        days = [_day("2026-04-13"), _day("2026-04-15", skipped="dir_dirty")]
        out = write_split_artifacts(tmp_path, days, metrics={}, diagnostics={})
        latency = pq.read_table(out / "latency_stress.parquet")
        assert set(latency.column("day").to_pylist()) == {"2026-04-13"}
        buckets = pq.read_table(out / "signal_bucket_returns.parquet")
        assert buckets.num_rows == 5

    def test_diagnostics_json_round_trip(self, tmp_path: Path) -> None:
        import json

        out = write_split_artifacts(
            tmp_path, [_day("2026-04-13")], metrics={"ic": 0.4}, diagnostics={"versions": {"e": "eval_v1"}}
        )
        payload = json.loads((out / "diagnostics.json").read_text())
        assert payload["metrics"]["ic"] == 0.4
        assert payload["diagnostics"]["versions"]["e"] == "eval_v1"
