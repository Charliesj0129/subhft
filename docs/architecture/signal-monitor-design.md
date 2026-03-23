# Signal Monitor Dashboard — 技術設計文件

> ADR: `docs/adr/001_signal_monitor_tui.md`
> 狀態: Accepted, 已實作
> 更新日期: 2026-03-19

## 1. 目標

提供一個簡易、可擴展的終端 dashboard，即時顯示追蹤標的的 alpha 信號，輔助手動操盤決策。

### 核心需求

| 需求 | 優先級 | 描述 |
|------|--------|------|
| 即時信號顯示 | P0 | 每 3 秒更新所有追蹤標的的 alpha 信號 |
| YAML 設定 | P0 | 新增 symbol/alpha 只改 YAML，不改 code |
| 暖機機制 | P0 | 啟動時回放歷史 tick，確保 EMA 收斂 |
| 鍵盤操作 | P0 | 選擇標的、展開詳情、暫停更新 |
| 離線回放 | P1 | 用 .npy 檔案回放歷史數據驗證信號 |
| Alert 通知 | P2 | 信號超閾值時發出通知 |

## 2. 架構

### 2.1 模組結構

> **實作位置**: 已從設計階段的 `research/monitor/` 移至正式模組 `src/hft_platform/monitor/`。

```
src/hft_platform/monitor/
├── __init__.py
├── cli.py               # CLI 入口: uv run hft monitor
├── _engine.py           # Alpha 計算引擎
├── _renderer.py         # TUI 渲染引擎
├── _tui.py              # Textual TUI 主應用
├── _config_loader.py    # 設定 YAML loader
├── _data_source.py      # DataSource protocol + 實作（CH/SHM/Hybrid）
├── _ch_poller.py        # ClickHouse 輪詢
├── _redis_poller.py     # Redis 即時資料輪詢
├── _redis_publish.py    # Redis live publisher
├── _redis_wire.py       # Redis wire protocol
├── _alpha_dispatcher.py # Alpha 信號分發
├── _enrichment.py       # 資料豐富化
├── _events.py           # Monitor 內部事件
├── _session.py          # Session 管理
├── _detail_panel.py     # 單一標的詳情面板
└── _types.py            # 型別定義
```

### 2.2 資料流

```
ClickHouse (remote <REMOTE_HOST>)
  hft.market_data
       │
       │  HTTP 8123 (poll every 3s)
       │  SSH tunnel 或 Tailscale 直連
       ▼
  source.py (ClickHouseSource)
       │
       │  latest N ticks per symbol
       ▼
  engine.py (AlphaEngine)
       │  ┌─ warmup(500 ticks) on startup
       │  └─ on_tick() incremental update
       │
       │  alpha signals + composite score
       ▼
  app.py (Textual TUI)
       │
       ├─ signal_table.py  (主表格)
       └─ detail_panel.py  (LOB depth, sparkline)
```

### 2.3 連線方式

```yaml
# 方式 1: SSH tunnel (推薦，安全)
# 先開 tunnel: ssh -L 8123:localhost:8123 ${REMOTE_USER}@${REMOTE_HOST}
source:
  host: localhost
  port: 8123

# 方式 2: Tailscale direct (已有 VPN)
source:
  host: <REMOTE_HOST>   # Set via .env.remote.local
  port: 8123
```

## 3. 模組詳細設計

### 3.1 `config.py` — 設定管理

```python
@dataclass(frozen=True, slots=True)
class SymbolConfig:
    id: str                  # e.g. "MXFC6", "2317"
    name: str                # e.g. "微台指06", "鴻海"
    point_value: int         # 每點價值 (NTD): MXFC6=10, TXFC6=200
    category: str            # "futures" | "stock"

@dataclass(frozen=True, slots=True)
class SourceConfig:
    host: str = "localhost"
    port: int = 8123
    database: str = "hft"
    poll_sec: int = 3
    warmup_ticks: int = 500

@dataclass(frozen=True, slots=True)
class DisplayConfig:
    refresh_sec: int = 3
    signal_strong: float = 0.15   # |signal| > 此值 → 強信號高亮
    signal_weak: float = 0.05     # |signal| < 此值 → 灰色

@dataclass(frozen=True, slots=True)
class MonitorConfig:
    symbols: tuple[SymbolConfig, ...]
    alpha_ids: tuple[str, ...]
    source: SourceConfig
    display: DisplayConfig

    @classmethod
    def from_yaml(cls, path: Path) -> "MonitorConfig": ...
```

