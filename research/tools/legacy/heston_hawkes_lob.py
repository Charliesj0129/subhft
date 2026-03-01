#!/usr/bin/env python3
"""
Heston + Multivariate Hawkes LOB Data Generator

Combines:
1. Heston Model for mid-price with stochastic volatility
2. Multivariate Hawkes Process for LOB event generation (Ogata's Thinning)

Output: 5-level bid/ask LOB + tick data over 6 months
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import List, Tuple, Dict
import numpy as np
from scipy import stats


@dataclass
class HestonParams:
    """Heston model parameters"""
    mu: float = 0.05 / 252  # Daily drift (5% annual)
    theta: float = 0.04     # Long-term variance (20% vol)
    kappa: float = 2.0      # Mean reversion speed
    xi: float = 0.3         # Vol of vol
    rho: float = -0.7       # Correlation (leverage effect)
    S0: float = 1000.0      # Initial price
    v0: float = 0.04        # Initial variance


@dataclass
class HawkesParams:
    """Multivariate Hawkes parameters for 4-dim process"""
    # Base intensities [MktBuy, MktSell, LimBuy, LimSell]
    mu: np.ndarray = field(default_factory=lambda: np.array([50.0, 50.0, 100.0, 100.0]))
    
    # Kernel matrix (cross-excitation) - 4x4
    # alpha[m, n] = how event type n excites type m
    alpha: np.ndarray = field(default_factory=lambda: np.array([
        [0.5, 0.3, 0.2, 0.1],  # MktBuy excited by...
        [0.3, 0.5, 0.1, 0.2],  # MktSell excited by...
        [0.4, 0.1, 0.3, 0.1],  # LimBuy excited by...
        [0.1, 0.4, 0.1, 0.3],  # LimSell excited by...
    ]))
    
    beta: float = 10.0  # Decay rate (shared)


@dataclass
class LOBParams:
    """LOB construction parameters"""
    tick_size: float = 1.0
    n_levels: int = 5
    base_depth: int = 100
    depth_decay: float = 0.7  # Geometric decay per level
    spread_mean: float = 2.0  # Mean spread in ticks
    spread_kappa: float = 5.0  # Spread mean reversion speed


@dataclass
class SimConfig:
    """Overall simulation config"""
    n_days: int = 125  # ~6 months
    events_per_day: int = 16000  # ~1 event per second for 4.5hr
    dt_heston: float = 1.0 / (252 * 16000)  # Sub-event time step
    seed: int = 42
    
    heston: HestonParams = field(default_factory=HestonParams)
    hawkes: HawkesParams = field(default_factory=HawkesParams)
    lob: LOBParams = field(default_factory=LOBParams)


class HestonSimulator:
    """Heston model mid-price generator with Euler-Maruyama"""
    
    def __init__(self, params: HestonParams, seed: int = 42):
        self.p = params
        self.rng = np.random.default_rng(seed)
        
    def simulate(self, n_steps: int, dt: float) -> Tuple[np.ndarray, np.ndarray]:
        """
        Returns:
            S: price path
            v: variance path
        """
        S = np.zeros(n_steps)
        v = np.zeros(n_steps)
        S[0] = self.p.S0
        v[0] = self.p.v0
        
        # Generate correlated noise via Cholesky
        Z1 = self.rng.standard_normal(n_steps)
        Zx = self.rng.standard_normal(n_steps)
        Z_S = Z1
        Z_v = self.p.rho * Z1 + np.sqrt(1 - self.p.rho**2) * Zx
        
        for t in range(1, n_steps):
            v_prev = max(v[t-1], 0)  # Full Truncation
            sqrt_v = np.sqrt(v_prev)
            sqrt_dt = np.sqrt(dt)
            
            # Variance process (CIR)
            v[t] = v[t-1] + self.p.kappa * (self.p.theta - v_prev) * dt \
                   + self.p.xi * sqrt_v * sqrt_dt * Z_v[t]
            
            # Price process (GBM with stochastic vol)
            S[t] = S[t-1] * np.exp(
                (self.p.mu - 0.5 * v_prev) * dt + sqrt_v * sqrt_dt * Z_S[t]
            )
        
        return S, v


class MultivariateHawkes:
    """4-dimensional Hawkes process with Ogata's Thinning (Optimized O(1) update)"""
    
    def __init__(self, params: HawkesParams, seed: int = 42):
        self.p = params
        self.rng = np.random.default_rng(seed)
        self.dim = len(params.mu)
        
    def simulate(self, T: float, max_events: int = 1000000) -> List[Tuple[float, int]]:
        """
        Ogata's Thinning with O(1) exponential kernel update trick.
        
        Key insight: For exponential kernel, intensity update is:
        λ(t) = μ + Σ α_n * R_n(t)
        where R_n(t) = e^{-β(t-t_last)} * R_n(t_last) + [event at t_last]
        """
        events: List[Tuple[float, int]] = []
        t = 0.0
        
        # Running intensity contribution per dimension (exponential decay state)
        R = np.zeros((self.dim, self.dim))  # R[m, n] = contribution from type n to type m
        t_last = 0.0
        
        while t < T and len(events) < max_events:
            # Compute current intensity
            decay = np.exp(-self.p.beta * (t - t_last))
            R_decayed = R * decay
            lam = self.p.mu + R_decayed.sum(axis=1)
            lam_total = lam.sum()
            
            # Upper bound
            lambda_bar = lam_total + self.p.alpha.sum()
            
            # Generate candidate time
            dt = self.rng.exponential(1.0 / max(lambda_bar, 1e-10))
            t += dt
            
            if t >= T:
                break
            
            # Decay R to new time
            decay_new = np.exp(-self.p.beta * dt)
            R = R * decay_new
            
            # Compute actual intensity at new time
            lam = self.p.mu + R.sum(axis=1)
            lam_total = lam.sum()
            
            # Accept/reject
            u = self.rng.uniform()
            if u < lam_total / max(lambda_bar, 1e-10):
                # Accept - determine which dimension
                probs = lam / max(lam_total, 1e-10)
                probs = np.maximum(probs, 0)
                probs /= probs.sum()
                dim = self.rng.choice(self.dim, p=probs)
                events.append((t, int(dim)))
                
                # Update R: add excitation from this event
                R[:, dim] += self.p.alpha[:, dim]
            
            t_last = t
        
        return events


