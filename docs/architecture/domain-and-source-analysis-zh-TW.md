# HFT Platform 全範圍架構與知識圖譜調查暨重整計畫

日期：2026-07-21（Asia/Taipei）  
正式分析基準：`56061e905d2ec0a9e49daaeaa95155a805af0f4e`  
初始 full-scope base：`075ce080b9aed907838d07fbc7fac636d4f1ddf7`  
promotion 前正式基準：`d71ef88e43dcb7d27a68f95ba8073d8e516c9e6c`  
狀態：**調查與 Stage 0–4 已完成；Stage 5–9 尚未開工。Stage 4 formal promotion：2026-07-23（exact-path approval：2026-07-22）。**

## 執行摘要

整體任務難度：**極高**。

判定依據如下：

- 專案是 money-facing、latency-sensitive HFT 系統，價格、時間、事件契約、風控、委託、broker callback、WAL、migration 與 Rust/PyO3 均屬高風險面。
- 目前 3,874 條 import 關係形成一個跨 30 個模組的強連通元件，並形成一個跨 7 個主要架構層的強連通元件；單純搬目錄無法解除耦合。
- production package 有 22 條直接 import 指向 `research/`；核心層另有對 adapter、feature 與 observability 的反向依賴證據。
- current-HEAD 主圖為 17,076 nodes／34,094 edges，知識量高度偏向 tests 與歷史研究；call graph 採 precision-first 且不完整，不能拿 degree 當 dead-code 證據。
- 主工作樹高度 dirty，正式圖譜是未追蹤、使用者所有內容；因此 promotion 必須以精確 allowlist、byte backup、同 commit provenance 與失敗全量回復執行。
- Shioaji 1.5.6 的 repository pin 已一致，但 repository authority、local environment、old-host runtime evidence 與 live/production 狀態必須保持分層。

本次已完成的決策與操作：

1. 已將 current-HEAD 主圖、domain 圖、domain context、meta 與本文件視為單一 promotion set；五者皆對齊 `56061e90...`。
2. `config.json` 維持原 bytes 與 SHA-256 `c4979382...`，因驗證未顯示需要更動。
3. 主圖補入 incremental provenance；208 個 Shioaji authority-normalized nodes 有 repository-level override 證據，155 個 Fubon source nodes 明確標為 `paused`，未知 owner 保留 `unresolved`。
4. `.dockerignore` 已由 `service:.dockerignore` 全引用遷移為 `config:.dockerignore`；stdlib/local logging collision 由 Stage 2 analyzer guardrail 防止。
5. Domain 圖維持 5 domains／12 flows／68 steps，Fubon 與 CI 都不進 active domain；完整 domain anchor gate 為 68/68。
6. CI、runtime、broker、live/prod、Fubon activation、dependency mutation與 Git mutation仍未執行。
7. Stage 4 的 K0–K7 disposition、logical identity 與 history namespace 已完成並正式 promotion；Stage 5–9 仍是建議計畫。本次仍不授權任何 source refactor、刪除、搬移、合併或 rename。

| 階段 | 現況 | 難度 | 風險 | 複雜度 |
|---|---|---|---|---|
| 0. authority／ownership／rollback freeze | 已完成 | 中 | 高 | M |
| 1. current-HEAD 增量刷新 | 已完成 | 高 | 高 | M |
| 2. analyzer／ontology guardrails | 已完成 | 高 | 高 | L |
| 3. 五項正式 artifact transaction promotion | 已完成 | 高 | 高 | L |
| 4. Orphan／duplicate／history disposition | 已完成 | 高 | 高 | L |
| 5–9. source architecture 重整與持續治理 | 未開工 | 高至極高 | 高至極高 | L–XL |

耐久 rollback/evidence manifest：`local-state:understand-anything/hft-platform/promotions/2026-07-21-56061e905d2e-stage3/promotion-manifest.json`。

## 證據與判讀規約

主要證據：

- `.understand-anything/knowledge-graph.json` 與 `domain-graph.json`：current-HEAD 正式主圖與領域圖。
- `current-head-candidate-provenance.json`：增量候選來源、reuse/fresh 比例、輸入 hash 與限制。
- `current-head-candidate-audit.json` 與 `currenthead-assembled-audit.json`：current-HEAD 完整性、scope、關係與語意 gates。
- `scope-knowledge-audit.json`：掃描範圍、文件分布、版本出現與排除項。
- `semantic-batch-validation.json`：55 個 fresh semantic batches 的完整性。
- `fullscope-candidate-audit.json`：圖譜完整性、孤立節點、分布、重複與正式 delta。
- `fullscope-module-dependency-audit.json`：module/layer dependency、SCC 與 boundary checks。
- `fullscope-domain-graph-audit.json`：正式與候選 domain 圖比較。
- `domain-anchor-audit.json`：68 個 domain steps 對候選 class/function source range 的追溯。
- `evidence-manifest.json`：所有關鍵 hash、正式 artifact hash、HEAD drift 與未執行事項。

判讀規則：

- 「已確認」只使用 source、git、schema validator 或 deterministic audit 可直接證明的內容。
- 「推論」必須能回指一項以上已確認證據，但不是 source 直接宣告的事實。
- 「假設」需要 owner、runtime、symbol binding、migration effect 或外部原始記錄才能確認。
- candidate 的 `calls` 採 precision-first；它是不完整關係集，不能作為 dead-code 或完整 runtime reachability 證明。
- domain step 的合法錨點可等於 declaration range，也可完全位於同檔 class/function range內；method-level span 不必等於整個 class range。

# 1. 已確認的事實

## 1.1 調查範圍與非操作邊界

- current-HEAD clean snapshot 共掃描 **2,537 files／619,381 lines**，明確排除 778 項。
- top-level files：`tests` 1,171、`research` 427、`src` 401、`docs` 294、`scripts` 91、`config` 82、`rust_core` 50、root 18、`ops` 3。
- 類型：code 1,888、docs 477、config 111、data 28、script 27、infra 6。
- CI path 為 0；`.ci` 2 項與 `.github` 17 項在排除 inventory 中。
- Fubon 有 28 個 source/test/config paths 被掃入，但 disposition 是 paused source evidence only。
- secrets、agent internals、generated outputs、test goldens、research archive/experiments 等有明確排除統計；secret surface 只記數量 4，未讀取或輸出內容。
- 調查與候選階段未執行 HFT tests、CI、broker login、order、live/prod、Fubon runtime、package install、dependency mutation或 Git mutation；Stage 3只覆寫五個已核准的正式知識 artifacts。

證據：`scope-knowledge-audit.json#scope`、`#excludedInventory`；`evidence-manifest.json#operationsNotPerformed`。

## 1.2 Promotion 前正式圖與 current-HEAD 正式圖

| 指標 | Promotion 前正式主圖 | Current-HEAD 正式圖 |
|---|---:|---:|
| source commit | `d71ef88...` | `56061e90...` |
| nodes | 3,367 | 17,076 |
| edges | 8,209 | 34,094 |
| layers | 3 | 10 |
| tour steps | 3 | 12 |
| files in scan | 589（meta） | 2,537 |
| imports | 807 | 3,874 |
| calls | 1,846 | 679（precision-first、不完整） |

current-HEAD node types：class 3,493、function 10,945、file 1,915、document 477、config 112、table 94、service 40。

