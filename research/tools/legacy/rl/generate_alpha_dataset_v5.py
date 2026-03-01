
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
    from research.alphas.alpha_dualformer import compute_alpha_dualformer
    from research.alphas.alpha_integrity import compute_alpha_integrity
    from research.alphas.alpha_stealth import compute_alpha_stealth
except ImportError as e:
    print(f"Error importing alphas: {e}")
    print("Ensure you are in the project root and all alpha scripts exist.")
    sys.exit(1)

DATA_PATH = 'research/data/market_data_backup.parquet'
OUTPUT_BASE = 'research/data/hbt_multiproduct/TXFB6.npy' 

import gc 

def generate_v5():
    print(f"Loading Raw Data from {DATA_PATH}...")
    try:
        # Load Raw Data
        df = pl.read_parquet(DATA_PATH)
        df = df.filter(pl.col("symbol").str.contains("TXF")).sort("exch_ts")
        
        # Flatten logic (same as before)
        if "bids_price" in df.columns:
             print("Flattening L1 for Alphas...")
             df = df.drop_nulls(subset=["bids_price", "asks_price"])
             df = df.filter(pl.col("bids_price").list.len() > 0)
             df = df.filter(pl.col("asks_price").list.len() > 0)
             
             df = df.with_columns([
                 pl.col("bids_price").list.get(0).alias("bid_px_0"),
                 pl.col("bids_vol").list.get(0).alias("bid_qty_0"),
                 pl.col("asks_price").list.get(0).alias("ask_px_0"),
                 pl.col("asks_vol").list.get(0).alias("ask_qty_0"),
             ])
             df = df.with_columns([
                 ((pl.col("bid_px_0") + pl.col("ask_px_0")) / 2.0).alias("mid_price"),
                 (pl.col("bid_qty_0") + pl.col("ask_qty_0")).alias("volume")
             ])
        
        # --- Memory Optimized Pipeline ---
        # Strategy: Compute -> Select -> Merge -> Free -> GC
        
        # 1. Base: Batch 002
        print("1. Computing Batch 002 (Foundational)...")
        features = compute_batch_002(df)
        cols_b2 = ["exch_ts", "alpha_hawkes", "alpha_micro_dev", "alpha_ofi_i", "alpha_trend", "alpha_hurst"]
        features = features.select([c for c in cols_b2 if c in features.columns])
        features = features.with_columns(pl.col("exch_ts").cast(pl.Int64))
        print(f"Base Features: {features.shape}")
        gc.collect()

        # Helper for Chunked Computation (for Heavy Batches like Thermo/Dualformer)
        def chunked_compute(func, input_df, chunk_size=500_000):
            print(f"  Chunked Compute: {chunk_size} rows/chunk...")
            n_rows = len(input_df)
            chunks = []
            
            # Use slice to create chunks (zero-copy if possible in polars, but we need to execute inputs)
            # Polars `slice` is cheap.
            
            for start in range(0, n_rows, chunk_size):
                end = min(start + chunk_size, n_rows)
                # Slice
                # Note: Some alphas (Dualformer/Hawkes) need history.
                # If we simple slice, we break history at boundaries.
                # However, Batch 4 (Thermo) is point-in-time (snapshot). Safe to chunk.
                # Cluster 4 (Dualformer) uses filters/rolling. Chunking breaks it unless we overlap.
                # Let's use chunking only for Point-in-Time alphas (Thermo).
                # For Dualformer, we might strictly need the full series. 
                # Or we overlap by WINDOW size.
                
                chunk_df = input_df.slice(start, end - start)
                res = func(chunk_df)
                chunks.append(res)
                
                # GC
                del chunk_df
                del res
                # gc.collect() # Optional inside loop
            
            # Vstack
            print("  Stacking chunks...")
            full_res = pl.concat(chunks)
            return full_res

        def iterative_merge(main_df, batch_func, name, cols, use_chunking=False):
            print(f"Processing {name}...")
            # Compute
            if use_chunking:
                tmp_df = chunked_compute(batch_func, df, chunk_size=500_000)
            else:
                tmp_df = batch_func(df) # Uses raw 'df'
            
            # Select only needed
            avail = [c for c in cols if c in tmp_df.columns]
            missing = set(cols) - set(avail)
            if missing:
                print(f"  Warning: {name} missing {missing}. Filling 0.")
                tmp_df = tmp_df.with_columns([pl.lit(0.0).alias(c) for c in missing])
            
            tmp_df = tmp_df.select(cols)
            tmp_df = tmp_df.with_columns(pl.col("exch_ts").cast(pl.Int64))
            
            # Merge
            print(f"  Merging {name}...")
            main_df = main_df.join(tmp_df, on="exch_ts", how="left")
            
            # Cleanup
            del tmp_df
            gc.collect()
            return main_df

        # 2. Batch 3
        cols_b3 = ["exch_ts", "alpha_smc", "alpha_criticality_cv", "alpha_smc_critical_interact"]
        features = iterative_merge(features, compute_batch_003, "Batch 003 (Physics)", cols_b3, use_chunking=False)
        
        # 3. Batch 4 (Thermo is Point-in-Time -> SAFE to chunk)
        cols_b4 = ["exch_ts", "alpha_lob_temp", "alpha_lob_entropy", "alpha_lob_chem_pot"]
        features = iterative_merge(features, compute_batch_004, "Batch 004 (Thermo)", cols_b4, use_chunking=True)
        
        # 4. Cluster 4 (Dualformer uses Rolling/Filters -> NOT safe to chunk without overlap)
        # We run it full memory. If crash, we need OverlapChunking. 
        # But Thermo was likely the culprit (vectorized lists).
        cols_c4 = ["exch_ts", "alpha_dual_hf", "alpha_dual_lf", "alpha_dual_energy"]
        features = iterative_merge(features, compute_alpha_dualformer, "Cluster 4 (Dualformer)", cols_c4, use_chunking=False)
        
        # 5. Integrity (Rolling -> No Chunk)
        cols_c5_int = ["exch_ts", "alpha_integrity_vol_z", "alpha_integrity_div", "alpha_integrity_score"]
        features = iterative_merge(features, compute_alpha_integrity, "Cluster 5 (Integrity)", cols_c5_int, use_chunking=False)
        
        # 6. Stealth (Rolling -> No Chunk)
        cols_c5_stl = ["exch_ts", "alpha_clustering_score", "alpha_stealth_ratio", "alpha_camouflaged_flow"]
        features = iterative_merge(features, compute_alpha_stealth, "Cluster 5 (Stealth)", cols_c5_stl, use_chunking=False)
        
        # Fill Nulls 
        features = features.fill_null(0)
        
        # Final Selection (Exclude exch_ts for Matrix)
        # Note: We must ensure order is consistent if we rely on index.
        # But we are selecting by name below.
        
        feature_list = [
            "alpha_hawkes", "alpha_micro_dev", "alpha_ofi_i", "alpha_trend", "alpha_hurst",
            "alpha_smc", "alpha_criticality_cv", "alpha_smc_critical_interact",
            "alpha_lob_temp", "alpha_lob_entropy", "alpha_lob_chem_pot",
            "alpha_dual_hf", "alpha_dual_lf", "alpha_dual_energy",
            "alpha_integrity_vol_z", "alpha_integrity_div", "alpha_integrity_score",
            "alpha_clustering_score", "alpha_stealth_ratio", "alpha_camouflaged_flow"
        ]
        
        print(f"Selected {len(feature_list)} features for V5.")
        
        # Check integrity
        final_mat = features.select(feature_list).to_numpy().astype(np.float32)
        
        # --- CLEANING & CLIPPING ---
        final_mat = np.nan_to_num(final_mat, nan=0.0, posinf=100.0, neginf=-100.0)
        final_mat = np.clip(final_mat, -1e6, 1e6)
        # Temp Clip (Index 8)
        # 5 (B2) + 3 (B3) + Index 0 of B4 = 8
        final_mat[:, 8] = np.clip(final_mat[:, 8], 0, 500)
        
        print(f"Final V5 Matrix Shape: {final_mat.shape}")
        
        # Save Features
        out_path = OUTPUT_BASE.replace('.npy', '_features_v5.npy')
        np.save(out_path, final_mat)
        print(f"Saved V5 Dataset to {out_path}")
        
        # Free memory before raw processing
        del final_mat
        gc.collect()
        
        # Save Raw Base
        print("Constructing Raw Base...")
        ts_arr = features["exch_ts"].to_numpy().astype(np.float64)
        
        # Join price from original df (efficiently)
        df_px = df.select(["exch_ts", "mid_price"]).with_columns(pl.col("exch_ts").cast(pl.Int64))
        # We need to join back to features to match rows (in case joins dropped/aligned?)
        # Actually our iterative merge used Left Join on Batch2 features.
        # Batch 2 features come from compute_batch_002(df).
        # Assuming batch_002 keeps all rows or drops some.
        # Safest is to join df_px to features.
        
        features_px = features.select("exch_ts").join(df_px, on="exch_ts", how="left")
        px_arr = features_px["mid_price"].fill_null(strategy="forward").fill_null(0.0).to_numpy().astype(np.float32)
        
        raw_base = np.zeros((len(ts_arr), 5), dtype=np.float32)
        raw_base[:, 0] = 2.0 
        raw_base[:, 1] = ts_arr
        raw_base[:, 4] = px_arr
        
        np.save(OUTPUT_BASE, raw_base)
        print(f"Saved Raw Base to {OUTPUT_BASE}")
        
    except Exception as e:
        print(f"Generation Failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    generate_v5()
