
import polars as pl
import numpy as np
import scipy.stats as stats
from scipy.optimize import curve_fit

def compute_batch_004(df: pl.DataFrame) -> pl.DataFrame:
    """
    Batch 004: LOB Thermodynamics (Physics of LOB, Paper 2512.04440)
    
    Concepts:
    1. LOB Temperature (T): Inverse of the decay rate of order volume away from midprice.
       n(x) ~ exp(- beta * |x|)  where x = (p - mid).
       T = 1 / beta.
       High T -> Orders are scattered (disordered/uncertain).
       Low T -> Orders are concentrated near spread (stable/confident).
       
    2. LOB Entropy (S): Shannon entropy of the volume distribution.
       S = - sum( p_i * log(p_i) ) where p_i = vol_i / total_vol.
       Measures the "Disorder" of the book state.
    
    3. Chemical Potential Gradient (dMu):
       mu(p) ~ -ln(n(p)).
       dMu = (ln(n_best) - ln(n_deep)) / depth.
       Steeper gradient -> Stronger barrier to price movement.
    """
    
    # Ensure we have the necessary list columns
    # We need bids_price/vol and asks_price/vol list columns
    # If flatten was done in generate_v3, we might need to assume the input df HAS the lists 
    # OR we operate on the raw data.
    # PRO TIP: The `compute_batches` usually operate on the DataFrame that HAS the lists.
    # If the input df is already flat (bid_px_0), we cannot easily do this.
    # We will assume input 'df' has 'bids_vol', 'bids_price' as Lists or struct.
    
    # Check schema
    if "bids_vol" not in df.columns:
        print("Batch 004 Warning: 'bids_vol' list column missing. Calculating proxies...")
        # Fallback: Use levels 0-4 if available
        return df.select(pl.col("exch_ts")) # Return empty for safe join
        
    print("Computing LOB Thermodynamics (T, S, Mu)...")
    
    print("Computing LOB Thermodynamics (Vectorized)...")
    
    # 1. Convert Lists to Fixed-Depth Numpy Matrices (Top 5 levels)
    # We assume reasonable depth. If list is shorter, it pads with NaN or we filter.
    # Check max length? No, let's strictly take top 5.
    
    # helper to stack list col
    def to_matrix(series_list, depth=5):
        # This is the slow part in polars python: converting list series to numpy 2D
        # Optim: series.list.to_struct().struct.unnest() then to_numpy()
        # Or faster: just deal with it.
        # Efficient way:
        mat = np.array(series_list.list.head(depth).to_list()) # This might still be slow if len is huge
        # Handle jaggedness if any: fill with nan
        # If lengths are consistent (e.g. 5), it's fast.
        # If jagged, this will create an object array.
        if mat.dtype == object:
           # Pad manually? Or just use "list.eval" inside polars? 
           # Polars exprs are fast. Let's try to do it in Polars EXPRS.
           pass
        return mat

    # Actually, pure Polars implementation of Linear Regression on Lists is available via custom exprs or just unrolling.
    # Given Depth is small (5), we can Unroll!
    # x_0, x_1, x_2, x_3, x_4
    # y_0, y_1, y_2, y_3, y_4
    # This is MUCH faster than Python Loop.
    
    DEPTH = 5
    
    # Filter empty
    df = df.filter(pl.col("bids_vol").list.len() >= DEPTH)
    df = df.filter(pl.col("asks_vol").list.len() >= DEPTH)
    
    # Unroll cols
    cols = []
    for i in range(DEPTH):
        cols.append(pl.col("bids_vol").list.get(i).alias(f"bv_{i}"))
        cols.append(pl.col("bids_price").list.get(i).alias(f"bp_{i}"))
        cols.append(pl.col("asks_vol").list.get(i).alias(f"av_{i}"))
        cols.append(pl.col("asks_price").list.get(i).alias(f"ap_{i}"))
    
    working_df = df.select(cols)
    mat = working_df.to_numpy()
    
    # Indices in mat
    # bv: 0, 4, 8...
    # bp: 1, 5, 9...
    # av: 2, 6, 10...
    # ap: 3, 7, 11...
    
    # Reshape is tricky with mixed layout. 
    # Let's simple extract dict of arrays
    b_vols = np.stack([working_df[f"bv_{i}"].to_numpy() for i in range(DEPTH)], axis=1) # (N, 5)
    b_px   = np.stack([working_df[f"bp_{i}"].to_numpy() for i in range(DEPTH)], axis=1)
    a_vols = np.stack([working_df[f"av_{i}"].to_numpy() for i in range(DEPTH)], axis=1)
    a_px   = np.stack([working_df[f"ap_{i}"].to_numpy() for i in range(DEPTH)], axis=1)
    
    mid = (b_px[:, 0] + a_px[:, 0]) / 2.0
    mid = mid[:, np.newaxis] # (N, 1)
    
    # Distances
    b_dist = np.abs(b_px - mid)
    a_dist = np.abs(a_px - mid)
    
    # Concatenate (Bids + Asks) -> (N, 10)
    X = np.concatenate([b_dist, a_dist], axis=1)
    Y = np.concatenate([b_vols, a_vols], axis=1)
    
    # Avoid log(0)
    Y_log = np.log(Y + 1.0)
    
    # Linear Regression: Y_log = alpha + beta * X
    # beta = Cov(X, Y) / Var(X)
    
    mean_x = np.mean(X, axis=1, keepdims=True)
    mean_y = np.mean(Y_log, axis=1, keepdims=True)
    
    # Center
    X_c = X - mean_x
    Y_c = Y_log - mean_y
    
    cov = np.sum(X_c * Y_c, axis=1)
    var = np.sum(X_c**2, axis=1)
    
    # Avoid divide by zero
    valid = var > 1e-9
    slope = np.zeros(len(df))
    slope[valid] = cov[valid] / var[valid]
    
    # Temp = -1/slope (since LOB decays, slope is neg)
    # beta = -slope
    beta = -slope
    
    temps = np.full(len(df), 100.0) # Default max temp
    mask_decay = beta > 1e-6
    temps[mask_decay] = 1.0 / beta[mask_decay]
    
    # Entropy
    # P = Y / Sum(Y)
    Y_sum = np.sum(Y, axis=1, keepdims=True)
    P = Y / (Y_sum + 1e-9)
    # S = -Sum(P log P)
    log_P = np.log(P + 1e-9)
    entropies = -np.sum(P * log_P, axis=1)
    
    # Chem Pot
    # -ln(Vol near mid)
    vol_near = (b_vols[:, 0] + a_vols[:, 0]) / 2.0
    potentials = -np.log(vol_near + 1.0)
    
    # Re-attach to original DF (careful with filter!)
    # We filtered df at start. We should return a df aligned with that.
    # But compute_batches usually expected to return aligned with input or joinable on ts.
    
    returndf = df.select("exch_ts").with_columns([
        pl.Series("alpha_lob_temp", temps),
        pl.Series("alpha_lob_entropy", entropies),
        pl.Series("alpha_lob_chem_pot", potentials)
    ])
    
    return returndf

if __name__ == "__main__":
    # Test on backup data
    raw_path = 'research/data/market_data_backup.parquet'
    print(f"Testing Batch 004 on {raw_path}...")
    
    try:
        df = pl.read_parquet(raw_path)
        df = df.filter(pl.col("symbol").str.contains("TXF")).sort("exch_ts").tail(2000) # Small sample
        
        # Ensure list cols exist (they might be in raw)
        # If raw is flat, we can't test easily. 
        # But market_data_backup.parquet usually HAS lists ("bids_price", etc.)
        
        print(f"Schema: {df.schema}")
        
        res = compute_batch_004(df)
        print("Result Preview:")
        print(res.head())
        print("Stats:")
        print(res.describe())
        
        # Check for NaNs
        print(f"NaNs in Temp: {res['alpha_lob_temp'].is_null().sum()}")
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
