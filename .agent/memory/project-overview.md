# Project Overview (stable orientation)

Record here: purpose, stack, runtime map, key invariants, canonical doc
pointers — only what stays true for months. Do NOT record: anything dated,
task state, metrics, counts that drift.

- Purpose: money-facing HFT platform for Taiwan markets (TAIFEX/TWSE) via
  Shioaji + Fubon; plus a gated alpha research program in `research/` that
  never directly enables live trading.
- Stack: Python 3.12 (uv) + Rust/PyO3 (`rust_core/`); ClickHouse + WAL; Redis;
  Prometheus/Grafana/Alertmanager; Telegram bot.
- Runtime map: Exchange -> BrokerFacade -> Normalizer -> LOBEngine ->
  FeatureEngine -> RingBufferBus -> StrategyRunner -> Risk/Gateway ->
  OrderAdapter -> Execution/Positions -> Recorder WAL/ClickHouse.
- Invariants: prices scaled int x10000 platform-wide (research ClickHouse raw
  scale is x1,000,000 — convert explicitly, see `core/pricing.PriceCodec`);
  local timestamps via `timebase.now_ns()`; broker callbacks cross threads
  only via `call_soon_threadsafe`; recording never blocks the hot path;
  broker SDK imports confined to `feed_adapter/<broker>/`; HALT blocks new
  orders but cancels remain allowed.
- Alpha governance: Gates A-F -> Canary -> Shadow -> Live; live registry
  FROZEN under loop_v1 L11 (`r47_tmf_v1`).
- Canonical docs: `CLAUDE.md`, `docs/MODULES_REFERENCE.md`,
  `docs/architecture/pipeline-chains.md`,
  `docs/runbooks/alpha-development-workflow.md`,
  `docs/loop_v1_stabilization_charter.md`.
