# adverse_flow_asymmetry

## Hypothesis
Informed traders preferentially consume one side of the book; the asymmetry of QI's second moment (squared positive vs squared negative QI) reveals adverse selection pressure.

## Formula
```
signal_t = clip(EMA_8((EMA_16(max(qi,0)^2) - EMA_16(max(-qi,0)^2))
                / max(EMA_16(max(qi,0)^2) + EMA_16(max(-qi,0)^2), eps)), -1, 1)
```

## References
- Paper 129: Detecting Toxic Flow
- Paper 133: Market Simulation under Adverse Selection
