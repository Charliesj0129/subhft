#!/usr/bin/env python3
"""Lint and test the tracked research code (`make research-check`).

`pyproject.toml` keeps research out of the platform gates: ruff excludes
`research/`, pytest ignores `tests/research` and does not recurse into any
directory named `research` (so `tests/unit/research` never runs either). This
script is the research gate:

1. ruff, bug classes only (pyflakes and syntax errors), on tracked research
   modules. Style is not enforced: most research files predate the platform's
   ruff settings, and reformatting them would bury every diff.
2. pytest over `tests/research`, `tests/unit/research` and the tracked alpha
   tests under `research/alphas/*/tests` (what Gate B runs), with branch
   coverage of `research` and its own floor.

Coverage counts tracked files only: untracked and ignored local research code
is omitted, as are the frozen `research/archive/` and `research/experiments/`,
so the number is the same on a laptop and in CI.

Tests that cannot run are listed, with a reason, in tests/research/quarantine.yaml.
An entry without a reason, or whose path or test no longer exists, is an error.

Usage:
    python3 scripts/research_check.py [--skip-ruff] [--skip-tests] [--dry-run]

Exit codes:
    0 - ruff and tests pass, coverage at or above the floor
    1 - a ruff finding, a failing test, or coverage below the floor
    2 - the quarantine file is invalid
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
QUARANTINE_FILE = ROOT / "tests" / "research" / "quarantine.yaml"

# Branch coverage measured 2026-10-26 on main (tracked files, archive and
# experiments omitted, Rust extension not built, as in CI): 40.13. The floor is
# that minus 2 points, rounded down, as in scripts/check_coverage_domains.py.
# Raise it as coverage grows; tests/unit/test_research_check.py fails if it is lowered.
COVERAGE_FLOOR = 38.0

# Frozen trees: never edited, never tested, not part of the coverage base.
FROZEN_PREFIXES = ("research/archive/", "research/experiments/")

# pyflakes (undefined names, redefinitions, bad comparisons, ...) and syntax
# errors. Unused imports/variables and placeholder-less f-strings are style.
RUFF_SELECT = "F,E9"
RUFF_IGNORE = "F401,F841,F541"

TEST_DIRS = ("tests/research", "tests/unit/research")
MIN_REASON_CHARS = 20


class QuarantineError(ValueError):
    """The quarantine file is malformed or names something that is gone."""


@dataclass(frozen=True)
class QuarantineEntry:
    kind: str
    path: str
    reason: str

    def pytest_args(self) -> list[str]:
        return [f"--ignore={self.path}"] if self.kind == "ignore" else [f"--deselect={self.path}"]


def _test_names(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def load_quarantine(path: Path = QUARANTINE_FILE, root: Path = ROOT) -> list[QuarantineEntry]:
    """Parse and validate the quarantine list; raise QuarantineError on any bad entry."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise QuarantineError(f"{path}: 'entries' must be a list")
    entries: list[QuarantineEntry] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise QuarantineError(f"entry {i}: must be a mapping")
        kind = str(item.get("kind", ""))
        target = str(item.get("path", "")).strip()
        reason = " ".join(str(item.get("reason", "")).split())
        if kind not in ("ignore", "deselect"):
            raise QuarantineError(f"entry {i} ({target}): kind must be 'ignore' or 'deselect', got {kind!r}")
        if len(reason) < MIN_REASON_CHARS:
            raise QuarantineError(f"entry {i} ({target}): a reason of at least {MIN_REASON_CHARS} characters")
        file_part, _, test_part = target.partition("::")
        if kind == "deselect" and not test_part:
            raise QuarantineError(f"entry {i} ({target}): deselect needs file::test")
        if kind == "ignore" and test_part:
            raise QuarantineError(f"entry {i} ({target}): ignore takes a path, not a test id")
        if not (root / file_part).exists():
            raise QuarantineError(f"entry {i} ({target}): {file_part} no longer exists; drop the entry")
        if test_part:
            wanted = test_part.split("::")[-1].split("[")[0]
            if wanted not in _test_names((root / file_part).read_text(encoding="utf-8")):
                raise QuarantineError(f"entry {i} ({target}): {wanted} is not in {file_part}; drop the entry")
        entries.append(QuarantineEntry(kind=kind, path=target, reason=reason))
    return entries


