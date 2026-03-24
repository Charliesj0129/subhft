# Production Rollout Design — HFT Platform

**Date**: 2026-03-23
**Author**: Charlie + Claude
**Status**: Draft
**Timeline**: 12 weeks (2026-03 → 2026-06)

---

## 1. Context & Constraints

### 1.1 What We're Building

A phased rollout plan to bring the HFT platform from "verified components" to "full production trading" on a self-hosted server, operated by a single developer/trader.

### 1.2 Current State (Validated)

| Component | Status | Evidence |
|-----------|--------|----------|
| Market data pipeline | ✅ Verified | Real Shioaji feed tested, tick + bidask normalized |
| Order execution (script) | ✅ Verified | 3-phase test: sim → live cancel → live fill + close |
| PnL consistency | ✅ Verified | Python / Rust / PositionStore = -60 NTD (三方一致) |
| Risk engine | ✅ Implemented | StormGuard FSM, exposure gate, circuit breaker |
| Persistence | ✅ Implemented | ClickHouse + WAL fallback |
| CI/CD | ✅ Implemented | Lint → typecheck → test → coverage (70%+) |
| Monitoring | ✅ Implemented | Prometheus + Grafana + Redis live cache |

### 1.3 What's NOT Yet Validated

- **Strategy-driven** end-to-end flow (策略自動驅動下單，非腳本)
- System stability over **consecutive trading days**
- **Reconnect / recovery** under real market conditions
- **Solo-operator** automation (no one watching = system must self-protect)

### 1.4 Constraints

| Constraint | Value |
|-----------|-------|
| Operator | 1 person (dev + trader + ops) |
| Environment | Self-hosted server, Docker Compose |
| Broker | Shioaji (永豐金) |
| Risk appetite | Conservative — 日損限額 10,000 NTD |
| Timeline | 12 weeks, thorough |
| Performance bottleneck | Broker API RTT (~30ms) — system internal latency not a concern |
| Rust optimization | Out of scope (FeatureEngine Rust, hot-path expansion 均不列入) |

### 1.5 Out of Scope

- FeatureEngine Rust production hardening
- Hot-path Rust kernel expansion (strategy/risk/order)
- 夜盤 (night session) support
- Second broker (Fubon) production deployment
- Kubernetes / cloud migration

---

## 2. Five-Phase Timeline Overview

```
已完成 ✅   Phase 0 — Execution Path Verification (API script 3-phase PASS)

Week 1-3:   Phase 1 — Solo-Operator Automation & Hardening
Week 4-6:   Phase 2 — Shadow Trading (Strategy-Driven)
Week 7-9:   Phase 3 — Canary Live Trading
Week 10-11: Phase 4 — Expansion Validation
Week 12:    Phase 5 — Full Production Declaration
```

Each phase has a quantitative Go/No-Go gate. Failure at any gate means STOP and fix before proceeding. No exceptions.

---

## 3. Phase 1 — Solo-Operator Automation & Hardening (Week 1-3)

**Goal**: Build the safety net so the system protects itself when you're not watching.

### 3.1 Three-Layer Defense Model

#### Layer 1 — Application-Level Self-Recovery (strengthen existing)

**StormGuard FSM** (already implemented in `risk/storm_guard.py`):
- States: `NORMAL(0) → WARM(1) → STORM(2) → HALT(3)` (defined in `contracts/strategy.py::StormGuardState`)
- `NORMAL → WARM`: feed gap > threshold, queue depth spike
- `WARM → STORM`: sustained degradation, multiple concurrent issues
- `STORM → HALT`: critical failure, feed gap > `HFT_STORMGUARD_FEED_GAP_HALT_S` (default 30s)
- `HALT → NORMAL`: requires explicit recovery (manual or scheduled)

Enhancements needed:
- **Strategy-level kill-switch**: When a single strategy produces anomalous signals (e.g., >10 OrderIntents/second, or OrderIntents totaling >5x normal size), HALT that strategy only — don't stop the entire system. Other strategies continue.
- **Order rate limiter**: Hard cap at `max_order_per_min` (configurable, default 10). Enforced in `OrderAdapter` before any broker API call. Exceeding → reject OrderCommand + WARNING log + Telegram alert.
- **Position size guard**: Reject any OrderIntent that would result in total position exceeding `max_open_positions` (default 1 for Canary, 3 for expanded).

Implementation location:
```
src/hft_platform/risk/engine.py     — daily loss check integration
src/hft_platform/order/adapter.py   — order rate limiter
src/hft_platform/strategy/runner.py — per-strategy kill-switch
```

Existing supporting modules to reuse instead of duplicating behavior:
```
src/hft_platform/order/rate_limiter.py       — global/per-symbol sliding window limiter
src/hft_platform/order/halt_canceller.py     — HALT-triggered cancel fanout
src/hft_platform/risk/halt_flattener.py      — optional HALT auto-flatten path
src/hft_platform/execution/reconciliation.py — runtime broker/platform reconciliation hooks
src/hft_platform/execution/eod_recon.py      — in-process EOD reconciliation trigger
```

#### Layer 2 — Process-Level Supervision (new)

**Systemd service unit** (`/etc/systemd/system/hft-engine.service`):

Note: Docker Compose does not natively forward `sd_notify`, so we use `Type=simple` with a health check script instead of `Type=notify`.

```ini
[Unit]
Description=HFT Trading Engine
After=docker.service
Requires=docker.service

[Service]
Type=simple
ExecStart=/usr/bin/docker compose -f /home/charlie/hft_platform/docker-compose.yml up
ExecStop=/usr/bin/docker compose -f /home/charlie/hft_platform/docker-compose.yml down
Restart=on-failure
RestartSec=10
StartLimitBurst=3    # Max 3 restarts per StartLimitIntervalSec
StartLimitIntervalSec=3600  # Per hour

# Health check: engine writes /tmp/hft-heartbeat every 30s from main loop.
# If file mtime > 90s old, systemd ExecReload triggers container restart.
ExecStartPost=/home/charlie/hft_platform/ops/wait-for-healthy.sh

# Resource protection (applied to compose process tree)
MemoryMax=4G
MemoryHigh=3G

[Install]
WantedBy=multi-user.target
```