候選 edge types：contains 14,473、exports 14,362、imports 3,874、calls 679、tested_by 212、related 168、documents 158、depends_on 80、migrates 66、configures 21、routes 1。

候選完整性結果：

- 0 duplicate node IDs、0 duplicate edge triples、0 dangling edges、0 self edges。
- 0 missing disk paths、0 scan/candidate file parity gap、0 invalid line ranges。
- importMap 原始 3,875 條；扣除 1 條 `stdlib logging` 與本地 `utils/logging.py` 名稱碰撞造成的 false self-import 後，應有 3,874，candidate 實際亦為 3,874；missing/unsupported 均為 0。
- 13,138 個 minimum-significant declarations 全部存在；另保留 1,300 個較寬鬆 declarations。4 個 candidate-only declarations 有 template/Fubon facade source supplement 證據。
- 2,638 個 file-level layer entities 全部且只歸屬一層；tour 12 步的 node references 全部有效。
- Fubon nodes 155，但 activation edge 為 0；CI path 為 0。
- Understand Anything `validateGraph` 對主圖回傳 `success:true, issueCount:0`。

current-HEAD 正式候選 SHA-256：`568cc2ec10e401c686abd92cec6a2b856a1617dbed64a3e2b00e87c15260791b`；`435de1c...` 是增量刷新前的 full-scope base hash。

證據：`candidate-provenance.json#counts`、`fullscope-candidate-audit.json#baseline`、`#integrity`。

## 1.3 語意 freshness 與組裝

- semantic base commit 為 `5bad82fd...`。
- 1,996 個未變更檔重用 source-valid summaries；541 個新增或變動檔重新分析。
- fresh work 分成 55 batches，全部載入；得到 1,454 nodes、2,682 edges。
- batch validation 的 missing batch、unexpected part、part gap、oversized part、parse error、node/edge issue、file coverage issue、duplicate 與 import parity failure 全為空。
- batch 25 與 55 曾有 fragment size 超限；已在 scratch 進行 content-preserving repartition。node/edge multiset hash 前後相同，`allContentHashesUnchanged:true`。
- canonical assembly 原有 17,076 nodes／34,037 edges；獨立 review 移除 3 條不在 current importMap 的 stale imports，成為 34,034 edges。
- 移除的三條 stale imports 是：
  - `tests/research/calibration/test_cli.py → tests/research/calibration/__init__.py`
  - `feed_adapter/shioaji/reconnect_orchestrator.py → feed_adapter/shioaji/__init__.py`
  - `feed_adapter/shioaji/subscription_manager.py → feed_adapter/shioaji/__init__.py`

Stage 1 後續只刷新 `docker-compose.yml`、`src/hft_platform/ops/backup.py` 與 `tests/unit/test_ops_backup_coverage.py`：nodes 維持 17,076，edges 由 34,034 增為 34,094；新增 59 條 `calls` 與 1 條 `tested_by`，3,874 條 import identities、30-module SCC、7-layer SCC 與 domain semantic nodes/edges均未改變。

證據：`semantic-batch-validation.json`、`repartition-report.json`、`assemble-review.json`、`candidate-provenance.json#semanticProvenance`。

## 1.4 Primary architecture layers 與知識分布

| Primary layer | File-level entities | 比例（2,638） |
|---|---:|---:|
| tests | 1,167 | 44.2% |
| documentation-governance | 477 | 18.1% |
| research-lab | 223 | 8.5% |
| services-observability-ops | 214 | 8.1% |
| config-infrastructure | 165 | 6.3% |
| data-durability | 126 | 4.8% |
| features-strategies | 99 | 3.8% |
| contracts-core | 71 | 2.7% |
| market-data-adapters | 51 | 1.9% |
| risk-order-execution | 45 | 1.7% |

若計入 class/function，`tests` top-level 共 11,441 nodes，占全部 17,076 nodes 約 67.0%；`src` 2,041（12.0%）、`research` 2,039（11.9%）。因此 node 數量主要反映測試函式密度，不等於 runtime 重要性。

Primary layer 是單一視圖：文件、config、script 目前主要按媒體類型分類，尚未同時表達其 domain owner；Rust 主要被歸入 contracts/core，scripts 主要被歸入 ops，也需要 secondary ownership/lifecycle relations 補足。

證據：`candidate-knowledge-graph.json#layers`、`fullscope-candidate-audit.json#distribution`。

## 1.5 核心流程與資料流

主圖 tour 有 12 個入口：平台邊界；契約/價格/時間；事件匯流與原生核心；Shioaji 1.5.6 行情；特徵到策略意圖；風控；委託與成交；WAL/錄製/回放；服務與營運安全；容器部署邊界；離線研究與 promotion；測試證據。

Domain candidate 有 **5 domains／12 flows／68 steps／87 edges**：

| Domain | Flows |
|---|---|
| 行情資料與合約 | Shioaji 1.5.6 合約更新；行情事件處理；session 重連與重新訂閱 |
| 策略與 Alpha 治理 | 策略事件轉交易意圖；Alpha 驗證與晉升 |
| 風控與委託執行 | 風險意圖准入；委託派送與成交對帳 |
| 記錄與回放 | WAL-first 持久化；決定性交易意圖回放 |
| 營運安全控制 | 受防護的 runtime 啟動；只減倉與重新啟用；健康觀測與降級輸入 |

Domain validation：

- 0 duplicate/dangling/self edges；每個 flow 恰有一個 domain owner，每個 step 恰有一個 flow owner。
- flow weights 合法且單調；source paths/line ranges 全部有效；0 stale entry point。
- 0 Fubon mention、0 CI path；Shioaji authority 為 1.5.6。
- 68 steps 中 9 個與 top-level function range 完全一致；其餘 59 個完全位於同檔 class/function declaration range 內。0 partial-overlap-only、0 unanchored。
- `validateGraph` 回傳 `success:true, issueCount:0`；domain SHA-256 為 `2f0ead0587d9595ca31ff41c70ae4b2b9741a3f7f33f83db76553f15f70c05ec`。

Promotion 前的正式 domain 圖只有 4 domains／12 flows／44 steps。當時 44 steps 全部有 source issue：43 個缺 line range，1 個指向不存在的 `src/hft_platform/feed_adapter/shioaji/execution_callbacks.py`；另有 stale flow entry `src/hft_platform/feed_adapter/quote_connection_pool.py`，且 active text 提到 Fubon 一次。這些問題已由本次 5 domains／12 flows／68 steps 的正式圖關閉。

證據：`fullscope-domain-graph-audit.json`、`domain-anchor-audit.json`。

## 1.6 模組邊界與依賴

- 3,874 條 file imports 中，3,205 條跨 module boundary。
- 一個 module SCC 橫跨 30 個 production/research modules。
- 一個 layer SCC 橫跨 contracts-core、data-durability、features-strategies、market-data-adapters、research-lab、risk-order-execution、services-observability-ops 共 7 層。
- 11 組直接雙向 module pairs；最大的幾組包括 `core ↔ feed_adapter`、`alpha ↔ research/registry`、`alpha ↔ research/backtest`、services/root、feed_adapter/config、risk/observability。
- production package 有 22 條直接 imports 指向 `research/`，集中於 alpha gate/promotion/validation、backtest bridge、CLI 與 monitor dispatcher。
- `src/hft_platform/core/pricing.py:21` 直接 import `feed_adapter.normalizer.SymbolMetadata`。
- `src/hft_platform/engine/event_bus.py:12` 直接 import `observability.metrics.MetricsRegistry`。
- `src/hft_platform/events.py:185` 的事件資料物件方法直接 import `feature.boundary.event_to_typed_frame`。
- broker adapter 直接 import strategy/risk/execution 的數量為 0，這個 boundary 目前沒有已確認的反向依賴。
- module fan-in 前四名：contracts 352、alpha 331、core 316、feed_adapter 213。
- module fan-out：tests 2,415（主要是 verification dependency，不可當 runtime fan-out）；非測試最高是 services 117、research/alphas 109、feed_adapter 57、CLI 51。

