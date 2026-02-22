
import numpy as np
import polars as pl
from numba import njit, prange

@njit
def calculate_rs_hurst(series: np.ndarray, min_chunk_size: int = 50) -> float:
    """
    Calculates the Hurst Exponent using R/S Analysis.
    Optimized for short-term windows in HFT.
    """
    n = len(series)
    if n < min_chunk_size * 2:
        return 0.5  # Return random walk default if insufficient data
    
    # Create chunks of different sizes (powers of 2)
    chunk_sizes = []
    curr_size = min_chunk_size
    while curr_size <= n // 2:
        chunk_sizes.append(curr_size)
        curr_size *= 2
        
    rs_values = []
    
    for size in chunk_sizes:
        num_chunks = n // size
        total_rs = 0.0
        
        for i in range(num_chunks):
            chunk = series[i*size : (i+1)*size]
            mean = np.mean(chunk)
            deviations = chunk - mean
            cum_deviations = np.cumsum(deviations)
            r = np.max(cum_deviations) - np.min(cum_deviations)
            s = np.std(chunk)
            
            if s == 0:
                total_rs += 0
            else:
                total_rs += r / s
                
        avg_rs = total_rs / num_chunks
        rs_values.append(avg_rs)
        
    # Log-Log Regression: log(R/S) ~ H * log(size)
    log_sizes = np.log(np.array(chunk_sizes))
    log_rs = np.log(np.array(rs_values))
    
    # Simple linear regression slope
    A = np.vstack((log_sizes, np.ones(len(log_sizes)))).T
    if len(log_sizes) < 2:
        return 0.5
        
    m, c = np.linalg.lstsq(A, log_rs, rcond=None)[0]
    return m

@njit(parallel=True)
def rolling_h0_estimator(signed_flow: np.ndarray, window: int = 1000) -> np.ndarray:
    """
    Computes rolling H0 (Hurst of Signed Order Flow) in a parallelized loop.
    
    Args:
        signed_flow (np.ndarray): +1 (Buy), -1 (Sell), 0 (No Trade) per tick.
        window (int): Rolling window size.
        
    Returns:
        np.ndarray: Array of H0 estimates.
    """
    n = len(signed_flow)
    result = np.full(n, 0.5, dtype=np.float64) # Default to 0.5 (Random)
    
    # Pre-compute integrated flow for efficiency? 
    # R/S needs the raw series of increments/flow, integrated inside.
    
    for i in prange(window, n):
        slice_data = signed_flow[i-window : i]
        # Only compute if we have enough activity
        if np.sum(np.abs(slice_data)) > 100: 
             result[i] = calculate_rs_hurst(slice_data)
             
    return result

def calculate_h0_alpha(df: pl.DataFrame, window: int = 1000) -> pl.DataFrame:
    """
    Polars interface for H0 Alpha.
    
    Theory:
    H0 > 0.5 -> Persistent Flow -> High Impact, Low Volatility (Initially)
    H0 < 0.5 -> Mean Reverting Flow -> choppy
    
    Unified Theory 2026:
    Vol_Roughness ~ 2*H0 - 1.5
    Impact_Decay ~ 2 - 2*H0
    """
    # 1. Construct Signed Flow (+1/-1 indicates direction of trade)
    # We estimate this from 'last_px' changes or separate buy/sell volume cols if available.
    # Assuming standard tick data: 'volume', 'is_buyer_maker' (if crypto) or price change
    
    # Heuristic for Signed Flow if not explicit:
    # If Price Up -> +1, Price Down -> -1, Else 0 (or repeat last)
    
    # Let's assume we have 'price' and 'volume'
    # We will use "Tick Rule":
    
    price_diff = df["price"].diff().fill_null(0.0)
    
    # Create simple signed flow series (-1, 0, 1)
    signed_flow = price_diff.map_elements(lambda x: 1.0 if x > 0 else (-1.0 if x < 0 else 0.0), return_dtype=pl.Float64)
    
    # Convert to numpy for Numba
    flow_np = signed_flow.to_numpy()
    
    # Calculate H0
    h0_values = rolling_h0_estimator(flow_np, window=window)
    
    return lambda_alpha_df(df, h0_values)

def lambda_alpha_df(original_df: pl.DataFrame, h0_values: np.ndarray) -> pl.DataFrame:
    return original_df.with_columns([
        pl.Series("alpha_h0", h0_values).alias("alpha_h0"),
        # Derived Volatility Prediction based on 2026 Paper
        (pl.Series("alpha_h0", h0_values) * 2 - 1.5).alias("alpha_predicted_vol_roughness"),
        (2.0 - pl.Series("alpha_h0", h0_values) * 2).alias("alpha_predicted_impact_decay")
    ])
