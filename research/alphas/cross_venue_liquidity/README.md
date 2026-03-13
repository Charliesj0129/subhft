# cross_venue_liquidity — Cross-Venue Liquidity Equilibrium

## Hypothesis

Asymmetric liquidity replenishment rates between bid and ask sides reveal
directional support: faster bid recovery signals sustained buying interest,
while faster ask recovery signals selling pressure.

The signal tracks how quickly depth replenishes after being consumed on each
side. A persistent imbalance in recovery rates indicates informed order flow
favoring one side.

## Formula

```
bid_recovery   = max(0, bid_qty - prev_bid)          # positive change = replenishment
ask_recovery   = max(0, ask_qty - prev_ask)
bid_recovery_ema += α4 * (bid_recovery - bid_recovery_ema)   # α4 = 1 - exp(-1/4) ≈ 0.2212
ask_recovery_ema += α4 * (ask_recovery - ask_recovery_ema)
recovery_imbalance = (bid_recovery_ema - ask_recovery_ema) / (bid_recovery_ema + ask_recovery_ema + ε)
signal = clip(EMA_8(recovery_imbalance), -1, 1)       # α8 = 1 - exp(-1/8) ≈ 0.1175
```

### Signal Interpretation

| Signal range | Interpretation |
|-------------|----------------|
| > 0 | Bid recovering faster — buying support |
| < 0 | Ask recovering faster — selling pressure |
| ~ 0 | Balanced recovery |

## Paper References

- **Paper 062**: Cross-Venue Liquidity Equilibrium

## Implementation

- **Complexity**: O(1) per tick (5 scalar float states, `__slots__`)
- **Data fields**: `bid_qty`, `ask_qty`
- **Feature set version**: `lob_shared_v1`

## Status

DRAFT — Target Gate C with synthetic UL5 data.

## Synthetic Data

Generated with `SyntheticLOBConfig` (v1, seed=51):

```bash
python -c "
from research.tools.synth_lob_gen import SyntheticLOBConfig, generate_lob_data
import numpy as np, json, os
config = SyntheticLOBConfig(n_rows=20000, rng_seed=51)
data, meta = generate_lob_data(config)
out = 'research/data/processed/cross_venue_liquidity'
os.makedirs(out, exist_ok=True)
np.save(f'{out}/cross_venue_liquidity_synth_v1.npy', data)
with open(f'{out}/cross_venue_liquidity_synth_v1.npy.meta.json', 'w') as f:
    json.dump(meta, f, indent=2)
"
```
