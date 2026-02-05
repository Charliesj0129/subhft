# Strategy Development Guide

本指南說明如何新增策略、事件處理、以及如何與下單/風控串接。

---

## 1) 核心概念
- 策略繼承 `BaseStrategy`
- 事件透過 `handle_event()` 分派到：
  - `on_tick(TickEvent)`
  - `on_book_update(BidAskEvent)`
  - `on_stats(LOBStatsEvent)`
  - `on_fill(FillEvent)`
  - `on_order(OrderEvent)`

事件型別在：`src/hft_platform/events.py`、`src/hft_platform/contracts/execution.py`

---

## 2) 建立策略骨架
```bash
uv run hft init --strategy-id my_strategy --symbol 2330
```

生成檔案：
- `src/hft_platform/strategies/my_strategy.py`
- `tests/test_my_strategy.py`

---

## 3) 最小策略範例
```python
from hft_platform.events import LOBStatsEvent
from hft_platform.strategy.base import BaseStrategy

class Strategy(BaseStrategy):
    def on_stats(self, event: LOBStatsEvent) -> None:
        if event.spread > 5:
            self.buy(event.symbol, event.best_bid, 1)
```

---

## 4) Price Scaling（非常重要）
- 系統內部 **price 使用整數**（scaled int）
- `StrategyContext` 會自動做 `price` → `scaled`

**規則**
- Strategy 可以傳 float，但實際會在 `place_order()` 自動縮放
- 若你自己算 price，請用 `ctx.scale_price(symbol, price)`

---

## 5) StrategyContext
`StrategyContext` 提供以下功能：
- `place_order(...)`：生成 `OrderIntent`
- `scale_price(symbol, price)`
- `positions`：目前策略的持倉

---

## 6) Intent Types & TIF
Intent in `hft_platform.contracts.strategy`：
- `IntentType.NEW`
- `IntentType.CANCEL`
- `IntentType.AMEND`

TIF：
- `TIF.LIMIT`
- `TIF.IOC`
- `TIF.FOK`

---

## 7) 註冊策略

### A) config/base/strategies.yaml
```yaml
strategies:
  - id: maker_01
    module: hft_platform.strategies.my_strategy
    class: Strategy
    enabled: true
    params:
      subscribe_symbols: ["2330"]
```

### B) config/settings.py（本機覆蓋）
```python
def get_settings():
    return {
        "mode": "sim",
        "symbols": ["2330"],
        "strategy": {
            "id": "maker_01",
            "module": "hft_platform.strategies.my_strategy",
            "class": "Strategy",
            "params": {"subscribe_symbols": ["2330"]},
        },
    }
```

---

## 8) 策略測試
```bash
uv run hft strat test --symbol 2330
```

---

## 9) 回測
```bash
uv run hft backtest run \
  --data data/sample_feed.npz \
  --strategy-module hft_platform.strategies.my_strategy \
  --strategy-class Strategy \
  --strategy-id maker_01 \
  --symbol 2330
```

---

## 10) 注意事項
- 不要在策略裡做阻塞 I/O
- 不要在 hot path 使用大量 allocation
- 記錄只用 `structlog`

