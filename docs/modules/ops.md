# ops — Operations Plane

> **Package**: `src/hft_platform/ops/`
> **Runtime Plane**: Operations
> **Key Pattern**: Autonomy degradation FSM

## Overview

Operational automation for session governance, autonomy monitoring, strategy health, position flattening, margin monitoring, and backup management. 14 files.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `session_governor.py` | `SessionGovernor`, `TrackGate`, `SessionPhase` | Multi-track session phase FSM |
| `autonomy_monitor.py` | `AutonomyMonitor` | Health-signal reactor (flattening, reduce-only) |
| `autonomy.py` | `AutonomyMode`, `AutonomyTransition` | Mode definitions and transitions |
| `platform_degrade.py` | `PlatformDegradeController` | Reduce-only mode with auto-recovery |
| `strategy_governor.py` | `StrategyHealthGovernor` | Per-strategy quarantine |
| `position_flattener.py` | `PositionFlattener` | Emergency position closing |
| `margin_monitor.py` | `MarginMonitor` | Margin utilization alerts |
| `flatten_gate.py` | `FlattenGate` | File-based IPC for `hft ops flatten`; the engine supervisor claims it (`HFTSystem._sync_operator_flatten`) |
| `backup.py` | `BackupManager` | ClickHouse daily backup with verify/restore |
| `evidence.py` | `AutonomyEvidenceWriter` | Autonomy audit trail (JSONL + Markdown) |
| `manual_rearm.py` | `ManualRearmGate` | File-based manual rearm state; platform re-arm needs `platform_token` + reason, audited |
| `release_audit.py` | `token_for`, `check_release`, `append` | Token and audit line every operator release of a stop uses |
| `platform_inputs.py` | `PlatformDegradeInputs` | Multi-source health signal aggregation |
| `config_snapshot.py` | `build_snapshot()` | Startup config capture to ClickHouse |
| `daily_pnl_report.py` | `DailyPnlSection` | PnL report formatting |

## Session Governor

Multi-track session phase state machine:

```
INIT → PRE_OPEN → OPEN → CLOSE_ONLY → FORCE_FLAT → CLOSED
```

Tracks: `stock` (09:00-13:30), `futures_day` (08:45-13:45), `futures_night` (15:00-05:00)

```python
governor = SessionGovernor(config_path="config/base/session_governor.yaml")
phase = governor.get_phase("TXFD6")  # Returns SessionPhase
```

**TrackGate**: Lightweight symbol → track → phase lookup used by StrategyRunner to filter intents.

## Autonomy Monitor

Async health-signal reactor polling broker, infra, reconciliation:

| Signal | Threshold | Action |
|--------|-----------|--------|
| Broker disconnect | 300s | Reduce-only |
| Feed gap majority | 120s | Reduce-only |
| Reconnect flapping | 5 events | Reduce-only |
| Queue depth | 5000 | Reduce-only |
| RSS memory | 2048 MB | Reduce-only |
| WAL backlog | 200 files | Reduce-only |
| Reconciliation drift | 2 streak | Reduce-only |
| HALT | 3 retries | Requires manual rearm |

## PlatformDegradeController

```python
ctrl = get_shared_platform_degrade_controller()
ctrl.enter_reduce_only(reason="feed_gap_exceeded")
ctrl.allow_intent(IntentType.NEW, opens_risk=True)  # False in reduce-only
```

- Auto-recovery for feed_reconnect/feed_gap reasons (60s cooldown)
- Reference position tracking for reduce-only enforcement
- Singleton pattern with lock

## PositionFlattener

```python
result = await flattener.flatten_all()
# result.fully_closed, partially_closed, failed, failed_symbols
```

- FORCE_FLAT market orders
- Per-symbol timeout (120s)
- Retry once per failed symbol

## Operator flatten (`hft ops flatten`)

The CLI writes `flatten_request.json` through `FlattenGate`; the engine's
supervisor (`HFTSystem._sync_operator_flatten`, 1 Hz) claims it, records the
platform net position per symbol in scope (`all`, or `strategy` with
`--scope-id`), and `LossExitController` sends a FORCE_FLAT for each with
trigger `operator_flatten` (IOC marketable limit, retried every 10 s). The
request finishes from the position store: COMPLETED with every symbol flat, or
at the deadline with the still-open symbols counted as partially closed or
failed. The CLI exits non-zero when any symbol is still open. A request left
PROCESSING by an engine that restarted is failed on the next boot. A FORCE_FLAT
closes the platform's whole net position in a symbol, so `strategy` scope
selects symbols, not lots. `PositionFlattener` is not on this path.

## BackupManager

```python
manager = BackupManager(retain_days=30)
success = manager.run_daily()  # disk check → backup → verify → cleanup
```

- ClickHouse `BACKUP DATABASE hft TO Disk('backup_local', 'daily_YYYYMMDD/')`
- Verification: restore to temp DB, compare row counts
- Retention: configurable days, oldest purged
- Notifications on success/failure

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_TRACK_GATE_DEFAULT_OPEN` | `0` | Default phase for unknown symbols |
| `HFT_MARGIN_WARN_RATIO` | `0.80` | Margin warning threshold |
| `HFT_MARGIN_CRITICAL_RATIO` | `0.90` | Margin critical threshold |
| `HFT_BACKUP_ENABLED` | `0` | Enable daily backup |
| `HFT_BACKUP_RETAIN_DAYS` | `7` | Backup retention days |
| `HFT_PLATFORM_AUTO_RECOVERY_ENABLED` | `1` | Enable auto-recovery from reduce-only |
| `HFT_PLATFORM_AUTO_RECOVERY_COOLDOWN_S` | `60` | Auto-recovery cooldown |
