# Data Retention Policy

**Effective**: 2026-03-02
**Review trigger**: When a new ClickHouse table is created, a new data source is added,
or when disk usage exceeds 60% on the primary drive.

---

## ClickHouse Table Retention

| Table | Retention | Rationale | TTL column | Migration |
|---|---|---|---|---|
| `hft.market_data` | 6 months | LOB replay window; older data superseded by aggregates | `ingest_ts` (ns) | Initial schema |
| `hft.orders` | 1 year | Trading record compliance; position reconciliation lookback | `ingest_ts` (ns) | 20260302_001 |
| `hft.trades` | 1 year | Fill reconciliation; broker confirmation matching | `match_ts` (ns) | 20260302_001 |
| `hft.fills` | 1 year | Fee/tax audit; P&L attribution | `ts_exchange` (ns) | 20260302_001 |
| `hft.backtest_runs` | 90 days | Research artifact; superseded by promoted scorecard | `created_at` (DateTime) | 20260302_001 |
| `hft.backtest_timeseries` | 30 days | Timeseries for active backtest analysis only | `ts` (DateTime64) | Initial schema |
| `hft.ohlcv_1m` | No TTL | Pre-aggregated OHLCV; small, valuable for charting | — | Consider 2-year TTL when >10 GB |
| `hft.latency_stats_1m` | No TTL | Pre-aggregated latency; small | — | Consider 1-year TTL when >5 GB |
| `hft.latency_spans` | No TTL | Sampled spans; may grow under high trace volume | — | Consider 90-day TTL |
| `audit.orders_log` | 2 years | Financial audit trail; minimum regulatory requirement | `ts` (ns) | 20260302_001 |
| `audit.risk_log` | 2 years | Risk decision audit; compliance | `ts` (ns) | 20260302_001 |
| `audit.guardrail_log` | 2 years | StormGuard state transitions; compliance | `ts` (ns) | 20260302_001 |
| `audit.alpha_gate_log` | 2 years | Alpha governance audit; required for Gate E re-evaluation | `ts` (ns) | 20260302_001 |
| `audit.alpha_promotion_log` | 2 years | Promotion decisions; compliance | `ts` (ns) | 20260302_001 |
| `audit.alpha_canary_log` | 2 years | Canary actions; post-incident root cause | `ts` (ns) | 20260302_001 |
| `system.trace_log` | 3 days | CK internal; root cause of 2026-03-02 disk crisis | — | config/clickhouse_system_logs.xml |
| `system.text_log` | 3 days (Warning+) | CK internal debug logs | — | config/clickhouse_system_logs.xml |
| `system.query_log` | 7 days | Query performance audit | — | config/clickhouse_system_logs.xml |
| `system.part_log` | 7 days | Merge/mutation audit | — | config/clickhouse_system_logs.xml |

### How to Apply Migrations

```bash
# Apply TTL migration to local ClickHouse
docker exec clickhouse clickhouse-client \
  < src/hft_platform/migrations/clickhouse/20260302_001_add_ttl_policies.sql

# Apply to remote machine
rsync -avz src/hft_platform/migrations/ \
  charl@100.91.176.126:/home/charl/subhft/src/hft_platform/migrations/

ssh charl@100.91.176.126 \
  'cd /home/charl/subhft && docker exec clickhouse clickhouse-client \
   < src/hft_platform/migrations/clickhouse/20260302_001_add_ttl_policies.sql'
```

### Verify TTLs Are Active

```sql
-- Run in ClickHouse to confirm all tables have TTL clauses
SELECT
    database,
    name AS table,
    extract(engine_full, 'TTL [^\\n]+') AS ttl_clause
FROM system.tables
WHERE database IN ('hft', 'audit')
  AND engine_full LIKE '%TTL%'
ORDER BY database, name;
```

ClickHouse applies TTL asynchronously during merges. To force immediate cleanup:

```sql
-- Force TTL enforcement on a specific table (use with caution on large tables)
OPTIMIZE TABLE hft.orders FINAL;
```

---

## WAL Archive Retention

| Setting | Value | Rationale |
|---|---|---|
| Retention window | 7 days | Covers one full trading week for replay/debugging |
| Cleanup method | Weekly cron (Sunday 02:00) | See `docs/operations/cron-setup-remote.md` |
| Local cleanup | `make wal-archive-cleanup` | Prompts for confirmation; configurable via `WAL_KEEP_DAYS` |
| Estimated disk cost at 7 days | ~217 GB/year equivalent if never cleaned | Cron reduces to near-zero |

