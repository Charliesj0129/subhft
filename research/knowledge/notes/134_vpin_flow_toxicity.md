# 134 — Flow Toxicity and Liquidity in a High Frequency World

**Authors**: David Easley, Marcos Lopez de Prado, Maureen O'Hara
**Year**: 2012
**Journal**: Review of Financial Studies 25(5), 1457-1493

## Core Contribution

Introduces VPIN (Volume-Synchronized Probability of Informed Trading), a real-time metric
for order flow toxicity that predicted the 2010 Flash Crash ~1 hour ahead.

## Key Concepts

### Volume Clock
Instead of sampling in calendar time, VPIN uses a **volume clock**: data is bucketed into
equal-volume intervals. This synchronizes with market activity and removes the need to model
trade arrival rates explicitly.

### Bulk Volume Classification (BVC)
Classifies buy/sell volume per bar without tick-level trade direction:

```
V^B_τ = V_τ × Φ((P_τ - P_{τ-1}) / σ_ΔP)
V^S_τ = V_τ - V^B_τ
```

Where Φ is the standard normal CDF and σ_ΔP is the rolling standard deviation of price changes.

### VPIN Formula

```
VPIN = (1/n) × Σ_{i=1}^{n} |V^S_i - V^B_i| / V_bucket
```

Where n is the number of volume buckets in the lookback window.

## Relevance to HFT Platform

- VPIN is a leading indicator of toxic flow: rising VPIN → market makers face higher
  adverse selection risk → spreads widen → liquidity deteriorates.
- Can be computed O(1) amortized per tick with a ring buffer of volume buckets.
- Unsigned signal (measures toxicity level, not direction).

## Alpha: `vpin_bvc`

Data fields: `mid_price`, `volume`
Complexity: O(1) amortized (bucket rotation every V_bucket volume units)
