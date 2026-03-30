# TMF CBS Live Design

**Date**: 2026-03-30
**Status**: Draft
**Scope**: Promote TMF CBS from session-specific research strategy to a full-day live-validatable strategy using one shared parameter set, one-lot max exposure, market entry, and limit exit.

## 1. Problem Statement

The platform already has a production-facing `CascadeBounceStrategy` for contrarian entries after large moves, but the current implementation is aligned to an earlier research profile:

- fixed absolute move threshold in bps
- day-session-only window
- single fixed hold/stop profile
- optional execution optimizer for entry type selection
- no explicit live-oriented session flatten and no hard alignment to the user's full-session live constraints

For the target use case, the strategy must satisfy the following human constraints:

- instrument: `TMF` micro TAIEX futures
- live validation immediately, not sim/shadow first
- max position: `1` lot, never pyramiding
- no cross-session inventory
- full-day coverage, using one shared parameter set across day and night sessions
- unlimited frequency subject to one-lot cap
- hard loss boundary: must never be allowed to drift to `8000 TWD` / `800` points
- limit exit is allowed; limit-entry complexity is explicitly deferred

The design goal is therefore not to invent a new alpha family. It is to convert CBS into a lower-degrees-of-freedom, live-usable `TMF` strategy that can be validated without opening a large overfitting surface.

## 2. Chosen Approach

Three approaches were considered:

1. Absolute-threshold CBS with one shared fixed point threshold across all sessions
2. Volatility-normalized CBS with market entry and limit exit
3. CBS plus additional gating from toxicity/regime features

We choose **Approach 2**.

### Why this approach

- It preserves the core CBS mean-reversion thesis already present in [`cascade_bounce.py`](/home/charlie/hft_platform/src/hft_platform/strategies/cascade_bounce.py).
- It allows one shared parameter set across day and night by normalizing move magnitude to local volatility instead of relying on one fixed absolute point threshold.
- It keeps entry simple and execution-realistic: market entry avoids adding another research degree of freedom.
- It keeps the only execution sophistication on exit, where existing research showed the most plausible benefit.
- It avoids prematurely coupling CBS to newer `toxicity` / `regime` filters that are still tuning-heavy and would materially increase overfitting risk.

## 3. Strategy Design

### 3.1 Core Signal

The strategy remains contrarian:

- observe rolling price movement over `lookback_sec`
- compute local realized volatility / ATR-like scale over the same rolling context
- when the signed move exceeds `trigger_sigma * local_vol`, enter in the opposite direction

This replaces the current fixed `move_threshold_bps` trigger with a normalized trigger:

```text
z_move = signed_move_points / local_vol_points
enter contrarian when |z_move| >= trigger_sigma
```

### 3.2 Entry Rules

- order entry is always **aggressive**, implemented on this platform as `IOC` crossing the current best bid/ask rather than as a broker-native market-without-price primitive
- only one open position per symbol at a time
- if a position already exists, all new entry signals are ignored
- no overlapping entries during the existing CBS cooldown / hold window
- entry is disabled during flatten-only windows before each session boundary

This means the v1 design must bypass or disable the current limit-vs-market branching in [`ExecutionOptimizer`](/home/charlie/hft_platform/src/hft_platform/execution/execution_optimizer.py) for CBS entry decisions. The optimizer may still be reused later for exit handling, but it is not part of the entry decision surface in this design.

### 3.3 Exit Rules

After a fill:

- immediately place a `LIMIT` take-profit order at `take_profit_pts`
- track a hard stop at `stop_loss_pts`
- force flat when `max_hold_sec` elapses
- force flat when session-end flatten buffer is reached
- force flat when global/session loss guard triggers

Limit exit is therefore the only place where passive execution is used in v1.

Because the current strategy skeleton does not yet maintain a dedicated exit-order state machine, the implementation must add explicit tracking for:

- active exit order intent / broker order id
- cancel-before-replace behavior when switching from passive exit to forced aggressive flat
- terminal-state reset on `FillEvent` / `OrderEvent`

This is required to avoid duplicate close orders when stop-loss, timeout, and session-flatten paths race with an outstanding passive take-profit.

### 3.4 Session Model

The strategy must run across the full Taiwan futures trading day, but may not carry inventory across session boundaries.

The design uses two session windows:

- day session
- night session

Both sessions share the same CBS parameter set. However, session boundaries remain execution boundaries:

- new entry disabled during `flatten_buffer_sec` before a session end
- if still long/short inside the buffer, cancel passive exits and flatten at market

This preserves the user's "same parameter set" constraint without allowing cross-session inventory risk.

## 4. Parameter Surface

To reduce overfitting, the v1 strategy is limited to five tunable parameters:

- `lookback_sec`
- `trigger_sigma`
- `take_profit_pts`
- `stop_loss_pts`
- `max_hold_sec`

Everything else is fixed policy, not optimized:

- market entry only
- limit exit allowed
- one lot only
- no pyramiding
- session flatten required
- one parameter set for full day

### 4.1 Explicitly Frozen Out of Scope

The following are intentionally not optimized in v1:

- separate day/night thresholds
- dynamic position sizing
- multi-signal alpha stacking
- VPIN conditioning
- toxicity-based gating
- regime classifier gating
- limit-entry patience tuning
- separate parameters by weekday / contract month / spread regime

## 5. Anti-Overfitting Validation Design

### 5.1 Validation Method

Parameter selection must use **anchored walk-forward**, not one-shot full-sample optimization.

At each validation fold:

- training uses only data available before the fold
- validation uses the immediately following unseen segment
- day and night sessions are both included in every fold
- the same parameter set is scored on the full-day combined population

### 5.2 Search Discipline

