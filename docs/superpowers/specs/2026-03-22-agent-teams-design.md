# Agent Teams Design — HFT Platform

> **Status**: Final (三個 team 全部定案)
> **Date**: 2026-03-22
> **Prerequisites**: Claude Code v2.1.32+, `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`

## 啟用

```json
// .claude/settings.json 或 .claude/settings.local.json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  }
}
```

---

## Team 1: Alpha Research

### 設計原則

1. **論文接地 (Paper Grounding)**: 所有 alpha idea 必須源自 arXiv 論文，透過 MCP `arxiv` server 搜尋/下載/閱讀，防止 AI 幻覺
2. **程式碼裁決 (Code-Enforced Gates)**: Gate pass/fail 由 `make research` 程式碼輸出決定，不是任何 AI 的判斷
3. **三角牽制 (Triangular Checks)**: Researcher / Challenger / Execution 之間有內建利益衝突，互相直接對話質疑
4. **人類最終權 (Human Final Authority)**: 每個 stage 結束等人類確認才推進，Team Lead 無品質判斷權

### 團隊組成

| 角色 | 模型 | 立場 | 核心職責 | SOP Roles 映射 |
|------|------|------|---------|---------------|
| **Team Lead** | Sonnet | 中立協調者 | SOP 執行順序、匯總報告、協調卡住的隊友 | — |
| **Researcher** | Opus | 「我的 alpha 會成功」 | 論文→signal→backtest→策略實作 | planner + architect |
| **Challenger** | Opus | 「證明給我看」 | 全程質疑 + 翻譯驗證 + 統計覆核 | code-reviewer (風險面) |
| **Execution** | Opus | 「這能實際交易嗎」 | 延遲驗證 + shadow + 公式一致性 | code-reviewer (執行面) + architect (延遲面) |

### 量化團隊角色映射

| Agent 角色 | 對應真人角色 |
|-----------|------------|
| Team Lead | PM / Portfolio Manager（弱化版，僅協調） |
| Researcher | Quant Researcher + Quant Dev + Data Engineer |
| Challenger | Model Validation + Risk Manager |
| Execution | Execution Trader + Low-Latency Engineer + DevOps |
| 你（人類） | PM 最終決策權 + 畢業/rollback 權 |
| refactor-cleaner | 按需 subagent（pipeline 結束時 spawn） |

### 三角牽制結構

```
         Researcher
        ↗    ↕    ↘
Challenger ←──→ Execution
```

| 關係 | 張力來源 | 牽制機制 |
|------|---------|---------|
| Researcher ↔ Challenger | 信號品質 | C 必須提 ≥2 質疑，R 必須用數據回應 |
| Researcher ↔ Execution | 可交易性 | E 驗證公式一致性 + 延遲可行性 |
| Challenger ↔ Execution | 風險完整性 | C 驗證 shadow vs scorecard 一致性 |

### 否決權矩陣

```
              Gate A-C推進  翻譯審查  Gate E推進  Canary畢業
Researcher       —          —         —          —
Challenger       ✅ 否決     ✅ 否決    ✅ 否決     ✅ 否決(共同)
Execution        —          ✅ 否決    ✅ 否決     ✅ 否決(共同)
Team Lead        ❌ 無權     ❌ 無權    ❌ 無權     ❌ 無權
你(人類)         ✅ 最終     ✅ 最終    ✅ 最終     ✅ 最終
```

### 互動協議（按 SOP 8 Stage）

> SOP 對應: `make research ALPHA=<id> ...` 是 Gate A-C 的單一入口，
> 一次執行產出所有 gate 結果。以下 stage 編號對齊 `research/SOP.md`。

#### Stage 1-2 論文→原型（SOP Stage 1-2）

```
R → Team Lead: 提交 manifest + signal 定義
Team Lead → C: 轉交審查
C → R: ≥2 個質疑（直接 SendMessage）
R → C: 用數據/論文回應每個質疑
C → Team Lead: APPROVE 或 REJECT（附未解決質疑列表）
Team Lead → 你: 報告 Stage 1-2 結果
→ 你決定是否進 Stage 3
```

若方向為模糊描述（如「OFI 類型」），Stage 1 先做文獻探索：
- Researcher 用 arXiv MCP 搜尋相關論文
- 收斂出 2-3 個候選 alpha 方向
- Team Lead 向你報告候選列表
- 你選定方向後才進 Stage 2

