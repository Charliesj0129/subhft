#!/usr/bin/env python3
"""
Realistic High-Frequency Data Generator v2.0

Based on academic research:
- Paper 026: Unified Theory of Order Flow (Hawkes + Hurst H≈0.75)
- Paper 032: Geometric Shear (Gamma LOB distribution)
- TRADES: Diffusion-based LOB simulation concepts

Features:
1. Multivariate Hawkes Process (self-exciting order flow)
2. Power-law kernel for long memory
3. Gamma-distributed LOB liquidity
4. Intraday seasonality (U-shape)
5. Regime switching (Normal/Volatility/Crisis)
6. hftbacktest compatible output format

Usage:
    python generate_realistic_hfd.py --symbol TXF --days 5 --output data/txf_sim.npz
    python generate_realistic_hfd.py --symbol TXF --events 10000000 --regime crisis
"""

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Tuple, Optional, Dict
import warnings

import numpy as np
from numpy.random import default_rng

# Try to import numba for JIT compilation
try:
    from numba import jit, prange
    HAS_NUMBA = True
except ImportError:
    HAS_NUMBA = False
    # Dummy decorator
    def jit(*args, **kwargs):
        def decorator(func):
            return func
        return decorator
    prange = range

warnings.filterwarnings('ignore')

# =============================================================================
# Constants
# =============================================================================

# hftbacktest event types
DEPTH_EVENT = 1
TRADE_EVENT = 2
DEPTH_CLEAR_EVENT = 3
DEPTH_SNAPSHOT_EVENT = 4

# Order sides
BUY = 1
SELL = -1


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class MarketParams:
    """Market simulation parameters"""
    symbol: str = "TXF"
    tick_size: float = 1.0
    lot_size: float = 1.0
    initial_mid: float = 20000.0
    n_levels: int = 10
    
    # Hawkes parameters (calibrated from real data)
    mu_base: float = 10.0       # Base intensity (events/sec)
    alpha_self: float = 0.7     # Self-excitation (higher for clustering)
    alpha_cross: float = 0.4    # Cross-excitation
    beta: float = 5.0           # Decay rate (slower for longer memory)
    
    # Long memory (Power-law kernel)
    use_powerlaw: bool = True
    powerlaw_gamma: float = 0.5  # H_0 ≈ 0.75 -> gamma ≈ 0.5
    
    # LOB shape (Gamma distribution) - REDUCED for more price impact
    gamma_shape: float = 1.5     # Shape parameter
    gamma_scale: float = 2.0     # Scale parameter
    liquidity_base: float = 5.0  # Base quantity per level (REDUCED 10x)
    
    # Intraday seasonality
    trading_hours: float = 5.0   # Hours per day
    seasonality_amplitude: float = 0.4  # U-shape amplitude


class MarketRegime(IntEnum):
    """Market regime types"""
    NORMAL = 0
    HIGH_VOLATILITY = 1
    CRISIS = 2
    LOW_LIQUIDITY = 3


# Regime-specific parameter multipliers
REGIME_PARAMS = {
    MarketRegime.NORMAL: {
        'mu_mult': 1.0,
        'alpha_mult': 1.0,
        'spread_mult': 1.0,
        'liquidity_mult': 1.0,
    },
    MarketRegime.HIGH_VOLATILITY: {
        'mu_mult': 2.0,
        'alpha_mult': 1.5,
        'spread_mult': 1.5,
        'liquidity_mult': 0.7,
    },
    MarketRegime.CRISIS: {
        'mu_mult': 5.0,
        'alpha_mult': 2.0,
        'spread_mult': 3.0,
        'liquidity_mult': 0.3,
    },
    MarketRegime.LOW_LIQUIDITY: {
        'mu_mult': 0.5,
        'alpha_mult': 0.8,
        'spread_mult': 2.0,
        'liquidity_mult': 0.4,
    },
}


