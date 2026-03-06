from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "release_converge.py"
    spec = importlib.util.spec_from_file_location("release_converge", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_main_scan_only_generates_report(tmp_path: Path) -> None:
    mod = _load_module()
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")

    rc = mod.main(
        [
            "--project-root",
            str(tmp_path),
            "--output-dir",
            "outputs/release_converge",
            "--skip-clean",
            "--skip-gate",
        ]
    )
    assert rc == 0

    latest = tmp_path / "outputs/release_converge/latest.json"
    assert latest.exists()
    payload = json.loads(latest.read_text(encoding="utf-8"))
    assert payload["result"]["overall"] == "pass"
    assert payload["mode"]["skip_clean"] is True
    assert payload["mode"]["skip_gate"] is True
    assert payload["skills_used"]
    assert payload["roles_used"]


def test_main_fails_when_cleanup_step_fails(monkeypatch, tmp_path: Path) -> None:
    mod = _load_module()
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")

    def _bad_steps(*, clean_rust: bool, cleanup_flags: dict[str, bool]):
        return [
            {
                "step": "bad",
                "command": "exit 9",
                "destructive": True,
                "targets": [],
                "risk": "low",
            }
        ]

    monkeypatch.setattr(mod, "_cleanup_steps", _bad_steps)

    rc = mod.main(
        [
            "--project-root",
            str(tmp_path),
            "--output-dir",
            "outputs/release_converge",
            "--skip-gate",
        ]
    )
    assert rc == 2
    payload = json.loads((tmp_path / "outputs/release_converge/latest.json").read_text(encoding="utf-8"))
    assert payload["cleanup_status"] == "fail"
    assert payload["result"]["overall"] == "fail"


def test_main_dry_run_keeps_cleanup_targets(tmp_path: Path) -> None:
    mod = _load_module()
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "outputs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "outputs" / "keep.txt").write_text("keep\n", encoding="utf-8")

    rc = mod.main(
        [
            "--project-root",
            str(tmp_path),
            "--output-dir",
            "outputs/release_converge",
            "--skip-gate",
            "--dry-run",
            "--clean-outputs",
        ]
    )
    assert rc == 0
    assert (tmp_path / "outputs" / "keep.txt").exists()

    payload = json.loads((tmp_path / "outputs/release_converge/latest.json").read_text(encoding="utf-8"))
    assert payload["mode"]["dry_run"] is True
    clean_outputs_rows = [row for row in payload["cleanup_steps"] if row.get("step") == "clean_outputs"]
    assert clean_outputs_rows
    assert clean_outputs_rows[0]["returncode"] == 0
    assert clean_outputs_rows[0]["dry_run"] is True
    assert clean_outputs_rows[0]["skipped"] is True


def test_main_guard_no_tracked_path_blocks_cleanup(tmp_path: Path) -> None:
    mod = _load_module()
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    tracked_file = tmp_path / "outputs" / "tracked.txt"
    tracked_file.parent.mkdir(parents=True, exist_ok=True)
    tracked_file.write_text("tracked\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "README.md", "outputs/tracked.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=tmp_path, check=True, capture_output=True)

    rc = mod.main(
        [
            "--project-root",
            str(tmp_path),
            "--output-dir",
            "outputs/release_converge",
            "--skip-gate",
            "--clean-outputs",
            "--guard-no-tracked-path",
            "outputs",
        ]
    )
    assert rc == 2
    assert tracked_file.exists() is True

    payload = json.loads((tmp_path / "outputs/release_converge/latest.json").read_text(encoding="utf-8"))
    assert payload["cleanup_status"] == "fail"
    guard_rows = [row for row in payload["cleanup_steps"] if str(row.get("step", "")).startswith("guard_no_tracked_path:")]
    assert guard_rows
    assert any(int(row.get("returncode", 0)) != 0 for row in guard_rows)
    blocked_rows = [row for row in payload["cleanup_steps"] if row.get("step") == "clean_outputs"]
    assert blocked_rows
    assert blocked_rows[0]["blocked_by_guard"] is True


def test_safe_prune_untracked_keeps_tracked_files(tmp_path: Path) -> None:
    mod = _load_module()

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=tmp_path, check=True, capture_output=True)

    tracked_keep = tmp_path / ".benchmarks" / ".gitkeep"
    tracked_keep.parent.mkdir(parents=True, exist_ok=True)
    tracked_keep.write_text("", encoding="utf-8")
    tracked_prof = tmp_path / "tracked.prof"
    tracked_prof.write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", ".benchmarks/.gitkeep", "tracked.prof"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / ".benchmarks" / "junk.bin").write_bytes(b"junk")
    (tmp_path / ".coverage").write_text("x", encoding="utf-8")
    (tmp_path / "untracked.prof").write_text("u\n", encoding="utf-8")

    tracked = mod._tracked_paths(tmp_path)
    tracked_dirs = mod._tracked_dirs(tracked)
    summary = mod._safe_prune_untracked(tmp_path, tracked=tracked, tracked_dirs=tracked_dirs)

    assert summary["removed_files"] >= 2
    assert (tmp_path / ".benchmarks" / "junk.bin").exists() is False
    assert (tmp_path / ".coverage").exists() is False
    assert (tmp_path / "untracked.prof").exists() is False
    assert tracked_keep.exists() is True
    assert tracked_prof.exists() is True
