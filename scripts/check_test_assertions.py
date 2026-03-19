"""Check test functions have at least one assertion. Exits 0 always."""

from __future__ import annotations
import ast, sys
from pathlib import Path


def _has_assert(node):
    for c in ast.walk(node):
        if isinstance(c, ast.Assert):
            return True
        if isinstance(c, ast.Call):
            f = c.func
            if isinstance(f, ast.Attribute) and (f.attr == "raises" or f.attr.startswith("assert_")):
                return True
    return False


def main():
    total, no_a = 0, []
    for f in sorted(Path("tests").rglob("*.py")):
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name.startswith("test_"):
                total += 1
                if not _has_assert(n):
                    no_a.append(f"  {f}:{n.lineno} {n.name}")
    if no_a:
        print(f"Tests without assertions ({len(no_a)}):")
        for l in no_a[:50]:
            print(l)
        if len(no_a) > 50:
            print(f"  ... and {len(no_a) - 50} more")
    print(f"\nFound {total} tests, {len(no_a)} without assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
