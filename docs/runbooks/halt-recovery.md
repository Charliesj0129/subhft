# HALT Recovery Runbook

## Overview

When StormGuard enters HALT state, all new order progression is blocked.
Cancel actions remain allowed. This runbook covers identification, verification,
and recovery from a HALT event.

Autonomy control-plane states:
- `STRATEGY_QUARANTINED`: only the offending strategy is isolated; other strategies may continue.
- `PLATFORM_REDUCE_ONLY`: no new opening orders; close/cancel/reconcile/monitoring stay enabled.
- `HALT`: cancel/query/evidence only.

Recovery is explicit. Do not assume the runtime will return to `NORMAL` on its own after a quarantine, `reduce-only`, or `HALT`.

---

## 1. Identify Trigger

### Check StormGuard State Metric

```bash
curl -s http://localhost:9090/metrics | grep hft_storm_guard_state
# Expected output when halted:
#   hft_storm_guard_state{state="HALT"} 1
```

### Check Guardrail Log in ClickHouse

```sql
SELECT
    toDateTime(ts / 1e9) AS event_time,
    trigger_reason,
    strategy,
    symbol,
    details
FROM hft.audit_guardrail_log
WHERE event_time > now() - INTERVAL 1 HOUR
ORDER BY ts DESC
LIMIT 20;
```

### Check structlog Output

```bash
docker compose logs -f hft-engine --since 10m 2>&1 | grep -E 'storm_guard|HALT|halt_trigger'
```

Common trigger reasons:
- `pnl_breach` -- Daily PnL limit exceeded
- `feed_gap` -- Market data feed stale beyond threshold
- `reconciliation_failure` -- Position mismatch between internal and broker
- `manual` -- Operator-initiated halt

---

## 2. Verify Positions

Compare internal position state against broker-reported positions.

### Internal Positions

```sql
SELECT symbol, net_qty, avg_price, realized_pnl
FROM hft.positions
WHERE updated_at > now() - INTERVAL 1 DAY
ORDER BY symbol;
```

### Broker Positions

```bash
# Shioaji
uv run hft admin positions --broker shioaji

# Fubon
uv run hft admin positions --broker fubon
```

### Reconciliation Diff

```bash
uv run hft admin reconcile --verbose
```

If discrepancies exist, record them before proceeding.

---

## 3. Decision Tree

### PnL Breach

1. Confirm realized + unrealized PnL from both internal and broker sources.
2. If PnL breach is genuine:
   - Do NOT resume trading.
   - The platform exit (`risk/loss_exit.py`) sends a FORCE_FLAT for every open
     position every `loss_exit.retry_interval_s` while the stop is latched.
     Check `loss_exit_orders_total` and `loss_exit_unpriced_total` (no fresh
     two-sided book: nothing is sent) and the `loss_exit_emit` log lines.
     To stop it, set `loss_exit.enabled: false` in
     `config/env/prod/strategy_limits.yaml` and send the engine SIGHUP
     (`docker compose kill -s HUP hft-engine`), which reloads the risk config;
     the switch is read on every tick.
   - Flatten remaining positions manually via broker UI if needed.
   - File incident report.
3. If PnL breach is spurious (e.g., stale price, missed fill):
   - Correct position state (see Manual Recovery below).
   - Resume after verification.

### Feed Gap

1. Check feed health:
   ```bash
   curl -s http://localhost:9090/metrics | grep hft_feed_last_tick_age_seconds
   ```
2. If feed is restored and age < threshold:
   - Clear halt via admin command (see below).
3. If feed remains stale:
   - Do NOT resume. Investigate broker connectivity.
   - Check broker API status page.

### Reconciliation Failure

1. Run full reconciliation:
   ```bash
   uv run hft admin reconcile --full
   ```
2. Identify mismatched symbols and quantities.
3. Apply corrections manually or via admin tool.
4. Re-run reconciliation to confirm zero diff.

---

## 4. Manual Recovery

### Step 1: Disable Auto-Flatten

Make sure the system does not automatically close positions during recovery. HALT auto-flatten is
**off by default**: the engine reads `HFT_HALT_AUTO_FLATTEN` (`src/hft_platform/risk/halt_flattener.py`)
and only `1`, `true`, `yes` or `on` enable it. Check that the engine environment does not set it to one
of those; leave it unset or `0`.

```bash
export HFT_HALT_AUTO_FLATTEN=0
```

### Step 2: Reconcile Positions

```bash
uv run hft admin reconcile --apply-corrections
```

Review each correction before confirming.

### Step 3: A restart is not a release

Do not restart the engine to clear a HALT. The four manual-release stops
(data loss, reconciliation mismatch, operator kill switch, daily loss limit)
are files on disk and survive a restart by design; everything else recovers on
its own. Any restart that is still needed follows
`.agent/rules/41-deployment.md` (stop, wait 60 s, start; never
`docker compose restart`).

### Step 4: Release the durable manual latches

```bash
hft risk status                      # kill switch + manual latches, each with its token
hft risk rearm daily_loss --token <token> --reason "<why it is safe>"
hft risk rearm reconciliation_mismatch --token <token> --reason "<why it is safe>"
hft risk resume --token <token> --reason "<why it is safe>"   # operator kill switch
```

Every release quotes the token `hft risk status` printed for that stop. The
token is derived from the latch record, so if the stop was rewritten after you
read it (a Telegram `/stop`, the engine's startup gate) the release is refused
and you read the status again. Latches live in `.state/manual_latches/`
(`HFT_MANUAL_LATCH_DIR`); each release appends a line to
`.state/manual_latches/audit.jsonl`, and a kill-switch release to
`.state/kill_switch_audit.jsonl`. The engine reads
the directory on every supervise tick, so a release takes effect without a
restart: HALT then de-escalates through the normal StormGuard cooldown. Watch
`manual_latch_active{latch=...}` go to 0.

`daily_loss` holds HALT until both the operator has released it and the
05:00 Taipei roll has cleared the day's stop; releasing it the same day does
not resume trading. While it is present, `loss_exit` keeps closing open
positions.

### Step 5: Clear Manual Re-Arm Lock

Only after reconciliation is clean, dependency health is green, and evidence capture is complete:

```bash
hft ops autonomy-status              # prints platform_token
hft ops rearm-platform --token <platform_token> --reason "<why it is safe>"
hft ops rearm-strategy --strategy-id <strategy_id>
```

The platform re-arm is appended to `rearm_audit.jsonl` beside
`runtime_state.json` before the flag is cleared.

The platform re-arm takes effect in the running engine on its next supervisor
tick (log `platform_reduce_only_force_cleared`); no restart is needed, and a
restart would re-arm everything that does not survive one.

If the platform remains in `HALT`, keep trading disabled and continue incident handling.

---

## 5. Post-Recovery Checklist

- [ ] Root cause identified and documented
- [ ] Position reconciliation shows zero diff
- [ ] StormGuard state is NORMAL
- [ ] `hft ops autonomy-status` shows no pending manual re-arm requirement
- [ ] PnL figures match between internal and broker
- [ ] Metrics pipeline is healthy (Prometheus scraping, Grafana panels green)
- [ ] WAL directory is not growing unboundedly
- [ ] ClickHouse inserts are succeeding
- [ ] Incident report filed (if PnL breach or data loss)
- [ ] Lessons learned appended to `.agent/memory/lessons_learned.md`
- [ ] Team notified via Slack/PagerDuty resolution
