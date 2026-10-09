---
name: hft-backtest
description: "Configure the research backtest engines (MakerEngine, HftNativeRunner, HftBacktestAdapter), BacktestContractSpec, latency profiles; judge results with the method bias matrix. Use when setting up a backtest or interpreting a PnL claim. Not for promotion gate thresholds (validation-gate)."
---

# Backtest engines and honesty

Canonical sources: `docs/runbooks/backtest-engine-selection.md` (engine
catalogue and bias matrix) and `research/backtest/contract.py`
(`BacktestContractSpec`). Memory has the evidence (`backtest_method_reliability`,
`unified_backtest_framework`, `calibration_queue_model_fix`).

## Pick the engine

| Question | Engine | Source |
|---|---|---|
| Does a maker survive TAIFEX costs on ClickHouse ticks? | `MakerEngine` (CK-direct, `QueueDepletionFill`) | `research/backtest/maker_engine.py` |
| Does a taker signal clear IC and PnL thresholds? | `HftNativeRunner` | `research/backtest/hft_native_runner.py` |
| Does a live-grade `BaseStrategy` survive queue + latency + risk gating? | `HftBacktestAdapter` | `src/hft_platform/backtest/adapter.py` |

Build all three from one object: `BacktestContractSpec.from_manifest(manifest)`
(cost profile refs come from the alpha manifest), then `spec.maker_engine_kwargs()`,
`spec.hft_native_runner_kwargs()`, `spec.hft_backtest_adapter_kwargs()`.

## Rules that decide whether a number means anything

1. **Name the method.** Any PnL claim states engine + queue model + latency
   profile. The same maker has read from ~14x pessimistic
   (`PowerProbQueueModel(3.0)` uncalibrated) to ~577x optimistic (native runner,
   zero latency). Calibrated, latency-enabled runs are the ones to quote.
2. **Declare a latency profile** from `config/research/latency_profiles.yaml`.
   Canonical measured Shioaji profile: `r47_maker_shioaji_p95_v2026-04-24_measured`
   (place and cancel latencies are asymmetric: model place, modify, cancel
   separately). P95 for scoring, P99 for stress. A backtest with no profile is a
   Gate D blocker. New broker profile: `uv run hft run sim` shadow, >= 1000 RTT
   samples per side, add a dated entry.
3. **Edge below 2x spread must use bid/ask fills**, never mid. A +3 bps mid-price
   edge turned into -48 bps on bid/ask.
4. **Recent data first.** Check the latest month before claiming an edge holds;
   regimes shift (spreads moved from tens of points to a few within months).
5. **Signal IC is screening only**, not PnL. MFE is not PnL.
6. **Re-export after the depth fix.** Hftbacktest outputs from before 2026-04-10
   are invalid (depth-export bug).
7. Golden parquet prices are x1,000,000 (platform x10,000): divide by 100.

## Raw hftbacktest V2 semantics

Return values are status codes (`== 0` is success); timestamps are ns;
structured event arrays only; keep `ev`, `exch_ts`, `local_ts` through every
transform (a missing `local_ts` distorts latency estimation). Generate an
end-of-day snapshot before any book-initialization-dependent backtest.

## Parity with live

Same `feature_set_id` (`lob_shared_v3`) and feature indices in backtest and live;
quality flags handled identically; `BacktestRiskConfig` for risk-gated runs.

## Verify

```bash
make research-stamp-data-meta DATA_PATH=path/to/data.npy
make research-validate-data-meta DATA_PATH=path/to/data.npy
make research ALPHA=<id> OWNER=<you> DATA='path/to/data.npy'
make test-file FILE=tests/unit/research/<relevant test>
```

## Done when

The report states engine, queue model, latency profile, cost profile, data
window, and which of the bias-matrix rows applies; fills are bid/ask where
required; and recent-window results agree with the full-window claim.

## References

- `references/validation.md` — read when diagnosing calibration, running walk-forward or CPCV, or reviewing a scorecard for overfitting.
