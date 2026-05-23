# F2-A External Driver + P2 Gate Audit

Date: 2026-05-11

## Verdict

F2-A does **not** yet prove a deployable external-driver alpha.

The audit finds a weak TXF->TMF directional edge only after the P2 strict maker
gate is applied, but most of the economics are still explained by P2 alone:

```text
F2-A: WATCH_WEAK_EDGE
P2:   still PROMOTE as maker execution-gate infrastructure
```

Do not promote TXF lag-return as a strategy driver yet.

## Setup

Command:

```bash
uv run python -m research.experiments.f2_external_driver.p2_gated_txf_tmf \
  --out outputs/f2_external_driver/txf_tmf_p2_gate
```

Data:

```text
TMF panel: research/data/derived/p2_fill_events_tmf_smoke
TXF panel: research/data/derived/p2_fill_events_txf_smoke
P2 model:  outputs/p2_exec_predictor/tmf/models
```

Output artifacts:

```text
outputs/f2_external_driver/txf_tmf_p2_gate/REPORT.md
sha256: 4ba30946c19b69a9565ca6a05be967ba000176bfed587545f31cfc64850a895b

outputs/f2_external_driver/txf_tmf_p2_gate/summary.json
sha256: 6f67935113a3b0f2e581cee1ccd5c4adc43d6cb631512b90c2c0e21efe3b4589
```

Split:

```text
Common active days: 31
Train: 2026-01-25 -> 2026-03-24, 21 days
Test:  2026-03-25 -> 2026-04-07, 10 days
```

Model contract:

```text
TXF lag return chooses direction.
P2 strict gate decides whether the selected maker side is allowed.
P2 does not choose direction.
```

## P2-Only Baseline

The necessary baseline is P2 strict gate without any TXF directional driver.

OOS test result:

| Horizon | P2-only raw EV | P2-only net EV | Attempts |
| ---: | ---: | ---: | ---: |
| 500ms | 1.137 pt | -2.863 pt | 307,553 |
| 2000ms | 2.775 pt | -1.225 pt | 45,777 |
| 5000ms | 5.717 pt | 1.717 pt | 34,962 |

This is the key comparator. Any F2 driver must beat this baseline, not merely
beat external-driver-only rows.

## Best F2 Candidate

Best candidate:

```text
Horizon: 5000ms
TXF lag: 500ms
Driver active threshold: top 30%, absolute TXF delta >= 2 pt
P2 gate: strict top 10%, threshold frozen on train days
```

OOS result:

| Metric | Value |
| --- | ---: |
| External active attempts | 784,846 |
| P2-gated attempts | 940 |
| External-only raw EV | 0.225 pt |
| P2-gated raw EV | 5.973 pt |
| P2-gated net EV | 1.973 pt |
| P2-only raw EV | 5.717 pt |
| F2 lift vs P2-only | +0.256 pt |
| Gate pass rate within active rows | 0.0012 |
| Gate lift positive days | 70% |
| Max single-day lift share | 0.263 |

Direction reversal control on the same candidate:

| Direction rule | Attempts | Raw EV | Net EV |
| --- | ---: | ---: | ---: |
| Normal TXF direction | 940 | 5.973 pt | 1.973 pt |
| Inverted TXF direction | 937 | 4.067 pt | 0.067 pt |

Interpretation:

```text
TXF direction contains some information inside the selected active rows.
But the incremental economics over broad P2-only strict gate are small.
```

The second-best F2 candidate beats P2-only by only:

```text
+0.012 pt raw EV
```

That is not enough margin to treat the driver as validated.

## Result Counts

Across 27 combinations:

```text
F2_EDGE_CANDIDATE:              2
P2_ONLY_EXPLAINS_POSITIVE_EV:   6
EXEC_GATE_IMPROVES:            19
KILL:                           0
```

This means P2 consistently improves execution quality, but TXF lag-return only
adds weak marginal value over the P2-only baseline.

## Operational Verdict

Do not start a strategy prototype from F2-A.

Current status:

```text
P2 strict gate:
    PROMOTE as execution infrastructure.

TXF lag-return external driver:
    WATCH_WEAK_EDGE.
    Not strategy-ready.
```

The cleanest reading is:

> P2 is still doing the work. TXF 500ms lag direction may add a small filter,
> but the effect is too thin and sample-sparse to promote.

## Next Research Step

Run F2-B only if it treats P2-only as the mandatory baseline.

Required controls:

```text
1. Direction reversal for every candidate.
2. Shifted-day / permuted TXF control.
3. Alternate chronological splits.
4. Compare TXF lag-return against same-instrument TMF lag-return.
5. Require F2 lift over P2-only, not just lift over external-only.
```

Priority F2-B candidates:

```text
1. TXF microprice or executable mid return, not raw mid only.
2. TXF move normalized by TXF spread / volatility.
3. TXF lead-lag with stricter causal timestamp alignment.
4. Cross-family driver only after same-family controls are clean.
```

Formal decision:

```text
If F2-B cannot produce stable lift over P2-only:
    keep P2 as execution gate only
    stop TXF-lag driver search
    move to richer external drivers or execution-only modeling
```
