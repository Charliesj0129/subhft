# HFT Platform 功能手冊

本文件針對專案的主要功能模組進行深入說明，包含「目的 / 輸入輸出 / 設定 / 典型流程」。

## 1. 系統資料流總覽
```
行情來源 -> Feed Adapter -> Normalizer -> LOB Engine -> Strategy
                                     \-> EventBus -> Recorder

Strategy -> Risk Engine -> Order Adapter -> Broker
Broker fills -> Execution Normalizer -> Position/Reconciliation
```

## 2. 設定與啟動流程
**目的**：統一讀取 config 與環境變數，組裝系統執行參數。

**核心模組**
- `src/hft_platform/config/loader.py`: 讀取 YAML 設定與可選 `config/settings.py`，並允許 `HFT_*` 環境覆蓋。
- `src/hft_platform/services/bootstrap.py`: 設定 `SYMBOLS_CONFIG` 預設值並建立服務實例。
- `src/hft_platform/main.py`, `src/hft_platform/cli.py`: 入口與 CLI 指令分派。

**關鍵設定**
- `config/symbols.yaml` 或 `SYMBOLS_CONFIG`：交易/訂閱標的。
- `config/base/strategies.yaml`：策略與參數（預設模板）；需要本地覆蓋可用 `config/strategies.yaml`。
- `config/risk.yaml`, `config/strategy_limits.yaml`：風控規則。
- `config/execution.yaml`, `config/order_adapter.yaml`：執行/下單參數。
- `config/base/*`：預設模板，可作為環境與策略的基準。

**典型流程**
1) CLI 解析 mode / strategy / symbols。
2) loader 合併 env + config。
3) bootstrap 建立各服務並開始 run loop。

## 3. 行情接收與正規化
**目的**：連線交易所/券商 API，將原始行情轉為內部事件結構。

**核心模組**
- `src/hft_platform/feed_adapter/shioaji_client.py`: Shioaji 連線/訂閱。
- `src/hft_platform/feed_adapter/normalizer.py`: `SymbolMetadata` + `MarketDataNormalizer`。
- `src/hft_platform/services/market_data.py`: 連線序列、快照初始化、事件分派。

**輸入/輸出**
- 輸入：Shioaji tick/bidask/snapshot 原始 payload。
- 輸出：`TickEvent` / `BidAskEvent` (見 `src/hft_platform/events.py`)。

**價格縮放**
- 來源：`config/symbols.yaml` 中的 `price_scale` 或 `tick_size`。
- 一致性：所有行情與執行價格採用同一縮放規則。

**典型流程**
1) `ShioajiClient.login()`，成功後 `subscribe_basket()`。
2) `MarketDataService._connect_sequence()` 先抓 snapshot。
3) `MarketDataNormalizer.normalize_*()` 轉換為內部事件。
4) 更新 LOB 並發佈到 `RingBufferBus`（`src/hft_platform/engine/event_bus.py`）與 Strategy。

## 4. LOB Engine（委託簿引擎）
**目的**：重建 LOB 狀態並產出 L1/L2 指標。

**核心模組**
- `src/hft_platform/feed_adapter/lob_engine.py`

**輸入/輸出**
- 輸入：`BidAskEvent`（含 snapshot 或增量）。
- 輸出：書本狀態 + 事件統計（如 spread、mid）。

**注意事項**
- 初始化依賴 snapshot。
- 若 LOB 空值，策略應能容忍或等待 warm-up。

## 5. Feature Library（因子特徵）
**目的**：計算可用於策略的市場微結構特徵。

**核心模組**
- `src/hft_platform/features/`：`micro_price.py`, `ofi.py`, `entropy.py`, `fractal.py`, `liquidity.py`

**輸入/輸出**
- 輸入：LOB / Tick。
- 輸出：指標 dict（由策略自行計算或封裝於 factors）。

## 6. Strategy SDK 與執行
**目的**：提供策略基底、路由與上下文，將策略輸出轉為意圖。

**核心模組**
- `src/hft_platform/strategy/base.py`: `BaseStrategy` / `StrategyContext`。
- `src/hft_platform/strategy/runner.py`: 策略執行與事件路由。
- `src/hft_platform/strategy/registry.py`: 策略註冊/載入。
- `src/hft_platform/strategies/simple_mm.py`: 範例策略。

**輸入/輸出**
- 輸入：Tick/BidAsk + Feature。
- 輸出：`OrderIntent`（下單意圖）。

**使用方式**
- 繼承 `BaseStrategy`，實作 `on_tick` / `on_book_update`。
- 透過 `self.buy()` / `self.sell()` 送出意圖。

## 7. Risk Engine（風控）
**目的**：策略意圖在進入執行前做風控檢查。

**核心模組**
- `src/hft_platform/risk/engine.py`: `RiskEngine`。
- `src/hft_platform/risk/validators.py`: `PriceBandValidator`, `MaxNotionalValidator`。
- `src/hft_platform/risk/storm_guard.py`: 風控狀態機。

**輸入/輸出**
- 輸入：`OrderIntent`。
- 輸出：`OrderCommand` 或 reject。

**注意事項**
- `price_scale` 影響 notional 計算。
- StormGuard 可阻擋新單或進入 HALT。

## 8. Execution & Order Management
**目的**：將風控通過的指令送往券商，並處理回報。

