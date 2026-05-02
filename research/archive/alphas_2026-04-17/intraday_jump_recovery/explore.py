"""IJR Data Exploration — TMFD6 push-response analysis.

Implements Vlasiuk & Smirnov's push-response methodology on TMFD6.

Analyzes:
1. Push distribution at different lag levels
2. Push-response conditional expectation (the core test)
3. Asymmetry: negative vs positive push response
4. Comparison with CBS triggers
5. Forward returns after jumps

Usage:
    CLICKHOUSE_PASSWORD=<password> python -m research.alphas.intraday_jump_recovery.explore
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np


def _get_ch_client():
    """Get ClickHouse client."""
    try:
        from clickhouse_driver import Client
        return Client(
            host=os.environ.get("HFT_CLICKHOUSE_HOST", "localhost"),
            port=int(os.environ.get("HFT_CLICKHOUSE_NATIVE_PORT", "9000")),
            user="default",
            password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        )
    except ImportError:
        print("ERROR: clickhouse_driver not installed")
        sys.exit(1)


def load_tmfd6_midprices() -> tuple[np.ndarray, np.ndarray]:
    """Load TMFD6 mid-prices from ClickHouse."""
    client = _get_ch_client()
    print("Loading TMFD6 data...")
    sql = """
    SELECT exch_ts, toInt64(bids_price[1] + asks_price[1]) as mid_x2
    FROM hft.market_data
    WHERE symbol = 'TMFD6'
      AND length(bids_price) > 0 AND length(asks_price) > 0
      AND bids_price[1] > 0 AND asks_price[1] > 0
    ORDER BY exch_ts
    """
    rows = client.execute(sql)
    if not rows:
        print("ERROR: No data")
        sys.exit(1)
    print(f"  Loaded {len(rows):,} rows")
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    mid = np.array([r[1] for r in rows], dtype=np.int64)
    return ts, mid


def find_day_boundaries(ts_ns: np.ndarray, gap_ns: int = 3_600_000_000_000) -> list[int]:
    """Find day start indices (gaps > 1 hour)."""
    starts = [0]
    for i in range(1, len(ts_ns)):
        if ts_ns[i] - ts_ns[i - 1] > gap_ns:
            starts.append(i)
    return starts


def compute_push_response(
    mid_x2: np.ndarray,
    ts_ns: np.ndarray,
    lag: int,
    day_starts: list[int],
    skip_opening_ticks: int = 1944,  # ~30 min at 1.08 ticks/sec... use 1944 for 1.8/sec * 30*60
) -> tuple[np.ndarray, np.ndarray]:
    """Compute push-response pairs for a given lag.

    Only includes pairs where both push and response are within the same session,
    and after the opening skip period.

    Returns (pushes, responses) arrays in fractional units (not bps).
    """
    n = len(mid_x2)
    # Map each tick to its day index
    day_idx = np.zeros(n, dtype=np.int32)
    for d, start in enumerate(day_starts):
        end = day_starts[d + 1] if d + 1 < len(day_starts) else n
        day_idx[start:end] = d

    pushes = []
    responses = []

    for i in range(lag, n - lag):
        # Same-day constraint for push and response
        if day_idx[i - lag] != day_idx[i] or day_idx[i] != day_idx[i + lag]:
            continue

        # Skip opening period
        day_start = day_starts[day_idx[i]]
        if i - day_start < skip_opening_ticks:
            continue

        prev = mid_x2[i - lag]
        curr = mid_x2[i]
        fut = mid_x2[i + lag]

        if prev <= 0 or curr <= 0 or fut <= 0:
            continue

        push = (float(curr) - float(prev)) / float(prev)
        response = (float(fut) - float(curr)) / float(curr)
        pushes.append(push)
        responses.append(response)

    return np.array(pushes), np.array(responses)


def analyze_push_response(
    pushes: np.ndarray,
    responses: np.ndarray,
    n_bins: int = 40,
    label: str = "",
) -> dict:
    """Bin pushes and compute conditional mean response.

    Returns dict with bin centers, mean responses, counts.
    """
    if len(pushes) == 0:
        return {"centers": np.array([]), "mean_resp": np.array([]),
                "counts": np.array([]), "mean_resp_bps": np.array([])}

    # Standardize pushes
    mu_p = pushes.mean()
    sigma_p = pushes.std()
    if sigma_p < 1e-15:
        sigma_p = 1.0
    z_push = (pushes - mu_p) / sigma_p

    # Standardize responses (for comparison, but report raw too)
    mu_r = responses.mean()
    sigma_r = responses.std()

    # Bin by z_push
    z_min, z_max = -4.0, 4.0
    edges = np.linspace(z_min, z_max, n_bins + 1)

    centers = []
    mean_resps = []
    mean_resps_bps = []
    counts = []

    for j in range(n_bins):
        mask = (z_push >= edges[j]) & (z_push < edges[j + 1])
        if j == n_bins - 1:
            mask = (z_push >= edges[j]) & (z_push <= edges[j + 1])

        count = mask.sum()
        if count < 50:  # min support
            continue

        center = (edges[j] + edges[j + 1]) / 2.0
        mean_r = responses[mask].mean()

        centers.append(center)
        mean_resps.append(mean_r)
        mean_resps_bps.append(mean_r * 10000.0)
        counts.append(count)

    return {
        "centers": np.array(centers),
        "mean_resp": np.array(mean_resps),
        "mean_resp_bps": np.array(mean_resps_bps),
        "counts": np.array(counts),
        "push_mean": mu_p,
        "push_std": sigma_p,
        "push_std_bps": sigma_p * 10000.0,
        "n_pairs": len(pushes),
    }


def test_asymmetry(pushes: np.ndarray, responses: np.ndarray) -> dict:
    """Test push-response asymmetry: negative push recovery vs positive push recovery."""
    if len(pushes) == 0:
        return {}

    mu_p = pushes.mean()
    sigma_p = max(pushes.std(), 1e-15)
    z_push = (pushes - mu_p) / sigma_p

    # Large negative pushes (z < -2)
    neg_mask = z_push < -2.0
    neg_count = neg_mask.sum()
    neg_resp = responses[neg_mask].mean() * 10000 if neg_count > 10 else float("nan")

    # Large positive pushes (z > 2)
    pos_mask = z_push > 2.0
    pos_count = pos_mask.sum()
    pos_resp = responses[pos_mask].mean() * 10000 if pos_count > 10 else float("nan")

    # Moderate pushes for comparison
    mod_mask = (z_push > -0.5) & (z_push < 0.5)
    mod_count = mod_mask.sum()
    mod_resp = responses[mod_mask].mean() * 10000 if mod_count > 10 else float("nan")

    return {
        "neg_push_count": int(neg_count),
        "neg_push_response_bps": neg_resp,
        "pos_push_count": int(pos_count),
        "pos_push_response_bps": pos_resp,
        "mod_push_count": int(mod_count),
        "mod_push_response_bps": mod_resp,
        "asymmetry_ratio": abs(neg_resp / pos_resp) if pos_resp != 0 and not (math.isnan(neg_resp) or math.isnan(pos_resp)) else float("nan"),
    }


def run_exploration() -> None:
    """Main exploration pipeline."""
    print("=" * 70)
    print("IJR Data Exploration — TMFD6 Push-Response Analysis")
    print("=" * 70)

    ts_ns, mid_x2 = load_tmfd6_midprices()
    n = len(mid_x2)
    day_starts = find_day_boundaries(ts_ns)
    n_days = len(day_starts)
    print(f"  {n_days} trading days detected")

    # --- Step 1: Push distribution at different lags ---
    # TMFD6: 1.8 ticks/sec
    # Lag mapping: 100 ticks ~ 56s, 540 ~ 300s (CBS hold), 1080 ~ 600s (CBS window)
    # 5000 ~ 46 min, 10000 ~ 93 min
    lags = [100, 540, 1080, 3000, 5000]
    lag_labels = ["56s (100t)", "300s (540t)", "600s (1080t)", "28min (3000t)", "46min (5000t)"]

    print("\n--- Push Distribution by Lag ---")
    print(f"{'Lag':>20s} {'N pairs':>10} {'Push std(bps)':>14} {'Response std(bps)':>18}")
    print("-" * 65)

    all_results: dict[int, dict] = {}

    for lag, label in zip(lags, lag_labels):
        print(f"\nComputing push-response for lag={lag} ({label})...", end=" ", flush=True)
        pushes, responses = compute_push_response(mid_x2, ts_ns, lag, day_starts)
        print(f"{len(pushes):,} pairs")

        if len(pushes) > 0:
            push_std_bps = pushes.std() * 10000
            resp_std_bps = responses.std() * 10000
            print(f"{'  ' + label:>20s} {len(pushes):>10,} {push_std_bps:>14.2f} {resp_std_bps:>18.2f}")

            # Analyze push-response
            result = analyze_push_response(pushes, responses, n_bins=20, label=label)
            all_results[lag] = result

            # Print condensed push-response table
            print(f"\n  Push-Response Table (lag={lag}):")
            print(f"  {'z_push':>8} {'E[resp] bps':>12} {'Count':>8}")
            for i in range(len(result["centers"])):
                print(f"  {result['centers'][i]:>8.2f} {result['mean_resp_bps'][i]:>12.4f} {result['counts'][i]:>8,}")

    # --- Step 2: Asymmetry Test ---
    print("\n" + "=" * 70)
    print("Asymmetry Test: Negative Push Recovery vs Positive Push Recovery")
    print("=" * 70)
    print(f"{'Lag':>20s} {'Neg(z<-2)':>10} {'Resp(bps)':>10} {'Pos(z>2)':>10} {'Resp(bps)':>10} {'Asym ratio':>12}")
    print("-" * 75)

    for lag, label in zip(lags, lag_labels):
        pushes, responses = compute_push_response(mid_x2, ts_ns, lag, day_starts)
        if len(pushes) > 0:
            asym = test_asymmetry(pushes, responses)
            neg_r = asym.get("neg_push_response_bps", float("nan"))
            pos_r = asym.get("pos_push_response_bps", float("nan"))
            ratio = asym.get("asymmetry_ratio", float("nan"))
            print(f"{'  ' + label:>20s} "
                  f"{asym.get('neg_push_count', 0):>10} {neg_r:>10.3f} "
                  f"{asym.get('pos_push_count', 0):>10} {pos_r:>10.3f} "
                  f"{ratio:>12.2f}")

    # --- Step 3: Key question — does contrarian work? ---
    print("\n" + "=" * 70)
    print("Contrarian Strategy Test: Enter opposite to large push, hold L ticks")
    print("=" * 70)
    print(f"{'Lag':>12} {'z_thresh':>10} {'Trades':>8} {'Mean PnL(bps)':>14} {'Net(bps)':>10} {'Win%':>8}")
    print("-" * 65)

    for lag in [540, 1080, 3000, 5000]:
        pushes, responses = compute_push_response(mid_x2, ts_ns, lag, day_starts)
        if len(pushes) == 0:
            continue

        mu_p = pushes.mean()
        sigma_p = max(pushes.std(), 1e-15)
        z_push = (pushes - mu_p) / sigma_p

        for z_thresh in [1.5, 2.0, 2.5, 3.0]:
            # Contrarian: buy after large negative push, sell after large positive push
            large_mask = np.abs(z_push) > z_thresh
            if large_mask.sum() < 5:
                continue

            # Contrarian PnL: response * -sign(push)
            signs = -np.sign(pushes[large_mask])
            contrarian_pnl = responses[large_mask] * signs
            contrarian_bps = contrarian_pnl * 10000

            n_trades = len(contrarian_bps)
            mean_pnl = contrarian_bps.mean()
            net_pnl = mean_pnl - 1.33
            win_pct = (contrarian_bps > 0).mean() * 100

            print(f"{lag:>12} {z_thresh:>10.1f} {n_trades:>8} "
                  f"{mean_pnl:>14.3f} {net_pnl:>10.3f} {win_pct:>8.1f}")

    # --- Step 4: Momentum test (opposite of contrarian) ---
    print("\n" + "=" * 70)
    print("Momentum Strategy Test: Enter WITH large push, hold L ticks")
    print("=" * 70)
    print(f"{'Lag':>12} {'z_thresh':>10} {'Trades':>8} {'Mean PnL(bps)':>14} {'Net(bps)':>10} {'Win%':>8}")
    print("-" * 65)

    for lag in [540, 1080, 3000, 5000]:
        pushes, responses = compute_push_response(mid_x2, ts_ns, lag, day_starts)
        if len(pushes) == 0:
            continue

        mu_p = pushes.mean()
        sigma_p = max(pushes.std(), 1e-15)
        z_push = (pushes - mu_p) / sigma_p

        for z_thresh in [1.5, 2.0, 2.5, 3.0]:
            large_mask = np.abs(z_push) > z_thresh
            if large_mask.sum() < 5:
                continue

            # Momentum: response * sign(push)
            signs = np.sign(pushes[large_mask])
            momentum_pnl = responses[large_mask] * signs
            momentum_bps = momentum_pnl * 10000

            n_trades = len(momentum_bps)
            mean_pnl = momentum_bps.mean()
            net_pnl = mean_pnl - 1.33
            win_pct = (momentum_bps > 0).mean() * 100

            print(f"{lag:>12} {z_thresh:>10.1f} {n_trades:>8} "
                  f"{mean_pnl:>14.3f} {net_pnl:>10.3f} {win_pct:>8.1f}")

    print("\n" + "=" * 70)
    print("Exploration complete.")
    print("=" * 70)


if __name__ == "__main__":
    run_exploration()
