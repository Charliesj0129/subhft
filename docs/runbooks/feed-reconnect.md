# Runbook: Feed Reconnect

## Trigger

- Prometheus alert `FeedGapCritical` fires (no tick data for > `HFT_QUOTE_NO_DATA_S` seconds).
- StormGuard transitions to STORM due to feed gap >= `HFT_STORMGUARD_FEED_GAP_HALT_S` (default 1.0s).
- Shioaji watchdog thread detects stale quotes (`HFT_QUOTE_WATCHDOG_S`, default 5s).
- Manual operator observation of zero ingest rate on Grafana dashboard.

## Impact

- **Market data pipeline halted**: No tick/bidask events flow to strategies.
- **StormGuard escalation**: Feed gap triggers STORM state; new orders are blocked.
- **Recording gap**: ClickHouse `market_data` table has a hole for the outage period.
- **Strategies go stale**: Alpha signals computed on stale LOB data produce no new intents.

## Diagnosis

### 1. Confirm feed is actually down

```bash
# Check engine logs for quote watchdog or reconnect messages
docker compose logs --tail=100 hft-engine | grep -E "watchdog|reconnect|no_data|feed_gap"

# Check Prometheus metrics
curl -s http://localhost:9090/metrics | grep -E "feed_reconnect_total|feed_gap|quote_watchdog"

# Check StormGuard state (0=NORMAL, 1=WARM, 2=STORM, 3=HALT)
curl -s http://localhost:9090/metrics | grep stormguard_mode
```

### 2. Check Shioaji API status

```bash
# Look for Shioaji SDK errors
docker compose logs --tail=200 hft-engine | grep -iE "shioaji|sj\.|ConnectionError|SSL|timeout"

# Check if login is still valid
docker compose logs --tail=50 hft-engine | grep -E "logged_in|login_failed|login_error"
```

### 3. Check network connectivity

```bash
# From inside the container
docker exec hft-engine wget --spider --timeout=5 https://api.sinopac.com 2>&1 || echo "UNREACHABLE"
```

## Resolution

### Automatic recovery (default behavior)

The `ReconnectOrchestrator` handles automatic reconnect with exponential backoff:

- Cooldown: `HFT_RECONNECT_COOLDOWN` (default 30s)
- Backoff: starts at `HFT_RECONNECT_BACKOFF_S` (default 30s), doubles up to `HFT_RECONNECT_BACKOFF_MAX_S`
- Sequence: logout -> login -> register callbacks -> subscribe basket
- Flap protection: `HFT_QUOTE_FLAP_THRESHOLD` (default 5) reconnects within `HFT_QUOTE_FLAP_WINDOW_S` (default 60s) triggers cooldown of `HFT_QUOTE_FLAP_COOLDOWN_S` (default 300s)

Wait 2-3 minutes for automatic recovery. Monitor:

```bash
docker compose logs -f hft-engine | grep -E "reconnect|Reconnecting|subscribe_basket"
```

### Manual reconnect (if automatic fails)

```bash
# Restart the engine container (preserves WAL, triggers fresh login)
docker compose restart hft-engine

# If login keeps failing, check credentials
docker exec hft-engine env | grep SHIOAJI_
```

### Scheduled reconnect

For known Shioaji session expiry, configure scheduled reconnects:

```bash
# In .env — reconnect at 08:30 and 12:30 Taipei time
HFT_RECONNECT_HOURS=8:30
HFT_RECONNECT_HOURS_2=12:30
HFT_RECONNECT_TZ=Asia/Taipei
```

### Nuclear option: full restart

```bash
docker compose down hft-engine
docker compose up -d hft-engine
```

## Rollback

No configuration rollback needed. If reconnect parameters were changed:

```bash
# Revert .env changes and restart
docker compose restart hft-engine
```

## Post-Incident

1. **Check recording gap**:
   ```bash
   docker exec clickhouse clickhouse-client \
     --password "${CLICKHOUSE_PASSWORD}" \
     --query "SELECT toStartOfMinute(toDateTime64(exch_ts/1e9, 3)) AS minute, count()
              FROM hft.market_data
              WHERE toDate(exch_ts/1e9) = today()
              GROUP BY minute ORDER BY minute"
   ```

2. **Verify WAL files were written during outage** (recorder continues if engine was up):
   ```bash
   ls -lt .wal/*.jsonl | head -10
   ```

3. **Check reconnect metrics for patterns**:
   ```bash
   curl -s http://localhost:9090/metrics | grep feed_reconnect
   ```

4. **Update monitoring thresholds** if false positives occurred.

5. **File incident report** if outage lasted > 5 minutes during trading hours.