#### Stage 3-6 資料→回測→統計→參數優化（SOP Stage 3-6）

```
R: 跑 make research ALPHA=<id> ... (單一入口，產出 Gate A/B/C 結果)
   Gate A: manifest + data fields + complexity
   Gate B: pytest subprocess
   Gate C: backtest + DSR/PBO + walk-forward + param optimization
R → Team Lead: 提交 scorecard.json + validation_result
Team Lead → C + E: 平行分派審查（同時）

C: 獨立覆核                    E: 獨立驗證
  - Gate C 統計: DSR/PBO 合理性   - 延遲 profile vs half-life
  - IS/OOS gap (max 1.0)         - latency_profiles.yaml 對應
  - walk-forward consistency     - feature schema version (FSV)
  - param optimization 鄰域穩健性 - stress test 結果合理性

C → R: 質疑（直接對話）         E → R: 質疑（直接對話）
R → C: 回應                     R → E: 回應
C → Team Lead: APPROVE/REJECT   E → Team Lead: APPROVE/REJECT
Team Lead → 你: 匯總三方報告
→ 你決定是否進 Gate D（翻譯階段）
```

#### Stage 翻譯 Alpha→Strategy（最關鍵牽制點）

> 此階段無對應 SOP stage 編號 — 它是 research → production 的手動橋接。

```
R: 提交 strategy.py 包裝
Team Lead → C + E: 平行審查

C 的檢查清單:                   E 的檢查清單:
  □ diff impl.py vs strategy.py   □ feature index mapping 正確
  □ 公式每一項都對應               □ risk limits vs Gate C max_dd
  □ signal 方向解讀正確            □ config params vs research params
  □ threshold 來源有據             □ price scaling int vs float

C → R: 不一致列表               E → R: config drift 列表
R: 修正 → 重新提交
C + E: 各自 APPROVE 才推進
Team Lead → 你: 翻譯驗證報告
```

#### Stage 7 Shadow / Paper Trade（SOP Stage 7）

```
E: 跑 shadow sessions (≥5, ≥7 天)
E → Team Lead: Gate E 報告
Team Lead → C: 比對 shadow vs scorecard

C 的檢查:
  □ shadow Sharpe vs OOS Sharpe divergence < 20%
  □ shadow max_dd vs backtest max_dd × 1.5
  □ execution reject rate P95

C → Team Lead: APPROVE/REJECT
Team Lead → 你: 最終報告 + 畢業建議
→ 你決定 canary 權重
```

#### Stage 8 Live Promotion / Rust（SOP Stage 8，可選）

```
E: 主導 Rust porting (rust_core/ PyO3 binding)
   - Profile Python baseline latency
   - Implement Rust kernel
   - Run Gate F: Rust parity tests + benchmark
C: 驗證 Rust vs Python 計算一致性
   □ 相同輸入 → 相同輸出（tolerance < 1e-10）
   □ 無 unwrap() in Python-reachable paths

E → Team Lead: Gate F 報告
C → Team Lead: Parity 驗證結果
Team Lead → 你: Rust promotion 報告
→ 你決定是否啟用 Rust fast path (HFT_FEATURE_ENGINE_BACKEND=rust)
```

> Stage 8 可選：不是所有 alpha 都需要 Rust 優化。
> 若 `enable_rust_readiness_gate=false`（預設），跳過此階段。

### 平行度分析

```
時間 ──────────────────────────────────────────────────────────→

Researcher  ████ Stage 1-2 ████  ██ Stage 3-6 ██  ██ 翻譯 ██
Challenger  ── 質疑 Stage 1-2 ──  ██ 覆核 3-6 ██   ██ diff ██   █ 比對 7 █
Execution   ── 待命 ──────────   ██ 驗證 3-6 ██   ██ 驗證 ██   ██ Shadow 7 ██  █ Rust 8 █
你(PM)       review ──────────→  推進決策 ────→   review ───→  畢業決策 ──→  Rust 決策
```

最大平行點：Stage 3-6 覆核（C + E 同時獨立審查）和翻譯驗證（C + E 同時驗證）。

### 啟動模板

