# ROADMAP（僅保留未完成項）

版本日期：2026-03-05
時間範圍：2026-03-05 至 2029-03-31

> 本文件僅保留未完成的目標、交付與驗收。已完成項目已自本文件移除。

> WS-G / WS-H 治理欄位標準：`技能`、`RACI`、`Agent 角色`、`KPI`、`風險與緩解`、`依賴`、`Gate 證據`。
> 固定角色字典：`Rust Lead`、`Tech Lead`、`Strategy Owner`、`Ops Oncall`、`Research Lead`、`Head of Research`、`Data Steward`、`Trading Runtime Owner`。

## 1. 最終目標（2029-03-31 前）
在舊電腦部署環境下，達成可量測、可回滾、可稽核的三年無人值守運轉能力。

## 2. 成功判定（需連續 12 個月滿足）
1. 可用性
- `hft-engine`、`hft-monitor`、`wal-loader`、`clickhouse`、`redis`、`prometheus` 月可用率 >= 99.95%。
- `execution_gateway_alive`、`execution_router_alive` 月可用率 >= 99.9%。

2. 資料完整性
- 每日資料完整度（符號/時段覆蓋）>= 99.5%。
- `wal_backlog_files`：p95 <= 20，p99 <= 100。
- `wal_replay_errors_24h = 0` 天數佔比 >= 99%。

3. 自治營運
- 非計畫性人工介入 <= 每季 1 次。
- 關鍵事故 MTTD <= 5 分鐘，MTTR <= 20 分鐘。

4. 變更安全
- 100% 生產同步具備 pre-sync 備份、回滾 tag/branch、回滾腳本。
- 非核准例外之外，remote 與 `origin/main` 不得存在失控漂移。

## 3. 目前未完成缺口（截至 2026-03-05）
1. WS-A（Feed / Session）
- 完成 production burn-in 證據化（重連穩定、callback 路徑穩定）。
- 長期追蹤 malformed payload 隔離與 gap/symbol_gap 節流是否符合日預算。

2. WS-B（WAL / ClickHouse）
- 完成 MV 壓力治理的持續優化（批次形狀、併發、拆分策略）。
- 落地 wal-loader 自適應 backpressure（依 ClickHouse 健康動態調整）。

3. WS-C（資料治理）
- 完成每日資料品質稽核全自動化（覆蓋、lag、重複估計、schema 異常）。
- 建立 15 分鐘內異常檢出與告警閉環。

4. WS-D（部署治理）
- 將變更前/後證據包抽查制度化，確保季度 0 次未授權漂移。

5. WS-E（HA / DR）
- 固化季度 chaos drills（CH down/slow、disk pressure、process crash、重連風暴）。
- 建立暖備援主機演練節奏與 failover/rejoin 實證紀錄。

6. WS-F（硬體與生命週期）
- SMART / 容量 / Prometheus 儲存用量 / OS patch 檢核自動化仍未完成。

7. WS-G（熱路徑 Rust 化）
- strategy/risk/order/execution 熱點仍有 Python 路徑未切分至 Rust kernel。
- 缺少全鏈路 FFI latency/alloc guardrail 與持續化 parity + perf gate。

8. WS-H（研究與分析工廠擴大）
- 研究輸入來源與批次分析產能不足，尚未形成穩定 conveyor。
- research→alpha promotion 缺少 SLA 與可稽核品質門檻。

## 4. 工作流與退場門檻（Open Only）

### WS-A：Feed / Session 韌性
- 交付：burn-in 證據、異常節流預算治理、回歸演練。
- 退場門檻：連續 60 個交易日無 crash-signature critical 告警。

### WS-B：WAL / ClickHouse 穩定吞吐
- 交付：MV 壓力治理、自適應 backpressure、持續 SLO 維持。
- 退場門檻：`Insert failed` 比率日 < 0.5%、週 < 0.1%，且 CH 短故障後 10 分鐘內 backlog 回基線。

### WS-C：資料治理與品質稽核
- 交付：品質標籤入 audit、每日報告全自動、異常快速告警。
- 退場門檻：連續 90 天每日報告自動產出且無人工介入。

