#!/usr/bin/env python3
"""
Patch NPZ files in real_data_reconstructed to add 'trade_side'.
Inference Logic (Quote Rule):
- Px >= BestAsk => Buy (1)
- Px <= BestBid => Sell (-1)
- Else => 0 (Unknown/Mid)
"""
import numpy as np
import os
import glob
from tqdm import tqdm

DATA_DIR = "research/data/real_data_reconstructed"

def patch_file(f_path):
    d = np.load(f_path)
    # Check if exists
    if 'trade_side' in d.files:
        print(f"Skipping {f_path}, 'trade_side' already exists.")
        return

    # Load arrays
    data_dict = {k: d[k] for k in d.files}
    
    is_trade = data_dict['is_trade']
    trade_price = data_dict['trade_price']
    bids_p = data_dict['bid_prices']
    asks_p = data_dict['ask_prices']
    
    N = len(is_trade)
    trade_side = np.zeros(N, dtype=np.int8)
    
    # Vectorized inference
    # Best Bid/Ask (Level 0)
    bb = bids_p[:, 0]
    ba = asks_p[:, 0]
    
    # Logic
    # We only care where is_trade == True.
    # But doing full vector op is fast enough.
    
    # Buy: Px >= Best Ask
    # Sell: Px <= Best Bid
    
    # Note: LOB might be zero if data is sparse?
    # Assuming bb/ba are valid where trades happen.
    
    # Masks
    buy_mask = (trade_price >= ba) & (ba > 0) & is_trade
    sell_mask = (trade_price <= bb) & (bb > 0) & is_trade
    
    trade_side[buy_mask] = 1
    trade_side[sell_mask] = -1
    
    # What about inside spread?
    # Leave as 0 for now. The factors usually filter for side != 0.
    
    # Add to dict
    data_dict['trade_side'] = trade_side
    
    # Log stats
    n_buys = np.sum(trade_side == 1)
    n_sells = np.sum(trade_side == -1)
    n_trades = np.sum(is_trade)
    print(f"File {os.path.basename(f_path)}: Trades={n_trades}, Buys={n_buys}, Sells={n_sells}, Unclassified={n_trades - n_buys - n_sells}")
    
    # Save back
    np.savez_compressed(f_path, **data_dict)

def main():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "*.npz")))
    if not files:
        print(f"No files found in {DATA_DIR}")
        return
        
    for f in tqdm(files, desc="Patching"):
        patch_file(f)

if __name__ == "__main__":
    main()
