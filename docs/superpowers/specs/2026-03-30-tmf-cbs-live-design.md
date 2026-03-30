# TMF CBS Live Design

**Date**: 2026-03-30
**Status**: Draft
**Scope**: Promote TMF CBS from session-specific research strategy to a full-day live-validatable strategy using one shared parameter set, one-lot max exposure, aggressive `IOC` entry-at-touch, and limit exit.

## 1. Problem Statement

The platform already has a production-facing `CascadeBounceStrategy` for contrarian entries after large moves, but the current implementation is aligned to an earlier research profile:

- fixed absolute move threshold in bps
- day-session-only window
- single fixed hold/stop profile
- optional execution optimizer for entry type selection
- no explicit live-oriented session flatten and no hard alignment to the user's full-session live constraints

For the target use case, the strategy must satisfy the following human constraints:

- instrument family: `TMF` micro TAIEX futures, implemented at runtime against one concrete contract symbol (for example `TMFD6`)
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
2. Volatility-normalized CBS with aggressive `IOC` entry-at-touch and limit exit
3. CBS plus additional gating from toxicity/regime features

We choose **Approach 2**.

### Why this approach

- It preserves the core CBS mean-reversion thesis already present in [`cascade_bounce.py`](/home/charlie/hft_platform/src/hft_platform/strategies/cascade_bounce.py).
- It allows one shared parameter set across day and night by normalizing move magnitude to local volatility instead of relying on one fixed absolute point threshold.
- It keeps entry simple and execution-realistic: aggressive `IOC` entry-at-touch avoids adding another research degree of freedom while staying representable in the current `OrderIntent` contract.
- It keeps the only execution sophistication on exit, where existing research showed the most plausible benefit.
- It avoids prematurely coupling CBS to newer `toxicity` / `regime` filters that are still tuning-heavy and would materially increase overfitting risk.

## 3. Strategy Design

### 3.1 Core Signal

The strategy remains contrarian:

- observe rolling price movement over `lookback_sec`
- compute local volatility in **points** over the same rolling context
- when the signed move exceeds `trigger_sigma * local_vol`, enter in the opposite direction

This replaces the current fixed `move_threshold_bps` trigger with a normalized trigger:

```text
z_move = signed_move_points / local_vol_points
enter contrarian when |z_move| >= trigger_sigma
```

For implementation clarity, `local_vol_points` in v1 is defined as:

- input stream: consecutive `LOBStatsEvent.mid_price_x2` observations
- transform: convert consecutive mid-price changes into absolute point changes
- estimator: rolling RMS of point changes over the active `lookback_sec` buffer
- warmup: no entry until at least `min_vol_samples` observations are present
- floor: clamp volatility to at least one tick/point-equivalent to avoid divide-by-zero and tiny-denominator explosions

This is intentionally narrower than "ATR-like". The backtest and live implementation must use the same estimator.

### 3.2 Entry Rules

- order entry is always **aggressive**, implemented on this platform as `IOC` crossing the current best bid/ask rather than as a broker-native market-without-price primitive
- only one open position per symbol at a time
- if a position already exists, all new entry signals are ignored
- no overlapping entries during the existing CBS cooldown / hold window
- entry is disabled outside `SessionPhase.OPEN`

This means the v1 design must bypass or remove the current limit-vs-market branching in [`ExecutionOptimizer`](/home/charlie/hft_platform/src/hft_platform/execution/execution_optimizer.py) for CBS entry decisions. `ExecutionOptimizer` is **out of scope** for v1 CBS entry.

The current `OrderIntent`/`BaseStrategy` contract cannot express broker-native `MKT`/`MKP` `price_type`, so the v1 design deliberately uses the already-supported pattern that current CBS uses today: aggressive `IOC` at the current touch price. No contract extension is required for entry if we keep that semantics.

### 3.3 Exit Rules

After the **entry fill is confirmed**:

- place a `LIMIT` take-profit order at `entry_fill_price +/- take_profit_pts`
- track a hard stop at `stop_loss_pts`
- force exit when `max_hold_sec` elapses
- force exit when platform session control enters `CLOSE_ONLY` / `FORCE_FLAT`
- force exit when global/session loss guard triggers

Limit exit is therefore the only place where passive execution is used in v1.

Because the current strategy skeleton does not yet maintain a dedicated exit-order state machine, the implementation must add explicit tracking for:

- active exit order intent / broker order id
- entry fill price basis (actual fill, not decision mid)
- remaining open quantity under partial fill
- cancel-before-replace behavior when switching from passive exit to forced aggressive flat
- terminal-state reset on `FillEvent` / `OrderEvent`

This is required to avoid duplicate close orders when stop-loss, timeout, and session-flatten paths race with an outstanding passive take-profit.

