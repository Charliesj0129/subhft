"""Check test functions have at least one assertion. Exits 0 always (advisory)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _has_assertion(node: ast.FunctionDef) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Assert):
            return True
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute) and (func.attr == "raises" or func.attr.startswith("assert_")):
                return True
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
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                total += 1
                if not _has_assertion(node):
                    no_assert.append(f"  {py_file}:{node.lineno} {node.name}")

    if no_assert:
        print(f"Tests without assertions ({len(no_assert)}):")
        for line in no_assert[:50]:
            print(line)
        if len(no_assert) > 50:
            print(f"  ... and {len(no_assert) - 50} more")

    print(f"\nFound {total} tests, {len(no_assert)} without assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
