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

## 8) FeatureEngine — 27 LOB 衍生特徵 (v3)

當 `HFT_FEATURE_ENGINE_ENABLED=1`（預設啟用）時，`FeatureEngine` 自動在 `LOBEngine` 後計算共享特徵，策略無需自行計算。預設使用 `lob_shared_v3`（27 features）。

**Schema 版本**：`lob_shared_v1`(16) → `lob_shared_v2`(22) → `lob_shared_v3`(27, default)

**v1 — 8 Stateless + 8 Rolling**：
`best_bid`, `best_ask`, `mid_price_x2`, `spread_scaled`, `bid_depth`, `ask_depth`, `imbalance_ppm`, `microprice_x2`, `ofi_l1_raw`, `ofi_l1_cum`, `ofi_l1_ema8`, `spread_ema8`, `imbalance_ema8_ppm`, `depth_slope_bid`, `depth_slope_ask`, `[reserved]`

**v2 additions** [16-21]：
`ofi_depth_norm_ppm`, `ret_autocov_5s_x1e6`, `tob_survival_ms`, `impact_surprise_x1000`, `deep_depth_momentum_x1000`, `toxicity_ema50_x1000`

**v3 additions** [22-26]：
Multi-window EMA aggregation (5s/30s/300s) for OFI, imbalance, spread

**策略存取方式**：
```python
features = self.ctx.get_feature_tuple(symbol)  # tuple[float, ...] 長度依 schema 版本
# v1 example (16 features):
best_bid, best_ask, mid_price_x2, spread_scaled, \
    bid_depth, ask_depth, imbalance_ppm, microprice_x2, \
    ofi_l1_raw, ofi_l1_cum, ofi_l1_ema8, spread_ema8, \
    imbalance_ema8_ppm, depth_slope_bid, depth_slope_ask, _ = features[:16]
```

**策略 manifest 宣告**（`config/base/strategies.yaml`）：
```yaml
required_feature_set_id: "lob_shared_v3"  # or v1/v2
required_feature_ids:
  - imbalance_ppm
  - ofi_l1_ema8
```

## 9) Feature 相容性預檢
策略上線前先做 feature 兼容檢查：
```bash
uv run hft feature preflight --strategies config/base/strategies.yaml
```

## 10) Alpha 治理（研究到上線）
```bash
uv run hft alpha validate --alpha-id <id> --data <...>
uv run hft alpha promote --alpha-id <id> --owner <owner>
```

## 11) 注意事項
- 不在策略主路徑做阻塞 I/O。
- 觀察 `strategy_latency_ns`、`risk_reject_total`。
- 任何策略改動需同步更新策略配置與測試。
