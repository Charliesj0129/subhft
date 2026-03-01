# Runbook: CE3 WAL-First Outage Drills and Recovery

## Purpose

Execute and interpret CE3-07 outage drills for WAL-first recorder resilience:

1. ClickHouse down
2. ClickHouse slow
3. WAL disk pressure / disk-full policy
4. Loader restart with stale shard-claim recovery

## Test Suite

```bash
uv run pytest -q --no-cov tests/integration/test_wal_outage_drills.py
```

Related hardening tests:

```bash
uv run pytest -q --no-cov \
  tests/integration/test_wal_loader_scale_out.py \
  tests/spec/test_replay_safety_contract.py
```

## Scenario Mapping

### Drill 1: ClickHouse Down
- Expected: `WALFirstWriter` writes to WAL, no ClickHouse calls.
- Recovery: follow `docs/runbooks/recorder-recovery-from-ck-down.md`.

### Drill 2: ClickHouse Slow
- Expected: `wal_first` write path returns quickly (runtime not blocked by CK delay).
- Verify WAL accumulation and loader drain after CK recovers.

### Drill 3: Disk Pressure / Disk-Full Policy
- Expected: CRITICAL/HALT pressure causes topic-based drop/halt behavior.
- Recovery: follow `docs/runbooks/recorder-wal-disk-pressure.md`.

### Drill 4: Loader Restart + Stale Claim Recovery
- Expected: stale `.claim` files are recovered and all WAL files become claimable.
- Verify `FileClaimRegistry.recover_stale_claims()` logs and loader resumes replay.

## Operational Metrics (CE3-06)

Watch during drills:
- `wal_backlog_files`
- `wal_replay_lag_seconds`
- `rate(wal_replay_throughput_rows_total[1m])`
- `wal_drain_eta_seconds`
- `disk_pressure_level`

Dashboard:
- `config/monitoring/dashboards/gateway_wal_slo.json`

## Success Criteria

- No duplicate inserts across loader scale-out (CE3-03)
- Replay safety invariants hold under restart/crash scenarios (CE3-04)
- WAL SLO metrics move in expected direction during outage and recovery (CE3-06)
- Runbooks for CK down / disk pressure are sufficient for operators (CE3-07)

