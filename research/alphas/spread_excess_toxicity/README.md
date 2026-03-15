# spread_excess_toxicity

## Hypothesis
The gap between current spread and EMA-64 baseline measures adverse selection pressure; spread widening events co-occurring with high QI signal toxic informed flow.

## Formula
```
signal_t = clip(EMA_8((spread - baseline)/baseline * |QI|) * sign(ofi_ema8), -2, 2)
```

## References
- Paper 131: Cartea & Sanchez-Betancourt 2025
