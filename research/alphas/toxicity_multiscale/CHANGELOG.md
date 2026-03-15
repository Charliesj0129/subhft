# Changelog

## 0.1.0 — 2026-03-14

- Initial DRAFT implementation of toxicity_multiscale alpha (paper 129).
- Multi-timescale composite: volatility EMA-16, queue imbalance, spread deviation EMA-64.
- Signal smoothed via EMA-8, clipped to [-2, 2].