def _git_ls(args: list[str], root: Path) -> list[str]:
    out = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line]


def tracked_research_modules(root: Path = ROOT) -> list[str]:
    return [p for p in _git_ls(["ls-files", "--", "research/*.py"], root) if not p.startswith(FROZEN_PREFIXES)]


def alpha_test_files(root: Path = ROOT) -> list[str]:
    """Tracked research/alphas/<id>/tests/test_*.py files (the Gate B suites)."""
    return [
        p
        for p in _git_ls(["ls-files", "--", "research/alphas/*/tests/test_*.py"], root)
        if Path(p).parent.name == "tests" and len(Path(p).parts) == 5
    ]


def coverage_rc(root: Path = ROOT) -> str:
    """A coverage config whose base is the tracked, non-frozen research modules."""
    untracked = _git_ls(["ls-files", "--others", "--", "research/*.py"], root)
    omit = [f"{prefix}*" for prefix in FROZEN_PREFIXES] + untracked
    lines = ["[run]", "branch = True", "source = research", "omit ="]
    lines += [f"    {p}" for p in omit]
    lines += ["", "[report]", "precision = 2"]
    return "\n".join(lines) + "\n"


def ruff_command(modules: list[str]) -> list[str]:
    return [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--no-force-exclude",
        "--select",
        RUFF_SELECT,
        "--ignore",
        RUFF_IGNORE,
        *modules,
    ]


def pytest_command(entries: list[QuarantineEntry], rc_path: Path, root: Path = ROOT) -> list[str]:
    ignored = {e.path.rstrip("/") for e in entries if e.kind == "ignore"}
    alpha_tests = [p for p in alpha_test_files(root) if not any(p == i or p.startswith(i + "/") for i in ignored)]
    quarantine_args = [arg for e in entries for arg in e.pytest_args()]
    return [
        sys.executable,
        "-m",
        "pytest",
        # The platform addopts measure src/ with a 70% floor and ignore tests/research.
        "-o",
        "addopts=",
        "--timeout=60",
        "-p",
        "no:cacheprovider",
        # Several alphas ship a tests/test_smoke.py; rootdir-relative module names would collide.
        "--import-mode=importlib",
        "-q",
        "--cov",
        f"--cov-config={rc_path}",
        "--cov-report=term:skip-covered",
        f"--cov-fail-under={COVERAGE_FLOOR}",
        *quarantine_args,
        *TEST_DIRS,
        *alpha_tests,
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-ruff", action="store_true")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="print the commands without running them")
    args = parser.parse_args(argv)

    try:
        entries = load_quarantine(QUARANTINE_FILE, ROOT)
    except QuarantineError as exc:
        print(f"research-check: invalid {QUARANTINE_FILE}: {exc}", file=sys.stderr)
        return 2

    rc = 0
    if not args.skip_ruff:
        modules = tracked_research_modules()
        cmd = ruff_command(modules)
        print(f"research-check: ruff ({RUFF_SELECT}, ignoring {RUFF_IGNORE}) on {len(modules)} tracked modules")
        if not args.dry_run and subprocess.run(cmd, cwd=ROOT).returncode != 0:
            rc = 1
    if not args.skip_tests:
        with tempfile.TemporaryDirectory(prefix="research-check-") as tmp:
            rc_path = Path(tmp) / "coveragerc"
            rc_path.write_text(coverage_rc(), encoding="utf-8")
            cmd = pytest_command(entries, rc_path)
            for e in entries:
                print(f"research-check: quarantined {e.path}: {e.reason}")
            if args.dry_run:
                print(" ".join(cmd))
            elif subprocess.run(cmd, cwd=ROOT).returncode != 0:
                rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
