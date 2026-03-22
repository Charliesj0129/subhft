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