**Watchdog heartbeat** (file-based, Docker-compatible):
- Engine writes timestamp to `/tmp/hft-heartbeat` every 30s from `services/system.py` main loop
- A companion cron job (`ops/check-heartbeat.sh`) runs every minute:
  - If heartbeat file mtime > 90s → `systemctl restart hft-engine` + Telegram alert
  - If file missing → same restart path
- This avoids the `sd_notify` + Docker incompatibility issue

**OOM protection**:
- `MemoryMax=4G` — hard kill if exceeded
- `MemoryHigh=3G` — kernel memory pressure + reclaim before OOM
- Prometheus metric `process_resident_memory_bytes` alerts at 2.5G (Grafana rule)

#### Layer 3 — External Notification (new)

**Telegram Bot** — the lifeline for solo-operator:

Architecture:
```
src/hft_platform/notifications/
├── __init__.py
├── telegram.py        # Async Telegram sender
├── dispatcher.py      # Event → notification routing
└── templates.py       # Message templates (structured, not free-text)
```

Design principles:
- **Async-only**: Uses `aiohttp` to send, never blocks hot path
- **Fire-and-forget**: Telegram send failure → log WARNING, never raise
- **Rate-limited**: Max 1 message/second, batch if multiple events in window. Exception: 🔴 CRITICAL alerts bypass rate limiting and send immediately (they often co-occur, e.g., daily loss + HALT)
- **Structured messages**: Fixed templates, not dynamic string interpolation (prevents injection)

Notification events and priority:

| Event | Priority | Template |
|-------|----------|----------|
| System HALT | 🔴 CRITICAL | `🔴 HALT: {reason}. All trading stopped. Manual recovery required.` |
| Daily loss limit hit | 🔴 CRITICAL | `🔴 日損限額觸及: PnL={pnl} NTD (limit={limit}). HALT activated.` |
| Process restart | 🟠 HIGH | `🟠 Engine restarted by systemd. Attempt {n}/3.` |
| Reconnect > 3x/day | 🟠 HIGH | `🟠 Reconnect #{n} today. Flap detection: {status}.` |
| PnL reconciliation mismatch | 🟠 HIGH | `🟠 對帳不一致: platform={a}, broker={b}, CH={c}. 明日 HALT pending.` |
| Pre-market check FAIL | 🟠 HIGH | `🟠 開盤前健檢 FAIL: {failed_checks}. 策略不啟動.` |
| Pre-market check PASS | 🟢 INFO | `🟢 08:15 健檢 PASS. 策略將於 08:45 啟動.` |
| Daily report | 🟢 INFO | (see Section 3.3 below) |
| StormGuard state change | 🟡 WARN | `🟡 StormGuard: {old} → {new}. Reason: {reason}.` |

**Telegram kill-switch** (emergency remote stop):
- Bot listens for `/stop` command from your Telegram user ID (whitelist, only 1 user)
- Receives `/stop` → sets Redis key `hft:emergency_halt=1` → engine's Telegram handler (running in the same asyncio loop) reads the key and triggers StormGuard → HALT + cancel all open orders. Redis is used as IPC because env vars are per-process and cannot be modified externally.
- Receives `/status` → replies with current state (positions, PnL, StormGuard state)
- Polling-based (not webhook) — simpler, no public endpoint needed
- Poll interval: 5 seconds (acceptable latency for emergency use)

Environment variables:
```
HFT_TELEGRAM_BOT_TOKEN=<token>     # From @BotFather
HFT_TELEGRAM_CHAT_ID=<your_id>     # Your personal chat ID
HFT_TELEGRAM_ENABLED=1             # 0 to disable
```

### 3.2 Daily Loss Limit Implementation

**Integration point**: `RiskEngine.evaluate()` in `src/hft_platform/risk/engine.py`

```
Flow:
  StrategyRunner produces OrderIntent
  → RiskEngine.evaluate(intent)
     1. Existing checks (exposure, dedup, circuit breaker, StormGuard)
     2. 【NEW】Daily PnL check:
        realized_pnl + unrealized_pnl = current_daily_pnl
        if current_daily_pnl <= daily_loss_limit:
          → return RiskDecision(action=REJECT, reason="daily_loss_limit")
          → trigger HALT (StormGuard → HALT state)
          → cancel all open orders
          → Telegram CRITICAL notification
          → 需人工解除 HALT（防止自動恢復後繼續虧損）
```

**PnL 計算來源**:
- `realized_pnl`: from existing `DailyLossLimitValidator._accumulated_loss` (已平倉損益, accumulated via `record_pnl()`)
- `unrealized_pnl` (new work): requires adding `mark_to_market(mid_price: int) -> int` method to `PositionStore` (`execution/positions.py`). This method does not currently exist — it must be implemented. It computes `(mid_price - avg_price) × net_qty` for each open position. The `mid_price` is sourced from the latest `LOBStatsEvent.mid_price_x2 // 2` (already available on the bus). Staleness tolerance: mid_price up to 5 seconds old is acceptable for risk gating.
- Daily loss formula: `realized_pnl + unrealized_pnl` (both in scaled int x10000)
- Display conversion: `pnl_ntd = pnl_scaled / 10000`
- Config unit note: `-10,000 NTD` = `-100_000_000` in scaled int (x10000). The existing `DailyLossLimitValidator` default of `500_000_000` = `50,000 NTD`. Override via config.

**Reset 時機**: The existing `DailyLossLimitValidator._maybe_reset()` auto-resets when UTC calendar date rolls over. Adjustment needed: change reset trigger to 05:00 local time (Taiwan, UTC+8) to align with futures settlement schedule. This is an in-process time check — no external cron script needed.

**Configuration** (`config/env/prod/risk.yaml`):
```yaml
risk:
  daily_loss_limit_ntd: -10000
  strategy_loss_limit_ntd: -10000
  max_open_positions: 1          # Phase 3 Canary
  max_order_per_min: 10
  order_size_limit: 1
  daily_pnl_reset_hour: 5       # 05:00 local time
  halt_requires_manual_recovery: true
```

### 3.3 Daily Reconciliation System

**Script**: `scripts/daily_reconcile.py`
**Schedule**: cron at 13:50 (收盤後 5 分鐘)

