# Troubleshooting

常見問題與排查方向。

## 1. run live 時自動降級 sim
**原因**：未設定 `SHIOAJI_API_KEY/SHIOAJI_SECRET_KEY`。

**解法**
```bash
export SHIOAJI_API_KEY=...
export SHIOAJI_SECRET_KEY=...
```

## 2. 無行情事件 / LOB 空白
**可能原因**
- `config/symbols.yaml` 設定錯誤
- 交易所沒有該標的
- 尚未抓到 snapshot

**排查**
- 使用 `config resolve` 驗證 exchange
- 看 Prometheus `feed_events_total`

## 3. 價格縮放錯誤
**症狀**：價格變成極大或極小。

**排查**
- 確認 `symbols.yaml` 中 `price_scale` 或 `tick_size`
- 避免不同模組使用不同 scale

## 4. ClickHouse 寫入失敗
**排查**
- 檢查 `HFT_CLICKHOUSE_HOST/PORT`
- `docker ps` 檢查 clickhouse container
- 日誌查看 recorder module

## 5. Prometheus metrics 無法連線
**排查**
- 確認 `HFT_PROM_PORT`
- 確認主程序已啟動
- 透過 `python -m hft_platform feed status` 快速檢查

## 6. 訂單沒有送出
**可能原因**
- 風控拒絕（PriceBand/Notional）
- Circuit Breaker 開啟
- Rate limiter 限速

**排查**
- 觀察 log 中的 `Order Rejected by Risk`
- 檢查 `config/strategy_limits.yaml`

## 7. 測試不穩定（async/timeout）
**排查**
- 使用 `pytest-timeout` 觀察卡住的 test
- 透過 `-k` 單獨跑 test 做隔離

## 8. Backtest 無法啟動
**排查**
- 確保 `--data` 路徑存在
- 使用 `backtest convert` 轉換 JSONL
