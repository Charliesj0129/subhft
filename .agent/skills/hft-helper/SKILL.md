<!-- REVIEW-2026-04-17: unreferenced by rules/workflows/teams/agents. Confirm or delete. -->
---
name: hft-helper
description: Use when starting an HFT platform task and unsure which project-specific skill fits, or when routing work across the research, strategy, broker, architecture, and operations workflows.
---

# HFT Helper

Use this skill as a router, not as a source of deep implementation detail.

## Routing Table

| If the task is about... | Use this skill |
| --- | --- |
| alpha scaffolding, governed datasets, research factory flow | `hft-alpha-research` |
| pass/fail interpretation for Gate A-E | `validation-gate` |
| adapter-based backtest realism or parity | `hft-backtester` |
| raw `hftbacktest` engine semantics | `hft-backtest` |
| live strategy code and feature-plane access | `hft-strategy-dev` |
| market data: normalizer, LOB, feed adapter, Rust accel, feature engine | `hft-market-data` |
| architecture boundaries, runtime planes, Python-Rust design | `hft-architect` |
| execution plane: fills, positions, reconciliation, optimizer | `hft-execution` |
| recorder: batcher, ClickHouse writer, WAL, WAL loader, disk monitor | `hft-recorder` |
| operations: session governor, autonomy, flattener, backup | `hft-ops` |
| Shioaji broker lifecycle | `shioaji-contracts` |
| Fubon broker lifecycle or API integration | `fubon-contracts` or `fubon-tradeapi` |
| broker switching and failover operations | `multi-broker-ops` |
| symbol universe generation and overrides | `symbols-sync` |
| ClickHouse tables or operational queries | `clickhouse-io` |
| runtime health, StormGuard, WAL, Docker ops | `troubleshoot-metrics` |
| dataset preparation, metadata sidecars, synthetic data | `research-data-governance` |
| end-to-end alpha pipeline orchestration | `research-factory` |

## Workflow Map

### Alpha Development (Paper to Live)
```text
paper -> hft-alpha-research -> validation-gate -> hft-backtester -> hft-strategy-dev -> hft-architect
```

### Runtime Debugging
```text
symptom -> troubleshoot-metrics -> hft-execution (fills/positions) -> hft-ops (session/autonomy)
```

### Deployment
```text
code change -> hft-architect (boundary check) -> multi-broker-ops (broker config) -> hft-ops (pre-market)
```

Use `research/SOP.md` as the canonical paper-to-live lane.

## Quick Commands

```bash
# Runtime
uv run hft run sim                           # Start simulation
uv run hft run live                          # Start live (needs SHIOAJI_API_KEY)
uv run hft check                             # Validate config

# Alpha
uv run hft alpha validate <alpha_id>         # Gate A-C
uv run hft alpha promote <alpha_id>          # Gate D-E
make research ALPHA=<id> OWNER=<name> DATA='<path>'

# Operations
make pre-market-check                        # Pre-market health
make post-market-check                       # Post-market health
make recorder-status                         # WAL/CK status

# Quality
make ci                                      # Full CI pipeline
make check                                   # All quality gates
make hotpath-profile                         # Latency profiling
```

## Key Reference Files

| File | Purpose |
| --- | --- |
| `CLAUDE.md` | Constitution (5 HFT Laws, architecture, env vars) |
| `docs/architecture/current-architecture.md` | Canonical architecture (7 planes) |
| `docs/MODULES_REFERENCE.md` | All 37 packages mapped |
| `docs/guides/cli-reference.md` | Full CLI reference |
| `docs/operations/env-vars-reference.md` | 60+ env vars |
| `.agent/rules/` | Auto-loaded governance rules |
