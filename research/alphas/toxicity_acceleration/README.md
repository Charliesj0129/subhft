# toxicity_acceleration

## Hypothesis
When short-term toxicity (EMA-8) exceeds long-term toxicity (EMA-64), informed traders are accelerating their activity; following the OFI direction during acceleration predicts short-term price movement.

## Formula
```
signal_t = clip((EMA_8(raw_tox) - EMA_64(raw_tox)) / max(EMA_64(raw_tox), eps) * sign(ofi), -2, 2)
```

## References
- Paper 129: Cartea et al. "Detecting Toxic Flow"
- Paper 132: Cartea et al. "Brokers and Informed Traders"
