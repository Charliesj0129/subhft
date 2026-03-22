# Agent Teams Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create quick-launch infrastructure for 3 agent teams (Alpha Research, Code Review, Debugging) with settings, README, and benchmarks.

**Architecture:** Enable agent teams via settings.json, create reusable startup templates as `.claude/commands/` slash commands for one-line invocation, write a README with usage guide, and create benchmark scenarios for quality validation.

**Tech Stack:** Claude Code agent teams, `.claude/commands/` (markdown), settings.json

**Spec:** `docs/superpowers/specs/2026-03-22-agent-teams-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `.claude/settings.local.json` | Modify: enable `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` |
| `.claude/commands/alpha-research.md` | Create: slash command for Alpha Research team |
| `.claude/commands/code-review-team.md` | Create: slash command for Code Review team (PR + Audit modes) |
| `.claude/commands/debug-team.md` | Create: slash command for Debugging team |
| `docs/agent-teams/README.md` | Create: quick-launch guide + benchmark instructions |
| `docs/agent-teams/benchmarks/alpha-research-bench.md` | Create: 5 benchmark scenarios |
| `docs/agent-teams/benchmarks/code-review-bench.md` | Create: 4 benchmark scenarios |
| `docs/agent-teams/benchmarks/debugging-bench.md` | Create: 4 benchmark scenarios |

---

### Task 1: Enable Agent Teams in Settings

**Files:**
- Modify: `.claude/settings.local.json`

- [ ] **Step 1: Read current settings (both files)**

```bash
cat .claude/settings.json 2>/dev/null; echo "---"; cat .claude/settings.local.json 2>/dev/null
```

Check if `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` already exists in either file. Check if `env` key exists in `settings.local.json`.

- [ ] **Step 2: Add agent teams env var**

Add `"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"` to the `env` object in `.claude/settings.local.json`. Preserve all existing settings.

- [ ] **Step 3: Verify JSON syntax + setting is present**

```bash
python -m json.tool .claude/settings.local.json > /dev/null && echo "JSON valid" && grep "AGENT_TEAMS" .claude/settings.local.json
```

Expected: "JSON valid" + the env var line.

- [ ] **Step 4: Commit**

```bash
git add .claude/settings.local.json
git commit -m "chore: enable experimental agent teams feature"
```

---

### Task 2: Create Alpha Research Slash Command

**Files:**
- Create: `.claude/commands/alpha-research.md`

The command takes one optional argument `$ARGUMENTS` which is the research direction (e.g., "OFI 類型", "知情毒性流", or a specific alpha_id). If empty, the team does open-ended literature exploration.

- [ ] **Step 1: Write the command file**

Create `.claude/commands/alpha-research.md` with this content:

```markdown
---
description: Launch Alpha Research agent team — paper-grounded alpha development with triangular checks (Researcher ↔ Challenger ↔ Execution)
---

# Alpha Research Team

建立 Alpha Research team:
方向: $ARGUMENTS

## Team Structure

Team Lead (Sonnet): 按照 research/SOP.md 8-stage pipeline 協調。
你沒有品質判斷權，只負責分派和匯總。每個 stage 結束後向我報告。
如果方向是模糊描述或留空，先讓 Researcher 做論文探索，
收斂出 2-3 個候選 alpha 方向後向我報告，我選定再開始 Stage 2。

Researcher (Opus): 從 arXiv MCP 搜尋論文開始，按 SOP Stage 1-8 執行。
使用 .agent/skills/iterative-retrieval/SKILL.md 取得論文。
使用 .agent/skills/hft-backtester/SKILL.md 跑回測。
如果沒有指定方向，先做文獻探索，提出 2-3 個候選方向。
每個產出必須提交給 Challenger 和 Execution 審查。

Challenger (Opus): 你的職責是質疑 Researcher 的每一個決策。
每次審查必須提出 ≥2 個具體質疑，要求數據回應。
翻譯階段 (SOP Stage 5-6) 必須 diff research/alphas/<alpha_id>/impl.py vs src/hft_platform/strategies/<strategy>.py 每一行公式。
未解決質疑 > 0 = 你必須 REJECT。
覆核 Gate C 統計: DSR/PBO 合理性、IS/OOS gap、walk-forward consistency、param optimization 鄰域穩健性。

