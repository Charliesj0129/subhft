# Research Pipeline Execution Plan (As-Built + Next 90 Days)

Date: 2026-02-20  
Owner: Principal HFT Architect  
Status: Active

## 1. Objective

Run one consistent alpha lifecycle:

1. hypothesis -> implementation -> validation (Gate A-C)
2. experiment logging and pool checks
3. promotion decision (Gate D-E)
4. canary lifecycle and rollback control

All stages must emit deterministic artifacts under `research/` or `config/strategy_promotions/`.

## 2. Current Implementation Status

### Stage A: Artifact structure and discovery (Done)
1. Registry and schema contracts: `research/registry/schemas.py`.
2. Discovery and loading: `research/registry/alpha_registry.py`.
3. Scaffold command: `hft alpha scaffold` -> `research/tools/alpha_scaffold.py`.

### Stage B: Validation gates (Done)
1. Gate A/B/C orchestrator: `src/hft_platform/alpha/validation.py`.
2. Research backtest core: `research/backtest/hbt_runner.py`.
3. Scorecard generation: `research/registry/scorecard.py`.
4. Command: `hft alpha validate`.

### Stage C: Experiment tracking and compare (Done)
1. Run metadata and artifacts: `src/hft_platform/alpha/experiments.py`.
2. Commands:
- `hft alpha experiments list`
- `hft alpha experiments compare`
- `hft alpha experiments best`

### Stage D: Pool analysis and combinatorial search (Done)
1. Pool matrix/weights/marginal contribution: `src/hft_platform/alpha/pool.py`.
2. Combinatorial search: `research/combinatorial/search_engine.py`.
3. Commands:
- `hft alpha pool ...`
- `hft alpha search ...`

### Stage E: Promotion and canary control (Done, operationally improving)
1. Gate D/E + promotion config writer: `src/hft_platform/alpha/promotion.py`.
2. Canary evaluate/apply: `src/hft_platform/alpha/canary.py`.
3. Commands:
- `hft alpha promote`
- `hft alpha canary status`
- `hft alpha canary evaluate --apply`

### Stage F: RL bridge (Done)
1. RL adapter and lifecycle: `research/rl/alpha_adapter.py`, `research/rl/lifecycle.py`.
2. Command: `hft alpha rl-promote`.

## 3. Canonical Command Path

1. Create artifact
- `uv run hft alpha scaffold <alpha_id> --complexity O1`

2. Validate
- `uv run hft alpha validate --alpha-id <alpha_id> --data <npy_or_npz>`

3. Inspect experiments
- `uv run hft alpha experiments list --alpha-id <alpha_id>`
- `uv run hft alpha experiments best --metric sharpe_oos --alpha-id <alpha_id>`

4. Pool checks
- `uv run hft alpha pool redundant --threshold 0.7`
- `uv run hft alpha pool marginal --alpha-id <alpha_id> --min-uplift 0.05`

5. Promote
- `uv run hft alpha promote --alpha-id <alpha_id> --owner <owner> --shadow-sessions <n>`

6. Operate canary
- `uv run hft alpha canary status`
- `uv run hft alpha canary evaluate --alpha-id <alpha_id> --slippage-bps <x> --dd-contrib <y> --error-rate <z> --sessions <n> --apply`

## 4. Gaps Requiring Architecture Work

1. Audit schema bootstrap gap
- `src/hft_platform/alpha/audit.py` can write to `audit.alpha_*`, but `audit.sql` is not auto-applied in runtime bootstrap.

2. Canary automation integration
- Canary logic exists, but automated runtime metric feed and scheduling are still operator-driven.

3. Artifact retention policy
- `research/experiments/runs` can grow without lifecycle pruning.

4. Promotion governance
- Guardrails are file-based and command-driven; missing centralized policy/audit dashboard.

## 5. Next 90-Day Plan

### D+30
1. Add explicit command/script to bootstrap both runtime and audit ClickHouse schemas.
2. Add smoke test validating alpha audit tables are writable when enabled.

### D+60
1. Add scheduled canary evaluation runner consuming runtime metrics snapshots.
2. Add automatic rollback incident record format.

### D+90
1. Add artifact GC policy for experiment runs and stale promotions.
2. Add operator report command consolidating gate status, canary state, and latest experiment metrics.
