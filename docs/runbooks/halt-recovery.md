# HALT Recovery Runbook

## Overview

When StormGuard enters HALT state, all new order progression is blocked.
Cancel actions remain allowed. This runbook covers identification, verification,
and recovery from a HALT event.

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

Prevent the system from automatically closing positions during recovery.

```bash
export HFT_AUTO_FLATTEN_DISABLED=1
```

### Step 2: Reconcile Positions

```bash
uv run hft admin reconcile --apply-corrections
```

Review each correction before confirming.

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
export HFT_AUTO_FLATTEN_DISABLED=0
docker compose restart hft-engine
```

Monitor for 5 minutes before walking away.

---

## 5. Post-Recovery Checklist

- [ ] Root cause identified and documented
- [ ] Position reconciliation shows zero diff
- [ ] StormGuard state is NORMAL
- [ ] PnL figures match between internal and broker
- [ ] Metrics pipeline is healthy (Prometheus scraping, Grafana panels green)
- [ ] WAL directory is not growing unboundedly
- [ ] ClickHouse inserts are succeeding
- [ ] Incident report filed (if PnL breach or data loss)
- [ ] Lessons learned appended to `.agent/memory/lessons_learned.md`
- [ ] Team notified via Slack/PagerDuty resolution