**核心模組**
- `src/hft_platform/order/adapter.py`: 下單/改單/撤單。
- `src/hft_platform/order/rate_limiter.py`: 速率限制。
- `src/hft_platform/order/circuit_breaker.py`: 熔斷。
- `src/hft_platform/execution/normalizer.py`: 回報正規化。
- `src/hft_platform/execution/positions.py`: 倉位更新。
- `src/hft_platform/execution/reconciliation.py`: 對帳流程。

**輸入/輸出**
- 輸入：`OrderCommand` / broker 回報。
- 輸出：`OrderEvent` / `FillEvent` / Position 更新。

**關鍵機制**
- `OrderIdResolver` 追蹤 broker order_id 與策略意圖關聯。
- 熔斷防止異常連續失敗造成大量下單。

## 9. Recorder & Storage
**目的**：保存行情/訂單/成交等事件，供回測與稽核。

**核心模組**
- `src/hft_platform/recorder/wal.py`: WAL 寫入。
- `src/hft_platform/recorder/batcher.py`: 批次。
- `src/hft_platform/recorder/writer.py`: ClickHouse 寫入。
- `src/hft_platform/recorder/worker.py`: 背景併發。
- `src/hft_platform/recorder/loader.py`: WAL 回灌。

**設定**
- `HFT_CLICKHOUSE_ENABLED`, `HFT_CLICKHOUSE_HOST`, `HFT_CLICKHOUSE_PORT`。
- 若關閉 ClickHouse，僅保留 WAL。

## 10. Backtest（回測）
**目的**：以歷史或模擬資料測試策略行為。

**核心模組**
- `src/hft_platform/backtest/runner.py`
- `src/hft_platform/backtest/adapter.py`
- `src/hft_platform/backtest/convert.py`
- `src/hft_platform/backtest/reporting.py`

**執行方式**
```bash
python -m hft_platform backtest run \
  --data data/sample_feed.npz \
  --strategy-module hft_platform.strategies.simple_mm \
  --strategy-class SimpleMarketMaker \
  --strategy-id demo \
  --symbol 2330
```

## 11. Observability（可觀測性）
**目的**：提供 Prometheus 指標與系統狀態追蹤。

**核心模組**
- `src/hft_platform/observability/metrics.py`
- `src/hft_platform/utils/metrics.py`

**輸出**
- Prometheus metrics 預設 `:9090`。

## 12. CLI 與開發工具
**CLI 入口**
- `src/hft_platform/cli.py`

**常用命令**
- `make run-sim`
- `make run-prod`
- `make test`
- `make coverage`

**測試標記**
`blackbox`, `regression`, `stress`, `system`, `acceptance`（見 `pyproject.toml`）。

## 13. 相關規格與參考
- `docs/ARCHITECTURE.md`: 系統架構總覽
- `docs/specs/`: 事件與流程規格
- `docs/references/`: 外部 API 參考與案例

## 14. 擴充指南（How to Extend）
本節提供常見擴充點的最小步驟，讓你快速新增功能而不破壞現有流程。

### 14.1 新增策略
1) 在 `src/hft_platform/strategies/` 建立策略檔。
2) 繼承 `BaseStrategy`，實作 `on_tick` 或 `on_book_update`。
3) 在 `config/base/strategies.yaml` 設定預設策略；需要本地覆蓋可建立 `config/strategies.yaml`。

```python
from hft_platform.strategy.base import BaseStrategy
from hft_platform.events import TickEvent

class MyStrategy(BaseStrategy):
    def on_tick(self, event: TickEvent):
        if event.price > 0:
            self.buy(event.symbol, event.price, 1)
```

### 14.2 新增風控規則
1) 在 `src/hft_platform/risk/validators.py` 新增 validator。
2) 在 `RiskEngine` 註冊 validator。
3) 需要參數時，新增至 `config/risk.yaml` 或 `config/strategy_limits.yaml`。

```python
class MaxQtyValidator(RiskValidator):
    def check(self, intent: OrderIntent):
        max_qty = self.defaults.get("max_qty", 100)
        if intent.qty > max_qty:
            return False, "MAX_QTY_EXCEEDED"
        return True, "OK"
```

### 14.3 新增資料來源（行情）
1) 擴充 `ShioajiClient` 或新增新的 client 類別。
2) 實作對應的 normalizer，把 payload 轉成 `TickEvent` / `BidAskEvent`。
3) 在 `MarketDataService` 接入新的 event 流。

### 14.4 新增執行路由或 Broker
1) 在 `src/hft_platform/order/adapter.py` 內增加 broker 呼叫分支。
2) 請維持 `OrderCommand` 的資料結構與 deadline 行為。
3) 擴充 `OrderIdResolver` 以支援新 broker id。

### 14.5 新增 Recorder 目的地
1) 在 `src/hft_platform/recorder/writer.py` 實作新的 writer。
2) 在 `recorder/worker.py` 中註冊並初始化。
3) 加上配置與 env（如 `HFT_*`）做切換。

### 14.6 新增 CLI 指令
1) 在 `src/hft_platform/cli.py` 新增子命令。
2) 提供對應的 handler 函數。
3) 若需要測試，新增 `tests/blackbox/` 或 `tests/unit/`。

### 14.7 新增 Metrics
1) 在 `src/hft_platform/observability/metrics.py` 註冊指標。
2) 在服務或模組中呼叫 `metrics.xxx` 更新。