```
Step-by-step flow:

1. Query broker positions
   api.list_positions(futopt_account)
   → broker_positions: Dict[symbol, {qty, avg_price, pnl}]

2. Query platform PositionStore
   Read from in-memory PositionStore (or Redis snapshot)
   → platform_positions: Dict[symbol, {qty, avg_price, pnl}]

3. Query ClickHouse records
   SELECT symbol, sum(qty), sum(realized_pnl)
   FROM hft.fills WHERE date = today()
   → ch_positions: Dict[symbol, {qty, pnl}]

4. Three-way comparison
   For each symbol:
     broker.qty == platform.qty == ch.qty?
     broker.realized_pnl ≈ platform.realized_pnl? (tolerance: ±10 NTD — accounts for fee/tax rounding across multiple round-trips)

5. Result
   ALL MATCH:
     → Telegram 日報 (see template below)
     → Write reconciliation record to ClickHouse hft.reconciliation table

   ANY MISMATCH:
     → Telegram CRITICAL alert with details
     → Set flag: next day pre-market check will HALT
     → Write mismatch record to ClickHouse with full details for post-mortem
```

**Daily report Telegram template**:
```
📊 日報 2026-04-15 (二)

💰 PnL: +1,230 NTD
📈 交易: 買 12 / 賣 12 / 成交 24
📋 持倉: flat (已全部平倉)
✅ 對帳: 三方一致

⏱ 系統:
  延遲 P95: 1.2ms (tick→signal)
  Reconnect: 0 次
  StormGuard: NORMAL
  記憶體: 1.8 GB / 4 GB
```

### 3.4 Pre-Market Health Check

**Script**: `scripts/pre_market_check.py`
**Schedule**: cron at 08:15 (開盤前 30 分鐘)

```
Checks (all must PASS):

1. Broker connectivity
   ├─ Login to Shioaji (simulation=False)
   ├─ Activate CA
   ├─ Fetch target contract (TMF/MXF)
   ├─ Query margin (>= required threshold)
   └─ Logout
   Timeout: 30s. FAIL if any step fails.

2. ClickHouse health
   ├─ SELECT 1 (native protocol, port 9000)
   ├─ Check hft.market_data table exists
   └─ Check yesterday's reconciliation record exists and status=OK
   Timeout: 10s.

3. Redis health
   ├─ PING
   ├─ Check session owner key (should be empty or self)
   └─ Check no stale locks
   Timeout: 5s.

4. Disk space
   ├─ WAL directory (.wal/) < 80% of partition
   ├─ Logs directory < 80% of partition
   └─ ClickHouse data < 80% of partition

5. Yesterday's reconciliation
   ├─ Read last reconciliation record
   ├─ Status must be MATCH
   └─ If MISMATCH → FAIL (block trading until manually resolved)

6. System resources
   ├─ Available RAM > 2 GB
   ├─ CPU load < 80%
   └─ No zombie hft-engine processes

Result:
  ALL PASS → Telegram 🟢 + auto-start strategy at 08:45
  ANY FAIL → Telegram 🟠 + do NOT start + list failed checks
```

### 3.5 Cron Schedule Summary

```crontab
# Pre-market health check (weekdays only)
# Uses core/market_calendar.py to skip holidays and half-days
15 8 * * 1-5  /home/charlie/hft_platform/scripts/pre_market_check.py

# Post-market reconciliation (weekdays only)
# Also uses market_calendar — adjusts time for half-day sessions
50 13 * * 1-5 /home/charlie/hft_platform/scripts/daily_reconcile.py

# Heartbeat watchdog check (every minute during trading hours)
* 8-14 * * 1-5 /home/charlie/hft_platform/ops/check-heartbeat.sh

# Weekly reliability summary (Friday after close)
0 14 * * 5    /home/charlie/hft_platform/scripts/weekly_summary.py
```

Note: `daily_pnl_reset.py` cron removed — PnL reset is handled in-process by `DailyLossLimitValidator._maybe_reset()` (adjusted to trigger at 05:00 local time).

**Weekly summary** (`scripts/weekly_summary.py`, Friday 14:00):
Aggregates Mon-Fri data from ClickHouse and Prometheus, sends Telegram report:
```
📊 週報 W17 (04/21 - 04/25)

💰 週 PnL: +3,450 NTD (5 trading days)
📈 日均交易: 22 筆 / 最高單日: +2,100 / 最低單日: -680
📋 對帳: 5/5 日一致

⏱ 系統穩定性:
  HALT: 0 次 / Reconnect: 2 次 (total)
  延遲 P95 avg: 2.8ms / RSS peak: 1.9 GB
  Uptime: 100%
```

**Holiday and half-day handling**: All scripts import `core.market_calendar` (already exists, 18 references in codebase) to check `is_trading_day()` and `get_session_end_time()`. On non-trading days, scripts exit immediately with a log entry. On half-day sessions, `force_flat_time` and `auto_stop_time` are adjusted relative to the actual session end time.

### 3.6 Phase 1 Hardening Controls

Phase 1 is not only about adding components; it is about proving that solo-operator failure defaults to safe behavior.

**Operational invariants**:
- **Config freeze window**: No config/code changes after `08:15` pre-market PASS. Any change after that automatically invalidates the trading-day evidence pack; the day does not count toward promotion.
- **Fail-closed startup**: If Telegram, Redis, reconciliation dependency, or risk config fails to initialize, strategy auto-start remains disabled. Feed-only startup is allowed; order progression is not.
- **No silent degrade for safety controls**: Notification send failure may fail-open, but missing risk limit config, missing reconciliation baseline, or missing force-flat scheduler must fail-closed.
- **Single source of truth for session state**: `StormGuard`, reconciliation status, and session phase (`pre_open`, `open`, `auto_stop`, `force_flat`, `closed`) must be emitted to logs/metrics so operator decisions are evidence-based, not inferred from scattered logs.

**Change-management rules**:
- **One profile per phase**: Phase 1 uses only the production-hardening profile. Do not mix `shadow`, `canary`, and ad-hoc env overrides on the same host during validation.
- **Recorded config fingerprint**: Each daily report includes git commit SHA, active config profile, and a config digest so incidents can be traced to an exact runtime bundle.
- **Runbook binding**: `docs/runbooks/halt-recovery.md`, `docs/runbooks/daily-ops-checklist.md`, and `docs/operations/incident-response-protocol.md` are part of the deliverable. If the implementation differs from those documents, the phase is not done.

