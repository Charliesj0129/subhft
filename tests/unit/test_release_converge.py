from __future__ import annotations

import importlib.util
import json
import re
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

    def _bad_steps(
        *,
        clean_rust: bool,
        cleanup_flags: dict[str, bool],
        cleanup_profile: str,
        seed_minimal_sample: bool,
        tracked_slimming_profile: str,
    ):
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
    guard_rows = [
        row for row in payload["cleanup_steps"] if str(row.get("step", "")).startswith("guard_no_tracked_path:")
    ]
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
    subprocess.run(
        ["git", "add", ".benchmarks/.gitkeep", "tracked.prof"], cwd=tmp_path, check=True, capture_output=True
    )
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


def test_resolve_cleanup_flags_mvp_release_defaults() -> None:
    mod = _load_module()
    flags = mod._resolve_cleanup_flags(
        cleanup_profile=mod.CLEANUP_PROFILE_MVP_RELEASE,
        clean_outputs=False,
        clean_reports=False,
        clean_state=False,
        clean_venv=False,
        clean_wal=False,
        clean_data=False,
    )
    assert flags["clean_outputs"] is True
    assert flags["clean_reports"] is True
    assert flags["clean_state"] is True
    assert flags["clean_wal"] is True
    assert flags["clean_data"] is True
    assert flags["clean_venv"] is False


