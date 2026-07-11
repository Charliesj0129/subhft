"""Behavior tests for scripts/git_bundle_backup.py (fail-closed local backup)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.git_bundle_backup import main


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=test@test", "-c", "user.name=test", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(["add", "a.txt"], cwd=repo)
    _git(["commit", "-m", "first"], cwd=repo)
    return repo


def _run(repo: Path, dest: Path, stamp: str = "20260711T000000Z") -> int:
    return main(["--repo", str(repo), "--dest", str(dest), "--stamp", stamp])


def test_backup_creates_verified_bundle_covering_head(tmp_path: Path, capsys) -> None:
    repo = _make_repo(tmp_path)
    dest = tmp_path / "backups"
    dest.mkdir()

    assert _run(repo, dest) == 0

    bundles = list(dest.glob("repo-20260711T000000Z-*.bundle"))
    assert len(bundles) == 1
    head = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    assert head in _git(["bundle", "list-heads", str(bundles[0])], cwd=repo)
    assert "created and verified" in capsys.readouterr().out


def test_backup_refuses_nonexistent_destination_without_creating_it(tmp_path: Path, capsys) -> None:
    repo = _make_repo(tmp_path)
    dest = tmp_path / "missing"

    assert _run(repo, dest) == 2
    assert not dest.exists()
    assert "fail-closed" in capsys.readouterr().out


def test_backup_refuses_destination_inside_repo(tmp_path: Path, capsys) -> None:
    repo = _make_repo(tmp_path)
    inside = repo / "backups"
    inside.mkdir()

    assert _run(repo, inside) == 2
    assert "OUTSIDE the repository" in capsys.readouterr().out
    assert not list(inside.glob("*.bundle"))


def test_backup_never_overwrites_existing_bundle(tmp_path: Path, capsys) -> None:
    repo = _make_repo(tmp_path)
    dest = tmp_path / "backups"
    dest.mkdir()

    assert _run(repo, dest) == 0
    first = list(dest.glob("*.bundle"))[0]
    original = first.read_bytes()

    assert _run(repo, dest) == 2  # same stamp + same HEAD -> same name
    assert "refusing to overwrite" in capsys.readouterr().out
    assert first.read_bytes() == original


def test_destination_flag_is_required(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    with pytest.raises(SystemExit):
        main(["--repo", str(repo)])
