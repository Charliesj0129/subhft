# Smart Symbols Refresh Design — HFT Platform

> **Status**: Final (user-approved design)
> **Date**: 2026-03-23
> **Scope**: `config/symbols.yaml` pre-open refresh policy and generation flow

## Goals

Design a smarter `symbols.yaml` refresh flow that:

- keeps the stock universe as the primary subscription set
- limits derivatives to only the user-required core products
- rebuilds before trading sessions, not during runtime
- preserves the existing generated-config pattern instead of encouraging manual YAML edits

## Confirmed Requirements

The approved requirements for this design are:

- mode: mixed universe, but biased toward stocks
- stock subscriptions should be expanded as much as practical
- derivatives should include only:
  - Taiwan Index Futures (`TXF`)
  - TSMC single-stock futures
- do not include `TXO` or any other options
- rebuild `config/symbols.yaml` before the day session and before the night session
- do not hot-refresh symbol selection continuously during trading
- if derivatives use fewer reserved slots than expected, the remainder should flow back to stocks

## Recommended Approach

Three approaches were considered:

1. Rule-only deterministic selection
2. Hybrid selection with static stocks and targeted derivative policy
3. Fully dynamic ranked universe

This design adopts approach `2`.

Rationale:

- it matches the user's need for a stable stock-heavy universe
- it keeps derivative logic narrow and explainable
- it avoids unnecessary daily churn
- it fits Shioaji subscription and query constraints better than a highly dynamic selector

## Design Summary

The system should treat `config/symbols.yaml` as a generated artifact built from two higher-level inputs:

1. a static stock pool
2. a small derivative policy

The builder runs only at pre-session refresh times, resolves the active derivative contracts from broker contracts, then fills the remaining capacity with stocks.

## Components

### 1. Static Stock Pool

Purpose:
Define the long-lived stock universe that should dominate subscriptions.

Design:

- move the stock universe definition into a dedicated source file such as `config/symbols.stocks.list`
- keep ordering stable so truncation is deterministic
- treat this file as the source of truth for stock membership

Non-goal:

- no daily stock ranking or scanner-based stock rotation in this design

### 2. Derivative Policy

Purpose:
Describe which derivative families to track without hardcoding transient contract codes.

Design:

- represent derivative intent as policy, not final symbols
- initial supported policies:
  - `TXF`: `front`, `next`
  - `TSMC single-stock futures`: `front`, `next`
- exclude all options logic
- allow the actual emitted derivative count to be smaller than the nominal reservation if valid contracts are missing

### 3. Pre-Open Builder

Purpose:
Generate the final `config/symbols.yaml` before each session.

Responsibilities:

- fetch or load fresh broker contracts
- resolve active derivative contracts from the derivative policy
- compute the remaining subscription budget
- fill the rest of the universe with stocks from the static stock pool
- validate the final symbol set
- atomically overwrite `config/symbols.yaml` only on successful build

### 4. Validation Gate

Purpose:
Prevent partial or invalid symbol output from reaching runtime consumers.

Required checks:

- total symbols must be `<= 200`
- emitted derivatives must only belong to allowed families
- contracts must exist in the current contract cache
- symbol metadata must preserve required fields such as `exchange`, `product_type`, `tick_size`, and `price_scale`
- `price_scale` must remain `10000` where the platform expects scaled pricing

## Allocation Logic

The current design should not hardcode a permanent `120/80` split.

Instead, use this policy:

- reserve a small derivative budget for `TXF` and TSMC single-stock futures
- treat stocks as the primary consumer of remaining capacity
- if the derivative build emits fewer contracts than the reserved amount, immediately return the unused budget to stocks

Conceptually:

```text
final_capacity = 200 - safety_margin
derivative_target = reserved_derivatives
derivative_actual = resolved_valid_derivatives
stock_target = final_capacity - derivative_actual
```

This ensures the universe stays stock-heavy while still tracking the required futures contracts.

## Contract Selection Rules

### TXF

- select `front` and `next` month by default
- when the front contract is close to expiry, the builder should be able to promote `next` as the effective primary contract
- the design target is predictable rollover behavior, not aggressive day-by-day optimization

### TSMC Single-Stock Futures

- select `front` and `next` month by default
- if only one valid liquid month is available, emitting a single valid contract is acceptable
- missing derivative contracts should degrade the derivative portion only, not fail the entire stock universe build

### Explicit Exclusions

- no `TXO`
- no equity options
- no ATM/OTM band logic
- no scanner-driven option expansion

## Refresh Schedule

The builder should run at two fixed times only:

- after Shioaji day-session contract updates
- after Shioaji night-session contract updates

Operationally, the intended schedule is:

- day session rebuild: after `08:00`
- night session rebuild: after `17:15`

This aligns the refresh with broker contract update windows and avoids runtime universe churn.

## Failure and Fallback Policy

The build must fail safely.

### Broker Contract Fetch Fails

- keep the previous `config/symbols.yaml`
- do not overwrite with a partial or empty output
- emit a clear warning and status signal

### Derivative Resolution Fails

- still allow stock output to succeed
- either emit no derivatives or reuse the previously known-good derivative subset
- do not block the stock universe because a derivative family failed to resolve

### Stock Count Exceeds Capacity

- truncate stocks deterministically using source-file order

### Final Universe Smaller Than 200

- allow it
- do not invent fallback symbols purely to fill capacity

## Data Flow

The intended cold-path flow is:

```text
static stock source
  + derivative policy
  + broker contracts
  -> pre-open builder
  -> validation
  -> atomic write to config/symbols.yaml
  -> runtime consumers load generated config
```

This preserves the existing architectural pattern where `symbols.yaml` is an output artifact consumed by runtime modules such as market data, monitor, and normalizer components.

## Why This Design Fits the Existing Repo

This repository already treats `config/symbols.yaml` as generated configuration derived from higher-level symbol inputs and broker contracts.

This design intentionally preserves that model:

- it does not reintroduce manual editing as the main workflow
- it keeps refresh logic on the cold path
- it minimizes churn in runtime-loaded symbol metadata
- it narrows derivative logic to the products the user actually wants

## Testing Strategy

Implementation should verify:

- successful generation with valid contract data
- deterministic fallback when contract fetch fails
- correct rollover behavior near expiry boundaries
- derivative family allowlist enforcement
- capacity accounting when derivatives emit fewer symbols than reserved
- atomic write semantics that preserve the previous file on failure

## Observability Expectations

The builder should expose enough signals to answer:

- when was the last successful refresh
- how many symbols were emitted
- how many were stocks vs derivatives
- which derivative contracts were selected
- whether fallback behavior was used

## Open Decisions Deferred to Planning

These items are intentionally deferred to implementation planning:

- exact config file names and schema for the stock list and derivative policy
- exact expiry threshold for rollover
- whether derivative fallback should emit zero contracts or reuse the last known-good subset
- whether refresh runs via CLI, cron, service bootstrap hook, or a dedicated scheduler entrypoint

## References

- Shioaji contract guide: https://sinotrade.github.io/tutor/contract/
- Shioaji limits guide: https://sinotrade.github.io/tutor/limit/
- Shioaji snapshots guide: https://sinotrade.github.io/tutor/market_data/snapshot/
- Shioaji scanners guide: https://sinotrade.github.io/tutor/market_data/scanners/
- LLM export reference: https://sinotrade.github.io/llms-full.txt