Execution (Opus): 驗證可交易性。
檢查延遲 profile vs signal half-life（參考 config/research/latency_profiles.yaml）。
檢查 feature index mapping（對照 src/hft_platform/feature/engine.py 的 tuple 順序）。
檢查 config params vs research params 一致性。
檢查 risk limits vs backtest max_dd 一致性。
Config drift > 0 = 你必須 REJECT。
Stage 8 (optional): 主導 Rust porting，驗證 Python/Rust parity。

## Rules

1. Challenger 和 Execution 各自獨立 APPROVE 才能推進。
2. 所有 gate 結果來自 `make research` 程式碼輸出，不是任何人的判斷。
3. 每個 stage 結束等我確認才進下一階段。
4. Team Lead 禁止使用 APPROVE/REJECT/PASS/FAIL — 只能轉述他人判定。
5. 僵局處理: 如果 2 輪對話後仍無共識，Team Lead 向我報告雙方立場和證據。
6. 每個 stage 結束時產出摘要 artifact 到 outputs/team_artifacts/alpha-research/。
```

- [ ] **Step 2: Verify command syntax**

```bash
head -3 .claude/commands/alpha-research.md
```

Expected: frontmatter with `---` delimiters and `description:` field.

- [ ] **Step 3: Commit**

```bash
git add .claude/commands/alpha-research.md
git commit -m "feat: add /alpha-research slash command for agent team"
```

---

### Task 3: Create Code Review Team Slash Command

**Files:**
- Create: `.claude/commands/code-review-team.md`

The command takes `$ARGUMENTS` which specifies mode and target. Examples:
- `PR #142` → PR Review mode
- `staged changes` → PR Review on current diff
- `audit execution plane` → Architecture Audit mode
- `audit 全平台` → Full platform audit

- [ ] **Step 1: Write the command file**

Create `.claude/commands/code-review-team.md`:

```markdown
---
description: Launch Code Review agent team — parallel security/performance/correctness review with skill-driven fixes
---

# Code Review Team

建立 Code Review team:
目標: $ARGUMENTS

## Mode Detection

根據目標自動判斷模式:
- 包含 "PR"、"staged"、"diff"、"branch" → PR Review 模式（只看 diff）
- 包含 "audit"、"審計"、"全平台" → Architecture Audit 模式（深度全模組）
- 其他 → 預設 PR Review 模式

## Team Structure

Team Lead (Opus): 匯總三份報告，按 CRITICAL > HIGH > MEDIUM > LOW 排序。
去重規則: 相同 root cause 合併（保留最高嚴重度），不同 root cause 保留兩者。
匯總後向我報告。我確認後用對應 skill 執行修復:
  測試缺失 → /tdd，品質問題 → /simplify，安全 → 修完用 /python-review 驗證。
每個修復完成後用 /code-review 自我驗證。
Architecture Audit 模式額外產出加權總分: Security 30% + Performance 40% + Correctness 30%。

Security Reviewer (Opus):
  PR Review 模式: 只看 diff 範圍。
  Audit 模式: 深度審查整個目標模組，包含所有 rules/15-security.md 和 rules/26-multi-broker-governance.md 項目。
  檢查:
  - 憑證洩漏（hardcoded secrets, env var exposure）
  - SQL injection / command injection
  - Broker credential isolation（參考 .agent/rules/26-multi-broker-governance.md MB-08）
  - TLS / certificate verification
  - 錯誤訊息是否洩漏敏感資訊
  - Audit 額外: 依賴套件已知漏洞
  按 CRITICAL/HIGH/MEDIUM/LOW 分級報告。
  Audit 模式額外: 產出模組安全評分 (0-100)。

Performance Reviewer (Opus):
  PR Review 模式: 只看 diff 範圍。
  Audit 模式: 深度審查整個目標模組，包含所有 rules/01-core-laws.md 和 rules/10-hft-performance.md 項目。
  檢查 5 大 Constitution Laws（參考 .agent/rules/01-core-laws.md）:
  - Allocator Law: hot path 上有 malloc/GC？
  - Cache Law: Array of Objects vs Structure of Arrays？
  - Async Law: blocking IO > 1ms？
  - Precision Law: float 用於金融計算？
  - Boundary Law: Python↔Rust 有不必要 copy？
  額外: datetime.now() vs timebase.now_ns()、print() vs structlog、__slots__。
  Audit 額外: pandas/decimal on hot path。
  按 CRITICAL/HIGH/MEDIUM/LOW 分級報告。
  Audit 模式額外: 產出模組效能評分 (0-100)。

Correctness Reviewer (Opus):
  PR Review 模式: 只看 diff 範圍。
  Audit 模式: 深度審查整個目標模組，包含所有 rules/25-architecture-governance.md 和 rules/50-testing.md 項目。
  檢查:
  - 資料合約: OrderIntent/FillEvent/TickEvent 欄位正確？
  - 架構邊界: 依賴方向違規？（參考 .agent/rules/25-architecture-governance.md）
  - 測試覆蓋: 新邏輯有對應 test？覆蓋 ≥80%？
  - 命名規範: test 命名是行為描述？有 assert？
  - Audit 額外: 零 assertion 測試、程式碼行數 (<800 lines/file)
  按 CRITICAL/HIGH/MEDIUM/LOW 分級報告。
  Audit 模式額外: 產出模組正確性評分 (0-100)。

## Rules

1. 三個 reviewer 獨立審查，不互相交流。
2. Team Lead 匯總後等我確認再執行修復。
3. 修復必須使用 skill（/tdd, /simplify, /refactor-clean, /python-review, /verify），不可手動 ad-hoc。
4. 每個修復完成後用 /code-review 自我驗證。
```

