
import polars as pl
import numpy as np

def compute_alpha_stealth(df: pl.DataFrame, window: int = 100) -> pl.DataFrame:
    """
    Camouflage/Stealth Trading Signals.
    Based on Paper 2512.06309.
    
    Detects "Stealth Trading" by analyzing trade size distributions.
    Hypothesis: Informed traders break orders into "Medium" sizes to hide.
    Retail/Algos cluster on Round Numbers (100, 500, 1000).
    Stealth = Medium Size AND Non-Round?
    
    Signals:
    - alpha_stealth_ratio: Ratio of Medium Trades (e.g. 5-20 lots) to Total Trades.
    - alpha_clustering_score: % of trades on round numbers (10, 50, 100).
    - alpha_camouflaged_flow: OFI computed only on "Medium" trades.
    """
    
    if "mid_price" not in df.columns:
        # Need trade data. If LOB snapshot, we can't see individual trades unless we diff volume?
        # Ideally we ingest a Trade Flow.
        # For this mock/LOB data, we approximate using "Volume Delta" or just L1 volume as proxy?
        # Assuming we have "trades_vol" or "volume" col.
        # If timestamp is Tick data, volume column is trade size.
        if "volume" not in df.columns:
             # Construct from L1 delta (very noisy)
             return df.select("exch_ts")
        
    df_temp = df
    
    # 1. Define "Medium" Trade Size
    # For Taiwan Futures (TXF), 1 lot is standard.
    # Large > 10? Medium 2-9? Small 1?
    
    # Clustering Check (Round Numbers)
    # Check if volume is multiple of 5 or 10.
    
    df_temp = df_temp.with_columns([
        (pl.col("volume") % 5 == 0).alias("is_round_5"),
        (pl.col("volume") % 10 == 0).alias("is_round_10"),
        ((pl.col("volume") > 1) & (pl.col("volume") < 10)).alias("is_medium")
    ])
    
    # Rolling Aggregation
    df_temp = df_temp.with_columns([
        pl.col("is_round_5").rolling_mean(window).alias("round_ratio"),
        pl.col("is_medium").rolling_mean(window).alias("medium_ratio"),
        pl.col("volume").rolling_mean(window).alias("vol_ma")
    ])
    
    # Camouflaged Flow (OFI on Medium Trades)
    # If mid_price rose, and Medium Trades were active?
    # Simple proxy: Medium Ratio * Price Change
    
    df_temp = df_temp.with_columns([
         pl.col("mid_price").diff().fill_null(0.0).alias("dp"),
    ])
    
    df_temp = df_temp.with_columns([
         (pl.col("dp") * pl.col("medium_ratio")).rolling_mean(window).alias("alpha_camouflaged_flow")
    ])
    
    return df_temp.select([
        "exch_ts",
        pl.col("round_ratio").alias("alpha_clustering_score"),
        pl.col("medium_ratio").alias("alpha_stealth_ratio"),
        pl.col("alpha_camouflaged_flow")
    ])

if __name__ == "__main__":
    print("Testing Stealth Alpha...")
    # Mock
    df = pl.DataFrame({
        "exch_ts": np.arange(100),
        "mid_price": np.linspace(100, 101, 100),
        "volume": np.random.randint(1, 20, 100) # Random trade sizes
    })
    
    res = compute_alpha_stealth(df)
    print(res.describe())
