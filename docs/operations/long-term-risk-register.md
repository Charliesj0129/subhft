# Long-Term Deployment Risk Register

**Created**: 2026-03-02 (post disk-crisis post-mortem)
**Review trigger**: quarterly, or whenever a new service/table is added
**Audience**: platform operator, on-call engineer

---

## Disk Consumption Annual Forecast

Based on 2026-03-02 exploration data (8-day docker stats, WAL inventory, schema audit).

| Data source | Monthly burn | 12-month projection | Retention policy | Risk |
|---|---|---|---|---|
| CK `hft.market_data` | ~1.3 GB | ~15.7 GB | ✅ 6-month TTL | LOW |
| CK `hft.orders / trades / fills` | ~5–10 GB | ~60–120 GB | ✅ 1-year TTL (added 2026-03-02) | LOW |
| CK `audit.*` (6 tables) | ~2–5 GB | ~24–60 GB | ✅ 2-year TTL (added 2026-03-02) | LOW |
| CK `hft.backtest_runs` | ~1–3 GB | ~12–36 GB | ✅ 90-day TTL (added 2026-03-02) | LOW |
| WAL archive (`.wal/archive/`) | ~25 GB | **~300 GB** | ⚠️ Weekly cron (see cron-setup-remote.md) | MEDIUM |
| `research/data/` (experiment data) | ~44 GB | **~528 GB** | ❌ No rotation — manual review needed | HIGH |
| CK system logs | ~0 GB | ~0 GB | ✅ TTL 3–7 days (config/clickhouse_system_logs.xml) | LOW |
| Docker build cache | ~2 GB | ~24 GB | ⚠️ Monthly cron prune | MEDIUM |
| Prometheus TSDB | ~0.5 GB | ~6 GB | ✅ 30-day retention (updated 2026-03-02) | LOW |
| **Total (conservative)** | **~80–90 GB** | **~970–1050 GB** | — | **HIGH without controls** |

**Action required**: With all TTLs applied and weekly WAL cron in place, monthly burn drops
to ~30–40 GB, giving ~18 months headroom on a 216 GB drive (currently at 28% used ≈ 155 GB
free after the 2026-03-02 cleanup). Research data remains the largest uncontrolled variable.

---

## Risk Register (12 risk categories)

### R01 — WAL Archive Unbounded Growth
- **Severity**: CRITICAL
- **Timeline**: 10 months to exhaust disk if unchecked
- **Cause**: `recorder/wal.py` archives processed WAL files but has no expiry
- **Mitigation**: Weekly cron (see `docs/operations/cron-setup-remote.md`); `make wal-archive-cleanup`
- **Status**: ✅ Cron template documented; `make wal-archive-cleanup` target added

### R02 — ClickHouse Operational Tables No TTL
- **Severity**: HIGH
- **Timeline**: 12 months for 60–120 GB accumulation
- **Cause**: `hft.orders`, `hft.trades`, `hft.fills` created without TTL in initial schema
- **Mitigation**: Migration `20260302_001_add_ttl_policies.sql` adds 1-year TTL to all three
- **Status**: ✅ Migration file created; apply with `docker exec clickhouse clickhouse-client < 20260302_001_add_ttl_policies.sql`

### R03 — Audit Tables No TTL
- **Severity**: HIGH
- **Timeline**: 24 months for 24–60 GB accumulation
- **Cause**: All 6 `audit.*` tables created without TTL
- **Mitigation**: Same migration as R02; 2-year TTL per financial compliance requirements
- **Status**: ✅ Covered in migration `20260302_001_add_ttl_policies.sql`

### R04 — Research Data Unlimited Growth
- **Severity**: HIGH
- **Timeline**: 12–18 months to exhaust disk
- **Cause**: `research/data/` holds NumPy datasets from every experiment; no rotation policy
- **Mitigation**: Establish a 90-day manual review cycle; archive to cold storage or delete
  old experiment data. See `docs/operations/data-retention-policy.md` for details.
- **Status**: ⚠️ Policy documented; enforcement is manual

### R05 — Disk Alerts Without Data Source (no node_exporter)
- **Severity**: HIGH
- **Timeline**: Immediate (alert rules exist but never fire without metrics)
- **Cause**: `HostDiskSpaceCritical` / `HostDiskSpaceWarn` alert rules use
  `node_filesystem_avail_bytes` which requires `node_exporter`; node_exporter was absent
- **Mitigation**: Added `node-exporter` service to `docker-compose.yml` (2026-03-02)
- **Status**: ✅ node_exporter v1.8.2 added; alerts now have a data source