證據：`fullscope-module-dependency-audit.json`；上述三個 source import 的實際行號。

## 1.7 孤立與低連結節點

候選共有：

- degree-0 orphans 336（2.0%）。
- degree-1 nodes 402。
- degree-2 nodes 13,895（81.4%）。
- 只有 `contains/exports` 等 structural relations 的 nodes 13,968（81.8%）。
- callable nodes 14,438；有 `calls` 連結者 603（4.2%）。

336 個 orphans 的 Stage 3 deterministic taxonomy：

| 類別 | 數量 | 已確認性質 |
|---|---:|---|
| K0 boundary anchors | 65 | package marker、`.gitkeep` 等邊界節點 |
| K0 test-support anchors | 6 | 測試支援邊界 |
| K1 missing owner/consumer | 33 | 尚無 owner/consumer relation；含已正確分類但仍未連結的 `.dockerignore` |
| K1 unlinked documents | 17 | 一般文件未連結 |
| K1 unlinked infra/data | 2 | 基礎設施或資料 artifact 未連結 |
| K1 unlinked operational docs | 25 | ops/runbook 類未連結 |
| K1 unlinked ops scripts | 11 | 維運 script 未連結 |
| K2 historical/research references | 168 | 歷史或研究證據，不屬 active runtime |
| K2 paused Fubon | 2 | paused package anchors |
| K3 classification error | 0 | Stage 2–3 已關閉 `.dockerignore` classifier defect |
| K6 unlinked code/artifact | 7 | 尚缺關係證據 |

degree-2 與 structural-only 高比例主要由 file→declaration 的 `contains/exports` 形狀造成；這些數字不是 dead-code evidence。

證據：`fullscope-candidate-audit.json#connectivity`、`#confirmedClassificationErrors`。

## 1.8 重複、過時、失效與歷史知識

- logical table names 有 16 組重複、28 個額外 occurrences。例如 `hft.fills` 分布在 7 個 migrations，`hft.orders` 分布在 5 個 migrations。
- logical service names 有 12 組重複、21 個額外 occurrences，主要是同一 service 在 base、production、prod-locked compose 中重複定義。
- 477 份文件中沒有 byte-exact duplicate group；但有 25 組相同 primary heading、6 組重複 curated topic，需要 semantic review，不能直接視為可刪除副本。
- `docs/superpowers` 有 23 組 plan/spec 配對、29 個 unpaired plans、17 個 unpaired specs。
- 對 clean source snapshot 而言，promotion 前正式主圖有 42 個 paths 不存在：34 個 Shioaji vendor/plugin bundle paths、7 個已移除 research legacy scripts、1 個 binary bundle。
- promotion 前正式圖另有 2 個 existing paths 被 candidate scope 排除：`.env.example`（secret-adjacent policy）與一個 generated research data audit artifact。
- promotion 前正式圖→current-HEAD圖 delta：nodes +14,030/-321，edges +28,218/-2,333；共同 nodes 3,046、共同 edges 5,876。
- 初始候選的 `.dockerignore` 曾被 infra parent-node rule 誤分類成 `service:.dockerignore`；Stage 2 已修正 analyzer，Stage 3 已把正式 node identity 與 layer reference 一致遷移成 `config:.dockerignore`。

證據：`fullscope-candidate-audit.json#duplicationAndHistory`、`#freshnessAndPromotion`、`#confirmedClassificationErrors`；`scope-knowledge-audit.json#knowledgeDistribution`。

## 1.9 架構文件狀態

Promotion 前的 `docs/architecture/domain-and-source-analysis-zh-TW.md` 仍以 `d71ef88...` 為基準、只描述 4 domains，並把 Fubon 寫入 active market-data flow；其 78 個 local Markdown references 所指的 47 個 targets 均存在，問題是內容與 anchor format drift，而非整份失效。

Stage 3 以本完整調查暨重整計畫取代該過時內容：正式基準改為 `56061e90...`，明列 5 domains、Fubon paused、CI excluded、事實／推論／假設／方案／阻塞五類資訊。Stage 4 於 2026-07-22 取得 exact-path approval，並於 2026-07-23 完成圖譜治理 promotion；Stage 5–9 仍保留驗證與 rollback 條件。文件只描述 source-backed evidence，不把 test existence、compose、broker code或 promotion decision寫成 runtime/live/production 已驗證。

## 1.10 Shioaji 1.5.6 dependency 與治理

已確認的 repository authority：

- `pyproject.toml:13,62`：`shioaji[speed]==1.5.6`。
- `uv.lock:1209-1210,4093`：specifier 與 locked version 都是 1.5.6。
- `CLAUDE.md:17-22`：pin 1.5.6，且明確警告 repo pin 不等於 deployed runtime。
- local `.venv` 唯讀版本查詢為 1.5.6。

版本字串分布：1.2.9 共 16 次／9 paths；1.3.3 共 45 次／15 paths；1.5.3 共 65 次／13 paths；1.5.5 共 20 次／4 paths；1.5.6 共 52 次／15 paths。舊版本出現在 compatibility、migration、benchmark 或歷史驗證脈絡；不能全域取代。

工作樹中已有 user-owned dirty docs diff：

- `docs/CODEMAPS/dependencies.md` 將 1.2.9 改為 1.5.6 pinned authority，並分離 deployed runtime gate。
- `docs/operations/long-term-risk-register.md` 將「未 pin」修正為 runtime/documentation drift，並分離 repo pin 與 runtime acceptance。

這兩個 diff 與建議方向一致，但尚未經本輪正式 docs review/commit，因此只能稱「工作樹已有對齊內容」，不能稱 repository baseline 已正式完成。

Old-host runbook `docs/runbooks/shioaji-version-diff.md:483-605` 記錄：

- 撤回早先把 1.3.3 rollback image 當作 1.5.6 的結論。
- true-1.5.6 quote gate：4/4 login、296/296 subscriptions、10-minute soak。
- 30-minute SIM soak 與 decimal parity 19,369/19,369。
- 305/305 orders 被 broker reject，因此該 probe latency 無效。
- AuthError compatibility fix、pool=4 最終 5/5 login、296 subscriptions、safe default 改為 SIM。
- day-session full-universe data-flow confirmation 仍欠缺。

Host raw bundle `deploy-staging/shioaji156-third-attempt-20260717T045731Z/` 本機未找到；本輪未 SSH、未登入 broker、未接觸 production。因此 runbook 是次級摘要，不是本輪獨立 raw-evidence 驗證。

證據：`scope-knowledge-audit.json#dependencyAuthority`；上述 source/doc line ranges。

## 1.11 遺失逐字委派封存

