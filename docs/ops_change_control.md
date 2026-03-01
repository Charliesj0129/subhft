# Ops Change Control

定義基礎變更管控流程，避免線上風險。

## Scope
- `docker-compose.yml` / `docker-stack.yml`
- `config/` 下會影響 live 行為的變更
- `ops.sh` 與 `scripts/` 中運維腳本
- 監控告警規則變更（Prometheus/Grafana/Alertmanager）

## Workflow
1. 建立變更單：`what / why / risk / rollback`。
2. 至少一位 reviewer 確認。
3. 先在 `sim` 或 staging 驗證。
4. 驗證指標：feed、queue、risk reject、recorder、/metrics scrape。
5. 若異常，5 分鐘內執行 rollback。

## 最小驗證證據
- `docker compose ps`
- `docker compose logs --tail=200 hft-engine`
- `curl -fsS http://localhost:9090/metrics | head`
- `uv run hft recorder status`

## Rollback
- 保留上一版 image/tag。
- 保留前一版 `.env`/config 備份。
- 回滾後重新執行最小驗證證據。
