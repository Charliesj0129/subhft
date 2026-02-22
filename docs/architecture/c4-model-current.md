# HFT Platform C4 Model (Current State)

Date: 2026-02-21
Scope: As-built architecture from `src/hft_platform`, `research`, `rust_core`, and `docker-compose.yml`.
Note: CE-M2 (GatewayService) and CE-M3 (WALFirstWriter, DiskPressureMonitor) are implemented as of 2026-02-21.

## 1. C1 - System Context

```mermaid
flowchart LR
  trader[Trader or Operator]
  researcher[Quant Researcher]
  shioaji[Shioaji API]
  clickhouse[(ClickHouse)]
  observability[Prometheus / Grafana / Alertmanager]
  redis[(Redis - optional)]
  hft[HFT Platform]

  trader -->|run sim/live, monitor, operate| hft
  researcher -->|alpha validate/promote/canary| hft
  hft -->|subscribe quote, place/cancel/amend| shioaji
  hft -->|write market/execution/latency data| clickhouse
  hft -->|expose metrics| observability
  hft -->|optional state cache| redis
```

## 2. C2 - Container Diagram

```mermaid
flowchart TB
  subgraph hftsys[Software System: HFT Platform]
    cli[CLI and Config Layer\nsrc/hft_platform/cli.py\nsrc/hft_platform/config/*]
    engine[Runtime Engine\nsrc/hft_platform/services/system.py]
    gateway[GatewayService - CE-M2\nsrc/hft_platform/gateway/\nenabled via HFT_GATEWAY_ENABLED=1]
    rec[Recorder and WAL Path\nsrc/hft_platform/recorder/*\nmode=direct or wal_first - CE-M3]
    loader[WAL Loader\nsrc/hft_platform/recorder/loader.py]
    alpha[Alpha Governance Pipeline - Offline\nsrc/hft_platform/alpha/*\nvalidation, promotion, canary,\npool, experiments, audit]
    research[Research Toolchain\nresearch/registry\nresearch/backtest\nresearch/combinatorial\nresearch/rl]
    rustcore[Rust Core Extension\nrust_core]
    ruststrategy[Rust Strategy Extension\nrust\nnot wired into runtime path]
    monitor[Runtime Monitor Script\nscripts/monitor_runtime_health.py]
    wal[(WAL .wal/\nDiskPressureMonitor)]
    exp[(Research Experiments\nresearch/experiments/runs)]
    promos[(Promotion Configs\nconfig/strategy_promotions)]
  end

  shioaji[Shioaji API]
  clickhouse[(ClickHouse)]
  observability[Prometheus / Grafana / Alertmanager]

  cli --> engine
  cli --> alpha
  alpha --> research
  research --> exp
  alpha --> promos
  engine -->|hot path acceleration| rustcore
  engine --> shioaji
  engine -->|OrderIntent| gateway
  gateway -->|OrderCommand| engine
  engine --> rec
  rec --> clickhouse
  rec --> wal
  wal --> loader
  loader --> clickhouse
  engine --> observability
  monitor --> engine
  monitor --> clickhouse
  alpha --> clickhouse
```

## 3. C3 - Component Diagram (Runtime Engine)

