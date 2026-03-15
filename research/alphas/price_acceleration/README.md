# price_acceleration

Price acceleration (second derivative of mid-price) captures momentum-of-momentum.
Positive acceleration signals momentum building; negative signals momentum fading or reversal.

## Formula

```
PA_t = EMA_8(delta_t - delta_{t-1})
```

where `delta_t = mid_price_t - mid_price_{t-1}` (first difference of mid-price).

## Data Fields

- `mid_price` (scaled int x10000)

## Status

DRAFT