`.agent/memory/delegations/2026-07-13-shioaji-156-plan-review.md` 已存在，內容包括：

- 第 5 行：原始 reviewer packet 的逐字內容。
- 第 9-12 行：`NONE DELIVERED`，兩次中斷、無 findings、無檔案編輯。
- 第 16-17 行：FAIL verdict，orchestrator 未把該 delegation 當證據。
- SHA-256：`bd3cc6dcd88b702337bc791f2d4dcffdc424e6ae73ee4622ed5038992ee1ee1c`。
- `.agent/memory/model-routing.md:359` 已建立 ledger link。

但該檔受 `.gitignore:231` 的 `/.agent/*` 規則忽略，`git ls-files` 顯示未追蹤。也就是：**內容 backfill 已完成，耐久化尚未完成**。是否 `git add -f`／commit 是獨立 git authority 決策，本輪未執行。

## 1.12 分支、dirty state 與 promotion baseline

- 固定 HEAD 為 `56061e905d2ec0a9e49daaeaa95155a805af0f4e`；promotion 前 dirty-status digest 為 `621de73bdd3345a5b29dddcf170933621229607e4979fb785b96bad5540efeaf`。
- promotion 前六項正式 artifacts 均為 `charlie:charlie`、mode `0644`、未追蹤且未被 ignore。
- 五個獲核准路徑及未變更的 `config.json` 都已在 repo 外耐久位置建立逐位元備份；任何 post-write gate 失敗都必須全數還原。
- 這次只變更 `.understand-anything/knowledge-graph.json`、`.understand-anything/domain-graph.json`、`.understand-anything/intermediate/domain-context.json`、`.understand-anything/meta.json` 與本文件；`config.json`、source、dependency、CI、部署環境與 Git state 不在 blast radius。
- 是否追蹤 generated artifacts 仍是後續 Git policy 決策；本次只核准 working-tree promotion，沒有 `git add`、commit 或 push。

# 2. 根據證據提出的推論

## 2.1 主要根因

1. **依賴方向只有文字規範，缺少可執行 boundary contract。** 30-module SCC、7-layer SCC、22 條 production→research imports 和三個已確認 inner→outer imports 同時存在，表示目前 package layout 不能可靠限制依賴方向。
2. **Control plane、domain behavior 與 observability 交織。** `services` 非測試 fan-out 最高，event bus 直接依賴 metrics；composition/wiring、健康狀態與業務處理沒有穩定 seam。
3. **Research 被當作 production 可直接使用的 library。** Alpha gates、CLI、monitor 與 backtest bridge 直接 import `research/`，使 offline governance 與 runtime packaging/availability 綁在一起。
4. **知識圖譜把 occurrence、logical identity、媒體類型與 domain ownership 混在同一層。** Migration table 與 compose service 的多次出現是歷史/variant evidence，不應直接合併，也不應在 current dependency 查詢中視為多個獨立 logical entities。
5. **增量語意內容缺少統一 freshness contract。** 舊圖可保留 summaries、calls 與 deleted/vendor paths，而 deterministic scan 已改變；若沒有 sourceCommit/evidenceKind/lastSeen 驗證，active knowledge 會逐次累積 stale edges。
6. **文件採累加式保存，缺 lifecycle 與 canonical pointer。** 25 組同 heading、6 組重複 topics、29/17 個 unpaired plan/spec 並不等於垃圾，但會讓 reader 無法判斷 active、superseded、historical 或 evidence-only。
7. **工作樹的未追蹤圖譜、vendor bundle、歷史 scripts 與 dirty governance docs 混在同一 namespace。** 這使「檔案存在」不等於「repository canonical」，也使 clean-snapshot 結果與當前 workspace observation 看似矛盾。
8. **Shioaji 的真正風險已從 pin 缺失轉為 authority 混淆。** Repo、venv、container、host runbook、raw evidence 與 live state是不同證據層；任一層都不能替代另一層。

## 2.2 影響範圍

- Impact analysis 容易被 SCC 放大，無法快速界定變更 blast radius。
- Runtime package 可能因 research import 或 optional dependency 缺失而受到非交易路徑影響。
- 僅看 degree/calls 會把大量 declarations 誤判成 dead code，造成高風險誤刪。
- Promotion 前的 domain 文件曾把 Fubon 放在 active flow，會誤導後續 Agent 進行已明確暫緩的工作；Stage 3 已從正式 domain 圖移除該 active 表述。
- Promotion 前正式圖中的 stale/vendor paths 曾污染 onboarding、architecture review 與關係查詢；Stage 3 已從 active graph 移除，Stage 4 已將 external reference／history disposition 記錄於獨立 namespace；exact deleted tombstone 仍待 removal commit 證據。
- Compose/migration occurrences 若被直接合併，會失去 profile override 或 schema evolution 的時間序。
- Shioaji 歷史版本若被全域替換，會破壞 compatibility tests 與事故證據；反之若不標 lifecycle，又會讓讀者誤認權威版本不一致。

# 3. 尚待驗證的假設

1. 30-module SCC 的一部分可能由 `__init__.py` re-export、CLI wiring、optional imports 與 tests 放大；需 symbol-level binding 和 entry-point reachability 才能拆成「實際 runtime cycle」與「靜態 packaging cycle」。
2. `src/hft_platform/alpha` 中若干模組可能實質是 offline governance，不是 live runtime；需列出 production entry points 與 deployment import closure 後才能決定移到哪一側。
3. K1/K6 orphan configs、docs、scripts 可能由 shell、manual SOP、scheduler 或外部 host 消費；沒有 owner 訪談或 runtime evidence 前不得刪除。
4. Compose 同名 services 多半是 profile variants，但是否存在 unintended field drift，要做 field-level normalized diff 才能決定 `variant_of` 或 `overrides`。
5. 同名 ClickHouse table occurrences多半是合法 schema evolution；必須建立 migration order、DDL effect 與 materialized-view dependency 後才能判斷是否 stale。
6. 68-step domain candidate 已有 source range 證據，但 flow completeness 仍需 domain owner 對照事件 topic、失敗分支、runbook 和實際 entry points 確認。
7. 81 條 cross-file calls 是否都正確、還缺多少 calls，需要 qualified symbol binder；目前不得外推完整 call graph。
8. Old-host runbook 對 true-1.5.6 的摘要大致一致，但未取得 raw bundle/checksum；day-session full-universe data-flow 也尚未閉合。
9. 目前 dirty docs diff 是否完全屬於使用者預期變更，需逐 hunk review；不能因方向正確就自動採納。

# 4. 建議的重整方案

## 4.1 孤立與低連結節點的正式分類規則

沿用並擴充 K0-K7；所有 disposition 必須同時記錄 `sourceCommit`、`evidenceKind`、`lifecycle`、`ownerStatus` 與 `reviewedAt`。