```mermaid
flowchart LR
  subgraph runtime[Container: Runtime Engine]
    system[HFTSystem Supervisor]
    bootstrap[SystemBootstrapper]
    bus[RingBufferBus]
    md[MarketDataService]
    strat[StrategyRunner]
    risk[RiskEngine]
    order[OrderAdapter]
    execgw[ExecutionGateway]
    execrouter[ExecutionRouter]
    recon[ReconciliationService]
    pos[PositionStore]
    recorder[RecorderService]
    storm[StormGuard]

    rawq[(raw_queue)]
    riskq[(risk_queue)]
    orderq[(order_queue)]
    rawexecq[(raw_exec_queue)]
    recq[(recorder_queue)]
  end

  subgraph gw[GatewayService - CE-M2\nHFT_GATEWAY_ENABLED=1]
    gwsvc[GatewayService\ngateway/service.py]
    exposure[ExposureStore\ngateway/exposure.py]
    dedup[IdempotencyStore\ngateway/dedup.py]
    policy[GatewayPolicy FSM\ngateway/policy.py]
    intentch[(LocalIntentChannel\ngateway/channel.py)]
  end

  subgraph walplane[Persistence Plane - CE-M3]
    walfirst[WALFirstWriter\nrecorder/wal_first.py]
    diskmon[DiskPressureMonitor\nrecorder/disk_monitor.py]
    wal[(WAL .wal/)]
  end

  shioaji[Shioaji API]
  clickhouse[(ClickHouse)]

  system --> bootstrap
  bootstrap --> md
  bootstrap --> strat
  bootstrap --> risk
  bootstrap --> order
  bootstrap --> execgw
  bootstrap --> execrouter
  bootstrap --> recon
  bootstrap --> recorder

  shioaji --> rawq
  rawq --> md
  md --> bus
  bus --> strat
  strat -->|OrderIntent| intentch
  intentch --> gwsvc
  gwsvc --> exposure
  gwsvc --> dedup
  gwsvc --> policy
  gwsvc --> risk
  risk --> orderq
  orderq --> order
  execgw --> order
  order --> shioaji
  shioaji --> rawexecq
  rawexecq --> execrouter
  execrouter --> bus
  execrouter --> pos
  md --> recq
  execrouter --> recq
  recq --> recorder
  recorder -->|direct mode| clickhouse
  recorder -->|wal_first mode| walfirst
  walfirst --> diskmon
  walfirst --> wal

  bus --> storm
  recon --> storm
  storm --> risk
  storm --> order
  storm --> gwsvc
```

## 4. C3 - Component Diagram (Alpha and Research Pipeline)

```mermaid
flowchart LR
  subgraph alpha[Container: Alpha Governance Pipeline - Offline CLI]
    validate[Validation Pipeline\nGate A/B/C\nsrc/hft_platform/alpha/validation.py]
    pool[Pool and Correlation Tools\nsrc/hft_platform/alpha/pool.py]
    promote[Promotion Pipeline\nGate D/E\nsrc/hft_platform/alpha/promotion.py]
    canary[Canary Monitor\nsrc/hft_platform/alpha/canary.py]
    audit[Audit Logger\nsrc/hft_platform/alpha/audit.py]
    exptracker[ExperimentTracker\nsrc/hft_platform/alpha/experiments.py]
  end

  subgraph research[Container: Research Toolchain]
    registry[AlphaRegistry\nresearch/registry/alpha_registry.py]
    backtest[ResearchBacktestRunner\nresearch/backtest/hbt_runner.py]
    scorecard[Scorecard Builder\nresearch/registry/scorecard.py]
    search[Combinatorial Search Engine\nresearch/combinatorial/search_engine.py]
    rl[RL Lifecycle\nresearch/rl/lifecycle.py]
  end

  alphas[(research/alphas/<alpha_id>/)]
  exps[(research/experiments/runs/<run_id>/meta.json)]
  promos[(config/strategy_promotions/YYYYMMDD/<alpha_id>.yaml)]
  clickhouse[(ClickHouse audit.alpha_*\nnot auto-bootstrapped - D5 gap)]

  alphas --> registry
  registry --> validate
  validate --> backtest
  backtest --> scorecard
  scorecard --> validate
  validate --> exptracker
  validate --> audit
  exptracker --> exps
  search --> validate
  pool --> promote
  validate --> promote
  promote --> promos
  promote --> audit
  canary --> promos
  canary --> audit
  rl --> exptracker
  rl --> promote
  audit --> clickhouse
```

## 5. C4 - Code-Level Dependency Map (Representative)

