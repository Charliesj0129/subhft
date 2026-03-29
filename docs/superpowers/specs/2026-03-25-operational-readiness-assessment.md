# Operational Readiness Assessment: Independent Quant Team Foundation

**Date**: 2026-03-25
**Scope**: Single-operator readiness for futures day trading (Mode A), with evolution path to 24/7 autonomous (Mode B)
**Overall Score**: 35/40 (87.5%)
**Verdict**: Foundation is sufficient. No structural gaps. P0 fixes (1 week) unlock Mode A; 6 more weeks unlock Mode B.

---

## Context

### User Profile
- Personal small quant team (1-2 people)
- Currently pure futures (TXF/MXF/EXF), planning stock expansion in ~6 months
- Target: Mode A first (unattended during trading, manual pre/post-market), evolving to Mode B (24/7 autonomous)

### Assessment Framework
- **Framework C (Hybrid)**: Trading Day Walkthrough + Capability Matrix
- Produces: daily walkthrough, 8-dimension radar, P0/P1/P2 gap list, evolution roadmap

---

## 1. Trading Day Walkthrough (Futures Day Session)

| Time | Stage | Module | Status | Notes |
|------|-------|--------|--------|-------|
| 08:30 | Pre-market health check | `pre_market_check` (Makefile) | READY | ClickHouse/Redis/Broker connectivity |
| 08:30 | SessionGovernor PRE_OPEN | `session_governor.py` | READY | Multi-track YAML scheduling, 5-phase FSM |
| 08:45 | Broker login + subscribe | `shioaji/session_runtime.py` | READY | TLS login, contract load, quote subscribe |
| 08:45 | Strategy warm-up | `strategy/runner.py` | READY | FeatureEngine warm-up, ring buffer fill |
| 09:00 | OPEN - trading begins | `TrackGate` -> `StrategyRunner` | READY | Per-symbol phase filter, O(1) lookup |
| 09:00-13:30 | Intraday risk | StormGuard + 6 validators | READY | NORMAL->WARM->STORM->HALT, drift-burst toxicity |
| 09:00-13:30 | Intraday PnL watermark | `DailyLossLimitValidator` | READY | Soft limit -> peak drawdown -> hard stop, auto-reset |
| 09:00-13:30 | Reconnection | `reconnect_orchestrator.py` | READY | Exponential backoff 30s->600s, trading hours guard |
| 09:00-13:30 | Orphan order detection | `orphan_detector.py` | READY | Broker vs local comparison, 60s stale threshold |
| 09:00-13:30 | Position reconciliation | `reconciliation.py` | READY | 5s interval, drift_streak tracking, 2x drift -> reduce-only |
| 09:00-13:30 | AutonomyMonitor patrol | `autonomy_monitor.py` | READY | 5s poll, 4 signal types (StormGuard/Broker/Infra/Recon) |
| 09:00-13:30 | Real-time notifications | `NotificationDispatcher` | READY | Telegram 16 event templates, critical bypass rate limit |
| 09:00-13:30 | Data persistence | `recorder/` + WAL | READY | ClickHouse + WAL fallback, fsync, disk pressure breaker |
| 13:00 | CLOSE_ONLY | `SessionGovernor` | READY | Phase transition callback |
| 13:30 | FORCE_FLAT | `position_flattener.py` | READY | all/track/strategy scope, 120s timeout, retry |
| 13:45 | CLOSED | `SessionGovernor` | READY | Terminal state |
| 13:45 | Daily report | Notification templates + Evidence | WEAK | Templates exist, but no auto-trigger orchestrator |
| 13:45 | TCA slippage analysis | `tca/types.py` | WEAK | Data structures only, analysis engine not implemented |
| 17:00 | Post-market review | ClickHouse queries | READY | Direct query on `hft.market_data`, `hft.trades` |

**Result: 15 READY, 2 WEAK, 0 MISSING**