class LOBBuilder:
    """Construct 5-level LOB from mid-price and Hawkes events"""
    
    EVENT_MKT_BUY = 0
    EVENT_MKT_SELL = 1
    EVENT_LIM_BUY = 2
    EVENT_LIM_SELL = 3
    
    def __init__(self, params: LOBParams, seed: int = 42):
        self.p = params
        self.rng = np.random.default_rng(seed)
        
    def build(
        self, 
        mid_prices: np.ndarray,
        events: List[Tuple[float, int]],
        total_time: float
    ) -> Dict[str, np.ndarray]:
        """
        Build LOB snapshots at each event time
        
        Returns dict with:
            timestamp, bid_prices, bid_volumes, ask_prices, ask_volumes,
            trade_prices, trade_volumes, trade_sides
        """
        n_events = len(events)
        n_steps = len(mid_prices)
        
        # Output arrays
        timestamps = np.zeros(n_events)
        bid_p = np.zeros((n_events, self.p.n_levels))
        bid_v = np.zeros((n_events, self.p.n_levels))
        ask_p = np.zeros((n_events, self.p.n_levels))
        ask_v = np.zeros((n_events, self.p.n_levels))
        trade_p = np.zeros(n_events)
        trade_v = np.zeros(n_events)
        trade_side = np.zeros(n_events, dtype=np.int8)
        
        # LOB state (depth at each level)
        bid_depth = np.array([
            self.p.base_depth * (self.p.depth_decay ** i) 
            for i in range(self.p.n_levels)
        ])
        ask_depth = bid_depth.copy()
        
        # Spread (OU process)
        spread = self.p.spread_mean
        
        for i, (t, event_type) in enumerate(events):
            # Map event time to mid-price index
            price_idx = min(int(t / total_time * n_steps), n_steps - 1)
            mid = mid_prices[price_idx]
            
            # Update spread (OU)
            spread += self.p.spread_kappa * (self.p.spread_mean - spread) * 0.001
            spread += self.rng.normal(0, 0.1)
            spread = max(self.p.tick_size, spread)
            
            # Snap to tick grid
            half_spread = (spread / 2.0)
            best_bid = np.floor((mid - half_spread) / self.p.tick_size) * self.p.tick_size
            best_ask = np.ceil((mid + half_spread) / self.p.tick_size) * self.p.tick_size
            
            # Process event
            order_size = max(1, int(self.rng.exponential(10)))
            
            if event_type == self.EVENT_MKT_BUY:
                # Consume ask side
                consumed = min(order_size, ask_depth[0])
                ask_depth[0] -= consumed
                trade_p[i] = best_ask
                trade_v[i] = consumed
                trade_side[i] = 1
                
            elif event_type == self.EVENT_MKT_SELL:
                # Consume bid side
                consumed = min(order_size, bid_depth[0])
                bid_depth[0] -= consumed
                trade_p[i] = best_bid
                trade_v[i] = consumed
                trade_side[i] = -1
                
            elif event_type == self.EVENT_LIM_BUY:
                # Add to bid
                level = min(self.rng.geometric(0.5), self.p.n_levels) - 1
                bid_depth[level] += order_size
                trade_p[i] = 0  # No trade
                trade_v[i] = 0
                trade_side[i] = 0
                
            else:  # EVENT_LIM_SELL
                # Add to ask
                level = min(self.rng.geometric(0.5), self.p.n_levels) - 1
                ask_depth[level] += order_size
                trade_p[i] = 0
                trade_v[i] = 0
                trade_side[i] = 0
            
            # Replenish empty levels
            bid_depth = np.maximum(bid_depth, 1)
            ask_depth = np.maximum(ask_depth, 1)
            
            # Record snapshot
            timestamps[i] = t
            for lvl in range(self.p.n_levels):
                bid_p[i, lvl] = best_bid - lvl * self.p.tick_size
                ask_p[i, lvl] = best_ask + lvl * self.p.tick_size
                bid_v[i, lvl] = bid_depth[lvl]
                ask_v[i, lvl] = ask_depth[lvl]
        
        return {
            "timestamp": timestamps,
            "bid_prices": bid_p,
            "bid_volumes": bid_v,
            "ask_prices": ask_p,
            "ask_volumes": ask_v,
            "trade_price": trade_p,
            "trade_volume": trade_v,
            "trade_side": trade_side,
        }


