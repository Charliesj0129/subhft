"""
SG-LP Backtest — March 2026 TMFD6

Walks through L1 quote data, simulates spread-gated passive quoting,
and measures P&L from post-fill mid-price movement.

Usage:
    python research/alphas/spread_gated_lp/backtest.py
"""

from __future__ import annotations

import sys
import os
from pathlib import Path
from dataclasses import dataclass, field
from collections import defaultdict

import numpy as np

# Add project root and local dir to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

from impl import SGLPStrategy, Side

# TMFD6 economics
FEE_PER_LEG_PTS = 2.0   # 20 NTD / 10 NTD per pt
PT_VALUE_NTD = 10.0      # 1 pt = 10 NTD

# Horizons for post-fill measurement (nanoseconds)
HORIZON_1S_NS = 1_000_000_000
HORIZON_5S_NS = 5_000_000_000

# Data files
DATA_DIR = Path("research/data/raw/tmfd6")
IS_DATES = ["2026-03-19", "2026-03-20", "2026-03-23"]  # 3 days IS
OOS_DATES = ["2026-03-24", "2026-03-25", "2026-03-26"]  # 3 days OOS


def load_day(date_str: str) -> np.ndarray:
    """Load a single day's L1 data."""
    fname = f"TMFD6_{date_str}_l1.npy"
    path = DATA_DIR / fname
    if not path.exists():
        raise FileNotFoundError(f"Missing: {path}")
    return np.load(str(path), allow_pickle=True)


def load_period(dates: list[str]) -> np.ndarray:
    """Load and concatenate multiple days."""
    arrays = []
    for d in dates:
        try:
            arr = load_day(d)
            arrays.append(arr)
        except FileNotFoundError:
            print(f"  WARNING: Missing {d}, skipping")
    if not arrays:
        raise RuntimeError("No data loaded")
    return np.concatenate(arrays)


@dataclass(slots=True)
class FillWithPnL:
    """A fill with post-fill P&L computed."""
    fill_ts: int
    side_str: str       # 'BUY' or 'SELL'
    fill_px: float
    spread_at_fill: float
    mid_at_fill: float
    mid_1s: float | None
    mid_5s: float | None
    pnl_1s_pts: float   # mid change in maker's favor
    pnl_5s_pts: float
    gross_capture_pts: float  # half spread
    net_pnl_pts: float  # gross - adverse - fee


def run_backtest(
    data: np.ndarray,
    spread_gate: int = 5,
    obi_threshold: float = 0.0,
    max_position: int = 1,
) -> list[FillWithPnL]:
    """Run SG-LP backtest on L1 data.

    Returns list of fills with P&L.
    """
    strategy = SGLPStrategy(
        spread_gate_pts=spread_gate,
        obi_threshold=obi_threshold,
        max_position=max_position,
        fee_per_leg_pts=FEE_PER_LEG_PTS,
    )

    bid_px = data['bid_px']
    ask_px = data['ask_px']
    bid_qty = data['bid_qty']
    ask_qty = data['ask_qty']
    mid_price = data['mid_price']
    local_ts = data['local_ts']
    n = len(data)

    # Run strategy on each quote update
    for i in range(n):
        strategy.on_quote(
            ts=int(local_ts[i]),
            bid_px=float(bid_px[i]),
            ask_px=float(ask_px[i]),
            bid_qty=float(bid_qty[i]),
            ask_qty=float(ask_qty[i]),
        )

    # Compute post-fill P&L for each fill
    fills_with_pnl = []
    ts_array = local_ts  # for searchsorted

    for fill in strategy.state.fills:
        fill_ts = fill.fill_ts

        # Find mid-price at fill time
        idx_at_fill = np.searchsorted(ts_array, fill_ts, side='right') - 1
        if idx_at_fill < 0 or idx_at_fill >= n:
            continue
        mid_at_fill = float(mid_price[idx_at_fill])

        # Find mid-price at +1s and +5s
        mid_1s = None
        mid_5s = None

        idx_1s = np.searchsorted(ts_array, fill_ts + HORIZON_1S_NS, side='right') - 1
        if 0 <= idx_1s < n:
            mid_1s = float(mid_price[idx_1s])

        idx_5s = np.searchsorted(ts_array, fill_ts + HORIZON_5S_NS, side='right') - 1
        if 0 <= idx_5s < n:
            mid_5s = float(mid_price[idx_5s])

        # P&L computation
        # Maker sold at ask (side=SELL): profit if mid drops (mid_later < fill_px)
        # Maker bought at bid (side=BUY): profit if mid rises (mid_later > fill_px)
        sign = 1 if fill.side == Side.BUY else -1

        pnl_1s = sign * (mid_1s - mid_at_fill) if mid_1s is not None else 0.0
        pnl_5s = sign * (mid_5s - mid_at_fill) if mid_5s is not None else 0.0

        # Gross capture: distance from fill_px to mid at fill time
        if fill.side == Side.BUY:
            gross_capture = mid_at_fill - fill.fill_px  # bought below mid
        else:
            gross_capture = fill.fill_px - mid_at_fill  # sold above mid

        # Net P&L per fill = gross_capture + post-fill drift - fee
        net_pnl = gross_capture + pnl_5s - FEE_PER_LEG_PTS

        fills_with_pnl.append(FillWithPnL(
            fill_ts=fill_ts,
            side_str=fill.side.name,
            fill_px=fill.fill_px,
            spread_at_fill=fill.spread_at_fill,
            mid_at_fill=mid_at_fill,
            mid_1s=mid_1s,
            mid_5s=mid_5s,
            pnl_1s_pts=pnl_1s,
            pnl_5s_pts=pnl_5s,
            gross_capture_pts=gross_capture,
            net_pnl_pts=net_pnl,
        ))

    return fills_with_pnl


