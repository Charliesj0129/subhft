# Documentation Index

## Start Here

| Doc | Description |
|-----|-------------|
| [AI_DEVELOPER_CHEAT_SHEET.md](AI_DEVELOPER_CHEAT_SHEET.md) | **(必讀) 最精簡的 AI 與開發者速查表** |
| [getting_started.md](getting_started.md) | Full step-by-step guide |
| [cli_reference.md](cli_reference.md) | CLI commands and examples |
| [config_reference.md](config_reference.md) | Config + env var reference |
| [strategy-guide.md](strategy-guide.md) | Strategy development |
| [feature_guide.md](feature_guide.md) | FeatureEngine flow and module behavior |
| [project_full_reference.md](project_full_reference.md) | Full project catalog |

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
| [hftbacktest_integration.md](hftbacktest_integration.md) | Backtest workflow |
| [feed_adapter.md](feed_adapter.md) | Feed adapter internals |

## Operations & Reliability

| Doc | Description |
|-----|-------------|
| [deployment_guide.md](deployment_guide.md) | Local + Docker deployment |
| [operations/env-vars-reference.md](operations/env-vars-reference.md) | HFT_* env vars with runbook mapping |
| [observability_minimal.md](observability_minimal.md) | Required metrics + alerts |
| [runbooks.md](runbooks.md) | Incident response playbooks |
| [runbooks/release-convergence.md](runbooks/release-convergence.md) | 發行收斂（深度清潔 + gate） |
| [troubleshooting.md](troubleshooting.md) | Common issues and fixes |
| [hft_low_latency_runbook.md](hft_low_latency_runbook.md) | Host tuning for low latency |
| [ops_change_control.md](ops_change_control.md) | Change approval process |
| [operations/cron-setup-remote.md](operations/cron-setup-remote.md) | Remote cron automation templates |
| [outputs_and_artifacts.md](outputs_and_artifacts.md) | Output/report locations |

## Architecture & Reference

| Doc | Description |
|-----|-------------|
| [architecture/current-architecture.md](architecture/current-architecture.md) | Canonical architecture baseline (7 planes) |
| [architecture/multi-broker-support.md](architecture/multi-broker-support.md) | Multi-broker ADR (Shioaji + Fubon) |
| [architecture/signal-monitor-design.md](architecture/signal-monitor-design.md) | Signal Monitor TUI design |
| [architecture/rust_pyo3.md](architecture/rust_pyo3.md) | Rust/PyO3 boundary spec |
| [architecture/latency-baseline-shioaji-sim-vs-system.md](architecture/latency-baseline-shioaji-sim-vs-system.md) | Latency realism baseline |
| [architecture/feature-engine-lob-research-unification-spec.md](architecture/feature-engine-lob-research-unification-spec.md) | Feature engine unification spec |
| [architecture/shioaji-client-resilience-decoupling-plan.md](architecture/shioaji-client-resilience-decoupling-plan.md) | Shioaji 韌性補強與解耦 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Architecture index → canonical source |
| [naming_conventions.md](naming_conventions.md) | File/code/metric naming rules |
| [performance_report.md](performance_report.md) | Latency benchmarks |
| [MODULES_REFERENCE.md](MODULES_REFERENCE.md) | Consolidated codebase map |
| [adr/](adr/) | Architecture decision records |

## Project TODOs & Tech Debt

| Doc | Description |
|-----|-------------|
| [../ROADMAP.md](../ROADMAP.md) | 三年無人值守運轉路線圖與 Gate 里程碑 |
| [TODO.md](TODO.md) | 專案全域已知 TODO 與系統架構技術債總覽 |

> If you are new: read [getting_started.md](getting_started.md) first.
