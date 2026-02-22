"""
AutoQuant Auditor (Paper 010)

Enforces Strict T+1 Execution and Funding Alignment.
Detects Lookahead Bias and PnL Inflation in backtest results.
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass

@dataclass
class AuditReport:
    passed: bool
    strict_pnl: float
    reported_pnl: float
    discrepancy: float
    notes: str

class ExecutionAuditor:
    def __init__(self, funding_interval_hours: int = 8):
        self.funding_interval = funding_interval_hours
        
    def audit_backtest(self, 
                       signals: pd.Series, 
                       prices: pd.DataFrame, 
                       reported_pnl: float,
                       funding_rates: pd.DataFrame = None) -> AuditReport:
        """
        Audit a strategy by re-calculating PnL using Strict T+1 logic.
        
        signals: Series of target positions at time t (Index=Timestamp).
        prices: DataFrame with 'Open', 'Close' (Index=Timestamp).
        reported_pnl: Total PnL claimed by the strategy.
        funding_rates: DataFrame with 'FundingRate' (Index=Timestamp).
        """
        
        # 1. Align Data
        # Signal at time t should be executed at Open of t+1
        # Shift signals forward by 1 step to match Execution Time
        exec_positions = signals.shift(1).fillna(0.0)
        
        # 2. Calculate Strict PnL
        # PnL = Position * (Open_change) ? 
        # Actually, simpler: 
        # Enter at Open(t). Exit at Open(t+1).
        # Daily PnL = Position(t-1) * (Open(t+1) - Open(t))
        # Or more accurately matches close-to-close if we assume daily rebalance.
        
        # Let's assume we trade at Open.
        # Price executed = Open price.
        # Position held from t to t+1 is determined by signal at t-1.
        
        # Returns from t to t+1: (Open[t+1] - Open[t]) / Open[t]
        # PnL = Position[t] * (Open[t+1] - Open[t])
        
        price_diff = prices['Open'].diff().shift(-1) # Return of holding from Open(t) to Open(t+1)
        # Actually:
        # At time t, we execute based on signal(t-1). We hold until time t+1 (next execution).
        # So we capture price move Open(t) to Open(t+1).
        
        # Vectorized calculation
        aligned_df = pd.DataFrame({
            'pos': exec_positions,
            'open': prices['Open']
        })
        
        # PnL of holding 'pos' from 'open' to next 'open'
        aligned_df['next_open'] = aligned_df['open'].shift(-1)
        aligned_df['pnl_diff'] = aligned_df['next_open'] - aligned_df['open']
        aligned_df['strict_pnl'] = aligned_df['pos'] * aligned_df['pnl_diff']
        
        total_strict_pnl = aligned_df['strict_pnl'].sum()
        
        # 3. Funding Audit (If provided)
        # Funding is usually paid every 8 hours.
        # We check if Funding Rate at time T was known at T.
        # Often datasets index Funding by "Payment Time", but the Rate is determined 8h prior.
        # If backtest uses Rate(T) to trade at T-8, it's Lookahead.
        # Here we just calculate funding cost.
        funding_cost = 0.0
        if funding_rates is not None:
             # Align funding to positions
             # Funding PnL = - Position * Rate * Price
             # Assuming Funding is paid at Close of execution candle (simplification)
             
             common_idx = aligned_df.index.intersection(funding_rates.index)
             funding_aligned = funding_rates.loc[common_idx]
             pos_aligned = aligned_df.loc[common_idx, 'pos']
             price_aligned = aligned_df.loc[common_idx, 'open']
             
             funding_pnl = - (pos_aligned * funding_aligned['FundingRate'] * price_aligned).sum()
             total_strict_pnl += funding_pnl
             
        # 4. Compare
        # Allow small epsilon for floating point
        discrepancy = abs(reported_pnl - total_strict_pnl)
        passed = True
        notes = "Audit Passed"
        
        if discrepancy > abs(reported_pnl) * 0.05: # > 5% Deviation
            passed = False
            notes = "FAIL: Significant PnL Discrepancy. Strategy might be using Lookahead (Exec at Close vs Open) or ignoring Funding."
            
        return AuditReport(passed, total_strict_pnl, reported_pnl, discrepancy, notes)
