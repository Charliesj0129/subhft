# 10 分鐘上手 – HFT Platform

1) **準備環境變數**  
   ```bash
   cp .env.example .env
   # 填入 SHIOAJI_API_KEY/SHIOAJI_SECRET_KEY；若先模擬可留空
   ```

2) **一鍵啟動（模擬）**  
   ```bash
   make run-sim
   ```  
   （或 `make run-live` 若已設 SHIOAJI_*）啟動後會輸出模式、symbols、憑證來源、風控/速率閾值，以及 Prometheus `:9090`。

2) **產生設定與策略樣板**  
   ```bash
   python -m hft_platform init --strategy-id my_alpha --symbol 2330
   ```  
   會生成 `config/settings.py`、策略檔與最小單元測試。

3) **驗證設定 / 導出給運維**  
   ```bash
   python -m hft_platform check --export json
   ```
   驗證必填鍵，並導出實際生效的設定。

4) **策略快測（錄製或合成資料）**  
   ```bash
   python -m hft_platform strat test --strategy-id my_alpha
   ```
   不依賴 Shioaji，直接用合成 LOB 特徵煙霧測試。

5) **觀察性**  
   - Metrics: http://localhost:9090/metrics  
   - 快速診斷：`python -m hft_platform feed status` / `python -m hft_platform diag`

6) **升級到 live**  
- 在 `.env` 或環境變數填 `SHIOAJI_API_KEY` / `SHIOAJI_SECRET_KEY`。  
- 若要啟用 CA，需加上 `SHIOAJI_PERSON_ID` 與 `CA_CERT_PATH`/`CA_PASSWORD`。  
- `make run-live`（或直接 `python -m hft_platform run live ...`），若憑證缺失會自動降級並提示。

常見陷阱：  
- 未安裝 Shioaji：系統會自動切模擬並提示。  
- YAML 可選：優先使用 CLI / `settings.py` / JSON。  
- 策略未載入：確認 `settings.py` 的 strategy.module/class/id，或在啟動輸出查詢。