Both weak points are "data collection done, analysis/report automation incomplete" -- no impact on trading safety, affects post-market efficiency only.

---

## 2. Capability Radar (8 Dimensions, 1-5 scale)

| Dimension | Score | Justification |
|-----------|-------|---------------|
| **Trade Execution** | 4/5 | Complete tick->intent->risk->order->fill pipeline; dual broker (Shioaji+Fubon); circuit breaker + dead letter + orphan detector. -1: TCA analysis layer incomplete |
| **Risk Control** | 5/5 | 6 validators + StormGuard 4-state FSM + drift-burst toxicity + intraday watermark (soft/peak/hard) + position reconciliation + circuit breaker |
| **Autonomous Operations** | 4/5 | SessionGovernor multi-track + AutonomyMonitor 4-signal patrol + PositionFlattener + platform degradation. -1: daily report orchestrator not automated |
| **Data Persistence** | 5/5 | ClickHouse direct + WAL fallback + fsync + disk pressure breaker + dedup replay + manifest tracking |
| **Observability** | 4/5 | Prometheus + Grafana + structlog + Telegram 16 event templates + TUI monitor. -1: missing attribution analysis dashboard |
| **Alpha Research** | 5/5 | Gates A-F fully automated + canary FSM + drift detector + batch promotion + 16 alpha templates |
| **Resilience/Recovery** | 4/5 | Exponential backoff reconnect + trading hours guard + quote verification + heartbeat + WAL replay. -1: chaos testing not yet executed |
| **Deployment/Ops** | 4/5 | Docker Compose full stack + ops.sh (tune/isolate/replay) + 160+ Makefile targets. -1: no CI/CD pipeline, no config validation |

**Total: 35/40 (87.5%)**

### Interpretation
- Three 5/5 dimensions (Risk, Persistence, Alpha Research) are the moat -- the biggest risks for solo quant ops (blowup, data loss, alpha misfire) are fully covered
- Four 4/5 dimensions are "feature complete but missing the last mile" -- orchestration, automation, and validation gaps, not architectural deficiencies
- No dimension below 4/5 -- no structural weaknesses

---

## 3. Gap List: 4->5 Remediation

### Dimension: Trade Execution (4->5)

| ID | Gap | Current State | Fix | Effort | Priority |
|----|-----|---------------|-----|--------|----------|
| E1 | TCA analysis engine | `tca/types.py` data structures only | `TCAAnalyzer`: aggregate per-fill slippage, implementation shortfall, realized spread from ClickHouse `hft.slippage_records`. **Depends on E2** (needs fee data for meaningful analysis) | 3-4 days | P1 |
| E2 | Fee calculation integration | `config/base/fees/futures.yaml` exists | Inject `FeeCalculator` into ExecutionRouter, auto-compute commission+tax per fill, write `FeeBreakdown`. **E1 depends on this** | 2 days | P1 |
| E3 | TCA daily CLI | None | `hft tca daily --date 2026-03-25`: per-strategy slippage, cost breakdown, fill quality | 2 days | P2 |
| E4 | Execution quality dashboard | None | Grafana panel: avg slippage, fill rate, cancel rate by strategy/symbol | 1-2 days | P2 |

### Dimension: Autonomous Operations (4->5)

| ID | Gap | Current State | Fix | Effort | Priority |
|----|-----|---------------|-----|--------|----------|
| A1 | Daily report orchestrator | Templates + Evidence exist, no auto-trigger | `DailyReportService`: SessionGovernor CLOSED callback -> collect from ClickHouse -> `notify_daily_report()` + `write_daily_summary()` | 2 days | P0 |
| A2 | `hft ops flatten` implementation | CLI stub, prints TODO | Connect to running PositionFlattener via Unix socket or shared state, support `--scope all/track/strategy` | 2 days | P0 |
| A3 | Weekly report automation | `notify_weekly_summary()` template exists | Sunday cron trigger, aggregate 5 daily reports -> Telegram weekly summary | 1 day | P2 |

