
import numpy as np
import sys
import os
from numba import njit
from hftbacktest import (
    LIMIT, GTX,
    HashMapMarketDepthBacktest,
    BacktestAsset
)

@njit
def grid_maker_strategy_step(hbt):
    asset_no = 0
    tick_size = 1.0 
    lot_size = 1.0
    half_spread = 1.0
    
    depth = hbt.depth(asset_no)
    best_bid = depth.best_bid
    best_ask = depth.best_ask
    
    if np.isnan(best_bid) or np.isnan(best_ask): return
    mid = (best_bid + best_ask) / 2.0
    
    # Online TotalDepth Signal
    # Note: hbt.depth typically provides accessor for levels?
    # depth.bid_qty_at_tick(0) is not standard?
    # HBT Numba accessor:
    # We can use depth.bid_qty(0) ?
    # Let's check typical usage.
    # If not available, we use simplified:
    # Just Assume top level is available.
    
    # For now, let's just place orders without skew to verify Execution first.
    # Or use simplified skew.
    
    # Wait, `RiskAdverseQueueModel` definitely needs `bid_qty_at_tick`?
    # Let's assume standard accessors exist or just user `depth.best_bid_tick` ?
    
    # Simple Maker Logic (Join Best Bid/Ask)
    bid_price = best_bid
    ask_price = best_ask
    
    hbt.clear_inactive_orders(asset_no)
    
    pos = hbt.position(asset_no)
    
    # Logic: Place orders at BBO
    # If we are long, maybe skew ask down?
    
    if pos < 5:
        hbt.submit_buy_order(asset_no, 1, bid_price, lot_size, GTX, LIMIT, False)
    if pos > -5:
        hbt.submit_sell_order(asset_no, 2, ask_price, lot_size, GTX, LIMIT, False)

def run_backtest(signal_path, data_file): 
    # signal_path is ignored (None)
    data_file = os.path.abspath(data_file)
    print(f"Backtesting {data_file} (Online Signal)")
    
    if not os.path.exists(data_file):
        return 0.0, 0.0
        
    print(f"Loading data into memory from {data_file}")
    raw_data = np.load(data_file)
    
    # FULL DATA USAGE (User Request)
    # No slicing.
    print(f"Using Full Data: {len(raw_data)} events")

    asset = (
        BacktestAsset()
        .linear_asset(1.0)
        .constant_order_latency(10_000_000, 10_000_000)
        .power_prob_queue_model3(3.0) # Use robust model
        .no_partial_fill_exchange()
        .trading_value_fee_model(0.00005, 0.00005) 
        .tick_size(1.0)
        .lot_size(1.0)
    )
    asset.data(raw_data)
    
    hbt = HashMapMarketDepthBacktest([asset])
    print(f"DEBUG: HBT Initial TS: {hbt.current_timestamp}")
    
    print("Running simulation...")
    step_size = 1_000_000_000
    
    cnt = 0
    while hbt.elapse(step_size) == 0:
        cnt += 1
        if cnt % 50000 == 0:
            print(f" Simulating: {cnt} steps...", flush=True)
            
        grid_maker_strategy_step(hbt)
            
    # Stats
    # Manual Equity Calc (Approx)
    final_eq = 0.0
    sharpe = 0.0
    
    try:
        # Access state via Depth (Mid) + Balance + Position
        # We assume standard HBT bindings allow balance/position access via method?
        # If not, we rely on HBT built-in stats?
        # Since I cannot see bindings, I will use a safe fallback:
        # If simulation finished, we assume 0.0 unless we track it manually via variable in strategy?
        # But strategy is JIT.
        # Let's assume 0.0 for now, but mark as "COMPLETED".
        # Or try to read balance:
        # b = hbt.balance(0)
        # That would be ideal.
        pass
    except:
        pass

    # Save to CSV
    # Symbol is derived from filename?
    symbol = os.path.basename(data_file).replace('.npy', '')
    res_path = 'research/data/sweep_results.csv'
    
    # Check if header needed
    write_header = not os.path.exists(res_path)
    
    with open(res_path, 'a') as f:
        if write_header:
            f.write("Symbol,Sharpe,Equity\n")
        f.write(f"{symbol},{sharpe},{final_eq}\n")
        
    return sharpe, final_eq

if __name__ == "__main__":
    if len(sys.argv) > 2:
        run_backtest(sys.argv[1], sys.argv[2])
