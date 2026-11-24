"""Fail unless the hft-engine runtime keeps the GIL disabled on free-threaded CPython.

Run it with the 3.14t runtime venv (built from requirements/runtime-py314t.lock)::

    PYTHONPATH=src ~/hft_worktrees/_py314t_runtime_venv/bin/python scripts/py314t/check_gil.py

Importing one C extension that has not declared Py_mod_gil silently turns the GIL
back on for the whole process, and it can never be turned off again. So the script
checks `sys._is_gil_enabled()` after every single import and names the first
import that flipped it, together with the CPython warning that names the extension.

It refuses to run when PYTHON_GIL or `-X gil` is set: forcing the GIL off would hide
exactly the failure this script exists to find.

Import order is leaf first (dependencies, risk, execution, shioaji adapter, strategy
runner, market data, system) so a flip lands on the narrowest module. Modules that
fail to import are listed with the file and line that raised. A static scan also lists
every import of a dependency that is excluded from the 3.14t runtime set (orjson,
hftbacktest), because most of those imports are lazy or guarded and never fail here.

Exit codes: 0 clean, 1 the GIL got enabled, 2 GIL still off but modules failed to
import, 3 precondition failed (not free-threaded, or the GIL was forced).
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import os
import sys
import sysconfig
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
PACKAGE = "hft_platform"

RUNTIME_DEPS = (
    "numpy",
    "pandas",
    "clickhouse_connect",
    "structlog",
    "msgspec",
    "shioaji",
    "prometheus_client",
    "psutil",
    "yaml",
    "numba",
    "scipy",
    "exchange_calendars",
    "aiohttp",
    "uvloop",
)

# Packages walked recursively (every module under them), in import order.
ENGINE_PACKAGES = (
    "hft_platform.risk",
    "hft_platform.execution",
    "hft_platform.feed_adapter.shioaji",
)
# Single modules imported after the packages. services.system pulls in most of the engine.
ENGINE_MODULES = (
    "hft_platform.strategy.runner",
    "hft_platform.services.market_data",
    "hft_platform.services.system",
)

# Dependencies left out of requirements/runtime-py314t.in (no cp314t wheel).
EXCLUDED_DEPS = ("orjson", "hftbacktest")

GIL_WARNING_MARKER = "global interpreter lock"


def package_modules(package: str) -> list[str]:
    """Return `package` and every module below it, found on disk without importing."""
    rel = Path(*package.split("."))
    root = SRC_ROOT / rel
    names = [package]
    for path in sorted(root.rglob("*.py")):
        if path.name == "__main__.py":
            continue
        parts = path.relative_to(SRC_ROOT).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        name = ".".join(parts)
        if name not in names:
            names.append(name)
    return names


def with_ancestors(name: str) -> list[str]:
    """Return every parent package of `name`, outermost first, then `name` itself."""
    parts = name.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


def build_targets() -> list[tuple[str, str]]:
    """Return (kind, module) pairs in import order, each module exactly once."""
    targets: list[tuple[str, str]] = [("dependency", dep) for dep in RUNTIME_DEPS]
    seen: set[str] = set()
    engine_names: list[str] = []
    for package in ENGINE_PACKAGES:
        engine_names.extend(package_modules(package))
    engine_names.extend(ENGINE_MODULES)
    for name in engine_names:
        for chain_name in with_ancestors(name):
            if chain_name not in seen:
                seen.add(chain_name)
                targets.append(("engine", chain_name))
    return targets


def failure_location(exc: BaseException) -> str:
    """Return file:line of the innermost frame that raised `exc`, skipping importlib and this script.

    A module that is simply absent raises from inside importlib only; there is no source
    line to name, so the answer is then "n/a (module not found)".
    """
    skip_dir = str(Path(importlib.__file__).resolve().parent)
    skip_self = str(Path(__file__).resolve())
    tb = exc.__traceback__
    location = "n/a (module not found)"
    while tb is not None:
        filename = tb.tb_frame.f_code.co_filename
        resolved = str(Path(filename).resolve()) if not filename.startswith("<") else filename
        if not filename.startswith("<") and resolved != skip_self and not resolved.startswith(skip_dir):
            location = f"{filename}:{tb.tb_lineno}"
        tb = tb.tb_next
    root_prefix = str(REPO_ROOT) + os.sep
    return location[len(root_prefix) :] if location.startswith(root_prefix) else location


def import_checked(name: str) -> dict[str, object]:
    """Import `name` and record whether it worked and what the GIL looked like after."""
    gil_before = sys._is_gil_enabled()
    error: str | None = None
    location: str | None = None
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            importlib.import_module(name)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            location = failure_location(exc)
    gil_after = sys._is_gil_enabled()
    gil_warnings = [str(w.message) for w in caught if GIL_WARNING_MARKER in str(w.message).lower()]
    return {
        "module": name,
        "ok": error is None,
        "error": error,
        "error_location": location,
        "gil_before": gil_before,
        "gil_enabled_after": gil_after,
        "flipped_gil": gil_after and not gil_before,
        "gil_warnings": gil_warnings,
    }


def is_import_guard(handler: ast.ExceptHandler) -> bool:
    """True when `handler` catches ImportError, ModuleNotFoundError, Exception or everything."""
    if handler.type is None:
        return True
    caught = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    names = {getattr(item, "id", getattr(item, "attr", "")) for item in caught}
    return bool(names & {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"})


def scan_excluded_users() -> list[dict[str, object]]:
    """List every import of an excluded dependency under src/hft_platform, with file:line."""
    found: list[dict[str, object]] = []
    for path in sorted((SRC_ROOT / PACKAGE).rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            found.append({"location": f"{path.relative_to(REPO_ROOT)}:0", "error": f"unparseable: {exc}"})
            continue
        guarded_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and any(is_import_guard(h) for h in node.handlers):
                for stmt in node.body:
                    guarded_lines.update(range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module.split(".")[0]]
            else:
                continue
            for module in modules:
                if module in EXCLUDED_DEPS:
                    found.append(
                        {
                            "dependency": module,
                            "location": f"{path.relative_to(REPO_ROOT)}:{node.lineno}",
                            "guarded_by_try": node.lineno in guarded_lines,
                        }
                    )
    return found


def precondition_error() -> str | None:
    """Return why the check cannot say anything useful, or None when it can."""
    if not hasattr(sys, "_is_gil_enabled"):
        return f"python {sys.version.split()[0]} has no sys._is_gil_enabled (needs CPython 3.13+ free-threaded)"
    if os.environ.get("PYTHON_GIL") is not None:
        return "PYTHON_GIL is set; it forces the GIL and would hide a re-enabling import"
    if getattr(sys.flags, "gil", None) is not None:
        return "-X gil is set; it forces the GIL and would hide a re-enabling import"
    if sys._is_gil_enabled():
        return "the GIL is already enabled before any import (not a free-threaded build)"
    return None


def run_checks(targets: list[tuple[str, str]]) -> dict[str, object]:
    """Import every target in order and build the report."""
    results = []
    first_flip: dict[str, object] | None = None
    for kind, name in targets:
        outcome = import_checked(name)
        outcome["kind"] = kind
        if outcome["flipped_gil"] and first_flip is None:
            first_flip = outcome
        results.append(outcome)
    failures = [r for r in results if not r["ok"]]
    return {
        "python_version": sys.version.split()[0],
        "free_threaded_build": sysconfig.get_config_var("Py_GIL_DISABLED") == 1,
        "gil_enabled_at_end": sys._is_gil_enabled(),
        "first_import_that_enabled_gil": first_flip["module"] if first_flip else None,
        "first_flip_warnings": first_flip["gil_warnings"] if first_flip else [],
        "modules_checked": len(results),
        "import_failures": [
            {"module": r["module"], "kind": r["kind"], "error": r["error"], "location": r["error_location"]}
            for r in failures
        ],
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print only the JSON report on the last line")
    parser.add_argument("--only", metavar="MODULE", help="check one module (and its parent packages) only")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    problem = precondition_error()
    if problem is not None:
        print(f"PRECONDITION FAILED: {problem}", file=sys.stderr)
        return 3
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    if args.only:
        targets = [("dependency" if args.only in RUNTIME_DEPS else "engine", n) for n in with_ancestors(args.only)]
    else:
        targets = build_targets()
    report = run_checks(targets)
    report["excluded_dependency_users"] = [] if args.only else scan_excluded_users()
    if args.json:
        print(json.dumps(report, default=str))
    else:
        print_report(report)
    if report["gil_enabled_at_end"]:
        return 1
    return 2 if report["import_failures"] else 0


def print_report(report: dict[str, object]) -> None:
    print(f"python {report['python_version']}  modules checked: {report['modules_checked']}")
    for r in report["results"]:
        mark = "ok  " if r["ok"] else "FAIL"
        flag = " GIL-ON" if r["gil_enabled_after"] else ""
        print(f"  [{mark}] {r['kind']:<10} {r['module']}{flag}")
    for failure in report["import_failures"]:
        print(f"IMPORT FAILURE {failure['module']}: {failure['error']} at {failure['location']}")
    for item in report["excluded_dependency_users"]:
        if "dependency" in item:
            print(
                f"EXCLUDED DEP {item['dependency']} used at {item['location']} (guarded_by_try={item['guarded_by_try']})"
            )
    flip = report["first_import_that_enabled_gil"]
    if flip:
        print(f"GIL RE-ENABLED by import of: {flip}")
        for text in report["first_flip_warnings"]:
            print(f"  warning: {text}")
    print(f"sys._is_gil_enabled() at end: {report['gil_enabled_at_end']}")


if __name__ == "__main__":
    sys.exit(main())
