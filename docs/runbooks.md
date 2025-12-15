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
