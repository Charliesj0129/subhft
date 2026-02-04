"""Darwin Gate — Benchmark regression checker.

Compares current benchmark results against a baseline and fails
if any benchmark regresses beyond the configured threshold.

Usage:
    python scripts/benchmark_gate.py \
        --baseline tests/benchmark/.benchmark_baseline.json \
        --current benchmark.json \
        --threshold 0.10
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REGRESSION_THRESHOLD = 0.10  # 10% default


def load_benchmarks(path: Path) -> dict[str, float]:
    """Load benchmark JSON and return {name: mean_time} mapping."""
    data = json.loads(path.read_text())
    results: dict[str, float] = {}
    for bench in data.get("benchmarks", []):
        name = bench.get("name", bench.get("fullname", "unknown"))
        stats = bench.get("stats", {})
        mean = stats.get("mean")
        if mean is not None:
            results[name] = mean
    return results


def check_regressions(
    baseline: dict[str, float],
    current: dict[str, float],
    threshold: float,
) -> list[tuple[str, float, float, float]]:
    """Return list of (name, baseline_mean, current_mean, pct_change) for regressions."""
    regressions: list[tuple[str, float, float, float]] = []
    for name, base_mean in baseline.items():
        if name not in current:
            continue
        curr_mean = current[name]
        if base_mean <= 0:
            continue
        pct_change = (curr_mean - base_mean) / base_mean
        if pct_change > threshold:
            regressions.append((name, base_mean, curr_mean, pct_change))
    return regressions


def main() -> int:
    parser = argparse.ArgumentParser(description="Darwin Gate — Benchmark regression checker")
    parser.add_argument("--baseline", type=Path, required=True, help="Path to baseline benchmark JSON")
    parser.add_argument("--current", type=Path, required=True, help="Path to current benchmark JSON")
    parser.add_argument(
        "--threshold",
        type=float,
        default=REGRESSION_THRESHOLD,
        help=f"Regression threshold (default: {REGRESSION_THRESHOLD})",
    )
    args = parser.parse_args()

    if not args.baseline.exists():
        print(f"[Darwin Gate] Baseline not found: {args.baseline} — skipping regression check")
        return 0

    if not args.current.exists():
        print(f"[Darwin Gate] Current results not found: {args.current} — skipping regression check")
        return 0

    baseline = load_benchmarks(args.baseline)
    current = load_benchmarks(args.current)

    if not baseline:
        print("[Darwin Gate] No benchmarks in baseline — skipping regression check")
        return 0

    if not current:
        print("[Darwin Gate] No benchmarks in current results — skipping regression check")
        return 0

    regressions = check_regressions(baseline, current, args.threshold)

    # Summary
    matched = sum(1 for name in baseline if name in current)
    print(f"[Darwin Gate] Compared {matched} benchmarks (threshold: {args.threshold:.0%})")

    if regressions:
        print(f"\n[Darwin Gate] FAILED — {len(regressions)} regression(s) detected:\n")
        for name, base_mean, curr_mean, pct in regressions:
            print(f"  {name}")
            print(f"    baseline: {base_mean:.6f}s  current: {curr_mean:.6f}s  change: +{pct:.1%}")
            print()
        return 1

    print("[Darwin Gate] PASSED — no regressions detected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
