# CLAUDE.md - HFT Platform Compact Context

## Identity

`hft_platform`: Python 3.12 + Rust/PyO3 high-frequency trading platform for Shioaji/Fubon, ClickHouse, Redis, Prometheus. Money-facing and latency-sensitive.

## Retrieval First

Before claims or edits, read:
1. `docs/MODULES_REFERENCE.md`
2. `.agent/rules/00-index.md`
3. `.agent/skills/00-index.md`

Then open only task-relevant rules, skills, source, architecture docs, and `.agent/memory/module_gotchas.md` when relevant. Missing referenced docs: report the path, find canonical replacements with `rg --files`.

## Non-Negotiable Laws

Hot path means market ingestion, normalizer, LOB, feature engine, event bus, strategy dispatch, risk, gateway, order/execution.

1. No heap allocation per tick; preallocate, pool, ring-buffer, or Rust.
2. Prefer cache-local packed data; avoid pointer chasing.
3. No blocking IO or >1 ms sync compute on the event loop.
4. Price/accounting values use scaled int x10000 or explicit safe types; no hot-path float price math.
5. Python/Rust boundary must avoid large copies; use buffers/shared memory/clear FFI contracts.

## Runtime Map

Exchange -> BrokerFacade -> Normalizer -> LOBEngine -> FeatureEngine -> RingBufferBus -> StrategyRunner -> RiskEngine/GatewayService -> OrderAdapter -> BrokerFacade -> Execution/Positions -> Recorder WAL/ClickHouse.

Core contracts: `OrderIntent -> RiskDecision -> OrderCommand -> FillEvent -> PositionDelta`. Canonical event prices are scaled int x10000.

## Alpha Governance

Research -> Gates A/B/C/D/E/F -> Canary -> Shadow -> Live. Live registry is FROZEN under loop_v1 L11, locked to `r47_tmf_v1`.

Canonical refs: `docs/runbooks/alpha-development-workflow.md`, `research/README.md`, `docs/loop_v1_stabilization_charter.md`, `config/research/profiles/vm_ul6_strict.yaml`.

## Work Rules

- Keep changes scoped; preserve dirty user work.
- Use `rg` and source inspection before behavioral claims.
- Use structured parsers/APIs for structured data.
- Read task-specific skill before hot-path, alpha, broker, config, Rust, ops, or testing work.
- Do not run live trading, destructive filesystem, or destructive git operations unless explicitly requested.
- Never expose secrets, broker credentials, account IDs, or tokens.

## Verification

Match blast radius. Docs-only: verify referenced paths and review diff. Bug fix: add/update focused regression test. Hot-path/shared contract: run targeted tests plus scaled-int, monotonic-time, fail-closed, state-transition, and latency-sensitive checks. Never claim tests pass unless run.

## Common Commands

Setup/install: `uv sync`; Rust: `uv run maturin develop --manifest-path rust_core/Cargo.toml`; tests: `make test`, `make test-all`; lint/type/CI: `make lint`, `make typecheck`, `make ci`; run sim: `uv run hft run sim`; Docker: `make start`, `make stop`, `make logs`.
