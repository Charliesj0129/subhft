
import polars as pl
import numpy as np
import os
import sys

# Define Local Paper References for Documentation
PAPERS = {
    "Hawkes": "2109.15110v1_Deep_Hawkes_Process_for_High-F.pdf",
    "MicroPrice": "2312.08927v5_Limit_Order_Book_Dynamics_and_.pdf",
    "OFI": "2502.17417v1_Event-Based_Limit_Order_Book_S.pdf",
    "Trend": "2405.03496v3_Price-Aware_Automated_Market_M.pdf",
    "Hurst": "2505.17388v1_Stochastic_Price_Dynamics_in_R.pdf"
}

def compute_batch_002(df: pl.DataFrame) -> pl.DataFrame:
    """
    Implements 5 Microstructure Alphas from Local Research Papers.
    Input: Polars DataFrame with columns [exch_ts, bid_px, bid_qty, ask_px, ask_qty, price, qty]
    Output: DataFrame with new columns [alpha_hawkes, alpha_micro, alpha_ofi_i, alpha_trend, alpha_hurst]
    """
    
    # Ensure sorted by time
    df = df.sort("exch_ts")
    
    # Force Cast exch_ts to Datetime (ns)
    df = df.with_columns(pl.col("exch_ts").cast(pl.Datetime("ns")))
    
    # ---------------------------------------------------------
    # 1. Hawkes Intensity (Volatility Proxy)
    # Logic: Rolling Count 1s
    # ---------------------------------------------------------
    # Use reliable df.rolling() API
    # Note: df must be sorted by index_column
    
    # Calculate counts
    # We select exch_ts, run rolling count, and get the count column
    # Then we append it to df.
    # Note: rolling().count() returns a DF with the same length.
    
    hawkes_intensity = (
        df.select(pl.col("exch_ts"))
          .rolling(index_column="exch_ts", period="1s", closed="left")
          .agg(pl.count("exch_ts").alias("alpha_hawkes"))
    )
    
    # Combine back (Assuming order preserved)
    df = df.with_columns(
        hawkes_intensity["alpha_hawkes"]
    )
    
    # Normalize Hawkes (log) 
    df = df.with_columns(
        (pl.col("alpha_hawkes").log() + 1.0).alias("alpha_hawkes")
    )
    
    # Normalize Hawkes (e.g. log transform to dampen outliers)
    df = df.with_columns(
        (pl.col("alpha_hawkes").log() + 1.0).alias("alpha_hawkes")
    )


    # ---------------------------------------------------------
    # 2. MicroPrice v3 (LOB Dynamics)
    # Logic: Mid + Imbalance Adjustment
    # Paper: Limit Order Book Dynamics (2312.08927)
    # ---------------------------------------------------------
    # Stoikov's Approx: M = Mid + S * (I - 0.5) where I = BQ / (BQ + AQ)
    # v3 adjustment from paper implies non-linear restoring force.
    # We implement: MicroPrice = Mid + Spread * (Imbalance^3) * 0.5 (Cube to penalize weak imbalance)
    
    df = df.with_columns([
        ((pl.col("bid_px_0") + pl.col("ask_px_0")) * 0.5).alias("mid_price"),
        (pl.col("ask_px_0") - pl.col("bid_px_0")).alias("spread")
    ])
    
    df = df.with_columns(
        (pl.col("bid_qty_0") / (pl.col("bid_qty_0") + pl.col("ask_qty_0") + 1e-9)).alias("imbalance")
    )
    
    # Alpha = Predicted Next Mid - Current Mid ~ MicroPrice deviation
    df = df.with_columns(
        (pl.col("spread") * (2 * pl.col("imbalance") - 1).pow(3)).alias("alpha_micro_dev")
    )
    
    
    # ---------------------------------------------------------
    # 3. OFI-I (Integrated Order Flow Imbalance)
    # Logic: Event-Based Accumulation with Decay
    # Paper: Event-Based LOB (2502.17417)
    # ---------------------------------------------------------
    # OFI = DeltaBidQty (if Bid >= PrevBid) - DeltaAskQty (if Ask <= PrevAsk)
    # Integrated OFI = EMA(OFI)
    
    # Shifted cols
    df = df.with_columns([
        pl.col("bid_px_0").shift(1).alias("prev_bid_px"),
        pl.col("bid_qty_0").shift(1).alias("prev_bid_qty"),
        pl.col("ask_px_0").shift(1).alias("prev_ask_px"),
        pl.col("ask_qty_0").shift(1).alias("prev_ask_qty"),
    ])
    
    # Vectorized OFI Logic
    # e_n (Bid)
    # if bp > prev_bp: +bq
    # if bp < prev_bp: 0 (or -prev_bq?) -> Cont paper says looking at level 1 changes. 
    # Simplified Cont/Stoikov 2014 definition:
    # e_n = I(bp > prev_bp)*bq - I(bp < prev_bp)*prev_bq + I(bp=prev_bp)*(bq - prev_bq)
    
    bp_gt = pl.col("bid_px_0") > pl.col("prev_bid_px")
    bp_lt = pl.col("bid_px_0") < pl.col("prev_bid_px")
    bp_eq = pl.col("bid_px_0") == pl.col("prev_bid_px")
    
    d_bid = (
        pl.when(bp_gt).then(pl.col("bid_qty_0"))
        .when(bp_lt).then(pl.col("prev_bid_qty") * -1) # Loss of liquidity
        .otherwise(pl.col("bid_qty_0") - pl.col("prev_bid_qty"))
    )
    
    # e_n (Ask) - Ask Side Liquidity Add is Negative Price Pressure
    ap_lt = pl.col("ask_px_0") < pl.col("prev_ask_px")
    ap_gt = pl.col("ask_px_0") > pl.col("prev_ask_px")
    ap_eq = pl.col("ask_px_0") == pl.col("prev_ask_px")
    
    d_ask = (
        pl.when(ap_lt).then(pl.col("ask_qty_0"))
        .when(ap_gt).then(pl.col("prev_ask_qty") * -1)
        .otherwise(pl.col("ask_qty_0") - pl.col("prev_ask_qty"))
    )
    
    # OFI = d_bid - d_ask
    df = df.with_columns(
        (d_bid - d_ask).fill_null(0).alias("raw_ofi")
    )
    
    # OFI-I (Integrated with Decay) -> decay alpha ~ 0.9 per tick? or time based?
    # Using ewm_mean_by (time) is better
    df = df.with_columns(
        pl.col("raw_ofi").ewm_mean_by("exch_ts", half_life="1s").alias("alpha_ofi_i")
    )


    # ---------------------------------------------------------
    # 4. Price Trend (Inventory Adjustment)
    # Logic: Mid - EMA(Mid)
    # Paper: Price-Aware AMM (2405.03496)
    # ---------------------------------------------------------
    # Measures deviation from "Fair Trend".
    # Positive = Trend Up.
    
    df = df.with_columns(
         pl.col("mid_price").ewm_mean_by("exch_ts", half_life="10s").alias("mid_ema_slow")
    )
    
    df = df.with_columns(
        (pl.col("mid_price") - pl.col("mid_ema_slow")).alias("alpha_trend")
    )


    # ---------------------------------------------------------
    # 5. Hurst Exponent (Diffusive Volatility)
    # Logic: Var(tau) ~ tau^2H
    # Paper: Stochastic Price Dynamics (2505.17388)
    # ---------------------------------------------------------
    # Real-time Hurst is hard approx. 
    # Use "VR Ratio": Variance(Window) / Variance(SubWindow)
    # Proxy: Volatility(10s) / Volatility(1s)
    
    # Use robust rolling aggregation for std dev too
    vol_long = (
        df.select(["exch_ts", "mid_price"])
          .rolling(index_column="exch_ts", period="10s", closed="left")
          .agg(pl.col("mid_price").std().alias("vol_long"))
    )
    
    vol_short = (
        df.select(["exch_ts", "mid_price"])
          .rolling(index_column="exch_ts", period="1s", closed="left")
          .agg(pl.col("mid_price").std().alias("vol_short"))
    )
    
    df = df.with_columns([
        vol_long["vol_long"],
        vol_short["vol_short"]
    ])
    
    # H approx via VR
    df = df.with_columns(
        (pl.col("vol_long") / (pl.col("vol_short") * 3.162 + 1e-9)).alias("alpha_hurst")
    )

    # ---------------------------------------------------------
    # Cleanup & Select
    # ---------------------------------------------------------
    return df.select([
        "exch_ts", 
        "alpha_hawkes", 
        "alpha_micro_dev", 
        "alpha_ofi_i", 
        "alpha_trend", 
        "alpha_hurst"
    ])

