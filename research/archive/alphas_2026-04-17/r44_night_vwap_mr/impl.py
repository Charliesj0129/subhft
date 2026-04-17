"""
R44 Night VWAP Mean-Reversion: Signal + Strategy Implementation
================================================================

Signal: VWAP deviation predicts mean-reversion at 30-min horizon
Entry: Limit order at best bid (sell) / best ask (buy) when |VWAP_dev| > threshold
Exit: VWAP cross-back or timeout
Cost: Exchange fee only (0.15 pts RT) — no spread crossing

This module provides the core computation functions, separate from
the backtesting/validation harness in research/scripts/r44_gate_c_validation.py.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class VwapMrParams:
    """Strategy parameters (immutable)."""
    vwap_threshold: float = 30.0      # points
    exit_timeout_bars: int = 30       # minutes
    fill_timeout_bars: int = 10       # minutes
    vol_filter: bool = True
    vol_pct: float = 0.50
    warmup_bars: int = 30
    fee_per_rt: float = 0.15          # exchange fee round-trip


@dataclass(frozen=True)
class VwapSignal:
    """A single VWAP MR signal."""
    bar_idx: int
    timestamp: str
    direction: int         # +1=buy (fade down), -1=sell (fade up)
    vwap_dev: float        # deviation in points
    limit_price: float     # proposed limit order price
    rvol: float            # realized vol at signal time
    vol_regime: str        # "high" or "low"


def compute_session_vwap(bars: pd.DataFrame) -> pd.DataFrame:
    """Add cumulative VWAP and deviation to 1-min bars.

    Expects bars with columns: close, vol (tick count per bar).
    Returns new DataFrame (immutable pattern) with added columns:
      cum_vol, vwap, vwap_dev
    """
    result = bars.copy()
    result["cum_vol"] = result["vol"].cumsum()
    result["cum_pv"] = (result["close"] * result["vol"]).cumsum()
    result["vwap"] = result["cum_pv"] / result["cum_vol"]
    result["vwap_dev"] = result["close"] - result["vwap"]
    return result


def compute_rvol(bars: pd.DataFrame, window: int = 30) -> pd.Series:
    """Trailing realized volatility (std of 1-min returns)."""
    return bars["close"].diff().rolling(window).std()


def generate_signals(
    bars: pd.DataFrame,
    params: VwapMrParams,
) -> list[VwapSignal]:
    """Generate VWAP MR signals from prepared bars.

    Args:
        bars: DataFrame with columns: close, vol, vwap_dev, rvol, best_bid, best_ask
        params: Strategy parameters

    Returns:
        List of VwapSignal objects (no side effects, pure function)
    """
    if len(bars) < params.warmup_bars + 1:
        return []

    vol_threshold = bars["rvol"].quantile(params.vol_pct) if params.vol_filter else 0.0

    signals: list[VwapSignal] = []
    # Track cooldown to avoid overlapping signals
    cooldown_until = 0

    for i in range(params.warmup_bars, len(bars)):
        if i < cooldown_until:
            continue

        row = bars.iloc[i]
        dev = row["vwap_dev"]
        rvol = row["rvol"]

        if np.isnan(rvol) or np.isnan(dev):
            continue

        is_high_vol = rvol > vol_threshold
        if abs(dev) < params.vwap_threshold:
            continue
        if params.vol_filter and not is_high_vol:
            continue

        direction = -1 if dev > 0 else 1

        if direction == -1:
            limit_price = row["best_bid"] if not np.isnan(row.get("best_bid", float("nan"))) else row["close"]
        else:
            limit_price = row["best_ask"] if not np.isnan(row.get("best_ask", float("nan"))) else row["close"]

        signals.append(VwapSignal(
            bar_idx=i,
            timestamp=str(bars.index[i]),
            direction=direction,
            vwap_dev=float(dev),
            limit_price=float(limit_price),
            rvol=float(rvol),
            vol_regime="high" if is_high_vol else "low",
        ))

        # Cooldown: at least fill_timeout + some holding time before next signal
        cooldown_until = i + params.fill_timeout_bars + 1

    return signals


def check_limit_fill(
    bars: pd.DataFrame,
    signal_bar_idx: int,
    direction: int,
    limit_price: float,
    fill_timeout_bars: int,
    latency_bars: int = 1,
) -> tuple[bool, int]:
    """Check if a limit order would be filled.

    Conservative model: price must trade THROUGH our level.
    - Sell limit at bid: filled if bar LOW < limit_price
    - Buy limit at ask: filled if bar HIGH > limit_price

    Returns: (filled, fill_bar_offset)
    """
    start = signal_bar_idx + latency_bars
    end = min(signal_bar_idx + fill_timeout_bars, len(bars))

    for i in range(start, end):
        row = bars.iloc[i]
        if direction == -1 and row["low"] < limit_price:
            return True, i - signal_bar_idx
        if direction == 1 and row["high"] > limit_price:
            return True, i - signal_bar_idx

    return False, 0
