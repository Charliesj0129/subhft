"""Ratchet: ``hft_platform`` does not reach into ``research`` (ADR-005 boundary).

Scans every module under ``src/hft_platform`` for the ways platform code can
pick up research code: ``import`` / ``from`` statements, ``import_module`` /
``__import__`` / ``find_spec`` with a literal name, ``-m research...`` in a
command list, ``sys.path`` edits, and ``src.`` imports. Each existing edge is
listed below with its reason. A new edge fails the test, and so does an entry
that no longer matches anything, so the list can only shrink.

Loading an alpha by file path (``alpha/discovery.py``, the monitor TUI) is
deliberate and not an import of the ``research`` package (ADR-005).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "hft_platform"

_DYNAMIC_IMPORTERS = frozenset({"import_module", "__import__", "find_spec"})
_SYS_PATH_EDITS = frozenset({"insert", "append", "extend"})

# (importer, imported) -> why it is still allowed. Research-v2 C5 moves the
# research subcommands behind ``python -m research`` and the Gate C runtime into
# research-side injection; those entries go then.
_ALLOWED: dict[tuple[str, str], str] = {
    ("src/hft_platform/alpha/_gate_c.py", "research.backtest.gate_c_runtime"): (
        "fallback when no GateCRuntime is injected; removed in C5"
    ),
    ("src/hft_platform/alpha/_validation_helpers.py", "sys.path"): (
        "puts the project root on sys.path so an alpha impl.py can be loaded"
    ),
    ("src/hft_platform/cli/__init__.py", "sys.path"): "lets `hft alpha` research subcommands import research",
    ("src/hft_platform/cli/_parser.py", "research"): "find_spec probe: research subcommands only when installed",
    ("src/hft_platform/cli/_alpha.py", "-m research.tools.alpha_scaffold"): "`hft alpha scaffold` shells out",
    ("src/hft_platform/cli/_alpha.py", "research.combinatorial.partitioning"): "`hft alpha` research subcommand",
    ("src/hft_platform/cli/_alpha.py", "research.combinatorial.promote"): "`hft alpha` research subcommand",
    ("src/hft_platform/cli/_alpha.py", "research.combinatorial.search_engine"): "`hft alpha` research subcommand",
    ("src/hft_platform/cli/_alpha.py", "research.combinatorial.smma_dataset"): "`hft alpha` research subcommand",
    ("src/hft_platform/cli/_alpha.py", "research.combinatorial.smma_runner"): "`hft alpha` research subcommand",
    ("src/hft_platform/cli/_alpha.py", "research.combinatorial.smma_screen"): "`hft alpha` research subcommand",
    ("src/hft_platform/cli/_alpha.py", "research.combinatorial.tick_dataset"): "`hft alpha` research subcommand",
    ("src/hft_platform/cli/_alpha.py", "research.pipeline"): "`hft alpha pipeline` runs the research pipeline",
    ("src/hft_platform/cli/_alpha.py", "research.rl.lifecycle"): "`hft alpha rl-promote`",
}


def _is_research(name: str) -> bool:
    return name == "research" or name.startswith("research.")


def _is_src(name: str) -> bool:
    return name == "src" or name.startswith("src.")


def _literal(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _edges_in(tree: ast.AST) -> set[str]:
    edges: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            edges.update(a.name for a in node.names if _is_research(a.name) or _is_src(a.name))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and (_is_research(module) or _is_src(module)):
                edges.add(module)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            target = _literal(node.args[0]) if node.args else None
            if name in _DYNAMIC_IMPORTERS and target and (_is_research(target) or _is_src(target)):
                edges.add(target)
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _SYS_PATH_EDITS
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "path"
                and getattr(func.value.value, "id", "") == "sys"
            ):
                edges.add("sys.path")
        elif isinstance(node, (ast.List, ast.Tuple)):
            values = [_literal(e) for e in node.elts]
            for flag, module in zip(values, values[1:], strict=False):
                if flag == "-m" and module and _is_research(module):
                    edges.add(f"-m {module}")
    return edges


def _scan(src: Path) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        found.update((rel, edge) for edge in _edges_in(tree))
    return found


def test_platform_research_edges_are_only_the_allowed_ones():
    new = sorted(_scan(_SRC) - set(_ALLOWED))
    assert new == [], f"new hft_platform -> research edge(s); remove them or justify in _ALLOWED: {new}"


def test_platform_research_allowlist_has_no_stale_entries():
    stale = sorted(set(_ALLOWED) - _scan(_SRC))
    assert stale == [], f"allowlist entries no longer present; delete them: {stale}"


@pytest.mark.parametrize(
    ("source", "edge"),
    [
        ("import research.factory", "research.factory"),
        ("from research.backtest import x", "research.backtest"),
        ("from src.hft_platform import x", "src.hft_platform"),
        ("importlib.import_module('research.pipeline')", "research.pipeline"),
        ("__import__('research')", "research"),
        ("find_spec('research')", "research"),
        ("cmd = [sys.executable, '-m', 'research.tools.x']", "-m research.tools.x"),
        ("sys.path.insert(0, root)", "sys.path"),
    ],
)
def test_boundary_scanner_detects_each_import_form(source: str, edge: str):
    assert _edges_in(ast.parse(source)) == {edge}


def test_boundary_scanner_ignores_relative_and_unrelated_imports():
    source = "from .research import x\nimport researchy\nimport_module(name)\nPath('research/alphas')"
    assert _edges_in(ast.parse(source)) == set()
