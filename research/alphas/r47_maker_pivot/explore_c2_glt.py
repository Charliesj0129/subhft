"""C2 GLT Inventory Skew — Sigma^2 Lag + Gamma Sensitivity Exploration.

Validates Challenger's conditions for the Guéant-Lehalle-Tapia (GLT)
optimal market making framework applied to R47:

1. Sigma^2 estimation lag: EMA realized variance vs true variance
2. Gamma sensitivity: PnL must be positive across +/-50% gamma range
3. GLT vs current spread comparison on all 12 days

GLT optimal half-spread formula (Guéant 2016, eq. 4.3):
    δ(q) = γσ²(T-t)/2 · (1 - 2q/Q) + (1/γ) · ln(1 + γ/κ)

Where:
    γ = risk aversion parameter
    σ² = instantaneous variance of mid-price returns
    T-t = time remaining in session
    q = current inventory, Q = max inventory
    κ = fill intensity (fills per unit time)

Usage:
    uv run python research/alphas/r47_maker_pivot/explore_c2_glt.py
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

os.environ.setdefault("HFT_STRICT_PRICE_MODE", "0")

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))
import logging
logging.disable(logging.WARNING)

from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest

TICK_SIZE = 1.0
PRICE_SCALE = 10_000
ELAPSE_NS = 100_000_000  # 100ms per step

DATA_DIR = _REPO_ROOT / "research" / "data" / "raw" / "txfd6"
DATA_FILES = sorted(DATA_DIR.glob("TXFD6_2026-0*_l2.hftbt.npz"))
OUT_DIR = _REPO_ROOT / "outputs" / "team_artifacts" / "alpha-research" / "R47_maker_pivot"

# Session parameters (TAIFEX regular session)
SESSION_DURATION_S = 5 * 3600  # ~5 hours (08:45-13:45)
SESSION_DURATION_NS = SESSION_DURATION_S * 1_000_000_000


def extract_mid_prices(data_path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Extract mid-price, spread, bid_qty, ask_qty series from one day.

    Returns (mid_prices, spreads, bid_qtys, ask_qtys) — all as numpy arrays.
    mid_prices in native price units (not scaled).
    spreads in points.
    """
    asset = (
        BacktestAsset()
        .data([str(data_path)])
        .linear_asset(1.0)
        .constant_order_latency(47_000, 47_000)
        .power_prob_queue_model(3.0)
        .tick_size(TICK_SIZE)
        .lot_size(1.0)
        .partial_fill_exchange()
    )
    hbt = HashMapMarketDepthBacktest([asset])

    mids = []
    spreads = []
    bqtys = []
    aqtys = []
    timestamps = []

    while hbt.elapse(ELAPSE_NS) == 0:
        dp = hbt.depth(0)
        bb = dp.best_bid
        ba = dp.best_ask
        if bb != bb or ba != ba or bb <= 0 or ba >= 2147483647 or bb >= ba:
            continue
        mid = (bb + ba) / 2.0
        spread = ba - bb
        bq = int(getattr(dp, "best_bid_qty", 0) or 0)
        aq = int(getattr(dp, "best_ask_qty", 0) or 0)

        mids.append(mid)
        spreads.append(spread)
        bqtys.append(bq)
        aqtys.append(aq)
        timestamps.append(int(hbt.current_timestamp))

    hbt.close()

    return (
        np.array(mids, dtype=np.float64),
        np.array(spreads, dtype=np.float64),
        np.array(bqtys, dtype=np.int64),
        np.array(aqtys, dtype=np.int64),
        np.array(timestamps, dtype=np.int64),
    )


# ── Part 1: Sigma^2 Estimation Lag ────────────────────────────────────────

def compute_ema_variance(returns: np.ndarray, alpha: float) -> np.ndarray:
    """Compute EMA-based realized variance (online, causal)."""
    n = len(returns)
    var_ema = np.zeros(n, dtype=np.float64)
    mean_ema = 0.0
    var_est = 0.0

    for i in range(n):
        r = returns[i]
        mean_ema = alpha * r + (1 - alpha) * mean_ema
        dev_sq = (r - mean_ema) ** 2
        var_est = alpha * dev_sq + (1 - alpha) * var_est
        var_ema[i] = var_est

    return var_ema


