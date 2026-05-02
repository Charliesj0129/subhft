# Track C: Autonomy Foundation Design

> Phase 1 Track C — Alert Routing + Operations State Machine + Self-Healing Framework
>
> Date: 2026-04-15 | Status: Approved

---

## Overview

Track C builds the autonomy foundation for 24/7 unattended operation. Three modules, developed in order:

1. **C1: Alert Tiered Router** — Unified alert severity, aggregation, silence rules, escalation chain
2. **C2: Operations State Machine** — Daily lifecycle orchestration, preflight checks, contract lifecycle automation
3. **C3: Self-Healing Framework v1** — Fault classification, diagnosis, playbook-driven repair, approval workflow

**Architecture**: Layered Event Bus — all faults modeled as typed `FaultEvent`, consumed by AlertRouter and HealingOrchestrator independently.

**Event model relationship**: `Alert` (C1) is the notification-layer message — severity, routing, formatting. `FaultEvent` (C3) is the healing-layer message — diagnosis, repair context. When HealingOrchestrator processes a FaultEvent, it emits `Alert` objects through AlertRouter for operator notification. Modules that don't need healing (e.g., daily reports, heartbeats) emit `Alert` directly.

**Automation boundary**: Low-risk repairs execute automatically (reconnect, restart, cleanup). High-risk actions (flatten positions, stop strategies) require Telegram `/approve` confirmation.

---

## C1: Alert Tiered Router

### Problem

Current `NotificationDispatcher` has 23+ individual `notify_*` methods with binary critical/non-critical classification. No unified severity levels, no deduplication/aggregation (same fault floods Telegram), no silence rules, no escalation chain.

### Core Data Model

```python
# src/hft_platform/notifications/alert.py

class AlertSeverity(enum.IntEnum):
    INFO = 0      # Routine state changes (heartbeat, daily report)
    WARN = 1      # Needs attention but not urgent (margin warning, reconnect)
    CRITICAL = 2  # Needs prompt action (STORM, position drift, daily loss approaching)
    FATAL = 3     # Trading stopped (HALT, broker crash, data loss)

@dataclass(slots=True, frozen=True)
class Alert:
    alert_id: str           # Unique ID (uuid4)
    severity: AlertSeverity
    category: str           # "feed", "broker", "risk", "infra", "contract"
    source: str             # Producing module name
    title: str              # Short title (<80 chars)
    detail: str             # Detailed description
    ts_ns: int              # timebase.now_ns()
    dedup_key: str | None   # Key for aggregation (same dedup_key alerts are merged)
    metadata: dict | None   # Additional data (symbol, strategy, error code, etc.)
```

### AlertRouter Pipeline

```
Alert arrives
  -> (1) Dedup/Aggregate: same dedup_key within window sends only once, with count
  -> (2) Silence check: match against silence rules (category + source + time window)
  -> (3) Severity routing:
        INFO     -> Telegram batched (every 60s)
        WARN     -> Telegram immediate
        CRITICAL -> Telegram + Webhook, bypass rate limit
        FATAL    -> Telegram + Webhook + escalation chain started
  -> (4) Escalation: CRITICAL/FATAL not /ack'd -> resend at 5min -> 15min (max 3 times)
```

### Silence Rules

```python
@dataclass(slots=True)
class SilenceRule:
    rule_id: str
    category: str | None      # None = match all
    source: str | None
    severity_max: AlertSeverity  # Only silence <= this level
    start_ns: int
    end_ns: int               # 0 = permanent until removed
    reason: str
```

Management:
- Telegram bot: `/silence feed 30m "maintenance"`
- Config YAML: `config/base/alert_silence.yaml`
- API: `AlertRouter.add_silence(rule)` / `remove_silence(rule_id)`

### Aggregation Mechanism

- Same `dedup_key` alerts within **aggregation window** (default 300s) are merged.
- First occurrence sent immediately; subsequent occurrences accumulated.
- At window end, if accumulated: send summary "{title} — repeated {N} times in past 5 minutes".
- FATAL severity is never aggregated; every occurrence is sent.

### Integration with Existing System

- `NotificationDispatcher`'s 23+ `notify_*` methods internally call `AlertRouter.emit(Alert(...))`.
- `TelegramSender` and `WebhookSender` remain unchanged; AlertRouter calls them.
- Backward compatible: `NotificationDispatcher` public API unchanged, internals rewired.

