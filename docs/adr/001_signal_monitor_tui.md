# ADR 001: Signal Monitor TUI Dashboard

## Status
Accepted (2026-03-17)

## Context

目前的 alpha 信號觀測完全依賴 ad-hoc 查詢（SSH → ClickHouse SQL → 手動計算），缺乏統一的即時監控介面。需求：

1. 即時查看追蹤標的的 alpha 信號（QI、FMD、MM 等已通過 Gate D/E 的 alpha）。
2. 手動操盤輔助 — 快速判斷進出場時機。
3. 隨時可擴展 — 新增 symbol 或 alpha 不需改 code。
4. 輕量 — 不需額外基礎設施，SSH 即可使用。

## Decision

採用 **Textual TUI** (Rich-based 終端應用) 作為 Signal Monitor 的 MVP 前端。

### 選型比較

| 方案 | 優點 | 缺點 | 結論 |
|------|------|------|------|
| **Textual TUI** | SSH 即用、鍵盤快、async 原生、sparkline | 非圖形化 | **採用** |
| Streamlit | 視覺化佳、圖表豐富 | 需瀏覽器、需 port forward | Phase 2 備選 |
| Grafana | 現有基礎設施 | 只吃 Prometheus、加自定義 alpha 複雜 | 不適合 |
| Jupyter | 已有 notebook 支援 | 非即時、不適合監控 | 不適合 |

### 關鍵決策

1. **資料源**: 透過 `clickhouse-connect` (HTTP 8123) polling ClickHouse，間隔 3 秒。
2. **Alpha 載入**: 重用 `AlphaRegistry.discover()` 自動發現機制，YAML 選擇啟用的 alpha。
3. **暖機策略**: 啟動時回放最近 500 tick 暖機 EMA 狀態，之後增量更新。
4. **連線方式**: 支援 SSH tunnel (`localhost:8123`) 或 Tailscale 直連。
5. **位置**: `research/monitor/` — 研究工具，非 production code。

## Consequences

### Pros
- 零基礎設施需求 — 不需要新容器、新服務。
- 與現有 alpha 研究管線完全相容 — 用同一套 `AlphaProtocol` 介面。
- 擴展成本極低 — YAML 加一行即完成。
- 離線回放支援 — `NpyReplaySource` 可用研究數據回放。

### Cons
- Polling 3s 延遲 — 非真即時（手動操盤可接受）。
- 終端限制 — 無法顯示 K 線圖或複雜視覺化（Phase 2 Streamlit 補充）。
- 新增依賴 — `textual` + `humanize`。

### Follow-ups
- Phase 2: Streamlit 備用前端（圖表/K 線）。
- Phase 3: Alert 系統（信號超閾值通知）。
- Phase 4: Signal history logging → 本地 SQLite。
- Phase 6: WebSocket 直接接 hft-engine RingBuffer（真即時）。

## Phase 6.5: Hybrid Live Display (Addendum)

**Status**: Accepted (2026-03-18)

### Context

Phase 1 的 CH polling 延遲 ~2s，不適合需要低延遲信號觀測的場景。Phase 6 (WebSocket) 尚未實作，但平台已有兩條 live data path:
1. **SHM (Shared Memory)** — engine 直接寫入 `ShmSnapshotTable`，延遲 ~μs
2. **Redis Live Cache** — engine 透過 `MonitorLivePublisher` 發布到 Redis，延遲 ~ms

### Decision

採用 **Hybrid Display** 架構：live source（SHM 或 Redis）負責主顯示的即時更新，CH 定期 backfill 提供 sparkline 歷史數據。

#### DataSource Protocol

```
DataSource.poll(cursors) → dict[symbol, list[RowView]]
DataSource.fetch_recent_valid(symbol, limit) → list[RowView]
```

四種 DataSource 實作:
| 實作 | 即時源 | 歷史源 | `data_source` 設定 |
|------|--------|--------|-------------------|
| `CHDataSource` | CH | CH | `ch` |
| `ShmDataSource` | SHM | — | `shm` |
| `HybridDataSource` | SHM | CH (backfill) | `auto` |
| `RedisHybridSource` | Redis | CH (backfill) | `auto` + `source: hybrid` |

#### Fallback Chain

```
SHM+CH → SHM → CH → disconnected
Redis+CH → Redis → CH → disconnected
```

### Consequences

- 監控延遲從 ~2s (CH polling) 降至 ~μs (SHM) 或 ~ms (Redis)。
- Sparkline 歷史數據透過定期 CH backfill 保留（預設 30 秒間隔）。
- 完全向後相容 — `data_source: ch` 行為不變。
- 新增 `HFT_MONITOR_DATA_SOURCE` 和 `HFT_MONITOR_HYBRID_BACKFILL_INTERVAL_S` 環境變數。