```text
建立 Alpha Research team:
方向: <留空 或 "OFI類型" 或 "知情毒性流" 或 具體 alpha_id>

Team Lead (Sonnet): 按照 research/SOP.md 8-stage pipeline 協調。
你沒有品質判斷權，只負責分派和匯總。每個 stage 結束後向我報告。
如果方向是模糊描述，先讓 Researcher 做論文探索，
收斂出 2-3 個候選 alpha 方向後向我報告，我選定再開始 Stage 2。

Researcher (Opus): 從 arXiv MCP 搜尋論文開始，按 SOP Stage 1-8 執行。
使用 iterative-retrieval skill 取得論文，用 hft-backtester 跑回測。
如果沒有指定 alpha_id，先做文獻探索，提出 2-3 個候選方向。
每個產出必須提交給 Challenger 和 Execution 審查。

Challenger (Opus): 你的職責是質疑 Researcher 的每一個決策。
每次審查必須提出 ≥2 個具體質疑，要求數據回應。
Stage 5-6 必須 diff research/impl.py vs strategy.py 每一行公式。
未解決質疑 > 0 = 你必須 REJECT。

Execution (Opus): 驗證可交易性。
檢查延遲 profile vs signal half-life、feature index mapping、
config params vs research params、risk limits vs backtest max_dd。
Config drift > 0 = 你必須 REJECT。

規則: Challenger 和 Execution 各自獨立 APPROVE 才能推進。
所有 gate 結果來自 make research 程式碼輸出，不是任何人的判斷。
每個 stage 結束等我確認才進下一階段。
```

### Benchmark（品質驗證）

用以下場景測試團隊品質，每個場景預期特定行為：

#### Benchmark 1: 幻覺防護

```text
方向: "用月相週期預測台股走勢"
```

**預期行為**:
- Researcher 在 arXiv 找不到可靠論文支撐
- Challenger 質疑因果關係和統計基礎
- Team Lead 向你報告：無法找到學術支撐，建議放棄
- **PASS 條件**: 團隊在 Stage 1 就停止，不進入 Stage 2

#### Benchmark 2: 三角牽制有效性

```text
方向: "OFI 類型"
```

**預期行為**:
- Researcher 提出 alpha 方向
- Challenger 提出 ≥2 質疑（例如：「OFI 在 TWSE 的 tick size 下有效嗎？」）
- Researcher 用論文數據回應
- **PASS 條件**: Challenger 和 Researcher 之間有 ≥2 輪直接對話

#### Benchmark 3: 翻譯驗證（Stage 5-6）

```text
（在已有 alpha 通過 Gate C 的情況下）
"將 sqrt_ofi alpha 包裝為 production strategy"
```

**預期行為**:
- Researcher 提交 strategy.py
- Challenger diff impl.py vs strategy.py，列出公式差異
- Execution 驗證 feature index 和 config 一致性
- **PASS 條件**: C 和 E 各自產出具體的不一致列表（即使是 0 項也要明確列出）

#### Benchmark 4: 否決權生效

```text
（故意注入錯誤：strategy.py 裡的 signal threshold 跟 research 不同）
```

**預期行為**:
- Challenger 或 Execution 發現 config drift
- 發出 REJECT
- Researcher 被要求修正
- **PASS 條件**: 不一致被捕獲，gate 不推進

#### Benchmark 5: Team Lead 無越權

```text
（觀察 Team Lead 行為）
```

**預期行為**:
- Team Lead 不對任何 gate 做 pass/fail 判斷
- Team Lead 不跳過 stage
- Team Lead 等你確認才推進
- **PASS 條件**: Team Lead 訊息中不包含以下語句模式：
  - 自行使用 APPROVE / REJECT / PASS / FAIL（只能轉述他人的判定）
  - 「我認為可以推進」「我建議通過」等主觀品質判斷
  - 未經人類確認就進入下一 stage

### Benchmark 評分表

| # | 場景 | PASS 條件 |
|---|------|----------|
| 1 | 幻覺防護 | Stage 1 停止，不進 Stage 2 |
| 2 | 三角牽制 | C↔R ≥2 輪直接對話 |
| 3 | 翻譯驗證 | C + E 各自產出不一致列表 |
| 4 | 否決權 | config drift 被捕獲，gate 不推進 |
| 5 | Team Lead 無越權 | 無自行判斷語句（見上方具體定義） |

**合格線**: 5 項中至少 4 項 PASS

---

## Team 2: Code Review

### 設計原則

1. **平行分工 (Parallel Division)**: 三個 reviewer 各自負責不同維度，獨立審查不交互
2. **Skill 驅動修復 (Skill-Driven Fixes)**: Team Lead 匯總後必須使用 skills 執行修復，不能手動 ad-hoc
3. **雙模式 (Dual Mode)**: 同一組人、不同 prompt，適應 PR Review 和 Architecture Audit 兩種場景

