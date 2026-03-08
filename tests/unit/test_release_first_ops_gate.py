from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "release_first_ops_gate.py"
    spec = importlib.util.spec_from_file_location("release_first_ops_gate", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_parser_supports_operational_args():
    mod = _load_module()
    parser = mod._build_parser()

    args = parser.parse_args(
        [
            "--change-id",
            "CHG-20260306-01",
            "--month",
            "2026-03",
            "--disk-path",
            ".",
            "--min-query-guard-runs",
            "2",
            "--allow-tracked-change-prefix",
            "outputs",
        ]
    )

    assert args.change_id == "CHG-20260306-01"
    assert args.month == "2026-03"
    assert args.disk_path == ["."]
    assert args.min_query_guard_runs == 2
    assert args.allow_tracked_change_prefix == ["outputs"]


def test_main_passes_and_writes_latest_report(monkeypatch, tmp_path: Path):
    mod = _load_module()
    monkeypatch.setenv("HFT_ALPHA_AUDIT_ENABLED", "1")
    monkeypatch.setattr(mod, "_is_git_repo", lambda root: False)

    def _fake_run_shell(command: str, *, cwd: Path, env: dict[str, str] | None = None) -> dict:
        if "release_converge.py" in command:
            _write_json(
                cwd / "outputs" / "release_converge" / "latest.json",
                {"result": {"overall": "pass"}},
            )
        elif "roadmap_delivery_executor.py" in command:
            _write_json(
                cwd / "outputs" / "roadmap_execution" / "summary" / "latest.json",
                {"result": {"overall": "pass"}},
            )
        elif "roadmap_delivery_guard.py" in command:
            _write_json(
                cwd / "outputs" / "roadmap_delivery" / "latest.json",
                {"result": {"overall": "pass"}},
            )
        elif "release_channel_guard.py gate" in command:
            _write_json(
                cwd
                / "outputs"
                / "deploy_guard"
                / "release_channel"
                / "decisions"
                / "release_gate_20260306T010101Z_CHG-20260306-01.json",
                {"result": {"overall": "pass"}},
            )
        elif "reliability_review_pack.py" in command:
            _write_json(
                cwd / "outputs" / "reliability" / "monthly" / "monthly_2026-03_20260306T010101Z.json",
                {"result": {"overall": "pass"}},
            )
        elif "pytest" in command or "ruff check" in command or "mypy" in command:
            pass
        else:
            raise AssertionError(f"unexpected command: {command}")

        return {
            "command": command,
            "returncode": 0,
            "duration_s": 0.01,
            "stdout": "",
            "stderr": "",
            "stdout_tail": "",
            "stderr_tail": "",
        }

    monkeypatch.setattr(mod, "_run_shell", _fake_run_shell)

    rc = mod.main(
        [
            "--project-root",
            str(tmp_path),
            "--output-dir",
            "outputs/release_first_ops",
            "--change-id",
            "CHG-20260306-01",
            "--month",
            "2026-03",
        ]
    )

    assert rc == 0
    latest = tmp_path / "outputs" / "release_first_ops" / "latest.json"
    assert latest.exists()
    payload = json.loads(latest.read_text(encoding="utf-8"))
    assert payload["result"]["overall"] == "pass"
    assert [row["step"] for row in payload["steps"]] == [
        "alpha_audit_enabled",
        "release_converge_no_clean",
        "roadmap_delivery_execute_strict",
        "roadmap_delivery_guard_strict",
        "release_operational_unit_tests",
        "release_operational_ruff",
        "release_operational_typecheck",
        "release_channel_gate",
        "reliability_monthly_pack",
    ]


def test_main_fails_on_release_converge_tracked_boundary_violation(monkeypatch, tmp_path: Path):
    mod = _load_module()
    tracked_file = tmp_path / "tracked.txt"
    tracked_file.write_text("before\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=tmp_path, check=True, capture_output=True)

    monkeypatch.setenv("HFT_ALPHA_AUDIT_ENABLED", "1")
    monkeypatch.setattr(mod, "_is_git_repo", lambda root: True)
    monkeypatch.setattr(mod, "_tracked_paths", lambda root: ["tracked.txt"])

    def _fake_run_shell(command: str, *, cwd: Path, env: dict[str, str] | None = None) -> dict:
        if "release_converge.py" not in command:
            raise AssertionError(f"unexpected downstream command: {command}")
        tracked_file.write_text("after-boundary\n", encoding="utf-8")
        _write_json(
            cwd / "outputs" / "release_converge" / "latest.json",
            {"result": {"overall": "pass"}},
        )
        return {
            "command": command,
            "returncode": 0,
            "duration_s": 0.01,
            "stdout": "",
            "stderr": "",
            "stdout_tail": "",
            "stderr_tail": "",
        }

    monkeypatch.setattr(mod, "_run_shell", _fake_run_shell)

    rc = mod.main(
        [
            "--project-root",
            str(tmp_path),
            "--output-dir",
            "outputs/release_first_ops",
            "--change-id",
            "CHG-20260306-01",
        ]
    )

    assert rc == 2
    payload = json.loads((tmp_path / "outputs" / "release_first_ops" / "latest.json").read_text(encoding="utf-8"))
    assert payload["result"]["overall"] == "fail"
    assert len(payload["steps"]) == 2
    assert payload["steps"][1]["step"] == "release_converge_no_clean"
    assert payload["steps"][1]["returncode"] == 97
    assert payload["steps"][1]["boundary_violation"] is True
    assert payload["steps"][1]["unexpected_tracked_changes"]
