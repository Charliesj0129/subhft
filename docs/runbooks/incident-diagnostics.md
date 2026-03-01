# Incident Diagnostics Runbook (Prototype)

## Decision Trace Sampling
Enable:
- `HFT_DIAG_TRACE_ENABLED=1`
- `HFT_DIAG_TRACE_SAMPLE_EVERY=100`

Output:
- `outputs/decision_traces/*.jsonl`

## Inspect traces (CLI)
- `hft diag --trace-file outputs/decision_traces/<day>.jsonl --limit 20`
- `hft diag --trace-file ... --trace-id <id>`

## Stages currently sampled (prototype)
- gateway reject / dispatch
- risk reject / approve
- order adapter enqueue / dispatch

## TODO
- Correlate market-data and feature-plane traces into one timeline
- Export timeline artifact for incident ticket
