# VPIN BVC Alpha (Paper 134)

## Hypothesis

Volume-synchronized probability of informed trading (VPIN) using bulk volume
classification detects flow toxicity: high VPIN indicates elevated adverse
selection risk and predicts short-term liquidity deterioration.

## Formula

```
V^B_τ = V_τ × Φ((P_τ - P_{τ-1}) / σ_ΔP)     # Bulk Volume Classification
V^S_τ = V_τ - V^B_τ
VPIN = (1/n) × Σ_{i=1}^{n} |V^S_i - V^B_i| / V_bucket
```

Where:
- `Φ` is the standard normal CDF
- `σ_ΔP` is the EMA(32) of |ΔP|
- `V_bucket` is the target volume per bucket (default 1000)
- `n` is the number of filled buckets (max 50)

## References

- Easley, D., Lopez de Prado, M., & O'Hara, M. (2012). "Flow Toxicity and
  Liquidity in a High Frequency World." *Review of Financial Studies*, 25(5),
  1457-1493. [Paper 134]

## Signal Range

[0, 1] — unsigned. Higher values indicate more informed/toxic order flow.
