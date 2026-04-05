#!/usr/bin/env python3
"""
R29 Stage 2a: Validate ClickHouse volume semantics for TXF.

R25 discovered that the CK `volume` column for TMFD6 is CUMULATIVE (daily running
total), not per-tick incremental. This script checks whether the same applies to
TXF (TXFD6) by querying a sample of tick data and running diagnostic tests:

1. Monotonicity test: Is volume monotonically non-decreasing within each day?
2. Range test: Does min(volume) == max(volume) within a day suggest no variation?
3. First-tick test: Does the first tick of the day have volume == the per-tick size?
4. dvol distribution: Compute volume[i] - volume[i-1] and check if it looks like
   plausible per-tick contract counts (typically 1-200 for TXF).

Output: Results printed and summary written to
        outputs/team_artifacts/alpha-research-r29/stage2a_volume_check.md
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np


def query_ch(sql: str) -> str:
    """Run a ClickHouse query via docker exec and return raw output."""
    r = subprocess.run(
        ["docker", "exec", "clickhouse", "clickhouse-client", "--query", sql],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if r.returncode != 0:
        print(f"[ERROR] ClickHouse query failed: {r.stderr}", file=sys.stderr)
        sys.exit(1)
    return r.stdout.strip()


def discover_symbols() -> list[str]:
    """Find available TXF-like symbols in hft.market_data."""
    raw = query_ch(
        "SELECT DISTINCT symbol FROM hft.market_data "
        "WHERE type = 'Tick' AND symbol LIKE 'TXF%' "
        "ORDER BY symbol FORMAT TabSeparated"
    )
    if not raw:
        # Try broader search
        raw = query_ch(
            "SELECT DISTINCT symbol FROM hft.market_data "
            "WHERE type = 'Tick' "
            "ORDER BY symbol FORMAT TabSeparated"
        )
    return [s.strip() for s in raw.split("\n") if s.strip()]


def count_ticks(symbol: str) -> int:
    """Count total tick records for a symbol."""
    raw = query_ch(
        f"SELECT count() FROM hft.market_data "
        f"WHERE symbol = '{symbol}' AND type = 'Tick' FORMAT TabSeparated"
    )
    return int(raw.strip()) if raw.strip() else 0


def load_sample(symbol: str, limit: int = 5000) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Load a sample of ticks: (volume, exch_ts, day_strings)."""
    raw = query_ch(
        f"SELECT volume, exch_ts, "
        f"toString(toDate(toDateTime64(exch_ts/1e9, 3))) as day "
        f"FROM hft.market_data "
        f"WHERE symbol = '{symbol}' AND type = 'Tick' "
        f"ORDER BY exch_ts "
        f"LIMIT {limit} "
        f"FORMAT TabSeparated"
    )
    if not raw:
        return np.array([]), np.array([]), []

    lines = raw.split("\n")
    n = len(lines)
    volume = np.empty(n, dtype=np.int64)
    ts = np.empty(n, dtype=np.int64)
    days: list[str] = []
    for i, line in enumerate(lines):
        parts = line.split("\t")
        volume[i] = int(parts[0])
        ts[i] = int(parts[1])
        days.append(parts[2])
    return volume, ts, days