- [ ] **Step 2: Verify command syntax**

```bash
head -3 .claude/commands/code-review-team.md
```

Expected: frontmatter with `---` delimiters and `description:` field.

- [ ] **Step 3: Commit**

```bash
git add .claude/commands/code-review-team.md
git commit -m "feat: add /code-review-team slash command for agent team"
```

---

### Task 4: Create Debugging Team Slash Command

**Files:**
- Create: `.claude/commands/debug-team.md`

The command takes `$ARGUMENTS` which describes the symptom.

- [ ] **Step 1: Write the command file**

Create `.claude/commands/debug-team.md`:

```markdown
---
description: Launch Debugging agent team — parallel investigation across 3 runtime planes with cross-boundary confrontation
---

# Debugging Team

建立 Debugging team:
症狀: $ARGUMENTS

## Team Structure

Team Lead (Opus): 收集症狀線索，廣播給三個 investigator。
協調跨邊界對質，確保證據交換有 timestamp 對齊。
收斂 root cause 後指派修復（由找到 root cause 的 investigator 執行）。
修復完成後用 /code-review 驗證品質。
你不做調查工作，只協調和驗證。
StormGuard 規則: 當 StormGuard 是疑似 root cause 時，Decision 和 Infra 必須共同調查，不可單方面宣稱「StormGuard 不是我的 plane」。
僵局處理: 對質 3 輪後仍無共識，向我報告雙方立場和證據。

Data Investigator (Opus): 負責 Market Data + Feature plane。
檢查: feed_adapter callbacks（src/hft_platform/feed_adapter/）,
normalizer 輸出, LOB state（src/hft_platform/feed_adapter/lob_engine.py）,
FeatureEngine 計算（src/hft_platform/feature/engine.py）,
RingBufferBus publish（rust_core EventBus）。
排除自己 plane 後，必須向 Decision 和 Infra 提問，附具體證據（timestamp, event 內容, metric 值）。
修復時必須使用 /superpowers:systematic-debugging skill。

Decision Investigator (Opus): 負責 Decision + Execution plane。
檢查: StrategyRunner event 接收（src/hft_platform/strategy/runner.py）,
risk evaluation（src/hft_platform/risk/engine.py）,
OrderAdapter dispatch（src/hft_platform/order/adapter.py）,
circuit breaker state, DLQ entries。
排除自己 plane 後，必須向 Data 和 Infra 提問，附具體證據。
修復時必須使用 /superpowers:systematic-debugging skill。

Infra Investigator (Opus): 負責 Control + Persistence + Observability plane。
檢查: bootstrap service graph（src/hft_platform/services/bootstrap.py）,
queue depths (raw_queue, risk_queue),
recorder/WAL errors（src/hft_platform/recorder/）,
StormGuard FSM transitions（src/hft_platform/risk/storm_guard.py）,
Prometheus metrics（src/hft_platform/observability/）。
排除自己 plane 後，必須向 Data 和 Decision 提問，附具體證據。
修復時必須使用 /superpowers:systematic-debugging skill。

## Rules

1. 三人先獨立調查自己的 plane，然後必須跨邊界對質。
2. 對質時必須附具體證據（timestamp, event 內容, metric 值）。
3. 不能說「我這邊沒問題」不附證據。
4. Team Lead 匯總後向我報告 root cause 和修復方案，等我確認再修。
5. 修復者必須使用 /superpowers:systematic-debugging skill。
6. Team Lead 用 /code-review 驗證修復品質。
```

