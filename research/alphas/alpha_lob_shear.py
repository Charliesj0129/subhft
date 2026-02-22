
import numpy as np
import polars as pl
from numba import njit, prange

@njit
def compute_wasserstein_1d(u_values: np.ndarray, v_values: np.ndarray) -> float:
    """
    Computes 1D Wasserstein distance (Earth Mover's Distance) between two distributions.
    For 1D, this is the L1 norm of the difference between CDfs.
    """
    # Create CDFs
    u_sorted = np.sort(u_values)
    v_sorted = np.sort(v_values)
    
    # We assume 'values' are samples from the distribution (e.g., volume-weighted price levels)
    # If len(u) != len(v), we need to interpolate.
    # For HFT simplicity, we compare the fixed 'levels' normalized by volume.
    
    # Simplified Shear Metric: L1 Divergence of Volume Structure
    # Not full Wasserstein because we align by 'distance from mid'.
    
    return np.sum(np.abs(u_values - v_values))

@njit(parallel=True)
def rolling_shear_estimator(
    bid_vols: np.ndarray, 
    ask_vols: np.ndarray, 
    levels: int = 5
) -> np.ndarray:
    """
    Computes Geometric Shear: Divergence between Bid and Ask shapes.
    
    Args:
        bid_vols: Shape (N, levels). Volumes at level 1..5
        ask_vols: Shape (N, levels). Volumes at level 1..5
        
    Returns:
        shear_metric: Shape (N,)
    """
    n = bid_vols.shape[0]
    result = np.zeros(n, dtype=np.float64)
    
    for i in prange(n):
        # 1. Normalize to get Density Distribution (pdf)
        b_total = np.sum(bid_vols[i]) + 1e-9
        a_total = np.sum(ask_vols[i]) + 1e-9
        
        b_pdf = bid_vols[i] / b_total
        a_pdf = ask_vols[i] / a_total
        
        # 2. Geometric Shear = L1 Distance between the two shapes
        # If the book is perfectly symmetric, Shear = 0.
        # If Bid is heavy at L1 but Ask is heavy at L5, Shear is high.
        
        # We assume strict alignment: L1_bid vs L1_ask.
        # "Shear" implies a deformation.
        
        dist = np.sum(np.abs(b_pdf - a_pdf))
        result[i] = dist
        
    return result

def calculate_shear_alpha(df: pl.DataFrame) -> pl.DataFrame:
    """
    Polars Interface for Geometric Shear Alpha.
    
    Concept:
    Liquidity Density rho(x) should be symmetric in equilibrium.
    Shear = Integral |rho_bid(x) - rho_ask(x)| dx
    High Shear -> One side is "pressing" closer than the other -> Directional Pressure.
    """
    
    # Extract LOB Levels (Assume 5 levels available in standard schema)
    # We list columns dynamically or assume standard 'bid_vol_0', 'bid_vol_1'...
    # The dataframe might have list columns 'bids_vol', 'asks_vol'
    
    # Handling List Columns
    if "bids_vol" in df.columns:
        # Extract top 5 levels
        # We convert to numpy for Numba processing
        # Note: This materializes data, but Alpha computation is offline/batch usually.
        
        # Helper to get numpy matrix
        def get_level_matrix(col_name, depth=5):
            # Polars list expansion is tricky efficiently. 
            # We map to list of lists then numpy?
            # Better: list.get(i) for i in 0..4
            cols = [df[col_name].list.get(i).fill_null(0.0) for i in range(depth)]
            return np.vstack([c.to_numpy() for c in cols]).T

        bid_vols = get_level_matrix("bids_vol", depth=5)
        ask_vols = get_level_matrix("asks_vol", depth=5)
        
    elif "bid_volume_0" in df.columns: # Flattened format
        # grab cols
        b_cols = [f"bid_volume_{i}" for i in range(5)]
        a_cols = [f"ask_volume_{i}" for i in range(5)]
        
        bid_vols = df.select(b_cols).to_numpy()
        ask_vols = df.select(a_cols).to_numpy()
        
    else:
        # Fallback or error
        # Assuming list format based on previous files
        # Let's try to infer or iterate
         return df.with_columns(pl.lit(0.0).alias("alpha_shear"))

    # Compute Shear
    shear_vals = rolling_shear_estimator(bid_vols, ask_vols, levels=5)
    
    return lambda_alpha_df(df, shear_vals)

def lambda_alpha_df(original_df: pl.DataFrame, shear_values: np.ndarray) -> pl.DataFrame:
    return original_df.with_columns([
        pl.Series("alpha_shear", shear_values).alias("alpha_shear"),
        # Directional Shear: Signed version?
        # The metric above is unsigned (magnitude of deformation).
        # We might want a signed version: (Bid_Skew - Ask_Skew).
        # For now, we stick to the 2026 paper's "Magnitude of Shear" as instability metric.
    ])
