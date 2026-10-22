---
name: hft-ops
description: "Operations: session governor, autonomy degradation and manual rearm, position flattening, margin monitor, pre/post-market checks, drills, local docker stack. Use when editing ops/ or operating the local stack. Not for production-host deploys (rules/41-deployment.md)."
---

# Operations

`src/hft_platform/ops/` plus the local runtime. Anything on the production host
(THESHOW) is governed by `.agent/rules/41-deployment.md`, never by this file.

## Session phases (wall-clock, not event-driven)

```
INIT(0) -> PRE_OPEN(1) -> OPEN(2) -> CLOSE_ONLY(3) -> FORCE_FLAT(4) -> CLOSED(5)
```

`session_governor.py` (`SessionGovernor`, `TrackGate`, `SessionPhase`) is driven
by `config/base/session_governor.yaml`, one track per product family. `TrackGate`
gives strategies an O(1) per-symbol phase lookup: `OPEN` normal, `CLOSE_ONLY`
closing orders only, `FORCE_FLAT` flattener active, `CLOSED` intents rejected;
unknown symbols map to CLOSED unless `HFT_TRACK_GATE_DEFAULT_OPEN=1`.

## Autonomy degradation

`autonomy_monitor.py` watches ClickHouse write staleness, feed gaps and
reconnect flapping, queue depth, RSS, drawdown, reconciliation drift, WAL
backlog. Modes (`autonomy.py`): `NORMAL -> PLATFORM_REDUCE_ONLY -> HALT`.

```
NORMAL --auto--> PLATFORM_REDUCE_ONLY --auto (critical)--> HALT
HALT --X auto--> NORMAL        (only manual_rearm.py, an operator action)
```

- HALT blocks new orders; cancels stay allowed. Never auto-recover from HALT.
- A latch must be clearable by something that does not depend on it already
  being clear. A HALT can fire on a profit; HALT stops orders, it does not flatten.
- `position_flattener.py` closes positions with a 120 s deadline; a timeout is a
  failure to escalate. `flatten_gate.py` filters FORCE_FLAT to close-only orders.
- Autonomy reason codes are a frozen set (for metrics): add a code to the set
  before using it.
- Other modules: `margin_monitor`, `platform_degrade`, `strategy_governor`,
  `backup`, `config_snapshot`, `daily_pnl_report`, `evidence`, `preflight_checker`.

## Commands

```bash
make pre-market-check      # containers, ClickHouse, Redis, WAL backlog, Prometheus scrape
make post-market-check     # WAL drained, recorder healthy, row counts, PnL reconciled
make recorder-status
uv run hft check           # config validation
make drill-ck-down         # 30 s ClickHouse outage (WAL fallback)
make drill-wal-pressure
make drill-recon-mismatch
make rollback-drill
make canary-auto
```

## Local stack

Local only: `docker compose up -d --build`, `docker compose ps`, `docker compose
logs -f hft-engine`, or `make start` / `make stop` / `make logs`. Production
uses different commands (`up -d` there destroys the engine's writable layer).

Ports: engine metrics 9090, ClickHouse 8123/9000, Redis 6379, Prometheus 9091,
Grafana 3000, Alertmanager 9093. Key env: `HFT_MODE`, `HFT_ORDER_MODE`,
`HFT_CLICKHOUSE_ENABLED`, `HFT_RECORDER_MODE`, `HFT_GATEWAY_ENABLED`,
`HFT_OBS_POLICY`; the rest is in `config-env`. Live-impacting config changes
follow `docs/operations/change-control.md` (what, why, risk, rollback; test in
`HFT_MODE=sim`; watch metrics; keep a rollback ready).

## Done when

Behavior is covered by tests of the state transition (including the edge that
must not be taken), `make pre-market-check` is green locally, and nothing in
the change auto-deploys or auto-recovers from HALT.
