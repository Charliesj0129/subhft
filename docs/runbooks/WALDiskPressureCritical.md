# WALDiskPressureCritical

## Symptom

Alert `WALDiskPressureCritical` fired (`disk_pressure_level >= 2` for 30s).

`disk_pressure_level` is published by `DiskPressureMonitor` and reflects the
**total size of regular files at the top level of the WAL directory** — not free
disk space. Levels: 0 OK, 1 WARN (`HFT_WAL_WARN_MB`, default 100), 2 CRITICAL
(`HFT_WAL_CRITICAL_MB`, 300), 3 HALT (`HFT_WAL_HALT_MB`, 500). It recovers with
10% downward hysteresis.

Free-space exhaustion is a **different** condition and fires
`WALDiskCircuitBreakerActive` (`wal_disk_circuit_breaker_active`); check
`wal_disk_available_mb` for that one.

At HALT, `WALFirstWriter.write` returns `False` and **drops rows**, which raises
`recorder_data_loss`. That reason is deliberately non-auto-recoverable, so the
platform latches `PLATFORM_REDUCE_ONLY` + `ManualRearmRequired` until an operator
re-arms.

## Investigation

```bash
# 1. What is actually in the WAL root? (only top-level files count)
du -sh .wal ; ls -la .wal | head -20
find .wal -maxdepth 1 -type f -printf '%s\t%p\n' | sort -rn | head

# 2. Real pending backlog vs. non-WAL junk
ls .wal/*.jsonl 2>/dev/null | wc -l

# 3. Free disk (a separate concern)
df -h . ; curl -s localhost:9090/metrics | grep -E 'wal_disk_available_mb|disk_pressure_level'

# 4. Did it already drop rows?
docker compose logs hft-engine --since 24h | grep -i 'WALFirstWriter HALT'
curl -s localhost:9090/metrics | grep -E 'autonomy_mode|manual_rearm_required|platform_reduce_only_active'
```

Distinguish the two shapes:

- **Genuine backlog** — many `.jsonl` files pending in the WAL root means the
  loader is not draining (ClickHouse down, loader crashed, insert failures). Fix
  the drain; see `ClickHouseConnectionDown.md` / `RecorderFailure.md`.
- **Non-WAL files inflating the sum** — pending count near zero while the root
  still measures hundreds of MB. Look for large non-`.jsonl` files.

### Known cause: WAL loader manifest inflation (2026-07-24)

`.wal/manifest.txt` grew to 4,595,589 lines / 165 MB, and `save_manifest` wrote a
same-size `manifest.txt.bak` plus a temp file **into the same directory**: ~315 MB
steady, ~496 MB during a save, against a 500 MB HALT threshold. Pending `.jsonl`
was 0 and 125 GB of disk was free. It tipped over at
`2026-07-24T05:36:28Z`, dropped 9 `market_data` rows, and left the platform in
reduce-only.

Two contributing defects, both fixed 2026-07-25:

- The manifest was never pruned. Processed files are archived and then deleted
  after `HFT_ARCHIVE_RETENTION_DAYS`, but their names stayed in the manifest
  forever. `prune_manifest` now drops entries absent from both the WAL and
  archive dirs.
- Sidecars (`.bak`, temp) now go in a `manifest.d/` subdirectory, which
  `_wal_dir_size_mb` does not count (it sums maxdepth-1 regular files only).

If you are on a build predating that fix, mitigate without an image rebuild by
moving the manifest out of the WAL root. `HFT_WAL_MANIFEST_PATH` (read at
`recorder/loader.py`) defaults to `<wal_dir>/manifest.txt`; point it at the
already-bind-mounted `.state` volume:

```bash
# append only -- never rewrite .env
cp -p .env .env.bak-$(date +%Y%m%d)
printf '\nHFT_WAL_MANIFEST_PATH=/app/.state/wal_manifest.txt\n' >> .env
```

Retiring the old manifest is safe when `ls .wal/*.jsonl | wc -l` is 0:
`get_new_files` computes `listdir(wal_dir) - manifest`, so an empty manifest
re-processes nothing, and `hft._wal_dedup` makes replay idempotent regardless.
Move rather than delete:

```bash
mv .wal/manifest.txt     ~/wal_manifest_archive_$(date +%Y%m%d).txt
mv .wal/manifest.txt.bak ~/wal_manifest_archive_$(date +%Y%m%d).bak
```

> **`docker compose restart` does not pick up `.env` changes.** Environment is
> fixed when the container is created. Applying a new env var requires
> `docker compose up -d <service>`, which **recreates** the container and
> discards its writable layer — see the recovery sequence below.

## Remediation and re-arm