| 類別 | 判定 | 預設動作 | 禁止動作 |
|---|---|---|---|
| K0 boundary/support anchor | 空 package、test support、`.gitkeep` | 保留 source；圖上摺疊或標 anchor | 因 degree 0 刪除 |
| K1 owner/consumer missing | config/doc/data/script 無 semantic consumer | 建 owner queue；補 `owned_by`/`consumed_by` | 猜測 owner 或直接歸檔 |
| K2 paused/historical | Fubon、版本歷史、研究參考 | 移出 active flow；保留 history view | 全域改版本、刪來源 |
| K3 classification error | source kind 與 node type 不符 | 修 analyzer policy，保留 correction provenance | 只手改單一 formal JSON |
| K4 occurrence/variant | 同 table/service/setting 多次定義 | 建 canonical identity + occurrence edges | 合併／刪 migration 或 compose source |
| K5 stale/tombstone | path 在指定 source commit 不存在 | active view 移除；建 tombstone/history | 把 dirty tree 同名 untracked 檔當 canonical |
| K6 relation deficit | code/artifact 有效但 only structural/low-link | 補 binder、entry point、owner evidence | 當 dead code |
| K7 money-path manual | price/time/event/risk/order/fill/WAL 等低連結 | Tier-3 人工 review | 自動刪除、移動或 rename |

處理優先分數建議：`confirmed error × freshness risk × ownership ambiguity × money-path criticality`。K3/K5 的 active-view 錯誤先處理；K7 永遠不能由分數自動處置。

## 4.2 目標專案架構與模組邊界

目標依賴方向：外層可依賴內層；內層只能依賴 contracts/ports，不依賴具體 adapter、research、control plane 或 observability implementation。

| 邊界 | 責任 | 可依賴 | 不可依賴 |
|---|---|---|---|
| Kernel / Contracts | event DTO、price/time、IDs、immutable contracts、ports | stdlib、純資料套件 | broker、services、research、storage/metrics implementation |
| Broker Ports | quote/order/account/session protocols | Kernel/Contracts | Shioaji/Fubon SDK implementation |
| Broker Adapters | Shioaji 1.5.6 mapping、callback、session、compat | Kernel + Broker Ports + telemetry port | strategy/risk policy、research |
| Market-data Plane | callback queue、normalizer、LOB、feature、publish | Kernel、quote port、telemetry port | order policy、research orchestration |
| Decision/Execution Plane | strategy runner、intent、risk、gateway、order/fill reconciliation | Kernel、broker order port、persistence/telemetry ports | concrete bootstrap、research implementation |
| Durability/Replay | recorder、WAL、ClickHouse adapters、replay | Kernel、storage ports | broker SDK、live control decisions |
| Control Plane | bootstrap、service lifecycle、health/degrade、CLI wiring | 各層公開 ports/adapters | 被 Kernel 或 domain layers 反向 import |
| Research/Governance | candidate、backtest、calibration、registry、promotion evidence | exported production contracts、offline data adapters | production runtime 直接 import implementation |
| Knowledge/Evidence | docs、runbooks、ADRs、tests、graph、provenance | 引用所有層 | 成為 runtime dependency |

第一批 architecture seams 建議依序是：

1. `core/pricing.py`：把 `SymbolMetadata` 建構移出 Kernel，以 constructor/provider injection 供給 `PriceScaleProvider`。
2. `engine/event_bus.py`：以 telemetry/metrics port 或 callback 注入，讓 event bus 不直接依賴 observability implementation。
3. `events.py`：把 `to_typed_frame` 的 feature conversion 移到 feature adapter，或只依賴 Kernel 內 protocol，避免資料 contract 反向依賴 feature layer。
4. `src/hft_platform/alpha` 與 `research/`：先抽 DTO/ports 與 offline adapter，再改 import；目錄搬移最後做。
5. `services` 固定為 composition root；先消除 inner layers 對 services/ops 的 imports，再收斂 bootstrap。

Broker adapter 目前沒有直接依賴 decision/execution，這條有效邊界應建立 import contract 並保持不動。

## 4.3 目標知識圖譜模型

每個 active node 至少需要：`sourceCommit`、`filePath` 或 logical identity、`lifecycle`、`evidenceKind`、`confidence`、`ownerStatus`。

應建立：

- `owned_by` / `owns`：module、config、doc、runbook、service、table 的責任歸屬。
- `implements`：Shioaji adapter → broker port；concrete store → persistence port。
- `publishes` / `subscribes`：event/topic 資料流。
- `transforms` / `validates`：normalizer、feature、risk gate、alpha gate。
- `reads_from` / `writes_to` / `persists` / `replays`：WAL、ClickHouse、Redis、replay。
- `uses_contract`：outer components 對 Kernel contracts 的使用。
- `configures` / `selected_by`：config key/profile → runtime component。
- `verified_by` / `evidence_for`：test、runbook evidence → behavior/claim。
- `defined_in`：logical table/service → migration/compose occurrence。
- `migrates` / `supersedes`：schema/document evolution。
- `variant_of` / `overrides`：compose/profile variants。
- `historical_of` / `tombstone_of`：舊版、刪除檔與 current entity。
- `paused_by`：Fubon → 明確 lifecycle decision。
- `violates_boundary`：真實但不符合目標架構的 imports；在 source 修正前不得從 graph 移除真實關係。

應修正：

- **已於 Stage 2–3 完成**：`.dockerignore` 的 node type、identity 與 layer reference由 service → config。
- **已於 Stage 2 完成**：stdlib `logging` 與 local `utils/logging.py` 的 resolver collision policy。
- **已於 Stage 3 完成**：正式 domain graph 的 44 個舊 source issues、stale quote-pool entry path與 Fubon active wording已由 current-HEAD 5-domain／68-step圖取代。
- 文件、config、script 的 primary media layer與 secondary domain owner 分離。
- **部分完成**：208 個主圖節點與 6 個 domain 節點已有 repository-level Shioaji authority override；local-env、runtime-summary、raw-runtime、live-state仍須各自證據。
- **已於 Stage 3 完成**：主圖、domain、domain-context、doc與 meta 指向同一 source commit。

Active-view 清理狀態：

- **Stage 3 已完成**：clean snapshot 不存在的 42 個舊 formal paths不再出現在 current active graph。
- **Stage 3 已完成**：Shioaji vendor/plugin bundle、binary bundle與已刪除 legacy research paths不再作 first-party active nodes；Stage 4仍需決定 external reference/tombstone。
- **Stage 1–2 已完成**：三條 stale imports與 resolver false self-import不在 current graph。
- **Stage 3 已完成**：舊 domain的 `execution_callbacks.py` step與 Fubon active flow已移除。

暫不得移除：

- source 中仍存在的 production→research 或 inner→outer imports；先標 boundary violation，source 改完後才更新關係。
- structural-only callables、K1/K6、logical table/service occurrences。

## 4.4 重複、過時與歷史知識處理原則

正式 lifecycle 建議固定為：

`active_canonical`、`active_variant`、`paused`、`historical_reference`、`superseded`、`deleted_tombstone`、`generated_vendor`、`unresolved`。

原則：

1. Active graph 只包含指定 source commit 的 source-backed entities 與有證據的 relations。
2. Migration source 永不因 logical table 重複而合併；canonical table 用 `defined_in/migrates` 保留完整時間序。
3. Compose source永不因 service 重複而合併；canonical service 用 `variant_of/overrides` 表達 profile 差異。
4. 相同 heading/topic 只進 semantic duplicate review queue；比較摘要、來源、日期、用途後才決定 canonical/superseded。
5. 舊 Shioaji 版本保留版本、日期與 evidence type；不做全域替換。
6. Deleted path 從 active view 移出，但保留 `lastSeenCommit/removedAtCommit/reason/evidence` tombstone 至少一個 release cycle。
7. Vendor/generated 使用獨立 namespace，不能與 first-party path identity 混用。
8. 非 deterministic relation 必須有 evidence、confidence 與 reviewer；低信心 relation 不參與 deletion/impact gate。
9. Plan/spec 配對以 topic identity 建立；unpaired 不代表過時，需 owner/lifecycle review。

