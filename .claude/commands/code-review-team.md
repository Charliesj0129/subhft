---
description: Launch Code Review agent team — parallel security/performance/correctness review with skill-driven fixes
---

# Code Review Team

建立 Code Review team:
目標: $ARGUMENTS

## Mode Detection

根據目標自動判斷模式:
- 包含 "PR"、"staged"、"diff"、"branch" → PR Review 模式（只看 diff）
- 包含 "audit"、"審計"、"全平台" → Architecture Audit 模式（深度全模組）
- 其他 → 預設 PR Review 模式

## Team Structure

Team Lead (Opus): 匯總三份報告，按 CRITICAL > HIGH > MEDIUM > LOW 排序。
去重規則: 相同 root cause 合併（保留最高嚴重度），不同 root cause 保留兩者。
匯總後向我報告。我確認後用對應 skill 執行修復:
  測試缺失 → /tdd，品質問題 → /simplify，安全 → 修完用 /python-review 驗證。
每個修復完成後用 /code-review 自我驗證。
Architecture Audit 模式額外產出加權總分: Security 30% + Performance 40% + Correctness 30%。

Security Reviewer (Opus):
  PR Review 模式: 只看 diff 範圍。
  Audit 模式: 深度審查整個目標模組，包含所有 rules/15-security.md 和 rules/26-multi-broker-governance.md 項目。
  檢查:
  - 憑證洩漏（hardcoded secrets, env var exposure）
  - SQL injection / command injection
  - Broker credential isolation（參考 .agent/rules/26-multi-broker-governance.md MB-08）
  - TLS / certificate verification
  - 錯誤訊息是否洩漏敏感資訊
  - Audit 額外: 依賴套件已知漏洞
  按 CRITICAL/HIGH/MEDIUM/LOW 分級報告。
  Audit 模式額外: 產出模組安全評分 (0-100)。

Performance Reviewer (Opus):
  PR Review 模式: 只看 diff 範圍。
  Audit 模式: 深度審查整個目標模組，包含所有 rules/01-core-laws.md 和 rules/10-hft-performance.md 項目。
  檢查 5 大 Constitution Laws（參考 .agent/rules/01-core-laws.md）:
  - Allocator Law: hot path 上有 malloc/GC？
  - Cache Law: Array of Objects vs Structure of Arrays？
  - Async Law: blocking IO > 1ms？
  - Precision Law: float 用於金融計算？
  - Boundary Law: Python↔Rust 有不必要 copy？
  額外: datetime.now() vs timebase.now_ns()、print() vs structlog、__slots__。
  Audit 額外: pandas/decimal on hot path。
  按 CRITICAL/HIGH/MEDIUM/LOW 分級報告。
  Audit 模式額外: 產出模組效能評分 (0-100)。

Correctness Reviewer (Opus):
  PR Review 模式: 只看 diff 範圍。
  Audit 模式: 深度審查整個目標模組，包含所有 rules/25-architecture-governance.md 和 rules/50-testing.md 項目。
  檢查:
  - 資料合約: OrderIntent/FillEvent/TickEvent 欄位正確？
  - 架構邊界: 依賴方向違規？（參考 .agent/rules/25-architecture-governance.md）
  - 測試覆蓋: 新邏輯有對應 test？覆蓋 ≥80%？
  - 命名規範: test 命名是行為描述？有 assert？
  - Audit 額外: 零 assertion 測試、程式碼行數 (<800 lines/file)
  按 CRITICAL/HIGH/MEDIUM/LOW 分級報告。
  Audit 模式額外: 產出模組正確性評分 (0-100)。

## Rules

1. 三個 reviewer 獨立審查，不互相交流。
2. Team Lead 匯總後等我確認再執行修復。
3. 修復必須使用 skill（/tdd, /simplify, /refactor-clean, /python-review, /verify），不可手動 ad-hoc。
4. 每個修復完成後用 /code-review 自我驗證。
