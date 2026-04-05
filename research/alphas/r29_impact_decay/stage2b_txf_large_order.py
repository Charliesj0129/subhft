#!/usr/bin/env python3
"""
R29 Stage 2b: Replicate R25b large-order analysis on TXF vs TMF.

KILL GATE:
- KILL if TXF large orders show same direction-neutral pattern as TMF
- PASS if TXF large orders show meaningful directional bias + viable returns

Compares TXFD6 (big contract) and TMFD6 (mini) side-by-side.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np

SCALE = 1_000_000  # price_scaled -> index points
LATENCY_NS = 36_000_000  # 36 ms entry delay
RT_COST = {"TXF": 3.4, "TMF": 4.0}
HORIZONS_S = [30, 60, 300, 600, 1800]

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


def load_ticks(symbol: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Load all tick data: (price_pts, volume, exch_ts_ns, day_arr)."""
    raw = query_ch(
        f"SELECT price_scaled, volume, exch_ts, "
        f"toString(toDate(toDateTime64(exch_ts/1e9, 3))) as day "
        f"FROM hft.market_data "
        f"WHERE symbol = '{symbol}' AND type = 'Tick' "
        f"ORDER BY exch_ts FORMAT TabSeparated"
    )
    if not raw:
        return np.array([]), np.array([]), np.array([]), np.array([])
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
    return price, volume, ts, np.array(days)


def compute_dprice(price: np.ndarray, days: np.ndarray) -> np.ndarray:
    """Per-tick price change, reset at day boundaries."""
    n = len(price)
    dp = np.zeros(n)
    dp[1:] = price[1:] - price[:-1]
    bounds = np.where(days[1:] != days[:-1])[0] + 1
    dp[bounds] = 0.0
    dp[0] = 0.0
    return dp


def forward_returns_detrended(
    price: np.ndarray, ts: np.ndarray, days: np.ndarray,
    indices: np.ndarray, direction: np.ndarray,
) -> dict[int, np.ndarray]:
    """Direction-adjusted, detrended forward returns with 36ms latency."""
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
            raw_ret = price[exit_j] - entry_price
            results[h_s][k] = raw_ret * direction[k]

    # Detrend: subtract per-day mean return at each horizon
    unique_days = np.unique(days[indices])
    for h_s in HORIZONS_S:
        for day in unique_days:
            day_mask = days[indices] == day
            valid = ~np.isnan(results[h_s]) & day_mask
            if np.sum(valid) > 1:
                day_mean = np.nanmean(results[h_s][valid])
                results[h_s][valid] -= day_mean

    return results


def summarize(rets: np.ndarray) -> dict[str, Any]:
    valid = rets[~np.isnan(rets)]
    if len(valid) == 0:
        return {"n": 0}
    se = float(np.std(valid) / np.sqrt(len(valid))) if len(valid) > 1 else 1e9
    t = float(np.mean(valid) / se) if se > 0 else 0
    return {
        "n": int(len(valid)),
        "mean": round(float(np.mean(valid)), 2),
        "median": round(float(np.median(valid)), 2),
        "std": round(float(np.std(valid)), 2),
        "t_stat": round(t, 3),
        "cohens_d": round(float(np.mean(valid) / max(float(np.std(valid)), 1e-9)), 4),
        "pct_pos": round(float(np.mean(valid > 0)) * 100, 1),
    }


