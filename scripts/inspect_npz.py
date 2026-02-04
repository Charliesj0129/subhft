import numpy as np
import sys
import os

path = "research/data/hbt_txfb6/dataset_000.npz"

if len(sys.argv) > 1:
    path = sys.argv[1]

print(f"Loading {path}...")
d = np.load(path)
data = d['data']

print(f"Shape: {data.shape}")
print(f"Dtype: {data.dtype}")

# Print first 50 rows
print("First 50 rows:")
for i in range(50):
    row = data[i]
    ev = row['ev']
    ts = row['exch_ts']
    px = row['px']
    qty = row['qty']
    
    # print(f"[{i}] TS:{ts} EV:{ev:#x} Px:{px} Qty:{qty}")
    
    start_ts = data[0]['exch_ts']
    end_ts = data[-1]['exch_ts']
    duration_sec = (end_ts - start_ts) / 1e9
    print(f"Start: {start_ts}")
    print(f"End:   {end_ts}")
    print(f"Duration: {duration_sec:.2f} seconds")
    print(f"Events: {len(data)}")
