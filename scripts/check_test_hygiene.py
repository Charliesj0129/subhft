"""Block new script-style patterns in core test suites.

Legacy files are explicitly allowlisted so the gate can land without masking
where the historical debt still lives. New or cleaned files must stay free of
``print()`` and ``__main__`` entrypoints.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CORE_DIRS = (
    Path("tests/unit"),
    Path("tests/integration"),
    Path("tests/spec"),
    Path("tests/acceptance"),
    Path("tests/blackbox"),
    Path("tests/regression"),
    Path("tests/chaos"),
    Path("tests/system"),
)

ROOT_TEST_GLOB = "test_*.py"
PRINT_RE = re.compile(r"^\s*print\(")
MAIN_RE = re.compile(r'^\s*if __name__ == ["\']__main__["\']:')
BAD_FILENAME_SUFFIXES = ("_cov.py",)
LEGACY_ALLOWLIST: set[Path] = {
    Path("tests/integration/test_full_cycle.py"),
    Path("tests/integration/test_persistence.py"),
    Path("tests/integration/test_regime_alpha.py"),
    Path("tests/integration/test_reversal_alpha.py"),
    Path("tests/integration/test_risk_and_safety.py"),
    Path("tests/integration/test_rust_alpha.py"),
    Path("tests/integration/test_stormguard_integration.py"),
    Path("tests/integration/test_strategy_logic.py"),
    Path("tests/spec/test_replay.py"),
    Path("tests/test_strategy_sdk.py"),
    Path("tests/unit/test_adapter_validation.py"),
    Path("tests/unit/test_feature_engine_default_on.py"),
    Path("tests/unit/test_hawkes_criticality.py"),
    Path("tests/unit/test_loader_dlq.py"),
    Path("tests/unit/test_price_band_validator.py"),
    Path("tests/unit/test_risk_extended_validators.py"),
    Path("tests/unit/test_rust_flow.py"),
    Path("tests/unit/test_rust_markov.py"),
    Path("tests/unit/test_rust_ofi.py"),
    Path("tests/unit/test_rust_transient.py"),
    Path("tests/unit/test_transient_reprice.py"),
    Path("tests/unit/test_writer_retry.py"),
}


def _iter_targets() -> list[Path]:
    files: set[Path] = set()
    for directory in CORE_DIRS:
        if directory.is_dir():
            files.update(directory.rglob("*.py"))
    tests_root = Path("tests")
    if tests_root.is_dir():
        files.update(tests_root.glob(ROOT_TEST_GLOB))
    return sorted(files)


def main() -> int:
    violations: list[str] = []

    for path in _iter_targets():
        if path in LEGACY_ALLOWLIST:
            continue
        if path.name.endswith(BAD_FILENAME_SUFFIXES):
            violations.append(f"{path}: coverage-targeted *_cov.py files are not allowed in core pytest suites")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            print(f"WARNING: could not read {path}: {exc}")
            continue

        for lineno, line in enumerate(lines, start=1):
            if PRINT_RE.search(line):
                violations.append(f"{path}:{lineno}: print() is not allowed in core pytest suites")
            if MAIN_RE.search(line):
                violations.append(f"{path}:{lineno}: __main__ entrypoint is not allowed in core pytest suites")

    if violations:
        print("Core test hygiene violations:")
        for entry in violations:
            print(f"  {entry}")
        print(
            "\n::error::Remove ad-hoc print() calls, __main__ blocks, and coverage-only *_cov.py files from core "
            "pytest suites. Use behavior-oriented test names and benchmark/manual directories for script-style flows."
        )
        return 1

    print("Core test hygiene OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