### New Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/ack <alert_id>` | Acknowledge receipt, stop escalation chain |
| `/silence <category> <duration> [reason]` | Silence a category of alerts |
| `/unsilence <rule_id>` | Remove silence rule |
| `/alerts` | Show active alert summary |

### New Files

```
src/hft_platform/notifications/
  alert.py          # Alert, AlertSeverity, SilenceRule data models
  alert_router.py   # AlertRouter core logic
  escalation.py     # Escalation chain management
  aggregator.py     # Dedup + aggregation window
config/base/
  alert_silence.yaml  # Silence rule configuration
```

---

## C2: Operations State Machine

### Problem

Current SessionGovernor manages trading phases (INIT->PRE_OPEN->OPEN->...->CLOSED), but:
1. Contract rollover still requires manual intervention (C0 alias mechanism exists but TXO option strikes/expiries need manual `symbols.yaml` updates)
2. No complete daily/weekly lifecycle (missing pre-market health checks, post-market settlement confirmation, weekend maintenance windows)
3. Night/day session cross-track coordination is missing (e.g., night session anomaly affecting day session startup)

### Architecture

Don't rewrite SessionGovernor. Add **OperationsStateMachine** as an orchestrator above it.

```
OperationsStateMachine (daily/weekly level)
  +-- SessionGovernor (second/minute level, existing)
  +-- ContractLifecycleManager (contract lifecycle)
  +-- PreflightChecker (pre-market health checks)
```

### OperationsStateMachine States

```
MAINTENANCE -> PRE_MARKET -> TRADING -> POST_MARKET -> SETTLEMENT -> MAINTENANCE
                                                          |
                                                    NIGHT_SESSION (optional)
```

| State | Trigger | Actions |
|-------|---------|---------|
| MAINTENANCE | After settlement / weekends | Contract updates, data cleanup, backup |
| PRE_MARKET | 60min before open (configurable) | PreflightChecker runs health check list |
| TRADING | All required health checks pass | SessionGovernor takes over |
| POST_MARKET | After close | Reconciliation, TCA report, PnL settlement |
| SETTLEMENT | Reconciliation complete | Position snapshot, ClickHouse flush |
| NIGHT_SESSION | Night session open time | SessionGovernor night track takes over |

### PreflightChecker

```python
@dataclass(slots=True, frozen=True)
class PreflightCheck:
    name: str
    check_fn: Callable[[], Awaitable[CheckResult]]
    required: bool        # True = must pass to allow trading
    timeout_s: float      # Per-check timeout

class CheckResult(enum.Enum):
    PASS = "pass"
    WARN = "warn"         # Can trade, but emit WARN alert
    FAIL = "fail"         # Blocks trading if required=True
```

**Check list (v1):**

| Check | Required | Description |
|-------|----------|-------------|
| broker_login | Yes | Shioaji/Fubon login successful |
| contract_cache_fresh | Yes | Contract cache updated (<24h) |
| clickhouse_alive | Yes | ClickHouse queryable |
| redis_alive | No | Redis pingable |
| position_reconciled | Yes | Positions match broker |
| disk_space | Yes | Available space > 5GB |
| wal_backlog_clear | No | WAL pending writes < 20 |
| stormguard_normal | Yes | StormGuard state is NORMAL |
| config_valid | Yes | Config files parse correctly |

Failed required check -> CRITICAL alert + block SessionGovernor OPEN transition. Wait for manual `/override` or automatic retry (30s interval, max 5 retries).

### ContractLifecycleManager

Core solution for monthly/weekly manual intervention:

```python
class ContractLifecycleManager:
    """Automatic futures rollover + options chain updates"""

    async def refresh_futures_aliases(self):
        """Daily check: R1/R2/C0 aliases still point to correct month contract"""
        # Query broker API for current tradeable contracts
        # Compare config aliases -> if month expired, auto-update

    async def refresh_option_chain(self):
        """Weekly check: fetch latest option chain from broker API"""
        # 1. Call Shioaji API to get all TXO contracts
        # 2. Auto-generate option section in symbols.yaml
        # 3. Select ATM +/- N strikes based on underlying price
        # 4. Write updated config, trigger SymbolMetadata.reload_if_changed()

    async def detect_expiry(self):
        """Detect contracts approaching expiry, alert in advance"""
        # 3 days before: INFO alert
        # 1 day before: WARN alert
        # Expiry day: auto-execute rollover (futures) or chain update (options)
```

