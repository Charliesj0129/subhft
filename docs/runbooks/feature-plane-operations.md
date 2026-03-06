# Feature Plane Operations Runbook

## 概述

Feature Engine 提供 16 個共享 LOB 微結構特徵（8 個無狀態 + 8 個滾動 EMA），供策略與研究環境共用，解決特徵飄移問題。預設關閉，透過 `HFT_FEATURE_ENGINE_ENABLED=1` 啟用。

**狀態**: Operational（Safe Rollout — 預設關閉，含 canary guard automation）
**Feature Set**: `lob_shared_v1`（16 features）

---

## 啟用與關閉

### 啟用（建議先走影子模式）

```bash
# 1. 影子模式：計算但不注入策略（監控期）
HFT_FEATURE_ENGINE_ENABLED=1 \
HFT_FEATURE_PROFILE_ID=shadow_default \
docker compose up -d hft-engine

# 2. 觀察 3–5 分鐘，確認 feature_plane_latency_ns 正常
curl -fsS http://localhost:9090/metrics | rg "feature_plane|feature_quality"

# 3. 切換為 active 模式（注入策略）
HFT_FEATURE_ENGINE_ENABLED=1 \
HFT_FEATURE_PROFILE_ID=lob_shared_v1_default \
docker compose up -d hft-engine
```

### 緊急關閉（回滾）
```bash
docker compose stop hft-engine
# 移除 HFT_FEATURE_ENGINE_ENABLED 或設為 0
docker compose up -d hft-engine
```

---

## CLI 指令

```bash
# 列出所有可用 feature profiles
hft feature profiles --json

# 驗證 profile 設定檔
hft feature validate

# 策略相容性 preflight
hft feature preflight --strategies config/base/strategies.yaml

# Feature canary guard（輸出 pass/warn/fail + recommendation）
make feature-canary-report
# 或
python3 scripts/feature_canary_guard.py --prom-url http://localhost:9091 --window 1h --output-dir outputs/feature_canary
```

---

## 監控指標

### 正常值基準

| 指標 | 正常值 | 告警值 |
|---|---|---|
| `feature_plane_latency_ns[p99]` | < 10,000 ns（10μs） | > 50,000 ns（50μs）→ 熱路徑退化 |
| `feature_plane_updates_total` | 持續遞增（每 tick 更新） | 停滯 → Feature Engine 未正常運作 |
| `feature_quality_flags_total[flag=gap]` | = 0（正常） | > 0 → LOB 資料中斷造成特徵缺口 |
| `feature_quality_flags_total[flag=stale]` | = 0（正常） | > 0 → 特徵計算落後於市場資料 |
| `feature_quality_flags_total[flag=reset]` | 偶發（重連後） | 持續增加 → 連線不穩 |
| `feature_shadow_parity_mismatch_total` | = 0（正常） | > 0 → 影子 vs. active 特徵不一致 |
| `feature_profile_rollout_state` | `2`（active）或 `1`（shadow） | `0`（disabled）→ rollout 被停用 |

### 查詢範例
```bash
curl -fsS http://localhost:9090/metrics | rg "feature_plane|feature_quality|feature_shadow|feature_profile"
```

---

## Feature 說明（16 個 LOB 特徵）

### 無狀態特徵（8 個）
| 索引 | 名稱 | 說明 |
|---|---|---|
| 0 | `mid_price` | (bid+ask)/2 × 10000 |
| 1 | `spread` | ask - bid |
| 2 | `bid_vol_l1` | Level-1 買量 |
| 3 | `ask_vol_l1` | Level-1 賣量 |
| 4 | `imbalance_l1` | (bid-ask)/(bid+ask) |
| 5 | `ofi_l1` | Order Flow Imbalance Level-1 |
| 6 | `depth_slope_bid` | 買側深度斜率 |
| 7 | `depth_slope_ask` | 賣側深度斜率 |

### 滾動 EMA 特徵（8 個，需足夠 warmup ticks）
| 索引 | 名稱 | 說明 |
|---|---|---|
| 8 | `ofi_ema8` | OFI EMA(α=8) |
| 9 | `spread_ema16` | Spread EMA(α=16) |
| 10 | `imbalance_ema16` | Imbalance EMA(α=16) |
| 11–15 | （保留） | 未來擴展 |

---

## 故障排查

### 問題 1：feature_shadow_parity_mismatch_total 升高
**徵兆**: 影子模式計算結果與 active 模式不一致。

**原因**: Feature registry 版本不符（`FEATURE_SET_VERSION` 不一致）。

**處置**:
1. 確認 feature set 版本：
```bash
docker compose logs hft-engine | rg "feature_set_version|FEATURE_SET_VERSION"
```
2. 確認研究端與 runtime 使用相同 registry：`src/hft_platform/feature/registry.py::FEATURE_SET_VERSION`。
3. 若版本不符 → 重新訓練 alpha 或升級 registry 版本。

### 問題 2：feature_plane_latency_ns P99 > 50μs
**徵兆**: Feature 計算熱路徑退化，可能影響策略延遲。

**原因**: EMA 累積計算量過大，或 LOB 深度過大。

**處置**:
1. 確認使用 `get_feature_tuple()` 而非 `compute_all()`（後者逐符號遍歷）。
2. 考慮降低 rolling window（EMA α 值）或減少 active 標的數。
3. 若問題持續 → 停用 Feature Engine（`HFT_FEATURE_ENGINE_ENABLED=0`）。

### 問題 3：feature_quality_flags_total[flag=gap] 持續增加
**徵兆**: LOB 資料中斷造成特徵缺口。

**原因**: Feed Gap → Feature Engine 無法取得完整 LOB 快照。

**處置**: 優先處理 Feed Gap（`docs/runbooks.md` Section 1），特徵缺口為衍生症狀。

---

## Shadow Canary Rollout 步驟

1. **影子期（1–3 天）**: `HFT_FEATURE_PROFILE_ID=shadow_default`，觀察 parity mismatch = 0。
2. **Canary（10% 策略）**: 透過 rollout controller 設定 `state=shadow` → `active`（10%）。
3. **全量啟用**: 確認 latency P99 < 10μs 且 quality flags 全 = 0 後，設 `state=active`。
4. **回滾**: 任何時候 `HFT_FEATURE_ENGINE_ENABLED=0` 即可安全停用。

詳細 canary 決策流程：`docs/runbooks/feature-plane-shadow-canary-runbook.md`。
自動化決策工具：`scripts/feature_canary_guard.py`（報表輸出至 `outputs/feature_canary/`）。

---

## 相關文件

- 架構規格：`docs/architecture/feature-engine-lob-research-unification-spec.md`
- 環境變數：`docs/operations/env-vars-reference.md` Section 8
- Dashboard：`config/monitoring/dashboards/feature_plane_slo.json`
- Alerts：`config/monitoring/alerts/rules.yaml`（Feature Plane canary/parity SLO）
- TODO 追蹤：`docs/TODO.md` Section 1（Feature Plane Unification）
