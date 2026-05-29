# AGENTS.md - HFT Platform Agent Rules

重要：本 repo 是 latency-sensitive、money-facing 的 Python/Rust HFT 平台。所有判斷採 retrieval-led reasoning，先讀現行文件與原始碼，不靠記憶猜測。

## First Reads

每次開始任務先讀：
1. `docs/MODULES_REFERENCE.md`
2. `.agent/rules/00-index.md`
3. `.agent/skills/00-index.md`

再只讀任務相關的 `SKILL.md`、`.agent/rules/*.md`、source、`docs/architecture/`。涉及 gotcha 時讀 `.agent/memory/module_gotchas.md`。引用路徑不存在時，明確回報並用 `rg --files` 找 canonical source。

## HFT Five Laws

Hot path 包含 market ingestion、Normalizer、LOB、FeatureEngine、EventBus、StrategyRunner、Risk、Gateway、Order/Execution。

1. Allocator: tick loop 不配置 heap；用預配置 buffer、ring buffer、object pool 或 Rust。
2. Cache: cache-local/packed data 優先；避免 pointer chasing。
3. Async: event loop 不做 blocking IO 或 >1 ms 同步計算。
4. Precision: 價格/金額用 scaled int x10000 或安全型別；hot path 禁 float price math。
5. Boundary: Python/Rust 邊界避免大 copy；使用 zero-copy buffer/shared memory/明確 FFI contract。

## Task Routing

先看 `.agent/skills/00-index.md`，只開直接相關技能：

- Market data/LOB/feature: `hft-market-data`, `hft-hot-path-dev`
- Strategy: `hft-strategy-dev`, `hft-strategy-sdk`
- Alpha/backtest/promotion: `hft-alpha-research`, `research-factory`, `hft-backtest-*`, `validation-gate`
- Execution/fills/TCA: `hft-execution`
- Recorder/WAL/ClickHouse: `hft-recorder`, `clickhouse-io`
- Ops/alerts/health: `hft-ops`, `troubleshoot-metrics`
- Rust/PyO3: `rust-pro`, `hft-rust-exports`
- Tests: `.agent/rules/50-testing.md`, `hft-test-hft`, `python-testing-patterns`
- Docs/architecture: `doc-updater`, `hft-architect`, `docs/architecture/`
- Broker/config: `broker-abstraction`, `multi-broker-ops`, `config-env`, `hft-env-vars`

## Alpha Governance

Research -> Gates A/B/C/D/E/F -> Canary -> Shadow -> Live. Live registry is FROZEN under loop_v1 L11 (`r47_tmf_v1`). Canonical refs: `docs/runbooks/alpha-development-workflow.md`, `research/README.md`, `docs/loop_v1_stabilization_charter.md`, `config/research/profiles/vm_ul6_strict.yaml`.

## Work Rules

- Scope edits to the request; never revert unrelated user work.
- Use `rg`/source inspection before claims.
- Prefer structured parsers/APIs for structured data.
- Docs listing paths must verify paths exist.
- Never expose secrets or broker/account identifiers.
- No production-impacting, live-trading, destructive filesystem, or destructive git commands without explicit request.
- Commit/stage only intentional files.

## Verification

Docs-only: inspect Markdown, verify paths, review diff. Bug fix: focused regression test. Hot-path/shared contract: targeted tests plus scaled-int, monotonic-time, fail-closed, state-transition, async/latency checks. Do not claim tests pass unless run.