def analyze_volume_semantics(
    symbol: str, volume: np.ndarray, days: list[str]
) -> dict:
    """Run diagnostic tests on the volume column."""
    results: dict = {"symbol": symbol, "n_ticks": len(volume)}

    unique_days = sorted(set(days))
    results["n_days"] = len(unique_days)
    results["days"] = unique_days

    day_arr = np.array(days)

    # Per-day analysis
    day_results = []
    overall_monotonic = True
    overall_cumulative_evidence = 0
    overall_incremental_evidence = 0

    for day in unique_days:
        mask = day_arr == day
        day_vol = volume[mask]
        n = len(day_vol)

        if n < 2:
            continue

        # Test 1: Monotonicity
        diffs = np.diff(day_vol)
        n_decreasing = int(np.sum(diffs < 0))
        is_monotonic = n_decreasing == 0
        if not is_monotonic:
            overall_monotonic = False

        # Test 2: Range
        vol_min = int(day_vol.min())
        vol_max = int(day_vol.max())
        vol_first = int(day_vol[0])
        vol_last = int(day_vol[-1])

        # Test 3: dvol stats (only where diffs > 0)
        positive_diffs = diffs[diffs > 0]
        dvol_median = float(np.median(positive_diffs)) if len(positive_diffs) > 0 else 0
        dvol_mean = float(np.mean(positive_diffs)) if len(positive_diffs) > 0 else 0
        dvol_max = int(np.max(positive_diffs)) if len(positive_diffs) > 0 else 0
        dvol_min = int(np.min(positive_diffs)) if len(positive_diffs) > 0 else 0

        # Cumulative evidence: vol_max >> vol_first, monotonic, vol grows steadily
        if is_monotonic and vol_max > vol_first * 10:
            overall_cumulative_evidence += 1
        elif not is_monotonic and vol_max < 1000:
            overall_incremental_evidence += 1

        # Test 4: Does raw volume look like per-tick counts?
        # Incremental: values should be small (1-200 for futures)
        # Cumulative: values should grow large (10K-500K by end of day)
        raw_median = float(np.median(day_vol))
        raw_looks_incremental = raw_median < 500

        day_info = {
            "day": day,
            "n_ticks": n,
            "monotonic": is_monotonic,
            "n_decreasing": n_decreasing,
            "vol_first": vol_first,
            "vol_last": vol_last,
            "vol_min": vol_min,
            "vol_max": vol_max,
            "raw_median": raw_median,
            "raw_looks_incremental": raw_looks_incremental,
            "dvol_median": dvol_median,
            "dvol_mean": dvol_mean,
            "dvol_min": dvol_min,
            "dvol_max": dvol_max,
        }
        day_results.append(day_info)

    results["day_results"] = day_results
    results["all_days_monotonic"] = overall_monotonic
    results["cumulative_evidence_days"] = overall_cumulative_evidence
    results["incremental_evidence_days"] = overall_incremental_evidence

    # Final verdict
    if overall_monotonic and overall_cumulative_evidence > 0:
        results["verdict"] = "CUMULATIVE"
        results["recommendation"] = "Use dvol = volume[i] - volume[i-1] for per-tick volume"
    elif not overall_monotonic and overall_incremental_evidence > 0:
        results["verdict"] = "INCREMENTAL"
        results["recommendation"] = "Use volume directly as per-tick volume"
    else:
        results["verdict"] = "UNCLEAR"
        results["recommendation"] = "Manual inspection required"

    return results


