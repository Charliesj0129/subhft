# Incident Diagnostics Runbook

## Decision Trace Sampling

Enable:
- `HFT_DIAG_TRACE_ENABLED=1`
- `HFT_DIAG_TRACE_SAMPLE_EVERY=100`

Output:
- `outputs/decision_traces/*.jsonl`

## Trace Stages Covered

Current runtime trace stream includes:
- market-data stages: `md_event`, `feature_update`, `md_normalize_error`, `feature_update_error`
- strategy/risk/order stages: dispatch/approve/reject/enqueue/dispatch_ok/dispatch_error
- gateway stages: reject / dispatch / dedup / policy paths

This allows one timeline to correlate market data, feature-plane, risk, and execution decisions.

## Inspect Traces (CLI)

- `hft diag --trace-file outputs/decision_traces/<day>.jsonl --limit 20`
- `hft diag --trace-file outputs/decision_traces/<day>.jsonl --trace-id <id>`
- `hft diag --trace-file outputs/decision_traces/<day>.jsonl --trace-id <id> --timeline --timeline-format md --out outputs/incidents/timeline.md`

## Incident Ticket Artifact Export

Use the helper target to export ticket-ready timeline artifact:

```bash
# Markdown timeline
make incident-timeline TRACE_FILE=outputs/decision_traces/<day>.jsonl TRACE_ID=<topic:seq> FORMAT=md OUT=outputs/incidents/<incident_id>/timeline.md

# JSON timeline
make incident-timeline TRACE_FILE=outputs/decision_traces/<day>.jsonl TRACE_ID=<topic:seq> FORMAT=json OUT=outputs/incidents/<incident_id>/timeline.json
```

Direct script usage:

```bash
PYTHONPATH=src python3 scripts/render_incident_timeline.py outputs/decision_traces/<day>.jsonl --trace-id <topic:seq> --format md --out outputs/incidents/<incident_id>/timeline.md
```
