# Code Review Team — Benchmarks

每次調整 `/code-review-team` 的 prompt 後，跑以下 4 個場景驗證品質。
合格線: 4 項中至少 3 項 PASS。

## Benchmark 1: 三維度覆蓋完整性

**指令**:
```
/code-review-team staged changes
```

**前置**: 確保有已修改的檔案 (git diff 非空)

**PASS 條件**: 三份報告各自包含 ≥1 個發現，且不重疊

## Benchmark 2: 嚴重度排序正確

**前置**: 在 src/hft_platform/order/adapter.py 中注入:
```python
API_KEY = "sk-test-12345678"  # 任意位置
price = 100.5  # 用 float 而非 scaled int
```

**指令**:
```
/code-review-team staged changes
```

**PASS 條件**: 兩者都被標為 CRITICAL，排在報告最前面

**清理**: 注入後記得 revert！`git checkout src/hft_platform/order/adapter.py`

## Benchmark 3: Team Lead 用 Skill 修復

**指令**:
```
/code-review-team staged changes
```
（審查後告訴 Team Lead 執行修復）

**PASS 條件**: Team Lead 修復時調用了 ≥1 個 skill（/tdd, /simplify, /python-review 等），且修復後用 /code-review 自我驗證

## Benchmark 4: Audit 產出評分

**指令**:
```
/code-review-team audit order adapter
```

**PASS 條件**: 三份報告各自包含 0-100 評分，Team Lead 產出加權總分 (Security 30% + Performance 40% + Correctness 30%)
