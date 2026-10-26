"""`make research-check` refuses an unexplained or stale quarantine, and measures tracked research code only."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "research_check.py"

# The floor committed with the gate. Raising it needs no change here; lowering it means editing this as well.
MINIMUM_COVERAGE_FLOOR = 38.0


def _load_module():
    spec = importlib.util.spec_from_file_location("research_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["research_check"] = module
    spec.loader.exec_module(module)
    return module


rc_mod = _load_module()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _write(tmp_path / "tests/research/test_a.py", "def test_kept():\n    assert True\n\n\nclass TestGroup:\n    pass\n")
    (tmp_path / "research/alphas/x/tests").mkdir(parents=True)
    return tmp_path


def _quarantine(repo: Path, body: str) -> Path:
    return _write(repo / "q.yaml", body)


REASON = "needs a dependency group that CI does not install"


def test_quarantine_valid_entries_become_pytest_args(repo: Path) -> None:
    q = _quarantine(
        repo,
        f"entries:\n"
        f"  - {{kind: ignore, path: research/alphas/x, reason: {REASON}}}\n"
        f"  - {{kind: deselect, path: 'tests/research/test_a.py::test_kept', reason: {REASON}}}\n",
    )
    entries = rc_mod.load_quarantine(q, repo)
    assert [arg for e in entries for arg in e.pytest_args()] == [
        "--ignore=research/alphas/x",
        "--deselect=tests/research/test_a.py::test_kept",
    ]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("entries:\n  - {kind: ignore, path: research/alphas/x}\n", "reason"),
        ("entries:\n  - {kind: ignore, path: research/alphas/x, reason: flaky}\n", "reason"),
        (f"entries:\n  - {{kind: skip, path: research/alphas/x, reason: {REASON}}}\n", "kind"),
        (f"entries:\n  - {{kind: ignore, path: research/alphas/gone, reason: {REASON}}}\n", "no longer exists"),
        (f"entries:\n  - {{kind: deselect, path: tests/research/test_a.py, reason: {REASON}}}\n", "file::test"),
        (
            f"entries:\n  - {{kind: deselect, path: 'tests/research/test_a.py::test_gone', reason: {REASON}}}\n",
            "test_gone is not in",
        ),
        (
            f"entries:\n  - {{kind: ignore, path: 'tests/research/test_a.py::test_kept', reason: {REASON}}}\n",
            "not a test id",
        ),
        ("entries: {}\n", "must be a list"),
        ("entries:\n  - just-a-string\n", "mapping"),
    ],
)
def test_quarantine_bad_or_stale_entry_is_rejected(repo: Path, body: str, message: str) -> None:
    with pytest.raises(rc_mod.QuarantineError, match=message):
        rc_mod.load_quarantine(_quarantine(repo, body), repo)


def test_quarantine_deselect_matches_class_and_parametrized_ids(repo: Path) -> None:
    q = _quarantine(
        repo,
        f"entries:\n"
        f"  - {{kind: deselect, path: 'tests/research/test_a.py::TestGroup', reason: {REASON}}}\n"
        f"  - {{kind: deselect, path: 'tests/research/test_a.py::test_kept[case-1]', reason: {REASON}}}\n",
    )
    assert len(rc_mod.load_quarantine(q, repo)) == 2


def test_shipped_quarantine_is_valid_against_the_repo() -> None:
    entries = rc_mod.load_quarantine()
    assert entries, "the shipped quarantine is expected to hold the known-broken suites"
    assert all(len(e.reason) >= rc_mod.MIN_REASON_CHARS for e in entries)


def test_coverage_floor_only_ratchets_up() -> None:
    assert rc_mod.COVERAGE_FLOOR >= MINIMUM_COVERAGE_FLOOR


def _git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for rel in (
        "research/kept.py",
        "research/archive/old.py",
        "research/experiments/frozen.py",
        "research/alphas/a1/tests/test_one.py",
        "research/alphas/a1/tests/sub/test_nested.py",
    ):
        _write(tmp_path / rel, "x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    _write(tmp_path / "research/local_only.py", "x = 1\n")
    return tmp_path


def test_coverage_rc_omits_untracked_and_frozen_research_code(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    rc = rc_mod.coverage_rc(repo)
    assert "source = research" in rc
    assert "research/local_only.py" in rc
    assert "research/archive/*" in rc and "research/experiments/*" in rc
    assert "research/kept.py" not in rc


def test_ruff_targets_skip_frozen_trees(tmp_path: Path) -> None:
    modules = rc_mod.tracked_research_modules(_git_repo(tmp_path))
    assert "research/kept.py" in modules
    assert not [m for m in modules if m.startswith(rc_mod.FROZEN_PREFIXES)]
    assert "research/local_only.py" not in modules


def test_alpha_tests_are_the_direct_gate_b_suites_minus_ignored_dirs(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    assert rc_mod.alpha_test_files(repo) == ["research/alphas/a1/tests/test_one.py"]
    ignored = [rc_mod.QuarantineEntry(kind="ignore", path="research/alphas/a1", reason=REASON)]
    cmd = rc_mod.pytest_command(ignored, tmp_path / "rc", repo)
    assert "research/alphas/a1/tests/test_one.py" not in cmd
    assert "--ignore=research/alphas/a1" in cmd
    assert f"--cov-fail-under={rc_mod.COVERAGE_FLOOR}" in cmd
    assert "addopts=" in cmd


def test_main_exits_2_on_invalid_quarantine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(rc_mod, "QUARANTINE_FILE", _write(tmp_path / "q.yaml", "entries: nope\n"))
    monkeypatch.setattr(rc_mod, "ROOT", tmp_path)
    assert rc_mod.main(["--dry-run"]) == 2
