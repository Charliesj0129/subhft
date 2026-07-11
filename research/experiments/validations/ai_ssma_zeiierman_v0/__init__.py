"""Faithful causal port + honest backtest of the Zeiierman 'AI Source Switching
Moving Average' Pine v6 indicator.

The tradeable signal is the AI Supertrend trend-flip (long on flip-up, short/flat
on flip-down). Bands are built on the AI-selected, EMA-smoothed OHLC source whose
width adapts to the model's KNN/neural drive score. Published defaults are used
verbatim -- no parameter tuning.
"""
