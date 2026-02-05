# 10 分鐘上手 – HFT Platform

這份是「超短版」。完整流程請看 `docs/getting_started.md`。

## 1) 安裝依賴
```bash
uv sync --dev
```

## 2) 建立 `.env`
```bash
cp .env.example .env
```

## 3) 生成 symbols.yaml
```bash
uv run hft config build --list config/symbols.list --output config/symbols.yaml
```

## 4) 啟動模擬
```bash
uv run hft run sim
```

## 5) 驗證
- Metrics: http://localhost:9090/metrics
- Feed 狀態：`uv run hft feed status --port 9090`

## 6) 產生策略骨架（選用）
```bash
uv run hft init --strategy-id my_alpha --symbol 2330
uv run hft strat test --symbol 2330
```

## 7) 進入 live（明確設定）
```bash
export SHIOAJI_API_KEY=...
export SHIOAJI_SECRET_KEY=...
export HFT_MODE=live
uv run hft run live
```

> 缺憑證時系統會自動降級 sim。