if __name__ == "__main__":
    # Test Driver
    print("Loading Sample Data...")
    try:
        # Load Raw Parquet (e.g. TXFB6)
        # Using a snippet logic or linking to known data
        df = pl.read_parquet("research/data/market_data_backup.parquet")
        
        # Filter for TXF
        df = df.filter(pl.col("symbol").str.contains("TXF")).head(100000)
        
        print(f"Data Loaded: {df.shape}")
        
        # Rename cols to match expectation if needed
        # Assuming schema: [exch_ts, bid_price, bid_volume, ask_price, ask_volume] -> [bid_px_0, bid_qty_0...]
        # Adjust mapping if needed. MarketDataBackup parquet usually has lists.
        # This script expects FLATTENED data.
        
        # Quick Hack: If list columns, assume index 0
        # Snapshot format often has lists.
        # Check for plural 'bids_price'
        if "bids_price" in df.columns:
             # Ensure we have data
             df = df.filter(pl.col("bids_price").list.len() > 0)
             
             df = df.with_columns([
                 pl.col("bids_price").list.get(0).alias("bid_px_0"),
                 pl.col("bids_vol").list.get(0).alias("bid_qty_0"),
                 pl.col("asks_price").list.get(0).alias("ask_px_0"),
                 pl.col("asks_vol").list.get(0).alias("ask_qty_0"),
             ])
        elif "bid_price" in df.columns:
             df = df.with_columns([
                 pl.col("bid_price").list.get(0).alias("bid_px_0"),
                 pl.col("bid_volume").list.get(0).alias("bid_qty_0"),
                 pl.col("ask_price").list.get(0).alias("ask_px_0"),
                 pl.col("ask_volume").list.get(0).alias("ask_qty_0"),
             ])
        
        # Run Batch
        
        # Run Batch
        print("Computing Alphas...")
        res = compute_batch_002(df)
        print(res.tail())
        print("Success.")
        
    except Exception as e:
        print(f"Test Run Failed (Expected if local data path differs): {e}")
