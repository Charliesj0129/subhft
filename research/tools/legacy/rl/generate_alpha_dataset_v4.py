
import polars as pl
import numpy as np
import os
import sys

# Add path to find batches
sys.path.append(os.getcwd())
try:
    from research.alphas.batch_002_papers import compute_batch_002
    from research.alphas.batch_003_physics import compute_batch_003
    from research.alphas.batch_004_thermo import compute_batch_004
except ImportError:
    print("Error importing batches. Ensure you are in the project root.")
    sys.exit(1)

DATA_PATH = 'research/data/market_data_backup.parquet'
OUTPUT_BASE = 'research/data/hbt_multiproduct/TXFB6.npy' 

def generate_v4():
    print(f"Loading Raw Data from {DATA_PATH}...")
    try:
        df = pl.read_parquet(DATA_PATH)
        df = df.filter(pl.col("symbol").str.contains("TXF")).sort("exch_ts")
        
        # Flatten logic (similar to v3)
        if "bid_px_0" in df.columns:
            pass
        elif "bids_price" in df.columns:
             print("Flattening bids_price list column (keeping lists for Batch 004)...")
             
             # Filter out empty lists and Nulls
             df = df.drop_nulls(subset=["bids_price", "asks_price"])
             df = df.filter(pl.col("bids_price").list.len() > 0)
             df = df.filter(pl.col("asks_price").list.len() > 0)
             
             # Double check to ensure we didn't miss anything with strange types
             # We use list.first() which is often safer/more idiomatic for head
             df = df.with_columns([
                 pl.col("bids_price").list.get(0).alias("bid_px_0"),
                 pl.col("bids_vol").list.get(0).alias("bid_qty_0"),
                 pl.col("asks_price").list.get(0).alias("ask_px_0"),
                 pl.col("asks_vol").list.get(0).alias("ask_qty_0"),
             ])
             
        # Compute Batches
        print("Computing Batch 002 (Foundational)...")
        df_b2 = compute_batch_002(df)
        
        print("Computing Batch 003 (Physics - Criticality)...")
        df_b3 = compute_batch_003(df)
        
        print("Computing Batch 004 (Physics - Thermodynamics)...")
        df_b4 = compute_batch_004(df)
        
        # Merge
        print("Merging Features (Standardizing TS to Int64)...")
        # Ensure join keys are consistent
        df_b2 = df_b2.with_columns(pl.col("exch_ts").cast(pl.Int64))
        df_b3 = df_b3.with_columns(pl.col("exch_ts").cast(pl.Int64))
        df_b4 = df_b4.with_columns(pl.col("exch_ts").cast(pl.Int64))
        
        features = df_b2.join(df_b3, on="exch_ts", how="left")
        features = features.join(df_b4, on="exch_ts", how="left")
        
        features = features.fill_null(0)
        
        # Select Features
        feature_cols = [
            # Batch 2
            "alpha_hawkes", "alpha_micro_dev", "alpha_ofi_i", "alpha_trend", "alpha_hurst",
            # Batch 3
            "alpha_smc", "alpha_criticality_cv", "alpha_smc_critical_interact",
            # Batch 4
            "alpha_lob_temp", "alpha_lob_entropy", "alpha_lob_chem_pot"
        ]
        
        final_mat = features.select(feature_cols).to_numpy().astype(np.float32)
        
        # --- CLEANING STEP ---
        # 1. Handle NaNs/Infs
        final_mat = np.nan_to_num(final_mat, nan=0.0, posinf=100.0, neginf=-100.0)
        
        # 2. Clipping
        # Hurst (Col 4)
        final_mat[:, 4] = np.clip(final_mat[:, 4], 0, 10)
        # Criticality (Col 6)
        final_mat[:, 6] = np.clip(final_mat[:, 6], 0, 20)
        # Temp (Col 8) -> Can be 3000. Let's log-transform it? Or clip.
        # Temp = 1/beta. High temp = Flat. 
        # Let's clip to 500.
        final_mat[:, 8] = np.clip(final_mat[:, 8], 0, 500)
        
        # Global Clip
        final_mat = np.clip(final_mat, -1e5, 1e5)
        
        print(f"Final Feature Matrix Shape: {final_mat.shape} (V4)")
        
        # Save
        out_path = OUTPUT_BASE.replace('.npy', '_features_v4.npy')
        print(f"Saving to {out_path}...")
        np.save(out_path, final_mat)
        
        # Stats
        print("Stats:")
        print(features.select(feature_cols).describe())
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    generate_v4()
