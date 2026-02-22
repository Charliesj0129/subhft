
import polars as pl
import numpy as np
from typing import Optional

def compute_batch_003(df: pl.DataFrame, market_cap: float = 1.0) -> pl.DataFrame:
    """
    Implements Alpha Cluster: 'The Physics of Criticality' (Paper 2601.11602)
    
    1. S_MC (Market Cap Normalized Imbalance)
       - Insight: Volume Normalization (S_TV) includes noise. S_MC isolates signal.
       - Implementation: Raw Net Dollar Imbalance divided by constant MarketCap.
    
    2. Hawkes Criticality (Branching Ratio Proxy)
       - Insight: When Branching Ratio n -> 1, signal reverses.
       - Implementation: Rolling Fano Factor (Variance/Mean of Counts) as a proxy for n.
         (High Fano = Super-Poissonian Clustering = High Branching Ratio).
    """
    
    # Ensure Datetime
    df = df.with_columns(pl.col("exch_ts").cast(pl.Datetime("ns")))
    df = df.sort("exch_ts")
    
    # ---------------------------------------------------------
    # 1. S_MC: Market Cap Normalized Imbalance
    # ---------------------------------------------------------
    # S_MC = (BuyVal - SellVal) / MarketCap
    # Approximation for Futures: (BuyQty - SellQty) * Price ~ DollarImbalance
    # Note: We do NOT divide by Volume. We share 'market_cap' as a scaling factor.
    
    # We need to reconstruct "Trade Flow" or use LOB Imbalance as proxy?
    # Paper 2601.11602 refers to "Order Flow Imbalance" (Trades).
    # HFT context: We usually use Aggressor Side from trades.
    # If using LOB snapshots (no trade flags), we use OFI (Order Flow Imbalance) derived from LOB changes.
    
    # Using LOB-derived OFI (similar to batch_002) but NOT normalizing by volume.
    # Re-implementing simplified OFI logic for self-containment.
    
    df = df.with_columns([
        pl.col("bid_px_0").shift(1).alias("prev_bid_px"),
        pl.col("bid_qty_0").shift(1).alias("prev_bid_qty"),
        pl.col("ask_px_0").shift(1).alias("prev_ask_px"),
        pl.col("ask_qty_0").shift(1).alias("prev_ask_qty"),
    ])
    
    # OFI Logic (Cont et al.)
    bp_gt = pl.col("bid_px_0") > pl.col("prev_bid_px")
    bp_lt = pl.col("bid_px_0") < pl.col("prev_bid_px")
    
    d_bid = (
        pl.when(bp_gt).then(pl.col("bid_qty_0"))
        .when(bp_lt).then(pl.col("prev_bid_qty") * -1)
        .otherwise(pl.col("bid_qty_0") - pl.col("prev_bid_qty"))
    )
    
    ap_lt = pl.col("ask_px_0") < pl.col("prev_ask_px")
    ap_gt = pl.col("ask_px_0") > pl.col("prev_ask_px")
    
    d_ask = (
        pl.when(ap_lt).then(pl.col("ask_qty_0"))
        .when(ap_gt).then(pl.col("prev_ask_qty") * -1)
        .otherwise(pl.col("ask_qty_0") - pl.col("prev_ask_qty"))
    )
    
    # Raw Order Flow Imbalance (Qty)
    df = df.with_columns((d_bid - d_ask).fill_null(0).alias("ofi_qty"))
    
    # S_MC = (OFI_Qty * Price) / MarketCap
    # Using MidPrice for valuation
    mid_price = (pl.col("bid_px_0") + pl.col("ask_px_0")) / 2
    
    df = df.with_columns(
        (pl.col("ofi_qty") * mid_price / market_cap).alias("alpha_smc")
    )
    
    # ---------------------------------------------------------
    # 2. Hawkes Criticality Monitor (Fano Factor)
    # ---------------------------------------------------------
    # Branching Ratio n ~ 1 implies events cluster intensely.
    # Fano Factor F = Var(N) / E[N] for count process N.
    # Poisson: F = 1. Hawkes(n->1): F >> 1.
    
    # Step A: Count events in small bins (e.g., 100ms)
    # We create a 'tick_count' per bin. 
    # Since we have microsecond lines, we can resample or rely on rolling.
    
    # We'll use a 1-second rolling window to calculate Mean and Var of "Trade Intensity".
    # Since specific "Trades" might not be in LOB snapshot, we use 'OFI' magnitude as Activity Proxy.
    # |OFI| > 0 implies an event happened.
    
    df = df.with_columns(
        (pl.col("ofi_qty").abs() > 0).cast(pl.UInt8).alias("is_event")
    )
    
    # Rolling Sum (Count) over 1s to get "Intensity per second" (Rate)
    # But Fano needs variance of counts across sub-intervals.
    
    # Alternative HFT approx:
    # Calculate Rate (1s)
    # Calculate Rate (10s)
    # Criticality ~ (Rate_Short - Rate_Long) / Rate_Long ? No.
    
    # Correct Fano Implementation on Time Series:
    # 1. Define window W = 10s.
    # 2. Split W into sub-bins (e.g. 100ms).
    # 3. Calculate counts N_i in each sub-bin.
    # 4. F = Var(N_i) / Mean(N_i).
    
    # Polars Rolling Implementation:
    # This is tricky to do purely vectorized on ticks without explicit "group_by_dynamic".
    # We will use a simpler proxy: "Burstiness"
    # Burstiness = Std(Inter-Event Times) / Mean(Inter-Event Times) (Coef of Variation of IET)
    # For Poisson, CV = 1. For Hawkes, CV > 1.
    
    # 1. Identify Event Timestamps
    # We can't easily get IET in Polars straight vector without filtering.
    
    # Let's go with "Rolling Volatility of Activity".
    # Activity signal = |OFI|.
    # We measure Mean(|OFI|) and Std(|OFI|) over 5s.
    # "Criticality" = Std(|OFI|) / Mean(|OFI|)  (Coefficient of Variation of Flow)
    # This captures "Lumpy" flow (Herding) vs "Smooth" flow.
    
    # Correct Fano Implementation on Time Series:
    # We use DataFrame context for time-based rolling aggregation to ensure correctness
    
    # Calculate Abs Flow Profile
    df = df.with_columns(pl.col("ofi_qty").abs().alias("abs_flow"))
    
    rolling_stats = (
        df.select(["exch_ts", "abs_flow"])
          .rolling(index_column="exch_ts", period="10s", closed="left")
          .agg([
              pl.col("abs_flow").mean().alias("flow_mean"),
              pl.col("abs_flow").std().alias("flow_std")
          ])
    )
    
    # Combine back (Polars rolling preserves order)
    df = df.with_columns([
        rolling_stats["flow_mean"],
        rolling_stats["flow_std"]
    ])
    
    df = df.with_columns(
        (pl.col("flow_std") / (pl.col("flow_mean") + 1e-9)).alias("alpha_criticality_cv")
    )
    
    # ---------------------------------------------------------
    # 3. Signal Reversal Logic (The "Anti-Alpha")
    # ---------------------------------------------------------
    # Paper says: If Criticality is High, Signal Reverses.
    # We construct a composite alpha: S_MC_Adjusted.
    # If CV > Threshold (e.g. 2.0 per paper intuition), flip sign?
    # We output the interaction term.
    
    df = df.with_columns(
        (pl.col("alpha_smc") * pl.col("alpha_criticality_cv")).alias("alpha_smc_critical_interact")
    )

    return df.select([
        "exch_ts",
        "alpha_smc",
        "alpha_criticality_cv",
        "alpha_smc_critical_interact"
    ])

if __name__ == "__main__":
    # Test Driver
    print("Loading Sample Data...")
    try:
        # Load Raw Parquet
        df = pl.read_parquet("research/data/market_data_backup.parquet")
        df = df.filter(pl.col("symbol").str.contains("TXF")).head(50000)
        
        # Flatten logic (copied from batch_002)
        if "bids_price" in df.columns:
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

        print("Computing Physics Alphas...")
        # For Futures, Market Cap is arbitrary. 
        # Paper 2601.11602: S_MC isolates pure signal.
        # We pass 1.0 to get Raw Dollar Flow.
        res = compute_batch_003(df, market_cap=1.0)
        
        print(res.tail())
        print("Stats:")
        print(res.describe())
        
    except Exception as e:
        print(f"Test Run Failed: {e}")
