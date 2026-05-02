---
description: Launch Alpha Research agent team — autonomous 24h maker/taker research loop. Team Lead actively drives candidate queue and coordinates triangular checks (Researcher ↔ Challenger ↔ Execution) without per-stage user confirmation. Use `--resume` to continue after interruption.
---

# Alpha Research Team (Autonomous Maker/Taker Loop)

建立 Alpha Research team:
方向 / 第一個候選: $ARGUMENTS  （留空 → Team Lead 自主從 maker/taker 候選池挑第一個）

支援 `--resume` 旗標（寫在 `$ARGUMENTS` 最前面）：Lead 在 T0 讀 `outputs/team_artifacts/alpha-research/resume_checkpoint.json`，若新鮮（updated_at < 24h 且 budget.json 匹配）則從 `current_round` / `current_stage` 繼續；否則在 chat 警告並開 fresh run。

## Bootstrap (T-1 — 必先執行)

**執行本指令的 session = Team Lead 本人**。你必須在 T0 之前依序完成以下 bootstrap，否則不會有真正的 agent team（只會變成你一個人跑）：

1. **建立 team 容器**（使用 `TeamCreate` 工具）：
   ```
   TeamCreate({
     team_name: "alpha-research-<YYYYMMDD-HHMM>",   // 以當前日期時間命名避免與舊 team config 衝突
     description: "Autonomous maker/taker research loop (24h)",
     agent_type: "team-lead"
   })
   ```

2. **準備 shared context**：把 `.agent/teams/alpha-research/shared-context.template.yaml` 複製到 `outputs/team_artifacts/alpha-research/shared-context.yaml`，填入當前 round 的 `round_id`（R<N>，由 candidate_pool 或 user 指定）、`target_instrument`、`research_goal`（來自 `$ARGUMENTS` 或候選池第一項）。

3. **並行 spawn 3 teammates**（單一訊息、三個 `Agent` tool call，都傳 `team_name`）：
   - `Agent({team_name: <上面建立的>, name: "researcher", subagent_type: "general-purpose", model: "opus", prompt: <讀 .agent/teams/alpha-research/roles/researcher.md 完整內容，並附上 shared-context.yaml 內容>})`
   - `Agent({team_name, name: "devils-advocate", subagent_type: "general-purpose", model: "opus", prompt: <讀 .agent/teams/alpha-research/roles/devils-advocate.md 完整內容 + shared-context>})`
   - `Agent({team_name, name: "executor", subagent_type: "general-purpose", model: "opus", prompt: <讀 .agent/teams/alpha-research/roles/executor.md 完整內容 + shared-context>})`

4. **初始化 artifacts**：在 `outputs/team_artifacts/alpha-research/` 寫 `budget.json`（`started_at`, `max_runtime_hours: 24`, `max_rounds: 20`, `max_promotes: 3`, `max_consecutive_kills: 8`）+ 初始 `candidate_pool.json`（若無 `--resume`）。

5. Bootstrap 完成 → 進 T0（Init 或 Resume，見 README.md 的 Task Chain 章節）。後續每個 task 用 `TaskCreate` 建立、`TaskUpdate({owner: "researcher"|"devils-advocate"|"executor"|"team-lead"})` 指派，teammate 收到會自動開始工作；完成後 `TaskUpdate({status:"completed"})`，Lead 依 T0-T9 推進。

若 `--resume`：略過步驟 1 與 4（使用既有的 `budget.json` 與 `candidate_pool.json`），仍需執行步驟 2-3（重新 spawn teammates；teammate 實例無法跨 session 保留）。

## 運行模式

**Autonomous Continuous Mode（預設）** — Team Lead 持續推進研究，不需要每個 stage 等使用者確認。設計運行時間：最長 **24 小時** 或 budget 用盡（見 README.md 的 Budget-guard Hook 章節）。

**Scope 硬性限制**（scope C from design spec Q4）：本指令只處理 maker / taker / hybrid / exec-support signals / options MM / cross-instrument MM。禁止的類別由 `shared-context.template.yaml` 的 `scope.forbidden` 定義（pure_directional_alpha、daily_horizon_directional、twse_stock_arbitrage、any_match_in_killed_directions）。

## Skill-Integrated Team Structure

每個角色啟動前必須讀取指定 skills（見 `.agent/teams/alpha-research/README.md` 的 Skill Pipeline 章節）。

### Team Lead (Sonnet, Active Driver)

**必讀 skills**: `hft-mm-design`, `hft-strategy-lifecycle`, `hft-backtest-calibration`, `taifex-alpha-kill-criteria`, `taifex-market-structure`

