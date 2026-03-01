
import numpy as np
import os
import sys
from adaptive_dataflow import DataFlowAugmentor

# Add path
sys.path.append(os.getcwd())

import polars as pl

# Paths
INPUT_FEAT = 'research/data/hbt_multiproduct/TXFB6_features_v4.npy'
INPUT_RAW = 'research/data/market_data_backup.parquet'
OUTPUT_RAW = 'research/data/hbt_multiproduct/TXFB6_augmented.npy' 
# HftEnv logic: data_path.replace('.npy', '_features.npy')
OUTPUT_FEAT = 'research/data/hbt_multiproduct/TXFB6_augmented_features.npy'

def generate_augmented():
    print(f"Loading V4 Features from {INPUT_FEAT}...")
    if not os.path.exists(INPUT_FEAT):
        print(f"Error: {INPUT_FEAT} not found.")
        return
        
    features = np.load(INPUT_FEAT)
    print(f"Features Shape: {features.shape}")

    print(f"Loading Raw Prices from {INPUT_RAW}...")
    df = pl.read_parquet(INPUT_RAW)
    df = df.filter(pl.col("symbol").str.contains("TXF")).sort("exch_ts")
    
    # Extract Price/TS
    # Mimic V4 flattening logic to ensuring alignment
    if "bids_price" in df.columns:
         df = df.drop_nulls(subset=["bids_price", "asks_price"])
         df = df.filter(pl.col("bids_price").list.len() > 0)
         df = df.filter(pl.col("asks_price").list.len() > 0)
         
         b0 = df["bids_price"].list.get(0).fill_null(0.0).to_numpy()
         a0 = df["asks_price"].list.get(0).fill_null(0.0).to_numpy()
         prices = (b0 + a0) / 2.0
    elif "mid_price" in df.columns:
         prices = df["mid_price"].to_numpy()
    else:
         prices = np.zeros(len(df)) # Should not happen
         
    timestamps = df["exch_ts"].to_numpy().astype(np.float64) # Float for processing
    
    # Alignment Check
    # Features might be slightly different if nulls were dropped differently?
    # generate_alpha_dataset_v4 used fill_null(0) on features.
    # It joined batches.
    # The feature matrix row count should match the raw df row count IF batch processing didn't drop rows.
    # V4 logic: df -> compute batches -> join.
    # If Batches drop rows (e.g. rolling window NaN), features will be shorter.
    # Batch 2/3/4 usually preserve length or fill null.
    
    min_len = min(len(features), len(prices))
    print(f" aligning len: {min_len} (Feat: {len(features)}, Price: {len(prices)})")
    
    features = features[:min_len]
    prices = prices[:min_len]
    timestamps = timestamps[:min_len]
    
    # ---------------------------------------------------------
    # Augmentation
    # ---------------------------------------------------------
    augmentor = DataFlowAugmentor(rng_seed=42)
    
    # We must augment (Price, Features) together to keep them consistent.
    # Stack them for processing?
    # Shape: (N, F+1)
    
    # 1. Scaling Helper
    def augment_pair(px, feats, type='none'):
        # Stack
        # feats: (N, D), px: (N, )
        # Combined: (N, D+1)
        combined = np.column_stack([px, feats])
        
        if type == 'wrap':
            # Magnitude Warp
            res = augmentor.magnitude_warping(combined, sigma=0.2, knots=10)
        elif type == 'jitter':
            res = augmentor.jitter(combined, sigma=0.05)
        elif type == 'crash':
            # Crash: Scale down heavily
            res = augmentor.scaling(combined, sigma=0.4) 
        else:
            res = combined
            
        return res[:, 0], res[:, 1:]

    print("Generating Augmentations...")
    
    # List of chunks
    all_px = [prices]
    all_feats = [features]
    all_ts = [timestamps]
    all_ev = [np.full(len(prices), 2, dtype=np.float32)] # 2 = Trade
    
    # Warped
    print("- Warped...")
    p_w, f_w = augment_pair(prices, features, 'wrap')
    all_px.append(p_w)
    all_feats.append(f_w)
    all_ts.append(timestamps) # Keep TS same (Parallel Universe)
    all_ev.append(np.full(len(p_w), 2))
    
    # Jittered
    print("- Jittered...")
    p_j, f_j = augment_pair(prices, features, 'jitter')
    all_px.append(p_j)
    all_feats.append(f_j)
    all_ts.append(timestamps)
    all_ev.append(np.full(len(p_j), 2))
    
    # Crashed
    print("- Crashed...")
    p_c, f_c = augment_pair(prices, features, 'crash')
    all_px.append(p_c)
    all_feats.append(f_c)
    all_ts.append(timestamps)
    all_ev.append(np.full(len(p_c), 2))
    
    # Concatenate
    final_px = np.concatenate(all_px)
    final_feats = np.concatenate(all_feats)
    final_ts = np.concatenate(all_ts)
    final_ev = np.concatenate(all_ev)
    
    print(f"Final Augmented Shape: {final_px.shape}")
    
    # Save Feats
    np.save(OUTPUT_FEAT, final_feats)
    print(f"Saved Features to {OUTPUT_FEAT}")
    
    # Save Raw Data Structure (for HftEnv)
    # HftEnv expects: [ev, ts, x, x, px, ...] or structured.
    # Unstructured fallback: col 0=ev, col 1=ts, col 4=px.
    # Let's build (N, 5) array: [ev, ts, 0, 0, px]
    
    raw_structure = np.zeros((len(final_px), 5), dtype=np.float32)
    raw_structure[:, 0] = final_ev # ev
    raw_structure[:, 1] = final_ts # ts
    raw_structure[:, 4] = final_px # px
    
    np.save(OUTPUT_RAW, raw_structure)
    print(f"Saved Raw Mock to {OUTPUT_RAW}")
    
if __name__ == "__main__":
    generate_augmented()