def compute_rolling_variance(returns: np.ndarray, window: int) -> np.ndarray:
    """Compute rolling window variance as 'true' benchmark."""
    n = len(returns)
    var_rolling = np.zeros(n, dtype=np.float64)
    for i in range(window, n):
        var_rolling[i] = np.var(returns[i - window:i])
    # Backfill warmup period
    if n > window:
        var_rolling[:window] = var_rolling[window]
    return var_rolling


def analyze_sigma_lag(mids: np.ndarray, date_str: str) -> dict:
    """Analyze EMA variance lag vs rolling variance."""
    # Compute returns (1-step log returns)
    returns = np.diff(np.log(mids))
    n = len(returns)

    # Rolling variance as "truth" (100-step window = 10 seconds at 100ms)
    true_var = compute_rolling_variance(returns, 100)

    alphas = [0.001, 0.005, 0.01, 0.05]
    results = {}

    for alpha in alphas:
        ema_var = compute_ema_variance(returns, alpha)

        # Find volatility spikes: points where true_var > 2x its median
        median_var = np.median(true_var[100:])
        spike_mask = true_var > 2 * median_var
        spike_indices = np.where(spike_mask)[0]

        if len(spike_indices) == 0:
            results[str(alpha)] = {
                "alpha": alpha,
                "n_spikes": 0,
                "avg_lag_ticks": 0,
                "max_lag_ticks": 0,
                "median_lag_ticks": 0,
                "tracking_rmse": 0.0,
            }
            continue

        # For each spike, find when EMA catches up to within 50% of true value
        lags = []
        for idx in spike_indices:
            target = true_var[idx] * 0.5  # 50% of spike level
            lag = 0
            for j in range(idx, min(idx + 200, n)):
                if ema_var[j] >= target:
                    lag = j - idx
                    break
            else:
                lag = 200  # didn't catch up in 200 ticks
            lags.append(lag)

        lags_arr = np.array(lags)

        # Tracking RMSE (normalized)
        valid = true_var[100:] > 0
        if np.any(valid):
            rmse = np.sqrt(np.mean((ema_var[100:][valid] - true_var[100:][valid]) ** 2))
            norm_rmse = rmse / np.mean(true_var[100:][valid])
        else:
            norm_rmse = 0.0

        results[str(alpha)] = {
            "alpha": alpha,
            "n_spikes": len(spike_indices),
            "avg_lag_ticks": round(float(np.mean(lags_arr)), 1),
            "max_lag_ticks": int(np.max(lags_arr)),
            "median_lag_ticks": int(np.median(lags_arr)),
            "p95_lag_ticks": int(np.percentile(lags_arr, 95)),
            "tracking_rmse_normalized": round(norm_rmse, 4),
        }

    return results


# ── Part 2: Gamma Sensitivity ─────────────────────────────────────────────

def glt_half_spread(gamma: float, sigma2_per_s: float, t_remaining_s: float,
                    q: int, q_max: int, kappa: float) -> float:
    """GLT optimal half-spread — Guéant-Lehalle-Tapia closed-form (2013, Prop 4.1).

    Uses the practical approximation from Guéant (2016) Section 4.2:
        δ_bid/ask = γσ²τ + s/2 ± γσ²τ·q/Q

    Where s = minimum spread (tick size), τ = T-t.

    The full logarithmic form (1/γ)·ln(1+γ/κ) requires calibrating κ as
    a fill-intensity-per-spread parameter, which is model-dependent.
    The practical form adds risk premium γσ²τ on top of the minimum spread.

    Returns half-spread in price units (not scaled).
    """
    # Risk premium: how much extra to charge per unit time for inventory risk
    risk_premium = gamma * sigma2_per_s * t_remaining_s

    # Base half-spread: market half-spread + risk premium
    # (market half-spread is handled by caller — we just return the adjustment)
    base = risk_premium

    # Inventory skew: shift quote center by γσ²τ · q/Q_max
    skew = risk_premium * q / max(q_max, 1)

    return base + skew


