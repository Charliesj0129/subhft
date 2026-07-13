"""Darwin Gate — Benchmark regression checker.

Compares current benchmark results against a baseline and fails if any
benchmark regresses beyond the configured threshold.

CI runs baseline and current on different shared runners whose speeds
differ by tens of percent, so raw mean-vs-mean comparison is noise-dominated
(observed: all six benchmarks "regressing" 26-52% in lockstep with zero code
changes). The gate therefore normalizes each benchmark's current/baseline
ratio by the median ratio across all benchmarks: a runner-wide speed shift
moves every ratio equally and cancels out, while a genuine regression in one
code path stands out against the rest. A separate unnormalized catastrophic
threshold still catches a uniform real slowdown that median normalization
would otherwise absorb (at the cost of tolerating anything below it when ALL
benchmarks regress together — with per-path regressions that is runner noise
far more often than code). Relative detection needs >=2 benchmarks; with a
single benchmark only the catastrophic threshold applies.

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
from statistics import median

REGRESSION_THRESHOLD = 0.10  # 10% default, on runner-speed-normalized change
CATASTROPHIC_THRESHOLD = 2.0  # +200% raw change fails even if uniform across benchmarks


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
    catastrophic_threshold: float = CATASTROPHIC_THRESHOLD,
) -> tuple[list[tuple[str, float, float, float, float]], float]:
    """Compare runner-speed-normalized changes against the threshold.

    Returns (regressions, speed_factor) where speed_factor is the median
    current/baseline ratio (the runner-speed shift between the two runs) and
    each regression is (name, baseline_mean, current_mean, normalized_change,
    raw_change). A benchmark regresses when its normalized change exceeds
    `threshold` or its raw change exceeds `catastrophic_threshold`.
    """
    ratios: dict[str, float] = {}
    for name, base_mean in baseline.items():
        if name not in current or base_mean <= 0:
            continue
        ratios[name] = current[name] / base_mean

    if not ratios:
        return [], 1.0

    speed_factor = median(ratios.values())
    regressions: list[tuple[str, float, float, float, float]] = []
    for name, ratio in ratios.items():
        normalized_change = ratio / speed_factor - 1.0
        raw_change = ratio - 1.0
        if normalized_change > threshold or raw_change > catastrophic_threshold:
            regressions.append((name, baseline[name], current[name], normalized_change, raw_change))
    return regressions, speed_factor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Darwin Gate — Benchmark regression checker")
    parser.add_argument("--baseline", type=Path, required=True, help="Path to baseline benchmark JSON")
    parser.add_argument("--current", type=Path, required=True, help="Path to current benchmark JSON")
    parser.add_argument(
        "--threshold",
        type=float,
        default=REGRESSION_THRESHOLD,
        help=f"Regression threshold on normalized change (default: {REGRESSION_THRESHOLD})",
    )
    parser.add_argument(
        "--catastrophic-threshold",
        type=float,
        default=CATASTROPHIC_THRESHOLD,
        help=f"Raw-change threshold that fails even runner-wide slowdowns (default: {CATASTROPHIC_THRESHOLD})",
    )
    args = parser.parse_args(argv)

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

    regressions, speed_factor = check_regressions(baseline, current, args.threshold, args.catastrophic_threshold)

    # Summary
    matched = sum(1 for name in baseline if name in current)
    print(f"[Darwin Gate] Compared {matched} benchmarks (threshold: {args.threshold:.0%})")
    print(f"[Darwin Gate] Runner speed factor vs baseline: {speed_factor:.2f}x (normalized out)")

    if regressions:
        print(f"\n[Darwin Gate] FAILED — {len(regressions)} regression(s) detected:\n")
        for name, base_mean, curr_mean, normalized, raw in regressions:
            print(f"  {name}")
            print(
                f"    baseline: {base_mean:.6f}s  current: {curr_mean:.6f}s  "
                f"normalized: +{normalized:.1%}  raw: +{raw:.1%}"
            )
            print()
        return 1

    print("[Darwin Gate] PASSED — no regressions detected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
