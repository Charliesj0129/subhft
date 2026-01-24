# HFT Platform Runbooks

## Alert Responses

### `FeedGapCritical`
**Trigger**: No market data events received from shioaji for > 15s.
**Impact**: Strategies are flying blind.
**Response**:
1. Check `shijim_feed_connector` logs.
2. Verify Internet connectivity.
3. Check Shioaji API status page or dashboard.
4. Restart the connector service: `systemctl restart hft-platform`.

### `StormGuardHalt`
**Trigger**: A strategy's PnL drawdown exceeded the HALT threshold (e.g., -1,000,000 TWD).
**Impact**: Strategy is disabled. Can only Cancel orders.
**Response**:
1. Do **NOT** blindly restart.
2. Investigate the `audit.risk_log` to see what trades caused the loss.
3. Verify positions in `audit.positions_snapshot` vs Broker.
4. If false positive or resolved, manually reset state via CLI: `hft-cli risk reset --strategy <id>`.

### `RecorderFailure`
**Trigger**: ClickHouse ingestion errors.
**Impact**: Loss of historical data/audit logs. Trading continues but compliance risk increases.
**Response**:
1. Check ClickHouse disk space and service status.
2. Check `recorder.log` for connectivity errors.

## Config Management
- All changes to `config/*.yaml` must be committed to git.
- **Hot Reload**: Send `SIGHUP` to the main process to reload strategy parameters without restart (if enabled).
- **Audit**: Commit hash is logged on startup.

## Time Sync
- Check NTP/PTP status: `ops/time_sync_check.sh`
- Alert if clock drift exceeds 50ms.

## Disaster Drills (Fault Injection)
Run these in dev/staging first.

### Broker Disconnect
1. Block broker network access for 60s.
2. Verify reconnect count and gap metrics.
3. Confirm recovery without process restart.

### Market Data Stall
1. Pause feed adapter callbacks for 30s.
2. Verify `FeedGapCritical` triggers.
3. Resume and confirm queue drains.

### Disk Full (Recorder)
1. Fill data volume to >95%.
2. Verify recorder failure alert.
3. Free space and confirm recovery.

### Clock Drift
1. Simulate clock skew on host (dev VM only).
2. Verify timestamp sanity checks and alert.
