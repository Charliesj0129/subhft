#!/usr/bin/env python3
"""
Run HFT Backtest: Robust Parallel Execution Pipeline
Uses joblib for efficient multi-core processing of large numpy datasets.
"""

from __future__ import annotations

import argparse
import sys
import os
import glob
import numpy as np
from pathlib import Path
# from joblib import Parallel, delayed

from collections import defaultdict
import time

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from factor_registry import FactorRegistry
from hft_factor_strategy import run_factor_backtest

# -----------------------------------------------------------------------------
# Core Worker Function
# -----------------------------------------------------------------------------

def process_single_file(file_path: str, factor_names: list[str] = None) -> dict:
    """
    Worker function to process a single data file.
    Loads data once, then runs backtest for all requested factors.
    Returns metrics AND dataset features (volatility) for regime analysis.
    """
    try:
        # Load data
        data = dict(np.load(file_path))
        
        # Calculate Dataset Features for Regime Classification
        mid_price = data.get("mid_price")
        if mid_price is not None:
            returns = np.diff(mid_price)
            # Simple realize vol proxy (std dev of tick-to-tick returns)
            realized_vol = np.std(returns)
            spread = data.get("best_ask", np.zeros_like(mid_price)) - data.get("best_bid", np.zeros_like(mid_price))
            avg_spread = np.mean(spread)
        else:
            realized_vol = 0.0
            avg_spread = 0.0
        
        dataset_features = {
            "volatility": float(realized_vol),
            "spread": float(avg_spread)
        }

        if factor_names is None:
            factor_names = FactorRegistry.list_factors()
            
        file_results = {}
        
        for name in factor_names:
            try:
                # 1. Compute
                factor = FactorRegistry.get_factor(name)
                signals = factor.compute(data)
                
                # 2. Clean & Normalize
                signals = np.nan_to_num(signals, nan=0.0, posinf=0.0, neginf=0.0)
                sig_std = np.std(signals)
                if sig_std > 1e-8:
                    signals = signals / (3 * sig_std)
                    signals = np.clip(signals, -1, 1)
                
                # 3. Simulate
                # Use slightly higher threshold for stability in large scale
                result = run_factor_backtest(data, signals, threshold=0.5, position_limit=10)
                m = result["metrics"]
                
                file_results[name] = {
                    "sharpe": m["sharpe"],
                    "pnl": m["pnl"],
                    "max_dd": m["max_dd"],
                    "n_trades": m["n_trades"]
                }
            except Exception as e:
                file_results[name] = {"error": str(e)}
                
        return {
            "file": os.path.basename(file_path), 
            "results": file_results,
            "features": dataset_features
        }
        
    except Exception as e:
        return {"file": os.path.basename(file_path), "error": str(e)}

# -----------------------------------------------------------------------------
# Aggregation & Reporting
# -----------------------------------------------------------------------------

def print_stats(metrics):
    """Print statistical summary of a metric list"""
    if not metrics:
        return "-"
    
    med = np.median(metrics)
    avg = np.mean(metrics)
    return f"{med:6.2f} (μ={avg:5.2f})"

