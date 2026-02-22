
import polars as pl
import numpy as np

def compute_alpha_integrity(df: pl.DataFrame, window: int = 60) -> pl.DataFrame:
    """
    AIMM-X Integrity Signals: Suspicious Window Detection.
    Based on Paper 2601.15304.
    
    Identifies "suspicious" periods where:
    1. Volatility is anomalous (Z-score > 2)
    2. Volume/Attention diverges from Price.
    
    Proxies:
    - Attention = Volume (assuming High Volume = High Attention)
    
    Signals:
    - alpha_integrity_vol_z: Z-score of realized volatility.
    - alpha_integrity_div: Price-Volume Divergence.
    - alpha_integrity_score: Composite score.
    """
    
    # 1. Compute Returns & Volatility
    # Need mid_price or similar
    if "mid_price" not in df.columns:
        # Construct
        if "bids_price" in df.columns:
            # Robust extraction
             df_temp = df.with_columns([
                 pl.col("bids_price").list.get(0).fill_null(0.0).alias("bp0"),
                 pl.col("asks_price").list.get(0).fill_null(0.0).alias("ap0"),
                 pl.col("asks_vol").list.get(0).fill_null(0.0).alias("av0"),
                 pl.col("bids_vol").list.get(0).fill_null(0.0).alias("bv0"),
             ])
             df_temp = df_temp.with_columns(( (pl.col("bp0") + pl.col("ap0")) / 2.0 ).alias("mid_price"))
             df_temp = df_temp.with_columns(( pl.col("av0") + pl.col("bv0") ).alias("volume")) # L1 Volume
        else:
             return df.select("exch_ts")
    else:
        df_temp = df
        
    # Log Returns
    df_temp = df_temp.with_columns([
        pl.col("mid_price").log().diff().fill_null(0.0).alias("log_ret")
    ])
    
    # Realized Volatility (std)
    df_temp = df_temp.with_columns([
        pl.col("log_ret").rolling_std(window_size=window).fill_null(0.0).alias("r_vol")
    ])
    
    # Rolling Mean/Std of Volatility (Baseline)
    baseline_win = window * 10
    df_temp = df_temp.with_columns([
        pl.col("r_vol").rolling_mean(window_size=baseline_win).fill_null(0.0).alias("vol_mean"),
        pl.col("r_vol").rolling_std(window_size=baseline_win).fill_null(1e-9).alias("vol_std")
    ])
    
    # Volatility Z-Score (Anomaly)
    df_temp = df_temp.with_columns([
        ((pl.col("r_vol") - pl.col("vol_mean")) / pl.col("vol_std")).alias("vol_z")
    ])
    
    # Price-Volume Divergence
    # Paper: High Attention + Low Return = Suspicious? Or High Return + Low Attention?
    # Usually: High Volume + Low Price Move = Absorption/Iceberg (Stealth).
    # Divergence = Volume / (Abs(Return) + epsilon)
    df_temp = df_temp.with_columns([
        (pl.col("volume") / (pl.col("log_ret").abs() + 1e-9)).alias("pv_div")
    ])
    
    # Normalize PV Div (Robust Z-score)
    df_temp = df_temp.with_columns([
         ((pl.col("pv_div") - pl.col("pv_div").rolling_mean(baseline_win)) / pl.col("pv_div").rolling_std(baseline_win)).fill_null(0).alias("pv_div_z")
    ])
    
    # Composite Score (Simple Mean of Abs Z-scores)
    # High Vol Z and High Div Z -> Suspicious
    df_temp = df_temp.with_columns([
        (pl.col("vol_z").abs() + pl.col("pv_div_z").abs()).alias("integrity_score")
    ])
    
    # Select cols
    return df_temp.select([
        "exch_ts",
        pl.col("vol_z").alias("alpha_integrity_vol_z"),
        pl.col("pv_div_z").alias("alpha_integrity_div"),
        pl.col("integrity_score").alias("alpha_integrity_score")
    ])

if __name__ == "__main__":
    print("Testing AIMM-X Integrity Signal...")
    # Mock
    df = pl.DataFrame({
        "exch_ts": np.arange(1000),
        "mid_price": np.linspace(100, 105, 1000) + np.random.normal(0, 0.1, 1000),
        "volume": np.random.random(1000) * 100
    })
    # Inject anomaly: High Vol, Flat Price? No, High Vol + High Price
    
    res = compute_alpha_integrity(df)
    print(res.describe())