### 3.7 Autonomous Maintenance Control Plane

Because the operator may not watch the system for the entire trading session, production runtime must include a layered autonomous maintenance control plane. The design target is **limited self-healing**:

- **Single-strategy abnormality** → isolate that strategy only.
- **Platform/infrastructure abnormality** → switch platform to `reduce-only`.
- **Clear financial/control-loss event** → escalate to `HALT`.
- **No automatic restore of trading privileges** after quarantine, `reduce-only`, or `HALT`; recovery is always manual.

#### 3.7.1 Runtime States

The runtime adds four operator-visible states on top of existing StormGuard metrics:

| State | Meaning | Allowed Actions |
|------|---------|-----------------|
| `NORMAL` | Platform healthy; strategies may open and close positions | all allowed |
| `STRATEGY_QUARANTINED` | One strategy isolated due to abnormal behavior | quarantined strategy blocked; others continue |
| `PLATFORM_REDUCE_ONLY` | Infrastructure trust degraded | cancel / close / reconcile / monitor only; no new opens |
| `HALT` | Risk control failure or critical loss of state trust | cancel / query / evidence capture only |

State transitions must be emitted to metrics, structlog, and the daily evidence pack so the operator can reconstruct exactly when and why the runtime degraded.

#### 3.7.2 Autonomous Controllers

Six controllers should be implemented as first-class production services or policy modules:

1. **Strategy Health Governor**
   Monitors per-strategy order rate, reject spike, intent/fill ratio, per-strategy drawdown, signal burstiness, and strategy heartbeat.
   Action: quarantine only the offending strategy, cancel its live orders, preserve all evidence, require manual re-arm.

2. **Platform Degrade Controller**
   Monitors Redis, ClickHouse, WAL backlog, reconnect flap, queue saturation, and RSS trend.
   Action: switch platform to `reduce-only`, freeze new opening intents globally, preserve close/cancel/reconcile/monitoring paths.

3. **Session Safety Governor**
   Enforces session-state transitions: `pre_open -> tradable -> close_only -> force_flat -> closed`.
   Action: block strategy auto-start after failed pre-market checks, convert late-session trading to close-only, escalate failed force-flat to `HALT`.

4. **Autonomous Reconciliation Guard**
   Performs lightweight intraday reconciliation of broker open orders, platform open orders, and position snapshots.
   Action: warn first, then move to `reduce-only` if drift persists or grows; unresolved drift blocks next-day open.

5. **Evidence and Diary Automation**
   Produces machine-written but human-readable session summaries: state transition timeline, quarantined strategies, reduce-only windows, alerts, and manual-action requirements.
   Action: persist evidence pack without operator involvement and send concise Telegram summaries.

6. **Manual Re-Arm Gate**
   Represents the recovery lock.
   Action: after any quarantine, `reduce-only`, or `HALT`, do not permit automatic return to `NORMAL`; require explicit operator approval after health and reconciliation checks pass.

#### 3.7.3 Trigger Threshold Families

Exact numbers remain config-tunable, but the spec should define threshold families up front:

- **Strategy quarantine thresholds**
  - intents per rolling window above strategy profile cap
  - reject rate above threshold for consecutive windows
  - strategy daily drawdown exceeds strategy loss limit
  - repeated same-direction signal burst beyond expected policy envelope
  - missing strategy heartbeat or no state advancement

- **Platform reduce-only thresholds**
  - reconnect count exceeds daily flap budget
  - WAL backlog exceeds size/time envelope
  - queue depth remains above high-water mark for sustained period
  - RSS exceeds threshold and keeps trending upward
  - Redis / ClickHouse / reconciliation dependency unhealthy beyond grace period

- **HALT thresholds**
  - daily loss limit breach
  - force-flat failure
  - broker/account state no longer trustworthy
  - critical reconciliation mismatch
  - platform continues degrading even after entering `reduce-only`

#### 3.7.4 Telegram and Evidence Model

Telegram must summarize control-plane decisions, not just raw exceptions:

- `INFO`: session start/stop, daily report, evidence pack ready
- `WARN`: strategy quarantine, single dependency flap, recoverable drift
- `HIGH`: platform `reduce-only`, sustained dependency failure, intraday reconciliation drift
- `CRITICAL`: `HALT`, force-flat failure, broker/account state untrusted, daily loss breach

Each session writes:
```
outputs/production_rollout/autonomy/<YYYYMMDD>/
  state_timeline.jsonl
  strategy_quarantine.json
  platform_degrade.json
  alert_digest.md
  manual_rearm_requirements.md
```

#### 3.7.5 Manual Recovery Contract

Manual recovery is a control requirement, not an operator convenience feature. Re-arm must be explicit and split by scope:

- `re-arm strategy <id>` only after reviewing quarantine cause, broker/order consistency, and strategy-specific health.
- `re-arm platform` only after dependency health, reconciliation status, and evidence capture are all green.
- No generic `resume everything` path should exist in Phase 1-3.

### 3.8 Phase 1 Failure-Drill Matrix

Before Phase 1 can pass, each critical failure mode must be exercised intentionally in a controlled window:

| Drill | Injection Method | Expected Result | Evidence |
|------|------------------|-----------------|----------|
| Heartbeat stale | Stop heartbeat writer or pause main loop in dry-run | `check-heartbeat.sh` restarts service within 90s; Telegram HIGH alert | journal excerpt + alert record |
| Telegram unavailable | Block outbound Telegram API temporarily | trading loop unaffected; WARNING logged; later recovery visible | logs + no loop-lag regression |
| ClickHouse unavailable | Stop ClickHouse container | WAL/reconciliation fail-safe path behaves as designed; no blocking on main loop | metrics + runbook note |
| Redis unavailable | Disable Redis during `/stop` test | remote kill-switch becomes unavailable, but local HALT path still works and alert is emitted | operator drill note |
| Feed gap | Suspend quote ingestion | StormGuard escalates through expected states and blocks new orders | metrics snapshot + logs |
| HALT with open position | Trigger HALT in simulation with synthetic position | cancel-open-orders path executes; optional flatten path behavior matches config | fills/orders trace |

Evidence for each drill is stored under:
```
outputs/production_rollout/phase1/<YYYYMMDD>/
  pre_market_check.json
  reconcile.json
  drill_notes.md
  metrics_summary.md
  alerts.jsonl
```

