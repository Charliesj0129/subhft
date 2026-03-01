
import numpy as np
import pandas as pd
from numba import njit

# Vectorized (Robust Numba) Implementation

def build_features():
    in_path = 'research/data/hbt_multiproduct/TXFB6.npy'
    out_path = 'research/data/hbt_multiproduct/TXFB6_features.npy'
    
    print(f"Loading {in_path}...")
    try:
        loaded = np.load(in_path)
        if isinstance(loaded, np.lib.npyio.NpzFile):
            raw = loaded['data']
        else:
            raw = loaded
    except Exception as e:
        print(f"Error loading: {e}")
        return

    # Structured Array Check
    if not raw.dtype.names:
        print("Error: Expected structured array for vectorized access")
        return
        
    print(f"Data Loaded. Shape: {len(raw)}")
    
    max_px = np.max(raw['px'])
    print(f"Max Price in Data: {max_px}")
    
    # Run Numba
    run_numba_robust(raw, out_path, int(max_px) + 2000)

@njit(boundscheck=True)
def compute_features_robust(data, output_len, max_price):
    # data: [ev, ts, local, side, px, qty]
    features = np.zeros((output_len, 8), dtype=np.float32)
    
    bids = np.zeros(max_price, dtype=np.float32)
    asks = np.zeros(max_price, dtype=np.float32)
    
    current_mid = 0.0
    last_trade_px = 0.0
    
    best_bid = 0
    best_ask = max_price - 1
    
    for i in range(len(data)):
        ev = int(data[i, 0])
        side = int(data[i, 3])
        px = int(data[i, 4])
        qty = data[i, 5]
        
        # Safety Check
        if px >= max_price or px < 0:
            continue
            
        if ev == 1 or ev == 4: # Depth Update or Snapshot
            if side == 1:
                bids[px] = qty
                # Update Best Bid (Simplified)
                if qty > 0:
                    if px > best_bid: best_bid = px
                else:
                    if px == best_bid:
                        # Find new best bid
                        best_bid = 0
                        for k in range(px-1, 0, -1):
                            if bids[k] > 0:
                                best_bid = k
                                break
            elif side == -1:
                asks[px] = qty
                # Update Best Ask
                if qty > 0:
                    if px < best_ask: best_ask = px
                else:
                    if px == best_ask:
                        # Find new best ask
                        best_ask = max_price - 1
                        for k in range(px+1, max_price):
                            if asks[k] > 0:
                                best_ask = k
                                break
                            
        elif ev == 2: # Trade
            last_trade_px = px
            
        # Calc Features
        # Valid BBO?
        if best_bid > 0 and best_ask < max_price and best_ask > best_bid:
            current_mid = (best_bid + best_ask) / 2.0
            
            # Imbalances L1
            q_b_1 = bids[best_bid]
            q_a_1 = asks[best_ask]
            imb_l1 = (q_b_1 - q_a_1) / (q_b_1 + q_a_1 + 1e-5)
            
            # L3, L4, L5 Imbalance (Approximation by scanning)
            # Find Levels
            # Bid Levels
            b_l3 = 0.0
            b_l4 = 0.0
            b_l5 = 0.0
            
            found = 0
            for k in range(best_bid, 0, -1):
                if bids[k] > 0:
                    found += 1
                    if found == 3: b_l3 = bids[k]
                    if found == 4: b_l4 = bids[k]
                    if found == 5: 
                        b_l5 = bids[k]
                        break
            
            # Ask Levels
            a_l3 = 0.0
            a_l4 = 0.0
            a_l5 = 0.0
            found = 0
            for k in range(best_ask, max_price):
                if asks[k] > 0:
                    found += 1
                    if found == 3: a_l3 = asks[k]
                    if found == 4: a_l4 = asks[k]
                    if found == 5:
                        a_l5 = asks[k]
                        break
            
            # Compute Imb
            imb_l3 = (b_l3 - a_l3) / (b_l3 + a_l3 + 1e-5)
            imb_l4 = (b_l4 - a_l4) / (b_l4 + a_l4 + 1e-5)
            imb_l5 = (b_l5 - a_l5) / (b_l5 + a_l5 + 1e-5)
            
            # Momentum
            mid_mom = 0.0
            if last_trade_px > 0:
                mid_mom = last_trade_px - current_mid
            
            features[i, 0] = imb_l3
            features[i, 1] = imb_l4
            features[i, 2] = imb_l5
            features[i, 3] = mid_mom
            features[i, 4] = imb_l1
            
    return features

def run_numba_robust(raw, out_path, max_px):
    print("Preparing data...")
    N = len(raw)
    data = np.zeros((N, 6), dtype=np.float64)
    data[:, 0] = raw['ev']
    # exch_ts might be large, float64 handles distinct timestamps okay-ish for sorting but here just passing through
    data[:, 1] = raw['exch_ts'] 
    data[:, 3] = raw['ival']
    data[:, 4] = raw['px']
    data[:, 5] = raw['qty']
    
    print(f"Computing Robust (MaxPx={max_px})...")
    # Call Numba
    feats = compute_features_robust(data, N, max_px)
    
    print(f"Saving to {out_path}...")
    np.save(out_path, feats)
    print("Done.")

if __name__ == '__main__':
    build_features()
