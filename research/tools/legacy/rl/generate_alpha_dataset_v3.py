
import polars as pl
import numpy as np
import os
import sys

# Add path to find batches
sys.path.append(os.getcwd())
from research.alphas.batch_002_papers import compute_batch_002
from research.alphas.batch_003_physics import compute_batch_003

DATA_PATH = 'research/data/market_data_backup.parquet'
OUTPUT_BASE = 'research/data/hbt_multiproduct/TXFB6.npy' 
# We will write to OUTPUT_BASE.replace('.npy', '_features_v3.npy')

def generate_v3():
    print(f"Loading Raw Data from {DATA_PATH}...")
    try:
        df = pl.read_parquet(DATA_PATH)
        df = df.filter(pl.col("symbol").str.contains("TXF")).sort("exch_ts")
        
        # Flatten logic
        # Check if already flattened (bid_px_0 exists?)
        if "bid_px_0" in df.columns:
            print("Data already flattened.")
        elif "bids_price" in df.columns:
             print("Flattening bids_price list column...")
             # Filter out empty or null lists first
             df = df.filter(pl.col("bids_price").list.len() > 0)
             df = df.filter(pl.col("asks_price").list.len() > 0)
             
             df = df.with_columns([
                 pl.col("bids_price").list.get(0).alias("bid_px_0"),
                 pl.col("bids_vol").list.get(0).alias("bid_qty_0"),
                 pl.col("asks_price").list.get(0).alias("ask_px_0"),
                 pl.col("asks_vol").list.get(0).alias("ask_qty_0"),
             ])
        elif "bid_price" in df.columns and df.schema["bid_price"] == pl.List:
             print("Flattening bid_price list column...")
             df = df.filter(pl.col("bid_price").list.len() > 0)
             df = df.filter(pl.col("ask_price").list.len() > 0)
             
             df = df.with_columns([
                 pl.col("bid_price").list.get(0).alias("bid_px_0"),
                 pl.col("bid_volume").list.get(0).alias("bid_qty_0"),
                 pl.col("ask_price").list.get(0).alias("ask_px_0"),
                 pl.col("ask_volume").list.get(0).alias("ask_qty_0"),
             ])
        else:
             # Assume flattened but named differently?
             # Or maybe it's bid_price but Float?
             if "bid_price" in df.columns and df.schema["bid_price"] != pl.List:
                  # Already flat
                  df = df.rename({
                      "bid_price": "bid_px_0",
                      "bid_volume": "bid_qty_0",
                      "ask_price": "ask_px_0",
                      "ask_volume": "ask_qty_0"
                  })
             else:
                  print(f"Unknown Schema: {df.columns}")
                  return
             
        # Compute Batches
        print("Computing Batch 002 (Foundational)...")
        df_b2 = compute_batch_002(df)
        
        print("Computing Batch 003 (Physics)...")
        df_b3 = compute_batch_003(df)
        
        # Join on exch_ts
        # Note: Polars join might require unique keys or we can just hstack if lengths identical?
        # compute_batch functions return SELECT exch_ts + features.
        # Since they run on same df (sorted), we can assume indices match if no filtering happened inside.
        # But safest is join.
        
        print("Merging Features...")
        features = df_b2.join(df_b3, on="exch_ts", how="left")
        
        # Fill Nulls (e.g. from rolling windows)
        features = features.fill_null(0)
        
        # Select Feature Columns strictly
        # Batch 2: alpha_hawkes, alpha_micro_dev, alpha_ofi_i, alpha_trend, alpha_hurst
        # Batch 3: alpha_smc, alpha_criticality_cv, alpha_smc_critical_interact
        
        feature_cols = [
            "alpha_hawkes", "alpha_micro_dev", "alpha_ofi_i", "alpha_trend", "alpha_hurst",
            "alpha_smc", "alpha_criticality_cv", "alpha_smc_critical_interact"
        ]
        
        final_mat = features.select(feature_cols).to_numpy().astype(np.float32)
        
        # --- CLEANING STEP ---
        # 1. Handle NaNs and Infs (Replace with bounds)
        final_mat = np.nan_to_num(final_mat, nan=0.0, posinf=100.0, neginf=-100.0)
        
        # 2. Robust Clipping
        # Hurst (Col 4) explodes -> Clip to [0, 10]
        final_mat[:, 4] = np.clip(final_mat[:, 4], 0, 10)
        
        # Criticality (Col 6) -> Clip to [0, 20]
        final_mat[:, 6] = np.clip(final_mat[:, 6], 0, 20)
        
        # Global Clip for safety
        final_mat = np.clip(final_mat, -1e5, 1e5)
        
        print(f"Final Feature Matrix Shape: {final_mat.shape} (Cleaned)")
        
        # Save
        out_path = OUTPUT_BASE.replace('.npy', '_features_v3.npy')
        print(f"Saving to {out_path}...")
        np.save(out_path, final_mat)
        
        # Validation
        print("Stats:")
        print(features.select(feature_cols).describe())
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    generate_v3()
