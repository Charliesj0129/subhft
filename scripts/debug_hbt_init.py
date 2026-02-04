
import numpy as np
import os
import sys
from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest
from hftbacktest.types import DEPTH_CLEAR_EVENT, DEPTH_SNAPSHOT_EVENT, TRADE_EVENT, BUY_EVENT

def main():
    # 1. Create Synthetic Data
    # 2 Events: Clear, Snapshot
    # Timestamp: 100, 200
    
    # Inspect all constants
    import hftbacktest.types as ht
    # Check values
    for name in dir(ht):
        if name.isupper():
            print(f"{name}: {getattr(ht, name)}")

    from hftbacktest.binding import event_dtype
    print(f"Official Dtype: {event_dtype}")
    
    # data = np.zeros(10, dtype=dtype)
    data = np.zeros(10, dtype=event_dtype)

    
    import hftbacktest.types as ht
    EXCH_EVENT = ht.EXCH_EVENT
    print(f"Adding EXCH_EVENT: {EXCH_EVENT}")
    
    # Create Snap Data (No clear, just snapshot)
    snap_data = np.zeros(1, dtype=event_dtype)
    snap_data[0]['ev'] = DEPTH_SNAPSHOT_EVENT | BUY_EVENT | EXCH_EVENT
    snap_data[0]['exch_ts'] = 1000
    snap_data[0]['local_ts'] = 1000
    snap_data[0]['px'] = 100.0
    snap_data[0]['qty'] = 1.0
    
    snap_file = "debug_snap.npz"
    np.savez_compressed(snap_file, data=snap_data)
    
    # Create Feed Data
    feed_data = np.zeros(1, dtype=event_dtype)
    feed_data[0]['ev'] = TRADE_EVENT | BUY_EVENT | EXCH_EVENT
    feed_data[0]['exch_ts'] = 2000
    feed_data[0]['local_ts'] = 2000
    feed_data[0]['px'] = 101.0
    feed_data[0]['qty'] = 1.0
    
    feed_file = "debug_feed.npz"
    np.savez_compressed(feed_file, data=feed_data)
    
    # Load
    snap_abs = os.path.abspath(snap_file)
    feed_abs = os.path.abspath(feed_file)
    
    print("Attempt 3: Separate Snapshot and Feed")
    try:
        asset = BacktestAsset().data([feed_abs]).initial_snapshot(snap_abs).linear_asset(1.0)
        hbt = HashMapMarketDepthBacktest([asset])
        print(f"HBT Init. TS: {hbt.current_timestamp}")
        
        # Elapse to feed
        hbt.elapse(2000)
        print(f"Elasped TS: {hbt.current_timestamp}")
        
        hbt.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