### 3.2 `watchlist.yaml` — 追蹤清單

```yaml
symbols:
  - id: MXFC6
    name: 微台指06
    point_value: 10
    category: futures
  - id: TXFC6
    name: 台指期06
    point_value: 200
    category: futures
  - id: "2317"
    name: 鴻海
    point_value: 1000
    category: stock
  - id: "2330"
    name: 台積電
    point_value: 1000
    category: stock
  - id: "2454"
    name: 聯發科
    point_value: 1000
    category: stock
  - id: "2881"
    name: 富邦金
    point_value: 1000
    category: stock

alphas: []
  # 加新的 alpha 只要這裡加一行，會自動從 research/alphas/ 載入

source:
  host: localhost           # SSH tunnel: ssh -L 8123:localhost:8123 ${REMOTE_USER}@${REMOTE_HOST}
  port: 8123
  database: hft
  poll_sec: 3
  warmup_ticks: 500

display:
  refresh_sec: 3
  signal_strong: 0.15
  signal_weak: 0.05
```

### 3.3 `source.py` — 資料源

```python
class DataSource(Protocol):
    """可插拔資料源介面。"""
    async def fetch_latest(self, symbol: str, since_ts: int, limit: int) -> np.ndarray: ...
    async def fetch_warmup(self, symbol: str, n_ticks: int) -> np.ndarray: ...
    async def healthcheck(self) -> bool: ...

class ClickHouseSource:
    """透過 clickhouse-connect HTTP 讀取 hft.market_data。"""
    # 查詢欄位:
    #   exch_ts, price_scaled, volume,
    #   bids_price[1] as bid_px, bids_vol[1] as bid_qty,
    #   asks_price[1] as ask_px, asks_vol[1] as ask_qty
    #
    # 價格轉換: price_scaled / 1_000_000 = NTD
    # 時間戳: exch_ts (nanoseconds since epoch)
    #
    # 注意: 只讀取有 LOB 深度的 tick (length(bids_price) > 0)

class NpyReplaySource:
    """離線回放 .npy 檔案。

    用法: 指向 research/data/raw/{symbol}/ 目錄，
    自動載入最新日期的 .npy 檔，模擬即時 tick 流。
    支援加速/減速回放。
    """
```

**返回格式**: 與現有 `ch_batch_export.py` 的 L1 schema 一致:

| Field | Type | 說明 |
|-------|------|------|
| `bid_px` | float | best bid price (NTD) |
| `ask_px` | float | best ask price (NTD) |
| `bid_qty` | float | best bid quantity |
| `ask_qty` | float | best ask quantity |
| `mid_price` | float | (bid + ask) / 2 |
| `spread_bps` | float | spread in basis points |
| `volume` | float | trade volume |
| `local_ts` | int64 | nanosecond timestamp |

### 3.4 `engine.py` — Alpha 計算引擎

```python
class AlphaEngine:
    """管理多 symbol × 多 alpha 的信號計算。"""

    def __init__(self, alpha_ids: list[str]):
        # 用 AlphaRegistry.discover() 載入指定的 alpha
        # 每個 (symbol, alpha_id) 組合維護獨立的 alpha instance

    def warmup(self, symbol: str, data: np.ndarray) -> None:
        """回放歷史 tick 暖機 EMA 狀態。

        重用 alpha_strategy_bridge.py 的 payload 建構邏輯:
        1. 建構 full payload dict (bid_qty, ask_qty, bid_px, ask_px,
           mid_price, microprice_x2, spread_scaled, ...)
        2. 嘗試 alpha.update(**payload)
        3. fallback: alpha.update(bid_qty=..., ask_qty=...)
        """

    def on_tick(self, symbol: str, tick: dict) -> dict[str, float]:
        """一筆新 tick → 返回所有 alpha 的最新信號。

        Returns:
            {"alpha_id_1": 0.23, "alpha_id_2": -0.01, ...}
        """

    def get_composite(self, symbol: str) -> float:
        """加權組合信號。

        權重來源: config/strategy_promotions/ 中的 weight 欄位。
        預設: 已核可 alpha 等權重。
        """

    def get_suggestion(self, symbol: str) -> str:
        """基於 composite 信號的操作建議。

        Returns: "▲ LONG?" | "▼ SHORT?" | "● WAIT" | "○ neutral"
        """
```