職責：
1. **啟動時建立 maker/taker 候選池**（≥ 5, ≤ 15）寫入 `outputs/team_artifacts/alpha-research/candidate_pool.json`；若 `$ARGUMENTS` 非空用它作第一個 round，否則從池頂 pop。
2. **主動驅動**：每 stage 結束後直接進下一 stage，不向使用者確認。
3. **Context 注入**：Researcher T1 開始前附上 R47 maker 三層架構（`hft-mm-design`）、taker 成本牆（`taifex-market-structure`，RT 4.68 pts、TMFD6 median spread 4 pts）、最近 3 round KILL 摘要。
4. **Checkpoint**：每 round 結束寫 `round-<N>/summary.md` + append `progress.jsonl`，每 stage 結束更新 `resume_checkpoint.json`。
5. **Budget guard**：budget-guard hook 會在每個 TaskCompleted 觸發；若 hook exit 2，Lead 必須寫 `final_summary.md` 並 PAUSE。
6. **Tie-break（有限授權）**：Challenger vs Executor 同 gate 2 輪仍無共識時，跑 Tie-break 協定（見 README）——evidence-weighted 裁定並寫入 round summary。
7. **PROMOTE 路徑**：Shadow scaffold 完成後繼續 pop 下一候選（shadow → live 維持手動）；達 `max_promotes` 則 PAUSE。
8. **T8-REGEN**：pool ≤ 2 且 regen_count < 3 → 觸發 Researcher 再生子流程（見 README 的 Pool Regen Protocol）。

禁止（硬規則）：
- ❌ 單方面宣告 APPROVE/REJECT/PASS/FAIL — 那是 Researcher/Challenger/Execution 的裁定
- ❌ 跳過 Challenger 的 Kill Checklist (S0 + H1-H6 + S1-S6)
- ❌ 篡改或過濾 `make research` 的程式碼輸出
- ❌ 為了跑滿 24h 而硬推已被 scope.forbidden 排除或 killed_directions 命中的方向

### Researcher (Opus)

**必讀 skills**: `taifex-alpha-kill-criteria`, `taifex-market-structure`；maker 候選再加讀 `hft-mm-design`
讀取 `.agent/teams/alpha-research/roles/researcher.md`。候選必須符合 `shared-context.template.yaml` 的 `scope` 節，Overlap check 對照 `killed_directions`。T8-REGEN 時只產 5–10 個新候選，不鋪陳完整提案。

### Devil's Advocate (Opus)

**必讀 skills**: `taifex-alpha-kill-criteria`, `hft-backtest-calibration`
讀取 `.agent/teams/alpha-research/roles/devils-advocate.md`。Kill Checklist 第一項 S0 先判 scope，再走 H1-H6 + S1-S6。T8-REGEN 時跑 Regen Sanity Pass（3 項）。

### Executor (Opus)

**必讀 skills**: `hft-strategy-sdk`, `hft-backtest-calibration`, `hft-test-hft`, `taifex-market-structure`；maker 候選再加讀 `hft-mm-design`
讀取 `.agent/teams/alpha-research/roles/executor.md`。契約不變。

## Rules

1. Challenger 和 Executor 各自獨立 APPROVE 才能推進。
2. 所有 gate 結果來自 `make research` 程式碼輸出，不是任何人的判斷。
3. 每 stage 結束自動進下一 stage；每 round 結束寫 summary + progress。
4. Team Lead 禁止覆寫個別 gate 判定；僅在 2-round 僵局時做 evidence-weighted tie-break（rationale 必須列點並寫入 round summary）。
5. 僵局處理：見 Tie-break Protocol 章節。
6. 每 round 結束產出 `outputs/team_artifacts/alpha-research/round-<N>/summary.md` + append `progress.jsonl`。
7. PROMOTE 後 Lead 跑 post-round 流程（`hft-strategy-lifecycle` → `hft-release-gate` → `hft-production-audit`），完成後自動進下一 round（未達 `max_promotes` 時）。
8. Budget-guard hook 觸發 → Lead 寫 `final_summary.md`（aggregate verdicts、KILL reasons、下輪建議）並 PAUSE（不自動進下一 round）。
9. 使用者隨時可 `echo STOP > outputs/team_artifacts/alpha-research/STOP`；Lead 下一個 TaskCompleted 就會被 hook 擋下。
10. 連續 `max_consecutive_kills` KILL → hook 自動觸發停止（「方向性耗盡」訊號，需人類介入調整 scope 或候選池）。
