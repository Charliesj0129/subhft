#!/usr/bin/env python3
"""
Convert Heston-Hawkes LOB data to hftbacktest format.

hftbacktest expects structured numpy array with columns:
- ev: event type (0=trade, 1=bid add, 2=ask add, 3=bid delete, 4=ask delete)
- exch_ts: exchange timestamp (nanoseconds)
- local_ts: local timestamp (nanoseconds)
- px: price (float)
- qty: quantity (float)
"""

from __future__ import annotations

import argparse
from typing import Dict
import numpy as np


# hftbacktest event types
EV_TRADE = 0
EV_BID_ADD = 1
EV_ASK_ADD = 2
EV_BID_DELETE = 3
EV_ASK_DELETE = 4


def convert_to_hft_format(data: Dict[str, np.ndarray]) -> np.ndarray:
    """
    Convert Heston-Hawkes LOB data to hftbacktest format.
    
    Input data keys:
    - timestamp: event times (normalized days)
    - bid_prices: (N, 5) bid prices
    - bid_volumes: (N, 5) bid volumes
    - ask_prices: (N, 5) ask prices
    - ask_volumes: (N, 5) ask volumes
    - trade_price: trade prices
    - trade_volume: trade volumes
    - trade_side: trade sides (1=buy, -1=sell, 0=no trade)
    
    Output: structured numpy array with hftbacktest columns
    """
    timestamps = data["timestamp"]
    bid_p = data["bid_prices"]
    bid_v = data["bid_volumes"]
    ask_p = data["ask_prices"]
    ask_v = data["ask_volumes"]
    trade_p = data["trade_price"]
    trade_v = data["trade_volume"]
    trade_side = data["trade_side"]
    
    n_events = len(timestamps)
    
    # Convert normalized days to nanoseconds (assume 4.5hr/day = 16200 seconds)
    ns_per_day = 16200 * 1e9
    ts_ns = (timestamps * ns_per_day).astype(np.int64)
    
    # Estimate total events: trades + LOB updates (5 levels * 2 sides = 10 per snapshot)
    # For simplicity, we'll create one LOB snapshot + optional trade per event
    max_events = n_events * 12  # 5 bid + 5 ask + 1 trade + buffer
    
    # Create structured array
    dtype = np.dtype([
        ('ev', np.int32),
        ('exch_ts', np.int64),
        ('local_ts', np.int64),
        ('px', np.float64),
        ('qty', np.float64),
    ])
    
    output = np.zeros(max_events, dtype=dtype)
    idx = 0
    
    for i in range(n_events):
        ts = ts_ns[i]
        
        # Add bid levels (L1 to L5)
        for lvl in range(min(5, bid_p.shape[1])):
            if bid_v[i, lvl] > 0:
                output[idx] = (EV_BID_ADD, ts, ts, bid_p[i, lvl], bid_v[i, lvl])
                idx += 1
        
        # Add ask levels
        for lvl in range(min(5, ask_p.shape[1])):
            if ask_v[i, lvl] > 0:
                output[idx] = (EV_ASK_ADD, ts, ts, ask_p[i, lvl], ask_v[i, lvl])
                idx += 1
        
        # Add trade if present
        if trade_v[i] > 0:
            output[idx] = (EV_TRADE, ts, ts, trade_p[i], trade_v[i])
            idx += 1
    
    # Trim to actual size
    output = output[:idx]
    
    # Sort by timestamp
    output.sort(order='exch_ts')
    
    return output


def main():
    parser = argparse.ArgumentParser(description="Convert LOB data to hftbacktest format")
    parser.add_argument("--input", type=str, required=True, help="Input .npz file")
    parser.add_argument("--output", type=str, required=True, help="Output .npz file")
    args = parser.parse_args()
    
    print(f"[Converter] Loading {args.input}...")
    data = dict(np.load(args.input))
    
    print("[Converter] Converting to hftbacktest format...")
    hft_data = convert_to_hft_format(data)
    
    print(f"[Converter] Generated {len(hft_data)} events")
    print(f"  Event types: trades={np.sum(hft_data['ev'] == EV_TRADE)}, "
          f"bid_add={np.sum(hft_data['ev'] == EV_BID_ADD)}, "
          f"ask_add={np.sum(hft_data['ev'] == EV_ASK_ADD)}")
    
    np.savez_compressed(args.output, data=hft_data)
    print(f"[Saved] {args.output}")


if __name__ == "__main__":
    main()