### Dimension: Observability (4->5)

| ID | Gap | Current State | Fix | Effort | Priority |
|----|-----|---------------|-----|--------|----------|
| O1 | Strategy PnL attribution | ClickHouse has raw data | Per-strategy daily PnL breakdown (gross/net/fees/slippage), leveraging TCA data | 2 days | P1 |
| O2 | Grafana operations dashboard | Metrics endpoint exists, no dashboard JSON | Single-page dashboard: real-time PnL, positions, StormGuard state, reconnect count, queue depth | 1-2 days | P1 |
| O3 | Alpha performance tracking | Canary metrics written to ClickHouse | Per-alpha IC/Sharpe/drawdown time series panel, alpha decay trend visibility | 2 days | P2 |

### Dimension: Resilience/Recovery (4->5)

| ID | Gap | Current State | Fix | Effort | Priority |
|----|-----|---------------|-----|--------|----------|
| R1 | Chaos test playbooks | Infrastructure ready, not executed | 5 standard playbooks: (1) Broker disconnect (2) ClickHouse down (3) Feed gap >30s (4) Position drift (5) Disk full. Each with injection method + expected behavior + verification commands | 2 days | P0 |
| R2 | WAL replay drill | `loader.py` complete | Execute once: stop ClickHouse -> accumulate WAL -> restart -> replay -> verify dedup + row count | 0.5 day | P0 |
| R3 | Quarterly resilience drill SOP | None | `docs/runbooks/quarterly-chaos-drill.md`: checklist + expected results + sign-off fields | 1 day | P1 |
| R4 | Reconnect burn-in evidence | Code complete, no production evidence | Run 5 trading days sim, collect reconnect metrics: success rate, P95 recovery time, quote verification pass rate | 5 days (passive) | P1 |

### Dimension: Deployment/Ops (4->5)

| ID | Gap | Current State | Fix | Effort | Priority |
|----|-----|---------------|-----|--------|----------|
| D1 | GitHub Actions CI | `make ci` manual | `.github/workflows/ci.yml`: push -> lint + typecheck + test + coverage gate | 1 day | P1 |
| D2 | Automated deployment | Manual `docker compose build` | CI green -> build image -> push registry -> SSH deploy (or watchtower) | 2 days | P2 |
| D3 | Config validation | Manual YAML check | Pydantic schema validation for all YAML at startup, fail-fast with clear error | 1 day | P1 |

### Cross-Cutting: Solo Operator Safety (identified by spec review)

| ID | Gap | Current State | Fix | Effort | Priority |
|----|-----|---------------|-----|--------|----------|
| S1 | Margin/capital monitoring | No margin utilization tracking (DailyLossLimit tracks PnL, not margin) | AutonomyMonitor polls broker margin API; alert at 80% utilization, reduce-only at 90% | 2 days | P1 |
| S2 | ClickHouse backup strategy | WAL replay covers write failures, but no backup for disk corruption/loss | Nightly `clickhouse-backup` cron -> local or S3, 7-day retention | 1 day | P1 |
| S3 | Notification channel redundancy | Telegram is sole channel; bot down = all alerts lost | Add webhook fallback (LINE Notify or Discord webhook as secondary); critical alerts fan-out to both | 1 day | P1 |
| S4 | Dead man's switch (Mode B only) | No unacknowledged-alert escalation | If critical alert not acknowledged within N minutes -> auto-flatten + halt. Needed for Phase 2 (night session) | 2 days | P1 (Phase 2) |
| S5 | Live mode startup guard | Only `HFT_ORDER_MODE=sim` default prevents accidental live orders | Startup confirmation gate: `HFT_ORDER_MODE=live` requires `HFT_LIVE_CONFIRM=yes-i-know` or interactive prompt | 0.5 day | P1 |

### Priority Summary

