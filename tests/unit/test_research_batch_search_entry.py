"""`make research-batch-search` and `python -m research batch-search` hand the searcher exactly the queries given."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_research_main_batch_search_does_not_pass_its_own_name_as_a_query(monkeypatch: pytest.MonkeyPatch) -> None:
    import research.__main__ as research_main
    import research.tools.batch_search as batch_search

    seen: list[list[str]] = []
    monkeypatch.setattr(batch_search, "main", lambda: seen.append(sys.argv[1:]) or 0)
    monkeypatch.setattr(sys, "argv", ["research", "batch-search", "order flow imbalance", "--dry-run"])

    assert research_main.main() == 0
    assert seen == [["order flow imbalance", "--dry-run"]]


@pytest.fixture
def fake_py(tmp_path: Path) -> Path:
    script = tmp_path / "fake_py.sh"
    script.write_text('#!/bin/sh\nfor a do printf "%s\\n" "$a"; done\n', encoding="utf-8")
    script.chmod(0o755)
    return script


def _make(fake_py: Path, queries: str | None) -> subprocess.CompletedProcess[str]:
    if shutil.which("make") is None:
        pytest.skip("make is not installed")
    cmd = ["make", "-s", "--no-print-directory", "research-batch-search", f"PY={fake_py}"]
    if queries is not None:
        cmd.append(f"QUERIES={queries}")
    return subprocess.run(cmd, cwd=_REPO_ROOT, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize(
    ("queries", "expected"),
    [
        ("order flow imbalance;market microstructure", ["order flow imbalance", "market microstructure"]),
        ("'order flow imbalance';'market microstructure'", ["order flow imbalance", "market microstructure"]),
        (' "a b" ;; c ', ["a b", "c"]),
        ("x`echo pwned`", ["x`echo pwned`"]),
    ],
)
def test_make_batch_search_splits_queries_on_semicolons_as_data(
    fake_py: Path, queries: str, expected: list[str]
) -> None:
    result = _make(fake_py, queries)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["-m", "research", "batch-search", *expected]


@pytest.mark.parametrize("queries", [None, " ; ;"])
def test_make_batch_search_without_a_query_exits_2(fake_py: Path, queries: str | None) -> None:
    result = _make(fake_py, queries)
    assert result.returncode == 2
    assert "-m" not in result.stdout.splitlines()  # the interpreter was never invoked