### 3.3a Exit State Machine Ownership

The passive-exit state machine is owned by the strategy plus normalized execution events:

- strategy emits the initial passive take-profit `NEW`
- `on_order` and `on_fill` maintain exit-order broker ids, remaining quantity, and terminal transitions
- stop-loss / timeout inside `SessionPhase.OPEN` follow `CANCEL -> aggressive close NEW`
- session-end and hard-halt flattening are **not** owned by the strategy; they are owned by platform-level flatteners and require a working `FORCE_FLAT` execution path

This split keeps strategy-owned logic limited to alpha exits and prevents duplicate session-control implementations.

### 3.4 Session Model

The strategy must run across the full Taiwan futures trading day, but may not carry inventory across session boundaries.

Session boundary ownership belongs to [`SessionGovernor` / `TrackGate`](/home/charlie/hft_platform/src/hft_platform/ops/session_governor.py), not to a duplicate wall-clock state machine inside CBS.

The required tracks are:

- `futures_day`
- `futures_night`

The same CBS parameter set applies to both tracks. Session behavior is:

- `OPEN`: strategy may open and close risk
- `CLOSE_ONLY`: strategy may only cancel / flatten existing exposure
- `FORCE_FLAT`: platform flattener force-closes any remaining exposure
- `CLOSED`: no intents

For v1, the implementation must provide or update the session-governor config so that TMF's concrete symbol is assigned to day and night tracks with explicit `close_only` and `force_flat` windows. Suggested defaults are:

- day session: `CLOSE_ONLY` at `13:40`, `FORCE_FLAT` at `13:44`
- night session: `CLOSE_ONLY` at `04:55`, `FORCE_FLAT` at `04:59`

This preserves the user's "same parameter set" constraint without duplicating session ownership in strategy code.

## 4. Parameter Surface

To reduce overfitting, the v1 strategy is limited to five tunable parameters:

- `lookback_sec`
- `trigger_sigma`
- `take_profit_pts`
- `stop_loss_pts`
- `max_hold_sec`

Everything else is fixed policy, not optimized:

- aggressive `IOC` entry-at-touch only
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

This must be enforced at two layers:

1. **Strategy layer**
   - CBS ignores entry signals whenever current net position is non-zero or an exit order is active
2. **Risk/config layer**
   - fix the current config/validator mismatch so the runtime-consumed key is actually `max_position_lots: 1` for the CBS strategy
   - add a resulting-position check, because the current `PositionLimitValidator` is stateless and only checks `abs(intent.qty)`

This is a rollout blocker, not an optional hardening item. Until resulting net exposure is enforced below the strategy layer, `1` lot is only a strategy-level convention, not a platform-level guarantee.

### 6.2 Loss Controls

Two layers are required:

1. **Per-trade stop**
   - enforced by `stop_loss_pts`
2. **Session/global hard guard**
   - if realized + unrealized loss approaches `8000 TWD` / `800 pts`, the platform must stop new risk and transition toward flattening

The hard guard overrides strategy logic.

This should be implemented by extending the existing [`DailyLossLimitValidator`](/home/charlie/hft_platform/src/hft_platform/risk/validators.py) and existing halt path, not by introducing a second independent strategy-local loss authority. The actual daily hard-stop source of truth remains in risk.

Important current limitation:

- today's daily-loss path escalates to `HALT`, but `HALT` by itself does **not** guarantee immediate flattening
- the current halt flattener emits `IntentType.NEW`, while `StormGuardState.HALT` only permits `CANCEL` and `FORCE_FLAT`

Therefore, "hard-loss halt implies flatten" is currently a **design requirement**, not an already-working reuse path. Rollout requires fixing that flatten path first.

The exact config source of truth is [`config/base/strategy_limits.yaml`](/home/charlie/hft_platform/config/base/strategy_limits.yaml), specifically the `intraday_pnl` block. For this rollout, the implementation must explicitly set:

- `intraday_pnl.hard_limit_ntd = 8000`

and treat it as platform-wide for the live TMF rollout window. If other live strategies remain enabled, this becomes a deployment conflict and must be resolved before rollout.

### 6.3 Flatten / No-New-Risk Ownership

Flatten and no-new-risk have different owners:

- **Session transitions**: owned by `SessionGovernor` + `TrackGate` + platform flattener
- **Broker/runtime degradation**: owned by platform degrade / watchdog / reconnect logic, not by strategy
- **Daily hard-loss halt**: owned by `DailyLossLimitValidator` + halt path
- **Strategy stop-loss / max-hold exits during OPEN**: owned by CBS itself

The strategy does not own broker-health detection. It only consumes the consequences of platform state by being filtered to `CLOSE_ONLY` or by having platform flatteners close positions.

Important interaction with current `TrackGate`:

