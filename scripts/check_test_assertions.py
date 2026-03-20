"""Check test functions have at least one assertion. Exits 1 if violations found (enforced)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Functions that are known test helpers containing assertions internally.
# Tests delegating to these are considered asserted.
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

# unittest.TestCase assertion methods to detect self.assert*() calls explicitly.
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

# Comment marker to allowlist benchmark/smoke tests that intentionally have no assertions.
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
        # bare ``assert`` statement
        if isinstance(child, ast.Assert):
            return True
        # regular function / method calls
        if isinstance(child, ast.Call):
            func = child.func
            # pytest.raises / pytest style
            if isinstance(func, ast.Attribute) and (
                func.attr == "raises" or func.attr.startswith("assert")
            ):
                return True
            # unittest self.assert* (explicit method set), self.fail
            if isinstance(func, ast.Attribute) and func.attr in UNITTEST_ASSERT_METHODS:
                return True
            if _is_self_attr(func, "assert") or _is_self_attr(func, "fail"):
                return True
            # Helper-delegation detection: calls to known assertion helpers
            if isinstance(func, ast.Name) and func.id in _HELPER_FUNCTIONS:
                return True
            if isinstance(func, ast.Attribute) and func.attr in _HELPER_FUNCTIONS:
                return True
        # ``with self.assertRaises(...):`` used as context manager
        if isinstance(child, ast.With):
            for item in child.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call) and _is_self_attr(ctx.func, "assert"):
                    return True
        # ``with pytest.raises(...):`` used as context manager
        if isinstance(child, ast.With):
            for item in child.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Attribute) and ctx.func.attr == "raises":
                    return True
    return False


def _is_allowlisted(source_lines: list[str], node: ast.FunctionDef) -> bool:
    """Check if the test function has a noqa: no-assert comment on its def line."""
    line_idx = node.lineno - 1  # 0-based
    if 0 <= line_idx < len(source_lines):
        return _NOQA_MARKER in source_lines[line_idx]
    return False


def main() -> int:
    tests_dir = Path("tests")
    if not tests_dir.is_dir():
        print("tests/ directory not found")
        return 0

    total = 0
    no_assert: list[str] = []

    for py_file in sorted(tests_dir.rglob("*.py")):
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

    if no_assert:
        print(f"Tests without assertions ({len(no_assert)}):")
        for line in no_assert[:50]:
            print(line)
        if len(no_assert) > 50:
            print(f"  ... and {len(no_assert) - 50} more")

    print(f"\nFound {total} tests, {len(no_assert)} without assertions")
    return 1 if no_assert else 0


if __name__ == "__main__":
    sys.exit(main())
