# Rejection Queue Consumer Design

**Date**: 2026-04-06
**Status**: Approved
**Scope**: Wire the orphaned `_rejection_queue` to deliver `RiskFeedback` to strategies

## Problem

`_rejection_queue` (maxsize=256) is written to by RiskEngine (4 paths) and StrategyRunner (1 path), but no consumer reads from it. `BaseStrategy.on_risk_feedback()` callback exists but is never invoked. Strategies cannot learn when their intents are rejected.

## Design: StrategyRunner Rejection Consumer

### Architecture

```
RiskEngine ──put_nowait──→ _rejection_queue ←──get──── StrategyRunner._run_rejection_consumer()
StrategyRunner ──put_nowait──↗                              ↓
                                                   strategy.on_risk_feedback(feedback)
```

### Changes

**1. StrategyRunner** (`src/hft_platform/strategy/runner.py`)
- Add `_rejection_queue: asyncio.Queue | None` to `__slots__`, init as `None`
- Add `async def _run_rejection_consumer(self)`:
  - Loop: `feedback = await self._rejection_queue.get()`
  - Lookup strategy by `feedback.strategy_id` via `self._strat_index`
  - Call `strategy.on_risk_feedback(feedback)` (synchronous, strategy decides scope)
  - Guard with try/except: strategy exception must not kill consumer
  - Unknown strategy_id: log warning, drop
  - CancelledError: break cleanly

**2. Bootstrap** (`src/hft_platform/services/bootstrap.py`)
- Wire `strategy_runner._rejection_queue = _rejection_queue`
- Start `strategy_runner._run_rejection_consumer()` as background task in service group

**3. Unchanged**
- `_rejection_sink` write paths (RiskEngine + StrategyRunner put_nowait)
- `RiskFeedback` dataclass
- `BaseStrategy.on_risk_feedback()` signature

### Edge Cases

| Situation | Behavior |
|-----------|----------|
| strategy_id not found | log warning, drop, continue |
| on_risk_feedback raises | log exception, continue |
| queue empty | await blocks (normal) |
| shutdown | CancelledError → break |
| _rejection_queue is None | consumer does not start |

### Test Plan

1. Feedback routed to correct strategy (assert on_risk_feedback called with correct RiskFeedback)
2. Unknown strategy_id → log + drop, consumer continues
3. on_risk_feedback raises → consumer continues processing next feedback
4. _rejection_queue=None → consumer exits immediately

### Estimated Size

~30 lines core logic + ~10 lines bootstrap wiring + ~60 lines tests
