# hftbacktest v2.4.3 Reference

Local reference extracted from source code and official docs (2026-04-15).

## Source Files

- Source tarball: `/tmp/hftbacktest_src/hftbacktest-2.4.3.tar.gz`
- Mirrored docs: `/tmp/hftbacktest_docs/hftbacktest.readthedocs.io/`

## Key Documents

| File | Content |
|------|---------|
| `queue-models.md` | Queue position models — PowerProb, LogProb, RiskAdverse. Calibration guide. |
| `api-reference.md` | BacktestAsset, exchange models, latency models, fee models, order types |
| `taifex-calibration-plan.md` | How to calibrate PowerProbQueueModel for TAIFEX (our action items) |

## Architecture Overview

```
Data (.npz) → BacktestAsset → Local Processor (latency) → Exchange Processor (fill) → State
                                    ↓                           ↓
                              QueueModel                  DepthModel
                              LatencyModel                FeeModel
```

## Queue Model Selection Guide

| Model | Formula | Best For |
|-------|---------|----------|
| `risk_adverse_queue_model()` | Only trades advance queue | Ultra-conservative, overestimates queue wait |
| `power_prob_queue_model(n)` | prob = back^n / (back^n + front^n) | General purpose, n=2 for deep books, n=0.5-1.5 for shallow |
| `log_prob_queue_model()` | prob = log(1+back) / (log(1+back) + log(1+front)) | Balanced, less sensitive to extreme positions |
| `l3_fifo_queue_model()` | Exact FIFO from L3 data | When order-by-order data available |

## Critical Finding for TAIFEX

The "Queue-Based Market Making in Large Tick Size Assets" tutorial is directly applicable:
- Large tick = queue position matters more than price signal
- Monitor BBO quantity, back off when thin
- Dynamic qty_threshold adjustment based on inventory skew
- Use `wait_next_feed()` instead of fixed intervals for faster response