def compute_stats(
    fills: list[FillWithPnL],
    n_sessions: int,
    label: str,
) -> dict:
    """Compute summary statistics for a set of fills."""
    if not fills:
        return {
            'label': label,
            'n_fills': 0,
            'fills_per_session': 0,
            'avg_pnl_pts': 0,
            'win_rate': 0,
            'net_daily_pts': 0,
            'net_daily_ntd': 0,
            'max_consec_loss': 0,
            'avg_spread': 0,
            'avg_gross_capture': 0,
        }

    net_pnls = np.array([f.net_pnl_pts for f in fills])
    gross_caps = np.array([f.gross_capture_pts for f in fills])
    spreads = np.array([f.spread_at_fill for f in fills])
    pnl_5s = np.array([f.pnl_5s_pts for f in fills])

    n = len(fills)
    wins = (net_pnls > 0).sum()
    total_pnl = net_pnls.sum()

    # Max consecutive losses
    max_consec = 0
    current_consec = 0
    for p in net_pnls:
        if p <= 0:
            current_consec += 1
            max_consec = max(max_consec, current_consec)
        else:
            current_consec = 0

    return {
        'label': label,
        'n_fills': n,
        'fills_per_session': n / max(n_sessions, 1),
        'avg_pnl_pts': float(net_pnls.mean()),
        'median_pnl_pts': float(np.median(net_pnls)),
        'std_pnl_pts': float(net_pnls.std()),
        'win_rate': float(wins / n),
        'net_daily_pts': float(total_pnl / max(n_sessions, 1)),
        'net_daily_ntd': float(total_pnl * PT_VALUE_NTD / max(n_sessions, 1)),
        'total_pnl_pts': float(total_pnl),
        'max_consec_loss': max_consec,
        'avg_spread': float(spreads.mean()),
        'avg_gross_capture': float(gross_caps.mean()),
        'avg_adverse_5s': float(pnl_5s.mean()),
    }


def print_stats(stats: dict) -> None:
    """Pretty-print a stats dict."""
    print(f"  Fills: {stats['n_fills']}  ({stats['fills_per_session']:.1f}/session)")
    print(f"  Win rate: {stats['win_rate']:.1%}")
    print(f"  Avg P&L/fill: {stats['avg_pnl_pts']:+.3f} pts")
    print(f"  Median P&L/fill: {stats.get('median_pnl_pts', 0):+.3f} pts")
    print(f"  Std P&L/fill: {stats.get('std_pnl_pts', 0):.3f} pts")
    print(f"  Net daily P&L: {stats['net_daily_pts']:+.1f} pts ({stats['net_daily_ntd']:+.0f} NTD)")
    print(f"  Total P&L: {stats['total_pnl_pts']:+.1f} pts")
    print(f"  Max consec losses: {stats['max_consec_loss']}")
    print(f"  Avg spread at post: {stats['avg_spread']:.1f} pts")
    print(f"  Avg gross capture: {stats['avg_gross_capture']:.2f} pts")
    print(f"  Avg post-fill drift (5s): {stats.get('avg_adverse_5s', 0):+.3f} pts")


def bucket_stats(fills: list[FillWithPnL], n_sessions: int) -> dict[str, dict]:
    """Compute stats by spread bucket."""
    buckets = {
        '5-6': (5, 6),
        '7-10': (7, 10),
        '11-20': (11, 20),
        '20+': (20, 99999),
    }
    results = {}
    for bname, (bmin, bmax) in buckets.items():
        bfills = [f for f in fills if bmin <= f.spread_at_fill <= bmax]
        results[bname] = compute_stats(bfills, n_sessions, f"spread_{bname}")
    return results


