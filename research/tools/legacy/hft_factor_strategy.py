#!/usr/bin/env python3
"""
HFT Factor Strategy: Numba-compiled strategy for hftbacktest.

Uses precomputed factor signals to generate buy/sell orders.
"""

from __future__ import annotations

import argparse
import numpy as np

try:
    from numba import njit
    from numba.typed import Dict as NumbaDict
    NUMBA_AVAILABLE = True
except ImportError:
    NUMBA_AVAILABLE = False
    def njit(f): return f  # Fallback


@njit
def factor_strategy_numba(
    timestamps: np.ndarray,
    signals: np.ndarray,
    mid_prices: np.ndarray,
    threshold: float = 0.5,
    position_limit: int = 10,
    tick_size: float = 1.0,
) -> tuple:
    """
    Numba-compiled factor strategy.
    
    Logic:
    - Signal > threshold: Buy
    - Signal < -threshold: Sell
    - Position limit enforced
    
    Returns:
        equity: equity curve
        trades: array of (timestamp, side, price, qty)
    """
    n = len(timestamps)
    equity = np.zeros(n)
    position = 0
    cash = 0.0
    entry_price = 0.0
    
    # Trade log: (ts, side, price, qty)
    max_trades = n // 10 + 1
    trades = np.zeros((max_trades, 4))
    trade_idx = 0
    
    for i in range(n):
        mid = mid_prices[i]
        signal = signals[i]
        
        # Mark-to-market
        equity[i] = cash + position * mid
        
        # Skip if signal is NaN or zero
        if np.isnan(signal) or signal == 0:
            continue
        
        # Trading logic
        if signal > threshold and position < position_limit:
            # Buy
            qty = 1
            price = mid + tick_size  # Cross spread
            position += qty
            cash -= price * qty
            if trade_idx < max_trades:
                trades[trade_idx, 0] = timestamps[i]
                trades[trade_idx, 1] = 1.0
                trades[trade_idx, 2] = price
                trades[trade_idx, 3] = float(qty)
                trade_idx += 1
                
        elif signal < -threshold and position > -position_limit:
            # Sell
            qty = 1
            price = mid - tick_size
            position -= qty
            cash += price * qty
            if trade_idx < max_trades:
                trades[trade_idx, 0] = timestamps[i]
                trades[trade_idx, 1] = -1.0
                trades[trade_idx, 2] = price
                trades[trade_idx, 3] = float(qty)
                trade_idx += 1
    
    # Final equity
    if n > 0:
        equity[-1] = cash + position * mid_prices[-1]
    
    return equity, trades[:trade_idx], position, cash


def compute_metrics(equity: np.ndarray, trades: np.ndarray) -> dict:
    """Compute backtest metrics from equity curve"""
    returns = np.diff(equity)
    
    if len(returns) == 0 or np.std(returns) == 0:
        return {"sharpe": 0.0, "max_dd": 0.0, "n_trades": 0, "pnl": 0.0}
    
    # Sharpe (annualized, assume 16000 events/day, 252 days)
    sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252 * 16000)
    
    # Max drawdown
    peak = np.maximum.accumulate(equity)
    drawdown = (peak - equity) / (peak + 1e-10)
    max_dd = np.max(drawdown)
    
    # PnL
    pnl = equity[-1] - equity[0]
    
    return {
        "sharpe": float(sharpe),
        "max_dd": float(max_dd),
        "n_trades": len(trades),
        "pnl": float(pnl),
    }


def run_factor_backtest(
    data: dict,
    signals: np.ndarray,
    threshold: float = 0.5,
    position_limit: int = 10,
) -> dict:
    """
    Run factor strategy backtest.
    
    Args:
        data: LOB data dict with 'timestamp', 'bid_prices', 'ask_prices'
        signals: precomputed factor signals
        threshold: signal threshold for trading
        position_limit: max position size
    
    Returns:
        dict with equity curve and metrics
    """
    timestamps = data["timestamp"]
    bid_p = data["bid_prices"][:, 0]
    ask_p = data["ask_prices"][:, 0]
    mid_prices = (bid_p + ask_p) / 2
    
    # Align signal length
    n = min(len(timestamps), len(signals))
    timestamps = timestamps[:n]
    signals = signals[:n]
    mid_prices = mid_prices[:n]
    
    # Run strategy
    equity, trades, final_pos, final_cash = factor_strategy_numba(
        timestamps, signals, mid_prices, threshold, position_limit
    )
    
    metrics = compute_metrics(equity, trades)
    metrics["final_position"] = int(final_pos)
    metrics["final_cash"] = float(final_cash)
    
    return {
        "equity": equity,
        "trades": trades,
        "metrics": metrics,
    }


def main():
    parser = argparse.ArgumentParser(description="Run factor backtest with hftbacktest-style simulation")
    parser.add_argument("--data", type=str, required=True, help="LOB data .npz")
    parser.add_argument("--signals", type=str, required=True, help="Factor signals .npy")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--position-limit", type=int, default=10)
    args = parser.parse_args()
    
    print(f"[FactorBacktest] Loading data...")
    data = dict(np.load(args.data))
    signals = np.load(args.signals)
    
    print(f"[FactorBacktest] Running strategy (threshold={args.threshold})...")
    result = run_factor_backtest(data, signals, args.threshold, args.position_limit)
    
    metrics = result["metrics"]
    print("\n" + "=" * 50)
    print("BACKTEST RESULTS")
    print("=" * 50)
    print(f"  Sharpe Ratio: {metrics['sharpe']:.4f}")
    print(f"  Max Drawdown: {metrics['max_dd']:.2%}")
    print(f"  Total Trades: {metrics['n_trades']}")
    print(f"  PnL: {metrics['pnl']:.2f}")
    print(f"  Final Position: {metrics['final_position']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