**Alpha 狀態管理**:
- 每個 `(symbol, alpha_id)` 對應一個獨立的 alpha instance
- Alpha instance 在啟動時建立，warmup 後保持狀態
- 新增 symbol 時自動建立對應的 alpha instances

**Payload 建構** (與 `alpha_strategy_bridge.py` 一致):

```python
def _build_payload(tick: dict) -> dict:
    bid_px = tick["bid_px"]
    ask_px = tick["ask_px"]
    bid_qty = tick["bid_qty"]
    ask_qty = tick["ask_qty"]
    total_qty = bid_qty + ask_qty + 1e-8

    return {
        "bid_px": bid_px,
        "ask_px": ask_px,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "mid_price": (bid_px + ask_px) / 2,
        "microprice_x2": (bid_px * ask_qty + ask_px * bid_qty) / total_qty * 2 * 10000,
        "spread_scaled": (ask_px - bid_px) * 10000,
        "imbalance": (bid_qty - ask_qty) / total_qty,
        "volume": tick.get("volume", 0.0),
        "local_ts": tick.get("local_ts", 0),
    }
```

### 3.5 `app.py` — TUI 畫面

#### 主畫面布局

```
┌─ Signal Monitor ──────────────────── 2026-03-17 21:03:15 TST ─── Poll: 3s ─┐
│                                                                              │
│  Symbol   Price    Chg%   │  A1     A2      A3     │ Combo  │ Suggestion   │
│  ──────── ──────── ────── │ ──────  ──────  ──────  │ ─────  │ ──────────   │
│▸ MXFC6    33,976   +0.3%  │ -0.29   -0.01   +0.09  │ -0.12  │ ● WAIT       │
│  TXFC6    33,961   +0.8%  │ -0.25   -0.01   +0.07  │ -0.10  │ ● WAIT       │
│  2317       212    -2.1%  │ +0.45   +0.03   +0.02  │ +0.22  │ ▲ LONG?      │
│  2330     1,870    +1.4%  │ +0.12   +0.02   +0.01  │ +0.07  │ ○ neutral    │
│  2454     1,730    +1.2%  │ -0.08   +0.01   -0.02  │ -0.04  │ ○ neutral    │
│  2881        90    +1.1%  │ +0.15   +0.01   +0.03  │ +0.09  │ ○ neutral    │
│                                                                              │
├─ Detail: MXFC6 ──────────────────────────────────────────────────────────────┤
│  Bid: 33,975 (6)  │  Ask: 33,977 (2)  │  Spread: 2  │  Imb: +0.50         │
│                                                                              │
│  A1  ▁▂▃▄▅▆▇█▇▆▅▄▃▂▁▁▂▃▄▅▃▂▁  (last 30 polls sparkline)                  │
│  A2  ▄▄▄▃▃▃▂▂▂▂▂▂▂▂▃▃▃▃▃▂▂▂▂  (stable, near zero)                        │
│  A3  ▅▅▅▆▆▆▆▅▅▅▄▄▃▃▃▃▃▃▄▄▅▅▅  (trending up)                              │
│                                                                              │
│  Point value: 10 NTD │ Session: NIGHT │ Ticks: 45,231 │ Last: 0.3s ago     │
└── [q]uit  [r]efresh  [↑↓]select  [a]dd  [d]etail  [p]ause  [s]napshot ─────┘
```

#### 色彩規則

| 條件 | 顏色 | 意義 |
|------|------|------|
| signal > +signal_strong | 綠色 (green) | 強看多 |
| signal > +signal_weak | 淺綠 (bright_green) | 弱看多 |
| \|signal\| <= signal_weak | 灰色 (dim) | 中性 |
| signal < -signal_weak | 淺紅 (bright_red) | 弱看空 |
| signal < -signal_strong | 紅色 (red) | 強看空 |

