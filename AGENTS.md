# AGENTS.md

> 重要：在處理本專案的任何任務時，請優先使用基於檢索的推理（retrieval-led reasoning），而非僅依賴預訓練的推理。

## 專案上下文 (Project Context)

|name:hft_platform|tech:Python 3.12 + Rust (PyO3)|broker:Shioaji (永豐金)|market:TWSE/OTC|

## 核心規範 (Core References)

- **HFT Laws & Coding Convention**: See `CLAUDE.md` (The Constitution)
- **Architecture Detail**: See `docs/architecture/current-architecture.md`
- **Governance Rules**: See `.agent/rules/` (auto-loaded by agents)

## 規則索引 (Rule Index, .agent/)

|skills:{00-index.md,README.md}|
|workflows:{00-index.md,alpha_contribution_pipeline.md,alpha_research_cycle.md,deploy-docker-old-computer.md,deploy-old-computer.md,rl_sim_to_real.md}|
|agents:{architect.md,build-error-resolver.md,code-reviewer.md,database-reviewer.md,doc-updater.md,e2e-runner.md,go-build-resolver.md,go-reviewer.md,planner.md,refactor-cleaner.md,security-reviewer.md,tdd-guide.md}|

## 開發指南 (Instructions)

開發本專案時請注意：

1. 實作功能前，請先閱讀 `CLAUDE.md` 了解 HFT Laws 和系統架構
2. 閱讀 `.agent/skills/` 中相關的技能檔案
3. 執行特定任務時，請遵循 `.agent/workflows/` 中的工作流指示
