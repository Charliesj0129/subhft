#!/usr/bin/env python3
"""
Batch Generator: Generate multiple realistic market datasets.

Based on Taiwan Futures (TX) parameters:
- ~107,000 trades/day (150K contracts / 1.4 avg size)
- ~2,000,000 LOB events/day (OTR = 20:1)
- Non-uniform: high at open (08:45-09:00) and close (13:30-13:45)
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
import numpy as np

from heston_hawkes_lob import SimConfig, HestonParams, HawkesParams, LOBParams, generate_lob_data


def create_realistic_config(
    seed: int,
    n_days: int = 1,
    trades_per_day: int = 107000,
    otr: int = 20,
) -> SimConfig:
    """
    Create config with realistic Taiwan futures parameters.
    
    Args:
        seed: Random seed
        n_days: Number of trading days
        trades_per_day: ~107,000 for TX
        otr: Order-to-Trade Ratio (~20 for HFT markets)
    """
    lob_events_per_day = trades_per_day * otr  # ~2,000,000
    
    # Scale Hawkes base intensity to achieve target event rate
    # Base intensity = events_per_second * 4 (4 event types)
    # Trading hours: 4.75 hours = 17100 seconds
    trading_seconds = 4.75 * 3600  # 17100s
    events_per_second = lob_events_per_day / trading_seconds  # ~117 events/sec
    
    # Distribute across 4 event types (MktBuy, MktSell, LimBuy, LimSell)
    # Market orders: 30% each side, Limit orders: 20% each side
    base_mkt = events_per_second * 0.10
    base_lim = events_per_second * 0.40
    
    hawkes = HawkesParams(
        mu=np.array([base_mkt, base_mkt, base_lim, base_lim]),
        alpha=np.array([
            [0.3, 0.2, 0.1, 0.05],
            [0.2, 0.3, 0.05, 0.1],
            [0.2, 0.1, 0.2, 0.1],
            [0.1, 0.2, 0.1, 0.2],
        ]),
        beta=20.0,  # Fast decay
    )
    
    heston = HestonParams(
        mu=0.0,  # No drift for intraday
        theta=0.0004,  # ~2% intraday vol
        kappa=5.0,
        xi=0.5,
        rho=-0.7,
        S0=20000.0,  # TX around 20000 points
        v0=0.0004,
    )
    
    lob = LOBParams(
        tick_size=1.0,  # TX tick = 1 point
        n_levels=5,
        base_depth=50,
        depth_decay=0.6,
        spread_mean=2.0,
        spread_kappa=10.0,
    )
    
    return SimConfig(
        n_days=n_days,
        events_per_day=lob_events_per_day,
        dt_heston=1.0 / (252 * lob_events_per_day),
        seed=seed,
        heston=heston,
        hawkes=hawkes,
        lob=lob,
    )


def generate_single_dataset(args: tuple) -> dict:
    """Generate a single dataset (for parallel execution)"""
    idx, output_dir, n_days, trades_per_day, otr = args
    
    seed = 42 + idx
    output_path = Path(output_dir) / f"dataset_{idx:03d}.npz"
    
    if output_path.exists():
        return {"idx": idx, "status": "skipped", "path": str(output_path)}
    
    try:
        config = create_realistic_config(
            seed=seed,
            n_days=n_days,
            trades_per_day=trades_per_day,
            otr=otr,
        )
        
        print(f"[{idx:03d}] Generating {config.n_days} days, target {config.events_per_day} events...")
        data = generate_lob_data(config)
        
        np.savez_compressed(str(output_path), **data)
        
        n_events = len(data["timestamp"])
        return {
            "idx": idx,
            "status": "success",
            "path": str(output_path),
            "n_events": n_events,
        }
    except Exception as e:
        return {"idx": idx, "status": "error", "error": str(e)}


def batch_generate(
    n_datasets: int,
    output_dir: str,
    n_days: int = 1,
    trades_per_day: int = 107000,
    otr: int = 20,
    n_workers: int = 4,
) -> list:
    """Generate multiple datasets in parallel"""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    
    args_list = [
        (i, output_dir, n_days, trades_per_day, otr)
        for i in range(n_datasets)
    ]
    
    results = []
    
    # Sequential for stability (Hawkes can be memory-intensive)
    for args in args_list:
        result = generate_single_dataset(args)
        results.append(result)
        
        if result["status"] == "success":
            print(f"  ✓ Dataset {result['idx']:03d}: {result['n_events']} events")
        elif result["status"] == "skipped":
            print(f"  - Dataset {result['idx']:03d}: skipped (exists)")
        else:
            print(f"  ✗ Dataset {result['idx']:03d}: {result.get('error', 'unknown error')}")
    
    return results


def main():
    parser = argparse.ArgumentParser(description="Batch generate market datasets")
    parser.add_argument("--n", type=int, default=100, help="Number of datasets")
    parser.add_argument("--days", type=int, default=1, help="Days per dataset")
    parser.add_argument("--trades-per-day", type=int, default=107000, help="Trades per day")
    parser.add_argument("--otr", type=int, default=20, help="Order-to-Trade Ratio")
    parser.add_argument("--output-dir", type=str, default="research/data/batch_100")
    parser.add_argument("--workers", type=int, default=1, help="Parallel workers")
    args = parser.parse_args()
    
    print(f"\n{'='*60}")
    print(f"  BATCH MARKET DATA GENERATOR")
    print(f"{'='*60}")
    print(f"  Datasets: {args.n}")
    print(f"  Days/dataset: {args.days}")
    print(f"  Trades/day: {args.trades_per_day:,}")
    print(f"  OTR: {args.otr}")
    print(f"  LOB events/day: {args.trades_per_day * args.otr:,}")
    print(f"  Output: {args.output_dir}")
    print(f"{'='*60}\n")
    
    results = batch_generate(
        n_datasets=args.n,
        output_dir=args.output_dir,
        n_days=args.days,
        trades_per_day=args.trades_per_day,
        otr=args.otr,
        n_workers=args.workers,
    )
    
    success = sum(1 for r in results if r["status"] == "success")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    errors = sum(1 for r in results if r["status"] == "error")
    
    print(f"\n{'='*60}")
    print(f"  COMPLETE: {success} success, {skipped} skipped, {errors} errors")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
