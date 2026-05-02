#!/usr/bin/env python3
"""HFT Platform discipline enforcement.

Enforces coding standards from CLAUDE.md (The Constitution) via AST analysis.
Usable as pre-commit hook, CI check, or importable module.

Usage:
    # Pre-commit (specific files)
    python scripts/check_discipline.py --files src/hft_platform/risk/engine.py

    # CI (full scan)
    python scripts/check_discipline.py --ci

    # Strict mode (fail on WARNING+)
    python scripts/check_discipline.py --ci --strict
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Sequence

# ---------------------------------------------------------------------------
# Severity & Violation model
# ---------------------------------------------------------------------------

class Severity(IntEnum):
    WARNING = 1
    HIGH = 2
    CRITICAL = 3


@dataclass(frozen=True, slots=True)
class Violation:
    file: str
    line: int
    severity: Severity
    rule_id: str
    message: str

    def __str__(self) -> str:
        sev = self.severity.name
        return f"{sev}: [{self.rule_id}] {self.file}:{self.line} — {self.message}"


# ---------------------------------------------------------------------------
# Path classification helpers
# ---------------------------------------------------------------------------

_HOT_PATH_PARTS = frozenset({"risk", "order", "execution"})
_HOT_PATH_FILES = frozenset({"normalizer.py", "lob_engine.py"})
_CORE_SERVICE_PARTS = frozenset({"services", "gateway", "engine", "recorder", "contracts"})

_SRC_ROOT = Path("src/hft_platform")


def _is_test_file(path: Path) -> bool:
    parts = path.parts
    return "tests" in parts or path.name.startswith("test_")


def _is_alpha_or_research(path: Path) -> bool:
    parts = path.parts
    return "alpha" in parts or "research" in parts


def _is_hot_path(path: Path) -> bool:
    parts = set(path.parts)
    if parts & _HOT_PATH_PARTS:
        return True
    if path.name in _HOT_PATH_FILES:
        return True
    # strategy/runner.py
    if "strategy" in parts and path.name == "runner.py":
        return True
    return False


def _is_core_module(path: Path) -> bool:
    """Hot path or core services (bootstrap, gateway, recorder, etc.)."""
    if _is_hot_path(path):
        return True
    parts = set(path.parts)
    return bool(parts & _CORE_SERVICE_PARTS)


def _is_feed_adapter_broker(path: Path) -> bool:
    """True if path is inside feed_adapter/<broker>/."""
    parts = path.parts
    try:
        idx = parts.index("feed_adapter")
        # Must have a sub-package (shioaji/, fubon/, etc.)
        return idx + 1 < len(parts) and parts[idx + 1] not in ("__init__.py",)
    except ValueError:
        return False


def _is_broker_import_allowed(path: Path) -> bool:
    """Broker SDK imports are allowed in feed_adapter/<broker>/, CLI, and config modules."""
    if _is_feed_adapter_broker(path):
        return True
    parts = set(path.parts)
    # CLI and config modules need broker SDK for contract resolution and utilities
    if parts & {"config"}:
        return True
    # cli.py or cli/ subdirectory
    if "cli" in parts or path.stem == "cli" or path.name.startswith("cli"):
        return True
    return False


def _is_contracts_module(path: Path) -> bool:
    return "contracts" in path.parts


def _is_events_module(path: Path) -> bool:
    return path.name == "events.py"


# ---------------------------------------------------------------------------
# AST-based checkers
# ---------------------------------------------------------------------------

def check_silent_except(tree: ast.Module, path: Path) -> list[Violation]:
    """HFT-D001: Silent exception swallowing (except ...: pass).

    CRITICAL in core modules (hot path, services, gateway, recorder, contracts).
    WARNING in peripheral modules (alpha, monitor, backtest, CLI, features).
    """
    if _is_test_file(path):
        return []

    severity = Severity.CRITICAL if _is_core_module(path) else Severity.WARNING
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        # Check if body is just `pass` (or `...`)
        body = node.body
        if len(body) == 1:
            stmt = body[0]
            is_pass = isinstance(stmt, ast.Pass)
            is_ellipsis = (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and stmt.value.value is ...
            )
            if is_pass or is_ellipsis:
                violations.append(Violation(
                    file=str(path),
                    line=node.lineno,
                    severity=severity,
                    rule_id="HFT-D001",
                    message="Silent exception swallowing (except: pass/...)",
                ))
    return violations


def check_pytest_in_sys_modules(tree: ast.Module, path: Path) -> list[Violation]:
    """HFT-D002: Runtime pytest detection pattern in production code."""
    if _is_test_file(path):
        return []

    violations: list[Violation] = []

    for node in ast.walk(tree):
        # Match: "pytest" in sys.modules
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            if isinstance(node.ops[0], ast.In):
                left = node.left
                right = node.comparators[0] if node.comparators else None
                if (
                    isinstance(left, ast.Constant)
                    and left.value == "pytest"
                    and isinstance(right, ast.Attribute)
                    and isinstance(right.value, ast.Name)
                    and right.value.id == "sys"
                    and right.attr == "modules"
                ):
                    violations.append(Violation(
                        file=str(path),
                        line=node.lineno,
                        severity=Severity.CRITICAL,
                        rule_id="HFT-D002",
                        message='"pytest" in sys.modules detected in production code',
                    ))
    return violations


def check_architecture_boundaries(tree: ast.Module, path: Path) -> list[Violation]:
    """HFT-A001/A002/A003: Architecture boundary violations."""
    if _is_test_file(path):
        return []

    violations: list[Violation] = []
    broker_sdks = {"shioaji", "fubon_neo"}

    for node in ast.walk(tree):
        imported_names: list[tuple[str, int]] = []

        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.append((node.module, node.lineno))

        for mod_name, lineno in imported_names:
            top_level = mod_name.split(".")[0]

            # A001: Broker SDK imports outside allowed paths
            if top_level in broker_sdks and not _is_broker_import_allowed(path):
                violations.append(Violation(
                    file=str(path),
                    line=lineno,
                    severity=Severity.HIGH,
                    rule_id="HFT-A001",
                    message=f"Broker-specific import '{top_level}' outside feed_adapter/<broker>/",
                ))

            # A002: contracts module importing runtime services
            # (intra-contracts imports like contracts.strategy are allowed)
            if _is_contracts_module(path):
                runtime_pkgs = {"services", "risk", "order", "execution", "recorder"}
                mod_parts = mod_name.split(".")
                # Skip if the import is within contracts package itself
                is_intra_contracts = "contracts" in mod_parts
                if not is_intra_contracts:
                    for part in mod_parts:
                        if part in runtime_pkgs:
                            violations.append(Violation(
                                file=str(path),
                                line=lineno,
                                severity=Severity.HIGH,
                                rule_id="HFT-A002",
                                message=f"contracts module imports runtime service '{part}'",
                            ))
                            break

            # A003: events.py importing strategy or execution
            if _is_events_module(path):
                forbidden = {"strategy", "execution"}
                mod_parts = mod_name.split(".")
                for part in mod_parts:
                    if part in forbidden:
                        violations.append(Violation(
                            file=str(path),
                            line=lineno,
                            severity=Severity.HIGH,
                            rule_id="HFT-A003",
                            message=f"events module imports '{part}' (forbidden dependency)",
                        ))
                        break

    return violations


def check_hot_path_antipatterns(tree: ast.Module, path: Path) -> list[Violation]:
    """HFT-P001/P002/P003: Hot-path anti-patterns."""
    if not _is_hot_path(path) or _is_test_file(path):
        return []

    violations: list[Violation] = []

    for node in ast.walk(tree):
        # P001: datetime.now() or time.time()
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            if isinstance(func.value, ast.Name):
                if func.value.id == "datetime" and func.attr == "now":
                    violations.append(Violation(
                        file=str(path),
                        line=node.lineno,
                        severity=Severity.HIGH,
                        rule_id="HFT-P001",
                        message="datetime.now() on hot path; use timebase.now_ns()",
                    ))
                if func.value.id == "time" and func.attr == "time":
                    violations.append(Violation(
                        file=str(path),
                        line=node.lineno,
                        severity=Severity.HIGH,
                        rule_id="HFT-P001",
                        message="time.time() on hot path; use timebase.now_ns()",
                    ))

        # P002: import pandas
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pandas" or alias.name.startswith("pandas."):
                    violations.append(Violation(
                        file=str(path),
                        line=node.lineno,
                        severity=Severity.HIGH,
                        rule_id="HFT-P002",
                        message="pandas import on hot path (too slow)",
                    ))
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "pandas" or node.module.startswith("pandas."):
                violations.append(Violation(
                    file=str(path),
                    line=node.lineno,
                    severity=Severity.HIGH,
                    rule_id="HFT-P002",
                    message="pandas import on hot path (too slow)",
                ))

        # P003: requests.get / requests.post
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            if (
                isinstance(func.value, ast.Name)
                and func.value.id == "requests"
                and func.attr in ("get", "post", "put", "delete", "patch")
            ):
                violations.append(Violation(
                    file=str(path),
                    line=node.lineno,
                    severity=Severity.HIGH,
                    rule_id="HFT-P003",
                    message=f"requests.{func.attr}() on hot path; use async HTTP",
                ))

    return violations


def _handler_has_reraise(handler: ast.ExceptHandler) -> bool:
    """Check if an except handler re-raises somewhere in its body."""
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise):
            return True
    return False


def _handler_has_logging(handler: ast.ExceptHandler) -> bool:
    """Heuristic: handler calls something that looks like logging."""
    log_names = {"log", "logger", "logging", "structlog"}
    log_methods = {"debug", "info", "warning", "error", "critical", "exception", "msg"}
    for node in ast.walk(handler):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            if func.attr in log_methods:
                if isinstance(func.value, ast.Name) and func.value.id in log_names:
                    return True
                # chained: self.log.error, self._log.warning
                if isinstance(func.value, ast.Attribute):
                    if func.value.attr in log_names or func.value.attr.startswith("_log"):
                        return True
    return False


def check_except_without_reraise(tree: ast.Module, path: Path) -> list[Violation]:
    """HFT-D003: Broad except without re-raise.

    In core modules: CRITICAL if no logging, HIGH if logged but no re-raise.
    In peripheral modules: WARNING regardless (existing tech debt).
    """
    if _is_test_file(path) or _is_alpha_or_research(path):
        return []

    is_core = _is_core_module(path)

    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue

        # Only flag broad catches: `except Exception` or bare `except`
        is_broad = node.type is None  # bare except
        if isinstance(node.type, ast.Name) and node.type.id == "Exception":
            is_broad = True

        if not is_broad:
            continue

        if _handler_has_reraise(node):
            continue

        # Already covered by D001 (pass-only body)
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            continue

        has_logging = _handler_has_logging(node)

        if is_core:
            severity = Severity.WARNING if has_logging else Severity.CRITICAL
        else:
            severity = Severity.WARNING

        violations.append(Violation(
            file=str(path),
            line=node.lineno,
            severity=severity,
            rule_id="HFT-D003",
            message="Broad 'except Exception' without re-raise",
        ))

    return violations


# ---------------------------------------------------------------------------
# Checker registry
# ---------------------------------------------------------------------------



# Money-related field-name fragments. Matched as exact name, prefix
# ("<pat>_..."), suffix ("..._<pat>"), or interior ("..._<pat>_...").
_MONEY_FIELD_PATTERNS: tuple[str, ...] = (
    "price", "bid", "ask", "mid",
    "balance", "equity", "cash", "capital", "notional",
    "pnl", "profit", "loss",
    "fee", "commission", "tax", "cost",
    "premium",
)

# Domains where money values must be Decimal/scaled-int per CLAUDE.md Law 4.
_MONEY_DOMAIN_PARTS = frozenset({"contracts", "order", "execution", "risk"})

# Files exempt from HFT-P004. Scenario / what-if simulation code legitimately
# uses float for synthetic inputs (e.g., underlying_price sweeps in stress
# tests). Live trading paths in those domains remain enforced.
_HFT_P004_EXEMPT_FILES = frozenset({"stress_test.py"})


def _is_money_field_name(name: str) -> bool:
    lower = name.lower()
    for pattern in _MONEY_FIELD_PATTERNS:
        if (
            lower == pattern
            or lower.endswith("_" + pattern)
            or lower.startswith(pattern + "_")
            or "_" + pattern + "_" in lower
        ):
            return True
    return False


def _is_float_annotation(annotation: ast.expr | None) -> bool:
    """True if annotation resolves to bare `float`, `Optional[float]`, or `float | None`."""
    if annotation is None:
        return False
    if isinstance(annotation, ast.Name) and annotation.id == "float":
        return True
    if isinstance(annotation, ast.Subscript):
        return _is_float_annotation(annotation.slice)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _is_float_annotation(annotation.left) or _is_float_annotation(annotation.right)
    if isinstance(annotation, ast.Tuple):
        return any(_is_float_annotation(e) for e in annotation.elts)
    return False


def check_no_float_money(tree: ast.Module, path: Path) -> list[Violation]:
    """HFT-P004: float annotation on money-related field in price-precision domains.

    Enforces CLAUDE.md Law 4 (Precision): never use float for prices, balances,
    fees, P&L, or notionals. Use Decimal or scaled int (x10000).

    Scope: contracts/, order/, execution/, risk/ (excluding stress_test.py).
    """
    if _is_test_file(path):
        return []
    if path.name in _HFT_P004_EXEMPT_FILES:
        return []
    parts = set(path.parts)
    if not (parts & _MONEY_DOMAIN_PARTS):
        return []

    violations: list[Violation] = []

    for node in ast.walk(tree):
        # Annotated assignment / class attr:  price: float = 0.0
        if isinstance(node, ast.AnnAssign):
            target = node.target
            name: str | None = None
            if isinstance(target, ast.Name):
                name = target.id
            elif isinstance(target, ast.Attribute):
                name = target.attr
            if name and _is_money_field_name(name) and _is_float_annotation(node.annotation):
                violations.append(Violation(
                    file=str(path),
                    line=node.lineno,
                    severity=Severity.HIGH,
                    rule_id="HFT-P004",
                    message=f"'{name}: float' violates Law 4 (Precision); use Decimal or scaled int x10000",
                ))

        # Function parameter annotations:  def buy(price: float, ...)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arg_groups = (node.args.args, node.args.posonlyargs, node.args.kwonlyargs)
            for group in arg_groups:
                for arg in group:
                    if (
                        arg.arg
                        and _is_money_field_name(arg.arg)
                        and _is_float_annotation(arg.annotation)
                    ):
                        violations.append(Violation(
                            file=str(path),
                            line=arg.lineno,
                            severity=Severity.HIGH,
                            rule_id="HFT-P004",
                            message=(
                                f"parameter '{arg.arg}: float' violates Law 4 (Precision); "
                                "use Decimal or scaled int x10000"
                            ),
                        ))

    return violations


def check_file_size(tree: ast.Module, path: Path) -> list[Violation]:
    """HFT-S001: File size enforcement (800 line warning, 1500 line critical).

    Excludes __init__.py and __main__.py.
    """
    if path.name in ("__init__.py", "__main__.py"):
        return []
    if not str(path).startswith(str(_SRC_ROOT)):
        return []

    try:
        line_count = sum(1 for _ in path.open(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return []

    if line_count > 1500:
        return [Violation(
            file=str(path),
            line=1,
            severity=Severity.CRITICAL,
            rule_id="HFT-S001",
            message=f"File exceeds 1500 lines ({line_count} LOC)",
        )]
    if line_count > 800:
        return [Violation(
            file=str(path),
            line=1,
            severity=Severity.WARNING,
            rule_id="HFT-S001",
            message=f"File exceeds 800 lines ({line_count} LOC)",
        )]
    return []

ALL_CHECKS = [
    check_silent_except,
    check_pytest_in_sys_modules,
    check_architecture_boundaries,
    check_hot_path_antipatterns,
    check_no_float_money,
    check_except_without_reraise,
    check_file_size,
]


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------

def check_file(filepath: Path) -> list[Violation]:
    """Run all checks against a single Python file."""
    if filepath.suffix != ".py":
        return []

    try:
        source = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    violations: list[Violation] = []
    for checker in ALL_CHECKS:
        violations.extend(checker(tree, filepath))

    return violations


def scan_directory(root: Path) -> list[Violation]:
    """Recursively scan a directory for violations."""
    violations: list[Violation] = []
    for py_file in sorted(root.rglob("*.py")):
        violations.extend(check_file(py_file))
    return violations


# ---------------------------------------------------------------------------
# Report & CLI
# ---------------------------------------------------------------------------

_SEVERITY_COLORS = {
    Severity.WARNING: "\033[33m",   # yellow
    Severity.HIGH: "\033[91m",      # red
    Severity.CRITICAL: "\033[1;91m", # bold red
}
_RESET = "\033[0m"


def _is_blocking(v: Violation, *, strict: bool = False) -> bool:
    """Check if a single violation is blocking."""
    if strict:
        return v.severity >= Severity.WARNING
    return v.rule_id in _BLOCKING_RULES and v.severity >= Severity.HIGH


def print_report(
    violations: list[Violation], *, color: bool = True, strict: bool = False
) -> None:
    """Print a human-readable violation report."""
    if not violations:
        print("All checks passed.")
        return

    sorted_vs = sorted(violations, key=lambda v: (-v.severity, v.file, v.line))
    for v in sorted_vs:
        marker = " [BLOCKING]" if _is_blocking(v, strict=strict) else ""
        if color and sys.stdout.isatty():
            c = _SEVERITY_COLORS.get(v.severity, "")
            print(f"{c}{v}{marker}{_RESET}")
        else:
            print(f"{v}{marker}")

    n_blocking = sum(1 for v in violations if _is_blocking(v, strict=strict))
    n_warnings = len(violations) - n_blocking
    print(f"\n{len(violations)} violations ({n_blocking} blocking, {n_warnings} warnings)")

    if n_blocking > 0:
        print("FAILED: blocking violations found.")
    elif violations:
        print("PASSED (with non-blocking warnings).")
    else:
        print("PASSED: no violations found.")


# Rules that are hard boundaries (fail CI immediately).
# D-rules (D001/D002/D003) are reported but non-blocking in default mode.
# Only architecture (A*) and performance (P*) rules cause CI failure.
_BLOCKING_RULES = frozenset({
    "HFT-A001", "HFT-A002", "HFT-A003",
    "HFT-P001", "HFT-P002", "HFT-P003", "HFT-P004",
})


def should_fail(violations: list[Violation], *, strict: bool = False) -> bool:
    """Determine if the run should exit with failure.

    Default mode: only fail on blocking rules (architecture + performance).
    Strict mode: fail on any WARNING+ violation.
    """
    return any(_is_blocking(v, strict=strict) for v in violations)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="HFT Platform discipline enforcement",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--files",
        nargs="+",
        type=Path,
        help="Check specific files (pre-commit mode)",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Scan entire src/ directory",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 on WARNING+ (default: only CRITICAL/HIGH)",
    )

    args = parser.parse_args(argv)

    if not args.files and not args.ci:
        parser.error("Specify --files or --ci")

    violations: list[Violation] = []

    if args.ci:
        src_root = Path("src")
        if not src_root.is_dir():
            print(f"Error: {src_root} not found. Run from project root.", file=sys.stderr)
            return 2
        violations = scan_directory(src_root)
    elif args.files:
        for f in args.files:
            violations.extend(check_file(f))

    print_report(violations, strict=args.strict)

    if should_fail(violations, strict=args.strict):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