### WS-D：部署漂移控制與回滾紀律
- 交付：部署證據包稽核制度、回滾演練常態化。
- 退場門檻：每季 0 次未授權漂移，且部署可回滾率 100%。

### WS-E：HA / 災難恢復
- 交付：季度演練全套證據、暖備援 failover/rejoin SOP 定期驗證。
- 退場門檻：季度演練全通過，RTO <= 20 分鐘，RPO <= 1 分鐘。

### WS-F：硬體與生命週期管理
- 交付：硬體健康與容量預算自動稽核、年度維護窗口決策機制。
- 退場門檻：無磁碟耗盡或過熱造成非計畫停機，容量餘裕達標（磁碟 >= 30%，RAM >= 25%）。

### WS-G：熱路徑 Rust 化擴編
- 交付：hotpath profiling matrix、分批 Rust cutover、parity/perf gate 自動化。
- 技能：`hft-strategy-dev`、`rust_feature_engineering`、`performance-profiling`
- RACI：R=Rust Lead、A=Tech Lead、C=Strategy Owner、I=Ops Oncall
- Agent 角色：`explorer`（profiling/baseline，輸出 `hotpath_matrix`）→ `worker`（Rust cutover/CI，輸出 `cutover_patch+ci_report`）→ `default`（整合驗收，輸出 `gate_summary`）
- KPI：
  - end-to-end `p95 latency` 較 2026-03 基線下降 >= 20%。
  - `FFI copy ratio` <= 5%，`alloc/tick` 較基線下降 >= 30%。
  - `parity pass rate = 100%`，且 30 天 soak 期間 `parity_critical=0`。
- 風險與緩解：
  - 風險：FFI 邊界隱性拷貝造成長尾延遲惡化。
  - 緩解：先完成 typed frame/zero-copy 合規檢查，未通過不允許 cutover。
  - 風險：價格精度/事件語義回歸。
  - 緩解：以 replay + parity 雙重 gate 封鎖 promotion。
  - 風險：GIL/CPU contention 造成 throughput 下滑。
  - 緩解：將 GIL 佔用與 core contention 納入 perf gate。
- 依賴：
  - 流程依賴：`profiling matrix -> kernel cutover -> CI parity/perf gate -> soak`。
  - 跨工作流依賴：依賴 WS-A burn-in 量測輸出與 WS-B 吞吐基線。
- Gate 證據：
  - `hotpath_matrix`、before/after latency 報表、FFI/alloc 指標、parity 報告、30 天 soak 摘要、RACI 簽核紀錄。
- Gate 映射：G4 需同時通過 KPI、parity/perf gate、證據包完整性與 RACI A 角色簽核。
- 退場門檻：至少 3 個核心熱模組完成 cutover，且 end-to-end p95 latency 較 2026-03 基線下降 >= 20%，連續 30 天無 parity critical。

### WS-H：研究與分析工廠擴大化
- 交付：多來源研究輸入、批次分析 pipeline、產能/品質儀表板、promotion SLA。
- 技能：`hft-alpha-research`、`validation-gate`、`clickhouse-io`
- RACI：R=Research Lead、A=Head of Research、C=Data Steward、I=Trading Runtime Owner
- Agent 角色：`explorer`（source inventory，輸出 `source_catalog`）→ `worker`（pipeline/quality gate，輸出 `factory_pipeline+quality_report`）→ `default`（報告與 promotion 整合，輸出 `promotion_readiness`）
- KPI：
  - 每週候選研究處理量 >= 50 篇，引用完整率 >= 98%。
  - 去重命中率 >= 95%，research->alpha scaffold 中位 lead time <= 2 天。
  - promotion 前 quality gate 通過率（月）>= 90%。
- 風險與緩解：
  - 風險：來源資料品質漂移導致引用/去重失真。
  - 緩解：來源分級 + 抽樣稽核，不合格來源先隔離。
  - 風險：批次管線失敗堆積，吞吐下滑。
  - 緩解：設定 retry/backlog 閾值與告警，超閾值切入人工 triage。
  - 風險：hypothesis queue 品質波動影響 promotion。
  - 緩解：promotion pre-check 強制加入可重現性/時效性 gate。