```mermaid
flowchart LR
  subgraph runtime_code[Runtime code paths]
    hftsystem[src/hft_platform/services/system.py]
    bootstrap[src/hft_platform/services/bootstrap.py]
    md[src/hft_platform/services/market_data.py]
    bus[src/hft_platform/engine/event_bus.py]
    strat[src/hft_platform/strategy/runner.py]
    risk[src/hft_platform/risk/engine.py]
    order[src/hft_platform/order/adapter.py]
    execrouter[src/hft_platform/execution/router.py]
    recorder[src/hft_platform/recorder/worker.py]
    writer[src/hft_platform/recorder/writer.py]
  end

  subgraph gateway_code[Gateway code paths - CE-M2]
    gwservice[src/hft_platform/gateway/service.py]
    gwchannel[src/hft_platform/gateway/channel.py]
    gwexposure[src/hft_platform/gateway/exposure.py]
    gwdedup[src/hft_platform/gateway/dedup.py]
    gwpolicy[src/hft_platform/gateway/policy.py]
  end

  subgraph recorder_ce3[Recorder CE-M3 paths]
    walfirst[src/hft_platform/recorder/wal_first.py]
    diskmon[src/hft_platform/recorder/disk_monitor.py]
    recmode[src/hft_platform/recorder/mode.py]
    shardclaim[src/hft_platform/recorder/shard_claim.py]
    replaycontract[src/hft_platform/recorder/replay_contract.py]
  end

  subgraph alpha_code[Alpha governance code paths]
    validate[src/hft_platform/alpha/validation.py]
    promote[src/hft_platform/alpha/promotion.py]
    canary[src/hft_platform/alpha/canary.py]
    experiments[src/hft_platform/alpha/experiments.py]
    pool[src/hft_platform/alpha/pool.py]
    alphaaudit[src/hft_platform/alpha/audit.py]
  end

  subgraph research_code[Research code paths]
    registry[research/registry/alpha_registry.py]
    scorecard[research/registry/scorecard.py]
    hbt[research/backtest/hbt_runner.py]
    search[research/combinatorial/search_engine.py]
    rl[research/rl/lifecycle.py]
  end

  rustcore[rust_core/src/lib.rs]

  hftsystem --> bootstrap
  bootstrap --> md
  bootstrap --> strat
  bootstrap --> risk
  bootstrap --> order
  bootstrap --> execrouter
  bootstrap --> recorder
  bootstrap -->|optional HFT_GATEWAY_ENABLED=1| gwservice
  md --> bus
  recorder --> writer
  recorder -->|wal_first mode| walfirst
  walfirst --> diskmon
  walfirst --> recmode
  md --> rustcore
  bus --> rustcore
  strat --> gwchannel
  gwservice --> gwchannel
  gwservice --> gwexposure
  gwservice --> gwdedup
  gwservice --> gwpolicy

  validate --> registry
  validate --> hbt
  validate --> scorecard
  validate --> experiments
  validate --> alphaaudit
  promote --> canary
  promote --> alphaaudit
  pool --> experiments
  search --> validate
  rl --> experiments
  rl --> promote
  canary --> alphaaudit
```

## 7. Hardening Backlog: CE-M2 (Gateway)

CE-M2 core implemented 2026-02-21. Enabled via `HFT_GATEWAY_ENABLED=1`.
Design review: `.agent/library/design-review-artifacts.md` § CE-M2.
Issue backlog: `.agent/library/cluster-evolution-backlog.md` § 2.

**Hardening TODO checklist**:
- [ ] [CE2-07] Add `gateway_dispatch_latency_ns`, `gateway_reject_total`, `gateway_dedup_hits_total` to Prometheus + dashboard
- [ ] [CE2-08] Chaos test: multi-runner + gateway outage, verify no duplicate broker dispatch
- [ ] [CE2-09] Active/standby gateway HA with leader lease; only leader dispatches to broker
- [ ] [CE2-11] `quote_version=v1` enforced with schema guard and reject-and-alert on mismatch