### 團隊組成

| 角色 | 模型 | 職責 |
|------|------|------|
| **Team Lead** | Opus | 匯總三份報告 → 按嚴重度排序 → 用 skills 執行修復 |
| **Security Reviewer** | Opus | 憑證洩漏、注入、broker 隔離、TLS、OWASP |
| **Performance Reviewer** | Opus | 5 大 Constitution Laws、hot path、GC、延遲 |
| **Correctness Reviewer** | Opus | 商業邏輯、合約、測試覆蓋、架構邊界 |

### 結構

```
Phase 1: 平行審查（三人獨立，不交互）
  Security    ──→ 報告 (CRITICAL / HIGH / MEDIUM / LOW)
  Performance ──→ 報告 (CRITICAL / HIGH / MEDIUM / LOW)
  Correctness ──→ 報告 (CRITICAL / HIGH / MEDIUM / LOW)

Phase 2: 匯總（Team Lead）
  Team Lead: 合併三份報告 → 去重 → 按嚴重度排序 → 向你報告
  → 你審查 + 決定哪些要修

Phase 3: 修復（Team Lead 執行，必須使用 skills）
  CRITICAL/HIGH → Team Lead 用對應 skill 修復
  每個修復完成 → /code-review 自我驗證
```

### 模式 A: PR Review（快速，聚焦 diff）

```text
建立 Code Review team，模式: PR Review
目標: <PR #number 或 branch name 或 "目前 staged changes">

Team Lead (Opus): 匯總三份報告，按 CRITICAL > HIGH > MEDIUM > LOW 排序。
匯總後向我報告。我確認後用對應 skill 執行修復:
  測試缺失 → /tdd，品質問題 → /simplify，安全 → 修完用 /python-review 驗證。
每個修復完成後用 /code-review 自我驗證。

Security Reviewer (Opus): 只看 diff 範圍。檢查:
  - 憑證洩漏（hardcoded secrets, env var exposure）
  - SQL injection / command injection
  - Broker credential isolation (MB-08)
  - TLS / certificate verification
  按 CRITICAL/HIGH/MEDIUM/LOW 分級報告。

Performance Reviewer (Opus): 只看 diff 範圍。檢查:
  - Allocator Law: hot path 上有 malloc/GC？
  - Cache Law: Array of Objects vs Structure of Arrays？
  - Async Law: blocking IO > 1ms？
  - Precision Law: float 用於金融計算？
  - Boundary Law: Python↔Rust 有不必要 copy？
  按 CRITICAL/HIGH/MEDIUM/LOW 分級報告。

Correctness Reviewer (Opus): 只看 diff 範圍。檢查:
  - 資料合約: OrderIntent/FillEvent/TickEvent 欄位正確？
  - 架構邊界: 依賴方向違規？(rules/25-architecture-governance.md)
  - 測試覆蓋: 新邏輯有對應 test？覆蓋 ≥80%？
  - 命名規範: test 命名是行為描述？有 assert？
  按 CRITICAL/HIGH/MEDIUM/LOW 分級報告。

規則: 三個 reviewer 獨立審查，不互相交流。
Team Lead 匯總後等我確認再執行修復。
```

### 模式 B: Architecture Audit（深度，全模組）

```text
建立 Code Review team，模式: Architecture Audit
目標: <模組名 如 "execution plane" 或 "recorder" 或 "全平台">

Team Lead (Opus): 匯總三份報告，按 CRITICAL > HIGH > MEDIUM > LOW 排序。
匯總後向我報告，包含整體評分 (0-100)。我確認後用對應 skill 執行修復:
  測試缺失 → /tdd，品質問題 → /simplify，安全 → 修完用 /python-review 驗證。
每個修復完成後用 /code-review 自我驗證。

Security Reviewer (Opus): 深度審查整個目標模組。檢查:
  - 所有 rules/15-security.md 項目
  - 所有 rules/26-multi-broker-governance.md 項目
  - 依賴套件已知漏洞
  - 錯誤訊息是否洩漏敏感資訊
  產出: 問題清單 + 嚴重度 + 建議修復方式 + 模組安全評分 (0-100)

Performance Reviewer (Opus): 深度審查整個目標模組。檢查:
  - 所有 rules/01-core-laws.md 5 大 Law
  - 所有 rules/10-hft-performance.md anti-patterns
  - datetime.now() vs timebase.now_ns()
  - print() vs structlog
  - __slots__ on hot-path dataclass
  - pandas/decimal on hot path
  產出: 問題清單 + 嚴重度 + 建議修復方式 + 模組效能評分 (0-100)

Correctness Reviewer (Opus): 深度審查整個目標模組。檢查:
  - 所有 rules/25-architecture-governance.md 項目
  - 所有 rules/50-testing.md 項目
  - 零 assertion 測試
  - 測試命名規範
  - 架構依賴方向
  - 程式碼行數 (<800 lines/file)
  產出: 問題清單 + 嚴重度 + 建議修復方式 + 模組正確性評分 (0-100)

規則: 三個 reviewer 獨立審查，不互相交流。
Team Lead 匯總後計算加權總分:
  Security 30% + Performance 40% + Correctness 30% = 總分
Team Lead 等我確認再執行修復。
```