Days without a complete evidence pack do not count toward the gate.

### 3.9 Phase 1 Deliverables Checklist

```
Infrastructure:
  □ hft-engine.service (systemd unit) installed and tested
  □ Watchdog heartbeat integrated into main event loop
  □ OOM protection (MemoryMax/MemoryHigh) configured

Notifications:
  □ Telegram bot created and token stored in .env
  □ src/hft_platform/notifications/ module implemented
  □ All notification events (table above) wired and tested
  □ /stop and /status commands functional
  □ Rate limiter (1 msg/sec) working

Risk:
  □ Daily loss limit (-10,000 NTD) integrated into RiskEngine
  □ Order rate limiter (10/min) in OrderAdapter
  □ Position size guard in RiskEngine
  □ Strategy-level kill-switch in StrategyRunner
  □ HALT → cancel all open orders flow tested

Operations:
  □ daily_reconcile.py — three-way comparison working
  □ pre_market_check.py — all 6 checks working
  □ Cron jobs installed and verified
  □ check-heartbeat.sh watchdog working

Testing:
  □ Unit tests for all new modules (≥80% coverage)
  □ Integration test: simulate HALT → verify cancel + Telegram
  □ Integration test: reconciliation mismatch → verify alert + next-day block
  □ make ci passes (lint + typecheck + test + coverage)

Documentation:
  □ Runbook updated: new HALT recovery procedure
  □ Architecture doc updated: notifications module
```

### 3.10 Phase 1 Go/No-Go Gate

```
All must PASS to proceed to Phase 2:
  □ Systemd service: start → stop → restart → watchdog-timeout → auto-restart (test all paths)
  □ Telegram: receive HALT, daily loss, reconnect, daily report, /stop, /status (test all events)
  □ Daily loss: trigger at -10,000 NTD → HALT + cancel + notification (simulated test)
  □ Reconciliation: match + mismatch paths both tested
  □ Pre-market: pass + fail paths both tested
  □ All cron jobs run successfully for 3 consecutive days (dry-run mode)
  □ Autonomous maintenance states (`NORMAL`, `STRATEGY_QUARANTINED`, `PLATFORM_REDUCE_ONLY`, `HALT`) visible in logs/metrics
  □ Strategy quarantine path tested with manual re-arm
  □ Platform `reduce-only` path tested with manual re-arm
  □ All Phase 1 failure drills executed with saved evidence pack
  □ Config freeze respected for 3 consecutive dry-run days
  □ HALT recovery runbook followed once end-to-end in simulation
  □ make ci green
```

---

## 4. Phase 2 — Shadow Trading (Week 4-6)

**Goal**: Run the full strategy-driven pipeline with real market data, without placing real orders. Validate system stability over consecutive trading days.

### 4.1 Shadow Mode Architecture

```
HFT_MODE=sim (keeps order path in simulation)
+ Real Shioaji feed connected

Data flow:
  Shioaji real feed
  → raw_queue (bounded, async)
  → MarketDataService.run()
  → normalizer.normalize_{tick,bidask}
  → LOBEngine.process_event()
  → FeatureEngine.compute() (if enabled)
  → RingBufferBus.publish_nowait()
  → StrategyRunner.process_event()
  → strategy.handle_event() → OrderIntent[]
  → RiskEngine.evaluate() → OrderCommand[]
  → OrderAdapter: LOG ONLY (do not call broker API)
  → ClickHouse records: full pipeline telemetry
```

**Shadow-specific OrderAdapter behavior**:
- Receives OrderCommand as normal
- Logs the command with full details (symbol, side, price, qty, strategy_id)
- Records to ClickHouse `hft.shadow_orders` table (new, schema below)
- Does NOT call `api.place_order()`
- Emits Prometheus counter `shadow_orders_total{strategy, symbol, side}`

**`hft.shadow_orders` schema** (migration in `src/hft_platform/migrations/clickhouse/`):
```sql
CREATE TABLE IF NOT EXISTS hft.shadow_orders (
    timestamp_ns  UInt64,       -- timebase.now_ns()
    strategy_id   LowCardinality(String),
    symbol        LowCardinality(String),
    side          UInt8,        -- 0=BUY, 1=SELL
    price         Int64,        -- scaled int x10000
    qty           UInt32,
    mid_price     Int64,        -- LOB mid_price at order time (for simulated PnL)
    risk_decision String,       -- ACCEPT/REJECT + reason
    event_date    Date DEFAULT toDate(timestamp_ns / 1000000000)
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(event_date)
ORDER BY (strategy_id, symbol, timestamp_ns)
TTL event_date + INTERVAL 90 DAY;
```

### 4.2 Shadow Validation Metrics

**Collected automatically every trading day:**

| Category | Metric | Target | Source |
|----------|--------|--------|--------|
| Latency | tick-to-signal P50 | < 5ms | Prometheus histogram |
| Latency | tick-to-signal P95 | < 15ms | Prometheus histogram |
| Latency | tick-to-signal P99 | < 50ms | Prometheus histogram |
| Stability | Unplanned HALT count | 0 | StormGuard metrics |
| Stability | Reconnect count / day | ≤ 2 | Reconnect counter |
| Stability | OOM / crash / restart | 0 | Systemd journal |
| Stability | Queue depth peak | < 80% of bound | Queue depth gauge |
| Memory | RSS trend (daily max) | No upward trend over 10 days | Prometheus |
| Signal | OrderIntent count / day | > 0 (strategy is active) | Shadow order table |
| Signal | Simulated PnL | Reasonable (no obvious bug) | Post-hoc analysis script |

**Simulated PnL calculation** (post-market, not real-time):
```
For each shadow OrderCommand:
  assumed_fill_price = mid_price at command timestamp + slippage_estimate
  slippage_estimate = 1 tick (conservative)

Aggregate: sum of (entry_price - exit_price) × qty × point_value
  point_value: from symbol config (TMF=10 NTD/point, MXF=50 NTD/point)
Compare with: strategy's backtest expected PnL range
Flag: if simulated PnL deviates >3σ from backtest distribution
```

### 4.3 Shadow Period Daily Routine

