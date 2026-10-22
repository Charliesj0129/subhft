#!/usr/bin/env python3
"""Domain-weighted coverage gate for HFT platform.

Reads coverage.xml and enforces per-package line-coverage floors. Critical
trading-domain modules (risk/order/execution/gateway/recorder/alpha) carry
floors above the global 70% default so regressions in money-touching code
are visible immediately, not buried under aggregate average.

Aspirational long-term targets (AGENTS.md states the hot-path target as >= 90%):
- risk/order/execution/gateway: 95% line coverage
- recorder: 90%
- alpha governance: 80%

Each floor is the line rate measured on a run with the Rust extension built,
minus 2 points, rounded down, so a drop of more than 2 points in a package
fails the gate instead of hiding under a floor 12-21 points below the
measurement. Re-measure with `make test-unit-ci && make coverage-domain` and
ratchet up; tests/unit/test_check_coverage_domains.py fails if one is lowered.

Usage:
    python3 scripts/check_coverage_domains.py [coverage.xml]

Exit codes:
    0 - all packages meet their floor
    1 - one or more packages below floor
    2 - coverage.xml missing or unparseable
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Per-package line-rate floors (percent): measured minus 2 points, rounded
# down (measured 2026-10-15, main tree with the Rust extension built; the
# measured rate is in each comment). Bump these up as coverage improves; do
# not lower them without an architecture review.
PACKAGE_FLOORS: dict[str, float] = {
    # Critical risk/execution domains - aspirational 95%
    "risk": 92.0,  # measured 94.14
    "order": 90.0,  # measured 92.48
    "execution": 92.0,  # measured 94.23
    "gateway": 94.0,  # measured 96.11
    # Recorder - aspirational 90%
    "recorder": 87.0,  # measured 89.53
    # Alpha governance - target 80%
    "alpha": 90.0,  # measured 92.99
    # Ratchet anchors for the entry-point glue (".") and the low-level IPC
    # primitives, and for strategies.alpha below. Their explicit floors
    # prevent regression while we incrementally add unit coverage. Bump as
    # coverage grows.
    ".": 88.0,  # measured 90.80
    "ipc": 81.0,  # measured 83.82
    # strategies.alpha is live alpha strategy bindings exercised primarily
    # via integration / promotion gates rather than unit tests. Pinned
    # ratchet anchor (measured 22.78, already within 2 points); raise once
    # unit coverage lands.
    "strategies.alpha": 22.0,
}

# Floor applied to packages not listed in PACKAGE_FLOORS.
DEFAULT_FLOOR: float = 70.0


def main(coverage_path: Path) -> int:
    if not coverage_path.is_file():
        print(f"ERROR: coverage report not found at {coverage_path}", file=sys.stderr)
        print("Hint: run `make test-unit-ci` first to generate coverage.xml.", file=sys.stderr)
        return 2

    try:
        root = ET.parse(coverage_path).getroot()
    except ET.ParseError as exc:
        print(f"ERROR: failed to parse {coverage_path}: {exc}", file=sys.stderr)
        return 2

    rows: list[tuple[str, float, float]] = []
    failures: list[tuple[str, float, float]] = []

    for package in root.iter("package"):
        name = package.get("name", "<unnamed>")
        try:
            rate = float(package.get("line-rate", "0")) * 100.0
        except ValueError:
            continue
        floor = PACKAGE_FLOORS.get(name, DEFAULT_FLOOR)
        rows.append((name, rate, floor))
        if rate + 1e-9 < floor:
            failures.append((name, rate, floor))

    rows.sort()
    print("Domain-weighted coverage gate")
    print("=" * 60)
    print(f"{'Package':<25}{'Coverage':>12}{'Floor':>10}{'Status':>10}")
    print("-" * 60)
    for name, rate, floor in rows:
        status = "OK" if rate + 1e-9 >= floor else "FAIL"
        print(f"{name:<25}{rate:>11.2f}%{floor:>9.1f}%{status:>10}")
    print("-" * 60)

    if failures:
        print()
        print(f"FAILED: {len(failures)} package(s) below floor:", file=sys.stderr)
        for name, rate, floor in failures:
            print(f"  {name}: {rate:.2f}% < {floor:.1f}%", file=sys.stderr)
        return 1

    print(f"PASSED: all {len(rows)} package(s) meet domain floors")
    return 0


if __name__ == "__main__":
    coverage_file = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("coverage.xml")
    raise SystemExit(main(coverage_file))