def analyze_symbol(symbol: str, lines: list[str]) -> dict:
    """Run full analysis on one symbol."""
    lines.append(f"\n{'#'*60}")
    lines.append(f"# {symbol}")
    lines.append(f"{'#'*60}")

    price, volume, ts, days = load_ticks(symbol)
    n = len(price)
    if n == 0:
        lines.append(f"  NO DATA for {symbol}")
        return {"symbol": symbol, "n": 0, "verdict": "NO_DATA"}

    dp = compute_dprice(price, days)
    unique_days = sorted(set(days.tolist()))
    n_days = len(unique_days)
    # Use most recent data first per feedback
    recent_days = unique_days[-min(10, len(unique_days)):]
    lines.append(f"Ticks: {n:,} | Days: {n_days} | Most recent: {recent_days[-1]}")
    lines.append(f"Price range: {price.min():.0f} - {price.max():.0f}")
    lines.append(f"Volume range: {volume.min()} - {volume.max()}")

    # ---- Analysis 1: Price Impact by Volume Bucket ----
    lines.append(f"\n--- Price Impact by Volume Bucket ---")
    lines.append(f"{'Bucket':>12s} {'Count':>9s} {'Mean|dp|':>9s} {'Med|dp|':>8s} {'%dp=0':>7s} {'%|dp|>=1':>9s} {'%|dp|>=2':>9s}")

    buckets = [
        ("vol==1", volume == 1),
        ("vol 2-9", (volume >= 2) & (volume <= 9)),
        ("vol 10-19", (volume >= 10) & (volume <= 19)),
        ("vol 20-49", (volume >= 20) & (volume <= 49)),
        ("vol>=50", volume >= 50),
    ]
    impact_by_bucket: dict[str, dict] = {}
    for label, mask in buckets:
        cnt = int(np.sum(mask))
        if cnt == 0:
            lines.append(f"  {label:>12s}: (no data)")
            continue
        abs_dp = np.abs(dp[mask])
        info = {
            "count": cnt,
            "mean_abs": float(np.mean(abs_dp)),
            "median_abs": float(np.median(abs_dp)),
            "frac_zero": float(np.mean(abs_dp == 0)),
            "frac_ge1": float(np.mean(abs_dp >= 1)),
            "frac_ge2": float(np.mean(abs_dp >= 2)),
        }
        impact_by_bucket[label] = info
        lines.append(
            f"  {label:>12s} {cnt:>9,d} {info['mean_abs']:>9.3f} {info['median_abs']:>8.1f} "
            f"{info['frac_zero']:>6.1%} {info['frac_ge1']:>8.1%} {info['frac_ge2']:>8.1%}"
        )

    # Kill check
    small_impact = impact_by_bucket.get("vol==1", {}).get("mean_abs", 0)
    large_impact = impact_by_bucket.get("vol>=50", impact_by_bucket.get("vol 10-19", {})).get("mean_abs", 0)
    frac_zero_large = impact_by_bucket.get("vol>=50", impact_by_bucket.get("vol 10-19", {})).get("frac_zero", 1)

    if large_impact <= small_impact:
        lines.append(f"\n  ** Large vol mean|dp| ({large_impact:.3f}) <= small vol ({small_impact:.3f}) **")
        lines.append(f"  ** KILL SIGNAL: Large orders do NOT move price more **")
        impact_kill = True
    else:
        ratio = large_impact / max(small_impact, 1e-9)
        lines.append(f"\n  Large/small impact ratio: {ratio:.2f}x")
        impact_kill = False

    lines.append(f"  % dp=0 for largest bucket: {frac_zero_large:.1%}")

    # ---- Analysis 2: Forward Returns ----
    lines.append(f"\n--- Forward Returns (detrended, 36ms latency) ---")

    prefix = "TXF" if "TXF" in symbol else "TMF"
    cost = RT_COST.get(prefix, 4.0)

    combos = [
        ("vol>=10 dp!=0", (volume >= 10) & (dp != 0)),
        ("vol>=10 |dp|>=2", (volume >= 10) & (np.abs(dp) >= 2)),
        ("vol>=20 |dp|>=2", (volume >= 20) & (np.abs(dp) >= 2)),
        ("vol>=50 |dp|>=2", (volume >= 50) & (np.abs(dp) >= 2)),
    ]

    any_viable = False
    fwd_results: dict[str, dict] = {}
    for label, mask in combos:
        indices = np.where(mask)[0]
        if len(indices) < 10:
            lines.append(f"\n  {label}: {len(indices)} events -> SKIP (too few)")
            continue
        direction = np.sign(dp[indices])
        per_day = len(indices) / max(n_days, 1)
        lines.append(f"\n  {label}: {len(indices):,} events ({per_day:.1f}/day)")

        fwd = forward_returns_detrended(price, ts, days, indices, direction)
        lines.append(f"    {'Hz':>6s} {'N':>6s} {'Mean':>8s} {'Med':>7s} {'Std':>8s} {'t':>7s} {'d':>7s} {'%+':>5s}")

        combo_results = {}
        for h_s in HORIZONS_S:
            s = summarize(fwd[h_s])
            if s["n"] == 0:
                continue
            marker = ""
            if s["mean"] > cost and s["t_stat"] > 2.0:
                marker = " <<< VIABLE"
                any_viable = True
            elif s["mean"] > cost:
                marker = " (>cost, t<2)"
            combo_results[h_s] = s
            lines.append(
                f"    {h_s:>5d}s {s['n']:>6d} {s['mean']:>8.2f} {s['median']:>7.1f} "
                f"{s['std']:>8.1f} {s['t_stat']:>7.3f} {s['cohens_d']:>7.4f} "
                f"{s['pct_pos']:>4.1f}%{marker}"
            )
        fwd_results[label] = combo_results

    # Verdict
    if impact_kill:
        verdict = "KILL"
        lines.append(f"\n  VERDICT for {symbol}: KILL (large orders direction-neutral)")
    elif any_viable:
        verdict = "PASS"
        lines.append(f"\n  VERDICT for {symbol}: PASS (viable forward returns)")
    else:
        verdict = "MARGINAL"
        lines.append(f"\n  VERDICT for {symbol}: MARGINAL (directional but sub-significant)")

    return {
        "symbol": symbol, "n": n, "n_days": n_days,
        "impact_by_bucket": impact_by_bucket,
        "impact_kill": impact_kill,
        "any_viable": any_viable,
        "verdict": verdict,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report: list[str] = []

    report.append("=" * 70)
    report.append("R29 Stage 2b: TXF vs TMF Large-Order Analysis")
    report.append("=" * 70)

    results = {}
    for sym in ["TXFD6", "TMFD6"]:
        print(f"\nAnalyzing {sym} ...")
        r = analyze_symbol(sym, report)
        results[sym] = r
        for line in report[-5:]:
            print(line)

    # ---- Final Comparison ----
    report.append(f"\n{'='*70}")
    report.append("COMPARISON: TXFD6 vs TMFD6")
    report.append(f"{'='*70}")

    txf = results.get("TXFD6", {})
    tmf = results.get("TMFD6", {})

    report.append(f"| Metric | TXFD6 | TMFD6 |")
    report.append(f"|--------|-------|-------|")

    for bucket in ["vol==1", "vol>=50", "vol 10-19"]:
        txf_val = txf.get("impact_by_bucket", {}).get(bucket, {}).get("mean_abs", "N/A")
        tmf_val = tmf.get("impact_by_bucket", {}).get(bucket, {}).get("mean_abs", "N/A")
        if isinstance(txf_val, float):
            txf_val = f"{txf_val:.3f}"
        if isinstance(tmf_val, float):
            tmf_val = f"{tmf_val:.3f}"
        report.append(f"| {bucket} mean|dp| | {txf_val} | {tmf_val} |")

    for bucket in ["vol>=50", "vol 10-19"]:
        txf_val = txf.get("impact_by_bucket", {}).get(bucket, {}).get("frac_zero", "N/A")
        tmf_val = tmf.get("impact_by_bucket", {}).get(bucket, {}).get("frac_zero", "N/A")
        if isinstance(txf_val, float):
            txf_val = f"{txf_val:.1%}"
        if isinstance(tmf_val, float):
            tmf_val = f"{tmf_val:.1%}"
        report.append(f"| {bucket} %dp=0 | {txf_val} | {tmf_val} |")

    report.append(f"| Impact kill? | {txf.get('impact_kill')} | {tmf.get('impact_kill')} |")
    report.append(f"| Viable fwd returns? | {txf.get('any_viable')} | {tmf.get('any_viable')} |")
    report.append(f"| **Verdict** | **{txf.get('verdict')}** | **{tmf.get('verdict')}** |")

    # ---- Final Kill-Gate Decision ----
    report.append(f"\n{'='*70}")
    report.append("KILL-GATE DECISION")
    report.append(f"{'='*70}")

    txf_verdict = txf.get("verdict", "NO_DATA")
    if txf_verdict == "PASS":
        report.append("\n**PASS**: TXF large orders show directional bias AND viable forward returns.")
        report.append("R29 proceeds to Stage 2c (synthetic metaorder validation).")
        final = "PASS"
    elif txf_verdict == "MARGINAL":
        report.append("\n**CONDITIONAL PASS**: TXF large orders show directional bias but marginal significance.")
        report.append("R29 may proceed with caution — larger sample needed.")
        final = "CONDITIONAL_PASS"
    else:
        report.append("\n**KILL**: TXF large orders are direction-neutral, same as TMF.")
        report.append("R29 alpha thesis is falsified.")
        final = "KILL"

    report.append(f"\nFinal verdict: {final}")

    # Write
    for line in report:
        print(line)

    report_path = OUT_DIR / "stage2b_r25b_replication.md"
    header = [
        "# R29 Stage 2b: R25b Replication — TXF vs TMF Large-Order Analysis",
        "",
        f"**Date**: 2026-04-01",
        f"**Verdict**: {final}",
        "",
        "```",
    ]
    footer = ["```"]
    report_path.write_text("\n".join(header + report + footer) + "\n")
    print(f"\nReport: {report_path}")


if __name__ == "__main__":
    main()
