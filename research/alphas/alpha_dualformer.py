
import polars as pl
import numpy as np
import scipy.signal
import scipy.fft

def compute_alpha_dualformer(df: pl.DataFrame, window_size: int = 64) -> pl.DataFrame:
    """
    Dualformer Alpha: Hierarchical Frequency Sampling (HFS).
    Based on Paper 2601.15669.
    
    Instead of a full Transformer, we implement the core 'HFS' signal processing logic
    to decompose the price signal into Dual Domains (Time & Frequency).
    
    Outputs:
    1. alpha_dual_hf (High-Freq): Microstructure noise/jitter (from shallow layer HFS proxy).
    2. alpha_dual_lf (Low-Freq): Underlying trend (from deep layer HFS proxy).
    3. alpha_dual_energy (Harmonic Ratio): Ratio of High-Freq energy to Total energy.
       - High Energy -> Volatile/Mean Reverting?
       - Low Energy -> Strong Trend?
    """
    
    # Needs standard columns
    # We basically work on MID PRICE or Trade Price
    # If not present, try to construct from bids/asks dim 0
    
    # Check inputs
    cols = df.columns
    price_col = None
    if "mid_price" in cols:
        price_col = "mid_price"
    elif "bid_px_0" in cols and "ask_px_0" in cols:
        # We need to compute mid, but input DF might be lazy or features.
        # Let's assume we can compute it/it exists.
        pass
    
    # If we are in the 'generating batches' flow, usually we have lists or raw data.
    # We will assume we can get a numpy array of prices.
    
    print("Computing Dualformer HFS Signals...")
    
    # 1. Extract Price Series (fill nulls)
    # This part depends on input schema.
    
    # Pre-filter for robust list handling
    if "bids_price" in df.columns:
        df = df.filter(pl.col("bids_price").list.len() > 0)
        df = df.filter(pl.col("asks_price").list.len() > 0)
    
    # Robust extraction:
    try:
        if "mid_price" in df.columns:
            prices = df["mid_price"].to_numpy()
        elif "bids_price" in df.columns:
            # Polars List to Numpy
            # Use fill_null to avoid None in list
            b0 = df["bids_price"].list.get(0).fill_null(0.0).to_numpy()
            a0 = df["asks_price"].list.get(0).fill_null(0.0).to_numpy()
            
            # Mask 0s just in case
            mask = (b0 > 0) & (a0 > 0)
            if mask.sum() == 0:
                 return df.select("exch_ts")
                 
            prices = (b0 + a0) / 2.0
        else:
            # Fallback for testing/empty
            return df.select("exch_ts")
            
        prices = np.nan_to_num(prices)
        
        # 2. Rolling Window Frequency Decomposition
        # We use STFT (Short Time Fourier Transform)
        # window_size = 64 ticks
        
        n = len(prices)
        hf_signal = np.zeros(n)
        lf_signal = np.zeros(n)
        energy_ratio = np.zeros(n)
        
        # Determine cutoff for HF vs LF
        # Nyquist = 0.5 * sampling_rate.
        # We split roughly at 1/4 of Nyquist.
        # HF: Top 50% of freq
        # LF: Bottom 50% of freq
        
        # Using scipy.signal.stft is good but returns a matrix (Freq, Time).
        # We want a time series of "instantaneous HF component".
        
        # Efficient approach: Simple Filters (Butterworth)
        # But paper emphasizes "Frequency Domain Learning".
        # Let's do Real FFT on rolling windows.
        
        # For Alpha generation (vectorized where possible):
        # Rolling apply is slow in Python.
        # Signal processing filter is O(N).
        # We will use High-Pass and Low-Pass Filters to approximate the HFS.
        
        # Normalized Frequency (0 to 1, where 1 is Nyquist)
        # Cutoff = 0.1 (Very low freq = Trend)
        
        sos_lp = scipy.signal.butter(4, 0.1, output='sos')
        sos_hf = scipy.signal.butter(4, 0.1, btype='highpass', output='sos')
        
        lf_signal = scipy.signal.sosfiltfilt(sos_lp, prices) # Zero-phase filter
        hf_signal = scipy.signal.sosfiltfilt(sos_hf, prices)
        
        # 3. Energy Ratio (Periodicity)
        # Windowed RMS of HF / RMS of Total
        # E_hf = RollingMean(hf^2)
        # E_tot = RollingMean(price^2) -- No, price is effectively non-stationary.
        # E_tot = E_hf + E_lf (approx)
        
        # Use simple pandas-like rolling (via uniform filter or convolution)
        win = window_size
        kernel = np.ones(win) / win
        
        hf_sq = hf_signal**2
        lf_sq = lf_signal**2 # (Centered LF via filter?) Filter removes DC?
        # Ideally we want Var(HF) / Var(Total)
        
        var_hf = np.convolve(hf_sq, kernel, mode='same')
        var_lf = np.convolve(lf_sq, kernel, mode='same')
        
        # Avoid div 0
        total_var = var_hf + var_lf + 1e-9
        energy_ratio = var_hf / total_var

        # 4. Return as Polars Series
        return df.select("exch_ts").with_columns([
            pl.Series("alpha_dual_hf", hf_signal),
            pl.Series("alpha_dual_lf", lf_signal),
            pl.Series("alpha_dual_energy", energy_ratio)
        ])
        
    except Exception as e:
        print(f"Dualformer Error: {e}")
        # Return empty cols to match schema if needed, or just crash explicitly
        return df.select("exch_ts") # Simplest fallback

if __name__ == "__main__":
    # Test on Real Data
    print("Testing Dualformer Alpha on Real Market Data...")
    raw_path = 'research/data/market_data_backup.parquet'
    
    try:
        df = pl.read_parquet(raw_path).tail(2000)
        # Check if we need to flatten list or if mid_price exists
        # Usually backup data has 'bids_price' as list.
        print(f"Schema: {df.schema}")
        
        res = compute_alpha_dualformer(df)
        print("Result Preview:")
        print(res.head())
        print("Stats:")
        print(res.describe())
        
        # Check correlation between HF and Volatility?
        # Just ensure non-zero
        hf_std = res["alpha_dual_hf"].std()
        print(f"HF Signal Std: {hf_std}")
        if hf_std == 0:
            print("WARNING: HF Signal is constant zero!")
            
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
