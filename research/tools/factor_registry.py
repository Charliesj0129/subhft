#!/usr/bin/env python3
"""
Factor Registry: Standardized interface for all alpha factors from research papers.

Each factor implements:
- compute(data: Dict) -> np.ndarray  (signal series)
- paper_id: str  (arXiv ID)
- name: str  (human readable)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Type

import numpy as np

try:
    from numba import njit
except ImportError:
    # Fallback if numba is missing
    def njit(func):
        return func


@dataclass
class FactorResult:
    """Result of factor computation"""

    signals: np.ndarray
    factor_name: str
    paper_id: str
    description: str


class FactorBase(ABC):
    """Base class for all alpha factors"""

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def paper_id(self) -> str:
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        pass

    @abstractmethod
    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        """
        Compute factor signal from LOB data.

        Args:
            data: Dict with keys like 'bid_prices', 'ask_prices', 'bid_volumes', etc.

        Returns:
            np.ndarray of signals (same length as input)
        """
        pass


# =============================================================================
# Factor Implementations
# =============================================================================


class OFIFactor(FactorBase):
    """
    Order Flow Imbalance (Cont et al. 2014)
    Paper: 2408.03594v1 - Forecasting High Frequency Order Flow Imbalance
    """

    @property
    def name(self) -> str:
        return "OFI"

    @property
    def paper_id(self) -> str:
        return "2408.03594v1"

    @property
    def description(self) -> str:
        return "Order Flow Imbalance: bid_flow - ask_flow based on price/size changes"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_p = data["bid_prices"][:, 0]  # L1 bid price
        ask_p = data["ask_prices"][:, 0]  # L1 ask price
        bid_v = data["bid_volumes"][:, 0]
        ask_v = data["ask_volumes"][:, 0]

        n = len(bid_p)
        ofi = np.zeros(n)

        for t in range(1, n):
            # Bid flow
            if bid_p[t] > bid_p[t - 1]:
                bid_flow = bid_v[t]
            elif bid_p[t] < bid_p[t - 1]:
                bid_flow = -bid_v[t - 1]
            else:
                bid_flow = bid_v[t] - bid_v[t - 1]

            # Ask flow
            if ask_p[t] < ask_p[t - 1]:
                ask_flow = ask_v[t]
            elif ask_p[t] > ask_p[t - 1]:
                ask_flow = -ask_v[t - 1]
            else:
                ask_flow = ask_v[t] - ask_v[t - 1]

            ofi[t] = bid_flow - ask_flow

        return ofi


class OBIFactor(FactorBase):
    """
    Order Book Imbalance (Static)
    """

    @property
    def name(self) -> str:
        return "OBI"

    @property
    def paper_id(self) -> str:
        return "2505.17388v1"

    @property
    def description(self) -> str:
        return "Order Book Imbalance: (bid_vol - ask_vol) / (bid_vol + ask_vol)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_v = data["bid_volumes"][:, 0]
        ask_v = data["ask_volumes"][:, 0]
        total = bid_v + ask_v
        obi = np.divide(bid_v - ask_v, total, out=np.zeros_like(bid_v, dtype=float), where=total > 0)
        return obi


class WeightedOBIFactor(FactorBase):
    """
    Weighted Order Book Imbalance (Multi-level)
    """

    @property
    def name(self) -> str:
        return "WOBI"

    @property
    def paper_id(self) -> str:
        return "2312.08927v5"

    @property
    def description(self) -> str:
        return "Weighted OBI across 5 levels with geometric decay"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_v = data["bid_volumes"]  # (N, 5)
        ask_v = data["ask_volumes"]

        n_levels = bid_v.shape[1]
        weights = np.array([0.5**i for i in range(n_levels)])
        weights /= weights.sum()

        weighted_bid = (bid_v * weights).sum(axis=1)
        weighted_ask = (ask_v * weights).sum(axis=1)
        total = weighted_bid + weighted_ask

        wobi = np.divide(weighted_bid - weighted_ask, total, out=np.zeros(len(bid_v), dtype=float), where=total > 0)
        return wobi


class SpreadFactor(FactorBase):
    """
    Bid-Ask Spread normalized
    """

    @property
    def name(self) -> str:
        return "Spread"

    @property
    def paper_id(self) -> str:
        return "2510.08085v1"

    @property
    def description(self) -> str:
        return "Normalized spread: (ask - bid) / mid"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_p = data["bid_prices"][:, 0]
        ask_p = data["ask_prices"][:, 0]
        mid = (bid_p + ask_p) / 2
        spread = np.divide(ask_p - bid_p, mid, out=np.zeros_like(mid), where=mid > 0)
        return spread


class TradeImbalanceFactor(FactorBase):
    """
    Trade Side Imbalance (Rolling)
    """

    @property
    def name(self) -> str:
        return "TradeImbalance"

    @property
    def paper_id(self) -> str:
        return "2506.07711v5"

    @property
    def description(self) -> str:
        return "Rolling sum of trade sides (buys - sells)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        trade_side = data["trade_side"]
        window = 20
        imbalance = np.convolve(trade_side, np.ones(window), mode="same") / window
        return imbalance


class DepthImbalanceFactor(FactorBase):
    """
    Depth Imbalance across levels
    """

    @property
    def name(self) -> str:
        return "DepthImbalance"

    @property
    def paper_id(self) -> str:
        return "2410.08744v3"

    @property
    def description(self) -> str:
        return "Total bid depth vs ask depth imbalance"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_v = data["bid_volumes"].sum(axis=1)
        ask_v = data["ask_volumes"].sum(axis=1)
        total = bid_v + ask_v
        return np.divide(bid_v - ask_v, total, out=np.zeros_like(bid_v, dtype=float), where=total > 0)


class MidPriceMomentumFactor(FactorBase):
    """
    Mid-price momentum (short-term)
    """

    @property
    def name(self) -> str:
        return "MidMomentum"

    @property
    def paper_id(self) -> str:
        return "2110.00771v2"

    @property
    def description(self) -> str:
        return "Short-term mid-price return (5-period)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_p = data["bid_prices"][:, 0]
        ask_p = data["ask_prices"][:, 0]
        mid = (bid_p + ask_p) / 2

        window = 5
        momentum = np.zeros_like(mid)
        momentum[window:] = (mid[window:] - mid[:-window]) / mid[:-window]
        return momentum


class VolatilityFactor(FactorBase):
    """
    Realized volatility (rolling)
    """

    @property
    def name(self) -> str:
        return "RealizedVol"

    @property
    def paper_id(self) -> str:
        return "2503.14814v1"

    @property
    def description(self) -> str:
        return "Rolling realized volatility of mid-price returns"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_p = data["bid_prices"][:, 0]
        ask_p = data["ask_prices"][:, 0]
        mid = (bid_p + ask_p) / 2

        returns = np.diff(np.log(mid + 1e-10), prepend=0)
        # O(n) Welford sliding-window std instead of O(n*w) numpy slicing loop
        return _rolling_std_welford(returns.astype(np.float64), window=20)


class SquareRootImpactFactor(FactorBase):
    """
    Square-root impact model
    Paper: 2506.07711v5 - Square Root Impact
    """

    @property
    def name(self) -> str:
        return "SqrtImpact"

    @property
    def paper_id(self) -> str:
        return "2506.07711v5"

    @property
    def description(self) -> str:
        return "Cumulative square-root impact from trade flow"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        trade_v = data["trade_volume"]
        trade_side = data["trade_side"]

        n = len(trade_v)
        impact = np.zeros(n)
        decay = 0.95  # Decay factor

        for t in range(1, n):
            signed_vol = trade_side[t] * trade_v[t]
            shock = np.sign(signed_vol) * np.sqrt(abs(signed_vol) + 1e-10) * 0.1
            impact[t] = impact[t - 1] * decay + shock

        return impact


class QueuePressureFactor(FactorBase):
    """
    Queue pressure from diffusive LOB model
    Paper: 2511.18117v1 - Diffusive LOB
    """

    @property
    def name(self) -> str:
        return "QueuePressure"

    @property
    def paper_id(self) -> str:
        return "2511.18117v1"

    @property
    def description(self) -> str:
        return "L1 queue pressure: bid_vol - ask_vol at best level"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_v = data["bid_volumes"][:, 0]
        ask_v = data["ask_volumes"][:, 0]
        return bid_v - ask_v


class PriceReversalFactor(FactorBase):
    """
    Short-term price reversal indicator
    Paper: 2110.00771v2 - Metaorder Impact
    """

    @property
    def name(self) -> str:
        return "PriceReversal"

    @property
    def paper_id(self) -> str:
        return "2110.00771v2"

    @property
    def description(self) -> str:
        return "Price deviation from moving average (mean reversion signal)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_p = data["bid_prices"][:, 0]
        ask_p = data["ask_prices"][:, 0]
        mid = (bid_p + ask_p) / 2

        window = 20
        ma = np.convolve(mid, np.ones(window) / window, mode="same")
        deviation = (mid - ma) / (ma + 1e-10)
        return -deviation  # Negative for reversal signal


class VolumeRatioFactor(FactorBase):
    """
    Volume ratio at top levels
    Paper: 2510.06879v1 - Impact Estimation
    """

    @property
    def name(self) -> str:
        return "VolumeRatio"

    @property
    def paper_id(self) -> str:
        return "2510.06879v1"

    @property
    def description(self) -> str:
        return "Ratio of L1 to total depth (liquidity concentration)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_v = data["bid_volumes"]
        ask_v = data["ask_volumes"]

        l1_total = bid_v[:, 0] + ask_v[:, 0]
        all_total = bid_v.sum(axis=1) + ask_v.sum(axis=1)

        ratio = np.divide(l1_total, all_total, out=np.zeros_like(l1_total, dtype=float), where=all_total > 0)
        return ratio


class MicroPriceFactor(FactorBase):
    """
    Microprice weighted by volume imbalance
    Paper: 2312.08927v5 - Compound Hawkes LOB
    """

    @property
    def name(self) -> str:
        return "MicroPrice"

    @property
    def paper_id(self) -> str:
        return "2312.08927v5"

    @property
    def description(self) -> str:
        return "Microprice deviation from mid: (vb*pa + va*pb)/(va+vb) - mid"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_p = data["bid_prices"][:, 0]
        ask_p = data["ask_prices"][:, 0]
        bid_v = data["bid_volumes"][:, 0]
        ask_v = data["ask_volumes"][:, 0]

        mid = (bid_p + ask_p) / 2
        total_v = bid_v + ask_v

        microprice = np.divide(bid_v * ask_p + ask_v * bid_p, total_v, out=mid.copy(), where=total_v > 0)

        return microprice - mid


class DepthSlopeFactor(FactorBase):
    """
    Depth slope across levels
    Paper: 2410.08744v3 - Small Tick LOB
    """

    @property
    def name(self) -> str:
        return "DepthSlope"

    @property
    def paper_id(self) -> str:
        return "2410.08744v3"

    @property
    def description(self) -> str:
        return "Slope of depth decay (bid vs ask asymmetry)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_v = data["bid_volumes"]
        ask_v = data["ask_volumes"]

        # Vectorized Slope Calculation
        # x = [1, 2, 3, 4, 5]
        # slope = cov(x, y) / var(x)
        # sum(x) = 15, mean(x) = 3
        # var(x) = 2
        # cov(x, y) = mean(xy) - mean(x)mean(y)

        n_levels = bid_v.shape[1]
        x = np.arange(1, n_levels + 1)
        mean_x = np.mean(x)
        var_x = np.var(x)

        # y = log(v + 1)
        log_bid = np.log(bid_v + 1)
        log_ask = np.log(ask_v + 1)

        # Mean(y) per time step
        mean_log_bid = np.mean(log_bid, axis=1, keepdims=True)
        mean_log_ask = np.mean(log_ask, axis=1, keepdims=True)

        # Mean(xy)
        # Einsum: (t, n) * (n,) -> (t,)
        mean_xy_bid = np.dot(log_bid, x) / n_levels
        mean_xy_ask = np.dot(log_ask, x) / n_levels

        # Slope = (Mean(xy) - Mean(x)Mean(y)) / Var(x)
        slope_bid = (mean_xy_bid - mean_x * mean_log_bid.flatten()) / var_x
        slope_ask = (mean_xy_ask - mean_x * mean_log_ask.flatten()) / var_x

        raw_signal = slope_bid - slope_ask

        # EWMA Smoothing (span=100 from grid-search; best OOS Sharpe = 0.88)
        # Delegates to @njit kernel — ~50x faster than pure Python loop.
        return _ewma_1d(raw_signal, 100)


class SpreadTicksFactor(FactorBase):
    """
    Spread in ticks (discrete)
    Paper: 2502.17417v1 - Neural Hawkes
    """

    @property
    def name(self) -> str:
        return "SpreadTicks"

    @property
    def paper_id(self) -> str:
        return "2502.17417v1"

    @property
    def description(self) -> str:
        return "Spread measured in tick units (integer)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_p = data["bid_prices"][:, 0]
        ask_p = data["ask_prices"][:, 0]
        spread_ticks = ask_p - bid_p  # Assumes tick_size normalization
        return spread_ticks


class OFI_LagFactor(FactorBase):
    """
    Lagged OFI for AR forecasting
    Paper: 2408.03594v1 - OFI Forecast
    """

    @property
    def name(self) -> str:
        return "OFI_Lag"

    @property
    def paper_id(self) -> str:
        return "2408.03594v1"

    @property
    def description(self) -> str:
        return "Lagged OFI signal (5-period lag)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        # First compute OFI
        ofi_factor = OFIFactor()
        ofi = ofi_factor.compute(data)

        # Apply lag
        lag = 5
        lagged = np.zeros_like(ofi)
        lagged[lag:] = ofi[:-lag]
        return lagged


class TotalDepthFactor(FactorBase):
    """
    Total book depth (bid + ask)
    Paper: 2312.08927v5 - Compound Hawkes
    """

    @property
    def name(self) -> str:
        return "TotalDepth"

    @property
    def paper_id(self) -> str:
        return "2312.08927v5"

    @property
    def description(self) -> str:
        return "Log total depth (liquidity proxy)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_v = data["bid_volumes"].sum(axis=1)
        ask_v = data["ask_volumes"].sum(axis=1)
        total = bid_v + ask_v
        return np.log(total + 1)


class BidAskRatioFactor(FactorBase):
    """
    Bid to Ask ratio
    Paper: 2502.17417v1 - Neural Hawkes
    """

    @property
    def name(self) -> str:
        return "BidAskRatio"

    @property
    def paper_id(self) -> str:
        return "2502.17417v1"

    @property
    def description(self) -> str:
        return "Log ratio of total bid to ask depth"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_v = data["bid_volumes"].sum(axis=1) + 1e-10
        ask_v = data["ask_volumes"].sum(axis=1) + 1e-10
        return np.log(bid_v / ask_v)


class EWMA_OBI_Factor(FactorBase):
    """
    Exponentially weighted OBI
    Paper: 2408.03594v1 - OFI Forecast
    """

    @property
    def name(self) -> str:
        return "EWMA_OBI"

    @property
    def paper_id(self) -> str:
        return "2408.03594v1"

    @property
    def description(self) -> str:
        return "EWMA smoothed order book imbalance"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        obi_factor = OBIFactor()
        obi = obi_factor.compute(data)

        # EWMA with span=20
        alpha = 2 / (20 + 1)
        ewma = np.zeros_like(obi)
        ewma[0] = obi[0]
        for i in range(1, len(obi)):
            ewma[i] = alpha * obi[i] + (1 - alpha) * ewma[i - 1]
        return ewma


class TradeIntensityFactor(FactorBase):
    """
    Trade intensity (volume/time)
    Paper: 2506.07711v5 - Square Root Impact
    """

    @property
    def name(self) -> str:
        return "TradeIntensity"

    @property
    def paper_id(self) -> str:
        return "2506.07711v5"

    @property
    def description(self) -> str:
        return "Rolling trade volume intensity"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        trade_v = data["trade_volume"]
        window = 10
        intensity = np.convolve(trade_v, np.ones(window) / window, mode="same")
        return intensity


class TransientRepriceFactor(FactorBase):
    """
    Transient Impact / Mean Reversion
    Paper: 2601.13421 - Market Making FX
    """

    @property
    def name(self) -> str:
        return "TransientReprice"

    @property
    def paper_id(self) -> str:
        return "2601.13421"

    @property
    def description(self) -> str:
        return "Short-term mean reversion after trade impact"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        # Reversion: if price moved up, predict down
        bid_p = data["bid_prices"][:, 0]
        ask_p = data["ask_prices"][:, 0]
        mid = (bid_p + ask_p) / 2

        # 10-step return
        k = 10
        ret = np.zeros_like(mid)
        ret[k:] = (mid[k:] - mid[:-k]) / mid[:-k]

        # Signal is negative of recent return (mean reversion)
        return -ret


class SlowFastMomentumFactor(FactorBase):
    """
    Timescale Separation Momentum
    Paper: 2601.11201 - Fast Times, Slow Times
    """

    @property
    def name(self) -> str:
        return "SlowFastMomentum"

    @property
    def paper_id(self) -> str:
        return "2601.11201"

    @property
    def description(self) -> str:
        return "Difference between fast (10) and slow (100) EWMA"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_p = data["bid_prices"][:, 0]
        ask_p = data["ask_prices"][:, 0]
        mid = (bid_p + ask_p) / 2

        # Use JIT-compiled _ewma_1d instead of nested Python loop
        arr = mid.astype(np.float64)
        fast = _ewma_1d(arr, 10)
        slow = _ewma_1d(arr, 100)
        return fast - slow


class LiquidityRecoveryFactor(FactorBase):
    """
    Liquidity Replenishment Rate
    Paper: 2601.13421 - Impact Resilience
    """

    @property
    def name(self) -> str:
        return "LiquidityRecovery"

    @property
    def paper_id(self) -> str:
        return "2601.13421"

    @property
    def description(self) -> str:
        return "Rate of depth recovery after trades"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        # Change in total depth relative to recent volume
        bid_v = data["bid_volumes"].sum(axis=1)
        ask_v = data["ask_volumes"].sum(axis=1)
        total_depth = bid_v + ask_v

        depth_change = np.zeros_like(total_depth)
        depth_change[1:] = total_depth[1:] - total_depth[:-1]

        # Positive depth change = recovery
        # Smooth it
        window = 20
        recovery = np.convolve(depth_change, np.ones(window) / window, mode="same")
        return recovery


class TradeClusteringFactor(FactorBase):
    """
    Trade Arrival Clustering (Volatility/Info)
    Paper: 2601.11958 - Agentic/Non-stationary
    """

    @property
    def name(self) -> str:
        return "TradeClustering"

    @property
    def paper_id(self) -> str:
        return "2601.11958"

    @property
    def description(self) -> str:
        return "Coefficient of variation of inter-trade times"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        times = data["timestamp"]
        # Find trade indices (where trade_price is non-zero)
        # Note: current data doesn't explicitly flag trade timestamps easily in array form
        # But we have 'trade_volume' > 0
        trade_v = data["trade_volume"]
        is_trade = trade_v > 0
        trade_times = times[is_trade]

        if len(trade_times) < 2:
            return np.zeros_like(times)

        inter_arrival = np.diff(trade_times)

        # We need to map this back to full time series.
        # We'll use a rolling window on the last N inter-arrival times.
        # This is expensive to do perfectly in vectorized way, so we approximation:
        # Count trades in windows, effectively intensity variance.

        # Simpler: Rolling std of Volume. High variance = clustering.
        vol_std = np.zeros_like(trade_v)
        window = 50

        # Manual rolling std
        # (Using pandas would be easier but we stick to numpy)
        # rolling_std(X) approx sqrt(E[X^2] - E[X]^2)

        v2 = trade_v**2

        kernel = np.ones(window) / window
        mean_v = np.convolve(trade_v, kernel, mode="same")
        mean_v2 = np.convolve(v2, kernel, mode="same")

        var = mean_v2 - mean_v**2
        clustering = np.sqrt(np.maximum(var, 0))

        return clustering


# =============================================================================
# Registry
# =============================================================================


class HybridFactor(FactorBase):
    """
    Hybrid = DepthSlope (Trend) + EWMA_OBI (Imbalance)
    """

    @property
    def name(self) -> str:
        return "Hybrid_Slope_OBI"

    @property
    def paper_id(self) -> str:
        return "Hybrid"

    @property
    def description(self) -> str:
        return "Combination of DepthSlope (w=100) and EWMA_OBI (w=100)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        # 1. DepthSlope
        ds_factor = DepthSlopeFactor()
        slope = ds_factor.compute(data)

        # 2. EWMA OBI (Reimplementing with w=100)
        obi_factor = OBIFactor()
        obi = obi_factor.compute(data)

        span = 100
        alpha = 2 / (span + 1)

        ewma_obi = np.zeros_like(obi)
        ewma_obi[0] = obi[0]
        # Fast vectorized approximate or loop? Loop is safe
        # Logic: We can trust the loop speed as seen in DepthSlope optimization
        for i in range(1, len(obi)):
            ewma_obi[i] = alpha * obi[i] + (1 - alpha) * ewma_obi[i - 1]

        # 3. Combine
        # Simple equal weight of raw signals (both roughly range -1 to 1)
        return (slope + ewma_obi) / 2


# =============================================================================
# Batch 5: Experimental Factors (Entropy, RSI, Volatility, Rate)
# =============================================================================


class OrderBookEntropyFactor(FactorBase):
    """
    Shannon Entropy of volume distribution across levels.
    """

    @property
    def name(self) -> str:
        return "OrderBookEntropy"

    @property
    def paper_id(self) -> str:
        return "Internal_Exp_01"

    @property
    def description(self) -> str:
        return "Shannon entropy of bid/ask volume distribution"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_v = data["bid_volumes"]
        ask_v = data["ask_volumes"]

        # Normalize to probability distribution
        total_bid = np.sum(bid_v, axis=1, keepdims=True) + 1e-10
        total_ask = np.sum(ask_v, axis=1, keepdims=True) + 1e-10

        p_bid = bid_v / total_bid
        p_ask = ask_v / total_ask

        # H = -sum(p * log(p))
        p_bid_safe = np.where(p_bid > 0, p_bid, 1.0)
        p_ask_safe = np.where(p_ask > 0, p_ask, 1.0)

        h_bid = -np.sum(p_bid * np.log(p_bid_safe), axis=1)
        h_ask = -np.sum(p_ask * np.log(p_ask_safe), axis=1)

        # Signal: Difference in entropy (Dispersion Imbalance)
        return h_bid - h_ask


class HighFreqRSIFactor(FactorBase):
    """
    Relative Strength Index on Micro-Price (Window=100)
    """

    @property
    def name(self) -> str:
        return "HighFreqRSI"

    @property
    def paper_id(self) -> str:
        return "Internal_Exp_02"

    @property
    def description(self) -> str:
        return "RSI(100) on MicroPrice (-1 to 1)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid_p = data["bid_prices"][:, 0]
        ask_p = data["ask_prices"][:, 0]
        bid_v = data["bid_volumes"][:, 0]
        ask_v = data["ask_volumes"][:, 0]
        micro = (bid_p * ask_v + ask_p * bid_v) / (bid_v + ask_v + 1e-10)

        delta = np.diff(micro, prepend=micro[0])
        up = np.maximum(delta, 0)
        down = np.abs(np.minimum(delta, 0))

        span = 100
        alpha = 2 / (span + 1)

        avg_up = np.zeros_like(up)
        avg_down = np.zeros_like(down)
        avg_up[0] = up[0]
        avg_down[0] = down[0]

        for i in range(1, len(up)):
            avg_up[i] = alpha * up[i] + (1 - alpha) * avg_up[i - 1]
            avg_down[i] = alpha * down[i] + (1 - alpha) * avg_down[i - 1]

        rs = avg_up / (avg_down + 1e-10)
        rsi = 100 - (100 / (1 + rs))
        return (rsi - 50) / 50


class SpreadVolatilityFactor(FactorBase):
    """
    Volatility of the Bid-Ask Spread
    """

    @property
    def name(self) -> str:
        return "SpreadVolatility"

    @property
    def paper_id(self) -> str:
        return "Internal_Exp_03"

    @property
    def description(self) -> str:
        return "Rolling std dev of spread (Negative)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        bid = data["bid_prices"][:, 0]
        ask = data["ask_prices"][:, 0]
        spread = ask - bid

        window = 100
        kernel = np.ones(window) / window
        mean_s = np.convolve(spread, kernel, mode="same")
        mean_s2 = np.convolve(spread**2, kernel, mode="same")

        var = mean_s2 - mean_s**2
        std = np.sqrt(np.maximum(var, 0))
        return -std


class TradeArrivalRateFactor(FactorBase):
    """
    Smoothed Trade Arrival Rate (Activity)
    """

    @property
    def name(self) -> str:
        return "TradeArrivalRate"

    @property
    def paper_id(self) -> str:
        return "Internal_Exp_04"

    @property
    def description(self) -> str:
        return "Trade count in rolling window"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        trade_vol = data["trade_volume"]
        is_trade = trade_vol > 0
        window = 100
        count = np.convolve(is_trade.astype(float), np.ones(window), mode="same")
        return count


# =============================================================================
# Batch 6: 2026 Propagator Factors (Impact Reversion, OFI Propagator)
# =============================================================================


class ImpactReversionFactor(FactorBase):
    """
    Transient Impact Reversion (Propagator Model)
    Paper: 2601.03215 - Propagator / 2601.03799
    """

    @property
    def name(self) -> str:
        return "ImpactReversion"

    @property
    def paper_id(self) -> str:
        return "2601.03215"

    @property
    def description(self) -> str:
        return "Reversion signal based on power-law decaying transient impact"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        trade_vol = data["trade_volume"]
        trade_side = data["trade_side"]
        signed_vol = trade_vol * trade_side

        # Power-law kernel: t^-0.5
        # Truncated at 100 lags
        window = 100
        kernel = (np.arange(window) + 1.0) ** -0.5

        # Convolve: returns causal signal if we slice correctly
        # impact[t] = sum(signed_vol[t-k] * kernel[k])
        impact = np.convolve(signed_vol, kernel, mode="full")[: len(signed_vol)]

        # Signal is POSITIVE Impact (Momentum) based on backtest failure of Reversion
        return impact


class PowerLawImbalanceFactor(FactorBase):
    """
    Propagator Imbalance (Power-law decayed OFI)
    Paper: 2601.03799 - Transient Impact
    """

    @property
    def name(self) -> str:
        return "PowerLawImbalance"

    @property
    def paper_id(self) -> str:
        return "2601.03799"

    @property
    def description(self) -> str:
        return "Buy vs Sell power-law decayed volume pressure"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        trade_vol = data["trade_volume"]
        trade_side = data["trade_side"]

        buy_vol = np.where(trade_side > 0, trade_vol, 0)
        sell_vol = np.where(trade_side < 0, trade_vol, 0)

        window = 100
        kernel = (np.arange(window) + 1.0) ** -0.5

        buy_pressure = np.convolve(buy_vol, kernel, mode="full")[: len(buy_vol)]
        sell_pressure = np.convolve(sell_vol, kernel, mode="full")[: len(sell_vol)]

        # Signal flipped to Reversion (Contra-OFI) to diversify
        return -(buy_pressure - sell_pressure)


# =============================================================================
# Batch 7: 2026 Microstructure Factors (Markov, Liquidity Resistance)
# =============================================================================


class MarkovTransitionFactor(FactorBase):
    """
    Adaptive Markov Expectation
    Paper: 2601.04959 (Markov Chain Analysis)
    Logic: Learns E[Return(t+1) | State(t)] adaptively. State(t) = Sign(Return(t)).
    """

    @property
    def name(self) -> str:
        return "MarkovTransition"

    @property
    def paper_id(self) -> str:
        return "2601.04959"

    @property
    def description(self) -> str:
        return "Expected next move condition on current state (Adaptive)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        mid_prices = (data["bid_prices"][:, 0] + data["ask_prices"][:, 0]) / 2.0
        returns = np.diff(mid_prices, prepend=mid_prices[0])
        states = np.sign(returns).astype(int)  # -1, 0, 1

        n = len(states)
        est_up = 0.0
        est_dn = 0.0
        est_flat = 0.0
        alpha = 0.02  # Window ~ 100

        signal = np.zeros(n)

        # Target is next return (Predictive)
        targets = np.roll(returns, -1)
        targets[-1] = 0.0

        # Loop
        for i in range(n - 1):
            s = states[i]

            if s == 1:
                signal[i] = est_up
                est_up = est_up * (1 - alpha) + targets[i] * alpha
            elif s == -1:
                signal[i] = est_dn
                est_dn = est_dn * (1 - alpha) + targets[i] * alpha
            else:
                signal[i] = est_flat
                est_flat = est_flat * (1 - alpha) + targets[i] * alpha

        return -signal


class LiquidityResistanceFactor(FactorBase):
    """
    Liquidity Resistance Ratio
    Paper: 2601.03215 (Concept of Market Resistance)
    Logic: (BidDepth / BuyVol) - (AskDepth / SellVol)
    Measures 'Time to consume liquidity'. High Value -> Bullish (Hard to push down).
    """

    @property
    def name(self) -> str:
        return "LiquidityResistance"

    @property
    def paper_id(self) -> str:
        return "2601.03215"

    @property
    def description(self) -> str:
        return "Depth normalized by trade volume (Resistance Time)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        # Paper suggests resistance of the book. Let's use Top 5 levels sum.
        bid_v = np.sum(data["bid_volumes"], axis=1)
        ask_v = np.sum(data["ask_volumes"], axis=1)

        trade_vol = data["trade_volume"]
        trade_side = data["trade_side"]

        buy_vol = np.where(trade_side > 0, trade_vol, 0)
        sell_vol = np.where(trade_side < 0, trade_vol, 0)

        # Rolling Volume (Turnover)
        window = 100
        kernel = np.ones(window)
        # Add epsilon to avoid div by zero
        roll_buy = np.convolve(buy_vol, kernel, mode="same") + 1.0
        roll_sell = np.convolve(sell_vol, kernel, mode="same") + 1.0

        # Resistance = Depth / Rate
        res_bid = bid_v / roll_sell  # Selling eats Bid liquidity
        res_ask = ask_v / roll_buy  # Buying eats Ask liquidity

        return -(res_bid - res_ask)


# =============================================================================
# Batch 9: Strategic & Institutional Flow (MeanRevertingOFI, InstitutionalOFI)
# =============================================================================


class MeanRevertingOFI(FactorBase):
    """
    Mean-Reverting Order Flow Imbalance (OU Process)
    Paper: 2512.20850v1
    Logic: OFI that decays over time (Ornstein-Uhlenbeck).
    alpha(t) = alpha(t-1) * exp(-k * dt) + impact * sign(trade)
    """

    @property
    def name(self) -> str:
        return "MeanRevertingOFI"

    @property
    def paper_id(self) -> str:
        return "2512.20850v1"

    @property
    def description(self) -> str:
        return "OU Process driven by Order Flow"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        trade_vol = data["trade_volume"]
        trade_side = data["trade_side"]
        timestamps = data["timestamp"]

        return _compute_mean_reverting_ofi(trade_vol, trade_side, timestamps)


@njit
def _compute_mean_reverting_ofi(trade_vol, trade_side, timestamps):
    n = len(timestamps)
    alpha = 0.0

    # Mean reversion speed k.
    # Half-life of 1 second? k = ln(2) / 1.0 approx 0.69
    k = 0.69

    # Impact gamma.
    gamma = 1.0

    signal = np.zeros(n, dtype=np.float64)
    last_t = timestamps[0]

    for i in range(n):
        t = timestamps[i]
        dt = (t - last_t) * 1e-9  # ns to seconds

        # Decay
        alpha *= np.exp(-k * dt)

        # Add Impact from trade
        vol = trade_vol[i]
        if vol > 0:
            side = trade_side[i]
            # Impact linear in sign, or could be log(vol)... paper says "dM" (counting process)
            # which usually implies unit jumps.
            alpha += side * gamma

        signal[i] = alpha
        last_t = t

    return signal


class InstitutionalOFI(FactorBase):
    """
    Institutional Order Flow Imbalance (Matched Filter)
    Paper: 2512.18648v2
    Logic: Raw Dollar Imbalance (not normalized by volume).
    Signal = Sum(Price * SignedVolume) over window.
    """

    @property
    def name(self) -> str:
        return "InstitutionalOFI"

    @property
    def paper_id(self) -> str:
        return "2512.18648v2"

    @property
    def description(self) -> str:
        return "Raw Dollar Imbalance (Capacity Scaled)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        trade_vol = data["trade_volume"]
        trade_side = data["trade_side"]
        mid_prices = (data["bid_prices"][:, 0] + data["ask_prices"][:, 0]) / 2.0

        # Signed Dollar Volume
        signed_dollar_vol = trade_side * trade_vol * mid_prices

        # Rolling Sum window
        window = 1000  # ticks
        kernel = np.ones(window)

        # Convolution for rolling sum
        # Output size same as input
        signal = np.convolve(signed_dollar_vol, kernel, mode="same")

        return signal


class CompositeAlpha(FactorBase):
    """
    Composite Alpha Factor (Batch 10)
    Combines Reversion, Flow, and LOB State
    Weights: 0.4 * Reversal + 0.3 * InstOFI + 0.3 * WOBI
    """

    @property
    def name(self) -> str:
        return "CompositeAlpha"

    @property
    def paper_id(self) -> str:
        return "Batch 10 (Composite)"

    @property
    def description(self) -> str:
        return "Weighted Ensemble: Reversal(40%) + InstOFI(30%) + WOBI(30%)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        # 1. Compute Components
        f_reversal = PriceReversalFactor()
        f_flow = InstitutionalOFI()
        f_state = WeightedOBIFactor()

        s_reversal = f_reversal.compute(data)
        s_flow = f_flow.compute(data)
        s_state = f_state.compute(data)

        # 2. Normalize (Robust Z-Score)
        # We need to handle static data or initial zeros carefully
        def robust_zscore(x):
            med = np.median(x)
            mad = np.median(np.abs(x - med))
            if mad < 1e-8:
                return np.zeros_like(x)
            return (x - med) / (1.4826 * mad)

        z_reversal = robust_zscore(s_reversal)
        z_flow = robust_zscore(s_flow)
        z_state = robust_zscore(s_state)

        # 3. Combine
        # Note: Reversal is usually counter-trend, so sign is already handled in PriceReversalFactor?
        # Let's check: PriceReversalFactor returns -(Mid - SMA) so it IS a directional alpha (buy when low).
        # InstitutionalOFI is directional (buy when flow > 0).
        # WOBI is directional (buy when Bids > Asks).

        final_signal = (0.4 * z_reversal) + (0.3 * z_flow) + (0.3 * z_state)

        return final_signal


# =============================================================================
# Batch 8: Deep Learning & Hawkes (T-KAN, HawkesOFI, Propagator)
# =============================================================================


class NonLinearImbalanceFactor(FactorBase):
    """
    Non-Linear "Dead-Zone" Imbalance
    Paper: 2601.02310 (T-KAN)
    Logic: sign(I) * max(0, abs(I) - threshold)
    """

    @property
    def name(self) -> str:
        return "NonLinearImbalance"

    @property
    def paper_id(self) -> str:
        return "2601.02310"

    @property
    def description(self) -> str:
        return "Order Book Imbalance with noise filtering (dead-zone)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        # Standard OBI first
        bid_v = data["bid_volumes"][:, 0]
        ask_v = data["ask_volumes"][:, 0]
        total = bid_v + ask_v

        # Avoid div zero
        raw_imb = np.divide(bid_v - ask_v, total, out=np.zeros_like(bid_v, dtype=float), where=total > 0)

        # Dead-zone threshold (e.g., 0.1)
        # T-KAN learns this, we define it heuristically or based on noise level.
        threshold = 0.1

        # Apply Logic: sign(x) * max(0, abs(x) - th)
        # Vectorized:
        abs_imb = np.abs(raw_imb)
        clamped = np.maximum(0.0, abs_imb - threshold)
        signal = np.sign(raw_imb) * clamped

        return signal


class HawkesOFI(FactorBase):
    """
    Hawkes Process Driven Order Flow Imbalance
    Paper: 2408.03594v1 (Forecasting OFI)
    Logic: Intensity(Buy) vs Intensity(Sell) using exponential decay
    """

    @property
    def name(self) -> str:
        return "HawkesOFI"

    @property
    def paper_id(self) -> str:
        return "2408.03594v1"

    @property
    def description(self) -> str:
        return "Imbalance of Buy/Sell trade arrival intensities"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        trade_vol = data["trade_volume"]
        trade_side = data["trade_side"]
        timestamps = data["timestamp"]

        # We need a numba kernel for event-based intensity update
        return _compute_hawkes_ofi(trade_vol, trade_side, timestamps)


@njit
def _compute_hawkes_ofi(trade_vol, trade_side, timestamps):
    n = len(timestamps)
    lambda_buy = 0.0
    lambda_sell = 0.0

    # Decay rate (beta). Time in seconds.
    # Suppose we want half-life of 0.1s => beta = ln(2)/0.1 ~= 6.9
    # Paper implies "high frequency", let's pick beta=10.0 (100ms decay)
    beta = 10.0

    signal = np.zeros(n, dtype=np.float64)

    # Timestamps are typically monotonic.
    # Unit: internal hftbacktest format often ns.
    # We convert to seconds for beta.

    last_t = timestamps[0]

    for i in range(n):
        t = timestamps[i]
        dt = (t - last_t) * 1e-9  # ns to seconds

        decay = np.exp(-beta * dt)
        lambda_buy *= decay
        lambda_sell *= decay

        # Update measures if trade occurred
        vol = trade_vol[i]
        side = trade_side[i]

        if vol > 0:
            # Impact of event: +1 or +log(vol) to intensity
            # Using +1 for pure "arrival rate"
            if side > 0:
                lambda_buy += 1.0
            elif side < 0:
                lambda_sell += 1.0

        # Compute imbalance
        total = lambda_buy + lambda_sell
        if total > 1e-6:
            signal[i] = (lambda_buy - lambda_sell) / total
        else:
            signal[i] = 0.0

        last_t = t

    return signal


class PropagatorFactor(FactorBase):
    """
    Transient Price Impact Model (Sum of Exponentials)
    Paper: 2301.05157v2 / Internal alpha_propagator.py
    Logic: Power-law decay approximated by 3 exponential kernels.
    """

    @property
    def name(self) -> str:
        return "Propagator"

    @property
    def paper_id(self) -> str:
        return "2301.05157v2"

    @property
    def description(self) -> str:
        return "Transient price pressure (Sum of Exponentials)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        trade_vol = data["trade_volume"]
        trade_side = data["trade_side"]
        timestamps = data["timestamp"]

        return _compute_propagator(trade_vol, trade_side, timestamps)


@njit
def _compute_propagator(trade_vol, trade_side, timestamps):
    n = len(timestamps)

    # 3 Components: Fast, Medium, Slow
    # Betas from alpha_propagator.py: [100.0, 10.0, 1.0]
    # Weights: [0.5, 0.3, 0.2]
    betas = np.array([100.0, 10.0, 1.0], dtype=np.float64)
    weights = np.array([0.5, 0.3, 0.2], dtype=np.float64)

    # State S_k
    s = np.zeros(3, dtype=np.float64)

    signal = np.zeros(n, dtype=np.float64)
    last_t = timestamps[0]

    for i in range(n):
        t = timestamps[i]
        dt = (t - last_t) * 1e-9

        # Decay
        for k in range(3):
            s[k] *= np.exp(-betas[k] * dt)

        # Add Impact
        vol = trade_vol[i]
        if vol > 0:
            side = trade_side[i]
            # Impact Function: sign * log(1 + vol)
            impact = side * np.log(1.0 + vol)

            for k in range(3):
                s[k] += weights[k] * impact

        # Total
        total_impact = 0.0
        for k in range(3):
            total_impact += s[k]

        signal[i] = total_impact
        last_t = t

    return signal


class HawkesCriticalityFactor(FactorBase):
    """
    Hawkes Process Phase Transition / herding detection factor.
    Paper: 2601.11602v1 - Criticality and herding in order flow via Hawkes intensity.

    Signal: contrarian (-trade_side) when Hawkes intensity exceeds threshold,
    trend-following (+trade_side) otherwise. Zero during warmup.
    """

    def __init__(
        self,
        vol_threshold: float = 1.5,
        mu: float = 0.1,
        alpha: float = 0.3,
        beta: float = 0.5,
        intensity_threshold: float = 0.3,
        warmup: int = 100,
    ) -> None:
        self.vol_threshold = vol_threshold
        self.mu = mu
        self.alpha = alpha
        self.beta = beta
        self.intensity_threshold = intensity_threshold
        self.warmup = warmup

    @property
    def name(self) -> str:
        return "HawkesCriticality"

    @property
    def paper_id(self) -> str:
        return "2601.11602v1"

    @property
    def description(self) -> str:
        return "Hawkes Process Phase Transition / herding regime via intensity threshold"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        trade_vol = data["trade_volume"]
        trade_side = data["trade_side"]
        n = len(trade_vol)
        signal = np.zeros(n, dtype=np.float64)

        decay = np.exp(-self.beta)
        lam = self.mu
        vol_sum = 0.0

        for i in range(n):
            vol_sum += trade_vol[i]
            rolling_mean = vol_sum / (i + 1)

            # Detect event: current volume exceeds threshold * rolling mean
            event = 1.0 if (i > 0 and trade_vol[i] > self.vol_threshold * rolling_mean) else 0.0

            # Discrete-time Hawkes intensity update
            lam = self.mu + (lam - self.mu) * decay + self.alpha * event

            if i < self.warmup:
                signal[i] = 0.0
            elif lam > self.intensity_threshold:
                signal[i] = -trade_side[i]  # Contrarian in herding regime
            else:
                signal[i] = trade_side[i]  # Trend-following in normal regime

        return signal


# Registry
# =============================================================================


class FactorRegistry:
    """Central registry of all factors"""

    _factors: Dict[str, Type[FactorBase]] = {
        # Original 8 factors
        "OFI": OFIFactor,
        "OBI": OBIFactor,
        "WOBI": WeightedOBIFactor,
        "Spread": SpreadFactor,
        "TradeImbalance": TradeImbalanceFactor,
        "DepthImbalance": DepthImbalanceFactor,
        "MidMomentum": MidPriceMomentumFactor,
        "RealizedVol": VolatilityFactor,
        # Batch 2: 6 factors
        "SqrtImpact": SquareRootImpactFactor,
        "QueuePressure": QueuePressureFactor,
        "PriceReversal": PriceReversalFactor,
        "VolumeRatio": VolumeRatioFactor,
        "MicroPrice": MicroPriceFactor,
        "DepthSlope": DepthSlopeFactor,
        # Batch 3: 6 more factors
        "SpreadTicks": SpreadTicksFactor,
        "OFI_Lag": OFI_LagFactor,
        "TotalDepth": TotalDepthFactor,
        "BidAskRatio": BidAskRatioFactor,
        "EWMA_OBI": EWMA_OBI_Factor,
        "TradeIntensity": TradeIntensityFactor,
        # Batch 4: 4 factors
        "TransientReprice": TransientRepriceFactor,
        "SlowFastMomentum": SlowFastMomentumFactor,
        "LiquidityRecovery": LiquidityRecoveryFactor,
        "TradeClustering": TradeClusteringFactor,
        # Hybrid
        "Hybrid_Slope_OBI": HybridFactor,
        # Batch 5 (Experimental)
        "OrderBookEntropy": OrderBookEntropyFactor,
        "HighFreqRSI": HighFreqRSIFactor,
        "SpreadVolatility": SpreadVolatilityFactor,
        "TradeArrivalRate": TradeArrivalRateFactor,
        # Batch 6 (2026 Propagator)
        "ImpactReversion": ImpactReversionFactor,
        "PowerLawImbalance": PowerLawImbalanceFactor,
        # Batch 7 (Microstructure)
        "MarkovTransition": MarkovTransitionFactor,
        "LiquidityResistance": LiquidityResistanceFactor,
        # Batch 8 (Deep Learning/Hawkes)
        "NonLinearImbalance": NonLinearImbalanceFactor,
        "HawkesOFI": HawkesOFI,
        "Propagator": PropagatorFactor,
        # Batch 9 (Strategic/Institutional Flow)
        "MeanRevertingOFI": MeanRevertingOFI,
        "InstitutionalOFI": InstitutionalOFI,
        # Batch 10 (Composite)
        "CompositeAlpha": CompositeAlpha,
        # Batch 11 (Hawkes Criticality / Phase Transition)
        "HawkesCriticality": HawkesCriticalityFactor,
    }

    @classmethod
    def list_factors(cls) -> List[str]:
        return list(cls._factors.keys())

    @classmethod
    def get_factor(cls, name: str) -> FactorBase:
        if name not in cls._factors:
            raise ValueError(f"Unknown factor: {name}. Available: {cls.list_factors()}")
        return cls._factors[name]()

    @classmethod
    def compute_all(cls, data: Dict[str, np.ndarray], n_jobs: int = 1) -> List[FactorResult]:
        """Compute all registered factors.

        Args:
            data:   Input arrays (bid_prices, ask_prices, etc.).
            n_jobs: Worker threads to use (>1 enables ThreadPoolExecutor).
                    GIL is released during numpy operations so thread-level
                    parallelism is effective for compute-bound factor code.
                    Reads HFT_FACTOR_COMPUTE_JOBS env var as default.
        """
        import os
        from concurrent.futures import ThreadPoolExecutor, as_completed

        n_workers = int(os.getenv("HFT_FACTOR_COMPUTE_JOBS", str(n_jobs)))

        def _run_one(name: str) -> FactorResult:
            factor = cls.get_factor(name)
            signals = factor.compute(data)
            return FactorResult(
                signals=signals,
                factor_name=factor.name,
                paper_id=factor.paper_id,
                description=factor.description,
            )

        names = list(cls._factors)
        if n_workers <= 1:
            return [_run_one(name) for name in names]

        results: List[FactorResult] = []
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = {pool.submit(_run_one, name): name for name in names}
            for fut in as_completed(futures):
                results.append(fut.result())
        return results


@njit
def _ewma_1d(raw: np.ndarray, span: int) -> np.ndarray:
    """Recursive 1-D EWMA.  JIT-compiled when numba is available.

    Args:
        raw:  Input signal array (float64).
        span: EWMA span (alpha = 2 / (span + 1)).

    Returns:
        Smoothed array of the same shape.
    """
    alpha = 2.0 / (span + 1)
    out = np.empty_like(raw)
    if len(raw) == 0:
        return out
    out[0] = raw[0]
    for i in range(1, len(raw)):
        out[i] = alpha * raw[i] + (1.0 - alpha) * out[i - 1]
    return out


@njit
def _rolling_std_welford(arr: np.ndarray, window: int) -> np.ndarray:
    """Rolling standard deviation via Welford/Chan sliding-window algorithm.

    O(n) time and O(window) space — avoids O(n*w) numpy slicing.
    JIT-compiled when numba is available.

    Args:
        arr:    Input array (float64).
        window: Rolling window size.

    Returns:
        Array of rolling std values (0.0 for initial warm-up period).
    """
    n = len(arr)
    out = np.zeros(n, dtype=np.float64)
    ring = np.zeros(window, dtype=np.float64)
    idx = 0
    count = 0
    mean = 0.0
    m2 = 0.0
    for i in range(n):
        x_new = float(arr[i])
        if count < window:
            count += 1
            delta = x_new - mean
            mean += delta / count
            m2 += delta * (x_new - mean)
        else:
            x_old = ring[idx]
            old_mean = mean
            mean += (x_new - x_old) / window
            m2 += (x_new - x_old) * (x_new - mean + x_old - old_mean)
            if m2 < 0.0:
                m2 = 0.0
        ring[idx] = x_new
        idx = (idx + 1) % window
        if count > 1 and m2 > 0.0:
            out[i] = (m2 / count) ** 0.5
    return out


@njit
def _compute_matched_filter_flow(
    trade_vol: np.ndarray,
    trade_side: np.ndarray,
    fast_w: int,
    slow_w: int,
) -> np.ndarray:
    """Python/Numba reference implementation of MatchedFilterTradeFlow.

    Signal = RollingSum(SignedFlow, fast_w) / RollingMean(Volume, slow_w)
    Mirrors rust_core.MatchedFilterTradeFlow exactly for parity tests.
    """
    n = len(trade_vol)
    result = np.zeros(n, dtype=np.float64)

    for i in range(n):
        # Not enough history yet (warmup — mirrors Rust: len < slow_window)
        if i < slow_w - 1:
            result[i] = 0.0
            continue

        # Fast window: rolling sum of signed flow
        fast_start = max(0, i - fast_w + 1)
        sum_signed = 0.0
        for j in range(fast_start, i + 1):
            sum_signed += trade_vol[j] * trade_side[j]

        # Slow window: rolling sum of volume
        slow_start = i - slow_w + 1
        sum_vol = 0.0
        for j in range(slow_start, i + 1):
            sum_vol += trade_vol[j]

        capacity = sum_vol / slow_w
        if capacity > 1e-8:
            result[i] = sum_signed / capacity
        else:
            result[i] = 0.0

    return result


def main():
    """Demo: list all factors"""
    print("Registered Alpha Factors:")
    print("-" * 60)
    for name in FactorRegistry.list_factors():
        factor = FactorRegistry.get_factor(name)
        print(f"  {factor.name:20} | {factor.paper_id:15} | {factor.description[:40]}...")
    print("-" * 60)
    print(f"Total: {len(FactorRegistry.list_factors())} factors")


if __name__ == "__main__":
    main()
