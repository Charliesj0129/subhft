# Documentation Index

## Start Here

| Doc                                                        | Description                           |
| ---------------------------------------------------------- | ------------------------------------- |
| [AI_DEVELOPER_CHEAT_SHEET.md](AI_DEVELOPER_CHEAT_SHEET.md) | **(必讀) 最精簡的 AI 與開發者速查表** |
| [getting_started.md](getting_started.md)                   | Full step-by-step guide               |
| [cli_reference.md](cli_reference.md)                       | CLI commands and examples             |
| [config_reference.md](config_reference.md)                 | Config + env var reference            |
| [strategy-guide.md](strategy-guide.md)                     | Strategy development                  |
| [project_full_reference.md](project_full_reference.md)     | Full project catalog                  |

## Operations & Reliability

| Doc                                                      | Description                 |
| -------------------------------------------------------- | --------------------------- |
| [deployment_guide.md](deployment_guide.md)               | Local + Docker deployment   |
| [observability_minimal.md](observability_minimal.md)     | Required metrics + alerts   |
| [runbooks.md](runbooks.md)                               | Incident response playbooks |
| [runbooks/release-convergence.md](runbooks/release-convergence.md) | 發行收斂（深度清潔 + gate） |
| [troubleshooting.md](troubleshooting.md)                 | Common issues and fixes     |
| [hft_low_latency_runbook.md](hft_low_latency_runbook.md) | Host tuning for low latency |
| [ops_change_control.md](ops_change_control.md)           | Change approval process     |
| [operations/cron-setup-remote.md](operations/cron-setup-remote.md) | Remote cron automation templates |
| [operations/env-vars-reference.md](operations/env-vars-reference.md) | HFT_* env vars with runbook mapping |
| [outputs_and_artifacts.md](outputs_and_artifacts.md)     | Output/report locations     |

## Architecture & Reference

| Doc                                            | Description                           |
| ---------------------------------------------- | ------------------------------------- |
| [ARCHITECTURE.md](ARCHITECTURE.md)             | Architecture index → canonical source |
| [naming_conventions.md](naming_conventions.md) | File/code/metric naming rules         |
| [performance_report.md](performance_report.md) | Latency benchmarks                    |
| [MODULES_REFERENCE.md](MODULES_REFERENCE.md)   | Consolidated codebase map             |
| [architecture/](architecture/)                 | Canonical architecture baseline       |
| [architecture/shioaji-client-resilience-decoupling-plan.md](architecture/shioaji-client-resilience-decoupling-plan.md) | Shioaji 韌性補強與解耦分階段落地 |
| [adr/](adr/)                                   | Architecture decision records         |

## Research & Backtest

| Doc                                                      | Description                     |
| -------------------------------------------------------- | ------------------------------- |
| [feature_guide.md](feature_guide.md)                     | System flow and module behavior |
| [hftbacktest_integration.md](hftbacktest_integration.md) | Backtest workflow               |
| [feed_adapter.md](feed_adapter.md)                       | Feed adapter internals          |

> If you are new: read [getting_started.md](getting_started.md) first.

## Project TODOs & Tech Debt

| Doc                | Description                            |
| ------------------ | -------------------------------------- |
| [../ROADMAP.md](../ROADMAP.md) | 三年無人值守運轉路線圖與 Gate 里程碑 |
| [TODO.md](TODO.md) | 專案全域已知 TODO 與系統架構技術債總覽 |
