# Agent System v3 Design — 分層演進(Layered Evolution)

Date: 2026-07-14 · Status: Approved(逐節經 Charlie 批准)· Author: orchestrator session
Predecessor: Agent System v2(AGENTS.md + `.agent/` rules/skills/memory + harness bindings 2026-07-14)
Implementation plan: 本 spec 的 plan 檔(session plan);落地 commit 見 `.agent/CHANGELOG.md` 2026-07-14 起之 v3 條目。

## Motivation

meta-audit 2026-07-14(`.agent/reports/agent-meta-audit-2026-07-14.md`)三個核心數據:

1. 委派介入率 50%(4/8),全部是誠實性/交付型介入(無 verdict、無報告、overclaim),零程式碼修正 — 問題在「交付結構」不在模型能力。
2. 唯一有淨收益的委派類型 = 平行 research fan-out;串行小任務委派成本高於直接做(ROI-first 路由因此保留)。
3. 「不在 agent 面前跑的 gate 會默默壞掉」:一條 scheduled gate 紅了 77 天(2026-04-27→07-13)才被發現。

Charlie 選定四軸全做(自動化例行/管線化/硬性防護/research factory),途徑 = 分層演進:每層疊在 v2 上、獨立可交付可回滾。**hooks 不建新政策**——只把 v2 已明文的政策從 prompt/ask 層下沉到工具攔截層。

## L1 — 硬性防護底層(hooks)

| Hook | 事件 | 失效策略 | 攔什麼(對應既有政策) |
|---|---|---|---|
| `scope_guard` | PreToolUse Edit\|Write\|NotebookEdit | fail-closed | delegation window(`.agent/runtime/active-packet.json` 存在)期間,packet allowlist 之外的寫入(AGENTS.md executor「只改 packet 列的檔案」) |
| `git_guard` | PreToolUse Bash | fail-closed | 子代理的非唯讀 git(AGENTS.md「git execution = orchestrator only」) |
| `discipline_feedback` | PostToolUse Edit\|Write | fail-open | 編輯 `src/hft_platform/` Python 檔後即跑 `scripts/check_discipline.py --files`,違規即刻回饋(01-core-laws) |
| `commit_audit` | PostToolUse Bash(git commit) | fail-open | HEAD 檔案集 vs 宣告的 allowlist marker(narrow-commit gate 的事後防線) |

腳本:`.claude/hooks/*.py`(stdlib-only,<25s,絕不輸出 secrets);接線:`.claude/settings.json` `hooks` 區塊;runtime markers:`.agent/runtime/`(不進版控,`/.agent/*` 本被 ignore)。子代理判別器由探針(hook stdin JSON 實測)定案,fallback = delegation-window 全域封鎖。失效原則:強制類 fail-closed(擋錯比放錯便宜),advisory 類 fail-open(hook 壞掉不能卡死所有編輯)。

## L2 — 多 agent 管線化

具名管線 = `.agent/skills/pipeline-*/SKILL.md` + Task 系統鏈(blockedBy)+ 固定交接 artifacts(`.agent/memory/delegations/<id>/{packet,executor-report,review-verdict}.md`)。**路由表不動**:ROI-first(direct-by-default)仍由 task-intake 判;管線只定義「決定委派之後怎麼走」。

- **P-implement**(本波唯一實作):PACKET → EXECUTE(hft-executor)→ REVIEW(hft-reviewer,sync、diff-scoped、verdict 強制)→ LAND(orchestrator 親驗 + narrow commit)→ LEDGER。直接針對 50% 介入率的交付型失敗。
- **P-research-fanout**(Future):唯一淨收益委派類型的正式化;等 P-implement 兩次乾淨跑完再加。
- **P-test-harden**(Future):test-gap-analysis → hft-test-writer → break-probe 審查;同上條件。

## L3 — 無人值守例行(預設只讀 + 白名單升級)

Routine 註冊制:每個例行 = `.agent/routines/<name>.md`,frontmatter 契約(schedule / write_scope / notify / venue)。**write_scope 預設 `none`;runner 拒跑任何 write_scope≠none 的 routine**;白名單升級 = authority 變更,需新 ADR + CHANGELOG。執行 local-first:專有交易程式碼不上 cloud sandbox;本機排程(cron 或 Windows Task Scheduler→WSL)跑 headless `claude -p`,venue = 專用 worktree(絕不在主工作樹),通知重用 `scripts/_notify.sh`(Telegram)。

首發 routine(全只讀):R1 nightly-ci-triage(夜間 CI 紅腿分流——直接回應 77 天盲區)、R2 gate-health-patrol(週)、R3 memory-hygiene(月)、R4 meta-audit-cadence(季/ledger+10,產草稿;正式報告仍由 orchestrator session 落檔)。

## L4 — Research Factory 管線(DEFERRED)

設計:Charlie 定方向(agent 永不反向提案)→ 管線跑完整驗證(P-research-fanout 平行回測/驗證 + L3 排程夜間 batch)→ 證據窄 commit(既有 verdict-evidence cadence,orchestrator 執行)。硬約束全繼承:frozen registry(`r47_tmf_v1`)不碰;Shioaji 延遲 profile `v2026-04-24_measured`;每個 PnL 聲明署名回測方法;edge < 2× spread 必用 bid/ask fills;recency-first 驗證;verdict 忠實(KILL / NEEDS-MORE-DAYS / INCONCLUSIVE);絕不為結果鬆 gate。**不排實作 wave;啟動需 Charlie 明示**(research lanes 目前全關)。

## 自主權模型

無人值守情境(排程例行、自主管線段)預設只讀;寫權逐 routine 白名單,白名單本身是治理文件;push / merge / live ops / 主機排程安裝永遠手動(Charlie 逐次批准)。正式決策:`docs/adr/002_unattended_agent_autonomy.md`;規則:`.agent/rules/65-unattended-autonomy.md`。

## 成功指標

| 指標 | 基線 | 目標 | 量測 |
|---|---|---|---|
| 委派介入率 | 50%(meta-audit 2026-07-14) | 下降(P-implement 管線後) | model-routing.md ledger |
| 紅 gate 發現延遲 | 77 天(最差案例) | <24h | R1 routine 報告 |
| hook 誤擋數 | —(新) | 0 | session 記錄 / lessons_learned |
| P-implement 淨收益 | —(新) | 正 | ledger net-win 欄 |

## Waves

W1 = L1 hooks → W2 = P-implement → W3 = L3 routines(全只讀)。L4 無 wave(DEFERRED)。每 wave:narrow commit(local only,不 push)、`.agent/CHANGELOG.md` 條目、`make agent-docs-check` 綠;routing-relevant 變更後 golden intake 8 案重跑。