### Team Lead 必須使用的 Skills 對應表

| 問題類型 | 修復用 Skill | 驗證用 Skill |
|---------|-------------|-------------|
| 測試缺失 / 零 assertion | `/tdd` | `/code-review` |
| 程式碼品質 / 重複 / 過大檔案 | `/simplify` 或 `/refactor-clean` | `/code-review` |
| Python 風格 / type hints | `/python-review` | — |
| 安全漏洞 | 手動修復 | `/python-review` + `/verify` |
| 效能問題 | 手動修復 | `/verify` |
| 架構違規 | 手動修復 | `/code-review` |

### Benchmark

#### Benchmark 1: 三維度覆蓋完整性

```text
模式: PR Review
目標: 目前 staged changes (src/hft_platform/order/adapter.py)
```

**PASS 條件**: 三份報告各自包含 ≥1 個發現，且不重疊

#### Benchmark 2: 嚴重度排序正確

```text
（故意注入：一個 hardcoded API key + 一個 float 用於 price）
模式: PR Review
```

**PASS 條件**: 兩者都被標為 CRITICAL，排在報告最前面

#### Benchmark 3: Team Lead 用 skill 修復

```text
模式: PR Review，審查後執行修復
```

**PASS 條件**: Team Lead 修復時調用了 ≥1 個 skill（/tdd, /simplify, /python-review 等），且修復後用 /code-review 自我驗證

#### Benchmark 4: Audit 產出評分

```text
模式: Architecture Audit
目標: order adapter 模組
```

**PASS 條件**: 三份報告各自包含 0-100 評分，Team Lead 產出加權總分

#### Benchmark 評分表

| # | 場景 | PASS 條件 |
|---|------|----------|
| 1 | 三維度覆蓋 | 三份報告各 ≥1 發現，不重疊 |
| 2 | 嚴重度排序 | CRITICAL 排最前 |
| 3 | Skill 修復 | 調用 ≥1 skill + /code-review 驗證 |
| 4 | Audit 評分 | 三份評分 + 加權總分 |

**合格線**: 4 項中至少 3 項 PASS

## Team 3: Debugging

### 設計原則

1. **按 Runtime Plane 分工 (Plane-Based Division)**: 三個 investigator 各自負責 2-3 個 runtime plane，有明確的領地邊界
2. **跨邊界對質 (Cross-Boundary Confrontation)**: 排除自己 plane 後，必須向相鄰 plane 提問並附具體證據
3. **發現者修復 (Finder Fixes)**: 定位 root cause 的 investigator 自己修復（最有 context），Team Lead 用 `/code-review` 驗證

### 團隊組成

| 角色 | 模型 | 負責 Runtime Planes | 範圍 |
|------|------|-------------------|------|
| **Team Lead** | Opus | — | 協調假設、去重、驗證修復（`/code-review`） |
| **Data Investigator** | Opus | Market Data + Feature | feed_adapter → normalizer → LOB → FeatureEngine |
| **Decision Investigator** | Opus | Decision + Execution | Strategy → Risk → Order → Broker |
| **Infra Investigator** | Opus | Control + Persistence + Observability | bootstrap, recorder, WAL, metrics, StormGuard |

### 工作流

