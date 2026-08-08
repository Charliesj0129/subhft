"""Regression guard: ``make rebuild-symbols-yaml`` must actually be runnable.

The target is the documented remedy for a contract roll — the pool never
rebuilds ``config/symbols.yaml`` itself, so an operator regenerates it offline
and restarts. It invoked ``hft symbols build --list-path ...`` while the CLI
exposes ``hft config build --list ...``, so argparse rejected it with
``invalid choice: 'symbols'`` (exit 2) before the builder ran. The target had
therefore never worked, and the only moment anyone reaches for it is a
rollover deadline — the worst time to find out it does not exist.

A Makefile recipe is not covered by any other test in the suite: lint and
typecheck never read it, and nothing imports it. This asserts the recipe the
operator will actually run parses into the command it claims to run.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
_CLI_PREFIX = "$(PY) -m hft_platform"


def _recipe_argv(target: str) -> list[str]:
    """Return the ``hft_platform`` argv the named target invokes.

    Joins backslash continuations, drops ``@#`` comment lines, and strips the
    interpreter prefix so what remains is exactly what argparse receives.
    """
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(target)}:.*?$(.*?)(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"target {target!r} not found in {MAKEFILE}"
    body = match.group(1).replace("\\\n", " ")
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(_CLI_PREFIX):
            return shlex.split(stripped[len(_CLI_PREFIX) :])
    raise AssertionError(f"target {target!r} has no {_CLI_PREFIX!r} recipe line")


@pytest.mark.unit
def test_rebuild_symbols_yaml_recipe_parses_into_the_symbols_builder() -> None:
    from hft_platform.cli._parser import build_parser
    from hft_platform.cli._symbols import cmd_symbols_build

    argv = _recipe_argv("rebuild-symbols-yaml")

    # parse_args raises SystemExit(2) on an unknown subcommand or flag, which
    # is exactly how this target failed.
    args = build_parser().parse_args(argv)

    assert args.func is cmd_symbols_build
    assert args.list_path == "config/symbols.list"
    assert args.contracts == "config/contracts.json"
    assert args.output == "config/symbols.yaml"


@pytest.mark.unit
def test_every_makefile_cli_target_names_a_real_subcommand() -> None:
    """Sibling guard for the same defect class across the whole Makefile.

    Recipes that interpolate make variables cannot be fully parsed (required
    arguments arrive from the caller), so this checks only the leading
    subcommand chain — which is what ``invalid choice`` rejects.
    """
    from hft_platform.cli._parser import build_parser

    parser = build_parser()
    top_level = {
        choice
        for action in parser._subparsers._group_actions  # noqa: SLF001 - argparse exposes choices no other way
        for choice in action.choices
    }
    assert top_level, "parser exposes no subcommands; the introspection above broke"

    lines = [
        line.strip()
        for line in MAKEFILE.read_text(encoding="utf-8").replace("\\\n", " ").splitlines()
        if line.strip().startswith(_CLI_PREFIX)
    ]
    assert lines, "no CLI recipes found; this guard would pass vacuously"

    for line in lines:
        remainder = line[len(_CLI_PREFIX) :].lstrip()
        if remainder.startswith("."):
            continue  # `-m hft_platform.alpha.spec_check` — a module, not the CLI
        command = shlex.split(remainder)[0]
        assert command in top_level, f"Makefile recipe invokes unknown subcommand {command!r}: {line}"