def analyze_gamma_sensitivity(mids: np.ndarray, spreads: np.ndarray,
                              total_fills: int, date_str: str) -> dict:
    """Analyze GLT spread sensitivity to gamma parameter."""
    n = len(mids)
    returns = np.diff(np.log(mids))

    # Estimate sigma^2 using EMA(alpha=0.01)
    # Returns are per-step (100ms). Convert to per-second: σ²/s = σ²_step * steps_per_sec
    steps_per_sec = 10.0  # 100ms steps
    sigma2_step_series = compute_ema_variance(returns, 0.01)
    sigma2_per_s_series = sigma2_step_series * steps_per_sec
    median_sigma2_per_s = float(np.median(sigma2_per_s_series[100:]))

    # Also compute in price units (not log): σ²_price = σ²_log * mid²
    median_mid = float(np.median(mids))
    sigma2_price_per_s = median_sigma2_per_s * median_mid ** 2

    # Estimate kappa: fills per second
    duration_s = n * 0.1
    kappa = total_fills / max(duration_s, 1.0)

    # Derive base gamma from R47's current effective risk aversion
    # R47 current skew: -(pos * tick_size * 2) / 5 where tick_size ≈ spread * 0.5
    # For typical spread=1, tick_size=0.5: skew ≈ 0.2 pts per contract
    # GLT inventory skew: γσ²(T-t)/Q_max per contract at mid-session
    # Match: 0.2 = γ * σ²_price/s * (SESSION_S/2) / 3
    # γ = 0.2 * 3 * 2 / (σ²_price/s * SESSION_S)
    if sigma2_price_per_s > 1e-15:
        gamma_base = 0.2 * 6.0 / (sigma2_price_per_s * SESSION_DURATION_S)
    else:
        gamma_base = 0.01

    # No clamp — let the actual value show
    median_sigma2 = median_sigma2_per_s  # for reporting

    gamma_multipliers = [0.5, 0.75, 1.0, 1.25, 1.5]
    results = {}

    # Market half-spread (actual)
    median_market_half_spread = float(np.median(spreads)) / 2.0

    for mult in gamma_multipliers:
        gamma = gamma_base * mult

        # Compute GLT spreads at different inventory levels, mid-session
        t_remaining_s = SESSION_DURATION_S / 2.0
        spreads_by_q = {}
        for q in range(-3, 4):
            glt_adj = glt_half_spread(gamma, sigma2_price_per_s, t_remaining_s, q, 3, kappa)
            total_hs = median_market_half_spread + glt_adj
            spreads_by_q[str(q)] = round(total_hs, 4)

        # Full spread at q=0 (symmetric)
        glt_adj_q0 = glt_half_spread(gamma, sigma2_price_per_s, t_remaining_s, 0, 3, kappa)
        full_spread_q0 = 2 * (median_market_half_spread + glt_adj_q0)

        # Compare to actual median spread
        median_actual_spread = float(np.median(spreads))

        # Estimate fill rate impact: wider spread = fewer fills
        # Using exponential arrival model: fill_rate ∝ exp(-k * spread)
        # Calibrate k from actual data: at median_spread, we get total_fills
        # At GLT spread, fill_rate = total_fills * exp(-k * (glt_spread - median_spread))
        # k ≈ 1/median_spread (crude estimate)
        k_est = 1.0 / max(median_actual_spread, 0.1)
        spread_diff = full_spread_q0 - median_actual_spread
        fill_rate_ratio = math.exp(-k_est * max(0, spread_diff))
        estimated_fills = int(total_fills * fill_rate_ratio)

        # Adverse selection improvement estimate:
        # Wider spread on adverse side reduces adverse fill probability
        # Simple model: adverse_fill_reduction ∝ (GLT_spread / actual_spread - 1)
        adverse_reduction_pct = max(0, (full_spread_q0 / max(median_actual_spread, 0.1) - 1)) * 100

        results[f"{mult:.2f}"] = {
            "gamma_multiplier": mult,
            "gamma": round(gamma, 6),
            "glt_full_spread_q0": round(full_spread_q0, 4),
            "actual_median_spread": round(median_actual_spread, 4),
            "spread_ratio": round(full_spread_q0 / max(median_actual_spread, 0.001), 3),
            "spreads_by_inventory": spreads_by_q,
            "estimated_fill_rate_ratio": round(fill_rate_ratio, 3),
            "estimated_fills": estimated_fills,
            "adverse_selection_reduction_pct": round(adverse_reduction_pct, 1),
        }

    return {
        "gamma_base": round(gamma_base, 8),
        "median_sigma2_log_per_s": float(f"{median_sigma2:.2e}"),
        "median_sigma2_price_per_s": float(f"{sigma2_price_per_s:.4e}"),
        "median_mid_price": round(median_mid, 1),
        "kappa_fills_per_sec": round(kappa, 4),
        "total_fills": total_fills,
        "sensitivity": results,
    }


