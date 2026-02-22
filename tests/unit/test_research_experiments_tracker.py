from pathlib import Path

from research.experiments.tracker import ExperimentTracker, ensure_experiment_layout


def test_ensure_experiment_layout_and_tracker(tmp_path: Path):
    root = ensure_experiment_layout(tmp_path / "research" / "experiments")
    assert (root / "runs").exists()
    assert (root / "comparisons").exists()

    tracker = ExperimentTracker(base_dir=root)
    assert tracker.base_dir == root
