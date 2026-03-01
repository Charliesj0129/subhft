"""
Advanced Verification Script for Research Paper Hypotheses (V2)

This script creates more realistic synthetic data to verify:
1. Paper 026: Unified Theory of Order Flow (Hurst Exponents)
2. Paper 032: Geometric Shear in Order Books (Gamma Distribution)

Key improvements:
- Proper Hawkes process simulation for order flow
- Realistic LOB shape generation
- Separate shear and drift dynamics
"""

import numpy as np
from scipy import stats
from scipy.optimize import minimize
import warnings
warnings.filterwarnings('ignore')

np.random.seed(42)

# =============================================================================
# Hurst Exponent Estimation (Paper 026)
# =============================================================================

def estimate_hurst_dfa(ts: np.ndarray, min_box: int = 4, max_box: int = None) -> tuple:
    """
    Detrended Fluctuation Analysis (DFA) for Hurst exponent.
    More robust than R/S and Variogram for financial data.
    """
    n = len(ts)
    if max_box is None:
        max_box = n // 4
    
    # Cumulative sum (profile)
    profile = np.cumsum(ts - np.mean(ts))
    
    box_sizes = []
    fluctuations = []
    
    for box_size in np.unique(np.logspace(np.log10(min_box), np.log10(max_box), 20).astype(int)):
        if box_size > n // 2:
            continue
            
        n_boxes = n // box_size
        rms_list = []
        
        for i in range(n_boxes):
            segment = profile[i * box_size:(i + 1) * box_size]
            # Linear detrend
            x = np.arange(len(segment))
            coef = np.polyfit(x, segment, 1)
            trend = np.polyval(coef, x)
            rms = np.sqrt(np.mean((segment - trend) ** 2))
            rms_list.append(rms)
        
        if len(rms_list) > 0:
            box_sizes.append(box_size)
            fluctuations.append(np.mean(rms_list))
    
    # Linear regression in log-log space
    log_box = np.log(box_sizes)
    log_fluct = np.log(fluctuations)
    
    slope, intercept, r_value, p_value, std_err = stats.linregress(log_box, log_fluct)
    
    return slope, r_value ** 2


# =============================================================================
# Hawkes Process Simulation (Paper 026)
# =============================================================================

def simulate_hawkes_order_flow(
    T: float = 10000,
    mu: float = 0.5,
    alpha: float = 0.8,
    beta: float = 1.0,
    H_target: float = 0.75
) -> tuple:
    """
    Simulate order flow using a modified Hawkes process.
    
    The key insight from Paper 026:
    - Core Flow (F) has long memory with Hurst H_0 ≈ 0.75
    - Reaction Flow (N) is martingale response
    
    We simulate using fractional Brownian motion + Hawkes.
    """
    dt = 1.0
    n_steps = int(T / dt)
    
    # Generate fractionally integrated noise for Core Flow
    # Using Cholesky decomposition of fGn covariance matrix
    H = H_target
    
    # For large n, use spectral method (more efficient)
    def generate_fbm(n, H):
        """Generate fractional Brownian motion increments"""
        # Spectral method for fGn
        half_n = n // 2 + 1
        k = np.arange(half_n)
        
        # Spectral density of fGn
        f = np.zeros(half_n)
        f[1:] = 2 * np.sin(np.pi * H) * (2 * np.pi * k[1:] / n) ** (-2 * H - 1)
        
        # Generate complex Gaussian noise
        rng = np.random.default_rng(42)
        noise = rng.standard_normal(half_n) + 1j * rng.standard_normal(half_n)
        
        # Apply spectral density
        fbm_freq = np.sqrt(f) * noise
        
        # Inverse FFT
        fbm_full = np.fft.irfft(fbm_freq, n=n)
        
        return fbm_full[:n]
    
    # Core Flow: Persistent (H > 0.5)
    core_flow = generate_fbm(n_steps, H)
    core_flow = core_flow * 10  # Scale
    
    # Reaction Flow: Mean-reverting (simulated as Hawkes residuals)
    events = []
    intensity = np.zeros(n_steps)
    current_intensity = mu
    
    for t in range(n_steps):
        # Update intensity based on past events (exponential decay)
        if len(events) > 0:
            event_arr = np.array(events)
            time_since = t - event_arr
            valid = (time_since > 0) & (time_since < 100)
            if np.sum(valid) > 0:
                current_intensity = mu + alpha * np.sum(np.exp(-beta * time_since[valid]))
            else:
                current_intensity = mu
        else:
            current_intensity = mu
        
        intensity[t] = min(current_intensity, 10)  # Cap
        
        # Generate event
        if np.random.random() < intensity[t] * dt:
            events.append(t)
    
    # Combine: Total signed flow = Core + Reaction noise
    reaction_flow = np.zeros(n_steps)
    for e in events:
        if e < n_steps:
            reaction_flow[e] += np.random.choice([-1, 1])
    
    signed_flow = core_flow + reaction_flow * 0.5
    unsigned_volume = np.abs(signed_flow)
    
    return signed_flow, unsigned_volume


