"""VRM Walk-Forward Validation + Parameter Sweep + Diagnostics.

Tasks:
1. Walk-forward: IS (first 30 days) → calibrate, OOS (last 15 days) → validate
2. Parameter robustness sweep (heatmap)
3. Detailed diagnostics (daily PnL, drawdown, long/short, time-of-day)
4. CBS orthogonality check

Usage:
    CLICKHOUSE_PASSWORD=changeme python -m research.alphas.vr_momentum.validate
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass

import numpy as np

from research.alphas.vr_momentum.impl import VRMomentum


def _get_ch_client():
    from clickhouse_driver import Client
    return Client(
        host=os.environ.get("HFT_CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("HFT_CLICKHOUSE_NATIVE_PORT", "9000")),
        user="default",
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
    )


def load_data() -> tuple[np.ndarray, np.ndarray]:
    """Load TMFD6 mid_x2 and timestamps."""
    client = _get_ch_client()
    print("Loading TMFD6 data...")
    rows = client.execute("""
        SELECT exch_ts, toInt64(bids_price[1] + asks_price[1]) as mid_x2
        FROM hft.market_data
        WHERE symbol = 'TMFD6'
          AND length(bids_price) > 0 AND length(asks_price) > 0
          AND bids_price[1] > 0 AND asks_price[1] > 0
        ORDER BY exch_ts
    """)
    print(f"  Loaded {len(rows):,} rows")
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    mid = np.array([r[1] for r in rows], dtype=np.int64)
    return ts, mid


def find_day_boundaries(ts_ns: np.ndarray) -> list[int]:
    starts = [0]
    for i in range(1, len(ts_ns)):
        if ts_ns[i] - ts_ns[i - 1] > 3_600_000_000_000:
            starts.append(i)
    return starts


@dataclass
class Trade:
    entry_idx: int
    exit_idx: int
    direction: int  # +1 long, -1 short
    entry_mid: int
    exit_mid: int
    pnl_bps: float
    exit_reason: str  # "time" or "stop"
    day_idx: int


def simulate_vrm(
    mid_x2: np.ndarray,
    ts_ns: np.ndarray,
    day_starts: list[int],
    vr_q: int = 540,
    vr_threshold: float = 1.2,
    z_threshold: float = 2.0,
    push_lag: int = 1080,
    hold_ticks: int = 3000,
    stop_bps: float = 20.0,
    cooldown_ticks: int = 3000,
    warmup_ticks: int = 7000,
    skip_opening_ns: int = 30 * 60 * 1_000_000_000,
    start_idx: int = 0,
    end_idx: int | None = None,
) -> list[Trade]:
    """Simulate VRM strategy with proper non-overlapping entries."""
    if end_idx is None:
        end_idx = len(mid_x2)

    vrm = VRMomentum(
        vr_q=vr_q, vr_threshold=vr_threshold, z_threshold=z_threshold,
        push_lag=push_lag, hold_ticks=hold_ticks, stop_bps=stop_bps,
        cooldown_ticks=cooldown_ticks, warmup_ticks=warmup_ticks,
    )

    trades: list[Trade] = []
    state = "idle"
    entry_idx = 0
    entry_mid = 0
    direction = 0
    next_allowed = 0
    current_day = 0

    for i in range(start_idx, end_idx):
        mid = mid_x2[i]
        if mid <= 0:
            continue

        # Track day
        while current_day + 1 < len(day_starts) and i >= day_starts[current_day + 1]:
            current_day += 1
            # Reset VRM at day boundary
            vrm.reset()
            if state == "positioned":
                # Force exit at day end
                pnl_diff = direction * (int(mid) - int(entry_mid))
                pnl_bps = pnl_diff / entry_mid * 10000.0
                trades.append(Trade(
                    entry_idx=entry_idx, exit_idx=i, direction=direction,
                    entry_mid=entry_mid, exit_mid=mid,
                    pnl_bps=pnl_bps, exit_reason="day_end", day_idx=current_day - 1,
                ))
                state = "idle"
                next_allowed = i + cooldown_ticks

        result = vrm.update(mid)

        if state == "positioned":
            elapsed = i - entry_idx

            # Stop-loss check
            pnl_diff = direction * (int(mid) - int(entry_mid))
            pnl_bps_curr = pnl_diff / entry_mid * 10000.0

            exit_reason = None
            if pnl_bps_curr <= -stop_bps:
                exit_reason = "stop"
            elif elapsed >= hold_ticks:
                exit_reason = "time"

            if exit_reason:
                trades.append(Trade(
                    entry_idx=entry_idx, exit_idx=i, direction=direction,
                    entry_mid=entry_mid, exit_mid=mid,
                    pnl_bps=pnl_bps_curr, exit_reason=exit_reason, day_idx=current_day,
                ))
                state = "idle"
                next_allowed = i + cooldown_ticks

        elif state == "idle":
            if i < next_allowed:
                continue

            # Session gate: skip opening
            day_start = day_starts[current_day]
            if ts_ns[i] - ts_ns[day_start] < skip_opening_ns:
                continue

            signal = result["signal"]
            if signal != 0:
                state = "positioned"
                entry_idx = i
                entry_mid = mid
                direction = signal

    return trades


def print_metrics(trades: list[Trade], label: str, cost_bps: float = 1.33) -> dict:
    """Print and return summary metrics."""
    if not trades:
        print(f"\n--- {label}: 0 trades ---")
        return {}

    pnl = np.array([t.pnl_bps for t in trades])
    net_pnl = pnl - cost_bps
    n = len(pnl)

    cum_net = np.cumsum(net_pnl)
    max_dd = 0.0
    peak = 0.0
    for v in cum_net:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd

    metrics = {
        "trades": n,
        "mean_pnl_gross": float(pnl.mean()),
        "mean_pnl_net": float(net_pnl.mean()),
        "total_net": float(net_pnl.sum()),
        "win_rate": float((net_pnl > 0).mean()),
        "std_pnl": float(pnl.std()),
        "max_dd_bps": float(max_dd),
        "sharpe_per_trade": float(net_pnl.mean() / net_pnl.std()) if net_pnl.std() > 0 else 0.0,
    }

    print(f"\n--- {label} ---")
    print(f"  Trades: {n}")
    print(f"  Mean P&L gross: {metrics['mean_pnl_gross']:.2f} bps")
    print(f"  Mean P&L net:   {metrics['mean_pnl_net']:.2f} bps")
    print(f"  Total net:      {metrics['total_net']:.1f} bps")
    print(f"  Win rate:       {metrics['win_rate']*100:.1f}%")
    print(f"  Std:            {metrics['std_pnl']:.2f} bps")
    print(f"  Max DD:         {metrics['max_dd_bps']:.1f} bps")
    print(f"  Sharpe/trade:   {metrics['sharpe_per_trade']:.3f}")

    return metrics


def run_validation() -> None:
    """Main validation pipeline."""
    print("=" * 70)
    print("VRM Walk-Forward Validation")
    print("=" * 70)

    ts_ns, mid_x2 = load_data()
    day_starts = find_day_boundaries(ts_ns)
    n_days = len(day_starts)
    print(f"  {n_days} trading days")

    # --- Task 1: Walk-forward ---
    is_days = int(n_days * 2 / 3)  # ~30 days
    oos_start_day = is_days
    is_end_idx = day_starts[oos_start_day] if oos_start_day < n_days else len(mid_x2)
    oos_start_idx = is_end_idx

    print(f"\n  IS: days 0-{is_days-1} ({is_days} days), ticks 0-{is_end_idx:,}")
    print(f"  OOS: days {oos_start_day}-{n_days-1} ({n_days - oos_start_day} days), ticks {oos_start_idx:,}-{len(mid_x2):,}")

    # IS with base params
    is_trades = simulate_vrm(
        mid_x2, ts_ns, day_starts,
        vr_threshold=1.2, z_threshold=2.0, hold_ticks=3000, push_lag=1080,
        end_idx=is_end_idx,
    )
    is_metrics = print_metrics(is_trades, "IS (base params: VR>1.2, z>2.0, hold=3000)")

    # OOS with same params
    oos_trades = simulate_vrm(
        mid_x2, ts_ns, day_starts,
        vr_threshold=1.2, z_threshold=2.0, hold_ticks=3000, push_lag=1080,
        start_idx=oos_start_idx,
    )
    oos_metrics = print_metrics(oos_trades, "OOS (base params: VR>1.2, z>2.0, hold=3000)")

    # Full period
    full_trades = simulate_vrm(
        mid_x2, ts_ns, day_starts,
        vr_threshold=1.2, z_threshold=2.0, hold_ticks=3000, push_lag=1080,
    )
    full_metrics = print_metrics(full_trades, "FULL (base params)")

    # --- Task 2: Parameter sweep ---
    print("\n" + "=" * 70)
    print("Parameter Robustness Sweep")
    print("=" * 70)

    # VR threshold x Z threshold sweep
    print("\n--- VR Threshold x Z Threshold (hold=3000, push_lag=1080) ---")
    print(f"{'VR':>6} {'Z':>6} {'Trades':>8} {'Gross':>8} {'Net':>8} {'Win%':>6} {'ShpT':>7}")
    print("-" * 55)

    vr_vals = [1.05, 1.1, 1.15, 1.2, 1.25, 1.3, 1.4]
    z_vals = [1.5, 1.75, 2.0, 2.25, 2.5, 3.0]

    best_net = -999.0
    best_params = {}

    for vr_t in vr_vals:
        for z_t in z_vals:
            trades = simulate_vrm(
                mid_x2, ts_ns, day_starts,
                vr_threshold=vr_t, z_threshold=z_t,
                hold_ticks=3000, push_lag=1080,
            )
            if not trades:
                print(f"{vr_t:>6.2f} {z_t:>6.2f} {0:>8} {'N/A':>8} {'N/A':>8} {'N/A':>6} {'N/A':>7}")
                continue

            pnl = np.array([t.pnl_bps for t in trades])
            net = pnl - 1.33
            mean_net = float(net.mean())
            win_pct = float((net > 0).mean()) * 100
            shp = float(net.mean() / net.std()) if net.std() > 0 else 0.0

            if mean_net > best_net and len(trades) >= 20:
                best_net = mean_net
                best_params = {"vr": vr_t, "z": z_t, "trades": len(trades)}

            print(f"{vr_t:>6.2f} {z_t:>6.2f} {len(trades):>8} "
                  f"{pnl.mean():>8.2f} {mean_net:>8.2f} {win_pct:>6.1f} {shp:>7.3f}")

    print(f"\nBest: VR>{best_params.get('vr','?')}, z>{best_params.get('z','?')}, "
          f"net={best_net:.2f} bps/trade, {best_params.get('trades','?')} trades")

    # Hold sweep
    print("\n--- Hold Period Sweep (VR>1.2, z>2.0) ---")
    print(f"{'Hold':>8} {'Trades':>8} {'Gross':>8} {'Net':>8} {'Win%':>6}")
    print("-" * 45)

    for hold in [540, 1080, 2000, 3000, 4000, 5000]:
        trades = simulate_vrm(
            mid_x2, ts_ns, day_starts,
            vr_threshold=1.2, z_threshold=2.0,
            hold_ticks=hold, push_lag=1080,
        )
        if trades:
            pnl = np.array([t.pnl_bps for t in trades])
            net = pnl - 1.33
            print(f"{hold:>8} {len(trades):>8} {pnl.mean():>8.2f} {net.mean():>8.2f} "
                  f"{(net > 0).mean()*100:>6.1f}")

    # Push lag sweep
    print("\n--- Push Lag Sweep (VR>1.2, z>2.0, hold=3000) ---")
    print(f"{'Lag':>8} {'Trades':>8} {'Gross':>8} {'Net':>8} {'Win%':>6}")
    print("-" * 45)

    for lag in [540, 1080, 2000, 3000]:
        trades = simulate_vrm(
            mid_x2, ts_ns, day_starts,
            vr_threshold=1.2, z_threshold=2.0,
            hold_ticks=3000, push_lag=lag,
        )
        if trades:
            pnl = np.array([t.pnl_bps for t in trades])
            net = pnl - 1.33
            print(f"{lag:>8} {len(trades):>8} {pnl.mean():>8.2f} {net.mean():>8.2f} "
                  f"{(net > 0).mean()*100:>6.1f}")

    # --- Task 3: Diagnostics ---
    print("\n" + "=" * 70)
    print("Diagnostics (base params: VR>1.2, z>2.0, hold=3000)")
    print("=" * 70)

    if full_trades:
        pnl = np.array([t.pnl_bps for t in full_trades])
        net = pnl - 1.33
        dirs = np.array([t.direction for t in full_trades])
        day_idxs = np.array([t.day_idx for t in full_trades])

        # Long vs Short
        long_mask = dirs == 1
        short_mask = dirs == -1
        print("\n--- Long vs Short ---")
        if long_mask.sum() > 0:
            print(f"  Long:  {long_mask.sum()} trades, net {net[long_mask].mean():.2f} bps, "
                  f"win {(net[long_mask] > 0).mean()*100:.1f}%")
        if short_mask.sum() > 0:
            print(f"  Short: {short_mask.sum()} trades, net {net[short_mask].mean():.2f} bps, "
                  f"win {(net[short_mask] > 0).mean()*100:.1f}%")

        # Exit reasons
        print("\n--- Exit Reasons ---")
        for reason in ["time", "stop", "day_end"]:
            mask = np.array([t.exit_reason == reason for t in full_trades])
            if mask.sum() > 0:
                print(f"  {reason:>8}: {mask.sum()} trades, net {net[mask].mean():.2f} bps")

        # Daily P&L
        print("\n--- Daily P&L ---")
        print(f"{'Day':>5} {'Trades':>8} {'Net bps':>10} {'Cum':>10}")
        cum = 0.0
        profitable_days = 0
        for d in range(n_days):
            mask = day_idxs == d
            n_t = mask.sum()
            if n_t > 0:
                day_net = net[mask].sum()
                cum += day_net
                if day_net > 0:
                    profitable_days += 1
                print(f"{d:>5} {n_t:>8} {day_net:>10.2f} {cum:>10.2f}")

        days_with_trades = len(set(day_idxs))
        print(f"\n  Days with trades: {days_with_trades}/{n_days}")
        print(f"  Profitable days: {profitable_days}/{days_with_trades} "
              f"({profitable_days/max(1,days_with_trades)*100:.0f}%)")

        # Cumulative P&L and max drawdown
        cum_net = np.cumsum(net)
        peak = np.maximum.accumulate(cum_net)
        drawdown = peak - cum_net
        max_dd = drawdown.max()
        print(f"\n  Total cumulative net: {cum_net[-1]:.1f} bps")
        print(f"  Max drawdown: {max_dd:.1f} bps")

    # --- Task 4: CBS Orthogonality ---
    print("\n" + "=" * 70)
    print("CBS Orthogonality Check")
    print("=" * 70)

    # Simulate CBS
    cbs_trades = _simulate_cbs(mid_x2, ts_ns, day_starts)
    if cbs_trades and full_trades:
        cbs_entry_set = set(t.entry_idx for t in cbs_trades)
        vrm_entry_set = set(t.entry_idx for t in full_trades)

        # Check overlap within 1000 ticks
        overlapping = 0
        same_dir = 0
        opp_dir = 0
        for vt in full_trades:
            for ct in cbs_trades:
                if abs(vt.entry_idx - ct.entry_idx) < 1000:
                    overlapping += 1
                    if vt.direction == ct.direction:
                        same_dir += 1
                    else:
                        opp_dir += 1
                    break

        print(f"  CBS trades: {len(cbs_trades)}")
        print(f"  VRM trades: {len(full_trades)}")
        print(f"  Overlapping (within 1000 ticks): {overlapping}")
        print(f"    Same direction: {same_dir}")
        print(f"    Opposite direction: {opp_dir}")

        # CBS performance
        cbs_pnl = np.array([t.pnl_bps for t in cbs_trades])
        cbs_net = cbs_pnl - 1.33
        print(f"  CBS net: {cbs_net.mean():.2f} bps/trade ({len(cbs_trades)} trades)")
    else:
        print("  Insufficient data for comparison")

    print("\n" + "=" * 70)
    print("Validation complete.")
    print("=" * 70)


def _simulate_cbs(
    mid_x2: np.ndarray,
    ts_ns: np.ndarray,
    day_starts: list[int],
    threshold_bps: float = 40.0,
    hold_ticks: int = 540,
    stop_bps: float = 15.0,
) -> list[Trade]:
    """Simulate CBS for comparison."""
    n = len(mid_x2)
    window_ticks = 1080
    trades = []
    state = "idle"
    entry_idx = 0
    entry_mid = 0
    direction = 0
    next_allowed = 0
    current_day = 0
    skip_ns = 30 * 60 * 1_000_000_000

    for i in range(window_ticks, n):
        # Day tracking
        while current_day + 1 < len(day_starts) and i >= day_starts[current_day + 1]:
            current_day += 1
            if state == "positioned":
                pnl_diff = direction * (int(mid_x2[i]) - int(entry_mid))
                trades.append(Trade(
                    entry_idx=entry_idx, exit_idx=i, direction=direction,
                    entry_mid=entry_mid, exit_mid=mid_x2[i],
                    pnl_bps=pnl_diff / entry_mid * 10000.0,
                    exit_reason="day_end", day_idx=current_day - 1,
                ))
                state = "idle"
                next_allowed = i + 10

        if state == "idle":
            if i < next_allowed:
                continue

            day_start = day_starts[current_day]
            if ts_ns[i] - ts_ns[day_start] < skip_ns:
                continue

            oldest_idx = max(0, i - window_ticks)
            oldest_mid = mid_x2[oldest_idx]
            if oldest_mid <= 0:
                continue

            diff = int(mid_x2[i]) - int(oldest_mid)
            move_bps = abs(diff) / oldest_mid * 10000.0

            if move_bps >= threshold_bps:
                direction = -1 if diff > 0 else 1  # contrarian
                entry_idx = i
                entry_mid = mid_x2[i]
                state = "positioned"

        elif state == "positioned":
            elapsed = i - entry_idx
            pnl_diff = direction * (int(mid_x2[i]) - int(entry_mid))
            pnl_bps = pnl_diff / entry_mid * 10000.0

            if pnl_bps <= -stop_bps or elapsed >= hold_ticks:
                trades.append(Trade(
                    entry_idx=entry_idx, exit_idx=i, direction=direction,
                    entry_mid=entry_mid, exit_mid=mid_x2[i],
                    pnl_bps=pnl_bps,
                    exit_reason="stop" if pnl_bps <= -stop_bps else "time",
                    day_idx=current_day,
                ))
                state = "idle"
                next_allowed = i + 10

    return trades


if __name__ == "__main__":
    run_validation()
