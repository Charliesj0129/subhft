"""Behavior tests for scripts/benchmark_gate.py (Darwin Gate regression checker)."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.benchmark_gate import check_regressions, main


def _bench_json(path: Path, means: dict[str, float]) -> Path:
    payload = {"benchmarks": [{"name": name, "stats": {"mean": mean}} for name, mean in means.items()]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_uniform_runner_slowdown_passes():
    baseline = {"a": 0.000016, "b": 0.000017, "c": 0.000001, "d": 0.000011}
    current = {name: mean * 1.45 for name, mean in baseline.items()}

    regressions, speed_factor = check_regressions(baseline, current, threshold=0.10)

    assert regressions == []
    assert abs(speed_factor - 1.45) < 1e-9


def test_single_path_regression_flagged_despite_runner_shift():
    baseline = {"a": 0.000016, "b": 0.000017, "c": 0.000001, "d": 0.000011, "e": 0.000020}
    current = {name: mean * 1.30 for name, mean in baseline.items()}
    current["c"] = baseline["c"] * 1.30 * 1.50  # +50% beyond the runner shift

    regressions, _ = check_regressions(baseline, current, threshold=0.10)

    assert [name for name, *_ in regressions] == ["c"]
    _, _, _, normalized, raw = regressions[0]
    assert abs(normalized - 0.50) < 1e-6
    assert abs(raw - 0.95) < 1e-6


def test_uniform_catastrophic_slowdown_still_fails():
    baseline = {"a": 0.000016, "b": 0.000017, "c": 0.000001}
    current = {name: mean * 3.5 for name, mean in baseline.items()}  # raw +250%, normalized 0%

    regressions, _ = check_regressions(baseline, current, threshold=0.10)

    assert sorted(name for name, *_ in regressions) == ["a", "b", "c"]


def test_improvement_and_flat_benchmarks_pass():
    baseline = {"a": 0.000016, "b": 0.000017, "c": 0.000011}
    current = {"a": 0.000016, "b": 0.000009, "c": 0.000011}

    regressions, _ = check_regressions(baseline, current, threshold=0.10)

    assert regressions == []


def test_missing_and_zero_baseline_entries_ignored():
    baseline = {"gone": 0.000016, "zero": 0.0, "kept": 0.000010}
    current = {"kept": 0.000010, "new": 0.000005}

    regressions, speed_factor = check_regressions(baseline, current, threshold=0.10)

    assert regressions == []
    assert speed_factor == 1.0


def test_main_exit_codes_and_normalization_end_to_end(tmp_path: Path, capsys):
    baseline = _bench_json(tmp_path / "base.json", {"a": 0.000016, "b": 0.000017, "c": 0.000011})
    uniform = _bench_json(tmp_path / "uniform.json", {"a": 0.000016 * 1.4, "b": 0.000017 * 1.4, "c": 0.000011 * 1.4})
    regressed = _bench_json(
        tmp_path / "regressed.json", {"a": 0.000016 * 1.4, "b": 0.000017 * 1.4, "c": 0.000011 * 2.8}
    )

    assert main(["--baseline", str(baseline), "--current", str(uniform)]) == 0
    assert "PASSED" in capsys.readouterr().out

    assert main(["--baseline", str(baseline), "--current", str(regressed)]) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "c" in out


def test_main_skips_when_baseline_absent(tmp_path: Path, capsys):
    current = _bench_json(tmp_path / "cur.json", {"a": 0.000016})

    assert main(["--baseline", str(tmp_path / "missing.json"), "--current", str(current)]) == 0
    assert "skipping" in capsys.readouterr().out