```
08:15  pre_market_check.py runs → Telegram result
08:30  Systemd starts hft-engine (real feed, shadow mode)
08:45  Strategy starts producing signals
13:45  Close — strategy stops
13:50  daily_reconcile.py runs (no position to reconcile, but validates CH records)
14:00  Shadow analysis script: latency summary + signal count + simulated PnL
       → Telegram shadow daily report

Shadow daily report template:
  📊 Shadow 日報 2026-04-22 (二)

  🔮 信號: 18 OrderIntents (買 10 / 賣 8)
  💰 模擬 PnL: +850 NTD (含 1-tick slippage)

  ⏱ 延遲:
    tick→signal P50: 1.1ms / P95: 3.2ms / P99: 8.7ms

  📈 系統:
    Reconnect: 0 / Queue peak: 12% / RSS: 1.7 GB
    StormGuard: NORMAL (全日)
```

### 4.4 Shadow Hardening Controls

Phase 2 exists to validate the real runtime, not to quietly drift into live trading.

**Hard safety boundary**:
- **Dual lock against accidental live orders**: Phase 2 requires both a non-live runtime profile and `HFT_ORDER_SHADOW_MODE=1`. Startup must refuse to proceed if a shadow profile is combined with live order mode.
- **Broker call audit**: Every trading day must prove `api.place_order()` was never reached. Evidence can be a broker call counter metric, a monkeypatched test harness in dry-run, or broker-side order history showing zero new orders.
- **Shadow sink is mandatory**: All accepted `OrderIntent` objects are persisted through `src/hft_platform/order/shadow.py` semantics. Logging-only without structured persistence does not count.

**Observability hardening**:
- **Daily evidence pack**: save shadow-order count, reject reasons, P50/P95/P99 latency, reconnect count, queue peaks, max RSS, and config fingerprint.
- **Review cadence**: every trading day ends with a short operator verdict: `promote`, `repeat`, or `rollback-to-phase1-fix`. Days with unresolved anomalies do not count.
- **Incident budget**: two anomalous days are allowed inside the 12-day window; the third anomaly resets the Phase 2 window after remediation.

**Mode-drift prevention**:
- **One-way promotion path**: Phase 2 may promote only to Phase 3 canary config. It may not directly relax risk limits or add symbols.
- **Feature/config freeze after 08:15**: same rule as Phase 1. If a hotfix is required mid-window, the day becomes a non-counting diagnostic day.
- **Autonomous maintenance remains active**: Shadow mode still exercises strategy quarantine and platform `reduce-only`; only live broker placement remains disabled.

Evidence directory:
```
outputs/production_rollout/phase2/<YYYYMMDD>/
  shadow_daily_report.json
  latency_summary.json
  shadow_orders.parquet
  incident_notes.md
```

### 4.5 Shadow Troubleshooting Guide

| Symptom | Likely Cause | Action |
|---------|-------------|--------|
| 0 OrderIntents all day | Strategy not receiving events, or strategy logic bug | Check bus subscription, verify FeatureEngine output |
| P99 latency > 100ms | GC pressure or blocking IO on event loop | Check `gc` logs, profile event loop lag metric |
| Reconnect > 5/day | Network instability or Shioaji server issues | Check flap detection logs, verify network path |
| RSS growing daily | Memory leak (likely in Python, not Rust) | Check object count growth, run `objgraph` analysis |
| StormGuard HALT | Feed gap or queue overflow | Check feed gap metric, verify queue bounds |

### 4.6 Phase 2 Go/No-Go Gate

```
All must PASS to proceed to Phase 3:
  □ 10 out of 12 trading days completed without unplanned HALT (allows up to 2 incident days without restarting the count)
  □ 10 out of 12 days with no OOM / crash / process restart
  □ Reconnect ≤ 2/day for at least 10 of 12 days
  □ Telegram notifications reliable for all trading days
  □ Simulated PnL within reasonable range (no bug-induced outliers)
  □ Memory RSS trend: flat or decreasing (no leak)
  □ Latency P95 < 15ms for at least 10 of 12 days
  □ Zero real broker orders placed during the entire shadow window
  □ Daily evidence pack complete for all counted days
  □ At least one strategy quarantine event tested without affecting other strategies
  □ At least one platform `reduce-only` drill tested while preserving close/cancel/reconcile behavior
  □ At least one rollback drill: shadow profile restart after injected failure without losing observability
```

---

## 5. Phase 3 — Canary Live Trading (Week 7-9)

**Goal**: First real-money strategy-driven trading. Smallest possible blast radius.

### 5.1 Canary Configuration

```yaml
# config/env/prod/canary.yaml
canary:
  enabled: true

  # Instrument
  symbol: TMF          # 微台指
  point_value: 10      # 1 point = 10 NTD
  session: day_only    # 08:45-13:45, no night session

  # Strategy
  strategies:
    - id: <gate-d-passed-strategy-id>
      enabled: true
      max_position: 1  # 1 lot only

  # Risk (strict)
  risk:
    daily_loss_limit_ntd: -10000
    strategy_loss_limit_ntd: -10000
    max_open_positions: 1
    max_order_per_min: 10
    order_size_limit: 1
    halt_requires_manual_recovery: true

  # Session
  session:
    auto_start: true          # After pre-market check PASS
    auto_stop_time: "13:40"   # 5 min before close, stop new orders
    force_flat_time: "13:43"  # 2 min before close, cancel + market close any open
```

**Force-flat mechanism** (`13:43`):
- If any position remains open at `force_flat_time`:
  1. Cancel all pending orders
  2. Send aggressive close order: IOC at daily limit price (limit_down for sell, limit_up for buy) — this is the TWSE futures equivalent of a market order, guaranteeing fill if any liquidity exists
  3. Wait 30s for fill callback
  4. If still not flat → Telegram CRITICAL + manual intervention required
- Purpose: prevent unintended overnight positions

### 5.2 Canary Mode vs Full Mode

| Aspect | Canary (Phase 3) | Full (Phase 5) |
|--------|------------------|----------------|
| `HFT_MODE` | `real` | `real` |
| `HFT_ORDER_MODE` | `live` | `live` |
| Symbols | TMF only | Multiple |
| Strategies | 1 | Multiple |
| Max position | 1 lot | Configurable |
| Force-flat | Yes (13:43) | Configurable |
| Manual HALT recovery | Yes | Configurable |
| Night session | No | Configurable |

