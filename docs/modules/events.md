# events

## Purpose
Internal event definitions for market data and LOB stats.

## Key Files
- `src/hft_platform/events.py`: `TickEvent`, `BidAskEvent`, `LOBStatsEvent`, and metadata.

## Usage
- Produced by normalizers and LOB engine.
- Consumed by strategies and recorder.

## Notes
- These are separate from execution events in `src/hft_platform/contracts/execution.py`.
