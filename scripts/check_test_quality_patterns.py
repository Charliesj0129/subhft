"""Detect narrow, high-signal weak test assertion patterns."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

TEST_FUNCTION_NODE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


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


def _same_ast(left: ast.AST, right: ast.AST) -> bool:
    return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def _is_none_expr(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _is_tautological_or_assert(expr: ast.AST) -> bool:
    if not isinstance(expr, ast.BoolOp) or not isinstance(expr.op, ast.Or) or len(expr.values) != 2:
        return False

    left, right = expr.values
    if not isinstance(left, ast.Compare) or not isinstance(right, ast.Compare):
        return False
    if len(left.ops) != 1 or len(right.ops) != 1:
        return False
    if len(left.comparators) != 1 or len(right.comparators) != 1:
        return False
    if not _same_ast(left.left, right.left):
        return False
    if not _same_ast(left.comparators[0], right.comparators[0]):
        return False

    op_pair = (type(left.ops[0]), type(right.ops[0]))
    if op_pair in ((ast.Is, ast.IsNot), (ast.IsNot, ast.Is)):
        return _is_none_expr(left.comparators[0]) and _is_none_expr(right.comparators[0])
    if op_pair in ((ast.Eq, ast.NotEq), (ast.NotEq, ast.Eq)):
        return True
    return False


def _is_blanket_except_pass(handler: ast.ExceptHandler) -> bool:
    if not isinstance(handler.type, ast.Name) or handler.type.id != "Exception":
        return False
    return len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass)


def _collect_violations(py_file: Path, node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    violations: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Assert) and _is_tautological_or_assert(child.test):
            violations.append(
                f"  {py_file}:{child.lineno} {node.name}: tautological assertion "
                f"('{ast.unparse(child.test)}')"
            )
        if isinstance(child, ast.Try):
            for handler in child.handlers:
                if _is_blanket_except_pass(handler):
                    violations.append(
                        f"  {py_file}:{handler.lineno} {node.name}: blanket 'except Exception: pass'"
                    )
    return violations


def main() -> int:
    tests_dir = Path("tests")
    if not tests_dir.is_dir():
        print("tests/ directory not found")
        return 0

    violations: list[str] = []

    for py_file in sorted(tests_dir.rglob("test*.py")):
        if ".claude/worktrees/" in str(py_file):
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in _iter_collectable_test_nodes(tree):
            violations.extend(_collect_violations(py_file, node))

    if violations:
        print("Weak test quality patterns found:")
        for line in violations:
            print(line)
        print(
            "\nReplace tautologies and blanket swallow blocks with assertions that "
            "prove a concrete postcondition."
        )
        return 1

    print("No weak test quality patterns found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
