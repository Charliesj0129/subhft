"""Every ``hft alpha <cmd>`` written in tracked docs names a real subcommand.

``hft alpha rl-promote`` stayed documented in four places after the module it
imported was removed, so the documented command could only fail.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

from hft_platform.cli._parser import build_parser

_REPO_ROOT = Path(__file__).resolve().parents[2]
# "hft alpha validate/promote/canary" lists several subcommands; each one must exist.
_USAGE = re.compile(r"\bhft alpha ([a-z][a-z0-9-]*(?:/[a-z][a-z0-9-]*)*)")
# Historical plans record what was run at the time; they are not instructions.
_EXCLUDED_PREFIXES = ("docs/superpowers/", "docs/archive/")


def _alpha_subcommands() -> set[str]:
    parser = build_parser()
    top = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    alpha = top.choices["alpha"]
    sub = next(a for a in alpha._actions if isinstance(a, argparse._SubParsersAction))
    return set(sub.choices)


def _tracked_docs() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "docs", ".agent", "research", "AGENTS.md", "README.md"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [_REPO_ROOT / f for f in out if f.endswith(".md") and not f.startswith(_EXCLUDED_PREFIXES)]


def test_documented_hft_alpha_subcommands_exist():
    known = _alpha_subcommands()
    assert "promote" in known  # the parser walk found the real alpha subparser

    unknown: list[str] = []
    for doc in _tracked_docs():
        for lineno, line in enumerate(doc.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            for match in _USAGE.finditer(line):
                for name in match.group(1).split("/"):
                    if name not in known:
                        unknown.append(f"{doc.relative_to(_REPO_ROOT)}:{lineno}: hft alpha {name}")

    assert not unknown, "documented hft alpha subcommands that do not exist:\n" + "\n".join(unknown)


def test_usage_pattern_reads_every_name_in_a_slash_list():
    match = _USAGE.search("`hft alpha validate/review-bogus-command` are live")
    assert match is not None
    assert match.group(1).split("/") == ["validate", "review-bogus-command"]
