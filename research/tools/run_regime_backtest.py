#!/usr/bin/env python3
"""
Run Backtest on Specific Regime Subset
"""
import pandas as pd
import numpy as np
import os
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import sys
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).parent))
from run_hft_backtest import process_single_file, print_stats

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--factor", type=str, required=True)
    parser.add_argument("--regime-col", type=str, default="volatility")
    parser.add_argument("--quantile", type=float, default=0.8, help="Threshold quantile (e.g. 0.8 for top 20%)")
    parser.add_argument("--direction", type=str, default="above", choices=["above", "below"])
    args = parser.parse_args()

    # Load Analysis
    df = pd.read_csv("../reports/regime_analysis.csv")
    
    # Filter Files
    threshold = df[args.regime_col].quantile(args.quantile)
    
    if args.direction == "above":
        subset = df[df[args.regime_col] >= threshold]
        print(f"Selecting {args.regime_col} >= {threshold:.6f} (Top {100*(1-args.quantile):.0f}%)")
    else:
        subset = df[df[args.regime_col] <= threshold]
        print(f"Selecting {args.regime_col} <= {threshold:.6f} (Bottom {100*args.quantile:.0f}%)")
        
    files = subset["file"].tolist()
    data_dir = "../data/batch_100"
    files = [os.path.join(data_dir, f) for f in files]
    
    print(f"Running backtest on {len(files)} files...")
    
    # Run Backtest
    completed = 0
    results = []
    
    with ProcessPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(process_single_file, f, [args.factor]): f for f in files}
        
        for future in as_completed(futures):
            res = future.result()
            if "error" in res:
                print(f"Error: {res['error']}")
                continue
                
            metrics = res["results"][args.factor]
            results.append(metrics)
            completed += 1
            
    # Stats
    sharpes = [m["sharpe"] for m in results]
    pnl = [m["pnl"] for m in results]
    
    print("\n" + "="*60)
    print(f"REGIME REPORT: {args.factor} in {args.regime_col} {args.direction} {threshold:.4f}")
    print("="*60)
    print(f"Count: {len(sharpes)}")
    print(f"Sharpe: {print_stats('', sharpes)}")
    print(f"Total PnL: {np.sum(pnl):,.0f}")
    print("="*60)

if __name__ == "__main__":
    main()