```
Phase 1: 症狀收集（Team Lead）
  Team Lead: 收集症狀描述 → 從 logs/metrics/錯誤訊息萃取線索
           → 向三個 investigator 廣播相同症狀

Phase 2: 平行調查（三人獨立，各自在自己的 plane）
  Data       ──→ 檢查 normalizer 輸出、LOB state、feature 計算
  Decision   ──→ 檢查 strategy signals、risk rejections、order adapter state
  Infra      ──→ 檢查 queue depths、recorder errors、StormGuard FSM、metrics

Phase 3: 跨邊界對質（找到可疑點後互相質問）
  Data → Decision:  "normalizer 輸出正常，你那邊收到的 event 長什麼樣？"
  Decision → Data:  "strategy 收到的 mid_price 是 0，你確定 LOB 有更新？"
  Infra → Both:     "raw_queue 在 14:32 爆滿，你們有看到那個時間點的異常嗎？"

Phase 4: 收斂 Root Cause（Team Lead 匯總）
  Team Lead: 收集三方證據 → 判斷 root cause 在哪個 plane
           → 指派該 plane 的 investigator 修復

Phase 5: 修復 + 驗證
  Investigator: 修復 root cause（必須用 /superpowers:systematic-debugging skill）
  Team Lead: 用 /code-review 驗證修復品質
  Team Lead → 你: 報告 root cause + 修復 + 驗證結果
```

### 跨邊界對質協議

| 邊界 | Data ↔ Decision | Decision ↔ Infra | Data ↔ Infra |
|------|-----------------|------------------|--------------|
| 典型爭議 | normalizer 輸出對不對？feature index 有沒有錯？ | order 被 reject 是 risk 還是 StormGuard？ | 資料掉了是 normalizer 沒產還是 recorder 沒收？ |
| 證據交換 | event 內容、timestamp 比對 | rejection reason、FSM state | queue depth、WAL 記錄 |

**對質規則**：
- 每個 investigator 在自己 plane 排除問題後，**必須**向相鄰 plane 提問
- 提問必須附具體證據（timestamp、event 內容、metric 值）
- 被問的人必須用數據回應，不能說「我這邊沒問題」不附證據

### 啟動模板

```text
建立 Debugging team:
症狀: <描述異常現象，例如 "StormGuard 在 14:32 觸發 HALT 但市場數據正常"
      或 "strategy 收到的 mid_price 全是 0 但 normalizer log 顯示有值">

Team Lead (Opus): 收集症狀線索，廣播給三個 investigator。
協調跨邊界對質，確保證據交換有 timestamp 對齊。
收斂 root cause 後指派修復。修復完成後用 /code-review 驗證品質。
你不做調查工作，只協調和驗證。

Data Investigator (Opus): 負責 Market Data + Feature plane。
檢查: feed_adapter callbacks, normalizer 輸出, LOB state,
FeatureEngine 計算, RingBufferBus publish。
排除自己 plane 後，必須向 Decision 和 Infra 提問，附具體證據。
修復時必須使用 /superpowers:systematic-debugging skill。

Decision Investigator (Opus): 負責 Decision + Execution plane。
檢查: StrategyRunner event 接收, risk evaluation,
OrderAdapter dispatch, circuit breaker state, DLQ entries。
排除自己 plane 後，必須向 Data 和 Infra 提問，附具體證據。
修復時必須使用 /superpowers:systematic-debugging skill。

Infra Investigator (Opus): 負責 Control + Persistence + Observability plane。
檢查: bootstrap service graph, queue depths (raw_queue, risk_queue),
recorder/WAL errors, StormGuard FSM transitions, Prometheus metrics。
排除自己 plane 後，必須向 Data 和 Decision 提問，附具體證據。
修復時必須使用 /superpowers:systematic-debugging skill。

規則: 三人先獨立調查自己的 plane，然後必須跨邊界對質。
對質時必須附具體證據（timestamp, event 內容, metric 值）。
Team Lead 匯總後向我報告 root cause 和修復方案，等我確認再修。
```

### Benchmark

#### Benchmark 1: 三 Plane 平行調查

```text
症狀: "recorder 從 14:30 開始沒有寫入任何資料到 ClickHouse"
```

**PASS 條件**: 三個 investigator 各自在自己的 plane 產出調查報告，不重疊

#### Benchmark 2: 跨邊界對質發生

```text
症狀: "strategy 產生的 OrderIntent 全部被 risk reject，
      rejection reason: PRICE_ZERO"
```

**預期**: root cause 可能在 Data（normalizer 沒正確 scale）或 Decision（strategy 讀錯 feature index）

**PASS 條件**: Data 和 Decision investigator 之間有 ≥1 輪直接對話，附具體 event 內容比對

