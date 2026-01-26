# HFT Low-Latency Deployment Runbook (Azure VM)

This complements `docs/azure_deployment.md` and adds HFT-oriented steps.

## 1) VM & Storage
- VM SKU: use Compute/Storage optimized (e.g., `F4s_v2` for research, `Ls_v3`/`Ds_v5` for live). Enable **Accelerated Networking** and put in a **PPG**.
- OS disk 64GB+; mount a Premium/Ultra data disk for ClickHouse/WAL (e.g., `/mnt/data/clickhouse`). Do **not** store data on the OS disk.

## 2) Host tuning (run as root)
```bash
sudo bash ops/host_tuning.sh
```
Then edit `/etc/default/grub` with the suggested kernel flags and `update-grub && reboot`.

## 3) Container layout (host network + pinning)
- Use the low-latency override:
```bash
CH_DATA_HOT="/mnt/data/clickhouse/hot" \
CH_DATA_COLD="/mnt/data/clickhouse/cold" \
docker compose --project-directory . up -d
```
- Data path override uses ClickHouse hot/cold mounts under `/mnt/data/clickhouse`.

## 4) Data pipeline hygiene
- WAL loader clamps `ingest_ts >= exch_ts` and warns on missing book sides. To backfill historical WAL:
```bash
docker compose stop wal-loader
mv .wal/archive/*.jsonl .wal/
docker compose start wal-loader
```
- 清理本機舊備份：確認雲端/遠端已備份後，刪除不再需要的 `backups/` 部分檔案以釋出空間。

## 5) Monitoring quick checks
- ClickHouse lag window:
```bash
docker exec clickhouse clickhouse-client --query \
  "SELECT count(), min(toDateTime64(exch_ts/1e9,3)), max(toDateTime64(exch_ts/1e9,3)) FROM hft.market_data"
```
- Per-symbol coverage:
```bash
docker exec clickhouse clickhouse-client --query \
  "SELECT symbol, count(), min(toDateTime64(exch_ts/1e9,3)), max(toDateTime64(exch_ts/1e9,3)) \
   FROM hft.market_data GROUP BY symbol ORDER BY count() DESC"
```

## 6) CI/CD alignment
- Build/push images to GHCR; deploy via `docker compose pull && docker compose up -d` with the low-latency override.
- Retire the SSH+pips+nohup path in `.github/workflows/deploy.yml` once images are available.
