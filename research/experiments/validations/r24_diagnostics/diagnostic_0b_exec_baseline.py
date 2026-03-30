"""Diagnostic 0b: Current ExecutionOptimizer baseline for Direction A.

Replays the current heuristic ExecutionOptimizer logic against L1 research
data to measure:
1. How often the heuristic would choose LIMIT vs MARKET.
2. Simulated fill rate for limit orders (based on price crossing within timeout).
3. Cost savings vs always-market baseline.

Since ClickHouse is offline, simulates against L1 numpy research data.

Usage:
    python -m research.experiments.validations.r24_diagnostics.diagnostic_0b_exec_baseline
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np


# -------------------------------------------------------------------
# Replicate ExecutionOptimizer heuristic
# -------------------------------------------------------------------
SPREAD_THRESHOLD_PTS = 2  # default
FILL_SCORE_THRESHOLD = 1.5  # default
LIMIT_TIMEOUT_NS = 3_000_000_000  # 3 seconds


def simulate_execution_optimizer(
    data: np.ndarray,
    tick_size: float = 1.0,
) -> dict:
    """Simulate ExecutionOptimizer decisions on L1 data.

    Simulates a trade every N ticks (configurable) and measures:
    - heuristic decision (LIMIT vs MARKET)
    - limit order fill outcome (does price cross our level within timeout?)
    - cost: market = full spread, limit = 0 if filled, full spread if timeout
    """
    n = len(data)
    bid = data["bid_px"]
    ask = data["ask_px"]
    bid_qty = data["bid_qty"]
    ask_qty = data["ask_qty"]
    ts = data["local_ts"]
    mid = data["mid_price"]
    spread = ask - bid

    # Simulate trades every ~500 ticks (~60s) for statistical significance
    trade_interval = 500
    results = {
        "n_decisions": 0,
        "n_limit": 0,
        "n_market": 0,
        "n_limit_filled": 0,
        "n_limit_timeout": 0,
        "cost_market_total": 0.0,  # always-market baseline cost
        "cost_heuristic_total": 0.0,  # heuristic-decided cost
        "cost_optimal_total": 0.0,  # retrospective optimal cost
        "spreads_at_limit": [],
        "spreads_at_market": [],
        "fill_times_ns": [],
    }

    for i in range(0, n - trade_interval, trade_interval):
        cur_spread_pts = spread[i] / tick_size
        near_depth = int(bid_qty[i])  # assume BUY side
        opp_depth = int(ask_qty[i])
        imbalance_ppm = 0
        if near_depth + opp_depth > 0:
            imbalance_ppm = int(1_000_000 * (bid_qty[i] - ask_qty[i]) / (bid_qty[i] + ask_qty[i]))

        results["n_decisions"] += 1
        # Always-market cost = half spread (one side)
        half_spread = spread[i] / 2.0
        results["cost_market_total"] += half_spread

        # Heuristic decision
        use_limit = False
        if cur_spread_pts >= SPREAD_THRESHOLD_PTS and opp_depth > 0:
            near_clamped = max(near_depth, 1)
            fill_score = opp_depth / near_clamped
            favorable_imb = imbalance_ppm > 200_000  # buy side favorable
            if favorable_imb:
                fill_score += 0.5
            if fill_score >= FILL_SCORE_THRESHOLD:
                use_limit = True

        if use_limit:
            results["n_limit"] += 1
            results["spreads_at_limit"].append(float(spread[i]))

            # Simulate limit order: place at best bid, wait for ask to come down
            limit_price = bid[i]
            filled = False
            fill_time_ns = 0

            # Look forward up to timeout
            timeout_ts = ts[i] + LIMIT_TIMEOUT_NS
            for j in range(i + 1, min(i + 5000, n)):
                if ts[j] > timeout_ts:
                    break
                # Limit BUY at bid[i] fills if ask crosses down to our level
                if ask[j] <= limit_price:
                    filled = True
                    fill_time_ns = ts[j] - ts[i]
                    break

            if filled:
                results["n_limit_filled"] += 1
                results["cost_heuristic_total"] += 0.0  # filled at bid = zero crossing cost
                results["fill_times_ns"].append(int(fill_time_ns))
            else:
                results["n_limit_timeout"] += 1
                # Timeout: cancel and market order at worse price
                # Cost = half spread at timeout point (likely similar or worse)
                j_timeout = min(i + 5000, n - 1)
                for j in range(i + 1, min(i + 5000, n)):
                    if ts[j] > timeout_ts:
                        j_timeout = j
                        break
                results["cost_heuristic_total"] += spread[j_timeout] / 2.0

            # Retrospective optimal: could we have filled at bid?
            # (best possible = zero cost if price ever crossed)
            found_cross = False
            for j in range(i + 1, min(i + 5000, n)):
                if ts[j] > timeout_ts:
                    break
                if ask[j] <= limit_price:
                    found_cross = True
                    break
            if found_cross:
                results["cost_optimal_total"] += 0.0
            else:
                results["cost_optimal_total"] += half_spread
        else:
            results["n_market"] += 1
            results["spreads_at_market"].append(float(spread[i]))
            results["cost_heuristic_total"] += half_spread
            results["cost_optimal_total"] += half_spread  # market is optimal when limit won't fill

    return results


def run_diagnostic() -> dict:
    """Run across all available data files."""
    data_dir = Path("research/data/raw")
    all_results: dict[str, list] = {}

    for symbol_dir in ["txfd6", "tmfd6"]:
        sym_path = data_dir / symbol_dir
        if not sym_path.exists():
            continue

        # Determine tick size from symbol
        tick_size = 1.0  # TXFD6 and TMFD6 tick = 1 index point

        npy_files = sorted(sym_path.glob(f"{symbol_dir.upper()}_*_l1.npy"))
        npy_files = [f for f in npy_files if "_all_" not in f.name and "_march_" not in f.name]

        for fpath in npy_files:
            date_str = fpath.stem.split("_")[1]
            print(f"Processing {symbol_dir.upper()} {date_str}...")

            data = np.load(str(fpath), allow_pickle=True)
            if len(data) < 3000:
                print(f"  Skipping (only {len(data)} rows)")
                continue

            result = simulate_execution_optimizer(data, tick_size=tick_size)
            result["symbol"] = symbol_dir.upper()
            result["date"] = date_str
            result["n_ticks"] = len(data)

            if symbol_dir not in all_results:
                all_results[symbol_dir] = []
            all_results[symbol_dir].append(result)

    return all_results


def format_results(all_results: dict) -> str:
    """Format as markdown report."""
    lines = [
        "# Diagnostic 0b: ExecutionOptimizer Baseline Analysis",
        "",
        f"**Date**: 2026-03-29",
        f"**Heuristic params**: spread_threshold_pts={SPREAD_THRESHOLD_PTS}, "
        f"fill_score_threshold={FILL_SCORE_THRESHOLD}, timeout=3s",
        "",
        "## Methodology",
        "",
        "Simulates the current ExecutionOptimizer heuristic against L1 research data.",
        "A synthetic BUY trade is generated every ~500 ticks (~60s). For each trade:",
        "- Heuristic decides LIMIT or MARKET based on spread, depth, imbalance.",
        "- Limit orders are placed at best bid; fill simulated if ask crosses within 3s timeout.",
        "- Cost: MARKET = half spread. LIMIT filled = 0. LIMIT timeout = half spread at timeout.",
        "",
        "**Limitation**: No actual order flow or queue position modeling. This is a",
        "best-case fill simulation (assumes front-of-queue). Real fills would be worse.",
        "",
    ]

    for sym_key, day_results in sorted(all_results.items()):
        sym = sym_key.upper()
        lines.append(f"## {sym}")
        lines.append("")
        lines.append("| Date | Ticks | Decisions | LIMIT% | Fill Rate | "
                      "Mkt Cost | Heur Cost | Savings | Optimal |")
        lines.append("|------|-------|-----------|--------|-----------|"
                      "---------|-----------|---------|---------|")

        total_decisions = 0
        total_limit = 0
        total_filled = 0
        total_mkt_cost = 0.0
        total_heur_cost = 0.0
        total_opt_cost = 0.0

        for r in day_results:
            nd = r["n_decisions"]
            if nd == 0:
                continue
            limit_pct = 100.0 * r["n_limit"] / nd
            fill_rate = (100.0 * r["n_limit_filled"] / r["n_limit"]) if r["n_limit"] > 0 else 0.0
            avg_mkt = r["cost_market_total"] / nd
            avg_heur = r["cost_heuristic_total"] / nd
            savings_pct = 100.0 * (1.0 - avg_heur / avg_mkt) if avg_mkt > 0 else 0.0
            avg_opt = r["cost_optimal_total"] / nd

            lines.append(
                f"| {r['date']} | {r['n_ticks']:,} | {nd} | "
                f"{limit_pct:.1f}% | {fill_rate:.1f}% | "
                f"{avg_mkt:.2f} | {avg_heur:.2f} | {savings_pct:+.1f}% | {avg_opt:.2f} |"
            )

            total_decisions += nd
            total_limit += r["n_limit"]
            total_filled += r["n_limit_filled"]
            total_mkt_cost += r["cost_market_total"]
            total_heur_cost += r["cost_heuristic_total"]
            total_opt_cost += r["cost_optimal_total"]

        # Summary
        if total_decisions > 0:
            lines.append("")
            lines.append(f"**{sym} Summary** ({len(day_results)} days, {total_decisions} decisions):")
            lines.append(f"- Limit order rate: {100.0 * total_limit / total_decisions:.1f}%")
            if total_limit > 0:
                lines.append(f"- Limit fill rate: {100.0 * total_filled / total_limit:.1f}%")
            avg_savings = 100.0 * (1.0 - total_heur_cost / total_mkt_cost) if total_mkt_cost > 0 else 0.0
            lines.append(f"- Avg cost (always-market): {total_mkt_cost / total_decisions:.2f} pts/trade")
            lines.append(f"- Avg cost (heuristic): {total_heur_cost / total_decisions:.2f} pts/trade")
            lines.append(f"- **Cost savings**: {avg_savings:+.1f}%")
            lines.append(f"- Avg cost (retrospective optimal): {total_opt_cost / total_decisions:.2f} pts/trade")
            gap = total_heur_cost - total_opt_cost
            gap_per_trade = gap / total_decisions if total_decisions > 0 else 0
            lines.append(f"- **Improvement ceiling** (heuristic vs optimal): {gap_per_trade:.2f} pts/trade")
            lines.append("")

            # Fill time distribution
            all_fill_times = []
            for r in day_results:
                all_fill_times.extend(r.get("fill_times_ns", []))
            if all_fill_times:
                ft_ms = np.array(all_fill_times) / 1e6
                lines.append(f"- Fill time (when filled): P50={np.percentile(ft_ms, 50):.0f}ms, "
                             f"P95={np.percentile(ft_ms, 95):.0f}ms, "
                             f"mean={np.mean(ft_ms):.0f}ms")

            # Spread distribution
            all_limit_spreads = []
            all_market_spreads = []
            for r in day_results:
                all_limit_spreads.extend(r.get("spreads_at_limit", []))
                all_market_spreads.extend(r.get("spreads_at_market", []))
            if all_limit_spreads:
                lines.append(f"- Spread when LIMIT chosen: mean={np.mean(all_limit_spreads):.2f}, "
                             f"median={np.median(all_limit_spreads):.2f}")
            if all_market_spreads:
                lines.append(f"- Spread when MARKET chosen: mean={np.mean(all_market_spreads):.2f}, "
                             f"median={np.median(all_market_spreads):.2f}")

        lines.append("")

    # Direction A feasibility assessment
    lines.append("## Direction A Feasibility Assessment")
    lines.append("")
    lines.append("The improvement ceiling (heuristic cost - retrospective optimal cost) represents")
    lines.append("the maximum possible gain from a perfect fill probability model. If the ceiling")
    lines.append("is < 0.3 pts/trade, Direction A is not worth the complexity.")
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parents[4])  # project root
    all_results = run_diagnostic()
    report = format_results(all_results)
    print(report)

    out_path = Path("docs/alpha-research/r24/diagnostic_0b_exec_baseline.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"\nReport saved to {out_path}")