#### 快捷鍵

| 鍵 | 功能 |
|----|------|
| `q` / `Ctrl+C` | 退出 |
| `↑` / `↓` | 選擇標的 |
| `Enter` / `d` | 展開/收起詳細面板 |
| `a` | 新增追蹤標的 (輸入 symbol ID) |
| `x` | 移除當前標的 |
| `p` | 暫停/繼續自動更新 |
| `r` | 立即刷新 |
| `s` | 匯出當前快照到 JSON (timestamp-named) |
| `1`–`9` | 按第 N 個 alpha 排序 |
| `c` | 按 composite 排序 |
| `?` | 顯示幫助 |

### 3.6 `__main__.py` — 入口

```bash
# 啟動 (預設用 watchlist.yaml)
uv run python -m research.monitor

# 指定設定檔
uv run python -m research.monitor --config path/to/custom.yaml

# 離線回放模式
uv run python -m research.monitor --replay research/data/raw/txfc6/ --speed 10x

# 只看期貨
uv run python -m research.monitor --category futures
```

## 4. 擴展設計

### 4.1 新增 Symbol

編輯 `watchlist.yaml`:

```yaml
symbols:
  # ... existing ...
  - id: "2603"
    name: 長榮
    point_value: 1000
    category: stock
```

重啟即生效（或按 `r` 重新載入設定）。

### 4.2 新增 Alpha

1. 在 `research/alphas/{new_alpha}/impl.py` 實作 `AlphaProtocol`
2. 在 `watchlist.yaml` 加一行:

```yaml
alphas:
  - new_alpha              # 新增
```

自動透過 `AlphaRegistry.discover()` 載入。

### 4.3 新增 DataSource

實作 `DataSource` protocol:

```python
class WebSocketSource:
    """直接從 hft-engine RingBuffer 接收 (Phase 6)。"""
    async def fetch_latest(self, symbol, since_ts, limit): ...
    async def fetch_warmup(self, symbol, n_ticks): ...
    async def healthcheck(self): ...
```

在 `watchlist.yaml` 切換:

```yaml
source:
  type: websocket          # 新增: "clickhouse" | "websocket" | "npy_replay"
  host: localhost
  port: 9090
```

## 5. 依賴

```toml
# pyproject.toml
[project.optional-dependencies]
monitor = [
    "textual>=0.60",
    "humanize>=4.0",
]
```

安裝: `uv sync --extra monitor`

現有依賴 (已在 pyproject.toml):
- `clickhouse-connect` — ClickHouse HTTP client
- `numpy` — 數據處理
- `pyyaml` — YAML 解析

## 6. 實作分階段

| Phase | 內容 | 預估行數 | 依賴 |
|-------|------|----------|------|
| **1 (MVP)** | TUI + 6 symbols + 3 alphas + ClickHouse polling | ~400 行 | textual |
| **2** | NpyReplaySource (離線回放 + 加速) | +100 行 | — |
| **3** | Alert 系統 (信號超閾值 → bell/通知) | +50 行 | — |
| **4** | Signal history → 本地 SQLite | +80 行 | sqlite3 (stdlib) |
| **5** | Streamlit 備用前端 (圖表/K 線) | +200 行 | streamlit |
| **6** | WebSocket 直接接 hft-engine RingBuffer | +150 行 | websockets |

### Phase 1 MVP 驗收標準

- [ ] `uv run python -m research.monitor` 一行啟動
- [ ] 顯示 6 個追蹤標的的即時價格和 3 個 alpha 信號
- [ ] 暖機 500 tick 後信號穩定（無冷啟動偏差）
- [ ] 鍵盤操作: 選擇標的、展開詳情、暫停/恢復
- [ ] YAML 修改後重啟即生效

## 7. 與現有系統的關係

