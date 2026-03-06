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
| `none`（預設） | 僅重載 symbols/routes，不重新訂閱行情 |
| `diff` | 若 contract diff 有變動（新增/移除），重新訂閱受影響標的 |
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
4. 長期防護：將 `HFT_CONTRACT_REFRESH_RESUBSCRIBE_POLICY` 設為 `none`（保守模式）。

### 模式 3：Thread 鎖死（skipped_locked 持續累積）
**徵兆**: `contract_refresh_total[result=skipped_locked]` 持續遞增，無 `ok` 記錄。

**原因**: Refresh lock 未正常釋放（前次 refresh 執行緒崩潰）。

**處置**:
```bash
docker compose restart hft-engine
```

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
