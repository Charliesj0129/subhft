"""Faithful causal port + honest backtest of the LuxAlgo 'Order Flow VWAP
Deviation' Pine v6 indicator.

This is an analysis overlay, not a mechanical strategy. The three computable
order-flow components are ported -- session VWAP +/- sigma bands, pivot
stop-zone liquidity sweeps, and inversion fair-value gaps (IFVGs) -- and driven
through four mechanical trade rules (VWAP-band fade, stop-run sweep reversal,
VWAP-cross trend, IFVG continuation). The anchored volume profile is a last-bar
drawing with no per-bar signal, so it generates no trades. Published defaults
are used verbatim -- no parameter tuning.
"""
