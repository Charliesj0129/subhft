"""Enforce behavior-oriented test naming conventions."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

FORBIDDEN_FUNCTION_PREFIXES = ("test_covers_", "test_line_", "test_cov_")
FORBIDDEN_FILE_SUFFIXES = ("_cov.py",)
TEST_FUNCTION_NODE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _is_forbidden_test_name(name: str) -> bool:
    return name.startswith(FORBIDDEN_FUNCTION_PREFIXES)


def _is_collectable_test_function(node: ast.AST) -> bool:
    return isinstance(node, TEST_FUNCTION_NODE_TYPES) and node.name.startswith("test_")


def _is_collectable_test_class(node: ast.AST) -> bool:
    return isinstance(node, ast.ClassDef) and node.name.startswith("Test")


def _iter_collectable_test_nodes(tree: ast.Module):
    for node in tree.body:
        if _is_collectable_test_function(node):
            yield node
            continue

        if not _is_collectable_test_class(node):
            continue

        for child in node.body:
            if _is_collectable_test_function(child):
                yield child


def main() -> int:
    tests_dir = Path("tests")
    if not tests_dir.is_dir():
        print("tests/ directory not found")
        return 0

    violations: list[str] = []

    for py_file in sorted(tests_dir.rglob("test*.py")):
        if py_file.name.endswith(FORBIDDEN_FILE_SUFFIXES):
            violations.append(f"  {py_file}: forbidden coverage-style filename")
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in _iter_collectable_test_nodes(tree):
            if _is_forbidden_test_name(node.name):
                violations.append(f"  {py_file}:{node.lineno} {node.name}")

    if violations:
        print("Forbidden test names found:")
        for line in violations:
            print(line)
        print(
            "\nUse behavior-oriented names such as 'test_rejects_order_when_halt' "
            "instead of coverage-oriented names."
        )
        return 1

    print("No forbidden coverage-style test names found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