| Priority | Items | Total Effort | Significance |
|----------|-------|--------------|--------------|
| **P0** | A1, A2, R1, R2 | ~6.5 days | **Blockers**: no auto daily report = manual check daily; flatten CLI is safety-critical for emergency ops; chaos not executed = resilience is theoretical |
| **P1** | E1, E2, O1, O2, R3, R4, D1, D3, S1, S2, S3, S5 | ~19.5 days | **Pre-live hardening**: TCA engine, Grafana dashboard, CI pipeline, margin monitoring, backup, notification redundancy, live mode guard |
| **P1 (Phase 2)** | S4 | 2 days | **Mode B blocker**: dead man's switch for unattended night sessions |
| **P2** | E3, E4, A3, O3, D2 | ~8 days | **Nice-to-have**: TCA CLI, weekly report, alpha tracking, auto-deploy |

**Recommended execution**: P0 first (1 week) -> P1 in two batches (2-3 weeks) -> P2 as needed

---

## 4. Evolution Roadmap: Mode A -> Mode B

### Phase 0: P0 Fixes (Week 1)

**Goal**: Eliminate "paper tiger" risks, establish daily operations loop

| Day | Work | Output |
|-----|------|--------|
| Day 1-2 | **A1** Daily report orchestrator | SessionGovernor CLOSED -> auto-collect + send Telegram daily report |
| Day 3-4 | **R1** 5 chaos test playbooks | 5 repeatable fault injection scripts + expected behavior docs |
| Day 5 | **R2** WAL replay live drill | Verification report: WAL accumulation -> replay -> dedup -> row count reconciliation |
| Day 6-7 | **A2** `hft ops flatten` implementation | Emergency flatten CLI operational, connects to running PositionFlattener |

**Phase 0 exit criteria**: Daily report arrives automatically after close + `hft ops flatten --scope all` works + all 5 chaos playbooks PASS

### Phase 1: Mode A Completion (Weeks 2-4)

**Goal**: One person can reliably run day session -- boot in morning, read report after close, get notified on incidents

| Week | Work | Output |
|------|------|--------|
| W2 | **E1** TCA analysis engine + **E2** Fee calculation | Per-fill auto slippage + fee calculation, written to ClickHouse |
| W2 | **D1** GitHub Actions CI | Push -> lint + typecheck + test, green gate for merge |
| W3 | **O1** Strategy PnL attribution + **O2** Grafana ops dashboard | Single-page view: real-time PnL, positions, StormGuard, queue depth |
| W3 | **A2** `hft ops flatten` implementation | Emergency flatten CLI operational (no need to SSH and hack) |
| W4 | **D3** Config validation + **R3** Quarterly drill SOP | Startup fail-fast + drill runbook documented |
| W4 | **R4** Reconnect burn-in begins | 5-day sim data collection (passive) |

**Phase 1 exit criteria**:
- Daily report auto-sent + Grafana dashboard live + TCA data flowing
- `hft ops flatten --scope all` completes in <30 seconds
- CI pipeline green-gating merges
- Chaos 5 playbooks quarterly-repeatable

### Phase 2: Mode B Preparation (Weeks 5-7)

**Goal**: Add night session support, prepare for 24/7 unattended operation

| Week | Work | New Requirement | Notes |
|------|------|-----------------|-------|
| W5 | Night session track config | `session_governor.yaml` add `futures_night` track | Schedule: 15:00 PRE_OPEN -> 15:15 OPEN -> 05:00 CLOSE_ONLY -> 05:15 FORCE_FLAT |
| W5 | Cross-midnight trading_date | Evidence + DailyLossLimit | Night session 22:00 trade belongs to "today" or "tomorrow" -- `set_trading_date()` API exists, wire to SessionGovernor |
| W6 | Heartbeat upgrade | Current: PID file + cron | Need: Telegram heartbeat every 30min (`notify_heartbeat()` template exists), configurable night quiet hours |
| W6 | Process supervisor | Current: cron watchdog | Need: Docker `restart: unless-stopped` or systemd unit, crash -> auto-restart -> notify |
| W6 | **S4** Dead man's switch | No unacknowledged-alert escalation | Critical alert not acked within N min -> auto-flatten + halt. Essential for unattended night sessions |
| W7 | Night session chaos addendum | 2 new playbooks | (6) Cross-midnight reconnect (7) Night feed gap >30s (low liquidity, more likely to trigger) |
| W7 | **A3** Weekly report + P2 wrap-up | Weekly report automation + TCA CLI | Sunday auto-send 5-day aggregation |