WAL files are safe to delete after they have been successfully loaded into ClickHouse.
The `wal-loader` service moves files to `.wal/archive/` only after confirmed CK insertion.

---

## Research Data Rotation Strategy

Research data in `research/data/` is the largest uncontrolled disk consumer (~44 GB baseline,
growing ~44 GB per major research round). The current strategy is manual:

### Classification

| Path pattern | Type | Retention guidance |
|---|---|---|
| `research/data/processed/<alpha_id>/` | Training/validation datasets | Keep while alpha is active (GATE_B or above) |
| `research/data/raw/` | Raw market data snapshots | Keep for 90 days; archive to cold storage |
| `research/data/synthetic/` | Synthetic test datasets | Keep the latest version; delete older versions |
| `research/experiments/runs/` | Experiment run outputs | Keep for 90 days; scorecard promoted to CK |

### MVP 發行最小樣本（release-converge-mvp）

為了讓第一個可發行版本可持續運作且可重現，`research/data` 採「最小 smoke 樣本」策略：

- 必留檔案（tracked）：
  - `research/data/processed/smoke/smoke_v1.npy`
  - `research/data/processed/smoke/smoke_v1.npy.meta.json`
- 其餘 `research/data/` 內容視為可重建產物，可由 `release-converge-mvp` 清理後重建骨架。
- 樣本重建命令（與收斂流程一致）：
  ```bash
  .venv/bin/python research/tools/synth_lob_gen.py \
    --out research/data/processed/smoke/smoke_v1.npy \
    --version v2 --n-rows 512 --rng-seed 7 \
    --owner release --symbols TXF --split smoke --generator-version smoke_v1
  .venv/bin/python -m research validate-data-meta research/data/processed/smoke/smoke_v1.npy
  ```

### Quarterly Review Procedure

1. List research data by size and age:
   ```bash
   du -sh research/data/*/ | sort -rh
   find research/data/ -name "*.npy" -mtime +90 | head -20
   ```

2. For alphas in `DRAFT` or `REJECTED` status, archive their data:
   ```bash
   tar -czf /tmp/alpha_archive_$(date +%Y%m%d).tar.gz research/alphas/<rejected_alpha>/
   ```

3. Delete synthetic datasets older than the current version (keep only `*_v<latest>.*`).

4. Archive old experiment runs to cold storage if available.

### Future Automation (TODO)

A future improvement is to tie research data lifecycle to alpha status:
- When an alpha is promoted past Gate E → archive raw training data (keep only processed)
- When an alpha is retired/rejected → delete all experiment runs, keep only scorecard
- Implement via `research/factory.py` `archive` subcommand
- Integrate with `scripts/release_converge.py --cleanup-profile mvp_release` lifecycle hooks

---

## Prometheus Metrics Retention

| Setting | Value | Storage estimate |
|---|---|---|
| Retention time | 30 days | ~6 GB/year |
| Storage path | `/prometheus` (Docker volume) | — |
| Command flag | `--storage.tsdb.retention.time=30d` | docker-compose.yml |

30 days provides sufficient window for monthly operational reviews and covers most
post-incident forensic needs. If long-term trend analysis is needed (>30 days),
configure Prometheus remote write to a dedicated time-series store.

---

## Docker Log Rotation

All services now have explicit log rotation configured. Summary:

| Container | Max size | Max files | Max total |
|---|---|---|---|
| `hft-engine` | 100 MB | 10 | 1 GB |
| `clickhouse` | 100 MB | 10 | 1 GB |
| `redis` | 50 MB | 5 | 250 MB |
| `prometheus` | 50 MB | 5 | 250 MB |
| `alertmanager` | 50 MB | 5 | 250 MB |
| `grafana` | 50 MB | 5 | 250 MB |
| `node-exporter` | 20 MB | 3 | 60 MB |

Total Docker log ceiling: ~2.8 GB. Docker logs are stored in
`/var/lib/docker/containers/<id>/<id>-json.log`.

---

## Policy Revision History

| Date | Change | Author |
|---|---|---|
| 2026-03-02 | Initial policy created after disk crisis post-mortem | claude-sonnet-4-6 |