# =============================================================================
# LOB Shape Simulation (Paper 032)
# =============================================================================

def generate_lob_snapshot(
    n_levels: int = 50,
    gamma_bid: float = 2.0,
    lambda_bid: float = 0.1,
    gamma_ask: float = 1.8,
    lambda_ask: float = 0.12,
    noise_std: float = 0.1
) -> tuple:
    """
    Generate LOB snapshot with Gamma-distributed liquidity.
    
    From Paper 032:
    q(x) ∝ x^γ * exp(-λx)
    
    Returns:
        bid_levels, ask_levels, bid_liquidity, ask_liquidity
    """
    levels = np.arange(1, n_levels + 1)
    
    # True Gamma density (not CDF)
    bid_density = stats.gamma.pdf(levels, a=gamma_bid, scale=1/lambda_bid)
    ask_density = stats.gamma.pdf(levels, a=gamma_ask, scale=1/lambda_ask)
    
    # Add noise
    bid_density = bid_density * (1 + noise_std * np.random.randn(n_levels))
    ask_density = ask_density * (1 + noise_std * np.random.randn(n_levels))
    
    # Ensure positive
    bid_density = np.maximum(bid_density, 0.01)
    ask_density = np.maximum(ask_density, 0.01)
    
    return levels, bid_density * 1000, ask_density * 1000


def simulate_lob_timeseries(
    n_snapshots: int = 500,
    n_levels: int = 20
) -> tuple:
    """
    Simulate time series of LOB snapshots with random shear.
    
    Key from Paper 032:
    - Shear (γ_bid - γ_ask) evolves independently of drift
    - Price drift is gauge freedom
    """
    # Parameters evolve as random walks
    gamma_bid = 2.0 + np.cumsum(0.1 * np.random.randn(n_snapshots))
    gamma_ask = 2.0 + np.cumsum(0.1 * np.random.randn(n_snapshots))
    
    # Ensure positive
    gamma_bid = np.maximum(gamma_bid, 0.5)
    gamma_ask = np.maximum(gamma_ask, 0.5)
    
    # Shear = difference in shape parameters
    shear = gamma_bid - gamma_ask
    
    # Price drift: INDEPENDENT random walk
    drift = np.cumsum(0.1 * np.random.randn(n_snapshots))
    
    # Generate LOB snapshots
    snapshots = []
    for i in range(n_snapshots):
        levels, bid_liq, ask_liq = generate_lob_snapshot(
            n_levels=n_levels,
            gamma_bid=gamma_bid[i],
            lambda_bid=0.3,
            gamma_ask=gamma_ask[i],
            lambda_ask=0.3,
            noise_std=0.2
        )
        snapshots.append((levels, bid_liq, ask_liq))
    
    return snapshots, shear, drift, gamma_bid, gamma_ask


