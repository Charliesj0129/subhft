from __future__ import annotations

import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


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


# --- the gate trusts what each step reports, not a second attempt ----------------------------------------------

_REPORTS = {
    "release_converge_no_clean": (
        "scripts/release_converge.py",
        "outputs/release_converge/latest.json",
    ),
    "roadmap_delivery_execute_strict": (
        "scripts/roadmap_delivery_executor.py",
        "outputs/roadmap_execution/summary/latest.json",
    ),
    "roadmap_delivery_guard_strict": (
        "scripts/roadmap_delivery_guard.py",
        "outputs/roadmap_delivery/latest.json",
    ),
    "release_channel_gate": (
        "scripts/release_channel_guard.py gate",
        "outputs/deploy_guard/release_channel/decisions/release_gate_20260306T010101Z_CHG-20260306-01.json",
    ),
    "reliability_monthly_pack": (
        "scripts/reliability_review_pack.py",
        "outputs/reliability/monthly/monthly_2026-03_20260306T010101Z.json",
    ),
}
_ARGV = ["--output-dir", "outputs/release_first_ops", "--change-id", "CHG-20260306-01", "--month", "2026-03"]


def _ok_row(command: str) -> dict:
    return {
        "command": command,
        "returncode": 0,
        "duration_s": 0.01,
        "stdout": "",
        "stderr": "",
        "stdout_tail": "",
        "stderr_tail": "",
    }


def _run_main_with_reports(monkeypatch, tmp_path: Path, *, bad_step: str | None, payload) -> tuple[int, dict]:
    """Every step exits 0. Each report-bearing step writes a passing report, except ``bad_step``, which writes
    *payload* (``None`` = no report at all, a ``str`` = that raw text)."""
    mod = _load_module()
    monkeypatch.setenv("HFT_ALPHA_AUDIT_ENABLED", "1")
    monkeypatch.setattr(mod, "_is_git_repo", lambda root: False)

    def _fake_run_shell(command: str, *, cwd: Path, env: dict[str, str] | None = None) -> dict:
        for step, (needle, rel) in _REPORTS.items():
            if needle not in command or "-m pytest" in command:
                continue
            target = cwd / rel
            if step != bad_step:
                _write_json(target, {"result": {"overall": "pass"}})
            elif isinstance(payload, str):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(payload, encoding="utf-8")
            elif payload is not None:
                _write_json(target, payload)
        return _ok_row(command)

    monkeypatch.setattr(mod, "_run_shell", _fake_run_shell)
    rc = mod.main(["--project-root", str(tmp_path), *_ARGV])
    latest = tmp_path / "outputs" / "release_first_ops" / "latest.json"
    return rc, json.loads(latest.read_text(encoding="utf-8"))


@pytest.mark.parametrize("bad_step", list(_REPORTS))
@pytest.mark.parametrize(
    "payload",
    [
        {"result": {"overall": "fail"}},
        {"result": {"overall": "warn"}},
        {"result": {"recommendation": "go"}},
        {"overall": "ready"},
        "not json at all",
        None,
    ],
    ids=["fail", "warn", "no-overall", "other-word", "unreadable", "missing"],
)
def test_step_that_exits_zero_but_does_not_report_pass_fails_the_gate(
    monkeypatch, tmp_path: Path, bad_step: str, payload
) -> None:
    rc, report = _run_main_with_reports(monkeypatch, tmp_path, bad_step=bad_step, payload=payload)

    assert rc == 2
    assert report["result"] == {"overall": "fail", "recommendation": "block"}
    last = report["steps"][-1]
    assert last["step"] == bad_step  # the gate stopped here
    assert last["returncode"] == 98
    assert last["report_violation"] is True
    assert "step report is not pass" in last["stderr"]
    markdown = (tmp_path / "outputs" / "release_first_ops" / "latest.md").read_text(encoding="utf-8")
    assert "report_not_pass" in markdown


def test_steps_with_a_passing_report_are_not_flagged(monkeypatch, tmp_path: Path) -> None:
    rc, report = _run_main_with_reports(monkeypatch, tmp_path, bad_step=None, payload=None)

    assert rc == 0
    assert report["result"]["overall"] == "pass"
    assert [row["report_violation"] for row in report["steps"]] == [False] * 9
    reported = {row["step"]: row["report_overall"] for row in report["steps"] if row["report_path"]}
    assert reported == {step: "pass" for step in _REPORTS}


def test_steps_without_a_report_are_judged_by_their_exit_code_only(monkeypatch, tmp_path: Path) -> None:
    rc, report = _run_main_with_reports(monkeypatch, tmp_path, bad_step=None, payload=None)

    assert rc == 0
    no_report = {"alpha_audit_enabled", "release_operational_unit_tests", "release_operational_ruff"}
    rows = {row["step"]: row for row in report["steps"]}
    for name in no_report | {"release_operational_typecheck"}:
        assert rows[name]["report_path"] == ""
        assert rows[name]["report_violation"] is False


def test_step_commands_use_one_interpreter_and_have_no_second_attempt(tmp_path: Path) -> None:
    mod = _load_module()
    args = mod._build_parser().parse_args(["--project-root", str(tmp_path), *_ARGV])

    commands = [s["command"] for s in mod._build_steps(args) if s["command"] != "__check_alpha_audit__"]

    assert len(commands) == 8
    for command in commands:
        assert "||" not in command, command
        assert "2>/dev/null" not in command, command
        assert command.startswith(shlex.quote(sys.executable)), command  # no .venv in tmp_path: the running interpreter
        assert command.count("python3") == 0, command


def test_project_venv_is_the_interpreter_when_it_exists(tmp_path: Path) -> None:
    mod = _load_module()
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n")
    venv_python.chmod(0o755)
    args = mod._build_parser().parse_args(["--project-root", str(tmp_path), *_ARGV])

    commands = [s["command"] for s in mod._build_steps(args) if s["command"] != "__check_alpha_audit__"]

    assert all(command.startswith(shlex.quote(str(venv_python))) for command in commands)


def test_failing_step_is_not_rerun_and_keeps_its_own_stderr(monkeypatch, tmp_path: Path) -> None:
    """The project's interpreter fails. The gate used to discard its stderr and run the step again under
    ``python3``, so the report showed the second attempt's error (or a pass) instead of the real failure."""
    log = tmp_path / "interpreter_calls.log"
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\necho "ORIGINAL FAILURE: $1" >&2\nexit 2\n')
    venv_python.chmod(0o755)
    monkeypatch.setenv("HFT_ALPHA_AUDIT_ENABLED", "1")
    mod = _load_module()
    monkeypatch.setattr(mod, "_is_git_repo", lambda root: False)

    rc = mod.main(["--project-root", str(tmp_path), *_ARGV])

    assert rc == 2
    report = json.loads((tmp_path / "outputs" / "release_first_ops" / "latest.json").read_text(encoding="utf-8"))
    failed = report["steps"][-1]
    assert failed["step"] == "release_converge_no_clean"
    assert failed["returncode"] == 2
    assert "ORIGINAL FAILURE: scripts/release_converge.py" in failed["stderr"]
    assert len(log.read_text().splitlines()) == 1  # exactly one attempt