```
                    ┌──────────────────────┐
                    │   hft-engine (prod)  │
                    │   ─────────────────  │
                    │   BrokerFacade       │
                    │   → Normalizer       │
                    │   → LOBEngine        │
                    │   → FeatureEngine    │
                    │   → StrategyRunner   │
                    │   → Recorder ────────┼──→ ClickHouse
                    └──────────────────────┘        │
                                                    │ (read-only)
                    ┌──────────────────────┐        │
                    │  Signal Monitor      │←───────┘
                    │  ─────────────────   │
                    │  source.py (poll)    │
                    │  engine.py (alphas)  │
                    │  app.py (TUI)        │
                    └──────────────────────┘
```

**完全只讀** — Signal Monitor 只讀取 ClickHouse，不寫入任何資料、不影響 hft-engine 運行。
**獨立部署** — 可在本地、遠端、或任何能連到 ClickHouse 的機器上運行。
**共用 Alpha 實作** — 直接 import `research/alphas/` 的 alpha class，確保信號計算一致性。

## 8. 已知限制

1. **Polling 延遲**: 3 秒 (非 tick-level 即時)。手動操盤可接受；高頻策略不應依賴此工具。
2. **ClickHouse 負載**: 每 3 秒 × N symbols 查詢。N=6 時負載可忽略；N>50 需考慮 batch query。
3. **Alpha 簽名不一致**: 102+ 個 alpha 的 `update()` 參數各異，透過 full-payload + fallback 模式相容。
4. **夜盤 Session 辨識**: 需依時間判斷 (15:00~05:00 = night, 08:45~13:30 = day)，目前由 TUI 顯示但不影響信號計算。

## 9. Hybrid Data Source Architecture (Phase 6.5)

### 9.1 DataSource Protocol

所有資料源實作統一的 `DataSource` 協議：

```
┌──────────────────────────────────────────────────────────────┐
│                    DataSource Protocol                       │
│                                                              │
│  connect()                                                   │
│  poll(cursors) → dict[symbol, list[RowView]]                │
│  fetch_recent_valid(symbol, limit) → list[RowView]]          │
│  try_reconnect() → bool                                      │
│  connected: bool                                             │
│  retry_count: int                                            │
│  last_error: str                                             │
│  remaining_backoff_seconds() → float                         │
└──────────────────────────────────────────────────────────────┘
        ▲              ▲               ▲              ▲
        │              │               │              │
  CHDataSource   ShmDataSource   HybridDataSource  RedisHybridSource
   (CH only)     (SHM only)      (SHM + CH)       (Redis + CH)
```

### 9.2 Dual-Source Data Flow

```
hft-engine
  ├─→ Recorder → ClickHouse ──────→ CHDataSource ──→ MonitorEngine
  ├─→ ShmSnapshotWriter ──────────→ ShmDataSource ─→ MonitorEngine
  └─→ MonitorLivePublisher → Redis → RedisPoller ──→ MonitorEngine
                                            │
                               HybridDataSource / RedisHybridSource
                               (live source + periodic CH backfill)
```

### 9.3 Configuration Matrix

| `data_source` | `source` | 實際 DataSource | 即時源 | 歷史源 |
|---------------|----------|----------------|--------|--------|
| `ch` | `clickhouse` | CHDataSource | CH | CH |
| `ch` | `redis` | CHDataSource(RedisPoller) | Redis | Redis |
| `ch` | `hybrid` | RedisHybridSource | Redis | CH |
| `shm` | — | ShmDataSource | SHM | — |
| `auto` | `clickhouse` | HybridDataSource (if SHM ok) else CH | SHM/CH | CH |
| `auto` | `hybrid` | RedisHybridSource | Redis | CH |

### 9.4 Periodic Backfill

Hybrid sources 定期從 CH 拉取歷史數據用於 sparkline 顯示：
- 預設間隔: 30 秒 (`hybrid_backfill_interval_s`)
- Best-effort: CH 失敗不影響 live path
- 環境變數: `HFT_MONITOR_HYBRID_BACKFILL_INTERVAL_S`

### 9.5 Stale Detection

| 來源 | 機制 |
|------|------|
| CH | `ingest_ts` age > `stale_threshold_s` |
| SHM | slot version 停止遞增 (planned: age-based check) |
| Redis | `heartbeat_stale` property (TTL-based) |
