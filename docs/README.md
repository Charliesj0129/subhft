# Documentation Index

## Start Here

| Doc | Description |
|-----|-------------|
| [AI Developer Cheat Sheet](guides/ai-developer-cheat-sheet.md) | **(必讀) 最精簡的 AI 與開發者速查表** |
| [Getting Started](guides/getting-started.md) | Full step-by-step guide |
| [CLI Reference](guides/cli-reference.md) | CLI commands and examples |
| [Strategy Guide](guides/strategy-guide.md) | Strategy development |
| [Feature Guide](guides/feature-guide.md) | FeatureEngine flow and module behavior |
| [Project Full Reference](project_full_reference.md) | Full project catalog |

## Agent Teams

| Doc | Description |
|-----|-------------|
| [agent-teams/README.md](agent-teams/README.md) | Quick launch guide for 3 agent teams |
| [superpowers/specs/2026-03-22-agent-teams-design.md](superpowers/specs/2026-03-22-agent-teams-design.md) | Full design spec (teams, protocols, benchmarks) |
| [agent-teams/benchmarks/](agent-teams/benchmarks/) | Benchmark scenarios for quality validation |

## Research & Alpha Pipeline

| Doc | Description |
|-----|-------------|
| [../research/SOP.md](../research/SOP.md) | Research SOP (Paper to Live Factory, 8 stages) |
| [HFTBacktest Integration](guides/hftbacktest-integration.md) | Backtest workflow |
| [Feed Adapter](guides/feed-adapter.md) | Feed adapter internals |

## Operations & Reliability

| Doc | Description |
|-----|-------------|
| [Deployment](operations/deployment.md) | Local + Docker deployment |
| [Env Vars Reference](operations/env-vars-reference.md) | HFT_* env vars with runbook mapping |
| [Observability](operations/observability.md) | Required metrics + alerts |
| [Runbooks](runbooks/README.md) | Incident response playbooks |
| [Release Convergence](runbooks/release-convergence.md) | 發行收斂（深度清潔 + gate） |
| [Troubleshooting](operations/troubleshooting.md) | Common issues and fixes |
| [Low-Latency Tuning](runbooks/low-latency-tuning.md) | Host tuning for low latency |
| [Change Control](operations/change-control.md) | Change approval process |
| [Cron Setup (Remote)](operations/cron-setup-remote.md) | Remote cron automation templates |
| [Outputs & Artifacts](outputs_and_artifacts.md) | Output/report locations |

## Architecture & Reference

| Doc | Description |
|-----|-------------|
| [Architecture Overview](architecture/overview.md) | Architecture entry point → canonical source |
| [Current Architecture](architecture/current-architecture.md) | Canonical architecture baseline (7 planes) |
| [Multi-Broker Support](architecture/multi-broker-support.md) | Multi-broker ADR (Shioaji + Fubon) |
| [Signal Monitor Design](architecture/signal-monitor-design.md) | Signal Monitor TUI design |
| [Rust/PyO3](architecture/rust_pyo3.md) | Rust/PyO3 boundary spec |
| [Latency Baseline](architecture/latency-baseline-shioaji-sim-vs-system.md) | Latency realism baseline |
| [Feature Engine Spec](architecture/feature-engine-lob-research-unification-spec.md) | Feature engine unification spec |
| [Shioaji Resilience](architecture/shioaji-client-resilience-decoupling-plan.md) | Shioaji 韌性補強與解耦 |
| [Naming Conventions](guides/naming-conventions.md) | File/code/metric naming rules |
| [Performance Report](reports/performance-report.md) | Latency benchmarks |
| [Modules Reference](MODULES_REFERENCE.md) | Consolidated codebase map (37 packages, ~210 files) |
| [CODEMAPS/](CODEMAPS/) | Quick-reference codemaps (architecture, backend, data, dependencies) |
| [ADRs](adr/) | Architecture decision records |

## Project TODOs & Tech Debt

| Doc | Description |
|-----|-------------|
| [ROADMAP.md](../ROADMAP.md) | 三年無人值守運轉路線圖與 Gate 里程碑 |
| [TODO.md](TODO.md) | 專案全域已知 TODO 與系統架構技術債總覽 |

> If you are new: read [Getting Started](guides/getting-started.md) first.
