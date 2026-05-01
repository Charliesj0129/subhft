#!/usr/bin/env python3
"""Domain-weighted coverage gate for HFT platform.

Reads coverage.xml and enforces per-package line-coverage floors. Critical
trading-domain modules (risk/order/execution/gateway/recorder/alpha) carry
floors above the global 70% default so regressions in money-touching code
are visible immediately, not buried under aggregate average.

Aspirational long-term targets (see .agent/rules/50-testing.md):
- risk/order/execution/gateway: 95% line coverage
- recorder: 90%
- alpha governance: 80%

Today's floors are pinned slightly below current measured rates so the gate
is enforceable now and we ratchet upward as targeted test work lands.

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

# Per-package line-rate floors (percent). Values are pinned to current
# measured coverage (rounded down with a 1-2pt safety buffer) to keep the
# gate green on day one while still blocking regressions. Bump these up as
# coverage improves; do not lower them without an architecture review.
PACKAGE_FLOORS: dict[str, float] = {
    # Critical risk/execution domains - aspirational 95%
    "risk": 73.0,
    "order": 77.0,
    "execution": 75.0,
    "gateway": 76.0,
    # Recorder - aspirational 90%
    "recorder": 77.0,
    # Alpha governance - target 80%
    "alpha": 80.0,
    # Lower-tier ratchet anchors. These packages are below the 70% default
    # today (mostly entry-point glue and low-level IPC primitives covered
    # via integration tests). Their explicit floors prevent regression
    # while we incrementally add unit coverage. Bump as coverage grows.
    ".": 67.0,
    "ipc": 64.0,
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