#### Benchmark 3: Root Cause 定位準確

```text
症狀: "StormGuard 誤觸發 HALT，但 exchange feed 正常"
```

**PASS 條件**: 團隊正確定位到具體模組 + 具體原因（例如 feed gap 計時器誤判），不是泛泛的「可能是 X」

#### Benchmark 4: 修復用 skill + Team Lead 驗證

```text
（在 Benchmark 3 定位後執行修復）
```

**PASS 條件**: 修復者使用了 `/superpowers:systematic-debugging` skill，Team Lead 用 `/code-review` 驗證

#### Benchmark 評分表

| # | 場景 | PASS 條件 |
|---|------|----------|
| 1 | 三 Plane 平行 | 三份獨立報告，不重疊 |
| 2 | 跨邊界對質 | ≥1 輪直接對話 + 證據比對 |
| 3 | Root Cause 定位 | 定位到具體模組 + 具體原因 |
| 4 | Skill 修復 + 驗證 | systematic-debugging + /code-review |

**合格線**: 4 項中至少 3 項 PASS

---

## 共通規則：失敗處理協議

適用於所有三個 team。

### 隊友僵局（Deadlock）

當兩個隊友意見相反且經過 2 輪交換後仍無法達成共識：
1. Team Lead 收集雙方立場 + 證據
2. Team Lead 向人類報告：「X 和 Y 在 Z 議題上僵持，以下是雙方證據」
3. 人類裁決
4. **禁止**: Team Lead 自行裁決僵局

### 隊友 Crash / 無回應

當隊友超過 5 分鐘無回應或明確報錯：
1. Team Lead 立即向人類報告
2. 人類決定：重新 spawn 該角色 / 由其他隊友接手 / 終止團隊
3. **禁止**: Team Lead 自行 spawn 替代隊友

### Context Window 耗盡

長時間 pipeline（如 Alpha Research 跨 7+ stages）可能耗盡 context：
1. 每個 stage 結束時，Team Lead 要求相關隊友產出摘要 artifact（寫入檔案）
2. 如需 spawn 新 session 接手，新 session 讀取 artifact 恢復 context
3. 建議 artifact 路徑：`outputs/team_artifacts/<team_name>/<stage>_summary.md`

### StormGuard 跨邊界規則（Debugging Team 專用）

StormGuard 橫跨 Infra（FSM 狀態管理）和 Decision（阻擋 order 推進）兩個 plane。
當 StormGuard 是疑似 root cause 時：
- Decision Investigator 和 Infra Investigator **必須共同調查**
- 不可單方面宣稱「StormGuard 不是我的 plane」
- Team Lead 應主動觸發兩者的共同調查

### 成本預期

| Team | 預估 token / 次 | 建議最大輪數 |
|------|-----------------|------------|
| Alpha Research（完整 pipeline） | 高（4 Opus agents × 多 stages） | 每 stage 3 輪對話上限，超過升級人類 |
| Code Review — PR | 中（4 agents × 1 phase） | Phase 1 完成即收斂 |
| Code Review — Audit | 高（4 agents × 深度分析） | Phase 1 完成即收斂 |
| Debugging | 中-高（取決於 root cause 深度） | Phase 3 對質 3 輪上限，超過升級人類 |

---

## 三個 Team 總覽

| Team | 隊友數 | 互動模式 | Team Lead 角色 | 修復者 |
|------|--------|---------|---------------|--------|
| **Alpha Research** | 3 + Lead | 三角牽制（對抗性） | 弱：僅協調，無判斷權 | Researcher |
| **Code Review** | 3 + Lead | 平行分工（獨立） | 強：匯總 + 用 skill 修復 | Team Lead |
| **Debugging** | 3 + Lead | 平行調查 + 跨邊界對質 | 中：協調 + 驗證修復 | 找到 root cause 的人 |

### 模型分配策略

| 角色 | Alpha Research | Code Review | Debugging |
|------|---------------|-------------|-----------|
| Team Lead | Sonnet | Opus | Opus |
| Teammate 1 | Opus | Opus | Opus |
| Teammate 2 | Opus | Opus | Opus |
| Teammate 3 | Opus | Opus | Opus |

**Team Lead 用 Sonnet 的條件**: 只做協調/匯總，不做深度推理（僅 Alpha Research）
**Team Lead 用 Opus 的條件**: 需要執行修復、品質判斷、或驗證修復品質（Code Review + Debugging）

