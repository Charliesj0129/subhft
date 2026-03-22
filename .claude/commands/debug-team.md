---
description: Launch Debugging agent team — parallel investigation across 3 runtime planes with cross-boundary confrontation
---

# Debugging Team

建立 Debugging team:
症狀: $ARGUMENTS

## Team Structure

Team Lead (Opus): 收集症狀線索，廣播給三個 investigator。
協調跨邊界對質，確保證據交換有 timestamp 對齊。
收斂 root cause 後指派修復（由找到 root cause 的 investigator 執行）。
修復完成後用 /code-review 驗證品質。
你不做調查工作，只協調和驗證。
StormGuard 規則: 當 StormGuard 是疑似 root cause 時，Decision 和 Infra 必須共同調查，不可單方面宣稱「StormGuard 不是我的 plane」。
僵局處理: 對質 3 輪後仍無共識，向我報告雙方立場和證據。

Data Investigator (Opus): 負責 Market Data + Feature plane。
檢查: feed_adapter callbacks（src/hft_platform/feed_adapter/）,
normalizer 輸出, LOB state（src/hft_platform/feed_adapter/lob_engine.py）,
FeatureEngine 計算（src/hft_platform/feature/engine.py）,
RingBufferBus publish（rust_core EventBus）。
排除自己 plane 後，必須向 Decision 和 Infra 提問，附具體證據（timestamp, event 內容, metric 值）。
修復時必須使用 /superpowers:systematic-debugging skill。

Decision Investigator (Opus): 負責 Decision + Execution plane。
檢查: StrategyRunner event 接收（src/hft_platform/strategy/runner.py）,
risk evaluation（src/hft_platform/risk/engine.py）,
OrderAdapter dispatch（src/hft_platform/order/adapter.py）,
circuit breaker state, DLQ entries。
排除自己 plane 後，必須向 Data 和 Infra 提問，附具體證據。
修復時必須使用 /superpowers:systematic-debugging skill。

Infra Investigator (Opus): 負責 Control + Persistence + Observability plane。
檢查: bootstrap service graph（src/hft_platform/services/bootstrap.py）,
queue depths (raw_queue, risk_queue),
recorder/WAL errors（src/hft_platform/recorder/）,
StormGuard FSM transitions（src/hft_platform/risk/storm_guard.py）,
Prometheus metrics（src/hft_platform/observability/）。
排除自己 plane 後，必須向 Data 和 Decision 提問，附具體證據。
修復時必須使用 /superpowers:systematic-debugging skill。

## Rules

1. 三人先獨立調查自己的 plane，然後必須跨邊界對質。
2. 對質時必須附具體證據（timestamp, event 內容, metric 值）。
3. 不能說「我這邊沒問題」不附證據。
4. Team Lead 匯總後向我報告 root cause 和修復方案，等我確認再修。
5. 修復者必須使用 /superpowers:systematic-debugging skill。
6. Team Lead 用 /code-review 驗證修復品質。