**Automation level (medium policy):**
- Futures R1/C0 alias update -> **auto-execute** (low risk, only alias pointers)
- Options chain update -> **auto-execute + post-notification** (pulls from broker API, no trading)
- Expiry with open positions -> **alert + wait for confirmation** (involves real money)

### Configuration

```yaml
# config/base/ops_state_machine.yaml
ops:
  pre_market_lead_minutes: 60
  post_market_delay_minutes: 15
  night_session_enabled: true

  contract_lifecycle:
    futures_refresh_cron: "0 7 * * 1-5"     # Every trading day 07:00
    options_refresh_cron: "0 7 * * 1"        # Every Monday 07:00
    expiry_warn_days: [3, 1]
    option_strike_range: 10                   # ATM +/- 10 strikes

  preflight:
    timeout_s: 300                            # Total timeout for all checks
    retry_interval_s: 30                      # Retry interval on failure
    max_retries: 5
```

### New/Modified Files

```
src/hft_platform/ops/
  ops_state_machine.py      # OperationsStateMachine
  preflight_checker.py      # PreflightChecker + check items
  contract_lifecycle.py     # ContractLifecycleManager
config/base/
  ops_state_machine.yaml    # Operations state machine config
```

### Integration with C1

- All state transitions -> `AlertRouter.emit(Alert(severity=INFO, category="ops", ...))`
- Preflight failure -> `CRITICAL` alert
- Contract update complete -> `INFO` alert (with update list)
- Expiry with positions -> `CRITICAL` alert + wait for `/ack`

---

## C3: Self-Healing Framework v1

### Problem

Current AutonomyMonitor is reactive polling (every 5s), only does "detect -> react" (stop trading, reduce-only). Missing:
1. Structured fault classification and diagnosis
2. Ordered repair sequences (not single actions)
3. Automatic execution of low-risk repairs
4. Repair result tracking and learning

### Core Data Model

```python
# src/hft_platform/healing/fault.py

class FaultCategory(enum.StrEnum):
    FEED = "feed"              # Feed disconnect, feed gap, quote flap
    BROKER = "broker"          # Broker disconnect, login expired
    INFRA = "infra"            # ClickHouse, Redis, disk
    POSITION = "position"      # Position drift, reconciliation failure
    CONTRACT = "contract"      # Contract expired, alias stale
    EXECUTION = "execution"    # Order failure, timeout

class FaultSeverity(enum.IntEnum):
    TRANSIENT = 0    # Expected to self-recover (occasional feed gap)
    DEGRADED = 1     # System degraded but functional (Redis down, CK slow)
    IMPAIRED = 2     # Core function impaired (broker disconnect, position mismatch)
    CRITICAL = 3     # Requires immediate action (HALT, data loss)

class RiskLevel(enum.IntEnum):
    AUTO = 0         # Auto-execute, notify afterward
    CONFIRM = 1      # Requires /approve before execution
```

### Healing Actions

```python
@dataclass(slots=True, frozen=True)
class HealingAction:
    action_id: str
    name: str                      # "reconnect_broker", "restart_redis", "clear_wal"
    risk_level: RiskLevel          # AUTO or CONFIRM
    execute_fn: Callable           # async callable
    rollback_fn: Callable | None   # Rollback on failure
    timeout_s: float
    description: str               # Human-readable action description

@dataclass(slots=True)
class HealingResult:
    action_id: str
    success: bool
    duration_ms: float
    detail: str                    # Success/failure reason
    ts_ns: int
```

### HealingOrchestrator Flow

```
FaultEvent arrives
  -> (1) Classify: FaultClassifier determines category + severity
  -> (2) Diagnose: FaultDiagnoser determines root cause from context
  -> (3) Lookup: HealingPlaybook retrieves repair steps for (category, root_cause)
  -> (4) Risk check:
        AUTO    -> execute directly
        CONFIRM -> send CRITICAL alert, wait for /approve (15min timeout then escalate)
  -> (5) Execute: run HealingAction list in sequence
        - Each step has timeout + rollback
        - Any step failure -> stop remaining steps -> send FATAL alert
  -> (6) Verify: re-check that fault is resolved
  -> (7) Record: write HealingResult to ClickHouse (for post-mortem analysis)
```

