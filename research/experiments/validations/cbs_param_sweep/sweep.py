"""
CBS Parameter Neighborhood Robustness Sweep (Direction A)

Walk-forward design:
  IS:  14 days (Jan 26-30, Feb 03-06, Feb 10-11, Feb 23-25, Mar 19-20)
  OOS: 6 days  (Mar 23-26) — recency-weighted, most important

Parameter grid:
  move_threshold_bps: [25, 30, 35, 40, 45, 50, 60]
  detect_window_s:    [300, 450, 600, 900]
  hold_s:             [120, 180, 300, 450, 600]
  stop_loss_bps:      [15, 20, 25, 30, 40, 50, 9999]

Total configs: 7 * 4 * 5 * 7 = 980

RT cost: 4 points (mid-to-mid)
Session gate: 09:15-13:35 TST (UTC+8)
"""

from __future__ import annotations

import itertools
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import stats as scipy_stats

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_DIR = Path("research/data/raw/tmfd6")
RT_COST_PTS = 4.0  # round-trip cost in points
PT_VALUE_NTD = 10.0

# Session gate (seconds of day, local time UTC+8)
SESSION_START_SOD = 9 * 3600 + 15 * 60   # 09:15 = 33300
SESSION_END_SOD = 13 * 3600 + 35 * 60    # 13:35 = 48900
UTC_OFFSET_S = 8 * 3600

# Walk-forward split
IS_DATES = [
    "2026-01-26", "2026-01-27", "2026-01-28", "2026-01-29", "2026-01-30",
    "2026-02-03", "2026-02-04", "2026-02-05", "2026-02-06",
    "2026-02-10", "2026-02-11",
    "2026-02-23", "2026-02-24", "2026-02-25",
    "2026-03-19", "2026-03-20",
]
OOS_DATES = ["2026-03-23", "2026-03-24", "2026-03-25", "2026-03-26"]

# Parameter grid
MOVE_THRESHOLDS = [25, 30, 35, 40, 45, 50, 60]
DETECT_WINDOWS_S = [300, 450, 600, 900]
HOLD_S = [120, 180, 300, 450, 600]
STOP_LOSS_BPS = [15, 20, 25, 30, 40, 50, 9999]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Trade:
    entry_ts: int
    exit_ts: int
    direction: int       # +1 long, -1 short
    entry_mid: float
    exit_mid: float
    pnl_pts: float       # net of RT cost
    exit_reason: str      # "stop_loss" | "time_exit"
    day: str


