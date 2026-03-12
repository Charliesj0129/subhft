# imbalance_divergence

## Signal

```
EMA_8((l1_imbalance_ppm - depth_imbalance_ppm) / 1_000_000)
```

## Hypothesis

When L1 imbalance disagrees with deeper book imbalance, informed traders
concentrate at best price. L1 >> depth signals short-term momentum;
L1 << depth signals deeper book knows better.

## Data Fields

- `l1_imbalance_ppm`: Level-1 bid/ask imbalance in parts per million
- `depth_imbalance_ppm`: Full-depth bid/ask imbalance in parts per million

## State

| Slot           | Type  | Description                    |
| -------------- | ----- | ------------------------------ |
| `_div_ema`     | float | EMA of divergence              |
| `_signal`      | float | Current signal value           |
| `_initialized` | bool  | Whether first update happened  |

## Status

DRAFT -- pending Gate A/B validation.
