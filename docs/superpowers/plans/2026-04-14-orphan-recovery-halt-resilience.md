# Orphan Recovery Halt Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the current restart-loss, reconciliation HALT-loop, unknown cost basis, missing strategy attribution, fill-gap recovery, and manual reset failure modes in the execution recovery plane.

**Architecture:** Harden the recovery path in dependency order. First move checkpoint state onto durable storage and make startup/runtime read the same location. Then add reconciliation zero-snapshot debounce so transient broker empties do not trigger HALT. After that, improve broker-only recovery metadata (cost basis and strategy ownership), add graceful reset tooling, and finally backfill missing fills from recorded broker/position evidence.

**Tech Stack:** Python 3.12, asyncio, structlog, JSON/orjson persistence, existing PositionStore/Reconciliation/Startup recovery services.

---

## File Map

- Modify: `src/hft_platform/execution/checkpoint.py`
- Modify: `src/hft_platform/execution/reconciliation.py`
- Modify: `src/hft_platform/execution/startup_recon.py`
- Modify: `src/hft_platform/services/bootstrap.py`
- Modify: `src/hft_platform/services/system.py`
- Modify: `src/hft_platform/execution/positions.py`
- Modify: `src/hft_platform/execution/router.py`
- Modify: `src/hft_platform/recorder/*` or recovery utilities only if fill-gap replay needs it
- Test: `tests/unit/test_position_checkpoint.py`
- Test: `tests/unit/test_reconciliation_service.py`
- Test: `tests/unit/test_startup_recon.py`
- Test: `tests/unit/test_position_store_*`
- Test: `tests/unit/test_execution_router_*`

## Task 1: G6 Checkpoint Persistence

**Files:**
- Modify: `src/hft_platform/execution/checkpoint.py`
- Modify: `src/hft_platform/services/bootstrap.py`
- Test: `tests/unit/test_position_checkpoint.py`

- [ ] Write failing tests for durable default checkpoint path in writer and bootstrap verifier wiring.
- [ ] Run focused tests and verify failure.
- [ ] Change the default checkpoint path from `.runtime/position_checkpoint.json` to `.state/position_checkpoint.json` in both writer and bootstrap startup verifier creation.
- [ ] Re-run focused tests and verify pass.
- [ ] Commit.

## Task 2: G3 Empty Broker Snapshot Debounce

**Files:**
- Modify: `src/hft_platform/execution/reconciliation.py`
- Test: `tests/unit/test_reconciliation_service.py`

- [ ] Write failing tests for “local non-zero + broker all-zero” snapshots requiring multiple consecutive observations before HALT.
- [ ] Run focused tests and verify failure.
- [ ] Add zero-snapshot detection/debounce state to reconciliation so a sudden broker-empty snapshot enters a guarded countdown instead of immediate critical mismatch HALT.
- [ ] Keep normal critical sign-mismatch behavior unchanged for non-empty broker snapshots.
- [ ] Re-run focused tests and verify pass.
- [ ] Commit.

## Task 3: G1 Cost Basis Backfill

**Files:**
- Modify: `src/hft_platform/execution/startup_recon.py`
- Modify: `src/hft_platform/execution/positions.py`
- Test: `tests/unit/test_startup_recon.py`
- Test: `tests/unit/test_position_store_*`

- [ ] Write failing tests for broker-only recovery preserving unknown cost basis safely without poisoning close PnL.
- [ ] Verify current `avg_price_scaled = -1` behavior reproduces the user-visible PnL issue.
- [ ] Add a cold-path cost-basis hydration mechanism from checkpoint / historical fills / broker data when available, with explicit “unknown” semantics when unavailable.
- [ ] Ensure close handling does not fabricate realized PnL from unknown basis.
- [ ] Re-run focused tests and verify pass.
- [ ] Commit.

## Task 4: G5 Graceful Reset

**Files:**
- Modify: `src/hft_platform/services/system.py`
- Modify: `src/hft_platform/execution/checkpoint.py`
- Modify: `src/hft_platform/execution/startup_recon.py`
- Test: `tests/unit/test_system_*`

- [ ] Write failing tests for reset flow clearing checkpoint/recovery state without manual file deletion.
- [ ] Add a controlled reset path that clears checkpoint, pending recovery positions, and HALT-related recovery residue.
- [ ] Ensure reset is gated and observable in logs/metrics.
- [ ] Re-run focused tests and verify pass.
- [ ] Commit.

## Task 5: G2 Strategy Attribution

**Files:**
- Modify: `src/hft_platform/execution/startup_recon.py`
- Modify: `src/hft_platform/execution/checkpoint.py`
- Modify: `src/hft_platform/execution/positions.py`
- Test: `tests/unit/test_startup_recon.py`

- [ ] Write failing tests for recovered positions keeping or reconstructing strategy ownership.
- [ ] Use checkpoint composite keys as authoritative when available; define explicit fallback semantics when only broker symbol-level data exists.
- [ ] Prevent recovered positions from silently becoming `strategy_id=""` unless ownership is truly unknowable.
- [ ] Re-run focused tests and verify pass.
- [ ] Commit.

## Task 6: G4 Fill Gap Backfill

**Files:**
- Modify: `src/hft_platform/execution/router.py`
- Modify: `src/hft_platform/execution/fill_dlq.py`
- Modify/Create: broker/fill recovery utility module
- Test: `tests/unit/test_execution_router_*`

- [ ] Write failing tests covering orphaned fills missing from ClickHouse / recorder after DLQ routing or crash windows.
- [ ] Add a cold-path recovery routine that reconciles persisted DLQ, broker execution history, and recorder state.
- [ ] Re-ingest missing fills idempotently.
- [ ] Re-run focused tests and verify pass.
- [ ] Commit.

## Execution Order

1. `G6` first: stops further checkpoint loss immediately.
2. `G3` second: removes current HALT-loop trigger.
3. `G1` third: fixes incorrect PnL semantics on broker-only recovery.
4. `G5` fourth: operational escape hatch.
5. `G2` fifth: improves recovered ownership fidelity.
6. `G4` last: cold-path historical repair after live safety is restored.