@dataclass(slots=True)
class ConfigResult:
    move_bps: int
    detect_s: int
    hold_s: int
    stop_bps: int
    # IS metrics
    is_n: int = 0
    is_avg_pnl: float = 0.0
    is_median_pnl: float = 0.0
    is_win_rate: float = 0.0
    is_stop_rate: float = 0.0
    is_tstat: float = 0.0
    is_pval: float = 1.0
    is_max_dd_pts: float = 0.0
    # OOS metrics
    oos_n: int = 0
    oos_avg_pnl: float = 0.0
    oos_median_pnl: float = 0.0
    oos_win_rate: float = 0.0
    oos_stop_rate: float = 0.0
    oos_tstat: float = 0.0
    oos_pval: float = 1.0
    oos_max_dd_pts: float = 0.0
    # daily breakdown (OOS)
    oos_daily_pnl: list[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_day(date_str: str) -> np.ndarray | None:
    """Load a single day .npy file, return None if missing."""
    path = DATA_DIR / f"TMFD6_{date_str}_l1.npy"
    if not path.exists():
        return None
    return np.load(str(path), allow_pickle=True)


def filter_session(data: np.ndarray) -> np.ndarray:
    """Filter to session gate 09:15-13:35 TST."""
    ts_sec = data["local_ts"] / 1e9
    sod = (ts_sec + UTC_OFFSET_S) % 86400
    mask = (sod >= SESSION_START_SOD) & (sod <= SESSION_END_SOD)
    return data[mask]


# ---------------------------------------------------------------------------
# CBS backtest engine (vectorized entry detection, sequential trade mgmt)
# ---------------------------------------------------------------------------
def run_cbs_backtest(
    data: np.ndarray,
    move_threshold_bps: int,
    detect_window_ns: int,
    hold_ns: int,
    stop_loss_bps: int,
    day_label: str,
) -> list[Trade]:
    """Run CBS backtest on a single day's data.

    Logic matches CascadeBounceStrategy exactly:
    1. Maintain price buffer with detect_window lookback
    2. Compute move from oldest to current mid in bps
    3. If |move| >= threshold -> enter contrarian
    4. Exit on stop_loss or hold period elapsed
    5. Non-overlapping: next entry after current hold completes
    """
    mid = data["mid_price"]
    ts = data["local_ts"]
    n = len(data)

    if n < 2:
        return []

    trades: list[Trade] = []

    # State
    state = "idle"  # "idle" | "positioned"
    entry_ts = 0
    entry_mid = 0.0
    direction = 0
    next_allowed_ts = 0

    # Price buffer as deque of (ts_ns, mid)
    buf: deque[tuple[int, float]] = deque()

    for i in range(n):
        t = int(ts[i])
        m = float(mid[i])

        if m <= 0:
            continue

        # Update buffer: expire old entries
        cutoff = t - detect_window_ns
        while buf and buf[0][0] < cutoff:
            buf.popleft()
        buf.append((t, m))

        if state == "positioned":
            # Check exit conditions
            if entry_mid <= 0:
                state = "idle"
                continue

            # Unrealized PnL in bps
            pnl_bps = direction * (m - entry_mid) / entry_mid * 10000
            elapsed = t - entry_ts
            exit_reason = None

            if pnl_bps < -stop_loss_bps:
                exit_reason = "stop_loss"
            if elapsed >= hold_ns:
                exit_reason = "time_exit"

            if exit_reason is not None:
                pnl_pts = direction * (m - entry_mid) - RT_COST_PTS
                trades.append(Trade(
                    entry_ts=entry_ts,
                    exit_ts=t,
                    direction=direction,
                    entry_mid=entry_mid,
                    exit_mid=m,
                    pnl_pts=pnl_pts,
                    exit_reason=exit_reason,
                    day=day_label,
                ))
                state = "idle"
                next_allowed_ts = entry_ts + hold_ns
                direction = 0

        elif state == "idle":
            # Check entry conditions
            if t < next_allowed_ts:
                continue

            if len(buf) < 2:
                continue

            oldest_ts, oldest_mid = buf[0]
            if oldest_mid <= 0:
                continue

            move_bps = (m - oldest_mid) / oldest_mid * 10000
            abs_move = abs(move_bps)

            if abs_move < move_threshold_bps:
                continue

            # Large move detected -> contrarian entry
            direction = -1 if (m - oldest_mid) > 0 else 1

            state = "positioned"
            entry_ts = t
            entry_mid = m

    return trades


def compute_metrics(trades: list[Trade]) -> dict:
    """Compute summary metrics from trade list."""
    n = len(trades)
    if n == 0:
        return {
            "n": 0, "avg_pnl": 0.0, "median_pnl": 0.0,
            "win_rate": 0.0, "stop_rate": 0.0,
            "tstat": 0.0, "pval": 1.0, "max_dd_pts": 0.0,
        }

    pnls = np.array([t.pnl_pts for t in trades])
    stops = sum(1 for t in trades if t.exit_reason == "stop_loss")

    avg = float(pnls.mean())
    med = float(np.median(pnls))
    wr = float((pnls > 0).sum() / n)
    sr = stops / n

    if n >= 2 and pnls.std() > 0:
        t_stat, p_val = scipy_stats.ttest_1samp(pnls, 0)
        t_stat = float(t_stat)
        p_val = float(p_val)
    else:
        t_stat, p_val = 0.0, 1.0

    # Max drawdown (cumulative PnL)
    cum = np.cumsum(pnls)
    running_max = np.maximum.accumulate(cum)
    dd = running_max - cum
    max_dd = float(dd.max()) if len(dd) > 0 else 0.0

    return {
        "n": n, "avg_pnl": avg, "median_pnl": med,
        "win_rate": wr, "stop_rate": sr,
        "tstat": t_stat, "pval": p_val, "max_dd_pts": max_dd,
    }


def daily_pnl_breakdown(trades: list[Trade], dates: list[str]) -> list[float]:
    """Per-day total PnL in points."""
    result = []
    for d in dates:
        day_pnl = sum(t.pnl_pts for t in trades if t.day == d)
        result.append(day_pnl)
    return result


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------
def main() -> None:
    print("=" * 100)
    print("CBS PARAMETER NEIGHBORHOOD ROBUSTNESS SWEEP")
    print("=" * 100)

    # Load all data
    print("\n--- Loading Data ---")
    is_data: dict[str, np.ndarray] = {}
    oos_data: dict[str, np.ndarray] = {}

    for d in IS_DATES:
        arr = load_day(d)
        if arr is not None:
            filtered = filter_session(arr)
            if len(filtered) > 0:
                is_data[d] = filtered
                print(f"  IS  {d}: {len(filtered):>9,} rows")

    for d in OOS_DATES:
        arr = load_day(d)
        if arr is not None:
            filtered = filter_session(arr)
            if len(filtered) > 0:
                oos_data[d] = filtered
                print(f"  OOS {d}: {len(filtered):>9,} rows")

    print(f"\nIS days: {len(is_data)}, OOS days: {len(oos_data)}")

    # Generate parameter grid
    grid = list(itertools.product(
        MOVE_THRESHOLDS, DETECT_WINDOWS_S, HOLD_S, STOP_LOSS_BPS
    ))
    total_configs = len(grid)
    print(f"Total configs: {total_configs}")

    # Run sweep
    print("\n--- Running Sweep ---")
    results: list[ConfigResult] = []
    t0 = time.time()

    for idx, (move_bps, detect_s, hold_s, stop_bps) in enumerate(grid):
        detect_ns = detect_s * 1_000_000_000
        hold_ns = hold_s * 1_000_000_000

        # IS trades
        is_trades: list[Trade] = []
        for d, data in is_data.items():
            is_trades.extend(run_cbs_backtest(
                data, move_bps, detect_ns, hold_ns, stop_bps, d))

        # OOS trades
        oos_trades: list[Trade] = []
        for d, data in oos_data.items():
            oos_trades.extend(run_cbs_backtest(
                data, move_bps, detect_ns, hold_ns, stop_bps, d))

        is_m = compute_metrics(is_trades)
        oos_m = compute_metrics(oos_trades)

        cr = ConfigResult(
            move_bps=move_bps, detect_s=detect_s,
            hold_s=hold_s, stop_bps=stop_bps,
            is_n=is_m["n"], is_avg_pnl=is_m["avg_pnl"],
            is_median_pnl=is_m["median_pnl"],
            is_win_rate=is_m["win_rate"], is_stop_rate=is_m["stop_rate"],
            is_tstat=is_m["tstat"], is_pval=is_m["pval"],
            is_max_dd_pts=is_m["max_dd_pts"],
            oos_n=oos_m["n"], oos_avg_pnl=oos_m["avg_pnl"],
            oos_median_pnl=oos_m["median_pnl"],
            oos_win_rate=oos_m["win_rate"], oos_stop_rate=oos_m["stop_rate"],
            oos_tstat=oos_m["tstat"], oos_pval=oos_m["pval"],
            oos_max_dd_pts=oos_m["max_dd_pts"],
            oos_daily_pnl=daily_pnl_breakdown(oos_trades, list(oos_data.keys())),
        )
        results.append(cr)

        if (idx + 1) % 100 == 0:
            elapsed = time.time() - t0
            rate = (idx + 1) / elapsed
            eta = (total_configs - idx - 1) / rate
            print(f"  {idx+1}/{total_configs} done ({rate:.0f} cfg/s, ETA {eta:.0f}s)")

    elapsed = time.time() - t0
    print(f"\nSweep completed in {elapsed:.1f}s ({total_configs / elapsed:.0f} cfg/s)")

    # -----------------------------------------------------------------------
    # Results analysis
    # -----------------------------------------------------------------------
    print("\n" + "=" * 100)
    print("RESULTS")
    print("=" * 100)

    # Sort by OOS avg PnL descending
    results_sorted = sorted(results, key=lambda r: r.oos_avg_pnl, reverse=True)

    # --- 1. Top 20 by OOS avg PnL ---
    print("\n--- Top 20 Configs by OOS Avg PnL ---")
    print(f"{'#':>3} {'Move':>5} {'DetW':>5} {'Hold':>5} {'Stop':>5} | "
          f"{'IS_N':>5} {'IS_Avg':>7} {'IS_WR':>6} {'IS_SR':>6} {'IS_t':>6} {'IS_p':>6} | "
          f"{'OOS_N':>5} {'OOS_Avg':>8} {'OOS_Med':>8} {'OOS_WR':>6} {'OOS_SR':>6} {'OOS_t':>6} {'OOS_p':>6} {'OOS_DD':>7}")
    print("-" * 140)

    for i, r in enumerate(results_sorted[:20]):
        stop_label = "none" if r.stop_bps >= 9000 else str(r.stop_bps)
        print(f"{i+1:>3} {r.move_bps:>5} {r.detect_s:>5} {r.hold_s:>5} {stop_label:>5} | "
              f"{r.is_n:>5} {r.is_avg_pnl:>+7.2f} {r.is_win_rate:>5.0%} {r.is_stop_rate:>5.0%} "
              f"{r.is_tstat:>+6.2f} {r.is_pval:>6.3f} | "
              f"{r.oos_n:>5} {r.oos_avg_pnl:>+8.2f} {r.oos_median_pnl:>+8.2f} "
              f"{r.oos_win_rate:>5.0%} {r.oos_stop_rate:>5.0%} "
              f"{r.oos_tstat:>+6.2f} {r.oos_pval:>6.3f} {r.oos_max_dd_pts:>7.1f}")

    # --- 2. OOS daily breakdown for top 5 ---
    print("\n--- OOS Daily PnL Breakdown (Top 5) ---")
    oos_date_list = sorted(oos_data.keys())
    header = f"{'#':>3} {'Config':>25} | " + " | ".join(f"{d[-5:]}" for d in oos_date_list) + " | Total"
    print(header)
    print("-" * len(header))

    for i, r in enumerate(results_sorted[:5]):
        stop_label = "none" if r.stop_bps >= 9000 else str(r.stop_bps)
        config_str = f"{r.move_bps}/{r.detect_s}/{r.hold_s}/{stop_label}"
        daily_str = " | ".join(f"{p:>+5.1f}" for p in r.oos_daily_pnl)
        total = sum(r.oos_daily_pnl)
        print(f"{i+1:>3} {config_str:>25} | {daily_str} | {total:>+6.1f}")

    # --- 3. Stop rate analysis ---
    print("\n--- Stop Rate Analysis (by stop_loss_bps) ---")
    print(f"{'Stop':>6} | {'OOS_N':>6} {'OOS_Avg':>8} {'OOS_SR':>7} {'OOS_WR':>7} | "
          f"{'IS_N':>6} {'IS_Avg':>8} {'IS_SR':>7}")
    print("-" * 80)

    for sl in STOP_LOSS_BPS:
        sl_configs = [r for r in results if r.stop_bps == sl]
        # Average across all configs with this stop level
        oos_ns = [r.oos_n for r in sl_configs if r.oos_n > 0]
        oos_avgs = [r.oos_avg_pnl for r in sl_configs if r.oos_n > 0]
        oos_srs = [r.oos_stop_rate for r in sl_configs if r.oos_n > 0]
        oos_wrs = [r.oos_win_rate for r in sl_configs if r.oos_n > 0]
        is_ns = [r.is_n for r in sl_configs if r.is_n > 0]
        is_avgs = [r.is_avg_pnl for r in sl_configs if r.is_n > 0]
        is_srs = [r.is_stop_rate for r in sl_configs if r.is_n > 0]

        sl_label = "none" if sl >= 9000 else str(sl)
        if oos_avgs:
            print(f"{sl_label:>6} | {np.mean(oos_ns):>6.0f} {np.mean(oos_avgs):>+8.2f} "
                  f"{np.mean(oos_srs):>6.0%} {np.mean(oos_wrs):>6.0%} | "
                  f"{np.mean(is_ns):>6.0f} {np.mean(is_avgs):>+8.2f} {np.mean(is_srs):>6.0%}")
        else:
            print(f"{sl_label:>6} | {'N/A':>6}")

    # --- 4. Neighborhood robustness for top 5 OOS configs ---
    print("\n--- Neighborhood Robustness (Top 5 OOS configs) ---")
    print("For each top config, check if +-1 grid step neighbors are also positive OOS\n")

    # Build lookup dict for fast neighbor lookup
    result_lookup: dict[tuple[int, int, int, int], ConfigResult] = {}
    for r in results:
        result_lookup[(r.move_bps, r.detect_s, r.hold_s, r.stop_bps)] = r

    for rank, r in enumerate(results_sorted[:5]):
        stop_label = "none" if r.stop_bps >= 9000 else str(r.stop_bps)
        print(f"  Rank {rank+1}: move={r.move_bps}, detect={r.detect_s}s, "
              f"hold={r.hold_s}s, stop={stop_label}")
        print(f"  Center OOS: avg={r.oos_avg_pnl:+.2f} pts, N={r.oos_n}, "
              f"WR={r.oos_win_rate:.0%}, SR={r.oos_stop_rate:.0%}")

        # Check each dimension
        dims = [
            ("move_bps", MOVE_THRESHOLDS, r.move_bps),
            ("detect_s", DETECT_WINDOWS_S, r.detect_s),
            ("hold_s", HOLD_S, r.hold_s),
            ("stop_bps", STOP_LOSS_BPS, r.stop_bps),
        ]

        neighbor_positive = 0
        neighbor_total = 0

        for dim_name, dim_values, center_val in dims:
            idx_in_grid = dim_values.index(center_val)
            neighbors = []

            for offset in [-1, 1]:
                ni = idx_in_grid + offset
                if 0 <= ni < len(dim_values):
                    nval = dim_values[ni]
                    # Build neighbor key
                    key = list((r.move_bps, r.detect_s, r.hold_s, r.stop_bps))
                    dim_idx = ["move_bps", "detect_s", "hold_s", "stop_bps"].index(dim_name)
                    key[dim_idx] = nval
                    key_tuple = tuple(key)
                    nr = result_lookup.get(key_tuple)
                    if nr is not None:
                        neighbors.append((nval, nr.oos_avg_pnl, nr.oos_n))
                        neighbor_total += 1
                        if nr.oos_avg_pnl > 0:
                            neighbor_positive += 1

            if neighbors:
                neighbor_strs = [
                    f"{v}={pnl:+.2f}(N={n})" for v, pnl, n in neighbors
                ]
                print(f"    {dim_name}: {' | '.join(neighbor_strs)}")

        if neighbor_total > 0:
            robustness = neighbor_positive / neighbor_total
            print(f"  -> Neighborhood robustness: {neighbor_positive}/{neighbor_total} "
                  f"= {robustness:.0%} positive")
        print()

    # --- 5. Heat maps (move_bps x hold_s, marginalizing over detect/stop) ---
    print("\n--- Heat Map: OOS Avg PnL by (move_bps x hold_s) [marginalized] ---")
    print(f"{'':>6}", end="")
    for h in HOLD_S:
        print(f"  h={h:>4}", end="")
    print()

    for m in MOVE_THRESHOLDS:
        print(f"m={m:>3}", end="")
        for h in HOLD_S:
            vals = [r.oos_avg_pnl for r in results
                    if r.move_bps == m and r.hold_s == h and r.oos_n > 0]
            if vals:
                avg = np.mean(vals)
                marker = "+" if avg > 0 else "-"
                print(f" {avg:>+6.1f}{marker}", end="")
            else:
                print(f"   {'N/A':>5}", end="")
        print()

    print("\n--- Heat Map: OOS Avg PnL by (move_bps x stop_bps) [marginalized] ---")
    print(f"{'':>6}", end="")
    for s in STOP_LOSS_BPS:
        sl = "none" if s >= 9000 else str(s)
        print(f" s={sl:>5}", end="")
    print()

    for m in MOVE_THRESHOLDS:
        print(f"m={m:>3}", end="")
        for s in STOP_LOSS_BPS:
            vals = [r.oos_avg_pnl for r in results
                    if r.move_bps == m and r.stop_bps == s and r.oos_n > 0]
            if vals:
                avg = np.mean(vals)
                print(f" {avg:>+7.1f}", end="")
            else:
                print(f"  {'N/A':>6}", end="")
        print()

    # --- 6. Overall statistics ---
    print("\n--- Overall Statistics ---")
    oos_positive = sum(1 for r in results if r.oos_avg_pnl > 0 and r.oos_n > 0)
    oos_total = sum(1 for r in results if r.oos_n > 0)
    oos_sig = sum(1 for r in results if r.oos_avg_pnl > 0 and r.oos_pval < 0.05 and r.oos_n > 0)
    print(f"Configs with OOS trades: {oos_total}/{total_configs}")
    print(f"Configs with OOS avg PnL > 0: {oos_positive}/{oos_total} ({100*oos_positive/max(oos_total,1):.1f}%)")
    print(f"Configs with OOS avg PnL > 0 AND p < 0.05: {oos_sig}/{oos_total}")

    is_positive = sum(1 for r in results if r.is_avg_pnl > 0 and r.is_n > 0)
    is_total = sum(1 for r in results if r.is_n > 0)
    print(f"Configs with IS avg PnL > 0: {is_positive}/{is_total} ({100*is_positive/max(is_total,1):.1f}%)")

    # Both IS and OOS positive
    both = sum(1 for r in results if r.is_avg_pnl > 0 and r.oos_avg_pnl > 0
               and r.is_n > 0 and r.oos_n > 0)
    print(f"Configs positive in BOTH IS and OOS: {both}/{oos_total} ({100*both/max(oos_total,1):.1f}%)")

    # --- 7. Best robust config (positive OOS + positive neighbors) ---
    print("\n--- Robust Config Recommendation ---")
    best_robust = None
    best_robust_score = -999.0

    for r in results_sorted:
        if r.oos_n < 5:
            continue
        if r.oos_avg_pnl <= 0:
            continue

        # Count positive neighbors
        pos_neighbors = 0
        total_neighbors = 0
        for dim_name, dim_values, center_val in [
            ("move_bps", MOVE_THRESHOLDS, r.move_bps),
            ("detect_s", DETECT_WINDOWS_S, r.detect_s),
            ("hold_s", HOLD_S, r.hold_s),
            ("stop_bps", STOP_LOSS_BPS, r.stop_bps),
        ]:
            idx_in_grid = dim_values.index(center_val)
            for offset in [-1, 1]:
                ni = idx_in_grid + offset
                if 0 <= ni < len(dim_values):
                    nval = dim_values[ni]
                    key = list((r.move_bps, r.detect_s, r.hold_s, r.stop_bps))
                    dim_idx = ["move_bps", "detect_s", "hold_s", "stop_bps"].index(dim_name)
                    key[dim_idx] = nval
                    nr = result_lookup.get(tuple(key))
                    if nr is not None and nr.oos_n > 0:
                        total_neighbors += 1
                        if nr.oos_avg_pnl > 0:
                            pos_neighbors += 1

        robustness = pos_neighbors / max(total_neighbors, 1)
        # Score: OOS avg * robustness, penalize low N
        score = r.oos_avg_pnl * robustness * min(r.oos_n / 10, 1.0)

        if score > best_robust_score:
            best_robust_score = score
            best_robust = (r, robustness, pos_neighbors, total_neighbors)

    if best_robust is not None:
        r, rob, pn, tn = best_robust
        stop_label = "none" if r.stop_bps >= 9000 else str(r.stop_bps)
        print(f"  Config: move={r.move_bps}, detect={r.detect_s}s, "
              f"hold={r.hold_s}s, stop={stop_label}")
        print(f"  OOS: avg={r.oos_avg_pnl:+.2f} pts, N={r.oos_n}, "
              f"WR={r.oos_win_rate:.0%}, SR={r.oos_stop_rate:.0%}, "
              f"t={r.oos_tstat:+.2f}, p={r.oos_pval:.3f}")
        print(f"  IS:  avg={r.is_avg_pnl:+.2f} pts, N={r.is_n}, "
              f"WR={r.is_win_rate:.0%}, SR={r.is_stop_rate:.0%}")
        print(f"  Neighborhood: {pn}/{tn} = {rob:.0%} positive")
        print(f"  Daily OOS PnL: {r.oos_daily_pnl}")
    else:
        print("  NO ROBUST CONFIG FOUND — all configs negative on OOS")

    # --- 8. Bottom 10 (worst configs) for contrast ---
    print("\n--- Bottom 10 Configs (worst OOS) ---")
    for i, r in enumerate(results_sorted[-10:]):
        stop_label = "none" if r.stop_bps >= 9000 else str(r.stop_bps)
        print(f"  {r.move_bps}/{r.detect_s}/{r.hold_s}/{stop_label}: "
              f"OOS avg={r.oos_avg_pnl:+.2f}, N={r.oos_n}, SR={r.oos_stop_rate:.0%}")


if __name__ == "__main__":
    main()