## 4.5 分階段執行總表

複雜度：S＝局部；M＝少量多檔且邊界清楚；L＝跨模組/圖譜；XL＝跨 money path 且需 migration strategy。

| 階段 | 優先級 | 難度 | 風險 | 複雜度 | 所需能力 | 前置相依 |
|---|---|---|---|---|---|---|
| 0. Promotion authority 與 dirty ownership freeze | P0 | 中 | 高 | M | repo governance、git read-only audit、provenance | 正式覆寫意圖確認 |
| 1. Current-HEAD 增量刷新 | P0 | 高 | 高 | M | static analysis、semantic merge、domain modeling | 0；固定 HEAD |
| 2. Analyzer/ontology guardrails | P0 | 高 | 高 | L | resolver、KG schema、identity/lifecycle | 1；policy 決策 |
| 3. 正式主圖/domain/docs atomic promotion | P0 | 高 | 高 | L | graph persistence、docs、rollback | 0-2；逐 artifact 核准 |
| 4. Orphan/duplicate/history disposition | P1 | 高 | 高 | L | ontology、data lineage、migration/compose | 3 的 canonical baseline |
| 5. Kernel boundary seams | P1 | 極高 | 極高 | L/XL | HFT contracts、price/time/event、tests | 4；Tier-3 packet/review |
| 6. Runtime/Research 分離 | P1 | 極高 | 極高 | XL | alpha governance、packaging、backtest/replay | 5；entry-point closure |
| 7. Control/data/decision plane 收斂 | P2 | 極高 | 極高 | XL | broker、risk/order/execution、WAL、ops | 5-6；SIM/replay evidence |
| 8. Shioaji 1.5.6 治理與 runtime evidence closure | P0/P2 | 高 | 極高 | M/L | Shioaji、ops safety、evidence governance | docs review；runtime另核准 |
| 9. 持續 freshness/import-contract 治理 | P2 | 高 | 高 | L | automation、schema、local gates | 3-8 穩定後 |

## 4.6 各階段可執行細節

### 階段 0：Promotion authority 與 dirty ownership freeze

- 預期成果：固定待分析 HEAD；產生 project/formal/user-owned allowlist；保存正式六個 artifacts 的 bytes、hash、ownership 與 rollback location；把 `/tmp` evidence 移到經核准的耐久 scratch/evidence location。
- 主要風險：覆蓋未追蹤圖譜、dirty docs、agent memory 或使用者檔案。
- 驗證：`git status --short --untracked-files=all`；逐檔 SHA-256；allowlist 與 current HEAD 一致；0 source edits。
- 回滾：本階段不做正式寫入；失敗時丟棄新 scratch manifest。
- Gate：使用者明確核准「哪些正式 artifacts 可覆寫」；這不是 blanket approval。

### 階段 1：Current-HEAD 增量刷新

- 預期成果：只對 snapshot 後三個變動路徑及其受影響 semantic/domain relations 刷新；重新組裝 current-HEAD candidate 與 delta。
- 主要風險：`docker-compose.yml` 變動會改 service/variant knowledge；ops backup 與 test 變動會使 layer/tour/domain evidence drift。
- 驗證：scan inventory parity；fresh batch coverage；declaration/import parity；schema 0 issues；domain anchors 0 miss；candidate `gitCommitHash==current HEAD`；CI仍排除。
- 回滾：只在隔離 scratch 生成；失敗即保留 `075ce080...` baseline，不 promotion。
- Gate：HEAD 在整個 refresh/review 期間不得再漂移；若漂移就重算 delta。

### 階段 2：Analyzer/ontology guardrails

- 預期成果：修正 `.dockerignore` classifier、stdlib/local module collision policy；定義 lifecycle、logical identity、owner與 evidence schema；把 exact/contained domain-anchor 規則納入 validator。
- 主要風險：全域 classifier 變動重分類大量 nodes；過寬 resolver 可能新增假 import/call。
- 驗證：unit corpus + HFT corpus precision checks；before/after classification delta；0 self/dangling/unsupported import；known false-positive fixtures；不合成 ambiguous cross-file calls。
- 回滾：analyzer policy 每項獨立變更；保留舊 schema reader；candidate 可由舊 analyzer重建。
- Gate：先 review Understand Anything plugin 既有 dirty diff；不能把 unrelated plugin change 一起帶入。

### 階段 3：正式主圖/domain/docs atomic promotion

- 預期成果：同一 current HEAD 的 `knowledge-graph.json`、`domain-graph.json`、`domain-context.json`、`meta.json` 與架構文件；config 只有在確有必要時變更。
- 主要風險：partial promotion、dashboard/schema incompatibility、使用者未追蹤內容被覆蓋、candidate inference 被寫成 fact。
- 驗證：兩圖 `validateGraph` 0 issues；file/declaration/import/layer/tour parity；domain owner/weight/path/range gates；Fubon active 0；CI paths 0；all artifacts commit/hash manifest一致；doc paths/anchors有效。
- 回滾：覆寫前 byte-for-byte snapshot；使用 atomic replace；任何一項 gate 失敗就全部恢復，不留混合版本。
- Gate：精確列出每個覆寫路徑並取得正式 approval；git add/commit 是另一個 approval point。

實際結果（2026-07-21）：五路徑逐一取得核准，建立 repo 外 byte backup，寫入同一 HEAD provenance；兩圖 schema、68/68 domain anchors、scope/import/declaration/layer/tour、Fubon/CI與文件 path gates全部通過後才保留。`config.json` hash未變；Git mutation仍未授權。

### 階段 4：Orphan/duplicate/history disposition

- 預期成果：336 orphans 全數有 K0-K7 disposition；table/service canonical identities；25 heading/6 topic review queue；正式 vendor/deleted nodes移到 history namespace。
- 主要風險：歷史 occurrence 被當垃圾、manual consumer 未被發現、migration/profile順序被抹平。
- 驗證：orphan queue 100% disposition；source occurrence count不減；所有 active-view removal 有 lifecycle/evidence；K7 全人工 review；owner 未知保持 unresolved。
- 回滾：只調整 graph/metadata，不刪 source；保留 before/after mapping，可整體回復。
- Gate：logical identity schema先核准；不得直接改 migration/compose。

### 階段 5：Kernel boundary seams

- 預期成果：分三個獨立 packets 切除 `core→feed_adapter`、event bus→metrics implementation、events→feature conversion 的反向依賴；新增 local import-contract gate。
- 主要風險：price scale、typed event layout、latency、metrics、overflow或serialization behavior drift。
- 驗證：每個 seam baseline-vs-after targeted tests；price/time/event contract tests；type/lint/local `make check`（若當次授權）；microbenchmark或性能預算；Tier-3 adversarial review；不執行 CI/live。
- 回滾：先新增 port/provider，再切 consumer；舊 adapter保留一個 migration window；每個 seam可獨立還原。
- Gate：任何 golden、frozen profile、dependency、Rust ABI 或 Do-NOT-Edit path 進 blast radius 時另行核准。

### 階段 6：Runtime/Research 分離

