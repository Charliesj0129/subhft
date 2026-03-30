# HFT Platform 功能手冊

本手冊對齊目前程式碼，說明主要模組、資料流與擴充點。

## 1. 系統資料流

```text
Exchange → BrokerFacade(Shioaji|Fubon) → Normalizer → LOBEngine → FeatureEngine → EventBus → Strategy → Risk → Order → BrokerFacade
                                                                         \→ Recorder (WAL/ClickHouse)
```

## 2. 核心模組
- `src/hft_platform/services/market_data.py`：行情流程協調
- `src/hft_platform/feed_adapter/`：Multi-broker（Shioaji/Fubon）+ normalizer + LOB
- `src/hft_platform/strategy/` + `src/hft_platform/strategies/`：策略 SDK 與策略
- `src/hft_platform/risk/`：風控與 StormGuard
- `src/hft_platform/order/` + `src/hft_platform/execution/`：下單與回報
- `src/hft_platform/recorder/`：WAL/ClickHouse
- `src/hft_platform/observability/`：metrics
- `src/hft_platform/gateway/`：gateway 與 HA/去重/曝險
- `src/hft_platform/feature/`：Feature Plane（profile/rollout/compat）
- `src/hft_platform/alpha/`：研究治理與 promotion/canary
- `src/hft_platform/monitor/`：Live signal monitoring TUI（ClickHouse/Redis 雙資料源）

## 3. 設定與啟動
- 設定來源：`config/base` + `config/env/*` + `config/settings.py` + env + CLI
- 入口：`uv run hft run sim|live`

## 4. Feature Plane 治理流程
```bash
hft feature profiles --json
hft feature validate
hft feature preflight --strategies config/base/strategies.yaml
hft feature rollout-status
```

切換：
```bash
hft feature rollout-set --feature-set <id> --state active --profile-id <pid>
hft feature rollout-rollback --feature-set <id>
```

## 5. Alpha 研究治理流程
```bash
hft alpha scaffold <alpha_id>
hft alpha validate --alpha-id <alpha_id> --data <...>
hft alpha promote --alpha-id <alpha_id> --owner <owner>
hft alpha canary status
```

## 6. Recorder 與資料
- `direct`：直接寫 ClickHouse
- `wal_first`：先寫 WAL，再回灌

檢查：
```bash
hft recorder status
```

## 7. Backtest
```bash
hft backtest convert --input <jsonl> --output <npz>
hft backtest run --data <npz> --symbol 2330 --report
```

## 8. 可觀測性
核心端點：`http://localhost:9090/metrics`

重點指標：
- feed/queue
- strategy/risk
- order/execution
- recorder/clickhouse
- shioaji api latency

## 9. 擴充指引
### 新增策略
1. 建立 `src/hft_platform/strategies/<name>.py`
2. 更新 `config/base/strategies.yaml`
3. 執行 `hft strat test`

### 新增風控
1. 新增/擴充 `src/hft_platform/risk/validators.py`
2. 在 `RiskEngine` 註冊
3. 補 unit test

### 新增 CLI
1. 更新 `src/hft_platform/cli.py`
2. 補 `docs/cli_reference.md`
3. 補對應測試

## 10. Phase 18 FeatureEngine — 27 Features (v3)

`FeatureEngine` 位於 `LOBEngine` 與 `EventBus` 之間，計算 27 個 LOB 衍生微結構特徵（v3 預設）。

### 啟用方式
```bash
HFT_FEATURE_ENGINE_ENABLED=1    # 啟用 FeatureEngine（預設啟用）
HFT_FEATURE_ENGINE_BACKEND=python|rust  # 後端選擇（預設 python）
```

### Schema 版本

| Schema            | Features | Description                                         |
| ----------------- | -------- | --------------------------------------------------- |
| `lob_shared_v1`   | 16       | 8 stateless + 8 rolling (OFI L1, EMA spread/imbalance) |
| `lob_shared_v2`   | 22       | v1 + depth-normalized OFI, return autocovariance, TOB survival, impact surprise, deep depth momentum, trade-signed toxicity |
| `lob_shared_v3`   | 27       | v2 + multi-window EMA aggregation (5s/30s/300s OFI, imbalance, spread) — **default** |

### 特徵清單 (v1 base)

**8 Stateless Features**（無狀態，逐 tick 計算）：
- Spread, Microprice, Imbalance, Mid Price, OFI L1, Bid/Ask Volume Ratio, Trade Intensity, Book Depth

**8 Rolling Features**（滾動窗口）：
- EMA Spread, EMA Imbalance, EMA OFI, EMA Microprice, Rolling VWAP, Rolling Volatility, Rolling Trade Flow, Rolling Depth Pressure

**v2 additions** [16-21]：
- `ofi_depth_norm_ppm`, `ret_autocov_5s_x1e6`, `tob_survival_ms`, `impact_surprise_x1000`, `deep_depth_momentum_x1000`, `toxicity_ema50_x1000`

**v3 additions** [22-26]：
- Multi-window EMA aggregation: 5s/30s/300s windows for OFI, imbalance, spread

### 架構
- Python 實作：`src/hft_platform/feature/engine.py`
- Rust 核心：`RustFeatureEngineV2`、`LobFeatureKernelV1`、`RustFeaturePipelineV1`
- Feature Registry：`src/hft_platform/feature/registry.py`（版本化 feature-set）
- Shadow parity 驗證：`HFT_FEATURE_SHADOW_PARITY=1` 啟用 Python/Rust 比對
