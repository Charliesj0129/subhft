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

No CLI queries broker positions (there is no `hft admin` command group). Read them in the broker's own UI or
API console. Do not open a second Shioaji session from a shell: the broker cap is 5 sessions.

### Reconciliation Diff

Reconciliation is done by the engine, not by a command. `ReconciliationService`
(`src/hft_platform/execution/reconciliation.py`) syncs positions from the broker at startup and then every
`HFT_RECON_CHECK_INTERVAL` seconds (default 5). Read its result from the engine:

```bash
curl -s http://localhost:9090/metrics | grep -E 'reconciliation_(discrepancy_count|consecutive_failures|last_success_ts|auto_corrected_total)'
```

The metric names carry the `HFT_METRICS_PREFIX` prefix when one is set. The engine log names the symbols:
`Position discrepancies detected`, `auto_correct_drift: adopting broker position`,
`Runtime reconciliation failed`.

If discrepancies exist, record them before proceeding.

---

## 3. Decision Tree

### PnL Breach

1. Confirm realized + unrealized PnL from both internal and broker sources.
2. If PnL breach is genuine:
   - Do NOT resume trading.
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
   - Clear the halt following Manual Recovery below.
3. If feed remains stale:
   - Do NOT resume. Investigate broker connectivity.
   - Check broker API status page.

### Reconciliation Failure

1. Read the engine's reconciliation state (see "Reconciliation Diff" above):
   `reconciliation_discrepancy_count` and `reconciliation_consecutive_failures`.
2. Identify mismatched symbols and quantities from the `Position discrepancies detected` log lines.
3. No command applies corrections by hand. The engine adopts the broker position itself for drift it classes
   as correctable (`auto_correct_drift: adopting broker position`, counted in
   `reconciliation_auto_corrected_total`).
4. Wait one cycle and confirm `reconciliation_discrepancy_count` is back to 0.

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

No command applies corrections by hand; the engine reconciles itself (see "Reconciliation Diff"). Confirm
`reconciliation_discrepancy_count` and `reconciliation_consecutive_failures` are both 0 before continuing.
If the broker and the engine still disagree after a cycle, the broker is the source of truth: do not resume,
and file an incident.

### Step 3: Restart in Sim Mode

```bash
export HFT_MODE=sim
docker compose restart hft-engine
```

Verify:
- StormGuard state returns to NORMAL
- Metrics are flowing
- No error logs

### Step 4: Resume Live (if appropriate)

```bash
export HFT_MODE=live
# Leave HFT_HALT_AUTO_FLATTEN as set in Step 1 unless you deliberately want HALT auto-flatten on.
docker compose restart hft-engine
```

Monitor for 5 minutes before walking away.

### Step 5: Clear Manual Re-Arm Lock

Only after reconciliation is clean, dependency health is green, and evidence capture is complete:

```bash
hft ops autonomy-status
hft ops rearm-platform
hft ops rearm-strategy --strategy-id <strategy_id>
```

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