- 預期成果：列出 production entry-point import closure；抽出 alpha/registry/backtest contracts；production runtime只依賴 ports/DTO，research implementation由 CLI/offline adapter注入；22 條直接 imports逐批歸零或明確豁免。
- 主要風險：promotion gate、scorecard schema、backtest parity、CLI、monitor dispatch或research reproducibility改變。
- 驗證：import contract；entry-point packaging smoke；Gate A-F behavior tests；replay/backtest parity；每批 diff有 Tier-3 review；source與graph同步。
- 回滾：先兼容 adapter、後改 consumer、最後才移目錄；舊 import path以明確 deprecation shim短期保留。
- Gate：owner確認哪些 alpha components 是 runtime；未確認前不搬目錄。

執行進度（2026-07-23）：alpha schema canonical ownership packet 已將 production→research
直接 imports 由 21 降至 16。DataUL packet 將 `DataUL` 與純 metadata validation rules
提升至 `hft_platform.contracts.data_ul`，保留 `research.tools.vm_ul` identity-compatible
re-export shim，並將 Gate A consumer 切至 canonical contract，已將 imports 由 16 降至 15。
Maker action DTO packet 將 `PostQuote`、`CancelQuote`、`Hold` 提升至
`hft_platform.contracts.maker_actions`，由 maker engine 相容 re-export，並將 maker bridge
切至 canonical contract，將 imports 降至 14 條。Calibration artifact boundary packet
新增 `hft_platform.config.calibration_profiles` 作為唯讀 artifact reader/read model；
`research.calibration.config` 保留完整評分 schema 與 writer，並相容 re-export canonical
path/error identity；`HftBacktestAdapter` 不再 import research implementation。當前 source
AST 為 13 條 production→research direct imports。Alpha discovery boundary packet 新增
`hft_platform.alpha.discovery.AlphaDiscoveryRegistry` 作為 canonical discovery/register
實作；`research.registry.alpha_registry.AlphaRegistry` 保留 legacy class/pickle identity
與 research-only correlation service，validation 與 monitor dispatcher 改依賴 canonical
registry；HEAD parity 與 boundary tests 通過後，source AST 已降至 11 條 direct imports。
但掃描 `research/alphas` 時，Python 仍會因 `research/__init__.py` 的 eager re-export
傳遞載入 legacy registry；此為後續 import-closure packet，不把本輪 direct-edge 成果
誤寫成完整 runtime/research 分離。
正式知識圖譜仍以 `56061e90...` 為 baseline，需待本 packet 驗證與 Tier-3 review 通過後，
再以獨立 promotion gate 同步，不能把 source working tree 狀態冒充正式 graph。

### 階段 7：Control/data/decision plane 收斂

- 預期成果：services成為唯一 composition root；market-data、decision/execution、durability透過 ports連接；逐步縮小 30-module與7-layer SCC。
- 主要風險：startup latch、HALT/reduce-only、reconnect、order identity、fill dedup/reconciliation、WAL durability、shutdown drain等 money-path invariants破壞。
- 驗證：每個 boundary edge單獨 packet；baseline-vs-after local suites；failure injection；deterministic replay/SIM；dependency SCC 指標只縮不增；Tier-3 orchestrator review；CI依使用者指示仍不納入。
- 回滾：不做 big-bang directory move；保留舊 composition path/feature flag一個 migration window；每批可獨立 rollback。
- Gate：live/production永遠需要當次明確核准；本階段預設只到 offline/SIM。

### 階段 8：Shioaji 1.5.6 治理與 runtime evidence closure

- 預期成果：repo pin、lock、codemap、risk register、ADR/runbook labels一致；歷史版本標示 compatibility/history；委派 backfill耐久化決策完成；runtime evidence另建 checksum manifest。
- 主要風險：把 repo pin當 deployment、把 SIM當 live、引用無效 305-order RTT、host override意外改為 live、broker session用盡。
- 驗證（repo）：lock consistency、docs source review、所有 authority claim帶 evidence level；**不建議修改 pin**。
- 驗證（runtime，另核准）：container interpreter version、raw bundle checksum、day-session 296-symbol data timestamps、session/pool headroom、SIM order mode、post-run logout/cleanup。
- 回滾：repo docs可逐檔還原；dependency不變；runtime需 rollback image、SIM default與stop procedure。
- Gate：`git add -f` backfill、container access、restart、broker login或任何 live probe各自需要授權。

### 階段 9：持續 freshness/import-contract 治理

- 預期成果：本地 freshness gate檢查 source commit/path/import parity/lifecycle；architecture boundary allowlist；圖譜 promotion manifest；定期 orphan/stale queue。
- 主要風險：gate誤報阻斷開發，或為了綠燈而放寬門檻。
- 驗證：用已知 stale path、false import、boundary violation做 red/green fixtures；gate failures輸出可操作證據；CI仍不在本次 scope。
- 回滾：先 advisory、後 blocking；保留明確且有期限的 exception registry。
- Gate：frozen registry/profile 或 enforcement config 的變更需獨立核准。

## 4.7 建議保留不動的高風險區域

在上述依賴 seam、baseline、owner與 rollback gate 完成前，不整理式搬移、合併、rename、刪除：

- `src/hft_platform/core/{timebase,pricing}.py`、`events.py`、contracts/event schemas。
- `risk/`、`gateway/`、`order/`、`execution/`、position/reconciliation。
- Shioaji order/account/callback/reconnect/session/quote pool paths。
- recorder/WAL、ClickHouse migrations、schema history、replay parity與 goldens。
- Rust/PyO3 hot path、ABI、benchmark baselines。
- `pyproject.toml`、`uv.lock`、frozen registries/profiles、enforcement config。
- compose、production `.env`、images、running containers與 broker sessions。
- Fubon 全部 source/test/config：保持 paused，不啟用也不順手刪除。
- current formal `.understand-anything/*` 與架構文件：任何未來 promotion仍須新的精確 approval、byte backup與 transaction gate。
- 主工作樹所有既有 dirty/untracked/deleted內容，包括 vendor bundle、legacy scripts、agent memory與 Shioaji docs。

# 5. 正式執行前的阻塞條件

## 5.1 Stage 0–3 blocker closure

| 原 blocker | 狀態 | 關閉證據／保留條件 |
|---|---|---|
| P-BLOCK-01 candidate 落後 HEAD | 已關閉 | 3 路徑增量刷新，正式 commit為 `56061e90...` |
| P-BLOCK-02 overwrite approval | 已關閉 | 使用者逐階段核准五個正式路徑 |
| P-BLOCK-03 dirty ownership | 已關閉 | 固定 dirty digest、ownership、allowlist與 byte backup |
| P-BLOCK-04 untracked artifact policy | working-tree promotion已關閉；Git policy未決 | 本次沒有 git mutation；是否追蹤需另核准 |
| P-BLOCK-05 analyzer defects | 已關閉 | `.dockerignore` classifier、logging resolver與 fixtures通過 |
| P-BLOCK-06 reuse/authority provenance | 已關閉 | root incremental provenance + 208/6 authority-normalized node governance |
| P-BLOCK-07 `/tmp` 非耐久 | 已關閉 | repo 外 local-state 保存 2,537 檔 source snapshot、incremental base、scan、full extraction、prune report、逐檔 SHA-256 與無 `/tmp` 依賴的參數化 replay validator |

