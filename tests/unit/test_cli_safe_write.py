"""Regression tests for hft_platform.cli._utils._safe_write.

Covers the FileNotFoundError crash that occurred when `path` was a bare
filename (no directory component): os.path.dirname(path) == "" and
os.makedirs("", exist_ok=True) raised FileNotFoundError.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hft_platform.cli._utils import _safe_write


def test_safe_write_bare_filename_writes_without_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare relative filename (no directory component) must not raise."""
    monkeypatch.chdir(tmp_path)

    _safe_write("out.txt", "hello world")

    written = tmp_path / "out.txt"
    assert written.read_text() == "hello world"


def test_safe_write_nested_path_creates_parent_dirs(tmp_path: Path) -> None:
    """A path with a directory component still creates missing parents."""
    target = tmp_path / "a" / "b" / "c.txt"

    _safe_write(str(target), "nested content")

    assert target.read_text() == "nested content"


def test_safe_write_existing_parent_dir_overwrites_content(tmp_path: Path) -> None:
    """An existing parent directory does not error; content is overwritten."""
    parent = tmp_path / "existing"
    parent.mkdir()
    target = parent / "file.txt"
    target.write_text("old content")

    _safe_write(str(target), "new content")

    assert target.read_text() == "new content"