### HealingPlaybook (Fault-Repair Mapping)

| Fault | Root Cause | Repair Steps | Risk |
|-------|-----------|--------------|------|
| feed gap > 1s | Shioaji quote disconnect | 1. unsubscribe 2. wait 3s 3. resubscribe | AUTO |
| feed gap > 30s | Shioaji session expired | 1. logout 2. wait 5s 3. relogin 4. resubscribe | AUTO |
| quote flap (>5x/60s) | Unstable connection | 1. cooldown 300s 2. resubscribe | AUTO |
| broker disconnect | Session interrupted | 1. relogin (backoff) 2. reconcile positions 3. resume | AUTO |
| broker disconnect > 5min | Extended outage | 1. enter reduce-only 2. alert + wait /approve to resume | CONFIRM |
| ClickHouse unavailable | CK crash/restart | 1. switch to WAL-only 2. retry CK connect (60s) 3. WAL replay on reconnect | AUTO |
| Redis unavailable | Redis crash | 1. disable live monitor 2. log warn 3. retry connect (30s) | AUTO |
| disk space < 5GB | WAL/log accumulation | 1. archive old WAL 2. compress logs 3. alert if still < 2GB | AUTO |
| disk space < 1GB | Severe shortage | 1. emergency WAL cleanup 2. stop recording 3. FATAL alert | CONFIRM |
| position drift (small) | Recon mismatch | 1. re-query broker 2. auto-correct | AUTO |
| position drift (large) | Recon mismatch | 1. re-query broker 2. alert + wait /approve | CONFIRM |
| contract alias stale | Month expired | 1. refresh contract cache 2. re-resolve aliases 3. resubscribe | AUTO |
| order timeout > 3s | Broker API slow | 1. log warn 2. if persistent: enter reduce-only | AUTO |

### Playbook Configuration

```yaml
# config/base/healing_playbook.yaml
playbooks:
  feed_gap_short:
    match:
      category: feed
      description_contains: "feed_gap"
      threshold_s: 1.0
    actions:
      - name: unsubscribe_symbol
        risk: auto
        timeout_s: 5
      - name: wait
        duration_s: 3
      - name: resubscribe_symbol
        risk: auto
        timeout_s: 10
    cooldown_s: 60
    max_retries: 3

  broker_disconnect_long:
    match:
      category: broker
      duration_min_s: 300
    actions:
      - name: enter_reduce_only
        risk: auto
      - name: alert_and_wait_approval
        risk: confirm
        timeout_s: 900
      - name: relogin_broker
        risk: auto
        timeout_s: 30
      - name: reconcile_positions
        risk: auto
        timeout_s: 60
    cooldown_s: 300
```

### Relationship with Existing System

```
StormGuard (unchanged) -- Still the safety valve, HALT = last line of defense
AutonomyMonitor (simplified) -- Detector role, emits FaultEvent instead of direct actions
HealingOrchestrator (new) -- Consumes FaultEvent, executes repairs
AlertRouter (C1) -- Receives all alerts during repair process
OperationsStateMachine (C2) -- Provides current operational state context
```

**AutonomyMonitor transformation:**
- Keep detection logic (polling broker status, StormGuard state, infra health)
- Remove direct repair actions (relogin retry, reduce-only switching)
- Instead emit `FaultEvent` -> HealingOrchestrator decides how to repair
- Transition period: keep old logic as fallback (`HFT_HEALING_ENABLED=0` uses old path)

### New Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/approve <fault_id>` | Approve CONFIRM-level repair action |
| `/reject <fault_id>` | Reject repair, maintain current state |
| `/healing status` | Show in-progress repair flows |
| `/healing history [N]` | Last N repair records |

### Observability

- **Prometheus metrics**: `healing_actions_total{action, result}`, `healing_duration_seconds`, `faults_detected_total{category, severity}`
- **ClickHouse table**: `hft.healing_log` (fault_id, actions, results, duration)
- **Daily summary**: via C1 AlertRouter during POST_MARKET phase

