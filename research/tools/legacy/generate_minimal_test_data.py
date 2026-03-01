import numpy as np
import os
from pathlib import Path

def generate_minimal_data(output_path):
    print(f"Generating minimal test data to {output_path}...")
    n_ticks = 2000
    levels = 5
    
    # Timestamps (seconds * 1e9 for ns)
    timestamps = np.arange(n_ticks) * 1e9 * 0.1 # 10 ticks/sec
    
    # Random Walk Price
    mid = 100.0 + np.cumsum(np.random.normal(0, 0.01, size=n_ticks))
    
    # Bid/Ask
    spread = 0.05
    bid_p0 = mid - spread/2
    ask_p0 = mid + spread/2
    
    # Replicate for levels
    bid_prices = np.zeros((n_ticks, levels))
    ask_prices = np.zeros((n_ticks, levels))
    bid_volumes = np.ones((n_ticks, levels)) * 100
    ask_volumes = np.ones((n_ticks, levels)) * 100
    
    for i in range(levels):
        bid_prices[:, i] = bid_p0 - i*0.01
        ask_prices[:, i] = ask_p0 + i*0.01
        
    # Trades
    trade_vol = np.abs(np.random.normal(10, 5, size=n_ticks))
    trade_side = np.sign(np.random.normal(0, 1, size=n_ticks))
    trade_price = mid # Approx
    
    data = {
        "timestamp": timestamps,
        "bid_prices": bid_prices,
        "ask_prices": ask_prices,
        "bid_volumes": bid_volumes,
        "ask_volumes": ask_volumes,
        "trade_volume": trade_vol,
        "trade_side": trade_side,
        "trade_price": trade_price,
        "mid_price": mid # Optional but helpful
    }
    
    Path(os.path.dirname(output_path)).mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output_path, **data)
    print("Done.")

if __name__ == "__main__":
    generate_minimal_data("research/data/batch9_test.npz")
