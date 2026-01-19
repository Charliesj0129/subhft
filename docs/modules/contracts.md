# contracts

## Purpose
Shared data structures for strategy intents and execution events.

## Key Files
- `src/hft_platform/contracts/strategy.py`: Strategy intents and enums (e.g., `OrderIntent`, `IntentType`).
- `src/hft_platform/contracts/execution.py`: Execution events and enums (e.g., `OrderEvent`, `FillEvent`).

## Usage
- Strategy outputs `OrderIntent`.
- Risk/Order modules convert to `OrderCommand`.
- Execution normalizer emits `OrderEvent`/`FillEvent`.

## Notes
- These types are the contract boundary between modules.
- Keep changes backward compatible where possible.
