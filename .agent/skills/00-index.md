# Skills Index

> 精簡後的 HFT Platform 技能索引。只保留本專案常用的 HFT、TAIFEX、Python/Rust、ClickHouse、研究治理、測試/安全/運維與架構技能。
>
> 使用原則：先讀本索引，再只開啟當前任務直接相關的 `SKILL.md`。不要把整個 skills 目錄載入上下文。

## HFT Core

| Skill | When to use |
| --- | --- |
| `hft-helper` | 不確定該用哪個 HFT 技能時的路由入口 |
| `hft-architect` | runtime planes、Python/Rust 邊界、模組邊界、架構決策 |
| `hft-market-data` | broker ingestion、Normalizer、LOBEngine、FeatureEngine、market-data hot path |
| `hft-hot-path-dev` | tick loop / hot path 變更，檢查 allocator、precision、latency discipline |
| `hft-strategy-dev` | live strategy、`BaseStrategy`、`StrategyContext`、`OrderIntent` |
| `hft-strategy-sdk` | strategy hook/API、position tracking、config-driven strategy wiring |
| `hft-strategy-lifecycle` | strategy scaffold -> shadow -> live 的生命週期與 gates |
| `hft-mm-design` | market-making 策略設計與失效條件 |
| `hft-execution` | fills、positions、reconciliation、execution optimizer、TCA |
| `hft-recorder` | WAL、ClickHouse writer、loader、disk pressure、recorder IO |
| `hft-ops` | session governor、autonomy degradation、position flattener、pre/post market ops |
| `hft-data-contracts` | `OrderIntent`、`RiskDecision`、`FillEvent` 等核心資料契約 |
| `hft-env-vars` | `HFT_*`、`SHIOAJI_*`、`CH_*` runtime/env reference |
| `hft-rust-exports` | `rust_core` PyO3 exports and Python/Rust boundary reference |
| `hft-production-audit` | 多平面 runtime safety audit |
| `hft-release-gate` | release readiness、latency、coverage、security、pre-market checklist |
| `hft-test-hft` | HFT-specific tests: scaled int、monotonic time、fail-closed Rust、state matrices |
| `troubleshoot-metrics` | Prometheus、Docker、StormGuard、WAL、execution、ops health diagnostics |

## Broker / Market Structure

| Skill | When to use |
| --- | --- |
| `broker-abstraction` | broker facade、multi-broker contracts、adapter boundary |
| `multi-broker-ops` | broker switching、failover、credentials、latency profiles |
| `fubon-tradeapi` | Fubon TradeAPI runtime/API reference |
| `fubon-contracts` | Fubon contracts and symbol handling |
| `taifex-market-structure` | TAIFEX/TMFD/TXFD/TXO conventions、fees、spread/liquidity regimes |
| `symbols-sync` | `symbols.list` -> `symbols.yaml` synchronization |

## Alpha Research / Backtest

| Skill | When to use |
| --- | --- |
| `hft-alpha-research` | alpha scaffold、governed datasets、Gate A-C、research artifacts |
| `research-factory` | paper -> prototype -> backtest -> promote -> live workflow |
| `research-data-governance` | dataset sidecars、synthetic LOB、UL6 provenance |
| `validation-gate` | Gate A-E interpretation and promotion blockers |
| `hft-backtest` | raw hftbacktest semantics、queue/fill model details |
| `hft-backtester` | project `HftBacktestAdapter`、latency modeling、Gate C lane |
| `hft-backtest-calibration` | fill-model realism、latency profiles、walk-forward traps |
| `taifex-alpha-kill-criteria` | pre-research feasibility and structural alpha kill criteria |

## Python / Rust / Performance

| Skill | When to use |
| --- | --- |
| `python-pro` | Python 3.12+ implementation details |
| `python-testing-patterns` | pytest patterns, fixtures, regression coverage |
| `async-python-patterns` | asyncio/concurrency and non-blocking service work |
| `rust-pro` | Rust implementation and review |
| `rust_feature_engineering` | Rust + PyO3 feature-kernel workflow |
| `performance-profiling` | profiling and latency/performance investigation |
| `sequential-thinking` | complex debugging where hypotheses must be explicitly eliminated |

## Data / Ops / Architecture Support

| Skill | When to use |
| --- | --- |
| `clickhouse-io` | ClickHouse schema、queries、TTL、recorder IO、WAL replay |
| `data-flow-verify` | event/data-flow contract verification |
| `config-env` | runtime configuration and environment variable work |
| `runtime-debug` | live/runtime debugging |
| `healthcheck` | host and runtime health checks |
| `c4-architecture` | C4 architecture diagrams |
| `mermaid-diagrams` | focused Mermaid diagrams for architecture or flow explanations |
| `doc-updater` | reconcile docs/codemaps with current source tree |
| `planner` | phased implementation plan for large HFT changes |
| `commit-work` | staging and commit hygiene |
| `git-parallel` | worktrees for isolated experiments or long-running backtests |
| `github` | GitHub CLI issue/PR operations |
| `session-manager` | project session state management |
