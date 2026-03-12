# rough_vol_ofi -- Rough Volatility from Order Flow

## Hypothesis

Order flow volatility roughness (Hurst exponent H < 0.5) indicates
mean-reverting microstructure. When the Hurst exponent is low (rough), OFI
autocorrelation decays quickly, suggesting contrarian price dynamics. When H
is high (smooth/trending), OFI is persistent and the signal favours momentum.

Combining the roughness measure (0.5 - H) with the sign of the OFI EMA yields
a directional signal: contrarian when rough, momentum when smooth.

## Formula

```
ofi         = bid_change - ask_change              # delta-based OFI
mean_fast  += alpha_fast * (ofi - mean_fast)       # alpha_fast = 1 - exp(-1/4)  ~ 0.2212
var_fast   += alpha_fast * ((ofi - mean_fast)^2 - var_fast)
mean_slow  += alpha_slow * (ofi - mean_slow)       # alpha_slow = 1 - exp(-1/16) ~ 0.0606
var_slow   += alpha_slow * ((ofi - mean_slow)^2 - var_slow)
H           = clip(log(var_slow / var_fast) / (2 * log(4)), 0, 1)
hurst_ema  += alpha_ema * (H - hurst_ema)          # alpha_ema  = 1 - exp(-1/16) ~ 0.0606
roughness   = 0.5 - hurst_ema
signal      = clip(roughness * sign(ofi_ema), -1, 1)
```

### Signal Interpretation

| Condition | Interpretation |
|-----------|----------------|
| H < 0.5, OFI > 0 | Rough + bid pressure -> contrarian long signal |
| H < 0.5, OFI < 0 | Rough + ask pressure -> contrarian short signal |
| H > 0.5, OFI > 0 | Smooth + bid pressure -> momentum long (negative roughness cancels) |
| H > 0.5, OFI < 0 | Smooth + ask pressure -> momentum short |

## Paper References

- **Paper 074**: Rough Volatility from Order Flow

## Implementation

- **Complexity**: O(1) per tick (10 scalar float states, `__slots__`)
- **Data fields**: `bid_qty`, `ask_qty`
- **Latency profile**: `shioaji_sim_p95_v2026-03-04`
- **Feature set version**: `lob_shared_v1`

## Status

DRAFT -- Target Gate C with synthetic UL5 data.

## Synthetic Data

Generated with `SyntheticLOBConfig` (v1, rng_seed=50):

```bash
python -c "
from research.tools.synth_lob_gen import SyntheticLOBConfig, generate_lob_data
import numpy as np, json, os
config = SyntheticLOBConfig(n_rows=20000, rng_seed=50)
data, meta = generate_lob_data(config)
os.makedirs('research/data/processed/rough_vol_ofi', exist_ok=True)
np.save('research/data/processed/rough_vol_ofi/rough_vol_ofi_synth_v1.npy', data)
with open('research/data/processed/rough_vol_ofi/rough_vol_ofi_synth_v1.npy.meta.json', 'w') as f:
    json.dump(meta, f, indent=2)
"
```

## Gate C Validation

```bash
python -m research.factory run-gate-c rough_vol_ofi \
  --data research/data/processed/rough_vol_ofi/rough_vol_ofi_synth_v1.npy \
  --latency-profile shioaji_sim_p95_v2026-03-04
```
