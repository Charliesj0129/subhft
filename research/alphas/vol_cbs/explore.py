"""Vol-CBS Data Exploration — TMFD6 volatility regime analysis.

Analyzes:
1. Rolling ATR at multiple windows
2. ATR distribution + time-of-day pattern
3. Vol regime clusters (low/medium/high)
4. CBS triggers cross-tabulated with vol regime
5. CBS forward returns conditioned on vol regime
6. Optimal k for ATR-based threshold

Usage:
    CLICKHOUSE_PASSWORD=changeme python -m research.alphas.vol_cbs.explore
"""

from __future__ import annotations

import math
import os
import sys
from collections import defaultdict

import numpy as np


def _get_ch_client():
    """Get ClickHouse client with env-based config."""
    try:
        from clickhouse_driver import Client

        client = Client(
            host=os.environ.get("HFT_CLICKHOUSE_HOST", "localhost"),
            port=int(os.environ.get("HFT_CLICKHOUSE_NATIVE_PORT", "9000")),
            user="default",
            password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        )
        return client
    except ImportError:
        print("ERROR: clickhouse_driver not installed")
        sys.exit(1)


def load_tmfd6_data() -> dict[str, np.ndarray]:
    """Load TMFD6 tick data from ClickHouse.

    Returns dict with keys: ts_ns, mid_x2, bid, ask, spread
    """
    client = _get_ch_client()
    print("Loading TMFD6 data...")

    sql = """
    SELECT
        exch_ts,
        bids_price[1] as bid,
        asks_price[1] as ask,
        toInt64(bids_price[1] + asks_price[1]) as mid_x2
    FROM hft.market_data
    WHERE symbol = 'TMFD6'
      AND length(bids_price) > 0
      AND length(asks_price) > 0
      AND bids_price[1] > 0
      AND asks_price[1] > 0
    ORDER BY exch_ts
    """
    rows = client.execute(sql)
    if not rows:
        print("ERROR: No TMFD6 data found")
        sys.exit(1)

    n = len(rows)
    print(f"  Loaded {n:,} rows")

    ts = np.array([r[0] for r in rows], dtype=np.int64)
    bid = np.array([r[1] for r in rows], dtype=np.int64)
    ask = np.array([r[2] for r in rows], dtype=np.int64)
    mid_x2 = np.array([r[3] for r in rows], dtype=np.int64)

    return {"ts_ns": ts, "bid": bid, "ask": ask, "mid_x2": mid_x2}


def compute_atr_ema(mid_x2: np.ndarray, halflife: int) -> np.ndarray:
    """Compute EMA-based ATR from tick-by-tick mid_x2."""
    n = len(mid_x2)
    alpha = 1.0 - math.exp(-math.log(2.0) / halflife)
    atr = np.zeros(n, dtype=np.float64)

    for i in range(1, n):
        tr = abs(float(mid_x2[i] - mid_x2[i - 1]))
        atr[i] = alpha * tr + (1.0 - alpha) * atr[i - 1]

    return atr


def compute_returns(mid_x2: np.ndarray) -> np.ndarray:
    """Compute tick-to-tick returns."""
    mid_f = mid_x2.astype(np.float64)
    ret = np.zeros(len(mid_f), dtype=np.float64)
    ret[1:] = np.diff(mid_f) / mid_f[:-1]
    return ret


def classify_vol_regime(
    atr_bps: np.ndarray,
) -> np.ndarray:
    """Classify each tick into vol regime based on ATR percentiles.

    Returns array of 0=low, 1=medium, 2=high
    """
    valid = atr_bps[atr_bps > 0]
    if len(valid) == 0:
        return np.zeros(len(atr_bps), dtype=np.int8)

    p33 = np.percentile(valid, 33)
    p66 = np.percentile(valid, 66)

    regime = np.zeros(len(atr_bps), dtype=np.int8)
    regime[atr_bps > p66] = 2  # high
    regime[(atr_bps > p33) & (atr_bps <= p66)] = 1  # medium
    # 0 = low (default)

    return regime