**Phase 2 exit criteria**:
- Night session track runs independently, trading_date attribution correct
- 24-hour heartbeat monitoring + crash auto-restart
- Cross-midnight chaos playbooks PASS

### Phase 3: Mode B Go-Live + Stock Expansion Prep (Weeks 8-12)

**Goal**: Actual 24/7 operation + lay groundwork for stock trading in 6 months

| Week | Work | Notes |
|------|------|-------|
| W8-9 | Night session shadow trading | Night strategy runs canary (Gate E), no real orders, collect 5-day execution quality |
| W10 | Night session go-live | Confirm P95 slippage acceptable -> enable live orders |
| W10 | 24/7 burn-in observation | 10 consecutive trading days fully autonomous, only read daily report |
| W11 | Stock track design | `session_governor.yaml` add `stock` track; identify Shioaji stock API differences (T+2 settlement, margin trading) |
| W12 | Stock risk parameters | Stock-specific: price limits (10%), last-call auction, odd lots. DailyLossLimit needs per-track independent watermarks |

**Phase 3 exit criteria**:
- 10 consecutive day+night sessions fully autonomous, daily reports normal, no unexpected HALTs
- Stock track design document complete (spec only, no implementation needed yet)

### Timeline Overview

```
Week  1      P0: Daily report + Chaos + WAL replay
Week  2-4    Phase 1: TCA + Grafana + CI + flatten CLI (Mode A complete)
Week  5-7    Phase 2: Night track + heartbeat + supervisor (Mode B prep)
Week  8-12   Phase 3: Night shadow -> go-live -> stock design (Mode B live)
              |
         ~6 months later: Stock track implementation + T+2 settlement + price limit risk
```

---

## Appendix: Module Verification Summary

All module assessments are based on actual code review, not file existence checks.

| Module | Files | LOC | Status |
|--------|-------|-----|--------|
| SessionGovernor | 1 | ~207 | Production-ready: multi-track, 6 phases (incl. INIT), YAML config |
| AutonomyMonitor | 1 | ~345 | Production-ready: 4 signals, cooldown, decision matrix |
| PositionFlattener | 1 | ~145 | Production-ready: 3 scopes, timeout, retry |
| OrphanDetector | 1 | ~100 | Production-ready: stateless, 60s threshold |
| EvidenceWriter | 1 | ~150 | Production-ready: 9 event types, trading_date override |
| NotificationDispatcher | 1 | ~400 | Production-ready: 16 Telegram event templates |
| DriftBurstDetector | 1 | ~100 | Production-ready: Christensen et al. (2022), ring buffers |
| DailyLossLimitValidator | 1 | ~300 | Production-ready: soft/peak/hard, intraday watermark |
| StormGuard | 1 | ~295 | Production-ready: 4-state FSM, hysteresis, drift-burst integrated |
| Reconciliation | 1 | ~350 | Production-ready: 5s interval, drift_streak, grace failures |
| ReconnectOrchestrator | 1 | ~284 | Production-ready: exponential backoff, quote verification |
| WAL | 2 | ~700 | Production-ready: fsync, disk pressure, dedup replay |
| TCA | 2 | ~100 | Types only: FeeSchedule, SlippageBreakdown, TCADailyReport |
| PlatformDegrade | 1 | ~140 | Production-ready: 4 autonomy levels, intent filtering |
| CircuitBreaker | 1 | ~100 | Production-ready: per-strategy, configurable threshold |