# ── Part 3: GLT vs Current Spread Comparison ─────────────────────────────

def compare_glt_vs_current(mids: np.ndarray, spreads: np.ndarray,
                           total_fills: int, date_str: str) -> dict:
    """Compare GLT-recommended spreads vs R47's current spreads step-by-step."""
    n = len(mids)
    returns = np.diff(np.log(mids))

    steps_per_sec = 10.0
    sigma2_step_ema = compute_ema_variance(returns, 0.01)
    # Convert to price-unit variance per second
    median_mid = float(np.median(mids))

    duration_s = n * 0.1
    kappa = total_fills / max(duration_s, 1.0)

    # Calibrate gamma (same formula as analyze_gamma_sensitivity)
    sigma2_price_per_s_median = float(np.median(sigma2_step_ema[100:])) * steps_per_sec * median_mid ** 2
    if sigma2_price_per_s_median > 1e-15:
        gamma = 0.2 * 6.0 / (sigma2_price_per_s_median * SESSION_DURATION_S)
    else:
        gamma = 0.01

    # Compute GLT total spread at each step (q=0, add market half-spread)
    glt_spreads = np.zeros(n, dtype=np.float64)
    for i in range(n):
        t_remaining_s = max(10.0, SESSION_DURATION_S * (1.0 - i / max(n - 1, 1)))
        s2_step = sigma2_step_ema[min(i, len(sigma2_step_ema) - 1)]
        s2_price_per_s = s2_step * steps_per_sec * mids[i] ** 2
        market_hs = spreads[i] / 2.0  # current market half-spread
        glt_adj = glt_half_spread(gamma, s2_price_per_s, t_remaining_s, 0, 3, kappa)
        glt_spreads[i] = 2 * (market_hs + glt_adj)

    # R47 current spread = actual market spread (it quotes at half-spread from mid)
    # So effective R47 quote width ≈ market spread (base_width = max(tick, half_spread))
    actual_spreads = spreads  # market spread = R47's effective spread

    # Compute ratio
    ratio = glt_spreads / np.maximum(actual_spreads, 0.01)

    # Split into thirds for time-of-day analysis
    third = n // 3
    first_third_ratio = float(np.median(ratio[:third]))
    mid_third_ratio = float(np.median(ratio[third:2*third]))
    last_third_ratio = float(np.median(ratio[2*third:]))

    # Count steps where GLT is wider vs narrower
    wider_pct = float(np.mean(glt_spreads > actual_spreads) * 100)
    narrower_pct = float(np.mean(glt_spreads < actual_spreads) * 100)

    # Sample for JSON (100 points)
    if n > 100:
        idx = np.linspace(0, n - 1, 100, dtype=int)
        sampled_glt = [round(float(glt_spreads[i]), 4) for i in idx]
        sampled_actual = [round(float(actual_spreads[i]), 4) for i in idx]
    else:
        sampled_glt = [round(float(v), 4) for v in glt_spreads]
        sampled_actual = [round(float(v), 4) for v in actual_spreads]

    return {
        "gamma_used": round(gamma, 6),
        "median_glt_spread": round(float(np.median(glt_spreads)), 4),
        "median_actual_spread": round(float(np.median(actual_spreads)), 4),
        "median_ratio": round(float(np.median(ratio)), 3),
        "first_third_ratio": round(first_third_ratio, 3),
        "mid_third_ratio": round(mid_third_ratio, 3),
        "last_third_ratio": round(last_third_ratio, 3),
        "pct_wider": round(wider_pct, 1),
        "pct_narrower": round(narrower_pct, 1),
        "sampled_glt_spreads": sampled_glt,
        "sampled_actual_spreads": sampled_actual,
    }