### R06 — hft-engine Logs Unbounded
- **Severity**: MEDIUM-HIGH
- **Timeline**: 3–6 months for 10+ GB accumulation under heavy log volume
- **Cause**: `hft-engine` container had no `logging` section; inherits Docker default (unlimited)
- **Mitigation**: Added `logging: driver: json-file, max-size: 100m, max-file: 10` (2026-03-02)
- **Status**: ✅ Fixed in docker-compose.yml

### R07 — Prometheus Retention Too Short (7 days)
- **Severity**: MEDIUM
- **Timeline**: Post-incident forensics window insufficient for weekly review
- **Cause**: `--storage.tsdb.retention.time=7d` default in docker-compose.yml
- **Mitigation**: Extended to 30 days (2026-03-02); storage cost ~6 GB/year
- **Status**: ✅ Fixed in docker-compose.yml

### R08 — Redis Version Unpinned
- **Severity**: MEDIUM
- **Timeline**: Next `docker compose pull` could upgrade major version silently
- **Cause**: `image: redis:7` resolves to latest 7.x patch, risking unexpected breaking changes
- **Mitigation**: Pinned to `redis:7.2` in docker-compose.yml (2026-03-02)
- **Status**: ✅ Fixed

### R09 — SSD Write Wear (Hardware)
- **Severity**: MEDIUM
- **Timeline**: Pre-TTL write rate was ~2.55 TB/day (CK trace_log). Post-fix: ~50 GB/day
  (rough estimate). At 50 GB/day × 365 = ~18 TB/year. Consumer NVMe TBW: 600–1200 TBW.
  **Estimated SSD life at post-fix rate: 30–65 years.** Risk was acute only during the
  trace_log unbounded-write period; now resolved.
- **Mitigation**: TTL on system logs (done); monitor SMART data for wear indicators
- **Status**: ⚠️ Install `smartmontools` and add weekly SMART cron (see cron-setup-remote.md)

### R10 — Shioaji SDK Version Unpinned
- **Severity**: MEDIUM
- **Timeline**: Next `uv sync` or `pip install -e .` could pull breaking API changes
- **Cause**: `pyproject.toml` specifies `shioaji` without a version upper bound
- **Mitigation**: Pin to `shioaji==X.Y.Z` after validating; document that upgrades require
  re-running latency baseline (`docs/architecture/latency-baseline-shioaji-sim-vs-system.md`)
- **Status**: ⚠️ Action required — check `pyproject.toml`

### R11 — Ubuntu Security Updates Pending
- **Severity**: LOW-MEDIUM
- **Timeline**: 77 packages pending update as of 2026-03-02; kernel pending restart
- **Cause**: 37+ day uptime without OS patching on the remote machine
- **Mitigation**: Run `sudo apt upgrade` + reboot during non-trading hours monthly
- **Status**: ⚠️ Schedule next maintenance window

### R12 — No Config Snapshot at Startup
- **Severity**: LOW
- **Timeline**: Gradual drift over time makes incident recreation harder
- **Cause**: No mechanism to snapshot active config (YAML + env vars) at each startup
- **Mitigation**: Future enhancement — log config hash to ClickHouse at boot
- **Status**: 🔵 Backlog (low urgency)

---

## Hardware Aging Warnings

### SSD Health

- Observed CK Block I/O before TTL fix: **8.52 TB read / 20.4 TB write** (8 days)
- This was primarily driven by `system.trace_log` + `system.text_log` (now TTL'd)
- Post-fix write rate is estimated at <50 GB/day — well within NVMe safe operating range
- Recommendation: install `smartmontools` and run weekly `smartctl -a /dev/sda` via cron

### RAM Stability

- Extended uptime (37+ days) without reboot may allow memory fragmentation to accumulate
- Schedule monthly maintenance reboots during non-trading hours (e.g., Sunday 03:00 TWN)

### Thermal Management

- AB350M-Gaming-3 is 5+ years old; clean CPU/GPU fans annually
- Add `sensors` monitoring if not already present: `sudo apt install lm-sensors`

---

## Review Checklist (Quarterly)

- [ ] Verify ClickHouse TTLs are active: `SELECT database, name, engine_full FROM system.tables WHERE engine_full LIKE '%TTL%'`
- [ ] Check `research/data/` size and archive/delete experiments >90 days old
- [ ] Review WAL archive cron log: `cat /tmp/wal_cleanup.log`
- [ ] Check SSD SMART data: `sudo smartctl -a /dev/sda | grep -i "wear\|power\|error"`
- [ ] Check Prometheus storage usage: `docker exec prometheus du -sh /prometheus`
- [ ] Verify node_exporter is scraping: `curl -s localhost:9091/metrics | grep node_filesystem_avail_bytes | head -3`
- [ ] Review pending OS updates: `apt list --upgradable 2>/dev/null | wc -l`
- [ ] Confirm Shioaji SDK version is still pinned: `uv pip show shioaji | grep Version`
