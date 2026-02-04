#!/usr/bin/env python3
"""
Minimal debug script for HFT Backtest LOB issue.
"""
import numpy as np
import os
from hftbacktest import (
    BacktestAsset,
    HashMapMarketDepthBacktest,
    ROIVectorMarketDepthBacktest
)

DATA_FILE = "research/data/hbt_txfb6/dataset_000.npz"
SNAPSHOT_FILE = "research/data/hbt_txfb6/dataset_000_snapshot.npz"

def main():
    data_file = os.path.abspath(DATA_FILE)
    snapshot_file = os.path.abspath(SNAPSHOT_FILE) if os.path.exists(SNAPSHOT_FILE) else None
    
    print(f"Data: {data_file}")
    print(f"Snapshot: {snapshot_file}")
    
    # Inspect data
    d = np.load(data_file)
    data = d['data']
    print(f"Data shape: {data.shape}")
    print(f"First 5 events:")
    for i in range(5):
        print(f"  {data[i]}")
    
    # Build asset
    asset_builder = BacktestAsset().data([data_file])
    if snapshot_file:
        asset_builder = asset_builder.initial_snapshot(snapshot_file)
    
    asset = (
        asset_builder
            .linear_asset(1.0)
            .tick_size(1.0)
            .lot_size(1.0)
    )
    
    # Test with HashMapMarketDepthBacktest
    print("\n--- HashMapMarketDepthBacktest ---")
    hbt = HashMapMarketDepthBacktest([asset])
    
    print(f"Initial TS: {hbt.current_timestamp}")
    depth = hbt.depth(0)
    print(f"Initial Best Bid: {depth.best_bid}")
    print(f"Initial Best Ask: {depth.best_ask}")
    
    # Elapse time
    for step in range(10):
        ret = hbt.elapse(1_000_000_000)  # 1 second
        depth = hbt.depth(0)
        ts = hbt.current_timestamp
        bb, ba = depth.best_bid, depth.best_ask
        print(f"Step {step}: TS={ts}, Bid={bb}, Ask={ba}, elapse_ret={ret}")
        if not np.isnan(bb):
            print("  LOB POPULATED!")
            break
    
    hbt.close()
    
    # Test with ROIVectorMarketDepthBacktest
    print("\n--- ROIVectorMarketDepthBacktest ---")
    asset_builder2 = BacktestAsset().data([data_file])
    if snapshot_file:
        asset_builder2 = asset_builder2.initial_snapshot(snapshot_file)
    asset2 = asset_builder2.linear_asset(1.0).tick_size(1.0).lot_size(1.0)
    
    hbt2 = ROIVectorMarketDepthBacktest([asset2])
    print(f"\nROIVector Methods: {dir(hbt2)}")
    
    print(f"Initial TS: {hbt2.current_timestamp}")
    depth2 = hbt2.depth(0)
    print(f"Initial Best Bid: {depth2.best_bid}")
    print(f"Initial Best Ask: {depth2.best_ask}")
    
    for step in range(10):
        ret = hbt2.elapse(1_000_000_000)
        depth2 = hbt2.depth(0)
        ts = hbt2.current_timestamp
        bb, ba = depth2.best_bid, depth2.best_ask
        print(f"Step {step}: TS={ts}, Bid={bb}, Ask={ba}, elapse_ret={ret}")
        if not np.isnan(bb):
            print("  LOB POPULATED!")
            break
            
    hbt2.close()

if __name__ == "__main__":
    main()