- 依賴：
  - 流程依賴：`source inventory -> metadata/dedup -> batch analysis -> hypothesis queue -> promotion pre-check`。
  - 跨工作流依賴：依賴 WS-C 品質稽核輸出與 Gate A-E 驗證管線。
- Gate 證據：
  - `source_catalog`、週期 throughput/quality 報表、dedup/citation audit、lead time 趨勢、promotion pre-check 結果、RACI 簽核紀錄。
- Gate 映射：G5 需同時通過 KPI、quality gate、證據包完整性與 RACI A 角色簽核。
- 退場門檻：每週候選研究處理量 >= 50 篇、引用完整率 >= 98%、research→alpha scaffold 中位 lead time <= 2 天。

## 4.1 跨工作流依賴（WS-G/WS-H）
1. WS-G -> WS-A：需沿用 WS-A 的 burn-in 量測口徑與告警基線。
2. WS-G -> WS-B：需沿用 WS-B 的 recorder/回壓基線，避免 cutover 誤判吞吐瓶頸。
3. WS-H -> WS-C：研究輸入/輸出品質標籤需與 WS-C 每日品質稽核同一口徑。
4. WS-H -> Gate A-E：研究工廠輸出需可直連 alpha validate/promote，不允許手工旁路。

## 5. 里程碑（未來）
| 里程碑 | 目標日期 | 核心工作流 | 驗收 Gate |
|---|---|---|---|
| M1 Feed 穩定化 | 2026-06-30 | WS-A | G1 |
| M2 WAL/CH 壓力受控 | 2026-09-30 | WS-B | G2 |
| M3 資料治理自動化 | 2026-12-31 | WS-C | G3 |
| M4 熱路徑 Rust 化擴編 | 2027-03-31 | WS-G | G4 |
| M5 研究工廠擴容 | 2027-06-30 | WS-H | G5 |
| M6 HA 演練合格 | 2027-09-30 | WS-E | G6 |
| M7 低介入營運 | 2027-12-31 | WS-A/B/C/D/E/G/H | G7 |
| M8 三年運轉認證 | 2029-03-31 | 全部 | G8 |

角色就緒驗收（M4/M5）：Gate 評審前需完成對應 RACI owner 簽核，並在月度審查包附掛 Skills/Agent 角色對位證據。
M4/M5 Gate 證據包最低欄位：性能快照、品質報告、風險處置紀錄、RACI 簽核、回滾與例外紀錄。

## 6. 接下來 30 天（2026-03-05 至 2026-04-04）
1. WS-A burn-in 量測包模板（Owner: Ops Oncall；截止: 2026-03-12；輸出: burn-in template；驗收: 指標口徑凍結並可週更）。
2. WS-B MV 壓力治理基線（Owner: Tech Lead；截止: 2026-03-15；輸出: MV baseline 報表；驗收: 批次/重試/回壓關聯可重現）。
3. WS-C 每日品質檢查固定產物（Owner: Data Steward；截止: 2026-03-18；輸出: quality report 格式與責任路由；驗收: 連續 7 天自動產出）。
4. WS-F 月度檢核串接審查包（Owner: Ops Oncall；截止: 2026-03-20；輸出: 月度檢核附掛流程；驗收: TTL/SMART/容量自動入包）。
5. WS-G 第一批 hotpath matrix 與 cutover 名單（Owner: Rust Lead；截止: 2026-03-22；輸出: `hotpath_matrix` + cutover backlog；驗收: 優先序與依賴鏈可審核）。
6. WS-H 研究工廠批次化設計（Owner: Research Lead；截止: 2026-03-25；輸出: `source_catalog` + pipeline 草案 + KPI 定義；驗收: 可接 promotion pre-check）。

## 7. 執行節奏（未來）
1. 每日：soak + quality report 自動產出，critical 告警 15 分鐘內 triage。
2. 每週：drill 子集驗證 + drift/rollback 證據抽查。
3. 每月：容量與風險審查，輸出 go/no-go 決策紀錄。
4. 每季：全量 chaos drill + Gate 評審 + backlog 重排。
