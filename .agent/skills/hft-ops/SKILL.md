---
name: hft-ops
description: Use when working on platform operations — session lifecycle, autonomy degradation, position flattening, margin monitoring, pre/post market checks, backup, or any code in ops/.
---

# HFT Operations

Use this skill for `ops/` (14 files), operational Makefile targets, and runtime lifecycle management.

## Module Map (14 files)

### Session Lifecycle
| File | Class | Purpose |
| --- | --- | --- |
| `session_governor.py` (12KB) | `SessionGovernor`, `TrackGate`, `SessionPhase` | Wall-clock session FSM per product track |
| `strategy_governor.py` | `StrategyGovernor` | Per-strategy risk management |

### Autonomy & Safety
| File | Class | Purpose |
| --- | --- | --- |
| `autonomy.py` | `AutonomyMode`, `AutonomyTransition` | NORMAL/QUARANTINED/REDUCE_ONLY/HALT enum + transitions |
| `autonomy_monitor.py` (16KB) | `AutonomyMonitor` | Continuous health monitoring -> auto-degradation |
| `position_flattener.py` | `PositionFlattener` | Emergency position closure (120s deadline) |
| `flatten_gate.py` | `FlattenGate` | FORCE_FLAT phase filter (close-only orders) |
| `manual_rearm.py` | `manual_rearm()` | Operator recovery from REDUCE_ONLY/HALT |
| `platform_degrade.py` | `PlatformDegradeManager` | Coordinated multi-subsystem degradation |
| `margin_monitor.py` | `MarginMonitor` | Broker margin availability monitoring |

### Ops Utilities
| File | Class | Purpose |
| --- | --- | --- |
| `backup.py` | `BackupManager` | ClickHouse backup automation |
| `config_snapshot.py` | `ConfigSnapshot` | Boot config -> CH audit trail |
| `daily_pnl_report.py` | `DailyPNLReport` | Daily PnL summary |
| `evidence.py` | `EvidenceCollector` | Degradation diagnosis evidence |
| `platform_inputs.py` | `PlatformInputs` | Platform input parameters |

## Session Phase FSM

```text
INIT(0) -> PRE_OPEN(1) -> OPEN(2) -> CLOSE_ONLY(3) -> FORCE_FLAT(4) -> CLOSED(5)
```

Driven by wall-clock schedule in `config/base/session_governor.yaml`:
```yaml
tracks:
  - name: stock
    symbols: [2330, 2317]
    schedule:
      - {phase: pre_open, at: "08:30"}
      - {phase: open, at: "09:00"}
      - {phase: close_only, at: "13:25"}
      - {phase: force_flat, at: "13:29"}
      - {phase: closed, at: "13:30"}
  - name: futures_day
    symbols: [MXF, TX]
    schedule: [...]
```

**TrackGate** provides O(1) per-symbol phase lookup for StrategyRunner:
- `OPEN`: normal trading
- `CLOSE_ONLY`: close positions only
- `FORCE_FLAT`: PositionFlattener active
- `CLOSED`: all intents rejected

## Autonomy Degradation

```text
AutonomyMonitor checks (every 100ms-1s):
  CH write stale > 60s         -> PLATFORM_REDUCE_ONLY
  Feed gap > 50% of symbols    -> PLATFORM_REDUCE_ONLY
  Feed reconnect flapping      -> PLATFORM_REDUCE_ONLY
  Queue depth > 90% maxsize    -> PLATFORM_REDUCE_ONLY
  RSS memory > threshold       -> PLATFORM_REDUCE_ONLY
  PnL drawdown > limit         -> HALT
  Reconciliation drift         -> PLATFORM_REDUCE_ONLY

Transitions:
  NORMAL -> PLATFORM_REDUCE_ONLY (auto)
  PLATFORM_REDUCE_ONLY -> HALT (auto, on critical triggers)
  HALT -> NORMAL (MANUAL ONLY via manual_rearm())
```

**Reason codes** (frozen set for metrics):
`broker_unavailable`, `clickhouse_unhealthy`, `feed_gap_majority`, `feed_reconnect_flapping`, `memory_pressure`, `persistence_failure`, `pnl_peak_drawdown`, `queue_depth_exceeded`, `reconciliation_drift`, `rss_unhealthy`, `wal_backlog_unhealthy`

## Pre/Post Market SOPs

### Pre-Market
```bash
make pre-market-check    # Docker, ClickHouse, Redis, WAL, metrics
```
Checks: containers healthy, CH responsive, Redis ping, WAL backlog zero, Prometheus scraping.

### Post-Market
```bash
make post-market-check   # WAL, recorder, ClickHouse records, PnL
```
Checks: WAL drained, recorder healthy, today's CH row count, PnL reconciled.

## Operational Commands

```bash
# Health
make pre-market-check              # Pre-market gates
make post-market-check             # Post-market verification
make recorder-status               # WAL backlog + CH status
uv run hft check                   # Config validation

# Drills
make drill-ck-down                 # ClickHouse 30s outage (WAL fallback test)
make drill-wal-pressure            # Disk pressure circuit breaker
make drill-recon-mismatch          # Reconciliation mismatch
make rollback-drill                # Rollback procedure

# Maintenance
make canary-auto                   # One-shot canary gate
make wal-archive-cleanup           # Clean old WAL archives
make wal-dlq-status                # DLQ status
```

## Key Environment Variables

| Variable | Default | Effect |
| --- | --- | --- |
| `HFT_STARTUP_RECON_ENABLED` | `1` | Startup position recovery |
| `HFT_STARTUP_RECON_QTY_THRESHOLD` | `10` | Stock discrepancy auto-correct threshold |
| `HFT_CHECKPOINT_ENABLED` | `1` | Periodic position checkpoint |
| `HFT_RECONNECT_HOURS` | `08:30-13:35` | Auto-reconnect window |
| `HFT_RECONNECT_HOURS_2` | — | Secondary window (night session) |
| `HFT_STORMGUARD_FEED_GAP_HALT_S` | `30` | Feed gap -> HALT threshold |
| `HFT_BACKUP_ENABLED` | `0` | Automated daily CH backup |
| `HFT_BACKUP_RETAIN_DAYS` | `30` | Backup retention |
| `HFT_TELEGRAM_ENABLED` | `0` | Telegram notifications |

## Notification Flow

```text
AutonomyMonitor detects degradation
  -> NotificationDispatcher
    -> Critical (HALT, daily_loss): bypass rate limit, immediate Telegram
    -> Normal (StormGuard, margin, position): rate-limited (10s/msg)
  -> Operator reviews -> manual_rearm() -> NORMAL
```

## Critical Rules

1. **HALT requires manual rearm** — never auto-recover from HALT
2. **FORCE_FLAT has 120s deadline** — timeout = fail, escalate
3. **SessionGovernor is wall-clock** — not event-driven
4. **TrackGate unknown symbols -> CLOSED** (safe default, unless `HFT_TRACK_GATE_DEFAULT_OPEN=1`)
5. **Autonomy reason codes are frozen** — add new ones to the frozen set before using
6. **Remote deployment is manual** — never auto-deploy (user feedback rule)
