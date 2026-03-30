"""CTR Data Exploration — validate Schmidhuber scaling on TMFD6.

Queries ClickHouse for TMFD6 L1 data and computes:
1. Distribution of z-scores (phi) at different horizons
2. E[R(t+1)] as a function of phi — cubic polynomial fit
3. Hit rate when phi crosses various thresholds
4. Forward returns conditioned on phi buckets
5. Comparison with CBS trigger distribution

Usage:
    python -m research.alphas.critical_trend_reversion.explore

Requires ClickHouse with TMFD6 data (9.16M rows, 58 days).
"""

from __future__ import annotations

import math
import sys
from collections import defaultdict

import numpy as np

# ---------------------------------------------------------------------------
# ClickHouse query helper
# ---------------------------------------------------------------------------


def _query_ch(sql: str) -> list[tuple]:
    """Query ClickHouse and return rows as list of tuples."""
    try:
        from clickhouse_driver import Client

        import os
        client = Client(
            host=os.environ.get("HFT_CLICKHOUSE_HOST", "localhost"),
            port=int(os.environ.get("HFT_CLICKHOUSE_NATIVE_PORT", "9000")),
            user="default",
            password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        )
        return client.execute(sql)
    except ImportError:
        print("ERROR: clickhouse_driver not installed. Run: pip install clickhouse-driver")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: ClickHouse query failed: {e}")
        sys.exit(1)


def load_tmfd6_midprices() -> tuple[np.ndarray, np.ndarray]:
    """Load TMFD6 mid-prices and timestamps from ClickHouse.

    Returns (timestamps_ns, mid_prices_x2) as numpy arrays.
    """
    print("Loading TMFD6 data from ClickHouse...")
    sql = """
    SELECT
        exch_ts,
        toInt64(bids_price[1] + asks_price[1]) as mid_x2
    FROM hft.market_data
    WHERE symbol = 'TMFD6'
      AND length(bids_price) > 0
      AND length(asks_price) > 0
      AND bids_price[1] > 0
      AND asks_price[1] > 0
    ORDER BY exch_ts
    """
    rows = _query_ch(sql)
    if not rows:
        print("ERROR: No TMFD6 data found in ClickHouse")
        sys.exit(1)

    print(f"  Loaded {len(rows):,} rows")
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    mid = np.array([r[1] for r in rows], dtype=np.int64)
    return ts, mid


def compute_returns(mid_x2: np.ndarray) -> np.ndarray:
    """Compute log-returns from mid_x2 prices."""
    # Use float64 for returns (alpha module, not accounting — float OK per rule 11)
    mid_f = mid_x2.astype(np.float64)
    returns = np.diff(mid_f) / mid_f[:-1]
    return returns


def compute_ema(values: np.ndarray, alpha: float) -> np.ndarray:
    """Compute exponential moving average."""
    n = len(values)
    ema = np.zeros(n, dtype=np.float64)
    ema[0] = values[0]
    one_minus_a = 1.0 - alpha
    for i in range(1, n):
        ema[i] = alpha * values[i] + one_minus_a * ema[i - 1]
    return ema


def compute_phi_series(
    returns: np.ndarray, horizon_min: int, tick_rate: float = 1.8
) -> np.ndarray:
    """Compute trend strength (phi / t-statistic) series for a given horizon.

    Parameters
    ----------
    returns : array of log-returns
    horizon_min : horizon in minutes
    tick_rate : ticks per second (TMFD6 ~ 1.8)

    Returns
    -------
    phi : array of trend strength values (same length as returns)
    """
    n_ticks = max(1, int(horizon_min * 60 * tick_rate))
    alpha = 1.0 - math.exp(-1.0 / n_ticks)

    n = len(returns)
    phi = np.zeros(n, dtype=np.float64)
    ema_ret = 0.0
    ema_ret_sq = 0.0

    for i in range(n):
        r = returns[i]
        ema_ret = alpha * r + (1.0 - alpha) * ema_ret
        ema_ret_sq = alpha * (r * r) + (1.0 - alpha) * ema_ret_sq

        var = ema_ret_sq - ema_ret * ema_ret
        if var < 1e-20 or i < 10:
            phi[i] = 0.0
            continue

        n_eff = min(i + 1, n_ticks)
        sqrt_n = math.sqrt(n_eff)
        phi[i] = ema_ret / math.sqrt(var) * sqrt_n
        phi[i] = max(-5.0, min(5.0, phi[i]))

    return phi


