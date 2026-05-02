# diagnostics — Replay & Trace

> **Package**: `src/hft_platform/diagnostics/`
> **Files**: 2

## Overview

Event replay for post-mortem analysis and decision trace sampling for debugging.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `replay.py` | `EventReplay` | Event replay from ClickHouse/WAL |
| `trace.py` | `DecisionTraceSampler` | Decision trace sampling |

## Event Replay

Replays recorded events for post-mortem debugging:

```python
replay = EventReplay(ch_client)
async for event in replay.stream(symbol="TXFD6", start_ts=..., end_ts=...):
    analyze(event)
```

## Decision Trace

Samples and records strategy decision traces:
- Input features at decision time
- Generated intents
- Risk decisions
- Used for debugging and alpha research