### 5.3 Canary Hardening Controls

Phase 3 is the first place where mistakes cost money, so promotion discipline becomes stricter than feature completeness.

**Promotion prerequisites**:
- Only strategies that have already passed Gate D/E and completed the full Phase 2 shadow window are eligible.
- Canary profile must be committed and reviewed before Day 1. No inline env var overrides except secrets.
- Day 1 through Day 3 require operator presence from pre-open through post-close. Autonomous operation is allowed only after three clean canary days.

**In-session guardrails**:
- **Session FSM**: `pre_open -> tradable -> auto_stop -> force_flat -> closed`. New opening orders are legal only in `tradable`. In `auto_stop`, only reduce/close intents are permitted. In `force_flat` and `closed`, all new orders are rejected.
- **Restart policy with open risk**: Any engine restart while position is non-flat immediately downgrades the next session back to Shadow until root cause and reconciliation are completed.
- **Infrastructure faults downgrade before HALT**: Redis/ClickHouse/WAL/reconnect/RSS/queue faults first push the platform into `reduce-only`; only continued deterioration or loss of state trust escalates to `HALT`.
- **Rollback triggers are hard, not advisory**:
  - any unreconciled fill at end of day
  - any manual broker UI intervention
  - any unexpected live order outside `tradable`
  - any two HIGH/CRITICAL alerts in a single session from unrelated causes
  - force-flat failure or force-flat path taking > 30s

**Canary evidence pack**:
```
outputs/production_rollout/phase3/<YYYYMMDD>/
  session_timeline.md
  order_fill_recon.json
  alert_timeline.jsonl
  config_fingerprint.json
  operator_verdict.md
```

Each day ends with one explicit verdict:
- `continue` — counts toward the five-day gate
- `repeat` — diagnostic day, does not count
- `rollback_to_shadow` — Phase 3 suspended until remediation completes

Canary-specific recovery policy:
- a quarantined strategy stays disabled until you explicitly re-arm it
- platform `reduce-only` stays in force until you explicitly restore normal trading
- if `reduce-only` persists into the close, the next session starts blocked until pre-market checks and manual review both pass

### 5.4 First Day Protocol (Must Be Present)

```
Canary 第一天全程在場，逐步確認每個環節：

07:30  ── Preparation ──
  □ Check Telegram bot is online (/status → response)
  □ Check broker web portal: margin sufficient, no open positions
  □ Review yesterday's Shadow report (final day): no anomalies

08:15  ── Pre-Market ──
  □ pre_market_check.py runs → Telegram 🟢
  □ Manually verify: all 6 checks PASS in log output
  □ Confirm: config loaded = canary config (not shadow)

08:30  ── System Start ──
  □ hft-engine starts (systemd or manual docker compose up)
  □ Verify: Shioaji feed connected (log: "feed connected")
  □ Verify: normalizer producing events (log: "tick_count > 0")
  □ Verify: LOB updating (Prometheus: lob_update_count increasing)

08:45  ── Market Open ──
  □ Observe: strategy receiving events (log: "strategy.handle_event called")
  □ Wait for first OrderIntent
  □ Verify: RiskEngine evaluates it (log: "risk_decision")
  □ First real OrderCommand sent to broker
  □ Verify: broker callback received (order accepted by exchange)

First Fill:
  □ First fill callback received
  □ Check PnL immediately:
    - Platform PositionStore: correct qty, correct avg_price
    - Prometheus metric: position_qty, unrealized_pnl
  □ Continue observing for 30 minutes

First Close:
  □ Strategy sends close OrderIntent
  □ Close fill received
  □ Verify: position = flat
  □ Verify: realized_pnl matches expected (price_diff × qty × point_value)

13:40  ── Pre-Close ──
  □ Auto-stop triggers (no new orders)
  □ If position open: force-flat at 13:43

13:45  ── Market Close ──
  □ Verify: position flat
  □ 13:50: daily_reconcile.py → three-way match
  □ Telegram daily report received

Post-Close Review:
  □ PnL: platform = broker = ClickHouse?
  □ All order callbacks accounted for? (no missing fills)
  □ Latency: within Shadow-period baseline?
  □ Any unexpected StormGuard transitions?
  □ Decision: proceed to Day 2, or fix issues first?
```

### 5.5 Day 2+ Routine (Automated)

```
After successful Day 1, transition to automated routine:

Daily:
  08:15  pre_market_check.py → Telegram (auto)
  08:45  Strategy auto-starts after check PASS
  13:40  Auto-stop new orders
  13:43  Force-flat if needed
  13:50  daily_reconcile.py → Telegram (auto)

Your involvement:
  Morning:  Glance at Telegram — 🟢 = OK, 🟠 = check
  Evening:  Review daily report — PnL, trades, system metrics
  On alert: Respond to CRITICAL notifications (HALT, loss limit, mismatch)
```

Additional Day 2+ rule:
- If a day ends with `repeat` or `rollback_to_shadow`, automation for the next day is disabled until the operator explicitly re-arms canary mode.

### 5.6 Canary Failure Scenarios & Response

| Scenario | Detection | Auto-Response | Your Action |
|----------|-----------|---------------|-------------|
| Strategy bug: rapid-fire orders | Order rate limiter (>10/min) | Block excess orders + Telegram 🟠 | Review strategy logic, fix, restart Shadow |
| Daily loss limit hit | RiskEngine PnL check | HALT + cancel all + Telegram 🔴 | Investigate cause, decide: resume tomorrow or fix first |
| Broker disconnect mid-session | Reconnect logic + Telegram 🟡 | Auto-reconnect (backoff + flap) | Monitor — if >3 reconnects, check network |
| Fill callback missing | Reconciliation mismatch | Telegram 🟠 at 13:50 | Check broker portal manually, file as incident |
| Position not flat at 13:45 | Force-flat failure | Telegram 🔴 | Log into broker portal, close manually IMMEDIATELY |
| ClickHouse down | Recorder WAL fallback | Auto-WAL, Telegram 🟡 | Restart ClickHouse, WAL replay will catch up |
| Engine OOM | Systemd MemoryMax kill | Auto-restart (max 3/hr) + Telegram 🟠 | Check memory leak, review RSS trend |