def bucket_analysis(
    phi: np.ndarray,
    forward_returns: np.ndarray,
    n_buckets: int = 20,
    warmup: int = 500,
) -> dict:
    """Analyze forward returns conditioned on phi buckets.

    Returns dict with bucket centers, mean returns, counts, and hit rates.
    """
    min_len = min(len(phi), len(forward_returns))
    phi_valid = phi[warmup:min_len]
    fwd_valid = forward_returns[warmup:min_len]

    # Compute percentile-based buckets
    pcts = np.linspace(0, 100, n_buckets + 1)
    edges = np.percentile(phi_valid, pcts)

    centers = []
    mean_rets = []
    counts = []
    hit_rates = []

    for j in range(n_buckets):
        mask = (phi_valid >= edges[j]) & (phi_valid < edges[j + 1])
        if j == n_buckets - 1:
            mask = (phi_valid >= edges[j]) & (phi_valid <= edges[j + 1])

        count = mask.sum()
        if count < 10:
            continue

        fwd = fwd_valid[mask]
        center = (edges[j] + edges[j + 1]) / 2.0
        mean_ret = fwd.mean()
        # Hit rate: fraction where contrarian would be correct
        # (positive fwd return when phi < 0, negative when phi > 0)
        if center > 0:
            hr = (fwd < 0).sum() / count
        elif center < 0:
            hr = (fwd > 0).sum() / count
        else:
            hr = 0.5

        centers.append(center)
        mean_rets.append(mean_ret)
        counts.append(count)
        hit_rates.append(hr)

    return {
        "centers": np.array(centers),
        "mean_returns": np.array(mean_rets),
        "counts": np.array(counts),
        "hit_rates": np.array(hit_rates),
    }


def fit_cubic(phi: np.ndarray, fwd_ret: np.ndarray, warmup: int = 500) -> dict:
    """Fit cubic polynomial E[R] = a + b*phi + c*phi^3 to data.

    Returns dict with coefficients and R-squared.
    """
    # Align: phi[t] predicts fwd_ret[t] (which is returns[t+1])
    min_len = min(len(phi), len(fwd_ret))
    x = phi[warmup:min_len]
    y = fwd_ret[warmup:min_len]

    # Design matrix: [1, phi, phi^3]
    X = np.column_stack([np.ones_like(x), x, x**3])

    # OLS fit
    try:
        beta, residuals, rank, sv = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return {"a": 0.0, "b": 0.0, "c": 0.0, "r_squared": 0.0, "phi_c": float("inf")}

    a, b, c = beta

    # R-squared
    y_pred = X @ beta
    ss_res = ((y - y_pred) ** 2).sum()
    ss_tot = ((y - y.mean()) ** 2).sum()
    r_sq = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    # Critical threshold: dE/dphi = b + 3c*phi^2 = 0 => phi_c = sqrt(-b/(3c))
    if b * c < 0:
        phi_c = math.sqrt(abs(b / (3.0 * c)))
    else:
        phi_c = float("inf")

    return {
        "a": a,
        "b": b,
        "c": c,
        "r_squared": r_sq,
        "r_squared_bp": r_sq * 10000,
        "phi_c": phi_c,
        "n_obs": len(x),
    }


def cbs_comparison(
    phi_32: np.ndarray,
    mid_x2: np.ndarray,
    ts_ns: np.ndarray,
    warmup: int = 500,
) -> dict:
    """Compare CTR signals with CBS trigger distribution.

    CBS triggers on 40bps move in 600s window.
    CTR triggers on phi_32 > phi_c.
    """
    # Compute CBS-style moves: max move in trailing 600s
    n = min(len(mid_x2), len(phi_32))
    # Rough: assume ~1.8 ticks/sec, 600s = 1080 ticks
    window = 1080
    cbs_moves_bps = np.zeros(n, dtype=np.float64)
    ctr_triggers = np.zeros(n, dtype=np.int8)

    phi_c_32 = 1.58  # sqrt(0.0005/0.0002) from defaults

    for i in range(warmup, n):
        # CBS move
        start = max(0, i - window)
        oldest_mid = mid_x2[start]
        if oldest_mid > 0:
            cbs_moves_bps[i] = abs(mid_x2[i] - oldest_mid) / oldest_mid * 10000

        # CTR trigger
        if abs(phi_32[i]) > phi_c_32:
            ctr_triggers[i] = 1

    cbs_triggers = (cbs_moves_bps > 40).astype(np.int8)

    # Overlap analysis
    both = ((cbs_triggers == 1) & (ctr_triggers == 1)).sum()
    cbs_only = ((cbs_triggers == 1) & (ctr_triggers == 0)).sum()
    ctr_only = ((cbs_triggers == 0) & (ctr_triggers == 1)).sum()
    neither = ((cbs_triggers == 0) & (ctr_triggers == 0)).sum()

    return {
        "cbs_trigger_count": int(cbs_triggers.sum()),
        "ctr_trigger_count": int(ctr_triggers.sum()),
        "both": int(both),
        "cbs_only": int(cbs_only),
        "ctr_only": int(ctr_only),
        "neither": int(neither),
        "cbs_trigger_pct": float(cbs_triggers.mean()) * 100,
        "ctr_trigger_pct": float(ctr_triggers.mean()) * 100,
    }


