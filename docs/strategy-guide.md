# Strategy Development Guide

本指南說明策略開發、註冊、測試與治理流程。

## 1) 核心概念
- 策略繼承 `BaseStrategy`。
- `handle_event()` 會分派至 `on_tick` / `on_book_update` / `on_stats` / `on_fill` / `on_order`。
- 策略輸出為 `OrderIntent`，再進入 Risk/Order。

## 2) 產生策略骨架
```bash
uv run hft init --strategy-id my_strategy --symbol 2330
```

## 3) 最小範例
```python
from hft_platform.events import LOBStatsEvent
from hft_platform.strategy.base import BaseStrategy

class Strategy(BaseStrategy):
    def on_stats(self, event: LOBStatsEvent) -> None:
        if event.spread > 5:
            self.buy(event.symbol, event.best_bid, 1)
```

## 4) 價格與精度規範
- 交易路徑價格應使用 scaled int。
- `StrategyContext` 會協助縮放；避免在熱路徑用浮點做會計。

## 5) StrategyContext 能力
- `buy/sell/place_order`
- `positions`
- `price_scaler`

## 6) 註冊策略
`config/base/strategies.yaml`：
```yaml
strategies:
  - id: my_strategy
    module: hft_platform.strategies.my_strategy
    class: Strategy
    enabled: true
```

## 7) 測試
```bash
uv run hft strat test --symbol 2330
uv run pytest tests/unit -k strategy
```

## 8) Feature 相容性預檢
策略上線前先做 feature 兼容檢查：
```bash
uv run hft feature preflight --strategies config/base/strategies.yaml
```

## 9) Alpha 治理（研究到上線）
```bash
uv run hft alpha validate --alpha-id <id> --data <...>
uv run hft alpha promote --alpha-id <id> --owner <owner>
```

## 10) 注意事項
- 不在策略主路徑做阻塞 I/O。
- 觀察 `strategy_latency_ns`、`risk_reject_total`。
- 任何策略改動需同步更新策略配置與測試。
