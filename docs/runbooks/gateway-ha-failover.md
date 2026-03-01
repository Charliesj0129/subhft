# Runbook: Gateway Active/Standby Failover (CE2-09)

## Purpose

Operate the CE-M2 Gateway active/standby mode with file-based leader lease so only the leader dispatches broker commands.

## Feature Flags / Config

| Variable | Default | Purpose |
| --- | --- | --- |
| `HFT_GATEWAY_HA_ENABLED` | `0` | Enable leader lease gating in `GatewayService` |
| `HFT_GATEWAY_LEADER_LEASE_PATH` | `.state/gateway_leader.lock` | Shared lease file path (same host/volume) |
| `HFT_GATEWAY_LEADER_LEASE_REFRESH_S` | `0.5` | Heartbeat / lease refresh loop interval |

## Expected Behavior

1. Exactly one gateway process holds the leader lease and dispatches broker commands.
2. Standby gateways continue processing but reject approved intents at dispatch stage with reason `NOT_LEADER`.
3. If leader exits/crashes, standby acquires lease and begins dispatching.

## Verification

1. Check gateway health snapshots (`leader_active=true` on only one process).
2. Monitor `gateway_reject_total{reason="NOT_LEADER"}` on standby.
3. Monitor `gateway_dispatch_latency_ns` and `gateway_dedup_hits_total` during failover.

## Chaos Drill (CE2-08)

Run the integration chaos test:

```bash
uv run pytest -q --no-cov tests/integration/test_gateway_multi_runner.py::test_gateway_ha_failover_no_duplicate_dispatch
```

Pass criteria:
- No duplicate broker dispatches across failover (`cmd_id` uniqueness)
- Standby takes over after leader task/process termination

## Rollback

Disable HA lease gating:

```bash
export HFT_GATEWAY_HA_ENABLED=0
```

Restart gateway service(s). This returns to single-instance semantics.