`recorder_data_loss` is non-auto-recoverable, and
`_restore_manual_rearm_state` re-reads `/app/outputs/production_rollout/autonomy/runtime_state.json`
on boot, so a plain restart **re-latches** reduce-only. Full sequence:

```bash
cd /home/charl/subhft

# 1. Rescue container-only state. On a host that predates the outputs/ bind
#    mount, the engine's CWD is /app and this evidence exists ONLY in the
#    writable layer, which any recreate destroys.
docker cp hft-engine:/app/outputs ./outputs/container_rescue_$(date +%Y%m%d)
docker cp hft-engine:/app/shioaji.log ./outputs/container_rescue_$(date +%Y%m%d)/shioaji.log

# 2. Move aside any misleading HOST-side state file (see daily-ops-checklist).
#    MANDATORY before enabling the outputs/ bind mount: once mounted, the engine
#    reads this file as real state, and the 2026-07-08 test artifact carries
#    reason="test_reason" plus phantom strat1/strat_a manual-rearm latches.
[ -f outputs/production_rollout/autonomy/runtime_state.json ] && \
  mv outputs/production_rollout/autonomy/runtime_state.json \
     outputs/production_rollout/autonomy/runtime_state.json.stale-$(date +%Y%m%d)

# 3a. Preferred — deploy the code fix by restart. src/ IS bind-mounted, so the
#     running code is the host working tree, not the baked image: no rebuild, and
#     a restart preserves the writable layer.
#     Retire the bloated manifest while the loader is DOWN — a running loader
#     rewrites the whole in-memory set on its next save, so moving the files
#     under it achieves nothing.
docker compose stop wal-loader
mkdir -p ~/wal_manifest_archive_$(date +%Y%m%d)
mv .wal/manifest.txt .wal/manifest.txt.bak ~/wal_manifest_archive_$(date +%Y%m%d)/ 2>/dev/null
docker compose exec hft-engine hft ops rearm-platform    # clear the persisted latch FIRST
docker compose restart hft-engine
docker compose start wal-loader

# 3b. Only if the env var (HFT_WAL_MANIFEST_PATH) or a compose change must apply:
#     env is fixed at container CREATION, so `restart` cannot pick it up. This
#     recreates and discards the writable layer — step 1 is not optional here.
docker compose up -d hft-engine wal-loader

# 4. Confirm; only if the latch survived, clear it explicitly and restart once.
docker compose exec hft-engine hft ops autonomy-status
docker compose exec hft-engine hft ops rearm-platform   # only if still latched
```

Why re-arm must come **before** the restart: `docker compose exec` starts a *new*
process inside the container, so the CLI cannot reach the running engine's live
controller. It says so and degrades gracefully —
`manual_rearm_ipc_unreachable … "Persisted to runtime_state.json but live
controller not in this process. Restart hft-engine to apply."` — writing
`manual_rearm_required: false` to the state file only. Re-arming *after* the
restart therefore needs a second restart to take effect (observed 2026-07-26).

Dropping an empty manifest is safe: `get_new_files` returns
`listdir(wal_dir) - manifest`, so with no pending `.jsonl` files an empty manifest
re-processes nothing, and `hft._wal_dedup` makes replay idempotent regardless.

## Verification

```bash
curl -s localhost:9090/metrics | grep -E 'autonomy_mode|manual_rearm_required|disk_pressure_level|wal_disk_available_mb'
# expect autonomy_mode{scope="platform"} 0.0, manual_rearm_required 0.0, disk_pressure_level 0.0

find .wal -maxdepth 1 -type f -printf '%s\t%p\n' | sort -rn | head   # no large non-.jsonl files
docker compose exec wal-loader sh -c 'ls -la /app/.state/wal_manifest.txt /app/.state/manifest.d/ 2>&1'
docker compose logs hft-engine --since 30m | grep -iE 'DiskPressure|WALFirstWriter HALT'   # expect nothing
curl -s localhost:9093/api/v2/alerts | python3 -c 'import sys,json;[print(a["labels"]["alertname"]) for a in json.load(sys.stdin)]'
```

## Escalation

Escalate if `disk_pressure_level` stays >= 2 for 15 minutes after remediation, or
if `WALFirstWriter HALT` recurs — recurrence means rows are being dropped and the
recorded corpus has holes. Cross-check ingested rows for the affected window in
ClickHouse before declaring the incident closed.

## Related

- `docs/runbooks/recorder-wal-disk-pressure.md`
- `docs/runbooks/disk-crisis-sop.md`, `docs/runbooks/HostDiskSpaceCritical.md` (free space)
- `docs/runbooks/daily-ops-checklist.md` (autonomy evidence, host-vs-container)
- `docs/runbooks/halt-recovery.md`