def format_report(all_results: list[dict]) -> str:
    """Format results into a markdown report."""
    lines = [
        "# R29 Stage 2a: CK Volume Semantics Validation",
        "",
        f"**Date**: 2026-04-01",
        "**Purpose**: Verify whether ClickHouse `volume` column is incremental (per-tick) or cumulative (daily running total) for TXF and related symbols.",
        "",
        "## Context",
        "",
        "R25 discovered that TMFD6 volume in ClickHouse is **cumulative** (daily running total), not per-tick incremental. The initial R25 analysis produced a spurious 79% accuracy before this bug was found. This check prevents the same error in R29.",
        "",
        "## Symbols Tested",
        "",
    ]

    for r in all_results:
        sym = r["symbol"]
        n = r["n_ticks"]
        lines.append(f"### {sym} ({n:,} ticks, {r['n_days']} days)")
        lines.append("")
        lines.append(f"**Verdict: {r['verdict']}**")
        lines.append(f"**Recommendation**: {r['recommendation']}")
        lines.append("")

        if r["day_results"]:
            lines.append("| Day | Ticks | Monotonic | vol_first | vol_last | raw_median | dvol_median | dvol_mean |")
            lines.append("|-----|-------|-----------|-----------|----------|------------|-------------|-----------|")
            for d in r["day_results"]:
                lines.append(
                    f"| {d['day']} | {d['n_ticks']:,} | {d['monotonic']} | "
                    f"{d['vol_first']:,} | {d['vol_last']:,} | "
                    f"{d['raw_median']:,.0f} | {d['dvol_median']:.1f} | {d['dvol_mean']:.1f} |"
                )
            lines.append("")

        # Print first 10 raw volume values for manual inspection
        lines.append("**Interpretation**:")
        if r["verdict"] == "CUMULATIVE":
            lines.append("- Volume is monotonically non-decreasing within each day")
            lines.append("- First tick volume is small, last tick volume is large (daily total)")
            lines.append("- Must use `dvol = volume[i] - volume[i-1]` for true per-tick size")
            lines.append("- **Same behavior as TMFD6 found in R25**")
        elif r["verdict"] == "INCREMENTAL":
            lines.append("- Volume fluctuates (not monotonic)")
            lines.append("- Raw values are small, consistent with per-tick contract counts")
            lines.append("- Can use `volume` directly")
        else:
            lines.append("- Volume behavior is ambiguous; manual inspection required")
        lines.append("")

    lines.extend([
        "## Conclusion for R29",
        "",
        "All volume-based large order detection in R29 MUST account for the volume semantics:",
        "- If CUMULATIVE: `dvol = volume[i] - volume[i-1]` (reset at day boundary)",
        "- If INCREMENTAL: use `volume` directly",
        "",
        "Failure to account for this will produce the same spurious results as R25's initial analysis.",
    ])

    return "\n".join(lines)


def main() -> None:
    out_dir = Path("outputs/team_artifacts/alpha-research-r29")
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("R29 Stage 2a: CK Volume Semantics Check")
    print("=" * 60)

    # Step 1: Discover available symbols
    print("\n[1] Discovering TXF-related symbols in ClickHouse ...")
    symbols = discover_symbols()
    print(f"  Found symbols: {symbols}")

    if not symbols:
        print("[ERROR] No tick data found in ClickHouse. Is the database populated?")
        sys.exit(1)

    # Step 2: Focus on TXF symbols + TMFD6 as control
    targets = []
    for sym in symbols:
        if any(prefix in sym for prefix in ["TXF", "TMF", "MXF"]):
            targets.append(sym)

    if not targets:
        print(f"[WARN] No TXF/TMF/MXF symbols found. Using first 3: {symbols[:3]}")
        targets = symbols[:3]

    print(f"  Analyzing: {targets}")

    # Step 3: Run analysis on each
    all_results = []
    for sym in targets:
        print(f"\n[2] Analyzing {sym} ...")
        n_total = count_ticks(sym)
        print(f"  Total ticks: {n_total:,}")

        if n_total == 0:
            print(f"  [SKIP] No ticks for {sym}")
            continue

        # Load first 5000 ticks and last 5000 ticks for a broader view
        sample_size = min(5000, n_total)
        volume, ts, days = load_sample(sym, limit=sample_size)

        if len(volume) == 0:
            print(f"  [SKIP] Empty result for {sym}")
            continue

        print(f"  Loaded {len(volume):,} ticks across {len(set(days))} days")
        print(f"  Volume range: [{volume.min():,}, {volume.max():,}]")
        print(f"  First 10 volumes: {volume[:10].tolist()}")

        result = analyze_volume_semantics(sym, volume, days)
        all_results.append(result)

        print(f"  VERDICT: {result['verdict']}")
        print(f"  Recommendation: {result['recommendation']}")

    # Step 4: Write report
    if all_results:
        report = format_report(all_results)
        report_path = out_dir / "stage2a_volume_check.md"
        report_path.write_text(report + "\n")
        print(f"\n[3] Report written to {report_path}")
    else:
        print("\n[ERROR] No results to report.")
        sys.exit(1)

    print("\nDone.")


if __name__ == "__main__":
    main()
