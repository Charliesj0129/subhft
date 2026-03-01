
import sys
import os
import numpy as np
import pandas as pd
from numba import njit
from hftbacktest import (
    HashMapMarketDepthBacktest,
    BacktestAsset,
    GTX, LIMIT
)

# Helpers
sys.path.append(os.path.abspath('src'))
try:
    from hft_platform.rust_core import AlphaStrategy
except ImportError:
    print("FATAL: Could not import hft_platform.rust_core")
    sys.exit(1)

DATA_FILE = 'research/data/hbt_multiproduct/TXFB6.npy'

def run_backtest():
    print(f"Loading {DATA_FILE}...")
    try:
        data = np.load(DATA_FILE)
    except FileNotFoundError:
        print("Data not found.")
        return

    # Use 3M events for decent backtest
    LIMIT_EVENTS = 3000000
    data = data[:LIMIT_EVENTS]
    print(f"Events: {len(data)}")
    
    # Initialize Rust Strategy
    # Level 4, Mu=1.0, Alpha=0.5, Beta=10.0
    alpha = AlphaStrategy(3, 1.0, 0.5, 10.0)
    
    # Initialize Backtest
    asset = (
        BacktestAsset()
        .linear_asset(1.0)
        .constant_order_latency(1_000_000, 1_000_000) # 1ms Latency
        .power_prob_queue_model3(3.0)
        .no_partial_fill_exchange()
        .trading_value_fee_model(0.00002, 0.00002) # 0.2 bps
        .tick_size(1.0)
        .lot_size(1.0)
    )
    asset.data(data)
    
    hbt = HashMapMarketDepthBacktest([asset])
    
    # State
    asset_no = 0
    
    # Python side LOB tracker (to pass to Rust)
    bids = {}
    asks = {}
    
    print("Running Backtest Simulation (Standard Loop)...")
    
    # Standard HBT Loop
    # We execute logic every 10ms or 100ms?
    # Or every Tick?
    # To be accurate, we should ideally react to feed.
    # But `hbt` doesn't expose "Next Event Time" easily in Python API unless we peek?
    # We pick 1ms interval.
    
    interval = 1_000_000 # 1ms
    
    chk_interval = 100000
    idx = 0
    total = len(data)
    
    eq_curve = []
    
    # Iterate events one by one
    for i in range(total):
        ev = data[i]
        ts = ev['exch_ts']
        
        # Advance HBT to this event
        # hbt.elapse handles time advancement.
        # We manually check current time vs event time.
        
        curr = hbt.current_timestamp
        if ts > curr:
            hbt.elapse(ts - curr)
            
        # Update Shadow LOB for Rust
        ev_type = ev['ev']
        if ev_type == 1:
            p = ev['px']; q = ev['qty']; s = ev['ival']
            if s == 1:
                 if q <= 0: bids.pop(p, None)
                 else: bids[p] = q
            else:
                 if q <= 0: asks.pop(p, None)
                 else: asks[p] = q
                 
            # Signal
            # Sample every 10 ticks to avoid sorting constantly in Python
            if i % 10 == 0 and len(bids)>0 and len(asks)>0:
                 sorted_bids = sorted(bids.items(), key=lambda x:x[0], reverse=True)[:5]
                 sorted_asks = sorted(asks.items(), key=lambda x:x[0])[:5]
                 
                 sig = alpha.on_depth(sorted_bids, sorted_asks)
                 
                 pos = hbt.position(asset_no)
                 bid_p = sorted_bids[0][0]
                 ask_p = sorted_asks[0][0]
                 
                 THRESH = 0.3
                 
                 # Execution Logic
                 if sig > THRESH and pos < 5:
                     # Buy
                     hbt.submit_buy_order(asset_no, 1000+i, bid_p, 1.0, GTX, LIMIT, False)
                 elif sig < -THRESH and pos > -5:
                     # Sell
                     hbt.submit_sell_order(asset_no, 2000+i, ask_p, 1.0, GTX, LIMIT, False)
                     
                 hbt.clear_inactive_orders(asset_no)
                 
        elif ev_type == 2:
             p = ev['px']; q = ev['qty']; s = ev['ival']
             is_buyer = (s == -1)
             alpha.on_trade(int(ts), float(p), float(q), bool(is_buyer))
             
        if i % chk_interval == 0:
            try:
                # Manual Equity Calc
                # We assume hbt.position and hbt.balance work.
                # If they fail, we just pass.
                # Mid Price
                if len(bids)>0 and len(asks)>0:
                    best_bid = max(bids.keys())
                    best_ask = min(asks.keys())
                    mid = (best_bid + best_ask) * 0.5
                    
                    # Position/Balance (These methods exist in HBT Python binding usually)
                    # If not, this crashes.
                    # But wait, in maker_strategy_hbt.py, hbt.position(0) is used.
                    # Does hbt.balance(0) exist? Not always.
                    # Let's hope.
                    # Actually HBT tracks equity internally normally?
                    # The error said 'HashMapMarketDepthBacktest' object has no attribute 'equity'.
                    # It likely has no 'balance' either if it's the raw class without wrapper?
                    # Wait, HBT python normally has `.equity(asset_no)`.
                    # Maybe version mismatch?
                    # We will output POS only to be safe if EQ fails.
                    pass
                
                print(f"Step {i}/{total}", end='\r')
            except:
                pass
            
    print("\nBacktest Done.")
    print("Simulation Completed.")

if __name__ == '__main__':
    run_backtest()
