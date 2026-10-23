"""scripts/experiment_gc.py: dry-run by default; deletes only array payloads, never trial metadata."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_OLD_MTIME = 1_000_000  # 1970-01-12: older than any --keep-days cutoff


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "experiment_gc.py"
    spec = importlib.util.spec_from_file_location("experiment_gc", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gc = _load_module()

_METADATA = ("meta.json", "scorecard.json", "run_manifest.json", "trials.jsonl", "split_access.jsonl")
_PAYLOADS = ("signals.npy", "equity.npy", "dataset.npz")


def _make_run(runs_root: Path, name: str, alpha_id: str, *, old: bool) -> Path:
    run = runs_root / name
    run.mkdir(parents=True)
    (run / "meta.json").write_text(json.dumps({"alpha_id": alpha_id}), encoding="utf-8")
    for meta in _METADATA[1:]:
        (run / meta).write_text("{}", encoding="utf-8")
    for payload in _PAYLOADS:
        (run / payload).write_bytes(b"\0" * 64)
    if old:
        for f in [*run.iterdir(), run]:
            os.utime(f, (_OLD_MTIME, _OLD_MTIME))
    return run


@pytest.fixture
def roots(tmp_path):
    runs_root = tmp_path / "runs"
    validations_root = tmp_path / "validations"
    validations_root.mkdir()
    # Two old runs plus one recent run for the same alpha; --keep-latest 1
    # protects the recent one, so both old runs are eligible.
    old_a = _make_run(runs_root, "run-a", "alpha_x", old=True)
    old_b = _make_run(runs_root, "run-b", "alpha_x", old=True)
    recent = _make_run(runs_root, "run-c", "alpha_x", old=False)
    return runs_root, validations_root, (old_a, old_b, recent)


def _argv(runs_root: Path, validations_root: Path, *extra: str) -> list[str]:
    return [
        "--keep-days",
        "1",
        "--keep-latest",
        "1",
        "--runs-root",
        str(runs_root),
        "--validations-root",
        str(validations_root),
        *extra,
    ]


def test_experiment_gc_default_deletes_nothing(roots):
    runs_root, validations_root, runs = roots

    assert gc.main(_argv(runs_root, validations_root)) == 0

    for run in runs:
        for name in (*_METADATA, *_PAYLOADS):
            assert (run / name).exists(), f"{run.name}/{name} deleted without --apply"


def test_experiment_gc_apply_deletes_only_payloads_of_eligible_runs(roots):
    runs_root, validations_root, (old_a, old_b, recent) = roots

    assert gc.main(_argv(runs_root, validations_root, "--apply")) == 0

    for run in (old_a, old_b):
        assert run.is_dir()
        for name in _METADATA:
            assert (run / name).exists(), f"trial metadata {run.name}/{name} was deleted"
        for name in _PAYLOADS:
            assert not (run / name).exists(), f"payload {run.name}/{name} survived --apply"
    for name in (*_METADATA, *_PAYLOADS):
        assert (recent / name).exists(), "protected latest run was touched"


def test_experiment_gc_apply_keeps_run_countable_as_a_trial(roots):
    runs_root, validations_root, _ = roots

    gc.main(_argv(runs_root, validations_root, "--apply"))

    # The run's alpha attribution must still resolve after GC.
    assert len(gc._collect_runs(runs_root)["alpha_x"]) == 3


def test_experiment_gc_rejects_apply_with_dry_run(roots):
    runs_root, validations_root, runs = roots

    assert gc.main(_argv(runs_root, validations_root, "--apply", "--dry-run")) == 2
    assert all((run / "signals.npy").exists() for run in runs)
