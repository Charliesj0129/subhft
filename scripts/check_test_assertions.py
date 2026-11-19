"""Check test functions have at least one assertion.

Exits 1 if zero-assertion tests are found in critical-path directories.
Non-critical violations are advisory only (exit 0).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

CRITICAL_PREFIXES = (
    "tests/unit/test_risk",
    "tests/unit/test_execution",
    "tests/unit/test_order",
    "tests/unit/test_recorder",
    "tests/unit/test_service",
    "tests/unit/test_graceful_shutdown",
)


def _is_critical(entry: str) -> bool:
    """Return True if a no-assert entry line matches a critical-path prefix."""
    path_str = entry.strip().split(":")[0]
    normalized = path_str.replace("\\", "/")
    return any(normalized.startswith(p) for p in CRITICAL_PREFIXES)


_HELPER_FUNCTIONS = frozenset({
    "_run_parity",
    "_assert_parity",
    "_check_result",
    "_verify",
    "_validate",
    "run_parity",
    "assert_parity",
    "check_result",
    "verify_result",
})

UNITTEST_ASSERT_METHODS = frozenset({
    "assertEqual",
    "assertNotEqual",
    "assertRaises",
    "assertTrue",
    "assertFalse",
    "assertIn",
    "assertNotIn",
    "assertIs",
    "assertIsNot",
    "assertIsNone",
    "assertIsNotNone",
    "assertIsInstance",
    "assertNotIsInstance",
    "assertGreater",
    "assertGreaterEqual",
    "assertLess",
    "assertLessEqual",
    "assertAlmostEqual",
    "assertNotAlmostEqual",
    "assertRegex",
    "assertNotRegex",
    "assertCountEqual",
    "assertMultiLineEqual",
    "assertSequenceEqual",
    "assertListEqual",
    "assertTupleEqual",
    "assertSetEqual",
    "assertDictEqual",
    "assertRaisesRegex",
    "assertWarns",
    "assertWarnsRegex",
    "assertLogs",
})

_NOQA_MARKER = "# noqa: no-assert"


def _is_self_attr(func: ast.expr, prefix: str) -> bool:
    """Check if ``func`` is ``self.<prefix>*`` attribute access."""
    return (
        isinstance(func, ast.Attribute)
        and func.attr.startswith(prefix)
        and isinstance(func.value, ast.Name)
        and func.value.id == "self"
    )


def _has_assertion(node: ast.FunctionDef) -> bool:
    """Check if a test function contains at least one assertion or delegates to an assertion helper."""
    for child in ast.walk(node):
        if isinstance(child, ast.Assert):
            return True
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute) and (
                func.attr == "raises" or func.attr.startswith("assert")
            ):
                return True
            if isinstance(func, ast.Attribute) and func.attr in UNITTEST_ASSERT_METHODS:
                return True
            if _is_self_attr(func, "assert") or _is_self_attr(func, "fail"):
                return True
            if isinstance(func, ast.Name) and func.id in _HELPER_FUNCTIONS:
                return True
            if isinstance(func, ast.Attribute) and func.attr in _HELPER_FUNCTIONS:
                return True
        if isinstance(child, ast.With):
            for item in child.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call) and _is_self_attr(ctx.func, "assert"):
                    return True
        if isinstance(child, ast.With):
            for item in child.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Attribute) and ctx.func.attr == "raises":
                    return True
    return False


def _is_allowlisted(source_lines: list[str], node: ast.FunctionDef) -> bool:
    """Check if the test function has a noqa: no-assert comment on its def line."""
    line_idx = node.lineno - 1
    if 0 <= line_idx < len(source_lines):
        return _NOQA_MARKER in source_lines[line_idx]
    return False


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-advisory",
        type=int,
        default=0,
        help="Fail if advisory zero-assertion count exceeds this threshold (0=no limit)",
    )
    args = parser.parse_args()

    tests_dir = Path("tests")
    if not tests_dir.is_dir():
        print("tests/ directory not found")
        return 0

    total = 0
    no_assert: list[str] = []

    for py_file in sorted(tests_dir.rglob("*.py")):
        if ".claude/worktrees/" in str(py_file):
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except SyntaxError:
            continue
        source_lines = source.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                total += 1
                if not _has_assertion(node) and not _is_allowlisted(source_lines, node):
                    no_assert.append(f"  {py_file}:{node.lineno} {node.name}")

    critical = [e for e in no_assert if _is_critical(e)]
    advisory = [e for e in no_assert if not _is_critical(e)]

    if advisory:
        print(f"Advisory — tests without assertions ({len(advisory)}):")
        for line in advisory[:50]:
            print(line)
        if len(advisory) > 50:
            print(f"  ... and {len(advisory) - 50} more")

    if critical:
        print(f"\nCRITICAL — zero-assertion tests in critical-path dirs ({len(critical)}):")
        for line in critical:
            print(line)

    print(f"\nFound {total} tests, {len(no_assert)} without assertions "
          f"({len(critical)} critical, {len(advisory)} advisory)")

    if critical:
        return 1
    if args.max_advisory > 0 and len(advisory) > args.max_advisory:
        print(f"\n::error::Advisory zero-assertion count ({len(advisory)}) exceeds "
              f"threshold ({args.max_advisory}). Add assertions that verify the intended behavior.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
