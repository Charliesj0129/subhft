
import numpy as np
import pandas as pd
import os
import sys

def compute_total_depth_vectorized(data):
    # Data: [event, exch_ts, local_ts, side, px, qty]
    df = pd.DataFrame(data, columns=['event', 'ts', 'local_ts', 'side', 'px', 'qty'])
    
    # Filter Depth Events (1)
    df_depth = df[df['event'] == 1]
    
    # Group by [ts, side] and sum qty
    # This assumes full refresh or sufficient density.
    # Since we emit 5 levels per update, summing all valid depths at TS gives TotalDepth.
    
    grouped = df_depth.groupby(['ts', 'side'])['qty'].sum().unstack(fill_value=0)
    
    # grouped has columns: -1 (Ask), 1 (Bid) (if present)
    if 1 not in grouped.columns: grouped[1] = 0
    if -1 not in grouped.columns: grouped[-1] = 0
    
    bids = grouped[1]
    asks = grouped[-1]
    
    # Formula: (Bid - Ask) / (Bid + Ask)
    total = bids + asks
    imbalance = (bids - asks) / total
    
    # Fill NaN (where total=0)
    imbalance = imbalance.fillna(0.0)
    
    # Result: [ts, value]
    # Reset index to get TS column
    res = imbalance.reset_index()
    res.columns = ['ts', 'signal']
    
    return res.to_numpy()

def main(symbol):
    data_path = f"research/data/hbt_multiproduct/{symbol}.npz"
    out_dir = f"research/data/signals/{symbol}"
    if not os.path.exists(out_dir): os.makedirs(out_dir)
    
    print(f"Loading {data_path}...")
    d_raw = np.load(data_path)
    if isinstance(d_raw, np.lib.npyio.NpzFile):
        d = d_raw['data']
    else:
        d = d_raw
        
    # OPTIMIZATION: Slice to 600s to match Backtest
    ts = d[:, 1]
    if len(ts) > 0:
        start_ts = ts[0]
        end_ts = start_ts + 600 * 1_000_000_000
        idx = np.searchsorted(ts, end_ts)
        print(f"Slicing Signal Computation to {idx} rows (600s)")
        d = d[:idx]

    print("Computing TotalDepth (Vectorized)...")
    sig = compute_total_depth_vectorized(d)
    
    out_path = f"{out_dir}/TotalDepth.npy"
    np.save(out_path, sig)
    print(f"Saved {out_path} ({len(sig)} points)")

if __name__ == '__main__':
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        main('TXFB6')