def simulate_cbs_triggers(
    mid_x2: np.ndarray,
    ts_ns: np.ndarray,
    threshold_bps: float = 40.0,
    window_ns: int = 600_000_000_000,
    hold_ns: int = 300_000_000_000,
    stop_bps: float = 15.0,
    skip_first_ns: int = 30 * 60 * 1_000_000_000,
) -> dict[str, np.ndarray]:
    """Simulate CBS triggers and compute per-trade results.

    Returns dict with arrays per-trade:
        trigger_idx, direction, entry_mid, exit_mid, pnl_bps, vol_regime_at_entry
    """
    n = len(mid_x2)

    trigger_indices = []
    directions = []
    entry_mids = []
    exit_mids = []
    pnl_bps_list = []

    # Build day boundaries for session gating
    # Approximate: detect gaps > 1 hour as day boundaries
    day_starts = [0]
    for i in range(1, n):
        if ts_ns[i] - ts_ns[i - 1] > 3_600_000_000_000:  # 1 hour gap
            day_starts.append(i)

    state = "idle"
    entry_idx = 0
    entry_mid = 0
    direction = 0
    next_allowed = 0

    # Approximate window in ticks: window_ns / (average tick interval)
    # For TMFD6: ~556ms per tick → window_ns / 556e6 ≈ 1080 ticks
    avg_tick_ns = 556_000_000  # ~1.8 ticks/sec
    window_ticks = max(1, window_ns // avg_tick_ns)
    hold_ticks = max(1, hold_ns // avg_tick_ns)

    for i in range(window_ticks, n):
        if state == "idle":
            if i < next_allowed:
                continue

            # Session gate: skip first 30 min of each day
            day_start = day_starts[0]
            for ds in day_starts:
                if ds <= i:
                    day_start = ds
                else:
                    break
            if ts_ns[i] - ts_ns[day_start] < skip_first_ns:
                continue

            # Compute move from window start
            oldest_idx = max(0, i - window_ticks)
            oldest_mid = mid_x2[oldest_idx]
            if oldest_mid <= 0:
                continue

            diff = int(mid_x2[i]) - int(oldest_mid)
            move_bps = abs(diff) / oldest_mid * 10000.0

            if move_bps >= threshold_bps:
                # Trigger! Enter contrarian
                direction = -1 if diff > 0 else 1
                entry_idx = i
                entry_mid = mid_x2[i]
                state = "positioned"

        elif state == "positioned":
            elapsed_ticks = i - entry_idx

            # Check stop-loss
            pnl_diff = direction * (int(mid_x2[i]) - int(entry_mid))
            pnl_bps_curr = pnl_diff / entry_mid * 10000.0

            exit_reason = None
            if pnl_bps_curr < -stop_bps:
                exit_reason = "stop"
            elif elapsed_ticks >= hold_ticks:
                exit_reason = "time"

            if exit_reason:
                trigger_indices.append(entry_idx)
                directions.append(direction)
                entry_mids.append(entry_mid)
                exit_mids.append(mid_x2[i])
                pnl_bps_list.append(pnl_bps_curr)

                state = "idle"
                next_allowed = i + 10  # small cooldown

    return {
        "trigger_idx": np.array(trigger_indices, dtype=np.int64),
        "direction": np.array(directions, dtype=np.int8),
        "entry_mid": np.array(entry_mids, dtype=np.int64),
        "exit_mid": np.array(exit_mids, dtype=np.int64),
        "pnl_bps": np.array(pnl_bps_list, dtype=np.float64),
    }


def run_exploration() -> None:
    """Main exploration pipeline."""
    print("=" * 70)
    print("Vol-CBS Data Exploration — TMFD6")
    print("=" * 70)

    data = load_tmfd6_data()
    ts_ns = data["ts_ns"]
    mid_x2 = data["mid_x2"]
    n = len(mid_x2)

    # --- Step 1: ATR at multiple windows ---
    print("\n--- ATR Computation ---")
    halflives = {"5min": 540, "15min": 1620, "30min": 3240, "60min": 6480}
    atr_series: dict[str, np.ndarray] = {}
    atr_bps_series: dict[str, np.ndarray] = {}

    for name, hl in halflives.items():
        atr = compute_atr_ema(mid_x2, hl)
        atr_bps = np.zeros(n, dtype=np.float64)
        mask = mid_x2 > 0
        atr_bps[mask] = atr[mask] / mid_x2[mask].astype(np.float64) * 10000.0
        atr_series[name] = atr
        atr_bps_series[name] = atr_bps

        valid = atr_bps[atr_bps > 0]
        if len(valid) > 0:
            p5, p25, p50, p75, p95 = np.percentile(valid, [5, 25, 50, 75, 95])
            print(f"  {name:>5s} ATR(bps): mean={valid.mean():.2f}, "
                  f"P5={p5:.2f}, P25={p25:.2f}, P50={p50:.2f}, "
                  f"P75={p75:.2f}, P95={p95:.2f}")

    # --- Step 2: Vol regime classification ---
    print("\n--- Vol Regime Classification (using 15min ATR) ---")
    atr_15m = atr_bps_series["15min"]
    vol_regime = classify_vol_regime(atr_15m)

    for r, label in [(0, "LOW"), (1, "MEDIUM"), (2, "HIGH")]:
        count = (vol_regime == r).sum()
        pct = count / n * 100
        if count > 0:
            avg_atr = atr_15m[vol_regime == r].mean()
            print(f"  {label:>6s}: {count:>10,} ticks ({pct:>5.1f}%), avg ATR={avg_atr:.2f} bps")

    # --- Step 3: Simulate CBS with fixed threshold ---
    print("\n--- CBS Simulation (fixed 40 bps) ---")
    cbs_results = simulate_cbs_triggers(mid_x2, ts_ns, threshold_bps=40.0)
    n_trades = len(cbs_results["pnl_bps"])
    print(f"  Total trades: {n_trades}")

    if n_trades > 0:
        pnl = cbs_results["pnl_bps"]
        print(f"  Mean P&L: {pnl.mean():.2f} bps (before cost)")
        print(f"  Mean P&L: {pnl.mean() - 1.33:.2f} bps (after 1.33 bps RT cost)")
        print(f"  Win rate: {(pnl > 0).mean() * 100:.1f}%")
        print(f"  Std: {pnl.std():.2f} bps")
        print(f"  Min: {pnl.min():.2f}, Max: {pnl.max():.2f}")

        # --- Step 4: Cross-tabulate CBS triggers with vol regime ---
        print("\n--- CBS Triggers by Vol Regime ---")
        trigger_idxs = cbs_results["trigger_idx"]

        for r, label in [(0, "LOW"), (1, "MEDIUM"), (2, "HIGH")]:
            mask = vol_regime[trigger_idxs] == r
            count = mask.sum()
            if count > 0:
                mean_pnl = pnl[mask].mean()
                mean_pnl_net = mean_pnl - 1.33
                win_rate = (pnl[mask] > 0).mean() * 100
                print(f"  {label:>6s}: {count:>4} trades, "
                      f"mean P&L={mean_pnl:.2f} bps (net {mean_pnl_net:.2f}), "
                      f"win rate={win_rate:.1f}%")
            else:
                print(f"  {label:>6s}: 0 trades")

    # --- Step 5: Scan different ATR multipliers ---
    print("\n--- ATR Threshold Scan (k * ATR_15min) ---")
    print(f"{'k':>6} {'Trades':>8} {'Mean PnL':>10} {'Net PnL':>10} {'Win%':>8}")
    print("-" * 50)

    atr_15m_raw = atr_series["15min"]
    for k in [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0]:
        # Dynamic threshold: k * ATR at each tick
        # Simulate CBS with dynamic threshold
        trigger_idxs_k = []
        pnl_k = []
        window_ticks = 1080
        hold_ticks = 540
        state = "idle"
        entry_idx = 0
        entry_mid = 0
        direction = 0
        next_allowed = 0

        # Day boundaries
        day_starts = [0]
        for i in range(1, n):
            if ts_ns[i] - ts_ns[i - 1] > 3_600_000_000_000:
                day_starts.append(i)

        skip_ns = 30 * 60 * 1_000_000_000

        for i in range(window_ticks, n):
            if state == "idle":
                if i < next_allowed:
                    continue

                day_start = day_starts[0]
                for ds in day_starts:
                    if ds <= i:
                        day_start = ds
                    else:
                        break
                if ts_ns[i] - ts_ns[day_start] < skip_ns:
                    continue

                # ATR-adaptive threshold
                current_atr = atr_15m_raw[i]
                if current_atr < 1.0:
                    continue
                threshold = k * current_atr

                oldest_idx = max(0, i - window_ticks)
                oldest_mid = mid_x2[oldest_idx]
                if oldest_mid <= 0:
                    continue

                diff = int(mid_x2[i]) - int(oldest_mid)
                if abs(diff) >= threshold:
                    direction = -1 if diff > 0 else 1
                    entry_idx = i
                    entry_mid = mid_x2[i]
                    state = "positioned"

            elif state == "positioned":
                elapsed = i - entry_idx

                # ATR-adaptive stop
                stop_threshold = 1.0 * atr_15m_raw[i]  # 1 * ATR stop
                pnl_diff = direction * (int(mid_x2[i]) - int(entry_mid))

                exit_now = False
                if pnl_diff < -stop_threshold:
                    exit_now = True
                elif elapsed >= hold_ticks:
                    exit_now = True

                if exit_now:
                    pnl_bps_val = pnl_diff / entry_mid * 10000.0
                    trigger_idxs_k.append(entry_idx)
                    pnl_k.append(pnl_bps_val)
                    state = "idle"
                    next_allowed = i + 10

        if pnl_k:
            pnl_arr = np.array(pnl_k)
            mean_pnl = pnl_arr.mean()
            net_pnl = mean_pnl - 1.33
            win_pct = (pnl_arr > 0).mean() * 100
            print(f"{k:>6.1f} {len(pnl_k):>8} {mean_pnl:>10.2f} {net_pnl:>10.2f} {win_pct:>8.1f}")
        else:
            print(f"{k:>6.1f} {0:>8} {'N/A':>10} {'N/A':>10} {'N/A':>8}")

    print("\n" + "=" * 70)
    print("Exploration complete.")
    print("=" * 70)


if __name__ == "__main__":
    run_exploration()
