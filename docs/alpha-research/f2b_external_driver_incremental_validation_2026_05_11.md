# F2-B External Driver Incremental Validation

Date: 2026-05-11

## Final Verdict

```text
TXF lag-return external driver: KILL_EXTERNAL_DRIVER
P2 strict gate:                 keep as execution infrastructure
```

F2-A showed a small positive candidate over P2-only, but F2-B rejects it as an
external-driver alpha. The incremental lift does not survive the required
controls.

## Setup

Command:

```bash
uv run python -m research.experiments.f2_external_driver.f2b_incremental_validation \
  --out outputs/f2_external_driver/f2b_incremental_validation
```

Data:

```text
TMF panel: research/data/derived/p2_fill_events_tmf_smoke
TXF panel: research/data/derived/p2_fill_events_txf_smoke
P2 model:  outputs/p2_exec_predictor/tmf/models
```

Output artifacts:

```text
outputs/f2_external_driver/f2b_incremental_validation/REPORT.md
sha256: 033b0d44f6b3ddfc3eb09efa81b6cda80ac3bd9a9da02015a0e6b12d7d09be12

outputs/f2_external_driver/f2b_incremental_validation/summary.json
sha256: ffcd226ecaff2fd5ada9514bf3acd73fe6f03798c2eb7f70a958fb97d37f52db

outputs/f2_external_driver/f2b_incremental_validation/parameter_grid.csv
sha256: a4eb386217a57c5c87e6cd544086f4c6c7d059c9d70c6dfa35220b4280cb7d48

outputs/f2_external_driver/f2b_incremental_validation/permutation_control.csv
sha256: 7d054aac1d745a8479d55ec1bd73c1f99a53997884c2adf600173bf744244ba8

outputs/f2_external_driver/f2b_incremental_validation/shifted_day_control.csv
sha256: 34dce4d27a9a3bcb49d4120a607cf783c4353ffc77b9a2830dd1ca334d9b3ddb
```

Available labels:

```text
Evaluated horizons: 2000ms, 5000ms
Unsupported in current P2 panel: 3000ms, 8000ms, 10000ms
```

The unsupported horizons require regenerating the P2 fill-event panel and P2
fill models before they can be evaluated consistently.

## Best Candidate Rechecked

Best F2-A cell:

```text
Horizon: 5000ms
TXF lag: 500ms
Active threshold: top 30%, absolute TXF move >= 2 pt
P2 gate: strict top 10%
```

OOS result:

| Metric | Value |
| --- | ---: |
| Candidate raw EV | 5.973 pt |
| Candidate net EV | 1.973 pt |
| P2-only raw EV | 5.717 pt |
| P2-only net EV | 1.717 pt |
| Incremental lift | +0.256 pt |
| Bootstrap CI of daily lift | [-2.059, 1.636] pt |
| Positive lift days | 30% |
| Median daily lift | -0.714 pt |
| Top 3 positive-day lift share | 1.000 |
| Worst daily lift | -4.366 pt |

Interpretation:

```text
The headline lift is small, statistically weak, and not day-stable.
```

## Shifted-Day Control

Shifted TXF day control fails.

| Shift | Real lift | Shifted lift | Real - shifted |
| ---: | ---: | ---: | ---: |
| t-1 TXF | +0.237 pt | +1.820 pt | -1.583 pt |
| t+1 TXF | +1.401 pt | +2.313 pt | -0.912 pt |

Expected:

```text
real aligned lift > shifted-day lift
```

Observed:

```text
shifted-day lift > real aligned lift
```

This is enough to reject the TXF lag-return driver as a causal lead-lag signal.

## Permutation Control

| Null | p50 lift | p95 lift | Real - p95 | Pass |
| --- | ---: | ---: | ---: | --- |
| Random sign | -0.750 pt | +0.033 pt | +0.224 pt | yes |
| Within-day shuffle | +0.082 pt | +0.656 pt | -0.399 pt | no |

Random sign confirms direction is not completely arbitrary.
But within-day shuffle beating the real aligned lift means the timing alignment
does not carry reliable incremental information beyond the same-day signal
distribution and P2 selection.

## Alternate Splits

| Split | Candidate raw EV | P2-only raw EV | Lift |
| --- | ---: | ---: | ---: |
| chrono 70/30 | 5.973 pt | 5.717 pt | +0.256 pt |
| front half / back half | 6.223 pt | 11.252 pt | -5.029 pt |
| even train / odd test | 0.343 pt | 1.863 pt | -1.521 pt |
| odd train / even test | 0.485 pt | 2.808 pt | -2.323 pt |

Only the original F2-A split is positive. The result is split-fragile.

## Parameter Neighborhood

Grid:

```text
horizon: 2000 / 5000 ms
lag:     250 / 500 / 750 / 1000 ms
active:  top20 / top30 / top40
```

At the 5000ms horizon:

```text
positive lift cells: 8 / 12
best lift: +0.313 pt at lag 250ms, top40
F2-A cell lift: +0.256 pt
```

This is the only partially constructive result. There is a small positive
neighborhood around the original cell. However, it is not enough to offset the
failed shifted-day, within-day shuffle, split, and daily-lift controls.

## Mechanism Check

Bucket summary on the best cell:

| Bucket | P2 pass rate | Directional mid return after P2 | Maker raw EV after P2 |
| --- | ---: | ---: | ---: |
| Long | 0.00127 | +6.067 pt | 7.114 pt |
| Short | 0.00116 | +1.176 pt | 4.731 pt |
| Neutral | 0.00233 | n/a | 7.908 pt |

The long bucket has stronger directional markout than the short bucket, so the
TXF sign is not pure noise. But the neutral/P2-only bucket is economically
stronger than the active TXF buckets, which means TXF lag-return is not
selecting the best execution subset after P2.

## Decision

Formal decision:

```text
F2-B KILL_EXTERNAL_DRIVER
```

Reasons:

```text
1. Incremental lift CI crosses zero.
2. Positive lift days only 30%.
3. Median daily lift is negative.
4. Shifted-day controls beat real alignment.
5. Within-day shuffle p95 beats real lift.
6. Alternate splits are negative.
7. P2-only explains the economics.
```

Operational implication:

```text
Do not prototype TXF lag-return + P2 as a strategy.
Do not keep tuning TXF raw lag-return thresholds.
Keep P2 strict gate as execution infrastructure.
Move to the next external driver class or execution-only modeling.
```

## Next Step

Recommended next branch:

```text
F2-C: richer external driver candidates
```

Do not reuse raw TXF lag-return as the main candidate. Better candidates:

```text
1. TXF microprice / executable-mid impulse normalized by TXF spread.
2. TXF move normalized by realized volatility and session.
3. Basis / spot-futures proxy if spot or ETF data are available.
4. Same-instrument TMF lag-return as a negative control before new external drivers.
```

Mandatory rule remains:

```text
Every external candidate must beat P2-only on incremental lift.
```