- during `CLOSE_ONLY`, `StrategyRunner` only allows `CANCEL` and `FORCE_FLAT`
- ordinary opposite-side `NEW` exits emitted by CBS will be dropped in that phase

Therefore session-boundary liquidation cannot rely on the current CBS stop/timeout exit path. It requires a real `FORCE_FLAT` execution path or a deliberate TrackGate/session-control redesign.

### 6.4 Failure Handling

The strategy must degrade safely when the broker/runtime is unhealthy.

If quote/runtime degradation occurs, the design does **not** create a new strategy-side controller. Instead, rollout depends on the existing Shioaji runtime hardening path and platform degrade gates to block new risk. CBS must remain compatible with that platform-level behavior.

## 7. Integration Plan

### 7.1 Strategy Layer

Primary strategy modifications are expected in:

- [`cascade_bounce.py`](/home/charlie/hft_platform/src/hft_platform/strategies/cascade_bounce.py)

Conceptual changes:

- replace fixed absolute move trigger with volatility-normalized trigger
- enforce aggressive `IOC` entry only
- add explicit limit-exit management as first-class state
- remove entry-time dependency on `ExecutionOptimizer`
- maintain one shared parameter set for both sessions
- consume real `FillEvent` / `OrderEvent` updates for exit state

### 7.2 Execution / Order Flow

Existing components remain the main path:

- strategy emits `OrderIntent`
- risk validates
- order adapter places/cancels orders
- execution callbacks normalize fills

No new execution subsystem is introduced. The design relies on the current stack, with one explicit gap to close:

- [`order/adapter.py`](/home/charlie/hft_platform/src/hft_platform/order/adapter.py)
- [`feed_adapter/shioaji/order_gateway.py`](/home/charlie/hft_platform/src/hft_platform/feed_adapter/shioaji/order_gateway.py)
- [`ops/position_flattener.py`](/home/charlie/hft_platform/src/hft_platform/ops/position_flattener.py)

Required execution-gap fix before rollout:

- `FORCE_FLAT` is currently allowed by guards but does not have a complete dispatch path in `OrderAdapter`
- session-end / hard-halt flattening therefore requires an explicit adapter implementation for `FORCE_FLAT` or an equivalent guaranteed aggressive-close execution branch
- the current halt flattener also needs alignment, because it presently emits `IntentType.NEW` and is not compatible with HALT semantics

### 7.3 Risk / Halt Wiring

The strategy-level hard-stop and flatten-only behavior must integrate with existing risk/halt controls, not compete with them.

Expected reuse:

- existing position tracking and order terminal-state handling
- existing broker watchdog / reconnect protections
- existing autonomy / halt mechanisms where available
- existing `SessionGovernor` / `TrackGate` session ownership
- existing `PositionFlattener` / halt flattener patterns

## 8. Testing Design

### 8.1 Strategy Tests

Required unit coverage:

- normalized trigger logic
- one-lot cap behavior
- no re-entry while positioned
- limit-exit placement after fill
- partial-fill exit bookkeeping
- cancel-before-replace on timeout/stop during OPEN
- stop-loss exit
- max-hold exit
- compatibility with `CLOSE_ONLY` / `FORCE_FLAT`
- FORCE_FLAT dispatch path

### 8.2 Validation Artifacts

Required research/backtest artifacts:

- anchored walk-forward fold report
- selected parameter plateau summary
- session-split stability table
- drawdown distribution
- trade concentration analysis

### 8.3 Live Readiness Checks

Before live activation:

- confirm session-governor ownership of session boundary flatten behavior
- confirm strategy ignores new signals while one-lot exposure exists
- confirm hard-loss guard blocks new orders
- confirm broker degradation path stops new risk
- confirm TMF runtime symbol is a concrete configured contract (for example `TMFD6`), not a symbolic alias with hidden auto-roll behavior
- confirm TMF symbol metadata (`tick_size`, `point_value`, `price_scale`) matches live contract configuration

## 8.4 Symbol / Contract Model

`TMF` in this design refers to the **strategy family and target instrument class**, not a magical alias resolved inside the strategy. Runtime trading must use one concrete configured symbol from the strategy registry, such as `TMFD6`.

Contract roll is out of scope for this strategy design. The rollout process must update the configured concrete symbol at expiry/roll time rather than relying on hidden strategy-side front-month discovery.

Immediate live rollout therefore requires an explicit pre-launch decision:

- choose the exact active contract symbol (for example `TMFD6`)
- confirm that the same symbol is wired consistently in `symbols.yaml`, strategy registry, session-governor track assignment, and broker contract resolution

## 9. Rollout Plan

### Phase 1

- implement normalized CBS logic
- keep execution simple: aggressive `IOC` entry-at-touch + limit exit
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