@dataclass
class LOBState:
    """Limit Order Book state"""
    mid_price: float
    bid_prices: np.ndarray  # [n_levels]
    ask_prices: np.ndarray  # [n_levels]
    bid_quantities: np.ndarray  # [n_levels]
    ask_quantities: np.ndarray  # [n_levels]
    last_trade_price: float = 0.0
    last_trade_side: int = 0


# =============================================================================
# Core Engine: Hawkes Process Simulation
# =============================================================================

class HawkesEngine:
    """
    Multivariate Hawkes Process simulator with power-law kernel.
    
    Dimensions:
    0: Limit Order Bid
    1: Limit Order Ask
    2: Market Buy (aggressive)
    3: Market Sell (aggressive)
    """
    
    def __init__(self, params: MarketParams, regime: MarketRegime, seed: int = 42):
        self.params = params
        self.regime = regime
        self.rng = default_rng(seed)
        
        # Apply regime multipliers
        regime_mult = REGIME_PARAMS[regime]
        self.mu = params.mu_base * regime_mult['mu_mult']
        
        # 4D intensity
        n_dim = 4
        self.mu_vec = np.array([
            self.mu * 1.0,   # Limit Bid
            self.mu * 1.0,   # Limit Ask
            self.mu * 0.3,   # Market Buy
            self.mu * 0.3,   # Market Sell
        ])
        
        # Excitation matrix [i,j] = effect of j on i
        alpha_s = params.alpha_self * regime_mult['alpha_mult']
        alpha_c = params.alpha_cross * regime_mult['alpha_mult']
        
        self.alpha = np.array([
            [alpha_s, alpha_c * 0.3, alpha_c * 0.5, alpha_c * 0.2],  # Limit Bid
            [alpha_c * 0.3, alpha_s, alpha_c * 0.2, alpha_c * 0.5],  # Limit Ask
            [alpha_c * 0.4, alpha_c * 0.1, alpha_s, alpha_c * 0.3],  # Market Buy
            [alpha_c * 0.1, alpha_c * 0.4, alpha_c * 0.3, alpha_s],  # Market Sell
        ])
        
        self.beta = params.beta
        self.use_powerlaw = params.use_powerlaw
        self.gamma = params.powerlaw_gamma
    
    def _kernel(self, dt: float) -> float:
        """Compute kernel value at time lag dt"""
        if dt <= 0:
            return 0.0
        
        if self.use_powerlaw:
            # Power-law kernel for long memory
            return (self.gamma * (self.beta ** self.gamma) / 
                    ((self.beta + dt) ** (self.gamma + 1)))
        else:
            # Exponential kernel
            return self.beta * np.exp(-self.beta * dt)
    
    def simulate_ogata(
        self, 
        T: float, 
        max_events: int = 10000000
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Simulate using Ogata's thinning algorithm.
        
        Returns:
            times: Event times
            types: Event types (0-3)
        """
        times = []
        types = []
        
        t = 0.0
        n_events = 0
        
        # History for intensity calculation (sliding window)
        history_times = []
        history_types = []
        max_history = 1000  # Keep last N events for efficiency
        
        # Progress tracking
        last_progress = 0
        print(f"  Simulating Hawkes process for T={T:.1f}s...")
        
        while t < T and n_events < max_events:
            # Compute current intensity upper bound
            lambda_bar = np.sum(self.mu_vec)
            
            # Add contribution from history
            for i, (ht, htype) in enumerate(zip(history_times[-max_history:], 
                                                  history_types[-max_history:])):
                dt = t - ht
                if dt > 0 and dt < 100:  # Truncate at 100s
                    kernel_val = self._kernel(dt)
                    lambda_bar += np.sum(self.alpha[:, htype]) * kernel_val
            
            lambda_bar = min(lambda_bar * 1.5, 1000)  # Safety cap
            
            # Generate candidate time
            u = self.rng.random()
            dt = -np.log(u) / lambda_bar
            t += dt
            
            if t >= T:
                break
            
            # Compute actual intensity at new time
            lambda_actual = self.mu_vec.copy()
            
            for ht, htype in zip(history_times[-max_history:], 
                                  history_types[-max_history:]):
                dt_hist = t - ht
                if dt_hist > 0 and dt_hist < 100:
                    kernel_val = self._kernel(dt_hist)
                    lambda_actual += self.alpha[:, htype] * kernel_val
            
            total_lambda = np.sum(lambda_actual)
            
            # Accept/reject
            if self.rng.random() < total_lambda / lambda_bar:
                # Accept - determine event type
                probs = lambda_actual / total_lambda
                event_type = self.rng.choice(4, p=probs)
                
                times.append(t)
                types.append(event_type)
                history_times.append(t)
                history_types.append(event_type)
                n_events += 1
                
                # Trim history
                if len(history_times) > max_history * 2:
                    history_times = history_times[-max_history:]
                    history_types = history_types[-max_history:]
            
            # Progress
            progress = int(t / T * 100)
            if progress > last_progress and progress % 10 == 0:
                print(f"    Progress: {progress}% ({n_events:,} events)")
                last_progress = progress
        
        print(f"  Generated {len(times):,} events")
        return np.array(times), np.array(types)


# =============================================================================
# LOB Simulator
# =============================================================================

class LOBSimulator:
    """
    Limit Order Book simulator with Gamma-distributed liquidity.
    Includes GARCH-like volatility dynamics and jump processes.
    """
    
    def __init__(self, params: MarketParams, regime: MarketRegime, seed: int = 42):
        self.params = params
        self.regime = regime
        self.rng = default_rng(seed)
        
        regime_mult = REGIME_PARAMS[regime]
        self.spread_mult = regime_mult['spread_mult']
        self.liquidity_mult = regime_mult['liquidity_mult']
        
        # GARCH-like volatility state
        self.vol_state = 1.0  # Current volatility multiplier
        self.vol_omega = 0.01  # Base volatility
        self.vol_alpha = 0.15  # Shock persistence
        self.vol_beta = 0.80   # Volatility persistence
        self.last_shock = 0.0
        
        # Jump process parameters
        self.jump_intensity = 0.002  # Probability of jump per event
        self.jump_mean = 5.0         # Mean jump size in ticks
        
        # Initialize LOB state
        self.state = self._init_lob()
    
    def _gamma_liquidity(self, level: int) -> float:
        """Generate liquidity at level using Gamma distribution"""
        from scipy.stats import gamma as gamma_dist
        
        x = level + 1
        density = gamma_dist.pdf(x, a=self.params.gamma_shape, 
                                  scale=self.params.gamma_scale)
        # More conservative liquidity for realistic price impact
        qty = density * self.params.liquidity_base * 10 * self.liquidity_mult
        
        # Reduce liquidity when volatility is high (flight to safety)
        qty /= self.vol_state
        
        # Add higher noise for variability
        qty *= (1 + 0.5 * self.rng.standard_normal())
        return max(qty, 0.5)
    
    def _init_lob(self) -> LOBState:
        """Initialize LOB with Gamma-distributed liquidity"""
        mid = self.params.initial_mid
        tick = self.params.tick_size
        n_levels = self.params.n_levels
        
        spread = tick * self.spread_mult
        best_bid = mid - spread / 2
        best_ask = mid + spread / 2
        
        bid_prices = np.array([best_bid - i * tick for i in range(n_levels)])
        ask_prices = np.array([best_ask + i * tick for i in range(n_levels)])
        
        bid_quantities = np.array([self._gamma_liquidity(i) for i in range(n_levels)])
        ask_quantities = np.array([self._gamma_liquidity(i) for i in range(n_levels)])
        
        return LOBState(
            mid_price=mid,
            bid_prices=bid_prices,
            ask_prices=ask_prices,
            bid_quantities=bid_quantities,
            ask_quantities=ask_quantities
        )
    
    def _update_volatility(self, price_change: float):
        """Update GARCH-like volatility state"""
        shock = abs(price_change) / self.params.tick_size
        self.vol_state = max(0.5, min(5.0, 
            self.vol_omega + 
            self.vol_alpha * shock + 
            self.vol_beta * self.vol_state
        ))
        self.last_shock = shock
    
    def process_event(
        self, 
        event_type: int, 
        time: float
    ) -> List[Tuple]:
        """
        Process a Hawkes event and generate LOB updates.
        
        Returns list of (event_code, price, quantity, side) tuples.
        """
        outputs = []
        tick = self.params.tick_size
        price_before = self.state.mid_price
        
        # Check for jump event
        if self.rng.random() < self.jump_intensity * self.vol_state:
            jump_size = int(self.rng.exponential(self.jump_mean * self.vol_state))
            jump_dir = self.rng.choice([-1, 1])
            for _ in range(max(1, jump_size)):
                self._shift_prices(direction=jump_dir)
            # Generate trade at jump price
            trade_price = self.state.ask_prices[0] if jump_dir > 0 else self.state.bid_prices[0]
            outputs.append((TRADE_EVENT, trade_price, self.rng.poisson(20), jump_dir))
        
        if event_type == 0:  # Limit Bid
            level = self.rng.integers(0, self.params.n_levels)
            qty = self._gamma_liquidity(level) * 0.3
            self.state.bid_quantities[level] += qty
            outputs.append((DEPTH_EVENT, self.state.bid_prices[level], 
                           self.state.bid_quantities[level], BUY))
            
        elif event_type == 1:  # Limit Ask
            level = self.rng.integers(0, self.params.n_levels)
            qty = self._gamma_liquidity(level) * 0.3
            self.state.ask_quantities[level] += qty
            outputs.append((DEPTH_EVENT, self.state.ask_prices[level], 
                           self.state.ask_quantities[level], SELL))
            
        elif event_type == 2:  # Market Buy (aggressive)
            # Trade size scales with volatility (momentum chasers)
            base_qty = self.rng.poisson(15) + self.rng.integers(1, 10)
            trade_qty = max(1, int(base_qty * self.vol_state))
            remaining = trade_qty
            
            # Consume asks
            for i in range(self.params.n_levels):
                if remaining <= 0:
                    break
                if self.state.ask_quantities[i] > 0:
                    filled = min(remaining, self.state.ask_quantities[i])
                    self.state.ask_quantities[i] -= filled
                    remaining -= filled
                    
                    outputs.append((TRADE_EVENT, self.state.ask_prices[i], 
                                   filled, BUY))
                    
                    if self.state.ask_quantities[i] <= 0:
                        outputs.append((DEPTH_EVENT, self.state.ask_prices[i], 
                                       0, SELL))
            
            # Price impact - more aggressive
            if self.state.ask_quantities[0] <= 0:
                # Multiple tick moves possible - volatility dependent
                n_ticks = 1 + self.rng.integers(0, max(1, int(3 * self.vol_state)))
                for _ in range(n_ticks):
                    self._shift_prices(direction=1)
                self._regenerate_far_level(side='ask')
            elif self.state.ask_quantities[0] < 5:
                # Partial impact
                if self.rng.random() < 0.3 * self.vol_state:
                    self._shift_prices(direction=1)
                    self._regenerate_far_level(side='ask')
            
        elif event_type == 3:  # Market Sell (aggressive)
            # Trade size scales with volatility
            base_qty = self.rng.poisson(15) + self.rng.integers(1, 10)
            trade_qty = max(1, int(base_qty * self.vol_state))
            remaining = trade_qty
            
            # Consume bids
            for i in range(self.params.n_levels):
                if remaining <= 0:
                    break
                if self.state.bid_quantities[i] > 0:
                    filled = min(remaining, self.state.bid_quantities[i])
                    self.state.bid_quantities[i] -= filled
                    remaining -= filled
                    
                    outputs.append((TRADE_EVENT, self.state.bid_prices[i], 
                                   filled, SELL))
                    
                    if self.state.bid_quantities[i] <= 0:
                        outputs.append((DEPTH_EVENT, self.state.bid_prices[i], 
                                       0, BUY))
            
            # Price impact - more aggressive
            if self.state.bid_quantities[0] <= 0:
                n_ticks = 1 + self.rng.integers(0, max(1, int(3 * self.vol_state)))
                for _ in range(n_ticks):
                    self._shift_prices(direction=-1)
                self._regenerate_far_level(side='bid')
            elif self.state.bid_quantities[0] < 5:
                if self.rng.random() < 0.3 * self.vol_state:
                    self._shift_prices(direction=-1)
                    self._regenerate_far_level(side='bid')
        
        # Random cancellations (more in high vol)
        if self.rng.random() < 0.1 * self.vol_state:
            side = self.rng.choice(['bid', 'ask'])
            level = self.rng.integers(1, self.params.n_levels)
            
            if side == 'bid' and self.state.bid_quantities[level] > 0:
                cancel_qty = self.state.bid_quantities[level] * self.rng.random() * 0.7
                self.state.bid_quantities[level] -= cancel_qty
                outputs.append((DEPTH_EVENT, self.state.bid_prices[level],
                               self.state.bid_quantities[level], BUY))
            elif side == 'ask' and self.state.ask_quantities[level] > 0:
                cancel_qty = self.state.ask_quantities[level] * self.rng.random() * 0.7
                self.state.ask_quantities[level] -= cancel_qty
                outputs.append((DEPTH_EVENT, self.state.ask_prices[level],
                               self.state.ask_quantities[level], SELL))
        
        # Update volatility state
        price_change = self.state.mid_price - price_before
        self._update_volatility(price_change)
        
        return outputs
    
    def _shift_prices(self, direction: int):
        """Shift all prices by tick size"""
        tick = self.params.tick_size * direction
        self.state.mid_price += tick
        self.state.bid_prices += tick
        self.state.ask_prices += tick
    
    def _regenerate_far_level(self, side: str):
        """Regenerate the farthest level after price shift"""
        if side == 'ask':
            # Shift quantities
            self.state.ask_quantities = np.roll(self.state.ask_quantities, -1)
            self.state.ask_quantities[-1] = self._gamma_liquidity(self.params.n_levels - 1)
        else:
            self.state.bid_quantities = np.roll(self.state.bid_quantities, -1)
            self.state.bid_quantities[-1] = self._gamma_liquidity(self.params.n_levels - 1)


# =============================================================================
# Seasonality Module
# =============================================================================

def intraday_seasonality(t: float, T_day: float, amplitude: float = 0.4) -> float:
    """
    U-shaped intraday seasonality pattern.
    
    High activity at open and close, low at midday.
    """
    # Normalize time within day
    t_norm = (t % T_day) / T_day
    
    # U-shape: high at 0 and 1, low at 0.5
    # Using cosine: cos(2*pi*t) gives U-shape
    seasonal = 1.0 + amplitude * np.cos(2 * np.pi * t_norm)
    
    return max(seasonal, 0.3)


# =============================================================================
# Main Generator
# =============================================================================

def generate_realistic_hfd(
    symbol: str = "TXF",
    n_events: Optional[int] = None,
    n_days: int = 1,
    regime: str = "normal",
    output_path: Optional[str] = None,
    seed: int = 42
) -> np.ndarray:
    """
    Generate realistic high-frequency data.
    
    Args:
        symbol: Symbol name
        n_events: Target number of events (overrides n_days if specified)
        n_days: Number of trading days
        regime: Market regime (normal, high_volatility, crisis, low_liquidity)
        output_path: Path to save output
        seed: Random seed
    
    Returns:
        Structured numpy array in hftbacktest format
    """
    print("=" * 70)
    print("  REALISTIC HIGH-FREQUENCY DATA GENERATOR v2.0")
    print("=" * 70)
    
    # Parse regime
    regime_map = {
        'normal': MarketRegime.NORMAL,
        'high_volatility': MarketRegime.HIGH_VOLATILITY,
        'crisis': MarketRegime.CRISIS,
        'low_liquidity': MarketRegime.LOW_LIQUIDITY,
    }
    market_regime = regime_map.get(regime.lower(), MarketRegime.NORMAL)
    
    # Initialize parameters
    params = MarketParams(symbol=symbol)
    
    # Calculate simulation time
    T_day = params.trading_hours * 3600  # seconds
    
    if n_events is not None:
        # Estimate time needed for n_events
        events_per_sec = params.mu_base * REGIME_PARAMS[market_regime]['mu_mult'] * 4
        T_total = n_events / events_per_sec * 1.5  # 1.5x buffer
    else:
        T_total = T_day * n_days
    
    print(f"\n  Configuration:")
    print(f"    Symbol: {symbol}")
    print(f"    Regime: {regime} ({market_regime.name})")
    print(f"    Target Events: {n_events or 'auto'}")
    print(f"    Simulation Time: {T_total/3600:.1f} hours")
    print(f"    Initial Mid: {params.initial_mid}")
    print()
    
    # Initialize engines
    hawkes = HawkesEngine(params, market_regime, seed)
    lob = LOBSimulator(params, market_regime, seed + 1)
    
    # Simulate Hawkes events
    start_time = time.time()
    times, types = hawkes.simulate_ogata(T_total, max_events=n_events or 10000000)
    
    if n_events and len(times) > n_events:
        times = times[:n_events]
        types = types[:n_events]
    
    print(f"\n  Processing {len(times):,} events through LOB...")
    
    # Generate LOB output
    output_events = []
    base_ts_ns = 1_700_000_000_000_000_000  # Base timestamp
    
    progress_interval = max(1, len(times) // 10)
    
    for i, (t, event_type) in enumerate(zip(times, types)):
        # Apply seasonality to event processing
        season_mult = intraday_seasonality(t, T_day, params.seasonality_amplitude)
        
        # Process through LOB
        lob_outputs = lob.process_event(event_type, t)
        
        # Convert to output format
        ts_ns = base_ts_ns + int(t * 1e9)
        
        for ev_code, price, qty, side in lob_outputs:
            output_events.append((
                ev_code,
                ts_ns,
                ts_ns,  # local_ts = exch_ts
                price,
                qty * season_mult,
                side
            ))
        
        if (i + 1) % progress_interval == 0:
            print(f"    Processed {i+1:,}/{len(times):,} events ({100*(i+1)/len(times):.0f}%)")
    
    # Create structured array
    event_dtype = np.dtype([
        ('ev', np.int64),
        ('exch_ts', np.int64),
        ('local_ts', np.int64),
        ('px', np.float64),
        ('qty', np.float64),
        ('ival', np.int64),
    ])
    
    data = np.zeros(len(output_events), dtype=event_dtype)
    
    for i, (ev, exch_ts, local_ts, px, qty, side) in enumerate(output_events):
        data[i]['ev'] = ev
        data[i]['exch_ts'] = exch_ts
        data[i]['local_ts'] = local_ts
        data[i]['px'] = px
        data[i]['qty'] = qty
        data[i]['ival'] = side
    
    elapsed = time.time() - start_time
    
    print(f"\n  Generation Complete:")
    print(f"    Total Events: {len(data):,}")
    print(f"    Time Elapsed: {elapsed:.1f}s")
    print(f"    Events/sec: {len(data)/elapsed:,.0f}")
    print(f"    Final Mid Price: {lob.state.mid_price:.1f}")
    print(f"    Price Change: {lob.state.mid_price - params.initial_mid:+.1f}")
    
    # Save
    if output_path:
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        np.savez_compressed(output_path, data=data)
        file_size = os.path.getsize(output_path) / (1024 * 1024)
        print(f"\n  Saved to: {output_path} ({file_size:.1f} MB)")
    
    print("=" * 70)
    
    return data


# =============================================================================
# Stylized Facts Validation
# =============================================================================

def validate_stylized_facts(data: np.ndarray) -> Dict[str, bool]:
    """
    Validate that generated data exhibits real market stylized facts.
    """
    print("\n  Validating Stylized Facts...")
    
    # Extract trade prices
    trades = data[data['ev'] == TRADE_EVENT]
    if len(trades) < 100:
        print("    Insufficient trades for validation")
        return {}
    
    prices = trades['px']
    returns = np.diff(np.log(prices))
    returns = returns[~np.isnan(returns) & ~np.isinf(returns)]
    
    results = {}
    
    # 1. Fat tails (kurtosis > 3)
    from scipy.stats import kurtosis
    kurt = kurtosis(returns)
    results['fat_tails'] = kurt > 3
    print(f"    Fat Tails (kurtosis > 3): {'✅' if results['fat_tails'] else '❌'} ({kurt:.2f})")
    
    # 2. Volatility clustering (|r| ACF > 0.1)
    abs_returns = np.abs(returns)
    if len(abs_returns) > 50:
        acf_1 = np.corrcoef(abs_returns[:-1], abs_returns[1:])[0, 1]
        results['vol_clustering'] = acf_1 > 0.1
        print(f"    Vol Clustering (|r| ACF(1) > 0.1): {'✅' if results['vol_clustering'] else '❌'} ({acf_1:.3f})")
    
    # 3. No return autocorrelation
    if len(returns) > 50:
        acf_r = np.corrcoef(returns[:-1], returns[1:])[0, 1]
        results['no_autocorr'] = abs(acf_r) < 0.1
        print(f"    No Return ACF (|ACF(1)| < 0.1): {'✅' if results['no_autocorr'] else '❌'} ({acf_r:.3f})")
    
    pass_rate = sum(results.values()) / len(results) if results else 0
    print(f"\n    Stylized Facts Pass Rate: {pass_rate*100:.0f}%")
    
    return results


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Realistic High-Frequency Data Generator v2.0',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate 1 million events for TXF
  python generate_realistic_hfd.py --symbol TXF --events 1000000 --output data/txf_1m.npz
  
  # Generate 5 days of normal market
  python generate_realistic_hfd.py --symbol TXF --days 5 --output data/txf_5d.npz
  
  # Generate crisis scenario
  python generate_realistic_hfd.py --symbol TXF --events 500000 --regime crisis --output data/txf_crisis.npz
        """
    )
    
    parser.add_argument('--symbol', type=str, default='TXF', help='Symbol name')
    parser.add_argument('--events', type=int, default=None, help='Target number of events')
    parser.add_argument('--days', type=int, default=1, help='Number of trading days')
    parser.add_argument('--regime', type=str, default='normal',
                       choices=['normal', 'high_volatility', 'crisis', 'low_liquidity'],
                       help='Market regime')
    parser.add_argument('--output', '-o', type=str, default=None, help='Output file path')
    parser.add_argument('--seed', type=int, default=42, help='Random seed')
    parser.add_argument('--validate', action='store_true', help='Run stylized facts validation')
    
    args = parser.parse_args()
    
    # Default output path
    if args.output is None:
        suffix = f"_{args.events}ev" if args.events else f"_{args.days}d"
        args.output = f"data/{args.symbol.lower()}_sim{suffix}_{args.regime}.npz"
    
    # Generate
    data = generate_realistic_hfd(
        symbol=args.symbol,
        n_events=args.events,
        n_days=args.days,
        regime=args.regime,
        output_path=args.output,
        seed=args.seed
    )
    
    # Validate
    if args.validate:
        validate_stylized_facts(data)


if __name__ == "__main__":
    main()