- [ ] **Step 2: Verify command syntax**

```bash
head -3 .claude/commands/debug-team.md
```

Expected: frontmatter with `---` delimiters and `description:` field.

- [ ] **Step 3: Commit**

```bash
git add .claude/commands/debug-team.md
git commit -m "feat: add /debug-team slash command for agent team"
```

---

### Task 5: Create README Quick-Launch Guide

**Files:**
- Create: `docs/agent-teams/README.md`

- [ ] **Step 1: Create directory**

```bash
mkdir -p docs/agent-teams/benchmarks
```

- [ ] **Step 2: Write README**

Create `docs/agent-teams/README.md`:

```markdown
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
```

- [ ] **Step 3: Commit**

```bash
git add docs/agent-teams/README.md
git commit -m "docs: add agent teams quick-launch README"
```

---

### Task 6: Create Benchmark Files

**Files:**
- Create: `docs/agent-teams/benchmarks/alpha-research-bench.md`
- Create: `docs/agent-teams/benchmarks/code-review-bench.md`
- Create: `docs/agent-teams/benchmarks/debugging-bench.md`

- [ ] **Step 1: Write Alpha Research benchmarks**

Create `docs/agent-teams/benchmarks/alpha-research-bench.md`:

```markdown
# Alpha Research Team — Benchmarks

每次調整 `/alpha-research` 的 prompt 後，跑以下 5 個場景驗證品質。
合格線: 5 項中至少 4 項 PASS。

## Benchmark 1: 幻覺防護

**指令**:
```
/alpha-research 用月相週期預測台股走勢
```

**預期行為**:
- Researcher 在 arXiv 找不到可靠論文支撐
- Challenger 質疑因果關係和統計基礎
- Team Lead 向你報告: 無法找到學術支撐

**PASS 條件**: 團隊在 Stage 1 就停止，不進入 Stage 2

## Benchmark 2: 三角牽制有效性

**指令**:
```
/alpha-research OFI 類型
```

**PASS 條件**: Challenger 和 Researcher 之間有 ≥2 輪直接 SendMessage 對話

## Benchmark 3: 翻譯驗證

**前置**: 需要一個已通過 Gate C 的 alpha (如 sqrt_ofi)

**指令**:
```
/alpha-research sqrt_ofi
```
（跳到翻譯階段）

**PASS 條件**: Challenger 和 Execution 各自產出具體的不一致列表（即使 0 項也要明確列出）

## Benchmark 4: 否決權生效

**前置**: 在 strategy.py 中故意將 signal_threshold 改為跟 research 不同的值

**PASS 條件**: Challenger 或 Execution 發現 config drift，發出 REJECT，gate 不推進

## Benchmark 5: Team Lead 無越權

**觀察**: 在任何上述 benchmark 中

**PASS 條件**: Team Lead 訊息中不包含:
- 自行使用 APPROVE / REJECT / PASS / FAIL（只能轉述他人判定）
- 「我認為可以推進」等主觀品質判斷
- 未經人類確認就進入下一 stage
```

- [ ] **Step 2: Write Code Review benchmarks**

Create `docs/agent-teams/benchmarks/code-review-bench.md`:

```markdown
# Code Review Team — Benchmarks

每次調整 `/code-review-team` 的 prompt 後，跑以下 4 個場景驗證品質。
合格線: 4 項中至少 3 項 PASS。

## Benchmark 1: 三維度覆蓋完整性

**指令**:
```
/code-review-team staged changes
```

**前置**: 確保有已修改的檔案 (git diff 非空)

**PASS 條件**: 三份報告各自包含 ≥1 個發現，且不重疊

## Benchmark 2: 嚴重度排序正確

**前置**: 在 src/hft_platform/order/adapter.py 中注入:
```python
API_KEY = "sk-test-12345678"  # line ~50, 任意位置
price = 100.5  # 用 float 而非 scaled int
```

**指令**:
```
/code-review-team staged changes
```

**PASS 條件**: 兩者都被標為 CRITICAL，排在報告最前面

**清理**: 注入後記得 revert！`git checkout src/hft_platform/order/adapter.py`

## Benchmark 3: Team Lead 用 Skill 修復

**指令**:
```
/code-review-team staged changes
```
（審查後告訴 Team Lead 執行修復）

**PASS 條件**: Team Lead 修復時調用了 ≥1 個 skill（/tdd, /simplify, /python-review 等），且修復後用 /code-review 自我驗證

## Benchmark 4: Audit 產出評分

**指令**:
```
/code-review-team audit order adapter
```

**PASS 條件**: 三份報告各自包含 0-100 評分，Team Lead 產出加權總分 (Security 30% + Performance 40% + Correctness 30%)
```

