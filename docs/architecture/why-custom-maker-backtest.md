# Why Custom Maker Backtest Engine (Alongside hftbacktest)

**Date**: 2026-04-15
**Context**: R47 calibration crisis — same strategy produced -27K to +61K PnL depending on method.

## The Problem

The `hftbacktest` library (v2.4) provides sophisticated queue position simulation based on Guéant-Lehalle-Fernandez-Tapia (GLT) market-making models, including `PowerProbQueueModel` for estimating fill probability at different queue positions. This is a **capable and well-designed** system.

However, when we applied it to TAIFEX micro-futures maker strategies with **default parameters**, the results were 14x too pessimistic. The root cause was **our failure to calibrate**, not a flaw in hftbacktest itself.

## What Went Wrong (Our Mistakes)

### 1. Uncalibrated Default Parameters

`PowerProbQueueModel(3.0)` uses a probability exponent of 3.0 — a default suitable for deeper US equity order books. We never calibrated this for TAIFEX:
- TAIFEX queue depth: 5-50 contracts (shallow)
- US equity queue depth: 500-5000 (deep)
- The 3.0 exponent decays fill probability too aggressively for shallow queues

**Result**: D2 gate suppressed 94.7% of quoting opportunities → PnL = -27,366 pts

With proper calibration (lower exponent for shallow books), the model might produce reasonable results. **This calibration work was never done.**

### 2. Data Export Bug (Our Code, Not hftbacktest)

Our export script (`ch_batch_export.py`) had a `DEPTH_EVENT` accumulation bug — L2-L5 levels accumulated instead of replacing, producing 17 levels instead of 5 and collapsing spread from 4→1 pt. This was a bug in **our data pipeline**, not in hftbacktest's format.

After fixing the export, hftbacktest results improved but the queue model parameters still needed calibration.

### 3. Expedient Choice Over Proper Calibration

Under time pressure during R47 validation (2026-04-10), we chose to build CK-direct backtest instead of calibrating hftbacktest's queue model. This was a pragmatic decision, not a principled one.

## Why We Built MakerEngine Anyway

Even acknowledging our calibration failure, there are legitimate reasons for the CK-direct approach:

### 1. Direct Data Path (No Translation Layer)

```
hftbacktest path:  ClickHouse → export script → .npz → hftbacktest → result
CK-direct path:   ClickHouse → MakerEngine → result
```

Fewer steps = fewer translation bugs. The export bug proved this matters.

### 2. Transparent vs. Model-Based Assumptions

| Aspect | QueueDepletionFill(qf=0.5) | PowerProbQueueModel(3.0) |
|--------|---------------------------|-------------------------|
| Assumption | "You enter at 50% of queue" | "Fill probability decays as p^3.0 with queue position" |
| Parameters | 1 (qf) | 1 (exponent) + internal model |
| Interpretability | Immediate — anyone knows what qf=0.5 means | Requires understanding GLT model |
| Calibration needed | qf is directly measurable from live fills | Exponent requires fitting to fill data |
| Sensitivity | Linear in qf | Non-linear, sensitive near boundaries |

For our current stage (single maker strategy, limited live fill data), the simpler model is easier to reason about and validate.

### 3. Integration with Standardized Pipeline

MakerEngine was designed as part of the `make research` pipeline:
- Auto-selected via `manifest.yaml` `strategy_type: maker`
- Results persisted to `ResultStore` with full provenance
- Gate C maker thresholds from `gate_thresholds.yaml`

Integrating hftbacktest's queue model into the same pipeline would require the same `BacktestEngine` Protocol wrapper — the calibration work is the bottleneck, not the integration.

## Future: Calibrate hftbacktest for TAIFEX

The right long-term approach is **both**:

1. **Calibrate PowerProbQueueModel** for TAIFEX using actual fill data from R47 live/shadow sessions
2. **Cross-validate** CK-direct (qf) vs calibrated hftbacktest on the same data
3. **Use the calibrated model** if it's more accurate (it likely will be — GLT is well-founded theory)

Required data for calibration:
- 30+ days of R47 live/shadow fills with queue position at order placement
- Match rate by queue depth bucket
- Adverse selection rate by queue position

This is blocked on accumulating clean live fill data (orphan fill bug fixed 2026-04-15).

## Current Architecture

```
manifest.yaml: strategy_type = ?
  ├── "taker" → TakerEngine → HftNativeRunner → PowerProbQueueModel (hftbacktest)
  └── "maker" → MakerEngine → QueueDepletionFill(qf) → CK-direct
```

Both paths produce `BacktestResult` with full provenance → `ResultStore` → `backtest_report.json`.

## Honest Assessment

| Claim | Truth |
|-------|-------|
| "hftbacktest is broken for maker" | **Wrong.** We didn't calibrate it. |
| "hftbacktest lacks queue modeling" | **Wrong.** PowerProbQueueModel is GLT-based queue simulation. |
| "CK-direct is better" | **Unproven.** It's simpler and worked for our immediate need. |
| "14x bias is inherent" | **Likely calibration issue.** Lower exponent may fix it. |
| "Export bug is hftbacktest's fault" | **Wrong.** Our export script had the bug. |

## Lessons

1. **Calibrate before blaming the tool** — PowerProbQueueModel(3.0) might work fine with exponent=1.5 or 2.0 for TAIFEX
2. **Simpler isn't always better** — qf=0.5 is transparent but loses queue dynamics that GLT captures
3. **Both approaches should coexist** — cross-validate CK-direct vs calibrated hftbacktest once we have live fill data
4. **Document assumptions honestly** — this doc replaces an earlier version that unfairly blamed hftbacktest
