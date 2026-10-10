---
name: runtime-diagnostics
description: "Triage runtime health (metrics, StormGuard, queues, WAL, feed, reconnect) and verify data flow, locally or read-only on a host. Use for health checks, post-restart validation, missing data, stale metrics, alert triage. Not for deploying, restarting, or schema work."
---

# Runtime diagnostics

Read-only. Any action on the production host follows `.agent/rules/41-deployment.md`
(read-only first; never restart to "see if it helps"). Local stack: `docker compose ps`.

## Triage order

1. Liveness: containers/processes up, `curl -fsS localhost:9090/metrics | head`, Redis ping,
   `curl -fsS localhost:8123/ping`. Confirm Prometheus actually scrapes each
   target (`up` per target): a dead scrape reads as healthy silence.
2. Signals: loop lag, queue depth, StormGuard state, fill activity, WAL growth, disk headroom.
3. Feed and session: `feed_events_total` rising during market hours; `subscribed_*`
   counts; per-shard quote health, not aggregates; login flags, not `FeedState` alone.
4. Recorder: queue depth, drops (should be 0), WAL file count, ClickHouse reachable.
5. Logs: `docker compose logs hft-engine --tail=100`; then the matching runbook.

Run `make pre-market-check` / `make post-market-check` / `make recorder-status`
for the standard sweeps.

## Traps that produce wrong conclusions

- **Registry-wide metrics lie both ways.** A metric that was never set reads as a
  confident `0`. Confirm the producer exists and has run before trusting `0`.
- `event_loop_lag_ms` is not literally loop lag; use the loop-lag metric.
- "No data" means a break only while the session is open; degraded clusters and
  detectors firing at every close are session edges, not incidents.
- A gauge exported is not always the one gated (the drawdown gauge read 0.0
  while the gate acted on -111 bps). Read the gate's source.
- Alerts: list history from Prometheus `ALERTS` over a range, not only
  Alertmanager's current view; read `/api/v2/alerts` at a deploy baseline.
- `HFT_ORDER_MODE=sim` still dispatches; fills in sim are simulated.
- Check `RestartCount`, not the logs, to see whether a boot halt restarted into trading.
- Audit tables outlive logs: list `audit.*` before calling a cause unknowable.

## StormGuard

`NORMAL -> WARM -> STORM -> HALT`. STORM and HALT block new orders; cancels
always pass. HALT clears only through the documented recovery (`ops/manual_rearm.py`);
see `docs/runbooks/halt-recovery.md` and `StormGuardHalt.md`.

## Data-flow verification (after feed, normalizer, LOB, or recorder changes)

Hot path: broker -> normalizer -> LOB -> feature -> bus -> strategy -> risk -> adapter.
Recording path: recorder queue -> batcher -> writer -> ClickHouse or WAL.

| Symptom | First checks |
|---|---|
| no `feed_events_total` | broker login in engine logs; session open? |
| high `raw_queue_depth` | normalizer latency; blocking call; backpressure |
| no ClickHouse rows today | recorder drops; ClickHouse `SELECT 1`; WAL growing? |
| WAL files growing | ClickHouse write failures; replay running? |
| `strategy_intents_total` flat | strategy enabled? `HFT_MODE`; one-sided book? |

Row check (guarded): `SELECT count() FROM hft.market_data WHERE toDate(exch_ts/1e9)=today()`
via `make ch-query-guard-run`.

Invariants to spot-check: bounded queues with drop policy; `timebase.now_ns()`
for timestamps; scaled-int prices downstream of the normalizer.

## Runbooks (open the one that matches)

`docs/runbooks/`: `clickhouse-down.md`, `recorder-recovery-from-ck-down.md`,
`recorder-wal-disk-pressure.md`, `wal-replay.md`, `feed-reconnect.md`,
`halt-recovery.md`, `disk-crisis-sop.md`, `strategy-rollback.md`,
`strategy-quarantine.md`, `incident-diagnostics.md`, `daily-ops-checklist.md`,
`TelegramAlertTriage.md`. Exact metric names: `src/hft_platform/observability/metrics.py`.

## Done when

Each claim cites a metric value, a log line, or a query result with its time
range and source, and says what was not checked.
