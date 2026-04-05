#!/usr/bin/env python3
"""
R29 Stage 2b: Replicate R25b large-order analysis on TXF.

KILL GATE: If TXF large-volume ticks are direction-neutral (like TMFD6),
the entire R29 alpha is dead. If TXF large orders DO show directional
bias and meaningful forward returns, R29 can proceed.

Tests:
1. Event frequency by volume threshold
2. Single-tick price impact: do large orders move price more?
3. Fraction dp=0 for large orders (passive vs aggressive)
4. Forward returns after large-volume + price-jump events
5. Statistical significance (t-stat, effect size)

Uses most-liquid TXF contract available.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy import stats

SCALE = 1_000_000  # price_scaled -> points
LATENCY_NS = 36_000_000  # 36 ms
RT_COST_PTS_TXF = 3.4  # TXF round-trip cost in points
HORIZONS_S = [5, 10, 30, 60, 120, 300, 600, 1800]
VOL_THRESHOLDS = [1, 5, 10, 20, 50, 100]

OUT_DIR = Path("outputs/team_artifacts/alpha-research-r29")


def query_ch(sql: str) -> str:
    r = subprocess.run(
        ["docker", "exec", "clickhouse", "clickhouse-client", "--query", sql],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        print(f"[ERROR] CK: {r.stderr}", file=sys.stderr)
        sys.exit(1)
    return r.stdout.strip()


def pick_symbol() -> str:
    """Pick the most liquid TXF symbol."""
    raw = query_ch(
        "SELECT symbol, count() as cnt FROM hft.market_data "
        "WHERE type = 'Tick' AND symbol LIKE 'TXF%' "
        "GROUP BY symbol ORDER BY cnt DESC LIMIT 1 FORMAT TabSeparated"
    )
    return raw.split("\t")[0]


def load_all_ticks(symbol: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Load all tick data: (price_pts, volume, exch_ts_ns, day_str_indices)."""
    print(f"  Loading all ticks for {symbol} ...")
    raw = query_ch(
        f"SELECT price_scaled, volume, exch_ts, "
        f"toString(toDate(toDateTime64(exch_ts/1e9, 3))) as day "
        f"FROM hft.market_data "
        f"WHERE symbol = '{symbol}' AND type = 'Tick' "
        f"ORDER BY exch_ts FORMAT TabSeparated"
    )
    lines = raw.split("\n")
    n = len(lines)
    price = np.empty(n, dtype=np.float64)
    volume = np.empty(n, dtype=np.int64)
    ts = np.empty(n, dtype=np.int64)
    days: list[str] = []
    for i, line in enumerate(lines):
        parts = line.split("\t")
        price[i] = int(parts[0]) / SCALE
        volume[i] = int(parts[1])
        ts[i] = int(parts[2])
        days.append(parts[3])
    days_arr = np.array(days)
    print(f"  Loaded {n:,} ticks across {len(set(days))} days")
    return price, volume, ts, days_arr


def compute_forward_returns(
    price: np.ndarray, ts: np.ndarray,
    indices: np.ndarray, direction: np.ndarray,
) -> dict[int, np.ndarray]:
    """Direction-adjusted forward returns at each horizon with latency."""
    n_total = len(price)
    results = {h: np.full(len(indices), np.nan) for h in HORIZONS_S}
    for k, idx in enumerate(indices):
        entry_ts = ts[idx] + LATENCY_NS
        entry_j = np.searchsorted(ts, entry_ts, side="left")
        if entry_j >= n_total:
            continue
        entry_price = price[entry_j]
        for h_s in HORIZONS_S:
            exit_ts = entry_ts + h_s * 1_000_000_000
            exit_j = np.searchsorted(ts, exit_ts, side="left")
            if exit_j >= n_total:
                continue
            if ts[exit_j] > exit_ts and exit_j > 0:
                exit_j -= 1
            results[h_s][k] = (price[exit_j] - entry_price) * direction[k]
    return results


