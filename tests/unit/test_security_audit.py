"""Hermetic CLI regressions for fail-closed dependency and CI security scans."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MAKE = shutil.which("make")
UV = shutil.which("uv")


@pytest.fixture
def audit_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repository"
    (root / "scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/security_audit.py", root / "scripts/security_audit.py")
    makefile = (ROOT / "Makefile").read_text()
    target = makefile.split("security-audit: ", 1)[1].split("\nlatency-audit:", 1)[0]
    (root / "Makefile").write_text("security-audit: " + target)
    (root / "uv.lock").write_text("valid lock fixture\n")
    binary = root / "bin"
    binary.mkdir()
    uv = binary / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "if args[0] == 'run':\n"
        "    args = args[1:]\n"
        "    while args and args[0].startswith('--'):\n"
        "        args.pop(0)\n"
        "    if args == ['python', '-m', 'pip', 'check']:\n"
        "        print('pip check fallback'); sys.exit(0)\n"
        "    os.execv(sys.executable, [sys.executable, *args[1:]])\n"
        "pathlib.Path('export-args.json').write_text(json.dumps(args))\n"
        "code = int(os.environ.get('AUDIT_EXPORT_STATUS', '0'))\n"
        "if code:\n"
        "    print('lock export error', file=sys.stderr); sys.exit(code)\n"
        "if os.environ.get('AUDIT_EMPTY_EXPORT') != '1':\n"
        "    pathlib.Path(args[args.index('--output-file') + 1]).write_text(os.environ.get('AUDIT_EXPORT', 'numpy==2.2.0\\n'))\n"
    )
    uv.chmod(0o755)
    modules = root / "modules"
    modules.mkdir()
    (modules / "pip_audit.py").write_text(
        "import json, os, pathlib, sys\n"
        "if os.environ.get('AUDIT_MISSING_SCANNER') == '1':\n"
        "    raise ModuleNotFoundError('No module named pip_audit')\n"
        "args = sys.argv[1:]\n"
        "pathlib.Path('audit-args.json').write_text(json.dumps(args))\n"
        "if '--requirement' in args:\n"
        "    requirements = pathlib.Path(args[args.index('--requirement') + 1])\n"
        "    names = [line.split('==')[0] for line in requirements.read_text().splitlines()]\n"
        "    if len(names) != len(set(names)): print('duplicate package'); sys.exit(2)\n"
        "    pathlib.Path('audited-requirements.txt').write_text(requirements.read_text())\n"
        "    with pathlib.Path('all-audited.txt').open('a') as output: output.write(requirements.read_text())\n"
        "print(os.environ.get('AUDIT_MESSAGE', 'No known vulnerabilities found'))\n"
        "sys.exit(int(os.environ.get('AUDIT_SCANNER_STATUS', '0')))\n"
    )
    monkeypatch.setenv("PATH", f"{binary}:/usr/bin:/bin")
    monkeypatch.setenv("PYTHONPATH", str(modules))
    for variable in (
        "AUDIT_EXPORT_STATUS",
        "AUDIT_EMPTY_EXPORT",
        "AUDIT_MISSING_SCANNER",
        "AUDIT_SCANNER_STATUS",
        "AUDIT_MESSAGE",
        "AUDIT_EXPORT",
    ):
        monkeypatch.delenv(variable, raising=False)
    return root


def run_audit(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([MAKE, "security-audit"], cwd=root, capture_output=True, text=True, check=False)


def test_security_audit_complete_lock_clean_result(audit_repo: Path) -> None:
    result = run_audit(audit_repo)
    assert result.returncode == 0, result.stdout + result.stderr
    exported = json.loads((audit_repo / "export-args.json").read_text())
    for flag in ("--locked", "--no-emit-project", "--no-hashes"):
        assert flag in exported
    assert [exported[index + 1] for index, item in enumerate(exported) if item == "--group"] == ["dev", "research"]
    assert [exported[index + 1] for index, item in enumerate(exported) if item == "--extra"] == ["bot", "monitor"]
    audited = json.loads((audit_repo / "audit-args.json").read_text())
    assert {"--strict", "--no-deps", "--disable-pip"}.issubset(audited)
    assert "--ignore-vuln" not in audited
    assert (audit_repo / "audited-requirements.txt").read_text() == "numpy==2.2.0\n"
    assert "No known vulnerabilities found" in (audit_repo / "audit-output.txt").read_text()
    assert "private Fubon SDK" in result.stdout


def test_security_audit_includes_all_platforms_and_versions(audit_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AUDIT_EXPORT",
        "example==1.0 ; sys_platform == 'win32'\nexample==2.0 ; sys_platform == 'linux'\n"
        "other==3.0 ; python_version < '3.12'\n",
    )
    result = run_audit(audit_repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert set((audit_repo / "all-audited.txt").read_text().splitlines()) == {
        "example==1.0",
        "example==2.0",
        "other==3.0",
    }


@pytest.mark.parametrize("export", ["example>=1.0\n", "example @ https://example.com/example.whl\n", "# empty\n"])
def test_security_audit_rejects_unusable_exports(
    audit_repo: Path, monkeypatch: pytest.MonkeyPatch, export: str
) -> None:
    monkeypatch.setenv("AUDIT_EXPORT", export)
    result = run_audit(audit_repo)
    assert result.returncode != 0
    assert "invalid locked dependency export" in result.stdout
    assert not (audit_repo / "audit-args.json").exists()


@pytest.mark.parametrize("status,message", [(1, "vulnerability found"), (2, "database or network error")])
def test_security_audit_scanner_failure_blocks_result(
    audit_repo: Path, monkeypatch: pytest.MonkeyPatch, status: int, message: str
) -> None:
    monkeypatch.setenv("AUDIT_SCANNER_STATUS", str(status))
    monkeypatch.setenv("AUDIT_MESSAGE", message)
    result = run_audit(audit_repo)
    assert result.returncode != 0
    assert message in result.stdout
    assert message in (audit_repo / "audit-output.txt").read_text()
    assert "pip check fallback" not in result.stdout


def test_security_audit_missing_scanner_blocks_result(audit_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_MISSING_SCANNER", "1")
    result = run_audit(audit_repo)
    assert result.returncode != 0
    assert "No module named pip_audit" in (audit_repo / "audit-output.txt").read_text()
    assert "pip check fallback" not in result.stdout


def test_security_audit_missing_uv_blocks_result(audit_repo: Path) -> None:
    (audit_repo / "bin/uv").unlink()
    result = run_audit(audit_repo)
    assert result.returncode != 0
    assert "uv" in (audit_repo / "audit-output.txt").read_text()
    assert not (audit_repo / "audit-args.json").exists()


def test_security_audit_missing_root_lock_rejects_wrong_lock(audit_repo: Path) -> None:
    (audit_repo / "uv.lock").unlink()
    nested = audit_repo / "rust_core"
    nested.mkdir()
    (nested / "uv.lock").write_text("wrong lock\n")
    result = run_audit(audit_repo)
    assert result.returncode != 0
    assert "repository uv.lock is missing" in (audit_repo / "audit-output.txt").read_text()
    assert not (audit_repo / "audit-args.json").exists()


@pytest.mark.parametrize("status", [1, 2])
def test_security_audit_invalid_or_stale_export_blocks_result(
    audit_repo: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setenv("AUDIT_EXPORT_STATUS", str(status))
    before = (audit_repo / "uv.lock").read_bytes()
    result = run_audit(audit_repo)
    assert result.returncode != 0
    assert "lock export error" in (audit_repo / "audit-output.txt").read_text()
    assert "--locked" in json.loads((audit_repo / "export-args.json").read_text())
    assert (audit_repo / "uv.lock").read_bytes() == before
    assert not (audit_repo / "audit-args.json").exists()


def test_security_audit_empty_export_blocks_result(audit_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_EMPTY_EXPORT", "1")
    result = run_audit(audit_repo)
    assert result.returncode != 0
    assert "empty or missing" in (audit_repo / "audit-output.txt").read_text()
    assert not (audit_repo / "audit-args.json").exists()


def test_security_audit_direct_entry_missing_uv_blocks_result(audit_repo: Path) -> None:
    (audit_repo / "bin/uv").unlink()
    result = subprocess.run(
        [sys.executable, str(audit_repo / "scripts/security_audit.py")],
        cwd=audit_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "cannot run uv export" in result.stdout


def test_security_audit_real_locked_export_rejects_stale_lock(
    audit_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert UV is not None
    project = audit_repo / "pyproject.toml"
    project.write_text(
        '[project]\nname = "security-audit-fixture"\nversion = "0.0.0"\nrequires-python = ">=3.12"\n'
        "[dependency-groups]\ndev = []\nresearch = []\n"
        "[project.optional-dependencies]\nbot = []\nmonitor = []\n"
    )
    (audit_repo / "uv.lock").unlink()
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_CACHE_DIR", str(audit_repo / "cache"))
    locked = subprocess.run([UV, "lock"], cwd=audit_repo, capture_output=True, text=True, check=False)
    assert locked.returncode == 0, locked.stdout + locked.stderr
    before = (audit_repo / "uv.lock").read_bytes()
    project.write_text(project.read_text().replace('version = "0.0.0"', 'version = "0.0.1"'))
    (audit_repo / "bin/uv").unlink()
    (audit_repo / "bin/uv").symlink_to(UV)
    result = subprocess.run(
        [sys.executable, str(audit_repo / "scripts/security_audit.py")],
        cwd=audit_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "locked dependency export failed" in result.stdout
    assert "--locked" in result.stdout
    assert (audit_repo / "uv.lock").read_bytes() == before
    assert not (audit_repo / "audit-args.json").exists()


def test_security_audit_make_bootstrap_keeps_checked_lock() -> None:
    target = (ROOT / "Makefile").read_text().split("security-audit: ", 1)[1].split("\nlatency-audit:", 1)[0]
    assert "uv run --locked --no-sync python scripts/security_audit.py" in target
    assert "pip check" not in target
    assert "audit-output.txt 2>&1" in target


def test_security_audit_ci_rust_workspace_lock_blocks_failures() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["rust"]["steps"]
    audit = next(step for step in steps if step.get("id") == "cargo_audit")
    assert audit["run"] == "cargo install cargo-audit --locked && cargo audit --file Cargo.lock"
    assert audit["if"] == "always()"
    assert not audit.get("continue-on-error", False)
    cache = next(step for step in steps if step.get("name") == "Cache Cargo")["with"]
    assert "target" in cache["path"].splitlines()
    assert "rust_core/target" not in cache["path"]
    assert "hashFiles('Cargo.lock', 'Cargo.toml', 'rust_core/Cargo.toml')" in cache["key"]
    summary = next(step for step in steps if step.get("name") == "Rust security summary")
    assert summary["if"] == "always()"
    assert "steps.cargo_audit.outcome" in summary["run"]


def test_security_audit_ci_scanners_continue_and_summarize_failures() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["security"]["steps"]
    summary = next(step for step in steps if step.get("name") == "Security Summary")
    assert summary["if"] == "always()"
    for scanner in ("pip_audit", "bandit", "gitleaks", "licenses"):
        step = next(step for step in steps if step.get("id") == scanner)
        assert step["if"] == "always()"
        assert not step.get("continue-on-error", False)
        assert f"steps.{scanner}.outcome" in summary["run"]
    assert "audit-output.txt" in summary["run"]


def test_dependency_compatibility_ci_installs_research_before_tests() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["research-check"]["steps"]
    install_index = next(
        index for index, step in enumerate(steps) if step.get("run") == "uv sync --locked --dev --group research"
    )
    test_index = next(index for index, step in enumerate(steps) if step.get("run") == "make research-check")
    assert install_index < test_index
    assert not steps[install_index].get("continue-on-error", False)
