# HFT Platform – 快速上手與回測指南

本專案提供一條龍流程：市場資料 → LOB → 策略 → 風控 → 下單 / 錄製，同時深度整合 **hftbacktest** 進行回測。本文涵蓋安裝、策略開發、回測與部署。

## 快速開始
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # 填 SHIOAJI_PERSON_ID / SHIOAJI_PASSWORD（先模擬可留空）

# 模擬 / 實盤
make run-sim           # 模擬模式
make run-live          # 實盤（需 SHIOAJI_*）
```
啟動後會輸出：模式、symbols、風控/速率閾值、Prometheus `:9090`。無憑證會自動降級模擬。

## 寫一支策略
基底：`hft_platform.strategy.base.Strategy`，實作 `on_book(ctx, event)` 回傳 `OrderIntent` 列表。
```python
from hft_platform.strategy.base import Strategy, StrategyContext
from hft_platform.contracts.strategy import OrderIntent, IntentType, Side, TIF

class MyStrategy(Strategy):
    def __init__(self, strategy_id: str, symbol: str = "2330"):
        super().__init__(strategy_id)
        self.symbol = symbol

    def on_book(self, ctx: StrategyContext, event: dict):
        if event.get("symbol") != self.symbol:
            return []
        feats = ctx.get_features(self.symbol)
        mid = feats.get("mid_price")
        spread = feats.get("spread", 0)
        if not mid or spread < 500:  # x10000 scale
            return []
        return [ctx.place_order(
            symbol=self.symbol, side=Side.BUY, price=mid - spread/2,
            qty=1, tif=TIF.LIMIT, intent_type=IntentType.NEW
        )]
```
生成樣板：`python -m hft_platform init --strategy-id my_alpha --symbol 2330`

## 回測（hftbacktest 深度整合）
### 內建示例數據
- 已附 `data/sample_feed.npz`（小型 DEPTH/TRADE 範例），可直接跑回測。
- 若有自有事件（JSONL），可轉檔成 npz。

### 轉檔：將內部 JSONL 事件轉成 hftbacktest npz
```bash
python -m hft_platform backtest convert --input data.jsonl --output feed.npz --scale 10000
```

2. 策略回測：
 ```bash
 python -m hft_platform backtest run --data feed.npz \
   --strategy-module hft_platform.strategies.demo_strategy \
   --strategy-class DemoStrategy --strategy-id demo --symbol 2330 \
   --price-scale 10000 --tick-size 0.01 --lot-size 1 --timeout 0
 ```
   - 橋接器會把 hftbacktest 深度事件轉為策略事件/特徵；Intent 轉為 hftbacktest 訂單。
   - NEW 支援下單；CANCEL/AMEND 目前記為未支援並統計。
3. 參考回測（無策略）：`python -m hft_platform backtest run --data feed.npz --tick-size 0.01 --lot-size 1 --record-out result.npz`

摘要輸出：事件數、意圖數、下單成功/拒絕、未支援 intent/TIF、策略錯誤、成交筆數、最終持倉/PnL。

## 錄製行情（Recorder）
- 主流程已包含 RecorderService，會將 bus 事件橋接到 `recorder_queue`，再以 WAL（或啟用時的 ClickHouse）批次寫入。
- 主題：`market_data`、`orders`、`risk_decisions`、`fills` 等。
- 若未開 ClickHouse，WAL 檔寫在 `.wal/` 供後續轉檔/回放。

## 配置與降噪
- 環境變數在 `.env.example`，可覆寫 `HFT_PROM_PORT`、`HFT_CLICKHOUSE_ENABLED` 等。
- ClickHouse 預設 WAL-only；要啟用設 `HFT_CLICKHOUSE_ENABLED=1` 並設定 host/port。
- 無 Shioaji 憑證自動模擬並提示，但不中斷。

## 部署（Azure VM / 容器）
- 參考 `docs/deploy_azure.md`：Makefile + .env + systemd 範例。
- 容器：以 python:3.12-slim 為 base，`pip install -e .`，CMD `python -m hft_platform run live ...`，環境變數帶 SHIOAJI_*，暴露 9090。

## 常見問題
- hftbacktest 未安裝：`hft backtest ...` 會提示安裝並退出。
- 資料為空/格式不符：convert/run 會報錯並停止，請確認 JSONL 或 npz。
- 未支援意圖：CANCEL/AMEND 記為未支援；需要完整撤單/改單可擴充橋接器。

## 目錄速覽
- `src/hft_platform/strategy/*`：策略基底、Runner、Registry
- `src/hft_platform/feed_adapter/*`：資料接入、正規化、LOB
- `src/hft_platform/backtest/*`：hftbacktest 橋接、轉檔、Runner
- `config/*.yaml`：symbols/limits 等配置（可被 CLI/env 覆寫）
- `docs/`：部署與上手指南