def run_parameter_sweep() -> None:
    """Run the full parameter sweep: spread gate x OBI threshold, IS vs OOS."""

    print("=" * 90)
    print("SG-LP BACKTEST — TMFD6 March 2026")
    print("=" * 90)

    # Load data
    print("\nLoading IS data...")
    is_data = load_period(IS_DATES)
    n_is_sessions = len(IS_DATES)
    print(f"  IS: {len(is_data):,} rows, {n_is_sessions} sessions")

    print("Loading OOS data...")
    oos_data = load_period(OOS_DATES)
    n_oos_sessions = len(OOS_DATES)
    print(f"  OOS: {len(oos_data):,} rows, {n_oos_sessions} sessions")

    # Parameter grid
    spread_gates = [5, 7, 10]
    obi_thresholds = [0.0, 0.2, 0.5]

    all_results = []

    for sg in spread_gates:
        for obi_t in obi_thresholds:
            config_label = f"SG={sg}, OBI={obi_t}"
            print(f"\n{'─' * 70}")
            print(f"Config: {config_label}")
            print(f"{'─' * 70}")

            # IS
            print(f"\n  --- In-Sample ({IS_DATES[0]} to {IS_DATES[-1]}) ---")
            is_fills = run_backtest(is_data, spread_gate=sg, obi_threshold=obi_t)
            is_stats = compute_stats(is_fills, n_is_sessions, f"IS_{config_label}")
            print_stats(is_stats)

            is_bucket = bucket_stats(is_fills, n_is_sessions)

            # OOS
            print(f"\n  --- Out-of-Sample ({OOS_DATES[0]} to {OOS_DATES[-1]}) ---")
            oos_fills = run_backtest(oos_data, spread_gate=sg, obi_threshold=obi_t)
            oos_stats = compute_stats(oos_fills, n_oos_sessions, f"OOS_{config_label}")
            print_stats(oos_stats)

            oos_bucket = bucket_stats(oos_fills, n_oos_sessions)

            # IS/OOS comparison
            print(f"\n  --- IS/OOS Comparison ---")
            if is_stats['avg_pnl_pts'] != 0 and oos_stats['avg_pnl_pts'] != 0:
                gap = abs(is_stats['avg_pnl_pts'] - oos_stats['avg_pnl_pts']) / abs(is_stats['avg_pnl_pts'])
                print(f"  IS avg P&L: {is_stats['avg_pnl_pts']:+.3f}, OOS: {oos_stats['avg_pnl_pts']:+.3f}, gap: {gap:.0%}")
                if gap > 0.50:
                    print(f"  >>> WARNING: IS/OOS gap {gap:.0%} > 50% <<<")
            elif is_stats['n_fills'] == 0 and oos_stats['n_fills'] == 0:
                print(f"  No fills in either period")

            # Kill gates
            print(f"\n  --- Kill Gates ---")
            if oos_stats['avg_pnl_pts'] <= 0 and oos_stats['n_fills'] > 0:
                print(f"  >>> KILL: OOS avg P&L = {oos_stats['avg_pnl_pts']:+.3f} <= 0 <<<")
            elif oos_stats['fills_per_session'] < 5:
                print(f"  >>> KILL: OOS fills/session = {oos_stats['fills_per_session']:.1f} < 5 <<<")
            else:
                print(f"  PASS: OOS P&L={oos_stats['avg_pnl_pts']:+.3f}, fills/session={oos_stats['fills_per_session']:.1f}")

            # Spread bucket breakdown
            print(f"\n  --- Spread Bucket Breakdown (OOS) ---")
            for bname, bstats in oos_bucket.items():
                if bstats['n_fills'] > 0:
                    print(f"    {bname}: n={bstats['n_fills']}, avg_pnl={bstats['avg_pnl_pts']:+.3f}, wr={bstats['win_rate']:.0%}")

            all_results.append({
                'spread_gate': sg,
                'obi_threshold': obi_t,
                'is_stats': is_stats,
                'oos_stats': oos_stats,
                'is_bucket': is_bucket,
                'oos_bucket': oos_bucket,
            })

    return all_results


def main():
    results = run_parameter_sweep()

    # Summary table
    print("\n" + "=" * 90)
    print("SUMMARY TABLE")
    print("=" * 90)
    header = f"{'Config':<20} {'IS fills':>10} {'IS P&L/f':>10} {'OOS fills':>10} {'OOS P&L/f':>10} {'OOS daily':>12} {'OOS WR':>8} {'Verdict':>10}"
    print(header)
    print("-" * len(header))

    for r in results:
        sg = r['spread_gate']
        obi = r['obi_threshold']
        is_s = r['is_stats']
        oos_s = r['oos_stats']

        verdict = "PASS"
        if oos_s['avg_pnl_pts'] <= 0 and oos_s['n_fills'] > 0:
            verdict = "KILL-PNL"
        elif oos_s['fills_per_session'] < 5:
            verdict = "KILL-FREQ"

        print(f"SG={sg}, OBI={obi:<4}   {is_s['n_fills']:>10} {is_s['avg_pnl_pts']:>+10.3f} "
              f"{oos_s['n_fills']:>10} {oos_s['avg_pnl_pts']:>+10.3f} "
              f"{oos_s['net_daily_ntd']:>+10.0f} NTD {oos_s['win_rate']:>7.0%} {verdict:>10}")


if __name__ == '__main__':
    main()
