# Shioaji Client 穩定性補強與深度解耦計畫

- 狀態: `🟡 Code cutover completed (2026-03-02), canary checks wired (2026-03-04), pending production burn-in`
- 最後更新: `2026-03-04`
- 主要目標:
  - 補齊 `login/reconnect/_trigger_reconnect` 的防爆與 timeout 保護。
  - 讓 quote watchdog 在非交易時段不觸發重連風暴。
  - 將 `hft-base` 從預設 runtime 中隔離（profile 化），避免多 runtime 競爭同一組 Shioaji session。
  - 深度解耦 `src/hft_platform/feed_adapter/shioaji_client.py`，降低單檔責任過重風險。

---

## 1. 背景與問題定義

### 1.1 已觀測事件 (Incident Evidence)

- `2026-02-28`（UTC 約 `21:34`）觀測到 `shioaji-quote-relogin` thread traceback，`token_login` timeout 導致 thread crash。
- `2026-03-02`（UTC 約 `08:32`）新 `hft-engine` 重啟後僅有 snapshot，未見 live quote callback 進入；watchdog 持續進入 `No quote data -> forcing reconnect` 迴圈。
- 同機存在 `hft-engine` 與 `subhft-hft-base-1` 兩個 runtime 容器，存在 session/訂閱競爭與狀態覆寫風險。

### 1.2 根因層級 (Architecture-level)

- `shioaji_client.py` 同時持有:
  - session login/logout/reconnect
  - quote callback routing/watchdog
  - contracts refresh
  - order/account 周邊
  - retry thread/state flags
- 單檔多責任導致:
  - 例外處理面分散且不一致（thread 可直接崩潰）。
  - 狀態機隱含耦合（watchdog / reconnect / callback retry 互相覆寫）。
  - 測試粒度過粗，難以針對單一行為回歸驗證。

---

## 2. 目標與非目標

### 2.1 目標 (Goals)

- 在不改策略邏輯前提下，先做 runtime 韌性補強，避免再次「重連風暴 + thread crash」。
- 明確切分部署角色，確保任一時刻僅一個 live feed runtime 持有 broker session。
- 將 Shioaji adapter 拆為可獨立測試與可分階段遷移的模組。

### 2.2 非目標 (Non-goals)

- 本階段不變更交易策略 alpha 計算邏輯。
- 本階段不重寫 broker SDK 行為，也不替換 Shioaji provider。
- 本階段不一次性大改所有 feed path；採漸進解耦。

---

## 3. Workstream A: P0 防爆與 Timeout 保護

### A1. `login()` 防爆

- TODO:
  - [x] 將第一次 `_do_login(fetch_contract=True)` 與 fallback `_do_login(False)` 全部包進同一層 fail-safe。
  - [x] 若 fallback 仍失敗，回傳結構化失敗（不中斷 caller thread），同步記錄 `login_fail_total`、最後錯誤原因。
  - [x] 增加 login timeout/retry 參數（例如 `HFT_SHIOAJI_LOGIN_TIMEOUT_S`, `HFT_SHIOAJI_LOGIN_RETRY_MAX`）。

- 驗收:
  - [x] 任一 login timeout 不可造成 `shioaji-quote-relogin` thread traceback。
  - [x] `reconnect()` 在 login 失敗時可安全返回 `False`，不留半初始化狀態。

### A2. `reconnect()` 防爆

- TODO:
  - [x] `logout/login/_ensure_callbacks/subscribe_basket` 各步驟隔離 try-catch，失敗時確保 state 回復一致。
  - [x] 明確 `logged_in/subscribed_codes/_pending_quote_resubscribe` 的原子更新順序。
  - [x] 對不可恢復錯誤設 backoff（避免 15s 高頻重連放大）。

- 驗收:
  - [x] 在 broker timeout 注入測試下，`reconnect()` 不拋出未捕捉例外。
  - [x] 重連失敗不會留下「callbacks 已註冊但 session 未建立」狀態。

### A3. `MarketDataService._trigger_reconnect()` 防爆

- TODO:
  - [x] 對 `asyncio.to_thread(self.client.reconnect, ...)` 加上 timeout + exception guard。
  - [x] 失敗時可回到 `DISCONNECTED/RECOVERING` 可重試狀態，不中止 monitor loop。
  - [x] 增加 metric：`feed_reconnect_exception_total`, `feed_reconnect_timeout_total`。

- 驗收:
  - [x] reconnect 連續失敗 1 小時內 monitor loop 不中止。

### A4. Quote watchdog 非交易時段 gating

- TODO:
  - [x] `quote_watchdog` 增加 market calendar + reconnect window 一致檢查。
  - [x] 非交易時段只記錄 health 訊號，不執行 `re-register/reconnect`。
  - [x] 交易開盤前 grace period 可配置，避免開盤初期誤判。

- 驗收:
  - [x] 非交易時段 `No quote data; re-registering callbacks` 與 `forcing reconnect` 應為 0。
  - [x] 交易時段內 watchdog 行為不回退。

### A5. 可觀測性補齊

- TODO:
  - [x] 新增 thread liveness 指標（quote watchdog/relogin/retry worker）。
  - [x] 新增 pending 狀態停滯告警（`pending_quote_resubscribe` 長時間未清）。
  - [x] 新增 structured incident fields（reason, retry_count, last_exception_type）。

---

## 4. Workstream B: 部署解耦 (hft-base profile 化)

### B1. Compose profile 策略

- TODO:
  - [x] `hft-base` 改為 maintenance/profile-only 服務，不預設啟動。
  - [x] 明確 `engine` profile 作為唯一持有 Shioaji session 的 runtime。
  - [x] `monitor`, `wal-loader` 保留，但避免自行觸發 feed login。

