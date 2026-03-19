#!/usr/bin/env python3
"""Detect zero-assertion tests (WU-15)."""
import ast, sys
from pathlib import Path

def find_zero_assertion_tests(test_dir="tests"):
    results = []
    for f in sorted(Path(test_dir).rglob("*.py")):
        try:
            tree = ast.parse(f.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_"):
                if not any(isinstance(c, ast.Assert) for c in ast.walk(n)) and \
                   not any(isinstance(c, ast.With) and any(isinstance(i.context_expr, ast.Call) and isinstance(i.context_expr.func, ast.Attribute) and i.context_expr.func.attr in ("raises","warns") for i in c.items) for c in ast.walk(n)):
                    results.append((str(f), n.name, n.lineno))
    return results

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--test-dir", default="tests")
    p.add_argument("--baseline", default="scripts/zero_assertion_baseline.txt")
    p.add_argument("--update-baseline", action="store_true")
    a = p.parse_args()
    r = find_zero_assertion_tests(a.test_dir)
    print(f"Zero-assertion tests: {len(r)}")
    bl = Path(a.baseline)
    if a.update_baseline:
        bl.parent.mkdir(parents=True, exist_ok=True)
        bl.write_text(str(len(r))+"\n")
        return 0
    if bl.exists() and len(r) > int(bl.read_text().strip()):
        print(f"FAIL: {len(r)} > baseline")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