Use a deliberately small discrete grid:

- no dense grid
- no random search
- no Bayesian optimization
- no continuous fitting

The output is not "best backtest PnL". The output is the **most stable surviving plateau**.

### 5.3 Selection Metrics

A candidate parameter set must be evaluated on:

- net PnL
- max drawdown
- trade count
- win/loss asymmetry
- per-session stability
- fold-to-fold sign consistency

### 5.4 Kill Rules

Discard a parameter set if any of the following holds:

- performance sign flips repeatedly across walk-forward folds
- profitability is driven by a tiny number of outlier trades
- drawdown approaches the live hard-loss boundary too closely
- trade count is too sparse to trust the result
- one session dominates while the other session degrades badly

## 6. Live Risk Design

### 6.1 Position Limits

Hard live constraints:

- max open position: `1` lot
- no add-on entries
- no simultaneous opposing orders
- no re-entry until the prior trade is terminal

### 6.2 Loss Controls

Two layers are required:

1. **Per-trade stop**
   - enforced by `stop_loss_pts`
2. **Session/global hard guard**
   - if realized + unrealized loss approaches `8000 TWD` / `800 pts`, the strategy enters no-new-risk mode and is flattened immediately

The hard guard overrides strategy logic.

This should be implemented by extending the existing [`DailyLossLimitValidator`](/home/charlie/hft_platform/src/hft_platform/risk/validators.py) and existing halt path, not by introducing a second independent strategy-local loss authority. Strategy-local behavior may still react by entering flatten-only mode, but the actual daily hard-stop source of truth should remain in risk.

### 6.3 Flatten-Only Mode

Flatten-only mode is entered when:

- session end buffer begins
- broker state is degraded
- quote feed is stale
- reconnect is in progress
- hard loss guard is triggered

In flatten-only mode:

- no new entries
- existing limit exits may remain only if they do not violate session boundary
- stop / timeout / force-flat logic stays active

### 6.4 Failure Handling

The strategy must degrade safely when the broker/runtime is unhealthy.

If any of the following occurs:

- quote callback stall
- reconnect loop
- order status uncertainty
- callback mismatch / missing fill updates

then the strategy must switch to `no-new-risk` and only reduce inventory.

This is intentionally aligned with the current Shioaji runtime hardening path in the feed/order stack rather than introducing a parallel failure controller.

## 7. Integration Plan

### 7.1 Strategy Layer

Primary strategy modifications are expected in:

- [`cascade_bounce.py`](/home/charlie/hft_platform/src/hft_platform/strategies/cascade_bounce.py)

Conceptual changes:

- replace fixed absolute move trigger with volatility-normalized trigger
- enforce aggressive `IOC` entry only
- add explicit limit-exit management as first-class state
- add dual-session flatten buffer logic
- maintain one shared parameter set for both sessions

### 7.2 Execution / Order Flow

Existing components remain the main path:

- strategy emits `OrderIntent`
- risk validates
- order adapter places/cancels orders
- execution callbacks normalize fills

No new execution subsystem is introduced. The design relies on the current stack:

- [`order/adapter.py`](/home/charlie/hft_platform/src/hft_platform/order/adapter.py)
- [`execution/execution_optimizer.py`](/home/charlie/hft_platform/src/hft_platform/execution/execution_optimizer.py)
- [`feed_adapter/shioaji/order_gateway.py`](/home/charlie/hft_platform/src/hft_platform/feed_adapter/shioaji/order_gateway.py)

### 7.3 Risk / Halt Wiring

The strategy-level hard-stop and flatten-only behavior must integrate with existing risk/halt controls, not compete with them.

Expected reuse:

- existing position tracking and order terminal-state handling
- existing broker watchdog / reconnect protections
- existing autonomy / halt mechanisms where available

## 8. Testing Design

### 8.1 Strategy Tests

Required unit coverage:

- normalized trigger logic
- one-lot cap behavior
- no re-entry while positioned
- limit-exit placement after fill
- stop-loss exit
- max-hold exit
- session flatten buffer
- no-new-risk behavior under degraded runtime

### 8.2 Validation Artifacts

Required research/backtest artifacts:

- anchored walk-forward fold report
- selected parameter plateau summary
- session-split stability table
- drawdown distribution
- trade concentration analysis

### 8.3 Live Readiness Checks

Before live activation:

- confirm session boundary flatten behavior
- confirm strategy ignores new signals while one-lot exposure exists
- confirm hard-loss guard blocks new orders
- confirm broker degradation path stops new risk
- confirm TMF symbol metadata (`tick_size`, `point_value`, `price_scale`) matches live contract configuration

## 9. Rollout Plan

### Phase 1

- implement normalized CBS logic
- keep execution simple: market entry + limit exit
- freeze a single validated parameter set

### Phase 2

- run one-lot live validation under the hard-loss boundary
- no parameter retuning during validation window
- perform daily reconciliation and execution-quality review

### Phase 3

Only after stable live evidence exists:

- consider toxicity or regime gating as a separate iteration
- treat any such addition as a new strategy version, not an in-place tweak

## 10. Decisions

Confirmed decisions from the design review:

- use CBS, not a new strategy family
- full-day coverage required
- one shared parameter set across day and night
- one lot max
- unlimited frequency, constrained only by one-lot cap and risk rules
- no cross-session inventory
- limit exit allowed
- direct live validation is the target operating mode

## 11. Non-Goals

- maximizing backtest Sharpe through broad parameter sweeps
- session-specific parameter tuning
- adding multiple alpha sources before the first live validation
- building a new execution engine
- proving long-term profitability in this design phase

This design is specifically for creating a defensible, low-overfit path from the current CBS implementation to a constrained TMF live validation strategy.