**Implemented components**:
- `gateway/channel.py` — `LocalIntentChannel` (bounded asyncio queue, ack/nack, DLQ)
- `gateway/exposure.py` — `ExposureStore` (atomic CAS, memory-bounded CE2-12)
- `gateway/dedup.py` — `IdempotencyStore` (fixed-capacity dedup window)
- `gateway/policy.py` — `GatewayPolicy` FSM (NORMAL/DEGRADED/HALT)
- `gateway/service.py` — `GatewayService` (asyncio dispatch loop, 7-step pipeline)

## 8. Hardening Backlog: CE-M3 (WAL-First)

CE-M3 core implemented 2026-02-21. Enabled via `HFT_RECORDER_MODE=wal_first`.
Design review: `.agent/library/design-review-artifacts.md` § CE-M3.
Issue backlog: `.agent/library/cluster-evolution-backlog.md` § 3.

**Hardening TODO checklist**:
- [ ] [CE3-03] Scale-out WAL loader workers with shard-claim protocol + integration tests (2 loaders, no dup inserts)
- [ ] [CE3-04] Full replay safety contract tests: ordering + dedup + manifest under restart/crash
- [ ] [CE3-06] WAL SLO metrics: backlog size, replay lag, replay throughput, drain ETA — dashboard
- [ ] [CE3-07] Outage drills: ClickHouse down, slow, WAL disk-full, loader restart + recovery runbook

**Implemented components**:
- `recorder/mode.py` — `RecorderMode` enum (`direct` | `wal_first`)
- `recorder/wal_first.py` — `WALFirstWriter` (WAL-only, disk pressure gated)
- `recorder/disk_monitor.py` — `DiskPressureMonitor` (background daemon, OK/WARN/CRITICAL/HALT)
- `recorder/shard_claim.py` — `FileClaimRegistry` (fcntl exclusive file ownership)
- `recorder/replay_contract.py` — `ReplayContract` type definitions

## 6. Code Anchors

**Runtime core**:
- Runtime composition: `src/hft_platform/services/bootstrap.py`
- Runtime supervisor: `src/hft_platform/services/system.py`
- Runtime event bus: `src/hft_platform/engine/event_bus.py`
- Runtime market data: `src/hft_platform/services/market_data.py`
- Runtime recorder: `src/hft_platform/recorder/worker.py`
- Runtime WAL replay: `src/hft_platform/recorder/loader.py`

**Gateway (CE-M2)**:
- Intent channel: `src/hft_platform/gateway/channel.py`
- Exposure tracking: `src/hft_platform/gateway/exposure.py`
- Deduplication: `src/hft_platform/gateway/dedup.py`
- Policy FSM: `src/hft_platform/gateway/policy.py`
- Gateway service: `src/hft_platform/gateway/service.py`

**WAL-first recorder (CE-M3)**:
- Recorder mode: `src/hft_platform/recorder/mode.py`
- WAL-first writer: `src/hft_platform/recorder/wal_first.py`
- Disk pressure monitor: `src/hft_platform/recorder/disk_monitor.py`
- Shard claim: `src/hft_platform/recorder/shard_claim.py`
- Replay contract: `src/hft_platform/recorder/replay_contract.py`

**Alpha governance**:
- Alpha validation: `src/hft_platform/alpha/validation.py`
- Alpha promotion: `src/hft_platform/alpha/promotion.py`
- Alpha canary: `src/hft_platform/alpha/canary.py`
- Alpha pool: `src/hft_platform/alpha/pool.py`
- Alpha experiments: `src/hft_platform/alpha/experiments.py`
- Alpha audit: `src/hft_platform/alpha/audit.py`

**Research toolchain**:
- Registry and scorecards: `research/registry/alpha_registry.py`, `research/registry/scorecard.py`
- Research backtest: `research/backtest/hbt_runner.py`
- Combinatorial search: `research/combinatorial/search_engine.py`
- RL lifecycle: `research/rl/lifecycle.py`

**Rust**:
- Rust extension module: `rust_core/src/lib.rs`

**Design reviews**:
- hft-architect artifacts: `.agent/library/design-review-artifacts.md`