### 5.7 Phase 3 Go/No-Go Gate

```
All must PASS to proceed to Phase 4:
  □ 5 consecutive trading days with real trades executed
  □ PnL three-way reconciliation: 100% match for all 5 days
  □ No unplanned HALT (StormGuard normal triggers are OK)
  □ Daily loss limit: not breached (or if breached, HALT worked correctly)
  □ All order/fill callbacks received (0 missing)
  □ Position flat at end of every day (force-flat not triggered, or triggered and succeeded)
  □ Telegram notifications: all events received correctly
  □ Latency: within Shadow-period baseline (no degradation)
  □ No canary rollback trigger fired on counted days
  □ Operator verdict recorded for all sessions
  □ At least one controlled strategy-quarantine drill passed without platform-wide interruption
  □ At least one controlled platform `reduce-only` drill passed with close-only behavior preserved
  □ At least one manual `/stop` drill performed during a controlled low-risk window
```

---

## 6. Phase 4 — Expansion Validation (Week 10-11)

**Goal**: Expand from Canary (1 strategy, 1 symbol) to near-production configuration.

### 6.1 Expansion Configuration

```yaml
# config/env/prod/expanded.yaml
expanded:
  strategies:
    - id: <strategy-1-id>    # Gate D passed
      symbols: [TMF]
      max_position: 1
    - id: <strategy-2-id>    # Gate D passed
      symbols: [MXF]
      max_position: 1

  risk:
    daily_loss_limit_ntd: -10000  # unchanged
    max_open_positions: 3
    max_order_per_min: 20
    order_size_limit: 1           # per strategy
```

### 6.2 New Validation Items

- Multi-strategy concurrent ordering (idempotency key uniqueness)
- Multi-symbol exposure tracking correctness
- Survive at least 1 volatile market day (intraday swing > 1%)
- Manual kill-switch test: send Telegram `/stop` → verify HALT + cancel

### 6.3 Go/No-Go Gate

```
  □ 5 consecutive trading days, PnL 100% reconciled
  □ Multi-strategy: no mutual interference
  □ At least 1 volatile day survived
  □ Kill-switch manual test PASS
```

---

## 7. Phase 5 — Full Production Declaration (Week 12)

### 7.1 Production Entry Criteria

```
  □ Phase 1-4 all Go/No-Go Gates PASSED
  □ Cumulative: ≥20 real trading days
  □ Reconciliation: 100% match
  □ Automation: pre-market, reconciliation, notifications all reliable
```

### 7.2 Post-Launch Monitoring (30-Day)

- Weekly reliability summary (Telegram, Friday)
- Monthly production review pack (PnL, Sharpe, drawdown, system metrics, incidents)
- Decision point: maintain / expand symbols / add strategies / adjust risk

### 7.3 Future Expansion Path (Not in Scope)

- Night session support
- Additional strategies beyond initial 2
- Fubon broker production deployment
- FeatureEngine Rust optimization (when broker API is no longer bottleneck)
- Cloud/hybrid deployment

---

## 8. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Strategy bug causes rapid loss | Medium | High | Daily loss limit + order rate limiter + strategy kill-switch |
| Broker API outage during session | Medium | Medium | Auto-reconnect + StormGuard HALT + cancel pending |
| ClickHouse crash | Low | Low | WAL fallback, auto-replay on recovery |
| Network partition (server ↔ broker) | Low | High | Watchdog → systemd restart; if persistent → HALT |
| Memory leak over days | Medium | Medium | MemoryMax OOM protection + RSS trend monitoring |
| Reconciliation drift undetected | Low | Critical | Three-way daily check + mismatch → next-day HALT |
| Solo operator unavailable | Medium | High | All critical paths auto-protected; HALT is safe default |
| Unintended overnight position | Low | High | Force-flat at 13:43 + Telegram CRITICAL if fails |

---

## Appendix A: New Files & Modules

```
New source files:
  src/hft_platform/notifications/__init__.py
  src/hft_platform/notifications/telegram.py
  src/hft_platform/notifications/dispatcher.py
  src/hft_platform/notifications/templates.py
  src/hft_platform/ops/autonomy.py
  src/hft_platform/ops/strategy_governor.py
  src/hft_platform/ops/platform_degrade.py
  src/hft_platform/ops/manual_rearm.py

New scripts:
  scripts/daily_reconcile.py
  scripts/pre_market_check.py
  scripts/weekly_summary.py

New config:
  config/env/prod/risk.yaml
  config/env/prod/canary.yaml
  config/env/prod/expanded.yaml

New infra:
  ops/hft-engine.service (systemd unit)
  ops/wait-for-healthy.sh (startup health gate)
  ops/check-heartbeat.sh (watchdog cron script)

Modified modules:
  src/hft_platform/risk/engine.py          — daily loss limit integration
  src/hft_platform/order/adapter.py        — order rate limiter + shadow mode
  src/hft_platform/order/rate_limiter.py   — sliding-window limits
  src/hft_platform/order/shadow.py         — structured shadow sink
  src/hft_platform/order/halt_canceller.py — HALT cancel fanout
  src/hft_platform/strategy/runner.py      — per-strategy kill-switch
  src/hft_platform/services/system.py      — watchdog heartbeat
  src/hft_platform/execution/eod_recon.py  — in-process EOD trigger
  src/hft_platform/risk/halt_flattener.py  — HALT flatten path

Operator runbooks to update in lockstep:
  docs/runbooks/halt-recovery.md
  docs/runbooks/daily-ops-checklist.md
  docs/operations/incident-response-protocol.md
```

## Appendix B: Environment Variables (New)

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_TELEGRAM_BOT_TOKEN` | — | Telegram bot token |
| `HFT_TELEGRAM_CHAT_ID` | — | Your Telegram chat ID |
| `HFT_TELEGRAM_ENABLED` | `0` | Enable Telegram notifications |
| `HFT_DAILY_LOSS_LIMIT` | `-10000` | Daily loss limit in NTD |
| `HFT_FORCE_FLAT_TIME` | `13:43` | Force close all positions time |
| `HFT_AUTO_STOP_TIME` | `13:40` | Stop new orders time |
| `HFT_EMERGENCY_HALT_REDIS_KEY` | `hft:emergency_halt` | Redis key for emergency halt (set by Telegram /stop) |
