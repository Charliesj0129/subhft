import numpy as np
import sys
import os

# Path setup
sys.path.append("/home/charlie/hft_platform/external_repos/hftbacktest_fresh/py-hftbacktest")
from hftbacktest import DEPTH_EVENT, TRADE_EVENT, DEPTH_SNAPSHOT_EVENT
from hftbacktest.types import event_dtype

def generate_correlated():
    print("Generating correlated synthetic data (TXF & MXF)...")
    num_events = 20000
    
    # Common Factor Process
    # We generate a common random walk, then add noise for each asset
    
    tick = 1.0
    
    # Initial Price
    price_txf = 10000.0
    price_mxf = 10000.0
    
    ts = 1_600_000_000_000_000_000
    
    data_txf = []
    data_mxf = []
    
    # Pre-allocate roughly? Using list and converting later is easier for non-uniform events
    # But let's stick to uniform 1ms steps for simplicity, but random events.
    
    for i in range(num_events):
        ts += 1_000_000 # 1ms
        
        # Correlated Innovation
        common_shock = np.random.normal(0, 1)
        
        # TXF leads?
        # Let's say TXF has 0.8 correlation with common, MXF has 0.8 but lagged? 
        # For simplicity: perfectly synchronous correlation for now.
        
        price_txf += 0.05 * common_shock + np.random.normal(0, 0.2)
        price_mxf += 0.05 * common_shock + np.random.normal(0, 0.2)
        
        # Round to tick
        mid_txf = round(price_txf)
        mid_mxf = round(price_mxf)
        
        # Create events randomly
        
        # Asset 0: TXF
        if np.random.random() < 0.2:
            ev = np.zeros(1, dtype=event_dtype)[0]
            ev['exch_ts'] = ts
            ev['local_ts'] = ts
            
            is_trade = np.random.random() < 0.1
            if is_trade:
                ev['ev'] = TRADE_EVENT
                ev['px'] = mid_txf + (tick * 0.5 if np.random.random() > 0.5 else -0.5 * tick)
                ev['qty'] = 1.0
                ev['ival'] = 1 if ev['px'] > mid_txf else -1
            else:
                ev['ev'] = DEPTH_EVENT
                ev['px'] = mid_txf - 0.5 * tick # Best Bid
                ev['qty'] = 10.0
            data_txf.append(ev)

        # Asset 1: MXF (Higher freq?)
        if np.random.random() < 0.2:
            ev = np.zeros(1, dtype=event_dtype)[0]
            ev['exch_ts'] = ts
            ev['local_ts'] = ts
            
            is_trade = np.random.random() < 0.1
            if is_trade:
                ev['ev'] = TRADE_EVENT
                ev['px'] = mid_mxf + (tick * 0.5 if np.random.random() > 0.5 else -0.5 * tick)
                ev['qty'] = 1.0
                ev['ival'] = 1 if ev['px'] > mid_mxf else -1
            else:
                ev['ev'] = DEPTH_EVENT
                ev['px'] = mid_mxf - 0.5 * tick # Best Bid
                ev['qty'] = 5.0
            data_mxf.append(ev)
            
    # Save
    if not os.path.exists("data"):
        os.makedirs("data")
        
    np.savez_compressed("data/synthetic_txf.npz", data=np.array(data_txf, dtype=event_dtype))
    np.savez_compressed("data/synthetic_mxf.npz", data=np.array(data_mxf, dtype=event_dtype))
    
    print(f"Saved {len(data_txf)} TXF events and {len(data_mxf)} MXF events.")

if __name__ == "__main__":
    generate_correlated()
