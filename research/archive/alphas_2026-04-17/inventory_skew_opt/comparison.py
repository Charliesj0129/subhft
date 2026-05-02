"""Compare linear vs Riccati-optimal inventory skew at various inventory levels.

Quantifies the improvement (if any) from replacing SimpleMarketMaker's linear
skew with the AS/Barzykin Riccati-optimal skew on TXFD6 parameters.

Usage:
    python -m research.alphas.inventory_skew_opt.comparison

Output:
    Table comparing skew values at inventory 0-10 with calibrated TXFD6 params.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from research.alphas.inventory_skew_opt.impl import (
    MarketParams,
    RiccatiSkewCalculator,
    calibrate_from_txfd6,
    compare_skews,
    solve_riccati,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


DATA_DIR = _PROJECT_ROOT / "research" / "data" / "raw" / "txfd6"


def run_comparison() -> dict:
    """Run the full linear vs Riccati comparison."""

    # --- 1. Load data and calibrate parameters ---
    l1_files = sorted(DATA_DIR.glob("TXFD6_*_l1.npy"))
    l1_files = [f for f in l1_files if "all" not in f.stem]

    if not l1_files:
        logger.error("No L1 data files found")
        return {}

    # Use first few days for calibration
    cal_data = np.load(l1_files[0])
    logger.info("Calibrating from %s (%d ticks)", l1_files[0].stem, len(cal_data))

    params = calibrate_from_txfd6(cal_data)
    logger.info("Calibrated parameters:")
    logger.info("  sigma = %.4f pts/sqrt(s)", params.sigma)
    logger.info("  gamma = %.6f", params.gamma)
    logger.info("  kappa = %.4f /pt", params.kappa)
    logger.info("  lambda_0 = %.2f ticks/s", params.lambda_0)
    logger.info("  T = %.0f s (%.1f hours)", params.T, params.T / 3600)

    # --- 2. Solve Riccati ODE ---
    solution = solve_riccati(params)
    logger.info("\nRiccati solution:")
    logger.info("  A_stationary = %.6f", solution.A_stationary)
    logger.info("  skew_per_unit = %.4f pts/contract", solution.skew_per_unit)
    logger.info("  half_spread_base = %.4f pts", solution.half_spread_base)

    # --- 3. Compare with linear skew ---
    avg_mid = float(np.mean(cal_data["mid_price"]))
    comparisons = compare_skews(
        params,
        solution,
        mid_price=avg_mid,
        inventories=list(range(0, 11)),
    )

    logger.info("\n" + "=" * 80)
    logger.info("INVENTORY SKEW COMPARISON: Linear vs Riccati")
    logger.info("=" * 80)
    logger.info(
        "%-6s %-12s %-12s %-12s %-12s",
        "Inv", "Linear(pts)", "Riccati(pts)", "Diff(pts)", "Diff(bps)",
    )
    logger.info("-" * 54)

    for c in comparisons:
        logger.info(
            "%-6d %-12.4f %-12.4f %-12.4f %-12.4f",
            c.inventory,
            c.linear_skew_pts,
            c.riccati_skew_pts,
            c.difference_pts,
            c.difference_bps,
        )

    # --- 4. Key question: at inventory=3, does Riccati differ from linear? ---
    q3 = next((c for c in comparisons if c.inventory == 3), None)
    q5 = next((c for c in comparisons if c.inventory == 5), None)
    q10 = next((c for c in comparisons if c.inventory == 10), None)

    logger.info("\n" + "=" * 80)
    logger.info("KEY FINDINGS")
    logger.info("=" * 80)

    if q3:
        logger.info(
            "At inventory=3: diff = %.4f pts = %.4f bps (%s threshold of 0.1 bps)",
            q3.difference_pts,
            q3.difference_bps,
            "ABOVE" if abs(q3.difference_bps) > 0.1 else "BELOW",
        )

    if q5:
        logger.info(
            "At inventory=5: diff = %.4f pts = %.4f bps",
            q5.difference_pts,
            q5.difference_bps,
        )

    if q10:
        logger.info(
            "At inventory=10: diff = %.4f pts = %.4f bps",
            q10.difference_pts,
            q10.difference_bps,
        )

    # --- 5. Time-varying A(t) analysis ---
    # Show how A(t) converges to stationary value
    logger.info("\nA(t) convergence:")
    logger.info("  A(T)     = %.6f (terminal)", solution.A_t[-1])
    logger.info("  A(T/2)   = %.6f", solution.A_t[len(solution.A_t) // 2])
    logger.info("  A(T/10)  = %.6f", solution.A_t[len(solution.A_t) // 10])
    logger.info("  A(0)     = %.6f (start)", solution.A_t[0])
    logger.info("  A_stat   = %.6f (stationary limit)", solution.A_stationary)

    # Time to reach 95% of stationary value
    target = 0.95 * solution.A_stationary
    convergence_idx = np.argmax(solution.A_t >= target)
    if convergence_idx > 0:
        convergence_time_from_end = params.T - solution.t_grid[convergence_idx]
        logger.info(
            "  95%% convergence: %.0f seconds from end of horizon",
            convergence_time_from_end,
        )

    # --- 6. Sensitivity analysis ---
    logger.info("\nSensitivity to gamma (risk aversion):")
    for gamma_mult in [0.1, 0.5, 1.0, 2.0, 5.0, 10.0]:
        alt_params = MarketParams(
            sigma=params.sigma,
            gamma=params.gamma * gamma_mult,
            kappa=params.kappa,
            lambda_0=params.lambda_0,
            T=params.T,
            tick_size=params.tick_size,
        )
        alt_solution = solve_riccati(alt_params)
        alt_skew_3 = alt_solution.skew_per_unit * 3
        logger.info(
            "  gamma*%.1f: A=%.6f, skew@q=3=%.4f pts (%.4f bps)",
            gamma_mult,
            alt_solution.A_stationary,
            alt_skew_3,
            alt_skew_3 / avg_mid * 10000,
        )

    # --- 7. Adiabatic approximation validity ---
    # Check if tau/T_tick << 1 (Barzykin's adiabatic condition)
    latency_ms = 36.0  # broker RTT in ms
    median_inter_tick_ms = 125.0  # TXFD6 median inter-tick
    adiabatic_ratio = latency_ms / median_inter_tick_ms
    logger.info("\nAdiabatic approximation validity:")
    logger.info("  Latency = %.0f ms", latency_ms)
    logger.info("  Median inter-tick = %.0f ms", median_inter_tick_ms)
    logger.info("  Ratio tau/T_tick = %.3f (%s)",
                adiabatic_ratio,
                "VALID (< 1)" if adiabatic_ratio < 1 else "BORDERLINE")

    # --- 8. Production integration assessment ---
    logger.info("\n" + "=" * 80)
    logger.info("PRODUCTION INTEGRATION ASSESSMENT")
    logger.info("=" * 80)

    # Demonstrate the RiccatiSkewCalculator
    calc = RiccatiSkewCalculator(solution.A_stationary)

    logger.info("RiccatiSkewCalculator output (scaled int x10000):")
    for q in [0, 1, 3, 5, 10]:
        skew_x2 = calc.compute_skew_x2(q, int(2.5 * 10000))
        # Compare with linear
        linear_skew_x2 = -(q * int(2.5 * 10000) * 2) // 5
        logger.info(
            "  q=%2d: linear_x2=%8d, riccati_x2=%8d, diff_x2=%6d",
            q, linear_skew_x2, skew_x2, skew_x2 - linear_skew_x2,
        )

    return {
        "params": {
            "sigma": params.sigma,
            "gamma": params.gamma,
            "kappa": params.kappa,
            "lambda_0": params.lambda_0,
        },
        "A_stationary": solution.A_stationary,
        "skew_per_unit": solution.skew_per_unit,
        "diff_bps_at_q3": q3.difference_bps if q3 else 0.0,
        "diff_bps_at_q5": q5.difference_bps if q5 else 0.0,
        "diff_bps_at_q10": q10.difference_bps if q10 else 0.0,
        "adiabatic_ratio": adiabatic_ratio,
    }


if __name__ == "__main__":
    run_comparison()
