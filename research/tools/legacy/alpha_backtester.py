#!/usr/bin/env python3
"""
Alpha Backtester: Unified backtest harness for factor evaluation.

Computes:
- Information Coefficient (IC)
- t-statistic
- Sharpe ratio (simplified)
- Hit rate
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Dict, List
import numpy as np

from factor_registry import FactorRegistry, FactorResult


@dataclass
class BacktestResult:
    """Result of backtesting a single factor"""
    factor_name: str
    paper_id: str
    ic: float           # Information Coefficient
    t_stat: float       # t-statistic
    sharpe: float       # Annualized Sharpe (simplified)
    hit_rate: float     # Fraction of correct sign predictions
    n_samples: int
    is_significant: bool  # t_stat > 2.0
    
    def __repr__(self) -> str:
        sig = "✅" if self.is_significant else "❌"
        return (f"{sig} {self.factor_name:20} | IC={self.ic:+.4f} | "
                f"t={self.t_stat:+.2f} | Sharpe={self.sharpe:+.2f} | "
                f"Hit={self.hit_rate:.1%} | {self.paper_id}")


class AlphaBacktester:
    """Backtest alpha factors against forward returns"""
    
    def __init__(self, horizon: int = 5, significance_threshold: float = 2.0):
        self.horizon = horizon
        self.threshold = significance_threshold
    
    def compute_forward_returns(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        """Compute forward returns at horizon"""
        bid_p = data["bid_prices"][:, 0]
        ask_p = data["ask_prices"][:, 0]
        mid = (bid_p + ask_p) / 2
        
        n = len(mid)
        fwd_ret = np.zeros(n)
        fwd_ret[:-self.horizon] = (mid[self.horizon:] - mid[:-self.horizon]) / mid[:-self.horizon]
        return fwd_ret
    
    def run_single(self, data: Dict[str, np.ndarray], factor: FactorResult) -> BacktestResult:
        """Backtest a single factor"""
        signals = factor.signals
        fwd_ret = self.compute_forward_returns(data)
        
        # Remove NaN/Inf
        valid = np.isfinite(signals) & np.isfinite(fwd_ret) & (signals != 0)
        signals = signals[valid]
        fwd_ret = fwd_ret[valid]
        n = len(signals)
        
        if n < 30:
            return BacktestResult(
                factor_name=factor.factor_name,
                paper_id=factor.paper_id,
                ic=0.0, t_stat=0.0, sharpe=0.0, hit_rate=0.0,
                n_samples=n, is_significant=False
            )
        
        # IC = correlation(signal, forward_return)
        ic = np.corrcoef(signals, fwd_ret)[0, 1]
        if np.isnan(ic):
            ic = 0.0
        
        # t-statistic
        t_stat = ic * np.sqrt(n - 2) / np.sqrt(1 - ic**2 + 1e-10)
        
        # Simplified Sharpe (assumes signal-weighted returns)
        signal_ret = signals * fwd_ret
        if signal_ret.std() > 0:
            sharpe = signal_ret.mean() / signal_ret.std() * np.sqrt(252 * 16000 / self.horizon)
        else:
            sharpe = 0.0
        
        # Hit rate
        correct = (np.sign(signals) == np.sign(fwd_ret)).sum()
        hit_rate = correct / n
        
        is_significant = abs(t_stat) > self.threshold
        
        return BacktestResult(
            factor_name=factor.factor_name,
            paper_id=factor.paper_id,
            ic=ic,
            t_stat=t_stat,
            sharpe=sharpe,
            hit_rate=hit_rate,
            n_samples=n,
            is_significant=is_significant,
        )
    
    def run_all(self, data: Dict[str, np.ndarray]) -> List[BacktestResult]:
        """Backtest all registered factors"""
        factor_results = FactorRegistry.compute_all(data)
        return [self.run_single(data, f) for f in factor_results]


def format_results_markdown(results: List[BacktestResult]) -> str:
    """Format results as markdown for brain recording"""
    lines = [
        "# Significant Alpha Factors",
        "",
        "| Factor | Paper ID | IC | t-stat | Sharpe | Hit Rate |",
        "|--------|----------|-----|--------|--------|----------|",
    ]
    
    significant = [r for r in results if r.is_significant]
    for r in sorted(significant, key=lambda x: -abs(x.t_stat)):
        lines.append(
            f"| {r.factor_name} | {r.paper_id} | "
            f"{r.ic:+.4f} | {r.t_stat:+.2f} | {r.sharpe:+.2f} | {r.hit_rate:.1%} |"
        )
    
    lines.append("")
    lines.append(f"## Summary")
    lines.append(f"- Total factors tested: {len(results)}")
    lines.append(f"- Significant (|t| > 2.0): {len(significant)}")
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Alpha Factor Backtester")
    parser.add_argument("--data", type=str, required=True, help="Path to LOB data (.npz)")
    parser.add_argument("--horizon", type=int, default=5, help="Forward return horizon")
    parser.add_argument("--out", type=str, default="", help="Output markdown path")
    args = parser.parse_args()
    
    # Load data
    print(f"[Backtester] Loading {args.data}...")
    data = dict(np.load(args.data))
    
    print(f"[Backtester] Running {len(FactorRegistry.list_factors())} factors...")
    backtester = AlphaBacktester(horizon=args.horizon)
    results = backtester.run_all(data)
    
    print("\n" + "=" * 80)
    print("BACKTEST RESULTS")
    print("=" * 80)
    for r in sorted(results, key=lambda x: -abs(x.t_stat)):
        print(r)
    print("=" * 80)
    
    significant = [r for r in results if r.is_significant]
    print(f"\nSignificant factors: {len(significant)}/{len(results)}")
    
    if args.out:
        md = format_results_markdown(results)
        with open(args.out, "w") as f:
            f.write(md)
        print(f"\n[Saved] {args.out}")


if __name__ == "__main__":
    main()
