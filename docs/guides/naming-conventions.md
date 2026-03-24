# Naming Conventions

命名規範以一致性與可維運為優先。

## 1) 檔名
- Python 模組：`snake_case.py`
- 文件：`kebab-case.md`
- ADR：`NNN_title.md`

## 2) Python
- 類別：`PascalCase`
- 函式/變數：`snake_case`
- 常數：`UPPER_SNAKE_CASE`
- 私有成員：`_leading_underscore`

## 3) 設定
- YAML key：`snake_case`
- enum-like value：`lower_snake`（如 `sim`, `live`, `wal_first`）
- 環境變數：`HFT_*`, `SHIOAJI_*`

## 4) Metrics / Log
- metric 名稱：`snake_case`，建議帶單位後綴（`_ms`, `_ns`, `_total`）
- label key：`snake_case`
- label value：**必須為字串**（避免 OpenMetrics 序列化錯誤）
- log 欄位：`snake_case`