### New Files

```
src/hft_platform/healing/
  __init__.py
  fault.py                # FaultEvent, FaultCategory, FaultSeverity
  classifier.py           # FaultClassifier
  diagnoser.py            # FaultDiagnoser (root cause analysis)
  orchestrator.py         # HealingOrchestrator (core flow)
  playbook.py             # HealingPlaybook (YAML-driven repair steps)
  actions.py              # Concrete repair action implementations
  result.py               # HealingResult, result recording
config/base/
  healing_playbook.yaml   # Fault-repair mapping
```

---

## Complete File Inventory

### New Files (all modules)

| Path | Module | Purpose |
|------|--------|---------|
| `src/hft_platform/notifications/alert.py` | C1 | Alert, AlertSeverity, SilenceRule models |
| `src/hft_platform/notifications/alert_router.py` | C1 | AlertRouter core logic |
| `src/hft_platform/notifications/escalation.py` | C1 | Escalation chain management |
| `src/hft_platform/notifications/aggregator.py` | C1 | Dedup + aggregation window |
| `config/base/alert_silence.yaml` | C1 | Silence rule configuration |
| `src/hft_platform/ops/ops_state_machine.py` | C2 | OperationsStateMachine |
| `src/hft_platform/ops/preflight_checker.py` | C2 | PreflightChecker + check items |
| `src/hft_platform/ops/contract_lifecycle.py` | C2 | ContractLifecycleManager |
| `config/base/ops_state_machine.yaml` | C2 | Operations state machine config |
| `src/hft_platform/healing/__init__.py` | C3 | Package init |
| `src/hft_platform/healing/fault.py` | C3 | FaultEvent, enums |
| `src/hft_platform/healing/classifier.py` | C3 | FaultClassifier |
| `src/hft_platform/healing/diagnoser.py` | C3 | FaultDiagnoser |
| `src/hft_platform/healing/orchestrator.py` | C3 | HealingOrchestrator |
| `src/hft_platform/healing/playbook.py` | C3 | HealingPlaybook |
| `src/hft_platform/healing/actions.py` | C3 | Repair action implementations |
| `src/hft_platform/healing/result.py` | C3 | HealingResult, recording |
| `config/base/healing_playbook.yaml` | C3 | Fault-repair mapping |

### Modified Files

| Path | Changes |
|------|---------|
| `src/hft_platform/notifications/dispatcher.py` | Rewire notify_* methods to emit Alert via AlertRouter |
| `src/hft_platform/notifications/templates.py` | Add templates for new alert types (healing, preflight, contract) |
| `src/hft_platform/bot/` | Add /ack, /silence, /unsilence, /alerts, /approve, /reject, /healing commands |
| `src/hft_platform/ops/session_governor.py` | Add hooks for OperationsStateMachine integration |
| `src/hft_platform/ops/autonomy_monitor.py` | Simplify to detector role, emit FaultEvent |
| `src/hft_platform/services/bootstrap.py` | Wire new components into service graph |
| `src/hft_platform/feed_adapter/shioaji/contracts_runtime.py` | Expose API for ContractLifecycleManager |

### Test Files

```
tests/unit/test_alert_router.py
tests/unit/test_alert_aggregator.py
tests/unit/test_escalation.py
tests/unit/test_silence_rules.py
tests/unit/test_ops_state_machine.py
tests/unit/test_preflight_checker.py
tests/unit/test_contract_lifecycle.py
tests/unit/test_fault_classifier.py
tests/unit/test_healing_orchestrator.py
tests/unit/test_healing_playbook.py
tests/unit/test_healing_actions.py
tests/integration/test_alert_router_telegram.py
tests/integration/test_ops_lifecycle.py
tests/integration/test_healing_e2e.py
```

---

## Development Order

```
C1 Alert Router (week 1-2)
  -> C2 Operations State Machine (week 3-4)
    -> C3 Self-Healing Framework (week 5-7)
```

Each module is independently testable. C1 can be deployed and validated before C2 begins. Feature flags (`HFT_ALERT_ROUTER_ENABLED`, `HFT_OPS_STATE_MACHINE_ENABLED`, `HFT_HEALING_ENABLED`) control activation with fallback to existing behavior.
