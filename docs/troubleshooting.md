# Troubleshooting

常見問題與快速排查。

## 1) `live` 自動降級成 `sim`
原因：缺 `SHIOAJI_API_KEY` 或 `SHIOAJI_SECRET_KEY`。

```bash
export SHIOAJI_API_KEY=...
export SHIOAJI_SECRET_KEY=...
export HFT_MODE=live
uv run hft run live
```

## 2) Compose 警告 `SHIOAJI_ACCOUNT` 未設定
現象：
- `The "SHIOAJI_ACCOUNT" variable is not set...`

說明：
- 多數情況不影響 sim 或行情訂閱。
- 若你的帳務流程需要指定 account，請在 `.env` 補上 `SHIOAJI_ACCOUNT`。

## 3) 無行情 / LOB 空白
```bash
uv run hft config preview
uv run hft config validate
curl -fsS http://localhost:9090/metrics | rg "feed_events_total|feed_last_event_ts"
```

## 4) Prometheus scrape 失敗（歷史 label 型別問題）
症狀：
- `/metrics` 抓取失敗
- log 出現 `AttributeError ... replace` 類錯誤

檢查：
```bash
curl -fsS http://localhost:9090/metrics >/tmp/metrics.txt
rg -n "AttributeError|Traceback|Catcher|<class" /tmp/metrics.txt
```

期望：
- 應無 traceback。
- `shioaji_api_latency_ms` 的 `op/result` labels 應為字串。

## 5) ClickHouse 連線失敗（DNS）
```bash
docker compose ps clickhouse hft-engine
docker compose logs --tail=200 hft-engine | rg -i "NameResolutionError|clickhouse"
```

修復：
```bash
docker compose up -d clickhouse redis
docker compose restart hft-engine
```

## 6) ClickHouse `MEMORY_LIMIT_EXCEEDED`
```bash
docker compose logs --tail=300 hft-engine | rg -i "MEMORY_LIMIT_EXCEEDED|Insert failed"
```

- 若後續持續出現 `Inserted batch`，代表重試成功。
- 若長時間失敗，需調整 ClickHouse 記憶體/merge 設定。

## 7) Metrics 無法連線
```bash
docker compose ps hft-engine
curl -fsS http://localhost:9090/metrics | head
```

## 8) 下單未送出 / 拒單
- 查 `risk_reject_total`, `order_reject_total`
- 檢查 `config/strategy_limits.yaml`, `config/risk.yaml`, `config/order_adapter.yaml`

## 9) 延遲異常升高
```bash
uv run python scripts/latency/shioaji_api_probe.py --mode sim --iters 30
curl -fsS http://localhost:9090/metrics | rg "event_loop_lag_ms|queue_depth"
```

## 10) 測試不穩定
```bash
uv run pytest -k <keyword> -vv
```