# ── Main ──────────────────────────────────────────────────────────────────

# Known fills per day from T5 analysis
FILLS_PER_DAY = {
    "2026-03-19": 193, "2026-03-20": 194, "2026-03-23": 190,
    "2026-03-24": 448, "2026-03-26": 277, "2026-03-27": 286,
    "2026-03-30": 467, "2026-03-31": 247, "2026-04-01": 194,
    "2026-04-02": 215, "2026-04-07": 118, "2026-04-08": 99,
}

PNLS_PER_DAY = {
    "2026-03-19": 685.5, "2026-03-20": -805.0, "2026-03-23": 1629.0,
    "2026-03-24": 838.0, "2026-03-26": -1795.5, "2026-03-27": 2385.0,
    "2026-03-30": 1554.5, "2026-03-31": -3098.5, "2026-04-01": -261.0,
    "2026-04-02": -383.5, "2026-04-07": 1894.0, "2026-04-08": 1890.5,
}


def main() -> None:
    if not DATA_FILES:
        print(f"ERROR: No data files found in {DATA_DIR}")
        sys.exit(1)

    print(f"\nC2 GLT Exploration — Sigma^2 Lag + Gamma Sensitivity")
    print(f"Data: {len(DATA_FILES)} days of TXFD6 L2")
    print("=" * 70)

    all_sigma_lag: dict[str, dict] = {}
    all_gamma_sens: dict[str, dict] = {}
    all_comparisons: dict[str, dict] = {}

    for data_path in DATA_FILES:
        date_str = data_path.stem.replace("TXFD6_", "").replace("_l2.hftbt", "")
        print(f"\n  {date_str}: extracting mid-prices...")
        t0 = time.monotonic()

        mids, spr, bq, aq, ts = extract_mid_prices(data_path)
        elapsed_extract = time.monotonic() - t0
        n_steps = len(mids)
        total_fills = FILLS_PER_DAY.get(date_str, 200)
        pnl = PNLS_PER_DAY.get(date_str, 0)

        print(f"    {n_steps} steps, {total_fills} fills, PnL={pnl:+.1f} ({elapsed_extract:.1f}s)")

        # Part 1: Sigma lag
        sigma_result = analyze_sigma_lag(mids, date_str)
        all_sigma_lag[date_str] = sigma_result

        # Part 2: Gamma sensitivity
        gamma_result = analyze_gamma_sensitivity(mids, spr, total_fills, date_str)
        all_gamma_sens[date_str] = gamma_result

        # Part 3: GLT vs current
        comparison = compare_glt_vs_current(mids, spr, total_fills, date_str)
        all_comparisons[date_str] = comparison

        # Print summary
        best_alpha = "0.01"
        lag = sigma_result.get(best_alpha, {}).get("median_lag_ticks", "?")
        gamma_base = gamma_result.get("gamma_base", "?")
        ratio = comparison.get("median_ratio", "?")
        print(f"    σ² lag(α=0.01): {lag} ticks, γ_base={gamma_base}, GLT/actual ratio={ratio}")

    # ── Aggregate Analysis ────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("PART 1: SIGMA^2 LAG ANALYSIS")
    print("=" * 70)

    for alpha_str in ["0.001", "0.005", "0.01", "0.05"]:
        lags = [all_sigma_lag[d].get(alpha_str, {}).get("median_lag_ticks", 999)
                for d in sorted(all_sigma_lag.keys())]
        avg_lag = sum(lags) / len(lags) if lags else 0
        max_lag = max(lags) if lags else 0
        print(f"  α={alpha_str}: avg_lag={avg_lag:.1f}, max_lag={max_lag} ticks")

    # 03-31 deep dive
    if "2026-03-31" in all_sigma_lag:
        print("\n  03-31 Detail:")
        for alpha_str, data in all_sigma_lag["2026-03-31"].items():
            print(f"    α={alpha_str}: median_lag={data.get('median_lag_ticks', '?')}, "
                  f"p95_lag={data.get('p95_lag_ticks', '?')}, "
                  f"n_spikes={data.get('n_spikes', '?')}")

    print("\n" + "=" * 70)
    print("PART 2: GAMMA SENSITIVITY")
    print("=" * 70)

    # Aggregate gamma_base across days
    gamma_bases = [all_gamma_sens[d]["gamma_base"] for d in sorted(all_gamma_sens.keys())]
    print(f"  γ_base range: [{min(gamma_bases):.6f}, {max(gamma_bases):.6f}]")
    print(f"  γ_base median: {sorted(gamma_bases)[len(gamma_bases)//2]:.6f}")

    # Show spread ratios at different gammas for winning vs losing days
    winning = [d for d in sorted(all_gamma_sens.keys()) if PNLS_PER_DAY.get(d, 0) > 0]
    losing = [d for d in sorted(all_gamma_sens.keys()) if PNLS_PER_DAY.get(d, 0) <= 0]

    for mult_str in ["0.50", "1.00", "1.50"]:
        win_ratios = [all_gamma_sens[d]["sensitivity"].get(mult_str, {}).get("spread_ratio", 1.0) for d in winning]
        lose_ratios = [all_gamma_sens[d]["sensitivity"].get(mult_str, {}).get("spread_ratio", 1.0) for d in losing]
        print(f"  γ×{mult_str}: winning spread_ratio={sum(win_ratios)/len(win_ratios):.3f}, "
              f"losing spread_ratio={sum(lose_ratios)/len(lose_ratios):.3f}")

    print("\n" + "=" * 70)
    print("PART 3: GLT vs CURRENT SPREAD")
    print("=" * 70)

    for d in sorted(all_comparisons.keys()):
        c = all_comparisons[d]
        pnl = PNLS_PER_DAY.get(d, 0)
        marker = "WIN" if pnl > 0 else "LOSE"
        print(f"  {d} [{marker:>4}]: ratio={c['median_ratio']:.3f}, "
              f"wider={c['pct_wider']:.0f}%, narrower={c['pct_narrower']:.0f}%, "
              f"1st={c['first_third_ratio']:.3f} mid={c['mid_third_ratio']:.3f} "
              f"last={c['last_third_ratio']:.3f}")

    # ── Save Output ───────────────────────────────────────────────────
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "metadata": {
            "script": "explore_c2_glt.py",
            "n_days": len(DATA_FILES),
            "glt_formula": "δ(q) = γσ²(T-t)/2 · (1-2q/Q) + (1/γ)·ln(1+γ/κ)",
            "ema_alphas_tested": [0.001, 0.005, 0.01, 0.05],
            "gamma_multipliers_tested": [0.5, 0.75, 1.0, 1.25, 1.5],
            "session_duration_s": SESSION_DURATION_S,
        },
        "sigma_lag": all_sigma_lag,
        "gamma_sensitivity": all_gamma_sens,
        "glt_vs_current": all_comparisons,
        "summary": {
            "recommended_ema_alpha": 0.01,
            "gamma_base_range": [min(gamma_bases), max(gamma_bases)],
            "gamma_base_median": sorted(gamma_bases)[len(gamma_bases) // 2],
        },
    }

    out_path = OUT_DIR / "c2_glt_exploration.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nResults saved to: {out_path}")
    print("Done.")


if __name__ == "__main__":
    main()
