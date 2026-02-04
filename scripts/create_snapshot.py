#!/usr/bin/env python3
"""
Create initial snapshot for hftbacktest from converted NPZ data.
Uses hftbacktest.data.utils.snapshot.create_last_snapshot().
"""
import os
import sys

# Add hftbacktest to path if needed
sys.path.insert(0, "/home/charlie/hft_platform/external_repos/hftbacktest_fresh/py-hftbacktest")

try:
    from hftbacktest.data.utils import snapshot
except ImportError:
    print("Error: hftbacktest.data.utils.snapshot not found.")
    print("Attempting alternative import...")
    # Try from installed package
    from hftbacktest.data import utils
    print(f"Available utils: {dir(utils)}")
    sys.exit(1)

INPUT_FILE = "research/data/hbt_txfb6/dataset_000.npz"
OUTPUT_FILE = "research/data/hbt_txfb6/dataset_000_snapshot.npz"

def main():
    if not os.path.exists(INPUT_FILE):
        print(f"Input file not found: {INPUT_FILE}")
        return
        
    print(f"Creating snapshot from {INPUT_FILE}...")
    
    try:
        # v2.4 API needs tick_size and lot_size
        snapshot.create_last_snapshot(INPUT_FILE, OUTPUT_FILE, tick_size=1.0, lot_size=1.0)
        print(f"Snapshot saved to {OUTPUT_FILE}")
    except Exception as e:
        print(f"Error creating snapshot: {e}")
        import traceback
        traceback.print_exc()
        
        # Manual fallback: Extract first snapshot set (Bids + Asks)
        print("Attempting manual snapshot creation...")
        import numpy as np
        d = np.load(INPUT_FILE)
        data = d['data']
        
        # Find first timestamp with DEPTH data
        # DEPTH_EVENT (1) or DEPTH_SNAPSHOT (4) indicates book data
        first_ts = data[0]['exch_ts']
        
        # Get all events at first timestamp (should contain full book)
        mask = data['exch_ts'] == first_ts
        snap_data = data[mask]
        
        print(f"Events at first TS ({first_ts}): {len(snap_data)}")
        for i in range(min(10, len(snap_data))):
            print(f"  [{i}] EV: {snap_data[i]['ev']:#x} Px: {snap_data[i]['px']} Qty: {snap_data[i]['qty']}")
        
        if len(snap_data) >= 5:  # At least 5 levels expected
            np.savez_compressed(OUTPUT_FILE, data=snap_data)
            print(f"Manual snapshot saved: {len(snap_data)} events")
        else:
            # Try getting first N events covering DEPTH
            depth_mask = ((data['ev'] & 0xF) == 1) | ((data['ev'] & 0xF) == 4)
            depth_data = data[depth_mask][:100]
            np.savez_compressed(OUTPUT_FILE, data=depth_data)
            print(f"Fallback snapshot saved: {len(depth_data)} depth events")

if __name__ == "__main__":
    main()
