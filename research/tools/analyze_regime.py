#!/usr/bin/env python3
"""
Regime Analysis Tool
Correlates Market Microstructure Features (Vol, Spread, Activity) 
with Factor Performance (Sharpe) across datasets.
"""

import os
import glob
import numpy as np
import pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from factor_registry import FactorRegistry
from hft_factor_strategy import run_factor_backtest

def analyze_dataset(file_path):
    """
    Extract regime metrics and run backtest for key factors.
    """
    try:
        data = dict(np.load(file_path))
        
        # 1. Extract Regime Metrics
        # Volatility (Realized Vol of Mid Price returns)
        mid = (data["bid_prices"][:, 0] + data["ask_prices"][:, 0]) / 2
        returns = np.diff(np.log(mid + 1e-10))
        realized_vol = np.std(returns) * np.sqrt(len(returns)) # Approx annualized if 1 day
        
        # Spread (Average Quoted Spread)
        bid = data["bid_prices"][:, 0]
        ask = data["ask_prices"][:, 0]
        spreads = ask - bid
        avg_spread = np.mean(spreads)
        
        # Activity (Trade Count)
        trades = np.sum(data["trade_volume"] > 0)
        
        # 2. Run Backtest for Target Factors
        # We focus on factors that failed or flipped: QueuePressure, TradeArrivalRate
        # And the robust baseline: DepthSlope.
        # Phase 13: Add Mean Reversion candidates (PriceReversal, HighFreqRSI)
        target_factors = ["QueuePressure", "DepthSlope", "PriceReversal", "HighFreqRSI"]
        
        results = {}
        for name in target_factors:
            factor = FactorRegistry.get_factor(name)
            signals = factor.compute(data)
            
            # Normalize
            signals = np.nan_to_num(signals, nan=0.0, posinf=0.0, neginf=0.0)
            std = np.std(signals)
            if std > 1e-8:
                signals = signals / (3 * std)
                signals = np.clip(signals, -1, 1)
                
            bt_res = run_factor_backtest(data, signals, threshold=0.5, position_limit=10)
            results[name] = bt_res["metrics"]["sharpe"]
            
        return {
            "file": os.path.basename(file_path),
            "volatility": realized_vol,
            "avg_spread": avg_spread,
            "trade_count": trades,
            "sharpe_QueuePressure": results["QueuePressure"],
            "sharpe_DepthSlope": results["DepthSlope"],
            "sharpe_PriceReversal": results["PriceReversal"],
            "sharpe_HighFreqRSI": results["HighFreqRSI"]
        }
        
    except Exception as e:
        return {"file": os.path.basename(file_path), "error": str(e)}

def main():
    data_dir = "../data/batch_100"
    files = sorted(glob.glob(os.path.join(data_dir, "*.npz")))
    
    # Use Stride=1 (Full Analysis) or Stride=5 for speed
    # Let's do Full Analysis on all 100 files to get good correlations
    print(f"Analyzing {len(files)} datasets for Regime Dependencies...")
    
    results = []
    completed = 0
    
    with ProcessPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(analyze_dataset, f): f for f in files}
        
        for future in as_completed(futures):
            res = future.result()
            if "error" not in res:
                results.append(res)
            else:
                print(f"Error: {res['error']}")
                
            completed += 1
            if completed % 10 == 0:
                print(f"Progress: {completed}/{len(files)}...")

    # DataFrame
    df = pd.DataFrame(results)
    print("\nanalysis complete. Data sample:")
    print(df.head())
    
    # Correlation Analysis
    print("\n--- Correlation Matrix (Regime vs Sharpe) ---")
    regime_cols = ["volatility", "avg_spread", "trade_count"]
    sharpe_cols = ["sharpe_QueuePressure", "sharpe_DepthSlope", "sharpe_PriceReversal", "sharpe_HighFreqRSI"]
    
    corr = df[regime_cols + sharpe_cols].corr().loc[regime_cols, sharpe_cols]
    print(corr)
    
    # Save
    df.to_csv("../reports/regime_analysis.csv", index=False)
    print("\nSaved to ../reports/regime_analysis.csv")

if __name__ == "__main__":
    main()