### B2. 啟動規範

- TODO:
  - [x] 文件化「單 runtime」啟動命令與健康檢查步驟。
  - [x] 增加 preflight：若檢測到多個 runtime 同時持有 broker creds，啟動時告警。
  - [x] 補齊 stale owner cleanup（login conflict path）與 lease refresh 一致化（`SHIOAJI-OPS-03b`，2026-03-04 code landed）。

### B3. 驗收

- [x] `docker ps` 不再同時存在兩個 feed runtime（例：`hft-engine` + `subhft-hft-base-1`）。
- [x] 單一 runtime 下 callback 流量與 heartbeat 指標正常。

---

## 5. Workstream C: `shioaji_client.py` 深度解耦設計

### C1. 目前責任分佈 (as-is)

- Session: login/logout/token refresh/reconnect/backoff
- Quote: callback registry/dispatch/watchdog/event handling
- Contracts: preflight/refresh/cache
- Order & Account: 下單、帳務查詢
- Global registry + route/cache + metrics

### C2. 目標模組切分 (to-be)

- `feed_adapter/shioaji/session_runtime.py`
  - session lifecycle、token/login/reconnect policy
- `feed_adapter/shioaji/quote_runtime.py`
  - callback registration、watchdog、quote event FSM
- `feed_adapter/shioaji/contracts_runtime.py`
  - contracts cache/refresh/preflight
- `feed_adapter/shioaji/order_gateway.py`
  - 下單與 cancel/modify API
- `feed_adapter/shioaji/account_gateway.py`
  - positions/pnl/balance 查詢
- `feed_adapter/shioaji/router.py`
  - quote code routing 與 registry snapshot
- `feed_adapter/shioaji/facade.py`
  - 保持既有對外 API（向後相容），內部委派上述模組

### C3. 邊界原則

- 所有 thread lifecycle 只允許由對應 runtime module 管理。
- session state 只由 `session_runtime` 寫入，其他模組僅讀取或透過介面請求操作。
- quote watchdog 不直接呼叫底層 login，改透過 session policy interface 發出 intent。
- facade 層不得持有業務邏輯，只做參數轉換與相容性 shim。

### C4. 分階段遷移 (避免一次性高風險)

- Phase 0: Safety Net
  - [x] 補齊回歸測試、契約測試、現行行為快照。
- Phase 1: Pure Types + Config 抽離
  - [x] 將 env parsing、常數、型別抽離。
- Phase 2: Session Runtime 抽離
  - [x] login/reconnect/backoff state machine 抽出。
- Phase 3: Quote Runtime 抽離
  - [x] callback/watchdog/event code handler 抽出。
- Phase 4: Contracts / Order / Account 抽離
  - [x] 以 gateway/runtime 形式拆分，`shioaji_client.py` 改為薄委派 wrapper。
- Phase 5: Facade 收斂
  - [x] `facade.py` 已移除 `__getattr__` passthrough；client 對外舊接口保留相容 wrapper。
  - [x] 進一步縮減 client 內部遺留方法（已降至約 1488 行，達成 < 1500 目標；2026-03-04）。

---

## 6. 測試與上線門檻

### 6.1 必要測試

- [x] unit: login timeout/fallback fail-safe
- [x] unit: reconnect exception safety
- [x] unit: watchdog off-hours gating
- [x] integration: 單 runtime session ownership
- [x] integration: reconnect chaos（token timeout / callback drop / event_12）

### 6.2 Canary 檢核

- [x] `First quote callback` 在交易時段內可觀測到（`feed_first_quote_total`，每日 soak 規則已接線）。
- [x] `No quote data; re-registering callbacks` 不超過警戒閾值（`quote_watchdog_callback_reregister_24h` + `--max-watchdog-callback-reregister`，2026-03-04）。
- [x] `feed_reconnect_total{result="ok"}` 與 `fail/exception` 比例受控（failure ratio 規則已接線）。
- [x] production canary 閾值簽核完成（`SHIOAJI-CANARY-01`，`soak_acceptance.py canary` + weekly cron/runbook）。

---

## 7. 風險與回滾

### 7.1 主要風險

- 解耦過程引入隱性行為差異，造成 callback 路由漏接。
- 狀態機拆分後跨模組同步順序出錯。
- profile 化過程誤關閉必要服務。

### 7.2 回滾策略

- 每一 phase 皆保留 facade shim 與 feature flag，可單獨回切。
- 部署端維持一鍵回到「舊版單檔 client + 單 runtime」模式。
- 指標異常立即停用新 module path，回復上版映像。

---

## 8. TODO 索引 (便於追蹤)

- `SHIOAJI-HARDEN-01..05`: 防爆、timeout、watchdog 與 observability
- `SHIOAJI-OPS-01..02`: compose profile 化與單 runtime 規範
- `SHIOAJI-OPS-03b`: ✅ Redis lease refresh + stale owner cleanup 一致化（login conflict path, code landed 2026-03-04）
- `SHIOAJI-DECOUPLE-01..04`: 深度解耦與階段遷移（code path 已切換）
- `SHIOAJI-DECOUPLE-05`: ✅ client 瘦身（1488 行，<1500）已達成；後續僅保留低風險 shim 清理
- `SHIOAJI-CANARY-01`: ✅ production canary 驗收流程已落地（first quote / reconnect success rate）
- `SHIOAJI-CANARY-02`: ✅ watchdog callback-reregister 閾值驗收已接線（daily/canary/alert/cron 同步）