def run_parallel_backtest(data_dir: str, output_dir: str = None, n_jobs: int = -1, stride: int = 1, factor_filter: str = None):
    """
    Main driver for parallel backtesting using JOBLIB.
    1. Finds all .npz files
    2. Runs parallel workers
    3. Splits by Regime (High Vol vs Low Vol)
    4. Aggregates and reports
    """
    start_time = time.time()
    
    # 1. Discovery
    files = sorted(glob.glob(os.path.join(data_dir, "*.npz")))
    if not files:
        print(f"No .npz files found in {data_dir}")
        return
        
    n_original = len(files)
    
    # Apply stride
    if stride > 1:
        files = files[::stride]
        
    n_files = len(files)
    
    # Determine factors to test
    if factor_filter:
        factors_to_test = [f.strip() for f in factor_filter.split(",")]
    else:
        factors_to_test = FactorRegistry.list_factors()
    
    print(f"Found {n_original} datasets. (Stride={stride} -> {n_files} selected)")
    print(f"Factors to Test:    {len(factors_to_test)}")
    print(f"Workers (n_jobs):   {n_jobs}")
    print("=" * 80)
    
    # 2. Execution with Joblib
    # Use n_jobs=-1 to use all cores
    # Serial execution instead of Parallel
    results = []
    for i, f in enumerate(files):
        print(f"[{i+1}/{len(files)}] Processing {f}...")
        results.append(process_single_file(f, factors_to_test))
    
    # 3. Post-Processing & Regime Classification
    # Collect all volatilities to determine regime threshold
    all_vols = []
    valid_results = []
    
    for res in results:
        if "error" in res and "features" not in res:
            print(f"Error in {res['file']}: {res.get('error')}")
            continue
        valid_results.append(res)
        all_vols.append(res["features"]["volatility"])
    
    if not all_vols:
        print("No valid results found.")
        return

    vol_median = np.median(all_vols)
    print(f"\nRegime Threshold (Median Volatility): {vol_median:.6f}")
    
    # Aggregate by Factor and Regime
    factor_stats = defaultdict(lambda: {"all": [], "high_vol": [], "low_vol": []})
    
    for res in valid_results:
        vol = res["features"]["volatility"]
        regime = "high_vol" if vol > vol_median else "low_vol"
        
        for name, metrics in res["results"].items():
            if "error" in metrics:
                continue
            
            sharpe = metrics["sharpe"]
            pnl = metrics["pnl"]
            
            # Store primary metric (Sharpe)
            factor_stats[name]["all"].append(sharpe)
            factor_stats[name][regime].append(sharpe)

    # 4. Consolidation & Reporting
    print("\n" + "=" * 120)
    print(f" FINAL REPORT (Regime-Based) - {len(valid_results)} files processed in {time.time()-start_time:.1f}s")
    print("=" * 120)
    print(f"{'Factor':<20} | {'All (Med)':<15} | {'High Vol (Med)':<18} | {'Low Vol (Med)':<18} | {'Robustness'}")
    print("-" * 120)
    
    summary_data = []
    
    for factor in sorted(factor_stats.keys()):
        stats = factor_stats[factor]
        
        med_all = np.median(stats["all"]) if stats["all"] else 0.0
        med_high = np.median(stats["high_vol"]) if stats["high_vol"] else 0.0
        med_low = np.median(stats["low_vol"]) if stats["low_vol"] else 0.0
        
        # Robustness Check: Works in both regimes?
        is_robust = (med_high > 0.5) and (med_low > 0.5)
        is_high_vol_specific = (med_high > 1.0) and (med_low < 0.5)
        
        note = ""
        if is_robust: note = "✅ ROBUST"
        elif is_high_vol_specific: note = "🔥 HIGH VOL ONLY"
        elif med_all < 0: note = "❌ FAIL"
        
        print(f"{factor:<20} | {med_all:6.2f}          | {med_high:6.2f}             | {med_low:6.2f}             | {note}")
        
        summary_data.append({
            "name": factor,
            "med_all": med_all,
            "med_high": med_high,
            "med_low": med_low,
            "note": note
        })
        
    # Save Summary
    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        summary_path = os.path.join(output_dir, "regime_summary.csv")
        with open(summary_path, "w") as f:
            f.write("factor,median_all,median_high_vol,median_low_vol,note\n")
            for item in summary_data:
                f.write(f"{item['name']},{item['med_all']:.4f},{item['med_high']:.4f},{item['med_low']:.4f},{item['note']}\n")
        print(f"\nSaved summary to {summary_path}")

def main():
    parser = argparse.ArgumentParser(description="Parallel HFT Factor Validator")
    parser.add_argument("--data-dir", type=str, required=True, help="Directory containing .npz LOB files")
    parser.add_argument("--output-dir", type=str, default="results", help="Directory to save reports")
    parser.add_argument("--workers", type=int, default=-1, help="Number of parallel workers (-1 for all)")
    parser.add_argument("--stride", type=int, default=1, help="Stride for data sampling")
    parser.add_argument("--factor", type=str, default=None, help="Specific factor to test")
    args = parser.parse_args()
    
    run_parallel_backtest(args.data_dir, args.output_dir, args.workers, args.stride, args.factor)

if __name__ == "__main__":
    main()
