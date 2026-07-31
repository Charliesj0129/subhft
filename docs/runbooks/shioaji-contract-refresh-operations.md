# Shioaji Contract Refresh Operations Runbook

## 概述

Contract refresh 負責在背景定期更新交易合約快取（`config/contracts.json`），確保策略引用的合約資訊保持最新。Refresh 失敗為非致命性，系統會繼續使用前次快取。

---

## 日常操作節奏

### 市場開盤前執行建議
- **建議時機**: 開盤前 10 分鐘（08:40 TWS）自動觸發。
- 若系統啟動於開盤後，contract refresh 會在首次 login 後 60s 內完成。
- 每日非交易時段（收盤後 2 小時）亦建議執行一次，確認隔日合約清單。

### 手動觸發
```bash
# 檢查當前快取狀態
docker compose logs hft-engine | rg "contract_refresh|contract_diff"

# 完整重啟（含合約重新載入）
docker compose restart hft-engine
```

---

## 執行行為

### 背景執行緒
- Contract refresh 由背景執行緒定期執行（lock-guarded，避免並行重疊）。
- Diff 結果記錄至 log（`contract_refresh_diff`）並快取於記憶體。
- `config/contracts.json` 以原子性寫入方式更新（`write_contract_cache()`）。

### Resubscribe 策略

由 `HFT_CONTRACT_REFRESH_RESUBSCRIBE_POLICY` 控制：

| 值 | 行為 |
|---|---|
| `none` | 僅重載 symbols/routes，不重新訂閱行情（Mode 2 應變使用） |
| `diff`（預設，2026-04-18 起） | 若 contract diff 有變動（新增/移除），重新訂閱受影響標的——讓月度 rollover（例如 TMFE6→TMFF6）自動生效 |
| `all` | 每次 refresh 後強制重新訂閱全部標的 |

---

## 監控指標

### 正常值基準

| 指標 | 正常值 | 告警值 |
|---|---|---|
| `contract_refresh_total[result=ok]` | 持續遞增（每日數次） | — |
| `contract_refresh_total[result=error]` | = 0 | >3 連續失敗 |
| `contract_refresh_total[result=skipped_locked]` | 偶發（重疊保護） | 持續 >10 → 可能 thread 鎖死 |
| `contract_refresh_symbols_changed_total[change=added]` | 偶爾出現（新合約上市） | — |
| `contract_refresh_symbols_changed_total[change=removed]` | 偶爾出現（合約到期） | 單次 >5% 總標的數 → 異常，見下方 |

### 查詢指標
```bash
curl -fsS http://localhost:9090/metrics | rg "contract_refresh"
```

---

## 失敗模式與處置

### 模式 1：Refresh 超時
**徵兆**: `contract_refresh_total[result=error]` 持續增加，log 出現 `contract refresh failed`。

**原因**: Shioaji API 連線不穩或 token 失效。

**處置**:
1. 確認 Shioaji session 正常：`docker compose logs hft-engine | rg "login|session"`。
2. 確認 `contract_refresh_total[result=ok]` 最後一次成功時間。
3. 若 session 有效但 refresh 持續失敗 → 重啟 hft-engine：
```bash
docker compose restart hft-engine
```
4. 系統在 refresh 失敗期間會繼續使用舊快取運作，非緊急情況可等待下一個 refresh 週期。

### 模式 2：Symbols 異常減少（>5%）
**徵兆**: `contract_refresh_symbols_changed_total[change=removed]` 單次大量出現。

**原因**: Broker 端 API 回傳異常（非實際合約下市），或網路中途截斷。

**影響**: 若 resubscribe policy 為 `diff` 或 `all`，系統可能取消訂閱正常運作中的標的。

**處置**:
1. **立即確認**: 比對 `config/contracts.json` 與前次備份。
2. 若確認為誤報（非真實下市），**不要重啟**，等待下一個 refresh 週期自動恢復。
3. 若策略正在交易受影響標的 → 手動確認倉位是否完整。
4. **臨時緩解**（Mode 2 期間）：將 `HFT_CONTRACT_REFRESH_RESUBSCRIBE_POLICY` 從預設 `diff` 改為 `none`，待 broker 回傳恢復正常後再改回。

### 模式 3：Thread 鎖死（skipped_locked 持續累積）
**徵兆**: `contract_refresh_total[result=skipped_locked]` 持續遞增，無 `ok` 記錄。

**原因**: Refresh lock 未正常釋放（前次 refresh 執行緒崩潰）。