# =============================================================================
# Test: Gamma Distribution Fit
# =============================================================================

def fit_gamma_to_lob(levels: np.ndarray, liquidity: np.ndarray) -> tuple:
    """Fit Gamma distribution to LOB liquidity profile"""
    # Normalize to density
    density = liquidity / np.sum(liquidity)
    
    # Method of moments
    mean = np.sum(levels * density)
    var = np.sum((levels - mean) ** 2 * density)
    
    if var > 0:
        shape = mean ** 2 / var
        scale = var / mean
    else:
        shape, scale = 1.0, 1.0
    
    # Predicted density
    pred = stats.gamma.pdf(levels, a=shape, scale=scale)
    pred = pred / np.sum(pred)
    
    # MSE
    mse = np.mean((density - pred) ** 2)
    
    return shape, 1/scale, mse


def fit_exponential_to_lob(levels: np.ndarray, liquidity: np.ndarray) -> tuple:
    """Fit Exponential distribution to LOB liquidity profile"""
    density = liquidity / np.sum(liquidity)
    
    # MLE
    lam = 1 / np.sum(levels * density)
    
    # Predicted
    pred = stats.expon.pdf(levels, scale=1/lam)
    pred = pred / np.sum(pred)
    
    mse = np.mean((density - pred) ** 2)
    
    return lam, mse


# =============================================================================
# Main Verification
# =============================================================================

def verify_paper_026():
    """Verify Paper 026: Unified Theory of Order Flow"""
    print("=" * 70)
    print("VERIFICATION: Paper 026 - Unified Theory of Order Flow")
    print("=" * 70)
    print("\nHypothesis:")
    print("  - Signed Order Flow: H ≈ 0.75 (persistent)")
    print("  - Unsigned Volume: H_vol = H_0 - 0.5 ≈ 0.25 (rough)")
    print("\nMethod: Simulate Hawkes + fBm process, estimate Hurst via DFA")
    print()
    
    results = []
    
    for H_target in [0.65, 0.75, 0.85]:
        print(f"\n--- Simulating with H_target = {H_target} ---")
        
        signed_flow, unsigned_vol = simulate_hawkes_order_flow(
            T=10000,
            H_target=H_target
        )
        
        # Estimate Hurst exponents
        H_signed, r2_signed = estimate_hurst_dfa(signed_flow)
        H_unsigned, r2_unsigned = estimate_hurst_dfa(unsigned_vol)
        
        print(f"  Signed Flow:   H_est = {H_signed:.3f} (R² = {r2_signed:.3f})")
        print(f"  Unsigned Vol:  H_est = {H_unsigned:.3f} (R² = {r2_unsigned:.3f})")
        print(f"  Expected:      H_signed ≈ {H_target:.2f}, H_unsigned ≈ {H_target - 0.5:.2f}")
        
        results.append({
            'H_target': H_target,
            'H_signed': H_signed,
            'H_unsigned': H_unsigned
        })
    
    print("\n" + "-" * 70)
    print("CONCLUSION:")
    
    # Check if the scaling relation holds
    for r in results:
        diff = r['H_signed'] - r['H_unsigned']
        print(f"  H_target={r['H_target']:.2f}: H_signed - H_unsigned = {diff:.3f} (expect ≈ 0.5)")
    
    avg_diff = np.mean([r['H_signed'] - r['H_unsigned'] for r in results])
    if 0.3 < avg_diff < 0.7:
        print(f"\n  ✅ SCALING RELATION SUPPORTED: Avg diff = {avg_diff:.3f}")
    else:
        print(f"\n  ❌ SCALING RELATION NOT SUPPORTED: Avg diff = {avg_diff:.3f}")
    
    return results


