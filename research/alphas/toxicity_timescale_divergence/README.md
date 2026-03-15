# toxicity_timescale_divergence

## Hypothesis
Divergence between fast (EMA-4) and slow (EMA-32) queue imbalance, gated by spread excess, identifies informed flow the market maker hasn't adjusted for.

## Formula
```
signal_t = clip((EMA_4(QI) - EMA_32(QI)) * spread_gate, -1, 1)
```

## References
- Paper 129: Cartea et al. "Detecting Toxic Flow"
- Paper 132: Cartea et al. "Brokers and Informed Traders"