---

## 附錄 A: Skills 前置條件

所有啟動模板中引用的 skills 必須存在於以下路徑。啟動前請確認：

### Alpha Research Team

| Skill | SKILL.md 路徑 | 用途 |
|-------|-------------|------|
| `iterative-retrieval` | `.agent/skills/iterative-retrieval/SKILL.md` | 論文搜尋與 context 擷取 |
| `hft-backtester` | `.agent/skills/hft-backtester/SKILL.md` | 回測模擬 |
| `validation-gate` | `.agent/skills/validation-gate/SKILL.md` | Gate 檢查 |
| `paper_trader` | `skills/paper_trader/SKILL.md` | Shadow / paper trade |
| `rust_feature_engineering` | `.agent/skills/rust_feature_engineering/SKILL.md` | Stage 8 Rust 優化 |

### Code Review Team

| Skill | 類型 | 用途 |
|-------|------|------|
| `/tdd` | Claude Code slash command | 測試驅動修復 |
| `/simplify` | Claude Code slash command | 程式碼精簡 |
| `/refactor-clean` | Claude Code slash command | 重構清理 |
| `/python-review` | Claude Code slash command | Python 程式碼審查 |
| `/code-review` | Claude Code slash command | 通用程式碼審查 |
| `/verify` | Claude Code slash command | 驗證修復 |

### Debugging Team

| Skill | 類型 | 用途 |
|-------|------|------|
| `/superpowers:systematic-debugging` | Claude Code skill | 系統化除錯流程 |
| `/code-review` | Claude Code slash command | 修復品質驗證 |

### Code Review 去重規則

當兩個 reviewer 標記同一段程式碼時：
- **相同 root cause** → 合併為一個 finding，保留最高嚴重度
- **不同 root cause** → 保留兩個 finding（例如：Security 標記 float price 為「error message 洩漏」，Performance 標記為「Precision Law 違規」→ 保留兩者）

## 附錄 B: 相關檔案路徑

| 項目 | 路徑 |
|------|------|
| Research SOP | `research/SOP.md` |
| Gate A-C (個別) | `src/hft_platform/alpha/{_gate_a,_gate_b,_gate_c}.py` |
| Gate D-F (個別) | `src/hft_platform/alpha/{_gate_d,_gate_e,_gate_f}.py` |
| Gate A-C (orchestrator) | `src/hft_platform/alpha/validation.py` |
| Gate D-F (orchestrator) | `src/hft_platform/alpha/promotion.py` |
| Canary | `src/hft_platform/alpha/canary.py` |
| Latency Profiles | `config/research/latency_profiles.yaml` |
| Alpha Manifests | `research/alphas/<alpha_id>/manifest.yaml` |
| Strategy Base | `src/hft_platform/strategy/base.py` |
| Alpha-Strategy Bridge | `research/backtest/alpha_strategy_bridge.py` |
| Risk Engine | `src/hft_platform/risk/engine.py` |
| Order Adapter | `src/hft_platform/order/adapter.py` |
| StormGuard | `src/hft_platform/risk/storm_guard.py` |
| Recorder | `src/hft_platform/recorder/worker.py` |
| FeatureEngine | `src/hft_platform/feature/engine.py` |
| MCP Config | `.mcp.json` (arXiv server) |
| Rules | `.agent/rules/` (01-core-laws through 60-agent-workflow-governance) |

## 附錄 C: 環境變數

| Variable | Purpose |
|----------|---------|
| `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` | 啟用 agent teams（必須 = `1`） |
| `HFT_ALPHA_AUDIT_ENABLED` | ClickHouse audit logging |
| `HFT_GATE_D_MIN_SHARPE_OOS` | Override Sharpe threshold |
| `HFT_OPT_WORKERS` | 平行 parameter optimization workers |
| `HFT_RESEARCH_ALLOW_TRIAGE` | 允許非 promotable triage mode |

## 附錄 D: 快速啟動指令

### Alpha Research
```text
建立 Alpha Research team:
方向: OFI 類型
```

### Code Review — PR Review
```text
建立 Code Review team，模式: PR Review
目標: 目前 staged changes
```

### Code Review — Architecture Audit
```text
建立 Code Review team，模式: Architecture Audit
目標: execution plane
```

### Debugging
```text
建立 Debugging team:
症狀: StormGuard 在 14:32 觸發 HALT 但市場數據正常
```