**處置**:
```bash
docker compose restart hft-engine
```

---

## Contract freshness alerts（2026-07-31 重寫）

### 前提：hourly poll 結構性失效，不是可修的 bug

shioaji 1.5.x 的 `fetch_contracts` 需要 inner client 的**獨佔所有權**（Arc
strong_count 1）。我們的 facade 全程持有 74 個已註冊訂閱，永遠不釋放，所以
**每小時的 poll 100% 失敗**（THESHOW 實測：24 h 內 100/100 失敗，重啟後 11 h
內 44/44）。這不是競爭問題 —— `fb71b840` 把四個 facade 用 2 s 錯開之後失敗率
完全沒變，那正是「跨 facade 競爭」假設被證偽的證據。

因此：

- `contract_cache_last_success_ts` 實際上由**登入時**的合約載入寫入，不是由
  poll 寫入。它追蹤的是**登入節奏**，不是 refresh 節奏。
- 合約的新鮮度上限 = 最後一次登入。

### 為什麼舊的兩條規則不能直接部署

兩條規則寫於 2026-07-29，從未部署。用實測資料檢查後都不成立：

| 原規則 | 問題 |
|---|---|
| `ContractCacheStale` 門檻 3 h | 登入集中在盤別交界。THESHOW 實測合法間隔為 **6.01 h**（22:58 CST 重啟 → 05:00 CST 夜盤收）與 3.43 h。最壞的**合法**情況是夜盤開盤重啟後到 05:00 才重登，約 14 h，且全程在交易時段內。3 h 門檻每晚誤報約 3 h。 |
| `ContractRefreshFetchFailing` `increase(...[3h]) >= 3` | 每小時 4 個 facade 失敗 = 每 3 h 12 次，**永遠成立**。部署等於製造一條永久燃燒的告警，而且它的 description 指向已被證偽的「facade 競爭」處置方向。 |

### 現行規則

**`ContractCacheStale`** — 門檻改為 **18 h**（14 h 最壞合法情況 + 餘裕），並加上
`market_trading_hours_active == 1`。它現在偵測的是「某個 facade 跨過盤別交界卻
沒有重新登入」，也就是 stranded facade，不是合約路徑本身的問題。查
`hft_quote_conn_logged_in` 與 `HFT_RECONNECT_HOURS` / `HFT_RECONNECT_HOURS_2`。

**`ContractsStaleVsBrokerAnnouncement`** — 取代 `ContractRefreshFetchFailing`。
Broker 會在 `APISUB/V1/SYS/CONTRACT` 主動推播合約異動，
`contract_update_last_event_ts` 記錄最後一次推播時間。若它**晚於**最後一次載入，
我們就是可證明地過期了 —— 不需要任何節奏假設。

`ContractRefreshFetchFailing` 已刪除：對一個結構上不可能成功的呼叫告警沒有意義。

### 尚未證實的部分（重要）

推播訂閱**已由 broker 確認**：登入後每個 facade 都會出現

```
Response Code: 200 | Event Code: 16 | Info: APISUB/V1/SYS/CONTRACT | Event: Subscribe or Unsubscribe ok
```

但截至 2026-07-31 **尚未收到任何一次實際推播**，`contract_update_last_event_ts`
仍為 `0.0`。所以 `ContractsStaleVsBrokerAnnouncement` 在現場未經驗證，**它的第一
次觸發資訊價值大於故障價值**：

- 先確認 `api.Contracts` 是否其實已經是最新（SDK 可能自己刷新快取）。若是，這條
  告警是良性的，且代表 poll 本來就是多餘的。
- 看 `contract_update_events_total{action}`：`force` 代表 broker 認定必須更新；
  `check` 代表 broker 期望 client 自己比對 `check_file_ts`。
- 把答案寫回這一節，並據此決定是否需要真正的重載策略。

目前唯一的補救手段是重啟引擎（登入時會重載合約）；poll 做不到這件事。

---

## 快取完整性

- 快取檔案：`config/contracts.json`
- 寫入方式：原子性（先寫入臨時檔，再 rename）
- 備份建議：每日 cron 備份快取至 `config/contracts.json.bak.{date}`

---

## 相關文件

- 環境變數完整參考：`docs/operations/env-vars-reference.md`
- Feed Gap runbook：`docs/runbooks.md` Section 1
- Shioaji 解耦計畫：`docs/architecture/shioaji-client-resilience-decoupling-plan.md`
