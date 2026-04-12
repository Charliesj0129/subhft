---
description: Launch Alpha Research agent team — paper-grounded alpha development with triangular checks (Researcher ↔ Challenger ↔ Execution), integrated with 10 strategy-focused skills
---

# Alpha Research Team

建立 Alpha Research team:
方向: $ARGUMENTS

## Skill-Integrated Team Structure

每個角色啟動前必須讀取指定 skills（見 `.agent/teams/alpha-research/README.md` Skill Pipeline）。

Team Lead (Sonnet): 按照 `.agent/teams/alpha-research/README.md` task chain 協調。
你沒有品質判斷權，只負責分派和匯總。每個 stage 結束後向我報告。
如果方向是模糊描述或留空，先讓 Researcher 做論文探索，
收斂出 2-3 個候選 alpha 方向後向我報告，我選定再開始 Stage 2。
PROMOTE 後使用 `hft-strategy-lifecycle` skill 引導 scaffold→shadow→live 流程。

Researcher (Opus): **必讀 skills: `taifex-alpha-kill-criteria`, `taifex-market-structure`**
讀取 `.agent/teams/alpha-research/roles/researcher.md` 的完整角色定義。
從 arXiv MCP 搜尋論文開始。使用 `taifex-alpha-kill-criteria` 的 3-question pre-research gate 過濾方向。
使用 .agent/skills/iterative-retrieval/SKILL.md 取得論文。
如果沒有指定方向，先做文獻探索，提出 2-3 個候選方向。
每個產出必須提交給 Challenger 和 Execution 審查。
❌ 禁止提出 tick-to-hour 方向性 alpha（TAIFEX 已結構性耗盡）。

Challenger (Opus): **必讀 skills: `taifex-alpha-kill-criteria`, `hft-backtest-calibration`**
讀取 `.agent/teams/alpha-research/roles/devils-advocate.md` 的完整角色定義。
你的職責是質疑 Researcher 的每一個決策。
每次審查必須執行完整 Kill Checklist (H1-H6 + S1-S6)。
使用 `taifex-alpha-kill-criteria` 的 mandatory signal validation gates（detrended IC、bid/ask execution、recency、subsampling）。
使用 `hft-backtest-calibration` 的 Common Traps 表驗證回測結果。
翻譯階段必須 diff impl.py vs strategy.py 每一行公式。
未解決質疑 > 0 = 你必須 REJECT。

Execution (Opus): **必讀 skills: `hft-strategy-sdk`, `hft-backtest-calibration`, `hft-test-hft`, `taifex-market-structure`**
讀取 `.agent/teams/alpha-research/roles/executor.md` 的完整角色定義。
使用 `hft-strategy-sdk` 實作 BaseStrategy（`__slots__`、`on_gap()` reset、`on_risk_feedback()` release）。
使用 `hft-backtest-calibration` 選擇正確 fill model（maker = CK direct，taker = hftbacktest default）。
使用 `hft-test-hft` 寫 scaled int + monotonic time 測試。
如果是 MM 策略: 必讀 `hft-mm-design`（R47 三層架構）。
檢查延遲 profile、feature mapping、config/risk limits 一致性。
Config drift > 0 = 你必須 REJECT。

## Rules

1. Challenger 和 Execution 各自獨立 APPROVE 才能推進。
2. 所有 gate 結果來自 `make research` 程式碼輸出，不是任何人的判斷。
3. 每個 stage 結束等我確認才進下一階段。
4. Team Lead 禁止使用 APPROVE/REJECT/PASS/FAIL — 只能轉述他人判定。
5. 僵局處理: 如果 2 輪對話後仍無共識，Team Lead 向我報告雙方立場和證據。
6. 每個 stage 結束時產出摘要 artifact 到 outputs/team_artifacts/alpha-research/。
7. PROMOTE 後 Team Lead 啟動 post-team 流程（`hft-strategy-lifecycle` → `hft-release-gate` → `hft-production-audit`）。
