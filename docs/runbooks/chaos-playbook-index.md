# Chaos Test Playbook Index

## Overview

Structured chaos test playbooks that validate the platform's resilience under
specific failure modes. Each playbook maps to a real-world incident scenario
and verifies the expected automated response.

## Run Command

```bash
uv run pytest tests/chaos/test_playbook_*.py -v -m chaos
```

## Playbook Summary

| # | Playbook | Test File | Failure Mode | Expected Response | Tests |
|---|----------|-----------|--------------|-------------------|-------|
| 1 | Broker Disconnect | `tests/chaos/test_playbook_broker_disconnect.py` | Broker connectivity loss | Enter reduce-only, block new opens, allow closes, restore on reconnect | 4 |
| 2 | ClickHouse Down | `tests/chaos/test_playbook_clickhouse_down.py` | ClickHouse unavailable | WAL fallback activates, hot path unblocked, disk pressure handled, files replayable | 4 |
| 3 | Feed Gap >30s | `tests/chaos/test_playbook_feed_gap.py` | Market data feed gap | StormGuard STORM, escalate to HALT on drawdown, block new orders, allow FORCE_FLAT | 5 |
| 4 | Position Drift | `tests/chaos/test_playbook_position_drift.py` | Local vs broker position mismatch | Detect drift, reduce-only on consecutive drift, restore on resolution, grace failures | 4 |
| 5 | Disk Full | `tests/chaos/test_playbook_disk_full.py` | Disk space exhaustion | Circuit breaker activates, recovery on space freed, no crash, cached checks | 4 |

## Quarterly Drill Sign-Off

| Quarter | Date | Operator | All Passed | Notes |
|---------|------|----------|------------|-------|
| Q2 2026 | | | | |
| Q3 2026 | | | | |
| Q4 2026 | | | | |
| Q1 2027 | | | | |

## Related Runbooks

- [StormGuard HALT Recovery](StormGuardHalt.md)
- [ClickHouse Down](clickhouse-down.md)
- [Feed Reconnect](feed-reconnect.md)
- [WAL Disk Pressure](recorder-wal-disk-pressure.md)
- [HALT Recovery](halt-recovery.md)