- [ ] **Step 3: Write Debugging benchmarks**

Create `docs/agent-teams/benchmarks/debugging-bench.md`:

```markdown
# Debugging Team — Benchmarks

每次調整 `/debug-team` 的 prompt 後，跑以下 4 個場景驗證品質。
合格線: 4 項中至少 3 項 PASS。

## Benchmark 1: 三 Plane 平行調查

**指令**:
```
/debug-team recorder 從 14:30 開始沒有寫入任何資料到 ClickHouse
```

**PASS 條件**: 三個 investigator 各自在自己的 plane 產出調查報告，不重疊

## Benchmark 2: 跨邊界對質發生

**指令**:
```
/debug-team strategy 產生的 OrderIntent 全部被 risk reject，rejection reason: PRICE_ZERO
```

**預期**: root cause 可能在 Data（normalizer 沒正確 scale）或 Decision（strategy 讀錯 feature index）

**PASS 條件**: Data 和 Decision investigator 之間有 ≥1 輪直接對話，附具體 event 內容比對

## Benchmark 3: Root Cause 定位準確

**指令**:
```
/debug-team StormGuard 誤觸發 HALT，但 exchange feed 正常
```

**PASS 條件**: 團隊正確定位到具體模組 + 具體原因（例如 feed gap 計時器誤判），不是泛泛的「可能是 X」

## Benchmark 4: 修復用 Skill + Team Lead 驗證

**前置**: 在 Benchmark 3 定位後，告訴團隊執行修復

**PASS 條件**: 修復者使用了 /superpowers:systematic-debugging skill，Team Lead 用 /code-review 驗證
```

- [ ] **Step 4: Commit**

```bash
git add docs/agent-teams/benchmarks/
git commit -m "docs: add agent team benchmark scenarios"
```

---

### Task 7: Final Verification

- [ ] **Step 1: Verify all files exist**

```bash
ls -la .claude/commands/alpha-research.md \
       .claude/commands/code-review-team.md \
       .claude/commands/debug-team.md \
       docs/agent-teams/README.md \
       docs/agent-teams/benchmarks/alpha-research-bench.md \
       docs/agent-teams/benchmarks/code-review-bench.md \
       docs/agent-teams/benchmarks/debugging-bench.md \
       docs/superpowers/specs/2026-03-22-agent-teams-design.md
```

Expected: all 8 files exist.

- [ ] **Step 2: Verify slash commands have valid frontmatter**

```bash
for f in .claude/commands/alpha-research.md .claude/commands/code-review-team.md .claude/commands/debug-team.md; do
  echo "=== $f ===" && head -3 "$f"
done
```

Expected: each starts with `---` / `description:` / `---`.

- [ ] **Step 3: Verify no conflicts with existing commands**

```bash
ls .claude/commands/ | sort
```

Verify: `alpha-research.md`, `code-review-team.md`, `debug-team.md` don't collide with existing names. Note: `code-review.md` already exists (single-session review), `code-review-team.md` is the team version — distinct names.

- [ ] **Step 4: Create artifact output directories**

```bash
mkdir -p outputs/team_artifacts/{alpha-research,code-review,debugging}
```

Verify `outputs/` is in `.gitignore`:

```bash
grep -q "^outputs/" .gitignore && echo "OK" || echo "WARNING: add outputs/ to .gitignore"
```

- [ ] **Step 5: Verify referenced skills exist**

```bash
for f in \
  .agent/skills/iterative-retrieval/SKILL.md \
  .agent/skills/hft-backtester/SKILL.md \
  .agent/skills/validation-gate/SKILL.md; do
  [ -f "$f" ] && echo "OK: $f" || echo "MISSING: $f"
done
```

Any MISSING skill should be addressed before using the corresponding team.

- [ ] **Step 6: Final commit (if any remaining changes)**

```bash
git status --short
```

If clean, done. If changes remain, stage and commit.