def test_slim_tracked_root_reports_removes_non_keep(tmp_path: Path) -> None:
    mod = _load_module()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=tmp_path, check=True, capture_output=True)

    base = tmp_path / "research" / "knowledge" / "reports" / "root_reports"
    base.mkdir(parents=True, exist_ok=True)
    keep_files = [
        "README.md",
        "pyspy_hotspot_triage.md",
        "e2e_latency.summary.json",
        "latency_e2e.json",
    ]
    for name in keep_files:
        (base / name).write_text("keep\n", encoding="utf-8")
    removable = base / "old_profile.svg"
    removable.write_text("old\n", encoding="utf-8")

    subprocess.run(
        ["git", "add", "research/knowledge/reports/root_reports"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(["git", "commit", "-m", "seed"], cwd=tmp_path, check=True, capture_output=True)

    tracked = mod._tracked_paths(tmp_path)
    summary = mod._slim_tracked_root_reports(
        root=tmp_path,
        tracked=tracked,
        output_dir=tmp_path / "outputs" / "release_converge",
        keep_rel_paths=set(mod.ROOT_REPORTS_MINIMAL_KEEP_PATHS),
        dry_run=False,
    )
    assert summary["removed_files"] == 1
    assert removable.exists() is False
    for name in keep_files:
        assert (base / name).exists() is True
    assert Path(summary["manifest"]).exists() is True


def test_gitignore_contract_keeps_latency_profile_and_smoke_sample() -> None:
    gitignore = (Path(__file__).resolve().parents[2] / ".gitignore").read_text(encoding="utf-8")
    assert "!research/tools/latency_profiles.py" in gitignore
    assert "!research/data/processed/smoke/smoke_v1.npy" in gitignore
    assert "!research/data/processed/smoke/smoke_v1.npy.meta.json" in gitignore


def _project_with_wal(tmp_path: Path) -> Path:
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    wal = tmp_path / ".wal"
    wal.mkdir()
    (wal / "segment-000001.wal").write_text("unreplayed\n", encoding="utf-8")
    (wal / "dlq").mkdir()
    (wal / "dlq" / "order.json").write_text("{}\n", encoding="utf-8")
    return tmp_path


def _skip_repo_tooling(monkeypatch, mod) -> None:
    """A temp project has no Makefile or research package, so drop the two steps that need them."""
    real = mod._cleanup_steps

    def _steps(**kwargs):
        return [row for row in real(**kwargs) if row["step"] not in {"make_clean", "research_clean"}]

    monkeypatch.setattr(mod, "_cleanup_steps", _steps)


def _run(mod, root: Path, *extra: str) -> tuple[int, dict]:
    rc = mod.main(["--project-root", str(root), "--output-dir", "outputs/release_converge", "--skip-gate", *extra])
    payload = json.loads((root / "outputs/release_converge/latest.json").read_text(encoding="utf-8"))
    return rc, payload


def test_clean_wal_is_blocked_when_the_directory_holds_files(tmp_path: Path) -> None:
    mod = _load_module()
    root = _project_with_wal(tmp_path)

    rc, payload = _run(mod, root, "--clean-wal")

    assert rc == 2
    assert (root / ".wal" / "segment-000001.wal").exists()
    assert (root / ".wal" / "dlq" / "order.json").exists()
    guard = [row for row in payload["cleanup_steps"] if row.get("step") == "guard_untracked_state:.wal"]
    assert len(guard) == 1 and guard[0]["returncode"] == 3
    assert "--allow-delete-wal" in guard[0]["stderr"]
    assert "segment-000001.wal" in guard[0]["stdout"]
    step = [row for row in payload["cleanup_steps"] if row.get("step") == "clean_wal"]
    assert step and step[0]["blocked_by_guard"] is True and step[0]["returncode"] == 3


def test_a_blocked_wal_stops_the_other_cleanup_steps_too(tmp_path: Path) -> None:
    mod = _load_module()
    root = _project_with_wal(tmp_path)
    (root / "reports").mkdir()
    (root / "reports" / "keep.txt").write_text("keep\n", encoding="utf-8")

    rc, payload = _run(mod, root, "--clean-wal", "--clean-reports")

    assert rc == 2
    assert (root / "reports" / "keep.txt").exists()
    reports_step = [row for row in payload["cleanup_steps"] if row.get("step") == "clean_reports"]
    assert reports_step and reports_step[0].get("skipped") is True


def test_allow_delete_wal_lets_the_cleanup_remove_it(monkeypatch, tmp_path: Path) -> None:
    mod = _load_module()
    _skip_repo_tooling(monkeypatch, mod)
    root = _project_with_wal(tmp_path)

    rc, payload = _run(mod, root, "--clean-wal", "--allow-delete-wal")

    assert rc == 0
    assert not (root / ".wal").exists()
    guard = [row for row in payload["cleanup_steps"] if row.get("step") == "guard_untracked_state:.wal"]
    assert guard and guard[0]["returncode"] == 0
    assert "allowed" in guard[0]["stdout"]


def test_allow_delete_wal_does_not_unlock_the_data_directory(tmp_path: Path) -> None:
    mod = _load_module()
    root = _project_with_wal(tmp_path)
    (root / "data").mkdir()
    (root / "data" / "ticks.parquet").write_text("rows\n", encoding="utf-8")

    rc, _ = _run(mod, root, "--clean-wal", "--clean-data", "--allow-delete-wal")

    assert rc == 2
    assert (root / "data" / "ticks.parquet").exists()
    assert (root / ".wal" / "segment-000001.wal").exists()  # the whole cleanup stops, not only the data step


def test_clean_data_is_blocked_until_allow_delete_data(monkeypatch, tmp_path: Path) -> None:
    mod = _load_module()
    _skip_repo_tooling(monkeypatch, mod)
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "ticks.parquet").write_text("rows\n", encoding="utf-8")

    rc, _ = _run(mod, tmp_path, "--clean-data")
    assert rc == 2
    assert (tmp_path / "data" / "ticks.parquet").exists()

    rc, _ = _run(mod, tmp_path, "--clean-data", "--allow-delete-data")
    assert rc == 0
    assert not (tmp_path / "data").exists()


def test_an_empty_or_missing_wal_directory_is_not_blocked(monkeypatch, tmp_path: Path) -> None:
    mod = _load_module()
    _skip_repo_tooling(monkeypatch, mod)
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    (tmp_path / ".wal" / "archive").mkdir(parents=True)

    rc, payload = _run(mod, tmp_path, "--clean-wal", "--clean-data")

    assert rc == 0
    assert not (tmp_path / ".wal").exists()
    guards = {
        row["step"]: row for row in payload["cleanup_steps"] if str(row.get("step", "")).startswith("guard_untracked")
    }
    assert set(guards) == {"guard_untracked_state:.wal", "guard_untracked_state:data"}
    assert all(row["returncode"] == 0 for row in guards.values())


def test_the_mvp_profile_cannot_delete_the_wal_without_the_flag(tmp_path: Path) -> None:
    mod = _load_module()
    root = _project_with_wal(tmp_path)

    rc, payload = _run(mod, root, "--cleanup-profile", "mvp_release", "--dry-run")

    assert rc == 2  # a dry run reports what would be blocked
    assert (root / ".wal" / "segment-000001.wal").exists()
    assert any(
        row.get("step") == "guard_untracked_state:.wal" and row["returncode"] == 3 for row in payload["cleanup_steps"]
    )


def test_nothing_is_guarded_when_cleanup_is_skipped(tmp_path: Path) -> None:
    mod = _load_module()
    root = _project_with_wal(tmp_path)

    rc, payload = _run(mod, root, "--skip-clean", "--cleanup-profile", "mvp_release")

    assert rc == 0
    assert not [row for row in payload["cleanup_steps"] if str(row.get("step", "")).startswith("guard_untracked")]
    assert (root / ".wal" / "segment-000001.wal").exists()


def _make_recipe(target: str) -> str:
    text = (Path(__file__).resolve().parents[2] / "Makefile").read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(target)}:.*\n((?:\t.*\n)+)", text, flags=re.MULTILINE)
    assert match is not None, f"no recipe for {target}"
    return match.group(1)


def test_make_release_converge_mvp_is_a_dry_run_unless_confirmed() -> None:
    recipe = _make_recipe("release-converge-mvp")

    assert "$(if $(filter yes,$(CONFIRM)),,--dry-run)" in recipe


def test_make_release_converge_mvp_passes_the_delete_flags_only_when_asked() -> None:
    recipe = _make_recipe("release-converge-mvp")

    assert "$(if $(filter 1,$(ALLOW_DELETE_WAL)),--allow-delete-wal,)" in recipe
    assert "$(if $(filter 1,$(ALLOW_DELETE_DATA)),--allow-delete-data,)" in recipe
    assert "--allow-delete-wal --allow-delete-data" not in recipe