def verify_paper_032():
    """Verify Paper 032: Geometric Shear in Order Books"""
    print("\n" + "=" * 70)
    print("VERIFICATION: Paper 032 - Geometric Shear in Order Books")
    print("=" * 70)
    print("\nHypotheses:")
    print("  1. LOB liquidity follows Gamma distribution")
    print("  2. Shear (imbalance) and Drift (price) are uncorrelated")
    print()
    
    # Generate LOB time series
    print("--- Generating LOB time series ---")
    snapshots, shear, drift, gamma_bid, gamma_ask = simulate_lob_timeseries(
        n_snapshots=500,
        n_levels=20
    )
    
    # Test 1: Gamma vs Exponential fit
    print("\n--- Test 1: Gamma vs Exponential Distribution Fit ---")
    
    gamma_wins = 0
    exp_wins = 0
    
    for i in range(0, len(snapshots), 50):  # Sample every 50th snapshot
        levels, bid_liq, ask_liq = snapshots[i]
        
        for side, liq in [('Bid', bid_liq), ('Ask', ask_liq)]:
            shape, scale, gamma_mse = fit_gamma_to_lob(levels, liq)
            lam, exp_mse = fit_exponential_to_lob(levels, liq)
            
            if gamma_mse < exp_mse:
                gamma_wins += 1
            else:
                exp_wins += 1
    
    total = gamma_wins + exp_wins
    print(f"  Gamma wins: {gamma_wins}/{total} ({100*gamma_wins/total:.1f}%)")
    print(f"  Exponential wins: {exp_wins}/{total} ({100*exp_wins/total:.1f}%)")
    
    if gamma_wins > exp_wins:
        print("  ✅ GAMMA DISTRIBUTION SUPPORTED")
    else:
        print("  ⚠️ GAMMA NOT CLEARLY SUPERIOR (may need more data/levels)")
    
    # Test 2: Shear-Drift Correlation
    print("\n--- Test 2: Shear-Drift Correlation ---")
    
    # Use changes (delta) to test independence
    delta_shear = np.diff(shear)
    delta_drift = np.diff(drift)
    
    corr, p_value = stats.spearmanr(delta_shear, delta_drift)
    
    print(f"  Spearman correlation (Δshear vs Δdrift): ρ = {corr:.4f}")
    print(f"  p-value: {p_value:.4f}")
    
    if abs(corr) < 0.1:
        print("  ✅ SHEAR-DRIFT DECOUPLING CONFIRMED (|ρ| < 0.1)")
    elif abs(corr) < 0.3:
        print("  ⚠️ WEAK CORRELATION EXISTS (0.1 < |ρ| < 0.3)")
    else:
        print("  ❌ SIGNIFICANT CORRELATION (|ρ| >= 0.3)")
    
    # Additional: Test if estimated gamma tracks true gamma
    print("\n--- Test 3: Gamma Parameter Recovery ---")
    
    estimated_gammas = []
    for i in range(len(snapshots)):
        levels, bid_liq, _ = snapshots[i]
        shape, scale, mse = fit_gamma_to_lob(levels, bid_liq)
        estimated_gammas.append(shape)
    
    corr_gamma, p_gamma = stats.pearsonr(gamma_bid, estimated_gammas)
    print(f"  Correlation (true γ vs estimated γ): r = {corr_gamma:.4f}")
    
    if corr_gamma > 0.7:
        print("  ✅ GAMMA PARAMETERS CAN BE RECOVERED FROM DATA")
    else:
        print("  ⚠️ PARAMETER RECOVERY IS NOISY")


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    print("\n" + "█" * 70)
    print("   ADVANCED RESEARCH PAPER HYPOTHESIS VERIFICATION (V2)")
    print("█" * 70)
    print("\nNote: Using synthetic data designed to test specific hypotheses.")
    print("Real market validation requires actual exchange data.\n")
    
    verify_paper_026()
    verify_paper_032()
    
    print("\n" + "=" * 70)
    print("VERIFICATION COMPLETE")
    print("=" * 70)
