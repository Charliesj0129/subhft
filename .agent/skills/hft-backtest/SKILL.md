---
name: hft-backtest
description: Use when working directly with low-level hftbacktest V2 engine semantics, standalone backtest scripts, queue or exchange-model selection, or raw event-array requirements outside the platform adapter layer.
---

# hftbacktest V2

Use this skill when you are touching raw `hftbacktest` engine code rather than the platform wrapper.

## V2 Rules

Follow these rules every time:

- treat return values as status codes
- test success with `== 0`
- keep timestamps in nanoseconds
- use structured event arrays, not ad hoc column layouts

Correct control-flow pattern:

```python
while hbt.elapse(10_000_000) == 0:
    if hbt.submit_buy_order(...) == 0:
        pass
```

## Data Expectations

Expect V2-style event arrays with:

- `ev` flags rather than a standalone side column
- exchange and local timestamps encoded in the supported event layout
- realistic order-book reconstruction inputs

Use the project docs for details when you need to reason about ingestion or queue reconstruction:

- `docs/operations/hftbacktest_data_pipeline.md`
- `docs/architecture/hftbacktest_orderbook_reconstruction.md`
- `docs/architecture/hftbacktest_queue_models.md`

## Model Selection

Choose models deliberately:

- use simple constant latency only for early scaffolding
- use measured or interpolated latency for serious scoring
- use conservative queue models for stress testing
- treat partial-fill exchanges carefully; they can overstate realism if market impact is ignored

## Boundary

Do not duplicate platform wrapper guidance here. If the task is about `HftBacktestAdapter`, research gates, or governed parity with live runtime, use `hft-backtester` instead.

If you are asked to ingest or clean raw tick data, you **MUST** ensure it conforms to these guidelines to avoid corrupted order books (e.g. Crossed Books caused by bad Delta/Snapshot logic).
