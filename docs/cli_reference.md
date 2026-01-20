# CLI Reference

CLI 入口為 `python -m hft_platform`（同 `python -m hft_platform.cli`）。

```bash
python -m hft_platform --help
```

## 1. run
啟動主流程（sim/live/replay）。
```bash
python -m hft_platform run sim
python -m hft_platform run live
```

可選參數：
- `--strategy <id>`
- `--strategy-module <module>`
- `--strategy-class <class>`
- `--symbols <list>`

## 2. init
產生 `config/settings.py` + 策略樣板 + 測試樣板。
```bash
python -m hft_platform init --strategy-id my_strategy --symbol 2330
```

## 3. check
驗證設定是否齊全；可輸出有效設定。
```bash
python -m hft_platform check
python -m hft_platform check --export json
```

## 4. wizard
互動式設定工具。
```bash
python -m hft_platform wizard
```

## 5. feed status
檢查 Prometheus 是否可連線（看 feed metrics）。
```bash
python -m hft_platform feed status --port 9090
```

## 6. diag
快速診斷。
```bash
python -m hft_platform diag
```

## 7. strat test
策略 smoke test（不連行情）。
```bash
python -m hft_platform strat test --symbol 2330
```

## 8. backtest convert
將 JSONL 正規化事件轉成 hftbacktest npz。
```bash
python -m hft_platform backtest convert \
  --input events.jsonl \
  --output data.npz \
  --scale 10000
```

## 9. backtest run
```bash
python -m hft_platform backtest run \
  --data data/sample_feed.npz \
  --symbol 2330 \
  --report
```

策略模式（Strategy Adapter）：
```bash
python -m hft_platform backtest run \
  --data data/sample_feed.npz \
  --strategy-module hft_platform.strategies.simple_mm \
  --strategy-class SimpleMarketMaker \
  --strategy-id demo \
  --symbol 2330
```

## 10. config resolve
查詢股票代碼的交易所 (TSE/OTC)。
```bash
python -m hft_platform config resolve 2330 2317 --output config/symbols.yaml
```

## 11. config build
從 `symbols.list` 產生 `symbols.yaml`。
```bash
python -m hft_platform config build --list config/symbols.list --output config/symbols.yaml
```

## 12. config preview
預覽展開後的 symbols 數量與前幾筆。
```bash
python -m hft_platform config preview
```

## 13. config validate
檢查 exchange/tick_size/price_scale、重複代碼、是否可訂閱。
```bash
python -m hft_platform config validate
python -m hft_platform config validate --online
```

## 14. config sync
從券商 API 拉合約快取，並重建 `symbols.yaml`。
```bash
python -m hft_platform config sync
```