def generate_lob_data(config: SimConfig) -> Dict[str, np.ndarray]:
    """Main entry point for LOB data generation"""
    print(f"[HestonHawkesLOB] Generating {config.n_days} days of data...")
    
    total_events = config.n_days * config.events_per_day
    
    # Trading hours: 4.75 hours = 17100 seconds per day
    trading_seconds_per_day = 4.75 * 3600  # 17100
    total_time_seconds = config.n_days * trading_seconds_per_day
    
    # Layer 1: Heston mid-price
    print("  [1/3] Simulating Heston mid-price...")
    heston = HestonSimulator(config.heston, seed=config.seed)
    S, v = heston.simulate(total_events, config.dt_heston)
    
    # Layer 2: Multivariate Hawkes events (time in SECONDS)
    print("  [2/3] Simulating Hawkes events (Ogata's Thinning)...")
    hawkes = MultivariateHawkes(config.hawkes, seed=config.seed + 1)
    events = hawkes.simulate(total_time_seconds, max_events=total_events)
    print(f"       Generated {len(events)} events")
    
    # Layer 3: Build LOB
    print("  [3/3] Building LOB snapshots...")
    lob_builder = LOBBuilder(config.lob, seed=config.seed + 2)
    lob_data = lob_builder.build(S, events, total_time_seconds)
    
    # Add mid-price and variance for analysis
    lob_data["mid_price"] = S
    lob_data["variance"] = v
    
    # Validation stats
    returns = np.diff(np.log(S))
    kurtosis = stats.kurtosis(returns)
    acf_1 = np.corrcoef(np.abs(returns[:-1]), np.abs(returns[1:]))[0, 1]
    
    print(f"\n[Validation]")
    print(f"  Kurtosis: {kurtosis:.2f} (>3 = fat tails)")
    print(f"  ACF(|r|, 1): {acf_1:.4f} (>0 = vol clustering)")
    print(f"  Events: {len(events)}")
    
    return lob_data


def main():
    parser = argparse.ArgumentParser(description="Heston + Hawkes LOB Generator")
    parser.add_argument("--days", type=int, default=125, help="Trading days (125 ≈ 6 months)")
    parser.add_argument("--events-per-day", type=int, default=16000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=str, default="research/data/heston_hawkes_lob.npz")
    args = parser.parse_args()
    
    config = SimConfig(
        n_days=args.days,
        events_per_day=args.events_per_day,
        seed=args.seed,
    )
    
    data = generate_lob_data(config)
    
    np.savez_compressed(args.out, **data)
    print(f"\n[Saved] {args.out}")


if __name__ == "__main__":
    main()
