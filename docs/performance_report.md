# HFT Platform Performance Report

> 本文件改為「可重現報告格式」，避免固定數字過期。

## 1. 報告欄位（每次更新必填）
- 測試日期：`YYYY-MM-DD`
- 測試 commit：`<git sha>`
- 測試環境：CPU / RAM / OS / Python 版本
- 測試模式：`sim` / `live-data-order-sim`

## 2. 重現命令

### 2.1 單元與 benchmark
```bash
uv run pytest tests/benchmark --benchmark-only --benchmark-json=benchmark.json
```

### 2.2 Shioaji API latency
```bash
uv run python scripts/latency/shioaji_api_probe.py --mode sim --iters 30
```

### 2.3 E2E latency（ClickHouse）
```bash
uv run python scripts/latency/e2e_clickhouse_report.py --window-min 10
```

## 3. 建議輸出
- `benchmark.json`
- `reports/shioaji_api_latency.json`
- `reports/e2e_latency.summary.json`

## 4. 指標解讀重點
- `event_loop_lag_ms` 是否長時間升高
- `queue_depth` 是否持續堆積
- `shioaji_api_latency_ms` 的 p95/p99 是否偏移
- recorder 是否出現大量 `Insert failed` 或回退 WAL

## 5. 近期觀察模板
```text
- 結論：
- 主要瓶頸：
- 是否可接受上線：
- 需要調整項：
```
