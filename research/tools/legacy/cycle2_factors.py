
import numpy as np
import pandas as pd
import os
import sys

# Constants
DIR = 'research/data/hbt_multiproduct/'
FILE_TARGET = os.path.join(DIR, 'TXFB6.npy')
FILE_SOURCE = os.path.join(DIR, '2330.npy')

def load_data():
    print("Loading data...")
    if not os.path.exists(FILE_TARGET) or not os.path.exists(FILE_SOURCE):
        print("Data missing.")
        return None, None
        
    # Mmap for speed
    target = np.load(FILE_TARGET, mmap_mode='r')
    source = np.load(FILE_SOURCE, mmap_mode='r')
    return target, source

def calc_cross_impact(target, source):
    print("\n--- Cross-Impact Analysis (2330 -> TXFB6) ---")
    
    # Extract Events
    # Source (2330): Trades only
    src_mask = (source['ev'] == 2) # Trades
    src_trades = source[src_mask]
    
    # Target (TXFB6): We need continuous Mid Price
    # We can't use all events, too slow.
    # We will look up Target Price at Source Trade times.
    
    ts_src = src_trades['exch_ts']
    qty_src = src_trades['qty']
    side_src = src_trades['ival'] # -1 = Buy, 1 = Sell (Aggressor?)
    # Wait, check 'ival' meaning in Process Parquet.
    # Usually: 1 = Bid Aggressor (Buy), -1 = Ask Aggressor (Sell)??
    # Let's assume standard: side 1 = Buy, -1 = Sell.
    
    # Signed Flow
    # Note: 'ival' in hbt is checks: 1=Ask, -1=Bid?
    # Let's verify standard HBT/Parquet conv. 
    # Usually side=1 means Aggressor is Buyer?
    # Let's assume side is correct direction for now.
    signed_flow = qty_src * side_src 
    
    # Target Prices
    ts_tgt = target['exch_ts']
    px_tgt = target['px']
    ev_tgt = target['ev']
    
    # Build a lookup for Target Mid
    # We need to reconstruct Mid from Target stream?
    # Or just use Last Trade Price for approx?
    # LOB reconstruction is expensive.
    # Let's use Last Trade Price of TXFB6 as proxy for Mid? Or BBO?
    # Better: Use 'px' from updates.
    # TXFB6 is high freq.
    
    print("Building Target Price Index...")
    # Get last price at each timestamp
    # We can use searchsorted.
    
    # To get Price Change (Return), we need Prices at t_i and t_i + Delta.
    
    # 1. Find indices of src trades in target stream
    idx_now = np.searchsorted(ts_tgt, ts_src)
    
    # 2. Find indices of src trades + Delta (e.g. 1s, 10s)
    DELTAS = [1_000_000_000, 10_000_000_000] # 1s, 10s in ns
    
    # We need a price array for TXFB6.
    # We will use the 'px' column. Note that 'px' is transaction price or LOB price?
    # For ev=1 (LOB), px is price. For ev=2 (Trade), px is price.
    # We just take the last known price.
    
    # Create simple price array (fill forward?)
    # Too big to pandas.
    # We treat target['px'] as the "current price update".
    # It's noisy but unbiased?
    
    # Better: Filter for Trades or BBO updates in Target.
    tgt_prices = target['px']
    
    # Metrics
    for dt in DELTAS:
        dt_sec = dt / 1e9
        print(f"Analyzing {dt_sec}s Horizon...")
        
        # Future Time
        ts_future = ts_src + dt
        idx_fut = np.searchsorted(ts_tgt, ts_future)
        
        # Clip
        valid = (idx_fut < len(tgt_prices)) & (idx_now < len(tgt_prices))
        
        # Price Now vs Future
        # Note: target['px'][idx] corresponds to the price at or *after* the timestamp?
        # searchsorted returns first index >= value.
        # So target[idx] is price *at or after* t.
        # This is a bit forward-looking if we take strictly >= ?
        # Actually it's fine for "Execution Price" measurement.
        
        p_now = tgt_prices[idx_now[valid]]
        p_fut = tgt_prices[idx_fut[valid]]
        
        ret = np.log(p_fut / p_now)
        flow = signed_flow[valid]
        
        # Remove outliers?
        # Correlation
        corr = np.corrcoef(flow, ret)[0, 1]
        print(f"  -> Correlation (Flow vs Return): {corr:.4f}")
        
        # Regression Coeff (Beta)
        # Ret = Beta * Flow + Alpha
        slope, intercept = np.polyfit(flow, ret, 1)
        print(f"  -> Impact Coeff (Beta): {slope:.2e}")
        
        # Stat Sig?
        n = len(flow)
        t_stat = corr * np.sqrt((n-2)/(1-corr**2))
        print(f"  -> T-Stat: {t_stat:.2f}")

def calc_hawkes_volatility(target):
    print("\n--- Hawkes Volatility Analysis (TXFB6) ---")
    
    # Filter Trades
    mask = (target['ev'] == 2)
    trades = target[mask]
    ts = trades['exch_ts']
    
    # 1. Estimate Intensity (Simple Rolling Count or Exp Decay)
    # Hawkes Intensity proxy: EMA of arrival rate.
    # Let's measure "Local Rate" = Count in last 1s.
    
    print("Computing Intensity Proxy...")
    # Window 1s
    W = 1_000_000_000
    
    # We can iterate or use strided tricks?
    # Simple: Count events in rolling window.
    # For correlation, we can just compute:
    # 1. Local Intensity at t (Events in [t-1s, t])
    # 2. Future Volatility at t (StdDev of Returns in [t, t+10s])
    
    # Sample every 1s to save time
    start = ts[0]
    end = ts[-1]
    
    # Sampling grid
    grid = np.arange(start + W, end - 10*W, W) # 1s steps
    
    # This is slow in Python loop.
    # Use searchsorted.
    
    print(f"Grid Points: {len(grid)}")
    
    # Indices for Past Window [t-1s, t]
    idx_t = np.searchsorted(ts, grid)
    idx_t_minus_1 = np.searchsorted(ts, grid - W)
    
    intensity = (idx_t - idx_t_minus_1) # Count per sec
    
    # Future Volatility
    # Need Prices.
    # We use 'px' from Trades.
    prices = trades['px']
    
    # Volatility = Sum of Squared Returns in [t, t+10s]?
    # Or Range?
    # Range is robust. High - Low in next 10s.
    idx_t_plus_10 = np.searchsorted(ts, grid + 10*W)
    
    vol = []
    
    # Vectorized loop is hard for Range.
    # But we can approximate?
    # Let's do a loop for Vol calc (10k points is fast).
    # Or just use Abs Return over 10s?
    # Volatility ~ |Ret_10s| or Standard Deviation?
    # Standard deviation is better.
    
    # For speed, let's use |Ret_10s| first (Absolute Return).
    p_now = prices[idx_t]
    p_fut = prices[idx_t_plus_10]
    
    # Clip indices to safe range
    valid = idx_t_plus_10 < len(prices)
    
    abs_ret = np.abs(np.log(p_fut[valid] / p_now[valid]))
    inten = intensity[valid]
    
    corr = np.corrcoef(inten, abs_ret)[0,1]
    print(f"  -> Correlation (Intensity vs AbsRet_10s): {corr:.4f}")
    
    # Is it positive?
    
def run_analysis():
    target, source = load_data()
    if target is None: return
    
    calc_cross_impact(target, source)
    calc_hawkes_volatility(target)

if __name__ == '__main__':
    run_analysis()
