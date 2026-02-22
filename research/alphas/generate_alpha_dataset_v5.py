
import polars as pl
import glob
import os
from research.alphas.alpha_h0_hurst import calculate_h0_alpha
from research.alphas.alpha_lob_shear import calculate_shear_alpha
# Import legacy alphas (Cluster 1) to maintain dataset continuity
from research.alphas.batch_003_physics import compute_batch_003

OUTPUT_DIR = "research/data/v5_datasets"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def generate_v5_dataset(files_pattern: str):
    """
    Generates V5 Dataset:
    - Includes Cluster 1 (Physics/Criticality)
    - Includes Cluster 2026 (H0, Shear)
    """
    files = glob.glob(files_pattern)
    print(f"Found {len(files)} files to process for V5.")
    
    for f in files:
        print(f"Processing {f}...")
        try:
            df = pl.read_parquet(f)
            
            # 1. Base Preprocessing (Ensure format)
            if "bids_price" not in df.columns and "bid_price" in df.columns:
                # Fix column names if needed or adapt
                pass
                
            # 2. Apply Cluster 1 Alphas (Baseline)
            # MarketCap=1.0 for generic scaling
            df = compute_batch_003(df, market_cap=1.0)
            
            # 3. Apply Cluster 2026 Alphas (New Frontiers)
            print("  - Calculating H0 Hurst...")
            df = calculate_h0_alpha(df, window=1000)
            
            print("  - Calculating Geometric Shear...")
            df = calculate_shear_alpha(df)
            
            # 4. Save
            out_name = os.path.basename(f).replace(".parquet", "_v5.parquet")
            out_path = os.path.join(OUTPUT_DIR, out_name)
            df.write_parquet(out_path)
            print(f"  -> Saved to {out_path}")
            
        except Exception as e:
            print(f"ERROR processing {f}: {e}")

if __name__ == "__main__":
    # Point to your raw data source
    # For now, using the backup test file
    TARGET_DATA = "research/data/market_data_backup.parquet"
    generate_v5_dataset(TARGET_DATA)