Stage 0–4 formal graph governance promotion已無剩餘 blocker。以下阻塞只適用於尚未授權的 Stage 5–9 與 source/runtime工作。

## 5.2 架構 source 重整的硬阻塞

1. Stage 4 的全量 K0–K7 disposition、lifecycle、logical identity 與 history namespace 已完成；剩餘 owner/consumer、service override、document chronology與 exact removal commit 仍需後續 evidence，不得由圖自動推定。
2. production entry points與runtime import closure尚未確認，無法安全拆 runtime/research。
3. 三個 Kernel seams 尚未形成獨立 contract/test packets。
4. 目前 call graph 不完整，不能產生可靠 dead-code清單。
5. money-path baseline、性能預算、SIM/replay驗證與 Tier-3 reviewer尚未逐批指定。
6. 任何 dirty file進入 blast radius時，需先釐清所有權；不得覆蓋。

## 5.3 Shioaji／委派治理的阻塞

1. Dependency pin 本身已是 1.5.6，**沒有待執行的 pin mutation**；若未來要再改版本，仍屬 Tier-X 並需新的明確核准。
2. Codemap/risk-register 目前只有 dirty working-tree diff，需 docs review與使用者所有權確認後才能正式採納。
3. 委派 backfill內容存在且可驗證，但受 ignore且未追蹤；耐久化需要明確 git planning/execution approval。
4. Old-host raw evidence bundle本機缺失，day-session full-universe data-flow未完成；這不阻擋 repo pin/graph authority，但阻擋 runtime acceptance或live cutover claim。
5. 任何 old-host、container、broker、restart或live操作均需當次 in-session 明確核准。

## 5.4 後續正式執行前仍需確認的事項

1. 已確認：Stage 4 已只補 K0–K7 disposition、logical identity與 lifecycle，未動 source。下一步仍需指定 K1/K6 owner 與外部 consumer 的確認責任。
2. 336 個 orphan 中，K1/K6 的 owner與外部 consumer由誰確認；未確認維持 `unresolved`。
3. 是否依序核准第一批 Tier-3 source seams：pricing → event-bus telemetry → event conversion；每批都需獨立 packet、baseline與 rollback。
4. 哪些 `src/hft_platform/alpha` components確屬 runtime import closure；未確認前不搬移 `research/`。
5. generated graphs維持 working-tree artifacts或納入版本控制；這是獨立 Git policy，不由本次 approval推定。
6. old-host raw bundle、day-session 296-symbol flow與 runtime acceptance何時另開；建議與 source重整分離，維持 SIM/no-order。
7. 任何 dependency、frozen registry/profile、golden、migration、live/prod、broker login、container restart或 Git mutation仍需新的精確核准。

## 交接結論

Stage 4 已完成；下一個執行 Agent應從 K1/K6 owner/consumer evidence queue 或 **Stage 5：Kernel boundary seams** 的單一、另行核准 packet 開始，不應直接搬目錄或修改 money path。若進入 Stage 5，第一個 source packet仍建議只處理 `core/pricing.py` dependency seam；在 Tier-3 baseline、targeted tests、性能預算、review與 rollback均寫清楚前，不得擴大到 events、risk/order、broker、WAL或 Rust。

Current canonical graph品質判定：**PASS，可作為後續 evidence baseline。**  
Stage 3 formal promotion判定：**COMPLETE。**  
Source refactor判定：**NO-GO，直到每個 Stage 5 seam 取得獨立 Tier-3 packet、baseline、tests、review與 rollback 核准。**

## 本輪驗證摘要

- 主圖 `validateGraph`：PASS，17,076 nodes／34,094 edges／0 issues；SHA-256 `568cc2ec10e401c686abd92cec6a2b856a1617dbed64a3e2b00e87c15260791b`。
- Domain 圖 `validateGraph`：PASS，85 nodes／87 edges／0 issues；SHA-256 `2f0ead0587d9595ca31ff41c70ae4b2b9741a3f7f33f83db76553f15f70c05ec`。
- Domain anchor audit：PASS，68/68 traceable，0 unanchored。
- 主圖 hard gates：3,874/3,874 imports、13,138 minimum-significant declarations missing 0、2,638/2,638 layer assignments、51 tour refs unknown 0、self/dangling/duplicate/CI/Fubon activation均 0。
- Governance：208 authority-normalized主圖 nodes、6 domain nodes、155 paused Fubon source nodes；`.dockerignore` active old-identity references為 0，並保留 1 筆 correction provenance。
- Domain context：758 source files、117 entry points、40 signatures；CI paths 0；project root已正規化。
- Meta、主圖、domain、domain-context與本文件均指向 `56061e90...`；`config.json` hash保持 `c4979382...`。
- 未執行：HFT tests、CI、network／old-host、package install、dependency mutation、container、broker、order、runtime、deployment、live/prod、Fubon activation或任何 Git mutation。

Stage 4 promotion 驗證：主圖 `validateGraph` PASS，17,104 nodes／34,209 edges／0 issues；preapproval evidence gate 42/42 與 promoted-transition prewrite gate 均全通過；738 dispositions、321 history records、77 source occurrences preserved。主圖 SHA-256 `9ef373cc3f544b7c541cf32691930bbae4fdedbdfe32e00bce5e04e1db3f6ea8`；disposition SHA-256 `fba9ffd1bef4f104576c7cddfecba4e733d08d70ddec6dd9572975daad626a3a`。


## Stage 4 formal promotion update（2026-07-23）

已完成只限 knowledge governance 的三路徑 formal promotion，未修改任何 source、migration、compose、runtime、CI、Fubon 或 Git。

使用者對三條 formal paths 的 exact-path approval 於 2026-07-22 取得；本次 formal write／驗證日期為 2026-07-23。

- 以 Stage 3 baseline connectivity 選出的 738 個 nodes（baseline：336 degree-zero、402 degree-one）皆有 K0–K7 disposition：K0 74、K1 301、K2 174、K4 57、K6 64、K7 68；71 個初始 K7 均完成 Tier-3 人工 review。新增 Stage 4 關係後，這 738 個 nodes 的即時 degree 分布為 334 degree-zero、344 degree-one、60 degree-greater-than-one。
- 新增 28 個 canonical logical identities（16 table、12 service）與 110 條 occurrence-preserving edges（77 `defined_in`、33 `variant_of`）；77 個 source occurrences 全數保留。
- 新增 5 條直接來源證據關係（2 `configures`、1 `historical_of`、2 `depends_on`）；不建立未證實的 owner、service override、document supersession 或 deleted tombstone 關係。
- history namespace 保存 321 筆紀錄：35 generated/vendor、119 deletion candidates、2 scope-excluded current paths、165 scope/identity transitions；未知 removal commit 維持 `unresolved`。
- 25 組 duplicate heading、6 組 curated-topic、23 組 plan/spec pair、29 unpaired plans 與 17 unpaired specs 僅進 review queue；沒有自動刪除或 supersession。
- `src/hft_platform/strategies/__init__.py` 已確認是可執行 source re-export surface，但 `runtimeReachability` 為 `unresolved`；不得外推 runtime、live 或 production reachability。

獨立 Tier-3 review verdict：`APPROVE-WITH-NITS`。唯一非阻塞語意事項已在 promoted graph/disposition 以 `sourceExecutable:true` 與 `runtimeReachability: unresolved` 明確表達。