def summarize(rets: np.ndarray) -> dict:
    valid = rets[~np.isnan(rets)]
    if len(valid) == 0:
        return {"n": 0}
    t_stat = float(np.mean(valid) / (np.std(valid) / np.sqrt(len(valid)))) if np.std(valid) > 0 else 0
    return {
        "n": int(len(valid)),
        "mean": round(float(np.mean(valid)), 4),
        "median": round(float(np.median(valid)), 4),
        "std": round(float(np.std(valid)), 4),
        "t_stat": round(t_stat, 3),
        "cohens_d": round(float(np.mean(valid) / max(np.std(valid), 1e-9)), 4),
        "pct_positive": round(float(np.mean(valid > 0)) * 100, 1),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report_lines: list[str] = []

    def p(line: str = "") -> None:
        print(line)
        report_lines.append(line)

    p("=" * 70)
    p("R29 Stage 2b: TXF Large-Order Analysis (R25b Replication)")
    p("=" * 70)

    symbol = pick_symbol()
    p(f"\nSymbol: {symbol}")

    price, volume, ts, days = load_all_ticks(symbol)
    n = len(price)

    # Price change per tick
    dprice = np.zeros(n)
    dprice[1:] = price[1:] - price[:-1]
    day_bounds = np.where(days[1:] != days[:-1])[0] + 1
    dprice[day_bounds] = 0.0
    dprice[0] = 0.0

    unique_days = sorted(set(days.tolist()))
    n_days = len(unique_days)
    p(f"Total ticks: {n:,}, Days: {n_days}")
    p(f"Price range: {price.min():.0f} - {price.max():.0f}")
    p(f"Volume range: {volume.min()} - {volume.max()}")

    # ===================================================================
    # Analysis 1: Volume Distribution
    # ===================================================================
    p(f"\n{'='*70}")
    p("ANALYSIS 1: Event Frequency by Volume Threshold")
    p(f"{'='*70}")
    p(f"{'Threshold':>10s} {'Count':>10s} {'Per Day':>10s} {'% of Total':>10s}")
    for thresh in VOL_THRESHOLDS:
        mask = volume >= thresh
        cnt = int(np.sum(mask))
        per_day = cnt / max(n_days, 1)
        pct = cnt / n * 100
        p(f"  vol>={thresh:>3d}:  {cnt:>10,d}  {per_day:>10.1f}  {pct:>9.2f}%")

    # ===================================================================
    # Analysis 2: Price Impact by Volume Bucket
    # ===================================================================
    p(f"\n{'='*70}")
    p("ANALYSIS 2: Single-Tick Price Impact (|dp| in points)")
    p(f"{'='*70}")
    p(f"{'Bucket':>12s} {'Count':>10s} {'Mean|dp|':>10s} {'%dp=0':>8s} {'%dp>=1':>8s} {'%dp>=2':>8s}")

    kill_impact = False
    impact_data = {}
    for label, mask in [
        ("vol==1", volume == 1),
        ("vol 2-4", (volume >= 2) & (volume <= 4)),
        ("vol 5-9", (volume >= 5) & (volume <= 9)),
        ("vol>=10", volume >= 10),
        ("vol>=20", volume >= 20),
        ("vol>=50", volume >= 50),
        ("vol>=100", volume >= 100),
    ]:
        dp = dprice[mask]
        cnt = int(np.sum(mask))
        if cnt == 0:
            p(f"  {label:>12s}:  {cnt:>10,d}  (no data)")
            continue
        abs_dp = np.abs(dp)
        mean_abs = float(np.mean(abs_dp))
        frac_zero = float(np.mean(abs_dp == 0))
        frac_ge1 = float(np.mean(abs_dp >= 1.0))
        frac_ge2 = float(np.mean(abs_dp >= 2.0))
        impact_data[label] = {"mean_abs": mean_abs, "frac_zero": frac_zero, "count": cnt}
        p(f"  {label:>12s}:  {cnt:>10,d}  {mean_abs:>10.4f}  {frac_zero:>7.1%}  {frac_ge1:>7.1%}  {frac_ge2:>7.1%}")

    # Kill check: do large orders move price more?
    if "vol==1" in impact_data and "vol>=10" in impact_data:
        small = impact_data["vol==1"]["mean_abs"]
        large = impact_data["vol>=10"]["mean_abs"]
        if large <= small:
            p(f"\n  *** KILL SIGNAL: vol>=10 mean|dp|={large:.4f} <= vol==1 mean|dp|={small:.4f} ***")
            p(f"  *** Large orders do NOT move price more (same as TMFD6 R25b finding) ***")
            kill_impact = True
        else:
            ratio = large / max(small, 1e-9)
            p(f"\n  Large/small impact ratio: {ratio:.2f}x (large orders move price MORE)")
            kill_impact = False

    # Kill check: fraction of dp=0
    if "vol>=10" in impact_data:
        fz = impact_data["vol>=10"]["frac_zero"]
        p(f"  Fraction dp=0 for vol>=10: {fz:.1%}")
        if fz > 0.50:
            p(f"  *** WARNING: >50% of large-volume ticks have dp=0 (passive fills) ***")

    # ===================================================================
    # Analysis 3: Forward Returns for Large+Directional Events
    # ===================================================================
    p(f"\n{'='*70}")
    p("ANALYSIS 3: Forward Returns (direction-adjusted, 36ms latency)")
    p(f"{'='*70}")

    # Define combined signal: vol >= threshold AND |dp| >= dp_threshold
    combos = [
        ("vol>=10 & dp!=0", (volume >= 10) & (dprice != 0)),
        ("vol>=10 & |dp|>=2", (volume >= 10) & (np.abs(dprice) >= 2)),
        ("vol>=20 & |dp|>=2", (volume >= 20) & (np.abs(dprice) >= 2)),
        ("vol>=50 & |dp|>=2", (volume >= 50) & (np.abs(dprice) >= 2)),
        ("vol>=10 & |dp|>=3", (volume >= 10) & (np.abs(dprice) >= 3)),
        ("vol>=20 & |dp|>=3", (volume >= 20) & (np.abs(dprice) >= 3)),
    ]

    any_viable = False
    for label, mask in combos:
        indices = np.where(mask)[0]
        if len(indices) == 0:
            p(f"\n  {label}: 0 events -> SKIP")
            continue
        direction = np.sign(dprice[indices])
        events_per_day = len(indices) / max(n_days, 1)
        p(f"\n  {label}: {len(indices):,} events ({events_per_day:.1f}/day)")

        fwd = compute_forward_returns(price, ts, indices, direction)
        p(f"    {'Horizon':>8s} {'N':>6s} {'Mean':>8s} {'Median':>8s} {'Std':>8s} {'t-stat':>8s} {'d':>8s} {'%pos':>6s}")

        for h_s in HORIZONS_S:
            s = summarize(fwd[h_s])
            if s["n"] == 0:
                continue
            marker = ""
            if s["mean"] > RT_COST_PTS_TXF and s["t_stat"] > 2.0:
                marker = " *** VIABLE ***"
                any_viable = True
            elif s["mean"] > RT_COST_PTS_TXF:
                marker = " (above cost but t<2)"
            p(f"    {h_s:>7d}s {s['n']:>6d} {s['mean']:>8.2f} {s['median']:>8.2f} "
              f"{s['std']:>8.2f} {s['t_stat']:>8.3f} {s['cohens_d']:>8.4f} "
              f"{s['pct_positive']:>5.1f}%{marker}")

    # ===================================================================
    # VERDICT
    # ===================================================================
    p(f"\n{'='*70}")
    p("VERDICT")
    p(f"{'='*70}")

    if kill_impact:
        p("\nKILL: Large-volume TXF ticks do NOT move price more than small ticks.")
        p("Same structural finding as TMFD6 R25b. Large volume = passive fills.")
        p("R29 alpha thesis (large orders carry directional information) is FALSIFIED on TXF.")
        verdict = "KILL"
    elif not any_viable:
        p("\nCONDITIONAL KILL: Large orders DO move price more, but forward returns")
        p(f"do not exceed RT cost ({RT_COST_PTS_TXF} pts) at any horizon with t>2.")
        p("Signal exists but is not economically significant.")
        verdict = "CONDITIONAL_KILL"
    else:
        p("\nAPPROVE: At least one signal combination shows viable forward returns")
        p(f"exceeding RT cost ({RT_COST_PTS_TXF} pts) with statistical significance (t>2).")
        p("R29 can proceed to Stage 2c.")
        verdict = "APPROVE"

    p(f"\nFinal verdict: {verdict}")

    # Write report
    report_path = OUT_DIR / "stage2b_r25b_replication.md"
    header = [
        "# R29 Stage 2b: TXF Large-Order Analysis (R25b Replication)",
        "",
        f"**Date**: 2026-04-01",
        f"**Symbol**: {symbol}",
        f"**Verdict**: {verdict}",
        "",
        "## Raw Output",
        "",
        "```",
    ]
    footer = ["```"]
    report_path.write_text("\n".join(header + report_lines + footer) + "\n")
    print(f"\nReport: {report_path}")


if __name__ == "__main__":
    main()
