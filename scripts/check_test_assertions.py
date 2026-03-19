"""Test assertion check."""

from __future__ import annotations
import ast, sys
from pathlib import Path


def _ha(n):
    for c in ast.walk(n):
        if isinstance(c, ast.Assert):
            return True
        if isinstance(c, ast.Call):
            f = c.func
            if isinstance(f, ast.Attribute) and (f.attr == "raises" or f.attr.startswith("assert_")):
                return True
    return False


def main():
    t, na = 0, []
    for f in sorted(Path("tests").rglob("*.py")):
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name.startswith("test_"):
                t += 1
                if not _ha(n):
                    na.append(f"  {f}:{n.lineno} {n.name}")
    if na:
        print(f"Tests without assertions ({len(na)}):")
        for l in na[:50]:
            print(l)
        if len(na) > 50:
            print(f"  ... and {len(na) - 50} more")
    print(f"\nFound {t} tests, {len(na)} without assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