def run_exploration() -> None:
    """Main exploration pipeline."""
    print("=" * 70)
    print("CTR Data Exploration — TMFD6")
    print("=" * 70)

    # Load data
    ts_ns, mid_x2 = load_tmfd6_midprices()
    returns = compute_returns(mid_x2)
    n = len(returns)
    print(f"  Total returns: {n:,}")
    print(f"  Date range: {ts_ns[0]} to {ts_ns[-1]}")

    # Compute forward returns (1-tick, 10-tick, 60-tick, 300-tick)
    fwd_1 = np.zeros(n, dtype=np.float64)
    fwd_1[:-1] = returns[1:]  # Next tick return

    # Compute phi at each horizon
    horizons = [2, 4, 8, 16, 32, 64]
    phi_series: dict[int, np.ndarray] = {}

    for h in horizons:
        print(f"\nComputing phi for T={h} min...")
        phi = compute_phi_series(returns, h)
        phi_series[h] = phi

        # Basic stats
        valid = phi[500:]  # skip warmup
        print(f"  phi stats: mean={valid.mean():.4f}, std={valid.std():.4f}, "
              f"min={valid.min():.2f}, max={valid.max():.2f}")

        # Percentiles
        p5, p25, p50, p75, p95 = np.percentile(valid, [5, 25, 50, 75, 95])
        print(f"  percentiles: P5={p5:.2f}, P25={p25:.2f}, P50={p50:.2f}, "
              f"P75={p75:.2f}, P95={p95:.2f}")

        # Fraction above various thresholds
        for thresh in [1.0, 1.5, 2.0, 2.5]:
            frac = (np.abs(valid) > thresh).mean() * 100
            print(f"  |phi| > {thresh}: {frac:.2f}%")

    # Cubic polynomial fit at each horizon
    print("\n" + "=" * 70)
    print("Cubic Polynomial Fit: E[R(t+1)] = a + b*phi + c*phi^3")
    print("=" * 70)
    # Compute multi-tick forward returns for different horizons
    fwd_horizons = {"1tick": fwd_1}
    for fwd_ticks, label in [(108, "1min"), (540, "5min"), (1080, "10min")]:
        fwd_k = np.zeros(n, dtype=np.float64)
        mid_f = mid_x2.astype(np.float64)
        for i in range(n - fwd_ticks):
            if mid_f[i] > 0:
                fwd_k[i] = (mid_f[i + fwd_ticks] - mid_f[i]) / mid_f[i]
        fwd_horizons[label] = fwd_k

    for fwd_label, fwd_ret in fwd_horizons.items():
        print(f"\n--- Forward return: {fwd_label} ---")
        print(f"{'Horizon':>8} {'b':>14} {'c':>14} {'phi_c':>8} {'R2 (bp)':>10} {'N':>10}")
        print("-" * 70)

        for h in horizons:
            result = fit_cubic(phi_series[h], fwd_ret)
            print(f"{h:>5}min {result['b']:>14.8f} {result['c']:>14.8f} "
                  f"{result['phi_c']:>8.2f} {result['r_squared_bp']:>10.4f} "
                  f"{result['n_obs']:>10,}")

    # Bucket analysis for actionable horizons
    print("\n" + "=" * 70)
    print("Bucket Analysis: Forward Returns by phi Bucket")
    print("=" * 70)

    for h in [16, 32, 64]:
        print(f"\n--- T = {h} min ---")
        ba = bucket_analysis(phi_series[h], fwd_1, n_buckets=10)
        print(f"{'Bucket':>8} {'Mean Ret':>12} {'Hit Rate':>10} {'Count':>8}")
        for i in range(len(ba["centers"])):
            print(f"{ba['centers'][i]:>8.2f} {ba['mean_returns'][i]:>12.8f} "
                  f"{ba['hit_rates'][i]:>10.4f} {ba['counts'][i]:>8,}")

    # CBS comparison
    if 32 in phi_series:
        print("\n" + "=" * 70)
        print("CBS vs CTR Trigger Comparison")
        print("=" * 70)
        comp = cbs_comparison(phi_series[32], mid_x2, ts_ns)
        for k, v in comp.items():
            print(f"  {k}: {v}")

    print("\n" + "=" * 70)
    print("Exploration complete.")
    print("=" * 70)


if __name__ == "__main__":
    run_exploration()
