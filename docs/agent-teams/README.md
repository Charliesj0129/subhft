# Agent Teams — Quick Launch Guide

> 三個預設團隊，一行指令啟動。需要 Claude Code v2.1.32+。

## 前置設定

Agent teams 已透過 `.claude/settings.local.json` 啟用:
```json
{ "env": { "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1" } }
```

## 快速啟動

### 1. Alpha Research — 論文驅動的 alpha 研發

```
/alpha-research OFI 類型
/alpha-research 知情毒性流
/alpha-research sqrt_ofi
/alpha-research              ← 留空 = 開放式文獻探索
```

**團隊**: Team Lead (Sonnet) + Researcher + Challenger + Execution (全 Opus)
**模式**: 三角牽制 — Challenger 全程質疑，Execution 驗證可交易性
**你的角色**: 每個 stage 結束後審查報告，決定是否推進
**SOP**: `research/SOP.md` 8-stage pipeline
**詳細設計**: `docs/superpowers/specs/2026-03-22-agent-teams-design.md#team-1-alpha-research`

### 2. Code Review — 多維度平行審查

```
/code-review-team staged changes
/code-review-team PR #142
/code-review-team audit execution plane
/code-review-team audit 全平台
```

**團隊**: Team Lead + Security + Performance + Correctness (全 Opus)
**模式**: 平行分工 — 三人獨立審查，Team Lead 匯總 + 用 skill 修復
**你的角色**: 審查匯總報告，決定哪些要修
**詳細設計**: `docs/superpowers/specs/2026-03-22-agent-teams-design.md#team-2-code-review`

### 3. Debugging — 跨 Runtime Plane 平行調查

```
/debug-team StormGuard 在 14:32 觸發 HALT 但市場數據正常
/debug-team strategy 收到的 mid_price 全是 0 但 normalizer log 顯示有值
/debug-team recorder 從 14:30 開始沒有寫入任何資料到 ClickHouse
```

**團隊**: Team Lead (Opus) + Data + Decision + Infra (全 Opus)
**模式**: 平行調查 + 跨邊界對質 — 找到 root cause 的人修復，Team Lead 驗證
**你的角色**: 確認 root cause 和修復方案
**詳細設計**: `docs/superpowers/specs/2026-03-22-agent-teams-design.md#team-3-debugging`

## 自然語言啟動（進階）

不用 slash command，直接描述也行:

```
建立 Alpha Research team，方向: 用 order flow imbalance 做 market making signal
```

```
建立 Code Review team，模式: Architecture Audit，目標: recorder 模組
```

```
建立 Debugging team，症狀: shadow 策略在 13:00 後 PnL 突然歸零
```

## Benchmark

驗證團隊生成品質，見:
- `docs/agent-teams/benchmarks/alpha-research-bench.md`
- `docs/agent-teams/benchmarks/code-review-bench.md`
- `docs/agent-teams/benchmarks/debugging-bench.md`

## 失敗處理

所有 team 共用規則:
- **僵局**: 2-3 輪後無共識 → Team Lead 向你報告雙方立場
- **Crash**: Team Lead 立即通知你，你決定重新 spawn 或終止
- **Context 耗盡**: 每個 stage 產出摘要到 `outputs/team_artifacts/<team>/`

## 相關文件

| 文件 | 路徑 |
|------|------|
| 完整設計 spec | `docs/superpowers/specs/2026-03-22-agent-teams-design.md` |
| Research SOP | `research/SOP.md` |
| HFT Constitution | `CLAUDE.md` |
| Agent Rules | `.agent/rules/` |
